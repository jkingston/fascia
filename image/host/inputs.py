#!/usr/bin/env python3
"""Prepare the official ARM rootfs and reviewed board sources; never opens disks."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

import rootfs

HERE = Path(__file__).resolve().parent
MAX_INPUT_BYTES = 512 * 1024 * 1024


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _safe_name(name):
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or name in (".", "..")
    ):
        raise ValueError("Invalid cache basename")
    return name


def _validate(record):
    _safe_name(record["file"])
    sha = record["sha256"]
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("Invalid pinned digest")
    size = record.get("bytes")
    if size is not None and (type(size) is not int or not 0 < size <= MAX_INPUT_BYTES):
        raise ValueError("Invalid pinned input length")
    if "url" in record and not record["url"].startswith("https://"):
        raise ValueError("HTTPS input required")
    if any(not u.startswith("https://") for u in record.get("urls", [])):
        raise ValueError("HTTPS mirrors required")


def _valid(path, record):
    return (
        path.is_file()
        and not path.is_symlink()
        and (record.get("bytes") is None or path.stat().st_size == record["bytes"])
        and digest(path) == record["sha256"]
    )


def fetch(record, directory, offline=False, progress=None):
    """Bounded resumable HTTPS cache with verification before atomic publication."""
    _validate(record)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / record["file"]
    if _valid(path, record):
        return path
    if path.exists() or path.is_symlink():
        raise ValueError("Invalid cached input: " + path.name)
    if offline:
        raise FileNotFoundError("Missing offline input: " + path.name)
    partial = directory / (record["file"] + ".partial")
    if partial.is_symlink():
        raise ValueError("Symlink partial cache input")
    limit = record.get("bytes", MAX_INPUT_BYTES)
    if _valid(partial, record):
        partial.replace(path)
        return path
    urls = record.get("urls") or [record["url"]]
    attempts = max(3, len(urls))
    for attempt in range(attempts):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset >= limit:
            partial.unlink()
            offset = 0
        request = urllib.request.Request(
            urls[attempt % len(urls)],
            headers={"Range": f"bytes={offset}-"} if offset else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                if not response.geturl().startswith("https://"):
                    raise ValueError("Non-HTTPS redirect")
                if offset and response.status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    if not content_range.startswith(f"bytes {offset}-"):
                        raise ValueError("Invalid partial response")
                    mode = "ab"
                else:
                    offset, mode = 0, "wb"
                with partial.open(mode) as target:
                    count = offset
                    while block := response.read(1024 * 1024):
                        count += len(block)
                        if count > limit:
                            raise ValueError("Oversized input")
                        target.write(block)
                    target.flush()
                    os.fsync(target.fileno())
            if not _valid(partial, record):
                partial.unlink(missing_ok=True)
                raise ValueError("Input checksum/length mismatch: " + path.name)
            partial.replace(path)
            if progress:
                progress(
                    {
                        "kind": "download",
                        "file": path.name,
                        "bytes": path.stat().st_size,
                    }
                )
            return path
        except ValueError:
            partial.unlink(missing_ok=True)
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))
        except OSError as error:
            if attempt == attempts - 1:
                raise OSError(
                    f"Unable to fetch reviewed board source {path.name}: {error}. Retry with network access or restore its verified cache."
                ) from error
            time.sleep(0.2 * (attempt + 1))
    raise AssertionError("unreachable")


def _git_archive(record, directory, offline, progress):
    _validate(record)
    path = directory / record["file"]
    if _valid(path, record):
        return path
    if path.exists() or path.is_symlink():
        raise ValueError("Invalid cached source archive")
    if offline:
        raise FileNotFoundError("Missing offline source: " + path.name)
    commit = record["commit"]
    if (
        len(commit) != 40
        or any(c not in "0123456789abcdef" for c in commit)
        or not record["git"].startswith("https://")
    ):
        raise ValueError("Unpinned Git source")
    directory.mkdir(parents=True, exist_ok=True)
    # Repository and output are private per attempt; no mutable shared Git cache.
    with tempfile.TemporaryDirectory(prefix=".source-", dir=directory) as work:
        repo = Path(work) / "repo.git"
        subprocess.run(["git", "init", "--bare", "-q", str(repo)], check=True)
        git = ["git", "--git-dir", str(repo)]
        subprocess.run(
            [*git, "fetch", "--depth", "1", record["git"], commit], check=True
        )
        resolved = subprocess.check_output(
            [*git, "rev-parse", "FETCH_HEAD"], text=True
        ).strip()
        if resolved != commit:
            raise ValueError("Source commit mismatch")
        archive = Path(work) / "archive.tar"
        subprocess.run(
            [*git, "archive", "--format=tar", "--output", str(archive), commit],
            check=True,
        )
        if not _valid(archive, record):
            raise ValueError("Source archive checksum mismatch")
        with archive.open("rb") as stream:
            os.fsync(stream.fileno())
        archive.replace(path)
    if progress:
        progress({"kind": "source", "file": path.name, "bytes": path.stat().st_size})
    return path


def _write_json(path, value):
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".manifest-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def prepare(
    cache, *, offline=False, progress=None, progresscallback=None, derived_dir=None
):
    """Stage an official signed rolling base and reviewed hardware source inputs."""
    progress = progress or progresscallback
    cache = Path(cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    base = rootfs.prepare(cache, offline, progress)
    profiles = json.loads((HERE / "profiles.json").read_text())
    source_lock = json.loads((HERE / "sources.lock.json").read_text())
    if any(x["schema_version"] != 1 for x in (profiles, source_lock)):
        raise ValueError("Unsupported input schema")
    for group in ("build_packages", "runtime_packages"):
        names = profiles[group]
        if not names or len(set(names)) != len(names):
            raise ValueError("Invalid package profile")
        for name in names:
            if not name or any(
                c not in "abcdefghijklmnopqrstuvwxyz0123456789@+_.-" for c in name
            ):
                raise ValueError("Invalid package name")
    derived = Path(derived_dir).resolve() if derived_dir is not None else cache
    derived.mkdir(parents=True, exist_ok=True)
    source_dir = cache / "sources"
    source_dir.mkdir(exist_ok=True)
    archives = {}
    for record in source_lock["archives"]:
        path = (
            _git_archive(record, source_dir, offline, progress)
            if record["method"] == "git-archive"
            else fetch(record, source_dir, offline, progress)
        )
        archives[record["key"]] = {
            "file": str(path),
            "sha256": record["sha256"],
            **({"commit": record["commit"]} if "commit" in record else {}),
        }

    def kernel_file(record):
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid selected kernel path")
        path = fetch(
            {**record, "file": relative.name},
            source_dir / "kernel-files" / relative.parent,
            offline,
            progress,
        )
        return {"path": record["path"], "file": str(path), "sha256": record["sha256"]}

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, os.cpu_count() or 1)
    ) as pool:
        files = list(pool.map(kernel_file, source_lock["kernel_files"]))
    offline_manifest = derived / "offline-source-manifest.json"
    _write_json(
        offline_manifest, {"schema_version": 1, "archives": archives, "files": files}
    )
    return {
        **base,
        "schema_version": 1,
        "runtime_names": profiles["runtime_packages"],
        "build_names": profiles["build_packages"],
        "sources": {k: v["file"] for k, v in archives.items()},
        "offline_source_manifest": str(offline_manifest),
        "kernel_files": files,
    }
