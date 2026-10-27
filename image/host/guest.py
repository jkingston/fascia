#!/usr/bin/env python3
"""Linux backend for a host-controlled Fascia SD installation.

Preparation builds a portable, finalized Arch root in RAM. Installation opens only
an explicitly authorized target after all prepared artifacts have been checked.
"""

import argparse
import base64
import contextlib
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import struct
import subprocess
import tempfile
import time
import zlib

BOARD = "pf1007l-pl5"
SERIALS = {"FASCIA-INSTALL-FILE", "FASCIA-INSTALL-CARD"}
ARTIFACTS = ("runtime.tar.zst", "board.tar.zst", "initramfs.img")
INITIAL_HOOKS = (
    "00-fascia-recovery.hook",
    "99-fascia-board-refresh.hook",
    "90-mkinitcpio-install.hook",
    "60-mkinitcpio-remove.hook",
    "70-dkms-install.hook",
    "70-dkms-upgrade.hook",
    "71-dkms-remove.hook",
    "60-depmod.hook",
)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def tree_hash(path, source_only=False):
    """Hash source names, executable bits, symlink targets and file contents."""
    h = hashlib.sha256()
    root = Path(path)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Expected a regular input tree")
    for p in sorted(root.rglob("*")):
        relative = p.relative_to(root)
        if any(
            part in {".git", ".local", ".zig-cache", "zig-out", "__pycache__"}
            for part in relative.parts
        ):
            continue
        if source_only and (
            p.suffix.lower() == ".md"
            or p.name.startswith("test")
            or "tests" in relative.parts
        ):
            continue
        info = p.lstat()
        if source_only and stat.S_ISDIR(info.st_mode):
            continue
        h.update(relative.as_posix().encode() + b"\0")
        if stat.S_ISLNK(info.st_mode):
            h.update(b"link\0" + os.readlink(p).encode() + b"\0")
        elif stat.S_ISREG(info.st_mode):
            h.update(b"file\0" + bytes([bool(info.st_mode & 0o111)]))
            h.update(bytes.fromhex(digest(p)))
        elif stat.S_ISDIR(info.st_mode):
            h.update(b"directory\0")
        else:
            raise ValueError("Special file in public inputs")
    return h.hexdigest()


def source_key(input_dir):
    """Runtime recipe + compiled release; documentation cannot force a rebuild."""
    p = Path(input_dir)
    h = hashlib.sha256()
    for name in ("image", "tools", "scripts"):
        h.update(
            name.encode()
            + bytes.fromhex(tree_hash(p / "project" / name, source_only=True))
        )
    h.update(b"release" + bytes.fromhex(tree_hash(p / "release")))
    initial = p / "initial-apps"
    if initial.exists():
        h.update(b"initial-apps" + bytes.fromhex(tree_hash(initial)))
    base = p / "rootfs-input.json"
    if base.exists():
        h.update(b"official-rootfs" + bytes.fromhex(digest(base)))
    return h.hexdigest()


