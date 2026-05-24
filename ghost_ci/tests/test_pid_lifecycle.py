"""Group 3: PID-Pinned lifecycle (tests 14-17) + Group 9 spawn-resolution tests."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

import mutex as mutex_mod
from mutex import acquire_daemon_mutex, release_daemon_mutex


def test_daemon_acquires_pid_mutex(tmp_path: Path):
    lock = str(tmp_path / "ghost_ci.pid")
    assert acquire_daemon_mutex(lock) is True
    assert os.path.exists(lock)
    with open(lock) as f:
        assert int(f.read().strip()) == os.getpid()
    release_daemon_mutex(lock)


def test_second_daemon_self_terminates(tmp_path: Path):
    lock = str(tmp_path / "ghost_ci.pid")
    # Hand-write a lock owned by current (live) process
    with open(lock, "w") as f:
        f.write(str(os.getpid()))
    with pytest.raises(SystemExit) as exc:
        acquire_daemon_mutex(lock)
    assert exc.value.code == 1


def test_stale_mutex_reclaimed(tmp_path: Path):
    lock = str(tmp_path / "ghost_ci.pid")
    # Write a definitely-dead PID
    with open(lock, "w") as f:
        f.write("999999")
    assert acquire_daemon_mutex(lock) is True
    with open(lock) as f:
        assert int(f.read().strip()) == os.getpid()
    release_daemon_mutex(lock)


def test_pid_heartbeat_detects_parent_death():
    """SystemObserverThread exits when parent_pid is gone.

    We mock psutil.pid_exists to return False, simulating parent death.
    """
    from daemon import SystemObserverThread

    with patch("daemon.psutil.pid_exists", return_value=False), \
         patch("daemon.cleanup_locks"), \
         patch("daemon.kill_pytest_subprocesses"), \
         patch("os._exit") as fake_exit:
        t = SystemObserverThread(parent_pid=999999, config_path="nonexistent.json",
                                 project_root=".", poll_seconds=0.1)
        t.run()
        fake_exit.assert_called_once_with(0)


# Group 9 R2 tests --------------------------------------------------

def test_daemon_path_resolves_absolute():
    """spawn_ghost.py builds DAEMON_PATH via __file__ resolution."""
    from pathlib import Path as P
    import spawn_ghost
    script_dir = P(spawn_ghost.__file__).resolve().parent
    daemon_path = script_dir / "daemon.py"
    assert daemon_path.is_absolute()
    assert daemon_path.exists()
    assert daemon_path.name == "daemon.py"


# Group 10 v5 tests --------------------------------------------------

def test_pid_traverses_to_node_exe():
    """get_claude_node_pid walks tree until node.exe."""
    from spawn_ghost import get_claude_node_pid

    fake_node = MagicMock()
    fake_node.name.return_value = "node.exe"
    fake_node.pid = 7777

    fake_cmd = MagicMock()
    fake_cmd.name.return_value = "cmd.exe"
    fake_cmd.pid = 555
    fake_cmd.parent.return_value = fake_node

    fake_self = MagicMock()
    fake_self.parent.return_value = fake_cmd

    with patch("spawn_ghost.psutil.Process", return_value=fake_self):
        # Walk: self -> cmd -> node
        result = get_claude_node_pid()
    assert result == 7777


def test_pid_fallback_when_no_node_exe():
    """get_claude_node_pid falls back to os.getppid() when no node.exe in tree."""
    from spawn_ghost import get_claude_node_pid

    fake_cmd = MagicMock()
    fake_cmd.name.return_value = "cmd.exe"
    fake_cmd.parent.return_value = None

    fake_self = MagicMock()
    fake_self.parent.return_value = fake_cmd

    with patch("spawn_ghost.psutil.Process", return_value=fake_self), \
         patch("spawn_ghost.os.getppid", return_value=12345):
        result = get_claude_node_pid()
    assert result == 12345


def test_project_root_resolves_from_subdirectory(tmp_path: Path, monkeypatch):
    """get_project_root walks up to find .git/."""
    from spawn_ghost import get_project_root

    root = tmp_path / "repo"
    sub = root / "src" / "hooks"
    sub.mkdir(parents=True)
    (root / ".git").mkdir()

    monkeypatch.chdir(sub)
    assert get_project_root().resolve() == root.resolve()
