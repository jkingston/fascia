"""Offline image layout/path guard tests; never opens a device or requires root."""

import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "assemble", Path(__file__).with_name("assemble-image.py")
)
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class ImageGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        (self.work / ".fascia-image-work").write_text("test\n")
        self.root = self.work / "root"
        self.board = self.work / "board"
        self.init = self.work / "initramfs"
        self.init.write_bytes(b"init")
        for path in ("boot", "etc/ssh", "usr/lib/modules/" + a.KERNEL):
            (self.root / path).mkdir(parents=True, exist_ok=True)
        (self.root / "boot/zImage").write_bytes(b"kernel")
        (self.root / "etc/fascia-system.json").write_text(
            json.dumps(dict(board_id=a.BOARD, shared_state_schema=1))
        )
        files = {}
        for path in (
            "boot/u-boot-sunxi-with-spl.bin",
            "boot/dtbs/pf1007l.dtb",
            "boot/dtbs/pf1007l-provision.dtb",
        ):
            file = self.board / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"board-artifact")
            files[path] = a.digest(file)
        (self.board / "manifest.json").write_text(
            json.dumps(dict(board_id=a.BOARD, kernel_release=a.KERNEL, files=files))
        )
        self.output = self.work / "new.img"

    def check(self):
        return a.validate_paths(
            self.work, self.output, self.root, self.board, self.init
        )

    def test_stock_dtbs_preserved_and_reviewed_board_override_wins(self):
        stock = self.root / "boot/dtbs"
        stock.mkdir()
        (stock / "stock-board.dtb").write_bytes(b"stock")
        (stock / "pf1007l-provision.dtb").write_bytes(b"generic collision")
        target = self.work / "fat"
        target.mkdir()
        a.copy_boot_dtbs(self.root, self.board, target)
        self.assertEqual((target / "dtbs/stock-board.dtb").read_bytes(), b"stock")
        self.assertEqual(
            (target / "dtbs/pf1007l-provision.dtb").read_bytes(), b"board-artifact"
        )
        self.assertEqual(
            (stock / "pf1007l-provision.dtb").read_bytes(), b"generic collision"
        )

    def test_dtb_symlink_escape_refused_before_destination_created(self):
        stock = self.root / "boot/dtbs"
        stock.mkdir()
        (stock / "escape.dtb").symlink_to(self.init)
        target = self.work / "fat"
        target.mkdir()
        with self.assertRaisesRegex(ValueError, "symlink/special"):
            a.copy_boot_dtbs(self.root, self.board, target)
        self.assertFalse((target / "dtbs").exists())

    def test_valid_new_file_inputs(self):
        self.assertEqual(self.check()[1], self.output.resolve())

    def test_existing_output_and_symlink_rejected(self):
        self.output.write_bytes(b"keep")
        with self.assertRaises(ValueError):
            self.check()
        self.assertEqual(self.output.read_bytes(), b"keep")
        self.output.unlink()
        self.output.symlink_to(self.work / "missing")
        with self.assertRaises(ValueError):
            self.check()

    def test_output_outside_marker_and_unmarked_work_rejected(self):
        original = self.output
        self.output = self.work.parent / ("outside-" + self.work.name + ".img")
        with self.assertRaises(ValueError):
            self.check()
        self.output = original
        (self.work / ".fascia-image-work").unlink()
        with self.assertRaises(FileNotFoundError):
            self.check()

    def test_fixed_layout_and_mbr_signature(self):
        data = a.mbr(8192)
        self.assertEqual(data[510:], b"\x55\xaa")
        self.assertEqual(struct.unpack_from("<I", data, 440)[0], 0x46524D45)
        parts = []
        for i in range(2):
            entry = struct.unpack_from("<B3sB3sII", data, 446 + i * 16)
            parts.append(entry)
        self.assertEqual(parts[0][4:], (8192, 524288))
        self.assertEqual(parts[1][4:], (532480, 16244736))
        self.assertEqual(parts[1][4] + parts[1][5], 8192 * 2048)
        self.assertEqual(data[478:510], bytes(32))
        self.assertEqual(parts[0][0], 0x80)
        with self.assertRaises(ValueError):
            a.layout(4096)

    def test_uboot_cannot_overlap_partition(self):
        with (self.board / "boot/u-boot-sunxi-with-spl.bin").open("wb") as file:
            file.truncate(4 * a.MIB - 8192 + 1)
        with self.assertRaises(ValueError):
            self.check()

    def test_private_identity_and_wrong_board_rejected(self):
        (self.root / "etc/machine-id").write_text("private")
        with self.assertRaises(ValueError):
            self.check()
        (self.root / "etc/machine-id").unlink()
        (self.root / "etc/ssh/ssh_host_ed25519_key").write_text("private")
        with self.assertRaises(ValueError):
            self.check()

    def test_board_artifact_tampering_rejected(self):
        (self.board / "boot/dtbs/pf1007l.dtb").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.check()

    def test_live_rootfs_submount_is_rejected(self):
        mountinfo = (
            "1 0 8:1 / / rw - ext4 /dev/sda rw\n2 1 0:5 / "
            + str(self.root)
            + "/dev rw - devtmpfs devtmpfs rw\n"
        )
        with self.assertRaises(ValueError):
            a.reject_submounts(self.root, mountinfo)
        a.reject_submounts(self.root, "1 0 8:1 / / rw - ext4 /dev/sda rw\n")

    def test_non_loop_device_rejected_before_sysfs_read(self):
        for name in (
            "/dev/mmcblk0",
            "/dev/sda",
            "/dev/loop0p1",
            "/dev/loop0/../mmcblk0",
        ):
            with self.assertRaises(ValueError):
                a.verify_loop(name, self.output)


if __name__ == "__main__":
    unittest.main()
