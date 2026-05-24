"""Ghost CI daemon main entry.

Responsibilities:
- Acquire .atlas/ghost_ci.pid mutex (or self-terminate)
- Warm up pytest-testmon dependency matrix
- Spawn SystemObserverThread (heartbeat + kill-switch)
- Run watchdog observer + per-file debouncer worker loop
- On any file save: pytest --testmon -> tail -> distill -> alert write
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
from watchdog.observers import Observer

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from alerts import (  # noqa: E402
    ALERT_FILENAME,
    render_failure,
    render_fallback,
    render_nominal,
    write_with_lock,
)
from distiller import (  # noqa: E402
    distill_error,
    filter_relevant_tracebacks,
    is_model_resident,
    is_syntax_error_output,
)
from event_handler import GhostCIEventHandler, MultiFileDebouncer  # noqa: E402
from mutex import LOCK_FILENAME, acquire_daemon_mutex, release_daemon_mutex  # noqa: E402
from pipe_reader import GhostPipeReader  # noqa: E402
from telemetry import build_record, log_telemetry  # noqa: E402

CREATE_NO_WINDOW = 0x08000000
PYTEST_HARD_TIMEOUT_S = 15.0
WORKER_TICK_S = 0.1
CONFIG_FILENAME = "config.json"
DEFAULT_CONFIG_DIR = Path.home() / ".tkk" / "ghost_ci"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--slave-to-pid", type=int, required=True)
    p.add_argument("--project-root", type=str, required=True)
    p.add_argument("--config", type=str, default=str(DEFAULT_CONFIG_DIR / CONFIG_FILENAME))
    return p.parse_args()


def warm_up_testmon(project_root: str) -> bool:
    """Build .testmondata on first boot via a detached --co subprocess.

    Returns False if pytest unavailable, True if warm-up launched (or already warm).
    """
    testmondata = Path(project_root) / ".testmondata"
    if testmondata.exists():
        return True
    try:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        subprocess.Popen(
            ["pytest", "-q", "--testmon", "--co",
             "-o", "cache_dir=.ghost_pytest_cache"],
            cwd=project_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            env=env,
        )
        return True
    except FileNotFoundError:
        return False


def execute_pytest(target_args: list[str], project_root: str) -> subprocess.Popen:
    """Run pytest with full cache isolation (v5 7th pre-mortem fix)."""
    ghost_env = os.environ.copy()
    ghost_env["PYTHONDONTWRITEBYTECODE"] = "1"
    ghost_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.Popen(
        ["pytest", *target_args, "-o", "cache_dir=.ghost_pytest_cache"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW,
        text=False,
        cwd=project_root,
        env=ghost_env,
    )


def interrupt_running_pytest(proc: subprocess.Popen) -> None:
    """SIGTERM the process tree, escalate to kill after 2s."""
    try:
        parent = psutil.Process(proc.pid)
    except psutil.NoSuchProcess:
        return
    for child in parent.children(recursive=True):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    try:
        parent.terminate()
        parent.wait(timeout=2.0)
    except psutil.TimeoutExpired:
        try:
            parent.kill()
        except psutil.NoSuchProcess:
            pass
    except psutil.NoSuchProcess:
        pass


_pytest_inflight: list[subprocess.Popen] = []


def kill_pytest_subprocesses() -> None:
    for p in list(_pytest_inflight):
        interrupt_running_pytest(p)
    _pytest_inflight.clear()


def cleanup_locks(project_root: str) -> None:
    atlas = Path(project_root) / ".atlas"
    release_daemon_mutex(str(atlas / LOCK_FILENAME))
    lockdir = atlas / (ALERT_FILENAME + ".lock")
    if lockdir.exists():
        try:
            os.rmdir(lockdir)
        except OSError:
            pass


class SystemObserverThread(threading.Thread):
    """Heartbeat + kill-switch in one low-frequency thread. Runs at 2s cadence."""

    def __init__(self, parent_pid: int, config_path: str, project_root: str,
                 poll_seconds: float = 2.0):
        super().__init__(daemon=True)
        self.parent_pid = parent_pid
        self.config_path = config_path
        self.project_root = project_root
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            if not psutil.pid_exists(self.parent_pid):
                self._shutdown()
                return
            try:
                import json as _json
                with open(self.config_path, "r", encoding="utf-8") as f:
                    cfg = _json.load(f)
                if not cfg.get("ghost_ci_enabled", True):
                    self._shutdown()
                    return
            except (FileNotFoundError, ValueError):
                pass
            self.stop_event.wait(self.poll_seconds)

    def _shutdown(self) -> None:
        cleanup_locks(self.project_root)
        kill_pytest_subprocesses()
        os._exit(0)


async def _process_file(modified_file: str, project_root: str, alert_path: str) -> None:
    """Run pytest, collect tail, distill, write alert. Single-exit + telemetry."""
    start_time = time.time()
    selection_mode = "testmon"
    interrupted = False
    distillation_attempted = False
    distillation_succeeded: bool | None = None
    distillation_tokens_out: int | None = None
    ollama_elapsed_ms: int | None = None
    model_resident_result: bool | None = None
    alert_written = False
    tail_text = ""
    exit_code = -1
    try:
        proc = execute_pytest(["-q", "--testmon"], project_root)
        _pytest_inflight.append(proc)
        reader = GhostPipeReader(proc)
        try:
            try:
                proc.wait(timeout=PYTEST_HARD_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                interrupt_running_pytest(proc)
                interrupted = True
            reader.join(timeout=1.0)
        finally:
            if proc in _pytest_inflight:
                _pytest_inflight.remove(proc)

        exit_code = proc.returncode if proc.returncode is not None else -1
        tail_lines = reader.tail(50)
        tail_text = "\n".join(tail_lines)

        # Empty suite (exit 5) = nominal
        if exit_code == 0 or exit_code == 5:
            if write_with_lock(alert_path, render_nominal()):
                alert_written = True
            return

        # SyntaxError barrage trap — silently swallow
        if is_syntax_error_output(tail_text, exit_code):
            return

        # Filter unrelated tracebacks
        relevant = filter_relevant_tracebacks(tail_text, modified_file)
        if relevant is None:
            if write_with_lock(alert_path, render_nominal()):
                alert_written = True
            return

        # Distill via 4070 Ti Ollama, fallback on eviction/failure
        model_resident_result = await is_model_resident()
        if not model_resident_result:
            if write_with_lock(alert_path, render_fallback(tail_text)):
                alert_written = True
            return
        distillation_attempted = True
        _ollama_start = time.time()
        distilled = await distill_error(tail_text)
        ollama_elapsed_ms = int((time.time() - _ollama_start) * 1000)
        if distilled is None:
            distillation_succeeded = False
            if write_with_lock(alert_path, render_fallback(tail_text)):
                alert_written = True
            return
        distillation_succeeded = True
        try:
            distillation_tokens_out = len(json.dumps(distilled))
        except Exception:
            distillation_tokens_out = None
        if write_with_lock(alert_path, render_failure(
            summary=str(distilled.get("summary", "")),
            failing_file=str(distilled.get("file", modified_file)),
            line_number=distilled.get("line_number"),
            exception_type=str(distilled.get("exception_type", "Unknown")),
            raw_tail=tail_text,
        )):
            alert_written = True
    finally:
        duration_ms = int((time.time() - start_time) * 1000)
        log_telemetry(project_root, build_record(
            trigger_file=modified_file,
            test_target="--testmon",
            selection_mode=(selection_mode + "_interrupted") if interrupted else selection_mode,
            exit_code=(-15 if interrupted else exit_code),
            pytest_output_bytes=len(tail_text.encode("utf-8")),
            pytest_duration_ms=duration_ms,
            distillation_attempted=distillation_attempted,
            distillation_succeeded=distillation_succeeded,
            distillation_tokens_out=distillation_tokens_out,
            ollama_latency_ms=ollama_elapsed_ms,
            model_resident=model_resident_result,
            alert_written=alert_written,
        ))


def main() -> int:
    args = parse_args()
    project_root = args.project_root
    atlas_dir = Path(project_root) / ".atlas"
    atlas_dir.mkdir(exist_ok=True)

    lock_path = str(atlas_dir / LOCK_FILENAME)
    acquire_daemon_mutex(lock_path)

    _warm_start = time.time()
    warm_up_testmon(project_root)
    log_telemetry(project_root, build_record(
        trigger_file=None,
        test_target=None,
        selection_mode="warm_up",
        exit_code=0,
        pytest_output_bytes=0,
        pytest_duration_ms=int((time.time() - _warm_start) * 1000),
    ))

    observer_thread = SystemObserverThread(
        parent_pid=args.slave_to_pid,
        config_path=args.config,
        project_root=project_root,
    )
    observer_thread.start()

    debouncer = MultiFileDebouncer()
    handler = GhostCIEventHandler(debouncer)
    observer = Observer()
    observer.schedule(handler, project_root, recursive=True)
    observer.start()

    alert_path = str(atlas_dir / ALERT_FILENAME)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        while True:
            for ready_file in debouncer.get_ready_files():
                try:
                    loop.run_until_complete(
                        _process_file(ready_file, project_root, alert_path))
                except Exception:
                    pass
            time.sleep(WORKER_TICK_S)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join(timeout=2.0)
        cleanup_locks(project_root)
        kill_pytest_subprocesses()
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
