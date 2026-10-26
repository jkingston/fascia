#!/usr/bin/env python3
"""Grow only the shared Btrfs partition of the running, verified Fascia SD image."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import tempfile
from typing import NamedTuple

SIGNATURE = 0x46524D45
START = 260 * 2048
SIZE_OFFSET = 446 + 16 + 12
SECTOR = 512
BLKROGET = 0x125E
BLKSSZGET = 0x1268
# Linux encodes sizeof(size_t) in this request, even though the result is u64.
# ARMv7 therefore differs from a 64-bit host.
BLKGETSIZE64 = 0x80001272 | (struct.calcsize("P") << 16)


class Target(NamedTuple):
    disk: Path
    partition: Path
    sysfs: Path
    disk_device: int
    partition_device: int


def mount_record(text, point):
    matches = []
    for line in text.splitlines():
        fields = line.split()
        separator = fields.index("-")
        if fields[4] == point:
            matches.append(
                (fields[3], fields[5], fields[separator + 1], fields[separator + 2])
            )
    if len(matches) != 1:
        raise ValueError("Expected one mounted " + point)
    return matches[0]


def discover():
    identity = json.loads(Path("/etc/fascia-system.json").read_text())
    if identity.get("board_id") != "pf1007l-pl5":
        raise ValueError("Not a supported Fascia image")
    records = Path("/proc/self/mountinfo").read_text()
    system = mount_record(records, "/system")
    data = mount_record(records, "/data")
    root = mount_record(records, "/")
    if system[0] != "/" or "rw" not in system[1].split(",") or system[2] != "btrfs":
        raise ValueError("System store must be the writable Btrfs top level")
    if (
        data[0] != "/data"
        or "rw" not in data[1].split(",")
        or data[2] != "btrfs"
        or not root[0].startswith("/generations/")
        or "rw" not in root[1].split(",")
        or root[2] != "btrfs"
        or len({system[3], data[3], root[3]}) != 1
        or not re.fullmatch(r"/dev/mmcblk[0-9]+p2", system[3])
    ):
        raise ValueError("Expected shared storage and a writable Fascia generation")
    partition = Path(system[3])
    disk = Path(str(partition)[:-2])
    sysfs = (Path("/sys/class/block") / partition.name).resolve(strict=True)
    parent = (Path("/sys/class/block") / disk.name).resolve(strict=True)
    if sysfs.parent != parent or (parent / "device/type").read_text().strip() != "SD":
        raise ValueError("Only the boot SD card may expand; internal MMC is refused")
    if (sysfs / "partition").read_text().strip() != "2":
        raise ValueError("Expected the second SD partition")
    if "root=PARTUUID=46524d45-02" not in Path("/proc/cmdline").read_text().split():
        raise ValueError("Boot partition identity does not match")
    devices = []
    for node, path in ((disk, parent), (partition, sysfs)):
        info = node.lstat()
        major, minor = map(int, (path / "dev").read_text().strip().split(":"))
        if not stat.S_ISBLK(info.st_mode) or info.st_rdev != os.makedev(major, minor):
            raise ValueError("Block node does not match mounted SD device")
        devices.append(info.st_rdev)
    return Target(disk, partition, sysfs, *devices)


def plan(header, sectors, kernel_start, kernel_size):
    if len(header) != SECTOR or header[510:] != b"\x55\xaa":
        raise ValueError("Invalid MBR")
    if struct.unpack_from("<I", header, 440)[0] != SIGNATURE:
        raise ValueError("Unexpected Fascia disk signature")
    if not START + 4608 * 2048 <= sectors <= 0xFFFFFFFF:
        raise ValueError("Unsupported SD capacity or MBR address range")
    expected = ((0x80, 0x0C, 8192, 524288), (0, 0x83, START, None))
    for index, (boot, kind, start, size) in enumerate(expected):
        values = struct.unpack_from("<B3sB3sII", header, 446 + index * 16)
        if (values[0], values[2], values[4]) != (boot, kind, start):
            raise ValueError("Unexpected Fascia partition layout")
        if size is not None and values[5] != size:
            raise ValueError("Boot partition size changed")
    if header[478:510] != bytes(32):
        raise ValueError(
            "Refusing extra partitions or the previous three-partition layout"
        )
    current = struct.unpack_from("<I", header, SIZE_OFFSET)[0]
    desired = sectors - START
    if not 4608 * 2048 <= kernel_size <= current <= desired or kernel_start != START:
        raise ValueError(
            "Partition geometry is inconsistent; shrinking is never allowed"
        )
    after = bytearray(header)
    struct.pack_into("<I", after, SIZE_OFFSET, desired)
    return bytes(after), desired


def filesystem_size(superblock):
    if len(superblock) != 4096 or superblock[64:72] != b"_BHRfS_M":
        raise ValueError("Expected a Btrfs superblock")
    if struct.unpack_from("<Q", superblock, 136)[0] != 1:
        raise ValueError("Only single-device Btrfs storage may expand")
    size = struct.unpack_from("<Q", superblock, 112)[0]
    sector = struct.unpack_from("<I", superblock, 144)[0]
    if sector != 4096 or size < 4608 * 1024**2 or size % sector:
        raise ValueError("Unexpected Btrfs geometry")
    return size


def ioctl_number(fd, request, fmt):
    value = bytearray(struct.calcsize(fmt))
    fcntl.ioctl(fd, request, value, True)
    return struct.unpack(fmt, value)[0]


def open_device(path, device, writable=False):
    flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISBLK(info.st_mode) or info.st_rdev != device:
            raise ValueError("Device changed before opening")
        if (
            ioctl_number(fd, BLKROGET, "=I")
            or ioctl_number(fd, BLKSSZGET, "=I") != SECTOR
        ):
            raise ValueError("Expected a writable 512-byte-sector SD device")
        return fd
    except BaseException:
        os.close(fd)
        raise


def backup_mbr(directory, header):
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("Unsafe root-owned growth backup directory")
    path = directory / ("mbr-" + hashlib.sha256(header).hexdigest() + ".bin")
    if path.exists():
        if path.is_symlink() or path.read_bytes() != header:
            raise ValueError("Growth backup is inconsistent")
    else:
        fd, temporary = tempfile.mkstemp(prefix=".mbr-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(header)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def command(*arguments):
    subprocess.run(arguments, check=True, timeout=240)


def expand(target, backup, check=False):
    disk_fd = open_device(target.disk, target.disk_device, writable=not check)
    try:
        part_fd = open_device(target.partition, target.partition_device)
        try:
            capacity = ioctl_number(disk_fd, BLKGETSIZE64, "=Q")
            kernel_size = ioctl_number(part_fd, BLKGETSIZE64, "=Q") // SECTOR
            start = int((target.sysfs / "start").read_text())
            before = os.pread(disk_fd, SECTOR, 0)
            after, desired = plan(before, capacity // SECTOR, start, kernel_size)
            fs_size = filesystem_size(os.pread(part_fd, 4096, 65536))
            if fs_size > kernel_size * SECTOR:
                raise ValueError("Filesystem exceeds its kernel partition")
            if check:
                return {
                    "partition_bytes": desired * SECTOR,
                    "filesystem_bytes": fs_size,
                }
            if before != after:
                backup_mbr(backup, before)
                if os.pread(disk_fd, SECTOR, 0) != before:
                    raise ValueError("Partition table changed during expansion")
                # This image uses a fixed DOS layout. Change only p2's 32-bit
                # sector count; preserve every bootloader/identity/start byte.
                if (
                    os.pwrite(
                        disk_fd, after[SIZE_OFFSET : SIZE_OFFSET + 4], SIZE_OFFSET
                    )
                    != 4
                ):
                    raise OSError("Short partition-size write")
                os.fsync(disk_fd)
                if os.pread(disk_fd, SECTOR, 0) != after:
                    raise OSError("Partition-size verification failed")
            if kernel_size != desired:
                command("/usr/bin/partx", "--update", "--nr", "2", str(target.disk))
                if ioctl_number(part_fd, BLKGETSIZE64, "=Q") != desired * SECTOR:
                    raise OSError(
                        "Kernel partition size did not update; retry next boot"
                    )
            wanted = desired * SECTOR // 4096 * 4096
            if fs_size != wanted:
                command("/usr/bin/btrfs", "filesystem", "resize", "max", "/system")
                command("/usr/bin/btrfs", "filesystem", "sync", "/system")
                if filesystem_size(os.pread(part_fd, 4096, 65536)) != wanted:
                    raise OSError(
                        "Filesystem expansion did not reach the expected size"
                    )
            return {"partition_bytes": desired * SECTOR, "filesystem_bytes": wanted}
        finally:
            os.close(part_fd)
    finally:
        os.close(disk_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Inspect without writing")
    args = parser.parse_args()
    if os.geteuid() != 0 or os.uname().sysname != "Linux":
        parser.error("Requires Linux root on the new Fascia SD image")
    try:
        with open("/run/fascia-grow-storage.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            print(
                json.dumps(
                    expand(discover(), Path("/data/fascia-system/growth"), args.check)
                )
            )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, "Storage expansion deferred: " + str(error) + "\n")


if __name__ == "__main__":
    main()
