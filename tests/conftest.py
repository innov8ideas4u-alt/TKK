"""pytest configuration for tkk read-guard tests."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import uuid

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "hooks" / "tkk_read_guard.py"


@pytest.fixture
def tkk_home(tmp_path: pathlib.Path) -> pathlib.Path:
    home = tmp_path / "tkk_home"
    home.mkdir()
    return home


@pytest.fixture
def session_id() -> str:
    return "test-" + uuid.uuid4().hex[:8]


@pytest.fixture
def run_hook(tkk_home, monkeypatch):
    """Return a callable that invokes the hook as a subprocess and returns
    (returncode, stdout, stderr)."""

    def _run(payload: dict, extra_env: dict | None = None, timeout: float = 10.0):
        env = os.environ.copy()
        env["TKK_HOME"] = str(tkk_home)
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr

    return _run


@pytest.fixture
def make_read_payload(session_id):
    def _mk(file_path: str, offset=None, limit=None, sid: str | None = None) -> dict:
        ti: dict = {"file_path": file_path}
        if offset is not None:
            ti["offset"] = offset
        if limit is not None:
            ti["limit"] = limit
        return {
            "session_id": sid or session_id,
            "tool_name": "Read",
            "tool_input": ti,
        }

    return _mk


@pytest.fixture
def sample_file(tmp_path: pathlib.Path) -> pathlib.Path:
    p = tmp_path / "sample.py"
    p.write_text("\n".join(f"line {i}" for i in range(2000)), encoding="utf-8")
    # Push mtime well into the past so the freshness bypass doesn't fire.
    past = time.time() - 3600
    os.utime(p, (past, past))
    return p


def read_session_log(tkk_home: pathlib.Path, session_id: str) -> list[dict]:
    path = tkk_home / "read_log" / f"{session_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_decisions(tkk_home: pathlib.Path) -> list[dict]:
    path = tkk_home / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_counter(tkk_home: pathlib.Path, session_id: str) -> dict:
    path = tkk_home / "counters" / f"{session_id}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_config(tkk_home: pathlib.Path, cfg: dict) -> None:
    (tkk_home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
