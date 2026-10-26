#!/usr/bin/env python3
"""Pacman recovery checkpoints pairing Btrfs system snapshots with FAT boot files."""

import argparse
import fcntl
import importlib.util
import platform
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import tempfile
from pathlib import Path

BOARD = "pf1007l-pl5"
SCHEMA = 1
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
MAX_SEQUENCE = 2_000_000_000
KEYS = ("sequence", "good", "candidate", "attempts", "selected", "schema")


def bounded_read(path, limit):
    with Path(path).open("rb") as file:
        raw = file.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Oversized input")
    return raw


def generation(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Invalid generation id")
    return value


def canonical(state):
    if set(state) != set(KEYS):
        raise ValueError("Invalid boot state fields")
    for key in ("sequence", "attempts", "schema"):
        if type(state[key]) is not int:
            raise ValueError("Invalid boot state integer")
    if (
        not 0 <= state["sequence"] <= MAX_SEQUENCE
        or not 0 <= state["attempts"] <= 2
        or state["schema"] != SCHEMA
    ):
        raise ValueError("Invalid boot state range/schema")
    generation(state["good"])
    for key in ("candidate", "selected"):
        if state[key] != "-":
            generation(state[key])
    if state["candidate"] == state["good"] or state["selected"] not in (
        "-",
        state["good"],
        state["candidate"],
    ):
        raise ValueError("Inconsistent boot state")
    return (
        "FASCIA_BOOT_STATE_V1\n" + "".join(f"{key}={state[key]}\n" for key in KEYS)
    ).encode()


def encode(state):
    payload = canonical(state)
    return payload + b"checksum=" + hashlib.sha256(payload).hexdigest().encode() + b"\n"


def decode(raw):
    if len(raw) > 1024:
        raise ValueError("Oversized state")
    lines = raw.decode("ascii").splitlines(keepends=True)
    if (
        len(lines) != 8
        or not lines[-1].endswith("\n")
        or not lines[-1].startswith("checksum=")
    ):
        raise ValueError("Invalid state record")
    body = "".join(lines[:-1]).encode()
    if lines[-1] != "checksum=" + hashlib.sha256(body).hexdigest() + "\n":
        raise ValueError("State checksum mismatch")
    state = dict(line.rstrip("\n").split("=", 1) for line in lines[1:-1])
    for key in ("sequence", "attempts", "schema"):
        state[key] = int(state[key])
    if canonical(state) != body:
        raise ValueError("Noncanonical boot state")
    return state


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, raw):
    fd, name = tempfile.mkstemp(prefix=".boot-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        fsync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def read_state(data):
    valid = []
    for name in ("boot-a.state", "boot-b.state"):
        try:
            if (data / name).is_symlink():
                raise ValueError("Boot state must be a regular file")
            valid.append(decode(bounded_read(data / name, 1024)))
        except (OSError, ValueError, UnicodeError, KeyError):
            pass
    if not valid:
        raise ValueError(
            "No valid boot state: manual recovery required; no system selected"
        )
    valid.sort(key=lambda s: s["sequence"], reverse=True)
    if (
        len(valid) == 2
        and valid[0]["sequence"] == valid[1]["sequence"]
        and valid[0] != valid[1]
    ):
        raise ValueError("Conflicting boot state copies")
    return valid[0]


def write_state(data, state):
    raw = encode(state)
    # Either durable copy is sufficient. A torn second write preserves the newer first.
    atomic(data / "boot-a.state", raw)
    atomic(data / "boot-b.state", raw)


def next_state(state, **changes):
    return dict(state, sequence=state["sequence"] + 1, **changes)


def select_state(state):
    candidate = state["candidate"]
    selected = (
        candidate if candidate != "-" and state["attempts"] < 2 else state["good"]
    )
    attempts = state["attempts"] + 1 if selected == candidate else state["attempts"]
    return next_state(state, selected=selected, attempts=attempts)


def run(args):
    return subprocess.run(
        args, check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True
    ).stdout


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


class Updater:
    def __init__(self, systems, data, command=run):
        self.systems, self.data, self.command = Path(systems), Path(data), command

    def subvolume(self, name):
        generation(name)
        path = self.systems / name
        if not path.is_dir() or path.is_symlink():
            raise ValueError("Generation missing")
        result = self.command(["btrfs", "property", "get", "-ts", str(path), "ro"])
        if result.strip() not in ("ro=true", "ro=false"):
            raise ValueError("Generation is not a Btrfs subvolume")

    def initialize(self, name):
        self.data.mkdir(parents=True, exist_ok=True)
        if any((self.data / n).exists() for n in ("boot-a.state", "boot-b.state")):
            raise ValueError("Boot state already exists")
        self.subvolume(name)
        write_state(
            self.data,
            dict(
                sequence=0,
                good=name,
                candidate="-",
                attempts=0,
                selected="-",
                schema=SCHEMA,
            ),
        )

    def select(self):
        state = select_state(read_state(self.data))
        self.subvolume(state["selected"])
        write_state(self.data, state)
        return state["selected"]

    def confirm(self, running):
        state = read_state(self.data)
        generation(running)
        if state["selected"] != running or running not in (
            state["candidate"],
            state["good"],
        ):
            raise ValueError("Running generation does not match boot selection")
        self.subvolume(running)
        write_state(
            self.data,
            next_state(
                state, good=running, candidate="-", attempts=0, selected=running
            ),
        )


def validate_storage(
    growth=None, mountinfo=None, sysfs=Path("/sys/class/block"), lstat=None
):
    """Verify shared writable root/data/system and FAT boot belong to one real SD.

    Optional dependencies support offline tests; there are no environment bypasses.
    """
    if growth is None:
        spec = importlib.util.spec_from_file_location(
            "fascia_growth_guard", "/usr/lib/fascia/fascia-grow-storage.py"
        )
        growth = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(growth)
    target = growth.discover()
    records = (
        Path("/proc/self/mountinfo").read_text() if mountinfo is None else mountinfo
    )
    try:
        boot = growth.mount_record(records, "/boot")
    except (IndexError, ValueError) as error:
        raise ValueError("Expected exactly one valid /boot mount") from error
    expected = str(target.disk) + "p1"
    if (
        boot[0] != "/"
        or "rw" not in boot[1].split(",")
        or boot[2] not in ("vfat", "fat")
        or boot[3] != expected
    ):
        raise ValueError("Boot must be the writable FAT first partition of the same SD")
    path = (Path(sysfs) / Path(expected).name).resolve(strict=True)
    disk = (Path(sysfs) / target.disk.name).resolve(strict=True)
    if (
        path.parent != disk
        or (path / "partition").read_text().strip() != "1"
        or (disk / "device/type").read_text().strip() != "SD"
    ):
        raise ValueError("Boot partition sysfs identity is not the matching SD")
    info = Path(expected).lstat() if lstat is None else lstat(Path(expected))
    major, minor = map(int, (path / "dev").read_text().strip().split(":"))
    if not stat.S_ISBLK(info.st_mode) or info.st_rdev != os.makedev(major, minor):
        raise ValueError("Boot block node does not match the mounted SD")
    return target


def mounted_generation(mountinfo=None):
    records = (
        Path("/proc/self/mountinfo").read_text() if mountinfo is None else mountinfo
    )
    roots = []
    try:
        for line in records.splitlines():
            fields = line.split()
            separator = fields.index("-")
            if fields[4] == "/":
                if fields[separator + 1] != "btrfs" or "rw" not in fields[5].split(","):
                    raise ValueError("Running root must be a writable Btrfs generation")
                match = re.fullmatch(
                    r"/generations/([a-z0-9][a-z0-9-]{0,63})", fields[3]
                )
                if match is None:
                    raise ValueError(
                        "Running root is not a recognized system generation"
                    )
                roots.append(generation(match[1]))
    except (IndexError, ValueError) as error:
        raise ValueError(
            "Cannot establish the actual mounted root generation"
        ) from error
    if len(roots) != 1:
        raise ValueError("Expected exactly one mounted root generation")
    return roots[0]


class Recovery:
    """Root-only pacman transaction checkpoint; user data is deliberately excluded."""

    def __init__(
        self,
        systems,
        data,
        boot=Path("/boot"),
        root=Path("/"),
        command=run,
        running_generation=mounted_generation,
    ):
        self.systems, self.data, self.boot, self.root = map(
            Path, (systems, data, boot, root)
        )
        self.command = command
        self.running_generation = running_generation

    def current(self):
        name = json.loads(bounded_read(self.data / "recovery-current.json", 4096))[
            "checkpoint"
        ]
        generation(name)
        folder = self.data / "recovery" / name
        if folder.is_symlink():
            raise ValueError("Recovery directory cannot be a symlink")
        value = json.loads(bounded_read(folder / "manifest.json", 1024 * 1024))
        if value.get("schema_version") != 1 or value.get("checkpoint") != name:
            raise ValueError("Invalid recovery manifest")
        return folder, value

    def inventory(self, tree):
        files, total = {}, 0
        for path in sorted(tree.rglob("*")):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError("Boot tree contains a link or special file")
            if path.is_file():
                relative = path.relative_to(tree).as_posix()
                if len(relative) > 1024 or any(ord(c) < 32 for c in relative):
                    raise ValueError("Invalid boot filename")
                size = path.stat().st_size
                total += size
                if len(files) >= 4096 or total > 512 * 1024**2:
                    raise ValueError("Boot recovery budget exceeded")
                files[relative] = {"bytes": size, "sha256": digest(path)}
        if not all(
            name in files
            for name in ("zImage", "initramfs.img", "extlinux/extlinux.conf")
        ):
            raise ValueError("Boot tree is incomplete")
        return files, total

    def snapshot(self):
        state = read_state(self.data)
        expected = state["selected"] if state["selected"] != "-" else state["good"]
        if self.running_generation() != expected:
            raise ValueError(
                "Reboot into the selected recovery generation before another pacman transaction"
            )
        if state["candidate"] != "-":
            raise ValueError("Finish or roll back the pending recovery before pacman")
        files, total = self.inventory(self.boot)
        if shutil.disk_usage(self.data).free < total + 128 * 1024**2:
            raise ValueError("Insufficient recovery space; pacman transaction aborted")
        name = "recovery-" + str(time.time_ns())
        folder = self.data / "recovery" / name
        folder.mkdir(parents=True, mode=0o700)
        backup = folder / "boot"
        backup.mkdir(mode=0o700)
        # The root snapshot is never selected directly: restore creates a writable clone.
        self.command(
            [
                "btrfs",
                "subvolume",
                "snapshot",
                "-r",
                str(self.root),
                str(self.systems / name),
            ]
        )
        for relative in files:
            source, target = self.boot / relative, backup / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
        if self.inventory(backup)[0] != files:
            raise ValueError("Boot files changed while making recovery checkpoint")
        for directory in sorted(
            (p for p in backup.rglob("*") if p.is_dir()), reverse=True
        ):
            fsync_dir(directory)
        fsync_dir(backup)
        self.command(["btrfs", "filesystem", "sync", str(self.systems)])
        value = {
            "schema_version": 1,
            "checkpoint": name,
            "root_generation": state["selected"]
            if state["selected"] != "-"
            else state["good"],
            "kernel": platform.release(),
            "status": "prepared",
            "files": files,
        }
        atomic(
            folder / "manifest.json",
            (json.dumps(value, sort_keys=True) + "\n").encode(),
        )
        fsync_dir(folder.parent)
        atomic(
            self.data / "recovery-current.json",
            (json.dumps({"checkpoint": name}) + "\n").encode(),
        )
        return name

    def commit(self):
        folder, value = self.current()
        value["status"] = "committed"
        atomic(
            folder / "manifest.json",
            (json.dumps(value, sort_keys=True) + "\n").encode(),
        )
        return value["checkpoint"]

    def rollback(self):
        folder, value = self.current()
        backup = folder / "boot"
        if self.inventory(backup)[0] != value["files"]:
            raise ValueError("Recovery boot files failed integrity validation")
        snapshot = self.systems / generation(value["checkpoint"])
        if (
            snapshot.is_symlink()
            or not snapshot.is_dir()
            or self.command(
                ["btrfs", "property", "get", "-ts", str(snapshot), "ro"]
            ).strip()
            != "ro=true"
        ):
            raise ValueError("Recovery root snapshot is missing or mutable")
        name = "restore-" + str(time.time_ns())
        target = self.systems / name
        self.command(["btrfs", "subvolume", "snapshot", str(snapshot), str(target)])
        # This lock belongs to the interrupted transaction, not the restored clone.
        pacman_lock = target / "var/lib/pacman/db.lck"
        if pacman_lock.is_symlink():
            raise ValueError("Unexpected symlink at restored pacman lock")
        pacman_lock.unlink(missing_ok=True)
        self.command(["btrfs", "filesystem", "sync", str(self.systems)])
        # Publish extlinux last. Individual renames are atomic; the whole update is not.
        for relative in sorted(
            value["files"], key=lambda n: n == "extlinux/extlinux.conf"
        ):
            dest = self.boot / relative
            if self.boot.is_symlink() or any(
                (self.boot / part).is_symlink()
                for part in (Path(relative), *Path(relative).parents)
                if str(part) != "."
            ):
                raise ValueError("Boot restore path traverses a symlink")
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".fascia-restore-", dir=dest.parent)
            try:
                with os.fdopen(fd, "wb") as out, (backup / relative).open("rb") as src:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, dest)
                fsync_dir(dest.parent)
            finally:
                Path(temporary).unlink(missing_ok=True)
        state = read_state(self.data)
        write_state(
            self.data,
            next_state(state, good=name, candidate="-", attempts=0, selected="-"),
        )
        value.update(status="rollback-selected", restored_generation=name)
        atomic(
            folder / "manifest.json",
            (json.dumps(value, sort_keys=True) + "\n").encode(),
        )
        return name


