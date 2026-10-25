"""
arch4/telemetry/rollup.py

ARCH6v1.2 long-term telemetry rollups.
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import json
import time
from pathlib import Path
from typing import Optional, Dict, Any

from arch4.controller_posture import ControllerPosture


class HourlyRollup:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.path = self.output_dir / "rollup_hourly.jsonl"
        self._current_hour_key: Optional[str] = None
        self._agg: Dict[str, Any] = {}

    @staticmethod
    def _hour_key(ts: float) -> str:
        return time.strftime("%Y-%m-%dT%H:00:00", time.localtime(ts))

    def _start_hour(self, key: str) -> None:
        self._current_hour_key = key
        self._agg = {
            "hour": key,
            "samples": 0,
            "sum_dl_mbps": 0.0,
            "sum_ul_mbps": 0.0,
            "sum_rtt_ms": 0.0,
            "sum_cq_cell": 0.0,
            "action_count": 0,
            "states": {},
        }

    def _flush(self) -> None:
        if not self._agg:
            return
        s = int(self._agg["samples"] or 0)
        if s <= 0:
            return
        rec = dict(self._agg)
        rec["avg_dl_mbps"] = rec["sum_dl_mbps"] / s
        rec["avg_ul_mbps"] = rec["sum_ul_mbps"] / s
        rec["avg_rtt_ms"] = rec["sum_rtt_ms"] / s
        rec["avg_cq_cell"] = rec["sum_cq_cell"] / s
        rec["ts_flush"] = time.time()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=True) + "\n")

    def update(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        posture: ControllerPosture,
        *,
        action_present: bool,
    ) -> None:
        key = self._hour_key(modem.generation.sample_ts)
        if self._current_hour_key is None:
            self._start_hour(key)
        elif key != self._current_hour_key:
            self._flush()
            self._start_hour(key)

        a = self._agg
        a["samples"] += 1
        a["sum_dl_mbps"] += float(modem.traffic.dl_mbps)
        a["sum_ul_mbps"] += float(modem.traffic.ul_mbps)
        a["sum_rtt_ms"] += float(computed.latency.rtt_ms)
        cq_cell = float(getattr(computed.quality, 'cq_cell', 0.0) or 0.0)
        a["sum_cq_cell"] += cq_cell
        if action_present:
            a["action_count"] += 1
        st = posture.state.name
        a["states"][st] = int(a["states"].get(st, 0)) + 1

    def close(self) -> None:
        self._flush()
