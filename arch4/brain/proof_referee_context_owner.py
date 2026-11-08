"""Context identity owner for delayed ROI proof/referee grading."""

from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

from typing import Any, Mapping, Optional

from arch4.action_transaction import ActionTransactionKey

OWNER = "arch4.brain.proof_referee_context_owner"
PROOF_IDENTITY_SCHEMA = "delayed_reward_proof_identity.v1"


def staged_roi_context_status(
    *,
    original: Any,
    modem_now: Optional[CellularIntelligenceGeneration],
) -> tuple[str, str, dict[str, Any]]:
    params = dict(getattr(original, "action_params", {}) or {})
    evidence: dict[str, Any] = {}
    if modem_now is None:
        evidence["grade_context_status"] = "unverified"
        evidence["grade_context_reason"] = "context_unverified:missing_modem"
        return "unverified", "context_unverified:missing_modem", evidence

    before_plmn = provider_identity(getattr(original, "provider_plmn", ""))
    before_provider = provider_identity(getattr(original, "provider_name", ""))
    now_plmn = provider_identity(modem_now.identity.provider_plmn)
    now_provider = provider_identity(modem_now.identity.provider_name)
    evidence.update(
        {
            "context_provider_plmn_before": before_plmn,
            "context_provider_plmn_now": now_plmn,
            "context_provider_before": before_provider,
            "context_provider_now": now_provider,
        }
    )
    if before_plmn and now_plmn and before_plmn != now_plmn:
        reason = f"context_changed:provider_plmn:{before_plmn}->{now_plmn}"
        evidence["grade_context_status"] = "changed"
        evidence["grade_context_reason"] = reason
        return "changed", reason, evidence
    if before_plmn and not now_plmn:
        evidence["context_provider_plmn_now_missing"] = True
    if before_provider and now_provider and before_provider != now_provider:
        reason = f"context_changed:provider:{before_provider}->{now_provider}"
        evidence["grade_context_status"] = "changed"
        evidence["grade_context_reason"] = reason
        return "changed", reason, evidence
    if before_provider and not now_provider:
        evidence["context_provider_now_missing"] = True

    anchor_before = int_or_zero(
        params.get("_tsar_candidate_band")
        or params.get("_ca_pool_anchor_band")
        or params.get("pband")
    )
    pband_now = int_or_zero(modem_now.cellular.primary_band)
    evidence["context_pband_before"] = anchor_before
    evidence["context_pband_now"] = pband_now
    if anchor_before > 0 and pband_now > 0 and anchor_before != pband_now:
        reason = f"context_changed:pband:B{anchor_before}->B{pband_now}"
        evidence["grade_context_status"] = "changed"
        evidence["grade_context_reason"] = reason
        return "changed", reason, evidence

    expected_pool = band_set_from_value(params.get("ca_pool") or params.get("bands"))
    current_pool = current_configured_band_pool(modem_now)
    evidence["context_expected_pool"] = sorted(expected_pool)
    evidence["context_current_pool"] = sorted(current_pool)
    if expected_pool and current_pool and expected_pool != current_pool:
        reason = (
            "context_changed:ca_pool:"
            + "+".join(f"B{b}" for b in sorted(expected_pool))
            + "->"
            + "+".join(f"B{b}" for b in sorted(current_pool))
        )
        evidence["grade_context_status"] = "changed"
        evidence["grade_context_reason"] = reason
        return "changed", reason, evidence
    if expected_pool and not current_pool:
        reason = "context_unverified:ca_pool_missing"
        evidence["grade_context_status"] = "unverified"
        evidence["grade_context_reason"] = reason
        return "unverified", reason, evidence

    evidence["grade_context_status"] = "same"
    evidence["grade_context_reason"] = "same_context"
    return "same", "same_context", evidence


def target_or_post_action_bands(
    *,
    original: Any,
    modem_now: Optional[CellularIntelligenceGeneration],
) -> list[int]:
    """Resolve reward attribution from commanded target and post-action truth."""

    params = dict(getattr(original, "action_params", {}) or {})
    bands: set[int] = set()
    for key in ("bands", "ca_pool", "dl_bands", "resolved_target_bands"):
        bands.update(band_set_from_value(params.get(key)))
    for key in ("pband", "band", "primary_band", "_ca_pool_anchor_band", "_tsar_candidate_band"):
        band = int_or_zero(params.get(key))
        if band > 0:
            bands.add(band)
    post_band = (
        int_or_zero(modem_now.cellular.primary_band)
        if modem_now is not None
        else 0
    )
    if post_band > 0:
        bands.add(post_band)
    return sorted(bands)


