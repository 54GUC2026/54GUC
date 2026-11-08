"""
External ops monitor adapters.

Collects lightweight process/heartbeat signals from runtime guardian / night-watch
lanes so runtime status can include layered monitor inputs without introducing
actuation side effects.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, Any


def _age_s(path: Path) -> float:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except Exception:
        return -1.0


def collect_ops_monitor_status(root_dir: str = ".") -> Dict[str, Any]:
    root = Path(root_dir)
    guardian_hb = root / ".guardian_state" / "guardian.heartbeat"
    night_watch_pid = root / ".night_watch_state" / "night_watch.pid"

    guardian_age = _age_s(guardian_hb)
    guardian_running = guardian_age >= 0.0 and guardian_age <= 180.0

    night_watch_running = False
    pid_val = ""
    try:
        pid_val = night_watch_pid.read_text(encoding="utf-8").strip()
        if pid_val.isdigit():
            p = Path("/proc") / pid_val
            night_watch_running = p.exists()
    except Exception:
        night_watch_running = False

    return {
        "guardian": {
            "heartbeat_age_s": guardian_age,
            "running": guardian_running,
        },
        "night_watch": {
            "running": bool(night_watch_running),
            "pid": pid_val,
        },
    }
