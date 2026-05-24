# COOK SPEC — TKK Phase 2: Ghost CI (v5 — Final, ready to fire)
## ID: TKK-P2-GHOSTCI | Priority: P0 | Owner: abacusai (Opus 4.7)

> **Purpose:** Pre-emptive validation loop. Runs tests on file save, compresses errors via local 4070 Ti, injects results into cc context BEFORE next API turn. Eliminates synchronous test-execution token cost.

**Version history:**
- v1 (2026-05-23) — Gemini 2.5 Pro initial cook spec
- v2 (2026-05-23) — Gemini follow-up clarifications: 8 architecture patches
- v3 (2026-05-23) — OpenRouter R1 + Gemini patches: 12 fixes (5 runtime-crash bugs, 6 consensus issues, testmon dependency graph)
- v4 (2026-05-23) — OpenRouter R2 patches: 5 fixes (1 critical daemon-launch bug + 4 mechanical drifts)
- v5 (2026-05-23) — Gemini 2nd reflection: 3 final critical fixes (PID traversal, project-root resolution, pytest cache isolation) + 7th pre-mortem (WinError 32 cache collision)

---

## CRITICAL — EXECUTOR ROUTING

**This cook runs on `abacusai --model OPUS_4_7`, NOT raw Claude Code session.**

Rationale: Victor's cc weekly quota recently hit 95% (now reset). Hook development
needs Opus-level reasoning (process management, async pipelines, regex carefully).
abacusai routes Opus on its own quota — same model intelligence, different bucket.

```
abacusai -p --dangerously-skip-permissions --model OPUS_4_7
```

---

## MEASURED TARGET & TOKEN ECONOMICS

Based on Victor's empirical data:
- **Current state:** 5,361 Bash tool calls in 14 days, ~3,500 tokens/test failure
- **Total context burn:** ~18.76M input tokens / 14 days
- **Cost at Opus pricing ($15/1M):** ~$281.45/sprint on test output ingestion alone

Ghost CI target state:
- Test execution triggers locally (zero API calls)
- 4070 Ti compresses 3,500-token traceback → 400-token summary + tail
- **Total context burn:** 5,361 × 400 = 2.14M tokens
- **Expected savings:** ~88.5% reduction = ~$249/sprint (~$540/month)

Plus: leaner context window = less hallucination = fewer retry loops = secondary savings.

---

## ENVIRONMENT & DEPENDENCIES

- **OS:** Windows 11/10 native (PowerShell/Windows Terminal). No WSL.
- **Runtime:** Python 3.13.7
- **Agent target:** Claude Code 2.1.150 (TKK Phase 1 already installed)
- **Local compute:** RTX 4070 Ti (12GB VRAM) running Ollama on `localhost:11535`
- **Reserved for future phases:** Tesla P40 (Semantic Search Intercept, Session Distillation)

### Python dependencies (`requirements.txt`)

```
watchdog==4.0.0          # Filesystem event monitoring
aiohttp==3.9.3           # Async Ollama API client
psutil==5.9.8            # PID monitoring, process-tree termination
colorama==0.4.6          # Daemon console output
pytest-testmon==2.1.1    # Dependency-graph aware test selection (Fix 12)
```

### File layout

```
D:\Dev\Projects\TKK\                  # Capital TKK — matches GitHub repo
├── ghost_ci\                          # NEW — sibling to existing hooks/
│   ├── daemon.py                      # Main daemon entry
│   ├── spawn_ghost.py                 # Launcher invoked by SessionStart hook
│   ├── event_handler.py               # Watchdog handler with two-stage filter
│   ├── pipe_reader.py                 # Deadlock-free STDOUT reader
│   ├── distiller.py                   # 4070 Ti Ollama client
│   ├── alerts.py                      # .atlas/00-urgent-alerts.md writer
│   ├── mutex.py                       # Daemon singleton lock
│   ├── requirements.txt
│   └── tests\
│       ├── test_event_handler.py
│       ├── test_pipe_reader.py
│       ├── test_distiller.py
│       ├── test_alerts.py
│       ├── test_mutex.py
│       ├── test_pid_lifecycle.py
│       ├── test_integration.py
│       └── conftest.py
├── hooks\                             # EXISTING (Phase 1)
│   └── tkk_read_guard.py
└── install\
    └── install_ghost_ci.ps1           # NEW — extends Phase 1 installer
```

State files:
- `.atlas/00-urgent-alerts.md` — populated by Ghost CI, read by cc
- `.atlas/ghost_ci.pid` — daemon mutex (atomic O_EXCL)
- `~/.tkk/ghost_ci/config.json` — kill switch + tunable params
- `~/.tkk/ghost_ci/daemon.log` — rotating log
- `.testmondata` — pytest-testmon dependency matrix (in project root, gitignored)

