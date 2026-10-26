"""Rolling-base cache, download races and transport bounds; no network or devices."""

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import rootfs


class Response(io.BytesIO):
    def geturl(self):
        return "https://example.invalid/rootfs"


class Rootfs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        anchor = self.source / "public.asc"
        anchor.write_bytes(b"reviewed public trust anchor")
        self.spec = {
            "schema_version": 1,
            "architecture": "armv7h",
            "urls": ["https://example.invalid/base"],
            "keyring_file": anchor.name,
            "keyring_sha256": rootfs.digest(anchor),
            "signing_fingerprint": "A" * 40,
        }
        (self.source / "rootfs.json").write_text(json.dumps(self.spec))
        self.here = patch.object(rootfs, "HERE", self.source)
        self.here.start()
        self.archive = b"official base archive fixture"
        self.signature = b"detached signature fixture"
        self.cache = self.root / "cache"

    def tearDown(self):
        self.here.stop()
        self.temp.cleanup()

    def response(self, url, **kwargs):
        return Response(self.signature if url.endswith(".sig") else self.archive)

    def prepare(self):
        with patch.object(rootfs.urllib.request, "urlopen", side_effect=self.response):
            return rootfs.prepare(self.cache)

    def test_complete_pair_publishes_and_offline_reuses_without_network(self):
        result = self.prepare()
        self.assertEqual(
            result["rootfs_sha256"], hashlib.sha256(self.archive).hexdigest()
        )
        self.assertEqual(Path(result["rootfs"]).read_bytes(), self.archive)
        with patch.object(rootfs.urllib.request, "urlopen") as network:
            self.assertEqual(rootfs.prepare(self.cache, offline=True), result)
            network.assert_not_called()

    def test_online_checks_signature_without_redownloading_unchanged_base(self):
        result = self.prepare()
        with patch.object(
            rootfs.urllib.request, "urlopen", side_effect=self.response
        ) as network:
            self.assertEqual(rootfs.prepare(self.cache), result)
            self.assertEqual(network.call_count, 1)
            self.assertTrue(network.call_args.args[0].endswith(".sig"))

    def test_changed_base_bytes_fail_cache_verification(self):
        result = self.prepare()
        Path(result["rootfs"]).write_bytes(b"tampered base")
        with self.assertRaisesRegex(ValueError, "Corrupt cached"):
            rootfs.prepare(self.cache, offline=True)

    def test_missing_offline_base_fails_without_network(self):
        with patch.object(rootfs.urllib.request, "urlopen") as network:
            with self.assertRaises(FileNotFoundError):
                rootfs.prepare(self.cache, offline=True)
            network.assert_not_called()

    def test_bad_trust_anchor_fails_before_network(self):
        (self.source / "public.asc").write_bytes(b"substituted key")
        with patch.object(rootfs.urllib.request, "urlopen") as network:
            with self.assertRaisesRegex(ValueError, "trust anchor"):
                rootfs.prepare(self.cache)
            network.assert_not_called()

    def test_upstream_signature_rotation_retries_a_complete_pair(self):
        responses = [
            b"old sig",
            self.archive,
            b"new sig",
            b"new sig",
            self.archive,
            b"new sig",
        ]
        with patch.object(
            rootfs.urllib.request,
            "urlopen",
            side_effect=lambda *a, **k: Response(responses.pop(0)),
        ):
            result = rootfs.prepare(self.cache)
        self.assertFalse(responses)
        self.assertEqual(Path(result["rootfs_signature"]).read_bytes(), b"new sig")
        self.assertEqual(len(list((self.cache / "rootfs").glob("*/snapshot.json"))), 1)

    def test_oversized_download_never_publishes(self):
        with (
            patch.object(rootfs, "MAX_ROOTFS", 2),
            patch.object(rootfs.urllib.request, "urlopen", side_effect=self.response),
        ):
            with self.assertRaisesRegex(OSError, "Oversized"):
                rootfs.prepare(self.cache)
        self.assertFalse((self.cache / "rootfs/latest.json").exists())

    def test_insecure_redirect_is_rejected(self):
        response = Response(b"bytes")
        with (
            patch.object(
                response, "geturl", return_value="http://example.invalid/base"
            ),
            patch.object(rootfs.urllib.request, "urlopen", return_value=response),
        ):
            with self.assertRaisesRegex(ValueError, "Non-HTTPS"):
                rootfs.download("https://example.invalid/base", self.root / "file", 100)

    def test_cache_pointer_cannot_escape_to_another_directory(self):
        self.prepare()
        (self.cache / "rootfs/latest.json").write_text(
            json.dumps({"snapshot": "../../outside"})
        )
        with self.assertRaisesRegex(ValueError, "cache identity"):
            rootfs.prepare(self.cache, offline=True)

    def test_rejected_exact_pair_is_quarantined_and_next_run_redownloads(self):
        result = self.prepare()
        quarantine = rootfs.invalidate(
            self.cache, result["rootfs_sha256"], result["signature_sha256"]
        )
        self.assertEqual((quarantine / "rootfs.tar.gz").read_bytes(), self.archive)
        self.assertFalse(Path(result["rootfs"]).exists())
        with patch.object(
            rootfs.urllib.request, "urlopen", side_effect=self.response
        ) as network:
            replacement = rootfs.prepare(self.cache)
        self.assertEqual(network.call_count, 3)
        self.assertEqual(replacement, result)

    def test_rejection_for_another_base_cannot_invalidate_the_cache(self):
        result = self.prepare()
        with self.assertRaisesRegex(ValueError, "does not match"):
            rootfs.invalidate(self.cache, "b" * 64, result["signature_sha256"])
        self.assertTrue(Path(result["rootfs"]).is_file())


if __name__ == "__main__":
    unittest.main()
