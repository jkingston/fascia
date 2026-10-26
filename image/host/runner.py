#!/usr/bin/env python3
"""Narrow privilege handoff for one reviewed installer job."""

import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import devices


def load(name):
    spec = importlib.util.spec_from_file_location(
        "fascia_runner_" + name, HERE / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def owner_directory(path, owner):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        raise ValueError(
            "Job directory must be owned and writable only by the invoking user: "
            + str(path)
        )
    return path.resolve(strict=True)


def read_job(path):
    owner = (
        int(os.environ["SUDO_UID"])
        if os.geteuid() == 0 and "SUDO_UID" in os.environ
        else os.getuid()
    )
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular installer job")
    jobdir = owner_directory(path.parent, owner)
    if (
        jobdir.parent.name != "jobs"
        or len(jobdir.name) != 32
        or any(c not in "0123456789abcdef" for c in jobdir.name)
    ):
        raise ValueError("Expected a dedicated installer job directory")
    cache = owner_directory(jobdir.parent.parent, owner)
    job = json.loads(path.read_text())
    if job.get("schema_version") != 1 or job.get("mode") not in {
        "prepare",
        "image",
        "card",
    }:
        raise ValueError("Unsupported installer job")
    input_dir = owner_directory(job["input"], owner)
    output_dir = owner_directory(job["output"], owner)
    if input_dir != jobdir / "input" or output_dir.parent != cache / "prepared":
        raise ValueError("Installer shares must stay inside this job's cache")
    request = json.loads((input_dir / "request.json").read_text())
    if output_dir.name != request["source_key"]:
        raise ValueError("Output cache does not match the prepared source key")
    if job["mode"] == "prepare":
        if (
            request.get("phase") != "prepare"
            or request.get("erase_authorized") is not False
        ):
            raise ValueError("Preparation must not authorize target writes")
    elif (
        request.get("phase") != "install" or request.get("erase_authorized") is not True
    ):
        raise ValueError("Target writes require the exact install request")
    for artifact in (job.get("helper") or {}).values():
        if not Path(artifact).resolve().is_relative_to(cache / "helper"):
            raise ValueError("Helper artifacts must belong to this cache")
    return job, request, owner, cache


def drop_privileges(owner):
    if os.geteuid() != 0:
        return
    if owner == 0:
        raise ValueError("Invoke the installer as a regular user")
    account = pwd.getpwuid(owner)
    os.initgroups(account.pw_name, account.pw_gid)
    os.setgid(account.pw_gid)
    os.setuid(owner)


def mac(job, request, owner, cache):
    descriptor = None
    helper = load("helper")
    with contextlib.ExitStack() as stack:
        guardian = None
        if job["mode"] == "card":
            if os.geteuid() != 0:
                raise ValueError(
                    "Opening the identified card requires the narrow sudo step"
                )
            launcher = helper.verified_launcher(cache / "launcher", owner)
            guardian = stack.enter_context(
                helper.MountGuard(launcher, Path(job["disk"]).name)
            )
            guardian.check()
            descriptor = devices.open_target(
                job["disk"], job["capacity"], job["identity"]
            )
        try:
            drop_privileges(owner)
            if guardian is None:
                launcher = helper.build_launcher(cache / "launcher")
            paths = job["helper"]
            helper.run_helper(
                paths["kernel"],
                paths["initrd"],
                job["input"],
                job["output"],
                image=Path(job["image"]) if job["mode"] == "image" else None,
                target_fd=descriptor,
                capacity=request["capacity"],
                timeout=14400,
                launcher=launcher,
                mount_guard=guardian,
            )
        finally:
            if descriptor is not None:
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                    if guardian:
                        guardian.check()
                    devices.eject(job["disk"])


def write_request(input_dir, request):
    path = Path(input_dir) / "request.json"
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(request, indent=2) + "\n")
    temporary.replace(path)


def verify_loop(loop, image):
    module = load_path(
        "fascia_assembler_guard", HERE.parent / "tools/assemble-image.py"
    )
    module.verify_loop(loop, image)


