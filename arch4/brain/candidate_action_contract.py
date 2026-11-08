"""Canonical Brain candidate action shape contract.

This module does not choose or execute actions. It only answers whether a
candidate row has a direct action shape that TSAR can materialize, or whether it
is advisory/blocked evidence for another owner to transform first.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping

DIRECT_EXECUTABLE_ACTIONS = frozenset(
    {
        "apply_radio_configuration",
        "prefer_band",
        "stabilise_anchor_band",
        "stabilise_ca_pool",
        "reduce_ca_pool",
    }
)

ADVISORY_ACTIONS = frozenset(
    {
        "avoid_anchor_band",
        "avoid_band",
        "learned_hint",
        "neutral_band",
        "ranked_band",
        "recover_anchor_flip",
        "seed_ltm_prediction",
        "stay_current",
    }
)

ADVISORY_BLOCK_REASONS = frozenset(
    {
        "advisory_only",
        "direct_action_not_allowed",
        "learning_advisory_only",
        "learning_provider_time_context_not_ready",
        "not_published",
        "outside_window",
        "provider_time_context_missing",
    }
)

EXPLANATION_SCHEMA = "brain_candidate_explanation.v1"
EXPLANATION_OWNER = "arch4.brain.candidate_action_contract"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _candidate_sources(candidate: Mapping[str, Any]) -> list[str]:
    raw = candidate.get("sources")
    if isinstance(raw, str):
        values: Iterable[Any] = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = []
    out: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in out:
            out.append(text)
    source = _text(candidate.get("source"))
    if source and source not in out:
        out.append(source)
    return out


def _recommendation_text(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("action", "kind", "name", "recommendation"):
            text = _text(value.get(key))
            if text:
                return text
        band = _int(value.get("band"))
        if band > 0:
            return f"B{band}"
        return ""
    return _text(value)


def _stable_reason_ref(*, source: str, action: str, bands: Iterable[int], reason_code: str, reason: str) -> str:
    band_text = ",".join(str(int(band)) for band in bands)
    raw = "|".join((source, action, band_text, reason_code, reason))
    return "0x" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def candidate_action(candidate: Mapping[str, Any]) -> str:
    """Return the normalized candidate action/lever name."""

    return _lower(candidate.get("action") or candidate.get("lever"))


def candidate_target_bands(candidate: Mapping[str, Any]) -> list[int]:
    """Return positive integer target bands from the canonical target fields."""

    if "target_bands" in candidate and candidate.get("target_bands") is not None:
        raw = candidate.get("target_bands")
    elif "resolved_target_bands" in candidate and candidate.get("resolved_target_bands") is not None:
        raw = candidate.get("resolved_target_bands")
    elif "bands" in candidate and candidate.get("bands") is not None:
        raw = candidate.get("bands")
    else:
        raw = [candidate.get("band")]
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    out: list[int] = []
    for value in raw:
        band = _int(value)
        if band > 0 and band not in out:
            out.append(band)
    out.sort()
    return out


def candidate_declared_block_reasons(candidate: Mapping[str, Any]) -> list[str]:
    """Return explicit blocker reasons already carried by the candidate."""

    reasons: list[str] = []
    raw = candidate.get("block_reasons")
    if isinstance(raw, str):
        values: Iterable[Any] = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = []
    for value in values:
        text = _text(value)
        if text and text not in reasons:
            reasons.append(text)
    for key in ("direct_action_block_reason", "block_reason", "gate"):
        text = _text(candidate.get(key))
        if text and text not in reasons:
            reasons.append(text)
    if candidate.get("direct_action_allowed") is False and not reasons:
        reasons.append("direct_action_not_allowed")
    return reasons


def candidate_execution_contract(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Classify a candidate row under the single TSAR action-shape contract."""

    row = _mapping(candidate)
    action = candidate_action(row)
    bands = candidate_target_bands(row)
    declared_blockers = candidate_declared_block_reasons(row)
    reason = _text(row.get("reason"))

    if declared_blockers:
        reason = declared_blockers[0]
        candidate_class = "advisory" if reason in ADVISORY_BLOCK_REASONS or action in ADVISORY_ACTIONS else "blocked"
        return {
            "direct_action_allowed": False,
            "direct_action_block_reason": reason,
            "candidate_class": candidate_class,
            "target_bands": bands,
        }

    if not reason:
        return {
            "direct_action_allowed": False,
            "direct_action_block_reason": "missing_candidate_reason",
            "candidate_class": "blocked",
            "target_bands": bands,
        }

    if action not in DIRECT_EXECUTABLE_ACTIONS:
        reason = f"non_direct_action:{action or 'unknown'}"
        sources = {_lower(source) for source in _candidate_sources(row)}
        candidate_class = (
            "advisory"
            if action in ADVISORY_ACTIONS
            or "learning_vector" in sources
            or any(source.startswith(("learning_", "crystal_ball")) for source in sources)
            else "blocked"
        )
        return {
            "direct_action_allowed": False,
            "direct_action_block_reason": reason,
            "candidate_class": candidate_class,
            "target_bands": bands,
        }

    if not bands:
        return {
            "direct_action_allowed": False,
            "direct_action_block_reason": "missing_target_band",
            "candidate_class": "blocked",
            "target_bands": [],
        }

    return {
        "direct_action_allowed": True,
        "direct_action_block_reason": "",
        "candidate_class": "operational",
        "target_bands": bands,
    }


