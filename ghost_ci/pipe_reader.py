"""Deadlock-free pipe reader. Dedicated thread drains pytest stdout into a
bounded deque so the OS pipe buffer never fills.
"""
from __future__ import annotations

import threading
from collections import deque


class GhostPipeReader:
    def __init__(self, process, maxlen: int = 1000):
        self.process = process
        self.buffer: deque[str] = deque(maxlen=maxlen)
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        try:
            for line in iter(self.process.stdout.readline, b""):
                decoded = line.decode("utf-8", errors="replace").rstrip()
                if decoded:
                    self.buffer.append(decoded)
        finally:
            try:
                self.process.stdout.close()
            except Exception:
                pass

    def tail(self, n: int = 50) -> list[str]:
        return list(self.buffer)[-n:]

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout=timeout)
