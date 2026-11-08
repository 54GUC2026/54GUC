"""Record Modem Live backend faults.

This module has no ``arch4.*`` imports. The I/O gate records suppressed backend
exceptions here, and acquisition publishes the resulting health state.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional


class BackendFaultStore:
    def __init__(self, initial: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.counts: dict[str, dict[str, Any]] = {
            str(key): dict(value or {})
            for key, value in dict(initial or {}).items()
        }

    def record(self, stage: str, exc: Exception) -> None:
        key = str(stage or "unknown")
        current = dict(self.counts.get(key) or {})
        current["count"] = int(current.get("count", 0) or 0) + 1
        current["last_ts"] = time.time()
        current["last_error_type"] = type(exc).__name__
        current["last_error"] = str(exc)
        self.counts[key] = current

    def health(self) -> dict[str, Any]:
        stages = {key: dict(value) for key, value in self.counts.items()}
        total = sum(int(value.get("count", 0) or 0) for value in stages.values())
        return {
            "fault_count": int(total),
            "stage_count": len(stages),
            "degraded": bool(total > 0),
            "stages": stages,
        }