def candidate_explanation_contract(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical explanation envelope for a TSAR-visible candidate."""

    row = _mapping(candidate)
    action = candidate_action(row)
    bands = candidate_target_bands(row)
    source = _text(row.get("source")) or "unknown"
    contract = candidate_execution_contract(row)
    block_reason = _text(contract.get("direct_action_block_reason"))
    reason = _text(row.get("reason")) or block_reason
    reason_code = _text(row.get("reason_code") or row.get("code") or block_reason or action or "candidate")
    reason_ref = _stable_reason_ref(
        source=source,
        action=action,
        bands=bands,
        reason_code=reason_code,
        reason=reason,
    )
    return {
        "schema": EXPLANATION_SCHEMA,
        "owner": EXPLANATION_OWNER,
        "reason_ref": reason_ref,
        "reason_code": reason_code,
        "reason": reason,
        "source": source,
        "action": action,
        "target_bands": bands,
        "candidate_class": _text(contract.get("candidate_class")),
        "direct_action_allowed": bool(contract.get("direct_action_allowed")),
        "direct_action_block_reason": block_reason,
        "recommendation": _recommendation_text(row.get("recommendation")),
        "score": round(_float(row.get("score"), 0.0), 4),
        "confidence": round(_float(row.get("confidence"), 0.0), 3),
        "evidence_count": _int(row.get("evidence_count"), 0),
    }


def attach_candidate_explanation(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return a candidate row with the canonical explanation envelope attached."""

    row = dict(_mapping(candidate))
    explanation = candidate_explanation_contract(row)
    row["explanation"] = explanation
    row["reason_ref"] = explanation["reason_ref"]
    if not _text(row.get("reason")) and explanation["reason"]:
        row["reason"] = explanation["reason"]
    return row


def stamp_candidate_execution_contract(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return a candidate row with top-level execution fields and explanation."""

    row = dict(_mapping(candidate))
    contract = candidate_execution_contract(row)
    row["direct_action_allowed"] = bool(contract.get("direct_action_allowed"))
    row["direct_action_block_reason"] = _text(contract.get("direct_action_block_reason"))
    row["candidate_class"] = _text(contract.get("candidate_class"))
    row["target_bands"] = list(contract.get("target_bands") or [])
    return attach_candidate_explanation(row)


def is_direct_candidate_action(candidate: Any) -> bool:
    """Return True only for candidate rows with direct executable TSAR shape."""

    return bool(candidate_execution_contract(_mapping(candidate)).get("direct_action_allowed"))


__all__ = [
    "ADVISORY_ACTIONS",
    "ADVISORY_BLOCK_REASONS",
    "DIRECT_EXECUTABLE_ACTIONS",
    "EXPLANATION_OWNER",
    "EXPLANATION_SCHEMA",
    "attach_candidate_explanation",
    "candidate_action",
    "candidate_declared_block_reasons",
    "candidate_explanation_contract",
    "candidate_execution_contract",
    "candidate_target_bands",
    "is_direct_candidate_action",
    "stamp_candidate_execution_contract",
]
