#!/usr/bin/env python3
"""Trusted local Fascia app releases. No sandbox, network download or shell execution."""

import argparse
import configparser
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile

ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
ARCH = "arm-linux-gnueabihf"
MAX_FILES, MAX_BYTES = 10000, 256 * 1024 * 1024


def valid_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value) or value == "home":
        raise ValueError("invalid app id")
    return value


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def manifest(directory):
    """Read the AppDir desktop entry; normalized JSON is private host metadata."""
    entries = list(directory.glob("*.desktop"))
    if len(entries) != 1:
        raise ValueError("AppDir requires exactly one root desktop entry")
    path = entries[0]
    app_id = valid_id(path.stem)
    with path.open("rb") as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise ValueError("desktop entry exceeds 8 KiB")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(raw.decode("utf-8"))
        entry = parser["Desktop Entry"]
    except (configparser.Error, KeyError, UnicodeError) as error:
        raise ValueError("invalid desktop entry") from error
    if (
        parser.defaults()
        or entry.get("Type") != "Application"
        or entry.get("Exec") != "AppRun"
    ):
        raise ValueError("expected Type=Application and Exec=AppRun")

    def text(key, limit, default=None):
        value = entry.get(key, default)
        if not value or len(value.encode()) > limit or any(ord(c) < 32 for c in value):
            raise ValueError("invalid desktop entry " + key)

        # Desktop Entry string escapes, not shell quoting.
        def unescape(match):
            char = match.group(1)
            if char not in {"s", "\\"}:
                raise ValueError("unsupported control escape in " + key)
            return " " if char == "s" else "\\"

        return re.sub(r"\\(.)", unescape, value)

    icon = text("Icon", 80)
    if icon != app_id or not (directory / (icon + ".png")).is_file():
        raise ValueError("Icon must name the app's root PNG")
    if not (directory / ".DirIcon").is_file():
        raise ValueError("AppDir requires .DirIcon")
    with (directory / (icon + ".png")).open("rb") as stream:
        signature = stream.read(8)
    if signature != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid PNG icon")
    return dict(
        schema_version=1,
        id=app_id,
        name=text("Name", 80),
        version=text("X-Fascia-Version", 80),
        description=text("Comment", 240, "Application"),
        entrypoint="AppRun",
        architecture=ARCH,
        display="cpu",
        min_glibc="2.43",
    )


