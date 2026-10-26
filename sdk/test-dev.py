#!/usr/bin/env python3
"""Offline process-level tests for the compiled Zig preview supervisor."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent.parent
FAKE_BUILD = r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, time
app = pathlib.Path.cwd()
events = app / '.local/events.jsonl'
def emit(**value):
    with events.open('a') as stream: stream.write(json.dumps(value) + '\n')
source = (app / 'main.zig').read_text()
emit(event='build', pid=os.getpid(), source=source)
if 'slow' in source:
    child = subprocess.Popen([sys.executable, '-c', 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
    emit(event='build-child', pid=child.pid)
    time.sleep(1)
    child.terminate()
    child.kill()
    child.wait()
if 'invalid' in source:
    emit(event='failed')
    sys.exit(1)
ident = next(app.glob('*.desktop')).stem
prefix = pathlib.Path(sys.argv[sys.argv.index('--prefix') + 1])
binary = prefix / 'preview' / ident
if sys.platform == 'darwin': binary = prefix / 'preview' / (ident + '.app') / 'Contents/MacOS' / ident
binary.parent.mkdir(parents=True, exist_ok=True)
script = """#!/usr/bin/env python3
import json,os,pathlib,signal,subprocess,sys,time
events = pathlib.Path(os.environ['XDG_STATE_HOME']) / 'events.jsonl'
def emit(**value):
    with events.open('a') as stream: stream.write(json.dumps(value)+'\\n')
emit(event='start',pid=os.getpid(),source=SOURCE,id=IDENT,args=sys.argv[1:],root=str(pathlib.Path.cwd()),gains=os.environ['FASCIA_DISPLAY_GAINS'])
if 'child' in SOURCE:
    child = subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
    emit(event='preview-child',pid=child.pid)