def parse_presentation(raw, boot_id, monotonic_ms):
    if len(raw) > 256:
        raise ValueError("Oversized Home presentation record")
    fields = raw.decode("ascii").split()
    if (
        len(fields) != 4
        or not re.fullmatch(
            "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", fields[0]
        )
        or fields[0] != boot_id.strip()
    ):
        raise ValueError("Home presentation is not from this boot")
    values = []
    for field in fields[1:]:
        if not re.fullmatch("[1-9][0-9]{0,19}", field):
            raise ValueError("Invalid Home presentation process/time")
        values.append(int(field))
    compositor_pid, home_pid, stamp = values
    if (
        compositor_pid > 2**31 - 1
        or home_pid > 2**31 - 1
        or stamp > monotonic_ms + 1000
    ):
        raise ValueError("Invalid Home presentation process/time range")
    return compositor_pid, home_pid


def validate_presentation():
    compositor, home = parse_presentation(
        bounded_read("/run/fascia-home/presented", 256),
        bounded_read("/proc/sys/kernel/random/boot_id", 64).decode("ascii"),
        time.monotonic_ns() // 1_000_000,
    )
    identities = (
        ((home, "fascia-host"),)
        if compositor == home
        else ((compositor, "fascia-compositor"), (home, "fascia-home"))
    )
    for pid, expected in identities:
        os.kill(pid, 0)
        executable = Path(os.readlink(f"/proc/{pid}/exe")).name
        if (
            executable != expected
            or "pf-kiosk.service"
            not in bounded_read(f"/proc/{pid}/cgroup", 4096).decode()
        ):
            raise ValueError("Home presentation process is no longer the kiosk")
    return home


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--systems", type=Path, default=Path("/system/generations"))
    parser.add_argument("--data", type=Path, default=Path("/data/fascia-system"))
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "rollback", "health"):
        sub.add_parser(action)
    sub.add_parser("initialize").add_argument("generation")
    recovery = sub.add_parser("recovery")
    recovery.add_argument(
        "operation",
        choices=("snapshot", "boot-commit", "boot-restore", "validate-storage"),
    )
    args = parser.parse_args()
    if args.action != "status" and os.geteuid() != 0:
        parser.error("Run recovery changes as root")
    try:
        if args.action == "status":
            value = {"boot": read_state(args.data), "updates": "sudo pacman -Syu"}
            try:
                value["recovery"] = Recovery(args.systems, args.data).current()[1]
            except FileNotFoundError:
                value["recovery"] = None
            print(json.dumps(value, sort_keys=True))
            return
        identity = json.loads(bounded_read("/etc/fascia-system.json", 4096))
        if (
            identity.get("board_id") != BOARD
            or identity.get("shared_state_schema") != SCHEMA
        ):
            raise ValueError("Installed board/state schema mismatch")
        args.data.mkdir(parents=True, exist_ok=True)
        with (args.data / ".update.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            tool = Updater(args.systems, args.data)
            if args.action == "initialize":
                tool.initialize(args.generation)
            elif args.action == "health":
                validate_presentation()
                tool.confirm(
                    bounded_read("/run/fascia-system/generation", 128)
                    .decode("ascii")
                    .strip()
                )
            else:
                validate_storage()
                if args.action == "recovery" and args.operation == "validate-storage":
                    print(
                        "Matching writable Fascia SD root, data, system and boot mounts verified"
                    )
                    return
                r = Recovery(args.systems, args.data)
                if args.action == "rollback" or args.operation == "boot-restore":
                    print(
                        json.dumps(
                            {
                                "next_boot_generation": r.rollback(),
                                "reboot_required": True,
                            }
                        )
                    )
                elif args.operation == "snapshot":
                    print(r.snapshot())
                else:
                    print(r.commit())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Recovery failed: {error}\n")


if __name__ == "__main__":
    main()
