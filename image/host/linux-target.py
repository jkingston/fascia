#!/usr/bin/env python3
"""Pin an approved, already-open Linux block target behind an owned loop FD.

The caller owns device discovery, unmounting, exclusive approval and mount namespace.
This module never opens the original target pathname and never formats anything.
"""

import errno
import contextlib
from types import SimpleNamespace
import fcntl
import os
from pathlib import Path
import stat
import struct
import sys

LOOP_CONFIGURE = 0x4C0A
LOOP_GET_STATUS64 = 0x4C05
LOOP_CLR_FD = 0x4C01
LOOP_CTL_GET_FREE = 0x4C82
LO_FLAGS_AUTOCLEAR = 4
LO_FLAGS_PARTSCAN = 8
INFO = struct.Struct("=QQQQQIIII64s64s32sQQ")
CONFIG = struct.Struct("=II")
BLKGETSIZE64 = (2 << 30) | (struct.calcsize("P") << 16) | (0x12 << 8) | 114
BLKSSZGET = 0x1268


def block_size(fd):
    result = bytearray(8)
    fcntl.ioctl(fd, BLKGETSIZE64, result, True)
    return struct.unpack("=Q", result)[0]


def verify_status(data, backing, capacity, number):
    values = INFO.unpack(data)
    device, inode, rdevice, offset, limit, index, crypt, keysize, flags = values[:9]
    if (device, inode, rdevice) != (backing.st_dev, backing.st_ino, backing.st_rdev):
        raise ValueError(
            "Loop backing no longer matches the approved opened descriptor"
        )
    if (offset, limit, index, crypt, keysize) != (0, capacity, number, 0, 0):
        raise ValueError(
            "Loop mapping offset, capacity, identity or encryption changed"
        )
    if flags != LO_FLAGS_AUTOCLEAR | LO_FLAGS_PARTSCAN:
        raise ValueError("Loop mapping flags changed")


