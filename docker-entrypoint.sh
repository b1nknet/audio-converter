#!/bin/sh
set -eu

: "${CRON_SCHEDULE:=0 3 * * *}"
: "${RUN_ON_START:=true}"

# BusyBox cron can use a restricted environment. Persist optional converter
# arguments so scheduled runs receive exactly the same arguments as startup.
printf '%s\n' "${CONVERTER_ARGS:-}" > /app/converter-args

cat > /etc/crontabs/root <<EOF
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
TZ=${TZ:-UTC}
${CRON_SCHEDULE} /bin/sh /app/run-converter.sh
EOF

if [ "$RUN_ON_START" = "true" ]; then
    /bin/sh /app/run-converter.sh
fi

echo "Schedule installed: ${CRON_SCHEDULE} (TZ=${TZ:-UTC})"
exec crond -f -l 2 -L /dev/stdout
