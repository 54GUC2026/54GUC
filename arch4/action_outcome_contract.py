"""Canonical action outcome contract.

This module owns the Referee-verdict-to-learning/outcome projection used by
engine learning, Memory commit, replay, and operator-facing status surfaces.
"""

from __future__ import annotations

from typing import Any, Mapping

SCHEMA = "action_outcome_contract.v1"
OWNER = "arch4.action_outcome_contract"

_BAD_LEARNING_CODES = {
    "post_action_regression",
    "radio_lost_after_action",
    "post_action_rf_collapse",
    "band_action_no_effect",
    "staged_roi_negative",
}
_GOOD_LEARNING_CODES = {"outcome_ok", "verified", "staged_roi_positive"}
_HARD_FAIL_CODES = {"radio_lost_after_action", "action_failed"}
_LTM_BLOCK_CODES = {
    "band_action_no_effect",
    "neutral_outcome",
    "execution_unclear",
    "missing_action_result",
    "action_failed",
    "nr_state_changed_during_eval",
    "observe_monitor",
    "staged_roi_context_changed",
    "staged_roi_invalid_evidence",
    "staged_roi_inconclusive",
    "staged_roi_neutral",
}
_BENEFICIAL_SEMANTIC_CODES = {"outcome_ok", "improved", "restored", "recovered"}
_HARMFUL_SEMANTIC_CODES = {"harmful_outcome", "outcome_bad", "regressed"}
_MIXED_SEMANTIC_CODES = {"observe_monitor", "post_action_instability"}
_BENIGN_REFEREE_SIGNAL_CODES = {"", "no_action", "no_outcome", "ok"}


def action_outcome_contract_from_referee(
    referee_outcome: Mapping[str, Any] | None,
    *,
    action_present: bool,
    action_success: bool | None = None,
) -> dict[str, Any]:
    """Project a Referee verdict into the only legal action-outcome contract."""
    verdict = referee_outcome if isinstance(referee_outcome, Mapping) else {}
    level = str(verdict.get("level", "") or "").strip().lower()
    code = str(verdict.get("code", "") or "").strip().lower()
    message = str(verdict.get("message", "") or "").strip()

    learning_label = _learning_label(level=level, code=code)
    semantic = _operator_semantic(
        level=level,
        code=code,
        action_present=action_present,
    )
    ltm_allowed = code not in _LTM_BLOCK_CODES
    policy_reason_suffix = f"referee_override:{code}" if code and not ltm_allowed else ""

    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "level": level,
        "code": code,
        "message": message,
        "action_scope": "action" if action_present else "no_action",
        "action_taken": bool(action_present),
        "action_success": action_success,
        "learning_outcome_label": learning_label,
        "operator_semantic": semantic,
        "hard_fail": code in _HARD_FAIL_CODES,
        "learning_ltm_allowed": bool(ltm_allowed),
        "learning_policy_reason_suffix": policy_reason_suffix,
    }


def action_outcome_contract_from_action_record(action_record: Any) -> dict[str, Any]:
    """Project a persisted ActionRecord through the canonical outcome contract."""
    code = str(getattr(action_record, "referee_outcome_code", "") or "").strip()
    level = str(getattr(action_record, "referee_outcome_level", "") or "").strip()
    message = str(
        getattr(action_record, "referee_outcome_message", "")
        or getattr(action_record, "learning_policy_reason", "")
        or ""
    ).strip()
    return action_outcome_contract_from_referee(
        {
            "level": level,
            "code": code,
            "message": message,
        },
        action_present=True,
        action_success=bool(getattr(action_record, "success", False)),
    )


def action_outcome_contract_from_staged_roi(
    *,
    label: str,
    reason: str,
) -> dict[str, Any]:
    """Project delayed staged-ROI proof into the canonical action contract."""

    label_norm = str(label or "").strip().lower()
    reason_text = str(reason or "")
    if label_norm == "bad":
        level = "bad"
        code = "staged_roi_negative"
    elif label_norm == "good":
        level = "good"
        code = "staged_roi_positive"
    else:
        level = "neutral"
        if reason_text.startswith("context_changed") or reason_text.startswith("context_unverified"):
            code = "staged_roi_context_changed"
        elif reason_text.startswith("invalid_grade_evidence"):
            code = "staged_roi_invalid_evidence"
        else:
            code = "staged_roi_inconclusive"
    return action_outcome_contract_from_referee(
        {
            "level": level,
            "code": code,
            "message": reason_text,
            "stage": "staged_roi",
            "authority": OWNER,
            "triggered_by": "engine",
        },
        action_present=True,
        action_success=label_norm == "good",
    )


def action_outcome_has_material_referee_signal(
    referee_outcome: Mapping[str, Any] | None,
) -> bool:
    """Return whether a raw Referee envelope is material enough to emit.

    Callers may use this to debounce/log snapshots, but the meaning of Referee
    codes still stays here with the canonical action outcome projection.
    """

    contract = action_outcome_contract_from_referee(
        referee_outcome,
        action_present=False,
        action_success=None,
    )
    level = str(contract.get("level", "") or "").strip().lower()
    code = str(contract.get("code", "") or "").strip().lower()
    if code in _BENIGN_REFEREE_SIGNAL_CODES and level in {"", "ok", "neutral", "deferred"}:
        return False
    if bool(contract.get("hard_fail", False)):
        return True
    if str(contract.get("learning_outcome_label", "") or "") in {"good", "bad"}:
        return True
    return code not in _BENIGN_REFEREE_SIGNAL_CODES or level not in {"", "ok"}


def _learning_label(*, level: str, code: str) -> str:
    if code in _BAD_LEARNING_CODES or level in {"bad", "conflict"}:
        return "bad"
    if code in _GOOD_LEARNING_CODES or level in {"good", "ok"}:
        return "good"
    if level or code:
        return "neutral"
    return ""


def _operator_semantic(*, level: str, code: str, action_present: bool) -> str:
    if not action_present or code == "no_action":
        return "none"
    if code == "learning_gate_blocked" or level == "blocked":
        return "blocked"
    if level == "good" or code in _BENEFICIAL_SEMANTIC_CODES:
        return "beneficial"
    if level in {"bad", "harmful"} or code in _HARMFUL_SEMANTIC_CODES:
        return "harmful"
    if level in {"warning"} or code in _MIXED_SEMANTIC_CODES:
        return "mixed"
    if level in {"neutral", "deferred"}:
        return "deferred"
    return "unknown"


__all__ = [
    "OWNER",
    "SCHEMA",
    "action_outcome_contract_from_action_record",
    "action_outcome_contract_from_referee",
    "action_outcome_contract_from_staged_roi",
    "action_outcome_has_material_referee_signal",
]
