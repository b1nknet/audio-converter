FROM python:3.14-alpine

RUN apk add --no-cache ffmpeg tzdata

WORKDIR /app

COPY audio_convert.py docker-entrypoint.sh run-converter.sh /app/

ENTRYPOINT ["/bin/sh", "/app/docker-entrypoint.sh"]