def safe_path(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or ".." in p.parts or "\\" in name or "\x00" in name:
        raise ValueError("unsafe bundle path")
    if any(x in (".git", ".local", "secrets", ".ssh") for x in p.parts):
        raise ValueError("private/build-cache directory is not an app payload")
    return p


def copy_bundle(source, dest):
    total, count = 0, 0
    links = []

    def link(target, value):
        nonlocal count
        count += 1
        if (
            count > MAX_FILES
            or not value
            or len(value.encode()) > 4096
            or os.path.isabs(value)
            or "\\" in value
            or "\x00" in value
        ):
            raise ValueError("unsafe bundle link")
        links.append((target, value))

    def copy_file(inp, target, size, mode):
        nonlocal total, count
        count += 1
        total += size
        if count > MAX_FILES or total > MAX_BYTES:
            raise ValueError("bundle too large")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as out:
            copied = 0
            while True:
                chunk = inp.read(65536)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > size:
                    raise ValueError("bundle file changed size while copying")
                out.write(chunk)
            if copied != size:
                raise ValueError("incomplete bundle file")
            out.flush()
            os.fchmod(out.fileno(), 0o755 if mode & 0o111 else 0o644)
            os.fsync(out.fileno())

    if source.is_dir():
        if source.is_symlink():
            raise ValueError("bundle directory cannot be a symlink")
        for base, dirs, files in os.walk(source, followlinks=False):
            for name in dirs + files:
                path = Path(base) / name
                rel = safe_path(path.relative_to(source).as_posix())
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    link(dest / str(rel), os.readlink(path))
                elif stat.S_ISDIR(info.st_mode):
                    count += 1
                    if count > MAX_FILES:
                        raise ValueError("bundle has too many entries")
                    (dest / str(rel)).mkdir(parents=True, exist_ok=True)
                elif stat.S_ISREG(info.st_mode):
                    with path.open("rb") as inp:
                        copy_file(inp, dest / str(rel), info.st_size, info.st_mode)
                else:
                    raise ValueError("bundle links and special files are forbidden")
    else:
        with tarfile.open(source, "r:*") as archive:
            for item in archive:
                rel = safe_path(item.name)
                target = dest / str(rel)
                if item.isdir():
                    count += 1
                    if count > MAX_FILES:
                        raise ValueError("bundle has too many entries")
                    target.mkdir(parents=True, exist_ok=True)
                elif item.issym():
                    link(target, item.linkname)
                elif item.isfile():
                    with archive.extractfile(item) as inp:
                        copy_file(inp, target, item.size, item.mode)
                else:
                    raise ValueError("bundle links and special files are forbidden")
    # Reject nested archive links that could escape staging.
    link_paths = {target for target, _ in links}
    for target, _ in links:
        if any(parent in link_paths for parent in target.parents):
            raise ValueError("bundle entry below a symlink")
    # Create links last: no archive entry can write through a previous symlink.
    for target, value in links:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(value)
    for target, _ in links:
        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError("broken or cyclic bundle link") from error
        if not resolved.is_relative_to(dest.resolve()):
            raise ValueError("bundle link escapes AppDir")
    if (dest / ".fascia.json").exists():
        raise ValueError("reserved host metadata")
    info = manifest(dest)
    atomic_json(dest / ".fascia.json", info)
    (dest / ".fascia.json").chmod(0o644)
    run = dest / "AppRun"
    if not run.is_file() or not os.access(run, os.X_OK):
        raise ValueError("bundle requires executable AppRun")
    with run.open("rb") as f:
        header = f.read(20)
    if header.startswith(b"\x7fELF") and (
        len(header) < 20
        or header[4:6] != b"\x01\x01"
        or struct.unpack_from("<H", header, 18)[0] != 40
    ):
        raise ValueError("AppRun ELF must be 32-bit little-endian ARM")
    if not header.startswith((b"\x7fELF", b"#!")):
        raise ValueError("AppRun must be ARM ELF or an explicit interpreter script")
    # Root-installed payloads must be traversable by the fascia user.
    os.chmod(dest, 0o755)
    build = release_digest(dest)
    for path in sorted((p for p in dest.rglob("*") if p.is_dir()), reverse=True):
        fsync_dir(path)
    fsync_dir(dest)
    return info, build


def release_digest(directory):
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0link\0")
            digest.update(os.readlink(path).encode() + b"\0")
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError("release contains links or special files")
        if stat.S_ISREG(mode):
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
            digest.update(bytes([bool(path.stat().st_mode & 0o111)]))
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    digest.update(chunk)
    return digest.hexdigest()[:24]


class Store:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.apps = self.root / "apps"
        self.state = self.root / "state"
        self.settings = self.root / "settings.json"

    @contextlib.contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        fsync_dir(self.root.parent)
        for path in (self.root, self.apps, self.state):
            if path.is_symlink():
                raise ValueError("registry directories cannot be symlinks")
            path.mkdir(exist_ok=True)
        fsync_dir(self.root)
        with (self.root / ".registry.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for directory in self.apps.iterdir():
                if ID.fullmatch(directory.name) and directory.is_dir():
                    self.recover_pointers(self.app_dir(directory.name))
            yield

    def app_dir(self, app_id):
        path = self.apps / valid_id(app_id)
        if path.is_symlink():
            raise ValueError("app directory cannot be a symlink")
        return path

    def active(self, app_id, link="active"):
        app = self.app_dir(app_id)
        value = os.readlink(app / link)
        if not re.fullmatch(r"releases/[a-f0-9]{24}", value):
            raise ValueError("invalid release pointer")
        if (app / "releases").is_symlink():
            raise ValueError("releases cannot be a symlink")
        release = app / value
        if release.is_symlink() or not release.is_dir():
            raise ValueError("missing release")
        info = manifest(release)
        if info["id"] != app_id:
            raise ValueError("manifest id does not match directory")
        return value, info

    def point(self, app, name, target):
        pending = app / ("." + name + "-pending")
        pending.unlink(missing_ok=True)
        os.symlink(target, pending)
        os.replace(pending, app / name)
        fsync_dir(app)

    def recover_pointers(self, directory):
        journal = directory / ".pointer-transaction.json"
        if not journal.exists():
            return
        with journal.open("rb") as stream:
            raw = stream.read(2049)
        if len(raw) > 2048:
            raise ValueError("pointer transaction too large")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("invalid pointer transaction")
        for name in ("previous", "active"):
            target = data.get(name)
            if target is not None and (
                not isinstance(target, str)
                or not re.fullmatch(r"releases/[a-f0-9]{24}", target)
            ):
                raise ValueError("invalid transaction pointer")
        # Keep the journal until both restored paths are durable.
        for name in ("previous", "active"):
            target = data[name]
            if target is None:
                (directory / name).unlink(missing_ok=True)
                fsync_dir(directory)
            else:
                self.point(directory, name, target)
        journal.unlink()
        fsync_dir(directory)

    def set_pointers(self, directory, active, previous):
        old = {"schema_version": 1}
        for name in ("active", "previous"):
            try:
                old[name] = os.readlink(directory / name)
            except FileNotFoundError:
                old[name] = None
        for target in old.values():
            if isinstance(target, str) and not re.fullmatch(
                r"releases/[a-f0-9]{24}", target
            ):
                raise ValueError("invalid existing release pointer")
        journal = directory / ".pointer-transaction.json"
        atomic_json(journal, old)
        for name, target in (("previous", previous), ("active", active)):
            if target is not None:
                self.point(directory, name, target)
        journal.unlink()
        fsync_dir(directory)

    def install(self, source, before_activate=None):
        with self.locked():
            staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.apps))
            try:
                info, build = copy_bundle(Path(source), staging)
                app = self.app_dir(info["id"])
                app.mkdir(exist_ok=True)
                releases = app / "releases"
                if releases.is_symlink():
                    raise ValueError("releases cannot be a symlink")
                releases.mkdir(exist_ok=True)
                fsync_dir(app)
                fsync_dir(self.apps)
                final = releases / build
                if final.exists():
                    if final.is_symlink():
                        raise ValueError("release cannot be a symlink")
                    if not final.is_dir() or release_digest(final) != build:
                        raise ValueError(
                            "existing release content does not match its hash"
                        )
                    os.chmod(final, 0o755)
                    fsync_dir(final)
                    shutil.rmtree(staging)
                else:
                    os.rename(staging, final)
                    fsync_dir(releases)
                state = self.state / info["id"]
                if state.is_symlink():
                    raise ValueError("state cannot be a symlink")
                state.mkdir(exist_ok=True)
                for name in ("config", "data", "state", "cache"):
                    directory = state / name
                    if directory.is_symlink():
                        raise ValueError("XDG directory cannot be a symlink")
                    directory.mkdir(exist_ok=True)
                fsync_dir(state)
                fsync_dir(self.state)
                old = None
                try:
                    old, _ = self.active(info["id"])
                except FileNotFoundError:
                    pass
                target = "releases/" + build
                if before_activate:
                    before_activate()
                try:
                    previous = os.readlink(app / "previous")
                except FileNotFoundError:
                    previous = None
                self.set_pointers(
                    app, target, old if old and old != target else previous
                )
                return info
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

    def rollback(self, app_id):
        with self.locked():
            active, _ = self.active(app_id)
            previous, info = self.active(app_id, "previous")
            app = self.app_dir(app_id)
            self.set_pointers(app, previous, active)
            return info

    def read_settings(self):
        try:
            with self.settings.open("rb") as stream:
                raw = stream.read(8193)
            if len(raw) > 8192:
                raise ValueError("settings too large")
            data = json.loads(raw)
            if (
                type(data.get("schema_version")) is not int
                or data.get("schema_version") != 1
                or type(data.get("startup_delay_seconds", 10)) is not int
            ):
                raise ValueError("invalid settings schema")
            delay = data.get("startup_delay_seconds", 10)
            if not 5 <= delay <= 60:
                raise ValueError("invalid startup delay")
            app_id = data.get("default_app")
            if app_id is not None:
                valid_id(app_id)
                self.active(app_id)
            return {
                "schema_version": 1,
                "default_app": app_id,
                "startup_delay_seconds": delay,
            }
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            return {
                "schema_version": 1,
                "default_app": None,
                "startup_delay_seconds": 10,
            }

    def default(self, app_id, delay=None):
        with self.locked():
            if app_id != "home":
                self.active(app_id)
            data = self.read_settings()
            data["default_app"] = None if app_id == "home" else app_id
            if delay is not None:
                if not 5 <= delay <= 60:
                    raise ValueError("startup delay must be 5..60")
                data["startup_delay_seconds"] = delay
            atomic_json(self.settings, data)

    def uninstall(self, app_id, delete_state=False):
        with self.locked():
            app = self.app_dir(app_id)
            data = self.read_settings()
            if data["default_app"] == app_id:
                data["default_app"] = None
                atomic_json(self.settings, data)
            tomb = self.apps / (".removed-" + app_id)
            if tomb.exists():
                shutil.rmtree(tomb)
            os.rename(app, tomb)
            fsync_dir(self.apps)
            shutil.rmtree(tomb)
            if delete_state:
                state = self.state / app_id
                if state.is_symlink():
                    raise ValueError("state cannot be a symlink")
                if state.exists():
                    shutil.rmtree(state)
                fsync_dir(self.state)
            fsync_dir(self.apps)

    def list(self):
        result = []
        if self.apps.exists():
            for path in sorted(self.apps.iterdir()):
                if not ID.fullmatch(path.name) or path.name == "home":
                    continue
                try:
                    _, info = self.active(path.name)
                    result.append(info)
                except (OSError, ValueError, KeyError):
                    continue
        return result


