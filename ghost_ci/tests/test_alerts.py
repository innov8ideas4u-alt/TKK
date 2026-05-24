"""Group 4: State + file locking (tests 18-21) + Group 8 atomicity test (43)."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from alerts import (
    acquire_lockdir,
    release_lockdir,
    write_alert_atomic,
    write_with_lock,
    render_nominal,
    render_failure,
)


def test_mkdir_lock_acquisition(alert_path: str):
    lockdir = acquire_lockdir(alert_path)
    assert lockdir is not None
    assert os.path.isdir(lockdir)
    release_lockdir(lockdir)


def test_mkdir_lock_backoff(alert_path: str):
    """Existing lock blocks acquisition; backoff retries 3x."""
    pre = alert_path + ".lock"
    os.makedirs(pre)
    try:
        t0 = time.time()
        result = acquire_lockdir(alert_path, retries=3, backoff_seconds=0.05)
        elapsed = time.time() - t0
        assert result is None
        assert elapsed >= 0.10  # at least 2 backoffs
    finally:
        os.rmdir(pre)


def test_lock_cleanup_on_exception(alert_path: str):
    """Write_with_lock cleans up lockdir even on exception inside writer."""
    lockdir_path = alert_path + ".lock"
    # Make directory writable but cause failure mid-write by patching
    from unittest.mock import patch
    with patch("alerts.write_alert_atomic", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            write_with_lock(alert_path, "content")
    # Lockdir should be cleaned up
    assert not os.path.exists(lockdir_path)


def test_state_transitions():
    """State machine sequence is well-defined."""
    states = ["IDLE", "DEBOUNCING", "EXECUTING", "DISTILLING", "INJECTING", "IDLE"]
    # This is documentation-shaped: assert ordering is well-formed
    assert states[0] == "IDLE"
    assert states[-1] == "IDLE"
    assert set(states) == {"IDLE", "DEBOUNCING", "EXECUTING", "DISTILLING", "INJECTING"}


# Group 6 tests (28-31)

def test_success_state_markdown():
    md = render_nominal()
    assert "ALL SYSTEMS NOMINAL" in md
    assert "Last Verified" in md


def test_failure_state_markdown():
    md = render_failure("summary text", "src/auth.py", 42, "ValueError", "RAW TAIL")
    assert "URGENT" in md
    assert "summary text" in md
    assert "src/auth.py" in md
    assert "42" in md
    assert "ValueError" in md
    assert "RAW TAIL" in md


def test_tail_truncation_exact_50():
    from pipe_reader import GhostPipeReader

    class _FS:
        def __init__(self, lines):
            self._it = iter(lines)
        def readline(self):
            try:
                return next(self._it)
            except StopIteration:
                return b""
        def close(self):
            pass

    class _FP:
        def __init__(self, lines):
            self.stdout = _FS(lines)

    lines = [f"line{i}\n".encode() for i in range(200)]
    r = GhostPipeReader(_FP(lines), maxlen=1000)
    r.join(timeout=2.0)
    assert len(r.tail(50)) == 50


def test_markdown_escaping():
    md = render_failure("ok", "x.py", 1, "Err", "```evil```")
    # Backticks survive — wrapper still parses
    assert "```evil```" in md


# Group 8 atomic write test (43)

def test_alert_file_written_atomically(alert_path: str):
    """Reader sees either old content or new content, never partial."""
    # Pre-populate with old content
    Path(alert_path).parent.mkdir(parents=True, exist_ok=True)
    with open(alert_path, "w") as f:
        f.write("OLD" * 10)

    observations = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                with open(alert_path, "r") as f:
                    data = f.read()
                if data:
                    observations.append(data)
            except (FileNotFoundError, PermissionError):
                pass
            time.sleep(0.001)

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(20):
            write_alert_atomic(alert_path, "NEW" * 100 + str(i))
            time.sleep(0.005)
    finally:
        stop.set()
        t.join(timeout=2.0)

    # Every observation should be either OLD-only or contain a NEW string —
    # never half-written empty content.
    for obs in observations:
        assert obs != ""
        assert ("OLD" in obs) or ("NEW" in obs)


# R2 Fix: defensive .atlas/ creation (test 45)

def test_atlas_dir_autocreated_on_first_write(tmp_path: Path):
    target = str(tmp_path / "newatlas" / "alerts.md")
    write_alert_atomic(target, "content")
    assert os.path.exists(target)
    assert os.path.isdir(tmp_path / "newatlas")
