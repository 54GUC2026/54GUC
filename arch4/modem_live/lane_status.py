"""Store status for the protected Modem Live lanes.

This module has no ``arch4.*`` imports. Acquisition adds register, maintenance,
and I/O details. This store tracks fast-lane samples and skipped maintenance.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def default_protected_live_lane_status() -> dict[str, Any]:
    return {
        "owner": "modem_live",
        "lane": "fast_modem_sample",
        "period_s": 0.0,
        "work_ms": 0.0,
        "last_start_ts": 0.0,
        "last_finish_ts": 0.0,
        "last_success_ts": 0.0,
        "last_missed_ts": 0.0,
        "sample_count": 0,
        "missed_count": 0,
        "maintenance_inline": False,
        "maintenance_skipped_count": 0,
        "last_maintenance_skip_reason": "",
        "source": "modem_live",
    }


class ProtectedLiveLaneStatus:
    def __init__(self, initial: Optional[Dict[str, Any]] = None) -> None:
        self.status = dict(initial or default_protected_live_lane_status())

    def current(self) -> dict[str, Any]:
        status = dict(self.status or {})
        if not status:
            status = default_protected_live_lane_status()
        return status

    def set(self, status: Optional[Dict[str, Any]]) -> None:
        self.status = dict(status or default_protected_live_lane_status())

    def mark_maintenance_skipped(
        self,
        reason: str,
        *,
        base_status: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        status = dict(base_status or self.current())
        status["maintenance_skipped_count"] = int(status.get("maintenance_skipped_count", 0) or 0) + 1
        status["last_maintenance_skip_reason"] = str(reason or "")
        status["source"] = "modem_live"
        self.status = status
        return dict(status)

    def record_sample(
        self,
        *,
        started_ts: float,
        finished_ts: float,
        success: bool,
    ) -> dict[str, Any]:
        previous = self.current()
        previous_start = float(previous.get("last_start_ts", 0.0) or 0.0)
        period_s = max(0.0, float(started_ts - previous_start)) if previous_start > 0.0 else 0.0
        work_ms = max(0.0, float(finished_ts - started_ts) * 1000.0)
        sample_count = int(previous.get("sample_count", 0) or 0) + 1
        missed_count = int(previous.get("missed_count", 0) or 0) + (0 if success else 1)
        status = {
            "owner": "modem_live",
            "lane": "fast_modem_sample",
            "period_s": round(period_s, 3),
            "work_ms": round(work_ms, 3),
            "last_start_ts": float(started_ts),
            "last_finish_ts": float(finished_ts),
            "last_success_ts": float(finished_ts if success else previous.get("last_success_ts", 0.0) or 0.0),
            "last_missed_ts": float(finished_ts if not success else previous.get("last_missed_ts", 0.0) or 0.0),
            "sample_count": sample_count,
            "missed_count": missed_count,
            "maintenance_inline": False,
            "maintenance_skipped_count": int(previous.get("maintenance_skipped_count", 0) or 0),
            "last_maintenance_skip_reason": str(previous.get("last_maintenance_skip_reason", "") or ""),
            "source": "modem_live",
        }
        self.status = status
        return dict(status)
