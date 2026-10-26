#!/usr/bin/env python3
import base64
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import subprocess
import json

spec = importlib.util.spec_from_file_location(
    "setup", Path(__file__).with_name("fascia-setup.py")
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def public_key():
    kind = b"ssh-ed25519"
    blob = (
        struct.pack(">I", len(kind)) + kind + struct.pack(">I", 32) + bytes(range(32))
    )
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


class FakeOps:
    def __init__(self):
        self.calls = []
        self.name = "fascia"
        self.fail_wifi = False
        self.networks = []

    def get_hostname(self):
        return self.name

    def set_hostname(self, value):
        self.calls.append(("hostname", value))
        self.name = value

    def get_timezone(self):
        return "UTC"

    def set_timezone(self, value):
        self.calls.append(("timezone", value))

    def set_time(self, value):
        self.calls.append(("time", value))

    def scan_wifi(self):
        return self.networks

    def connect_wifi(self, value):
        self.calls.append(("wifi", value))
        if self.fail_wifi:
            raise RuntimeError("connection failed")

    def add_key(self, value):
        self.calls.append(("key", setup.normalize_key(value)))
        return True

    def key_count(self):
        return sum(c[0] == "key" for c in self.calls)

    def connect_wifi_touch(self, name, password):
        self.connect_wifi(name)

    def enable_ssh(self):
        self.calls.append(("ssh", True))

    def disable_ssh(self):
        self.calls.append(("ssh", False))

    def snapshot(self):
        return dict(
            hostname=self.name,
            mdns_service_active=False,
            ssh_active=False,
            addresses=[],
            ssh_host_fingerprint=None,
        )


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "state" / "setup.json"
        self.ops = FakeOps()
        self.messages = []

    def wizard(self, answers, progress=None):
        iterator = iter(answers)
        return setup.Wizard(
            progress or setup.Progress(self.path),
            self.ops,
            lambda _: next(iterator),
            self.messages.append,
        )

    def test_success_is_durable_without_credentials_in_progress(self):
        wizard = self.wizard(["study-frame", "UTC", "", "My WiFi", public_key(), "y"])
        wizard.resume()
        progress = setup.Progress(self.path)
        self.assertTrue(progress.data["completed"])
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        saved = self.path.read_text()
        self.assertNotIn("WiFi", saved)
        self.assertNotIn(public_key(), saved)
        self.assertEqual(
            [
                ("hostname", "study-frame"),
                ("timezone", "UTC"),
                ("wifi", "My WiFi"),
                ("key", public_key()),
                ("ssh", True),
            ],
            self.ops.calls,
        )

    def test_disconnect_resumes_only_unfinished_stages(self):
        answers = iter(["study-frame"])

        def ask(_):
            try:
                return next(answers)
            except StopIteration:
                raise EOFError()

        wizard = setup.Wizard(
            setup.Progress(self.path), self.ops, ask, self.messages.append
        )
        with self.assertRaises(EOFError):
            wizard.resume()
        self.assertTrue(setup.Progress(self.path).done("hostname"))
        self.wizard(["", "", "", "", "n"]).resume()
        self.assertEqual(1, sum(c[0] == "hostname" for c in self.ops.calls))
        self.assertFalse(any(c[0] == "ssh" for c in self.ops.calls))

    def test_wifi_failure_never_advances(self):
        self.ops.fail_wifi = True
        with self.assertRaises(RuntimeError):
            self.wizard(["", "UTC", "", "Network"]).resume()
        progress = setup.Progress(self.path)
        self.assertTrue(progress.done("hostname"))
        self.assertFalse(progress.done("wifi"))
        self.ops.fail_wifi = False
        self.wizard(["", "", "n"]).resume()
        self.assertTrue(setup.Progress(self.path).data["completed"])

    def test_skip_offline_and_keep_ssh_does_not_change_access(self):
        self.wizard(["", "UTC", "", "", "", ""]).resume()
        self.assertEqual([("hostname", "fascia"), ("timezone", "UTC")], self.ops.calls)
        self.assertTrue(any("disabled / inactive" in m for m in self.messages))
        self.assertTrue(any("mDNS service active: no" in m for m in self.messages))

    def test_corrected_inputs_and_invalid_choice_do_not_repeat_system_changes(self):
        self.wizard(
            [
                "bad.local",
                "study",
                "Not/AZone",
                "UTC",
                "",
                "",
                "not a key",
                public_key(),
                "maybe",
                "y",
            ]
        ).resume()
        self.assertEqual(1, sum(c[0] == "hostname" for c in self.ops.calls))
        self.assertEqual(1, sum(c[0] == "timezone" for c in self.ops.calls))
        self.assertEqual(1, sum(c[0] == "key" for c in self.ops.calls))
        self.assertEqual(1, sum(c[0] == "ssh" for c in self.ops.calls))
        self.assertTrue(setup.Progress(self.path).data["completed"])

    def test_resumed_wizard_displays_saved_progress(self):
        progress = setup.Progress(self.path)
        progress.advance("hostname")
        self.wizard(["UTC", "", "", "", "n"], progress).resume()
        text = "\n".join(self.messages)
        self.assertIn("Resuming with 1 of 5 steps saved", text)
        self.assertIn("[#>...]  2 / 5", text)
        self.assertNotIn("Name your frame", text)

    def test_connection_card_uses_actual_status_and_copyable_commands(self):
        self.ops.snapshot = lambda: dict(
            hostname="study",
            mdns_service_active=True,
            ssh_active=True,
            addresses=[{"interface": "wlan0", "address": "192.168.1.42"}],
            ssh_host_fingerprint="SHA256:test",
        )
        self.wizard([]).summary()
        text = "\n".join(self.messages)
        self.assertIn("Wi-Fi IPv4: 192.168.1.42", text)
        self.assertIn("ssh fascia@study.local", text)
        self.assertIn("ssh fascia@192.168.1.42", text)
        self.assertIn("not verified", text)
        self.assertIn("SHA256:test", text)
        self.messages.clear()
        self.ops.snapshot = FakeOps().snapshot
        self.wizard([]).summary()
        self.assertFalse(any("ssh fascia@" in line for line in self.messages))
        self.assertTrue(any("not connected" in line for line in self.messages))

    def test_terminal_styling_fallback_and_status_sanitization(self):
        with mock.patch.object(setup.sys.stdout, "isatty", return_value=True):
            with mock.patch.dict(setup.os.environ, {"TERM": "vt100"}, clear=True):
                self.assertIn("\x1b[", setup.Terminal().paint("Setup"))
                self.assertNotIn("\x1b[", setup.Terminal(plain=True).paint("Setup"))
            for environment in ({"TERM": "dumb"}, {"TERM": "vt100", "NO_COLOR": ""}):
                with mock.patch.dict(setup.os.environ, environment, clear=True):
                    self.assertNotIn("\x1b[", setup.Terminal().paint("Setup"))
        lines = []
        terminal = setup.Terminal(lines.append, plain=True)
        terminal.width = 40
        terminal.card("Connections", ["host\x1b[2J\r\n" + "x" * 100])
        self.assertTrue(all(len(line) <= 40 for line in lines))
        self.assertNotIn("\x1b", "\n".join(lines))

    def test_wifi_scan_selection_rescan_and_manual_numeric_name(self):
        self.ops.networks = [
            {"name": " Studio ", "dbm": -48, "security": "psk", "connected": True}
        ]
        with mock.patch.object(self.ops, "scan_wifi", wraps=self.ops.scan_wifi) as scan:
            self.wizard(["r", "99", "1"]).stage_wifi()
        self.assertEqual(2, scan.call_count)
        self.assertEqual([("wifi", " Studio ")], self.ops.calls)
        text = "\n".join(self.messages)
        self.assertIn("[####] -48 dBm", text)
        self.assertIn("connected", text)
        self.assertIn("Choose a listed number", text)
        self.wizard(["m", "1234"]).stage_wifi()
        self.assertEqual(("wifi", "1234"), self.ops.calls[-1])

    def test_wifi_scan_failure_allows_manual_entry_and_offline_skip(self):
        with mock.patch.object(self.ops, "scan_wifi", side_effect=TimeoutError("scan")):
            self.wizard(["Manual network"]).stage_wifi()
            self.wizard([""]).stage_wifi()
        self.assertEqual([("wifi", "Manual network")], self.ops.calls)
        self.assertTrue(any("Scan unavailable" in line for line in self.messages))

    def test_wifi_scan_uses_typed_dbus_results_and_waits_for_completion(self):
        station = "/net/connman/iwd/0/8"
        network = station + "/one"

        def variant(data):
            return {"data": data}

        objects = {
            station: {
                "net.connman.iwd.Device": {"Name": variant("wlan0")},
                "net.connman.iwd.Station": {
                    "State": variant("connected"),
                    "ConnectedNetwork": variant(network),
                },
            },
            network: {
                "net.connman.iwd.Network": {
                    "Name": variant("Room with spaces"),
                    "Type": variant("psk"),
                }
            },
        }
        ops = setup.SystemOps.__new__(setup.SystemOps)
        with (
            mock.patch.object(ops, "wifi_objects", return_value=objects),
            mock.patch.object(
                ops, "iwd", side_effect=[None, True, False, [[network, -4850]]]
            ) as bus,
            mock.patch.object(setup.time, "sleep"),
        ):
            networks = ops.scan_wifi()
        self.assertEqual(
            [
                {
                    "name": "Room with spaces",
                    "security": "psk",
                    "dbm": -48,
                    "connected": True,
                }
            ],
            networks,
        )
        self.assertEqual("Scan", bus.call_args_list[0].args[-1])
        self.assertEqual("Scanning", bus.call_args_list[1].args[-1])
        with (
            mock.patch.object(ops, "wifi_objects", return_value=objects),
            mock.patch.object(ops, "iwd", side_effect=[None, True]),
            mock.patch.object(setup.time, "sleep"),
            mock.patch.object(setup.time, "monotonic", side_effect=[0, 13]),
        ):
            with self.assertRaises(TimeoutError):
                ops.scan_wifi()

    def test_busctl_json_invocation_is_bounded_and_password_free(self):
        reply = subprocess.CompletedProcess(
            [], 0, stdout='{"type":"a(on)","data":[[["/network",-4800]]]}'
        )
        with mock.patch.object(setup.subprocess, "run", return_value=reply) as run:
            self.assertEqual(
                [["/network", -4800]],
                setup.SystemOps.iwd(
                    "call",
                    "net.connman.iwd",
                    "/station",
                    "net.connman.iwd.Station",
                    "GetOrderedNetworks",
                ),
            )
        args, kwargs = run.call_args
        self.assertIn("--json=short", args[0])
        self.assertEqual(4, kwargs["timeout"])
        self.assertNotIn("passphrase", " ".join(args[0]))

    def test_touch_setup_shares_progress_without_persisting_request_secrets(self):
        progress = setup.Progress(self.path)

        def request(action, **fields):
            return setup.touch_request(
                self.ops, progress, dict(action=action, **fields)
            )

        request("hostname", value="study")
        request("timezone", value="Europe/London")
        request("wifi", value="Private network", password="FAKE-password")
        request("ssh", key=public_key(), enable=True)
        result = request("finish")
        self.assertTrue(result["completed"])
        self.assertTrue(all(progress.done(stage) for stage in setup.STAGES))
        self.assertNotIn("password", self.path.read_text())
        self.assertNotIn("Private network", self.path.read_text())
        self.assertNotIn(public_key(), self.path.read_text())

    def test_touch_password_access_toggle_and_optional_ssh_step(self):
        progress = setup.Progress(self.path)
        original = self.ops.snapshot
        self.ops.snapshot = lambda: dict(original(), ssh_auth="password")
        setup.touch_request(self.ops, progress, {"action": "ssh", "enable": True})
        setup.touch_request(self.ops, progress, {"action": "ssh", "enable": False})
        self.assertEqual(self.ops.calls, [("ssh", True), ("ssh", False)])
        progress = setup.Progress(self.root / "fresh.json")
        for stage in ("hostname", "timezone", "time", "wifi"):
            progress.advance(stage)
        result = setup.touch_request(self.ops, progress, {"action": "finish"})
        self.assertTrue(result["completed"])
        self.assertEqual(self.ops.calls, [("ssh", True), ("ssh", False)])

    def test_touch_setup_requires_key_before_enabling_and_rejects_unknown_actions(self):
        progress = setup.Progress(self.path)
        for request in (
            {"action": "ssh", "enable": True},
            {"action": "finish"},
            {"action": "shell"},
            {"action": "status", "command": "reboot"},
        ):
            with self.assertRaises(ValueError):
                setup.touch_request(self.ops, progress, request)
        self.assertFalse(progress.done("ssh"))
        self.assertEqual([], self.ops.calls)

    def test_empty_busctl_reply_and_scalar_property_are_supported(self):
        for text, expected in (("", None), ('{"type":"b","data":false}', False)):
            with mock.patch.object(
                setup.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=text),
            ):
                self.assertEqual(
                    expected,
                    setup.SystemOps.iwd(
                        "get-property",
                        "net.connman.iwd",
                        "/station",
                        "net.connman.iwd.Station",
                        "Scanning",
                    ),
                )

    def test_touch_password_uses_private_tty_not_command_arguments(self):
        executable = self.root / "iwctl"
        argv = self.root / "argv.json"
        executable.write_text(
            "#!"
            + setup.sys.executable
            + "\nimport sys,json\nfrom pathlib import Path\nPath("
            + repr(str(argv))
            + ").write_text(json.dumps(sys.argv))\nsys.stdout.write('Passphrase: '); sys.stdout.flush()\nraise SystemExit(0 if input() == 'FAKE-secret' else 2)\n"
        )
        executable.chmod(0o755)
        ops = setup.SystemOps.__new__(setup.SystemOps)
        with mock.patch.dict(
            setup.os.environ,
            {"PATH": str(self.root) + ":" + setup.os.environ.get("PATH", "")},
        ):
            ops.connect_wifi_touch("Test network", "FAKE-secret")
        self.assertIn("Test network", argv.read_text())
        self.assertNotIn("FAKE-secret", argv.read_text())
        with self.assertRaises(ValueError):
            ops.connect_wifi_touch("Test network", "bad\npassword")

    def test_duplicate_public_keys_preserve_existing_options_and_comments(self):
        path = self.root / "home" / ".ssh" / "authorized_keys"
        path.parent.mkdir(parents=True)
        original = 'restrict,command="/bin/true" ' + public_key() + " developer key\n"
        path.write_text(original)
        self.assertFalse(setup.add_public_key(path, public_key() + " new comment"))
        self.assertEqual(original, path.read_text())
        path.write_text("# existing developer keys\n")
        self.assertTrue(setup.add_public_key(path, public_key()))
        self.assertFalse(setup.add_public_key(path, public_key()))
        self.assertEqual(1, path.read_text().count(public_key()))
        self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_invalid_keys_and_names_cannot_reach_operations(self):
        for name in ("bad name", "bad.local", "-bad", "bad;reboot", "x" * 64):
            with self.assertRaises(ValueError):
                setup.hostname(name)
        self.assertEqual("study-frame", setup.hostname("STUDY-FRAME"))
        for value in (
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "ssh-ed25519 !!!!",
            "ssh-rsa " + public_key().split()[1],
        ):
            with self.assertRaises(ValueError):
                setup.normalize_key(value)
        answers = iter(["bad;reboot"])

        def ask(_):
            try:
                return next(answers)
            except StopIteration:
                raise EOFError() from None

        wizard = setup.Wizard(
            setup.Progress(self.path), self.ops, ask, self.messages.append
        )
        with self.assertRaises(EOFError):
            wizard.resume()
        self.assertEqual([], self.ops.calls)
        self.assertFalse(self.path.exists())

    def test_corrupt_progress_recovers_without_resetting_system_identity(self):
        self.path.parent.mkdir()
        self.path.write_text("{invalid")
        progress = setup.Progress(self.path)
        self.assertTrue(progress.recovered)
        self.ops.name = "existing-frame"
        self.wizard(["", "UTC", "", "", "", "n"], progress).resume()
        self.assertEqual("existing-frame", self.ops.name)
        self.assertFalse(any(c[0] in ("key", "wifi", "ssh") for c in self.ops.calls))

    def test_revisit_completed_does_not_automatically_rerun_or_reset(self):
        self.wizard(["", "UTC", "", "", "", "n"]).resume()
        self.ops.calls.clear()
        self.wizard(["1", "0"]).menu()
        self.assertEqual([], self.ops.calls)
        self.wizard(["4", "new-name", "0"]).menu()
        self.assertEqual([("hostname", "new-name")], self.ops.calls)

    def test_symlink_progress_and_ssh_paths_are_rejected(self):
        self.path.parent.mkdir()
        self.path.symlink_to(self.root / "other")
        with self.assertRaises(ValueError):
            setup.Progress(self.path)
        path = self.root / "home" / ".ssh" / "authorized_keys"
        path.parent.mkdir(parents=True)
        path.symlink_to(self.root / "other")
        with self.assertRaises(ValueError):
            setup.add_public_key(path, public_key())

    def test_older_completed_setup_prompts_only_new_time_stages(self):
        self.path.parent.mkdir()
        self.path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "completed": True,
                    "stages": {"hostname": True, "wifi": True, "ssh": True},
                }
            )
        )
        progress = setup.Progress(self.path)
        self.assertFalse(progress.data["completed"])
        self.wizard(["Europe/London", ""], progress).resume()
        self.assertEqual(self.ops.calls, [("timezone", "Europe/London")])
        self.assertTrue(setup.Progress(self.path).data["completed"])

    def test_timezone_and_manual_time_are_separate_resumable_stages(self):
        wizard = self.wizard(["", "Europe/London", "2026-10-21 12:34:56", "", "", "n"])
        wizard.resume()
        self.assertIn(("timezone", "Europe/London"), self.ops.calls)
        self.assertIn(("time", "2026-10-21 12:34:56"), self.ops.calls)
        self.assertTrue(setup.Progress(self.path).done("time"))
        for value in ["../UTC", "/etc/passwd", "Not/AZone"]:
            with self.assertRaises(ValueError):
                setup.timezone(value)
        for value in [
            "2026-02-30 12:00:00",
            "2026-01-01; reboot",
            "1970-01-01 00:00:00",
        ]:
            with self.assertRaises(ValueError):
                setup.utc_time(value)

    def test_manual_time_marker_only_after_success_and_is_readable_this_boot(self):
        marker = self.root / "run/fascia/time-set"
        boot = self.root / "boot-id"
        boot.write_text("current-boot\n")
        ops = setup.SystemOps.__new__(setup.SystemOps)
        with (
            mock.patch.multiple(setup, TIME_MARKER=marker, BOOT_ID_PATH=boot),
            mock.patch.object(ops, "run") as run,
        ):
            ops.set_time("2026-10-21 12:34:56")
            self.assertEqual(json.loads(marker.read_text())["boot_id"], "current-boot")
            self.assertEqual(marker.stat().st_mode & 0o777, 0o644)
            self.assertEqual(
                run.call_args_list[1].args[0],
                ["date", "-u", "--set", "2026-10-21 12:34:56"],
            )
            run.side_effect = subprocess.CalledProcessError(1, "date")
            with self.assertRaises(subprocess.CalledProcessError):
                ops.set_time("2026-10-21 12:34:57")
            self.assertFalse(marker.exists())

    def test_chosen_identity_persists_without_bulk_etc_state(self):
        root = self.root / "identity"
        root.mkdir()
        with mock.patch.object(setup, "IDENTITY_ROOT", root):
            setup.persist_identity(
                hostname="study-frame", timezone="Europe/London", ssh_enabled=True
            )
        value = json.loads((root / "config.json").read_text())
        self.assertEqual(value["hostname"], "study-frame")
        self.assertTrue(value["ssh_enabled"])
        self.assertNotIn("password", value)


if __name__ == "__main__":
    unittest.main()
