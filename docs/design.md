# COOK SPEC — TKK Phase 1: Read Guard (v5 — Final, all 3 review rounds applied)
## ID: TKK-P1-READGUARD | Priority: P0 | Owner: abacusai (Opus 4.7)

> **Purpose:** Block redundant Read tool calls in Claude Code via PreToolUse hook.
> Eliminates the redundant-read problem (real measured: `memory_ui_server.py` read 443x in one session, 5,554 Read calls in 14 days, ~30-50% of cc cost).

**Version history:**
- v1 (2026-05-23) — initial draft
- v2 (2026-05-23) — Gemini 2.5 Pro patches (8 fixes)
- v3 (2026-05-23) — OpenRouter reviewer pool round 1 patches (12 fixes)
- v4 (2026-05-23) — OpenRouter reviewer pool round 2 patches (5 fixes)
- v5 (2026-05-23) — OpenRouter reviewer pool round 3 cleanup (8 mechanical fixes — stale refs, missing tests)

---

## CRITICAL — EXECUTOR ROUTING

**This cook runs on `abacusai --model OPUS_4_7`, NOT raw Claude Code session.**

Reason: Victor's cc weekly quota at 95% until tomorrow 2pm. abacusai routes to Opus 4.7 via separate billing/quota — same model intelligence, different bucket. Hook development needs Opus-level reasoning (lock semantics, atomic writes, JSON merge logic) so we don't downgrade to GPT-5 Codex on this one.

```
abacusai -p --dangerously-skip-permissions --model OPUS_4_7
```

The fire prompt below is shaped for Opus 4.7 via abacusai. The instructions assume the executor has Desktop Commander, can write files, run pytest, run git, and run PowerShell.

---

## ENVIRONMENT (critical context)

