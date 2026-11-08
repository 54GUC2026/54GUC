"""Generic learned-baseline mutation owner for Memory."""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import logging
import time
from typing import Any, Dict, Optional

from arch4.brain.memory_baselines import BaselineStats
from arch4.brain.memory_learning_gate_journal_owner import (
    store_learning_gate_allowed as memory_store_learning_gate_allowed,
)


OWNER = "arch4.brain.memory_baseline_owner"
log = logging.getLogger("arch4.brain.memory_baseline_owner")

_UNSAFE_BASELINE_STATES = frozenset(
    {
        "BAD_RF",
        "E_RADIO_DOWN",
        "E_SESSION_RESET",
        "E_CONGESTION_STORM",
    }
)


def update_baseline(
    memory: Any,
    modem: CellularIntelligenceGeneration,
    computed: DerivedSignalFrames,
    *,
    state_name: str,
    learning_gate: Dict[str, Any],
) -> None:
    """Update Memory's generic learned-performance baseline bucket."""
    if str(state_name or "") in _UNSAFE_BASELINE_STATES:
        return
    if not memory_store_learning_gate_allowed(
        memory,
        learning_gate,
        surface="baseline_memory",
        context={
            "state": str(state_name or ""),
            "cell_id": int(getattr(modem.cellular, 'cell_id', 0) or 0),
            "band": int(getattr(modem.cellular, 'primary_band', 0) or 0),
        },
    ):
        return
    bucket = signal_bucket(modem, computed)
    if bucket not in memory.baselines:
        memory.baselines[bucket] = BaselineStats()
    baseline = memory.baselines[bucket]
    baseline.update(modem, computed)
    memory._mark_dirty()


def get_baseline(
    memory: Any,
    modem: CellularIntelligenceGeneration,
    computed: Optional[DerivedSignalFrames] = None,
) -> Optional[BaselineStats]:
    return memory.baselines.get(signal_bucket(modem, computed))


def is_below_baseline(memory: Any, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> bool:
    baseline = get_baseline(memory, modem, computed)
    if baseline is None or baseline.sample_count < 30:
        return False
    if not bool(getattr(modem.traffic, 'has_traffic', False)):
        return False
    throughput = float(getattr(modem.traffic, 'dl_mbps', 0.0) or 0.0)
    expected = float(getattr(baseline, "avg_throughput_mbps", 0.0) or 0.0)
    return throughput < expected * 0.60


def seed_baselines_from_historical(memory: Any, historical_hours: Dict) -> None:
    seeded = 0
    for _hour, hour_profile in dict(historical_hours or {}).items():
        samples = int(hour_profile.get("samples", 0) or 0)
        if samples < 10:
            continue
        avg_sinr = float(hour_profile.get("avg_sinr", 10.0) or 10.0)
        sinr_bucket = int(avg_sinr / 5) * 5
        ca_ratio = float(hour_profile.get("ca_active_ratio", 0.8) or 0.0)
        ca = "ca" if ca_ratio > 0.5 else "noca"
        bucket = f"sinr{sinr_bucket}_{ca}_b3"
        if bucket in memory.baselines:
            continue
        baseline = BaselineStats()
        baseline.sample_count = min(samples, 50)
        baseline.avg_throughput_mbps = float(hour_profile.get("avg_throughput_mbps", 0.0) or 0.0)
        baseline.avg_mcs = float(hour_profile.get("avg_mcs", 0.0) or 0.0)
        baseline.avg_sinr = avg_sinr
        baseline.ca_active_ratio = ca_ratio
        baseline.last_updated = time.time()
        memory.baselines[bucket] = baseline
        seeded += 1
    if seeded:
        log.info("Seeded %d baseline buckets from historical priors", seeded)


def signal_bucket(modem: CellularIntelligenceGeneration, computed: Optional[DerivedSignalFrames] = None) -> str:
    sinr_bucket = int(float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0) / 5) * 5
    ca = "ca" if bool(getattr(modem.cellular, 'ca_active', False)) else "noca"
    nsa = bool(modem.cellular.nsa_active)
    nr = "_nr" if nsa else ""
    return f"sinr{sinr_bucket}_{ca}_b{getattr(modem.cellular, 'primary_band', 0)}{nr}"


__all__ = [
    "OWNER",
    "get_baseline",
    "is_below_baseline",
    "seed_baselines_from_historical",
    "signal_bucket",
    "update_baseline",
]
