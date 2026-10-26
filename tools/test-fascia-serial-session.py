#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "session", Path(__file__).with_name("fascia-serial-session.py")
)
session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session)


class SessionTests(unittest.TestCase):
    def test_only_frame_interactive_usb_serial_is_eligible(self):
        self.assertTrue(session.eligible("fascia", "/dev/ttyGS0", True))
        for args in [
            ("root", "/dev/ttyGS0", True),
            ("fascia", "/dev/pts/0", True),
            ("fascia", "/dev/ttyGS0", False),
        ]:
            self.assertFalse(session.eligible(*args))

    def test_eof_or_failure_offers_setup_only_once_then_returns_shell(self):
        calls = []
        text = []
        session.run_session(
            {"setup_completed": False}, lambda: calls.append(1) or 1, text.append
        )
        self.assertEqual([1], calls)
        self.assertIn("shell", text[-1])

    def test_completed_login_never_reopens_wizard_and_reports_actual_disabled_ssh(self):
        calls = []
        text = []
        session.run_session(
            {
                "setup_completed": True,
                "hostname": "study",
                "ssh_active": False,
                "mdns_service_active": False,
                "addresses": [{"interface": "usb0", "address": "10.55.0.1"}],
            },
            lambda: calls.append(1),
            text.append,
        )
        self.assertEqual([], calls)
        self.assertIn("SSH: disabled / inactive", text)
        self.assertIn("usb0: 10.55.0.1", text)


if __name__ == "__main__":
    unittest.main()
