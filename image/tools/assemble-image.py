#!/usr/bin/env python3
"""Assemble a NEW regular SD image. Formats only verified partitions of its own file-backed loop."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import time

MIB = 1024 * 1024
SIGNATURE = 0x46524D45
KERNEL = "7.2.10-1-armv7-ARCH"
BOARD = "pf1007l-pl5"
DATA_DIRS = (
    "identity",
    "fascia",
    "iwd",
    "home/fascia",
    "systemd-timesync",
    "firmware",
    "fascia-system",
)


def layout(size_mib):
    if type(size_mib) is not int or not 4868 <= size_mib <= 65536:
        raise ValueError("Image size must be 4868..65536 MiB")
    return [(4, 256, 0x0C), (260, size_mib - 260, 0x83)]


def mbr(size_mib):
    block = bytearray(512)
    struct.pack_into("<I", block, 440, SIGNATURE)
    for index, (start, size, kind) in enumerate(layout(size_mib)):
        entry = struct.pack(
            "<B3sB3sII",
            0x80 if index == 0 else 0,
            b"\xfe\xff\xff",
            kind,
            b"\xfe\xff\xff",
            start * 2048,
            size * 2048,
        )
        block[446 + index * 16 : 462 + index * 16] = entry
    block[510:512] = b"\x55\xaa"
    return block


def digest(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(MIB), b""):
            sha.update(block)
    return sha.hexdigest()


def regular(path):
    path = Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Expected a regular file: " + str(path))
    return path


def copy_boot_dtbs(rootfs, board, boot):
    """Preserve Arch-owned DTBs, then apply the reviewed board overrides."""
    trees = [Path(rootfs) / "boot/dtbs", Path(board) / "boot/dtbs"]
    total = count = 0
    for tree in trees:
        if tree.parent.is_symlink() or tree.is_symlink() or not tree.is_dir():
            raise ValueError("DTB source must be a real directory: " + str(tree))
        for path in tree.rglob("*"):
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ValueError(
                    "DTB source contains a symlink/special file: " + str(path)
                )
            count += 1
            total += path.stat().st_size
            if count > 4000 or total > 192 * MIB:
                raise ValueError("DTB tree exceeds boot partition budget")
    destination = Path(boot) / "dtbs"
    if destination.is_symlink():
        raise ValueError("DTB destination cannot be a symlink")
    for tree in trees:
        shutil.copytree(tree, destination, dirs_exist_ok=True)


def validate_paths(work, output, rootfs, board, initramfs):
    work = Path(work).resolve()
    if work in (Path("/"), Path("/dev"), Path("/boot"), Path("/system"), Path("/data")):
        raise ValueError("Use a dedicated marked work directory")
    marker = regular(work / ".fascia-image-work")
    if marker.stat().st_size > 1024:
        raise ValueError("Invalid work-directory marker")
    output = Path(output)
    if (
        output.exists()
        or output.is_symlink()
        or output.with_suffix(output.suffix + ".json").exists()
    ):
        raise ValueError("Output must be a NEW regular file")
    output = output.parent.resolve() / output.name
    if not output.is_relative_to(work):
        raise ValueError("Output must be inside marked work directory")
    rootfs = Path(rootfs).resolve()
    board = Path(board).resolve()
    initramfs = regular(initramfs).resolve()
    if not rootfs.is_dir() or not board.is_dir():
        raise ValueError("Missing prepared rootfs/board directory")
    if output.is_relative_to(rootfs) or output.is_relative_to(board):
        raise ValueError("Output overlaps its input")
    regular(rootfs / "boot/zImage")
    regular(rootfs / "etc/fascia-system.json")
    identity = json.loads((rootfs / "etc/fascia-system.json").read_text())
    if identity.get("board_id") != BOARD or identity.get("shared_state_schema") != 1:
        raise ValueError("Rootfs board/state schema mismatch")
    if (rootfs / "etc/machine-id").exists() and (
        rootfs / "etc/machine-id"
    ).stat().st_size:
        raise ValueError("Prepared rootfs carries a machine identity")
    if list((rootfs / "etc/ssh").glob("ssh_host_*")):
        raise ValueError("Prepared rootfs carries SSH host identity")
    if any((rootfs / "var/lib/iwd").glob("*")):
        raise ValueError("Prepared rootfs carries Wi-Fi configuration")
    if not (rootfs / "usr/lib/modules" / KERNEL).is_dir():
        raise ValueError("Prepared rootfs lacks initial kernel modules")
    bootloader = regular(board / "boot/u-boot-sunxi-with-spl.bin")
    if not 0 < bootloader.stat().st_size <= 4 * MIB - 8192:
        raise ValueError("U-Boot exceeds its reserved pre-partition area")
    proof = json.loads(regular(board / "manifest.json").read_text())
    if proof.get("board_id") != BOARD or proof.get("kernel_release") != KERNEL:
        raise ValueError("Board artifact identity mismatch")
    for name in (
        "boot/u-boot-sunxi-with-spl.bin",
        "boot/dtbs/pf1007l.dtb",
        "boot/dtbs/pf1007l-provision.dtb",
    ):
        file = regular(board / name)
        if proof.get("files", {}).get(name) != digest(file):
            raise ValueError("Board artifact checksum mismatch: " + name)
    return work, output, rootfs, board, initramfs


def reject_submounts(rootfs, mountinfo):
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            raise ValueError("Invalid mount table")
        decoded = re.sub(
            r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]
        )
        mounted = Path(decoded)
        if mounted != rootfs and mounted.is_relative_to(rootfs):
            raise ValueError("Prepared rootfs still has a live submount: " + decoded)


def run(*args):
    return subprocess.run(
        list(map(str, args)),
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    ).stdout.strip()


def verify_loop(loop, image):
    if not re.fullmatch("/dev/loop[0-9]+", loop):
        raise ValueError("Refusing a non-loop device")
    node = Path(loop).name
    backing = Path("/sys/block") / node / "loop/backing_file"
    text = backing.read_text().strip()
    if not text or Path(text).name != image.name:
        raise ValueError("Loop sysfs backing file is not this image")
    # Bind-mounted chroot paths can differ from the host's sysfs pathname. Compare
    # the kernel's actual backing inode+device as well, not just a path string.
    import fcntl

    with open(loop, "rb", buffering=0) as device:
        info = bytearray(232)
        fcntl.ioctl(device.fileno(), 0x4C05, info, True)  # LOOP_GET_STATUS64
    backing_device, backing_inode = struct.unpack_from("=QQ", info)
    actual = image.stat()
    if backing_device != actual.st_dev or backing_inode != actual.st_ino:
        raise ValueError("Loop kernel backing identity mismatch")
    return node


def partition_nodes(loop, image, workspace):
    node = verify_loop(loop, image)
    result = []
    for number in (1, 2):
        sysfs = Path("/sys/block") / node / (node + "p" + str(number))
        for _ in range(100):
            if (sysfs / "dev").exists():
                break
            time.sleep(0.05)
        if (sysfs / "partition").read_text().strip() != str(number):
            raise ValueError("Unexpected loop partition")
        major, minor = map(int, (sysfs / "dev").read_text().strip().split(":"))
        path = workspace / ("part" + str(number))
        os.mknod(path, stat.S_IFBLK | 0o600, os.makedev(major, minor))
        result.append(path)
    return result


def copy_tree(source, destination):
    run("cp", "-a", "--reflink=auto", str(source) + "/.", destination)


def data_directories(destination, rootfs, template):
    for name in DATA_DIRS:
        path = destination / name
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700 if name in ("identity", "iwd", "fascia-system") else 0o755)
    os.chown(destination / "home/fascia", 1000, 1000)
    os.chown(destination / "fascia", 1000, 1000)
    users = {
        line.split(":")[0]: line.split(":")
        for line in (rootfs / "etc/passwd").read_text().splitlines()
        if ":" in line
    }
    if "systemd-timesync" in users:
        account = users["systemd-timesync"]
        os.chown(destination / "systemd-timesync", int(account[2]), int(account[3]))
    if template:
        template = Path(template).resolve()
        allowed = {path.split("/")[0] for path in DATA_DIRS}
        if any(
            path.name not in allowed or path.is_symlink() for path in template.iterdir()
        ):
            raise ValueError("Unexpected persistent-data template entry")
        for private in ("identity", "iwd"):
            if (template / private).exists() and any((template / private).iterdir()):
                raise ValueError(
                    "Factory data must not contain device identity or Wi-Fi credentials"
                )
        if (template / "fascia-system").exists() and any(
            (template / "fascia-system").iterdir()
        ):
            raise ValueError("Assembler owns initial boot state")
        copy_tree(template, destination)
        # These modes and ownership are a device contract, not template preferences.
        os.chmod(destination / "identity", 0o700)
        os.chmod(destination / "iwd", 0o700)
        os.chmod(destination / "fascia-system", 0o700)
        for name in ("identity", "iwd", "fascia-system", "firmware"):
            os.chown(destination / name, 0, 0)
        os.chown(destination / "home/fascia", 1000, 1000)
        os.chown(destination / "fascia", 1000, 1000)
        if "systemd-timesync" in users:
            account = users["systemd-timesync"]
            os.chown(destination / "systemd-timesync", int(account[2]), int(account[3]))


def assemble(args):
    if os.geteuid() != 0 or os.uname().sysname != "Linux":
        raise ValueError("Assembly requires Linux root")
    work, output, rootfs, board, initramfs = validate_paths(
        args.work_root, args.output, args.rootfs, args.board, args.initramfs
    )
    layout(args.size_mib)
    reject_submounts(rootfs, Path("/proc/self/mountinfo").read_text())
    if not output.parent.is_dir():
        raise ValueError("Output parent must already exist")
    for command in (
        "losetup",
        "udevadm",
        "mkfs.vfat",
        "mkfs.btrfs",
        "mount",
        "umount",
        "btrfs",
        "mkimage",
        "cp",
        "sync",
    ):
        if not shutil.which(command):
            raise ValueError("Missing image tool: " + command)
    workspace = Path(tempfile.mkdtemp(prefix="assemble-", dir=work))
    loop = None
    mounts = []
    created = False
    complete = False
    fd = None
    try:
        fd = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Output is not a regular image")
        os.ftruncate(fd, args.size_mib * MIB)
        os.pwrite(fd, mbr(args.size_mib), 0)
        with (board / "boot/u-boot-sunxi-with-spl.bin").open("rb") as source:
            os.pwrite(fd, source.read(), 8192)
        os.fsync(fd)
        loop = run("losetup", "--find", "--show", "--partscan", output)
        verify_loop(loop, output)
        # Udev's partition probe may re-read the table after losetup returns.
        # Wait for those events before copying transient partition device IDs.
        run("udevadm", "settle", "--timeout=30")
        parts = partition_nodes(loop, output, workspace)
        verify_loop(loop, output)
        run("mkfs.vfat", "-F", "32", "-n", "FASCIABOOT", "-i", "46524d45", parts[0])
        verify_loop(loop, output)
        run("mkfs.btrfs", "-f", "-L", "FASCIASYS", "-m", "dup", parts[1])
        verify_loop(loop, output)
        targets = []
        for name, part, kind in zip(("boot", "system"), parts, ("vfat", "btrfs")):
            target = workspace / name
            target.mkdir()
            verify_loop(loop, output)
            run("mount", "-t", kind, part, target)
            mounts.append(target)
            targets.append(target)
        boot, system = targets
        data = system / "data"
        run("btrfs", "subvolume", "create", data)
        generations = system / "generations"
        generations.mkdir()
        stage = generations / ".factory-staging"
        run("btrfs", "subvolume", "create", stage)
        copy_tree(rootfs, stage)
        # Build-root markers never belong in the installed system generation.
        for marker in (".fascia-image-build-root", ".fascia-image-root"):
            (stage / marker).unlink(missing_ok=True)
        run("btrfs", "filesystem", "sync", system)
        run("btrfs", "subvolume", "snapshot", stage, generations / "factory-1")
        run("btrfs", "subvolume", "delete", stage)
        run("btrfs", "filesystem", "sync", system)
        data_directories(data, rootfs, args.data_template)
        helper = (
            Path(__file__).resolve().parent.parent / "system/fascia-system-update.py"
        )
        spec = importlib.util.spec_from_file_location("fascia_update", helper)
        update = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(update)
        update.Updater(generations, data / "fascia-system").initialize("factory-1")
        shutil.copy2(rootfs / "boot/zImage", boot / "zImage")
        shutil.copy2(initramfs, boot / "initramfs.img")
        copy_boot_dtbs(rootfs, board, boot)
        (boot / "extlinux").mkdir()
        config = """DEFAULT fascia
