# TKK - Token Killer Kit

Phase 1: Read Guard. Phase 2: Ghost CI (pre-emptive validation daemon).

A Claude Code `PreToolUse` hook that blocks redundant `Read` tool calls.

Real measured target: `memory_ui_server.py` was re-read 443 times in a single
session. 5,554 Read calls in 14 days, ~30-50% of cc cost. This hook makes
that impossible.

## Status

- Spec: v5 final (3 review rounds applied)
- Tests: 35/35 passing (stdlib only)
- Platform: Windows Terminal native, Python 3.10+, claude >= 2.0.10

## What it does

For every PreToolUse-on-Read event:

1. **Bypass 1 - Override sentinel.** `limit == 999911` -> ALLOW, mutate to
   `limit=2000` via stdout JSON.
2. **Bypass 2 - Always-fresh paths.** `*.log`, `**/scratch/**`, `**/tmp/**`,
   `**/.tkk/**`, etc. -> always ALLOW.
3. **Bypass 3 - Recently modified.** `mtime > now - 60s` -> ALLOW.
4. **Bypass 4 - File changed since last read.** Purge prior entries, ALLOW.
5. **Rule A** First read of file -> ALLOW.
6. **Rule B** Re-read with bounded `offset`/`limit` (limit<=1000) -> ALLOW.
7. **Rule C** Re-read overlapping >80% with prior reads (and >=100 lines) -> BLOCK.
8. **Rule D** Re-read with no/unbounded range -> BLOCK.

Failure-mode: any exception -> log to `~/.tkk/errors.log`, default-ALLOW.
Token waste is recoverable; blocking cc on a hook crash is not.

## Install

```powershell
git clone https://github.com/innov8ideas4u-alt/tkk.git D:\Dev\Projects\tkk
cd D:\Dev\Projects\tkk
powershell -NoProfile -ExecutionPolicy Bypass -File .\install\install.ps1
```

The installer:
- Verifies `python >= 3.10` and `claude >= 2.0.10`
- Creates `~/.tkk/{read_log,counters,locks}/`
- Copies the hook into `~/.claude/hooks/tkk_read_guard.py`
- Writes default `~/.tkk/config.json` (preserves existing)
- Backs up `~/.claude/settings.json` to `settings.json.bak.<timestamp>`
- Atomically merges the new entry (idempotent - safe to re-run)
- Runs a smoke test
- Prints a summary

## Verify

Open a new Claude Code session, ask Claude to read a file, then ask it to
read the same file again with no offset/limit. The second call should
block with `TKK Read Guard (Rule D): ...`.

## Override

Pass `limit: 999911` in any Read call to force a full re-read:

```
Read(file_path="big_file.py", limit=999911)
```

The hook mutates `limit` to 2000 (configurable via
`override_replacement_limit`).

See [`docs/override.md`](docs/override.md) for details.

## Kill switch

```powershell
'{"enabled": false}' | Set-Content $env:USERPROFILE\.tkk\config.json
```

Re-enable by restoring `enabled: true` or running the installer again.

## Uninstall

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install\uninstall.ps1
```

Add `-PurgeState` to also wipe `~/.tkk/`.

## Audit

```powershell
powershell -NoProfile -File .\hooks\burn_audit.ps1
```

Aggregates per-session counter shards + `~/.tkk/decisions.jsonl` and prints
total invocations, blocks by rule, estimated tokens saved, top 10 blocked
files, top 10 overridden files. Writes JSON summary to
`~/.tkk/burn_report_<timestamp>.json`.

## Layout

```
D:\Dev\Projects\tkk\
  hooks\
    tkk_read_guard.py     # the hook (stdlib only)
    burn_audit.ps1        # aggregation + report
  install\
    install.ps1           # 8-step install
    uninstall.ps1         # restore backup + remove hook
    verify_version.ps1    # claude >= 2.0.10 check
  tests\
    test_read_guard.py    # 35 pytest tests
    conftest.py
    fixtures\
  docs\
    design.md             # full v5 spec
    override.md           # how to bypass
    recovery.md           # if cc breaks
