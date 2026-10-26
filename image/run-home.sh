#!/bin/sh
set -eu
export FASCIA_APPS_ROOT=/var/lib/fascia/apps
export FASCIA_STATE_ROOT=/var/lib/fascia/state
export FASCIA_HOME_ROOT=/var/lib/fascia
if [ -z "${FASCIA_AUTOSTART:-}" ]; then
    if mkdir /run/fascia-home/autostart-offered 2> /dev/null; then
        export FASCIA_AUTOSTART=1
    else
        export FASCIA_AUTOSTART=0
    fi
fi
unset FASCIA_CONCEPT FASCIA_SCREEN
if [ "${FASCIA_HOME_DISPLAY:-0}" = 1 ]; then
    exec /usr/lib/fascia/bin/fascia-host --display
fi
exec /usr/lib/fascia/bin/fascia-host
