#!/usr/bin/env python3
"""Declare opted-in services before PID1 constructs its boot transaction."""

import importlib.util
from pathlib import Path
import sys

source = Path("/usr/lib/fascia/provision/fascia_identity.py")
if not source.exists():
    source = Path(__file__).with_name("fascia_identity.py")
spec = importlib.util.spec_from_file_location("fascia_identity", source)
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)


def generate(root, output):
    config = identity.load(root)
    for key, unit in [
        ("ssh_enabled", "sshd.service"),
        ("physical_home", "fascia-home-button.service"),
    ]:
        if config[key]:
            path = output / "multi-user.target.wants" / unit
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to("/usr/lib/systemd/system/" + unit)
    if config["physical_home"]:
        path = output / "pf-kiosk.service.d/97-physical-home.conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[Service]\nEnvironment=FASCIA_HOME_OVERLAY=auto\n")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 4:
            raise ValueError("systemd generator requires three output directories")
        generate(identity.ROOT, Path(sys.argv[1]))
    except (OSError, ValueError) as exc:
        print("Fascia identity generator: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
