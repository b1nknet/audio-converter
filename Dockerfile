FROM python:3.14-alpine

RUN apk add --no-cache ffmpeg tzdata && pip install --no-cache-dir flask

WORKDIR /app

COPY audio_convert.py webapp.py docker-entrypoint.sh run-converter.sh /app/

EXPOSE 8000

ENTRYPOINT ["/bin/sh", "/app/docker-entrypoint.sh"]
