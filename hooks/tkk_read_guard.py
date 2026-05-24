#!/usr/bin/env python3
"""TKK Phase 1 - Read Guard PreToolUse hook for Claude Code.

Blocks redundant Read tool calls. Stdlib only. UTF-8 / LF.

State root: ~/.tkk/ (override via TKK_HOME env var for tests).

Exit codes:
    0 = allow (Claude proceeds with the Read tool call)
    2 = block (Claude must not call Read; stderr message shown to user)

Stdout protocol:
    Bypass 1 (override sentinel) ONLY: a single JSON object mutating
    tool_input. Every other code path emits empty stdout.
"""
from __future__ import annotations

import errno
import fnmatch
import json
import os
import pathlib
import sys
import time
import traceback
from typing import Any

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "overlap_block_threshold": 0.8,
    "small_read_exemption_lines": 100,
    "rule_b_max_limit": 1000,
    "override_sentinel": 999911,
    "override_replacement_limit": 2000,
    "always_fresh_patterns": [
        "*.log",
        "**/scratch/**",
        "**/tmp/**",
        "**/.tkk/**",
        "**/logs/**",
        "**/.atlas/03-active.md",
    ],
    "always_fresh_age_seconds": 60,
    "state_retention_hours": 4,
    "state_retention_calls": 200,
    "lock_timeout_seconds": 5,
    "stale_lock_age_seconds": 30,
    "max_decisions_log_mb": 100,
    "max_errors_log_mb": 10,
    "log_decisions": True,
    "case_insensitive_paths": True,
}


def tkk_home() -> pathlib.Path:
    env = os.environ.get("TKK_HOME")
    if env:
        return pathlib.Path(env)
    return pathlib.Path.home() / ".tkk"


def ensure_dirs() -> None:
    home = tkk_home()
    for sub in ("read_log", "counters", "locks"):
        (home / sub).mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    cfg_path = tkk_home() / "config.json"
    cfg = dict(DEFAULT_CONFIG)
    if cfg_path.exists():
        try:
            with cfg_path.open("r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update(user)
        except Exception as exc:
            log_error(f"config_load_failed: {exc!r}")
    return cfg


# ---------------------------------------------------------------------------
# Error log
# ---------------------------------------------------------------------------

def log_error(msg: str) -> None:
    try:
        ensure_dirs()
        path = tkk_home() / "errors.log"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(f"[{stamp}] {msg}\n")
    except Exception:
        pass  # never raise from the error logger


# ---------------------------------------------------------------------------
# Lockfile (atomic os.mkdir)
# ---------------------------------------------------------------------------

def acquire_lock(session_id: str, timeout_s: float, stale_age_s: float) -> pathlib.Path | None:
    lockpath = tkk_home() / "locks" / f"{session_id}.lock"
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_s
    while True:
        try:
            os.mkdir(lockpath)
            return lockpath
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lockpath)
                if age > stale_age_s:
                    try:
                        os.rmdir(lockpath)
                    except OSError:
                        pass
                    continue
            except FileNotFoundError:
                continue
            if time.time() >= deadline:
                return None
            time.sleep(0.05)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                if time.time() >= deadline:
                    return None
                time.sleep(0.05)
                continue
            log_error(f"lock_oserror: {exc!r}")
            return None


def release_lock(lockpath: pathlib.Path | None) -> None:
    if lockpath is None:
        return
    try:
        os.rmdir(lockpath)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log_error(f"lock_release_failed: {exc!r}")


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def session_log_path(session_id: str) -> pathlib.Path:
    return tkk_home() / "read_log" / f"{session_id}.jsonl"


def counter_path(session_id: str) -> pathlib.Path:
    return tkk_home() / "counters" / f"{session_id}.json"


def decisions_path() -> pathlib.Path:
    return tkk_home() / "decisions.jsonl"


def load_entries(session_id: str) -> list[dict[str, Any]]:
    path = session_log_path(session_id)
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        out.append(obj)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        log_error(f"state_load_failed: {exc!r}")
    return out


