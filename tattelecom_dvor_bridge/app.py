#!/usr/bin/env python3
"""Tattelecom Dvor Bridge — держит live-потоки камер «Безопасный двор»
(newlk.letai.ru) живыми в go2rtc, перевыпуская ссылки по расписанию.

История багов API Tattelecom (почему тут столько «обвязки»):
- 2026-10: get-live-video стал требовать ДВА РАЗНЫХ одноразовых product-токена
  (URL ?product_token= ≠ заголовок product-token:). Один в оба → 400 «Отсутствует
  токен авторизации».
- 2026-11-10: get-live-video стал требовать COOKIE (token=<session_token>;
  tokenDevice=…). Без cookie → 400 «Что-то пошло не так…».
- 2026-11-13: API стал требовать заголовок `Version: 300`. Без него → 400
  «…попробуйте через пару минут». (SERVER_frontend оказался НЕ нужен — выкинут.)

Устойчивость:
- Точно повторяем браузер (Version, cookie, UA, Referer, два токена).
- Ретраи с бэкоффом: один 400/таймаут не роняет камеру.
- Неудачный рефреш НЕ трогает уже живой поток в go2rtc (push только при успехе).
- 401/419 = протух session_token (Web_long_lived, ~2-3 нед) — ЕДИНСТВЕННОЕ, что
  требует человека: пишем явный ALERT в лог. Снаружи за этим следит HA-автоматизация
  «камеры в unavailable → пуш».
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
# Заголовок версии клиента веб-ЛК. Требуется с 2026-11-13. Если снова начнёт
# отдавать 400 на get-live-video при живом токене — проверить актуальное значение
# в DevTools (заголовок Version на запросах к newlk.letai.ru) и обновить тут
# или задать опцию api_version в конфиге аддона.
API_VERSION = "300"
HTTP_TIMEOUT = 20
RETRIES = 3          # попыток на камеру за цикл
RETRY_BACKOFF = 4    # секунд между попытками (растёт линейно)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("dvor")


class AuthExpired(Exception):
    """session_token протух — нужен ручной перелогин в ЛК."""


def load_options():
    with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
        opts = json.load(f)
    opts["session_token"] = str(opts.get("session_token", "")).strip()
    opts["account_number"] = str(opts.get("account_number", "")).strip()
    opts["go2rtc_url"] = str(opts.get("go2rtc_url", "")).strip().rstrip("/")
    opts["refresh_interval_minutes"] = int(opts.get("refresh_interval_minutes", 15))
    opts["cameras"] = opts.get("cameras", []) or []
    opts["cookie"] = str(opts.get("cookie", "") or "").strip()
    opts["api_version"] = str(opts.get("api_version", "") or API_VERSION).strip()
    return opts


def extract_token_device(cookie_raw):
    """Берём только статический tokenDevice из опции cookie (остальное неважно)."""
    for part in cookie_raw.split(";"):
        part = part.strip()
        if part.startswith("tokenDevice="):
            return part[len("tokenDevice="):].strip()
    return ""


def build_cookie(opts):
    """Минимальный рабочий cookie: token (= session_token, URL-encoded) + tokenDevice."""
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


def get_product_token(session, opts):
    """Один одноразовый product-токен. Сервер выдаёт новый на каждый вызов."""
    r = session.get(
        f"{BASE}/v3/auth/get-product-token",
        params={"account_number": opts["account_number"], "product_code": PRODUCT_CODE},
        headers=base_headers(opts),
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code in (401, 419):
        raise AuthExpired(f"get-product-token {r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    token = (r.json() or {}).get("token")
    if not token:
        raise RuntimeError(f"empty product_token: {r.text[:200]}")
    return token


def get_live_url(session, opts, cam_id):
    # ДВА РАЗНЫХ одноразовых токена: один в URL, другой в заголовок.
    t_url = get_product_token(session, opts)
    t_hdr = get_product_token(session, opts)
    tries = 0
    while t_hdr == t_url and tries < 3:
        t_hdr = get_product_token(session, opts)
        tries += 1

    headers = base_headers(opts)
    headers["product-token"] = t_hdr
    headers["Referer"] = f"{BASE}/safe-yard/{cam_id}/{opts['account_number']}"

    r = session.get(
        f"{BASE}/v3/safeyard/get-live-video",
        params={"cam_id": cam_id, "product_token": t_url},
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code in (401, 419):
        raise AuthExpired(f"get-live-video {r.status_code}: {r.text[:200]}")
    if r.status_code != 200:
        raise RuntimeError(
            f"get-live-video {r.status_code}: {r.text[:200]} "
            f"(ver={opts['api_version']} ptlen={len(t_url)} hdrlen={len(t_hdr)})"
        )
    live = (r.json() or {}).get("live")
    if not live:
        raise RuntimeError(f"no 'live' in response: {r.text[:200]}")
    return live


def push_to_go2rtc(session, go2rtc_url, stream_name, src_url):
    r = session.put(
        f"{go2rtc_url}/api/streams",
        params={"name": stream_name, "src": src_url},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()


def refresh_camera(session, opts, cam):
    """Рефреш одной камеры с ретраями. Поднимает AuthExpired наружу (общая причина)."""
    stream_name = cam["stream_name"]
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            live = get_live_url(session, opts, cam["cam_id"])
            push_to_go2rtc(session, opts["go2rtc_url"], stream_name, live)
            log.info("[%s] refreshed OK", stream_name)
            return True
        except AuthExpired:
            raise  # не ретраим — токен не починится повтором
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    # все попытки мимо — поток в go2rtc НЕ трогаем, он мог остаться живым
    log.error("[%s] failed after %d tries: %s", stream_name, RETRIES, last_err)
    return False


def main():
    log.info("Starting Tattelecom Dvor Bridge... (API Version=%s)", API_VERSION)
    session = requests.Session()
    auth_alerted = False
    while True:
        interval = 15 * 60
        try:
            opts = load_options()
            interval = max(1, opts["refresh_interval_minutes"]) * 60
            try:
                for cam in opts["cameras"]:
                    refresh_camera(session, opts, cam)
                auth_alerted = False
            except AuthExpired as e:
                # Единственное, что требует человека. Орём в лог заметно и редко.
                if not auth_alerted:
                    log.error(
                        "=== SESSION TOKEN EXPIRED (%s). Нужен ручной перелогин в ЛК "
                        "newlk.letai.ru и обновление опции session_token аддона. "
                        "Камеры будут жить на старом потоке, пока go2rtc его держит. ===",
                        e,
                    )
                    auth_alerted = True
                interval = 10 * 60  # не долбим сервер протухшим токеном
        except Exception as e:  # noqa: BLE001
            log.error("Cycle failed: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
