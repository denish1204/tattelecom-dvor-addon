#!/usr/bin/env python3
"""Tattelecom Dvor Bridge — держит live-потоки камер «Безопасный двор»
(newlk.letai.ru) живыми в go2rtc, перевыпуская ссылки по расписанию.

История багов API (важно при отладке):
- 2026-10: get-live-video стал требовать ДВА РАЗНЫХ одноразовых product-токена
  (один в URL ?product_token=, другой в заголовке product-token:). Один и тот же
  в оба слота → 400 «Отсутствует токен авторизации». Исправлено: минтим два токена.
- 2026-11: get-live-video стал требовать COOKIE. Без cookie → 400 «Что-то пошло
  не так… попробуйте через пару минут» (иногда 502 от узла app-2). Исправлено:
  шлём cookie на все запросы; token в cookie = session_token; SERVER_frontend
  закреплён на app-1 (app-2 отдавал 400/502).
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
# Узел балансировщика Tattelecom. app-2 замечен в отдаче 400/502 (ноя 2026) —
# закрепляемся на app-1. Если и он ляжет — сменить тут и в опции cookie.
SERVER_FRONTEND = "app-1"
HTTP_TIMEOUT = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("dvor")


def load_options():
    with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
        opts = json.load(f)
    opts["session_token"] = str(opts.get("session_token", "")).strip()
    opts["account_number"] = str(opts.get("account_number", "")).strip()
    opts["go2rtc_url"] = str(opts.get("go2rtc_url", "")).strip().rstrip("/")
    opts["refresh_interval_minutes"] = int(opts.get("refresh_interval_minutes", 15))
    opts["cameras"] = opts.get("cameras", []) or []
    opts["cookie"] = str(opts.get("cookie", "") or "").strip()
    return opts


def build_cookie_header(opts):
    """Cookie для запросов к newlk.letai.ru. Берём статику (tokenDevice и пр.)
    из опции cookie, но token синхронизируем с session_token, а SERVER_frontend
    закрепляем на рабочем узле — чтобы при ротации токена править одно место."""
    jar = {}
    for part in opts["cookie"].split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    # актуальный session_token в cookie, URL-encoded (как шлёт браузер: ':' → %3A)
    jar["token"] = urllib.parse.quote(opts["session_token"], safe="")
    jar["SERVER_frontend"] = SERVER_FRONTEND
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def base_headers(opts):
    h = {
        "Authorization": f"Bearer {opts['session_token']}",
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
    }
    cookie = build_cookie_header(opts)
    if cookie:
        h["Cookie"] = cookie
    return h


def get_product_token(session, opts):
    """Один одноразовый product-токен. Сервер выдаёт новый на каждый вызов."""
    r = session.get(
        f"{BASE}/v3/auth/get-product-token",
        params={"account_number": opts["account_number"], "product_code": PRODUCT_CODE},
        headers=base_headers(opts),
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    token = r.json()["token"]
    if not token:
        raise RuntimeError(f"empty product_token in response: {r.text!r}")
    return token


def get_live_url(session, opts, cam_id):
    # ДВА РАЗНЫХ одноразовых токена: один в URL, другой в заголовок.
    t_url = get_product_token(session, opts)
    t_hdr = get_product_token(session, opts)
    tries = 0
    while t_hdr == t_url and tries < 3:
        t_hdr = get_product_token(session, opts)
        tries += 1
    log.info("Got product_token OK (x2)")

    headers = base_headers(opts)
    headers["product-token"] = t_hdr
    headers["Referer"] = f"{BASE}/safe-yard/{cam_id}/{opts['account_number']}"

    r = session.get(
        f"{BASE}/v3/safeyard/get-live-video",
        params={"cam_id": cam_id, "product_token": t_url},
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        log.error(
            "get-live-video %s body=%r referer=%r product_token_len=%d header_token_len=%d cookie=%s",
            r.status_code,
            r.text[:300],
            headers["Referer"],
            len(t_url),
            len(t_hdr),
            "yes" if "Cookie" in headers else "no",
        )
        r.raise_for_status()

    live = r.json().get("live")
    if not live:
        raise RuntimeError(f"no 'live' in response: {r.text!r}")
    return live


def push_to_go2rtc(session, go2rtc_url, stream_name, src_url):
    """PUT /api/streams?name=<stream>&src=<live_url> — добавить/обновить поток."""
    r = session.put(
        f"{go2rtc_url}/api/streams",
        params={"name": stream_name, "src": src_url},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()


def refresh_camera(session, opts, cam):
    stream_name = cam["stream_name"]
    try:
        live = get_live_url(session, opts, cam["cam_id"])
        push_to_go2rtc(session, opts["go2rtc_url"], stream_name, live)
        log.info("[%s] refreshed OK", stream_name)
    except Exception as e:  # noqa: BLE001 — не валим весь цикл из-за одной камеры
        log.error("[%s] failed: %s", stream_name, e)


def main():
    log.info("Starting Tattelecom Dvor Bridge...")
    session = requests.Session()
    while True:
        try:
            opts = load_options()
            interval = max(1, opts["refresh_interval_minutes"]) * 60
            for cam in opts["cameras"]:
                refresh_camera(session, opts, cam)
        except Exception as e:  # noqa: BLE001
            log.error("Cycle failed: %s", e)
            interval = 15 * 60
        time.sleep(interval)


if __name__ == "__main__":
    main()
