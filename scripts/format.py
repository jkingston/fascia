#!/usr/bin/env python3
"""Format repository-owned sources, including Python embedded in shell scripts."""

import argparse
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parent.parent
PYTHON_HEREDOC = re.compile(
    r"(?m)^[^\n]*\bpython3\b[^\n]*<<\s*'(?P<end>[A-Z_]+)'[^\n]*\n"
    r"(?P<body>[\s\S]*?)(?=^(?P=end)$)"
)


def run(*arguments, **kwargs):
    return subprocess.run(
        ["mise", "exec", "--", *arguments], cwd=ROOT, check=False, **kwargs
    )


def embedded_python(path, check):
    source = path.read_text()

    def replace(match):
        result = run(
            "ruff",
            "format",
            "--stdin-filename",
            "embedded.py",
            "-",
            input=match["body"],
            text=True,
            capture_output=True,
        )
        result.check_returncode()
        prefix = match.group()[: match.start("body") - match.start()]
        return prefix + result.stdout

    formatted = PYTHON_HEREDOC.sub(replace, source)
    if formatted == source:
        return True
    if check:
        print(f"Embedded Python needs formatting: {path.relative_to(ROOT)}")
        return False
    path.write_text(formatted)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check without editing")
    args = parser.parse_args()
    names = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
        )
        .decode()
        .split("\0")
    )
    paths = sorted({ROOT / name for name in names if name and (ROOT / name).is_file()})
    zig = [str(p.relative_to(ROOT)) for p in paths if p.suffix in (".zig", ".zon")]
    python = [str(p.relative_to(ROOT)) for p in paths if p.suffix == ".py"]
    shell = [
        p
        for p in paths
        if p.suffix == ".sh" or p.read_bytes().startswith(b"#!/bin/sh\n")
    ]

    valid = True
    for path in shell:
        valid = embedded_python(path, args.check) and valid
    commands = [
        ["zig", "fmt", *(["--check"] if args.check else []), *zig],
        ["ruff", "format", *(["--check"] if args.check else []), *python],
        [
            "shfmt",
            "-d" if args.check else "-w",
            "-i",
            "4",
            "-sr",
            "-ci",
            *(str(p.relative_to(ROOT)) for p in shell),
        ],
    ]
    for command in commands:
        valid = run(*command).returncode == 0 and valid
    raise SystemExit(0 if valid else 1)


if __name__ == "__main__":
    main()