Ghost CI is **stateless by design** between daemon restarts. All transient state (debounce queue, in-flight pytest, deque buffer) lives in memory. Persistent state is limited to config (read-only from daemon's perspective), the alert file (written by daemon), and testmon's dependency matrix.

---

## ARCHITECTURE & CORE LOGIC

Ghost CI is a deterministic state machine driven by filesystem events. The daemon
runs as a detached process **slaved to the cc parent PID** — when cc dies, daemon
dies. No zombies. No manual cleanup.

### State machine

```
IDLE → DEBOUNCING → EXECUTING → DISTILLING → INJECTING → IDLE
```

### A. The PID-Pinned Epidemic Hook (daemon lifecycle)

This pattern guarantees the daemon mirrors cc's lifecycle exactly:

**Spawning flow:**

1. cc fires `SessionStart` hook → executes `spawn_ghost.py`
2. `spawn_ghost.py` reads `os.getppid()` (the parent's PID = cc's node.exe)
3. Validates that parent exists. **CRITICAL: Two Windows-specific traversals required.**

   **(a) PID resolution — DO NOT use `os.getppid()`.** On Windows, cc fires hooks via cmd.exe shell wrappers. Your immediate parent is cmd.exe, which dies the moment the hook completes. Heartbeat would detect the dead cmd.exe and kill Ghost CI within 5 seconds of every session start. **Walk the process tree until you find the persistent `node.exe`** that represents Claude Code itself.

   **(b) Project root resolution — DO NOT use `os.getcwd()`.** If cc launches from a subdirectory (e.g. `D:\Dev\Projects\TKK\src\hooks\`), `.atlas/` lands in the wrong place and the daemon monitors the wrong tree. **Walk upward looking for `.git/` or `.atlas/`** to find the actual project root.

   ```python
   import os, sys, subprocess
   from pathlib import Path
   import psutil

   CREATE_NO_WINDOW = 0x08000000

   def get_claude_node_pid() -> int:
       """Traverse up the process tree to find the persistent node.exe.
       Falls back to os.getppid() if node.exe isn't found (compiled binary case)."""
       current = psutil.Process(os.getpid())
       while current.parent() is not None:
           parent = current.parent()
           if parent.name().lower() == "node.exe":
               return parent.pid
           current = parent
       return os.getppid()

   def get_project_root() -> Path:
       """Traverse up the filesystem to find the actual project root
       (anchored by .git/ or existing .atlas/). Falls back to cwd."""
       cur = Path(os.getcwd()).resolve()
       for candidate in [cur] + list(cur.parents):
           if (candidate / ".git").exists() or (candidate / ".atlas").exists():
               return candidate
       return cur

   if __name__ == "__main__":
       SCRIPT_DIR = Path(__file__).resolve().parent     # ~/.tkk/ghost_ci/
       DAEMON_PATH = SCRIPT_DIR / "daemon.py"           # absolute, install-dir-anchored

       PROJECT_ROOT = get_project_root()
       atlas_dir = PROJECT_ROOT / ".atlas"
       atlas_dir.mkdir(exist_ok=True)

       target_pid = get_claude_node_pid()

       subprocess.Popen(
           [
               sys.executable,
               str(DAEMON_PATH),
               "--slave-to-pid", str(target_pid),
               "--project-root", str(PROJECT_ROOT),
           ],
           creationflags=CREATE_NO_WINDOW,
           close_fds=True,
           cwd=str(PROJECT_ROOT),
       )
   ```

   Key changes vs v4:
   - `get_claude_node_pid()` traverses process tree (v5 fix — cmd.exe wrapper trap)
   - `get_project_root()` traverses filesystem (v5 fix — subdir launch trap)
   - `sys.executable` not bare `"python"` (uses same interpreter cc launched with)
   - `DAEMON_PATH` is absolute, derived from `__file__`
   - `cwd=PROJECT_ROOT` explicit + `--project-root` arg passed to daemon
   - `atlas_dir.mkdir(exist_ok=True)` (v4 fix — `.atlas/` may not pre-exist)
4. spawn_ghost.py exits — hook completes quickly (cc proceeds without delay)

**Daemon heartbeat — SystemObserverThread (merged lifecycle + config-poll):**

Single low-frequency thread handles BOTH parent-PID death detection AND config kill switch. Avoids the trap of running config I/O inside the high-frequency watchdog event loop:

```python
import psutil, time, json, sys, threading

class SystemObserverThread(threading.Thread):
    def __init__(self, parent_pid: int, config_path: str, poll_seconds: float = 2.0):
        super().__init__(daemon=True)
        self.parent_pid = parent_pid
        self.config_path = config_path
        self.poll_seconds = poll_seconds

    def run(self):
        while True:
            # 1. Epidemic lifecycle: parent dead → daemon commits suicide
            if not psutil.pid_exists(self.parent_pid):
                cleanup_locks()
                kill_pytest_subprocesses()
                sys.exit(0)

            # 2. Config kill-switch check
            try:
                with open(self.config_path, "r") as f:
                    config = json.load(f)
                    if not config.get("ghost_ci_enabled", True):
                        cleanup_locks()
                        kill_pytest_subprocesses()
                        sys.exit(0)
            except (FileNotFoundError, json.JSONDecodeError):
                pass  # Fail open — assume enabled if config missing/locked

            time.sleep(self.poll_seconds)
```

**Cleanup obligations** on shutdown (parent death OR kill switch):

1. Release `.atlas/ghost_ci.pid` mutex
2. Release `.atlas/00-urgent-alerts.md.lock` if held
3. SIGTERM any active pytest subprocesses
4. Flush deque buffers to log
5. `sys.exit(0)`

### B. The Daemon Mutex (Cross-Session Alert Hijack prevention)

On boot, the daemon claims `.atlas/ghost_ci.pid` via atomic file creation:

```python
import os, sys

LOCK_PATH = ".atlas/ghost_ci.pid"

def acquire_daemon_mutex():
    """Atomic mutex. Second daemon in same dir self-terminates."""
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        existing_pid = read_existing_pid(LOCK_PATH)
        if not psutil.pid_exists(existing_pid):
            # Stale lock from crashed daemon — reclaim
            os.remove(LOCK_PATH)
            return acquire_daemon_mutex()  # one retry
        print(
            f"FATAL: Ghost CI daemon (PID {existing_pid}) already monitoring "
            f"this directory.\n"
            f"Parallel cc sessions MUST use separate git worktrees:\n"
            f"  git worktree add ../{os.path.basename(os.getcwd())}_branch\n"
            f"Then launch the second cc session inside the new worktree.",
            file=sys.stderr,
        )
        sys.exit(1)
```

**Stale lock handling:** if existing PID isn't running, reclaim once. Prevents
stuck mutex after daemon crash.

### C. The Watchdog Event Filter (two-stage)

Reject everything that's not actual user source code:

```python
import os
from watchdog.events import FileSystemEventHandler

IGNORED_DIRS = (
    ".git", "__pycache__", ".pytest_cache", ".tkk", ".atlas",
    "venv", "env", ".tox", "node_modules", ".vscode", ".idea",
)
IGNORED_SUFFIXES = (".pyc", ".pyo", ".pyi", ".swp", ".swo", ".bak", "~")

class GhostCIEventHandler(FileSystemEventHandler):
    def __init__(self, event_queue):
        self.queue = event_queue

    def is_valid_target(self, path: str) -> bool:
        """Three-boolean filter. Each check independent and testable."""

        # Stage 1: Must be a real .py file (not swap/backup/bytecode)
        if not path.endswith(".py"):
            return False
        if any(path.endswith(s) for s in IGNORED_SUFFIXES):
            return False

        # Stage 2: Path must not contain ignored directory components
        parts = os.path.normpath(path).split(os.sep)
        if any(p in IGNORED_DIRS for p in parts):
            return False

        # Stage 3: Reject test files — enforces unidirectional loop
        # (cc editing tests is "test-maintenance mode"; CI stays silent)
        filename = os.path.basename(path)
        if filename.startswith("test_") or filename.endswith("_test.py"):
            return False

        return True

    def on_modified(self, event):
        if not event.is_directory and self.is_valid_target(event.src_path):
            self.queue.put(event.src_path)
```

**Three readable boolean stages** — preferred over a single cryptic regex with
lookbehinds. Each stage independently testable.

### D. Debouncing — MultiFileDebouncer (per-file timestamps)

IDEs and cc Edit tool may produce multiple `on_modified` events per save (50-200ms apart). The debouncer MUST handle multiple distinct files saved concurrently — a single `last_path` variable drops the first file silently. Per-file dict keyed by path:

```python
import time, threading

class MultiFileDebouncer:
    def __init__(self, debounce_seconds: float = 1.5):
        self.debounce_seconds = debounce_seconds
        self._files: dict[str, float] = {}
        self._lock = threading.Lock()

    def trigger(self, file_path: str):
        """Called by watchdog event handler. Stamps file with current time."""
        with self._lock:
            self._files[file_path] = time.time()

    def get_ready_files(self) -> list[str]:
        """Yields files whose quiet-period has elapsed. Worker calls this each tick."""
        ready = []
        now = time.time()
        with self._lock:
            for path, ts in list(self._files.items()):
                if now - ts >= self.debounce_seconds:
                    ready.append(path)
                    del self._files[path]
        return ready
```

Worker loop polls `get_ready_files()` every ~100ms. Each ready file gets its own pytest invocation in sequence (no concurrent pytest — would tear up the same source tree). If pytest is in-flight when a new file becomes ready, the running pytest gets SIGTERM and restarts on the queue tail.

### E. Subprocess Creation — CREATE_NO_WINDOW + Cache Isolation (v5 fix)

**CRITICAL Windows gotcha #1:** `subprocess.DETACHED_PROCESS` (0x00000008) severs the new process from the parent's console subsystem. When pytest is spawned with `stdout=subprocess.PIPE` AND `DETACHED_PROCESS`, the stdout pipe frequently binds to `None`, silently breaking the pipe reader and the entire distillation pipeline.

**CRITICAL Windows gotcha #2 (v5 — 7th pre-mortem):** If Ghost CI runs `pytest --testmon` in the background AND cc separately fires `pytest` via the Bash tool, both pytest instances collide on `__pycache__/*.pyc`, `.pytest_cache/v/cache/stepwise`, and `.testmondata`. Windows mandatory file locking throws `PermissionError: [WinError 32] The process cannot access the file...` cc reads this traceback, doesn't understand Ghost CI exists, and hallucinates `os.chmod` fixes into the source code. Application logic gets destroyed chasing a phantom env error.

**Solution: Use `CREATE_NO_WINDOW` (0x08000000) AND isolate ALL pytest caches** from anything the user might invoke directly:

```python
import os, subprocess

CREATE_NO_WINDOW = 0x08000000

def execute_pytest(target_args: list[str], project_root: str) -> subprocess.Popen:
    """Spawn pytest with full cache isolation to avoid WinError 32 collisions
    when cc separately runs pytest via the Bash tool.

    target_args: pytest args like ['-q', '--testmon'] or ['-q', 'tests/test_auth.py']
    """
    ghost_env = os.environ.copy()
    # 1. Prevent .pyc writes — eliminates __pycache__ collision
    ghost_env["PYTHONDONTWRITEBYTECODE"] = "1"
    # 2. Prevent plugin auto-load — eliminates SQLite plugin cache collisions
    ghost_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    return subprocess.Popen(
        [
            "pytest",
            *target_args,
            # 3. Dedicated cache_dir — never collides with user's normal pytest
            "-o", "cache_dir=.ghost_pytest_cache",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # merge stderr into stdout
        creationflags=CREATE_NO_WINDOW,
        text=False,                  # byte streams, decode in pipe reader
        cwd=project_root,
        env=ghost_env,
    )
```

**Add `.ghost_pytest_cache/` and `.testmondata` to project `.gitignore` automatically via installer.**

The `spawn_ghost.py` daemon launcher uses the same flag pattern:

```python
subprocess.Popen(
    [sys.executable, str(DAEMON_PATH), "--slave-to-pid", str(parent_pid),
     "--project-root", str(PROJECT_ROOT)],
    creationflags=CREATE_NO_WINDOW,   # NOT DETACHED_PROCESS
    close_fds=True,
    cwd=str(PROJECT_ROOT),
)
```

### F. Test Selection — pytest-testmon (dependency-graph aware)

**Static naming-convention mapping (`foo.py → test_foo.py`) is FATAL for autonomous agents.** When cc modifies a shared utility (`utils/formatter.py`), the leaf test passes — but downstream tests in `api/`, `database/`, `engine/` are silently broken. Ghost CI reports ALL SYSTEMS NOMINAL. cc commits the cascade failure. Eight weeks later: structural amnesia, agent can't trace cause.

**Solution: pytest-testmon.** Pytest plugin that tracks which lines of code each test exercises. After initial full-suite warm-up, every subsequent `pytest --testmon` selects ONLY the tests whose execution graph touches modified files. Catches downstream cascade failures the moment the source changes.

**Add dependency:** `pytest-testmon==2.1.1` to `requirements.txt`.

**Initial warm-up — invoked by daemon on boot, NOT just declared (R2 Fix):** `daemon.py` MUST run a silent background full-suite execution to build `.testmondata` (the dependency matrix). One-time ~30-60s cost per project. After that, every test run is dependency-aware:

```python
import subprocess, os
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000

def warm_up_testmon(project_root: str) -> bool:
    """Run on daemon boot. Builds .testmondata if missing. Non-blocking — runs
    detached. Returns False if testmon unavailable, True if warm-up launched."""
    testmondata = Path(project_root) / ".testmondata"
    if testmondata.exists():
        return True  # already warm
    try:
        subprocess.Popen(
            ["pytest", "-q", "--testmon", "--co"],  # --co = collect only, fast warm-up
            cwd=project_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        return True
    except FileNotFoundError:
        return False  # pytest-testmon not installed; fall back to full suite
```

Called from `daemon.py` main():

```python
def main():
    args = parse_args()  # --slave-to-pid, --project-root
    acquire_daemon_mutex()
    warm_up_testmon(args.project_root)  # R2 Fix — was declared but never called
    observer_thread = SystemObserverThread(args.slave_to_pid, CONFIG_PATH)
    observer_thread.start()
    # ... start watchdog observer
```

Then dynamic execution:

**Fallback when testmon data is missing or stale** (e.g., fresh project, .testmondata corrupted): run full suite (`pytest -q`), then filter reported errors. **NEVER use `pytest --lf`** — it re-runs last-failed tests regardless of which file was just edited, causing the Ghost Test Disconnect (Trap 2).

**Traceback-touching filter (fallback path):**

```python
def filter_relevant_tracebacks(pytest_output: str, modified_file: str) -> str | None:
    """Returns traceback only if modified_file appears in the trace.
    Otherwise returns None — Ghost CI writes ALL SYSTEMS NOMINAL."""
    if modified_file in pytest_output or modified_file.replace("\\", "/") in pytest_output:
        return pytest_output
    return None  # Failure unrelated to current edit — suppress alert
```

This ensures Ghost CI only surfaces errors *causally linked* to cc's most recent action. cc never gets blamed for pre-existing broken tests in unrelated parts of the codebase.

### G. Mid-test Interruption

If `on_modified` fires while pytest is running:

```python
def interrupt_running_pytest(proc):
    """SIGTERM the process tree, drain pipe, return to DEBOUNCING."""
    parent = psutil.Process(proc.pid)
    for child in parent.children(recursive=True):
        child.terminate()
    parent.terminate()
    try:
        parent.wait(timeout=2.0)
    except psutil.TimeoutExpired:
        parent.kill()  # force
```

Wasted pytest cycles cost <2s. Worth it to keep results aligned with current state.

---

## THE GHOST CI STATE FILE

### Path
`.atlas/00-urgent-alerts.md` — sits inside Victor's existing atlas directory, next to `01-brief.md` through `05-dont-rebuild.md`.

**IMPORTANT: Claude Code does NOT auto-read `.atlas` by default.** The integrity of Ghost CI relies 100% on the CLAUDE.md injection pattern below. The system-prompt directive (appended by `install_ghost_ci.ps1`) is the SOLE mechanism that forces cc to poll `00-urgent-alerts.md` before tool execution. There is no built-in atlas awareness — the directive IS the wiring, not a redundant convenience.

### CLAUDE.md directive (installer appends this exact block)

```markdown
# CRITICAL SYSTEM DIRECTIVE: PRE-EMPTIVE VALIDATION
Before invoking ANY tool, generating ANY code, or formulating ANY plan,
you MUST read `.atlas/00-urgent-alerts.md`.
If this file contains active failures, you are strictly forbidden from
working on new features. Your sole objective is to debug and clear the alerts.
```

"Strictly forbidden" framing is intentional — soft suggestions get ignored
by cc when momentum is high. Hard constraints get respected.

### Schema — on test success

```markdown
# 🟢 ALL SYSTEMS NOMINAL — NO ACTIVE ALERTS
**Last Verified:** 2026-05-24T14:32:10Z
```

### Schema — on test failure

```markdown
# 🔴 URGENT CI ALERTS — IMMEDIATE ACTION REQUIRED
**Generated:** 2026-05-24T14:32:10Z

> ⚠️ Ghost CI detected a failure resulting from your last file modification.
> Do NOT proceed with new features. Fix this immediately.

### 1. DISTILLED ROOT CAUSE
[Local LLM summary — max 3 sentences]

### 2. FAILING TARGET
- **File:** `tests/test_memory_server.py`
- **Line:** 142
- **Exception:** `RuntimeError: dictionary changed size during iteration`

### 3. RAW TRACEBACK TAIL
\`\`\`python
[Last 50 lines of pytest output, verbatim]
\`\`\`
```

### Write coordination — atomic via tempfile + os.replace (Fix 11)

Before overwriting `00-urgent-alerts.md`, the writer guarantees zero observable partial states. If cc reads during the write, it sees either the OLD content or the NEW content — never an empty/half-written file.

```python
import os, tempfile

def write_alert_atomic(target_path: str, content: str):
    """Atomic write — readers never see partial/empty files.
    Guarantees parent directory exists (R2 Fix — .atlas/ may not pre-exist
    in fresh projects)."""
    directory = os.path.dirname(target_path) or "."
    os.makedirs(directory, exist_ok=True)  # R2 Fix #3 — defensive .atlas/ creation
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix="alert_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, target_path)  # atomic on NTFS since Win 10
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
```

Coordination with the lockdir pattern remains:

1. Acquire lockdir: `os.mkdir(".atlas/00-urgent-alerts.md.lock")` — atomic
2. Call `write_alert_atomic(target, content)` — guarantees no partial state
3. Release lockdir: `os.rmdir(...)`
4. If lockdir acquisition fails 3× with 50ms backoff → log warning, skip write (cc may be mid-read; stale alert is better than corrupted alert)

---

## THE 4070 TI ERROR DISTILLER PIPELINE

### Model selection

- **Model:** `llama3.1:8b-instruct-q8_0`
- **VRAM footprint:** ~8.5GB (fits comfortably in 4070 Ti's 12GB)
- **Why Llama 3.1 8B Q8:** rigorously instruct-tuned, resistant to "helpful
  fix suggestions" (we want extraction, not advice)
- **Ollama config:** `keep_alive: "24h"` — never unload from VRAM
- **VRAM eviction check:** before each inference, query `/api/ps` to verify
  model is resident. If evicted → skip distillation, write raw tail directly.

### Deadlock-free pipe reader

```python
import subprocess, threading
from collections import deque

class GhostPipeReader:
    def __init__(self, process, maxlen=1000):
        self.process = process
        self.buffer = deque(maxlen=maxlen)
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        for line in iter(self.process.stdout.readline, b""):
            decoded = line.decode("utf-8", errors="replace").rstrip()
            if decoded:
                self.buffer.append(decoded)
        self.process.stdout.close()

    def tail(self, n=50):
        return list(self.buffer)[-n:]
```

The dedicated reader thread ensures the OS pipe buffer never fills regardless
of how fast pytest dumps output. The bounded `deque(maxlen=1000)` caps memory
even on runaway test output.

### Distillation request

**CRITICAL: Use f-strings + json.dumps(), NEVER `%s` formatting.** Tracebacks contain literal `%` characters (printf format specs, modulo math, URL encoding) which crash legacy `%`-formatting with `TypeError: not all arguments converted during string formatting`.

```python
import aiohttp, json, asyncio

OLLAMA_URL = "http://localhost:11535/api/generate"
DISTILLATION_TIMEOUT = 3.0  # hard cap

def generate_distill_payload(traceback_tail: str) -> str:
    """Build JSON payload safely — f-string interpolation immune to `%` chars,
    json.dumps escapes quotes/backslashes/newlines for HTTP transport."""
    raw_prompt = (
        "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
        "You are an error distillation pipeline for an autonomous AI.\n"
        "Your sole job is to extract the root cause of the test failure.\n"
        "DO NOT suggest fixes. DO NOT write code. Output ONLY valid JSON:\n"
        "{\n"
        '  "summary": "Concise 2-sentence explanation of what broke",\n'
        '  "file": "Main file causing the error",\n'
        '  "line_number": <integer or null>,\n'
        '  "exception_type": "Name of the exception (e.g. ValueError)"\n'
        "}\n"
        "<|eot_id|><|start_header_id|>user<|end_header_id|>\n"
        "Extract the root cause from this traceback tail:\n\n"
        f"{traceback_tail}\n"
        "<|eot_id|><|start_header_id|>assistant<|end_header_id|>"
    )
    return json.dumps({
        "model": "llama3.1:8b-instruct-q8_0",
        "prompt": raw_prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0, "num_predict": 150},
    })

async def distill_error(traceback_tail: str) -> dict | None:
    """Returns parsed dict or None on any failure."""
    try:
        payload = generate_distill_payload(traceback_tail)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OLLAMA_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=DISTILLATION_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return json.loads(data["response"])
    except (asyncio.TimeoutError, json.JSONDecodeError, aiohttp.ClientError):
        return None
```

### VRAM eviction guard

```python
async def is_model_resident() -> bool:
    """Cheap pre-flight check before distillation request."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://localhost:11535/api/ps",
                timeout=aiohttp.ClientTimeout(total=1.0),
            ) as resp:
                data = await resp.json()
                resident = [m["name"] for m in data.get("models", [])]
                return "llama3.1:8b-instruct-q8_0" in resident
    except Exception:
        return False
```

If `is_model_resident()` returns False, skip distillation, write raw tail,
log warning. Distillation latency in spilled-VRAM state is 30-45s — refusing
to wait is the right call.

### Fallback behavior (Ollama down / model evicted / garbage JSON)

Write `00-urgent-alerts.md` with:

```markdown
> ⚠️ SYSTEM ALERT: Local error distiller unreachable. Raw traceback provided below.
### RAW TRACEBACK
[Last 50 lines verbatim]
```

cc still gets actionable info. Just no compressed summary.

---

## TEST PLAN (37 tests — unified count)

### Group 1: Watchdog & event filtering (8 tests)

1. `test_watchdog_ignores_pycache` — `.pyc` creation dropped
2. `test_watchdog_ignores_pytest_cache` — `.pytest_cache/v/cache/stepwise` mutations ignored
3. `test_watchdog_ignores_git_dir` — `.git/HEAD` changes don't trigger
4. `test_watchdog_ignores_atlas_dir` — `.atlas/01-brief.md` modifications ignored
5. `test_watchdog_ignores_swap_files` — `.auth.py.swp`, `auth.py~`, `auth.py.bak` all dropped
6. `test_watchdog_ignores_test_files` — `test_auth.py` modification does NOT trigger (unidirectional loop)
7. `test_debounce_collapses_rapid_saves` — 5 saves in 200ms → pytest runs exactly once
8. `test_debounce_timer_reset` — save at t=0, save at t=1400ms → execution delays to t=2900ms

### Group 2: Subprocess & concurrency (5 tests)

9. `test_process_spawn_success` — valid Popen on trigger
10. `test_mid_run_interruption` — 2nd save during pytest run → SIGTERM first process
11. `test_zombie_process_cleanup` — orphaned pytests killed on daemon shutdown
12. `test_pipe_reader_no_deadlock` — 5MB of garbage on stdout → deque truncates cleanly, no hang
13. `test_stderr_redirected_to_stdout` — tracebacks captured in deque

### Group 3: PID-Pinned lifecycle (4 tests — NEW in v2)

14. `test_daemon_acquires_pid_mutex` — fresh boot creates `.atlas/ghost_ci.pid`
15. `test_second_daemon_self_terminates` — second daemon in same dir exits with worktree error
16. `test_stale_mutex_reclaimed` — mutex with dead PID → reclaimed on retry
17. `test_pid_heartbeat_detects_parent_death` — mock parent PID disappear → daemon exits within 10s

### Group 4: State machine & file locking (4 tests)

18. `test_mkdir_lock_acquisition` — alert file written when lock free
19. `test_mkdir_lock_backoff` — existing lock → retries 3× with 50ms backoff
20. `test_lock_cleanup_on_exception` — write failure mid-process → lock removed
21. `test_state_transitions` — IDLE → DEBOUNCING → EXECUTING → DISTILLING → INJECTING → IDLE

### Group 5: Distillation pipeline (6 tests)

22. `test_ollama_payload_format` — JSON payload matches Llama 3 prompt spec exactly
23. `test_ollama_success_parsing` — valid Ollama JSON → markdown schema correctly formatted
24. `test_ollama_timeout_fallback` — `asyncio.TimeoutError` → raw tail written
25. `test_ollama_garbage_json_fallback` — Ollama returns conversational text → `JSONDecodeError` caught
26. `test_ollama_connection_refused` — port closed → graceful fallback
27. `test_vram_eviction_skips_distillation` — `/api/ps` returns no llama3.1 → skip Ollama, write raw

### Group 6: Markdown generation & schema (4 tests)

28. `test_success_state_markdown` — zero exit code → "ALL SYSTEMS NOMINAL"
29. `test_failure_state_markdown` — non-zero exit code → full 3-part alert schema
30. `test_tail_truncation_exact_50` — 200 lines in deque → exactly 50 in markdown
31. `test_markdown_escaping` — backticks in code don't break markdown block

### Group 7: Integration & edge cases (4 tests)

32. `test_syntax_error_bypass` — `SyntaxError` in pytest output → silently swallowed, no alert written
33. `test_empty_test_suite` — pytest exit code 5 (no tests collected) → treated as nominal
34. `test_infinite_loop_test` — `while True` in test → killed after hard 15s timeout
35. `test_tkk_phase1_coexistence` — Ghost CI alongside TKK Phase 1 hook → no collisions
36. `test_config_killswitch` — `ghost_ci_enabled: false` → daemon exits cleanly
37. `test_end_to_end_mocked` — full loop: synthetic file touch → mocked pytest → mocked Ollama → file verified

### Group 8: v3 reviewer-pool fixes (6 NEW tests)

38. `test_ollama_payload_escapes_percent_and_quotes` — mock traceback contains `ValueError: %d format` AND `"embedded quotes"` AND backslashes → `generate_distill_payload()` returns valid JSON, no `TypeError` raised (Fix 1)
39. `test_observer_kills_daemon_on_config_disable` — start daemon with mock config, update to `ghost_ci_enabled: false`, assert `SystemExit` raised within 2.5s (Fix 2)
40. `test_debounce_yields_multiple_independent_files` — trigger `file_A.py` at t=0, `file_B.py` at t=0.5 → check at t=1.6 yields A only, check at t=2.1 yields B only (Fix 3)
41. `test_create_no_window_preserves_stdout_pipe` — spawn subprocess using `CREATE_NO_WINDOW` that echoes a known string, assert `proc.stdout.read()` captures exact bytes (NOT None, NOT empty) (Fix 4)
42. `test_testsuite_fallback_filters_unrelated_tracebacks` — mock `auth.py` change, mock full-suite pytest fails in `test_database.py` with traceback entirely internal to `database.py` → assert Ghost CI discards error, writes ALL SYSTEMS NOMINAL (Fixes 9 + 12)
43. `test_alert_file_written_atomically` — write to target path via `tempfile.mkstemp` + `os.replace`, concurrent reader thread during write → reader gets either old content OR new content, never partial/empty 0-byte (Fix 11)

### Group 9: v4 R2-patch regression tests (4 NEW tests)

44. `test_daemon_path_resolves_absolute` — `spawn_ghost.py` builds `DAEMON_PATH` via `__file__` resolution, assert path is absolute AND exists AND points at `~/.tkk/ghost_ci/daemon.py` (R2 Fix #1)
45. `test_atlas_dir_autocreated_on_first_write` — point `write_alert_atomic` at a path inside non-existent `.atlas/` dir, assert call succeeds, assert `.atlas/` now exists, assert file written (R2 Fix #3)
46. `test_config_json_created_by_installer` — run `install_ghost_ci.ps1` step 5.5 in test sandbox, assert `~/.tkk/ghost_ci/config.json` exists AND parses as JSON AND has `ghost_ci_enabled: true` default (R2 Fix #2)
47. `test_testmon_warmup_invoked_on_boot` — boot daemon in temp project, assert `pytest --testmon --co` subprocess spawned, assert `.testmondata` written within 60s (R2 Fix #4)

### Group 10: v5 Gemini-reflection fixes (3 NEW tests)

48. `test_pid_traverses_to_node_exe` — mock process tree: python <- cmd.exe <- node.exe → `get_claude_node_pid()` returns node.exe's PID (NOT cmd.exe's). Fallback case: no node.exe in tree → returns `os.getppid()` (v5 Fix #1a)
49. `test_project_root_resolves_from_subdirectory` — launch spawn_ghost.py from `<repo>/src/hooks/`, where `<repo>/.git` exists → `get_project_root()` returns `<repo>`, NOT the subdirectory (v5 Fix #1b)
50. `test_pytest_uses_isolated_cache_dir` — run `execute_pytest` in temp project, assert `.ghost_pytest_cache/` created BUT `.pytest_cache/` NOT created. Assert `PYTHONDONTWRITEBYTECODE=1` in subprocess env. Then concurrently spawn a 2nd pytest with default args in same dir → assert NO WinError 32 (v5 Fix #2, 7th pre-mortem)

**Total: 50 tests** (47 from v4 + 3 new from v5 Gemini reflection).

---

## INSTALL & ROLLBACK

### `install_ghost_ci.ps1` (8-step procedure)

```powershell
# 1. Idempotency: ensure TKK Phase 1 exists
$TkkDir = "$env:USERPROFILE\.tkk"
if (-Not (Test-Path $TkkDir)) {
    Write-Error "TKK Phase 1 not found. Install TKK Phase 1 first."
    exit 1
}

# 1.5. PRE-FLIGHT: Verify Ollama on port 11535 (Fix 8 — non-default port)
Write-Host "Verifying local Ollama endpoint on port 11535..."
try {
    $response = Invoke-RestMethod -Uri "http://localhost:11535/api/tags" -Method Get -TimeoutSec 5 -ErrorAction Stop
    $hasModel = $response.models | Where-Object { $_.name -like "llama3.1:8b-instruct-q8_0*" }
    if (-not $hasModel) {
        Write-Error "PRE-FLIGHT FAILED: llama3.1:8b-instruct-q8_0 not pulled."
        Write-Error "Run: ollama pull llama3.1:8b-instruct-q8_0"
        exit 1
    }
    Write-Host "  Ollama OK on :11535, llama3.1:8b-instruct-q8_0 available."
} catch {
    Write-Error "PRE-FLIGHT FAILED: Ollama not accessible on http://localhost:11535/api/tags"
    Write-Error "Ensure Ollama is running with OLLAMA_HOST=0.0.0.0:11535 (non-default port)"
    Write-Error "Default Ollama port is 11434 — Victor's setup uses 11535"
    exit 1
}

# 2. PowerShell 7+ guard (learned from Phase 1)
if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Error "Ghost CI installer requires PowerShell 7+. Run via pwsh."
    exit 1
}

# 3. Install Python deps
python -m pip install --user watchdog==4.0.0 aiohttp==3.9.3 psutil==5.9.8 colorama==0.4.6 pytest-testmon==2.1.1

# 4. Verify Ollama model resident
$ollamaPs = Invoke-RestMethod -Uri "http://localhost:11535/api/ps" -ErrorAction SilentlyContinue
if ($null -eq $ollamaPs -or -not ($ollamaPs.models | Where-Object { $_.name -like "llama3.1:8b*" })) {
    Write-Warning "Llama 3.1 8B not currently loaded. Run: ollama pull llama3.1:8b-instruct-q8_0"
    Write-Warning "Then: ollama run llama3.1:8b-instruct-q8_0 (let it load, then ctrl+c)"
}

# 5. Deploy daemon
$GhostDir = "$TkkDir\ghost_ci"
New-Item -ItemType Directory -Force -Path $GhostDir | Out-Null
Copy-Item ".\ghost_ci\*" -Destination $GhostDir -Recurse -Force

# 5.5. Create config.json with default kill-switch state (R2 Fix — was missing entirely)
$ConfigPath = "$GhostDir\config.json"
if (-not (Test-Path $ConfigPath)) {
    @{
        ghost_ci_enabled = $true
        debounce_seconds = 1.5
        pytest_timeout_seconds = 15
        ollama_url = "http://localhost:11535"
        distillation_timeout_seconds = 3.0
    } | ConvertTo-Json | Set-Content -Path $ConfigPath -Encoding UTF8
    Write-Host "  Created default config at $ConfigPath"
}

# 5.7. Add Ghost CI cache paths to project .gitignore (v5 Fix #2 — WinError 32 prevention)
$ProjectGitignore = "$pwd\.gitignore"
$GhostIgnores = @(
    "# Ghost CI cache isolation (TKK Phase 2)",
    ".ghost_pytest_cache/",
    ".testmondata",
    ".atlas/00-urgent-alerts.md.lock/",
    ".atlas/ghost_ci.pid"
)
if (Test-Path $ProjectGitignore) {
    $existing = Get-Content $ProjectGitignore -Raw
    if ($existing -notmatch ".ghost_pytest_cache") {
        Add-Content -Path $ProjectGitignore -Value "`n$($GhostIgnores -join "`n")"
        Write-Host "  Added Ghost CI ignores to $ProjectGitignore"
    }
}

# 6. Register SessionStart hook (extends Phase 1 settings.json)
$SettingsPath = "$env:USERPROFILE\.claude\settings.json"
$BackupPath = "$SettingsPath.bak.ghost_ci.$(Get-Date -Format 'yyyyMMdd_HHmmss')"
Copy-Item $SettingsPath $BackupPath

$settings = Get-Content $SettingsPath -Raw | ConvertFrom-Json -AsHashtable
if (-not $settings.hooks.SessionStart) {
    $settings.hooks.SessionStart = @()
}
$existingGhost = $settings.hooks.SessionStart | Where-Object { $_.command -like "*spawn_ghost*" }
if ($null -eq $existingGhost) {
    $settings.hooks.SessionStart += @{
        type = "command"
        command = "python `"$GhostDir\spawn_ghost.py`""
        timeout = 5
    }
}
$settings | ConvertTo-Json -Depth 10 |
    Set-Content -Path "$SettingsPath.tmp" -Encoding UTF8 -NoNewline
Move-Item -Force "$SettingsPath.tmp" $SettingsPath

# 7. Inject CLAUDE.md directive (if .atlas exists for current project)
# Note: this is project-scoped. Run per-project where you want Ghost CI active.
$claudeMd = "$pwd\CLAUDE.md"
$directive = @"

# CRITICAL SYSTEM DIRECTIVE: PRE-EMPTIVE VALIDATION
Before invoking ANY tool, generating ANY code, or formulating ANY plan,
you MUST read `.atlas/00-urgent-alerts.md`.
If this file contains active failures, you are strictly forbidden from
working on new features. Your sole objective is to debug and clear the alerts.
"@
if ((Test-Path $claudeMd) -and -not ((Get-Content $claudeMd -Raw) -match "00-urgent-alerts")) {
    Add-Content -Path $claudeMd -Value $directive
}

# 8. Smoke test
Write-Host "Smoke testing daemon spawn..."
& python "$GhostDir\spawn_ghost.py" --test-mode
if ($LASTEXITCODE -ne 0) {
    Write-Error "Smoke test failed. Restore settings.json from $BackupPath"
    exit 1
}

Write-Host "Ghost CI installed. It will auto-launch on your next cc session."
```

### Rollback

`uninstall_ghost_ci.ps1`:
1. Restore `settings.json` from most recent `.bak.ghost_ci.*`
2. Remove `$env:USERPROFILE\.tkk\ghost_ci\`
3. Optionally remove CLAUDE.md directive (prompt user)
4. Kill any running daemon: `Stop-Process -Name python -Force -ErrorAction SilentlyContinue`

### Kill switch (no uninstall needed)

```powershell
'{"ghost_ci_enabled": false}' | Set-Content "$env:USERPROFILE\.tkk\ghost_ci\config.json"
```

Daemon polls config every 2s. Setting `ghost_ci_enabled: false` triggers
clean shutdown within that window.

---

## PERFORMANCE TARGETS

| Component | Target | Hard ceiling | Notes |
|---|---|---|---|
| Watchdog event detection | <50ms | 100ms | NTFS event queue dependent |
| Debouncer window | 1500ms | 2000ms | Configurable; tune via observed cc-edit cadence |
| pytest execution (unit) | <2500ms | 15000ms | Hard kill at 15s (infinite-loop protection) |
| 4070 Ti distillation | <1200ms | 3000ms | Q8 8B model: 60+ tok/s × 150 target = ~2.5s max |
| VRAM resident check | <100ms | 500ms | Single GET /api/ps |
| Disk I/O injection | <20ms | 100ms | NVMe SSD write |
| **Total turnaround** | **<5.2s** | **<20s** | File-save → alerts.md populated |

At 5.2s turnaround, cc cannot beat the validation loop. By the time cc
formulates its next thought and tool call, `.atlas/00-urgent-alerts.md` is
already up to date with reality.

---

## THE PRE-MORTEMS (architectural traps)

### Trap 1: SyntaxError Barrage

**Scenario:** cc Edit tool saves a file mid-stream. File momentarily missing
closing paren. Watchdog fires. pytest crashes with `SyntaxError: unexpected EOF`.

**The bug:** Ghost CI distills "missing parenthesis" → writes alert → cc reads
alert mid-thought → panics → tries to fix paren → collides with its own ongoing
edit. Schizophrenic agent loop.

**The fix:** If pytest exit code is 2 (usage error) AND traceback contains
`SyntaxError` OR `IndentationError`, **silently swallow** — do NOT write alert.
We surface only logic failures, not mid-keystroke syntax states.

### Trap 2: Ghost Test Disconnect (stale --lf target)

**Scenario:** cc edits `memory_ui_server.py`. Ghost CI runs `pytest --lf` which
last failed `test_auth.py`. test_auth.py fails again (unrelated to UI work).
Ghost CI tells cc "test_auth.py failed". cc reverts perfectly good UI code
trying to fix unrelated auth test.

**The fix:** Don't use `--lf` blindly. Map modified source to specific test
file via convention (`auth.py` → `tests/test_auth.py`). If no mapping exists,
run full suite BUT filter reported errors to only those where the stack trace
touches the recently-modified file. Prevents cc from "fixing" code it didn't
break.

### Trap 3: VRAM Eviction Race

**Scenario:** Background process (e.g., other CUDA work) evicts Llama 3.1
from 4070 Ti VRAM. Ghost CI fires distillation request. Ollama spills model
to system RAM. Request that usually takes 1.2s now takes 45s. Debouncer
backs up. cc makes 3 more tool calls while alert is wrong.

**The fix:** Pre-flight check via `/api/ps`. If model not resident, skip
distillation entirely, write raw tail directly, log visible warning:
`"VRAM EVICTION DETECTED. BYPASSING DISTILLATION."`

### Trap 7: WinError 32 Pytest Cache Collision (NEW in v5 — Gemini 7th pre-mortem)

**Scenario:** Ghost CI runs `pytest --testmon` in background after a file save. Simultaneously, cc — wanting to verify its own work — fires `pytest tests/test_parsers.py` via the Bash tool. Two pytest instances now concurrent on same codebase.

**The bug:** Windows enforces mandatory file locking (unlike Linux). Both pytests try to write `__pycache__/*.pyc`, both lock `.pytest_cache/v/cache/stepwise`, both mutate `.testmondata`. cc's Bash-spawned pytest crashes with:

```
PermissionError: [WinError 32] The process cannot access the file because
it is being used by another process: '.pytest_cache/v/cache/stepwise'
```

cc receives this traceback. **It does not know Ghost CI exists.** cc assumes its source change introduced a file-permission bug. It starts hallucinating fixes — adding `os.chmod` calls, implementing manual file locks, possibly rewriting `database/parsers.py` to "avoid the permission issue." The application logic is destroyed chasing a phantom environment error.

**The fix:**
1. **Disable bytecode writes** in Ghost CI's pytest: `PYTHONDONTWRITEBYTECODE=1` env var
2. **Disable plugin autoload** to skip SQLite plugin cache collisions: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`
3. **Use dedicated cache dir**: `-o cache_dir=.ghost_pytest_cache` (NEVER share with user's normal pytest)
4. **Add `.ghost_pytest_cache/` and `.testmondata` to project `.gitignore`** automatically via installer

Result: Ghost CI and user-invoked pytest live in parallel universes. Both can run concurrently on the same codebase. Zero collisions.

### Trap 4: Cross-Session Alert Hijack (NEW in v2)

**Scenario:** Two parallel cc sessions in same directory. Session A breaks
something. Alert file populated. Session B reads alert mid-thought, drops
its own task, pivots to "fix" Session A's bug. Session A also fixing same
file. Race-condition writes. Files corrupted.

**The fix:**
- **Documentation:** Parallel cc sessions MUST use `git worktree add` for
  physical isolation. Each worktree = own atlas dir = own alert file.
- **Enforcement:** Daemon mutex via `os.O_CREAT | os.O_EXCL`. Second daemon
  in same directory self-terminates with clear worktree error message.
  Fail-fast prevents corruption.

---

## DEFINITION OF DONE

- [ ] All 50 pytest tests pass (cycle 1 target, hard 5-cycle cap)
- [ ] Daemon launches via SessionStart hook on cc session start
- [ ] Daemon dies within 10s of cc process death (heartbeat verified)
- [ ] Daemon refuses to start if another instance holds mutex (worktree error)
- [ ] Mutex reclaimed automatically when previous owner crashed (dead PID)
- [ ] `SystemObserverThread` polls config every 2s for kill switch (NOT in event loop) — Fix 2
- [ ] `MultiFileDebouncer` handles 2+ concurrent files independently — Fix 3
- [ ] All subprocess spawns use `CREATE_NO_WINDOW`, NOT `DETACHED_PROCESS` — Fix 4
- [ ] No reference to `state.json` anywhere in code or docs — Fix 5
- [ ] No reference to `pytest --lf` anywhere — Fix 9
- [ ] `pytest-testmon` declared in requirements.txt AND installed by installer — Fix 12
- [ ] DISTILL_PROMPT uses f-string + json.dumps, NOT `%s` — Fix 1
- [ ] Watchdog ignores `.git`, `__pycache__`, `.pytest_cache`, `.tkk`, `.atlas`, `venv`
- [ ] Watchdog ignores swap files (`.swp`, `~`, `.bak`)
- [ ] Watchdog ignores test files (`test_*.py`, `*_test.py`) — unidirectional loop verified
- [ ] Debouncer collapses 5 saves in 200ms to 1 pytest execution per file
- [ ] Mid-test interruption sends SIGTERM, drains pipe, restarts cleanly
- [ ] Pipe reader handles 5MB of garbage output without deadlock
- [ ] SyntaxError bypassed silently (no alert written)
- [ ] VRAM-evicted Llama detected via `/api/ps`, distillation skipped
- [ ] Ollama timeout/garbage → raw tail fallback, no crash
- [ ] Alert file lockdir prevents read-collision with cc
- [ ] `.atlas/00-urgent-alerts.md` written atomically via `tempfile.mkstemp` + `os.replace` — Fix 11
- [ ] CLAUDE.md contains the "strictly forbidden" directive after install
- [ ] Kill switch (`ghost_ci_enabled: false`) → daemon exits within 2s
- [ ] Integration test confirms TKK Phase 1 hook + Ghost CI coexist
- [ ] settings.json backup created before merge (`.bak.ghost_ci.<timestamp>`)
- [ ] Installer pre-flights Ollama on port 11535 (Fix 8) — aborts cleanly if missing
- [ ] All paths consistently use uppercase `TKK` (Fix 7)
- [ ] `spawn_ghost.py` resolves `daemon.py` via absolute `__file__`-derived path — R2 Fix #1
- [ ] `spawn_ghost.py` passes `cwd=PROJECT_ROOT` AND `--project-root` arg to daemon — R2 Fix #1
- [ ] `spawn_ghost.py` ensures `.atlas/` exists before daemon launch — R2 Fix #3a
- [ ] `write_alert_atomic` calls `os.makedirs(directory, exist_ok=True)` defensively — R2 Fix #3b
- [ ] `install_ghost_ci.ps1` creates `~/.tkk/ghost_ci/config.json` with defaults (step 5.5) — R2 Fix #2
- [ ] `daemon.py` invokes `warm_up_testmon()` on boot (not just declared) — R2 Fix #4
- [ ] `spawn_ghost.py` uses `get_claude_node_pid()` traversal (NOT bare `os.getppid()`) — v5 Fix #1a
- [ ] `spawn_ghost.py` uses `get_project_root()` traversal (NOT bare `os.getcwd()`) — v5 Fix #1b
- [ ] `execute_pytest()` sets `PYTHONDONTWRITEBYTECODE=1` AND `cache_dir=.ghost_pytest_cache` — v5 Fix #2 (7th pre-mortem)
- [ ] Installer adds `.ghost_pytest_cache/` and `.testmondata` to project `.gitignore` — v5 Fix #2
- [ ] Repo pushed to github.com/innov8ideas4u-alt/TKK (extends Phase 1 repo)

---

## CHANGE LOG v4 → v5 (3 critical fixes from Gemini 2.5 Pro 2nd reflection)

**3 architectural traps caught by deep reflection** (not by reviewer pools — these require Windows-specific deep knowledge that LLM reviewers consistently miss):

1. **`os.getppid()` trap → `get_claude_node_pid()` process-tree traversal.** On Windows, cc fires hooks via cmd.exe wrappers. Immediate parent is cmd.exe which dies after 5s. Heartbeat would kill Ghost CI 5 seconds after every session start. Fix: walk process tree until `node.exe` found, fallback to `os.getppid()` for compiled-binary cc installs.

2. **`os.getcwd()` trap → `get_project_root()` filesystem traversal.** If cc launches from subdirectory, `.atlas/` lands in wrong place. Daemon monitors wrong tree. cc never sees alerts. Fix: walk up filesystem looking for `.git/` or `.atlas/`, fallback to cwd.

3. **WinError 32 Pytest Cache Collision (7th pre-mortem).** Ghost CI pytest + cc-Bash-tool pytest race on `__pycache__`, `.pytest_cache/v/cache/stepwise`, `.testmondata`. Windows file locking blows up cc's bash tool. cc hallucinates chmod fixes into source code. Fix: env vars (`PYTHONDONTWRITEBYTECODE=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`) + dedicated `cache_dir=.ghost_pytest_cache` + installer auto-adds to `.gitignore`.

**3 new tests added** (48-50) covering process-tree traversal, subdirectory launch, cache isolation under concurrent pytest.

**Total test count: 47 → 50.** Spec line count: ~1110 → ~1215.

**Gemini verdict on v5:** "SHIP V4 IMMEDIATELY. Fire abacusai. Any bugs remaining are strictly mechanical and Opus 4.7's 5-retry harness will catch them. You are no longer protecting the architecture; you are just delaying the build." — Locked v5 = ship.

---

## CHANGE LOG v3 → v4 (5 fixes from OpenRouter reviewer pool R2)

**1 critical bug (DeepSeek):**

1. **Daemon launch path broken** → `spawn_ghost.py` referenced `daemon.py` by relative path, but `daemon.py` lives in `~/.tkk/ghost_ci/` (not project root). Without fix, daemon never starts. Patched: `__file__`-derived absolute path, explicit `cwd=PROJECT_ROOT`, `--project-root` arg passed to daemon, `sys.executable` instead of bare `"python"`.

**4 mechanical drifts (consensus):**

2. **`config.json` never created by installer** → daemon polls a file that doesn't exist. Added step 5.5 to installer with default config (kill switch + tunable params).
3. **`.atlas/` directory not guaranteed to exist** → `write_alert_atomic` crashed on fresh projects. Added defensive `os.makedirs(directory, exist_ok=True)` in atomic writer AND in `spawn_ghost.py` pre-launch.
4. **`pytest-testmon` warm-up never invoked** → declared in requirements but daemon never built `.testmondata`. Added `warm_up_testmon()` function + `daemon.py main()` invokes it on boot.
5. **4 new tests added** (44-47) covering daemon path resolution, defensive .atlas/ creation, config.json install, testmon warm-up.

**Total test count: 43 → 47.** Spec line count: 957 → ~1110.

---

## CHANGE LOG v2 → v3 (12 fixes from OpenRouter reviewer pool R1 + Gemini patches)

**5 runtime-crash bugs (MiMo DO_NOT_SHIP findings):**

1. `DISTILL_PROMPT %s` → f-string + json.dumps (crashed on `%` in tracebacks)
2. Config-poll thread → merged into `SystemObserverThread` (was missing entirely)
3. Single-file debouncer → `MultiFileDebouncer` (concurrent files were silently dropped)
4. `DETACHED_PROCESS` → `CREATE_NO_WINDOW` (stdout pipe bound to None on Windows)
5. Phantom `state.json` → removed (was listed but never used)

**6 consensus issues (all 3 reviewers):**

6. Test count chaos → unified to 43 (was 35/37 split)
7. Path casing → uppercase `TKK` throughout (matches shipped GitHub repo)
8. Ollama port 11535 pre-flight → added to `install_ghost_ci.ps1`
9. Test selection `pytest --lf` → DELETED, replaced with traceback-touching filter
10. ".atlas auto-read" myth → documented honestly: CLAUDE.md directive IS the wiring
11. Atomic write → `tempfile.mkstemp` + `os.replace` implementation + new test

**Gemini's 5th pre-mortem (Import-Graph Cycle Blindspot):**

12. `pytest-testmon` added → dependency-graph aware test selection. Catches downstream cascade failures (cc edits shared utility, all dependent tests run automatically).

---

## CHANGE LOG v1 → v2 (8 Gemini-clarification patches)

1. **Alert path:** `[PROJECT_ROOT]/00-urgent-alerts.md` → `.atlas/00-urgent-alerts.md`
   (cognitive namespace isolation, matches existing atlas pattern)

2. **CLAUDE.md directive:** Updated to Gemini's "strictly forbidden" framing —
   hard constraint, not soft suggestion.

3. **Daemon launch:** Replaced `launch.ps1` manual model with PID-Pinned Epidemic
   Hook architecture. Daemon mortality slaved to cc parent PID. Zero orphans.

4. **Watchdog filter:** Replaced single cryptic regex with two-stage `is_valid_target()`
   using three readable boolean checks. Each check independently testable.

5. **Test file exclusion:** Explicit rejection of `test_*.py` and `*_test.py`.
   Documents unidirectional validation loop principle.

6. **Parallel execution:** New documentation requiring `git worktree add` for
   parallel cc sessions. Documented as architectural constraint.

7. **Daemon mutex:** Added `os.O_CREAT | os.O_EXCL` mutex on `.atlas/ghost_ci.pid`.
   Stale-lock reclamation for crashed-daemon recovery.

8. **Test count:** 30 → 37 tests. Added 4 PID-lifecycle tests (mutex acquire,
   second-daemon termination, stale reclaim, heartbeat detection), 1 VRAM
   eviction test, 1 swap-file rejection test, 1 test-file modification ignored.

---

## FIRE PROMPT (for abacusai Opus 4.7)

```
abacusai -p --dangerously-skip-permissions --model OPUS_4_7

Read D:\Dev\scratch\spec_GHOST_CI.md and execute it end-to-end.

This is the v2 spec for TKK Phase 2 (Ghost CI) — a Windows-native Python
daemon that pre-emptively runs tests on file save, compresses failures via
local 4070 Ti Ollama, and injects alerts into cc's context via
.atlas/00-urgent-alerts.md.

Phase 1 (TKK Read Guard) is already shipped — repo at github.com/innov8ideas4u-alt/TKK.
This builds Phase 2 in the same repo (extends Phase 1, doesn't replace).

The spec went through:
- Gemini 2.5 Pro initial design (v1)
- Gemini 2.5 Pro architectural follow-up (4 critical clarifications → v2)
- About to run OpenRouter reviewer pool against this spec

Your job: execute, not redesign. Architecture is locked.

CRITICAL CONTEXT:
- cc runs in Windows Terminal native (not WSL)
- Daemon must be SLAVED to parent PID via psutil heartbeat (no orphans)
- Watchdog filter MUST exclude test_*.py and *_test.py (unidirectional loop)
- Mutex on .atlas/ghost_ci.pid prevents Cross-Session Alert Hijack
- Llama 3.1 8B Q8 must be VRAM-resident on 4070 Ti before any distillation
- Existing TKK Phase 1 hook (matcher=Read) must continue working unchanged

EXECUTION PLAN (HARD STOPS BETWEEN PHASES):

PHASE 0 — Pre-flight (REPORT BACK BEFORE PROCEEDING):
  - Verify TKK Phase 1 installed (~/.tkk/ exists, hook in settings.json)
  - Verify Ollama running on port 11535
  - Verify llama3.1:8b-instruct-q8_0 model pulled (run `ollama list`)
  - Verify python 3.13+, pip available
  - Show current ~/.claude/settings.json hooks structure
  - Confirm D:\Dev\Projects\TKK\ghost_ci\ doesn't already exist
  - HALT before Phase 1

PHASE 1 — Build daemon + tests:
  - Create ghost_ci/ subdir in existing D:\Dev\Projects\TKK\
  - Write daemon.py, spawn_ghost.py, event_handler.py, pipe_reader.py,
    distiller.py, alerts.py, mutex.py per spec
  - Write all 50 pytest tests (37 from v2 + 6 v3 + 4 v4 + 3 v5)
  - Run pytest. HARD CAP: 5 fix-retry cycles. HALT if still failing.
  - Show pytest output, HALT before Phase 2

PHASE 2 — Build installer:
  - Write install/install_ghost_ci.ps1 (8-step per spec)
  - Write install/uninstall_ghost_ci.ps1
  - Update README.md (Phase 2 section)
  - Add docs/ghost_ci_design.md (copy of this spec)
  - HALT before live install

PHASE 3 — Live install + smoke test:
  - Run install_ghost_ci.ps1
  - Verify SessionStart hook added to settings.json (Phase 1 Read hook preserved)
  - Verify ~/.tkk/ghost_ci/ created
  - Verify .atlas/ghost_ci.pid NOT present (no daemon running yet)
  - Manual smoke test: open new cc session, verify daemon spawns + heartbeat works
  - HALT before commit

PHASE 4 — Commit + push to existing TKK repo:
  - Stage all Phase 2 files
  - Commit message: "feat: TKK Phase 2 Ghost CI - pre-emptive validation daemon"
  - Push to main of github.com/innov8ideas4u-alt/TKK
  - Report SHA + diff stats

CODING RULES:
- stdlib + the 4 declared dependencies only (watchdog, aiohttp, psutil, colorama)
- UTF-8 no BOM
- LF on .py, CRLF on .ps1
- Use the canonical git_push.ps1 pattern from D:\Dev\Projects\pgvector_load\
- Daemon must shut down cleanly on parent PID death within 10s
- Mutex must use atomic os.O_CREAT | os.O_EXCL (not msvcrt, not fcntl)
- Watchdog uses two-stage is_valid_target() filter, NOT lookbehind regex

Stop and ask before any commit. Plan everything in scratch first.
```

---

## NEXT STEPS

1. Save this v2 spec to disk ✓ (this file)
2. Fire OpenRouter reviewer pool against v2 (~$0.04, ~3 min, expect 1-2 rounds)
3. Patch v3 with reviewer findings
4. Fire abacusai Opus 4.7 with the fire prompt above
5. Phase 0-4 with HALT points (same flow as TKK Phase 1)
