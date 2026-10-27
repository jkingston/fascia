#!/usr/bin/env python3
"""Narrow privileged device actions for Fascia Home; no arbitrary commands."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import importlib.util

BACKLIGHT = Path("/sys/class/backlight/backlight")
SETTINGS = Path("/var/lib/fascia/device.json")
BRIGHTNESS_SOCKET = Path("/run/fascia-device/brightness.sock")
DISPLAY_OVERRIDE = Path("/run/systemd/system/pf-kiosk.service.d/98-display-check.conf")
HOME_BINARY = Path("/usr/lib/fascia/bin/fascia-host")
BUTTON_BINARY = Path("/usr/local/libexec/fascia-home-button")
IDENTITY_ROOT = Path("/data/identity")
BUTTON_OVERRIDE = Path("/etc/systemd/system/pf-kiosk.service.d/97-physical-home.conf")


def save(value):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=".device-", dir=SETTINGS.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, SETTINGS)
        directory = os.open(SETTINGS.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def backlight_maximum():
    maximum = int((BACKLIGHT / "max_brightness").read_text())
    if not 1 <= maximum <= 0xFFFFFFFF:
        raise ValueError("invalid backlight range")
    return maximum


def brightness_level(level):
    maximum = backlight_maximum()
    if type(level) is not int or not 1 <= level <= maximum:
        raise ValueError(f"brightness level must be between 1 and {maximum}")
    (BACKLIGHT / "brightness").write_text(str(level))
    if int((BACKLIGHT / "brightness").read_text()) != level:
        raise OSError("backlight did not accept the requested level")
    return level


def level_for_percent(percent):
    if type(percent) is not int or not 5 <= percent <= 100:
        raise ValueError("brightness must be between 5 and 100")
    maximum = backlight_maximum()
    return max(1, min(maximum, round(maximum * percent / 100)))


def brightness(percent):
    return brightness_level(level_for_percent(percent))


def request_brightness(level, *, durable):
    if not BRIGHTNESS_SOCKET.exists():
        return False
    verb = "save" if durable else "set"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(BRIGHTNESS_SOCKET))
        client.sendall(f"{verb} {level}\n".encode("ascii"))
        response = b""
        while b"\n" not in response:
            part = client.recv(96 - len(response))
            if not part or len(response) + len(part) >= 96:
                raise ValueError("brightness service did not confirm the change")
            response += part
    parts = response.decode("ascii").strip().split()
    if len(parts) != 3 or parts[0] != "ok":
        raise ValueError("brightness service could not confirm the change")
    actual, maximum = int(parts[1]), int(parts[2])
    if actual != level or maximum != backlight_maximum():
        raise ValueError("brightness changed again before confirmation")
    return True


def change_brightness(level):
    if type(level) is not int or not 1 <= level <= backlight_maximum():
        raise ValueError("invalid brightness level")
    # A fallback write could race the service and overwrite newer preferences.
    if not request_brightness(level, durable=True):
        brightness_level(level)
        save_brightness(level)
    return level


def apply_restored_brightness(level):
    if not request_brightness(level, durable=False):
        brightness_level(level)


def save_brightness(level):
    maximum = backlight_maximum()
    # Keep a percentage fallback for older helpers. Exact level/range preserve
    # coarse hardware steps and allow proportional migration to a new table.
    percent = max(5, (level * 100 + maximum // 2) // maximum)
    save(
        {
            "schema_version": 1,
            "brightness": percent,
            "brightness_level": level,
            "brightness_maximum": maximum,
        }
    )


def restore_brightness(value):
    if (
        type(value) is not dict
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
    ):
        raise ValueError("invalid saved brightness")
    if "brightness_level" in value or "brightness_maximum" in value:
        level = value.get("brightness_level")
        maximum = value.get("brightness_maximum")
        if (
            type(level) is not int
            or type(maximum) is not int
            or not 1 <= maximum <= 0xFFFFFFFF
            or not 1 <= level <= maximum
        ):
            raise ValueError("invalid saved brightness level")
        current = backlight_maximum()
        # Integer nearest rounding avoids both percentage loss and float error.
        restored = max(1, min(current, (level * current + maximum // 2) // maximum))
        apply_restored_brightness(restored)
    else:
        apply_restored_brightness(level_for_percent(value.get("brightness")))


def display_adjustment(opening):
    if opening:
        if not HOME_BINARY.is_file() or not os.access(HOME_BINARY, os.X_OK):
            raise ValueError("Fascia Home is not installed")
        DISPLAY_OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".display-", dir=DISPLAY_OVERRIDE.parent
        )
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(
                    "[Service]\nEnvironment=FASCIA_HOME_DISPLAY=1\nEnvironment=FASCIA_AUTOSTART=0\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, DISPLAY_OVERRIDE)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    else:
        DISPLAY_OVERRIDE.unlink(missing_ok=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "restart", "pf-kiosk.service"], check=True)


def persist_physical_home(enabled):
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
    module.change(IDENTITY_ROOT, physical_home=enabled)


def physical_home(enabled):
    """Opt in only after the board's PL2 button has been verified."""
    if enabled:
        if not BUTTON_BINARY.is_file() or not os.access(BUTTON_BINARY, os.X_OK):
            raise ValueError("Physical Home support is not installed")
        # Type=notify waits for GPIO and uinput initialization, not just exec.
        subprocess.run(["systemctl", "start", "fascia-home-button.service"], check=True)
        subprocess.run(
            ["systemctl", "enable", "fascia-home-button.service"], check=True
        )
        BUTTON_OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".physical-home-", dir=BUTTON_OVERRIDE.parent
        )
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write("[Service]\nEnvironment=FASCIA_HOME_OVERLAY=auto\n")
                os.fchmod(stream.fileno(), 0o644)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, BUTTON_OVERRIDE)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    else:
        BUTTON_OVERRIDE.unlink(missing_ok=True)
    if BUTTON_OVERRIDE.parent.is_dir():
        directory = os.open(BUTTON_OVERRIDE.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    if not enabled:
        subprocess.run(
            ["systemctl", "disable", "--now", "fascia-home-button.service"], check=True
        )
    persist_physical_home(enabled)
    subprocess.run(["systemctl", "restart", "pf-kiosk.service"], check=True)


def system_update(action, bundle=None):
    if action not in ("status", "rollback") or bundle is not None:
        raise ValueError(
            "Use sudo pacman -Syu for updates; recovery accepts status or rollback"
        )
    subprocess.run(["/usr/local/libexec/fascia-system-update", action], check=True)


def main():
    parser = argparse.ArgumentParser(prog="fascia-system", description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "update",
            "brightness",
            "brightness-level",
            "restore",
            "sound-test",
            "shutdown",
            "display-open",
            "display-close",
            "button-enable",
            "button-disable",
        ],
    )
    parser.add_argument("value", nargs="?")
    parser.add_argument("bundle", nargs="?")
    args = parser.parse_args()
    if args.action != "update" and args.bundle is not None:
        parser.error("recovery status/rollback do not accept extra arguments")
    if args.action == "update":
        system_update(args.value, args.bundle)
    elif args.action in ("brightness", "brightness-level"):
        if args.value is None or not args.value.isascii() or not args.value.isdigit():
            parser.error("brightness requires an integer value")
        value = int(args.value)
        level = level_for_percent(value) if args.action == "brightness" else value
        change_brightness(level)
        result = {"saved": True, "level": level, "maximum": backlight_maximum()}
        if args.action == "brightness":
            result["brightness"] = value
        print(json.dumps(result))
    elif args.action == "shutdown":
        if args.value is not None:
            parser.error("shutdown takes no value")
        subprocess.run(["/usr/bin/systemctl", "poweroff"], check=True)
    elif args.action == "restore":
        if args.value is not None:
            parser.error("restore takes no value")
        if SETTINGS.exists():
            value = json.loads(SETTINGS.read_text())
            restore_brightness(value)
    elif args.action in ("display-open", "display-close"):
        if args.value is not None:
            parser.error("display adjustment takes no value")
        display_adjustment(args.action == "display-open")
        print(
            "Display adjustment opened."
            if args.action == "display-open"
            else "Display adjustment closed."
        )
    elif args.action in ("button-enable", "button-disable"):
        if args.value is not None:
            parser.error("physical Home configuration takes no value")
        physical_home(args.action == "button-enable")
        print(
            "Physical Home enabled."
            if args.action == "button-enable"
            else "Physical Home disabled."
        )
    else:
        if args.value is not None:
            parser.error("sound-test takes no value")
        subprocess.run(
            ["systemctl", "start", "--no-block", "pf-speaker-test.service"], check=True
        )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit("Could not confirm change: " + str(error))
