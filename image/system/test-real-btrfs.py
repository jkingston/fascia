#!/usr/bin/env python3
"""Linux/root-only isolated Btrfs test. Creates and detaches its own file-backed loop."""

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

spec = importlib.util.spec_from_file_location(
    "update", Path(__file__).with_name("fascia-system-update.py")
)
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


def command(*args):
    return subprocess.run(
        args, check=True, capture_output=True, text=True
    ).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or os.uname().sysname != "Linux":
        parser.error("Requires Linux root and Btrfs/loop tools")
    root = args.work_root.resolve()
    if (
        not root.is_absolute()
        or str(root) in ("/", "/data", "/system", "/boot")
        or str(root).startswith("/dev")
    ):
        parser.error("Use a dedicated test directory")
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="isolated-", dir=root))
    loop = None
    mounted = False
    try:
        image = work / "system.img"
        with image.open("wb") as file:
            file.truncate(512 * 1024**2)
        loop = command("losetup", "--find", "--show", str(image))
        if not loop.startswith("/dev/loop") or not loop[len("/dev/loop") :].isdigit():
            raise RuntimeError("Unexpected loop device")
        command("mkfs.btrfs", "-f", loop)
        system = work / "system"
        system.mkdir()
        command("mount", "-t", "btrfs", loop, str(system))
        mounted = True
        generations = system / "generations"
        generations.mkdir()
        data = work / "data"
        data.mkdir()
        command("btrfs", "subvolume", "create", str(generations / "base"))
        command(
            "btrfs", "property", "set", "-ts", str(generations / "base"), "ro", "true"
        )
        tool = u.Updater(generations, data)
        tool.initialize("base")
        assert tool.select() == "base"
        tool.confirm("base")
        (data / "boot-b.state").write_bytes(b"torn")
        assert u.read_state(data)["good"] == "base"
        # Force actual BusyBox applets, rather than whichever host commands happen to be installed.
        busybox = shutil.which("busybox")
        if not busybox:
            raise RuntimeError("BusyBox is required for the initramfs selector test")
        applets = set(command(busybox, "--list").splitlines())
        required = {
            "sh",
            "head",
            "tail",
            "wc",
            "sha256sum",
            "cmp",
            "flock",
            "mktemp",
            "cp",
            "mv",
            "rm",
            "sync",
        }
        if not required <= applets:
            raise RuntimeError(
                "Missing BusyBox applets: " + ",".join(sorted(required - applets))
            )
        bins = work / "busybox-bin"
        bins.mkdir()
        for applet in required:
            (bins / applet).symlink_to(busybox)
        (bins / "btrfs").symlink_to(shutil.which("btrfs"))
        env = dict(os.environ, PATH=str(bins))
        result = subprocess.run(
            [
                str(bins / "sh"),
                str(Path(__file__).with_name("fascia-boot-select.sh")),
                str(data),
                str(generations),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "base"
        assert u.read_state(data)["selected"] == "base"
        # Actual writable-root checkpoint/rollback, with matched boot bytes and
        # a real pacman-style lock removed only from the restored clone.
        live = generations / "live"
        command("btrfs", "subvolume", "create", str(live))
        (live / "var/lib/pacman").mkdir(parents=True)
        (live / "var/lib/pacman/db.lck").write_text("active transaction")
        (live / "modules-version").write_text("before")
        boot = work / "boot"
        (boot / "extlinux").mkdir(parents=True)
        for name in ("zImage", "initramfs.img", "extlinux/extlinux.conf"):
            (boot / name).write_text("before:" + name)
        u.write_state(
            data,
            dict(
                sequence=u.read_state(data)["sequence"] + 1,
                good="live",
                candidate="-",
                attempts=0,
                selected="live",
                schema=1,
            ),
        )
        recovery = u.Recovery(
            generations, data, boot, live, running_generation=lambda: "live"
        )
        checkpoint = recovery.snapshot()
        assert (
            command(
                "btrfs", "property", "get", "-ts", str(generations / checkpoint), "ro"
            )
            == "ro=true"
        )
        (live / "modules-version").write_text("after")
        (boot / "zImage").write_text("new kernel")
        recovery.commit()
        restored = recovery.rollback()
        assert (
            command(
                "btrfs", "property", "get", "-ts", str(generations / restored), "ro"
            )
            == "ro=false"
        )
        assert (generations / restored / "modules-version").read_text() == "before"
        assert not (generations / restored / "var/lib/pacman/db.lck").exists()
        assert (live / "var/lib/pacman/db.lck").exists()
        assert (boot / "zImage").read_text() == "before:zImage"
        assert u.read_state(data)["good"] == restored
        writable = subprocess.run(
            [
                str(bins / "sh"),
                str(Path(__file__).with_name("fascia-boot-select.sh")),
                str(data),
                str(generations),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert writable.stdout.strip() == restored
        print(
            "PASS real pacman-style RO checkpoint -> matched boot restore + RW clone, live lock preserved, clone lock removed; writable BusyBox selection"
        )
        print(
            "PASS actual BusyBox selector+syncfs, durable boot records and corrupt-copy fallback"
        )
        print(
            "NOT tested: boot/initramfs integration, rendered-Home marker, physical power cuts or watchdog"
        )
    finally:
        if mounted:
            command("umount", str(work / "system"))
        if loop:
            command("losetup", "--detach", loop)
        shutil.rmtree(work)


if __name__ == "__main__":
    main()