def delayed_reward_proof_identity(
    *,
    original: Any,
    record: Any = None,
    modem_now: Optional[CellularIntelligenceGeneration] = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the exact identity attached to every terminal reward projection."""

    meta_raw = getattr(original, "objective_metadata", {}) or {}
    meta = dict(meta_raw) if isinstance(meta_raw, Mapping) else {}
    evidence_row = dict(evidence or {}) if isinstance(evidence, Mapping) else {}
    action_transaction_raw = (
        dict(meta.get("action_transaction") or {})
        if isinstance(meta.get("action_transaction"), Mapping)
        else {}
    )
    action_key: ActionTransactionKey | None = None
    try:
        action_key = ActionTransactionKey.from_mapping(action_transaction_raw)
    except (TypeError, ValueError):
        action_key = None
    action_transaction = action_key.as_dict() if action_key is not None else {}
    action_root_id = str(action_key.root_id if action_key is not None else "")
    selected_action_command_id = str(
        action_key.command_id if action_key is not None else ""
    )
    selected_action_proof_id = str(
        action_key.proof_id if action_key is not None else ""
    )
    context_key = str(meta.get("smart_loop_context_key") or "")
    command_id = selected_action_command_id
    proof_id = selected_action_proof_id
    parent_trace = str(getattr(original, "decision_trace_id", "") or "")
    child_trace = str(getattr(record, "decision_trace_id", "") or "") if record is not None else ""
    post_band = (
        int_or_zero(modem_now.cellular.primary_band)
        if modem_now is not None
        else 0
    )
    brain_decision_receipt = dict(
        action_transaction.get("brain_decision_receipt") or {}
    )
    selected_owner_class = str(
        brain_decision_receipt.get("selected_owner_class") or ""
    )
    optimization_selected = selected_owner_class == "optimization"
    expected_optimization_context = str(
        brain_decision_receipt.get("learning_context_key") or ""
    )
    exact_identity_complete = bool(
        parent_trace
        and child_trace
        and action_root_id
        and action_key is not None
        and (
            not optimization_selected
            or (
                context_key
                and expected_optimization_context
                and context_key == expected_optimization_context
                and action_key.bandit_selection_receipt_id
            )
        )
    )
    return {
        "schema": PROOF_IDENTITY_SCHEMA,
        "owner": OWNER,
        "parent_trace_id": parent_trace,
        "child_trace_id": child_trace,
        "selected_action_command_id": command_id,
        "selected_action_proof_id": proof_id,
        "command_id": command_id,
        "proof_id": proof_id,
        "smart_loop_context_key": context_key,
        "smart_loop_reason_ref": str(meta.get("smart_loop_reason_ref") or ""),
        "selected_owner_class": selected_owner_class,
        "optimization_selected": optimization_selected,
        "action_root_id": action_root_id,
        "action_transaction": action_transaction,
        "session_scope_key": str(
            action_key.matrix_identity.session_scope_key
            if action_key is not None
            else ""
        ),
        "identity_key": str(action_transaction.get("identity_key") or ""),
        "source_generation": int(
            action_key.matrix_identity.matrix_sequence
            if action_key is not None
            else 0
        ),
        "matrix_identity": (
            action_key.matrix_identity.as_dict()
            if action_key is not None
            else {}
        ),
        "opportunity_id": str(action_transaction.get("opportunity_id") or ""),
        "opportunity_set_id": str(
            action_transaction.get("opportunity_set_id") or ""
        ),
        "decision_id": str(action_transaction.get("decision_id") or ""),
        "decision_receipt_id": str(
            action_transaction.get("decision_receipt_id") or ""
        ),
        "bandit_selection_receipt_id": str(
            action_transaction.get("bandit_selection_receipt_id") or ""
        ),
        "brain_decision_receipt": brain_decision_receipt,
        "bandit_selection_receipt": dict(
            action_transaction.get("bandit_selection_receipt") or {}
        ),
        "roi_horizon_s": int_or_zero(getattr(record, "roi_horizon_s", 0)) if record is not None else 0,
        "target_post_action_bands": target_or_post_action_bands(original=original, modem_now=modem_now),
        "post_action_band": post_band,
        "provider_plmn": str(evidence_row.get("context_provider_plmn_now") or ""),
        "provider_name": str(evidence_row.get("context_provider_now") or ""),
        "grade_context_status": str(evidence_row.get("grade_context_status") or ""),
        "exact_identity_complete": exact_identity_complete,
    }


def current_configured_band_pool(modem_now: Optional[CellularIntelligenceGeneration]) -> set[int]:
    if modem_now is None:
        return set()
    for value in (
        modem_now.identity.configured_band_pool,
        modem_now.identity.configured_band_pool_signature,
        modem_now.cellular.selected_band_set,
        modem_now.cellular.selected_band_set_signature,
    ):
        bands = band_set_from_value(value)
        if bands:
            return bands
    return set()


def band_set_from_value(value: Any) -> set[int]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        text = str(value or "")
        for marker in ("B", "b", "+", ",", ";", "|", "/", "\\"):
            text = text.replace(marker, " ")
        values = text.split()
    bands: set[int] = set()
    for raw in values:
        band = int_or_zero(raw)
        if band > 0:
            bands.add(band)
    return bands


def provider_identity(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"unknown", "none", "n/a", "na", "-", "--"}:
        return ""
    return text


def int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "OWNER",
    "PROOF_IDENTITY_SCHEMA",
    "band_set_from_value",
    "current_configured_band_pool",
    "delayed_reward_proof_identity",
    "int_or_zero",
    "provider_identity",
    "staged_roi_context_status",
    "target_or_post_action_bands",
]
