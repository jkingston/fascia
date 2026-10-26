#!/usr/bin/env python3
"""Pinned native Linux helper for the Apple Virtualization build backend."""

import concurrent.futures
import gzip
import hashlib
import importlib.util
import json
import lzma
import re
import os
from pathlib import Path
import shutil
import selectors
import time
from collections import deque
import stat
import subprocess
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


class HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not newurl.startswith("https://"):
            raise ValueError("Helper download attempted an insecure redirect")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch(record, cache, offline=False):
    path = cache / record["url"].rsplit("/", 1)[1]
    if not record["url"].startswith("https://"):
        raise ValueError("Helper archive URL must use HTTPS")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Helper cache archive is not a regular file")
    if (
        path.exists()
        and path.stat().st_size == record["bytes"]
        and digest(path) == record["sha256"]
    ):
        return path
    if offline:
        raise ValueError("Missing or corrupt offline helper archive: " + record["name"])
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=cache)
    try:
        with (
            os.fdopen(fd, "wb") as target,
            urllib.request.build_opener(HTTPSOnlyRedirect()).open(
                record["url"], timeout=90
            ) as source,
        ):
            size = 0
            while block := source.read(1024 * 1024):
                size += len(block)
                if size > record["bytes"]:
                    raise ValueError("Helper archive exceeds pinned length")
                target.write(block)
        if size != record["bytes"] or digest(temporary) != record["sha256"]:
            raise ValueError("Helper archive authentication failed: " + record["name"])
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def extract_deb(archive, root):
    listing = subprocess.check_output(
        ["tar", "-tf", str(archive)], text=True
    ).splitlines()
    member = next(name for name in listing if name.startswith("data.tar"))
    data = subprocess.check_output(["tar", "-xOf", str(archive), member])
    # All archive bytes were authenticated before reaching the extractor.
    subprocess.run(["tar", "-xf", "-", "-C", str(root)], input=data, check=True)


def module_subset(source, destination, release):
    base = source / "usr/lib/modules" / release
    deps = {}
    paths = {
        path.name.split(".ko")[0].replace("-", "_"): path
        for path in base.rglob("*.ko*")
    }

    def dependencies(path):
        data = path.read_bytes()
        if path.suffix == ".xz":
            data = lzma.decompress(data)
        match = re.search(rb"(?:^|\x00)depends=([^\x00]*)", data)
        names = match.group(1).decode().split(",") if match else []
        return [paths[name.replace("-", "_")] for name in names if name]

    pending_paths = [
        paths[name]
        for name in (
            "virtio_blk",
            "virtio_pci",
            "virtio_mmio",
            "virtio_rng",
            "virtio_net",
            "btrfs",
            "crc32c_generic",
            "crc32_generic",
            "binfmt_misc",
            "loop",
            "vfat",
            "nls_cp437",
            "nls_ascii",
            "nls_utf8",
            "ext4",
        )
        if name in paths
    ]
    while pending_paths:
        path = pending_paths.pop()
        name = str(path.relative_to(base))
        if name in deps:
            continue
        children = dependencies(path)
        deps[name] = [str(child.relative_to(base)) for child in children]
        pending_paths.extend(children)
    selected = set(deps)
    target = destination / "usr/lib/modules" / release
    target.mkdir(parents=True)
    for name in selected:
        out = target / name
        out.parent.mkdir(parents=True, exist_ok=True)
        data = (base / name).read_bytes()
        if out.suffix == ".xz":
            data = lzma.decompress(data)
            out = out.with_suffix("")
        out.write_bytes(data)
    for path in base.glob("modules.*"):
        if path.is_file():
            shutil.copyfile(path, target / path.name)
    return sorted(selected)


