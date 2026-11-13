#!/usr/bin/env python3
"""Tattelecom Dvor Bridge — держит live-потоки камер «Безопасный двор»
(newlk.letai.ru) живыми в go2rtc.

РЕЖИМ: ЛЕНИВЫЙ РЕФРЕШ (с 2026-11-13).
Каждый цикл мост НЕ дёргает API Летая вслепую. Он:
  1. пробует текущий flussonic-токен лёгким GET плейлиста прямо у медиасервера
     (kzn-vn-strm40.tattelecom.ru) — это НЕ хрупкий API ЛК;
  2. если токен жив И go2rtc всё ещё держит стрим → ничего не делает;
  3. только если поток реально умер (токен протух) или go2rtc потерял стрим —
     перевыпускает ссылку через API Летая и пушит в go2rtc.
Так обращений к ломкому API кратно меньше → меньше шансов налететь на их
очередное изменение, и разовые сбои API не трогают живой поток.

История багов API Летая (почему тут столько «обвязки»):
- 2026-10: get-live-video требует ДВА РАЗНЫХ одноразовых product-токена
  (URL ?product_token= ≠ заголовок product-token:).
- 2026-11-10: требует COOKIE (token=<session_token>; tokenDevice=…).
- 2026-11-13: требует заголовок `Version: 300`.
session_token (Web_long_lived) живёт ~2-3 нед, протух → 401/419 → нужен
ручной перелогин по SMS (автоматизировать нельзя). Ловим и орём в лог.
"""

import json
import logging
import sys
import time
import urllib.parse

import requests

OPTIONS_PATH = "/data/options.json"
BASE = "https://newlk.letai.ru"
PRODUCT_CODE = "safeyard"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/154.0.0.0 Safari/537.36"
)
API_VERSION = "300"          # заголовок Version, требуется с 2026-11-13
HTTP_TIMEOUT = 20
PROBE_TIMEOUT = 10           # лёгкая проверка плейлиста у медиасервера
RETRIES = 3
RETRY_BACKOFF = 4
HEARTBEAT_EVERY = 12         # раз в сколько «тихих» циклов писать, что всё живо

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("dvor")


class AuthExpired(Exception):
    """session_token протух — нужен ручной перелогин в ЛК по SMS."""


def load_options():
    with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
        o = json.load(f)
    o["session_token"] = str(o.get("session_token", "")).strip()
    o["account_number"] = str(o.get("account_number", "")).strip()
    o["go2rtc_url"] = str(o.get("go2rtc_url", "")).strip().rstrip("/")
    o["refresh_interval_minutes"] = int(o.get("refresh_interval_minutes", 5))
    o["cameras"] = o.get("cameras", []) or []
    o["cookie"] = str(o.get("cookie", "") or "").strip()
    o["api_version"] = str(o.get("api_version", "") or API_VERSION).strip()
    return o


def extract_token_device(cookie_raw):
    for part in cookie_raw.split(";"):
        part = part.strip()
        if part.startswith("tokenDevice="):
            return part[len("tokenDevice="):].strip()
    return ""


