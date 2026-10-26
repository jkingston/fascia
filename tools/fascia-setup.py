#!/usr/bin/env python3
"""Resumable serial setup; root wrapper only, no identity reset or secret storage."""

import argparse
import base64
import binascii
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import struct
import subprocess
import shutil
import sys
import tempfile
import textwrap
import time
import pty
import select
import signal
import termios
import datetime
import importlib.util
import ipaddress
from zoneinfo import ZoneInfo

HOSTNAME = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\Z")
KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}
STAGES = ("hostname", "timezone", "time", "wifi", "ssh")
IDENTITY_ROOT = Path("/data/identity")
TIME_MARKER = Path("/run/fascia/time-set")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
STAGE_NAMES = {
    "hostname": "Name your frame",
    "timezone": "Choose your timezone",
    "time": "Set the clock",
    "wifi": "Connect to Wi-Fi",
    "ssh": "Set up SSH access",
}


class Terminal:
    """Append-only output: no cursor queries or redraws over a serial connection."""

    def __init__(self, tell=print, plain=False):
        self.tell = tell
        interactive = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
        self.color = interactive and not plain and "NO_COLOR" not in os.environ
        self.width = max(24, min(72, shutil.get_terminal_size((80, 24)).columns - 4))

    def paint(self, text, style="2"):
        return f"\033[{style}m{text}\033[0m" if self.color else text

    @staticmethod
    def clean(value):
        # Host/network status is data, never terminal escape sequences.
        return "".join(c for c in str(value) if c.isprintable())

    def note(self, text):
        for line in textwrap.wrap(self.clean(text), self.width - 4):
            self.tell("  " + self.paint(line))

    def heading(self, title, subtitle):
        self.tell("")
        self.tell("  " + self.paint(title, "1;36"))
        self.note(subtitle)
        self.tell("  " + self.paint("-" * (self.width - 4)))

    def step(self, stage, progress):
        number = STAGES.index(stage) + 1
        bar = "".join(
            "#" if progress.done(s) else ">" if s == stage else "." for s in STAGES
        )
        self.tell("")
        self.tell("  " + self.paint(f"[{bar}]  {number} / {len(STAGES)}", "36"))
        self.tell("  " + self.paint(STAGE_NAMES[stage], "1"))

    def prompt(self, text):
        return "  " + self.paint(">", "36") + " " + self.clean(text) + " "

    def saved(self):
        self.tell("  " + self.paint("Saved.", "32"))

    def card(self, title, rows):
        self.tell("")
        self.tell("  " + self.paint("+" + "-" * (self.width - 4) + "+", "36"))
        for line in [title, "", *rows]:
            for part in textwrap.wrap(self.clean(line), self.width - 6) or [""]:
                self.tell(
                    "  "
                    + self.paint("|", "36")
                    + " "
                    + part.ljust(self.width - 6)
                    + " "
                    + self.paint("|", "36")
                )
        self.tell("  " + self.paint("+" + "-" * (self.width - 4) + "+", "36"))


def hostname(value):
    value = value.strip().lower()
    if not HOSTNAME.fullmatch(value):
        raise ValueError(
            "Use one hostname label: letters, digits and interior hyphens, at most 63 characters"
        )
    return value


def normalize_key(value):
    if len(value) > 16384 or "\n" in value.strip() or "\r" in value.strip():
        raise ValueError("Paste one OpenSSH public-key line")
    fields = value.strip().split()
    if len(fields) < 2 or fields[0] not in KEY_TYPES:
        raise ValueError("Expected an OpenSSH public key, never a private key")
    try:
        blob = base64.b64decode(fields[1], validate=True)
        if len(blob) < 8:
            raise ValueError("short public key")
        length = struct.unpack_from(">I", blob)[0]
        if length > 128 or blob[4 : 4 + length].decode("ascii") != fields[0]:
            raise ValueError("public-key type mismatch")
    except (ValueError, UnicodeError, binascii.Error, struct.error) as exc:
        raise ValueError("Invalid public-key encoding") from exc
    return fields[0] + " " + base64.b64encode(blob).decode("ascii")


