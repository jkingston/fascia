#!/usr/bin/env python3
"""On-device systemd/Wayland proof; isolated runtime and apps, no screen changes."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--installer", type=Path, required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("app", args.installer)
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    env = os.environ.copy()
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"

    def systemctl(*args, check=True):
        return subprocess.run(
            ["systemctl", "--user", *args],
            env={**env, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"},
            check=check,
            capture_output=True,
            text=True,
        )

    target = Path(f"/run/user/{os.getuid()}/systemd/user/fascia-session.target")
    if systemctl("is-active", "fascia-session.target", check=False).returncode == 0:
        raise SystemExit("Refusing to share a live Fascia session target")
    created_target = not target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    if created_target:
        target.write_text("[Unit]\nDescription=Fascia isolated test session\n")
    systemctl("daemon-reload")
    systemctl("start", "fascia-session.target")
    results = {}
    try:
        with tempfile.TemporaryDirectory(prefix="fascia-proof-") as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            home = root / "home"
            home.write_text("#!/bin/sh\nexec sleep 600\n")
            home.chmod(0o700)
            store = app.Store(root / "registry")
            for ident, body in {
                "proof-native": f'exec "{args.native}"',
                "proof-crash": "exit 7",
                "proof-timeout": "exec sleep 600",
                "proof-stubborn": 'trap "" TERM\nsleep 600 &\necho $! > "$XDG_STATE_HOME/child"\nwait',
            }.items():
                bundle = root / (ident + ".AppDir")
                bundle.mkdir()
                (bundle / (ident + ".desktop")).write_text(
                    f"[Desktop Entry]\nType=Application\nName=Proof\nExec=AppRun\nIcon={ident}\nX-Fascia-Version=1\n"
                )
                (bundle / (ident + ".png")).write_bytes(b"\x89PNG\r\n\x1a\n")
                (bundle / ".DirIcon").symlink_to(ident + ".png")
                (bundle / "AppRun").write_text(
                    '#!/bin/sh\nprintf "%s\\n%s\\n%s\\n%s\\n" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CACHE_HOME" > "$XDG_STATE_HOME/observed"\n'
                    + body
                    + "\n"
                )
                (bundle / "AppRun").chmod(0o755)
                store.install(bundle)
            env.update(
                XDG_RUNTIME_DIR=str(runtime),
                WLR_BACKENDS="headless",
                WLR_HEADLESS_OUTPUTS="1",
                WLR_RENDER_DRM_DEVICE="/dev/dri/renderD128",
                FASCIA_APPS_ROOT=str(store.apps),
                FASCIA_STATE_ROOT=str(store.state),
                FASCIA_AUTOSTART="0",
            )
            log_path = root / "host.log"
            with log_path.open("w") as log:
                process = subprocess.Popen(
                    [str(args.binary), "--home", str(home)],
                    env=env,
                    stdout=log,
                    stderr=log,
                )

                def call(command):
                    with socket.socket(socket.AF_UNIX) as connection:
                        connection.settimeout(2)
                        connection.connect(str(runtime / "fascia-home.sock"))
                        connection.sendall((command + "\n").encode())
                        return connection.recv(128).decode().strip()

                def wait(predicate, seconds=5):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        assert process.poll() is None, log_path.read_text()
                        try:
                            if predicate():
                                return
                        except (FileNotFoundError, ConnectionRefusedError):
                            pass
                        time.sleep(0.025)
                    raise AssertionError("timeout\n" + log_path.read_text())

                def stopped(ident):
                    return (
                        systemctl(
                            "is-active", "fascia-app-" + ident + ".service", check=False
                        ).returncode
                        != 0
                    )

                try:
                    wait(lambda: call("status") == "home")
                    assert call("launch ../bad") == "error InvalidCommand"
                    assert call("launch missing") == "error.AppNotInstalled".replace(
                        ".", " "
                    )
                    start = time.monotonic()
                    assert call("launch proof-native") == "ok"
                    wait(lambda: call("status") == "app proof-native")
                    results["c_client_first_frame_ms"] = round(
                        (time.monotonic() - start) * 1000
                    )
                    observed = store.state / "proof-native/state/observed"
                    assert observed.read_text().splitlines() == [
                        str(store.state / "proof-native" / key)
                        for key in ("config", "data", "state", "cache")
                    ]
                    assert "member=true visible=true" in log_path.read_text()
                    assert call("home") == "ok"
                    wait(lambda: stopped("proof-native"))
                    wait(lambda: "ended id=proof-native" in log_path.read_text())
                    assert (store.state / "proof-native/state/clean-exit").is_file()
                    results["graceful_exit_saved_state"] = True
                    time.sleep(0.2)
                    assert call("launch proof-crash") == "ok"
                    wait(lambda: "ended id=proof-crash" in log_path.read_text())
                    assert call("status") == "home"
                    assert call("launch proof-timeout") == "ok"
                    wait(
                        lambda: "ended id=proof-timeout" in log_path.read_text(),
                        seconds=12,
                    )
                    assert call("status") == "home"
                    assert "first-fascia-timeout" in log_path.read_text()
                    results["first_frame_timeout_returns_home"] = True
                    assert call("launch proof-stubborn") == "ok"
                    child_file = store.state / "proof-stubborn/state/child"
                    wait(child_file.exists)
                    child = int(child_file.read_text())
                    start = time.monotonic()
                    assert call("home") == "ok"
                    wait(lambda: "ended id=proof-stubborn" in log_path.read_text())
                    results["stubborn_stop_ms"] = round(
                        (time.monotonic() - start) * 1000
                    )
                    assert (
                        not Path(f"/proc/{child}").exists()
                        or ") Z " in Path(f"/proc/{child}/stat").read_text()
                    )
                    assert call("launch proof-native") == "ok"
                    wait(lambda: call("status") == "app proof-native")
                    systemctl("stop", "fascia-session.target")
                    wait(
                        lambda: "ended id=proof-native" in log_path.read_text()
                        and stopped("proof-native")
                    )
                    assert call("status") == "home"
                    results.update(
                        cgroup_identity=True,
                        xdg_directories=True,
                        crash_returns_home=True,
                        descendants_cleaned=True,
                        session_stop_cleanup=True,
                    )
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    if process.returncode:
                        print(log_path.read_text())
    finally:
        systemctl("stop", "fascia-session.target", check=False)
        if created_target:
            target.unlink()
        systemctl("daemon-reload")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
