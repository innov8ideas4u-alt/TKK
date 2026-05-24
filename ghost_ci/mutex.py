"""Daemon singleton mutex via atomic O_CREAT | O_EXCL.

Second daemon in the same directory self-terminates with a worktree error.
Stale lock from crashed daemon is reclaimed automatically.
"""
from __future__ import annotations

import os
import sys

import psutil

LOCK_FILENAME = "ghost_ci.pid"


def _read_existing_pid(lock_path: str) -> int:
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return -1


def acquire_daemon_mutex(lock_path: str, _retry: bool = True) -> bool:
    """Atomic mutex. Returns True if acquired; calls sys.exit(1) if held."""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        existing_pid = _read_existing_pid(lock_path)
        if existing_pid < 0 or not psutil.pid_exists(existing_pid):
            try:
                os.remove(lock_path)
            except FileNotFoundError:
                pass
            if _retry:
                return acquire_daemon_mutex(lock_path, _retry=False)
            return False
        cwd_name = os.path.basename(os.getcwd())
        print(
            f"FATAL: Ghost CI daemon (PID {existing_pid}) already monitoring "
            f"this directory.\n"
            f"Parallel cc sessions MUST use separate git worktrees:\n"
            f"  git worktree add ../{cwd_name}_branch\n"
            f"Then launch the second cc session inside the new worktree.",
            file=sys.stderr,
        )
        sys.exit(1)


def release_daemon_mutex(lock_path: str) -> None:
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass
