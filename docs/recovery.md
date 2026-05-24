# TKK Read Guard — Recovery & Panic Guide

If the guard is misbehaving, here is the escalation ladder.

## 1. Kill switch (instant disable, no restart)

Edit `~/.tkk/config.json` and set:
```json
{ "enabled": false }
```

The hook exits immediately on the next Read call with exit 0 and writes nothing else. All Reads pass through unfiltered.

**Oneliner (PowerShell):**
```powershell
(Get-Content $env:USERPROFILE\.tkk\config.json -Raw | ConvertFrom-Json) | ForEach-Object { $_.enabled = $false; $_ } | ConvertTo-Json | Set-Content $env:USERPROFILE\.tkk\config.json -Encoding utf8
```

Or just write `{"enabled": false}` to the file.

## 2. Uninstall (full removal)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\Dev\Projects\tkk\install\uninstall.ps1
```

This restores `~/.claude/settings.json` from the most recent `.bak.<timestamp>` backup, or surgically removes only the TKK hook entry if no backup is found. `~/.tkk/` is left in place (your logs and counters survive).

## 3. Manual revert (if uninstall fails)

a) Edit `~/.claude/settings.json`. Find the `PreToolUse` array entry with `matcher: "Read"` and the command pointing to `tkk_read_guard.py`. Delete that entry. Save.

b) Restart Claude Code (or just open a new session — settings reload on session start).

c) Optionally: `Remove-Item -Recurse $env:USERPROFILE\.tkk` to wipe all state.

## 4. Diagnostics

**Errors:** `~/.tkk/errors.log` — any exception from the hook lands here. The hook itself always exits 0 on error (fail-open) so it never breaks Claude.

**Per-session decisions:** `~/.tkk/read_log/<session_id>.jsonl` — one line per Read decision, with the rule/bypass that fired.

**Global audit:** `~/.tkk/decisions.jsonl` — append-only mirror of every decision across all sessions.

**Counters:** `~/.tkk/counters/<session_id>.json` — per-session read counts per normalized path. Used by Rule A (5x same path).

## 5. Burn-down audit (what got blocked recently?)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\Dev\Projects\tkk\hooks\burn_audit.ps1
```

Prints a summary table of bypass/block tallies across `decisions.jsonl`.

## 6. Stale `.tmp` cleanup

If a write was interrupted, you may see `*.tmp` files in `~/.tkk/read_log/` or `~/.tkk/counters/`. They're safe to delete; the hook writes via atomic `os.replace`, so any `.tmp` that survived means the rename never landed.

```powershell
Get-ChildItem $env:USERPROFILE\.tkk -Recurse -Filter *.tmp | Remove-Item
```

## 7. Locks stuck

Locks are atomic `mkdir` directories under `~/.tkk/locks/`. If a hook crashed mid-run with a held lock, the next call will time out (default 2s) and fail-open. Manually clear:

```powershell
Remove-Item -Recurse $env:USERPROFILE\.tkk\locks\*
```