class PinnedTarget:
    """Context manager returning /dev/loopN and retaining both opened file references."""

    def __init__(self, source_fd, capacity):
        if sys.platform != "linux":
            raise ValueError("Pinned block mappings require Linux")
        if type(capacity) is not int or capacity < 1024 * 1024 or capacity % 512:
            raise ValueError("Expected a positive, sector-aligned target capacity")
        self.source_fd = os.dup(source_fd)
        self.loop_fd = None
        self.path = None
        self.capacity = capacity
        try:
            self.backing = os.fstat(self.source_fd)
            if not (
                stat.S_ISBLK(self.backing.st_mode) or stat.S_ISREG(self.backing.st_mode)
            ):
                raise ValueError(
                    "Approved descriptor must be a block device or regular image"
                )
            if fcntl.fcntl(self.source_fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDWR:
                raise ValueError("Approved descriptor must be writable")
            if stat.S_ISBLK(self.backing.st_mode):
                sector = bytearray(4)
                fcntl.ioctl(self.source_fd, BLKSSZGET, sector, True)
                actual = block_size(self.source_fd)
                if struct.unpack("=I", sector)[0] != 512:
                    raise ValueError("Target logical sector size must be 512 bytes")
            else:
                actual = self.backing.st_size
            if actual != capacity:
                raise ValueError("Target capacity does not match approval")
        except BaseException:
            os.close(self.source_fd)
            self.source_fd = None
            raise

    def __enter__(self):
        try:
            with open("/dev/loop-control", "r+b", buffering=0) as control:
                for _ in range(16):
                    number = fcntl.ioctl(control.fileno(), LOOP_CTL_GET_FREE)
                    path = Path("/dev") / ("loop" + str(number))
                    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
                    try:
                        device = os.fstat(fd)
                        if (
                            not stat.S_ISBLK(device.st_mode)
                            or os.major(device.st_rdev) != 7
                            or os.minor(device.st_rdev) != number
                        ):
                            raise ValueError("Unexpected loop device node identity")
                        info = INFO.pack(
                            0,
                            0,
                            0,
                            0,
                            self.capacity,
                            0,
                            0,
                            0,
                            LO_FLAGS_AUTOCLEAR | LO_FLAGS_PARTSCAN,
                            b"fascia-approved-open-fd",
                            b"",
                            b"",
                            0,
                            0,
                        )
                        config = CONFIG.pack(self.source_fd, 512) + info + bytes(64)
                        fcntl.ioctl(fd, LOOP_CONFIGURE, config)
                    except OSError as error:
                        os.close(fd)
                        if error.errno == errno.EBUSY:
                            continue
                        raise
                    except BaseException:
                        os.close(fd)
                        raise
                    self.loop_fd, self.path, self.number = fd, path, number
                    self.loop_identity = (device.st_dev, device.st_ino, device.st_rdev)
                    self.verify()
                    return self
            raise RuntimeError("No exclusively configurable loop device available")
        except BaseException:
            self.close()
            raise

    def verify(self):
        if self.loop_fd is None:
            raise ValueError("Loop mapping is not active")
        current = os.stat(self.path, follow_symlinks=False)
        if (
            not stat.S_ISBLK(current.st_mode)
            or (current.st_dev, current.st_ino, current.st_rdev) != self.loop_identity
        ):
            raise ValueError("Loop device node was replaced")
        data = bytearray(INFO.size)
        fcntl.ioctl(self.loop_fd, LOOP_GET_STATUS64, data, True)
        verify_status(data, self.backing, self.capacity, self.number)
        if block_size(self.loop_fd) != self.capacity:
            raise ValueError("Loop capacity changed")
        return self.path

    def close(self):
        # Never detach a loop that failed to become our mapping. On a mounted mapping,
        # AUTOCLEAR defers release until the final user closes it; callers must unmount.
        try:
            if self.loop_fd is not None:
                try:
                    fcntl.ioctl(self.loop_fd, LOOP_CLR_FD)
                finally:
                    os.close(self.loop_fd)
                    self.loop_fd = None
        finally:
            if self.source_fd is not None:
                os.close(self.source_fd)
                self.source_fd = None

    def __exit__(self, *_):
        self.close()


def verify(path, identity):
    """Verify an existing owned loop mapping from its serialized kernel identity."""
    required = {
        "source_dev",
        "source_inode",
        "source_rdev",
        "loop_dev",
        "loop_inode",
        "loop_rdev",
        "number",
        "capacity",
    }
    if (
        not isinstance(identity, dict)
        or set(identity) != required
        or any(type(identity[k]) is not int or identity[k] < 0 for k in required)
    ):
        raise ValueError("Invalid pinned loop identity")
    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        node = os.fstat(fd)
        if not stat.S_ISBLK(node.st_mode) or (
            node.st_dev,
            node.st_ino,
            node.st_rdev,
        ) != (identity["loop_dev"], identity["loop_inode"], identity["loop_rdev"]):
            raise ValueError("Pinned loop node identity changed")
        data = bytearray(INFO.size)
        fcntl.ioctl(fd, LOOP_GET_STATUS64, data, True)
        backing = SimpleNamespace(
            st_dev=identity["source_dev"],
            st_ino=identity["source_inode"],
            st_rdev=identity["source_rdev"],
        )
        verify_status(data, backing, identity["capacity"], identity["number"])
        if block_size(fd) != identity["capacity"]:
            raise ValueError("Pinned loop capacity changed")
    finally:
        os.close(fd)


@contextlib.contextmanager
def attach(descriptor, capacity):
    """Yield a mapping and detach only this held loop after the helper exits."""
    with PinnedTarget(descriptor, capacity) as target:
        identity = {
            "source_dev": target.backing.st_dev,
            "source_inode": target.backing.st_ino,
            "source_rdev": target.backing.st_rdev,
            "loop_dev": target.loop_identity[0],
            "loop_inode": target.loop_identity[1],
            "loop_rdev": target.loop_identity[2],
            "number": target.number,
            "capacity": capacity,
        }
        mapping = {
            "path": str(target.path),
            "major": os.major(target.loop_identity[2]),
            "minor": os.minor(target.loop_identity[2]),
            "capacity": capacity,
            "loop_identity": identity,
        }
        verify(target.path, identity)
        yield mapping


def validate_backing(descriptor, identity):
    """Require the inherited descriptor to identify the approved opened source."""
    if type(descriptor) is not int or descriptor < 0:
        raise ValueError("Missing inherited backing descriptor")
    source = os.fstat(descriptor)
    if (source.st_dev, source.st_ino, source.st_rdev) != (
        identity["source_dev"],
        identity["source_inode"],
        identity["source_rdev"],
    ):
        raise ValueError("Inherited backing descriptor identity changed")
    if not (stat.S_ISREG(source.st_mode) or stat.S_ISBLK(source.st_mode)):
        raise ValueError("Inherited backing descriptor has invalid type")
    return source


def evict_backing(descriptor, identity):
    """Flush and request eviction on the approved held source, never a pathname."""
    source = validate_backing(descriptor, identity)
    os.fsync(descriptor)
    if stat.S_ISREG(source.st_mode):
        os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
        return "held-file-fsync-and-dontneed; kernel advisory, not media-cache proof"
    fcntl.ioctl(descriptor, 0x1261)  # BLKFLSBUF, Linux _IO(0x12, 97).
    return "held-block-fsync-and-flushbufs; not hardware-cache or physical-boot proof"
