#!/usr/bin/env python3
"""Grow a newly created file-backed Btrfs image; never accepts a physical device."""

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HERE = Path(__file__).resolve().parent
assemble = load("assemble", HERE / "assemble-image.py")
grow = load("grow", HERE.parent / "fascia-grow-storage.py")


def run(*arguments):
    return subprocess.check_output(list(map(str, arguments)), text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or os.uname().sysname != "Linux":
        parser.error("Requires Linux root and Btrfs tools")
    root = args.work_root.resolve()
    if root in (Path("/"), Path("/dev"), Path("/boot"), Path("/system"), Path("/data")):
        parser.error("Use a dedicated test directory")
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="storage-growth-", dir=root))
    image = work / "new.img"
    loop = None
    mounted = False
    mount = work / "system"
    try:
        with image.open("xb") as file:
            file.truncate(4868 * assemble.MIB)
            file.write(assemble.mbr(4868))
            file.seek(8192)
            file.write(b"Bootloader must remain untouched" * 100)
            file.flush()
            os.fsync(file.fileno())
        with image.open("rb") as file:
            boot_before = file.read(4 * assemble.MIB)[512:]
        loop = run("losetup", "--find", "--show", "--partscan", image)
        node = assemble.verify_loop(loop, image)
        run("udevadm", "settle", "--timeout=30")
        parts = assemble.partition_nodes(loop, image, work)
        run("mkfs.btrfs", "-f", parts[1])
        mount.mkdir()
        run("mount", "-t", "btrfs", parts[1], mount)
        mounted = True
        (mount / "generations").mkdir()
        run("btrfs", "subvolume", "create", mount / "generations/base")
        run("btrfs", "subvolume", "create", mount / "data")
        (mount / "generations/base/etc").mkdir()
        (mount / "generations/base/etc/example").write_text("Arch settings survive")
        (mount / "data/photo").write_bytes(b"Personal data survives")
        run(
            "btrfs",
            "subvolume",
            "snapshot",
            "-r",
            mount / "generations/base",
            mount / "generations/recovery",
        )
        run("btrfs", "filesystem", "sync", mount)
        backup = mount / "data/growth"
        target = grow.Target(
            Path(loop),
            parts[1],
            Path("/sys/block") / node / (node + "p2"),
            Path(loop).stat().st_rdev,
            parts[1].stat().st_rdev,
        )

        def enlarge_file(mib):
            assemble.verify_loop(loop, image)
            with image.open("r+b") as file:
                file.truncate(mib * assemble.MIB)
                file.flush()
                os.fsync(file.fileno())
            run("losetup", "--set-capacity", loop)

        calls = []
        fail_at = ["partx"]

        def command(*arguments):
            assemble.verify_loop(loop, image)
            calls.append(arguments)
            if fail_at and Path(arguments[0]).name == fail_at[0]:
                fail_at.pop()
                raise InterruptedError("Simulated interruption")
            translated = [shutil.which(Path(arguments[0]).name), *arguments[1:]]
            if translated[-1] == "/system":
                translated[-1] = str(mount)
            run(*translated)

        grow.command = command
        enlarge_file(8192)
        try:
            grow.expand(target, backup)
        except InterruptedError:
            pass
        else:
            raise AssertionError("Partition update fault was not injected")
        result = grow.expand(target, backup)
        assert result["filesystem_bytes"] == (8192 - 260) * assemble.MIB
        # A larger replacement card can also resume between kernel and FS resize.
        enlarge_file(9216)
        fail_at[:] = ["btrfs"]
        try:
            grow.expand(target, backup)
        except InterruptedError:
            pass
        else:
            raise AssertionError("Filesystem resize fault was not injected")
        result = grow.expand(target, backup)
        assert result["filesystem_bytes"] == (9216 - 260) * assemble.MIB
        calls.clear()
        assert grow.expand(target, backup) == result
        assert calls == [], "Completed expansion must do no resize or writes"
        assert (
            mount / "generations/base/etc/example"
        ).read_text() == "Arch settings survive"
        assert (mount / "data/photo").read_bytes() == b"Personal data survives"
        assert (
            run("btrfs", "property", "get", "-ts", mount / "generations/recovery", "ro")
            == "ro=true"
        )
        with image.open("rb") as file:
            assert file.read(4 * assemble.MIB)[512:] == boot_before
        assert len(list(backup.glob("mbr-*.bin"))) == 2
        print(
            "PASS real online shared-Btrfs expansion, both interruption points, later larger card, no-op retry, unchanged boot/root/data/snapshot"
        )
    finally:
        if mounted:
            run("umount", mount)
        if loop:
            run("losetup", "--detach", loop)
        shutil.rmtree(work)


if __name__ == "__main__":
    main()