def atomic_write_jsonl(path: pathlib.Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False, sort_keys=True))
            f.write("\n")
    os.replace(tmp, path)


def append_jsonl(path: pathlib.Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True))
        f.write("\n")


def prune_entries(entries: list[dict[str, Any]], retention_hours: float, retention_calls: int) -> list[dict[str, Any]]:
    cutoff = time.time() - retention_hours * 3600.0
    kept = [e for e in entries if float(e.get("ts", 0.0)) >= cutoff]
    if len(kept) > retention_calls:
        kept = kept[-retention_calls:]
    return kept


# ---------------------------------------------------------------------------
# Counter shard
# ---------------------------------------------------------------------------

COUNTER_FIELDS = (
    "total_invocations",
    "allowed_rule_a",
    "allowed_rule_b",
    "allowed_rule_c_overlap_ok",
    "allowed_bypass_override",
    "allowed_bypass_fresh_path",
    "allowed_bypass_fresh_age",
    "allowed_bypass_file_changed",
    "blocked_rule_c",
    "blocked_rule_d",
    "lock_timeouts",
    "errors",
)


def load_counter(session_id: str) -> dict[str, Any]:
    path = counter_path(session_id)
    base: dict[str, Any] = {"session_id": session_id, "started_at": time.time()}
    for f in COUNTER_FIELDS:
        base[f] = 0
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fp:
                disk = json.load(fp)
            if isinstance(disk, dict):
                for k, v in disk.items():
                    base[k] = v
        except Exception as exc:
            log_error(f"counter_load_failed: {exc!r}")
    return base


