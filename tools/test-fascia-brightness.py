#!/usr/bin/env python3
"""Local fake-backlight integration; never touches /sys or the real socket."""

import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time


def run(binary):
    with tempfile.TemporaryDirectory(prefix="fbr-", dir="/tmp") as temp:
        root = Path(temp)
        (root / "brightness").write_text("4\n")
        (root / "max_brightness").write_text("8\n")
        proc = subprocess.Popen(
            [binary, "--test-root", temp, "--test-save-delay-ms", "1200"],
            stderr=subprocess.PIPE,
        )

        def request(packet):
            start = time.monotonic()
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(5)
                client.connect(str(root / "brightness.sock"))
                client.sendall(packet)
                response = client.recv(256)
            return response, time.monotonic() - start

        try:
            deadline = time.monotonic() + 3
            while not (root / "brightness.sock").exists():
                if proc.poll() is not None:
                    raise AssertionError(proc.stderr.read().decode())
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert request(b"get\n")[0] == b"ok 4 8\n"
            for bad in (
                b"set 0\n",
                b"set 9\n",
                b"set -1\n",
                b"set 2\nset 3\n",
                b"set 4294967296\n",
            ):
                assert request(bad)[0] == b"error request\n"
            assert (root / "brightness").read_text() == "4\n"
            slow = socket.socket(socket.AF_UNIX)
            slow.connect(str(root / "brightness.sock"))
            slow.sendall(b"set ")
            for level in (1, 2, 3, 4, 5):
                reply, elapsed = request(f"set {level}\n".encode())
                assert reply == f"ok {level} 8\n".encode()
                assert elapsed < 0.15, elapsed
            time.sleep(0.85)  # persistence worker is now deliberately blocked for 1.2 s
            reply, elapsed = request(b"set 7\n")
            assert reply == b"ok 7 8\n" and elapsed < 0.15, (reply, elapsed)
            assert not (
                root / "device.json"
            ).exists(), "save completed before injected storage delay"
            slow.settimeout(1)
            assert slow.recv(1) == b""
            slow.close()
            # Durable CLI waits for its own (or newer superseding) committed generation.
            reply, durable_elapsed = request(b"save 6\n")
            assert reply == b"ok 6 8\n", reply
            assert durable_elapsed > 1, durable_elapsed
            saved = json.loads((root / "device.json").read_text())
            assert saved == {
                "schema_version": 1,
                "brightness": 75,
                "brightness_level": 6,
                "brightness_maximum": 8,
            }
            assert stat.S_IMODE((root / "device.json").stat().st_mode) == 0o600
            assert request(b"set 8\n")[0] == b"ok 8 8\n"
            proc.terminate()
            output = proc.communicate(timeout=5)[1].decode()
            assert proc.returncode == 0, output
            assert (
                json.loads((root / "device.json").read_text())["brightness_level"] == 8
            )
            assert not list(root.glob("*.tmp.*"))
            assert output.count("BRIGHTNESS_TEST_SAVE") == 3, output
            print(
                json.dumps(
                    {
                        "protocol": "pass",
                        "slow_client": "pass",
                        "coalesced_writes": 3,
                        "set_during_slow_save_ms": round(elapsed * 1000, 3),
                        "durable_save_ms": round(durable_elapsed * 1000, 3),
                        "shutdown_flush": "pass",
                        "file_mode": "0600",
                    }
                )
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    if os.geteuid() == 0:
        raise SystemExit("Run this test unprivileged")
    run(str(Path(sys.argv[1]).resolve()))