def key_identity(line):
    # Existing keys may carry options and comments; preserve those lines exactly.
    fields = line.split()
    for index, part in enumerate(fields[:-1]):
        if part in KEY_TYPES:
            try:
                return normalize_key(part + " " + fields[index + 1])
            except ValueError:
                return None
    return None


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, data, owner=None, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".setup-pending-" + secrets.token_hex(12)
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if owner is not None:
                os.fchown(stream.fileno(), *owner)
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        if owner is not None:
            os.fchmod(directory, 0o700)
            os.fchown(directory, *owner)
        os.fsync(directory)
        fsync_dir(path.parent.parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def add_public_key(path, value, owner=None, validate=True):
    key = normalize_key(value)
    if validate:
        with tempfile.TemporaryDirectory(prefix="fascia-public-key-") as temporary:
            candidate = Path(temporary) / "key.pub"
            candidate.write_text(key + "\n")
            result = subprocess.run(
                ["ssh-keygen", "-l", "-f", str(candidate)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode:
                raise ValueError("ssh-keygen rejected this public key")
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("SSH key paths must not be symlinks")
    if path.exists() and path.stat().st_size > 1024 * 1024:
        raise ValueError("authorized_keys exceeds the setup size limit")
    existing = path.read_text() if path.exists() else ""
    if any(
        key_identity(line) == key
        for line in existing.splitlines()
        if not line.lstrip().startswith("#")
    ):
        return False
    text = (
        existing
        + ("\n" if existing and not existing.endswith("\n") else "")
        + key
        + " fascia-setup\n"
    )
    atomic_bytes(path, text.encode(), owner)
    return True


class Progress:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {"schema_version": 1, "stages": {}, "completed": False}
        self.recovered = False
        if self.path.is_symlink():
            raise ValueError("setup progress cannot be a symlink")
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(8193)
            if len(raw) > 8192:
                raise ValueError("oversized progress")
            value = json.loads(raw)
            if (
                type(value) is not dict
                or type(value.get("schema_version")) is not int
                or value.get("schema_version") != 1
                or type(value.get("stages")) is not dict
            ):
                raise ValueError("invalid progress")
            if any(
                type(value["stages"].get(stage, False)) is not bool for stage in STAGES
            ):
                raise ValueError("invalid stage")
            self.data = {
                "schema_version": 1,
                "stages": {
                    stage: value["stages"].get(stage, False) for stage in STAGES
                },
                "completed": value.get("completed") is True
                and all(value["stages"].get(stage) is True for stage in STAGES),
            }
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, RecursionError):
            self.recovered = True

    def done(self, stage):
        return self.data["stages"].get(stage, False)

    def advance(self, stage):
        previous = dict(self.data["stages"])
        self.data["stages"][stage] = True
        try:
            self.save()
        except BaseException:
            self.data["stages"] = previous
            raise

    def save(self):
        atomic_bytes(self.path, (json.dumps(self.data, sort_keys=True) + "\n").encode())

    def complete(self):
        self.data["completed"] = all(self.done(stage) for stage in STAGES)
        self.save()


def persist_identity(**values):
    # Legacy development installations keep their existing writable-/etc behavior.
    if not IDENTITY_ROOT.is_dir():
        return
    source = Path("/usr/lib/fascia/provision/fascia_identity.py")
    if not source.is_file():
        source = (
            Path(__file__).resolve().parents[1] / "image/provision/fascia_identity.py"
        )
    spec = importlib.util.spec_from_file_location("fascia_identity_settings", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.change(IDENTITY_ROOT, **values)


def timezone(value):
    if (
        not re.fullmatch(r"[A-Za-z0-9_+/-]{1,128}", value)
        or any(p in ("", ".", "..") for p in value.split("/"))
        or value.startswith("/")
    ):
        raise ValueError("Use a timezone name such as Europe/London or UTC")
    try:
        ZoneInfo(value)
    except (KeyError, ValueError) as exc:
        raise ValueError("Unknown timezone") from exc
    return value


def utc_time(value):
    if not re.fullmatch(r"20\d\d-\d\d-\d\d \d\d:\d\d:\d\d", value):
        raise ValueError("Use UTC YYYY-MM-DD HH:MM:SS (2000..2099)")
    return datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=datetime.timezone.utc
    )


class SystemOps:
    def __init__(self, user="fascia"):
        self.user = pwd.getpwnam(user)

    @staticmethod
    def run(args):
        subprocess.run(args, check=True)

    @staticmethod
    def query(args):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=4)
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def get_hostname(self):
        return self.query(["hostnamectl", "--static"]) or "fascia"

    def set_hostname(self, value):
        if value != self.get_hostname():
            self.run(["hostnamectl", "set-hostname", hostname(value)])
        persist_identity(hostname=hostname(value))
        if self.query(["systemctl", "is-active", "avahi-daemon"]) == "active":
            self.run(["systemctl", "reload-or-restart", "avahi-daemon"])

    def get_timezone(self):
        return (
            self.query(["timedatectl", "show", "--property=Timezone", "--value"])
            or "UTC"
        )

    def set_timezone(self, value):
        value = timezone(value)
        self.run(["timedatectl", "set-timezone", value])
        persist_identity(timezone=value)

    def set_time(self, value):
        moment = utc_time(value)
        # A failed clock change must not leave a stale trusted manual marker.
        TIME_MARKER.unlink(missing_ok=True)
        self.run(["timedatectl", "set-ntp", "false"])
        self.run(["date", "-u", "--set", value])
        TIME_MARKER.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        marker = {
            "schema_version": 1,
            "unix_seconds": int(moment.timestamp()),
            "boot_id": BOOT_ID_PATH.read_text().strip(),
        }
        atomic_bytes(TIME_MARKER, (json.dumps(marker) + "\n").encode(), mode=0o644)
        # Keep trying network synchronization after an offline manual correction.
        self.run(["timedatectl", "set-ntp", "true"])

    @staticmethod
    def iwd(*args):
        result = subprocess.run(
            ["busctl", "--system", "--json=short", "--timeout=3", *args],
            capture_output=True,
            text=True,
            timeout=4,
            check=True,
        )
        if not result.stdout.strip():
            return None  # Void D-Bus method replies have no JSON payload.
        data = json.loads(result.stdout)["data"]
        return data if args[0] == "get-property" else data[0] if data else None

    def wifi_objects(self):
        return self.iwd(
            "call",
            "net.connman.iwd",
            "/",
            "org.freedesktop.DBus.ObjectManager",
            "GetManagedObjects",
        )

    def scan_wifi(self):
        # Use typed D-Bus results, not iwctl's terminal-formatted network table.
        objects = self.wifi_objects()
        station = next(
            (
                path
                for path, interfaces in objects.items()
                if interfaces.get("net.connman.iwd.Device", {})
                .get("Name", {})
                .get("data")
                == "wlan0"
                and "net.connman.iwd.Station" in interfaces
            ),
            None,
        )
        if station is None:
            raise ValueError("Wi-Fi interface wlan0 is not ready")
        interface = "net.connman.iwd.Station"
        if not objects[station][interface].get("Scanning", {}).get("data", False):
            self.iwd("call", "net.connman.iwd", station, interface, "Scan")
        deadline = time.monotonic() + 12
        while True:
            time.sleep(0.25)
            if not self.iwd(
                "get-property", "net.connman.iwd", station, interface, "Scanning"
            ):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Wi-Fi scan is taking too long")
        ordered = self.iwd(
            "call", "net.connman.iwd", station, interface, "GetOrderedNetworks"
        )
        objects = self.wifi_objects()
        state = objects.get(station, {}).get(interface, {})
        connected = (
            state.get("ConnectedNetwork", {}).get("data")
            if state.get("State", {}).get("data") == "connected"
            else None
        )
        networks = []
        for path, strength in ordered:
            network = objects.get(path, {}).get("net.connman.iwd.Network", {})
            name = network.get("Name", {}).get("data", "")
            if not name:
                continue
            networks.append(
                {
                    "name": name,
                    "security": network.get("Type", {}).get("data", "unknown"),
                    "dbm": round(strength / 100),
                    "connected": path == connected,
                }
            )
        return networks

    def connect_wifi(self, ssid):
        if not ssid or len(ssid.encode()) > 32 or "\x00" in ssid or "\n" in ssid:
            raise ValueError("SSID must contain 1..32 UTF-8 bytes")
        # iwd owns its interactive secret prompt and persistent Wi-Fi profile.
        # No password in argv, our JSON state, a pipe or captured command output.
        self.run(["iwctl", "station", "wlan0", "connect", ssid])

    def connect_wifi_touch(self, ssid, password):
        if (
            not isinstance(ssid, str)
            or not ssid
            or len(ssid.encode()) > 32
            or any(c in ssid for c in "\0\r\n")
        ):
            raise ValueError("Enter a Wi-Fi name of 1 to 32 bytes")
        if (
            not isinstance(password, str)
            or len(password.encode()) > 128
            or any(c in password for c in "\0\r\n")
        ):
            raise ValueError("Invalid Wi-Fi password")
        pid, fd = pty.fork()
        if pid == 0:
            try:
                attrs = termios.tcgetattr(0)
                attrs[3] &= ~termios.ECHO
                termios.tcsetattr(0, termios.TCSANOW, attrs)
                os.execlp("iwctl", "iwctl", "station", "wlan0", "connect", ssid)
            finally:
                os._exit(127)
        deadline = time.monotonic() + 45
        output = b""
        answered = False
        reaped = False
        try:
            while time.monotonic() < deadline:
                readable, _, _ = select.select([fd], [], [], 0.1)
                if readable:
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError:
                        chunk = b""
                    output = (output + chunk)[-8192:]
                    plain = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", output)
                    if not answered and re.search(
                        rb"(?:^|[\r\n])(?:Passphrase|Password): *$", plain, re.I
                    ):
                        attrs = termios.tcgetattr(fd)
                        attrs[3] &= ~termios.ECHO
                        termios.tcsetattr(fd, termios.TCSANOW, attrs)
                        os.write(fd, password.encode() + b"\n")
                        answered = True
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited:
                    reaped = True
                    if status != 0:
                        raise ValueError(
                            "Could not join Wi-Fi. Check the password and try again."
                        )
                    return
            raise TimeoutError(
                "Wi-Fi connection timed out. Try again or continue offline."
            )
        finally:
            os.close(fd)
            if not reaped:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)

    def key_count(self):
        path = Path(self.user.pw_dir) / ".ssh/authorized_keys"
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            return 0
        return sum(
            key_identity(line) is not None
            for line in path.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )

    def add_key(self, value):
        return add_public_key(
            Path(self.user.pw_dir) / ".ssh" / "authorized_keys",
            value,
            (self.user.pw_uid, self.user.pw_gid),
        )

    def ssh_auth(self):
        try:
            config = json.loads((IDENTITY_ROOT / "config.json").read_text())
            if config.get("ssh_password_auth") is True:
                return "password"
        except (OSError, ValueError):
            pass
        return "key" if self.key_count() else "unconfigured"

    def disable_ssh(self):
        persist_identity(ssh_enabled=False)
        self.run(["systemctl", "disable", "--now", "sshd.service"])
        self.run(["systemctl", "mask", "sshd.service"])

    def enable_ssh(self):
        self.run(["systemctl", "unmask", "sshd.service"])
        self.run(["systemctl", "enable", "--now", "sshd.service"])
        persist_identity(ssh_enabled=True)

    def snapshot(self):
        addresses = []
        raw = self.query(["ip", "-j", "-4", "address", "show"])
        try:
            for item in json.loads(raw):
                if item.get("ifname") == "lo":
                    continue
                for address in item.get("addr_info", []):
                    if address.get("family") == "inet":
                        addresses.append(
                            {"interface": item["ifname"], "address": address["local"]}
                        )
        except (ValueError, KeyError, TypeError):
            pass
        active = self.query(["systemctl", "is-active", "sshd.service"]) == "active"
        avahi = (
            self.query(["systemctl", "is-active", "avahi-daemon.service"]) == "active"
        )
        fingerprint = self.query(
            ["ssh-keygen", "-l", "-f", "/etc/ssh/ssh_host_ed25519_key.pub"]
        )
        return {
            "hostname": self.get_hostname(),
            "mdns_service_active": avahi,
            "ssh_active": active,
            "ssh_auth": self.ssh_auth(),
            "addresses": addresses,
            "ssh_host_fingerprint": fingerprint or None,
        }


