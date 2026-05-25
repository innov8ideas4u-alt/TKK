"""Tests for spawn_ghost.get_project_root() resolution logic."""
from __future__ import annotations

from pathlib import Path

import pytest

from spawn_ghost import get_project_root


def test_env_var_takes_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLAUDE_PROJECT_DIR wins even when cwd would resolve elsewhere."""
    env_root = tmp_path / "from_env"
    env_root.mkdir()
    other = tmp_path / "from_cwd"
    (other / ".git").mkdir(parents=True)

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(env_root))
    monkeypatch.chdir(other)

    assert get_project_root() == env_root.resolve()


def test_env_var_invalid_falls_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nonexistent CLAUDE_PROJECT_DIR is ignored; walk runs."""
    bogus = tmp_path / "does_not_exist"
    project = tmp_path / "real"
    (project / ".git").mkdir(parents=True)

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(bogus))
    monkeypatch.chdir(project)

    assert get_project_root() == project.resolve()


def test_git_beats_closer_atlas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A .git/ further up the tree beats a closer orphan .atlas/."""
    outer = tmp_path / "outer"
    inner = outer / "inner"
    sub = inner / "sub"
    (outer / ".git").mkdir(parents=True)
    (inner / ".atlas").mkdir(parents=True)
    sub.mkdir(parents=True)

    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(sub)

    assert get_project_root() == outer.resolve()


def test_atlas_only_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no .git/ anywhere, an .atlas/ ancestor is accepted."""
    project = tmp_path / "scratch"
    sub = project / "deep" / "nested"
    (project / ".atlas").mkdir(parents=True)
    sub.mkdir(parents=True)

    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(sub)

    assert get_project_root() == project.resolve()
