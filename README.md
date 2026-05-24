# TKK - Token Killer Kit, Phase 1: Read Guard

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

- Phase 2: Stop-hook breadcrumb file (replaces `/resume` cache tax)
- Phase 3: SessionStart Ollama-compressed atlas brief
- Phase 4: PostToolUse Bash output compression
- Phase 5: Burn-alarm Windows toast at $25/$50/$100 thresholds

## License

MIT (see LICENSE).
