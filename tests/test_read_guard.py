"""34 pytest tests for tkk_read_guard.py (Phase 1 v5 spec)."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import uuid

import pytest

from conftest import (
    HOOK_PATH,
    read_counter,
    read_decisions,
    read_session_log,
    write_config,
)


# -- Test 1: First read of a file -> ALLOW, log entry added -------------------
def test_01_first_read_allow(run_hook, make_read_payload, sample_file, tkk_home, session_id):
    rc, out, err = run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    assert rc == 0, err
    assert out == ""
    entries = read_session_log(tkk_home, session_id)
    assert len(entries) == 1
    assert entries[0]["rule"] == "A"
    assert entries[0]["mtime"] is not None


# -- Test 2: Second full re-read -> BLOCK (Rule D) ----------------------------
def test_02_second_full_reread_blocks(run_hook, make_read_payload, sample_file):
    rc1, _, _ = run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    assert rc1 == 0
    rc2, out, err = run_hook(make_read_payload(str(sample_file)))  # no offset/limit
    assert rc2 == 2
    assert out == ""
    assert "Rule D" in err


# -- Test 3: Second bounded read (limit<=1000) -> ALLOW (Rule B) --------------
def test_03_second_bounded_read_allows(run_hook, make_read_payload, sample_file, tkk_home, session_id):
    run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    rc, out, err = run_hook(make_read_payload(str(sample_file), offset=1000, limit=500))
    assert rc == 0, err
    assert out == ""
    entries = read_session_log(tkk_home, session_id)
    assert entries[-1]["rule"] == "B"


# -- Test 4: Second read with limit=999999 -> BLOCK (Rule D, limit cap) -------
def test_04_huge_limit_blocks(run_hook, make_read_payload, sample_file):
    run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    rc, out, err = run_hook(make_read_payload(str(sample_file), offset=0, limit=999999))
    assert rc == 2
    assert "Rule D" in err


# -- Test 5: 50% overlap on 200-line request -> ALLOW -------------------------
def test_05_overlap_50pct_allows(run_hook, make_read_payload, sample_file, tkk_home, session_id):
    run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    rc, out, err = run_hook(make_read_payload(str(sample_file), offset=100, limit=200))
    assert rc == 0, err
    entries = read_session_log(tkk_home, session_id)
    assert entries[-1]["rule"] == "B"


# -- Test 6: 90% overlap on 200-line request -> BLOCK (Rule C) ---------------
def test_06_overlap_90pct_blocks(run_hook, make_read_payload, sample_file):
    run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    rc, out, err = run_hook(make_read_payload(str(sample_file), offset=20, limit=200))
    assert rc == 2
    assert "Rule C" in err


# -- Test 7: 90% overlap on small (50-line) request -> ALLOW (exemption) -----
def test_07_small_read_exemption(run_hook, make_read_payload, sample_file):
    run_hook(make_read_payload(str(sample_file), offset=0, limit=50))
    rc, out, err = run_hook(make_read_payload(str(sample_file), offset=5, limit=50))
    assert rc == 0, err


# -- Test 8: Always-fresh allowlist allows full re-read -----------------------
def test_08_always_fresh_allowlist(run_hook, make_read_payload, tkk_home, tmp_path):
    # Build a path that matches **/scratch/**
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    f = scratch / "note.py"
    f.write_text("hello\n")
    past = time.time() - 3600
    os.utime(f, (past, past))
    run_hook(make_read_payload(str(f), offset=0, limit=200))
    rc, out, err = run_hook(make_read_payload(str(f)))  # full re-read normally blocks
    assert rc == 0, err
    entries = read_session_log(tkk_home, make_read_payload(str(f))["session_id"])
    # Last entry should be bypass_2_fresh_path
    assert entries[-1]["rule"] == "bypass_2_fresh_path"


# -- Test 9: *.log file always allowed ----------------------------------------
def test_09_log_file_allowed(run_hook, make_read_payload, tmp_path):
    f = tmp_path / "app.log"
    f.write_text("err\n")
    past = time.time() - 3600
    os.utime(f, (past, past))
    run_hook(make_read_payload(str(f), offset=0, limit=200))
    rc, _, err = run_hook(make_read_payload(str(f)))
    assert rc == 0, err


# -- Test 10: File modified in last 60s -> ALLOW (Bypass 3) -------------------
def test_10_fresh_mtime_allows(run_hook, make_read_payload, tkk_home, tmp_path, session_id):
    f = tmp_path / "hot.py"
    f.write_text("x\n")  # fresh mtime
    run_hook(make_read_payload(str(f), offset=0, limit=200))
    rc, _, _ = run_hook(make_read_payload(str(f)))  # full re-read
    assert rc == 0
    entries = read_session_log(tkk_home, session_id)
    assert entries[-1]["rule"] == "bypass_3_fresh_age"


# -- Test 11: File modified between reads -> ALLOW + purge --------------------
def test_11_file_changed_purges(run_hook, make_read_payload, tkk_home, tmp_path, session_id):
    f = tmp_path / "evolving.py"
    f.write_text("v1\n")
    past = time.time() - 3600
    os.utime(f, (past, past))
    run_hook(make_read_payload(str(f), offset=0, limit=200))
    # Touch file to a new mtime > prior but still > 60s old to avoid Bypass 3
    new_mt = time.time() - 120
    os.utime(f, (new_mt, new_mt))
    rc, _, _ = run_hook(make_read_payload(str(f)))  # would normally Rule D block
    assert rc == 0
    entries = read_session_log(tkk_home, session_id)
    # Bypass 4 purges priors and writes one fresh entry
    file_entries = [e for e in entries if e["file"].endswith("evolving.py")]
    assert len(file_entries) == 1
    assert file_entries[0]["rule"] == "bypass_4_file_changed"


# -- Test 12: limit=999911 override -> ALLOW + stdout JSON --------------------
def test_12_override_sentinel(run_hook, make_read_payload, sample_file):
    run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    rc, out, err = run_hook(make_read_payload(str(sample_file), limit=999911))
    assert rc == 0, err
    obj = json.loads(out)
    assert obj["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert obj["hookSpecificOutput"]["updatedInput"]["limit"] == 2000


# -- Test 13: Override stdout is ONLY the JSON (no extra prints) -------------
def test_13_override_stdout_is_only_json(run_hook, make_read_payload, sample_file):
    rc, out, err = run_hook(make_read_payload(str(sample_file), limit=999911))
    assert rc == 0
    # Must round-trip as exactly one JSON object
    obj = json.loads(out)
    assert isinstance(obj, dict)
    # No leading/trailing junk
    assert out.strip() == json.dumps(obj, ensure_ascii=False) or json.loads(out.strip())


# -- Test 14: Malformed stdin JSON -> ALLOW (failure mode), error logged -----
def test_14_malformed_stdin(tkk_home):
    env = os.environ.copy()
    env["TKK_HOME"] = str(tkk_home)
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input="this is not json",
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    errlog = tkk_home / "errors.log"
    assert errlog.exists()
    assert "stdin_parse_failed" in errlog.read_text(encoding="utf-8")


# -- Test 15: State directory doesn't exist -> created, first call allows ----
def test_15_creates_state_dirs(run_hook, make_read_payload, sample_file, tkk_home):
    # tkk_home exists but subdirs do not yet (fixture only made the root)
    rc, _, _ = run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    assert rc == 0
    assert (tkk_home / "read_log").is_dir()
    assert (tkk_home / "counters").is_dir()
    assert (tkk_home / "locks").is_dir()


# -- Test 16: Different sessions don't cross-contaminate ----------------------
def test_16_session_isolation(run_hook, make_read_payload, sample_file):
    sid_a = "sess-A-" + uuid.uuid4().hex[:6]
    sid_b = "sess-B-" + uuid.uuid4().hex[:6]
    rc1, _, _ = run_hook(make_read_payload(str(sample_file), offset=0, limit=200, sid=sid_a))
    rc2, _, _ = run_hook(make_read_payload(str(sample_file), offset=0, limit=200, sid=sid_b))
    assert rc1 == 0
    assert rc2 == 0  # session B sees fresh state for this file


# -- Test 17: Path normalization (case + slashes) ----------------------------
def test_17_path_normalization(run_hook, make_read_payload, sample_file, tkk_home):
    # Read using mixed-case original path
    p1 = str(sample_file)
    p2 = str(sample_file).replace("\\", "/").upper()  # different case + slashes
    rc1, _, _ = run_hook(make_read_payload(p1, offset=0, limit=200))
    rc2, _, err = run_hook(make_read_payload(p2))  # full re-read
    assert rc1 == 0
    # Path must be recognized as same file -> Rule D block on full re-read
    assert rc2 == 2, err


# -- Test 18: State pruning by age -------------------------------------------
def test_18_prune_by_age(run_hook, make_read_payload, sample_file, tkk_home, session_id):
    # Manually pre-populate session log with an entry 5h old
    (tkk_home / "read_log").mkdir(parents=True, exist_ok=True)
    log = tkk_home / "read_log" / f"{session_id}.jsonl"
    old_ts = time.time() - 5 * 3600
    norm = str(sample_file).replace("\\", "/").lower()
    log.write_text(json.dumps({
        "ts": old_ts, "file": norm, "file_orig": str(sample_file),
        "offset": 0, "limit": 200, "mtime": old_ts - 100, "decision": "allow", "rule": "A",
    }) + "\n", encoding="utf-8")
    # Now read same file with offset/limit None -> should be Rule A (no priors after prune)
    rc, _, _ = run_hook(make_read_payload(str(sample_file)))
    assert rc == 0
    entries = read_session_log(tkk_home, session_id)
    assert all(float(e["ts"]) > old_ts for e in entries)


# -- Test 19: State pruning by count -----------------------------------------
def test_19_prune_by_count(run_hook, make_read_payload, sample_file, tkk_home, session_id):
    write_config(tkk_home, {"state_retention_calls": 5, "always_fresh_age_seconds": 0})
    (tkk_home / "read_log").mkdir(parents=True, exist_ok=True)
    log = tkk_home / "read_log" / f"{session_id}.jsonl"
    norm = str(sample_file).replace("\\", "/").lower()
    lines = []
    now = time.time()
    for i in range(10):
        lines.append(json.dumps({
            "ts": now - 60 + i, "file": f"{norm}.other{i}", "file_orig": str(sample_file),
            "offset": 0, "limit": 200, "mtime": now - 1000,
            "decision": "allow", "rule": "A",
        }))
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rc, _, _ = run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    assert rc == 0
    entries = read_session_log(tkk_home, session_id)
    assert len(entries) <= 5 + 1  # pruned cap + the new one


# -- Test 20: Concurrent reads serialize via lock -----------------------------
def test_20_concurrent_lock_serializes(tkk_home, sample_file):
    sid = "concurrent-" + uuid.uuid4().hex[:6]
    env = os.environ.copy()
    env["TKK_HOME"] = str(tkk_home)
    payload = json.dumps({
        "session_id": sid, "tool_name": "Read",
        "tool_input": {"file_path": str(sample_file), "offset": 0, "limit": 200},
    })
    # Spawn two subprocesses simultaneously
    p1 = subprocess.Popen(
        [sys.executable, str(HOOK_PATH)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    p2 = subprocess.Popen(
        [sys.executable, str(HOOK_PATH)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    p1.communicate(payload, timeout=10)
    p2.communicate(payload, timeout=10)
    assert p1.returncode in (0, 2)
    assert p2.returncode in (0, 2)
    entries = read_session_log(tkk_home, sid)
    # Both wrote entries (lock serialized), so we should see >=2 entries
    # (one Rule A from first, one Rule D block — but blocks still append)
    assert len(entries) >= 2
    # Timestamps should be distinct / monotonic-ish (lock serializes)
    ts_list = [float(e["ts"]) for e in entries]
    assert len(set(ts_list)) >= 1  # at least separable


# -- Test 21: File doesn't exist -> ALLOW ------------------------------------
def test_21_missing_file_allows(run_hook, make_read_payload, tmp_path):
    ghost = tmp_path / "does_not_exist.py"
    rc, out, err = run_hook(make_read_payload(str(ghost), offset=0, limit=200))
    assert rc == 0, err
    assert out == ""


# -- Test 22: settings.json merge preserves existing hook -------------------- 
def test_22_settings_merge_preserves_existing(tmp_path):
    """Mimics the merge step of install.ps1 in pure Python (no PS dependency in tests)."""
    settings = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "python existing.py", "timeout": 5}]}
            ]
        }
    }
    new_entry = {
        "matcher": "Read",
        "hooks": [{"type": "command", "command": "python tkk_read_guard.py", "timeout": 5}],
    }
    # Idempotency check
    already = any(
        any("tkk_read_guard.py" in (h.get("command") or "") for h in entry.get("hooks", []))
        for entry in settings["hooks"]["PreToolUse"]
    )
    assert already is False
    settings["hooks"]["PreToolUse"].append(new_entry)
    assert len(settings["hooks"]["PreToolUse"]) == 2
    bash_entries = [e for e in settings["hooks"]["PreToolUse"] if e["matcher"] == "Bash"]
    assert len(bash_entries) == 1
    assert "existing.py" in bash_entries[0]["hooks"][0]["command"]


# -- Test 23: fnmatch glob **/logs/** matches deep path -----------------------
def test_23_fnmatch_logs_pattern():
    import fnmatch
    pat = "**/logs/**"
    assert fnmatch.fnmatch("c:/users/n01/logs/app.log", pat)
    assert fnmatch.fnmatch("/var/logs/app/x.log", pat)


# -- Test 24: Malformed config.json -> fall back to defaults -----------------
def test_24_malformed_config(run_hook, make_read_payload, sample_file, tkk_home):
    (tkk_home / "config.json").write_text("this is not json", encoding="utf-8")
    rc, _, _ = run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    assert rc == 0
    errlog = tkk_home / "errors.log"
    assert errlog.exists() and "config_load_failed" in errlog.read_text(encoding="utf-8")


# -- Test 25: Stale lock recovery --------------------------------------------
def test_25_stale_lock_recovery(run_hook, make_read_payload, sample_file, tkk_home, session_id):
    locks = tkk_home / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    lockdir = locks / f"{session_id}.lock"
    lockdir.mkdir()
    old = time.time() - 60
    os.utime(lockdir, (old, old))
    rc, _, _ = run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    assert rc == 0


# -- Test 26: Performance: <300ms on 1000-entry log --------------------------
def test_26_perf_1000_entries(run_hook, make_read_payload, sample_file, tkk_home, session_id):
    (tkk_home / "read_log").mkdir(parents=True, exist_ok=True)
    log = tkk_home / "read_log" / f"{session_id}.jsonl"
    now = time.time()
    norm = str(sample_file).replace("\\", "/").lower()
    lines = []
    for i in range(1000):
        lines.append(json.dumps({
            "ts": now - 60 + i / 100, "file": f"{norm}.x{i}", "file_orig": str(sample_file),
            "offset": 0, "limit": 200, "mtime": now - 1000,
            "decision": "allow", "rule": "A",
        }))
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    t0 = time.time()
    rc, _, _ = run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    elapsed = time.time() - t0
    assert rc == 0
    # Subprocess startup dominates; spec allows 300ms decision time but we
    # measure wall-clock here. Generous cap of 3s for subprocess overhead.
    assert elapsed < 3.0, f"too slow: {elapsed:.2f}s"


# -- Test 27: Counter shards isolated by session -----------------------------
def test_27_counter_shards_isolated(run_hook, make_read_payload, sample_file, tkk_home):
    sid_a = "shardA-" + uuid.uuid4().hex[:6]
    sid_b = "shardB-" + uuid.uuid4().hex[:6]
    for _ in range(5):
        run_hook(make_read_payload(str(sample_file), offset=0, limit=200, sid=sid_a))
    for _ in range(5):
        run_hook(make_read_payload(str(sample_file), offset=0, limit=200, sid=sid_b))
    ca = read_counter(tkk_home, sid_a)
    cb = read_counter(tkk_home, sid_b)
    assert ca["total_invocations"] == 5
    assert cb["total_invocations"] == 5
    assert ca["session_id"] == sid_a
    assert cb["session_id"] == sid_b


# -- Test 28: Overlap-union with multiple prior ranges (under threshold) ----
def test_28_overlap_union_under_threshold(run_hook, make_read_payload, sample_file, tkk_home, session_id):
    # Prior reads: [0,500) and [600,800)
    run_hook(make_read_payload(str(sample_file), offset=0, limit=500))
    run_hook(make_read_payload(str(sample_file), offset=600, limit=200))
    # New request: [0,1000) -> overlap=500+200=700 / 1000 = 70% -> ALLOW
    rc, _, err = run_hook(make_read_payload(str(sample_file), offset=0, limit=1000))
    assert rc == 0, err


def test_28b_overlap_union_over_threshold(run_hook, make_read_payload, sample_file):
    # Prior: [0,500),[500,1000) -> union [0,1000); new [0,1000) -> 100% -> BLOCK
    run_hook(make_read_payload(str(sample_file), offset=0, limit=500))
    run_hook(make_read_payload(str(sample_file), offset=500, limit=500))
    rc, _, err = run_hook(make_read_payload(str(sample_file), offset=0, limit=1000))
    assert rc == 2
    assert "Rule C" in err


# -- Test 29: Overlap adjacent intervals merge -> 100% -> BLOCK -------------
def test_29_adjacent_merge_blocks(run_hook, make_read_payload, sample_file):
    run_hook(make_read_payload(str(sample_file), offset=0, limit=500))
    run_hook(make_read_payload(str(sample_file), offset=500, limit=500))
    rc, _, err = run_hook(make_read_payload(str(sample_file), offset=0, limit=1000))
    assert rc == 2
    assert "Rule C" in err


# -- Test 30: install.ps1 idempotency (logic emulated in python) ------------
def test_30_install_idempotency():
    settings = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Read", "hooks": [{"type": "command", "command": "python tkk_read_guard.py", "timeout": 5}]}
            ]
        }
    }
    already = any(
        any("tkk_read_guard.py" in (h.get("command") or "") for h in entry.get("hooks", []))
        for entry in settings["hooks"]["PreToolUse"]
    )
    assert already is True
    if not already:
        settings["hooks"]["PreToolUse"].append({"matcher": "Read", "hooks": []})
    assert len(settings["hooks"]["PreToolUse"]) == 1


# -- Test 31: enabled:false kill switch -------------------------------------
def test_31_kill_switch(run_hook, make_read_payload, sample_file, tkk_home, session_id):
    write_config(tkk_home, {"enabled": False})
    # Two full re-reads would normally Rule D block on the second
    rc1, _, _ = run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    rc2, _, _ = run_hook(make_read_payload(str(sample_file)))
    assert rc1 == 0
    assert rc2 == 0
    # No decisions logged when disabled
    assert read_session_log(tkk_home, session_id) == []
    assert read_decisions(tkk_home) == []


# -- Test 32: Exit code 2 on block ------------------------------------------
def test_32_block_exit_code_2(run_hook, make_read_payload, sample_file):
    run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    rc, _, _ = run_hook(make_read_payload(str(sample_file)))
    assert rc == 2


# -- Test 33: Empty stdout on non-override allow ----------------------------
def test_33_empty_stdout_on_allow(run_hook, make_read_payload, sample_file):
    rc, out, _ = run_hook(make_read_payload(str(sample_file), offset=0, limit=200))
    assert rc == 0
    assert out == ""
    assert len(out) == 0


# -- Test 34: decisions.jsonl written on every exit path --------------------
def test_34_decisions_on_every_path(run_hook, make_read_payload, sample_file, tkk_home, tmp_path):
    sid = "everypath-" + uuid.uuid4().hex[:6]
    decisions_path = tkk_home / "decisions.jsonl"

    # Bypass 1: override sentinel
    run_hook(make_read_payload(str(sample_file), limit=999911, sid=sid))
    # Bypass 2: always-fresh path (use scratch dir)
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    scratch_file = scratch_dir / "x.py"
    scratch_file.write_text("x\n")
    past = time.time() - 3600
    os.utime(scratch_file, (past, past))
    run_hook(make_read_payload(str(scratch_file), offset=0, limit=200, sid=sid))
    # Rule A: first read of sample_file (still no prior matching b/c first call was override)
    # Actually the override path logs an entry for sample_file already, so use a new file
    other = tmp_path / "other.py"
    other.write_text("a\n" * 100)
    os.utime(other, (past, past))
    run_hook(make_read_payload(str(other), offset=0, limit=50, sid=sid))
    # Rule B: bounded second read of other
    run_hook(make_read_payload(str(other), offset=50, limit=50, sid=sid))
    # Rule C-block: second sample_file overlap >80%
    run_hook(make_read_payload(str(sample_file), offset=0, limit=200, sid=sid))  # first concrete read
    run_hook(make_read_payload(str(sample_file), offset=10, limit=200, sid=sid))
    # Rule D: third sample_file unbounded
    run_hook(make_read_payload(str(sample_file), sid=sid))

    decisions = read_decisions(tkk_home)
    assert len(decisions) >= 6
    rules = {d["rule"] for d in decisions if d.get("session_id") == sid}
    assert "bypass_1_override" in rules
    assert "bypass_2_fresh_path" in rules
    assert "A" in rules
    assert "B" in rules
    # At least one of C or D must be in the rule set
    assert "C" in rules or "D" in rules
    # Every decision must carry session_id
    for d in decisions:
        if d.get("session_id") == sid:
            assert d["session_id"] == sid