def board_build_key(project, base_digest, installed_packages, kernel_references=None):
    """Board recipes plus the actual signed base and resolved build toolchain."""
    project = Path(project)
    if not re.fullmatch("[a-f0-9]{64}", base_digest):
        raise ValueError("Invalid official base digest")
    h = hashlib.sha256(b"Fascia rolling board build schema 2\0")
    for relative in ("image/boards/pf1007l", "image/arch/fascia-board-support"):
        h.update(
            relative.encode()
            + bytes.fromhex(tree_hash(project / relative, source_only=True))
        )
    for relative in (
        "image/sources.lock.json",
        "image/host/sources.lock.json",
        "image/arch/build-package.py",
    ):
        h.update(relative.encode() + bytes.fromhex(digest(project / relative)))
    h.update(bytes.fromhex(base_digest))
    h.update(
        json.dumps(installed_packages, sort_keys=True, separators=(",", ":")).encode()
    )
    h.update(
        json.dumps(
            kernel_references or {}, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    return h.hexdigest()


def request(input_dir):
    p = Path(input_dir)
    value = json.loads((p / "request.json").read_text())
    if value.get("schema_version") != 1 or value.get("board_id") != BOARD:
        raise ValueError("Unsupported install request")
    if value.get("phase") not in {"prepare", "install"}:
        raise ValueError("Invalid request phase")
    if not isinstance(value.get("source_key"), str) or not re.fullmatch(
        "[a-f0-9]{64}", value["source_key"]
    ):
        raise ValueError("Invalid source key")
    if source_key(p) != value["source_key"]:
        raise ValueError("Public source/release changed after confirmation")
    return value


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as f:
        json.dump(value, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def block_counters(path):
    values = [int(value) for value in Path(path).read_text().split()]
    if len(values) < 11:
        raise ValueError("Incomplete kernel block statistics")
    return {
        "reads": values[0],
        "read_bytes": values[2] * 512,
        "writes": values[4],
        "write_bytes": values[6] * 512,
        "discards": values[11] if len(values) > 11 else 0,
        "discard_bytes": values[13] * 512 if len(values) > 13 else 0,
        "flushes": values[15] if len(values) > 15 else 0,
    }


class Runner:
    def __init__(self, output):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.stages = []
        self.target = None
        self.sync_mounts = []

    def run(self, args, **kwargs):
        print("FASCIA_COMMAND " + json.dumps(list(map(str, args))), flush=True)
        with (self.output / "guest.log").open("a") as log:
            result = subprocess.run(
                list(map(str, args)), stdout=log, stderr=subprocess.STDOUT, **kwargs
            )
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, args)

    @contextlib.contextmanager
    def phase(self, name):
        started = time.monotonic()
        print("FASCIA_STAGE " + name, flush=True)
        record = {"stage": name, "state": "running"}
        if self.target is not None:
            record["io_before"] = block_counters(
                Path("/sys/class/block") / self.target.name / "stat"
            )
        self.stages.append(record)
        atomic_json(self.output / "status.json", {"stages": self.stages})
        try:
            yield
            for mount in self.sync_mounts:
                if Path(mount).is_mount():
                    self.run(["sync", "-f", mount])
        except BaseException as error:
            record.update(
                state="failed",
                error=str(error),
                seconds=round(time.monotonic() - started, 3),
            )
            raise
        else:
            record.update(
                state="complete", seconds=round(time.monotonic() - started, 3)
            )
        finally:
            if self.target is not None:
                record["io_after"] = block_counters(
                    Path("/sys/class/block") / self.target.name / "stat"
                )
                record["io_delta"] = {
                    key: record["io_after"][key] - value
                    for key, value in record["io_before"].items()
                }
            atomic_json(self.output / "status.json", {"stages": self.stages})


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extract_archive(archive, destination, runner):
    destination.mkdir(parents=True, exist_ok=True)
    runner.run(
        [
            "tar",
            "--numeric-owner",
            "--xattrs",
            "--acls",
            "-xf",
            archive,
            "-C",
            destination,
        ]
    )


def chroot_agents(root, proc=Path("/proc")):
    found = []
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            if (process / "root").resolve() == root.resolve() and (
                process / "comm"
            ).read_text().strip() in {"gpg-agent", "dirmngr"}:
                found.append(int(process.name))
        except (OSError, RuntimeError):
            continue
    return found


def stop_chroot_agents(root, runner):
    if (root / "usr/bin/gpgconf").exists():
        try:
            runner.run(["chroot", root, "/usr/bin/gpgconf", "--kill", "all"])
        except subprocess.CalledProcessError:
            pass
    for sig, timeout in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
        for pid in chroot_agents(root):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + timeout
        while chroot_agents(root) and time.monotonic() < deadline:
            time.sleep(0.05)
    if chroot_agents(root):
        raise RuntimeError("Bound GPG processes did not exit; refusing lazy unmount")


def unmount_checked(target, runner, timeout=8):
    """Allow short-lived chroot children to close /dev; never detach lazily."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            runner.run(["umount", "-R", target])
            return
        except subprocess.CalledProcessError:
            holders = []
            root = Path(target).parent.resolve()
            for process in Path("/proc").iterdir():
                if not process.name.isdigit():
                    continue
                try:
                    if (process / "root").resolve() == root:
                        holders.append(
                            {
                                "pid": int(process.name),
                                "name": (process / "comm").read_text().strip(),
                            }
                        )
                except (OSError, RuntimeError):
                    pass
            print(
                "FASCIA_UNMOUNT_WAIT "
                + json.dumps({"mount": str(target), "bound_processes": holders}),
                flush=True,
            )
            if not Path(target).is_mount() or time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


@contextlib.contextmanager
def api_mounts(root, runner):
    root = Path(root)
    already_mounted = root.is_mount()
    mountinfo = Path("/proc/self/mountinfo")
    if mountinfo.exists():
        # is_mount alone does not recognize same-filesystem bind mounts.
        for line in mountinfo.read_text().splitlines():
            escaped = line.split()[4]
            name = re.sub(
                r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), escaped
            )
            if Path(name) == root.resolve():
                already_mounted = True
                break
    if root.is_symlink() or not root.is_dir() or already_mounted:
        raise ValueError("API root must be an unmounted isolated directory")
    mounted = []
    try:
        # Self-bind the chroot so libalpm CheckSpace can resolve its root mount.
        runner.run(["mount", "--bind", root, root])
        mounted.append(root)
        for name, kind, options in (
            ("proc", "proc", "ro,nosuid,nodev,noexec"),
            ("sys", "sysfs", "ro,nosuid,nodev,noexec"),
            ("dev", "tmpfs", "mode=755,nosuid"),
        ):
            target = root / name
            target.mkdir(parents=True, exist_ok=True)
            runner.run(["mount", "-t", kind, "-o", options, kind, target])
            mounted.append(target)
        for name, major, minor in (
            ("null", 1, 3),
            ("zero", 1, 5),
            ("random", 1, 8),
            ("urandom", 1, 9),
            ("tty", 5, 0),
        ):
            os.mknod(
                root / "dev" / name, stat.S_IFCHR | 0o666, os.makedev(major, minor)
            )
            (root / "dev" / name).chmod(0o666)
        (root / "dev/fd").symlink_to("/proc/self/fd")
        yield
    finally:
        stop_chroot_agents(root, runner)
        for target in reversed(mounted):
            unmount_checked(target, runner)


@contextlib.contextmanager
def build_mounts(root, input_dir, work, runner):
    """Never expose the host's raw device tree to an ARM package scriptlet."""
    mounted = []
    with api_mounts(root, runner):
        try:
            for name, source in (("input", input_dir), ("work", work)):
                target = root / name
                target.mkdir(parents=True, exist_ok=True)
                runner.run(["mount", "--bind", source, target])
                mounted.append(target)
                if name == "input":
                    runner.run(["mount", "-o", "remount,bind,ro", target])
            yield
        finally:
            for target in reversed(mounted):
                unmount_checked(target, runner)


@contextlib.contextmanager
def runtime_alias(bootstrap, runtime, runner):
    """Expose the sanitized runtime API mounts through the private build chroot."""
    target = bootstrap / "run/fascia-runtime-target"
    target.mkdir(parents=True, exist_ok=True)
    runner.run(["mount", "--rbind", runtime, target])
    try:
        yield "/run/fascia-runtime-target"
    finally:
        stop_chroot_agents(runtime, runner)
        unmount_checked(target, runner)


def chroot_path(input_dir, path):
    return (
        "/input/"
        + Path(path).resolve().relative_to(Path(input_dir).resolve()).as_posix()
    )


def create_boot_script(path):
    text = b"# Fascia SD extlinux\nif sysboot mmc 0:1 any ${pxefile_addr_r} /extlinux/extlinux.conf; then\n echo Fascia boot returned unexpectedly\nfi\n"
    data = struct.pack(">II", len(text), 0) + text
    header = struct.pack(
        ">7I4B32s",
        0x27051956,
        0,
        0,
        len(data),
        0,
        0,
        zlib.crc32(data),
        5,
        2,
        6,
        0,
        b"Fascia SD extlinux",
    )
    path.write_bytes(
        header[:4] + struct.pack(">I", zlib.crc32(header)) + header[8:] + data
    )


def prepared_directory(output, key):
    if not re.fullmatch("[a-f0-9]{64}", key):
        raise ValueError("Invalid prepared key")
    return Path(output)


def verify_prepared(directory, key):
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("Prepared manifest cannot be a symlink")
    manifest = json.loads(manifest_path.read_text())
    if (
        manifest.get("schema_version") != 1
        or manifest.get("source_key") != key
        or manifest.get("board_id") != BOARD
    ):
        raise ValueError("Prepared artifacts belong to different inputs")
    if set(manifest.get("files", {})) != set(ARTIFACTS):
        raise ValueError("Incomplete prepared artifacts")
    for name, record in manifest["files"].items():
        path = directory / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != record["bytes"]
            or digest(path) != record["sha256"]
        ):
            raise ValueError("Prepared artifact changed: " + name)
    return manifest


def prepared_manifest(output_dir, key):
    return verify_prepared(prepared_directory(output_dir, key), key)


def export_board_cache(work, staging, key, runner):
    files = {}
    for prefix in ("board/boot", "board/modules"):
        for path in sorted((work / prefix).rglob("*")):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError("Unexpected board cache entry")
            if path.is_file():
                files[path.relative_to(work).as_posix()] = {
                    "bytes": path.stat().st_size,
                    "sha256": digest(path),
                    "mode": stat.S_IMODE(path.stat().st_mode),
                }
    for path in (work / "dkms-package").glob("*.pkg.tar.*"):
        if not path.name.endswith(".sig"):
            files[path.relative_to(work).as_posix()] = {
                "bytes": path.stat().st_size,
                "sha256": digest(path),
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
    path = work / "board/manifest.json"
    files["board/manifest.json"] = {
        "bytes": path.stat().st_size,
        "sha256": digest(path),
        "mode": 0o644,
    }
    if (
        len(
            [
                name
                for name in files
                if name.startswith("dkms-package/")
                and name.endswith((".pkg.tar.xz", ".pkg.tar.zst"))
            ]
        )
        != 1
    ):
        raise ValueError("Expected one board source package in cache")
    destination = staging / "board-cache.tar.gz"
    runner.run(
        [
            "tar",
            "--numeric-owner",
            "-I",
            "gzip -6",
            "-cf",
            destination,
            "-C",
            work,
            *sorted(files),
        ]
    )
    return {
        "schema_version": 1,
        "key": key,
        "file": destination.name,
        "bytes": destination.stat().st_size,
        "sha256": digest(destination),
        "files": files,
    }


def valid_board_record(record):
    if (
        record.get("schema_version") != 1
        or not re.fullmatch("[a-f0-9]{64}", record.get("key", ""))
        or record.get("file") != "board-cache.tar.gz"
    ):
        raise ValueError("Invalid board cache manifest")
    if (
        not isinstance(record.get("files"), dict)
        or not record["files"]
        or len(record["files"]) > 32
    ):
        raise ValueError("Invalid board cache file set")
    for name, item in record["files"].items():
        path = Path(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not (
                name.startswith(("board/boot/", "board/modules/"))
                or name == "board/manifest.json"
                or (name.startswith("dkms-package/") and len(path.parts) == 2)
            )
        ):
            raise ValueError("Unsafe board cache path")
        if (
            item["bytes"] < 0
            or item["bytes"] > 64 * 1024**2
            or not re.fullmatch("[a-f0-9]{64}", item["sha256"])
            or item["mode"] & 0o7022
        ):
            raise ValueError("Unsafe board cache metadata")


def verify_board_record(archive, record):
    valid_board_record(record)
    archive = Path(archive)
    if (
        archive.is_symlink()
        or not archive.is_file()
        or archive.stat().st_size != record["bytes"]
        or digest(archive) != record["sha256"]
    ):
        raise ValueError("Board cache archive changed")


def check_board_members(archive, record, work=None):
    """Verify every member before reuse; optionally write only fresh RAM files."""
    import tarfile

    seen = set()
    with tarfile.open(archive, mode="r:gz") as source:
        for member in source:
            name = member.name.rstrip("/")
            path = Path(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or member.issym()
                or member.islnk()
            ):
                raise ValueError("Unsafe board cache archive entry")
            if member.isdir():
                if not any(f.startswith(name + "/") for f in record["files"]):
                    raise ValueError("Unlisted cache directory")
                continue
            expected = record["files"].get(name)
            if (
                not member.isfile()
                or expected is None
                or name in seen
                or member.size != expected["bytes"]
                or stat.S_IMODE(member.mode) != expected["mode"]
            ):
                raise ValueError("Unlisted, duplicate or changed cache file")
            h = hashlib.sha256()
            with source.extractfile(member) as data:
                output = None
                try:
                    if work is not None:
                        destination = work / path
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        output = destination.open("xb")
                    while block := data.read(1024 * 1024):
                        h.update(block)
                        if output is not None:
                            output.write(block)
                finally:
                    if output is not None:
                        output.close()
            if h.hexdigest() != expected["sha256"]:
                raise ValueError("Board cache member checksum mismatch")
            if work is not None:
                destination.chmod(expected["mode"])
            seen.add(name)
    if seen != set(record["files"]):
        raise ValueError("Incomplete board cache")


def restore_board_cache(cache, work, key):
    """Authenticate archive and member bytes before using public cached builds."""
    folder = Path(cache) / "boards" / key
    if not folder.exists():
        return False
    record = json.loads((folder / "manifest.json").read_text())
    if record.get("key") != key:
        raise ValueError("Board cache belongs to another recipe")
    archive = folder / "board-cache.tar.gz"
    verify_board_record(archive, record)
    check_board_members(archive, record, work)
    return True


def publish_board_checkpoint(work, output, key, runner):
    output = Path(output)
    # A refresh must not replace the board archive referenced by a completed
    # preparation before its new runtime and manifest have been exported.
    if (output / "manifest.json").exists():
        return None
    staging = Path(tempfile.mkdtemp(prefix=".board-checkpoint-", dir=output))
    try:
        record = export_board_cache(work, staging, key, runner)
        os.replace(staging / "board-cache.tar.gz", output / "board-cache.tar.gz")
        atomic_json(output / "board-checkpoint.json", {"board_cache": record})
        return record
    finally:
        shutil.rmtree(staging)


def promote_board_cache(output_dir, download_cache):
    """Host-side promotion of a completed preparation's optional portable board cache."""
    output_dir = Path(output_dir)
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        manifest_path = output_dir / "board-checkpoint.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text())
    record = manifest.get("board_cache")
    if record is None:
        return  # Older successful preparations have no separate board cache.
    archive = output_dir / "board-cache.tar.gz"
    verify_board_record(archive, record)
    check_board_members(archive, record)
    parent = Path(download_cache) / "boards"
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / record["key"]
    if target.exists():
        existing = json.loads((target / "manifest.json").read_text())
        verify_board_record(target / "board-cache.tar.gz", existing)
        check_board_members(target / "board-cache.tar.gz", existing)
        if existing["files"] != record["files"]:
            raise ValueError("Existing board cache differs for the same recipe")
        return
    staging = Path(tempfile.mkdtemp(prefix=".board-", dir=parent))
    try:
        shutil.copyfile(archive, staging / "board-cache.tar.gz")
        atomic_json(staging / "manifest.json", record)
        os.rename(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def preflight_filesystems(work_base, runner):
    """Exercise actual filesystem/module capabilities on owned RAM file loops only."""
    pinned = load_module(
        "fascia_preflight_loops", Path(__file__).with_name("linux-target.py")
    )
    with tempfile.TemporaryDirectory(
        prefix="fascia-fs-preflight-", dir=work_base
    ) as temporary:
        work = Path(temporary)
        for kind, size in (("vfat", 64 * 1024**2), ("btrfs", 256 * 1024**2)):
            backing = work / (kind + ".img")
            mount = work / kind
            mount.mkdir()
            with backing.open("x+b") as stream:
                stream.truncate(size)
                with pinned.attach(stream.fileno(), size) as mapping:
                    device = mapping["path"]
                    if kind == "vfat":
                        runner.run(["mkfs.vfat", "-F", "32", device])
                    else:
                        runner.run(["mkfs.btrfs", "-f", "-K", device])
                    runner.run(["mount", "-t", kind, device, mount])
                    try:
                        marker = mount / "capability.txt"
                        marker.write_bytes(b"Fascia installer filesystem preflight\n")
                        runner.run(["sync", "-f", mount])
                        if (
                            marker.read_bytes()
                            != b"Fascia installer filesystem preflight\n"
                        ):
                            raise ValueError("Filesystem preflight write/read failed")
                    finally:
                        unmount_checked(mount, runner)


def binary_keyring(keyring, directory):
    keyring = Path(keyring)
    if keyring.read_bytes().startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----"):
        text = keyring.read_text().splitlines()
        body = []
        started = False
        for line in text[1:]:
            if not started:
                started = line == ""
                continue
            if line.startswith(("=", "-----END")):
                break
            body.append(line)
        raw = base64.b64decode("".join(body), validate=True)
        directory.mkdir(parents=True, exist_ok=True)
        keyring = directory / "rootfs-trust.gpg"
        keyring.write_bytes(raw)
    return keyring


def verify_rootfs(resolved, directory, runner):
    archive, signature = Path(resolved["rootfs"]), Path(resolved["rootfs_signature"])
    if any(p.is_symlink() or not p.is_file() for p in (archive, signature)):
        raise ValueError("Official base and signature must be regular files")
    actual = digest(archive)
    if actual != resolved["rootfs_sha256"]:
        raise ValueError("Official base digest changed")
    expected = resolved.get(
        "fingerprint", resolved.get("trust_fingerprint", "")
    ).upper()
    if not re.fullmatch("[A-F0-9]{40}", expected):
        raise ValueError("Missing reviewed official base signing fingerprint")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    keyring = binary_keyring(resolved["trust_keyring"], directory)
    status = directory / "rootfs-signature.status"
    status.unlink(missing_ok=True)

    def mark_rejected():
        atomic_json(
            Path(runner.output) / "rejected-rootfs.json",
            {
                "schema_version": 1,
                "rootfs_sha256": actual,
                "signature_sha256": digest(signature),
            },
        )

    try:
        with status.open("wb") as stream:
            runner.run(
                [
                    "gpgv",
                    "--keyring",
                    keyring,
                    "--status-fd",
                    str(stream.fileno()),
                    signature,
                    archive,
                ],
                pass_fds=(stream.fileno(),),
            )
    except subprocess.CalledProcessError as error:
        mark_rejected()
        raise ValueError(
            "Official base signature verification failed; cached base/signature pair was rejected"
        ) from error

    valid = []
    for line in status.read_text().splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[:2] == ["[GNUPG:]", "VALIDSIG"]:
            valid.append((fields[2].upper(), fields[-1].upper()))
    if not valid or any(expected not in pair for pair in valid):
        mark_rejected()
        raise ValueError(
            "Official base signer differs from reviewed trust anchor; cached base/signature pair was rejected"
        )

    return actual


def initial_hooks(root):
    directory = root / "etc/pacman.d/hooks"
    directory.mkdir(parents=True, exist_ok=True)
    for name in INITIAL_HOOKS:
        path = directory / name
        if path.exists() or path.is_symlink():
            raise ValueError("Unexpected pre-existing Fascia/bootstrap hook: " + name)
        path.symlink_to("/dev/null")


def remove_initial_hooks(root):
    for name in INITIAL_HOOKS:
        path = root / "etc/pacman.d/hooks" / name
        if path.is_symlink() and os.readlink(path) == "/dev/null":
            path.unlink()


def initialize_rolling_root(root, archive, runner):
    root.mkdir(parents=True)
    # Firmware is supplied only by the device's read-only vendor provisioner.
    # Its package records are removed normally below, never edited by hand.
    runner.run(
        [
            "tar",
            "--numeric-owner",
            "--xattrs",
            "--acls",
            "--exclude=usr/lib/firmware",
            "--exclude=./usr/lib/firmware",
            "-xf",
            archive,
            "-C",
            root,
        ]
    )
    (root / ".fascia-image-build-root").write_text("isolated\n")
    for name in ("run", "tmp", "root", "usr/lib/firmware"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "tmp").chmod(0o1777)
    resolv = root / "etc/resolv.conf"
    resolv.unlink(missing_ok=True)
    resolv.write_bytes(Path("/etc/resolv.conf").read_bytes())
    mirror = root / "etc/pacman.d/mirrorlist"
    mirror.write_text(
        "Server = https://fl.us.mirror.archlinuxarm.org/$arch/$repo\n"
        "Server = https://mirrors.dotsrc.org/archlinuxarm/$arch/$repo\n"
        "Server = https://mirror.math.princeton.edu/pub/archlinuxarm/$arch/$repo\n"
    )
    config = root / "etc/pacman.conf"
    text, count = re.subn(
        r"(?m)^Architecture\s*=.*$", "Architecture = armv7h", config.read_text()
    )
    if count == 0:
        text = text.replace("[options]", "[options]\nArchitecture = armv7h", 1)
    # NoExtract applies before upgrades, and keeps wireless-regdb available.
    text = text.replace(
        "[options]",
        "[options]\nNoExtract = usr/lib/firmware/* !usr/lib/firmware/regulatory.db !usr/lib/firmware/regulatory.db.p7s",
        1,
    )
    config.write_text(text)
    initial_hooks(root)


def installed_packages(root):
    text = subprocess.check_output(["chroot", root, "/usr/bin/pacman", "-Q"], text=True)
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if (
            len(fields) != 2
            or fields[0] in result
            or not re.fullmatch(r"[a-zA-Z0-9@._+:-]+", fields[0])
        ):
            raise ValueError("Invalid installed pacman package inventory")
        result[fields[0]] = fields[1]
    if not result:
        raise ValueError("Empty installed pacman inventory")
    return result


def installed_kernel(root, owned_paths=None):
    # Arch Linux ARM does not supply pkgbase. Stale DKMS directories and uname
    # are not authoritative: use the installed kernel package's file ownership.
    if owned_paths is None:
        owned_paths = subprocess.check_output(
            ["chroot", root, "/usr/bin/pacman", "-Qql", "linux-armv7"], text=True
        ).splitlines()
    expression = re.compile(r"/?usr/lib/modules/([0-9][A-Za-z0-9_.+\-]*)(?:/|$)")
    choices = {
        match.group(1) for line in owned_paths if (match := expression.match(line))
    }
    if len(choices) != 1:
        raise ValueError("Expected one installed linux-armv7 kernel")
    release = choices.pop()
    if not (root / "usr/lib/modules" / release).is_dir():
        raise ValueError("Installed kernel module directory missing")
    return release


def kernel_reference_hashes(root):
    kernel = installed_kernel(root)
    names = [
        f"/usr/lib/modules/{kernel}/build/{name}"
        for name in (
            ".config",
            "Module.symvers",
            "include/config/kernel.release",
            "include/generated/utsrelease.h",
        )
    ]
    text = subprocess.check_output(
        ["chroot", root, "/usr/bin/sha256sum", *names], text=True
    )
    result = {}
    for line in text.splitlines():
        value, path = line.split(maxsplit=1)
        if (
            not re.fullmatch("[a-f0-9]{64}", value)
            or path not in names
            or path in result
        ):
            raise ValueError("Invalid installed kernel reference digest")
        result[path] = value
    if set(result) != set(names):
        raise ValueError("Incomplete installed kernel reference digests")
    return result


def pinned_upgrade(root, names, runner, repository, expected):
    if not names or any(
        not re.fullmatch(r"[a-zA-Z0-9@_+][a-zA-Z0-9@._+:-]*", name) for name in names
    ):
        raise ValueError("Invalid pinned package profile")
    runner.run(["chroot", root, "/usr/bin/pacman-key", "--init"])
    runner.run(
        [
            "chroot",
            root,
            "/usr/bin/pacman-key",
            "--populate",
            "archlinuxarm",
            "archlinux",
        ]
    )
    firmware = [
        name
        for name in installed_packages(root)
        if name == "linux-firmware" or name.startswith("linux-firmware-")
    ]
    target = root / "run/fascia-packages"
    target.mkdir(exist_ok=True)
    config = root / "run/fascia-packages.conf"
    config.write_text(
        "[options]\nArchitecture = armv7h\nSigLevel = Required DatabaseOptional\n"
        "NoExtract = usr/lib/firmware/* !usr/lib/firmware/regulatory.db !usr/lib/firmware/regulatory.db.p7s\n"
        + "".join(
            f"[{name}]\nServer = file:///run/fascia-packages\n"
            for name in ("core", "extra", "alarm")
        )
    )
    runner.run(["mount", "--bind", repository, target])
    try:
        runner.run(["mount", "-o", "remount,bind,ro", target])
        command = [
            "chroot",
            root,
            "/usr/bin/pacman",
            "--disable-sandbox",
            "--config",
            "/run/fascia-packages.conf",
            "--noconfirm",
        ]
        if firmware:
            run_package_transaction(command + ["-Rdd", *sorted(firmware)], runner)
        run_package_transaction(
            command + ["-Syu", "--needed", *sorted(set(names))], runner
        )
        run_package_transaction(command + ["-S", "wireless-regdb"], runner)
        if installed_packages(root) != expected:
            raise ValueError(
                "Installed packages differ from the pinned installation inventory"
            )
    finally:
        unmount_checked(target, runner)
        config.unlink()
        target.rmdir()


def prepare(input_dir, output, req, runner, work_base=Path("/work")):
    if req.get("offline") and req.get("refresh"):
        raise ValueError(
            "Refresh requires online preparation; --offline cannot refresh"
        )
    with runner.phase("preflight-filesystems"):
        preflight_filesystems(work_base, runner)
    prepared = prepared_directory(output, req["source_key"])
    if (prepared / "manifest.json").exists() and not req.get("refresh", False):
        with runner.phase("verify-cached-preparation"):
            verify_prepared(prepared, req["source_key"])
        return prepared
    project = input_dir / "project"
    inputs = load_module("fascia_host_inputs", project / "image/host/inputs.py")
    with runner.phase("verify-official-base"):
        resolved = inputs.prepare(
            input_dir / "cache", offline=True, derived_dir=work_base / "derived-inputs"
        )
        base_digest = verify_rootfs(resolved, work_base / "derived-inputs", runner)
        base_record = json.loads((input_dir / "rootfs-input.json").read_text())
        if (
            base_record.get("schema_version") != 1
            or base_record.get("rootfs_sha256") != base_digest
            or base_record.get("signature_sha256")
            != digest(resolved["rootfs_signature"])
        ):
            raise ValueError("Official base differs from confirmed source inputs")
    with tempfile.TemporaryDirectory(prefix="fascia-host-", dir=work_base) as temporary:
        work = Path(temporary)
        bootstrap, runtime = work / "build-root", work / "runtime-root"
        with runner.phase("bootstrap-build-environment"):
            initialize_rolling_root(bootstrap, resolved["rootfs"], runner)
            source_manifest = json.loads(
                Path(resolved["offline_source_manifest"]).read_text()
            )
            source_manifest["cache_root"] = "/input/cache"
            for record in [
                *source_manifest["archives"].values(),
                *source_manifest["files"],
            ]:
                record["file"] = chroot_path(input_dir, record["file"])
            atomic_json(bootstrap / "run/fascia-source-manifest.json", source_manifest)
            with build_mounts(bootstrap, input_dir, work, runner):
                pinned_upgrade(
                    bootstrap,
                    resolved["build_names"],
                    runner,
                    resolved["package_repository"],
                    resolved["package_lock"]["build_packages"],
                )
                build_packages = installed_packages(bootstrap)
                board_key = board_build_key(
                    project,
                    base_digest,
                    build_packages,
                    kernel_reference_hashes(bootstrap),
                )
                with runner.phase("verify-board-cache"):
                    board_cached = restore_board_cache(
                        input_dir / "cache", work, board_key
                    )
                if not board_cached:
                    with runner.phase("build-board-from-public-sources"):
                        runner.run(
                            [
                                "chroot",
                                bootstrap,
                                "/usr/bin/python3",
                                "/input/project/image/boards/pf1007l/build_board.py",
                                "--source-dir",
                                "/work/sources",
                                "--cache-dir",
                                "/work/source-cache",
                                "--output-dir",
                                "/work/board",
                                "--offline-source-manifest",
                                "/run/fascia-source-manifest.json",
                                "--kernel-root",
                                "/",
                            ]
                        )
                    with runner.phase("build-source-dkms-package"):
                        (bootstrap / "build").mkdir(exist_ok=True)
                        runner.run(
                            [
                                "python3",
                                project / "image/arch/build-package.py",
                                "--root",
                                bootstrap,
                                "--sources",
                                work / "board/sources",
                                "--output",
                                work / "dkms-package",
                            ]
                        )
                with runner.phase("checkpoint-verified-board"):
                    publish_board_checkpoint(work, output, board_key, runner)
                with runner.phase("install-runtime-packages"):
                    runtime_packages = install_runtime(
                        bootstrap, runtime, input_dir, resolved, work, runner
                    )
                    for name in ("linux-armv7", "linux-armv7-headers"):
                        if runtime_packages.get(name) != build_packages.get(name):
                            raise ValueError(
                                "Pinned build and runtime kernels differ: " + name
                            )
                    provenance = runtime / "usr/share/fascia/build-inputs"
                    provenance.mkdir(parents=True, exist_ok=True)
                    atomic_json(
                        provenance / "packages.json",
                        {
                            "rootfs_sha256": base_digest,
                            "build": build_packages,
                            "runtime": runtime_packages,
                        },
                    )
        with runner.phase("finalize-runtime"):
            finalize_runtime(project, runtime, input_dir / "release", work, runner)
        with runner.phase("export-portable-artifacts"):
            prepared.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".preparing-", dir=prepared))
            try:
                runner.run(
                    [
                        "tar",
                        "--numeric-owner",
                        "--xattrs",
                        "--acls",
                        "-I",
                        "zstd -3 -T0",
                        "-cf",
                        staging / "runtime.tar.zst",
                        "-C",
                        runtime,
                        ".",
                    ]
                )
                runner.run(
                    [
                        "tar",
                        "--numeric-owner",
                        "-I",
                        "zstd -3 -T0",
                        "-cf",
                        staging / "board.tar.zst",
                        "-C",
                        work / "board",
                        "boot",
                        "manifest.json",
                    ]
                )
                shutil.copy2(work / "initramfs.img", staging / "initramfs.img")
                board_cache = export_board_cache(work, staging, board_key, runner)
                files = {
                    name: {
                        "bytes": (staging / name).stat().st_size,
                        "sha256": digest(staging / name),
                    }
                    for name in ARTIFACTS
                }
                atomic_json(
                    staging / "manifest.json",
                    {
                        "schema_version": 1,
                        "board_id": BOARD,
                        "source_key": req["source_key"],
                        "files": files,
                        "runtime_names": sorted(runtime_packages),
                        "rootfs_sha256": base_digest,
                        "rootfs_signature_sha256": digest(resolved["rootfs_signature"]),
                        "rootfs_signer": resolved.get(
                            "trust_fingerprint", resolved.get("fingerprint")
                        ),
                        "build_packages": build_packages,
                        "runtime_packages": runtime_packages,
                        "board_cache": board_cache,
                        "initial_modules": "Built from public sources; runtime DKMS installation verified",
                    },
                )
                for name in ARTIFACTS:
                    os.replace(staging / name, prepared / name)
                os.replace(
                    staging / "board-cache.tar.gz", prepared / "board-cache.tar.gz"
                )
                os.replace(staging / "manifest.json", prepared / "manifest.json")
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
    return prepared


def retryable_package_download(text):
    """Recognize transport-only aborts before libalpm can change packages."""
    text = text.lower()
    forbidden = (
        b"processing package changes",
        b"pre-transaction hooks",
        b"post-transaction hooks",
        b"command failed to execute correctly",
        b"/proc is not mounted",
        b"signature",
        b"unknown trust",
        b"corrupt",
        b"checksum",
        b"disk space",
        b"no space left",
        b"could not determine",
        b"failed to run transaction hooks",
    )
    if any(value in text for value in forbidden) or re.search(
        rb"\(\s*\d+/\d+\)\s+(?:installing|upgrading|downgrading|reinstalling|removing)\b",
        text,
    ):
        return False
    transport = (
        b"operation too slow",
        b"connection timed out",
        b"resolving timed out",
        b"could not resolve host",
        b"could not resolve proxy",
        b"failed to connect",
        b"connection reset by peer",
        b"broken pipe",
        b"ssl connection timeout",
        b"transfer closed with",
        b"was not closed cleanly",
    )
    seen = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith(b"error:"):
            continue
        if b"failed retrieving file" in line and any(
            value in line for value in transport
        ):
            seen = True
        elif line == b"error: failed to commit transaction (unexpected error)":
            continue
        elif line in (
            b"error: failed to synchronize all databases (download library error)",
            b"error: failed to synchronize all databases (failed to retrieve some files)",
        ):
            continue
        else:
            return False  # Includes 404, trust, filesystem and unknown failures.
    return seen


def run_package_transaction(args, runner):
    """Retry only transport-only sync aborts; retain this root's downloaded files."""
    log = runner.output / "guest.log"
    sync = "-Syu" in args or "-S" in args
    for attempt in range(1, 4):
        offset = log.stat().st_size if log.exists() else 0
        try:
            runner.run(args)
        except subprocess.CalledProcessError:
            with log.open("rb") as stream:
                stream.seek(offset)
                text = stream.read().lower()
            if not sync or attempt == 3 or not retryable_package_download(text):
                raise
            message = f"FASCIA_PACKAGE_RETRY attempt={attempt + 1}/3 transport-only abort; keeping downloaded cache"
            print(message, flush=True)
            with log.open("a") as stream:
                stream.write(message + "\n")
            time.sleep(2 * attempt)
            continue
        with log.open("rb") as stream:
            stream.seek(offset)
            text = stream.read().lower()
        if (
            b"error: command failed to execute correctly" in text
            or b"/proc is not mounted" in text
        ):
            raise RuntimeError(
                "Package hook failed; refusing to export incomplete runtime"
            )
        return


def seed_package_cache(bootstrap, runtime):
    """Reuse only completed downloads; pacman still verifies their signatures."""
    source = bootstrap / "var/cache/pacman/pkg"
    target = runtime / "var/cache/pacman/pkg"
    target.mkdir(parents=True, exist_ok=True)
    if source.is_symlink() or target.is_symlink():
        raise ValueError("Package cache must be a regular directory")
    if not source.is_dir():
        return {"files": 0, "bytes": 0}
    archives = [
        path
        for path in source.iterdir()
        if re.fullmatch(r".+\.pkg\.tar\.(?:xz|zst|gz|bz2)", path.name)
        and not path.is_symlink()
        and path.is_file()
    ]
    result = {"files": 0, "bytes": 0}
    for archive in sorted(archives):
        for path in (archive, Path(str(archive) + ".sig")):
            if path.is_symlink() or not path.is_file():
                continue
            destination = target / path.name
            if destination.exists() or destination.is_symlink():
                if (
                    destination.is_symlink()
                    or not destination.is_file()
                    or digest(destination) != digest(path)
                ):
                    raise ValueError(
                        "Conflicting completed package cache entry: " + path.name
                    )
                continue
            # Self-bind mounts can cause EXDEV on the same tmpfs; copy via a temporary file.
            try:
                os.link(path, destination)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=target, prefix=".fascia-cache-", delete=False
                    ) as output:
                        temporary = Path(output.name)
                        with path.open("rb") as source_file:
                            shutil.copyfileobj(source_file, output, length=1024 * 1024)
                    os.replace(temporary, destination)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            result["files"] += 1
            result["bytes"] += path.stat().st_size
    return result


