#!/bin/sh
set -eu
install -d -o root -g root -m 0755 /usr/local/libexec
printf '%s\n' '#!/bin/sh' 'exec /opt/mimir-worklink/venv/bin/python -m mimir_cultivate.worklink.worker_exec "$@"' > /usr/local/libexec/worklink-execd
chmod 0755 /usr/local/libexec/worklink-execd
chmod +x /etc/s6-overlay/s6-rc.d/mimir/run /etc/s6-overlay/s6-rc.d/worklink-execd/run
if [ -f /etc/s6-overlay/s6-rc.d/watchdog/run ]; then
    chmod +x /etc/s6-overlay/s6-rc.d/watchdog/run
fi
