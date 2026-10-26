#!/usr/bin/env python3
"""Offline root finalization tests. No mounts, services, ARM execution or devices."""

import importlib.util
import fnmatch
import re
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock


def load(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), Path(__file__).with_name(name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load("install-root")
state = load("fascia-state-init")


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.target = self.base / "root"
        (self.target / "etc/ssh").mkdir(parents=True)
        (self.target / installer.MARKER).write_text("isolated\n")
        (self.target / "etc/passwd").write_text(
            "root:x:0:0:root:/root:/bin/bash\nalarm:x:1000:1000::/home/alarm:/bin/bash\n"
        )
        (self.target / "etc/shadow").write_text(
            "root:factoryhash:0:0:99999:7:::\nalarm:factoryhash:0:0:99999:7:::\n"
        )
        (self.target / "etc/pacman.conf").write_text(
            "[options]\nIgnorePkg = linux-armv7 linux-armv7-headers linux-firmware*\n"
            "NoExtract = usr/lib/firmware/* !usr/lib/firmware/regulatory.db*\n"
            "[core]\nInclude = /build/mirrorlist\n"
        )
        (self.target / "etc/group").write_text(
            "root:x:0:\nalarm:x:1000:\nwheel:x:998:alarm\nvideo:x:983:alarm\n"
        )
        (self.target / "etc/ssh/ssh_host_ed25519_key").write_text("private factory key")
        (self.target / "build/project").mkdir(parents=True)
        (self.target / "build/project/private-build-input").write_text("must not ship")
        (self.target / "var/cache/pacman/pkg").mkdir(parents=True)
        (self.target / "var/cache/pacman/pkg/archive.pkg.tar.xz").write_bytes(b"cache")
        self.release = self.base / "release"
        elf = bytearray(52)
        elf[:7] = b"\x7fELF\x01\x01\x01"
        elf[18:20] = b"\x28\x00"
        for name in (
            "fascia-host",
            "fascia-brightness",
            "fascia-home-button",
        ):
            self.release_file("bin/" + name, elf)
        for app in ("clock", "counter"):
            self.release_file("apps/" + app + "/AppRun", elf)
            (self.release / "apps" / app / "AppRun").chmod(0o755)
            self.release_file(
                "apps/" + app + "/" + app + ".desktop",
                (
                    f"[Desktop Entry]\nType=Application\nName={app}\nExec=AppRun\nIcon={app}\nX-Fascia-Version=0.1.0\n"
                ).encode(),
            )
            self.release_file("apps/" + app + "/" + app + ".png", b"\x89PNG\r\n\x1a\n")
            self.release_file("apps/" + app + "/.DirIcon", b"\x89PNG\r\n\x1a\n")
        self.initial_apps = self.base / "initial-apps"
        self.initial_apps.mkdir()
        (self.release / "apps").rename(self.initial_apps / "apps")
        shutil.copyfile(
            installer.PROJECT / "image/initial-apps.json",
            self.initial_apps / "manifest.json",
        )
        for name in (
            "fascia",
            "fascia-app",
            "fascia-system",
            "fascia-setup",
            "fascia-serial-session",
        ):
            shutil.copyfile(
                installer.PROJECT / "tools" / (name + ".py"),
                self.release_file("tools/" + name + ".py", b""),
            )
        for name in (
            "fascia-display",
            "fascia-brightness.service",
            "fascia-home-button.service",
            "fascia-device-state.service",
            "profile.d/90-fascia-serial.sh",
        ):
            shutil.copyfile(
                installer.PROJECT / "scripts" / name,
                self.release_file("scripts/" + name, b""),
            )
        self.release_file("share/fonts/DejaVuSans.ttf", b"fixture font")
        self.release_file("share/licenses/test/LICENSE", b"fixture license")
        shutil.copyfile(
            installer.PROJECT / "system/40-fascia-volatile.conf",
            self.release_file("system/40-fascia-volatile.conf", b""),
        )

    def release_file(self, path, data):
        target = self.release / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def test_unmarked_or_live_roots_refused_before_write(self):
        with self.assertRaises(ValueError):
            installer.Root("/")
        (self.target / installer.MARKER).unlink()
        original = (self.target / "etc/passwd").read_bytes()
        with self.assertRaises(FileNotFoundError):
            installer.Root(self.target, ownership=False)
        self.assertEqual((self.target / "etc/passwd").read_bytes(), original)

    def test_os_release_rejects_standalone_debug_executables(self):
        for name in ("fascia-home", "fascia-compositor"):
            with self.subTest(name=name):
                path = self.release / "bin" / name
                shutil.copyfile(self.release / "bin/fascia-host", path)
                with self.assertRaisesRegex(ValueError, "Standalone debug"):
                    installer.validate_release(self.release)
                path.unlink()

    def test_public_commands_are_installed_without_legacy_aliases(self):
        installer.finalize(installer.Root(self.target, ownership=False), self.release)
        self.assertTrue((self.target / "var/lib/systemd/linger/fascia").is_file())
        for name in ("app", "setup", "system", "display"):
            canonical = (
                "usr/bin/fascia-" + name
                if name in ("setup", "system")
                else "usr/local/bin/fascia-" + name
            )
            target = self.target / canonical
            self.assertTrue(target.is_file())
            self.assertTrue(target.stat().st_mode & 0o111)
            if name in ("setup", "system"):
                self.assertEqual(
                    os.readlink(self.target / ("usr/local/bin/fascia-" + name)),
                    "/" + canonical,
                )
                self.assertIn(
                    "exec sudo -n /usr/local/libexec/fascia-" + name + "-root",
                    target.read_text(),
                )
        self.assertIn(
            "/usr/local/bin/fascia-app",
            (self.target / "usr/local/bin/fascia").read_text(),
        )

    def test_integrated_host_only_release_and_display_launch(self):
        installer.finalize(installer.Root(self.target, ownership=False), self.release)
        self.assertTrue((self.target / "usr/lib/fascia/bin/fascia-host").is_file())
        self.assertFalse((self.target / "usr/lib/fascia/bin/fascia-home").exists())
        wrapper = (self.target / "usr/lib/fascia/run-home").read_text()
        self.assertIn("exec /usr/lib/fascia/bin/fascia-host --display", wrapper)
        self.assertNotIn("--home", wrapper)

    def test_os_only_root_needs_no_examples_and_removes_legacy_seed_payload(self):
        legacy = self.target / "usr/share/fascia/factory-apps/old-app/run"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"old payload")
        installer.finalize(installer.Root(self.target, ownership=False), self.release)
        self.assertFalse((self.target / "usr/share/fascia/factory-apps").exists())
        self.assertFalse(
            (self.target / "usr/share/fascia/image-defaults.json").exists()
        )
        self.assertTrue((self.target / "usr/lib/fascia/bin/fascia-host").is_file())

    def test_os_release_refuses_bundled_apps_and_initial_apps_must_be_removable(self):
        (self.release / "apps").mkdir()
        with self.assertRaisesRegex(ValueError, "OS releases must not contain apps"):
            installer.validate_release(self.release)

    def test_native_x86_release_refused_without_account_changes(self):
        bad = self.release / "bin/fascia-host"
        data = bytearray(bad.read_bytes())
        data[18:20] = b"\x3e\x00"
        bad.write_bytes(data)
        original = (self.target / "etc/passwd").read_bytes()
        with self.assertRaises(ValueError):
            installer.finalize(
                installer.Root(self.target, ownership=False), self.release
            )
        self.assertEqual((self.target / "etc/passwd").read_bytes(), original)

    def test_symlink_ancestor_never_writes_outside_prepared_root(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.target / "usr").symlink_to(outside)
        with self.assertRaises(ValueError):
            installer.Root(self.target, ownership=False).write("usr/bin/escape", b"bad")
        self.assertFalse(list(outside.iterdir()))

    def test_root_defaults_no_credentials_and_readonly_home_paths(self):
        root = installer.Root(self.target, ownership=False)
        installer.finalize(root, self.release, self.initial_apps)
        installer.finalize(
            root, self.release, self.initial_apps
        )  # repeat is safe on the offline root
        self.assertEqual((self.target / "etc/machine-id").read_bytes(), b"")
        self.assertEqual(
            json.loads((self.target / "etc/fascia-system.json").read_text()),
            {"board_id": "pf1007l-pl5", "shared_state_schema": 1},
        )
        self.assertFalse((self.target / "build").exists())
        self.assertFalse((self.target / "var/cache/pacman/pkg").exists())
        self.assertTrue((self.target / installer.MARKER).is_file())
        for directory in (
            "data",
            "system",
            "run",
            "var/lib/systemd/timesync",
            "var/lib/lastlog",
        ):
            self.assertTrue((self.target / directory).is_dir())
        self.assertFalse(list((self.target / "etc/ssh").glob("ssh_host_*")))
        shadow = (self.target / "etc/shadow").read_text()
        self.assertNotIn("factoryhash", shadow)
        self.assertIn("fascia:!::", shadow)
        self.assertNotIn("alarm", (self.target / "etc/passwd").read_text())
        groups = (self.target / "etc/group").read_text().splitlines()
        self.assertNotIn(
            "fascia", next(g for g in groups if g.startswith("wheel:")).split(":")[3]
        )
        journal = next(g for g in groups if g.startswith("systemd-journal:"))
        self.assertIn("fascia", journal.split(":")[3].split(","))
        self.assertEqual(
            (self.target / "etc/sudoers.d/fascia-home").stat().st_mode & 0o777, 0o440
        )
        pacman = (self.target / "etc/pacman.conf").read_text()
        self.assertNotIn("IgnorePkg", pacman)
        self.assertNotIn("NoExtract", pacman)
        self.assertTrue((self.target / "usr/lib/fascia/bin/fascia-host").is_file())
        self.assertFalse((self.target / "home/fascia/fascia-home").exists())
        unit = (self.target / "usr/lib/systemd/system/pf-kiosk.service").read_text()
        self.assertIn("LIBSEAT_BACKEND=seatd", unit)
        self.assertIn("/usr/lib/fascia/run-home", unit)
        self.assertIn(
            "exec /usr/lib/fascia/bin/fascia-host",
            (self.target / "usr/lib/fascia/run-home").read_text(),
        )
        self.assertFalse(
            (
                self.target / "etc/systemd/system/multi-user.target.wants/sshd.service"
            ).is_symlink()
        )
        self.assertFalse(
            (
                self.target
                / "etc/systemd/system/multi-user.target.wants/fascia-home-button.service"
            ).is_symlink()
        )
        fstab = (self.target / "etc/fstab").read_text()
        self.assertTrue(
            all(not line or line.startswith("#") for line in fstab.splitlines())
        )
        self.assertIn(
            "/run/fascia-home 0755 fascia fascia",
            (self.target / "etc/tmpfiles.d/fascia.conf").read_text(),
        )
        self.assertTrue(
            (self.target / "usr/share/fascia/factory-apps/apps/clock/AppRun").is_file()
        )

    def test_writable_arch_keeps_package_maintenance_and_runtime_networking(self):
        installer.finalize(installer.Root(self.target, ownership=False), self.release)
        linger = self.target / "var/lib/systemd/linger"
        self.assertTrue(linger.is_dir())
        self.assertEqual(["fascia"], [path.name for path in linger.iterdir()])
        self.assertEqual(linger.stat().st_mode & 0o777, 0o755)
        for unit in (
            "systemd-networkd-persistent-storage.service",
            "systemd-backlight@.service",
            "systemd-rfkill.service",
            "systemd-rfkill.socket",
            "systemd-pstore.service",
        ):
            self.assertEqual(
                os.readlink(self.target / "etc/systemd/system" / unit), "/dev/null"
            )
        for unit in (
            "systemd-random-seed.service",
            "archlinux-keyring-wkd-sync.timer",
            "shadow.timer",
        ):
            self.assertFalse((self.target / "etc/systemd/system" / unit).is_symlink())
        self.assertIn(
            "/usr/bin/pacman *",
            (self.target / "etc/sudoers.d/fascia-home").read_text(),
        )
        self.assertTrue((self.target / "usr/lib/fascia/build-initramfs").is_file())
        self.assertTrue((self.target / "usr/share/fascia/initramfs/init").is_file())
        for unit in ("fascia-grow-storage.service", "fascia-pacman-init.service"):
            self.assertTrue(
                (
                    self.target / "etc/systemd/system/multi-user.target.wants" / unit
                ).is_symlink()
            )
        self.assertEqual(
            os.readlink(
                self.target / "etc/systemd/system-generators/systemd-ssh-generator"
            ),
            "/dev/null",
        )
        self.assertFalse((self.target / "etc/systemd/system/sshd.service").is_symlink())
        # Disable persistence helpers, not networking, logind or Fascia's own restore.
        for unit in ("systemd-networkd.service", "systemd-logind.service"):
            path = self.target / "etc/systemd/system" / unit
            self.assertFalse(path.is_symlink() and os.readlink(path) == "/dev/null")
        for unit in ("systemd-networkd.service", "fascia-device-state.service"):
            self.assertTrue(
                (
                    self.target / "etc/systemd/system/multi-user.target.wants" / unit
                ).is_symlink()
            )

    def test_board_defaults_keep_touch_after_firmware_and_restore_public_mirror(self):
        installer.finalize(installer.Root(self.target, ownership=False), self.release)
        modules = (self.target / "etc/modules-load.d/pf1007l-display.conf").read_text()
        self.assertEqual(
            [
                line
                for line in modules.splitlines()
                if line and not line.startswith("#")
            ],
            ["panel-jadard-jd9365da-h3", "pwm-sun4i", "sun6i_mipi_dsi", "sun4i_drm"],
        )
        self.assertNotIn("silead", modules.lower())
        softdeps = (self.target / "etc/modprobe.d/pf1007l-display.conf").read_text()
        self.assertIn("softdep sun6i_mipi_dsi pre: panel-jadard-jd9365da-h3", softdeps)
        self.assertIn("softdep sun4i-drm pre: pwm-sun4i", softdeps)
        touch = (self.target / "etc/udev/rules.d/90-pf1007l-touch.rules").read_text()
        self.assertIn('ATTRS{name}=="silead_ts"', touch)
        self.assertIn('ENV{LIBINPUT_CALIBRATION_MATRIX}="0 1 0 1 0 0"', touch)
        wifi = (self.target / "etc/modprobe.d/pf1007l-wifi.conf").read_text()
        self.assertIn(
            "aic_fw_path=/run/fascia-firmware/aic8800_fw/SDIO/aic8800DC", wifi
        )
        pacman = (self.target / "etc/pacman.conf").read_text()
        self.assertIn("Include = /etc/pacman.d/mirrorlist", pacman)
        self.assertNotIn("/build/", pacman)
        self.assertNotIn("IgnorePkg", pacman)

    def test_late_emmc_readonly_rule_excludes_sd_and_other_block_devices(self):
        installer.finalize(installer.Root(self.target, ownership=False), self.release)
        path = self.target / "etc/udev/rules.d/55-fascia-emmc-readonly.rules"
        rule = path.read_text()
        pattern = re.search(r'ENV\{DEVPATH\}=="([^"]+)"', rule).group(1)
        internal = (
            "/devices/platform/soc/1c11000.mmc/mmc_host/mmc2/mmc2:0001/block/mmcblk2"
        )
        self.assertTrue(fnmatch.fnmatchcase(internal, pattern))
        self.assertTrue(fnmatch.fnmatchcase(internal + "/mmcblk2p7", pattern))
        self.assertFalse(
            fnmatch.fnmatchcase(internal.replace("1c11000.mmc", "1c0f000.mmc"), pattern)
        )
        self.assertFalse(fnmatch.fnmatchcase("/devices/virtual/block/loop0", pattern))
        self.assertIn('SUBSYSTEM=="block"', rule)
        self.assertIn('KERNEL=="mmcblk*"', rule)
        self.assertIn("--setro /dev/%k", rule)
        self.assertNotIn("--setrw", rule)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_unexpected_personal_account_and_factory_symlink_refused(self):
        with (self.target / "etc/passwd").open("a") as stream:
            stream.write("someone:x:1001:1001::/home/someone:/bin/bash\n")
        with self.assertRaises(ValueError):
            installer.finalize(
                installer.Root(self.target, ownership=False), self.release
            )
        (self.initial_apps / "apps/clock/private").symlink_to("/etc/passwd")
        with self.assertRaises(ValueError):
            installer.validate_initial_apps(self.initial_apps)

    def test_first_boot_seed_is_once_and_preserves_user_removal_and_settings(self):
        root, home = self.base / "state", self.base / "home"
        user = mock.Mock(pw_uid=os.getuid(), pw_gid=os.getgid())
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if "install" in argv:
                app = Path(argv[-1]).name
                (root / "apps" / app / "active").mkdir(parents=True)
            else:
                (root / "settings.json").write_text('{"default_app":"clock"}')

        with mock.patch.object(state.pwd, "getpwnam", return_value=user):
            state.initialize(root, self.initial_apps, run=run, home=home)
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[-1][-4:], ["default", "clock", "--delay", "10"])
            shutil.rmtree(root / "apps/counter")
            (root / "settings.json").write_text("user setting")
            calls.clear()
            state.initialize(root, self.initial_apps, run=run, home=home)
            self.assertEqual(calls, [])
            self.assertFalse((root / "apps/counter").exists())
            self.assertEqual((root / "settings.json").read_text(), "user setting")

    def test_os_without_initial_payload_boots_without_installing_apps(self):
        root, home = self.base / "state", self.base / "home"
        user = mock.Mock(pw_uid=os.getuid(), pw_gid=os.getgid())
        run = mock.Mock()
        with mock.patch.object(state.pwd, "getpwnam", return_value=user):
            state.initialize(root, self.base / "no-initial-apps", run=run, home=home)
        run.assert_not_called()
        self.assertFalse((root / "settings.json").exists())
        self.assertEqual(
            json.loads((root / ".factory-seeded.json").read_text()),
            {"schema_version": 1, "apps": []},
        )

    def test_generic_seed_and_old_marker_survive_os_update_without_payload(self):
        root, home = self.base / "state", self.base / "home"
        user = mock.Mock(pw_uid=os.getuid(), pw_gid=os.getgid())
        package = self.base / "another-image"
        package.mkdir()
        plan = {
            "schema_version": 1,
            "apps": ["planetarium"],
            "default_app": "planetarium",
            "startup_delay_seconds": 15,
        }
        (package / "manifest.json").write_text(json.dumps(plan))
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if "install" in argv:
                (root / "apps/planetarium/active").mkdir(parents=True)
            else:
                (root / "settings.json").write_text("user settings")

        with mock.patch.object(state.pwd, "getpwnam", return_value=user):
            state.initialize(root, package, run=run, home=home)
            self.assertEqual(
                calls[-1][-4:], ["default", "planetarium", "--delay", "15"]
            )
            shutil.rmtree(root / "apps/planetarium")
            calls.clear()
            # An OS deployment carries no first-image payload. Never consult it
            # after initialization, even if an unrelated image manifest appears.
            (package / "manifest.json").write_text("invalid newer payload")
            state.initialize(root, package, run=run, home=home)
            state.initialize(
                root, self.base / "new-os-without-apps", run=run, home=home
            )
        self.assertFalse((root / "apps/planetarium").exists())
        self.assertEqual(calls, [])
        self.assertEqual((root / "settings.json").read_text(), "user settings")

    def test_initial_manifest_rejects_traversal_duplicates_and_unknown_defaults(self):
        for apps, default in [
            (["../escape"], None),
            (["clock", "clock"], "clock"),
            (["clock"], "missing"),
        ]:
            value = {
                "schema_version": 1,
                "apps": apps,
                "default_app": default,
                "startup_delay_seconds": 10,
            }
            (self.initial_apps / "manifest.json").write_text(json.dumps(value))
            with (
                self.subTest(apps=apps, default=default),
                self.assertRaises(ValueError),
            ):
                state.read_plan(self.initial_apps)

    def test_failed_seed_retries_without_resetting_existing_app(self):
        root, home = self.base / "state", self.base / "home"
        user = mock.Mock(pw_uid=os.getuid(), pw_gid=os.getgid())
        (root / "apps/clock/active").mkdir(parents=True)
        with mock.patch.object(state.pwd, "getpwnam", return_value=user):
            with self.assertRaises(OSError):
                state.initialize(
                    root,
                    self.initial_apps,
                    run=mock.Mock(side_effect=OSError("interrupted")),
                    home=home,
                )
        self.assertFalse((root / ".factory-seeded.json").exists())
        self.assertTrue((root / "apps/clock/active").is_dir())


if __name__ == "__main__":
    unittest.main()
