#!/usr/bin/env python3
"""Synthetic ELF/block metadata only: no device reads and no vendor blobs."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fascia_identity as identity
from extract_gsl import extract, MAX_MODULE


def module(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), Path(__file__).with_name(name + ".py")
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


provision = module("fascia-provision")
generator = module("fascia-identity-generator")


def synthetic_elf():
    names = b"\0.shstrtab\0.strtab\0.symtab\0.data\0.rel.data\0"
    strings = b"\0gslx680_fw_grp\0"
    fw = struct.pack("<II", 0xF0, 2) + b"".join(
        struct.pack("<II", i * 4, 0x12340000 + i) for i in range(32)
    )
    data = struct.pack("<III", 12, 33, 32) + b"gsl_m86_hd\0"
    data = data.ljust(32, b"\0") + fw
    symbols = (
        bytes(16)
        + struct.pack("<IIIBBH", 0, 0, 0, 3, 0, 4)
        + struct.pack("<IIIBBH", 1, 0, 12, 0x11, 0, 4)
    )
    relocations = struct.pack("<IIII", 0, 1 << 8 | 2, 8, 1 << 8 | 2)
    sections = [None, names, strings, symbols, data, relocations]
    output = bytearray(52)
    headers = [(0,) * 10]
    for i, part in enumerate(sections[1:], 1):
        offset = len(output)
        output.extend(part)
        name = [None, b".shstrtab", b".strtab", b".symtab", b".data", b".rel.data"][i]
        headers.append(
            (
                names.index(name),
                [0, 3, 3, 2, 1, 9][i],
                0,
                0,
                offset,
                len(part),
                2 if i == 3 else 3 if i == 5 else 0,
                4 if i == 5 else 0,
                4,
                16 if i == 3 else 8 if i == 5 else 0,
            )
        )
    shoff = len(output)
    output.extend(b"".join(struct.pack("<10I", *header) for header in headers))
    output[:52] = struct.pack(
        "<16sHHIIIIIHHHHHH",
        b"\x7fELF\x01\x01\x01".ljust(16, b"\0"),
        1,
        40,
        1,
        0,
        0,
        shoff,
        0,
        52,
        0,
        0,
        40,
        len(headers),
        1,
    )
    return bytes(output), fw


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_synthetic_relocation_extraction_and_truncation_bounds(self):
        data, expected = synthetic_elf()
        self.assertEqual(extract(data), expected)
        for length in [0, 12, 51, len(data) - 1]:
            with self.assertRaises((ValueError, struct.error)):
                extract(data[:length])
        with self.assertRaises(ValueError):
            extract(b"x" * (MAX_MODULE + 1))
        broken = bytearray(data)
        fw = broken.index(expected)
        struct.pack_into("<I", broken, fw + 8, 5)
        with self.assertRaisesRegex(ValueError, "register sequence"):
            extract(bytes(broken))

    def test_discovery_uses_type_controller_and_partition_not_index(self):
        sysfs = self.root / "class/block"
        sysfs.mkdir(parents=True)
        controller = self.root / "devices/1c11000.mmc/mmc_host/mmc7/mmc7:0001"
        controller.mkdir(parents=True)
        (controller / "type").write_text("MMC\n")
        disk = controller / "block/mmcblk7"
        (disk / "mmcblk7p7").mkdir(parents=True)
        (disk / "device").symlink_to(controller)
        (sysfs / "mmcblk7").symlink_to(disk)
        (sysfs / "mmcblk7p7").symlink_to(disk / "mmcblk7p7")
        self.assertEqual(
            (Path("/dev/mmcblk7"), Path("/dev/mmcblk7p7")), provision.discover(sysfs)
        )
        (controller / "type").write_text("SD\n")
        with self.assertRaises(ValueError):
            provision.discover(sysfs)

    def test_ioctl_readonly_gate_refuses_writable_or_nonblock(self):
        fake = self.root / "device"
        fake.touch()
        block = mock.Mock(st_mode=stat.S_IFBLK)

        def readonly(_, request, value, mutate):
            self.assertEqual(request, provision.BLKROGET)
            self.assertTrue(mutate)
            value[0] = 1

        with (
            mock.patch.object(provision.os, "fstat", return_value=block),
            mock.patch.object(provision.fcntl, "ioctl", side_effect=readonly),
        ):
            provision.require_readonly(fake)
        with (
            mock.patch.object(provision.os, "fstat", return_value=block),
            mock.patch.object(provision.fcntl, "ioctl"),
        ):
            with self.assertRaisesRegex(ValueError, "read-only"):
                provision.require_readonly(fake)
        with self.assertRaisesRegex(ValueError, "block device"):
            provision.require_readonly(fake)

    def test_vendor_reader_checks_both_nodes_and_never_debugfs_write(self):
        with (
            mock.patch.object(
                provision,
                "require_readonly",
                side_effect=[None, ValueError("writable")],
            ) as check,
            mock.patch.object(provision, "debugfs") as debug,
        ):
            with self.assertRaises(ValueError):
                provision.read_vendor(
                    Path("/dev/mmcblk7"),
                    Path("/dev/mmcblk7p7"),
                    provision.MODULE_SOURCE,
                    100,
                    self.root,
                )
            self.assertEqual(check.call_count, 2)
            debug.assert_not_called()
        with mock.patch.object(
            provision.subprocess,
            "run",
            return_value=mock.Mock(stdout=b"hello", stderr=b""),
        ) as run:
            self.assertEqual(provision.debugfs("/dev/test", "stat /vendor/x"), "hello")
            args = run.call_args.args[0]
            self.assertEqual(args[:2], ["debugfs", "-R"])
            self.assertNotIn("-w", args)

    def test_fresh_hostname_is_readable_unique_and_durable(self):
        first, second = self.root / "card-one", self.root / "card-two"
        with mock.patch.object(
            identity.secrets, "choice", side_effect=list("m7k3pb9x2v")
        ):
            one = identity.change(first, ssh_enabled=True)
            two = identity.change(second, ssh_enabled=False)
        self.assertEqual(one["hostname"], "fascia-m7k3p")
        self.assertEqual(two["hostname"], "fascia-b9x2v")
        self.assertNotEqual(one["hostname"], two["hostname"])
        with mock.patch.object(
            identity, "new_hostname", side_effect=AssertionError("must retain identity")
        ):
            self.assertEqual(
                identity.ensure_identities(first)["hostname"], one["hostname"]
            )
            self.assertEqual(
                identity.change(first, timezone="Europe/London")["hostname"],
                one["hostname"],
            )
            identity.change(first, hostname="kitchen")
            self.assertEqual(identity.ensure_identities(first)["hostname"], "kitchen")
        self.assertFalse(set("ilo01") & set(identity.HOSTNAME_ALPHABET))

    def test_existing_unsuffixed_hostname_is_not_renamed(self):
        root = self.root / "old-card"
        identity.change(root, hostname="fascia")
        with mock.patch.object(
            identity, "new_hostname", side_effect=AssertionError("must retain identity")
        ):
            self.assertEqual(identity.ensure_identities(root)["hostname"], "fascia")
            self.assertEqual(
                identity.change(root, ssh_enabled=True)["hostname"], "fascia"
            )

    def test_identity_survives_reboot_and_failed_replacement(self):
        root = self.root / "identity"
        identity.ensure_identities(root)
        original = {
            name: (root / name).read_bytes() for name in ["machine-id", "wifi-mac"]
        }
        identity.change(
            root, hostname="study-frame", timezone="Europe/London", ssh_enabled=True
        )
        identity.ensure_identities(root)
        self.assertEqual(identity.load(root)["hostname"], "study-frame")
        for name, value in original.items():
            self.assertEqual((root / name).read_bytes(), value)
        self.assertEqual(int(original["wifi-mac"][:2], 16) & 3, 2)
        old = (root / "config.json").read_bytes()
        with mock.patch.object(
            identity.os, "replace", side_effect=OSError("power interruption")
        ):
            with self.assertRaises(OSError):
                identity.change(root, hostname="different")
        self.assertEqual((root / "config.json").read_bytes(), old)
        self.assertFalse(list(root.glob(".identity-*")))
        self.assertEqual((root / "machine-id").stat().st_mode & 0o777, 0o600)
        (root / "machine-id").write_text("corrupt")
        with self.assertRaises(ValueError):
            identity.ensure_identities(root)

    def test_directory_sync_failure_reports_uncertain_commit_not_old_state(self):
        root = self.root / "identity"
        identity.ensure_identities(root)
        sync = identity.os.fsync

        def fail_parent(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory sync failed")
            return sync(fd)

        with mock.patch.object(identity.os, "fsync", side_effect=fail_parent):
            with self.assertRaises(OSError):
                identity.change(root, hostname="changed")
        # Rename already succeeded: callers must report unconfirmed, not rollback.
        self.assertEqual(identity.load(root)["hostname"], "changed")
        self.assertFalse(list(root.glob(".identity-*")))

    @unittest.skipUnless(
        shutil.which("ssh-keygen"), "ssh-keygen needed for actual key-pair test"
    )
    def test_disposable_etc_reconstructed_with_same_hostkey_and_optins(self):
        root = self.root / "identity"
        etc = self.root / "etc"
        zone = self.root / "zoneinfo"
        zone.mkdir()
        (zone / "UTC").write_bytes(b"test timezone")
        provision.materialize(root, etc, zone)
        self.assertEqual(os.readlink(etc / "systemd/system/sshd.service"), "/dev/null")
        key = (etc / "ssh/ssh_host_ed25519_key").read_bytes()
        machine = (etc / "machine-id").read_bytes()
        shutil.rmtree(etc)
        identity.change(root, hostname="new-name", physical_home=True, ssh_enabled=True)
        provision.materialize(root, etc, zone)
        self.assertEqual((etc / "ssh/ssh_host_ed25519_key").read_bytes(), key)
        self.assertEqual((etc / "machine-id").read_bytes(), machine)
        self.assertEqual((etc / "hostname").read_text(), "new-name\n")
        self.assertFalse((etc / "systemd/system/sshd.service").exists())
        self.assertTrue(
            (etc / "systemd/system/pf-kiosk.service.d/97-physical-home.conf").is_file()
        )
        output = self.root / "generator"
        generator.generate(root, output)
        self.assertTrue((output / "multi-user.target.wants/sshd.service").is_symlink())
        self.assertTrue(
            (output / "multi-user.target.wants/fascia-home-button.service").is_symlink()
        )

    def test_extension_paths_and_identity_fields_are_allowlisted(self):
        with self.assertRaises(ValueError):
            identity.validate(dict(identity.DEFAULTS, timezone="../etc/passwd"))
        with self.assertRaises(ValueError):
            identity.validate(dict(identity.DEFAULTS, ssh_enabled="yes"))
        mapping = self.root / "map.json"
        mapping.write_text(
            json.dumps(
                [
                    {
                        "source": "/vendor/x",
                        "destination": "../../etc/passwd",
                        "sha256": "0" * 64,
                        "max_bytes": 16,
                    }
                ]
            )
        )
        with mock.patch.object(provision, "discover") as discover:
            with self.assertRaises(ValueError):
                provision.extension_files(mapping, self.root / "firmware")
            discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
