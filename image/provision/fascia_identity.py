"""Durable, explicitly allowlisted identity; /etc remains a disposable overlay."""

import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile

ROOT = Path("/data/identity")
HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
HOSTNAME_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def new_hostname():
    # Generate once per card; omit ambiguous i/l/o and 0/1.
    return "fascia-" + "".join(secrets.choice(HOSTNAME_ALPHABET) for _ in range(5))


DEFAULTS = {
    "schema_version": 1,
    "hostname": "fascia",
    "timezone": "UTC",
    "ssh_enabled": False,
    "ssh_password_auth": False,
    "physical_home": False,
}


def ensure_directory(path, mode=0o700):
    path = Path(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=mode)
        fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic(path, data, mode=0o600):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("identity paths cannot be symlinks")
    ensure_directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=".identity-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_bounded(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("expected a regular identity file")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("identity file too large")
    return data


def validate(value):
    if (
        type(value) is not dict
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
    ):
        raise ValueError("unsupported identity configuration")
    result = DEFAULTS | value
    if set(result) != set(DEFAULTS):
        raise ValueError("unknown identity setting")
    if type(result["hostname"]) is not str or not HOSTNAME.fullmatch(
        result["hostname"]
    ):
        raise ValueError("invalid hostname")
    zone = result["timezone"]
    if (
        type(zone) is not str
        or len(zone) > 128
        or zone.startswith("/")
        or any(x in ("", ".", "..") for x in zone.split("/"))
    ):
        raise ValueError("invalid timezone")
    if not re.fullmatch(r"[A-Za-z0-9_+/-]+", zone):
        raise ValueError("invalid timezone")
    for key in ("ssh_enabled", "ssh_password_auth", "physical_home"):
        if type(result[key]) is not bool:
            raise ValueError("invalid identity flag")
    return result


def load(root=None):
    root = Path(root) if root is not None else ROOT
    try:
        return validate(json.loads(read_bounded(root / "config.json", 4096)))
    except FileNotFoundError:
        return dict(DEFAULTS)


def change(root=None, **values):
    root = Path(root) if root is not None else ROOT
    ensure_directory(root)
    lock = os.open(root / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = load(root)
        if not (root / "config.json").exists() and "hostname" not in values:
            current["hostname"] = new_hostname()
        value = validate(current | values)
        atomic(
            root / "config.json", (json.dumps(value, sort_keys=True) + "\n").encode()
        )
        return value
    finally:
        os.close(lock)


def ensure_identities(root):
    """Existing malformed identity is an error, never silently replaced."""
    root = Path(root)
    ensure_directory(root)
    for name, generate, valid in (
        (
            "machine-id",
            lambda: secrets.token_hex(16),
            lambda v: bool(re.fullmatch("[0-9a-f]{32}", v)) and v != "0" * 32,
        ),
        (
            "wifi-mac",
            lambda: ":".join(
                f"{v:02x}" for v in bytes([0x02]) + secrets.token_bytes(5)
            ),
            lambda v: bool(re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", v))
            and int(v[:2], 16) & 3 == 2,
        ),
    ):
        path = root / name
        try:
            value = read_bounded(path, 128).decode().strip()
        except FileNotFoundError:
            value = generate()
            atomic(path, (value + "\n").encode())
        if not valid(value):
            raise ValueError("invalid persistent " + name)
    if not (root / "config.json").exists():
        change(root)
    return load(root)