class Wizard:
    def __init__(self, progress, ops, ask=input, tell=print, plain=False):
        self.progress, self.ops, self.ask, self.tell = progress, ops, ask, tell
        self.ui = Terminal(tell, plain)

    def prompt(self, text, validate=None):
        while True:
            value = self.ask(self.ui.prompt(text)).strip()
            if not value or validate is None:
                return value
            try:
                validate(value)
                return value
            except ValueError as error:
                self.ui.note(str(error))

    def stage_hostname(self):
        current = self.ops.get_hostname()
        self.ui.note(
            "This becomes your frame's .local address. Enter keeps the current name."
        )
        value = self.prompt("Name [" + current + "]:", hostname)
        self.ops.set_hostname(hostname(value or current))

    def stage_timezone(self):
        current = self.ops.get_timezone()
        self.ui.note("For example Europe/London. Enter keeps the current timezone.")
        value = self.prompt("Timezone [" + current + "]:", timezone)
        self.ops.set_timezone(timezone(value or current))

    def stage_time(self):
        self.ui.note(
            "Enter uses network time. Offline? Set UTC for this boot; network time will take over when available."
        )
        value = self.prompt("UTC YYYY-MM-DD HH:MM:SS [automatic]:", utc_time)
        if value:
            self.ops.set_time(value)

    def stage_wifi(self):
        self.ui.note(
            "Enter keeps your current connection or continues offline. Your password is entered privately at iwd's prompt."
        )
        while True:
            self.ui.note("Scanning nearby networks...")
            try:
                networks = self.ops.scan_wifi()
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                subprocess.SubprocessError,
            ):
                networks = []
                self.ui.note(
                    "Scan unavailable. Enter a network name, rescan, or continue offline."
                )
            if networks:
                for number, network in enumerate(networks, 1):
                    bars = sum(
                        network["dbm"] >= level for level in (-90, -75, -65, -55)
                    )
                    signal = "#" * bars + "." * (4 - bars)
                    security = {
                        "psk": "secured",
                        "open": "open",
                        "8021x": "enterprise",
                        "wep": "WEP",
                    }.get(network["security"], "unknown")
                    marker = "  connected" if network["connected"] else ""
                    self.ui.note(
                        f"{number:>2}  [{signal}] {network['dbm']} dBm  {security}{marker}"
                    )
                    self.tell("      " + self.ui.clean(network["name"]))
            else:
                self.ui.note("No networks listed. You can enter a name manually.")
            self.ui.note(
                "Choose a number or type a name. r: rescan / m: enter a name / Enter: keep or skip."
            )
            while True:
                choice = self.ask(self.ui.prompt("Network:"))
                if choice == "":
                    return
                if choice.lower() == "r":
                    break
                if choice.lower() == "m":
                    ssid = self.ask(self.ui.prompt("Wi-Fi name [Enter cancels]:"))
                    if not ssid:
                        continue
                elif choice.isascii() and choice.isdigit():
                    number = int(choice)
                    if not 1 <= number <= len(networks):
                        self.ui.note(
                            "Choose a listed number, or use m to enter a numeric network name."
                        )
                        continue
                    ssid = networks[number - 1]["name"]
                else:
                    ssid = choice
                if (
                    not ssid
                    or len(ssid.encode()) > 32
                    or any(c in ssid for c in "\x00\r\n")
                ):
                    self.ui.note("A Wi-Fi name must contain 1 to 32 UTF-8 bytes.")
                    continue
                self.ops.connect_wifi(ssid)
                self.ui.note("Connection accepted. Your network is saved by iwd.")
                return

    def stage_ssh(self):
        self.ui.note(
            "Paste a public key from your computer, e.g. ~/.ssh/id_ed25519.pub. Enter keeps existing keys; private keys belong on your computer."
        )
        key = self.prompt("Public SSH key [keep / skip]:", normalize_key)
        if key:
            self.ui.note(
                "Public key added."
                if self.ops.add_key(key)
                else "That public key is already installed."
            )
        self.ui.note(
            "SSH uses the credentials chosen during installation, plus any added keys. Enter keeps the current access setting."
        )
        while True:
            choice = self.prompt("Enable SSH? [y / keep]:").lower()
            if choice in ("y", "yes"):
                self.ops.enable_ssh()
                break
            if choice in ("", "n", "no"):
                break
            self.ui.note("Type y or n, or press Enter to keep the current setting.")

    def summary(self):
        value = self.ops.snapshot()
        name = value["hostname"]
        mdns = value["mdns_service_active"]
        ssh = value["ssh_active"]
        rows = [
            "Name: " + name,
            "mDNS service active: " + ("yes - " + name + ".local" if mdns else "no"),
            "SSH: " + ("active" if ssh else "disabled / inactive"),
        ]
        addresses = value["addresses"]
        for address in addresses:
            label = (
                "Wi-Fi IPv4"
                if address["interface"] == "wlan0"
                else address["interface"]
            )
            rows.append(label + ": " + address["address"])
        if not any(address["interface"] == "wlan0" for address in addresses):
            rows.append("Wi-Fi IPv4: not connected")
        self.ui.card("Connections", rows)
        commands = []
        if ssh:
            try:
                safe_name = hostname(name)
                if mdns:
                    commands.append("ssh fascia@" + safe_name + ".local")
            except ValueError:
                pass
            for address in addresses:
                try:
                    ip = ipaddress.IPv4Address(address["address"])
                except ipaddress.AddressValueError:
                    continue
                commands.append("ssh fascia@" + str(ip))
        if commands:
            self.tell("")
            self.ui.note("Connect from your computer:")
            for command in commands:
                self.tell("    " + self.ui.paint(command, "1;36"))
        if mdns:
            self.ui.note(".local name resolution from your computer is not verified.")
        if value["ssh_host_fingerprint"]:
            self.tell("")
            self.ui.note("SSH host key: " + value["ssh_host_fingerprint"])
        if not ssh:
            self.ui.note("SSH is off. Enable it in setup when you want remote access.")

    def run_stage(self, stage):
        self.ui.step(stage, self.progress)
        getattr(self, "stage_" + stage)()
        self.progress.advance(stage)
        self.ui.saved()

    def resume(self):
        completed = sum(self.progress.done(stage) for stage in STAGES)
        self.ui.heading(
            "Fascia / Setup",
            "USB console  /  "
            + (
                f"Resuming with {completed} of {len(STAGES)} steps saved"
                if completed
                else "Let's get your frame ready"
            ),
        )
        self.ui.note(
            "Enter keeps defaults or skips optional steps. Ctrl-C pauses; reconnect and run fascia setup to resume."
        )
        if self.progress.recovered:
            self.ui.note(
                "Saved progress could not be read. Rechecking setup; your name, networks and access keys are preserved."
            )
        for stage in STAGES:
            if not self.progress.done(stage):
                self.run_stage(stage)
        self.progress.complete()
        self.ui.heading(
            "Setup saved",
            "Your frame is ready to use. Open fascia setup to change anything later.",
        )
        self.summary()

    def menu(self):
        self.ui.heading(
            "Fascia / Settings",
            "Changes are saved as you go. Enter returns to the shell.",
        )
        while True:
            self.tell("")
            for number, label in (
                ("1", "Connections"),
                ("2", "Wi-Fi"),
                ("3", "SSH access"),
                ("4", "Fascia name"),
                ("5", "Review all setup"),
                ("6", "Timezone"),
                ("7", "Set UTC time"),
                ("0", "Done"),
            ):
                self.tell("  " + self.ui.paint(number, "36") + "  " + label)
            choice = self.prompt("Choose [0]:")
            if choice in ("", "0"):
                return
            if choice == "1":
                self.summary()
            elif choice in ("2", "3", "4", "6", "7"):
                stage = {
                    "2": "wifi",
                    "3": "ssh",
                    "4": "hostname",
                    "6": "timezone",
                    "7": "time",
                }[choice]
                self.run_stage(stage)
                self.summary()
            elif choice == "5":
                self.ui.note(
                    "Reviewing keeps your existing name, keys and saved networks unless you change them."
                )
                for stage in STAGES:
                    self.run_stage(stage)
                self.progress.complete()
                self.summary()
            else:
                self.ui.note("Choose a number from 0 to 7.")


