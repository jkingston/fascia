#!/usr/bin/python3 -I
"""Initialize persistent state and apply an optional first-image app manifest once."""

import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import subprocess

source = Path("/usr/lib/fascia/provision/fascia_identity.py")
if not source.exists():
    source = Path(__file__).parent / "provision/fascia_identity.py"
spec = importlib.util.spec_from_file_location("fascia_identity", source)
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)


def app_ids(value):
    if (
        not isinstance(value, list)
        or len(value) > 32
        or any(
            not isinstance(app, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", app)
            or app == "home"
            for app in value
        )
        or len(set(value)) != len(value)
    ):
        raise ValueError("invalid initial app list")
    return value


def read_plan(factory):
    factory = Path(factory)
    if factory.is_symlink():
        raise ValueError("initial app directory cannot be a symlink")
    if not factory.exists():
        return None
    value = json.loads(identity.read_bounded(factory / "manifest.json", 4096))
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schema_version", "apps", "default_app", "startup_delay_seconds"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("invalid initial app manifest")
    apps = app_ids(value["apps"])
    default = value["default_app"]
    delay = value["startup_delay_seconds"]
    if (
        (default is not None and default not in apps)
        or type(delay) is not int
        or not 5 <= delay <= 60
    ):
        raise ValueError("invalid initial app defaults")
    return value


def initialize(
    root=Path("/var/lib/fascia"),
    factory=Path("/usr/share/fascia/factory-apps"),
    run=subprocess.run,
    home=Path("/home/fascia"),
):
    user = pwd.getpwnam("fascia")
    for directory in (root, root / "apps", root / "state", home):
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise ValueError("persistent mount point cannot be a symlink")
        os.chown(directory, user.pw_uid, user.pw_gid)
        os.chmod(directory, 0o755)
    # Preserve this marker across OS updates; it records a completed first install,
    # never the set of apps that should currently be installed.
    marker = root / ".factory-seeded.json"
    if marker.exists():
        value = json.loads(identity.read_bounded(marker, 4096))
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "apps"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
        ):
            raise ValueError("invalid factory seeding marker")
        app_ids(value["apps"])
        return
    plan = read_plan(factory)
    apps = plan["apps"] if plan is not None else []
    for app in apps:
        bundle = factory / "apps" / app
        if not (root / "apps" / app / "active").exists():
            run(
                [
                    "/usr/bin/runuser",
                    "-u",
                    "fascia",
                    "--",
                    "/usr/local/bin/fascia-app",
                    "--root",
                    str(root),
                    "install",
                    str(bundle),
                ],
                check=True,
            )
    if plan is not None and not (root / "settings.json").exists():
        run(
            [
                "/usr/bin/runuser",
                "-u",
                "fascia",
                "--",
                "/usr/local/bin/fascia-app",
                "--root",
                str(root),
                "default",
                plan["default_app"] or "home",
                "--delay",
                str(plan["startup_delay_seconds"]),
            ],
            check=True,
        )
    identity.atomic(
        marker, (json.dumps({"schema_version": 1, "apps": apps}) + "\n").encode(), 0o644
    )


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise PermissionError("state initialization requires root")
    if not os.path.ismount("/data"):
        raise PermissionError("persistent /data must be mounted")
    initialize()
