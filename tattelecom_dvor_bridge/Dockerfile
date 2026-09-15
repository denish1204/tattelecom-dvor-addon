ARG BUILD_FROM
FROM $BUILD_FROM

# Python runtime
RUN apk add --no-cache python3 py3-pip

COPY requirements.txt /requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /requirements.txt

COPY rootfs /
COPY app.py /app.py

RUN chmod a+x /etc/services.d/tattelecom_dvor_bridge/run