TIMEOUT 10
LABEL fascia
    LINUX /zImage
    INITRD /initramfs.img
    FDT /dtbs/pf1007l-provision.dtb
    APPEND root=PARTUUID=46524d45-02 rootwait rw quiet loglevel=0 logo.nologo vt.global_cursor_default=0 systemd.show_status=false rd.systemd.show_status=false systemd.gpt_auto=0 firmware_class.path=/run/fascia-firmware panic=20
"""
        (boot / "extlinux/extlinux.conf").write_text(config)
        script = workspace / "boot.cmd"
        script.write_text("""# Stable SD extlinux boot. No environment/FAT writes or automatic storage recovery.
if sysboot mmc 0:1 any ${pxefile_addr_r} /extlinux/extlinux.conf; then
    echo Fascia boot returned unexpectedly
fi
""")
        run(
            "mkimage",
            "-C",
            "none",
            "-A",
            "arm",
            "-T",
            "script",
            "-n",
            "Fascia SD extlinux",
            "-d",
            script,
            boot / "boot.scr",
        )
        while mounts:
            target = mounts[-1]
            run("sync", "-f", target)
            run("umount", target)
            mounts.pop()
        run("losetup", "--detach", loop)
        loop = None
        os.fsync(fd)
        report = {
            "schema_version": 1,
            "board_id": BOARD,
            "kernel_release": KERNEL,
            "image_bytes": args.size_mib * MIB,
            "image_sha256": digest(output),
            "mbr_signature": "46524d45",
            "storage_layout": "btrfs-shared-v1",
            "automatic_expansion": True,
            "partitions": [
                {"number": i + 1, "start_mib": v[0], "size_mib": v[1]}
                for i, v in enumerate(layout(args.size_mib))
            ],
            "factory_generation": "factory-1",
            "default_dtb": "pf1007l-provision.dtb",
            "kernel_sha256": digest(rootfs / "boot/zImage"),
            "initramfs_sha256": digest(initramfs),
        }
        sidecar = output.with_suffix(output.suffix + ".json")
        with sidecar.open("x") as file:
            json.dump(report, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        complete = True
        print("Assembled new SD image: " + str(output))
    finally:
        cleanup_failed = False
        for target in reversed(mounts):
            try:
                run("umount", target)
            except subprocess.SubprocessError:
                cleanup_failed = True
        if loop and not cleanup_failed:
            try:
                run("losetup", "--detach", loop)
            except subprocess.SubprocessError:
                cleanup_failed = True
        if fd is not None:
            os.close(fd)
        if cleanup_failed:
            raise RuntimeError(
                "Cleanup incomplete; kept workspace/image for manual cleanup: "
                + str(workspace)
            )
        shutil.rmtree(workspace)
        if created and not complete:
            output.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("work-root", "rootfs", "board", "initramfs", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--data-template", type=Path)
    parser.add_argument("--size-mib", type=int, default=8192)
    args = parser.parse_args()

    def interrupted(number, _frame):
        raise SystemExit(128 + number)

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        assemble(args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        detail = (
            error.stderr if isinstance(error, subprocess.CalledProcessError) else None
        )
        parser.exit(1, "Image assembly failed: " + str(error) + "\n" + (detail or ""))


if __name__ == "__main__":
    main()