while True: time.sleep(1)
""".replace('SOURCE',repr(source)).replace('IDENT',repr(ident))
temporary = binary.with_suffix('.next')
temporary.write_text(script)
temporary.chmod(0o755)
os.replace(temporary,binary)
emit(event='built')
'''


class DevTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compilation = tempfile.TemporaryDirectory(prefix="fascia-dev-compiler-")
        cls.runner = Path(cls.compilation.name) / "fascia-dev"
        result = subprocess.run(
            [
                "mise",
                "exec",
                "--",
                "zig",
                "build-exe",
                str(ROOT / "sdk/dev.zig"),
                "-lc",
                "-O",
                "ReleaseFast",
                "-femit-bin=" + str(cls.runner),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.compilation.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="fascia-dev-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.app = self.root / "app"
        (self.app / ".local/preview").mkdir(parents=True)
        self.events_path = self.app / ".local/preview/state/events.jsonl"
        # Build events and app events share the same ignored output file.
        (self.app / ".local/events.jsonl").symlink_to(self.events_path)
        (self.app / "demo.desktop").write_text("[Desktop Entry]\nName=Demo\n")
        self.source("version one")
        self.sdk = self.root / "sdk"
        self.sdk.mkdir()
        (self.sdk / "layout.zig").write_text("SDK one")
        (self.root / "build.zig").write_text("build one")
        (self.root / "build.zig.zon").write_text("package one")
        self.compiler = self.root / "fake-zig"
        self.compiler.write_text(FAKE_BUILD)
        self.compiler.chmod(0o755)
        self.log = (self.root / "dev.log").open("w+")
        self.addCleanup(self.log.close)
        self.process = None
        self.addCleanup(self.shutdown)

    def source(self, value):
        (self.app / "main.zig").write_text(value)

    def start(self):
        self.process = subprocess.Popen(
            [
                str(self.runner),
                str(self.compiler),
                str(self.app),
                str(self.app / "zig-out"),
                "-Doptimize=ReleaseFast",
                str(self.sdk),
                str(self.root / "build.zig"),
                "example-argument",
            ],
            stdout=self.log,
            stderr=self.log,
            start_new_session=True,
        )

    def shutdown(self):
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def events(self, name):
        try:
            lines = self.events_path.read_text().splitlines()
        except FileNotFoundError:
            return []
        result = []
        for line in lines:
            try:
                value = json.loads(line)
                if value["event"] == name:
                    result.append(value)
            except json.JSONDecodeError:
                pass
        return result

    def wait(self, predicate, timeout=6):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.03)
        self.fail("Timed out:\n" + (self.root / "dev.log").read_text())

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_edit_restart_arguments_and_environment(self):
        self.start()
        self.wait(lambda: len(self.events("start")) == 1)
        first = self.events("start")[0]
        self.assertEqual(first["args"], ["example-argument"])
        self.assertEqual(Path(first["root"]).resolve(), self.app.resolve())
        self.assertEqual(first["gains"], "1,1,1")
        self.source("version two")
        self.wait(lambda: len(self.events("start")) == 2)
        self.assertEqual(self.events("start")[-1]["source"], "version two")
        self.assertFalse(self.alive(first["pid"]))

    def test_compile_error_keeps_preview_then_recovers(self):
        self.start()
        self.wait(lambda: len(self.events("start")) == 1)
        first = self.events("start")[0]["pid"]
        self.source("invalid code")
        self.wait(lambda: len(self.events("failed")) == 1)
        time.sleep(0.2)
        self.assertTrue(self.alive(first))
        self.assertEqual(len(self.events("start")), 1)
        self.source("fixed code")
        self.wait(lambda: len(self.events("start")) == 2)
        self.assertFalse(self.alive(first))

    def test_initial_compile_error_is_resumable(self):
        self.source("invalid initial code")
        self.start()
        self.wait(lambda: len(self.events("failed")) == 1)
        self.source("fixed initial code")
        self.wait(lambda: len(self.events("start")) == 1)

    def test_generated_outputs_do_not_trigger_builds(self):
        self.start()
        self.wait(lambda: len(self.events("start")) == 1)
        for directory in (".local", ".zig-cache", "zig-out", "zig-pkg"):
            path = self.app / directory / "generated.zig"
            path.parent.mkdir(exist_ok=True)
            path.write_text("generated")
        time.sleep(0.6)
        self.assertEqual(len(self.events("build")), 1)

    def test_sdk_asset_and_manifest_edits(self):
        self.start()
        self.wait(lambda: len(self.events("start")) == 1)
        (self.sdk / "layout.zig").write_text("SDK two")
        self.wait(lambda: len(self.events("start")) == 2)
        (self.app / "photo.png").write_bytes(b"image fixture")
        self.wait(lambda: len(self.events("start")) == 3)
        (self.app / "demo.desktop").rename(self.app / "renamed.desktop")
        self.wait(lambda: len(self.events("start")) == 4)
        self.assertEqual(self.events("start")[-1]["id"], "renamed")

    def test_edits_during_build_skip_stale_restart(self):
        self.start()
        self.wait(lambda: len(self.events("start")) == 1)
        self.source("slow stale version")
        self.wait(lambda: len(self.events("build-child")) == 1)
        self.source("latest version")
        self.wait(lambda: len(self.events("start")) == 2)
        self.assertEqual(self.events("start")[-1]["source"], "latest version")
        self.assertEqual(len(self.events("build")), 3)

    def test_closed_preview_reopens_on_next_edit(self):
        self.start()
        self.wait(lambda: len(self.events("start")) == 1)
        os.kill(self.events("start")[0]["pid"], signal.SIGTERM)
        time.sleep(0.3)
        self.assertIsNone(self.process.poll())
        self.source("reopen")
        self.wait(lambda: len(self.events("start")) == 2)

    def test_interrupt_cleans_preview_build_and_descendants(self):
        self.source("version child")
        self.start()
        self.wait(lambda: len(self.events("preview-child")) == 1)
        self.source("slow build")
        self.wait(lambda: len(self.events("build-child")) == 1)
        pids = [
            self.events(name)[-1]["pid"]
            for name in ("start", "preview-child", "build", "build-child")
        ]
        self.shutdown()
        self.assertEqual(self.process.returncode, 0)
        self.wait(lambda: not any(self.alive(pid) for pid in pids))


if __name__ == "__main__":
    unittest.main()
