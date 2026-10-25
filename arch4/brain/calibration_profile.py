"""
arch4/brain/calibration_profile.py

Shared calibration-profile types and policy constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from arch4.operator_profiles import (
    FAMILY_HUAWEI_HILINK,
    FAMILY_OPENWRT_MM,
    FAMILY_ROUTEROS_AT,
    FAMILY_ZTE_GOFORM,
    FAMILY_ZTE_ZWRT,
)

BACKGROUND_CANDIDATE_TRIGGER_HITS = 6
BOOTSTRAP_MIN_DURATION_S = 60.0
COMPARATIVE_MIN_DURATION_S = 30.0
BACKGROUND_MIN_DURATION_S = 120.0
MAX_EXTERNAL_KNOWLEDGE_FILE_BYTES = 262_144
RECENT_SESSION_HISTORY_LIMIT = 8

BACKGROUND_TRIGGER_FAMILY_RULES: dict[str, dict[str, float | int | str]] = {
    FAMILY_ROUTEROS_AT: {
        "trigger_hits": 5,
        "gap_margin": 0.08,
        "headline": "MikroTik background drift is strong enough to open a candidate",
    },
    FAMILY_HUAWEI_HILINK: {
        "trigger_hits": 6,
        "gap_margin": 0.10,
        "headline": "Huawei HiLink-like background drift is strong enough to open a candidate",
    },
    FAMILY_OPENWRT_MM: {
        "trigger_hits": 7,
        "gap_margin": 0.15,
        "headline": "OpenWrt background drift needs stronger confirmation before opening a candidate",
    },
    FAMILY_ZTE_GOFORM: {
        "trigger_hits": 8,
        "gap_margin": 0.18,
        "headline": "ZTE background drift needs stronger confirmation before opening a candidate",
    },
    FAMILY_ZTE_ZWRT: {
        "trigger_hits": 8,
        "gap_margin": 0.18,
        "headline": "ZTE background drift needs stronger confirmation before opening a candidate",
    },
    "default": {
        "trigger_hits": 7,
        "gap_margin": 0.12,
        "headline": "Background drift needs stronger confirmation before opening a candidate",
    },
}


@dataclass
class CalibrationProfile:
    profile_id: str
    version: int
    state: str
    values: Dict[str, Any]
    raw_samples: List[Dict[str, Any]]
    analysis_report: Dict[str, Any]
    created_ts: float
    updated_ts: float
    approved_by: str = ""
