"""Typed advisory projections from the canonical external-memory prior pack.

This boundary never restores previous owner files into the active runtime.  It
translates already promoted, exact-modem historical facts into the small input
shape owned by each consumer.
"""

from __future__ import annotations

from typing import Any, Mapping


SCHEMA = "external_memory_consumer_projection.v1"
OWNER = "arch4.external_memory_consumer_projection"


def calibration_knowledge_from_learning_prior(
    learning_prior: Mapping[str, Any],
    *,
    target_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Build CalibrationAuthority's existing low-confidence advisory shape."""

    prior = dict(learning_prior or {})
    target = dict(target_identity or {})
    if (
        prior.get("schema") != "external_memory_learning_prior.v1"
        or prior.get("owner") != "arch4.external_memory_learning_projection"
        or prior.get("present") is not True
    ):
        return {}
    bands = sorted(
        {
            _positive_int(str(key).split(":", 1)[0])
            for key in dict(prior.get("band_hour_quality") or {})
        }
        - {0}
    )
    sources = [
        dict(row)
        for row in prior.get("sources", [])
        if isinstance(row, Mapping)
    ]
    age_days = max(
        (float(row.get("age_days", 0.0) or 0.0) for row in sources),
        default=0.0,
    )
    confidence = min(
        0.35,
        max(0.05, float(prior.get("profile_confidence", 0.0) or 0.0) * 0.35),
    )
    source_name = "historical_continuity_import"
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "advisory_only": True,
        "live_truth_override": False,
        "provider_hints": {
            "provider_name": str(target.get("provider_name") or ""),
            "provider_plmn": str(target.get("provider_plmn") or ""),
            "coverage_note": "same-modem promoted historical continuity",
            "confidence": confidence,
            "freshness_hours": age_days * 24.0,
            "source": source_name,
            "bands": bands,
        },
        "device_hints": {
            "model": str(target.get("model") or ""),
            "firmware": str(target.get("firmware") or ""),
            "confidence": confidence,
            "freshness_hours": age_days * 24.0,
            "source": source_name,
            "note": "translated from exact-identity historical prior",
            "known_limits": [],
        },
        "site_hints": [
            {
                "cell_id": "",
                "bands": bands,
                "bandwidth_mhz": 0.0,
                "confidence": confidence,
                "freshness_hours": age_days * 24.0,
                "source": source_name,
                "note": "advisory band availability; live calibration remains authority",
            }
        ]
        if bands
        else [],
    }


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


__all__ = [
    "OWNER",
    "SCHEMA",
    "calibration_knowledge_from_learning_prior",
]
