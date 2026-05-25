"""SessionStart hook entrypoint. Launches the daemon as a detached subprocess
slaved to cc's node.exe PID, anchored to the actual project root.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psutil

CREATE_NO_WINDOW = 0x08000000


def get_claude_node_pid() -> int:
    """Walk the process tree until node.exe is found.

    On Windows, cc fires hooks via cmd.exe wrappers. The immediate parent is
    cmd.exe, which dies seconds after the hook returns. Heartbeat would then
    kill Ghost CI within 5 seconds of every session start. Fallback to
    os.getppid() handles compiled-binary cc installs.
    """
    try:
        current = psutil.Process(os.getpid())
        while current.parent() is not None:
            parent = current.parent()
            try:
                if parent.name().lower() == "node.exe":
                    return parent.pid
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
            current = parent
    except Exception:
        pass
    return os.getppid()


def get_project_root() -> Path:
    """Resolve project root.

    Priority order:
    1. $CLAUDE_PROJECT_DIR if set and valid (cc-authoritative)
    2. Walk upward from cwd, preferring .git/ (real repo)
    3. Walk upward from cwd, accepting .atlas/ (atlas-only project)
    4. Fallback: cwd

    Two-pass walk ensures a real git repo further up the tree beats
    a closer orphan .atlas/ directory.
    """
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        env_path = Path(env_root).resolve()
        if env_path.exists() and env_path.is_dir():
            return env_path

    cur = Path(os.getcwd()).resolve()
    candidates = [cur] + list(cur.parents)

    # Pass 1: prefer .git/ (real repo wins over orphan .atlas/)
    for candidate in candidates:
        if (candidate / ".git").exists():
            return candidate

    # Pass 2: accept .atlas/ (atlas-only project, e.g. early-stage scratch)
    for candidate in candidates:
        if (candidate / ".atlas").exists():
            return candidate

    return cur


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    daemon_path = script_dir / "daemon.py"

    project_root = get_project_root()
    atlas_dir = project_root / ".atlas"
    atlas_dir.mkdir(exist_ok=True)

    target_pid = get_claude_node_pid()

    if "--test-mode" in sys.argv:
        # Smoke test: don't actually spawn. Just verify resolution succeeds.
        print(f"spawn_ghost test_mode OK: daemon={daemon_path} pid={target_pid} root={project_root}")
        return 0

    subprocess.Popen(
        [
            sys.executable,
            str(daemon_path),
            "--slave-to-pid", str(target_pid),
            "--project-root", str(project_root),
        ],
        creationflags=CREATE_NO_WINDOW,
        close_fds=True,
        cwd=str(project_root),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