def load_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def linux_namespace(job, request):
    subprocess.run(["mount", "--make-rprivate", "/"], check=True)
    input_dir = Path(job["input"])
    subprocess.run(["mount", "--bind", str(input_dir), str(input_dir)], check=True)
    subprocess.run(["mount", "-o", "remount,bind,ro", str(input_dir)], check=True)
    work = Path(job["work"])
    subprocess.run(
        ["mount", "-t", "tmpfs", "-o", "mode=700,size=80%", "tmpfs", str(work)],
        check=True,
    )
    target = None
    if job["mode"] in {"image", "card"}:
        expected = request["linux_device"]
        target = expected["path"]
        load("linux-target").verify(target, expected["loop_identity"])
    args = [
        sys.executable,
        str(input_dir / "project/image/host/guest.py"),
        "--input",
        str(input_dir),
        "--output",
        job["output"],
        "--work",
        str(work),
    ]
    if target:
        args.extend(
            [
                "--target",
                target,
                "--capacity",
                str(request["capacity"]),
                "--serial",
                request["serial"],
            ]
        )
    inherited = ()
    if target:
        descriptor = request["linux_device"].get("backing_fd")
        if type(descriptor) is not int or descriptor < 0:
            raise ValueError("Missing inherited backing descriptor")
        inherited = (descriptor,)
    subprocess.run(args, check=True, pass_fds=inherited)


def restore_outputs(output, owner):
    gid = pwd.getpwuid(owner).pw_gid
    for path in [Path(output), *Path(output).rglob("*")]:
        os.chown(path, owner, gid, follow_symlinks=False)


def linux(jobpath, job, request, owner):
    if os.geteuid() != 0 or owner == 0:
        raise ValueError(
            "The Linux namespace backend needs the installer's narrow sudo handoff"
        )
    work = Path(tempfile.mkdtemp(prefix="fascia-install-", dir="/run"))
    descriptor = None
    try:
        job["work"] = str(work)
        if job["mode"] == "card":
            descriptor = devices.open_target(
                job["disk"], job["capacity"], job["identity"]
            )
        elif job["mode"] == "image":
            image = Path(job["image"])
            before = image.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size != request["capacity"]
                or before.st_uid != owner
                or before.st_nlink != 1
            ):
                raise ValueError("Invalid newly-created owned test image")
            descriptor = os.open(image, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
            ):
                raise ValueError("Test image changed while opening it")
        with contextlib.ExitStack() as stack:
            mapping = None
            if descriptor is not None:
                mapping = stack.enter_context(
                    load("linux-target").attach(descriptor, request["capacity"])
                )
                request["linux_device"] = {**mapping, "backing_fd": descriptor}
            jobpath.write_text(json.dumps(job, indent=2) + "\n")
            write_request(job["input"], request)
            subprocess.run(
                [
                    "unshare",
                    *([] if job["mode"] == "prepare" else ["--net"]),
                    "--mount",
                    "--pid",
                    "--fork",
                    "--kill-child",
                    sys.executable,
                    str(HERE / "runner.py"),
                    "--job",
                    str(jobpath),
                    "--namespace",
                ],
                check=True,
                pass_fds=(() if descriptor is None else (descriptor,)),
            )
            if mapping is not None:
                load("linux-target").verify(mapping["path"], mapping["loop_identity"])
                os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        restore_outputs(job["output"], owner)
        for path in (jobpath, Path(job["input"]) / "request.json"):
            os.chown(path, owner, pwd.getpwuid(owner).pw_gid)
        os.rmdir(work)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--namespace", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    job, request, owner, cache = read_job(args.job)
    if sys.platform == "darwin":
        mac(job, request, owner, cache)
    elif sys.platform.startswith("linux"):
        if args.namespace:
            linux_namespace(job, request)
        else:
            linux(args.job, job, request, owner)
    else:
        raise ValueError("Unsupported host")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print("Installer backend stopped: " + str(error), file=sys.stderr)
        raise SystemExit(1)
