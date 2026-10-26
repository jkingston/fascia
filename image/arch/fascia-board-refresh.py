#!/usr/bin/python3
"""Pacman post-hook: build first; publish only complete matching boot artifacts."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import subprocess
import tempfile

RECOVERY = "/usr/local/libexec/fascia-system-update"
MODULES = ("panel-jadard-jd9365da-h3", "aic8800_bsp", "aic8800_fdrv", "aic8800_btlpm")


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def kernel_release(base=Path("/usr/lib/modules"), owned_paths=None):
    # Use package file ownership: Arch Linux ARM lacks pkgbase, and uname may be stale.
    if owned_paths is None:
        owned_paths = run("pacman", "-Qql", "linux-armv7").splitlines()
    expression = re.compile(r"/?usr/lib/modules/([0-9][A-Za-z0-9_.+\-]*)(?:/|$)")
    choices = {
        match.group(1) for line in owned_paths if (match := expression.match(line))
    }
    if len(choices) != 1:
        raise ValueError("expected exactly one installed linux-armv7 kernel")
    release = choices.pop()
    if not (base / release).is_dir():
        raise ValueError("installed kernel module directory missing")
    return release


def validate_artifacts(kernel, initrd, dtb):
    if kernel.stat().st_size < 48 or kernel.open("rb").read(48)[36:40] != bytes.fromhex(
        "18286f01"
    ):
        raise ValueError("candidate is not an ARM zImage")
    if initrd.stat().st_size < 128 or initrd.open("rb").read(2) != b"\x1f\x8b":
        raise ValueError("candidate initramfs is not gzip")
    if dtb.stat().st_size < 40 or dtb.open("rb").read(4) != bytes.fromhex("d00dfeed"):
        raise ValueError("candidate is not a flattened device tree")


def publish(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".fascia-boot-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as target, source.open("rb") as stream:
            shutil.copyfileobj(stream, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_header_helpers(headers):
    for relative in (
        "scripts/basic/fixdep",
        "scripts/mod/modpost",
        "scripts/dtc/dtc",
        "scripts/genksyms/genksyms",
        "scripts/recordmcount",
        "scripts/sign-file",
    ):
        tool = headers / relative
        if not tool.is_file():
            continue
        try:
            subprocess.run(
                [str(tool)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(
                "Arch headers contain unusable native helper: " + relative
            ) from error


def production_boot_guard(sysfs=Path("/sys/class/block")):
    def mounted(target):
        record = json.loads(
            run(
                "findmnt", "--json", "--mountpoint", target, "--output", "SOURCE,FSTYPE"
            )
        )
        entries = record.get("filesystems", [])
        if len(entries) != 1:
            raise ValueError("required exact mount missing: " + target)
        return entries[0]

    boot, system = mounted("/boot"), mounted("/system")
    source = system["source"].split("[", 1)[0]
    match = re.fullmatch(r"/dev/(mmcblk[0-9]+)p2", source)
    if system["fstype"] != "btrfs" or not match:
        raise ValueError("/system must be SD Btrfs partition 2")
    disk = match.group(1)
    if (
        boot["fstype"] not in ("vfat", "fat", "msdos")
        or boot["source"] != "/dev/" + disk + "p1"
    ):
        raise ValueError("/boot must be FAT partition 1 of the system SD")
    if (sysfs / disk / "device/type").read_text().strip() != "SD":
        raise ValueError("refusing non-SD boot device")
    return disk


def ensure_dkms(name, version, release):
    status = run("dkms", "status", "-m", name, "-v", version, "-k", release)
    expression = re.compile(
        re.escape(name + "/" + version + ", " + release + ", ")
        + r"[A-Za-z0-9_.+\-]+: (built|installed)$"
    )
    states = [
        match.group(1)
        for line in status.splitlines()
        if (match := expression.fullmatch(line.strip()))
    ]
    if states == ["installed"]:
        return
    if states != ["built"]:
        run("dkms", "build", "-m", name, "-v", version, "-k", release)
    run("dkms", "install", "-m", name, "-v", version, "-k", release)


def protected_staging(boundary=Path("/data")):
    # Keep privileged boot candidates outside user-owned /var/lib/fascia.
    paths = [
        boundary,
        boundary / "fascia-system",
        boundary / "fascia-system/boot-staging",
    ]
    for index, path in enumerate(paths):
        if index and not path.exists() and not path.is_symlink():
            path.mkdir(mode=0o700)
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise ValueError("unsafe privileged staging ancestor: " + str(path))
        if index and info.st_mode & 0o077:
            raise ValueError("privileged staging must be root-only: " + str(path))
    return paths[-1]


def refresh():
    run(RECOVERY, "recovery", "validate-storage")
    boot_disk = production_boot_guard()
    release = kernel_release()
    headers = Path("/usr/lib/modules") / release / "build"
    if not (headers / "Makefile").is_file():
        raise ValueError("matching linux-armv7-headers missing")
    check_header_helpers(headers)
    for name in ("fascia-panel", "fascia-aic8800"):
        ensure_dkms(name, "1.0", release)
    run("depmod", "-a", release)
    for name in MODULES:
        filename = run("modinfo", "-k", release, "-F", "filename", name)
        if "/updates/" not in filename:
            raise ValueError("DKMS module did not override stock module: " + name)
        vermagic = run("modinfo", "-k", release, "-F", "vermagic", name)
        if vermagic.split()[0] != release:
            raise ValueError("module vermagic mismatch: " + name)
    state = protected_staging()
    with tempfile.TemporaryDirectory(prefix="candidate-", dir=state) as directory:
        candidate = Path(directory)
        kernel, initrd, dtb = [
            candidate / n for n in ("zImage", "initramfs.img", "pf1007l-provision.dtb")
        ]
        shutil.copyfile("/boot/zImage", kernel)
        shutil.copyfile("/usr/share/fascia/board/pf1007l-provision.dtb", dtb)
        run(
            "/usr/lib/fascia/build-initramfs",
            "--root",
            "/",
            "--runtime",
            "--kernel",
            release,
            "--output",
            str(initrd),
        )
        validate_artifacts(kernel, initrd, dtb)
        hashes = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (kernel, initrd, dtb)
        }
        run(RECOVERY, "recovery", "validate-storage")
        if production_boot_guard() != boot_disk:
            raise ValueError("boot device changed during transaction")
        # Multi-file publication needs the boot backup and matching root snapshot for recovery.
        for source, destination in (
            (kernel, Path("/boot/zImage")),
            (initrd, Path("/boot/initramfs.img")),
            (dtb, Path("/boot/dtbs/pf1007l-provision.dtb")),
        ):
            publish(source, destination)
        run(RECOVERY, "recovery", "boot-commit")
        print(json.dumps({"kernel": release, "boot_files": hashes}, sort_keys=True))


def main():
    if os.geteuid() != 0:
        raise PermissionError("root required")
    if sys.argv[1:] == ["--refuse-removal"]:
        print(
            "Refusing removal of essential Fascia boot dependencies. Use pacman -Syu to upgrade them; removing them would disable boot or recovery.",
            file=sys.stderr,
        )
        return 1
    if len(sys.argv) != 1:
        raise ValueError("unsupported board-refresh arguments")
    with open("/run/fascia-board-refresh.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        guarded_refresh()


def guarded_refresh():
    try:
        refresh()
    except Exception:
        try:
            run(RECOVERY, "recovery", "boot-restore")
        except Exception as recovery:
            print(
                "ERROR: automatic recovery failed; preserve boot backup and use serial recovery:",
                recovery,
                flush=True,
            )
        print(
            "ERROR: board upgrade failed. Reboot into the selected pretransaction recovery generation before further package changes.",
            flush=True,
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
