"""Group 1: Watchdog & event filtering (tests 1-8)."""
from __future__ import annotations

import time

import pytest

from event_handler import MultiFileDebouncer, is_valid_target


def test_watchdog_ignores_pycache():
    assert is_valid_target("project/__pycache__/auth.cpython-313.pyc") is False
    assert is_valid_target("__pycache__/mod.pyc") is False


def test_watchdog_ignores_pytest_cache():
    assert is_valid_target(".pytest_cache/v/cache/stepwise") is False
    assert is_valid_target("proj/.pytest_cache/foo.py") is False


def test_watchdog_ignores_git_dir():
    assert is_valid_target(".git/HEAD") is False
    assert is_valid_target("project/.git/index") is False


def test_watchdog_ignores_atlas_dir():
    assert is_valid_target(".atlas/01-brief.md") is False
    assert is_valid_target("proj/.atlas/something.py") is False


def test_watchdog_ignores_swap_files():
    assert is_valid_target("auth.py.swp") is False
    assert is_valid_target("auth.py~") is False
    assert is_valid_target("auth.py.bak") is False


def test_watchdog_ignores_test_files():
    assert is_valid_target("tests/test_auth.py") is False
    assert is_valid_target("foo_test.py") is False
    # Real source still passes
    assert is_valid_target("src/auth.py") is True


def test_debounce_collapses_rapid_saves():
    d = MultiFileDebouncer(debounce_seconds=0.3)
    for _ in range(5):
        d.trigger("auth.py")
        time.sleep(0.02)
    # Immediately after: not yet ready
    assert d.get_ready_files() == []
    time.sleep(0.35)
    ready = d.get_ready_files()
    assert ready == ["auth.py"]
    # Subsequent call: file removed
    assert d.get_ready_files() == []


def test_debounce_timer_reset():
    d = MultiFileDebouncer(debounce_seconds=0.5)
    d.trigger("a.py")
    time.sleep(0.3)
    d.trigger("a.py")  # reset
    time.sleep(0.3)
    assert d.get_ready_files() == []
    time.sleep(0.3)
    assert d.get_ready_files() == ["a.py"]
