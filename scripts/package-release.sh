#!/bin/sh
# OS-only packaging. Initial example apps are built by package-examples.sh.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test "$#" -eq 0 || {
    echo "Usage: scripts/package-release.sh" >&2
    exit 64
}
mkdir -p "$root/.local"
final="$root/.local/release"
out=$(mktemp -d "$root/.local/release-build.XXXXXX")
trap 'test -z "$out" || rm -rf "$out"' EXIT
trap 'exit 130' HUP INT TERM
cd "$root"
# Resolve normal ZON dependencies explicitly before compilation on Zig 0.16.
mise exec -- zig build --fetch
mise exec -- zig build --release=fast --prefix "$out"
(cd "$root/host" && mise exec -- zig build --fetch && mise exec -- zig build --release=small --prefix "$out")
mkdir -p "$out/bin" "$out/tools" "$out/scripts/profile.d" "$out/system" "$out/share/fonts" "$out/share/licenses/ttf-dejavu"
test -x "$out/bin/fascia-host"
test ! -e "$out/bin/fascia-home"
test ! -e "$out/bin/fascia-compositor"
test -x "$out/bin/fascia-brightness"
test -x "$out/bin/fascia-home-button"
test -s "$out/share/fonts/DejaVuSans.ttf"
test -s "$out/share/licenses/ttf-dejavu/LICENSE"
for name in fascia fascia-app fascia-system fascia-setup fascia-serial-session; do
    cp "$root/tools/$name.py" "$out/tools/"
done
for name in fascia-device-state.service fascia-brightness.service fascia-home-button.service fascia-display; do
    cp "$root/scripts/$name" "$out/scripts/"
done
cp "$root/scripts/profile.d/90-fascia-serial.sh" "$out/scripts/profile.d/"
cp "$root/system/40-fascia-volatile.conf" "$out/system/"
mkdir -p "$out/share/licenses/freetype" "$out/share/licenses/zlib"
test -s "$out/share/licenses/freetype/FTL.TXT"
test -s "$out/share/licenses/zlib/LICENSE"
cp "$root/LICENSE" "$out/"
python3 - "$root" "$out" << 'VERIFY'
import importlib.util
from pathlib import Path
import sys

root, release = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location(
    "fascia_image", root / "image/install-root.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
installer.validate_release(release)
VERIFY
python3 - "$out" "$final" << 'PUBLISH'
import os, sys, uuid
from pathlib import Path

stage, final = map(Path, sys.argv[1:])
if final.exists() and not final.is_symlink():
    if not final.is_dir():
        raise SystemExit("Refusing to replace unexpected release file")
    os.rename(final, final.with_name("release.previous." + uuid.uuid4().hex[:12]))
link = final.with_name(".release-pointer-" + uuid.uuid4().hex)
os.symlink(stage.name, link)
os.replace(link, final)
fd = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
PUBLISH
out=""
printf 'OS release staged at %s\n' "$final"