def install_runtime(bootstrap, runtime, input_dir, resolved, work, runner):
    """Separate official base: build-only tools cannot leak into the installed root."""
    initialize_rolling_root(runtime, resolved["rootfs"], runner)
    seeded = seed_package_cache(bootstrap, runtime)
    print("FASCIA_PACKAGE_CACHE_SEEDED " + json.dumps(seeded), flush=True)
    with api_mounts(runtime, runner):
        pinned_upgrade(
            runtime,
            resolved["runtime_names"],
            runner,
            resolved["package_repository"],
            resolved["package_lock"]["runtime_packages"],
        )
        packages = list((work / "dkms-package").glob("*.pkg.tar.*"))
        if len(packages) != 1 or packages[0].name.endswith(".sig"):
            raise ValueError("Expected one locally built board source package")
        # Only this locally built, reviewed source package is unsigned. Repository
        # transactions retain normal mandatory package signature verification.
        local = runtime / "run/fascia-local-board.pkg.tar.zst"
        shutil.copy2(packages[0], local)
        config = runtime / "run/fascia-local.conf"
        config.write_text(
            "[options]\nArchitecture = armv7h\nSigLevel = Required DatabaseOptional\nLocalFileSigLevel = Optional\n"
        )
        run_package_transaction(
            [
                "chroot",
                runtime,
                "/usr/bin/pacman",
                "--disable-sandbox",
                "--config",
                "/run/fascia-local.conf",
                "--noconfirm",
                "-U",
                "/run/fascia-local-board.pkg.tar.zst",
            ],
            runner,
        )
        local.unlink()
        config.unlink()
        for command in (
            ["/usr/bin/journalctl", "--update-catalog"],
            ["/usr/bin/systemd-tmpfiles", "--create"],
        ):
            runner.run(["chroot", runtime] + command)
        versions = installed_packages(runtime)
    remove_initial_hooks(runtime)
    return versions


