#!/usr/bin/env python3
"""Stream an AppDir to a frame over SSH, install it atomically, and open it."""

import argparse
import importlib.util
from pathlib import Path
import shlex
import subprocess
import tarfile

REMOTE = r"""
import os, shutil, subprocess, sys, tempfile, time
ident = sys.argv[1]
with tempfile.NamedTemporaryFile(prefix="fascia-deploy-", suffix=".tar") as archive:
    shutil.copyfileobj(sys.stdin.buffer, archive)
    archive.flush()
    subprocess.run(["fascia", "app", "home"], check=True)
    subprocess.run(["fascia", "app", "install", archive.name], check=True)
    deadline = time.monotonic() + 5
    while True:
        result = subprocess.run(["fascia", "app", "launch", ident], capture_output=True, text=True)
        if result.returncode == 0:
            print(result.stdout, end="")
            break
        if "AppBusy" not in result.stderr or time.monotonic() >= deadline:
            sys.stderr.write(result.stderr)
            sys.exit(result.returncode)
        time.sleep(.1)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument(
        "destination", help="SSH destination, e.g. fascia@fascia-m7k3p.local"
    )
    parser.add_argument("--ssh-config", type=Path)
    args = parser.parse_args()
    if args.destination.startswith("-") or any(c.isspace() for c in args.destination):
        parser.error("invalid SSH destination")
    spec = importlib.util.spec_from_file_location(
        "fascia_app", Path(__file__).with_name("fascia-app.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        info = module.manifest(args.bundle)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    command = ["ssh"]
    if args.ssh_config:
        command += ["-F", str(args.ssh_config)]
    command += [
        "--",
        args.destination,
        shlex.join(["python3", "-c", REMOTE, info["id"]]),
    ]
    with subprocess.Popen(command, stdin=subprocess.PIPE) as remote:
        try:
            with tarfile.open(fileobj=remote.stdin, mode="w|") as archive:
                for path in sorted(args.bundle.iterdir()):
                    archive.add(path, arcname=path.name, recursive=True)
        except BrokenPipeError:
            pass
        finally:
            remote.stdin.close()
        code = remote.wait()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
