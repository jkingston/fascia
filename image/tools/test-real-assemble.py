#!/usr/bin/env python3
"""Linux/root guard test: new sparse image + own loop, no formatting or boot."""

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

spec = importlib.util.spec_from_file_location(
    "assemble", Path(__file__).with_name("assemble-image.py")
)
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or os.uname().sysname != "Linux":
        parser.error("Requires Linux root")
    root = args.work_root.resolve()
    if root in (Path("/"), Path("/dev"), Path("/boot"), Path("/data"), Path("/system")):
        parser.error("Use a dedicated test directory")
    root.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="layout-guard-", dir=root))
    loop = None
    try:
        image = workspace / "new.img"
        with image.open("wb") as file:
            file.truncate(8192 * a.MIB)
            file.seek(0)
            file.write(a.mbr(8192))
            file.flush()
            os.fsync(file.fileno())
        loop = a.run("losetup", "--find", "--show", "--partscan", image)
        node = a.verify_loop(loop, image)
        parts = a.partition_nodes(loop, image, workspace)
        for number, (start, size, _) in enumerate(a.layout(8192), 1):
            sysfs = Path("/sys/block") / node / (node + "p" + str(number))
            assert int((sysfs / "start").read_text()) == start * 2048
            assert int((sysfs / "size").read_text()) == size * 2048
        wrong = workspace / "wrong.img"
        wrong.write_bytes(b"not this loop")
        try:
            a.verify_loop(loop, wrong)
        except ValueError:
            pass
        else:
            raise AssertionError("Wrong backing file accepted")
        assert len(parts) == 2
        print(
            "PASS real loop kernel inode/device + sysfs backing identity; exact 8 GiB MBR partition layout; wrong backing rejected"
        )
        print("No filesystem formatted, mounted or booted by this guard test")
    finally:
        if loop:
            subprocess.run(["losetup", "--detach", loop], check=True)
        shutil.rmtree(workspace)


if __name__ == "__main__":
    main()
