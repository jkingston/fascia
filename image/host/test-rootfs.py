"""Pinned base integrity; no network or devices."""

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
        self.archive = b"official base archive fixture"
        self.signature = b"detached signature fixture"
        self.spec = {
            "schema_version": 1,
            "architecture": "armv7h",
            "urls": ["https://example.invalid/base"],
            "keyring_file": anchor.name,
            "keyring_sha256": rootfs.digest(anchor),
            "signing_fingerprint": "A" * 40,
            "rootfs_sha256": hashlib.sha256(self.archive).hexdigest(),
            "rootfs_bytes": len(self.archive),
            "signature_sha256": hashlib.sha256(self.signature).hexdigest(),
            "signature_bytes": len(self.signature),
        }
        (self.source / "rootfs.json").write_text(json.dumps(self.spec))
        self.here = patch.object(rootfs, "HERE", self.source)
        self.here.start()
        self.cache = self.root / "cache"

    def tearDown(self):
        self.here.stop()
        self.temp.cleanup()

    def response(self, url, **kwargs):
        return Response(self.signature if url.endswith(".sig") else self.archive)

    def prepare(self):
        with patch.object(rootfs.urllib.request, "urlopen", side_effect=self.response):
            return rootfs.prepare(self.cache)

    def test_cached_base_needs_no_network_even_online(self):
        result = self.prepare()
        with patch.object(rootfs.urllib.request, "urlopen") as network:
            self.assertEqual(rootfs.prepare(self.cache), result)
            self.assertEqual(rootfs.prepare(self.cache, offline=True), result)
            network.assert_not_called()

    def test_changed_base_or_signature_rejected_before_publication(self):
        for suffix in ("base", ".sig"):
            with (
                self.subTest(suffix=suffix),
                patch.object(
                    rootfs.urllib.request,
                    "urlopen",
                    side_effect=lambda url, **kw: Response(b"changed")
                    if url.endswith(suffix)
                    else self.response(url),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    rootfs.prepare(self.cache)
            self.assertFalse(
                (self.cache / "rootfs" / self.spec["signature_sha256"]).exists()
            )

    def test_changed_cached_bytes_fail(self):
        result = self.prepare()
        Path(result["rootfs"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "Corrupt cached"):
            rootfs.prepare(self.cache, offline=True)

    def test_modified_cache_metadata_cannot_override_lock(self):
        result = self.prepare()
        path = Path(result["rootfs"])
        path.write_bytes(b"other")
        metadata = path.parent / "snapshot.json"
        record = json.loads(metadata.read_text())
        record.update(
            rootfs_sha256=rootfs.digest(path), rootfs_bytes=path.stat().st_size
        )
        metadata.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "installation lock"):
            rootfs.prepare(self.cache, offline=True)

    def test_missing_offline_base_fails_without_network(self):
        with patch.object(rootfs.urllib.request, "urlopen") as network:
            with self.assertRaises(FileNotFoundError):
                rootfs.prepare(self.cache, offline=True)
            network.assert_not_called()

    def test_bad_trust_anchor_fails_before_network(self):
        (self.source / "public.asc").write_bytes(b"substituted")
        with patch.object(rootfs.urllib.request, "urlopen") as network:
            with self.assertRaisesRegex(ValueError, "trust anchor"):
                rootfs.prepare(self.cache)
            network.assert_not_called()

    def test_oversized_download_never_publishes(self):
        with (
            patch.object(rootfs, "MAX_ROOTFS", 2),
            patch.object(rootfs.urllib.request, "urlopen", side_effect=self.response),
        ):
            with self.assertRaisesRegex(ValueError, "Oversized"):
                rootfs.prepare(self.cache)

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

    def test_rejected_pair_is_quarantined(self):
        result = self.prepare()
        quarantine = rootfs.invalidate(
            self.cache, result["rootfs_sha256"], result["signature_sha256"]
        )
        self.assertEqual((quarantine / "rootfs.tar.gz").read_bytes(), self.archive)
        self.assertFalse(Path(result["rootfs"]).exists())
        self.assertEqual(self.prepare(), result)


if __name__ == "__main__":
    unittest.main()
