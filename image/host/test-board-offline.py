"""Network refusal and archive identity for existing board build's offline mode."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1] / "boards/pf1007l"
spec = importlib.util.spec_from_file_location("offline_board", HERE / "build_board.py")
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)


class OfflineBoard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.out = self.root / "output"
        self.out.mkdir()
        self.old = board.OFFLINE_SOURCES

    def tearDown(self):
        board.OFFLINE_SOURCES = self.old
        self.temp.cleanup()

    def record(self, name="source.tar"):
        p = self.cache / name
        p.write_bytes(b"trusted pinned source")
        return dict(file=str(p), sha256=board.digest(p), commit="a" * 40)

    def test_verified_offline_git_archive_never_fetches(self):
        record = self.record()
        board.OFFLINE_SOURCES = ({"archives": {"wifi": record}}, self.cache)
        with mock.patch.object(
            board, "run", side_effect=AssertionError("git/network invoked")
        ):
            result = board.git_archive(
                {"commit": "a" * 40}, self.root, self.out, "wifi"
            )
        self.assertEqual(b"trusted pinned source", result.read_bytes())

    def test_changed_commit_or_bytes_rejected(self):
        record = self.record()
        board.OFFLINE_SOURCES = ({"archives": {"wifi": record}}, self.cache)
        with self.assertRaisesRegex(ValueError, "commit"):
            board.git_archive({"commit": "b" * 40}, self.root, self.out, "wifi")
        Path(record["file"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            board.git_archive({"commit": "a" * 40}, self.root, self.out, "wifi")

    def test_missing_source_cannot_fall_back_to_network(self):
        board.OFFLINE_SOURCES = ({"files": []}, self.cache)
        with mock.patch.object(
            board.urllib.request,
            "urlopen",
            side_effect=AssertionError("network invoked"),
        ):
            with self.assertRaisesRegex(ValueError, "absent"):
                board.fetch(["https://unused.invalid"], self.out / "missing", "a" * 64)

    def test_kernel_archive_is_found_by_hash(self):
        record = self.record()
        board.OFFLINE_SOURCES = ({"archives": {"kernel": record}}, self.cache)
        board.fetch(
            ["https://unused.invalid"], self.out / "kernel.tar", record["sha256"]
        )
        self.assertEqual(
            b"trusted pinned source", (self.out / "kernel.tar").read_bytes()
        )

    def test_source_cannot_escape_boundary(self):
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        with self.assertRaises(ValueError):
            board.offline_file(
                {"file": str(outside), "sha256": board.digest(outside)}, self.cache
            )


if __name__ == "__main__":
    unittest.main()
