#!/bin/sh
# A local userspace readiness check, not a watchdog or network reachability test.
set -eu
for unit in pf-kiosk.service fascia-brightness.service; do
    systemctl is-active --quiet "$unit"
done
exec /usr/local/libexec/fascia-system-update health
