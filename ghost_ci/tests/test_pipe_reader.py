"""Group 2: Subprocess + pipe reader (tests 9-13)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from io import BytesIO

import pytest

from pipe_reader import GhostPipeReader


class FakeStdout:
    def __init__(self, lines: list[bytes]):
        self._iter = iter(lines)

    def readline(self):
        try:
            return next(self._iter)
        except StopIteration:
            return b""

    def close(self):
        pass


class FakeProc:
    def __init__(self, lines: list[bytes]):
        self.stdout = FakeStdout(lines)


def test_process_spawn_success():
    """test_process_spawn_success — valid Popen on trigger."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('hello')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=0x08000000,
    )
    out, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0
    assert b"hello" in out


def test_mid_run_interruption():
    """SIGTERM a long-running subprocess cleanly."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=0x08000000,
    )
    import psutil
    parent = psutil.Process(proc.pid)
    parent.terminate()
    try:
        parent.wait(timeout=5)
    except psutil.TimeoutExpired:
        parent.kill()
    assert proc.poll() is not None


def test_zombie_process_cleanup():
    """Process killed externally is reaped."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        creationflags=0x08000000,
    )
    proc.kill()
    proc.wait(timeout=5)
    assert proc.returncode is not None


def test_pipe_reader_no_deadlock():
    """5MB of garbage on stdout — deque truncates cleanly, no hang."""
    lines = [(b"x" * 1000 + b"\n") for _ in range(5000)]  # ~5MB
    fake = FakeProc(lines)
    reader = GhostPipeReader(fake, maxlen=100)
    reader.join(timeout=5.0)
    assert len(reader.buffer) == 100  # bounded


def test_stderr_redirected_to_stdout():
    """When stderr=STDOUT, tracebacks land in stdout."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stderr.write('ERR\\n'); sys.stdout.write('OUT\\n')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=0x08000000,
    )
    out, _ = proc.communicate(timeout=10)
    assert b"ERR" in out
    assert b"OUT" in out
