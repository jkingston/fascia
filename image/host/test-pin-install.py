"""Snapshot repository metadata must preserve dependency and provider records."""

import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "pin_install", Path(__file__).parent.parent / "tools/pin-install.py"
)
pin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pin)


class Snapshot(unittest.TestCase):
    def test_snapshot_keeps_all_selected_metadata_and_signatures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            data = b"signed package fixture"
            filename = "example-1-1-armv7h.pkg.tar.xz"
            (repository / filename).write_bytes(data)
            fields = {
                "NAME": "example",
                "VERSION": "1-1",
                "FILENAME": filename,
                "CSIZE": str(len(data)),
                "SHA256SUM": hashlib.sha256(data).hexdigest(),
                "PGPSIG": base64.b64encode(b"signature").decode(),
            }
            desc = "".join(
                f"%{key}%\n{value}\n\n" for key, value in fields.items()
            ).encode()
            depends = b"%PROVIDES%\nvirtual-example\n\n%DEPENDS%\nglibc\n"
            for repo in ("core", "extra", "alarm"):
                with tarfile.open(root / f"{repo}.db", "w:gz") as archive:
                    if repo == "core":
                        for name, content in (("desc", desc), ("depends", depends)):
                            member = tarfile.TarInfo("example-1-1/" + name)
                            member.size = len(content)
                            archive.addfile(member, io.BytesIO(content))
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "build_packages": {"example": "1-1"},
                        "runtime_packages": {
                            "example": "1-1",
                            "fascia-board-support-dkms": "1",
                        },
                    }
                )
            )
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "pin-install.py",
                        str(manifest),
                        str(root),
                        "--release",
                        "fixture",
                    ],
                ),
                patch.object(pin.urllib.request, "urlretrieve") as network,
            ):
                pin.main()
                network.assert_not_called()
            with tarfile.open(repository / "core.db") as archive:
                self.assertEqual(
                    archive.extractfile("example-1-1/depends").read(), depends
                )
            self.assertEqual(
                (repository / (filename + ".sig")).read_bytes(), b"signature"
            )
            lock = json.loads((root / "packages.lock.json").read_text())
            self.assertEqual(lock["runtime_packages"], {"example": "1-1"})
            self.assertEqual(lock["bundle"]["sha256"], pin.sha(root / "packages.tar"))


if __name__ == "__main__":
    unittest.main()
