#!/usr/bin/env python3
"""Device helper checks using a temporary backlight and settings directory."""

import importlib.util
import json
import io
import contextlib
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "fascia_system", Path(__file__).with_name("fascia-system.py")
)
system = importlib.util.module_from_spec(spec)
spec.loader.exec_module(system)


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.backlight = root / "backlight"
        self.backlight.mkdir()
        (self.backlight / "max_brightness").write_text("255")
        (self.backlight / "brightness").write_text("128")
        self.settings = root / "settings" / "device.json"
        self.patch = mock.patch.multiple(
            system,
            BACKLIGHT=self.backlight,
            SETTINGS=self.settings,
            BRIGHTNESS_SOCKET=root / "brightness.sock",
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_shutdown_uses_normal_systemd_poweroff(self):
        with (
            mock.patch.object(sys, "argv", ["fascia-system", "shutdown"]),
            mock.patch.object(system.subprocess, "run") as run,
        ):
            system.main()
        run.assert_called_once_with(["/usr/bin/systemctl", "poweroff"], check=True)
        with (
            mock.patch.object(sys, "argv", ["fascia-system", "shutdown", "--force"]),
            mock.patch.object(system.subprocess, "run") as run,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            system.main()
        run.assert_not_called()

    def test_bounds_never_black_out_the_screen(self):
        for percent, expected in ((5, 13), (100, 255)):
            self.assertEqual(system.brightness(percent), expected)
            self.assertEqual(int((self.backlight / "brightness").read_text()), expected)
        for percent in (-1, 0, 101):
            with self.assertRaises(ValueError):
                system.brightness(percent)
        self.assertEqual((self.backlight / "brightness").read_text(), "255")

    def test_failed_replace_preserves_saved_settings_and_cleans_temporary_file(self):
        old = {"schema_version": 1, "brightness": 70}
        system.save(old)
        with mock.patch.object(system.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                system.save({"schema_version": 1, "brightness": 80})
        self.assertEqual(json.loads(self.settings.read_text()), old)
        self.assertEqual(list(self.settings.parent.iterdir()), [self.settings])
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)

    def test_restore_uses_saved_value(self):
        system.save({"schema_version": 1, "brightness": 50})
        with mock.patch("sys.argv", ["fascia-system", "restore"]):
            system.main()
        self.assertEqual((self.backlight / "brightness").read_text(), "128")

    def test_every_coarse_level_survives_save_restore_exactly(self):
        (self.backlight / "max_brightness").write_text("8")
        for level in range(1, 9):
            system.brightness_level(level)
            system.save_brightness(level)
            saved = json.loads(self.settings.read_text())
            (self.backlight / "brightness").write_text("8")
            system.restore_brightness(saved)
            self.assertEqual(int((self.backlight / "brightness").read_text()), level)
        for level in (0, -1, 9, True, 3.5):
            with self.assertRaises(ValueError):
                system.brightness_level(level)
        self.assertEqual((self.backlight / "brightness").read_text(), "8")

    def test_saved_level_migrates_to_a_changed_range_without_percent_rounding(self):
        system.restore_brightness(
            {"schema_version": 1, "brightness_level": 3, "brightness_maximum": 8}
        )
        self.assertEqual((self.backlight / "brightness").read_text(), "96")
        (self.backlight / "max_brightness").write_text("8")
        system.restore_brightness(
            {"schema_version": 1, "brightness_level": 1, "brightness_maximum": 255}
        )
        self.assertEqual((self.backlight / "brightness").read_text(), "1")

    def test_corrupt_level_and_unknown_range_never_change_backlight(self):
        for bad in (
            {"schema_version": 1, "brightness_level": 0, "brightness_maximum": 8},
            {"schema_version": 1, "brightness_level": True, "brightness_maximum": 8},
            {"schema_version": 1, "brightness_level": 2},
            {"schema_version": 1, "brightness": True},
        ):
            with self.assertRaises(ValueError):
                system.restore_brightness(bad)
        self.assertEqual((self.backlight / "brightness").read_text(), "128")
        (self.backlight / "max_brightness").write_text("0")
        with self.assertRaises(ValueError):
            system.brightness_level(1)

    def test_running_service_is_the_only_writer_for_cli_changes(self):
        with mock.patch.object(
            system, "request_brightness", return_value=True
        ) as request:
            self.assertEqual(system.change_brightness(3), 3)
        request.assert_called_once_with(3, durable=True)
        self.assertEqual((self.backlight / "brightness").read_text(), "128")
        self.assertFalse(self.settings.exists())

    def test_service_failure_never_falls_back_to_a_competing_writer(self):
        with mock.patch.object(
            system, "request_brightness", side_effect=OSError("busy")
        ):
            with self.assertRaises(OSError):
                system.change_brightness(3)
        self.assertEqual((self.backlight / "brightness").read_text(), "128")
        self.assertFalse(self.settings.exists())

    def test_physical_home_start_failure_keeps_overlay_configuration_unchanged(self):
        binary = self.backlight / "fascia-home-button"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        override = self.settings.parent / "physical-home.conf"
        with (
            mock.patch.multiple(system, BUTTON_BINARY=binary, BUTTON_OVERRIDE=override),
            mock.patch.object(
                system.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, "systemctl"),
            ) as run,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                system.physical_home(True)
            self.assertFalse(override.exists())
            run.assert_called_once_with(
                ["systemctl", "start", "fascia-home-button.service"], check=True
            )

    def test_physical_home_uses_connected_device_fallback_and_can_be_disabled(self):
        binary = self.backlight / "fascia-home-button"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        override = self.settings.parent / "physical-home.conf"
        with (
            mock.patch.multiple(system, BUTTON_BINARY=binary, BUTTON_OVERRIDE=override),
            mock.patch.object(system.subprocess, "run") as run,
        ):
            system.physical_home(True)
            self.assertIn("FASCIA_HOME_OVERLAY=auto", override.read_text())
            self.assertNotIn("FASCIA_HOME_OVERLAY=0", override.read_text())
            self.assertLess(
                run.call_args_list.index(
                    mock.call(
                        ["systemctl", "start", "fascia-home-button.service"], check=True
                    )
                ),
                run.call_args_list.index(
                    mock.call(["systemctl", "restart", "pf-kiosk.service"], check=True)
                ),
            )
            system.physical_home(False)
            self.assertFalse(override.exists())
            run.assert_any_call(
                ["systemctl", "disable", "--now", "fascia-home-button.service"],
                check=True,
            )

    def test_display_adjustment_uses_only_fixed_paths_and_home(self):
        override = self.settings.parent / "display.conf"
        home = self.settings.parent / "fascia-home"
        home.parent.mkdir(parents=True)
        home.write_text("#!/bin/sh\n")
        home.chmod(0o755)
        with (
            mock.patch.multiple(system, DISPLAY_OVERRIDE=override, HOME_BINARY=home),
            mock.patch.object(system.subprocess, "run") as run,
        ):
            system.display_adjustment(True)
            self.assertIn("FASCIA_HOME_DISPLAY=1", override.read_text())
            self.assertNotIn("FASCIA_CONCEPT", override.read_text())
            self.assertEqual(
                [
                    mock.call(["systemctl", "daemon-reload"], check=True),
                    mock.call(["systemctl", "restart", "pf-kiosk.service"], check=True),
                ],
                run.call_args_list,
            )
            system.display_adjustment(False)
            self.assertFalse(override.exists())
            home.unlink()
            with self.assertRaises(ValueError):
                system.display_adjustment(True)

    def test_physical_home_optin_is_durable_only_in_identity_config(self):
        root = self.settings.parent / "identity"
        root.mkdir(parents=True)
        with mock.patch.object(system, "IDENTITY_ROOT", root):
            system.persist_physical_home(True)
            self.assertTrue(
                json.loads((root / "config.json").read_text())["physical_home"]
            )
            system.persist_physical_home(False)
            self.assertFalse(
                json.loads((root / "config.json").read_text())["physical_home"]
            )

    def test_updates_delegate_only_fixed_allowed_commands(self):
        bundle = self.settings.parent / "local bundle;not-a-command"
        bundle.mkdir(parents=True)
        with mock.patch.object(system.subprocess, "run") as run:
            system.system_update("status")
            system.system_update("rollback")
            self.assertEqual(
                run.call_args_list,
                [
                    mock.call(
                        ["/usr/local/libexec/fascia-system-update", "status"],
                        check=True,
                    ),
                    mock.call(
                        ["/usr/local/libexec/fascia-system-update", "rollback"],
                        check=True,
                    ),
                ],
            )

    def test_updates_reject_internal_verbs_and_argument_injection(self):
        with mock.patch.object(system.subprocess, "run") as run:
            for verb, path in [
                ("initialize", None),
                ("health", None),
                ("confirm", "x"),
                ("select", None),
                ("stage", None),
                ("stage", "relative/path"),
                ("stage", "/missing/bundle"),
                ("stage", "/tmp/ok\nreboot"),
                ("status", "/tmp/extra"),
                ("rollback", "--systems=/tmp"),
            ]:
                with self.assertRaises(ValueError, msg=str((verb, path))):
                    system.system_update(verb, path)
            run.assert_not_called()

    def test_update_cli_does_not_forward_updater_flags(self):
        with (
            mock.patch(
                "sys.argv",
                [
                    "fascia-system",
                    "update",
                    "stage",
                    "/tmp/bundle",
                    "--systems",
                    "/tmp/elsewhere",
                ],
            ),
            mock.patch.object(system.subprocess, "run") as run,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as failed:
                system.main()
            self.assertEqual(failed.exception.code, 2)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
