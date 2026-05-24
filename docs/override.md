# TKK Read Guard — Override Mechanisms

When the guard blocks a Read you legitimately need, there are two escape hatches.

## 1. Sentinel Override (per-call, one-shot)

Set `limit: 999911` on the Read call. The guard rewrites it to `limit: 2000` and lets it through, regardless of prior reads.

**Use when:** you want a single fresh read of a file the guard would otherwise block.

**How it shows up:** the Read tool receives `limit=2000`. The guard emits the override JSON on stdout (the only path that writes stdout content). The decision is logged as `bypass: sentinel_override`.

**Example tool call (in your prompt to Claude):**
> Read `important.log` with limit 999911 — I need a fresh look.

Claude will pass `limit: 999911` to the Read tool. Guard intercepts, rewrites, allows.

## 2. `always_fresh_patterns` Config (persistent, per-pattern)

Add glob patterns to `~/.tkk/config.json` under `always_fresh_patterns`. Matching paths always bypass.

**Use when:** a specific file or directory needs to be re-readable on every call (e.g. a live log you're tailing across the session).

**Example `~/.tkk/config.json`:**
```json
{
  "enabled": true,
  "always_fresh_patterns": [
    "**/live_tail.log",
    "C:/var/log/active/*"
  ]
}
```

Patterns are matched via `fnmatch.fnmatch` against the normalized path. Bare patterns like `*.log` are also matched against the basename.

**Logged as:** `bypass: always_fresh_pattern`.

## Which to use?

| Situation | Use |
|---|---|
| One-time fresh read | Sentinel |
| Recurring file, every read | Pattern |
| Emergency, guard misbehaving | Kill switch (`enabled: false`) — see `recovery.md` |
