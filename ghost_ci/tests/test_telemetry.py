"""Phase 2.5a — telemetry logger tests."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from telemetry import build_record, log_telemetry


def test_log_telemetry_creates_atlas_dir(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    rec = build_record(
        trigger_file="src/x.py",
        test_target="--testmon",
        selection_mode="testmon",
        exit_code=0,
        pytest_output_bytes=100,
        pytest_duration_ms=500,
    )
    ok = log_telemetry(project, rec)
    assert ok is True
    atlas = project / ".atlas"
    assert atlas.is_dir()
    tel = atlas / "ghost_telemetry.jsonl"
    assert tel.exists()
    lines = tel.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["event"] == "ghost_validation"
    assert obj["trigger_file"] == "src/x.py"


def test_log_telemetry_appends_not_overwrites(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    for i in range(3):
        rec = build_record(
            trigger_file=f"src/f{i}.py",
            test_target="--testmon",
            selection_mode="testmon",
            exit_code=i,
            pytest_output_bytes=10 * i,
            pytest_duration_ms=100 * i,
        )
        assert log_telemetry(project, rec) is True
    tel = project / ".atlas" / "ghost_telemetry.jsonl"
    lines = tel.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert [p["trigger_file"] for p in parsed] == ["src/f0.py", "src/f1.py", "src/f2.py"]
    assert [p["exit_code"] for p in parsed] == [0, 1, 2]


def test_log_telemetry_handles_missing_timestamp(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    rec = {"event": "ghost_validation", "exit_code": 0}
    assert "timestamp" not in rec
    ok = log_telemetry(project, rec)
    assert ok is True
    tel = project / ".atlas" / "ghost_telemetry.jsonl"
    obj = json.loads(tel.read_text(encoding="utf-8").splitlines()[0])
    assert "timestamp" in obj
    ts = obj["timestamp"]
    assert ts.endswith("Z")
    # ISO 8601 sanity: YYYY-MM-DDTHH:MM:SS...Z
    assert "T" in ts and len(ts) >= 20


def test_log_telemetry_swallows_errors(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    rec = build_record(
        trigger_file=None,
        test_target=None,
        selection_mode="warm_up",
        exit_code=0,
        pytest_output_bytes=0,
        pytest_duration_ms=0,
    )
    with patch("telemetry.open", side_effect=PermissionError("denied")):
        result = log_telemetry(project, rec)
    assert result is False  # No exception propagates


def test_build_record_field_completeness():
    rec = build_record(
        trigger_file="x.py",
        test_target="--testmon",
        selection_mode="testmon",
        exit_code=0,
        pytest_output_bytes=0,
        pytest_duration_ms=0,
    )
    expected = {
        "timestamp", "event", "trigger_file", "test_target", "selection_mode",
        "exit_code", "pytest_output_bytes", "pytest_duration_ms",
        "distillation_attempted", "distillation_succeeded", "distillation_tokens_out",
        "ollama_latency_ms", "model_resident", "alert_written",
    }
    assert set(rec.keys()) == expected
    assert rec["event"] == "ghost_validation"
    assert rec["distillation_attempted"] is False
    assert rec["distillation_succeeded"] is None
    assert rec["distillation_tokens_out"] is None
    assert rec["ollama_latency_ms"] is None
    assert rec["model_resident"] is None
    assert rec["alert_written"] is False