SERVICE_SOCKET = "/run/pf-kiosk/fascia-home.sock"


def ipc_socket_path(socket_path=None):
    """Explicit destinations win; SSH sessions discover the kiosk service socket."""
    if socket_path:
        return os.fspath(socket_path)
    if explicit := os.environ.get("FASCIA_HOME_SOCKET"):
        return explicit
    if runtime := os.environ.get("XDG_RUNTIME_DIR"):
        candidate = os.path.join(runtime, "fascia-home.sock")
        if Path(candidate).is_socket():
            return candidate
    if Path(SERVICE_SOCKET).is_socket():
        return SERVICE_SOCKET
    return f"/run/user/{os.getuid()}/fascia-home.sock"


def ipc(command, socket_path=None):
    path = ipc_socket_path(socket_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(path)
        client.sendall((command + "\n").encode())
        result = client.recv(1024).decode("utf-8", "replace").strip()
    if not result.startswith("ok"):
        raise ValueError(result or "empty compositor response")
    return result


def main():
    parser = argparse.ArgumentParser(prog="fascia-app", description=__doc__)
    parser.add_argument(
        "--root", default=os.environ.get("FASCIA_HOME_ROOT", "/var/lib/fascia")
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("FASCIA_HOME_SOCKET"),
        help="explicit compositor socket",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("settings")
    commands.add_parser("home")
    p = commands.add_parser("install")
    p.add_argument("bundle")
    for name in ("rollback", "launch", "uninstall", "default"):
        p = commands.add_parser(name)
        p.add_argument("id")
        if name == "uninstall":
            state = p.add_mutually_exclusive_group()
            state.add_argument(
                "--delete-state",
                "--purge-state",
                action="store_true",
                help="explicitly delete app state",
            )
            state.add_argument(
                "--keep-state",
                action="store_false",
                dest="delete_state",
                help="preserve app state (default)",
            )
            p.set_defaults(delete_state=False)
        if name == "default":
            p.add_argument("--delay", type=int)
    args = parser.parse_args()
    store = Store(args.root)
    if args.command == "install":
        result = store.install(args.bundle)
    elif args.command == "rollback":
        result = store.rollback(args.id)
    elif args.command == "uninstall":
        valid_id(args.id)
        # Compositor atomically stops only this app; an unrelated app stays running.
        # No running compositor is also a valid maintenance/offline installation.
        try:
            ipc("stop " + args.id, args.socket)
            # Home appears immediately; wait for SIGTERM/cleanup before deleting files.
            environment = dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}")
            unit = "fascia-app-" + args.id + ".service"
            loaded = subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    "--property=LoadState",
                    "--value",
                    unit,
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            if loaded.stdout.strip() != "not-found":
                subprocess.run(
                    ["systemctl", "--user", "stop", unit], env=environment, check=True
                )
        except (FileNotFoundError, ConnectionRefusedError):
            pass
        result = store.uninstall(args.id, args.delete_state)
    elif args.command == "default":
        result = store.default(args.id, args.delay)
    elif args.command == "settings":
        result = store.read_settings()
    elif args.command == "list":
        result = store.list()
    elif args.command == "home":
        result = ipc("home", args.socket)
    elif args.command == "launch":
        valid_id(args.id)
        store.active(args.id)
        result = ipc("launch " + args.id, args.socket)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        ValueError,
        KeyError,
        RecursionError,
        tarfile.TarError,
        subprocess.CalledProcessError,
    ) as exc:
        print("fascia-app: " + str(exc), file=sys.stderr)
        sys.exit(1)
