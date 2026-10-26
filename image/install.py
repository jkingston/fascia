#!/usr/bin/env python3
"""Build a PF1007L installation on an explicitly selected card or new test image."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import uuid

PROJECT = Path(__file__).resolve().parent.parent
HOST = PROJECT / "image/host"
sys.path.insert(0, str(HOST))
import devices  # noqa: E402
import inputs  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(
        "fascia_installer_" + name, HOST / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def atomic_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=".request-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextlib.contextmanager
def installer_lock(cache):
    if cache.is_symlink():
        raise ValueError("The installer cache cannot be a symlink")
    cache.mkdir(parents=True, exist_ok=True)
    info = cache.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("The installer cache must be owned and writable only by you")
    marker = cache / ".fascia-installer-cache"
    if marker.is_symlink():
        raise ValueError("The installer cache marker cannot be a symlink")
    if not marker.exists():
        if any(cache.iterdir()) and cache != PROJECT / ".local/install":
            raise ValueError("Choose an empty dedicated cache directory")
        marker.write_text("Fascia host installer cache v1\n")
    elif marker.read_text() != "Fascia host installer cache v1\n":
        raise ValueError("Invalid installer cache marker")
    cache.chmod(0o700)
    with (cache / ".installer.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another installer is using this cache") from error
        yield


def source_files(project):
    """Git excludes private build output; source archives use the same exclusions."""
    excluded = {
        ".git",
        ".local",
        ".DS_Store",
        "__pycache__",
        ".zig-cache",
        "zig-out",
        "zig-pkg",
        "secrets",
        ".ruff_cache",
    }
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=project,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if Path(root).resolve() != project.resolve():
            raise ValueError("Not a standalone checkout")
        raw = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=project,
        )
        names = sorted(set(raw.decode().split("\0")) - {""})
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        names = []
        for directory, folders, files in os.walk(project):
            folders[:] = [name for name in folders if name not in excluded]
            names.extend(
                (Path(directory) / name).relative_to(project).as_posix()
                for name in files
            )
    for name in names:
        relative = Path(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or set(relative.parts) & excluded
        ):
            continue
        path = project / relative
        if path.is_symlink():
            raise ValueError("Source snapshots do not follow symlinks: " + name)
        if path.is_file():
            yield relative


def snapshot_tree(source, destination, names=None):
    source = Path(source).resolve()
    destination.mkdir(parents=True)
    names = (
        names
        if names is not None
        else [
            p.relative_to(source)
            for p in source.rglob("*")
            if p.is_file() or p.is_symlink()
        ]
    )
    for relative in names:
        path = source / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError("Snapshot inputs must be regular files: " + str(path))
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def snapshot_downloads(source, destination):
    """Hardlink immutable verified archives; never extract Linux metadata onto APFS."""
    destination.mkdir(parents=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if path.is_symlink():
            raise ValueError("Download cache contains a symlink: " + str(relative))
        if path.is_dir():
            (destination / relative).mkdir(exist_ok=True)
        elif path.is_file() and not path.name.endswith((".partial", ".download")):
            target = destination / relative
            try:
                os.link(path, target)
            except OSError:
                shutil.copy2(path, target)


def release_directory(args):
    release = (args.release or PROJECT / ".local/release").resolve()
    if args.release is None and (not release.is_dir() or (release / "apps").exists()):
        if args.offline:
            raise ValueError(
                "Offline preparation requires an already built app release"
            )
        if not shutil.which("mise"):
            raise ValueError(
                "Install mise, run mise install, then build scripts/package-release.sh"
            )
        print("Building the pinned Zig app release…", flush=True)
        subprocess.run(["sh", "scripts/package-release.sh"], cwd=PROJECT, check=True)
        release = (PROJECT / ".local/release").resolve()
    for name in ("fascia-host",):
        path = release / "bin" / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing compiled public release: " + str(path))
    if (release / "apps").exists() or (release / "apps").is_symlink():
        raise ValueError(
            "Expected an OS-only release; rebuild with scripts/package-release.sh"
        )
    return release


def initial_apps_directory(args):
    explicit = getattr(args, "initial_apps", None)
    directory = (explicit or PROJECT / ".local/initial-apps").resolve()
    if explicit is None and not directory.is_dir():
        if args.offline:
            raise ValueError("Offline preparation requires already built initial apps")
        print("Building the separate first-image apps…", flush=True)
        subprocess.run(["sh", "scripts/package-examples.sh"], cwd=PROJECT, check=True)
        directory = (PROJECT / ".local/initial-apps").resolve()
    installer = load("guest").load_module(
        "fascia_image_release", PROJECT / "image/install-root.py"
    )
    installer.validate_initial_apps(directory)
    return directory


def arm32_handler_available(handlers):
    handlers = Path(handlers)
    if not handlers.is_dir():
        return False
    for path in handlers.iterdir():
        if path.name in {"register", "status"} or not path.is_file():
            continue
        text = path.read_text()
        flags = next(
            (
                line.split(":", 1)[1]
                for line in text.splitlines()
                if line.startswith("flags:")
            ),
            "",
        )
        if text.startswith("enabled\n") and "qemu-arm" in text and "F" in flags:
            return True
    return False


def check_host():
    if sys.version_info < (3, 11):
        raise ValueError("Python 3.11 or newer is required")
    if sys.platform == "darwin":
        if platform.machine() not in ("arm64", "aarch64"):
            raise ValueError("The Mac helper currently supports Apple Silicon only")
        if int(platform.mac_ver()[0].split(".")[0]) < 14:
            raise ValueError("The Mac helper requires macOS 14 or newer")
        if not shutil.which("xcrun"):
            raise ValueError(
                "Apple Command Line Tools are required: xcode-select --install"
            )
    elif sys.platform.startswith("linux"):
        version = platform.release().split("-", 1)[0].split(".")
        if tuple(map(int, version[:2])) < (5, 8):
            raise ValueError("The Linux backend requires kernel 5.8 or newer")
        required = (
            "unshare",
            "mount",
            "umount",
            "losetup",
            "sfdisk",
            "mkfs.vfat",
            "mkfs.btrfs",
            "btrfs",
            "bsdtar",
            "blockdev",
            "gpgv",
            "chroot",
            "tar",
            "zstd",
            "modinfo",
            "modprobe",
            "cp",
            "sync",
        )
        missing = [name for name in required if not shutil.which(name)]
        if missing:
            raise ValueError("Missing Linux host tools: " + ", ".join(missing))
        if platform.machine() not in ("armv7", "armv7l"):
            if not arm32_handler_available("/proc/sys/fs/binfmt_misc"):
                raise ValueError(
                    "This Linux host needs an enabled ARM32 binfmt handler with flag F for chroot execution (normally qemu-user-static/systemd-binfmt). The installer does not change global handlers."
                )
    else:
        raise ValueError("Only Apple Silicon macOS and Linux hosts are supported")


def progress(event):
    if event.get("kind") in ("download", "source"):
        print("Cached " + event["file"], flush=True)


def worker(job):
    command = [sys.executable, str(HOST / "runner.py"), "--job", str(job)]
    if sys.platform.startswith("linux"):
        command.insert(0, "sudo")
    subprocess.run(command, check=True)


def run_preparation(job, guest, output_dir, downloads):
    try:
        worker(job)
    except BaseException:
        rejected = Path(output_dir) / "rejected-rootfs.json"
        if rejected.is_file() and not rejected.is_symlink():
            try:
                record = json.loads(rejected.read_text())
                if record.get("schema_version") != 1:
                    raise ValueError("Invalid rejected-rootfs record")
                inputs.rootfs.invalidate(
                    downloads, record["rootfs_sha256"], record["signature_sha256"]
                )
                print(
                    "The downloaded base failed signature verification and was quarantined. Rerun preparation to download a fresh copy.",
                    file=sys.stderr,
                )
            except (OSError, ValueError, KeyError) as error:
                print(
                    "Unable to quarantine rejected base: " + str(error), file=sys.stderr
                )
        try:
            guest.promote_board_cache(output_dir, downloads)
        except (OSError, ValueError) as error:
            print("Board checkpoint unavailable: " + str(error), file=sys.stderr)
        raise
    guest.promote_board_cache(output_dir, downloads)


def prepare(args, cache):
    release = release_directory(args)
    initial_apps = initial_apps_directory(args)
    print("Preparing the official Arch Linux ARM base and board sources…", flush=True)
    resolved = inputs.prepare(
        cache / "downloads",
        offline=args.offline,
        progress=progress,
    )
    helper_paths = None
    if sys.platform == "darwin":
        helper_paths = load("helper").prepare_helper(
            cache / "helper", offline=args.offline
        )
    jobs = cache / "jobs"
    jobs.mkdir(exist_ok=True)
    jobdir = jobs / uuid.uuid4().hex
    jobdir.mkdir(mode=0o700)
    input_dir = jobdir / "input"
    input_dir.mkdir()
    snapshot_tree(PROJECT, input_dir / "project", source_files(PROJECT))
    snapshot_tree(release, input_dir / "release")
    snapshot_tree(initial_apps, input_dir / "initial-apps")
    snapshot_downloads(cache / "downloads", input_dir / "cache")
    atomic_json(
        input_dir / "rootfs-input.json",
        {
            "schema_version": 1,
            "rootfs_sha256": resolved["rootfs_sha256"],
            "signature_sha256": resolved["signature_sha256"],
        },
    )
    guest = load("guest")
    source_key = guest.source_key(input_dir)
    output_dir = cache / "prepared" / source_key
    output_dir.mkdir(parents=True, exist_ok=True)
    request = {
        "schema_version": 1,
        "phase": "prepare",
        "board_id": "pf1007l-pl5",
        "capacity": 0,
        "serial": "FASCIA-INSTALL-FILE",
        "erase_authorized": False,
        "source_key": source_key,
        "offline": args.offline,
        "refresh": args.refresh,
    }
    atomic_json(input_dir / "request.json", request)
    control = {
        "schema_version": 1,
        "input": str(input_dir),
        "output": str(output_dir),
        "helper": {key: str(value) for key, value in helper_paths.items()}
        if helper_paths
        else None,
        "mode": "prepare",
    }
    job = jobdir / "job.json"
    atomic_json(job, control)
    run_preparation(job, guest, output_dir, cache / "downloads")
    print(
        "Prepared installation inputs. No card has been opened or written.", flush=True
    )
    return job, control, request


def create_image(path, size_mib):
    if type(size_mib) is not int or not 8192 <= size_mib <= 65536:
        raise ValueError("Test images must be 8192–65536 MiB")
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Image output cannot be a symlink")
    path = path.parent.resolve(strict=True) / path.name
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        os.ftruncate(descriptor, size_mib * 1024**2)
    finally:
        os.close(descriptor)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List external disks; never selects or writes one")
    for name in ("prepare", "image", "install"):
        sub = commands.add_parser(
            name,
            help={
                "prepare": "Download and build everything before selecting a card",
                "image": "Build and verify a new disposable file-backed card",
                "install": "Build directly onto an explicitly identified external card",
            }[name],
        )
        sub.add_argument("--cache", type=Path, default=PROJECT / ".local/install")
        sub.add_argument(
            "--release", type=Path, help="Explicit previously built OS-only release"
        )
        sub.add_argument(
            "--initial-apps", type=Path, help="Separate first-image app package"
        )
        freshness = sub.add_mutually_exclusive_group()
        freshness.add_argument(
            "--offline",
            action="store_true",
            help="Reuse an already completed preparation without network access",
        )
        freshness.add_argument(
            "--refresh",
            action="store_true",
            help="Run fresh rolling package preparation instead of reusing a completed snapshot",
        )
        if name != "prepare":
            load("ssh").arguments(sub)
        if name == "image":
            sub.add_argument("--output", type=Path, required=True)
            sub.add_argument("--size-mib", type=int, default=8192)
        elif name == "install":
            sub.add_argument(
                "--disk",
                required=True,
                help="Exact whole disk: disk9 on Mac, /dev/sdb on Linux",
            )
            sub.add_argument(
                "--capacity",
                type=int,
                required=True,
                help="Exact capacity reported by list, in bytes",
            )
            sub.add_argument(
                "--erase",
                action="store_true",
                required=True,
                help="Authorize erasing this specific external card",
            )
    args = parser.parse_args()
    if args.command == "list":
        devices.list_targets()
        return
    if os.geteuid() == 0:
        parser.error("Run as your normal user; the installer requests sudo when needed")
    check_host()
    if args.command == "install":
        identity = devices.inspect(args.disk, args.capacity)
        print(
            f"Selected {args.disk}: {args.capacity} bytes. Preparation finishes before it is opened.",
            flush=True,
        )
    elif args.command == "image" and (args.output.exists() or args.output.is_symlink()):
        parser.error("Image output must be a new file")
    if args.cache.is_symlink():
        parser.error("The installer cache cannot be a symlink")
    cache = args.cache.resolve()
    if cache.is_relative_to(PROJECT) and not cache.is_relative_to(PROJECT / ".local"):
        parser.error("Keep the installer cache under .local/ or outside the checkout")
    access = load("ssh").configure(args) if args.command != "prepare" else None
    with installer_lock(cache):
        job, control, request = prepare(args, cache)
        if args.command == "prepare":
            return
        request.update(phase="install", erase_authorized=True, ssh=access)
        prepared = load("guest").prepared_manifest(
            Path(control["output"]), request["source_key"]
        )
        request["prepared_files"] = prepared["files"]
        if args.command == "image":
            image = create_image(args.output, args.size_mib)
            request.update(capacity=image.stat().st_size, serial="FASCIA-INSTALL-FILE")
            control.update(mode="image", image=str(image))
        else:
            devices.assert_identity(identity, devices.inspect(args.disk, args.capacity))
            request.update(capacity=args.capacity, serial="FASCIA-INSTALL-CARD")
            control.update(
                mode="card", disk=args.disk, capacity=args.capacity, identity=identity
            )
        atomic_json(Path(control["input"]) / "request.json", request)
        atomic_json(job, control)
        print("Writing and verifying the identified target…", flush=True)
        if args.command == "install":
            subprocess.run(
                ["sudo", sys.executable, str(HOST / "runner.py"), "--job", str(job)],
                check=True,
            )
        else:
            worker(job)
        if args.command == "image":
            print("Test image verified: " + str(image), flush=True)
        else:
            print(
                "Fascia installation verified. Insert the card into the frame and follow touchscreen setup.",
                flush=True,
            )


if __name__ == "__main__":
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print("Installation cancelled.", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("Installer stopped: " + str(error), file=sys.stderr)
        raise SystemExit(1)
