#!/usr/bin/env python3
"""Build a small early USB/recovery image from the pinned ARM runtime root."""

import argparse
import gzip
import os
from pathlib import Path
import re
import struct
import stat
import subprocess

PROJECT = Path(__file__).resolve().parents[2]
MODULES = [
    "btrfs",
    "mmc_block",
    "sunxi_mmc",
    "sunxi_wdt",
    "nls_cp437",
    "nls_ascii",
    "vfat",
    "libcomposite",
    "u_serial",
    "usb_f_acm",
    "u_ether",
    "usb_f_ncm",
    "pwm_sun4i",
    "panel-jadard-jd9365da-h3",
    "sun4i_tcon",
    "sun4i_frontend",
    "sun4i_backend",
    "sun6i_mipi_dsi",
    "sun4i_drm",
]
APPLETS = "sh mount umount mkdir cat sleep setsid insmod ls head echo ln cut blockdev dd od tr sed wc grep cp chmod chown sync mktemp mv rm flock tail sha256sum cmp switch_root reboot poweroff dmesg ip".split()


def run(*args):
    return subprocess.check_output(list(map(str, args)), text=True).strip()


def root_file(root, name):
    """Resolve absolute symlinks within the target root, never on the build host."""
    root = root.resolve()
    if not name.startswith("/") or "\x00" in name:
        raise ValueError("Expected an absolute target-root path")
    parts = list(Path(name).parts[1:])
    current = root
    links = 0
    while parts:
        part = parts.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if current == root:
                raise ValueError("Escaping root symlink")
            current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            links += 1
            if links > 32:
                raise ValueError("Symlink loop")
            link = os.readlink(current)
            current = root if link.startswith("/") else current.parent
            parts = (
                list(Path(link).parts[1:] if link.startswith("/") else Path(link).parts)
                + parts
            )
    if not current.is_relative_to(root):
        raise ValueError("Path outside target root")
    return current


def elf_interpreter(data):
    if (
        len(data) < 52
        or data[:6] != b"\x7fELF\x01\x01"
        or struct.unpack_from("<H", data, 18)[0] != 40
    ):
        raise ValueError("Expected ARM ELF32 little-endian binary")
    phoff = struct.unpack_from("<I", data, 28)[0]
    phsize, count = struct.unpack_from("<HH", data, 42)
    if phsize < 32 or count > 1024 or phoff + phsize * count > len(data):
        raise ValueError("Invalid ELF program-header table")
    interpreter = None
    for index in range(count):
        position = phoff + index * phsize
        if struct.unpack_from("<I", data, position)[0] != 3:
            continue
        offset = struct.unpack_from("<I", data, position + 4)[0]
        size = struct.unpack_from("<I", data, position + 16)[0]
        if interpreter is not None or size < 2 or offset + size > len(data):
            raise ValueError("Invalid ELF interpreter")
        text = data[offset : offset + size]
        if text[-1:] != b"\0" or b"\0" in text[:-1] or not text.startswith(b"/"):
            raise ValueError("Invalid ELF interpreter path")
        interpreter = text[:-1].decode("utf-8")
    return interpreter


def newc(entries):
    output = bytearray()
    for name, record in entries.items():
        if (
            not name
            or name == "TRAILER!!!"
            or name.startswith("/")
            or ".." in Path(name).parts
            or "\x00" in name
            or len(name.encode()) > 4096
        ):
            raise ValueError("Unsafe CPIO entry")
        for parent in Path(name).parents:
            if str(parent) != "." and (
                parent.as_posix() not in entries
                or stat.S_IFMT(entries[parent.as_posix()][0]) != stat.S_IFDIR
            ):
                raise ValueError("CPIO entry traverses a non-directory/missing parent")
        if stat.S_IFMT(record[0]) not in (
            stat.S_IFDIR,
            stat.S_IFREG,
            stat.S_IFLNK,
            stat.S_IFCHR,
        ):
            raise ValueError("Unsupported CPIO entry type")
    records = list(entries.items()) + [("TRAILER!!!", (0, b""))]
    for inode, (name, record) in enumerate(records, 1):
        mode, body = record[:2]
        major, minor = record[2:] if len(record) == 4 else (0, 0)
        encoded = name.encode() + b"\0"
        fields = [
            inode,
            mode,
            0,
            0,
            1,
            0,
            len(body),
            0,
            0,
            major,
            minor,
            len(encoded),
            0,
        ]
        if any(not 0 <= value <= 0xFFFFFFFF for value in fields):
            raise ValueError("CPIO integer overflow")
        output += (
            b"070701" + "".join(f"{value:08x}" for value in fields).encode() + encoded
        )
        output += b"\0" * (-len(output) % 4)
        output += body
        output += b"\0" * (-len(output) % 4)
    output += b"\0" * (-len(output) % 512)
    return bytes(output)


