"""Cache the rolling official ARM rootfs; Linux verifies its signature before use."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent
MAX_ROOTFS = 2 * 1024**3
MAX_SIGNATURE = 65536


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, destination, limit, progress=None):
    if not url.startswith("https://"):
        raise ValueError("HTTPS root filesystem required")
    if destination.is_symlink():
        raise ValueError("Root filesystem download cannot be a symlink")
    count = 0
    with urllib.request.urlopen(url, timeout=90) as response:
        if not response.geturl().startswith("https://"):
            raise ValueError("Non-HTTPS root filesystem redirect")
        with destination.open("xb") as target:
            while block := response.read(1024 * 1024):
                count += len(block)
                if count > limit:
                    raise ValueError("Oversized root filesystem input")
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
    if not count:
        raise ValueError("Empty root filesystem input")
    if progress:
        progress({"kind": "download", "file": destination.name, "bytes": count})


def read_snapshot(cache, key):
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("Invalid root filesystem cache identity")
    directory = cache / key
    if directory.is_symlink():
        raise ValueError("Root filesystem cache cannot be a symlink")
    metadata = directory / "snapshot.json"
    if metadata.is_symlink():
        raise ValueError("Root filesystem metadata cannot be a symlink")
    record = json.loads(metadata.read_text())
    if record.get("schema_version") != 1 or record.get("signature_sha256") != key:
        raise ValueError("Invalid root filesystem snapshot metadata")
    for name, field, maximum in (
        ("rootfs.tar.gz", "rootfs", MAX_ROOTFS),
        ("rootfs.tar.gz.sig", "signature", MAX_SIGNATURE),
    ):
        path = directory / name
        size = record[field + "_bytes"]
        if (
            type(size) is not int
            or not 0 < size <= maximum
            or not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != size
            or digest(path) != record[field + "_sha256"]
        ):
            raise ValueError("Corrupt cached root filesystem input: " + name)
    return {
        **record,
        "rootfs": str(directory / "rootfs.tar.gz"),
        "rootfs_signature": str(directory / "rootfs.tar.gz.sig"),
    }


def publish_pointer(cache, key):
    with tempfile.NamedTemporaryFile(mode="w", dir=cache, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump({"schema_version": 1, "snapshot": key}, stream)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(cache / "latest.json")


def invalidate(cache, archive_digest, signature_digest):
    """Quarantine only the exact downloaded pair rejected by Linux's verifier."""
    cache = Path(cache) / "rootfs"
    record = read_snapshot(cache, signature_digest)
    if record["rootfs_sha256"] != archive_digest:
        raise ValueError("Rejected base does not match the cached snapshot")
    quarantine = Path(tempfile.mkdtemp(prefix=".rejected-", dir=cache))
    (cache / signature_digest).rename(quarantine)
    pointer = cache / "latest.json"
    if pointer.is_symlink():
        raise ValueError("Root filesystem cache pointer cannot be a symlink")
    if (
        pointer.exists()
        and json.loads(pointer.read_text()).get("snapshot") == signature_digest
    ):
        pointer.unlink()
    return quarantine


def prepare(cache, offline=False, progress=None):
    cache = Path(cache) / "rootfs"
    if cache.is_symlink():
        raise ValueError("Root filesystem cache cannot be a symlink")
    cache.mkdir(parents=True, exist_ok=True)
    spec = json.loads((HERE / "rootfs.json").read_text())
    if spec.get("schema_version") != 1 or spec.get("architecture") != "armv7h":
        raise ValueError("Unsupported root filesystem profile")
    keyring = HERE / spec["keyring_file"]
    if (
        keyring.parent != HERE
        or keyring.is_symlink()
        or digest(keyring) != spec["keyring_sha256"]
    ):
        raise ValueError("Root filesystem public trust anchor mismatch")
    fingerprint = spec["signing_fingerprint"]
    if len(fingerprint) != 40 or any(c not in "0123456789ABCDEF" for c in fingerprint):
        raise ValueError("Invalid root filesystem signing fingerprint")
    if offline:
        pointer = cache / "latest.json"
        if pointer.is_symlink():
            raise ValueError("Root filesystem cache pointer cannot be a symlink")
        if not pointer.is_file():
            raise FileNotFoundError("No cached official ARM root filesystem")
        record = read_snapshot(cache, json.loads(pointer.read_text())["snapshot"])
    else:
        errors = []
        record = None
        for url in spec["urls"]:
            for _ in range(2):
                try:
                    with tempfile.TemporaryDirectory(
                        prefix=".rootfs-", dir=cache
                    ) as tmp:
                        staging = Path(tmp)
                        signature = staging / "rootfs.tar.gz.sig"
                        download(url + ".sig", signature, MAX_SIGNATURE)
                        key = digest(signature)
                        if (cache / key).exists():
                            record = read_snapshot(cache, key)
                            break
                        archive = staging / "rootfs.tar.gz"
                        download(url, archive, MAX_ROOTFS, progress)
                        after = staging / "signature-after"
                        download(url + ".sig", after, MAX_SIGNATURE)
                        if signature.read_bytes() != after.read_bytes():
                            raise ValueError(
                                "Upstream root filesystem changed during download"
                            )
                        after.unlink()
                        metadata = {
                            "schema_version": 1,
                            "url": url,
                            "rootfs_sha256": digest(archive),
                            "rootfs_bytes": archive.stat().st_size,
                            "signature_sha256": key,
                            "signature_bytes": signature.stat().st_size,
                        }
                        (staging / "snapshot.json").write_text(
                            json.dumps(metadata) + "\n"
                        )
                        staging.rename(cache / key)
                        record = read_snapshot(cache, key)
                        break
                except (OSError, ValueError) as error:
                    errors.append(f"{url}: {error}")
            if record is not None:
                break
        if record is None:
            raise OSError(
                "Unable to download official ARM root filesystem: " + "; ".join(errors)
            )
        publish_pointer(cache, record["signature_sha256"])
    return {
        **record,
        "trust_keyring": str(keyring),
        "trust_fingerprint": fingerprint,
    }
