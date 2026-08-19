#!/bin/sh
set -eu

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Starting audio conversion"

# CONVERTER_ARGS is intentionally split into command-line arguments, allowing
# values such as "--format alac -s" in docker-compose.yml.
# shellcheck disable=SC2086
CONVERTER_ARGS=$(cat /app/converter-args)
if SOURCE_DIRECTORY="${SOURCE_DIRECTORY:-/music/source}" \
    OUTPUT_DIRECTORY="${OUTPUT_DIRECTORY:-/music/output}" \
    python /app/audio_convert.py ${CONVERTER_ARGS}; then
    status=0
else
    status=$?
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Audio conversion finished (exit ${status})"
exit "$status"
