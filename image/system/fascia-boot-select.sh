#!/bin/sh
# BusyBox initramfs: writable data subvolume and Btrfs top level already mounted.
# Prints one generation id. Errors stop selection; never guesses a device or root.
set -eu
data=${1:-/data/fascia-system}
systems=${2:-/system/generations}
valid_id() {
    case "$1" in '' | *[!a-z0-9-]* | -*) return 1 ;; esac
    test "${#1}" -le 64
}
read_record() {
    file=$1
    test -f "$file" && test ! -L "$file" && test "$(wc -c < "$file")" -le 1024 || return 1
    test "$(wc -l < "$file")" -eq 8 || return 1
    checksum=$(tail -n 1 "$file")
    actual=$(head -n 7 "$file" | sha256sum)
    test "$checksum" = "checksum=${actual%% *}" || return 1
    {
        IFS= read -r header
        IFS= read -r seq
        IFS= read -r good
        IFS= read -r candidate
        IFS= read -r attempts
        IFS= read -r selected
        IFS= read -r schema
    } < "$file"
    test "$header" = FASCIA_BOOT_STATE_V1 && test "$schema" = schema=1 || return 1
    case "$seq" in sequence=*) seq=${seq#sequence=} ;; *) return 1 ;; esac
    case "$seq" in '' | *[!0-9]*) return 1 ;; esac
    test "${#seq}" -le 10 && test "$seq" -le 2000000000 || return 1
    test "$seq" = 0 || test "${seq#0}" = "$seq" || return 1
    case "$good" in good=*) good=${good#good=} ;; *) return 1 ;; esac
    case "$candidate" in candidate=*) candidate=${candidate#candidate=} ;; *) return 1 ;; esac
    case "$selected" in selected=*) selected=${selected#selected=} ;; *) return 1 ;; esac
    case "$attempts" in attempts=0 | attempts=1 | attempts=2) attempts=${attempts#attempts=} ;; *) return 1 ;; esac
    valid_id "$good" || return 1
    test "$candidate" = - || valid_id "$candidate" || return 1
    test "$selected" = - || valid_id "$selected" || return 1
    test "$candidate" != "$good" || return 1
    test "$selected" = - || test "$selected" = "$good" || test "$selected" = "$candidate" || return 1
}
# BusyBox flock holds the same lock as userspace for the entire transaction.
exec 9> "$data/.update.lock"
flock -x 9
best=-1
chosen=
for record in "$data/boot-a.state" "$data/boot-b.state"; do
    if read_record "$record"; then
        if test "$seq" -gt "$best"; then
            best=$seq
            chosen=$record
        elif test "$seq" -eq "$best" && ! cmp -s "$chosen" "$record"; then
            echo 'Conflicting boot state; manual recovery required' >&2
            exit 1
        fi
    fi
done
test -n "$chosen" || {
    echo 'No valid boot state; manual recovery required' >&2
    exit 1
}
read_record "$chosen"
next=$good
if test "$candidate" != - && test "$attempts" -lt 2; then
    next=$candidate
    attempts=$((attempts + 1))
fi
# Missing candidate fails safely to known-good without mounting it.
if test ! -d "$systems/$next" || test -L "$systems/$next"; then
    next=$good
    attempts=2
fi
test -d "$systems/$next" && test ! -L "$systems/$next" || {
    echo 'Known-good generation missing' >&2
    exit 1
}
# Selected system generations are writable Arch roots. Recovery checkpoints stay
# read-only and are cloned to a writable generation by the root recovery helper.
property=$(btrfs property get -ts "$systems/$next" ro 2> /dev/null) || {
    echo 'Selected generation is not a Btrfs subvolume' >&2
    exit 1
}
case "$property" in ro=true | ro=false) ;; *) exit 1 ;; esac
seq=$((best + 1))
test "$seq" -le 2000000000
umask 077
temporary=$(mktemp "$data/.boot-select.XXXXXX")
trap 'rm -f "$temporary" "$temporary.record"' EXIT
trap 'exit 130' HUP INT TERM
printf 'FASCIA_BOOT_STATE_V1\nsequence=%s\ngood=%s\ncandidate=%s\nattempts=%s\nselected=%s\nschema=1\n' "$seq" "$good" "$candidate" "$attempts" "$next" > "$temporary"
checksum=$(sha256sum "$temporary")
printf 'checksum=%s\n' "${checksum%% *}" >> "$temporary"
# sync -f invokes syncfs: flush data filesystem before and after each atomic rename.
for name in boot-a.state boot-b.state; do
    cp "$temporary" "$temporary.record"
    sync -f "$temporary.record"
    mv -f "$temporary.record" "$data/$name"
    sync -f "$data"
done
printf '%s\n' "$next"
