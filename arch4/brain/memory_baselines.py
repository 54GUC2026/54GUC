"""
arch4/brain/memory_baselines.py
───────────────────────────────────────────────────────────────────────────────
Baseline statistics helpers for Memory.

Separated from memory.py so baseline serialization/deserialization can be
reused by namespace/persistence helpers without pulling in the full Memory
implementation.
───────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import time




class BaselineStats:
    """
    Rolling statistics for a signal condition bucket.
    Tracks what's "normal" so we can detect deviation.
    """

    def __init__(self):
        self.sample_count: int = 0
        self.avg_throughput_mbps: float = 0.0
        self.avg_mcs: float = 0.0
        self.avg_rtt_ms: float = 0.0
        self.avg_sinr: float = 0.0
        self.ca_active_ratio: float = 0.0
        self.last_updated: float = 0.0

    def update(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> None:
        """Update running averages with new observation."""
        self.sample_count += 1
        alpha = max(0.01, 1.0 / min(self.sample_count, 100))

        self.avg_throughput_mbps += alpha * (modem.traffic.dl_mbps - self.avg_throughput_mbps)
        self.avg_rtt_ms += alpha * (computed.latency.rtt_sliding_avg - self.avg_rtt_ms)
        self.avg_sinr += alpha * (modem.cellular.sinr_db - self.avg_sinr)
        ca_val = 1.0 if modem.cellular.ca_active else 0.0
        self.ca_active_ratio += alpha * (ca_val - self.ca_active_ratio)
        self.last_updated = time.time()

    def to_dict(self) -> dict:
        return {
            "sample_count": self.sample_count,
            "avg_throughput_mbps": self.avg_throughput_mbps,
            "avg_mcs": self.avg_mcs,
            "avg_rtt_ms": self.avg_rtt_ms,
            "avg_sinr": self.avg_sinr,
            "ca_active_ratio": self.ca_active_ratio,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BaselineStats":
        bs = cls()
        bs.sample_count = d.get("sample_count", 0)
        bs.avg_throughput_mbps = d.get("avg_throughput_mbps", 0)
        bs.avg_mcs = d.get("avg_mcs", 0)
        bs.avg_rtt_ms = d.get("avg_rtt_ms", 0)
        bs.avg_sinr = d.get("avg_sinr", 0)
        bs.ca_active_ratio = d.get("ca_active_ratio", 0)
        bs.last_updated = d.get("last_updated", 0)
        return bs
