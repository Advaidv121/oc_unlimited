"""Restarts the keeper and guard when they die or stop reporting.

Runs from a one-minute scheduled task. The worker tasks were registered by an
earlier install and can only be edited with elevation, so recovery lives here:
this process reads the same mutex and heartbeat signals the dashboard shows and
uses the existing tasks to bring a worker back.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ouc_common import (
    GUARD_LOG_FILE,
    KEEPER_LOG_FILE,
    LANE_DIR,
    TASK_GUARD,
    TASK_KEEPER,
    append_log,
    ensure_dirs,
    is_mutex_held,
    run_hidden,
    task_enabled,
)

WATCHDOG_LOG_FILE = LANE_DIR / "watchdog.log"
# Same ceiling the dashboard uses: the guard writes every 10s and the keeper
# every 45s, so a few quiet minutes means the process is wedged, not busy.
STALE_SECONDS = 240.0

WORKERS = (
    {
        "name": "keeper",
        "script": "warp_keeper.py",
        "mutex": "OpencodeUnlimitedKeeper",
        "task": TASK_KEEPER,
        "heartbeat": KEEPER_LOG_FILE.parent / "heartbeat.txt",
    },
    {
        "name": "guard",
        "script": "zen_guard.py",
        "mutex": "OpencodeUnlimitedGuard",
        "task": TASK_GUARD,
        "heartbeat": GUARD_LOG_FILE.parent / "heartbeat.txt",
    },
)


def _heartbeat_age(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _start_task(task: str) -> bool:
    try:
        res = run_hidden(["schtasks", "/Run", "/TN", task], timeout=15.0)
        return res.returncode == 0
    except Exception:
        return False


def _kill_script(script: str) -> int:
    """Terminate python processes running this worker script."""
    try:
        import psutil
    except Exception:
        return 0
    killed = 0
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.pid == 0:
                continue
            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if name in ("python.exe", "pythonw.exe") and script in cmdline:
                proc.kill()
                killed += 1
        except Exception:
            continue
    return killed


def check(worker: dict) -> str:
    """Bring one worker back if needed; returns an action name, or ''."""
    if not task_enabled(worker["task"]):
        return ""
    alive = is_mutex_held(worker["mutex"])
    age = _heartbeat_age(worker["heartbeat"])
    if alive and (age is None or age <= STALE_SECONDS):
        return ""
    if alive:
        # The mutex is still held, so the task would refuse to start another
        # copy. Only a wedged process gets killed; a fresh start writes its
        # first heartbeat within seconds.
        killed = _kill_script(worker["script"])
        time.sleep(1.0)
        ok = _start_task(worker["task"])
        return f"restarted {worker['name']} after {age:.0f}s without a heartbeat (killed {killed}): {'ok' if ok else 'failed'}"
    ok = _start_task(worker["task"])
    return f"started {worker['name']} (not running): {'ok' if ok else 'failed'}"


def main() -> int:
    ensure_dirs()
    for worker in WORKERS:
        try:
            action = check(worker)
        except Exception as exc:
            append_log(WATCHDOG_LOG_FILE, f"{worker['name']} check failed: {exc}")
            continue
        if action:
            append_log(WATCHDOG_LOG_FILE, action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