def touch_request(ops, progress, request):
    if not isinstance(request, dict) or set(request) - {
        "action",
        "value",
        "password",
        "key",
        "enable",
    }:
        raise ValueError("Invalid setup request")
    action = request.get("action")
    if action == "status":
        return dict(
            ops.snapshot(),
            timezone=ops.get_timezone(),
            stages=progress.data["stages"],
            completed=progress.data["completed"],
            key_count=ops.key_count(),
        )
    if action == "scan":
        return {"networks": ops.scan_wifi()}
    value = request.get("value", "")
    if not isinstance(value, str) or len(value) > 16384:
        raise ValueError("Invalid setup value")
    if action == "hostname":
        ops.set_hostname(hostname(value))
        progress.advance("hostname")
    elif action == "timezone":
        ops.set_timezone(timezone(value))
        progress.advance("timezone")
        progress.advance(
            "time"
        )  # Network time; manual UTC remains available over serial.
    elif action == "wifi":
        if value:
            ops.connect_wifi_touch(value, request.get("password", ""))
        progress.advance("wifi")
    elif action == "ssh":
        key = request.get("key", "")
        enable = request.get("enable", False)
        if not isinstance(key, str) or type(enable) is not bool:
            raise ValueError("Invalid SSH settings")
        if key:
            ops.add_key(key)
        if enable:
            if ops.key_count() == 0 and ops.snapshot().get("ssh_auth") != "password":
                raise ValueError(
                    "Add a public SSH key first, or keep SSH off and add it over USB later."
                )
            ops.enable_ssh()
        elif "enable" in request:
            ops.disable_ssh()
        progress.advance("ssh")
    elif action == "finish":
        if not all(progress.done(stage) for stage in STAGES if stage != "ssh"):
            raise ValueError("Finish the remaining setup steps first")
        progress.advance("ssh")
        progress.complete()
    else:
        raise ValueError("Unknown setup action")
    return touch_request(ops, progress, {"action": "status"})