def elf_interpreter(path):
    """Read ARM32 PT_INTERP without a host readelf dependency."""
    import struct

    with Path(path).open("rb") as stream:
        header = stream.read(52)
        if (
            len(header) != 52
            or header[:7] != b"\x7fELF\x01\x01\x01"
            or struct.unpack_from("<H", header, 18)[0] != 40
        ):
            raise ValueError("Expected a little-endian ARM32 executable: " + str(path))
        offset = struct.unpack_from("<I", header, 28)[0]
        size, count = struct.unpack_from("<HH", header, 42)
        if (
            size != 32
            or count > 256
            or offset + size * count > Path(path).stat().st_size
        ):
            raise ValueError("Invalid ELF program header table")
        stream.seek(offset)
        entries = stream.read(size * count)
        interpreters = []
        for index in range(count):
            values = struct.unpack_from("<8I", entries, index * size)
            if values[0] == 3:
                if not 1 < values[4] <= 256:
                    raise ValueError("Invalid ELF interpreter")
                stream.seek(values[1])
                name = stream.read(values[4])
                if (
                    len(name) != values[4]
                    or not name.endswith(b"\0")
                    or b"\0" in name[:-1]
                ):
                    raise ValueError("Invalid ELF interpreter string")
                interpreters.append(name[:-1].decode("ascii"))
        if len(interpreters) > 1:
            raise ValueError("Duplicate ELF interpreters")
        return interpreters[0] if interpreters else None


