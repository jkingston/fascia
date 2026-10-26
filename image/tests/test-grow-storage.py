#!/usr/bin/env python3
"""Storage-layout and interruption guards; pure files, no device access."""

import importlib.util
from pathlib import Path
import struct
import unittest


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IMAGE = Path(__file__).resolve().parents[1]
grow = load("grow", IMAGE / "fascia-grow-storage.py")
assemble = load("assemble", IMAGE / "tools/assemble-image.py")


class GrowthGuards(unittest.TestCase):
    def setUp(self):
        self.header = assemble.mbr(8192)
        self.size = 8192 * 2048 - grow.START

    def plan(self, header=None, sectors=32768 * 2048, start=grow.START, size=None):
        return grow.plan(
            self.header if header is None else header,
            sectors,
            start,
            self.size if size is None else size,
        )

    def test_expands_only_second_partition_count(self):
        after, desired = self.plan()
        offset = grow.SIZE_OFFSET
        self.assertEqual(after[:offset], self.header[:offset])
        self.assertEqual(after[offset + 4 :], self.header[offset + 4 :])
        self.assertEqual(desired, 32768 * 2048 - grow.START)
        self.assertEqual(struct.unpack_from("<I", after, offset)[0], desired)

    def test_retries_after_table_write_without_shrinking_or_flag_file(self):
        after, desired = self.plan()
        # Kernel geometry may still be the old size after interruption.
        self.assertEqual(self.plan(after), (after, desired))
        self.assertEqual(self.plan(after, size=desired), (after, desired))
        # A completed card moved to a still larger card can grow again.
        _, bigger = self.plan(after, size=desired, sectors=65536 * 2048)
        self.assertGreater(bigger, desired)

    def test_existing_capacity_is_noop(self):
        self.assertEqual(self.plan(sectors=8192 * 2048), (self.header, self.size))

    def test_bad_signature_extra_partition_and_moved_geometry_refused(self):
        for offset, data in (
            (440, b"WRNG"),
            (510, b"\0\0"),
            (478, b"\x80"),
            (446, b"\0"),
            (450, b"\x83"),
            (454, struct.pack("<I", 1)),
            (466, b"\xee"),
            (470, struct.pack("<I", grow.START + 2048)),
        ):
            with self.subTest(offset=offset):
                changed = bytearray(self.header)
                changed[offset : offset + len(data)] = data
                with self.assertRaises(ValueError):
                    self.plan(changed)

    def test_small_card_address_overflow_and_kernel_mismatch_refused(self):
        for kwargs in (
            {"sectors": 4096 * 2048},
            {"sectors": 2**32},
            {"start": grow.START + 1},
            {"size": self.size + 1},
            {"size": 1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.plan(**kwargs)

    def test_btrfs_geometry_is_single_device_and_checked(self):
        raw = bytearray(4096)
        raw[64:72] = b"_BHRfS_M"
        struct.pack_into("<Q", raw, 112, self.size * 512)
        struct.pack_into("<Q", raw, 136, 1)
        struct.pack_into("<I", raw, 144, 4096)
        self.assertEqual(grow.filesystem_size(raw), self.size * 512)
        for offset, value in (
            (64, b"WRONGMAG"),
            (136, struct.pack("<Q", 2)),
            (144, struct.pack("<I", 512)),
        ):
            changed = raw.copy()
            changed[offset : offset + len(value)] = value
            with self.assertRaises(ValueError):
                grow.filesystem_size(changed)

    def test_mount_records_preserve_source_and_subvolume_identity(self):
        text = "1 0 0:3 /data /data rw,noatime - btrfs /dev/mmcblk0p2 rw\n"
        self.assertEqual(
            grow.mount_record(text, "/data"),
            ("/data", "rw,noatime", "btrfs", "/dev/mmcblk0p2"),
        )
        for invalid in ("", text + text):
            with self.assertRaises(ValueError):
                grow.mount_record(invalid, "/data")


if __name__ == "__main__":
    unittest.main()
