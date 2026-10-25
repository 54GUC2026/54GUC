"""
Incident-aware learning policy helpers.

This layer decides when incident context should or should not influence
band-learning consolidation. The goal is to avoid poisoning long-term
radio learning with non-radio or low-confidence incidents.
"""

from __future__ import annotations

from typing import Any, Dict

from arch4.policy.incident_action_policy import incident_class_name


RADIO_RELEVANT_NEGATIVE_CLASSES = {
    "healthy",
    "radio_ca_loss",
}


def band_learning_policy(assessment: Dict[str, Any] | None) -> Dict[str, Any]:
    incident_class = incident_class_name(assessment)
    self_induced = bool((assessment or {}).get("self_induced", False))

    negative_radio_relevant = incident_class in RADIO_RELEVANT_NEGATIVE_CLASSES and not self_induced
    if negative_radio_relevant:
        reason = "negative band-learning is relevant for this incident class"
    elif self_induced:
        reason = "self-induced instability should not poison negative band-learning"
    else:
        reason = "non-radio or low-confidence incident should not poison negative band-learning"

    return {
        "incident_class": incident_class,
        "self_induced": self_induced,
        "negative_radio_relevant": negative_radio_relevant,
        "reason": reason,
    }


def adapt_band_learning_outcome(
    *,
    outcome_label: str,
    hard_fail: bool,
    assessment: Dict[str, Any] | None,
) -> Dict[str, Any]:
    policy = band_learning_policy(assessment)
    normalized = str(outcome_label or "").strip().lower() or "neutral"
    use_ltm = True
    effective_label = normalized
    effective_hard_fail = bool(hard_fail)

    if normalized == "bad" and not policy["negative_radio_relevant"]:
        effective_label = "neutral"
        effective_hard_fail = False
        use_ltm = False
    elif normalized == "neutral" and policy["incident_class"] not in RADIO_RELEVANT_NEGATIVE_CLASSES:
        use_ltm = False

    return {
        "incident_class": policy["incident_class"],
        "effective_label": effective_label,
        "effective_hard_fail": effective_hard_fail,
        "use_ltm": use_ltm,
        "reason": policy["reason"],
    }
