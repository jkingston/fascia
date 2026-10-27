#!/usr/bin/env python3
"""Input integrity fixtures: no network, installs, or device access."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "inputs", Path(__file__).with_name("inputs.py")
)
inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inputs)


class Response(io.BytesIO):
    status = 200
    headers = {}

    def geturl(self):
        return "https://example.invalid/file"


class TestInputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = b"verified bytes"
        self.record = {
            "file": "item.tar",
            "bytes": len(self.data),
            "sha256": hashlib.sha256(self.data).hexdigest(),
            "url": "https://example.invalid/file",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def package_fixture(self, member_name="core.db", kind=tarfile.REGTYPE):
        bundle = self.root / "packages.tar"
        with tarfile.open(bundle, "w") as output:
            member = tarfile.TarInfo(member_name)
            member.type = kind
            member.size = len(self.data) if kind == tarfile.REGTYPE else 0
            output.addfile(member, io.BytesIO(self.data) if member.size else None)
        lock = {
            "schema_version": 1,
            "bundle": {
                "file": bundle.name,
                "sha256": inputs.digest(bundle),
                "bytes": bundle.stat().st_size,
                "url": "https://example.invalid/packages.tar",
            },
            "files": {
                "core.db": {
                    "sha256": hashlib.sha256(self.data).hexdigest(),
                    "bytes": len(self.data),
                }
            },
        }
        (self.root / "packages.lock.json").write_text(json.dumps(lock))
        return bundle

    def test_package_snapshot_integrity_and_cache_reuse(self):
        bundle = self.package_fixture()
        with (
            patch.object(inputs, "HERE", self.root),
            patch.object(inputs, "fetch", return_value=bundle),
        ):
            directory, lock = inputs.prepare_packages(self.root, offline=True)
            self.assertEqual((directory / "core.db").read_bytes(), self.data)
            self.assertEqual(
                inputs.prepare_packages(self.root, offline=True)[0], directory
            )
            (directory / "core.db").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "retained package"):
                inputs.prepare_packages(self.root, offline=True)

    def test_package_snapshot_rejects_paths_links_and_missing_files(self):
        for name, kind in (
            ("../escape", tarfile.REGTYPE),
            ("core.db", tarfile.SYMTYPE),
            ("unknown", tarfile.REGTYPE),
        ):
            with self.subTest(name=name, kind=kind):
                bundle = self.package_fixture(name, kind)
                with (
                    patch.object(inputs, "HERE", self.root),
                    patch.object(inputs, "fetch", return_value=bundle),
                    self.assertRaises(ValueError),
                ):
                    inputs.prepare_packages(self.root, offline=True)

    def test_cached_integrity(self):
        path = self.root / "item.tar"
        path.write_bytes(self.data)
        self.assertEqual(inputs.fetch(self.record, self.root, True), path)
        path.write_bytes(b"bad")
        with self.assertRaises(ValueError):
            inputs.fetch(self.record, self.root, True)

    def test_missing_offline(self):
        with patch.object(inputs.urllib.request, "urlopen") as net:
            with self.assertRaises(FileNotFoundError):
                inputs.fetch(self.record, self.root, True)
            net.assert_not_called()

    def test_symlink_rejected(self):
        real = self.root / "real"
        real.write_bytes(self.data)
        (self.root / "item.tar").symlink_to(real)
        with self.assertRaises(ValueError):
            inputs.fetch(self.record, self.root)
        self.assertEqual(real.read_bytes(), self.data)

    def test_bad_metadata(self):
        for change in [
            {"file": "../escape"},
            {"url": "http://example.invalid"},
            {"bytes": -1},
            {"sha256": "z" * 64},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                inputs.fetch({**self.record, **change}, self.root)

    def test_atomic_download(self):
        with patch.object(
            inputs.urllib.request, "urlopen", return_value=Response(self.data)
        ):
            self.assertEqual(
                inputs.fetch(self.record, self.root).read_bytes(), self.data
            )

    def test_truncated_or_oversized_not_published(self):
        for data in [self.data[:-1], self.data + b"bad"]:
            with (
                self.subTest(data=data),
                patch.object(
                    inputs.urllib.request,
                    "urlopen",
                    side_effect=lambda *a, **k: Response(data),
                ),
                patch.object(inputs.time, "sleep"),
            ):
                with self.assertRaises(ValueError):
                    inputs.fetch(self.record, self.root)
                self.assertFalse((self.root / "item.tar").exists())

    def test_resume(self):
        (self.root / "item.tar.partial").write_bytes(self.data[:4])
        response = Response(self.data[4:])
        response.status = 206
        response.headers = {"Content-Range": "bytes 4-13/14"}
        with patch.object(
            inputs.urllib.request, "urlopen", return_value=response
        ) as net:
            self.assertEqual(
                inputs.fetch(self.record, self.root).read_bytes(), self.data
            )
            self.assertEqual(net.call_args.args[0].headers["Range"], "bytes=4-")

    def test_server_ignores_range(self):
        (self.root / "item.tar.partial").write_bytes(self.data[:4])
        with patch.object(
            inputs.urllib.request, "urlopen", return_value=Response(self.data)
        ):
            self.assertEqual(
                inputs.fetch(self.record, self.root).read_bytes(), self.data
            )

    def test_complete_partial_without_network(self):
        (self.root / "item.tar.partial").write_bytes(self.data)
        with patch.object(inputs.urllib.request, "urlopen") as net:
            inputs.fetch(self.record, self.root)
            net.assert_not_called()

    def test_corrupt_full_length_partial_restarts_without_out_of_range_request(self):
        (self.root / "item.tar.partial").write_bytes(b"x" * len(self.data))
        with patch.object(
            inputs.urllib.request, "urlopen", return_value=Response(self.data)
        ) as network:
            self.assertEqual(
                inputs.fetch(self.record, self.root).read_bytes(), self.data
            )
            self.assertNotIn("Range", network.call_args.args[0].headers)

    def test_profiles_match_retained_package_inventory(self):
        profiles = json.loads((inputs.HERE / "profiles.json").read_text())
        self.assertEqual(profiles["architecture"], "armv7h")
        for group in ("build_packages", "runtime_packages"):
            self.assertEqual(len(profiles[group]), len(set(profiles[group])))
        self.assertIn("linux-armv7", profiles["runtime_packages"])
        self.assertIn("linux-armv7-headers", profiles["runtime_packages"])
        self.assertIn("base-devel", profiles["build_packages"])
        lock = json.loads((inputs.HERE / "packages.lock.json").read_text())
        for group in ("build_packages", "runtime_packages"):
            self.assertTrue(set(profiles[group]) <= set(lock[group]))

    def test_prepare_uses_pinned_base_and_package_repository(self):
        locks = self.root / "profiles"
        locks.mkdir()
        (locks / "profiles.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "build_packages": ["base-devel"],
                    "runtime_packages": ["linux-armv7", "iwd"],
                }
            )
        )
        (locks / "sources.lock.json").write_text(
            json.dumps({"schema_version": 1, "archives": [], "kernel_files": []})
        )
        base = {
            "rootfs": "fixture",
            "rootfs_sha256": "a" * 64,
            "rootfs_signature": "signature",
            "trust_keyring": "public",
        }
        with (
            patch.object(inputs, "HERE", locks),
            patch.object(
                inputs, "prepare_packages", return_value=(self.root / "repository", {})
            ),
            patch.object(inputs.rootfs, "prepare", return_value=base) as rootfs,
            patch.object(inputs.urllib.request, "urlopen") as net,
        ):
            result = inputs.prepare(
                self.root / "cache", offline=True, derived_dir=self.root / "derived"
            )
        rootfs.assert_called_once()
        net.assert_not_called()
        self.assertEqual(result["build_names"], ["base-devel"])
        self.assertEqual(result["runtime_names"], ["linux-armv7", "iwd"])
        self.assertEqual(result["rootfs_sha256"], "a" * 64)
        self.assertTrue(Path(result["offline_source_manifest"]).is_file())


if __name__ == "__main__":
    unittest.main()
