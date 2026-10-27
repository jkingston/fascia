"""Host-side SSH choices. Passwords are hashed before any installer job is saved."""

import base64
import getpass
import hashlib
import importlib.util
import os
from pathlib import Path
import secrets
import select
import shutil
import subprocess
import sys
import termios
import tty

PROJECT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "fascia_setup_prompts", PROJECT / "tools/fascia-setup.py"
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def arguments(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--ssh-key",
        type=Path,
        metavar="PUBLIC_KEY",
        help="Enable SSH with this public key",
    )
    group.add_argument(
        "--ssh-password",
        action="store_true",
        help="Prompt privately for an SSH password",
    )
    group.add_argument("--no-ssh", action="store_true", help="Leave SSH disabled")
    parser.add_argument(
        "--plain", action="store_true", help="Use numbered, unstyled prompts"
    )


def public_key(text):
    key = setup.normalize_key(text)
    try:
        result = subprocess.run(
            ["ssh-keygen", "-lf", "/dev/stdin"],
            input=key + "\n",
            text=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(
            "OpenSSH ssh-keygen is required to check public keys"
        ) from error
    if result.returncode:
        raise ValueError("That is not a valid SSH public key")
    digest = (
        base64.b64encode(hashlib.sha256(base64.b64decode(key.split()[1])).digest())
        .decode()
        .rstrip("=")
    )
    comment = " ".join(text.strip().split()[2:])
    return {
        "key": key,
        "fingerprint": "SHA256:" + digest,
        "comment": setup.Terminal.clean(comment),
    }


def key_file(path):
    path = Path(path).expanduser()
    if not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("Choose a public key file of at most 16 KiB")
    return dict(public_key(path.read_text()), name=path.name)


def existing_keys(directory=None):
    found = []
    for path in sorted((directory or Path.home() / ".ssh").glob("*.pub")):
        try:
            found.append(key_file(path))
        except (OSError, UnicodeError, ValueError):
            continue
    return found


def choose(ui, options, *, plain=False, default_label="SSH off"):
    """Arrow keys in a terminal; a numbered fallback needs only line input."""
    if (
        plain
        or not sys.stdin.isatty()
        or not sys.stdout.isatty()
        or os.environ.get("TERM") == "dumb"
    ):
        for index, (label, detail) in enumerate(options, 1):
            ui.tell(f"  {index:>2}  {ui.clean(label)}")
            if detail:
                ui.note("    " + detail)
        while True:
            answer = input(ui.prompt(f"Choose a number [{default_label}]:")).strip()
            if not answer:
                return len(options) - 1
            if answer.isascii() and answer.isdigit() and len(answer) < 4:
                number = int(answer)
                if 1 <= number <= len(options):
                    return number - 1
            ui.note(
                f"Choose one of the listed numbers, or press Enter for {default_label}."
            )
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    selected = len(options) - 1
    lines = 0
    try:
        tty.setcbreak(fd)
        while True:
            start = max(0, min(selected - 3, len(options) - 8))
            visible = options[start : start + 8]
            if lines:
                sys.stdout.write(f"\033[{lines}A\033[J")
            for index, (label, _) in enumerate(visible, start):
                line = ("> " if index == selected else "  ") + ui.clean(label)[
                    : ui.width - 6
                ]
                ui.tell("  " + (ui.paint(line, "1;36") if index == selected else line))
            ui.tell("")
            ui.tell("  " + ui.paint(ui.clean(options[selected][1])[: ui.width - 4]))
            ui.tell(
                "  " + ui.paint("Up/down to choose / Enter to confirm / Esc to cancel")
            )
            sys.stdout.flush()
            lines = len(visible) + 3
            char = os.read(fd, 1).decode("ascii", "ignore")
            if not char:
                raise EOFError()
            if char in ("\r", "\n"):
                return selected
            if char in ("j", "\x0e"):
                selected = (selected + 1) % len(options)
            elif char in ("k", "\x10"):
                selected = (selected - 1) % len(options)
            elif char == "\x1b":
                if not select.select([fd], [], [], 0.1)[0]:
                    raise KeyboardInterrupt()
                sequence = os.read(fd, 1)
                if select.select([fd], [], [], 0.1)[0]:
                    sequence += os.read(fd, 1)
                if sequence == b"[A":
                    selected = (selected - 1) % len(options)
                elif sequence == b"[B":
                    selected = (selected + 1) % len(options)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        ui.tell("")


def openssl():
    candidates = [
        shutil.which("openssl"),
        "/opt/homebrew/opt/openssl@3/bin/openssl",
        "/usr/local/opt/openssl@3/bin/openssl",
    ]
    for path in dict.fromkeys(filter(None, candidates)):
        try:
            result = subprocess.run(
                [path, "passwd", "-help"], capture_output=True, text=True, timeout=5
            )
            if "-6" in result.stdout + result.stderr:
                return path
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise ValueError(
        "Password access needs OpenSSL with SHA-512 support. On Mac: brew install openssl@3. Or choose an SSH key / SSH off."
    )


def password_hash(ui):
    executable = openssl()
    while True:
        first = getpass.getpass("  Password for fascia: ")
        if (
            len(first.encode()) < 12
            or len(first.encode()) > 128
            or any(c in first for c in "\0\r\n")
        ):
            ui.note("Use 12 to 128 bytes; a few words make a good passphrase.")
            continue
        second = getpass.getpass("  Repeat password: ")
        if first != second:
            ui.note("Those passwords did not match. Try again.")
            continue
        salt = "rounds=100000$" + "".join(
            secrets.choice(
                "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            )
            for _ in range(16)
        )
        result = subprocess.run(
            [executable, "passwd", "-6", "-salt", salt, "-stdin"],
            input=first + "\n",
            text=True,
            capture_output=True,
            timeout=30,
        )
        del first, second
        if result.returncode or not result.stdout.startswith("$6$rounds=100000$"):
            raise ValueError("OpenSSL could not produce a supported password hash")
        return result.stdout.strip()


def configure(args):
    ui = setup.Terminal(plain=getattr(args, "plain", False))
    if getattr(args, "no_ssh", False):
        result = {"mode": "off"}
    elif getattr(args, "ssh_key", None):
        key = key_file(args.ssh_key)
        result = {"mode": "key", "public_key": key["key"]}
        ui.card(
            "Remote access",
            [
                "SSH key: " + key["name"],
                key["fingerprint"],
                "Account: fascia / Password login: off",
            ],
        )
        return result
    else:
        if not sys.stdin.isatty():
            raise ValueError(
                "Choose --ssh-key PUBLIC_KEY, --ssh-password (interactive), or --no-ssh for this installation"
            )
        if getattr(args, "ssh_password", False):
            result = {"mode": "password", "password_hash": password_hash(ui)}
        else:
            keys = existing_keys()
            ui.heading(
                "Remote access", "Optional / Deploy apps and manage your frame over SSH"
            )
            ui.note(
                "Choose a public key, paste one, use a password, or leave SSH off. Private keys stay on your computer."
            )
            options = [
                (
                    key["name"]
                    + (" / " + key["comment"] if key["comment"] else "")
                    + "  (recommended)",
                    key["fingerprint"],
                )
                for key in keys
            ]
            options += [
                (
                    "Paste a public key",
                    "One ssh-ed25519, ssh-rsa or ECDSA public-key line",
                ),
                ("Use a password", "Typed privately twice / account: fascia"),
                (
                    "Leave SSH disabled",
                    "You can configure access later over USB serial",
                ),
            ]
            while True:
                selected = choose(ui, options, plain=getattr(args, "plain", False))
                if selected < len(keys):
                    result = {"mode": "key", "public_key": keys[selected]["key"]}
                    ui.note(keys[selected]["fingerprint"])
                    break
                if selected == len(keys):
                    try:
                        key = public_key(input(ui.prompt("Paste public key:")))
                    except ValueError as error:
                        ui.note(str(error))
                        continue
                    result = {"mode": "key", "public_key": key["key"]}
                    ui.note(key["fingerprint"])
                    break
                if selected == len(keys) + 1:
                    try:
                        result = {
                            "mode": "password",
                            "password_hash": password_hash(ui),
                        }
                    except ValueError as error:
                        ui.note(str(error))
                        continue
                    break
                result = {"mode": "off"}
                break
    ui.card(
        "Remote access",
        [
            {
                "off": "SSH disabled",
                "password": "SSH enabled / Password authentication / Account: fascia",
                "key": "SSH enabled / Key authentication / Account: fascia",
            }[result["mode"]]
        ],
    )
    return result