def save_counter(session_id: str, counter: dict[str, Any]) -> None:
    path = counter_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(counter, f, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def bump_counter(counter: dict[str, Any], field: str) -> None:
    counter["total_invocations"] = int(counter.get("total_invocations", 0)) + 1
    if field:
        counter[field] = int(counter.get(field, 0)) + 1


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------

def normalize_path(file_path: str, case_insensitive: bool) -> tuple[str, str]:
    """Return (norm_key, orig_resolved_str). norm_key uses forward slashes."""
    try:
        resolved = pathlib.Path(file_path).resolve()
        orig = str(resolved)
    except Exception:
        orig = str(file_path)
    norm = orig.replace("\\", "/")
    if case_insensitive:
        norm = norm.lower()
    return norm, orig


def fnmatch_any(norm_key: str, patterns: list[str]) -> bool:
    for pat in patterns:
        try:
            if fnmatch.fnmatch(norm_key, pat.lower()):
                return True
            # Also try matching against basename for simple patterns like "*.log"
            if "/" not in pat and fnmatch.fnmatch(os.path.basename(norm_key), pat.lower()):
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Overlap math
# ---------------------------------------------------------------------------

def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    sorted_iv = sorted(intervals, key=lambda x: x[0])
    merged: list[tuple[int, int]] = [sorted_iv[0]]
    for start, end in sorted_iv[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def overlap_with_union(new_start: int, new_end: int, prior: list[tuple[int, int]]) -> tuple[int, int]:
    merged = merge_intervals(prior)
    new_lines = max(0, new_end - new_start)
    overlap_lines = 0
    for s, e in merged:
        lo = max(new_start, s)
        hi = min(new_end, e)
        if hi > lo:
            overlap_lines += hi - lo
    return overlap_lines, new_lines


# ---------------------------------------------------------------------------
# Decision recording
# ---------------------------------------------------------------------------

def record_decision(
    session_id: str,
    entry: dict[str, Any],
    counter: dict[str, Any],
    counter_field: str,
    log_decisions: bool,
    append_to_session_log: bool = True,
) -> None:
    """Append to per-session log + global decisions.jsonl, bump counter, save counter."""
    bump_counter(counter, counter_field)
    save_counter(session_id, counter)
    if not log_decisions:
        return
    if append_to_session_log:
        try:
            append_jsonl(session_log_path(session_id), entry)
        except Exception as exc:
            log_error(f"session_log_append_failed: {exc!r}")
    global_entry = dict(entry)
    global_entry["session_id"] = session_id
    try:
        append_jsonl(decisions_path(), global_entry)
    except Exception as exc:
        log_error(f"decisions_log_append_failed: {exc!r}")


# ---------------------------------------------------------------------------
# Main decision logic
# ---------------------------------------------------------------------------

def main() -> int:
    lock = None
    session_id = "unknown"
    try:
        ensure_dirs()
        cfg = load_config()

        # Read stdin
        try:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
        except Exception as exc:
            log_error(f"stdin_parse_failed: {exc!r}")
            return 0  # default-allow, empty stdout

        session_id = str(payload.get("session_id") or "unknown")
        tool_input = payload.get("tool_input") or {}
        file_path = tool_input.get("file_path")
        offset = tool_input.get("offset")
        limit = tool_input.get("limit")

        if not file_path:
            log_error("missing_file_path")
            return 0

        # Kill switch — pass through cleanly, no decision log
        if not cfg.get("enabled", True):
            return 0

        norm_key, orig_resolved = normalize_path(str(file_path), bool(cfg.get("case_insensitive_paths", True)))

        # mtime (may be missing if file doesn't exist yet)
        try:
            current_mtime: float | None = os.path.getmtime(orig_resolved)
        except OSError:
            current_mtime = None

        # Acquire lock
        lock = acquire_lock(session_id, float(cfg.get("lock_timeout_seconds", 5)), float(cfg.get("stale_lock_age_seconds", 30)))
        if lock is None:
            log_error(f"lock_timeout session={session_id}")
            counter = load_counter(session_id)
            bump_counter(counter, "lock_timeouts")
            save_counter(session_id, counter)
            return 0

        counter = load_counter(session_id)

        # Load + prune state
        entries = load_entries(session_id)
        pruned = prune_entries(
            entries,
            float(cfg.get("state_retention_hours", 4)),
            int(cfg.get("state_retention_calls", 200)),
        )
        if pruned != entries:
            try:
                atomic_write_jsonl(session_log_path(session_id), pruned)
            except Exception as exc:
                log_error(f"prune_write_failed: {exc!r}")
        entries = pruned

        now = time.time()

        def make_entry(decision: str, rule: str) -> dict[str, Any]:
            return {
                "ts": now,
                "file": norm_key,
                "file_orig": orig_resolved,
                "offset": offset,
                "limit": limit,
                "mtime": current_mtime,
                "decision": decision,
                "rule": rule,
            }

        # ---- Step 2: Bypasses (lock held) ----

        sentinel = int(cfg.get("override_sentinel", 999911))
        replacement = int(cfg.get("override_replacement_limit", 2000))
        log_decisions = bool(cfg.get("log_decisions", True))

        # Bypass 1: override sentinel
        if isinstance(limit, int) and limit == sentinel:
            entry = make_entry("override", "bypass_1_override")
            record_decision(session_id, entry, counter, "allowed_bypass_override", log_decisions)
            payload_out = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": {"limit": replacement},
                }
            }
            sys.stdout.write(json.dumps(payload_out, ensure_ascii=False))
            sys.stdout.flush()
            return 0

        # Bypass 2: always-fresh path
        patterns = list(cfg.get("always_fresh_patterns") or [])
        if fnmatch_any(norm_key, patterns):
            entry = make_entry("fresh_path", "bypass_2_fresh_path")
            record_decision(session_id, entry, counter, "allowed_bypass_fresh_path", log_decisions)
            return 0

        # Bypass 3: recently-modified file
        fresh_age = float(cfg.get("always_fresh_age_seconds", 60))
        if current_mtime is not None and current_mtime > (now - fresh_age):
            entry = make_entry("fresh_age", "bypass_3_fresh_age")
            record_decision(session_id, entry, counter, "allowed_bypass_fresh_age", log_decisions)
            return 0

        # Match prior entries
        prior_for_file = [e for e in entries if e.get("file") == norm_key]

        # Bypass 4: file changed since last read
        if prior_for_file and current_mtime is not None:
            prior_mtimes = [float(e["mtime"]) for e in prior_for_file if e.get("mtime") is not None]
            if prior_mtimes and current_mtime > max(prior_mtimes):
                # Purge all prior entries for this file
                kept = [e for e in entries if e.get("file") != norm_key]
                try:
                    atomic_write_jsonl(session_log_path(session_id), kept)
                except Exception as exc:
                    log_error(f"purge_write_failed: {exc!r}")
                entry = make_entry("file_changed", "bypass_4_file_changed")
                record_decision(session_id, entry, counter, "allowed_bypass_file_changed", log_decisions)
                return 0

        # ---- Step 3: Block rules ----

        max_limit = int(cfg.get("rule_b_max_limit", 1000))

        # Rule A: first read of this file
        if not prior_for_file:
            entry = make_entry("allow", "A")
            record_decision(session_id, entry, counter, "allowed_rule_a", log_decisions)
            return 0

        # File read before. Determine bounded vs unbounded.
        bounded = (
            isinstance(offset, int)
            and isinstance(limit, int)
            and limit <= max_limit
            and offset >= 0
            and limit > 0
        )

        # Rule D: unbounded re-read -> BLOCK
        if not bounded:
            entry = make_entry("block", "D")
            record_decision(session_id, entry, counter, "blocked_rule_d", log_decisions)
            sys.stderr.write(
                "TKK Read Guard (Rule D): "
                f"{orig_resolved} was already read this session and the current call "
                "is unbounded (no offset/limit or limit > 1000). "
                "Re-read the relevant slice with offset+limit<=1000, or pass "
                f"limit={sentinel} to force a full re-read.\n"
            )
            return 2

        # Rule B / Rule C: bounded
        small_read_lines = int(cfg.get("small_read_exemption_lines", 100))
        overlap_threshold = float(cfg.get("overlap_block_threshold", 0.8))

        new_start = int(offset)
        new_end = new_start + int(limit)

        # Small-read exemption applies before overlap analysis (per spec exception)
        if (new_end - new_start) < small_read_lines:
            entry = make_entry("allow", "B")
            record_decision(session_id, entry, counter, "allowed_rule_b", log_decisions)
            return 0

        # Compute overlap with union of prior ranges (treat None offset/limit as full-file 0..1e9)
        prior_intervals: list[tuple[int, int]] = []
        BIG = 10**9
        for e in prior_for_file:
            po = e.get("offset")
            pl = e.get("limit")
            if isinstance(po, int) and isinstance(pl, int):
                prior_intervals.append((po, po + pl))
            else:
                prior_intervals.append((0, BIG))

        ov_lines, new_lines = overlap_with_union(new_start, new_end, prior_intervals)
        ratio = (ov_lines / new_lines) if new_lines > 0 else 0.0

        if new_lines >= small_read_lines and ratio > overlap_threshold:
            entry = make_entry("block", "C")
            entry["overlap_ratio"] = ratio
            record_decision(session_id, entry, counter, "blocked_rule_c", log_decisions)
            sys.stderr.write(
                "TKK Read Guard (Rule C): "
                f"{orig_resolved} range [{new_start},{new_end}) overlaps prior reads by "
                f"{ratio:.0%} (>{overlap_threshold:.0%}). "
                f"Pick a narrower slice or pass limit={sentinel} to override.\n"
            )
            return 2

        # Allowed: Rule B (bounded, small overlap) — counter rule_b
        entry = make_entry("allow", "B")
        entry["overlap_ratio"] = ratio
        record_decision(session_id, entry, counter, "allowed_rule_b", log_decisions)
        return 0

    except Exception:
        tb = traceback.format_exc()
        log_error(f"unhandled_exception:\n{tb}")
        try:
            counter = load_counter(session_id)
            bump_counter(counter, "errors")
            save_counter(session_id, counter)
        except Exception:
            pass
        return 0  # default-allow on any crash
    finally:
        release_lock(lock)


if __name__ == "__main__":
    sys.exit(main())
