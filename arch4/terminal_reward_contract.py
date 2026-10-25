"""Pure terminal-reward schema and validation contract."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from arch4.action_selection_receipts import (
    brain_decision_receipt_error,
    contextual_bandit_selection_receipt_error,
)
from arch4.action_transaction import (
    ActionTransactionKey,
    terminal_reward_horizon_s,
)
from arch4.brain.proof_feedback_owner import (
    terminal_layered_action_feedback_is_valid,
)
from arch4.evidence_scope import EvidenceScope, FormulaVector
from arch4.interfaces import ARCH_QUALITY_FORMULA_EPOCH
from arch4.terminal_reward_observation import (
    TerminalRewardObservation,
    terminal_reward_observation_error,
)


TERMINAL_REWARD_OWNER = "arch4.brain.reward_ledger"
TERMINAL_REWARD_SCHEMA = "TerminalRewardRecord.v2"
TERMINAL_REWARD_FORMULA_GENERATION = (
    "layered_action_feedback_terminal_action_horizon.v3"
)
TERMINAL_REWARD_FORMULA_EPOCH = "layered_roi_minus3_plus3_terminal_v2"
TERMINAL_REWARD_PREDICTION_SCHEMA = "TerminalRewardPrediction.v1"


def terminal_reward_formula_vector() -> FormulaVector:
    return FormulaVector.build(
        {
            "action_reward": TERMINAL_REWARD_FORMULA_EPOCH,
            "quality": str(ARCH_QUALITY_FORMULA_EPOCH or ""),
        }
    )


def terminal_reward_entry_error(entry: Mapping[str, Any]) -> str:
    """Validate one exact terminal ledger entry without mutating any owner."""

    if (
        entry.get("schema") != TERMINAL_REWARD_SCHEMA
        or entry.get("owner") != TERMINAL_REWARD_OWNER
    ):
        return "terminal_reward_owner_schema_invalid"
    identity = entry.get("proof_identity")
    identity_row = dict(identity) if isinstance(identity, Mapping) else {}
    action_transaction = identity_row.get("action_transaction")
    if not isinstance(action_transaction, Mapping):
        return "terminal_reward_action_transaction_missing"
    try:
        action_key = ActionTransactionKey.from_mapping(action_transaction)
    except (TypeError, ValueError):
        return "terminal_reward_action_transaction_invalid"
    if str(identity_row.get("action_root_id") or "") != action_key.root_id:
        return "terminal_reward_action_root_mismatch"
    if str(entry.get("action_root_id") or "") != action_key.root_id:
        return "terminal_reward_entry_action_root_mismatch"
    identity_fields = {
        "command_id": action_key.command_id,
        "proof_id": action_key.proof_id,
        "identity_key": action_key.identity_key,
    }
    for field, expected in identity_fields.items():
        if str(
            identity_row.get(field)
            or identity_row.get(f"smart_loop_{field}")
            or ""
        ) != expected:
            return f"terminal_reward_{field}_mismatch"
    if str(identity_row.get("session_scope_key") or "") != (
        action_key.matrix_identity.session_scope_key
    ):
        return "terminal_reward_session_scope_key_mismatch"
    if identity_row.get("source_generation") != (
        action_key.matrix_identity.matrix_sequence
    ):
        return "terminal_reward_source_generation_mismatch"
    if identity_row.get("matrix_identity") != (
        action_key.matrix_identity.as_dict()
    ):
        return "terminal_reward_matrix_identity_mismatch"
    if str(entry.get("proof_id") or "") != action_key.proof_id:
        return "terminal_reward_entry_proof_id_mismatch"
    if str(entry.get("command_id") or "") != action_key.command_id:
        return "terminal_reward_entry_command_id_mismatch"
    observation_raw = entry.get("terminal_observation")
    if not isinstance(observation_raw, Mapping):
        return "terminal_reward_observation_missing"
    reward_scope_raw = entry.get("evidence_scope")
    if not isinstance(reward_scope_raw, Mapping):
        return "terminal_reward_evidence_scope_missing"
    try:
        reward_scope = EvidenceScope.from_mapping(reward_scope_raw)
    except (TypeError, ValueError) as exc:
        return f"terminal_reward_evidence_scope_invalid:{exc}"
    if reward_scope.formula_vector != terminal_reward_formula_vector():
        return "terminal_reward_formula_vector_invalid"
    projection_payload = entry.get("projection_payload")
    projection_row = (
        dict(projection_payload)
        if isinstance(projection_payload, Mapping)
        else {}
    )
    record_raw = projection_row.get("record")
    record_row = dict(record_raw) if isinstance(record_raw, Mapping) else {}
    terminal_scope_raw = record_row.get("evidence_scope")
    terminal_scope_row = (
        dict(terminal_scope_raw)
        if isinstance(terminal_scope_raw, Mapping)
        else {}
    )
    try:
        terminal_scope = EvidenceScope.from_mapping(terminal_scope_row)
    except (TypeError, ValueError) as exc:
        return f"terminal_reward_terminal_evidence_scope_invalid:{exc}"
    baseline_scope_raw = record_row.get("baseline_evidence_scope")
    baseline_scope_row = (
        dict(baseline_scope_raw)
        if isinstance(baseline_scope_raw, Mapping)
        else {}
    )
    try:
        baseline_scope = EvidenceScope.from_mapping(baseline_scope_row)
    except (TypeError, ValueError) as exc:
        return f"terminal_reward_baseline_evidence_scope_invalid:{exc}"
    if (
        not reward_scope.same_physical_namespace(terminal_scope)
        or reward_scope.source_sequence != terminal_scope.source_sequence
    ):
        return "terminal_reward_terminal_evidence_scope_mismatch"
    if not baseline_scope.same_physical_namespace(terminal_scope):
        return "terminal_reward_action_evidence_namespace_mismatch"
    if (
        baseline_scope.session_scope_key
        != action_key.matrix_identity.session_scope_key
        or baseline_scope.source_epoch
        != action_key.matrix_identity.source_epoch
    ):
        return "terminal_reward_action_scope_namespace_mismatch"
    if (
        baseline_scope.source_sequence
        != action_key.matrix_identity.matrix_sequence
    ):
        return "terminal_reward_action_scope_generation_mismatch"
    target_error = _terminal_record_target_error(
        record_row=record_row,
        action_key=action_key,
    )
    if target_error:
        return target_error
    observation_error = terminal_reward_observation_error(
        observation_raw,
        evidence_scope=reward_scope.as_dict(),
    )
    if observation_error:
        suffix = observation_error.removeprefix(
            "terminal_observation_"
        )
        return f"terminal_reward_observation_{suffix}"
    observation = TerminalRewardObservation.from_mapping(observation_raw)
    if str(identity_row.get("opportunity_id") or "") != action_key.opportunity_id:
        return "terminal_reward_opportunity_id_mismatch"
    if (
        str(identity_row.get("opportunity_set_id") or "")
        != action_key.opportunity_set_id
    ):
        return "terminal_reward_opportunity_set_id_mismatch"
    if str(entry.get("opportunity_id") or "") != action_key.opportunity_id:
        return "terminal_reward_entry_opportunity_id_mismatch"
    if (
        str(entry.get("opportunity_set_id") or "")
        != action_key.opportunity_set_id
    ):
        return "terminal_reward_entry_opportunity_set_id_mismatch"
    for field, expected in (
        ("decision_id", action_key.decision_id),
        ("decision_receipt_id", action_key.decision_receipt_id),
        (
            "bandit_selection_receipt_id",
            action_key.bandit_selection_receipt_id,
        ),
    ):
        if str(identity_row.get(field) or "") != expected:
            return f"terminal_reward_{field}_mismatch"
        if str(entry.get(field) or "") != expected:
            return f"terminal_reward_entry_{field}_mismatch"
    if canonical_json(
        identity_row.get("brain_decision_receipt") or {}
    ) != canonical_json(
        action_key.as_dict().get("brain_decision_receipt") or {}
    ):
        return "terminal_reward_brain_decision_receipt_mismatch"
    if canonical_json(
        identity_row.get("bandit_selection_receipt") or {}
    ) != canonical_json(
        action_key.as_dict().get("bandit_selection_receipt") or {}
    ):
        return "terminal_reward_bandit_selection_receipt_mismatch"
    brain_receipt = dict(identity_row.get("brain_decision_receipt") or {})
    bandit_receipt = dict(identity_row.get("bandit_selection_receipt") or {})
    if action_key.opportunity_id:
        brain_error = brain_decision_receipt_error(
            brain_receipt,
            expected_opportunity_set_id=action_key.opportunity_set_id,
            expected_opportunity_id=action_key.opportunity_id,
        )
        if brain_error:
            return f"terminal_reward_{brain_error}"
        if (
            str(brain_receipt.get("decision_id") or "")
            != action_key.decision_id
        ):
            return "terminal_reward_brain_decision_id_mismatch"
        if (
            str(brain_receipt.get("decision_receipt_id") or "")
            != action_key.decision_receipt_id
        ):
            return "terminal_reward_brain_decision_receipt_id_mismatch"
        if bandit_receipt:
            bandit_error = contextual_bandit_selection_receipt_error(
                bandit_receipt,
                expected_learning_context_key=str(
                    brain_receipt.get("learning_context_key") or ""
                ),
                expected_opportunity_set_id=action_key.opportunity_set_id,
            )
            if bandit_error:
                return f"terminal_reward_{bandit_error}"
            if (
                str(bandit_receipt.get("selected_opportunity_id") or "")
                != action_key.opportunity_id
            ):
                return "terminal_reward_bandit_opportunity_id_mismatch"
            if (
                str(bandit_receipt.get("selection_receipt_id") or "")
                != action_key.bandit_selection_receipt_id
            ):
                return "terminal_reward_bandit_selection_receipt_id_mismatch"
        elif (
            str(brain_receipt.get("selected_owner_class") or "")
            == "optimization"
        ):
            return "terminal_reward_optimization_bandit_receipt_missing"
    expected_horizon = terminal_reward_horizon_s(action_key)
    if int(entry.get("horizon_s", 0) or 0) != expected_horizon:
        return f"terminal_reward_horizon_not_{expected_horizon}s"
    grade = entry.get("grade")
    if type(grade) is not int or not -3 <= grade <= 3:
        return "terminal_reward_grade_invalid"
    feedback = entry.get("layered_feedback")
    if not terminal_layered_action_feedback_is_valid(
        feedback if isinstance(feedback, Mapping) else None,
        horizon_s=expected_horizon,
    ):
        return "terminal_reward_layered_feedback_invalid"
    feedback_row = dict(feedback)
    if feedback_row.get("memory_grade_score") != grade:
        return "terminal_reward_grade_feedback_mismatch"
    if str(entry.get("label") or "") != str(
        feedback_row.get("learning_label") or ""
    ):
        return "terminal_reward_label_feedback_mismatch"
    for key in (
        "ledger_key",
        "proof_id",
        "event_id",
        "context_key",
        "formula_generation",
    ):
        if not str(entry.get(key) or ""):
            return f"terminal_reward_{key}_missing"
    if (
        str(entry.get("formula_generation"))
        != TERMINAL_REWARD_FORMULA_GENERATION
    ):
        return "terminal_reward_formula_generation_invalid"
    if str(identity_row.get("child_trace_id") or "") != str(
        entry.get("event_id") or ""
    ):
        return "terminal_reward_event_id_mismatch"
    if reward_context_key(identity_row) != str(entry.get("context_key") or ""):
        return "terminal_reward_context_key_mismatch"
    prediction_error = terminal_reward_prediction_error(
        entry.get("prediction"),
        grade=int(grade),
        action_key=action_key,
    )
    if prediction_error:
        return prediction_error
    key_parts = {
        "action_root_id": str(entry.get("action_root_id") or ""),
        "proof_id": str(entry.get("proof_id") or ""),
        "event_id": str(entry.get("event_id") or ""),
        "context_key": str(entry.get("context_key") or ""),
        "opportunity_id": str(entry.get("opportunity_id") or ""),
        "opportunity_set_id": str(entry.get("opportunity_set_id") or ""),
        "decision_id": str(entry.get("decision_id") or ""),
        "decision_receipt_id": str(entry.get("decision_receipt_id") or ""),
        "bandit_selection_receipt_id": str(
            entry.get("bandit_selection_receipt_id") or ""
        ),
        "terminal_observation_id": observation.observation_id,
        "evidence_scope_id": reward_scope.scope_id,
        "prediction_id": str(
            dict(entry.get("prediction") or {}).get("prediction_id") or ""
        ),
        "formula_generation": str(entry.get("formula_generation") or ""),
        "horizon_s": int(entry.get("horizon_s", 0) or 0),
    }
    expected_ledger_key = hashlib.sha256(
        canonical_json(key_parts).encode("utf-8")
    ).hexdigest()
    if str(entry.get("ledger_key") or "") != expected_ledger_key:
        return "terminal_reward_ledger_key_checksum_mismatch"
    return ""


def terminal_reward_prediction(
    *,
    bandit_selection_receipt: Mapping[str, Any] | None,
    grade: int,
) -> dict[str, Any]:
    receipt = (
        dict(bandit_selection_receipt)
        if isinstance(bandit_selection_receipt, Mapping)
        else {}
    )
    selected_forecast = (
        dict(receipt.get("selected_forecast") or {})
        if isinstance(receipt.get("selected_forecast"), Mapping)
        else {}
    )
    selected = bool(
        receipt
        and receipt.get("selected") is True
        and str(receipt.get("selection_receipt_id") or "")
        and selected_forecast
    )
    expected = (
        _finite_float(selected_forecast.get("expected_referee_grade"))
        if selected
        else None
    )
    content = {
        "schema": TERMINAL_REWARD_PREDICTION_SCHEMA,
        "owner": TERMINAL_REWARD_OWNER,
        "status": "measured" if expected is not None else "not_bandit_selected",
        "selection_receipt_id": str(
            receipt.get("selection_receipt_id") or ""
        ),
        "opportunity_id": str(
            selected_forecast.get("opportunity_id") or ""
        ),
        "expected_referee_grade": expected,
        "actual_referee_grade": int(grade),
        "signed_error": (
            round(float(grade) - float(expected), 6)
            if expected is not None
            else None
        ),
        "absolute_error": (
            round(abs(float(grade) - float(expected)), 6)
            if expected is not None
            else None
        ),
        "selected_forecast": selected_forecast,
    }
    return {**content, "prediction_id": _digest(content)}


def terminal_reward_prediction_error(
    raw: Mapping[str, Any] | None,
    *,
    grade: int,
    action_key: ActionTransactionKey,
) -> str:
    row = dict(raw) if isinstance(raw, Mapping) else {}
    if (
        row.get("schema") != TERMINAL_REWARD_PREDICTION_SCHEMA
        or row.get("owner") != TERMINAL_REWARD_OWNER
    ):
        return "terminal_reward_prediction_owner_schema_invalid"
    content = {
        key: value for key, value in row.items() if key != "prediction_id"
    }
    if str(row.get("prediction_id") or "") != _digest(content):
        return "terminal_reward_prediction_checksum_invalid"
    if row.get("actual_referee_grade") != grade:
        return "terminal_reward_prediction_actual_grade_mismatch"
    selected_forecast = (
        dict(row.get("selected_forecast") or {})
        if isinstance(row.get("selected_forecast"), Mapping)
        else {}
    )
    if action_key.bandit_selection_receipt_id:
        receipt = dict(_thaw_action_value(action_key.bandit_selection_receipt))
        if (
            row.get("status") != "measured"
            or str(row.get("selection_receipt_id") or "")
            != action_key.bandit_selection_receipt_id
            or str(row.get("opportunity_id") or "")
            != action_key.opportunity_id
            or selected_forecast
            != dict(receipt.get("selected_forecast") or {})
        ):
            return "terminal_reward_prediction_selection_mismatch"
        expected = _finite_float(
            selected_forecast.get("expected_referee_grade")
        )
        if expected is None or not -3.0 <= expected <= 3.0:
            return "terminal_reward_prediction_expected_grade_invalid"
        signed = _finite_float(row.get("signed_error"))
        absolute = _finite_float(row.get("absolute_error"))
        if (
            signed is None
            or absolute is None
            or abs(signed - (float(grade) - expected)) > 0.000001
            or abs(absolute - abs(float(grade) - expected)) > 0.000001
        ):
            return "terminal_reward_prediction_error_mismatch"
    elif (
        row.get("status") != "not_bandit_selected"
        or row.get("expected_referee_grade") is not None
        or row.get("signed_error") is not None
        or row.get("absolute_error") is not None
        or selected_forecast
    ):
        return "terminal_reward_prediction_not_applicable_invalid"
    return ""


def reward_context_key(identity: Mapping[str, Any]) -> str:
    explicit = str(identity.get("smart_loop_context_key") or "")
    if explicit:
        return explicit
    fallback = {
        "provider_plmn": str(identity.get("provider_plmn") or ""),
        "provider_name": str(identity.get("provider_name") or ""),
        "target_post_action_bands": list(
            identity.get("target_post_action_bands") or []
        ),
        "post_action_band": int(identity.get("post_action_band", 0) or 0),
        "grade_context_status": str(
            identity.get("grade_context_status") or ""
        ),
    }
    return "derived:" + hashlib.sha256(
        canonical_json(fallback).encode("utf-8")
    ).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return parsed


def _thaw_action_value(value: Any) -> Any:
    if isinstance(value, tuple):
        if (
            len(value) == 2
            and value[0] == "__action_transaction_mapping__"
            and isinstance(value[1], tuple)
        ):
            return {
                item[0]: _thaw_action_value(item[1]) for item in value[1]
            }
        if (
            len(value) == 2
            and value[0] == "__action_transaction_sequence__"
            and isinstance(value[1], tuple)
        ):
            return [_thaw_action_value(item) for item in value[1]]
        return [_thaw_action_value(item) for item in value]
    return value


def _terminal_record_target_error(
    *,
    record_row: Mapping[str, Any],
    action_key: ActionTransactionKey,
) -> str:
    if str(record_row.get("lever") or "") != action_key.lever:
        return "terminal_reward_target_lever_mismatch"
    params = (
        dict(record_row.get("action_params") or {})
        if isinstance(record_row.get("action_params"), Mapping)
        else {}
    )
    target = dict(
        _thaw_action_value(action_key.target.to_mapping())
    )
    for key, expected in target.items():
        if key == "lever":
            continue
        if key == "command_shape":
            if str(expected or "") not in {
                action_key.lever,
                str(params.get("intent") or ""),
            }:
                return "terminal_reward_target_command_shape_mismatch"
            continue
        if key not in params or canonical_json(params.get(key)) != canonical_json(expected):
            return f"terminal_reward_target_{key}_mismatch"
    return ""


__all__ = [
    "TERMINAL_REWARD_FORMULA_GENERATION",
    "TERMINAL_REWARD_FORMULA_EPOCH",
    "TERMINAL_REWARD_OWNER",
    "TERMINAL_REWARD_PREDICTION_SCHEMA",
    "TERMINAL_REWARD_SCHEMA",
    "canonical_json",
    "reward_context_key",
    "terminal_reward_formula_vector",
    "terminal_reward_entry_error",
    "terminal_reward_prediction",
    "terminal_reward_prediction_error",
]
