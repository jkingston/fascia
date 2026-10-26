#!/usr/bin/env python3
"""Privilege handoff fixtures; never mount filesystems or open target devices."""

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location(
    "fascia_runner", Path(__file__).with_name("runner.py")
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name).resolve()
        self.owner = os.getuid()
        self.jobdir = self.cache / "jobs" / ("a" * 32)
        self.jobdir.mkdir(parents=True, mode=0o700)
        self.input = self.jobdir / "input"
        self.input.mkdir(mode=0o700)
        self.key = "b" * 64
        self.output = self.cache / "prepared" / self.key
        self.output.mkdir(parents=True, mode=0o700)
        self.request = {
            "source_key": self.key,
            "phase": "prepare",
            "erase_authorized": False,
            "capacity": 0,
        }
        self.job = {
            "schema_version": 1,
            "mode": "prepare",
            "input": str(self.input),
            "output": str(self.output),
            "helper": None,
        }
        self.path = self.jobdir / "job.json"
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        (self.input / "request.json").write_text(json.dumps(self.request))
        self.path.write_text(json.dumps(self.job))

    def read(self):
        with patch.dict(os.environ, {}, clear=True):
            return runner.read_job(self.path)

    def test_prepare_job_has_no_target_authority(self):
        self.assertEqual(self.read()[0]["mode"], "prepare")
        self.request["erase_authorized"] = True
        self.save()
        with self.assertRaises(ValueError):
            self.read()

    def test_install_requires_exact_authority(self):
        self.job["mode"] = "image"
        self.request["phase"] = "install"
        self.save()
        with self.assertRaises(ValueError):
            self.read()
        self.request["erase_authorized"] = True
        self.save()
        self.assertEqual(self.read()[0]["mode"], "image")

    def test_external_output_rejected(self):
        self.job["output"] = str(self.cache)
        self.save()
        with self.assertRaises(ValueError):
            self.read()

    def test_source_key_mismatch_rejected(self):
        self.request["source_key"] = "c" * 64
        self.save()
        with self.assertRaises(ValueError):
            self.read()

    def test_unsafe_job_directory_rejected(self):
        self.jobdir.chmod(0o777)
        with self.assertRaises(ValueError):
            self.read()

    def test_job_symlink_rejected(self):
        other = self.jobdir / "real.json"
        self.path.rename(other)
        self.path.symlink_to(other)
        with self.assertRaises(ValueError):
            self.read()

    def test_helper_escape_rejected(self):
        self.job["helper"] = {"kernel": str(self.cache / "other")}
        self.save()
        with self.assertRaises(ValueError):
            self.read()

    def test_namespace_prepare_never_opens_device(self):
        self.job["work"] = str(self.cache / "work")
        commands = []
        with (
            patch.object(
                runner.subprocess,
                "run",
                side_effect=lambda args, **kw: commands.append(args),
            ),
            patch.object(runner.devices, "open_target") as opened,
        ):
            runner.linux_namespace(self.job, self.request)
            opened.assert_not_called()
        self.assertTrue(any("guest.py" in str(c) for c in commands))
        self.assertFalse(any("/dev" in list(map(str, c)) for c in commands))
        self.assertIn(["mount", "--make-rprivate", "/"], commands)
        self.assertIn(["mount", "-o", "remount,bind,ro", str(self.input)], commands)

    def test_loop_mismatch_stops_before_guest(self):
        self.job.update(mode="image", work=str(self.cache / "work"))
        self.request["linux_device"] = {
            "path": "/dev/loop123",
            "loop_identity": {"fixture": True},
        }
        commands = []
        backend = Mock()
        backend.verify.side_effect = ValueError("wrong backing")
        with (
            patch.object(
                runner.subprocess,
                "run",
                side_effect=lambda args, **kw: commands.append(args),
            ),
            patch.object(runner, "load", return_value=backend),
        ):
            with self.assertRaises(ValueError):
                runner.linux_namespace(self.job, self.request)
        backend.verify.assert_called_once_with("/dev/loop123", {"fixture": True})
        self.assertFalse(any("guest.py" in str(c) for c in commands))

    def test_invalid_image_stops_before_mapping(self):
        image = self.cache / "test.img"
        image.write_bytes(b"fixture")
        self.job.update(mode="image", image=str(image))
        self.request["capacity"] = 1000
        work = self.cache / "work"
        work.mkdir()
        with (
            patch.object(runner.os, "geteuid", return_value=0),
            patch.object(runner.tempfile, "mkdtemp", return_value=str(work)),
            patch.object(runner, "load") as load,
            patch.object(runner, "restore_outputs"),
            patch.object(runner.os, "chown"),
            patch.object(runner.os, "rmdir"),
        ):
            with self.assertRaises(ValueError):
                runner.linux(self.path, self.job, self.request, self.owner)
            load.assert_not_called()

    def _mapping_fixture(self, fail):
        image = self.cache / "test.img"
        image.write_bytes(b"fixture")
        self.job.update(mode="image", image=str(image))
        self.request["capacity"] = 7
        work = self.cache / "work"
        work.mkdir()
        events = []
        commands = []
        mapping = {
            "path": "/dev/loop123",
            "loop_identity": {"fixture": True},
            "capacity": 7,
        }

        @contextlib.contextmanager
        def attach(fd, capacity):
            self.assertEqual(os.fstat(fd).st_ino, image.stat().st_ino)
            self.assertEqual(capacity, 7)
            events.append("attach")
            try:
                yield mapping
            finally:
                events.append("detach")

        backend = Mock()
        backend.attach = attach

        def command(args, **kw):
            commands.append(args)
            if args[0] == "unshare" and fail:
                raise subprocess.CalledProcessError(1, args)

        with (
            patch.object(runner.os, "geteuid", return_value=0),
            patch.object(runner.tempfile, "mkdtemp", return_value=str(work)),
            patch.object(runner.subprocess, "run", side_effect=command),
            patch.object(runner, "load", return_value=backend),
            patch.object(runner, "restore_outputs"),
            patch.object(runner.os, "chown"),
            patch.object(runner.os, "rmdir"),
        ):
            if fail:
                with self.assertRaises(subprocess.CalledProcessError):
                    runner.linux(self.path, self.job, self.request, self.owner)
            else:
                runner.linux(self.path, self.job, self.request, self.owner)
        self.assertEqual(events, ["attach", "detach"])
        self.assertEqual(
            {
                k: v
                for k, v in self.request["linux_device"].items()
                if k != "backing_fd"
            },
            mapping,
        )
        self.assertIsInstance(self.request["linux_device"]["backing_fd"], int)
        self.assertFalse(any(c[0] == "losetup" for c in commands))
        self.assertIn("--net", next(c for c in commands if c[0] == "unshare"))
        if fail:
            backend.verify.assert_not_called()
        else:
            backend.verify.assert_called_once_with("/dev/loop123", {"fixture": True})

    def test_unshare_failure_closes_held_fd_mapping(self):
        self._mapping_fixture(True)

    def test_linux_prepare_keeps_host_network_without_any_target_mapping(self):
        work = self.cache / "work"
        work.mkdir()
        with (
            patch.object(runner.os, "geteuid", return_value=0),
            patch.object(runner.tempfile, "mkdtemp", return_value=str(work)),
            patch.object(runner.subprocess, "run") as command,
            patch.object(runner, "load") as backend,
            patch.object(runner, "restore_outputs"),
            patch.object(runner.os, "chown"),
            patch.object(runner.os, "rmdir"),
        ):
            runner.linux(self.path, self.job, self.request, self.owner)
        argv = command.call_args.args[0]
        self.assertEqual("unshare", argv[0])
        self.assertNotIn("--net", argv)
        self.assertEqual((), command.call_args.kwargs["pass_fds"])
        backend.assert_not_called()

    def test_success_verifies_held_fd_mapping(self):
        self._mapping_fixture(False)


if __name__ == "__main__":
    unittest.main()