```

## State dir layout

```
~/.tkk/
  config.json                       # runtime config
  decisions.jsonl                   # global append-only decision log
  errors.log                        # hook crashes
  read_log/<session_id>.jsonl       # per-session read history
  counters/<session_id>.json        # per-session counter shard
  counters/_archive/                # >30d shards archived by burn_audit
  locks/<session_id>.lock           # atomic os.mkdir lockfile (dir)
```

## Roadmap (future phases)

- Phase 2: Ghost CI (DONE — see below)
- Phase 3: Stop-hook breadcrumb file (replaces `/resume` cache tax)
- Phase 4: PostToolUse Bash output compression
- Phase 5: Burn-alarm Windows toast at $25/$50/$100 thresholds

---

# Phase 2: Ghost CI

A Windows-native Python daemon that watches `.py` saves, runs `pytest --testmon`,
distills failures via local Ollama (llama3.1:8b-instruct-q8_0 on port 11535),
and writes alerts to `.atlas/00-urgent-alerts.md` before cc can react.

Goal: cc cannot beat the validation loop. By the time cc formulates its next
tool call, the alerts file already reflects reality.

## Status

- Spec: v5 final (`docs/ghost_ci_design.md`)
- Tests: 55/55 passing (`ghost_ci/tests/`)
- Deps: watchdog 4.0.0, aiohttp 3.9.3, psutil 5.9.8, colorama 0.4.6, pytest-testmon 2.1.1

## Architecture

1. **SessionStart hook** spawns `daemon.py` detached (`CREATE_NO_WINDOW`),
   PID-pinned to the cc Node.js process. Daemon dies when cc dies.
2. **Watchdog** watches the project root for `.py` saves.
3. **Debouncer** (1.5s window) coalesces edit bursts per-file.
4. **pytest --testmon** runs only impacted tests in an isolated cache
   (`.ghost_pytest_cache/`).
5. **Distiller** sends failing traceback to Ollama; produces 1-2 sentence summary.
6. **Atomic writer** (tempfile + `os.replace` + lockdir) updates
   `.atlas/00-urgent-alerts.md`. cc reads it before every tool call.

SyntaxError mid-keystroke is silently swallowed (Trap 1: schizophrenic loop).

## Install (Phase 2)

```powershell
cd <your-project-with-.atlas>
pwsh -NoProfile -ExecutionPolicy Bypass -File D:\Dev\Projects\tkk\install\install_ghost_ci.ps1
```

Pre-flight verifies Ollama on port 11535 has llama3.1:8b-instruct-q8_0 pulled.

## Kill switch (Phase 2)

```powershell
'{"ghost_ci_enabled": false}' | Set-Content $env:USERPROFILE\.tkk\ghost_ci\config.json
```

Daemon polls config every 2s and shuts down within that window.

## Uninstall (Phase 2)

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File D:\Dev\Projects\tkk\install\uninstall_ghost_ci.ps1
```

Restores most recent `settings.json.bak.ghost_ci.*` backup, removes
`~/.tkk/ghost_ci/`, kills any running daemon. Phase 1 hooks preserved.

## Telemetry (Phase 2.5a)

Ghost CI emits one append-only JSON line per validation event to
`.atlas/ghost_telemetry.jsonl` (gitignored, runtime data).

Failure-safe: telemetry never crashes the daemon. Phase 2.5b (next week)
adds the analyzer + dashboard; 2.5a just captures raw data.

Schema (14 fields per record):

- `timestamp` — UTC ISO 8601 with `Z` suffix
- `event` — `ghost_run` | `warm_up`
- `trigger_file` — relative path of changed file
- `test_target` — resolved test target (or `null`)
- `selection_mode` — `testmon` | `testmon_interrupted` | `warm_up`
- `exit_code` — pytest exit code (`-15` if interrupted)
- `pytest_output_bytes` — total stdout+stderr bytes
- `pytest_duration_ms` — wall time
- `distillation_attempted` / `distillation_succeeded` — bool
- `distillation_tokens_out` — `len(json.dumps(distilled))` proxy
- `ollama_latency_ms` — model call wall time (or `null`)
- `model_resident` — `True` if Ollama kept model in RAM
- `alert_written` — `True` if a `00-urgent-alerts.md` entry was appended

## License

MIT (see LICENSE).