def build(root, output, kernel, runtime=False):
    if runtime and (root != Path("/") or os.geteuid() != 0):
        raise ValueError("Runtime initramfs builds require the real root as root")
    if not runtime and not (root / ".fascia-image-build-root").is_file():
        raise ValueError("Expected an isolated prepared runtime root")
    if output.exists():
        raise ValueError("Output already exists")
    available = set(run("chroot", root, "/usr/bin/busybox", "--list").splitlines())
    if not set(APPLETS) <= available:
        raise ValueError("BusyBox missing applets: " + str(set(APPLETS) - available))
    sync_help = subprocess.run(
        ["chroot", str(root), "/usr/bin/busybox", "sync", "--help"],
        capture_output=True,
        text=True,
    )
    if "-f" not in sync_help.stderr + sync_help.stdout:
        raise ValueError("BusyBox sync must support -f")
    entries = {}

    def add(name, data, mode=0o100755):
        name = name.lstrip("/")
        for parent in reversed(Path(name).parents):
            if str(parent) != ".":
                entries.setdefault(str(parent), (0o040755, b""))
        record = (mode, data)
        if name in entries and entries[name] != record:
            raise ValueError("Conflicting initramfs entry: " + name)
        entries[name] = record

    def binary(name, destination):
        data = root_file(root, name).read_bytes()
        add(destination, data)
        interpreter = elf_interpreter(data)
        if interpreter is None:
            return  # Static BusyBox: no ldd, dynamic loader or shared libraries.
        add(interpreter, root_file(root, interpreter).read_bytes())
        dependencies = run("chroot", root, "/usr/bin/ldd", name)
        for line in dependencies.splitlines():
            if "not found" in line:
                raise ValueError("Missing binary dependency: " + line)
            match = re.search(r"(/[^\s()]+)", line)
            if match:
                path = match[1]
                add(path, root_file(root, path).read_bytes())

    binary("/usr/bin/busybox", "/bin/busybox")
    binary("/usr/bin/btrfs", "/bin/btrfs")
    binary(
        "/usr/local/libexec/fascia-boot-progress",
        "/usr/lib/fascia/fascia-boot-progress",
    )
    for applet in APPLETS:
        add("/bin/" + applet, b"busybox", 0o120777)
    for directory in [
        "proc",
        "sys",
        "dev",
        "run",
        "new_root",
        "system",
        "data",
        "tmp",
        "sbin",
        "usr/bin",
        "usr/sbin",
    ]:
        entries.setdefault(directory, (0o040755, b""))
    add("dev/console", b"", stat.S_IFCHR | 0o600)
    entries["dev/console"] = (stat.S_IFCHR | 0o600, b"", 5, 1)
    entries["dev/null"] = (stat.S_IFCHR | 0o666, b"", 1, 3)
    module_paths = []
    for module in MODULES:
        dependencies = run(
            "modprobe", "-d", root, "-S", kernel, "--show-depends", module
        )
        for line in dependencies.splitlines():
            if not line.startswith("insmod "):
                continue
            source = Path(line.split()[1])
            if not source.resolve().is_relative_to(root.resolve()):
                raise ValueError("Module dependency escaped prepared root")
            if source not in module_paths:
                module_paths.append(source)
    listing = []
    for source in module_paths:
        data = source.read_bytes()
        name = source.name
        if name.endswith(".zst"):
            data = subprocess.check_output(["zstd", "-dc", str(source)])
            name = name[:-4]
        elif name.endswith(".xz"):
            import lzma

            data = lzma.decompress(data)
            name = name[:-3]
        if name.startswith("silead"):
            raise ValueError("Touch firmware must be provisioned before driver loading")
        add("/lib/modules/" + name, data)
        listing.append(name)
    add(
        "/lib/modules/fascia-modules.list",
        ("\n".join(listing) + "\n").encode(),
        0o100644,
    )
    for name in ["init", "fascia-gadget", "fascia-emmc-readonly"]:
        source = (
            Path("/usr/share/fascia/initramfs")
            if runtime
            else PROJECT / "image/initramfs"
        )
        add(
            "/init" if name == "init" else "/usr/lib/fascia/" + name,
            (source / name).read_bytes(),
        )
    add(
        "/usr/lib/fascia/fascia-boot-select",
        (
            Path("/usr/share/fascia/fascia-boot-select.sh")
            if runtime
            else PROJECT / "image/system/fascia-boot-select.sh"
        ).read_bytes(),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as file:
        file.write(gzip.compress(newc(entries), compresslevel=9, mtime=0))
    print(f"Built {output}: {len(entries)} records, {len(listing)} early modules")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--kernel", default="7.2.10-1-armv7-ARCH")
    parser.add_argument(
        "--runtime", action="store_true", help="Build on the running Fascia"
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Build inside the Linux builder as root")
    build(args.root.resolve(), args.output, args.kernel, args.runtime)


if __name__ == "__main__":
    main()