def main():
    parser = argparse.ArgumentParser(prog="fascia-setup", description=__doc__)
    parser.add_argument(
        "--status", action="store_true", help="read-only actual connection status JSON"
    )
    parser.add_argument("--plain", action="store_true", help="disable terminal styling")
    parser.add_argument("--request", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    ops = SystemOps()
    if args.status:
        status = ops.snapshot()
        status["setup_completed"] = Progress("/var/lib/fascia/setup.json").data[
            "completed"
        ]
        print(json.dumps(status, sort_keys=True))
        return
    if os.geteuid() != 0:
        raise PermissionError(
            "Run fascia setup through its installed privileged wrapper"
        )
    if not args.request and not os.isatty(0):
        raise ValueError("Interactive serial terminal required")
    lock_fd = os.open(
        "/run/fascia-setup.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Setup is already open on another terminal")
        progress = Progress("/var/lib/fascia/setup.json")
        if args.request:
            try:
                raw = sys.stdin.buffer.read(20001)
                if len(raw) > 20000:
                    raise ValueError("Setup request is too large")
                result = touch_request(ops, progress, json.loads(raw))
                print(json.dumps(dict(ok=True, **result)))
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(json.dumps({"ok": False, "error": str(error)}))
            return
        wizard = Wizard(progress, ops, plain=args.plain)
        if progress.data["completed"]:
            wizard.menu()
        else:
            wizard.resume()
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print(
            "\nSetup paused. Reconnect and run fascia setup to continue; completed stages were saved."
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        if "--request" in sys.argv:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(
                "Setup paused: "
                + str(exc)
                + ". Reconnect and run fascia setup to retry."
            )
            raise SystemExit(1)
