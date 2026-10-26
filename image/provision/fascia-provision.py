#!/usr/bin/env python3
"""Provision only explicit identity/firmware paths; never mount or write eMMC."""

import array
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import stat
import subprocess
import tempfile
import sys
import socket

# -I intentionally ignores user/PYTHONPATH imports; only our root-owned bundle.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_gsl import extract, MAX_MODULE
import fascia_identity as identity
import fascia_ssh

BLKROGET = 0x125E
TOUCH_SHA256 = "e11ffa8a33a56b78f8d2f57925fef1b67ea9a58c264bb5f3a8553ea52b14a9e6"
MODULE_SOURCE = "/vendor/modules/gslX680new.ko"
FIRMWARE = Path("/data/firmware")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def discover(sys_block=Path("/sys/class/block"), dev=Path("/dev")):
    matches = []
    for entry in sys_block.glob("mmcblk*"):
        if not re.fullmatch(r"mmcblk\d+", entry.name):
            continue
        try:
            kind = (entry / "device/type").read_text().strip()
            controller = (entry / "device").resolve(strict=True).parts
        except (OSError, ValueError):
            continue
        if kind == "MMC" and "1c11000.mmc" in controller:
            partition = sys_block / (entry.name + "p7")
            if not partition.exists() or partition.resolve().parent != entry.resolve():
                raise ValueError("expected system partition 7 on discovered eMMC")
            matches.append((dev / entry.name, dev / partition.name))
    if len(matches) != 1:
        raise ValueError("expected exactly one MMC device on controller 1c11000")
    return matches[0]


def require_readonly(path):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if not stat.S_ISBLK(os.fstat(fd).st_mode):
            raise ValueError("vendor input must be a block device")
        value = array.array("I", [0])
        fcntl.ioctl(fd, BLKROGET, value, True)
        if value[0] != 1:
            raise ValueError("eMMC is not kernel-enforced read-only")
    finally:
        os.close(fd)


