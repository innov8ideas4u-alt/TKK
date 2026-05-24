"""Watchdog event handler with two-stage filter + per-file debouncer."""
from __future__ import annotations

import os
import threading
import time

from watchdog.events import FileSystemEventHandler

IGNORED_DIRS = (
    ".git", "__pycache__", ".pytest_cache", ".tkk", ".atlas",
    "venv", "env", ".tox", "node_modules", ".vscode", ".idea",
    ".ghost_pytest_cache",
)
IGNORED_SUFFIXES = (".pyc", ".pyo", ".pyi", ".swp", ".swo", ".bak", "~")


def is_valid_target(path: str) -> bool:
    """Three-stage filter — each stage independently testable."""
    if not path.endswith(".py"):
        return False
    if any(path.endswith(s) for s in IGNORED_SUFFIXES):
        return False
    parts = os.path.normpath(path).split(os.sep)
    if any(p in IGNORED_DIRS for p in parts):
        return False
    filename = os.path.basename(path)
    if filename.startswith("test_") or filename.endswith("_test.py"):
        return False
    return True


class GhostCIEventHandler(FileSystemEventHandler):
    def __init__(self, debouncer: "MultiFileDebouncer"):
        self.debouncer = debouncer

    def on_modified(self, event):
        if not event.is_directory and is_valid_target(event.src_path):
            self.debouncer.trigger(event.src_path)


class MultiFileDebouncer:
    """Per-file debounce. A single last_path drops concurrent edits silently."""

    def __init__(self, debounce_seconds: float = 1.5):
        self.debounce_seconds = debounce_seconds
        self._files: dict[str, float] = {}
        self._lock = threading.Lock()

    def trigger(self, file_path: str) -> None:
        with self._lock:
            self._files[file_path] = time.time()

    def get_ready_files(self) -> list[str]:
        ready: list[str] = []
        now = time.time()
        with self._lock:
            for path, ts in list(self._files.items()):
                if now - ts >= self.debounce_seconds:
                    ready.append(path)
                    del self._files[path]
        return ready