- **cc runs in Windows Terminal native** (not WSL). All paths Windows-style.
- Python 3.10+ from Windows Python (not WSL Python). Confirm via `python --version`.
- Hook scripts live at `~/.claude/hooks/` which on this Windows machine = `C:\Users\N01\.claude\hooks\`.
- An existing PreToolUse hook (`pretooluse_enforce_rules.py`) already lives there — must coexist.
- Lock files use `os.mkdir` atomicity (cross-platform), NOT `msvcrt.locking` (had 1-byte bug + fd issues per reviewer findings).

---

## DELIVERABLES

1. `~/.claude/hooks/tkk_read_guard.py` — the hook script
2. Update to `~/.claude/settings.json` — merge in registration, preserve existing hooks
3. `~/.tkk/` — state directory created with appropriate gitignore
4. `D:\Dev\Projects\tkk\tests\test_read_guard.py` — 34 pytest tests (all must pass)
5. `D:\Dev\Projects\tkk\hooks\burn_audit.ps1` — reads decisions.jsonl, reports savings
6. `D:\Dev\Projects\tkk\README.md` — install + usage + override + recovery docs
7. Push to `git@github.com:innov8ideas4u-alt/tkk.git`

---

## CORE LOGIC (REWRITTEN — v3)

For each PreToolUse-on-Read event:

### Step 1 — Parse & Normalize

- Parse stdin JSON. Extract `session_id`, `tool_input.file_path`, `tool_input.offset`, `tool_input.limit`.
- Normalize file path:
  ```python
  resolved = pathlib.Path(file_path).resolve()
  norm_key = str(resolved).replace('\\', '/').lower()  # comparison key
  ```
- Capture current `mtime` of file (if exists): `os.path.getmtime(resolved)`. If file doesn't exist, allow + log warning (let cc's Read tool report the actual FileNotFoundError).

### Step 1.5 — Acquire Lock & Load State (FIRST — fixes Bypass 4 deadlock)

This moved BEFORE bypasses so any rule (including bypasses) can update state cleanly.

- State file: `~/.tkk/read_log/<session_id>.jsonl`
- Lock file: `~/.tkk/locks/<session_id>.lock` (a directory, not a file)
- Acquire lock via atomic `os.mkdir(lockdir)`. Loop with `time.sleep(0.05)` until success or 5s timeout.
  - If timeout: log warning to errors.log, default-ALLOW (correctness > performance). Increment `lock_timeouts` counter in `~/.tkk/counters/<session_id>.json` (per-session shard).
- Load all entries for this session: read JSONL line-by-line, parse JSON, keep in memory.
- **Prune in-memory list:**
  - Drop entries with `ts < now - state_retention_hours * 3600`
  - If list still > `state_retention_calls`, drop oldest excess
- Write pruned list back atomically: write to `.tmp`, then `os.replace()` (atomic on Windows since Python 3.3).
- **Do NOT release lock yet** — we hold it for the rest of the decision so the entry append at the end is atomic.

### Step 2 — Bypass Rules (run AFTER lock acquired)

Bypass rules can now safely update state because lock is held.

**IMPORTANT — applies to ALL bypass and rule exits below:** Before releasing the lock and exiting, EVERY exit path must:
1. Append the decision entry to the per-session log `~/.tkk/read_log/<session_id>.jsonl`
2. Append the same decision entry (with `session_id` added) to the global `~/.tkk/decisions.jsonl` (for burn_audit aggregation)
3. Increment the appropriate counter in `~/.tkk/counters/<session_id>.json`
4. Release lock
5. Exit (0 for allow, 2 for block)

This applies to Bypass 1-4, Rule A, Rule B, Rule C-allow, Rule C-block, and Rule D. Round-3 review caught that bypass paths were originally missing the decisions.jsonl write — fix unified here.

**Bypass 1 — Override sentinel (`limit == 999911`):**
- Allow.
- Emit JSON to stdout to mutate `limit` back to a sane value (2000):
  ```json
  {
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "permissionDecision": "allow",
      "updatedInput": {"limit": 2000}
    }
  }
  ```
- **CRITICAL: this is the ONLY thing on stdout.** No `print()`, no logging to stdout. All other output goes to stderr or files.
- Increment `overrides` counter in `~/.tkk/counters/<session_id>.json` (per-session shard, not global).
- Append entry to log with `decision: "override"`.
- Release lock. Exit 0.

**Bypass 2 — Always-fresh path:**
- Match `norm_key` against `always_fresh_patterns` using `fnmatch.fnmatch` (NOT `pathlib.match` — broken with `**` on Windows).
- Patterns must use forward-slash conventions to match `norm_key`.
- If match: log entry with `decision: "fresh_path"`. Release lock. Exit 0.

**Bypass 3 — Recently-modified file:**
- If `current_mtime > (time.time() - always_fresh_age_seconds)` (default 60s).
- Log entry. Release lock. Exit 0.

**Bypass 4 — File changed since last read:**
- Look for prior entries in loaded state matching `norm_key`.
- If any exist AND `current_mtime > max(entry.mtime for matching entries)`:
  - **PURGE all prior entries for this `norm_key` from state** (so Rule C doesn't reference stale ranges).
  - Write purge to disk (lock still held).
  - Append fresh entry as if it were Rule A.
  - Log `decision: "file_changed"`. Release lock. Exit 0.

### Step 3 — Block Rules (apply in order)

**Rule A — File NEVER read this session (no entries match `norm_key`):**
- Append entry. Release lock. Exit 0.

**Rule B — File read before, current call has BOUNDED line range:**
- Bounded := `offset is not None AND limit is not None AND limit <= 1000`
- Allow. Append entry. Release lock. Exit 0.
- *(Limit cap at 1000 prevents `limit=999999` malicious compliance.)*

**Rule C — File read before, current range overlaps prior reads:**
- **Overlap math (defined for multiple prior entries):**
  1. Collect all prior entries matching `norm_key` for this session.
  2. Compute the UNION of all prior `(offset, offset+limit)` ranges — i.e., merge overlapping intervals into a set of non-overlapping ranges.
  3. Compute the NEW request's range: `(new_offset, new_offset + new_limit)`.
  4. Compute overlap as: `(number of lines in NEW request that fall within the union) / (lines in NEW request)`
- Exception: if NEW request < 100 lines, ALLOW regardless of overlap % (micro-reads exempt).
- If overlap > 80% AND new request >= 100 lines: BLOCK.
- Else: Allow. Append entry. Release lock. Exit 0.

**Interval-union helper (implementation note for executor):**
```python
def merge_intervals(intervals):
    """[(start, end), ...] -> sorted non-overlapping list"""
    if not intervals:
        return []
    sorted_iv = sorted(intervals, key=lambda x: x[0])
    merged = [sorted_iv[0]]
    for start, end in sorted_iv[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged

def overlap_with_union(new_start, new_end, prior_intervals):
    """Returns (overlapping_lines, total_new_lines)."""
    merged = merge_intervals(prior_intervals)
    new_lines = new_end - new_start
    overlap_lines = 0
    for s, e in merged:
        # intersection of [new_start, new_end) and [s, e)
        lo = max(new_start, s)
        hi = min(new_end, e)
        if hi > lo:
            overlap_lines += (hi - lo)
    return overlap_lines, new_lines
```

**Rule D — File read before, NO line range OR unbounded range:**
- Unbounded := `offset is None OR limit is None OR limit > 1000`
- BLOCK with Rule D message.

### Step 4 — On BLOCK (Rule C or D)

- Write block reason to stderr (the message Claude sees).
- Append block decision to log.
- Append entry to `~/.tkk/decisions.jsonl` (global log for burn-audit script).
- Release lock.
- Exit 2.

### Step 5 — On ALLOW (any path above)

- Append decision to log.
- Append entry to `~/.tkk/decisions.jsonl`.
- Release lock.
- Exit 0 (with stdout JSON only for Bypass 1; all others: empty stdout).

### Failure Mode

- ANY exception anywhere: catch, write to `~/.tkk/errors.log`, release lock if held, default-ALLOW (exit 0, empty stdout).
- Token waste is recoverable. Blocking cc on a hook crash is not.

---

## LOCKING ARCHITECTURE (CHANGED FROM v2)

**v2 used `msvcrt.locking`. v3 uses lockfile-as-directory.**

Reasons (from reviewer findings):
- `msvcrt.locking(fd, LK_LOCK, 1)` only locks **1 byte**, not the file — race conditions guaranteed
- Requires `os.open()` not Python `open()` — fd handling complexity
- Lockfile pattern is portable, simple, and atomic on every OS

Implementation:
```python
import os, time, errno

LOCK_DIR = pathlib.Path.home() / ".tkk" / "locks"
LOCK_DIR.mkdir(parents=True, exist_ok=True)

def acquire_lock(session_id: str, timeout_s: float = 5.0) -> pathlib.Path | None:
    lockpath = LOCK_DIR / f"{session_id}.lock"
    deadline = time.time() + timeout_s
    while True:
        try:
            os.mkdir(lockpath)
            return lockpath
        except FileExistsError:
            if time.time() >= deadline:
                return None
            # Stale-lock detection: if lock dir is >30s old, force-remove
            try:
                age = time.time() - os.path.getmtime(lockpath)
                if age > 30.0:
                    os.rmdir(lockpath)  # racy but acceptable; just retry
            except FileNotFoundError:
                pass
            time.sleep(0.05)

def release_lock(lockpath: pathlib.Path):
    try:
        os.rmdir(lockpath)
    except FileNotFoundError:
        pass  # already released
```

`os.mkdir` is atomic on Windows (NTFS) and POSIX. Lock file existence == held. Stale-lock detection handles crash-without-release case.

---

## STDOUT MUTATION API (VERIFIED 2026-05-23)

Per https://code.claude.com/docs/en/hooks (also confirmed by claudelog.com, paul-schick.com, claudefa.st):

- Feature: PreToolUse hooks can modify `tool_input` via `updatedInput` field in stdout JSON
- Available since: cc v2.0.10 (October 2025)
- Exit code MUST be 0 (Claude Code IGNORES stdout JSON on exit 2)
- Stdout must contain ONLY the JSON object — no other prints
- Stdout cap: 10,000 chars (our payload is ~100 chars)

Exact schema for our override path:
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "updatedInput": {"limit": 2000}
  }
}
```

If for some reason cc version is < 2.0.10, mutation silently fails and `limit=999911` reaches Read tool = full file read. Install script must verify `claude --version >= 2.0.10` and abort install if older.

---

## STATE FILES (defined per reviewer feedback)

### `~/.tkk/read_log/<session_id>.jsonl` — per-session active state

Each line:
```json
{
  "ts": 1716494072.123,
  "file": "d:/dev/projects/foo/bar.py",
  "file_orig": "D:\\Dev\\Projects\\foo\\bar.py",
  "offset": 0,
  "limit": 500,
  "mtime": 1716494070.0,
  "decision": "allow",
  "rule": "A"
}
```

`ts` as float epoch (not ISO string) for fast comparison. `file` is the lookup key (lowercase forward-slash). `file_orig` preserves original for readable logs.

### `~/.tkk/decisions.jsonl` — global decision log (for burn-audit)

Append-only. One line per hook invocation. Same schema as session log but with `session_id` added. Rotated when > 100MB (rename to `.<timestamp>.bak`).

### `~/.tkk/counters/<session_id>.json` — PER-SESSION running tallies

**Why per-session sharding:** A single global `counters.json` cannot be safely updated by parallel cc sessions even with a per-session lock — each session would acquire its own lock and then race against the others on the global file (the v3 bug caught in reviewer round 2). Solution: each session writes its own counter shard. Aggregation happens at read time in `burn_audit.ps1`.

Each shard:

```json
{
  "session_id": "abc123",
  "started_at": 1716494072.0,
  "total_invocations": 0,
  "allowed_rule_a": 0,
  "allowed_rule_b": 0,
  "allowed_rule_c_overlap_ok": 0,
  "allowed_bypass_override": 0,
  "allowed_bypass_fresh_path": 0,
  "allowed_bypass_fresh_age": 0,
  "allowed_bypass_file_changed": 0,
  "blocked_rule_c": 0,
  "blocked_rule_d": 0,
  "lock_timeouts": 0,
  "errors": 0
}
```

The per-session lock already held during state update ALSO protects this shard (same session_id = same lock). No global contention.

`burn_audit.ps1` aggregates by reading all `~/.tkk/counters/*.json` files and summing fields. Read-only aggregation = no race risk.

Shards older than 30 days get archived by `burn_audit.ps1` to `~/.tkk/counters/_archive/`.

### `~/.tkk/errors.log` — append-only error log

Simple text. Each entry: `[ISO timestamp] [error_type] [traceback]`. Rotated at 10MB.

### `~/.tkk/config.json` — runtime config

```json
{
  "enabled": true,
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
    "**/.atlas/03-active.md"
  ],
  "always_fresh_age_seconds": 60,
  "state_retention_hours": 4,
  "state_retention_calls": 200,
  "lock_timeout_seconds": 5,
  "stale_lock_age_seconds": 30,
  "max_decisions_log_mb": 100,
  "max_errors_log_mb": 10,
  "log_decisions": true,
  "case_insensitive_paths": true
}
```

Hook loads config on each invocation. If config JSON is malformed: log error, fall back to internal defaults, do NOT crash.

---

## HOOK REGISTRATION

`~/.claude/settings.json` must merge in (installer expands `$env:USERPROFILE` at install time):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read",
        "hooks": [
          {
            "type": "command",
            "command": "python C:\\Users\\N01\\.claude\\hooks\\tkk_read_guard.py",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

**Install logic in `install.ps1` (EXPLICIT STEP LIST — do all in order):**

1. **Pre-flight verification** (already in Phase 0 of fire prompt, but installer also checks):
   - `python --version >= 3.10` (abort if not)
   - `claude --version >= 2.0.10` (abort if not — mutation API requirement)
   - Verify `D:\Dev\Projects\tkk\hooks\tkk_read_guard.py` source file exists

2. **Create state directories** (use `$env:USERPROFILE` resolved at install time):
   - `$env:USERPROFILE\.tkk\`
   - `$env:USERPROFILE\.tkk\read_log\`
   - `$env:USERPROFILE\.tkk\counters\`
   - `$env:USERPROFILE\.tkk\locks\`
   - `$env:USERPROFILE\.claude\hooks\` (if not exists)

3. **Copy hook file**:
   - Source: `D:\Dev\Projects\tkk\hooks\tkk_read_guard.py`
   - Dest: `$env:USERPROFILE\.claude\hooks\tkk_read_guard.py`
   - UTF-8 no BOM, LF line endings
   - Verify file size matches source

4. **Write default `config.json`** (if not exists — preserve user edits):
   - Dest: `$env:USERPROFILE\.tkk\config.json`
   - Content: default config from CONFIG FILE section above
   - UTF-8 no BOM

5. **Backup existing `settings.json`**:
   - Source: `$env:USERPROFILE\.claude\settings.json`
   - Dest: `$env:USERPROFILE\.claude\settings.json.bak.<timestamp>`
   - Skip if source doesn't exist (fresh install)

6. **Merge into `settings.json`**:
   - Read existing (if any) with `ConvertFrom-Json -AsHashtable`
   - If parse fails: ABORT with clear error message ("settings.json has invalid JSON / comments; clean it first")
   - If `hooks.PreToolUse` array exists: append our entry
   - If not: create the array with our entry
   - **Idempotency check:** before appending, scan existing entries for an `tkk_read_guard.py` registration — if present, skip merge, report "already installed"
   - Write via `ConvertTo-Json -Depth 10`
   - **Atomic write:** write to `settings.json.tmp` first, then `Move-Item -Force` (atomic on NTFS)
   - UTF-8 no BOM

7. **Smoke test**:
   - Pipe sample stdin JSON (representing a Read of a non-existent file) to the hook
   - Verify exit code 0
   - Verify stderr is empty or matches expected pattern
   - Verify `~/.tkk/errors.log` doesn't have a new entry

8. **Report install summary** to console:
   - Hook path
   - Config path
   - State dir path
   - Backup file path (if created)
   - "Verify with: open Claude Code, read any file, then read the same file again — second should block"

---

## FILE STRUCTURE

```
D:\Dev\Projects\tkk\
├── README.md
├── .gitignore                  # __pycache__, *.pyc, .pytest_cache
├── hooks\
│   ├── tkk_read_guard.py
│   └── burn_audit.ps1
├── install\
│   ├── install.ps1
│   ├── uninstall.ps1           # restore settings.json from backup, remove hook
│   └── verify_version.ps1      # checks claude --version >= 2.0.10
├── tests\
│   ├── test_read_guard.py
│   ├── fixtures\
│   │   ├── sample_session.jsonl
│   │   └── sample_settings_with_existing_hook.json
│   └── conftest.py
└── docs\
    ├── design.md               # copy of v3 spec
    ├── override.md             # how to bypass
    └── recovery.md             # how to recover if hook breaks cc
```

---

## TEST CASES (30 — final after 3 review rounds)

`test_read_guard.py` must cover:

1. **First read of a file** → allow, log entry added with mtime
2. **Second full re-read** → BLOCK with Rule D message
3. **Second read with bounded offset/limit (limit<=1000)** → ALLOW (Rule B)
4. **Second read with limit=999999** → BLOCK (limit cap exceeded → Rule D)
5. **Second read overlapping 50% with 200-line request** → ALLOW
6. **Second read overlapping 90% with 200-line request** → BLOCK (Rule C)
7. **Second read overlapping 90% with 50-line request** → ALLOW (small-read exemption)
8. **Read of file in always-fresh allowlist** → ALLOW even on full re-read
9. **Read of `*.log` file** → ALLOW
10. **Read of file modified in last 60s** → ALLOW (mtime fresh by age)
11. **Read of file modified BETWEEN first and second read** → ALLOW + prior entries purged
12. **limit=999911 override sentinel** → ALLOW, stdout JSON contains `updatedInput.limit=2000`
13. **Override sentinel: stdout is ONLY the JSON object** (no extra prints)
14. **Malformed stdin JSON** → ALLOW (failure mode), error logged
15. **State directory doesn't exist** → create it, allow first call
16. **Different sessions don't cross-contaminate** — read in A, then same file in B → ALLOW
17. **Path normalization:** `C:\Dev\Foo.py` and `c:/dev/foo.py` and `C:/dev/FOO.PY` treated as same
18. **State pruning by age:** entries >4h old dropped before block-rule evaluation
19. **State pruning by count:** beyond 200 entries, oldest dropped
20. **Concurrent reads from parallel cc lanes** — spawn 2 subprocesses, both hit same session, lock serializes (verify via timestamps in log)
21. **File doesn't exist on disk** → ALLOW (let Read report the real error)
22. **`settings.json` merge with existing `pretooluse_enforce_rules.py` hook** → existing hook entry preserved, new entry appended, backup file created
23. **`fnmatch` glob `**/logs/**` matches `c:/users/n01/logs/app.log`** (positive case)
24. **Malformed config.json** → log error, fall back to defaults, don't crash
25. **Stale lock recovery** — pre-create lock dir, set mtime to 31s ago, hook should force-remove and proceed
26. **Performance:** decision time <300ms on 1000-entry log (relaxed from 200ms per reviewer concern)
27. **Counter shards isolated by session** — write 5 invocations from session A and 5 from session B, verify shards `A.json` and `B.json` each have correct counts (no race, no cross-contamination)
28. **Overlap-union with multiple prior ranges** — prior reads at lines 0-500 AND 600-800, new request 0-1000 covers union 0-500+600-800 = 700 overlapping lines out of 1000 = 70% overlap → ALLOW (under 80% threshold). Then expand to verify > 80% case blocks.
29. **Overlap-union adjacent intervals merge** — prior reads at 0-500 AND 500-1000, new request 0-1000 sees union 0-1000 = 100% overlap → BLOCK
30. **install.ps1 idempotency** — run install twice in a row; second run detects existing registration, reports "already installed", does NOT duplicate the entry in settings.json
31. **`enabled: false` kill switch** — set config `enabled: false`, fire a read that would normally block, verify ALLOW + no decision logged
32. **Exit code 2 on block** — fire a Rule D block, capture exit code, assert `== 2` (not just "block happened")
33. **Empty stdout on non-override allow paths** — fire Rule A allow, capture stdout, assert it's exactly empty (zero bytes). Stray prints would corrupt cc's hook protocol.
34. **decisions.jsonl written on every exit path** — fire one of each (Bypass 1, Bypass 2, Rule A, Rule B, Rule C-block, Rule D), verify each appended a line to `~/.tkk/decisions.jsonl` with correct session_id

Each test must run in <100ms (except #20 concurrent which can be <2s). Total suite <12 seconds.

---

## BURN AUDIT SCRIPT

`D:\Dev\Projects\tkk\hooks\burn_audit.ps1`:

Reads `~/.tkk/decisions.jsonl` and reports:
- Total invocations
- Allows by rule (A, B, bypasses)
- Blocks by rule (C, D)
- Overrides count
- Lock timeouts count
- Estimated tokens saved (blocks × ~5000 tokens/avg-file-read)
- Top 10 files that triggered blocks
- Top 10 files that triggered overrides

Output format: human-readable table to console + JSON summary to `~/.tkk/burn_report_<timestamp>.json`.

---

## PERFORMANCE TARGETS (REVISED FROM v2)

- Hook startup: <100ms
- Decision logic: <300ms on 1000-entry log (relaxed — full-load required for count-based prune, can't stream)
- Lock acquisition: <50ms uncontested, <5s under contention (timeout)
- Total wall-clock: <500ms normal case
- 99th percentile: <1s
- Hook timeout in settings.json: 5s (then cc treats as non-blocking failure per docs)

Use stdlib only. No numpy, pandas, requests. The hook fires on EVERY Read call.

---

## RECOVERY DOCS (`docs/recovery.md`)

Required content per reviewer findings:

### If cc seems broken after install:
1. Don't panic. Hook defaults to ALLOW on any error.
2. To disable hook entirely without uninstall:
   ```powershell
   '{"enabled": false}' | Set-Content $env:USERPROFILE\.tkk\config.json
   ```
3. To fully revert: run `D:\Dev\Projects\tkk\install\uninstall.ps1`
4. Manual emergency revert:
   - Restore `settings.json` from `settings.json.bak.<timestamp>`
   - Delete `C:\Users\N01\.claude\hooks\tkk_read_guard.py`

### If hook is blocking legitimate reads:
- Use override sentinel: set `limit: 999911` in the Read call
- Or add the file's path pattern to `always_fresh_patterns` in config.json

### Diagnose what's happening:
- Check `~/.tkk/errors.log` for crashes
- Check `~/.tkk/counters/<session_id>.json` for invocation counts (per-session shards; use `burn_audit.ps1` to aggregate across all sessions)
- Run `burn_audit.ps1` for full breakdown
- Delete stale `.tmp` files in `~/.tkk/read_log/` if any persist after a crash (rare; defensive cleanup)

---

## RISKS + MITIGATIONS (UPDATED v3)

| Risk | Mitigation |
|---|---|
| False positive block | Bypass 1-4 + small-read exemption + override sentinel + config kill switch |
| Hook crashes break cc | try/except wrapping ALL logic, default-allow, errors logged |
| State log grows unbounded | Time + count pruning per call; decisions.jsonl rotated at 100MB |
| Override sentinel reaches Read tool | Stdout JSON mutation per verified API; install verifies cc version >= 2.0.10 |
| Race conditions on parallel lanes | Lockfile-as-directory pattern (atomic os.mkdir), stale-lock recovery |
| Lock timeout silently disables guard | Counter tracked in counters.json; burn_audit surfaces it |
| Path normalization wrong on Windows | `pathlib.resolve()` + lowercase + forward-slash; fnmatch for globs |
| File changed but stale entries persist | Bypass 4 PURGES prior entries for that file before treating as Rule A |
| settings.json corruption during merge | Backup before write, abort if JSON has comments, atomic .tmp + replace |
| cc version too old for mutation API | install/verify_version.ps1 checks `claude --version >= 2.0.10`, aborts if older |
| Bypass 4 deadlock from v2 | Lock acquired BEFORE bypasses (Step 1.5), so bypasses can update state |

---

## DEFINITION OF DONE

- [ ] All 34 pytest tests pass
- [ ] Hook installed and registered, existing `pretooluse_enforce_rules.py` preserved
- [ ] Smoke test: open cc, read a file twice (full re-read), see block on second
- [ ] Override path tested live: `limit: 999911` allowed, actual Read sees limit=2000
- [ ] mtime test live: read file, edit it, read again — allowed with prior entries purged
- [ ] Config kill switch tested: `enabled: false` makes hook pass-through
- [ ] State pruning tested at boundaries (199, 200, 201, 250 entries)
- [ ] settings.json backup created with timestamp before merge
- [ ] One full real cc session run end-to-end, decisions logged
- [ ] `burn_audit.ps1` produces readable report from real session data
- [ ] Repo pushed to GitHub
- [ ] README + override.md + recovery.md complete

---

## CHANGE LOG v4 → v5 (8 fixes from reviewer pool round 3 — final cleanup)

1. **TEST CASES header:** "26 — expanded from 21" → "30 — final after 3 review rounds" (then bumped to 34 with new tests).
2. **Stale `counters.json` in Bypass 1:** updated to `counters/<session_id>.json`.
3. **Stale `counters.json` in Failure Mode (lock timeout):** updated to per-session shard.
4. **Stale `counters.json` in Recovery Docs:** clarified per-session shards + burn_audit aggregation.
5. **decisions.jsonl writes on bypass paths:** added explicit "IMPORTANT" block at top of Step 2 saying ALL exit paths (bypasses + rules) must write to decisions.jsonl. Burn audit would have undercounted otherwise.
6. **Fire prompt "Write all 26 pytest tests":** updated to 34.
7. **Test #31 added:** `enabled: false` kill switch verification (was in DoD, missing from tests).
8. **Tests #32-34 added:** exit code 2 on block, empty stdout on non-override allow, decisions.jsonl written on every exit path.

Executor model also changed from GPT-5 Codex → Opus 4.7 (per Victor's call, abacusai routes Opus on its own quota).

---

## CHANGE LOG v3 → v4 (5 fixes from reviewer pool round 2)

1. **counters.json global-race fixed:** sharded to `~/.tkk/counters/<session_id>.json` (per-session files). Aggregation happens at read-time in burn_audit. No global lock contention.
2. **Test count contradiction resolved:** 22 → 30 everywhere (DELIVERABLES, TEST CASES header, DoD all aligned). Added 4 new tests (27-30) for round-2 fixes.
3. **Rule C overlap math defined:** computes union of all prior intervals via `merge_intervals` helper, overlap = intersection-with-union / new-request-lines. Includes complete Python pseudocode.
4. **pytest retry cap:** hard 5-cycle limit in Phase 1 of fire prompt. If still failing, HALT and report verbatim — no infinite token-burn loops.
5. **install.ps1 fully enumerated:** explicit 8-step list (preflight, mkdirs, copy hook, write config, backup settings, merge settings, smoke test, summary). Idempotency check added.

---

## CHANGE LOG v2 → v3 (12 fixes from reviewer pool round 1)

1. **Lock mechanism switched:** `msvcrt.locking` → lockfile-as-directory (atomic `os.mkdir`). Fixes 1-byte lock bug + fd complexity.
2. **Override mutation API verified and documented:** `hookSpecificOutput.updatedInput` per cc v2.0.10+ docs. Install script verifies version.
3. **Bypass 4 deadlock fixed:** Lock + state load moved to Step 1.5 (BEFORE bypasses), so any rule can update state.
4. **Bypass 4 purge logic added:** When file changes, PURGE prior entries before logging fresh (prevents Rule C from using stale ranges).
5. **`pathlib.match()` → `fnmatch.fnmatch`:** for `always_fresh_patterns` glob matching. Pathlib's match() broken with `**` on Windows.
6. **`decisions.jsonl` writer defined:** in STATE FILES section. Append-only global log, rotated at 100MB.
7. **Pruning contradiction resolved:** Honest budget — full load required for count-based prune. Performance target relaxed 200ms → 300ms.
8. **settings.json merge:** explicit backup-before-write, abort on JSON comments, atomic write.
9. **Rule B gap 1001-9999 lines:** explicitly documented as intentional (chunked re-reads must be ≤1000).
10. **Lock timeout counter:** tracked in counters.json, surfaced by burn_audit so silent guard-failures are visible.
11. **Hardcoded username:** installer resolves `$env:USERPROFILE` at install time, bakes resolved path into settings.json.
12. **Recovery docs:** explicit `recovery.md` with kill-switch, uninstall, and manual revert instructions.

Added test cases for all the new fixes (22 → 26 tests).

---

## FIRE PROMPT (for abacusai / GPT-5 Codex)

```
abacusai -p --dangerously-skip-permissions --model OPENAI_GPT5_CODEX

Read D:\Dev\scratch\spec_TKK_phase1_read_guard.md and execute it end-to-end.

This builds Phase 1 of TKK (Token-Killer-Kit) — a PreToolUse hook that blocks
redundant Read tool calls in Claude Code. Real measured target: cc re-read
memory_ui_server.py 443 times in one session. Hook makes that impossible.

CRITICAL: cc runs in Windows Terminal native (not WSL). All paths Windows-style.
DO NOT use msvcrt for locking — use atomic os.mkdir lockfile pattern per spec.
DO NOT use pathlib.Path.match() for ** globs — use fnmatch.fnmatch.

Execution plan:

PHASE 0 — Pre-flight (DO FIRST, REPORT BACK BEFORE PROCEEDING):
  - Verify python --version >= 3.10
  - Verify claude --version >= 2.0.10 (mutation API requirement)
  - Show me the current contents of ~/.claude/settings.json (existing hooks)
  - Confirm D:\Dev\Projects\tkk\ doesn't already exist (or is safe to overwrite)
  - HALT and ask me before proceeding to Phase 1

PHASE 1 — Build the hook:
  - Create repo structure at D:\Dev\Projects\tkk\
  - Write hooks\tkk_read_guard.py with all bypass rules (1-4) and block rules (A-D)
  - Write hooks\burn_audit.ps1
  - Write all 34 pytest tests
  - Run pytest. If failures: fix and re-run. HARD CAP: 5 fix-retry cycles.
    If pytest still has failures after 5 cycles, HALT and report all failures
    verbatim. Do NOT keep trying — that's a token-burn loop.
  - Show me pytest output (final or 5th-cycle-failure), HALT before Phase 2

PHASE 2 — Build installer:
  - Write install\install.ps1 (merge settings.json safely, backup first)
  - Write install\uninstall.ps1
  - Write install\verify_version.ps1
  - Write README, docs\override.md, docs\recovery.md
  - HALT before live install

PHASE 3 — Live install + smoke test:
  - Run install.ps1
  - Verify ~/.tkk/ created
  - Verify settings.json contains both old hook AND new TKK hook
  - Smoke test: pipe sample stdin to hook, verify exit 0 and clean stderr
  - HALT before git push

PHASE 4 — Push:
  - git init in D:\Dev\Projects\tkk\
  - First commit: full build
  - Create repo at git@github.com:innov8ideas4u-alt/tkk.git (if not exists)
  - Push
  - Report SHA + status

CODING RULES (do not skip):
- stdlib only for the hook (no numpy/pandas/requests)
- UTF-8 no BOM on all files
- LF line endings on .py, CRLF on .ps1
- Use Victor's canonical git_push.ps1 if present (check D:\Dev\Projects\pgvector_load\git_push.ps1 for the pattern)
- Bypass rules run AFTER lock acquired but BEFORE block rules (Step 1.5 → Step 2 → Step 3)
- Override sentinel limit=999911 must emit stdout JSON with updatedInput.limit=2000
- Stdout JSON on Bypass 1 path must be the ONLY thing on stdout
- All other paths: empty stdout, exit 0 (allow) or exit 2 (block) with stderr message

Stop and ask before any commit. Plan everything in scratch first.
```

---

## NOTES FOR FUTURE PHASES

After Phase 1 ships and stabilizes for ~1 week, the next phases:

- **Phase 2:** Stop hook → breadcrumb file → replaces `/resume` cache invalidation tax
- **Phase 3:** SessionStart hook → Ollama-compressed atlas brief
- **Phase 4:** PostToolUse on Bash → Ollama bash output compression (RTK-equivalent)
- **Phase 5:** Burn alarm Windows toast at $25/$50/$100 thresholds

All four will leverage the same lockfile pattern and decisions.jsonl from Phase 1.