def debugfs(device, command):
    result = subprocess.run(
        ["debugfs", "-R", command, str(device)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=True,
        preexec_fn=lambda: resource.setrlimit(
            resource.RLIMIT_FSIZE, (MAX_MODULE, MAX_MODULE)
        ),
    )
    # debugfs can exit zero on lookup errors; check stat and dump results.
    if len(result.stdout) + len(result.stderr) > 65536:
        raise ValueError("unexpected debugfs output")
    return result.stdout.decode("utf-8", "replace")


def read_vendor(disk, partition, source, limit, directory):
    if not re.fullmatch(r"/vendor/[A-Za-z0-9_./-]+", source) or ".." in source.split(
        "/"
    ):
        raise ValueError("vendor source outside allowlist")
    # Check BOTH the whole device and the chosen partition before every read.
    require_readonly(disk)
    require_readonly(partition)
    info = debugfs(partition, "stat " + source)
    found = re.search(r"\bSize:\s*(\d+)", info)
    if not found or not 1 <= int(found[1]) <= limit:
        raise ValueError("vendor file absent or exceeds size bound")
    output = Path(directory) / "module"
    debugfs(partition, "dump " + source + " " + str(output))
    data = identity.read_bounded(output, limit)
    if len(data) != int(found[1]):
        raise ValueError("incomplete vendor read")
    output.unlink()
    return data


def touch_firmware(firmware=FIRMWARE, discover_device=discover):
    target = firmware / "silead/gsl_m86_hd.fw"
    try:
        raw = identity.read_bounded(target, 262144)
        valid = digest(raw) == TOUCH_SHA256
    except FileNotFoundError:
        valid = False
    if not valid:
        disk, partition = discover_device()
        with tempfile.TemporaryDirectory(
            prefix="fascia-vendor-", dir="/run"
        ) as temporary:
            module = read_vendor(disk, partition, MODULE_SOURCE, MAX_MODULE, temporary)
            raw = extract(module)
        if digest(raw) != TOUCH_SHA256:
            raise ValueError(
                "unsupported touch firmware; verified PF1007L hash differs"
            )
        identity.atomic(target, raw, 0o644)
    alias = firmware / "silead/gsl1680-pf1007l.fw"
    if (
        not alias.exists()
        or digest(identity.read_bounded(alias, 262144)) != TOUCH_SHA256
    ):
        identity.atomic(alias, raw, 0o644)
    return {"touch_sha256": TOUCH_SHA256, "bytes": len(raw)}


def extension_files(mapping, firmware=FIRMWARE):
    """Only builder-reviewed public metadata may extend the vendor file map."""
    entries = json.loads(identity.read_bounded(mapping, 16384))
    if type(entries) is not list or len(entries) > 32:
        raise ValueError("invalid vendor allowlist")
    disk_partition = None
    for item in entries:
        if type(item) is not dict or set(item) != {
            "source",
            "destination",
            "sha256",
            "max_bytes",
        }:
            raise ValueError("invalid vendor allowlist entry")
        destination = item["destination"]
        if (
            not isinstance(destination, str)
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", destination)
            or destination.startswith("/")
            or any(p in ("", ".", "..") for p in destination.split("/"))
        ):
            raise ValueError("invalid firmware destination")
        expected = item["sha256"]
        limit = item["max_bytes"]
        if (
            not isinstance(expected, str)
            or not re.fullmatch("[0-9a-f]{64}", expected)
            or type(limit) is not int
            or not 1 <= limit <= MAX_MODULE
        ):
            raise ValueError("invalid firmware fingerprint or size bound")
        target = firmware / destination
        try:
            if digest(identity.read_bounded(target, limit)) == expected:
                continue
        except FileNotFoundError:
            pass
        if disk_partition is None:
            disk_partition = discover()
        with tempfile.TemporaryDirectory(
            prefix="fascia-vendor-", dir="/run"
        ) as temporary:
            raw = read_vendor(*disk_partition, item["source"], limit, temporary)
        if digest(raw) != expected:
            raise ValueError("vendor firmware fingerprint differs: " + destination)
        identity.atomic(target, raw, 0o644)


def ensure_hostkey(root):
    target = root / "ssh"
    if not target.exists():
        temporary = Path(tempfile.mkdtemp(prefix=".ssh-", dir=root))
        try:
            subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(temporary / "ssh_host_ed25519_key"),
                ],
                check=True,
            )
            for path in temporary.iterdir():
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            fd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(temporary, target)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    if target.is_symlink():
        raise ValueError("SSH identity directory cannot be a symlink")
    private = identity.read_bounded(target / "ssh_host_ed25519_key", 16384)
    public = identity.read_bounded(target / "ssh_host_ed25519_key.pub", 16384)
    derived = subprocess.run(
        ["ssh-keygen", "-y", "-f", str(target / "ssh_host_ed25519_key")],
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    if public.split()[:2] != derived.split()[:2]:
        raise ValueError("persistent SSH key pair does not match")
    return private, public


def link(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.parent / (".provision-" + path.name)
    pending.unlink(missing_ok=True)
    pending.symlink_to(target)
    os.replace(pending, path)


def materialize(
    root=identity.ROOT, etc=Path("/etc"), zoneinfo=Path("/usr/share/zoneinfo")
):
    config = identity.ensure_identities(root)
    fascia_ssh.apply_auth(root, etc, config)
    private, public = ensure_hostkey(root)
    zone = zoneinfo / config["timezone"]
    if not zone.is_file() or not zone.resolve().is_relative_to(zoneinfo.resolve()):
        raise ValueError("configured timezone is unavailable")
    identity.atomic(
        etc / "machine-id", identity.read_bounded(root / "machine-id", 128), 0o444
    )
    identity.atomic(etc / "hostname", (config["hostname"] + "\n").encode(), 0o644)
    identity.atomic(etc / "timezone", (config["timezone"] + "\n").encode(), 0o644)
    link(etc / "localtime", str(zone))
    identity.atomic(etc / "ssh/ssh_host_ed25519_key", private)
    identity.atomic(etc / "ssh/ssh_host_ed25519_key.pub", public, 0o644)
    mac = identity.read_bounded(root / "wifi-mac", 128).decode().strip()
    identity.atomic(
        etc / "systemd/network/10-fascia-wifi.link",
        (
            "[Match]\nOriginalName=wlan*\n\n[Link]\nMACAddressPolicy=none\nMACAddress="
            + mac
            + "\n"
        ).encode(),
        0o644,
    )
    system = etc / "systemd/system"
    ssh = system / "sshd.service"
    wants = system / "multi-user.target.wants"
    if config["ssh_enabled"]:
        if ssh.is_symlink() and os.readlink(ssh) == "/dev/null":
            ssh.unlink()
        link(wants / "sshd.service", "/usr/lib/systemd/system/sshd.service")
    else:
        link(ssh, "/dev/null")
        (wants / "sshd.service").unlink(missing_ok=True)
    override = system / "pf-kiosk.service.d/97-physical-home.conf"
    if config["physical_home"]:
        identity.atomic(
            override, b"[Service]\nEnvironment=FASCIA_HOME_OVERLAY=auto\n", 0o644
        )
        link(
            wants / "fascia-home-button.service",
            "/usr/lib/systemd/system/fascia-home-button.service",
        )
    else:
        override.unlink(missing_ok=True)
        (wants / "fascia-home-button.service").unlink(missing_ok=True)
    return config


def main():
    if len(sys.argv) != 1:
        raise ValueError("provision accepts no arguments")
    if os.geteuid() != 0 or not os.path.ismount("/data"):
        raise PermissionError("provision requires root and mounted persistent /data")
    root = identity.ROOT
    identity.ensure_directory(root)
    lock = os.open(
        root / ".provision-lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = materialize()
        socket.sethostname(config["hostname"])
        print(json.dumps(touch_firmware(), sort_keys=True))
        extension_files(Path(__file__).with_name("vendor-files.json"))
    finally:
        os.close(lock)


if __name__ == "__main__":
    main()
