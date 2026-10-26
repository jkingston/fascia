#!/usr/bin/env python3
"""Fascia: apps, setup and device controls."""

import os
import sys

COMMANDS = {
    "app": "/usr/local/bin/fascia-app",
    "setup": "/usr/local/bin/fascia-setup",
    "display": "/usr/local/bin/fascia-display",
    "system": "/usr/local/bin/fascia-system",
}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help"):
        print(
            "Fascia\n\nUsage: fascia COMMAND [ARGS]\n\n  app       Install, launch and manage apps\n  setup     Wi-Fi, SSH and device settings\n  display   Visual display calibration\n  system    Device controls\n\nUse fascia COMMAND --help for details."
        )
        return
    executable = COMMANDS.get(args[0])
    if executable is None:
        raise SystemExit("Unknown command: " + args[0] + ". Run fascia --help.")
    try:
        os.execv(executable, [executable, *args[1:]])
    except FileNotFoundError:
        raise SystemExit(
            "This command runs on the Fascia device; its system tools are not installed here."
        ) from None


if __name__ == "__main__":
    main()