def validate_runtime_elfs(root, runner):
    """Resolve shipped dynamic binaries against target libraries without app execution."""
    required = (
        "usr/lib/fascia/bin/fascia-host",
        "usr/local/libexec/fascia-brightness",
        "usr/local/libexec/fascia-home-button",
    )
    optional = ()
    factory = root / "usr/share/fascia/factory-apps"
    if factory.exists():
        installer = load_module(
            "fascia_image_apps", Path(__file__).parents[1] / "install-root.py"
        )
        plan = installer.validate_initial_apps(factory)
        optional += tuple(
            "usr/share/fascia/factory-apps/apps/" + app + "/AppRun"
            for app in plan["apps"]
        )
    for name in (*required, *optional):
        interpreter = elf_interpreter(root / name)
        if interpreter is None:
            continue  # Statically linked helper: no dynamic dependencies.
        if interpreter != "/lib/ld-linux-armhf.so.3":
            raise ValueError("Unexpected shipped ELF interpreter: " + interpreter)
        runner.run(["chroot", root, interpreter, "--list", "/" + name])


def finalize_runtime(project, runtime, release, work, runner):
    kernel = installed_kernel(runtime)
    installer = load_module("fascia_root_installer", project / "image/install-root.py")
    initial_apps = project.parent / "initial-apps"
    installer.finalize(
        installer.Root(runtime),
        release,
        initial_apps if initial_apps.exists() else None,
    )
    board = work / "board"
    destination = runtime / "usr/share/fascia/board"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(board / "boot/dtbs/pf1007l-provision.dtb", destination)
    provenance = runtime / "usr/share/fascia/build-inputs"
    provenance.mkdir(parents=True, exist_ok=True)
    for name in ("sources.lock.json", "profiles.json", "rootfs.json"):
        shutil.copy2(project / "image/host" / name, provenance / name)
    shutil.copy2(
        project.parent / "rootfs-input.json", provenance / "official-rootfs.json"
    )
    shutil.copy2(board / "manifest.json", provenance / "board-manifest.json")
    # Compile/install the package-owned source through normal DKMS in the new root.
    with build_mounts(runtime, project.parent, work, runner):
        for module in ("fascia-panel", "fascia-aic8800"):
            runner.run(
                ["chroot", runtime, "/usr/bin/dkms", "add", "-m", module, "-v", "1.0"]
            )
            runner.run(
                [
                    "chroot",
                    runtime,
                    "/usr/bin/dkms",
                    "install",
                    "--verbose",
                    "-m",
                    module,
                    "-v",
                    "1.0",
                    "-k",
                    kernel,
                ]
            )
        runner.run(["chroot", runtime, "/usr/bin/depmod", kernel])
    for module in (
        "panel-jadard-jd9365da-h3",
        "aic8800_bsp",
        "aic8800_fdrv",
        "aic8800_btlpm",
    ):
        path = subprocess.check_output(
            [
                "chroot",
                runtime,
                "/usr/bin/modinfo",
                "-k",
                kernel,
                "-n",
                module,
            ],
            text=True,
        ).strip()
        if "/updates/dkms/" not in path:
            raise ValueError(
                "Runtime module did not come from package-owned DKMS: " + module
            )
        built = board / "modules" / kernel / "updates" / (module + ".ko")
        expected = subprocess.check_output(
            ["modinfo", "-F", "vermagic", built], text=True
        ).strip()
        actual = subprocess.check_output(
            [
                "chroot",
                runtime,
                "/usr/bin/modinfo",
                "-k",
                kernel,
                "-F",
                "vermagic",
                module,
            ],
            text=True,
        ).strip()
        if actual != expected:
            raise ValueError("Runtime DKMS module vermagic mismatch: " + module)
    runner.run(
        [
            "python3",
            project / "image/tools/build-initramfs.py",
            "--root",
            runtime,
            "--kernel",
            kernel,
            "--output",
            work / "initramfs.img",
        ]
    )
    validate_runtime_elfs(runtime, runner)
    # DKMS can generate a build-time signing private key. Never distribute it.
    (runtime / "var/lib/dkms/mok.key").unlink(missing_ok=True)
    (runtime / ".fascia-image-build-root").unlink()


