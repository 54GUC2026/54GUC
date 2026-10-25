"""Hard-failed tombstone for the retired synthetic scheduler lane.

ESCHEDULE and the canonical scheduler processor own scheduler derivation.
Upper layers must consume accepted-generation scheduler facts; one-shot
reconstruction from modem fields is prohibited.
"""

from __future__ import annotations

from typing import Any, Dict

from arch4.signal_processing.scheduler_processor import (
    PROVENANCE_CQI_REPORTED,
    PROVENANCE_DUAL_CQI,
    PROVENANCE_DUAL_MCS,
    PROVENANCE_MCS_REPORTED,
    PROVENANCE_UNAVAILABLE,
    TRUST_HIGH,
    TRUST_NONE,
    SchedulerLane as SyntheticSchedulerLane,
)

__all__ = [
    "SyntheticSchedulerLane",
    "PROVENANCE_CQI_REPORTED",
    "PROVENANCE_MCS_REPORTED",
    "PROVENANCE_DUAL_CQI",
    "PROVENANCE_DUAL_MCS",
    "PROVENANCE_UNAVAILABLE",
    "TRUST_HIGH",
    "TRUST_NONE",
    "SYNTHETIC_SCHEDULER_ALLOWED_CONSUMERS",
    "IS_SYNTHETIC_SCHEDULER_CONSUMER",
    "derive_synthetic_scheduler_lane",
    "export_synthetic_quality_fields",
]


SYNTHETIC_SCHEDULER_ALLOWED_CONSUMERS: tuple[dict[str, object], ...] = ()


def IS_SYNTHETIC_SCHEDULER_CONSUMER(module_dotted_path: str) -> bool:
    del module_dotted_path
    return False


def derive_synthetic_scheduler_lane(
    modem: Any,
    *,
    computed: Any = None,
    backend: str = "",
    vendor: Any = "",
    model: Any = "",
    firmware: Any = "",
) -> SyntheticSchedulerLane:
    del modem, computed, backend, vendor, model, firmware
    raise RuntimeError(
        "retired_synthetic_scheduler_lane:"
        "consume_accepted_generation_scheduler_facts"
    )


def export_synthetic_quality_fields(
    modem: Any,
    *,
    computed: Any = None,
    backend: str = "",
    vendor: Any = "",
    model: Any = "",
    firmware: Any = "",
) -> Dict[str, Any]:
    del modem, computed, backend, vendor, model, firmware
    raise RuntimeError(
        "retired_synthetic_scheduler_export:"
        "consume_accepted_generation_scheduler_facts"
    )
