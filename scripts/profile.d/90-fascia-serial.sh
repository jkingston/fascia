# Sourced by a login shell. Do not alter the user's normal shell or other TTYs.
case $- in
    *i*)
        if [ "$(id -un 2> /dev/null)" = fascia ] && [ "$(tty 2> /dev/null)" = /dev/ttyGS0 ]; then
            /usr/local/bin/fascia-serial-session
        fi
        ;;
esac