def validate_target(
    target, req, capacity=None, serial=None, sys_block=Path("/sys/class/block")
):
    if req.get("phase") != "install" or req.get("erase_authorized") is not True:
        raise ValueError("Erasure was not explicitly authorized")
    if target is None:
        raise ValueError("No target supplied")
    target = Path(target)
    if target.is_symlink() or not stat.S_ISBLK(target.stat().st_mode):
        raise ValueError("Target must be the explicitly validated block device")
    linux = req.get("linux_device")
    if linux is not None:
        if not isinstance(linux, dict) or str(target) != linux.get("path"):
            raise ValueError("Linux target path changed")
        info = target.stat()
        if (os.major(info.st_rdev), os.minor(info.st_rdev)) != (
            linux.get("major"),
            linux.get("minor"),
        ):
            raise ValueError("Linux target device identity changed")
        if capacity != req.get("capacity") or capacity != linux.get("capacity"):
            raise ValueError("Linux target capacity arguments changed")
        pinned = load_module(
            "fascia_pinned_target", Path(__file__).with_name("linux-target.py")
        )
        with target.open("rb", buffering=0) as disk:
            size = pinned.block_size(disk.fileno())
        if (
            size != capacity
            or type(capacity) is not int
            or capacity < 4868 * 1024**2
            or capacity % 512
        ):
            raise ValueError("Linux target capacity changed")
        if not target.name.startswith("loop") or not isinstance(
            linux.get("loop_identity"), dict
        ):
            raise ValueError(
                "Linux target requires an owned descriptor-pinned loop mapping"
            )
        pinned.verify(target, linux["loop_identity"])
        pinned.validate_backing(linux.get("backing_fd"), linux["loop_identity"])
        return target
    if target != Path("/dev/vda"):
        raise ValueError("Expected the helper's sole validated virtio target")
    if (
        req.get("serial") not in SERIALS
        or serial != req["serial"]
        or capacity != req.get("capacity")
    ):
        raise ValueError("Target arguments do not match immutable request")
    if type(capacity) is not int or capacity < 4868 * 1024**2 or capacity % 512:
        raise ValueError("Invalid target capacity")
    root = sys_block / "vda"
    if (root / "serial").read_text().strip() != serial or int(
        (root / "size").read_text()
    ) * 512 != capacity:
        raise ValueError("Target identity or capacity changed")
    disks = {
        p.name
        for p in sys_block.iterdir()
        if (p / "device").exists() and not (p / "partition").exists()
    }
    if disks != {"vda"}:
        raise ValueError("Unexpected additional disks exposed to installer")
    return target


