"""Host prompts and durable per-card SSH policy; never touches a real card."""

import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ssh = load("host_ssh", ROOT / "image/host/ssh.py")
policy = load("policy", ROOT / "image/provision/fascia_ssh.py")


class Access(unittest.TestCase):
    def test_flags_and_noninteractive(self):
        parser = argparse.ArgumentParser()
        ssh.arguments(parser)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                ssh.configure(parser.parse_args(["--no-ssh"])), {"mode": "off"}
            )
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(ValueError, "Choose --ssh-key"):
                ssh.configure(parser.parse_args([]))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--no-ssh", "--ssh-password"])

    def test_keys_and_private_key_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = root / "id_ed25519"
            subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    "test workstation",
                    "-f",
                    str(key),
                ],
                check=True,
            )
            item = ssh.key_file(str(key) + ".pub")
            self.assertEqual(len(ssh.existing_keys(root)), 1)
            self.assertEqual(item["comment"], "test workstation")
            fingerprint = subprocess.check_output(
                ["ssh-keygen", "-lf", str(key) + ".pub"], text=True
            ).split()[1]
            self.assertEqual(item["fingerprint"], fingerprint)
            with self.assertRaises(ValueError):
                ssh.key_file(key)
            data = root / "data"
            policy.seed(
                data, {"mode": "key", "public_key": item["key"]}, ownership=False
            )
            self.assertEqual(
                (data / "home/fascia/.ssh/authorized_keys").read_text().strip(),
                item["key"],
            )
            self.assertEqual((data / "home/fascia/.ssh").stat().st_mode & 0o777, 0o700)
            self.assertFalse((data / "identity/ssh-password.hash").exists())

    def test_password_retries_and_boot_materialization(self):
        ui = mock.Mock()
        with mock.patch.object(
            ssh.getpass,
            "getpass",
            side_effect=[
                "short",
                "twelve characters",
                "different words",
                "twelve characters",
                "twelve characters",
            ],
        ):
            hashed = ssh.password_hash(ui)
        self.assertRegex(hashed, policy.HASH)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data, etc = root / "data", root / "etc"
            policy.seed(
                data, {"mode": "password", "password_hash": hashed}, ownership=False
            )
            config = policy.identity.load(data / "identity")
            self.assertTrue(config["ssh_password_auth"])
            self.assertEqual(
                (data / "identity/ssh-password.hash").stat().st_mode & 0o777, 0o600
            )
            progress = json.loads((data / "fascia/setup.json").read_text())
            self.assertTrue(progress["stages"]["ssh"])
            etc.mkdir()
            for _ in range(2):
                (etc / "shadow").write_text(
                    "root:!:1:0:99999:7:::\nfascia:!:1:0:99999:7:::\n"
                )
                policy.apply_auth(data / "identity", etc, config)
                self.assertIn("fascia:" + hashed + ":", (etc / "shadow").read_text())
                self.assertIn("root:!:", (etc / "shadow").read_text())
                self.assertIn(
                    "PasswordAuthentication yes",
                    (etc / "ssh/sshd_config.d/40-fascia.conf").read_text(),
                )
            self.assertNotIn(
                "twelve characters",
                "".join(p.read_text() for p in root.rglob("*") if p.is_file()),
            )

    def test_invalid_policies_fail_before_creating_files(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data"
            for value in (
                {"mode": "password", "password_hash": "plaintext"},
                {"mode": "key", "public_key": "command=bad ssh-ed25519 AAAA"},
                {"mode": "off", "password": "secret"},
                {"mode": []},
            ):
                with self.assertRaises(ValueError):
                    policy.seed(data, value, ownership=False)
                self.assertFalse(data.exists())

    def test_plain_picker_retry_and_default(self):
        ui = mock.Mock()
        options = [("key", "fingerprint"), ("off", "")]
        with mock.patch("builtins.input", side_effect=["bad", "0", "1"]):
            self.assertEqual(ssh.choose(ui, options, plain=True), 0)
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(ssh.choose(ui, options, plain=True), 1)

    def test_arrow_picker_in_real_terminal(self):
        master, slave = pty.openpty()
        source = f"import runpy; m=runpy.run_path({str(ROOT / 'image/host/ssh.py')!r}); print('SELECTED', m['choose'](m['setup'].Terminal(), [('Key', 'fingerprint'), ('Off', 'disabled')]))"
        process = subprocess.Popen(
            [sys.executable, "-c", source],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=os.environ | {"TERM": "xterm-256color"},
        )
        os.close(slave)
        output = b""
        try:
            deadline = time.monotonic() + 8
            while b"Esc to cancel" not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.2)[0]:
                    output += os.read(master, 65536)
            self.assertIn(b"Esc to cancel", output)
            os.write(master, b"\x1b[A\r")
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        output += os.read(master, 65536)
                    except OSError:
                        break
            self.assertEqual(process.wait(timeout=2), 0)
            self.assertIn(b"SELECTED 0", output)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)


if __name__ == "__main__":
    unittest.main()
