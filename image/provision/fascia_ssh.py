"""Per-card credentials, kept outside reusable OS payloads and restored at boot."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import re

spec = importlib.util.spec_from_file_location(
    "fascia_ssh_identity", Path(__file__).with_name("fascia_identity.py")
)
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)
HASH = re.compile(r"\$6\$rounds=100000\$[./0-9A-Za-z]{16}\$[./0-9A-Za-z]{86}\Z")


def validate(policy):
    if type(policy) is not dict:
        raise ValueError("Invalid SSH policy")
    mode = policy.get("mode")
    fields = {
        "off": {"mode"},
        "key": {"mode", "public_key"},
        "password": {"mode", "password_hash"},
    }
    if not isinstance(mode, str) or mode not in fields or set(policy) != fields[mode]:
        raise ValueError("Invalid SSH policy")
    if mode == "password":
        if not isinstance(policy["password_hash"], str) or not HASH.fullmatch(
            policy["password_hash"]
        ):
            raise ValueError("Invalid SSH password hash")
    if mode == "key":
        key = policy["public_key"]
        if (
            not isinstance(key, str)
            or len(key) > 16384
            or any(ord(c) < 32 or ord(c) == 127 for c in key)
        ):
            raise ValueError("Invalid SSH public key")
        parts = key.split()
        allowed = {
            "ssh-ed25519",
            "ssh-rsa",
            "ecdsa-sha2-nistp256",
            "ecdsa-sha2-nistp384",
            "ecdsa-sha2-nistp521",
            "sk-ssh-ed25519@openssh.com",
            "sk-ecdsa-sha2-nistp256@openssh.com",
        }
        if len(parts) < 2 or parts[0] not in allowed:
            raise ValueError("Invalid SSH public key")
        try:
            blob = base64.b64decode(parts[1], validate=True)
        except ValueError as error:
            raise ValueError("Invalid SSH public key") from error
        size = int.from_bytes(blob[:4], "big")
        if (
            size != len(parts[0])
            or blob[4 : 4 + size] != parts[0].encode()
            or len(blob) <= 4 + size
        ):
            raise ValueError("Invalid SSH public key")
    return policy


def seed(data, policy, *, ownership=True):
    policy = validate(policy)
    root = data / "identity"
    mode = policy["mode"]
    if mode == "key":
        directory = data / "home/fascia/.ssh"
        identity.ensure_directory(directory)
        identity.atomic(
            directory / "authorized_keys", (policy["public_key"] + "\n").encode()
        )
        if ownership:
            os.chown(directory, 1000, 1000)
            os.chown(directory / "authorized_keys", 1000, 1000)
    if mode == "password":
        identity.atomic(
            root / "ssh-password.hash", (policy["password_hash"] + "\n").encode()
        )
    identity.change(
        root, ssh_enabled=mode != "off", ssh_password_auth=mode == "password"
    )
    identity.atomic(
        data / "fascia/setup.json",
        json.dumps(
            {"schema_version": 1, "stages": {"ssh": True}, "completed": False}
        ).encode(),
    )


def apply_auth(root, etc, config):
    password = config["ssh_password_auth"]
    if password:
        hashed = identity.read_bounded(root / "ssh-password.hash", 256).decode().strip()
        validate({"mode": "password", "password_hash": hashed})
        shadow = (
            identity.read_bounded(etc / "shadow", 1024 * 1024).decode().splitlines()
        )
        matches = [i for i, line in enumerate(shadow) if line.startswith("fascia:")]
        if len(matches) != 1:
            raise ValueError("Expected exactly one fascia account")
        index = matches[0]
        fields = shadow[index].split(":")
        if len(fields) != 9:
            raise ValueError("Invalid fascia shadow entry")
        fields[1] = hashed
        shadow[index] = ":".join(fields)
        identity.atomic(etc / "shadow", ("\n".join(shadow) + "\n").encode())
    policy = (
        "PasswordAuthentication "
        + ("yes" if password else "no")
        + "\nKbdInteractiveAuthentication no\nPermitRootLogin no\nPubkeyAuthentication yes\nAllowUsers fascia\nUsePAM yes\n"
    )
    identity.atomic(etc / "ssh/sshd_config.d/40-fascia.conf", policy.encode(), 0o644)
