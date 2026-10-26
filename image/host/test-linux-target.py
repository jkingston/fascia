#!/usr/bin/env python3
"""Loop identity checks without kernel access or device operations."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
import os
import tempfile

spec = importlib.util.spec_from_file_location(
    "target", Path(__file__).with_name("linux-target.py")
)
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)


class IdentityTests(unittest.TestCase):
    def test_regular_source_eviction_and_unrelated_descriptor_rejection(self):
        with tempfile.TemporaryFile() as approved, tempfile.TemporaryFile() as other:
            approved.write(b"fixture")
            approved.flush()
            info = os.fstat(approved.fileno())
            identity = {
                "source_dev": info.st_dev,
                "source_inode": info.st_ino,
                "source_rdev": info.st_rdev,
            }
            if hasattr(os, "posix_fadvise"):
                self.assertIn(
                    "dontneed", target.evict_backing(approved.fileno(), identity)
                )
            with self.assertRaises(ValueError):
                target.evict_backing(other.fileno(), identity)
            for fd in (None, True, -1):
                with self.assertRaises(ValueError):
                    target.evict_backing(fd, identity)

    def test_malformed_identity_is_rejected_before_opening_any_device(self):
        for identity in (None, {}, {"capacity": True}, {"capacity": -1}):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                target.verify("/never-open-this-device", identity)

    def test_exact_opened_backing_required_even_for_same_capacity(self):
        backing = SimpleNamespace(st_dev=5, st_ino=77, st_rdev=123)
        fields = [5, 77, 123, 0, 8192, 2, 0, 0, 12, b"", b"", b"", 0, 0]
        target.verify_status(target.INFO.pack(*fields), backing, 8192, 2)
        for index, value in [
            (0, 6),
            (1, 78),
            (2, 124),
            (3, 512),
            (4, 16384),
            (5, 3),
            (8, 8),
        ]:
            changed = list(fields)
            changed[index] = value
            with self.subTest(field=index), self.assertRaises(ValueError):
                target.verify_status(target.INFO.pack(*changed), backing, 8192, 2)
        self.assertEqual(232, target.INFO.size)
        self.assertEqual(304, target.CONFIG.size + target.INFO.size + 64)


if __name__ == "__main__":
    unittest.main()
