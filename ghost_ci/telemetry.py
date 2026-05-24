"""Append-only telemetry logger for Ghost CI. Phase 2.5a.

No analyzer logic. No estimates. Just raw measurements.
Logger errors MUST NOT crash the daemon - wrap in try/except, return bool.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("ghost_ci.telemetry")

TELEMETRY_FILENAME = "ghost_telemetry.jsonl"


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log_telemetry(project_root, record: dict) -> bool:
    """Append one telemetry record. Returns True on success, False on any error.

    Atomic append: single write call <4KB is atomic on NTFS.
    Failure-safe: never raises. All failure modes swallow + log + return False.
    """
    try:
        atlas_dir = Path(project_root) / ".atlas"
        atlas_dir.mkdir(parents=True, exist_ok=True)
        telemetry_path = atlas_dir / TELEMETRY_FILENAME

        if "timestamp" not in record:
            record["timestamp"] = _now_iso_z()

        line = json.dumps(record, separators=(",", ":")) + "\n"

        with open(telemetry_path, "a", encoding="utf-8") as f:
            f.write(line)
        return True
    except Exception as e:
        logger.warning("Telemetry write failed (non-fatal): %s", e)
        return False


def build_record(
    trigger_file: str | None,
    test_target: str | None,
    selection_mode: str,
    exit_code: int,
    pytest_output_bytes: int,
    pytest_duration_ms: int,
    distillation_attempted: bool = False,
    distillation_succeeded: bool | None = None,
    distillation_tokens_out: int | None = None,
    ollama_latency_ms: int | None = None,
    model_resident: bool | None = None,
    alert_written: bool = False,
) -> dict:
    """Construct a telemetry record with all 14 schema fields populated."""
    return {
        "timestamp": _now_iso_z(),
        "event": "ghost_validation",
        "trigger_file": trigger_file,
        "test_target": test_target,
        "selection_mode": selection_mode,
        "exit_code": exit_code,
        "pytest_output_bytes": pytest_output_bytes,
        "pytest_duration_ms": pytest_duration_ms,
        "distillation_attempted": distillation_attempted,
        "distillation_succeeded": distillation_succeeded,
        "distillation_tokens_out": distillation_tokens_out,
        "ollama_latency_ms": ollama_latency_ms,
        "model_resident": model_resident,
        "alert_written": alert_written,
    }
