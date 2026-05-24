"""Atomic alert writer for .atlas/00-urgent-alerts.md.

Combines tempfile + os.replace atomic write with a lockdir for cross-reader
coordination. The lockdir prevents cc from reading mid-write; the atomic
write guarantees zero observable partial state.
"""
from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timezone

ALERT_FILENAME = "00-urgent-alerts.md"
LOCKDIR_SUFFIX = ".lock"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_alert_atomic(target_path: str, content: str) -> None:
    """Atomic write — readers never see partial/empty files.

    Defensively creates the parent directory (R2 Fix #3b — .atlas/ may not
    pre-exist in fresh projects).
    """
    directory = os.path.dirname(target_path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix="alert_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        last_err: Exception | None = None
        for _ in range(5):
            try:
                os.replace(tmp_path, target_path)
                last_err = None
                break
            except PermissionError as e:
                last_err = e
                time.sleep(0.02)
        if last_err is not None:
            raise last_err
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def acquire_lockdir(target_path: str, retries: int = 3, backoff_seconds: float = 0.05) -> str | None:
    """Returns lockdir path if acquired, None otherwise."""
    lockdir = target_path + LOCKDIR_SUFFIX
    for attempt in range(retries):
        try:
            os.makedirs(lockdir, exist_ok=False)
            return lockdir
        except FileExistsError:
            time.sleep(backoff_seconds)
    return None


def release_lockdir(lockdir: str) -> None:
    try:
        os.rmdir(lockdir)
    except OSError:
        pass


def render_nominal() -> str:
    return (
        f"# ALL SYSTEMS NOMINAL - NO ACTIVE ALERTS\n"
        f"**Last Verified:** {_now_iso()}\n"
    )


def render_failure(
    summary: str,
    failing_file: str,
    line_number: int | None,
    exception_type: str,
    raw_tail: str,
) -> str:
    line_str = str(line_number) if line_number is not None else "unknown"
    return (
        f"# URGENT CI ALERTS - IMMEDIATE ACTION REQUIRED\n"
        f"**Generated:** {_now_iso()}\n\n"
        f"> Ghost CI detected a failure resulting from your last file modification.\n"
        f"> Do NOT proceed with new features. Fix this immediately.\n\n"
        f"### 1. DISTILLED ROOT CAUSE\n"
        f"{summary}\n\n"
        f"### 2. FAILING TARGET\n"
        f"- **File:** `{failing_file}`\n"
        f"- **Line:** {line_str}\n"
        f"- **Exception:** `{exception_type}`\n\n"
        f"### 3. RAW TRACEBACK TAIL\n"
        f"```\n{raw_tail}\n```\n"
    )


def render_fallback(raw_tail: str) -> str:
    return (
        f"# URGENT CI ALERTS - IMMEDIATE ACTION REQUIRED\n"
        f"**Generated:** {_now_iso()}\n\n"
        f"> SYSTEM ALERT: Local error distiller unreachable. Raw traceback provided below.\n\n"
        f"### RAW TRACEBACK\n"
        f"```\n{raw_tail}\n```\n"
    )


def write_with_lock(target_path: str, content: str) -> bool:
    """High-level: acquire lockdir, write atomically, release. Returns success."""
    lockdir = acquire_lockdir(target_path)
    if lockdir is None:
        return False
    try:
        write_alert_atomic(target_path, content)
        return True
    finally:
        release_lockdir(lockdir)
