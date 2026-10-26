#!/bin/sh
# First-image examples, packaged separately from every OS release.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test "$#" -eq 0 || {
    echo "Usage: scripts/package-examples.sh" >&2
    exit 64
}
mkdir -p "$root/.local"
final="$root/.local/initial-apps"
out=$(mktemp -d "$root/.local/initial-apps-build.XXXXXX")
trap 'test -z "$out" || rm -rf "$out"' EXIT
trap 'exit 130' HUP INT TERM
cd "$root"
for example in hello clock; do
    case "$example" in hello) app_id=counter ;; clock) app_id=clock ;; esac
    bundle="$out/apps/$app_id"
    prefix="$out/build-$example"
    (cd "$root/examples/$example" && mise exec -- zig build --fetch && mise exec -- zig build --release=fast --prefix "$prefix")
    mkdir -p "$out/apps"
    mv "$prefix/$app_id.AppDir" "$bundle"
    rmdir "$prefix"

done
cp "$root/image/initial-apps.json" "$out/manifest.json"
test ! -e "$out/apps/clock/assets"
test -s "$out/apps/clock/licenses/stb.txt"
python3 - "$root" "$out" << 'VERIFY'
import importlib.util
from pathlib import Path
import sys

root, package = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location(
    "fascia_image", root / "image/install-root.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
installer.validate_initial_apps(package)
VERIFY
python3 - "$out" "$final" << 'PUBLISH'
import os, sys, uuid
from pathlib import Path

stage, final = map(Path, sys.argv[1:])
if final.exists() and not final.is_symlink():
    if not final.is_dir():
        raise SystemExit("Refusing to replace unexpected release file")
    os.rename(final, final.with_name(final.name + ".previous." + uuid.uuid4().hex[:12]))
link = final.with_name(".apps-pointer-" + uuid.uuid4().hex)
os.symlink(stage.name, link)
os.replace(link, final)
fd = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
PUBLISH
out=""
printf 'First-image apps staged at %s\n' "$final"
