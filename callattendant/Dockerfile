# https://developers.home-assistant.io/docs/add-ons/configuration#add-on-dockerfile
ARG BUILD_FROM
FROM $BUILD_FROM

# Execute during the build of the image
ARG TEMPIO_VERSION BUILD_ARCH

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN curl -sSLf -o /usr/bin/tempio \
    "https://github.com/home-assistant/tempio/releases/download/${TEMPIO_VERSION}/tempio_${BUILD_ARCH}"
RUN \
     apk add   python3  py3-pip py3-lxml nginx gettext --no-cache \
     && rm -f -r \
      /etc/nginx \
      /tmp/*

# Crea le directory necessarie per nginx e imposta i permessi corretti
RUN \
    case "${BUILD_ARCH}" in \
        "armv7") \
             mkdir -p /run/nginx \
             && mkdir -p /var/lib/nginx/logs \
             && mkdir -p /var/lib/nginx/tmp/client_body \
             && chown -R root:root /run/nginx /var/lib/nginx /var/log/nginx \
            ;; \
        "aarch64") \
             mkdir -p /run/nginx \
             && mkdir -p /var/lib/nginx/logs \
             && mkdir -p /var/lib/nginx/tmp/client_body \
             && chown -R root:root /run/nginx /var/lib/nginx /var/log/nginx \
            ;; \
        *) \
            echo "Architettura non gestita: ${BUILD_ARCH}, eseguo comandi di default..." \
            ;; \
    esac


COPY rootfs /
COPY requirements.txt callattendant



RUN mkdir -p /home/callattendant/.venv \
    && python3 -m venv /home/callattendant/.venv \
    && /home/callattendant/.venv/bin/pip install --upgrade pip \
    && /home/callattendant/.venv/bin/pip install -r callattendant/requirements.txt

COPY app.cfg /home/callattendant

WORKDIR callattendant