def partition_path(target, number):
    text = str(target)
    return text + ("p" if text[-1].isdigit() else "") + str(number)


def inventory(root, metadata=True):
    result = {}
    seen_inodes = {}
    for path in sorted(Path(root).rglob("*")):
        st = path.lstat()
        relative = path.relative_to(root).as_posix()
        item = {"type": stat.S_IFMT(st.st_mode)}
        if metadata:
            item.update(mode=stat.S_IMODE(st.st_mode), uid=st.st_uid, gid=st.st_gid)
        if metadata and hasattr(os, "listxattr"):
            item["xattrs"] = {
                name: os.getxattr(path, name, follow_symlinks=False).hex()
                for name in sorted(os.listxattr(path, follow_symlinks=False))
            }
        if stat.S_ISREG(st.st_mode):
            inode = (st.st_dev, st.st_ino)
            first = seen_inodes.get(inode)
            item.update(
                bytes=st.st_size,
                sha256=result[first]["sha256"] if first else digest(path),
            )
            if metadata:
                item["links"] = st.st_nlink
                if first:
                    item["hardlink"] = first
            seen_inodes.setdefault(inode, relative)
        elif stat.S_ISLNK(st.st_mode):
            item["target"] = os.readlink(path)
        elif not stat.S_ISDIR(st.st_mode):
            item["rdev"] = st.st_rdev
        result[relative] = item
    return result


