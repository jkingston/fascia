#!/usr/bin/env python3
"""Build source-only DKMS package offline in a marked disposable ARM root."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
WIFI_SHA256 = "a712a6a36dac6a1bee1fe20bdf2d774137e77413c9921f4300195fcfeb2af5f8"


def verify_archive(path, expected):
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != expected:
        raise ValueError("retained source archive hash mismatch: " + path.name)


def validate_package(path):
    with tarfile.open(path) as archive:
        sources = [
            member
            for member in archive.getmembers()
            if member.name.startswith("usr/src/")
        ]
        if not sources:
            raise ValueError("package has no DKMS sources")
        for member in sources:
            if member.uid != 0 or member.gid != 0 or member.mode & 0o022:
                raise ValueError("unsafe package ownership/mode: " + member.name)
            if member.name.endswith((".ko", ".bin", ".fw")):
                raise ValueError("binary in source package: " + member.name)
        configs = {
            member.name for member in sources if member.name.endswith("/dkms.conf")
        }
        if configs != {
            "usr/src/fascia-panel-1.0/dkms.conf",
            "usr/src/fascia-aic8800-1.0/dkms.conf",
        }:
            raise ValueError("package DKMS configuration incomplete")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True, type=Path)
    p.add_argument(
        "--sources",
        required=True,
        type=Path,
        help="trusted retained board-out/sources directory",
    )
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    root = args.root.resolve()
    if (
        os.geteuid() != 0
        or root == Path("/")
        or (root / ".fascia-image-build-root").read_text() != "isolated\n"
    ):
        raise ValueError("root and marked offline build root required")
    sources = args.sources.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "chroot",
            str(root),
            "sh",
            "-ec",
            "command -v makepkg; command -v fakeroot; command -v git",
        ],
        check=True,
    )
    verify_archive(
        sources / "wifi-e9e43ed79c50483080e9bcdfb08caeeb869d7191.tar", WIFI_SHA256
    )
    mirrorlist = root / "build/mirrorlist"
    temporary_mirrorlist = (
        not mirrorlist.exists()
        and "/build/mirrorlist" in (root / "etc/pacman.conf").read_text()
    )
    if temporary_mirrorlist:
        shutil.copy2(root / "etc/pacman.d/mirrorlist", mirrorlist)
    workspace = Path(tempfile.mkdtemp(prefix="fascia-dkms-", dir=root / "build"))
    try:
        for f in (HERE / "fascia-board-support").iterdir():
            shutil.copy2(f, workspace / f.name)
        src = workspace / "src"
        src.mkdir()
        archives = {
            "aic8800": sources / "wifi-e9e43ed79c50483080e9bcdfb08caeeb869d7191.tar",
            "selected": sources / "kernel-v7.2.10-selected.tar",
        }
        for key, archive in archives.items():
            dest = src / key
            dest.mkdir()
            with tarfile.open(archive) as tar:
                tar.extractall(dest, filter="data")
        panel = src / "selected/drivers/gpu/drm/panel/panel-jadard-jd9365da-h3.c"
        if (
            hashlib.sha256(panel.read_bytes()).hexdigest()
            != "153da8a833089060a60ea688b2477fa05faa0fb03c1e89c942dc6ec9162dcf32"
        ):
            raise ValueError("pinned panel source mismatch")
        shutil.copy2(panel, src / "panel.c")
        for name in ["panel.patch", "panel.dkms.conf", "wifi.dkms.conf"]:
            shutil.copy2(workspace / name, src / name)
        # git apply does not need the upstream history, only an initialized worktree.
        relative = "/" + str(workspace.relative_to(root))
        subprocess.run(
            ["chroot", str(root), "git", "init", "-q", relative + "/src/aic8800"],
            check=True,
        )
        # Build only: never run package manager hooks or change the root's packages.
        for path in [workspace, *workspace.rglob("*")]:
            if not path.is_symlink():
                os.chown(path, 65534, 65534)
        command = 'cd "$1"; source ./PKGBUILD; srcdir="$PWD/src"; (prepare); makepkg --noextract --nodeps --noconfirm --skipinteg'
        subprocess.run(
            [
                "chroot",
                str(root),
                "runuser",
                "-u",
                "nobody",
                "--",
                "bash",
                "-euc",
                command,
                "build",
                relative,
            ],
            check=True,
        )
        packages = [
            p for p in workspace.glob("*.pkg.tar.*") if not p.name.endswith(".sig")
        ]
        if len(packages) != 1:
            raise ValueError("expected one package output")
        validate_package(packages[0])
        result = output / packages[0].name
        shutil.copy2(packages[0], result)
        corresponding = output / "sources"
        corresponding.mkdir(exist_ok=True)
        for archive in archives.values():
            shutil.copy2(archive, corresponding / archive.name)
        for item in (HERE / "fascia-board-support").iterdir():
            shutil.copy2(item, corresponding / item.name)
        print(result)
    finally:
        shutil.rmtree(workspace)
        if temporary_mirrorlist:
            mirrorlist.unlink()


if __name__ == "__main__":
    main()
