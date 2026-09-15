import json
import logging
import sys
import time

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tattelecom_dvor_bridge")

OPTIONS_PATH = "/data/options.json"

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}


def load_options():
    with open(OPTIONS_PATH, "r") as f:
        return json.load(f)


def get_product_token(session_token: str, account_number: str) -> str:
    url = "https://newlk.letai.ru/v3/auth/get-product-token"
    params = {"account_number": account_number, "product_code": "safeyard"}
    headers = dict(BASE_HEADERS)
    headers["Authorization"] = f"Bearer {session_token}"
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()["token"]


def get_live_video(
    session_token: str,
    product_token: str,
    cam_id: str,
    account_number: str,
    cookie: str = "",
) -> str:
    url = "https://newlk.letai.ru/v3/safeyard/get-live-video"
    params = {"cam_id": cam_id, "product_token": product_token}
    headers = dict(BASE_HEADERS)
    headers["Authorization"] = f"Bearer {session_token}"
    headers["product-token"] = product_token
    # Without a matching Referer, newlk.letai.ru answers every request with a
    # generic HTTP 400 (same anti-bot check that broke the old rest_command).
    headers["Referer"] = f"https://newlk.letai.ru/safe-yard/{cam_id}/{account_number}"
    if cookie:
        # /v3/safeyard/* (unlike /v3/auth/*) started enforcing a browser
        # cookie session on top of the Authorization header — without it,
        # every call gets a generic HTTP 400 regardless of how correct the
        # other headers are. Grab this from DevTools -> Network -> Cookie
        # header of any request to newlk.letai.ru.
        headers["Cookie"] = cookie
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    if not resp.ok:
        log.error(
            f"get-live-video {resp.status_code} body={resp.text[:500]!r} "
            f"referer={headers['Referer']!r} "
            f"auth_len={len(headers['Authorization'])} "
            f"product_token_len={len(product_token)}"
        )
    resp.raise_for_status()
    return resp.json()["live"]


def push_to_go2rtc(go2rtc_url: str, stream_name: str, src: str) -> None:
    url = f"{go2rtc_url.rstrip('/')}/api/streams"
    resp = requests.put(url, params={"name": stream_name, "src": src}, timeout=15)
    resp.raise_for_status()


def run_cycle(opts: dict) -> None:
    session_token = opts["session_token"]
    account_number = opts["account_number"]
    go2rtc_url = opts["go2rtc_url"]
    cookie = opts.get("cookie", "")
    cameras = opts.get("cameras", [])

    if not session_token or not account_number:
        log.warning("session_token / account_number not set yet — skipping cycle.")
        return

    if not cameras:
        log.warning("Camera list is empty — nothing to refresh.")
        return

    product_token = get_product_token(session_token, account_number)
    log.info("Got product_token OK")

    for cam in cameras:
        cam_id = cam["cam_id"]
        stream_name = cam["stream_name"]
        try:
            live_url = get_live_video(
                session_token, product_token, cam_id, account_number, cookie
            )
            push_to_go2rtc(go2rtc_url, stream_name, live_url)
            log.info(f"[{stream_name}] refreshed OK")
        except Exception as exc:  # noqa: BLE001 - log and keep going with other cameras
            log.error(f"[{stream_name}] failed: {exc}")


def main() -> None:
    opts = load_options()
    interval_seconds = max(1, int(opts.get("refresh_interval_minutes", 15))) * 60

    while True:
        try:
            opts = load_options()
            run_cycle(opts)
        except Exception as exc:  # noqa: BLE001 - never let the loop die
            log.error(f"Cycle failed: {exc}")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
