"""Offline boot-state and pacman recovery tests; Btrfs commands are mocked."""

import importlib.util
import os
import stat
from types import SimpleNamespace
from unittest import mock
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "update", Path(__file__).with_name("fascia-system-update.py")
)
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class MockBtrfs:
    def __init__(self):
        self.fail = None
        self.read_only = set()

    def __call__(self, args):
        if self.fail and self.fail in args:
            raise subprocess.CalledProcessError(1, args)
        if args[1:3] == ["property", "get"]:
            return "ro=true\n" if args[-2] in self.read_only else "ro=false\n"
        if args[1:3] == ["subvolume", "create"]:
            Path(args[-1]).mkdir()
        elif args[1:3] == ["subvolume", "snapshot"]:
            shutil.copytree(args[-2], args[-1], symlinks=True)
            if "-r" in args:
                self.read_only.add(args[-1])
        elif args[1:3] == ["subvolume", "delete"]:
            shutil.rmtree(args[-1])
        return ""


class BootState(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.state = dict(
            sequence=0,
            good="factory-1",
            candidate="-",
            attempts=0,
            selected="factory-1",
            schema=1,
        )

    def test_checksum_and_torn_copy(self):
        u.write_state(self.data, self.state)
        (self.data / "boot-b.state").write_bytes(b"torn")
        self.assertEqual(u.read_state(self.data), self.state)
        (self.data / "boot-a.state").write_bytes(b"also torn")
        with self.assertRaises(ValueError):
            u.read_state(self.data)

    def test_conflicting_same_sequence_is_rejected(self):
        u.write_state(self.data, self.state)
        (self.data / "boot-b.state").write_bytes(
            u.encode(dict(self.state, good="other", selected="other"))
        )
        with self.assertRaises(ValueError):
            u.read_state(self.data)

    def test_writable_generation_initialize_select_confirm(self):
        systems = self.data / "generations"
        systems.mkdir()
        (systems / "factory-1").mkdir()
        tool = u.Updater(systems, self.data, MockBtrfs())
        tool.initialize("factory-1")
        with self.assertRaises(ValueError):
            tool.confirm("factory-1")
        self.assertEqual(tool.select(), "factory-1")
        tool.confirm("factory-1")
        self.assertEqual(u.read_state(self.data)["good"], "factory-1")

    def test_generation_traversal_and_corruption_rejected(self):
        for name in ("../escape", "", "/root", "has space"):
            with self.assertRaises(ValueError):
                u.generation(name)
        raw = bytearray(u.encode(self.state))
        raw[20] ^= 1
        with self.assertRaises(ValueError):
            u.decode(bytes(raw))

    def test_presentation_is_current_boot_bounded_and_has_real_process_ids(self):
        boot = "12345678-1234-1234-1234-123456789abc"
        self.assertEqual(
            u.parse_presentation((boot + " 123 124 5000\n").encode(), boot, 6000),
            (123, 124),
        )
        for suffix in (
            "123 124 9000",
            "0 124 5000",
            "123 -1 5000",
            "123 124 5000 extra",
        ):
            with self.assertRaises(ValueError):
                u.parse_presentation((boot + " " + suffix).encode(), boot, 6000)
        with self.assertRaises(ValueError):
            u.parse_presentation((boot + " 123 124 5000").encode(), "other-boot", 6000)

    def test_presentation_accepts_integrated_host_or_exact_legacy_pair(self):
        boot = "12345678-1234-1234-1234-123456789abc"
        for compositor, home, executables, valid in (
            (123, 123, {123: "fascia-host"}, True),
            (123, 124, {123: "fascia-compositor", 124: "fascia-home"}, True),
            (123, 123, {123: "fascia-home"}, False),
            (123, 124, {123: "fascia-host", 124: "fascia-home"}, False),
        ):

            def read(path, _limit):
                if path == "/run/fascia-home/presented":
                    return f"{boot} {compositor} {home} 5000\n".encode()
                if path.endswith("boot_id"):
                    return boot.encode()
                return b"0::/system.slice/pf-kiosk.service\n"

            with (
                self.subTest(compositor=compositor, home=home, executables=executables),
                mock.patch.object(u, "bounded_read", side_effect=read),
                mock.patch.object(u.time, "monotonic_ns", return_value=6000000000),
                mock.patch.object(u.os, "kill"),
                mock.patch.object(
                    u.os,
                    "readlink",
                    side_effect=lambda path: "/usr/bin/"
                    + executables[int(path.split("/")[2])],
                ),
            ):
                if valid:
                    self.assertEqual(u.validate_presentation(), home)
                else:
                    with self.assertRaises(ValueError):
                        u.validate_presentation()

    def test_health_confirmation_has_no_network_dependency(self):
        commands = self.data / "health-commands"
        commands.mkdir()
        runtime = self.data / "generation"
        runtime.write_text("next\n")
        log = self.data / "confirmed"
        systemctl = commands / "systemctl"
        systemctl.write_text("#!/bin/sh\nexit 0\n")
        systemctl.chmod(0o755)
        confirm = commands / "confirm"
        confirm.write_text('#!/bin/sh\nprintf "%s %s" "$1" "$2" > "' + str(log) + '"\n')
        confirm.chmod(0o755)
        source = (
            Path(__file__)
            .with_name("fascia-system-health.sh")
            .read_text()
            .replace("/run/fascia-system/generation", str(runtime))
            .replace("/usr/local/libexec/fascia-system-update", str(confirm))
        )
        env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"])
        result = subprocess.run(
            ["sh", "-c", source], env=env, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text(), "health ")
        log.unlink()
        systemctl.write_text("#!/bin/sh\nexit 1\n")
        result = subprocess.run(
            ["sh", "-c", source], env=env, capture_output=True, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(log.exists())


class StorageGuard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.disk = self.base / "devices/mmcblk0"
        (self.disk / "device").mkdir(parents=True)
        (self.disk / "device/type").write_text("SD")
        part = self.disk / "mmcblk0p1"
        part.mkdir()
        (part / "partition").write_text("1")
        (part / "dev").write_text("179:1")
        self.sysfs = self.base / "block"
        self.sysfs.mkdir()
        (self.sysfs / "mmcblk0").symlink_to(self.disk)
        (self.sysfs / "mmcblk0p1").symlink_to(part)
        spec = importlib.util.spec_from_file_location(
            "growth", Path(__file__).resolve().parents[1] / "fascia-grow-storage.py"
        )
        growth = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(growth)
        self.target = SimpleNamespace(disk=Path("/dev/mmcblk0"))
        self.growth = SimpleNamespace(
            discover=mock.Mock(return_value=self.target),
            mount_record=growth.mount_record,
        )
        self.record = "10 1 179:1 / /boot rw - vfat /dev/mmcblk0p1 rw\n"
        self.block = lambda p: SimpleNamespace(
            st_mode=stat.S_IFBLK, st_rdev=os.makedev(179, 1)
        )

    def check(self, record=None, block=None):
        return u.validate_storage(
            self.growth,
            self.record if record is None else record,
            self.sysfs,
            self.block if block is None else block,
        )

    def test_exact_matching_sd_boot_partition_passes(self):
        self.assertIs(self.check(), self.target)
        self.growth.discover.assert_called_once_with()

    def test_wrong_source_filesystem_readonly_or_bind_mount_rejected(self):
        for old, new in (
            ("/dev/mmcblk0p1", "/dev/mmcblk1p1"),
            ("/dev/mmcblk0p1", "/dev/sda1"),
            ("/dev/mmcblk0p1", "/dev/mmcblk0p2"),
            ("vfat", "ext4"),
            ("/boot rw", "/boot ro"),
            ("179:1 / /boot", "179:1 /subdir /boot"),
        ):
            with self.subTest(new=new), self.assertRaises(ValueError):
                self.check(self.record.replace(old, new))

    def test_missing_duplicate_or_malformed_boot_mount_rejected(self):
        for record in ("", self.record * 2, "bad mountinfo"):
            with self.subTest(record=record), self.assertRaises(ValueError):
                self.check(record)

    def test_mmc_and_wrong_block_node_rejected(self):
        (self.disk / "device/type").write_text("MMC")
        with self.assertRaises(ValueError):
            self.check()
        (self.disk / "device/type").write_text("SD")
        with self.assertRaises(ValueError):
            self.check(
                block=lambda p: SimpleNamespace(
                    st_mode=stat.S_IFREG, st_rdev=os.makedev(179, 1)
                )
            )
        with self.assertRaises(ValueError):
            self.check(
                block=lambda p: SimpleNamespace(
                    st_mode=stat.S_IFBLK, st_rdev=os.makedev(179, 9)
                )
            )

    def test_shared_root_guard_failure_aborts_before_boot_inspection(self):
        self.growth.discover.side_effect = ValueError(
            "Root/data/system are not the same SD"
        )
        inspect = mock.Mock()
        with self.assertRaises(ValueError):
            self.check(block=inspect)
        inspect.assert_not_called()


class PacmanRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.systems = self.path / "generations"
        self.systems.mkdir()
        self.root = self.systems / "factory-1"
        self.root.mkdir()
        (self.root / "package-version").write_text("old-kernel-modules")
        (self.root / "var/lib/pacman").mkdir(parents=True)
        (self.root / "var/lib/pacman/db.lck").write_text("transaction lock")
        self.data = self.path / "data"
        self.data.mkdir()
        self.boot = self.path / "boot"
        (self.boot / "extlinux").mkdir(parents=True)
        for name in ("zImage", "initramfs.img", "extlinux/extlinux.conf"):
            (self.boot / name).write_bytes(("old-" + name).encode())
        self.command = MockBtrfs()
        u.write_state(
            self.data,
            dict(
                sequence=0,
                good="factory-1",
                candidate="-",
                attempts=0,
                selected="factory-1",
                schema=1,
            ),
        )
        self.tool = u.Recovery(
            self.systems,
            self.data,
            self.boot,
            self.root,
            self.command,
            running_generation=lambda: "factory-1",
        )

    def test_pacman_checkpoint_pairs_root_and_boot_then_writable_rollback(self):
        checkpoint = self.tool.snapshot()
        self.assertIn(str(self.systems / checkpoint), self.command.read_only)
        (self.root / "package-version").write_text("new-kernel-modules")
        (self.boot / "zImage").write_bytes(b"new-kernel")
        self.tool.commit()
        restored = self.tool.rollback()
        self.assertEqual(
            (self.systems / restored / "package-version").read_text(),
            "old-kernel-modules",
        )
        self.assertNotIn(str(self.systems / restored), self.command.read_only)
        self.assertFalse((self.systems / restored / "var/lib/pacman/db.lck").exists())
        self.assertTrue((self.root / "var/lib/pacman/db.lck").exists())
        self.assertEqual((self.boot / "zImage").read_bytes(), b"old-zImage")
        self.assertEqual(u.read_state(self.data)["good"], restored)
        self.assertTrue((self.systems / checkpoint).is_dir())

    def test_selected_rollback_requires_reboot_before_next_snapshot(self):
        checkpoint = self.tool.snapshot()
        restored = self.tool.rollback()
        self.assertNotEqual(restored, "factory-1")
        before = (self.data / "recovery-current.json").read_bytes()
        count = len(list(self.systems.iterdir()))
        with self.assertRaisesRegex(ValueError, "Reboot"):
            self.tool.snapshot()
        self.assertEqual((self.data / "recovery-current.json").read_bytes(), before)
        self.assertEqual(len(list(self.systems.iterdir())), count)
        self.assertEqual(self.tool.current()[1]["checkpoint"], checkpoint)
        self.tool.running_generation = lambda: restored
        self.assertTrue(self.tool.snapshot().startswith("recovery-"))

    def test_actual_mount_record_not_runtime_marker_selects_generation(self):
        valid = "10 1 179:2 /generations/factory-1 / rw - btrfs /dev/mmcblk0p2 rw\n"
        self.assertEqual(u.mounted_generation(valid), "factory-1")
        for value in (
            "",
            valid * 2,
            valid.replace("/generations/factory-1", "/data"),
            valid.replace(" / rw ", " / ro "),
            valid.replace("btrfs", "ext4"),
            "malformed",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                u.mounted_generation(value)

    def test_corrupt_backup_never_selects_or_restores(self):
        checkpoint = self.tool.snapshot()
        folder, _ = self.tool.current()
        (folder / "boot/zImage").write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            self.tool.rollback()
        self.assertEqual(u.read_state(self.data)["good"], "factory-1")
        self.assertEqual((self.boot / "zImage").read_bytes(), b"old-zImage")
        self.assertTrue((self.systems / checkpoint).is_dir())

    def test_snapshot_failure_does_not_publish_checkpoint(self):
        self.command.fail = "snapshot"
        with self.assertRaises(subprocess.CalledProcessError):
            self.tool.snapshot()
        self.assertFalse((self.data / "recovery-current.json").exists())
        self.assertEqual(u.read_state(self.data)["good"], "factory-1")

    def test_symlink_or_missing_boot_file_aborts_before_snapshot(self):
        (self.boot / "zImage").unlink()
        (self.boot / "zImage").symlink_to(self.root / "package-version")
        with self.assertRaises(ValueError):
            self.tool.snapshot()
        self.assertFalse((self.data / "recovery-current.json").exists())


if __name__ == "__main__":
    unittest.main()
