#!/usr/bin/env python3
"""Retain packages from a prepared manifest. Upload packages.tar with the pinned base,
then copy packages.lock.json into image/host and test a fresh preparation.
"""

import argparse
import base64
import concurrent.futures
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--release", required=True)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    inventory = json.loads(args.manifest.read_text())
    wanted = inventory["build_packages"] | inventory["runtime_packages"]
    wanted.pop("fascia-board-support-dkms", None)
    packages = {}
    databases = {}
    mirror = "https://fl.us.mirror.archlinuxarm.org/armv7h"
    for repo in ("core", "extra", "alarm"):
        path = args.directory / f"{repo}.db"
        if not path.exists():
            urllib.request.urlretrieve(f"{mirror}/{repo}/{repo}.db", path)
        entries = []
        selected = set()
        with tarfile.open(path) as source:
            for member in source:
                if not member.name.endswith("/desc"):
                    continue
                raw = source.extractfile(member).read()
                fields = {
                    lines[0].strip("%"): lines[1:]
                    for block in raw.decode().strip().split("\n\n")
                    if (lines := block.splitlines())
                }
                name = fields["NAME"][0]
                if name not in wanted or name in packages:
                    continue
                if fields["VERSION"][0] != wanted[name]:
                    raise ValueError(f"{name}: mirror no longer has {wanted[name]}")
                packages[name] = {
                    "file": fields["FILENAME"][0],
                    "sha256": fields["SHA256SUM"][0],
                    "bytes": int(fields["CSIZE"][0]),
                    "version": wanted[name],
                    "url": f"{mirror}/{repo}/{fields['FILENAME'][0]}",
                    "signature": fields["PGPSIG"][0],
                }
                selected.add(member.name.split("/")[0])
            for member in source.getmembers():
                if member.isfile() and member.name.split("/")[0] in selected:
                    entries.append((member.name, source.extractfile(member).read()))
        databases[repo] = entries
    if set(wanted) != set(packages):
        raise ValueError(f"Missing packages: {set(wanted) - set(packages)}")
    directory = args.directory / "repository"
    directory.mkdir(exist_ok=True)
    for repo, entries in databases.items():
        with tarfile.open(directory / f"{repo}.db", "w:gz") as output:
            for name, raw in entries:
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                output.addfile(member, io.BytesIO(raw))

    def download(record):
        path = directory / record["file"]
        if not path.exists():
            temporary = path.with_name(path.name + ".partial")
            urllib.request.urlretrieve(record["url"], temporary)
            temporary.replace(path)
        if path.stat().st_size != record["bytes"] or sha(path) != record["sha256"]:
            raise ValueError(f"Package checksum mismatch: {path.name}")
        path.with_name(path.name + ".sig").write_bytes(
            base64.b64decode(record["signature"], validate=True)
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(download, packages.values()))
    bundle = args.directory / "packages.tar"
    with tarfile.open(bundle, "w") as output:
        for path in sorted(directory.iterdir()):
            output.add(path, arcname=path.name, recursive=False)
    lock = {
        "schema_version": 1,
        "release": args.release,
        "bundle": {
            "file": bundle.name,
            "url": f"https://github.com/jkingston/fascia/releases/download/{args.release}/{bundle.name}",
            "bytes": bundle.stat().st_size,
            "sha256": sha(bundle),
        },
        "files": {
            p.name: {"bytes": p.stat().st_size, "sha256": sha(p)}
            for p in sorted(directory.iterdir())
        },
        "build_packages": inventory["build_packages"],
        "runtime_packages": {
            n: v
            for n, v in inventory["runtime_packages"].items()
            if n != "fascia-board-support-dkms"
        },
        "upstream": {
            n: {k: v for k, v in r.items() if k != "signature"}
            for n, r in sorted(packages.items())
        },
    }
    (args.directory / "packages.lock.json").write_text(
        json.dumps(lock, indent=2) + "\n"
    )
    print(f"Retained {len(packages)} packages in {bundle}")


if __name__ == "__main__":
    main()
