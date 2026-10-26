#!/usr/bin/env python3
"""Host-only negative tests; never attaches a device or starts a VM."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import io
import hashlib
import contextlib
import subprocess
import sys
import os

spec = importlib.util.spec_from_file_location(
    "helper", Path(__file__).with_name("helper.py")
)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class HelperTests(unittest.TestCase):
    def test_prebuilt_launcher_integrity_and_guard_target_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            binary = cache / "mac-helper"
            binary.write_bytes(b"fixture")
            manifest = cache / "mac-helper.json"
            import json

            manifest.write_text(
                json.dumps(
                    {
                        "source": helper.digest(helper.HERE / "mac-helper.swift"),
                        "binary": helper.digest(binary),
                    }
                )
            )
            self.assertEqual(binary, helper.verified_launcher(cache, os.getuid()))
            binary.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                helper.verified_launcher(cache, os.getuid())
        with mock.patch.object(helper.subprocess, "Popen") as spawn:
            for name in ("/dev/disk4", "disk4s1", "disk4;true", ""):
                with self.assertRaises(ValueError):
                    helper.MountGuard("unused", name)
            spawn.assert_not_called()

    def test_ca_bundle_missing_or_malformed_is_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                helper.prepare_certificates(root)
            ca = root / "usr/share/ca-certificates/mozilla"
            ca.mkdir(parents=True)
            (ca / "bad.crt").write_bytes(b"not a certificate")
            with self.assertRaises(ValueError):
                helper.prepare_certificates(root)
            self.assertFalse((root / "etc/ssl/certs/ca-certificates.crt").exists())

    def test_bad_archive_never_reaches_extractor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.deb"
            source.write_bytes(b"untrusted")
            record = {
                "name": "bad",
                "url": "https://example.invalid/source.deb",
                "bytes": 9,
                "sha256": "0" * 64,
            }
            cache = root / "cache"
            cache.mkdir()
            response = io.BytesIO(b"untrusted")
            with mock.patch.object(helper.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = response
                with self.assertRaises(ValueError):
                    helper.fetch(record, cache)
            self.assertEqual([], list(cache.iterdir()))

    def test_offline_cache_authentication_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {
                "name": "fixture",
                "url": "https://example.invalid/a.deb",
                "bytes": 4,
                "sha256": hashlib.sha256(b"good").hexdigest(),
            }
            with mock.patch.object(helper.urllib.request, "build_opener") as opener:
                with self.assertRaises(ValueError):
                    helper.fetch(record, root, offline=True)
                (root / "a.deb").write_bytes(b"good")
                self.assertEqual(
                    root / "a.deb", helper.fetch(record, root, offline=True)
                )
                (root / "a.deb").write_bytes(b"evil")
                with self.assertRaises(ValueError):
                    helper.fetch(record, root, offline=True)
                (root / "a.deb").unlink()
                (root / "target").write_bytes(b"good")
                (root / "a.deb").symlink_to(root / "target")
                with self.assertRaises(ValueError):
                    helper.fetch(record, root, offline=True)
                opener.assert_not_called()

    def test_image_symlink_and_overlap_refused_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "in"
            outputs = root / "out"
            inputs.mkdir()
            outputs.mkdir()
            image = root / "disk"
            image.write_bytes(b"1234")
            link = root / "link"
            link.symlink_to(image)
            for target, capacity in ((link, 4), (image, 3)):
                with self.assertRaises(ValueError):
                    helper.run_helper(
                        "kernel",
                        "initrd",
                        inputs,
                        outputs,
                        image=target,
                        capacity=capacity,
                    )
            with self.assertRaises(ValueError):
                helper.run_helper("k", "i", inputs, inputs)

    def test_hyphenated_module_and_recursive_dependency_are_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source/usr/lib/modules/test/kernel"
            source.mkdir(parents=True)
            (source / "virtio-rng.ko").write_bytes(b"\x00depends=shared_dep\x00")
            (source / "shared-dep.ko").write_bytes(b"\x00depends=\x00")
            selected = helper.module_subset(root / "source", root / "dest", "test")
            self.assertEqual(["kernel/shared-dep.ko", "kernel/virtio-rng.ko"], selected)
            self.assertTrue(
                (root / "dest/usr/lib/modules/test/kernel/shared-dep.ko").is_file()
            )

    def test_stage_stream_keeps_full_log_without_terminal_noise(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "log"
            terminal = io.StringIO()
            with contextlib.redirect_stdout(terminal):
                result = helper.stream_process(
                    [
                        sys.executable,
                        "-c",
                        "print('ordinary command output');print('FASCIA_STAGE build-board');print('more transcript')",
                    ],
                    log,
                    5,
                )
            self.assertEqual(0, result)
            self.assertEqual("Build board…\n", terminal.getvalue())
            self.assertIn("ordinary command output", log.read_text())
            with self.assertRaises(subprocess.TimeoutExpired):
                helper.stream_process(
                    [sys.executable, "-c", "import time;time.sleep(30)"], log, 0.1
                )

    def test_guard_failure_stops_and_reaps_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            processes = []
            real_spawn = subprocess.Popen

            def spawn(*args, **kwargs):
                process = real_spawn(*args, **kwargs)
                processes.append(process)
                return process

            def failed_guard():
                raise RuntimeError("guardian exited")

            with mock.patch.object(helper.subprocess, "Popen", side_effect=spawn):
                with self.assertRaisesRegex(RuntimeError, "guardian exited"):
                    helper.stream_process(
                        [sys.executable, "-c", "import time;time.sleep(30)"],
                        Path(directory) / "log",
                        5,
                        monitor=failed_guard,
                    )
            self.assertIsNotNone(processes[0].poll())

    def test_regular_fd_cannot_be_presented_as_card(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a = root / "a"
            b = root / "b"
            a.mkdir()
            b.mkdir()
            with (root / "disk").open("wb") as source:
                with self.assertRaises(ValueError):
                    helper.run_helper(
                        "k", "i", a, b, target_fd=source.fileno(), capacity=512
                    )


if __name__ == "__main__":
    unittest.main()
