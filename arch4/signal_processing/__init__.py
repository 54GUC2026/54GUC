"""
arch4/signal_processing/
═══════════════════════════════════════════════════════════════
Signal processor modules — one per raw modem signal family.

Each processor:
  - Takes one raw input (or small group of related inputs)
  - Owns ALL derived state for that signal
  - Produces typed outputs on ComputedSignals
  - Manages its own baseline, EMA, sliding window, ratchet
  - Exports / restores its own runtime state

No processor reads another processor's internal state.
The CongestionModule is now the canonical congestion entrypoint. It
currently wraps the older authority/combiner internals while we migrate
full ownership into one module.

Trust levels:
  - HIGH:   RTT, RSRQ, NR  (modem reports reliably at 2s polling)
  - GUI:    Scheduler       (CQI/MCS/QAM display only; no Brain learning)
═══════════════════════════════════════════════════════════════
"""

from arch4.signal_processing.rtt_processor import RTTProcessor
from arch4.signal_processing.dl_jitter_processor import DLJitterProcessor
from arch4.signal_processing.rsrq_processor import RSRQProcessor
from arch4.signal_processing.scheduler_processor import SchedulerProcessor
from arch4.signal_processing.nr_processor import NRProcessor
from arch4.signal_processing.ca_telemetry_processor import (
    CATelemetryProcessor,
    ca_demand_load_mbps,
)
from arch4.signal_processing.congestion_combiner import CongestionCombiner
from arch4.signal_processing.congestion_authority import CongestionAuthority
from arch4.signal_processing.congestion_module import CongestionModule

__all__ = [
    "RTTProcessor",
    "DLJitterProcessor",
    "RSRQProcessor",
    "SchedulerProcessor",
    "NRProcessor",
    "CATelemetryProcessor",
    "ca_demand_load_mbps",
    "CongestionCombiner",
    "CongestionAuthority",
    "CongestionModule",
]
