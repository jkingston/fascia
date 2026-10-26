import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "refresh", Path(__file__).with_name("fascia-board-refresh.py")
)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class RefreshTests(unittest.TestCase):
    def test_target_kernel_uses_package_files_without_pkgbase_and_ignores_stale_dirs(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "7.2-new-ARCH").mkdir()
            (base / "7.1-old-ARCH").mkdir()  # Leftover DKMS tree is not authoritative.
            paths = [
                "/boot/zImage",
                "/usr/lib/modules/7.2-new-ARCH/",
                "/usr/lib/modules/7.2-new-ARCH/kernel/example.ko.xz",
            ]
            self.assertEqual(r.kernel_release(base, paths), "7.2-new-ARCH")
            self.assertFalse((base / "7.2-new-ARCH/pkgbase").exists())
            with self.assertRaises(ValueError):
                r.kernel_release(
                    base, paths + ["/usr/lib/modules/7.1-old-ARCH/kernel/old.ko"]
                )
            with self.assertRaises(ValueError):
                r.kernel_release(base, ["/usr/lib/modules/../../escape"])

    def test_failed_boot_write_preserves_previous_file_and_leaks_no_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "new"
            target = base / "boot"
            source.write_bytes(b"new")
            target.write_bytes(b"known-good")
            with patch.object(
                r.os, "replace", side_effect=OSError("injected pre-rename")
            ):
                with self.assertRaises(OSError):
                    r.publish(source, target)
            self.assertEqual(target.read_bytes(), b"known-good")
            self.assertEqual(sorted(p.name for p in base.iterdir()), ["boot", "new"])
            r.publish(source, target)
            self.assertEqual(target.read_bytes(), b"new")

    def test_module_or_initrd_failure_invokes_matching_recovery(self):
        for failure in [
            "module compilation",
            "initramfs generation",
            "boot publish fsync",
        ]:
            with (
                self.subTest(failure=failure),
                patch.object(r, "refresh", side_effect=RuntimeError(failure)),
                patch.object(r, "run") as run,
            ):
                with self.assertRaisesRegex(RuntimeError, failure):
                    r.guarded_refresh()
                run.assert_called_once_with(r.RECOVERY, "recovery", "boot-restore")

    def test_non_native_header_helper_fails_before_any_boot_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            headers = Path(tmp)
            tool = headers / "scripts/basic/fixdep"
            tool.parent.mkdir(parents=True)
            tool.write_bytes(b"invalid executable format")
            tool.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "unusable native helper"):
                r.check_header_helpers(headers)

    def test_boot_guard_rejects_unmounted_wrong_disk_and_emmc(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            sysfs = Path(tmp)
            (sysfs / "mmcblk0/device").mkdir(parents=True)
            kind = sysfs / "mmcblk0/device/type"
            kind.write_text("SD\n")

            def records(
                boot_source="/dev/mmcblk0p1",
                boot_fs="vfat",
                system="/dev/mmcblk0p2[/]",
                system_fs="btrfs",
            ):
                return [
                    json.dumps(
                        {"filesystems": [{"source": boot_source, "fstype": boot_fs}]}
                    ),
                    json.dumps(
                        {"filesystems": [{"source": system, "fstype": system_fs}]}
                    ),
                ]

            with patch.object(r, "run", side_effect=records()):
                self.assertEqual(r.production_boot_guard(sysfs), "mmcblk0")
            for values in (
                records("/dev/mmcblk1p1"),
                records(boot_fs="btrfs"),
                records(system="/dev/loop0p2"),
            ):
                with (
                    patch.object(r, "run", side_effect=values),
                    self.assertRaises(ValueError),
                ):
                    r.production_boot_guard(sysfs)
            kind.write_text("MMC\n")
            with (
                patch.object(r, "run", side_effect=records()),
                self.assertRaisesRegex(ValueError, "non-SD"),
            ):
                r.production_boot_guard(sysfs)

    def test_stock_hook_built_or_installed_modules_do_not_trigger_false_failure(self):
        for state, expected in (
            ("installed", []),
            ("built", ["install"]),
            (None, ["build", "install"]),
        ):
            calls = []

            def command(*args):
                calls.append(args)
                return (
                    "fascia-panel/1.0, test-ARCH, armv7l: " + state
                    if args[1] == "status" and state
                    else ""
                )

            with self.subTest(state=state), patch.object(r, "run", side_effect=command):
                r.ensure_dkms("fascia-panel", "1.0", "test-ARCH")
                self.assertEqual([item[1] for item in calls[1:]], expected)

    def test_privileged_staging_rejects_symlink_and_writable_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            boundary = Path(tmp)
            stage = r.protected_staging(boundary)
            self.assertEqual(stage.stat().st_mode & 0o777, 0o700)
            stage.rmdir()
            stage.symlink_to(boundary)
            with self.assertRaisesRegex(ValueError, "unsafe privileged"):
                r.protected_staging(boundary)
            stage.unlink()
            (boundary / "fascia-system").chmod(0o777)
            with self.assertRaisesRegex(ValueError, "unsafe privileged"):
                r.protected_staging(boundary)

    def test_removal_guard_rejects_remove_without_affecting_upgrades(self):
        hook = Path(__file__).with_name("00-fascia-board-protect.hook").read_text()
        operations = [
            line.split("=", 1)[1].strip()
            for line in hook.splitlines()
            if line.startswith("Operation =")
        ]
        self.assertEqual(operations, ["Remove"])
        self.assertNotIn("Upgrade", operations)
        self.assertIn("AbortOnFail", hook)
        for package in (
            "linux-armv7",
            "linux-armv7-headers",
            "fascia-board-support-dkms",
            "dkms",
            "busybox",
            "btrfs-progs",
            "python",
        ):
            self.assertIn("Target = " + package + "\n", hook)
        with (
            patch.object(r.os, "geteuid", return_value=0),
            patch("sys.argv", ["refresh", "--refuse-removal"]),
            patch.object(r, "guarded_refresh") as refresh,
        ):
            self.assertEqual(r.main(), 1)
            refresh.assert_not_called()

    def test_bad_artifact_rejected_before_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad"
            p.write_bytes(b"not a kernel")
            with self.assertRaises(ValueError):
                r.validate_artifacts(p, p, p)


class SourceIntegrityTests(unittest.TestCase):
    def test_changed_retained_wifi_archive_rejected_before_extraction(self):
        spec = importlib.util.spec_from_file_location(
            "package_builder", Path(__file__).with_name("build-package.py")
        )
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "wifi.tar"
            archive.write_bytes(b"changed archive")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                builder.verify_archive(archive, builder.WIFI_SHA256)

    def test_fakeroot_metadata_failure_rejected_before_install(self):
        import io
        import tarfile

        spec = importlib.util.spec_from_file_location(
            "package_builder", Path(__file__).with_name("build-package.py")
        )
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "bad.tar"
            with tarfile.open(package, "w") as archive:
                entry = tarfile.TarInfo("usr/src/fascia-panel-1.0/dkms.conf")
                entry.uid = 65534
                entry.mode = 0o666
                entry.size = 1
                archive.addfile(entry, io.BytesIO(b"x"))
            with self.assertRaisesRegex(ValueError, "ownership/mode"):
                builder.validate_package(package)


if __name__ == "__main__":
    unittest.main()
