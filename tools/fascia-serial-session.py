#!/usr/bin/env python3
"""One setup offer per serial login; EOF returns to the normal shell."""

import json
import os
import pwd
import subprocess


def eligible(user, tty, interactive):
    return user == "fascia" and tty == "/dev/ttyGS0" and interactive


def run_session(status, wizard, tell=print):
    if status.get("setup_completed") is not True:
        wizard()  # Exactly one invocation; a disconnect/EOF never retries in a loop.
        tell(
            "Type fascia setup to resume or review setup. The shell is available below."
        )
        return
    tell("\n  Fascia / USB console\n  ----------------------------------------")
    name = status.get("hostname", "fascia")
    tell("Fascia: " + str(name))
    tell(
        "SSH: "
        + ("active" if status.get("ssh_active") is True else "disabled / inactive")
    )
    tell(
        "Local discovery: "
        + (
            str(name) + ".local (service active; client resolution not verified)"
            if status.get("mdns_service_active") is True
            else "inactive"
        )
    )
    for item in status.get("addresses", []):
        if isinstance(item, dict):
            tell(
                str(item.get("interface", "network"))
                + ": "
                + str(item.get("address", "unknown"))
            )
    tell(
        "\nfascia setup   Connections and settings\nfascia display Display calibration"
    )


def main():
    try:
        if not eligible(pwd.getpwuid(os.getuid()).pw_name, os.ttyname(0), os.isatty(0)):
            return
    except OSError:
        return
    try:
        result = subprocess.run(
            ["/usr/local/bin/fascia-setup", "--status"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode or len(result.stdout) > 8192:
            raise ValueError("status unavailable")
        status = json.loads(result.stdout)
        if not isinstance(status, dict):
            raise ValueError("invalid status")
        run_session(
            status, lambda: subprocess.run(["/usr/local/bin/fascia-setup"]).returncode
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print("Fascia console ready. Type fascia setup to start or resume setup.")


if __name__ == "__main__":
    main()