def install(
    input_dir, output, req, runner, target, capacity, serial, work_base=Path("/work")
):
    provision = input_dir / "project/image/provision"
    access = load_module("fascia_ssh_install", provision / "fascia_ssh.py")
    policy = access.validate(req.get("ssh", {"mode": "off"}))
    prepared = prepared_directory(output, req["source_key"])
    with runner.phase("verify-prepared-artifacts"):
        manifest = verify_prepared(prepared, req["source_key"])
    if req.get("prepared_files") != manifest["files"]:
        raise ValueError("Prepared artifact hashes differ from host approval")
    validate_target(target, req, capacity, serial)
    target = Path(target)
    runner.target = target
    with tempfile.TemporaryDirectory(prefix="fascia-write-", dir=work_base) as temp:
        work = Path(temp)
        top = work / "system"
        boot = work / "boot"
        board = work / "board"
        top.mkdir()
        boot.mkdir()
        board.mkdir()
        extract_archive(prepared / "board.tar.zst", board, runner)
        mounted = []
        runner.sync_mounts = [top, boot]
        try:
            with runner.phase("partition-and-format"):
                # Last identity check immediately before the first destructive syscall.
                validate_target(target, req, capacity, serial)
                table = "label: dos\nlabel-id: 0x46524d45\nunit: sectors\n\nstart=8192,size=524288,type=c,bootable\nstart=532480,type=83\n"
                runner.run(["sfdisk", target], input=table.encode())
                runner.run(
                    [
                        "mkfs.vfat",
                        "-F",
                        "32",
                        "-n",
                        "FASCIABOOT",
                        "-i",
                        "46524d45",
                        partition_path(target, 1),
                    ]
                )
                runner.run(
                    [
                        "mkfs.btrfs",
                        "-f",
                        "-K",
                        "-L",
                        "FASCIASYS",
                        "-m",
                        "dup",
                        partition_path(target, 2),
                    ]
                )
                runner.run(
                    [
                        "mount",
                        "-t",
                        "btrfs",
                        "-o",
                        "compress=zstd:3,noatime",
                        partition_path(target, 2),
                        top,
                    ]
                )
                mounted.append(top)
                runner.run(["mount", "-t", "vfat", partition_path(target, 1), boot])
                mounted.append(boot)
            with runner.phase("materialize-system"):
                runner.run(["btrfs", "subvolume", "create", top / "data"])
                (top / "generations").mkdir()
                root = top / "generations/factory-1"
                runner.run(["btrfs", "subvolume", "create", root])
                extract_archive(prepared / "runtime.tar.zst", root, runner)
                assembly = load_module(
                    "fascia_assemble",
                    input_dir / "project/image/tools/assemble-image.py",
                )
                recovery = load_module(
                    "fascia_recovery",
                    input_dir / "project/image/system/fascia-system-update.py",
                )
                assembly.data_directories(top / "data", root, None)
                access.seed(top / "data", policy)
                recovery.Updater(
                    top / "generations", top / "data/fascia-system"
                ).initialize("factory-1")
                shutil.copy2(root / "boot/zImage", boot / "zImage")
                shutil.copy2(prepared / "initramfs.img", boot / "initramfs.img")
                assembly.copy_boot_dtbs(root, board, boot)
                (boot / "extlinux").mkdir()
                (boot / "extlinux/extlinux.conf").write_text(
                    "DEFAULT fascia\nTIMEOUT 10\nLABEL fascia\n    LINUX /zImage\n    INITRD /initramfs.img\n    FDT /dtbs/pf1007l-provision.dtb\n    APPEND root=PARTUUID=46524d45-02 rootwait rw quiet systemd.gpt_auto=0 firmware_class.path=/run/fascia-firmware panic=20\n"
                )
                create_boot_script(boot / "boot.scr")
                with target.open("r+b", buffering=0) as disk:
                    disk.seek(8192)
                    disk.write((board / "boot/u-boot-sunxi-with-spl.bin").read_bytes())
                    os.fsync(disk.fileno())
            with runner.phase("capture-verification-manifest"):
                expected = {
                    "root": inventory(root),
                    "data": inventory(top / "data"),
                    "boot": inventory(boot, False),
                }
                runner.run(["chroot", root, "/usr/bin/pacman", "-Dk"])
                actual = subprocess.check_output(
                    ["chroot", root, "/usr/bin/pacman", "-Q"], text=True
                )
                if {line.split()[0] for line in actual.splitlines()} != set(
                    manifest["runtime_names"]
                ):
                    raise ValueError(
                        "Installed package set differs from prepared manifest"
                    )
                for mount in (top, boot):
                    runner.run(["sync", "-f", mount])
            for p in reversed(mounted):
                runner.run(["umount", p])
            mounted = []
            with runner.phase("full-readback"):
                if req.get("linux_device") is not None:
                    runner.run(["blockdev", "--flushbufs", target])
                    for number in (1, 2):
                        runner.run(
                            ["blockdev", "--flushbufs", partition_path(target, number)]
                        )
                    pinned = load_module(
                        "fascia_pinned_readback",
                        Path(__file__).with_name("linux-target.py"),
                    )
                    pinned.verify(target, req["linux_device"]["loop_identity"])
                    eviction_scope = pinned.evict_backing(
                        req["linux_device"].get("backing_fd"),
                        req["linux_device"]["loop_identity"],
                    )
                else:
                    Path("/proc/sys/vm/drop_caches").write_text("3\n")
                    eviction_scope = "helper-guest kernel caches dropped; host and media caches unproven"
                runner.run(
                    [
                        "mount",
                        "-t",
                        "btrfs",
                        "-o",
                        "ro,nologreplay,subvolid=5",
                        partition_path(target, 2),
                        top,
                    ]
                )
                mounted.append(top)
                runner.run(
                    ["mount", "-t", "vfat", "-o", "ro", partition_path(target, 1), boot]
                )
                mounted.append(boot)
                if expected != {
                    "root": inventory(top / "generations/factory-1"),
                    "data": inventory(top / "data"),
                    "boot": inventory(boot, False),
                }:
                    raise ValueError(
                        "Installed content or Linux metadata changed on readback"
                    )
                with target.open("rb", buffering=0) as disk:
                    disk.seek(8192)
                    spl = (board / "boot/u-boot-sunxi-with-spl.bin").read_bytes()
                    if disk.read(len(spl)) != spl:
                        raise ValueError("Bootloader readback mismatch")
                atomic_json(
                    Path(output) / "result.json",
                    {
                        "state": "verified",
                        "source_key": req["source_key"],
                        "capacity": capacity,
                        "serial": serial,
                        "root_entries": len(expected["root"]),
                        "cache_eviction_scope": eviction_scope,
                        "scope": "Full file/content/metadata and bootloader readback; physical boot still required",
                    },
                )
        finally:
            for p in reversed(mounted):
                runner.run(["umount", p])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("/input"))
    parser.add_argument("--output", type=Path, default=Path("/output"))
    parser.add_argument("--target", type=Path)
    parser.add_argument("--work", type=Path, default=Path("/work"))
    parser.add_argument("--capacity", type=int)
    parser.add_argument("--serial")
    args = parser.parse_args()
    if os.geteuid() != 0 or os.uname().sysname != "Linux":
        parser.error("Linux helper root required")
    args.work.mkdir(parents=True, exist_ok=True)
    req = request(args.input)
    runner = Runner(args.output)
    if req["phase"] == "prepare":
        if args.target is not None:
            parser.error("Preparation must not expose a target")
        prepare(args.input, args.output, req, runner, args.work)
    else:
        install(
            args.input,
            args.output,
            req,
            runner,
            args.target,
            args.capacity,
            args.serial,
            args.work,
        )


if __name__ == "__main__":
    main()