def build_cookie(opts):
    jar = {"token": urllib.parse.quote(opts["session_token"], safe="")}
    dev = extract_token_device(opts["cookie"])
    if dev:
        jar["tokenDevice"] = dev
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def base_headers(opts):
    return {
        "Authorization": f"Bearer {opts['session_token']}",
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Version": opts["api_version"],
        "Cookie": build_cookie(opts),
        "sec-ch-ua": '"Chromium";v="154", "Google Chrome";v="154", "Not A(Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }


# ---------- API Летая (вызывается ТОЛЬКО когда поток реально надо перевыпустить) ----------

def get_product_token(session, opts):
    r = session.get(
        f"{BASE}/v3/auth/get-product-token",
        params={"account_number": opts["account_number"], "product_code": PRODUCT_CODE},
        headers=base_headers(opts),
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code in (401, 419):
        raise AuthExpired(f"get-product-token {r.status_code}: {r.text[:160]}")
    r.raise_for_status()
    token = (r.json() or {}).get("token")
    if not token:
        raise RuntimeError(f"empty product_token: {r.text[:160]}")
    return token


def get_live_url(session, opts, cam_id):
    t_url = get_product_token(session, opts)   # два РАЗНЫХ одноразовых токена
    t_hdr = get_product_token(session, opts)
    n = 0
    while t_hdr == t_url and n < 3:
        t_hdr = get_product_token(session, opts); n += 1

    headers = base_headers(opts)
    headers["product-token"] = t_hdr
    headers["Referer"] = f"{BASE}/safe-yard/{cam_id}/{opts['account_number']}"
    r = session.get(
        f"{BASE}/v3/safeyard/get-live-video",
        params={"cam_id": cam_id, "product_token": t_url},
        headers=headers, timeout=HTTP_TIMEOUT,
    )
    if r.status_code in (401, 419):
        raise AuthExpired(f"get-live-video {r.status_code}: {r.text[:160]}")
    if r.status_code != 200:
        raise RuntimeError(f"get-live-video {r.status_code}: {r.text[:160]} (ver={opts['api_version']})")
    live = (r.json() or {}).get("live")
    if not live:
        raise RuntimeError(f"no 'live' in response: {r.text[:160]}")
    return live


# ---------- go2rtc ----------

def push_to_go2rtc(session, go2rtc_url, name, src):
    r = session.put(f"{go2rtc_url}/api/streams", params={"name": name, "src": src}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()


def go2rtc_has_stream(session, go2rtc_url, name):
    try:
        r = session.get(f"{go2rtc_url}/api/streams", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return name in (r.json() or {})
    except Exception as e:  # noqa: BLE001
        log.warning("go2rtc check failed (%s) — считаю, что стрим надо перепушить", e)
        return False


# ---------- Лёгкая проверка, жив ли flussonic-токен ----------

def probe_stream_alive(session, live_url):
    """GET плейлиста у медиасервера тем же токеном. True = токен ещё валиден.
    Строим HLS-плейлист из любой формы live-URL (mpegts/index.m3u8/…)."""
    try:
        p = urllib.parse.urlsplit(live_url)
        token = urllib.parse.parse_qs(p.query).get("token", [""])[0]
        if not token:
            return False
        base_path = p.path.rsplit("/", 1)[0]  # отбрасываем mpegts / index.m3u8
        probe = urllib.parse.urlunsplit(
            (p.scheme, p.netloc, f"{base_path}/index.m3u8", f"token={urllib.parse.quote(token)}", "")
        )
        r = session.get(probe, headers={"User-Agent": UA, "Referer": BASE + "/"}, timeout=PROBE_TIMEOUT)
        return r.status_code == 200 and "#EXTM3U" in r.text[:64]
    except Exception as e:  # noqa: BLE001
        log.info("probe error (%s) — буду перевыпускать ссылку", e)
        return False


# ---------- Цикл по камере ----------

def ensure_camera(session, opts, cam, last_urls):
    """Ленивый рефреш одной камеры. Возвращает 'alive' | 'refreshed' | 'failed'.
    AuthExpired поднимается наружу."""
    name = cam["stream_name"]
    live = last_urls.get(name)

    # 1) если знаем ссылку, стрим есть в go2rtc и токен жив — ничего не делаем
    if live and go2rtc_has_stream(session, opts["go2rtc_url"], name) and probe_stream_alive(session, live):
        return "alive"

    # 2) иначе — перевыпуск через API Летая (с ретраями)
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            new_live = get_live_url(session, opts, cam["cam_id"])
            push_to_go2rtc(session, opts["go2rtc_url"], name, new_live)
            last_urls[name] = new_live
            log.info("[%s] refreshed OK", name)
            return "refreshed"
        except AuthExpired:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    log.error("[%s] refresh failed after %d tries: %s", name, RETRIES, last_err)
    return "failed"


def main():
    log.info("Starting Tattelecom Dvor Bridge... (lazy refresh, Version=%s)", API_VERSION)
    session = requests.Session()
    last_urls = {}      # stream_name -> последний рабочий live URL (в памяти)
    auth_alerted = False
    quiet = 0           # счётчик «тихих» циклов для редкого heartbeat
    while True:
        interval = 5 * 60
        try:
            opts = load_options()
            interval = max(1, opts["refresh_interval_minutes"]) * 60
            try:
                results = [ensure_camera(session, opts, c, last_urls) for c in opts["cameras"]]
                auth_alerted = False
                if any(r == "refreshed" or r == "failed" for r in results):
                    quiet = 0  # было событие — его уже залогировали
                else:
                    quiet += 1
                    if quiet >= HEARTBEAT_EVERY:
                        log.info("alive: все потоки живы, токены валидны (%d камер)", len(results))
                        quiet = 0
            except AuthExpired as e:
                if not auth_alerted:
                    log.error(
                        "=== SESSION TOKEN EXPIRED (%s). Нужен ручной перелогин в ЛК "
                        "newlk.letai.ru по SMS и обновление опции session_token аддона. "
                        "Пока go2rtc держит старый поток — камеры живут. ===", e)
                    auth_alerted = True
                interval = 10 * 60
        except Exception as e:  # noqa: BLE001
            log.error("Cycle failed: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
