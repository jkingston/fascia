#!/usr/bin/env python3
"""Exercise the actual early-directory function on disposable files only."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

INIT = Path(__file__).resolve().parents[1] / "initramfs/init"


class EarlyDataTests(unittest.TestCase):
    def test_owner_access_precedes_provisioning_and_volatile_modes_are_scoped(self):
        source = INIT.read_text()
        self.assertLess(
            source.index("prepare_persistent_dirs /data"),
            source.index("exec switch_root"),
        )
        self.assertLess(
            source.index("umask 077"), source.index("mkdir -p /data/identity")
        )
        entries = re.search(r"for entry in (.+); do", source).group(1)
        modes = dict(re.findall(r"'([^']+) ([0-7]+)'", entries))
        self.assertEqual(
            modes,
            {
                "var/log": "755",
                "var/lib/lastlog": "755",
                "var/tmp": "1777",
                "tmp": "1777",
            },
        )
        self.assertIn("mode=$2", source)

    def test_first_shared_filesystem_mount_enables_runtime_compression(self):
        commands = [
            line
            for line in INIT.read_text().splitlines()
            if line.startswith("mount -t btrfs")
        ]
        self.assertIn("compress=zstd:3", commands[0])
        self.assertIn("subvolid=5", commands[0])
        self.assertIn(" /system", commands[0])
        self.assertTrue(all("rw," in command for command in commands))

    @unittest.skipUnless(
        os.geteuid() == 0,
        "real UID1000 directory ownership needs root; no devices/mounts used",
    )
    def test_fresh_and_existing_data_are_accessible_without_seeding_service(self):
        source = INIT.read_text()
        body = re.search(
            r"^prepare_persistent_dirs\(\) \{\n.*?^\}", source, re.M | re.S
        ).group(0)
        with tempfile.TemporaryDirectory(prefix="fascia-early-data-") as directory:
            root = Path(directory)
            (root / "fascia/state/clock").mkdir(parents=True)
            existing = root / "fascia/state/clock/settings.json"
            existing.write_text("owner settings survive")
            previous_owner = existing.stat().st_uid
            (root / "home/fascia").mkdir(parents=True, mode=0o700)
            for _ in range(2):
                subprocess.run(
                    [
                        "sh",
                        "-c",
                        "umask 077\n" + body + '\nprepare_persistent_dirs "$1"',
                        "test-data",
                        directory,
                    ],
                    check=True,
                )
            for path in ("fascia", "fascia/apps", "fascia/state", "home/fascia"):
                info = (root / path).stat()
                self.assertEqual(
                    (info.st_uid, info.st_gid, info.st_mode & 0o777),
                    (1000, 1000, 0o755),
                )
            self.assertEqual(existing.read_text(), "owner settings survive")
            self.assertEqual(
                existing.stat().st_uid,
                previous_owner,
                "no recursive ownership mutation",
            )
            self.assertEqual((root / "iwd").stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
