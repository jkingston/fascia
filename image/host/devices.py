#!/usr/bin/env python3
"""Identify an explicit external target and keep its open descriptor stable."""

import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys

MINIMUM_BYTES = 8 * 1024**3
MAC_IDENTITY = (
    "DeviceIdentifier",
    "DeviceNode",
    "DeviceTreePath",
    "MediaUUID",
    "MediaName",
    "BusProtocol",
    "TotalSize",
)
LINUX_IDENTITY = ("path", "maj:min", "size", "serial", "wwn", "model", "tran")


def command(*args):
    return subprocess.check_output(args, stdin=subprocess.DEVNULL)


def validate_capacity(capacity):
    if (
        type(capacity) is not int
        or not MINIMUM_BYTES <= capacity <= 2 * 1024**4
        or capacity % 512
    ):
        raise ValueError("Specify the exact card capacity in bytes, at least 8 GiB")


def mac_info(name):
    return plistlib.loads(command("diskutil", "info", "-plist", name))


def validate_mac(info, name, capacity):
    validate_capacity(capacity)
    if not re.fullmatch(r"disk[0-9]+", name):
        raise ValueError("Specify a whole disk identifier such as disk9")
    if (
        info.get("DeviceIdentifier") != name
        or info.get("DeviceNode") != "/dev/" + name
        or info.get("WholeDisk") is not True
        or info.get("Internal") is not False
        or info.get("VirtualOrPhysical") != "Physical"
        or info.get("OSInternalMedia") is True
    ):
        raise ValueError(
            "Only the explicitly identified external physical disk is allowed"
        )
    if info.get("Writable") is not True or info.get("ReadOnlyMedia") is True:
        raise ValueError("The selected card is read-only")
    if info.get("TotalSize") != capacity:
        raise ValueError(
            "The card capacity differs from the requested exact byte count"
        )
    return {key: info.get(key) for key in MAC_IDENTITY}


def linux_info(name):
    if not re.fullmatch(r"/dev/(sd[a-z]+|mmcblk[0-9]+)", name):
        raise ValueError(
            "Specify a whole external disk such as /dev/sdb or /dev/mmcblk1"
        )
    devices = json.loads(command("lsblk", "--bytes", "--json", "--output-all", name))[
        "blockdevices"
    ]
    if len(devices) != 1:
        raise ValueError("Expected exactly one whole disk")
    return devices[0]


def descendants(info):
    yield info
    for child in info.get("children", []):
        yield from descendants(child)


def validate_linux(info, name, capacity, swaps=()):
    validate_capacity(capacity)
    if not re.fullmatch(r"/dev/(sd[a-z]+|mmcblk[0-9]+)", name):
        raise ValueError("Specify an external whole-disk device, not a partition")
    if info.get("path") != name or info.get("type") != "disk":
        raise ValueError("The target is not the requested whole disk")
    if info.get("tran") != "usb" and info.get("rm") not in (True, 1):
        raise ValueError("Only USB or removable external media is allowed")
    if info.get("ro") not in (False, 0) or info.get("size") != capacity:
        raise ValueError("The target is read-only or its exact capacity changed")
    for item in descendants(info):
        if item is not info and item.get("type") != "part":
            raise ValueError("Target contains an active storage mapping")
        if item.get("path") in swaps:
            raise ValueError("Target contains active swap")
        for mount in item.get("mountpoints") or []:
            if mount and not mount.startswith(("/media/", "/run/media/", "/mnt/")):
                raise ValueError(
                    "Target contains a system or unfamiliar mount: " + mount
                )
    return {key: info.get(key) for key in LINUX_IDENTITY}


def active_swaps():
    return [
        line.split()[0] for line in Path("/proc/swaps").read_text().splitlines()[1:]
    ]


def inspect(name, capacity):
    if sys.platform == "darwin":
        identity = validate_mac(mac_info(name), name, capacity)
        path = "/dev/" + name
    elif sys.platform.startswith("linux"):
        identity = validate_linux(linux_info(name), name, capacity, active_swaps())
        path = name
    else:
        raise ValueError("Card installation currently supports macOS and Linux")
    node = os.stat(path, follow_symlinks=False)
    if not stat.S_ISBLK(node.st_mode):
        raise ValueError("Expected the selected whole-disk block node")
    identity["device_node"] = {
        "device": node.st_dev,
        "inode": node.st_ino,
        "rdevice": node.st_rdev,
        "ctime_ns": node.st_ctime_ns,
    }
    return identity


def assert_identity(expected, actual):
    if expected != actual:
        raise ValueError("The selected disk identity changed; no write is authorized")


def unmount(name):
    if sys.platform == "darwin":
        subprocess.run(["diskutil", "unmountDisk", name], check=True)
    else:
        for item in reversed(list(descendants(linux_info(name)))):
            for mount in sorted(
                set(item.get("mountpoints") or []) - {None}, key=len, reverse=True
            ):
                subprocess.run(["umount", "--", mount], check=True)


def open_target(name, capacity, expected):
    """Caller owns cleanup. Return a checked whole-device FD, never a guessed path."""
    assert_identity(expected, inspect(name, capacity))
    unmount(name)
    assert_identity(expected, inspect(name, capacity))
    path = "/dev/" + name if sys.platform == "darwin" else name
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISBLK(before.st_mode):
        raise ValueError("Expected the whole-disk block device")
    flags = os.O_RDWR | os.O_NOFOLLOW
    if sys.platform.startswith("linux"):
        flags |= os.O_EXCL
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISBLK(opened.st_mode) or (
            before.st_dev,
            before.st_ino,
            before.st_rdev,
        ) != (opened.st_dev, opened.st_ino, opened.st_rdev):
            raise ValueError("The device node changed while opening it")
        assert_identity(expected, inspect(name, capacity))
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def eject(name):
    if sys.platform == "darwin":
        subprocess.run(["diskutil", "eject", name], check=True)
    else:
        subprocess.run(["blockdev", "--flushbufs", name], check=True)


def list_targets():
    count = 0
    if sys.platform == "darwin":
        listing = plistlib.loads(
            command("diskutil", "list", "-plist", "external", "physical")
        )
        for disk in listing.get("AllDisksAndPartitions", []):
            name = disk["DeviceIdentifier"]
            info = mac_info(name)
            print(
                f"{name}\t{info.get('TotalSize', 0)} bytes\t{info.get('MediaName', '')}"
            )
            count += 1
    elif sys.platform.startswith("linux"):
        listing = json.loads(command("lsblk", "--bytes", "--json", "--output-all"))
        for item in listing["blockdevices"]:
            if item.get("type") == "disk" and (
                item.get("tran") == "usb" or item.get("rm") in (True, 1)
            ):
                print(
                    f"{item.get('path')}\t{item.get('size', 0)} bytes\t{item.get('model', '')}"
                )
                count += 1
    else:
        raise ValueError("Only macOS and Linux are supported")
    if not count:
        print("No external disks found. Insert the spare card into its reader.")
