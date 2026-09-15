# Tattelecom Dvor Bridge

Аддон обновляет ссылки на live-потоки камер платформы Tattelecom
«Безопасный двор» (`newlk.letai.ru`, work за Flussonic) и пушит их в
`go2rtc`, чтобы Home Assistant / Frigate всегда получали рабочий URL,
даже когда ссылка периодически перевыпускается платформой.

## Настройки (Configuration)

- **session_token** — долгоживущий сессионный токен вида
  `478677:6aa14ae7ae08e`. Получить его можно один раз, залогинившись
  в веб-версию «Безопасного двора» (newlk.letai.ru) через браузер и
  скопировав заголовок `Authorization: Bearer ...` из DevTools →
  Network → любой запрос к `newlk.letai.ru`. Токен живёт долго (дни),
  но не вечно — если аддон вдруг перестанет обновлять потоки, скорее
  всего протух именно он, и его нужно обновить вручную тем же
  способом (полностью автоматизировать это нельзя — вход защищён
  SMS-2FA).
- **account_number** — номер лицевого счёта (виден в том же
  DevTools, параметр `account_number` в запросах).
- **refresh_interval_minutes** — как часто дёргать API и обновлять
  поток (по умолчанию 15 минут).
- **go2rtc_url** — адрес go2rtc, куда пушить свежие ссылки. Если
  используется go2rtc, встроенный в аддон Frigate — это внутренний
  IP контейнера Frigate и порт 1984 (пример:
  `http://172.30.33.7:1984`). Уточнить IP можно в логах Frigate или
  через `docker inspect` соответствующего контейнера.
- **cameras** — список камер вида:
  ```yaml
  - cam_id: "4e0fced3973b382ce53f38f0ed4245c3"
    stream_name: "dvor_parkovka_9"
  ```
  `cam_id` берётся из ответа `GET /v3/safeyard/house-camera-list`
  (поле `id` у нужной камеры). `stream_name` — как поток будет
  называться внутри go2rtc (используй его же в `rtsp://127.0.0.1:8554/<stream_name>`
  при настройке источника в Frigate, или в HA Generic Camera).

## Как аддон работает

Раз в `refresh_interval_minutes`:
1. `GET /v3/auth/get-product-token` с `session_token` → получает
   `product_token`.
2. Для каждой камеры из списка — `GET /v3/safeyard/get-live-video`
   (с `Authorization` **и** заголовком `product-token`, оба
   обязательны) → получает свежий `live` URL (mpegts-поток).
3. `PUT <go2rtc_url>/api/streams?name=<stream_name>&src=<live URL>` —
   обновляет источник потока в go2rtc.

Дальше go2rtc и всё, что от него зависит (Frigate, HA Generic
Camera через `rtsp://127.0.0.1:8554/<stream_name>`), продолжают
работать бесшовно — им не нужно ничего знать про ротацию токена.

## Логи

Смотри вкладку **Log** аддона — там видно, какие камеры успешно
обновились, а какие упали с ошибкой (и текстом ошибки от API).
