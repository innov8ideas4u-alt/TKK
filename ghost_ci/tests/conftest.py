"""Shared test fixtures."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make ghost_ci modules importable as top-level (matches daemon.py runtime path)
_GHOST_DIR = Path(__file__).resolve().parent.parent
if str(_GHOST_DIR) not in sys.path:
    sys.path.insert(0, str(_GHOST_DIR))

import pytest


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Fresh project root with .atlas/ created."""
    (tmp_path / ".atlas").mkdir()
    return tmp_path


@pytest.fixture
def alert_path(tmp_project: Path) -> str:
    return str(tmp_project / ".atlas" / "00-urgent-alerts.md")
