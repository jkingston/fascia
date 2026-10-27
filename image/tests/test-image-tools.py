"""Offline early-boot/initramfs tests. No device writes or root required."""

import importlib.util
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


init = load("build-initramfs")
assembler = load("assemble-image")


def elf(interpreter=None, machine=40):
    raw = bytearray(128)
    raw[:6] = b"\x7fELF\x01\x01"
    struct.pack_into("<H", raw, 18, machine)
    struct.pack_into("<I", raw, 28, 52)
    struct.pack_into("<HH", raw, 42, 32, 1)
    if interpreter:
        data = interpreter.encode() + b"\0"
        struct.pack_into("<I", raw, 52, 3)
        struct.pack_into("<I", raw, 56, 88)
        struct.pack_into("<I", raw, 68, len(data))
        raw[88 : 88 + len(data)] = data
    return bytes(raw)


def unpack_newc(raw):
    pos = 0
    records = {}
    while True:
        if raw[pos : pos + 6] != b"070701":
            raise AssertionError("bad CPIO magic/alignment")
        fields = [int(raw[pos + 6 + i * 8 : pos + 14 + i * 8], 16) for i in range(13)]
        pos += 110
        name = raw[pos : pos + fields[11] - 1].decode()
        pos = (pos + fields[11] + 3) & ~3
        body = raw[pos : pos + fields[6]]
        pos = (pos + fields[6] + 3) & ~3
        if name == "TRAILER!!!":
            return records
        records[name] = (fields, body)


class EarlyBoot(unittest.TestCase):
    def test_absolute_symlinks_stay_inside_target_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "usr/lib").mkdir(parents=True)
            (root / "lib").symlink_to("/usr/lib")
            (root / "usr/lib/loader").write_bytes(b"target")
            self.assertEqual(
                init.root_file(root, "/lib/loader"), (root / "usr/lib/loader").resolve()
            )
            self.assertEqual(
                init.root_file(root, "/lib/loader").read_bytes(), b"target"
            )

    def test_symlink_escape_loop_and_relative_input_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "escape").symlink_to("../../outside")
            (root / "loop").symlink_to("/loop")
            for path in ("/escape", "/loop", "usr/bin/tool"):
                with self.assertRaises(ValueError):
                    init.root_file(root, path)

    def test_static_arm_binary_needs_no_loader_and_foreign_elf_rejected(self):
        self.assertIsNone(init.elf_interpreter(elf()))
        self.assertEqual(
            init.elf_interpreter(elf("/lib/ld-linux-armhf.so.3")),
            "/lib/ld-linux-armhf.so.3",
        )
        for raw in (elf(machine=3), b"bad", elf()[:60]):
            with self.assertRaises(ValueError):
                init.elf_interpreter(raw)

    def test_elf_bounds_and_interpreter_termination_checked(self):
        raw = bytearray(elf("/lib/loader"))
        struct.pack_into("<I", raw, 28, 0xFFFFFFFF)
        with self.assertRaises(ValueError):
            init.elf_interpreter(raw)
        raw = bytearray(elf("/lib/loader"))
        struct.pack_into("<I", raw, 68, 0xFFFFFFFF)
        with self.assertRaises(ValueError):
            init.elf_interpreter(raw)

    def test_newc_alignment_symlink_and_console_device_survive(self):
        import stat

        entries = {
            "dev": (stat.S_IFDIR | 0o755, b""),
            "dev/console": (stat.S_IFCHR | 0o600, b"", 5, 1),
            "bin": (stat.S_IFDIR | 0o755, b""),
            "bin/tool": (stat.S_IFREG | 0o755, b"abc"),
            "bin/sh": (stat.S_IFLNK | 0o777, b"tool"),
        }
        raw = init.newc(entries)
        records = unpack_newc(raw)
        self.assertEqual(len(raw) % 512, 0)
        self.assertEqual(records["bin/tool"][1], b"abc")
        self.assertEqual(records["bin/sh"][1], b"tool")
        self.assertEqual(records["dev/console"][0][9:11], [5, 1])

    def test_cpio_cannot_escape_or_write_through_symlink(self):
        import stat

        for entries in (
            {"../escape": (stat.S_IFREG, b"x")},
            {"bin": (stat.S_IFLNK, b"/host"), "bin/file": (stat.S_IFREG, b"x")},
            {"missing/file": (stat.S_IFREG, b"x")},
        ):
            with self.assertRaises(ValueError):
                init.newc(entries)

    def test_static_busybox_build_never_calls_ldd_and_has_console(self):
        import gzip

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            (root / "usr/bin").mkdir(parents=True)
            (root / ".fascia-image-build-root").write_text("isolated")
            (root / "usr/bin/busybox").write_bytes(elf())
            (root / "usr/bin/btrfs").write_bytes(elf())
            output = Path(directory) / "initramfs.img"

            def command(*args):
                if args[0] == "chroot" and args[-1] == "--list":
                    return "\n".join(init.APPLETS)
                if args[0] == "modprobe":
                    return "builtin module"
                raise AssertionError(
                    "Unexpected command, especially ldd for static ELF: " + str(args)
                )

            with (
                mock.patch.object(init, "run", side_effect=command),
                mock.patch.object(
                    init.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, stdout="sync -f", stderr=""
                    ),
                ),
            ):
                (root / "usr/local/libexec").mkdir(parents=True)
                (root / "usr/local/libexec/fascia-boot-progress").write_bytes(
                    (root / "usr/bin/busybox").read_bytes()
                )
                init.build(root, output, "7.2.10-1-armv7-ARCH")
            records = unpack_newc(gzip.decompress(output.read_bytes()))
            self.assertIn("bin/busybox", records)
            self.assertEqual(records["dev/console"][0][9:11], [5, 1])
            self.assertIn("usr/lib/fascia/fascia-boot-select", records)
        self.assertIn("usr/lib/fascia/fascia-boot-progress", records)

    def test_storage_modules_asserted_and_touch_not_loaded_early(self):
        self.assertTrue({"btrfs", "mmc_block", "sunxi_mmc"} <= set(init.MODULES))
        self.assertNotIn("silead", init.MODULES)
        self.assertTrue(
            {"panel-jadard-jd9365da-h3", "sun4i_drm", "sun6i_mipi_dsi"}
            <= set(init.MODULES)
        )

    def test_sd_selection_rejects_ambiguous_or_mmc_cards(self):
        source = (TOOLS.parent / "initramfs/init").read_text()
        function = source.split("find_sd() {", 1)[1].split("\ni=0", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sys").mkdir()
            (root / "dev").mkdir()

            def card(name, kind):
                path = root / "sys" / name / "device"
                path.mkdir(parents=True)
                (path / "type").write_text(kind + "\n")
                (root / "dev" / name).write_bytes(assembler.mbr(8192))

            card("mmcblk0", "MMC")
            card("mmcblk1", "SD")
            script = (
                (
                    "signature=46524d45\nfind_sd() {"
                    + function
                    + '\nfind_sd; result=$?; printf "%s:%s" "$result" "$disk"'
                )
                .replace("/sys/block", str(root / "sys"))
                .replace("/dev/", str(root / "dev") + "/")
            )
            result = subprocess.run(
                ["sh", "-c", script], capture_output=True, text=True
            )
            self.assertEqual(result.stdout, "0:mmcblk1")
            card("mmcblk2", "SD")
            result = subprocess.run(
                ["sh", "-c", script], capture_output=True, text=True
            )
            self.assertEqual(result.stdout, "2:")


if __name__ == "__main__":
    unittest.main()