def prepare_certificates(root):
    """Build the standard PEM bundle from authenticated Debian Mozilla CA data."""
    certificates = sorted((root / "usr/share/ca-certificates/mozilla").glob("*.crt"))
    if not certificates:
        raise ValueError("Authenticated helper CA certificates are missing")
    bundle = bytearray()
    for certificate in certificates:
        data = certificate.read_bytes()
        if (
            not data.startswith(b"-----BEGIN CERTIFICATE-----")
            or b"-----END CERTIFICATE-----" not in data
        ):
            raise ValueError("Invalid helper CA certificate")
        bundle.extend(data.rstrip() + b"\n")
    destination = root / "etc/ssl/certs/ca-certificates.crt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(bundle)
    default = root / "usr/lib/ssl/cert.pem"
    default.parent.mkdir(parents=True, exist_ok=True)
    if default.is_symlink():
        default.unlink()
    elif default.exists():
        raise ValueError("Unexpected existing helper CA default")
    default.symlink_to("/etc/ssl/certs/ca-certificates.crt")


def prepare_helper(cache, offline=False):
    if Path(cache).is_symlink():
        raise ValueError("Helper cache directory must not be a symlink")
    cache = Path(cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    lock = json.loads((HERE / "helper.lock.json").read_text())
    sources = {
        name: digest(HERE / name)
        for name in ("helper.lock.json", "helper-init", "helper.py")
    }
    sources["cpio_builder"] = digest(HERE.parent / "tools/build-initramfs.py")
    key = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()[:20]
    final = cache / ("helper-" + key)
    if final.is_symlink():
        raise ValueError("Prepared helper directory must not be a symlink")
    if final.exists():
        manifest_path = final / "manifest.json"
        if manifest_path.is_symlink():
            raise ValueError("Prepared helper manifest must not be a symlink")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("sources") != sources or set(manifest.get("artifacts", {})) != {
            "kernel",
            "initrd",
        }:
            raise ValueError("Prepared helper source manifest mismatch")
        for name in ("kernel", "initrd"):
            path = final / name
            if (
                path.is_symlink()
                or not path.is_file()
                or digest(path) != manifest["artifacts"][name]
            ):
                raise ValueError("Prepared helper binary hash mismatch: " + name)
        return {name: final / name for name in ("kernel", "initrd")}
    packages = cache / "packages"
    if packages.is_symlink():
        raise ValueError("Helper package directory must not be a symlink")
    packages.mkdir(exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        archives = list(
            pool.map(lambda record: fetch(record, packages, offline), lock["packages"])
        )
    with tempfile.TemporaryDirectory(prefix="helper-build-", dir=cache) as temporary:
        stage = Path(temporary)
        root = stage / "root"
        kernel_root = stage / "kernel-root"
        root.mkdir()
        kernel_root.mkdir()
        for record, archive in zip(lock["packages"], archives):
            extract_deb(
                archive,
                kernel_root if record["name"].startswith("linux-image-") else root,
            )
        modules = module_subset(kernel_root, root, lock["kernel_release"])
        prepare_certificates(root)
        # Keep only the emulator used for the target architecture, not every QEMU CPU.
        for emulator in (root / "usr/bin").glob("qemu-*"):
            if emulator.name != "qemu-arm":
                emulator.unlink()
        licenses = root / "usr/share/licenses/fascia-helper"
        licenses.mkdir(parents=True, exist_ok=True)
        for copyright_file in (root / "usr/share/doc").glob("*/copyright"):
            if copyright_file.is_file():
                shutil.copyfile(
                    copyright_file,
                    licenses / (copyright_file.parent.name + ".copyright"),
                )
        for directory in (
            "usr/share/doc",
            "usr/share/man",
            "usr/share/locale",
            "usr/include",
            "var",
        ):
            shutil.rmtree(root / directory, ignore_errors=True)
        for directory in ("dev", "proc", "sys", "tmp", "run", "input", "output"):
            (root / directory).mkdir(exist_ok=True)
        for name in ("bin", "sbin", "lib"):
            path = root / name
            if not path.exists() and not path.is_symlink():
                path.symlink_to("usr/" + name)
        for name in (
            "sh",
            "chroot",
            "stat",
            "id",
            "find",
            "sort",
            "cp",
            "mv",
            "rm",
            "mkdir",
            "cat",
            "echo",
            "printf",
            "sleep",
            "sync",
            "poweroff",
            "insmod",
            "grep",
            "cut",
            "chmod",
            "chown",
            "ln",
            "readlink",
            "dd",
            "udhcpc",
            "ifconfig",
            "route",
        ):
            path = root / "usr/bin" / name
            if not path.exists():
                path.symlink_to("busybox")
        shutil.copyfile(HERE / "helper-init", root / "init")
        (root / "init").chmod(0o755)
        spec = importlib.util.spec_from_file_location(
            "fascia_cpio", HERE.parent / "tools/build-initramfs.py"
        )
        cpio = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cpio)
        entries = {}
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                payload = os.readlink(path).encode()
            elif stat.S_ISREG(info.st_mode):
                payload = path.read_bytes()
            elif stat.S_ISDIR(info.st_mode):
                payload = b""
            else:
                continue
            entries[relative] = (info.st_mode, payload)
        kernel = kernel_root / "boot" / ("vmlinuz-" + lock["kernel_release"])
        data = kernel.read_bytes()
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        (stage / "kernel").write_bytes(data)
        (stage / "initrd").write_bytes(
            gzip.compress(cpio.newc(entries), compresslevel=6, mtime=0)
        )
        manifest = {
            "artifacts": {name: digest(stage / name) for name in ("kernel", "initrd")},
            "modules": modules,
            "lock_sha256": digest(HERE / "helper.lock.json"),
            "sources": sources,
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        shutil.rmtree(root)
        shutil.rmtree(kernel_root)
        os.rename(stage, final)
    return {name: final / name for name in ("kernel", "initrd")}


def build_launcher(cache):
    cache = Path(cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    output = cache / "mac-helper"
    subprocess.run(
        [
            "xcrun",
            "swiftc",
            "-target",
            "arm64-apple-macosx14.0",
            "-module-cache-path",
            str(cache / "swift-cache"),
            "-O",
            str(HERE / "mac-helper.swift"),
            "-o",
            str(output),
        ],
        check=True,
    )
    entitlements = cache / "virtualization.entitlements"
    entitlements.write_text(
        '<?xml version="1.0"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>com.apple.security.virtualization</key><true/></dict></plist>'
    )
    subprocess.run(
        [
            "codesign",
            "--force",
            "--sign",
            "-",
            "--entitlements",
            str(entitlements),
            str(output),
        ],
        check=True,
    )
    (cache / "mac-helper.json").write_text(
        json.dumps(
            {"source": digest(HERE / "mac-helper.swift"), "binary": digest(output)}
        )
        + "\n"
    )
    return output


def verified_launcher(cache, owner):
    cache = Path(cache)
    binary = cache / "mac-helper"
    manifest = cache / "mac-helper.json"
    for path in (cache, binary, manifest):
        info = path.lstat()
        if path.is_symlink() or info.st_uid != owner or info.st_mode & 0o022:
            raise ValueError("Unsafe prebuilt Mac helper")
    record = json.loads(manifest.read_text())
    if record != {
        "source": digest(HERE / "mac-helper.swift"),
        "binary": digest(binary),
    }:
        raise ValueError("Prebuilt Mac helper differs; prepare it as the invoking user")
    return binary


class MountGuard:
    def __init__(self, launcher, disk):
        if not re.fullmatch(r"disk[0-9]+|probe", disk):
            raise ValueError("Expected a whole BSD disk name")
        self.process = subprocess.Popen(
            [str(launcher), "--guard", disk],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self._expect(b"FASCIA_GUARD_READY\n")
        except BaseException:
            self.close()
            raise

    def _expect(self, expected):
        selector = selectors.DefaultSelector()
        try:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(5) or self.process.stdout.readline(128) != expected:
                raise RuntimeError(
                    "Disk mount guardian failed readiness/liveness check"
                )
        finally:
            selector.close()
        if self.process.poll() is not None:
            raise RuntimeError("Disk mount guardian exited")

    def check(self):
        if self.process.poll() is not None:
            raise RuntimeError("Disk mount guardian exited; installation stopped")
        self.process.stdin.write(b"ping\n")
        self.process.stdin.flush()
        self._expect(b"FASCIA_GUARD_ALIVE\n")

    def close(self):
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        finally:
            self.process.stdout.close()
            self.process.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def stream_process(command, log, timeout, pass_fds=(), monitor=None):
    """Keep the transcript while exposing only bounded stage events."""
    started = time.monotonic()
    stage = None
    stage_started = last_notice = started
    pending = b""
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        pass_fds=pass_fds,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        with Path(log).open("wb") as output:
            while True:
                if monitor is not None:
                    monitor()
                now = time.monotonic()
                if now - started > timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                if stage and now - last_notice >= 30:
                    print(
                        f"Still {stage} ({int(now-stage_started)} seconds)…", flush=True
                    )
                    last_notice = now
                events = selector.select(
                    timeout=min(1, max(0.01, timeout - (now - started)))
                )
                if not events:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                output.write(chunk)
                output.flush()
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    match = re.search(
                        rb"FASCIA_STAGE ([a-z0-9_-]{1,100})(?:\r|$)", line
                    )
                    if match:
                        stage = (
                            match.group(1).decode().replace("-", " ").replace("_", " ")
                        )
                        stage_started = last_notice = time.monotonic()
                        print(stage.capitalize() + "…", flush=True)
                if len(pending) > 65536:
                    pending = pending[-65536:]
        return process.wait(timeout=5)
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()


def failure_tail(path):
    if Path(path).is_file():
        with Path(path).open(errors="replace") as source:
            lines = deque(source, maxlen=30)
        print("Last helper log lines (" + str(path) + "):", flush=True)
        print("".join(lines), end="", flush=True)


def run_helper(
    kernel,
    initrd,
    input_dir,
    output_dir,
    *,
    image=None,
    target_fd=None,
    capacity=0,
    timeout=7200,
    launcher=None,
    mount_guard=None,
):
    if image is not None and target_fd is not None:
        raise ValueError("Choose an image or inherited device descriptor")
    input_dir = Path(input_dir).resolve()
    output_dir = Path(output_dir).resolve()
    if (
        input_dir == output_dir
        or input_dir in output_dir.parents
        or output_dir in input_dir.parents
    ):
        raise ValueError("Input and output shares must not overlap")
    mode, target, serial = "none", "-", "FASCIA-PREPARE"
    if image is not None:
        path = Path(image)
        if (
            path.is_symlink()
            or not stat.S_ISREG(path.stat().st_mode)
            or path.stat().st_size != capacity
        ):
            raise ValueError("Image must be a capacity-matched regular file")
        mode, target, serial = "image", str(path.resolve()), "FASCIA-INSTALL-FILE"
    elif target_fd is not None:
        info = os.fstat(target_fd)
        if not (stat.S_ISCHR(info.st_mode) or stat.S_ISBLK(info.st_mode)):
            raise ValueError("Target descriptor is not a device")
        mode, target, serial = "fd", str(target_fd), "FASCIA-INSTALL-CARD"
    binary = Path(launcher) if launcher else build_launcher(output_dir / "launcher")
    command = [
        str(binary),
        str(Path(kernel).resolve()),
        str(Path(initrd).resolve()),
        str(input_dir),
        str(output_dir),
        str(capacity),
        serial,
        mode,
        target,
    ]
    log = output_dir / "helper-console.log"
    try:
        returncode = stream_process(
            command,
            log,
            timeout,
            (() if target_fd is None else (target_fd,)),
            monitor=(mount_guard.check if mount_guard else None),
        )
        text = log.read_text()
        if (
            returncode
            or "FASCIA_HELPER_SUCCESS" not in text
            or "FASCIA_HELPER_FAILED" in text
        ):
            raise RuntimeError("Linux helper failed; see " + str(log))
    except BaseException:
        failure_tail(output_dir / "guest.log")
        failure_tail(log)
        raise
    return {"console": log, "returncode": returncode}
