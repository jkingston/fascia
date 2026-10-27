"""Host UX and input isolation tests; no card, root, network or VM required."""

import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "fascia_install", Path(__file__).parents[1] / "install.py"
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class Install(unittest.TestCase):
    def test_default_legacy_mixed_release_is_rebuilt_and_new_pointer_is_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            old, new = root / "old", root / "new"
            (old / "apps").mkdir(parents=True)
            (old / "bin").mkdir()
            (old / "bin/fascia-host").write_bytes(b"old")
            (new / "bin").mkdir(parents=True)
            (new / "bin/fascia-host").write_bytes(b"new")
            (root / ".local").mkdir()
            pointer = root / ".local/release"
            pointer.symlink_to(old)

            def build(*args, **kwargs):
                pointer.unlink()
                pointer.symlink_to(new)

            args = argparse.Namespace(release=None, offline=False)
            with (
                mock.patch.object(installer, "PROJECT", root),
                mock.patch.object(installer.shutil, "which", return_value="mise"),
                mock.patch.object(
                    installer.subprocess, "run", side_effect=build
                ) as run,
            ):
                self.assertEqual(new, installer.release_directory(args))
            self.assertEqual("scripts/package-release.sh", run.call_args.args[0][-1])
            args.release = old
            with self.assertRaisesRegex(ValueError, "OS-only release"):
                installer.release_directory(args)

    def test_initial_apps_are_a_separate_explicit_image_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            package = root / "initial-apps"
            package.mkdir()
            guest = mock.Mock()
            with mock.patch.object(installer, "load", return_value=guest):
                result = installer.initial_apps_directory(
                    argparse.Namespace(initial_apps=package, offline=True)
                )
            self.assertEqual(package, result)
            guest.load_module.return_value.validate_initial_apps.assert_called_once_with(
                package
            )
            with (
                mock.patch.object(installer, "PROJECT", root),
                self.assertRaisesRegex(ValueError, "already built initial apps"),
            ):
                installer.initial_apps_directory(
                    argparse.Namespace(initial_apps=None, offline=True)
                )

    def test_refresh_and_offline_are_rejected_before_host_or_worker_actions(self):
        for command, extra in (
            ("prepare", []),
            ("image", ["--output", "unused.raw"]),
            ("install", ["--disk", "disk9", "--capacity", "32000000000", "--erase"]),
        ):
            with (
                self.subTest(command=command),
                mock.patch.object(
                    installer.sys,
                    "argv",
                    ["install.py", command, *extra, "--refresh", "--offline"],
                ),
                mock.patch.object(installer, "check_host") as check,
                mock.patch.object(installer, "worker") as worker,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                installer.main()
            self.assertEqual(2, error.exception.code)
            check.assert_not_called()
            worker.assert_not_called()

    def test_refresh_is_carried_in_immutable_worker_request_without_deleting_cache(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            key = "a" * 64
            old = cache / "prepared" / key / "runtime.tar.zst"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"previous completed preparation")
            args = argparse.Namespace(offline=False, refresh=True)
            guest = mock.Mock()
            guest.source_key.return_value = key
            with (
                mock.patch.object(
                    installer, "release_directory", return_value=cache / "release"
                ),
                mock.patch.object(
                    installer,
                    "initial_apps_directory",
                    return_value=cache / "initial-apps",
                ),
                mock.patch.object(
                    installer.inputs,
                    "prepare",
                    return_value={
                        "rootfs_sha256": "b" * 64,
                        "signature_sha256": "c" * 64,
                    },
                ),
                mock.patch.object(installer.sys, "platform", "linux"),
                mock.patch.object(installer, "snapshot_tree") as snapshot,
                mock.patch.object(installer, "snapshot_downloads"),
                mock.patch.object(installer, "source_files", return_value=[]),
                mock.patch.object(installer, "load", return_value=guest),
                mock.patch.object(installer, "run_preparation"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                job, control, request = installer.prepare(args, cache)
            saved = json.loads((Path(control["input"]) / "request.json").read_text())
            self.assertIs(saved["refresh"], True)
            self.assertIs(saved["offline"], False)
            self.assertEqual(saved, request)
            snapshot.assert_any_call(
                cache / "initial-apps", Path(control["input"]) / "initial-apps"
            )
            self.assertTrue(job.is_file())
            self.assertEqual(b"previous completed preparation", old.read_bytes())

    def test_linux_arm_handler_must_be_enabled_and_chroot_capable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu-arm"
            for status, flags, expected in (
                ("enabled", "F", True),
                ("enabled", "POCF", True),
                ("enabled", "", False),
                ("disabled", "F", False),
            ):
                path.write_text(
                    f"{status}\ninterpreter /usr/bin/qemu-arm-static\nflags: {flags}\n"
                )
                with self.subTest(status=status, flags=flags):
                    self.assertEqual(
                        expected, installer.arm32_handler_available(directory)
                    )

    def test_completed_board_checkpoint_survives_later_prepare_failure(self):
        guest = mock.Mock()
        failure = ValueError("runtime hook failed")
        with mock.patch.object(installer, "worker", side_effect=failure):
            with self.assertRaisesRegex(ValueError, "runtime hook"):
                installer.run_preparation("job", guest, "output", "downloads")
        guest.promote_board_cache.assert_called_once_with("output", "downloads")

    def test_invalid_checkpoint_does_not_hide_original_failure(self):
        guest = mock.Mock()
        guest.promote_board_cache.side_effect = ValueError("bad checkpoint")
        with (
            mock.patch.object(
                installer, "worker", side_effect=ValueError("runtime hook failed")
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(ValueError, "runtime hook failed"):
                installer.run_preparation("job", guest, "output", "downloads")

    def test_new_sparse_image_and_existing_output_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "test.raw"
            image = installer.create_image(output, 8192)
            self.assertEqual(8 * 1024**3, image.stat().st_size)
            with self.assertRaises(FileExistsError):
                installer.create_image(output, 8192)
            for size in (True, 1, 8192.0, 65537):
                with self.subTest(size=size), self.assertRaises(ValueError):
                    installer.create_image(Path(directory) / "invalid", size)

    def test_image_symlink_cannot_overwrite_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "keep").write_text("preserve")
            (root / "link").symlink_to(root / "keep")
            with self.assertRaises(ValueError):
                installer.create_image(root / "link", 8192)
            self.assertEqual("preserve", (root / "keep").read_text())

    def test_archive_source_snapshot_excludes_private_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "main.zig").write_text("public")
            for name in (".git", ".local", "secrets", "__pycache__"):
                (source / name).mkdir()
                (source / name / "private").write_text("exclude")
            with mock.patch.object(
                installer.subprocess, "check_output", side_effect=FileNotFoundError
            ):
                self.assertEqual(
                    [Path("main.zig")], list(installer.source_files(source))
                )

    def test_snapshot_rejects_symlink_in_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "external").symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                installer.snapshot_tree(source, root / "snapshot")

    def test_single_cache_operation_and_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            with installer.installer_lock(cache):
                self.assertEqual(0o700, cache.stat().st_mode & 0o777)
                with self.assertRaises(ValueError):
                    with installer.installer_lock(cache):
                        pass
            cache.chmod(0o777)
            with self.assertRaises(ValueError):
                with installer.installer_lock(cache):
                    pass

    def test_bad_device_is_rejected_before_any_preparation(self):
        args = [
            "install.py",
            "install",
            "--disk",
            "disk0",
            "--capacity",
            str(32 * 1024**3),
            "--erase",
        ]
        with (
            mock.patch.object(installer.sys, "argv", args),
            mock.patch.object(installer.os, "geteuid", return_value=501),
            mock.patch.object(installer, "check_host"),
            mock.patch.object(
                installer.devices, "inspect", side_effect=ValueError("internal disk")
            ),
            mock.patch.object(installer, "prepare") as prepare,
        ):
            with self.assertRaisesRegex(ValueError, "internal"):
                installer.main()
            prepare.assert_not_called()

    def test_noninteractive_install_needs_explicit_erasure(self):
        args = argparse.Namespace(disk="disk9", capacity=None, erase=False, plain=True)
        with mock.patch.object(installer.sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(ValueError, "terminal"):
                installer.select_card(args)

    def test_picker_records_capacity_and_checks_identity_after_confirmation(self):
        identity = {"TotalSize": 32 * 1024**3, "MediaName": "SD card"}
        args = argparse.Namespace(disk=None, capacity=None, erase=False, plain=True)
        prompts = mock.MagicMock()
        prompts.choose.return_value = 0
        with (
            mock.patch.object(installer.sys.stdin, "isatty", return_value=True),
            mock.patch.object(installer, "load", return_value=prompts),
            mock.patch.object(
                installer.devices, "candidates", return_value=[("disk9", identity)]
            ),
            mock.patch.object(
                installer.devices, "inspect", return_value=identity
            ) as inspect,
            mock.patch("builtins.input", return_value="disk9"),
        ):
            self.assertEqual(identity, installer.select_card(args))
            self.assertEqual("disk9", args.disk)
            self.assertEqual(32 * 1024**3, args.capacity)
            inspect.assert_called_once_with("disk9", 32 * 1024**3)

    def test_picker_cancel_never_reinspects_or_prepares(self):
        args = argparse.Namespace(disk=None, capacity=None, erase=False, plain=True)
        prompts = mock.MagicMock()
        prompts.choose.return_value = 1
        with (
            mock.patch.object(installer.sys.stdin, "isatty", return_value=True),
            mock.patch.object(installer, "load", return_value=prompts),
            mock.patch.object(
                installer.devices,
                "candidates",
                return_value=[("disk9", {"TotalSize": 32 * 1024**3})],
            ),
            mock.patch.object(installer.devices, "inspect") as inspect,
        ):
            with self.assertRaisesRegex(ValueError, "cancelled"):
                installer.select_card(args)
            inspect.assert_not_called()

    def test_unattended_install_infers_size_and_rejects_a_swapped_card(self):
        original = {
            "TotalSize": 32 * 1024**3,
            "MediaName": "SD card",
            "MediaUUID": "first",
        }
        swapped = {**original, "MediaUUID": "second"}
        args = argparse.Namespace(disk="disk9", capacity=None, erase=True, plain=True)
        with (
            mock.patch.object(installer, "load", return_value=mock.MagicMock()),
            mock.patch.object(
                installer.devices, "inspect", side_effect=[original, swapped]
            ) as inspect,
            mock.patch("builtins.input") as prompt,
        ):
            with self.assertRaisesRegex(ValueError, "identity changed"):
                installer.select_card(args)
            self.assertEqual(
                inspect.call_args_list,
                [mock.call("disk9", None), mock.call("disk9", 32 * 1024**3)],
            )
            prompt.assert_not_called()

    def test_erase_never_chooses_a_disk_implicitly(self):
        args = argparse.Namespace(disk=None, capacity=None, erase=True, plain=True)
        with self.assertRaisesRegex(ValueError, "explicit"):
            installer.select_card(args)


if __name__ == "__main__":
    unittest.main()
