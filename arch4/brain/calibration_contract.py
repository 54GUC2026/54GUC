"""Canonical self-calibration observation and proposal contracts.

The numerical calibration/scoring engines remain the evidence producers.  This
module owns the admission envelope that prevents evidence from crossing modem,
runtime-session, accepted-generation, or formula boundaries, and the proposal
shape that may later be approved into a configuration generation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
from typing import Any, Mapping, Sequence

from arch4.evidence_scope import (
    EvidencePortability,
    EvidenceScope,
    FormulaVector,
    evidence_scope_from_mapping,
    validated_evidence_scope,
)
from arch4.interfaces import ARCH_QUALITY_FORMULA_EPOCH


CALIBRATION_OBSERVATION_SCHEMA = "CalibrationObservation.v1"
CALIBRATION_PROPOSAL_SCHEMA = "CalibrationProposal.v1"
CALIBRATION_OBSERVATION_OWNER = "calibration_observation_authority"
CALIBRATION_PROPOSAL_OWNER = "calibration_proposal_authority"
CALIBRATION_NUMERIC_FORMULA_EPOCH = "vb53_tuned_calibration_v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return out if math.isfinite(out) else float(default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def calibration_formula_vector() -> FormulaVector:
    """Return the calibration lane's canonical FormulaVector.v1."""

    return FormulaVector.build(
        {
            "calibration_numeric": CALIBRATION_NUMERIC_FORMULA_EPOCH,
            "quality": str(ARCH_QUALITY_FORMULA_EPOCH or ""),
        }
    )


def build_calibration_evidence_scope(
    *,
    identity_code: str,
    identity_status: str,
    identity_catalog_version: str,
    identity_catalog_checksum: str,
    session_scope_key: str,
    source_epoch: str,
    source_sequence: int,
    observed_at: float,
    evidence_age_s: float,
    provider_plmn: str = "",
    confidence: float = 1.0,
    evidence_count: int = 1,
    access_type_id: str = "",
    sim_scope_hash: str = "",
    site_scope_id: str = "",
    cell_scope_key: str = "",
    serving_band: int = 0,
    action_shape_key: str = "",
) -> dict[str, Any]:
    return EvidenceScope(
        identity_code=_text(identity_code),
        identity_status=_text(identity_status),
        identity_catalog_version=_text(identity_catalog_version),
        identity_catalog_checksum=_text(identity_catalog_checksum),
        session_scope_key=_text(session_scope_key),
        source_epoch=_text(source_epoch),
        source_sequence=_int(source_sequence),
        observed_at=_float(observed_at),
        evidence_age_s=_float(evidence_age_s),
        confidence=_float(confidence),
        evidence_count=_int(evidence_count),
        formula_vector=calibration_formula_vector(),
        portability=EvidencePortability.EXACT_IDENTITY,
        access_type_id=_text(access_type_id),
        provider_plmn=_text(provider_plmn),
        sim_scope_hash=_text(sim_scope_hash),
        site_scope_id=_text(site_scope_id),
        cell_scope_key=_text(cell_scope_key),
        serving_band=_int(serving_band),
        action_shape_key=_text(action_shape_key),
    ).as_dict()


def evidence_scope_error(scope: Mapping[str, Any] | None) -> str:
    try:
        validated = validated_evidence_scope(_mapping(scope))
    except ValueError as exc:
        return str(exc)
    if validated.formula_vector != calibration_formula_vector():
        return "formula_vector_mismatch"
    return ""


def evidence_scopes_match(
    left: Mapping[str, Any] | None,
    right: Mapping[str, Any] | None,
) -> bool:
    try:
        a = validated_evidence_scope(_mapping(left))
        b = validated_evidence_scope(_mapping(right))
    except ValueError:
        return False
    return bool(
        a.formula_vector == calibration_formula_vector()
        and b.formula_vector == calibration_formula_vector()
        and a.exact_runtime_match(b)
    )


def _sample_error(sample: Mapping[str, Any], *, generation_confirmed: bool) -> str:
    if not generation_confirmed:
        return "accepted_generation_confirmation_required"
    if sample.get("sample_fresh") is not True:
        return "sample_not_fresh"
    if bool(sample.get("sample_stale", False)):
        return "sample_stale"
    if _int(sample.get("sample_sequence")) <= 0:
        return "sample_sequence_required"
    sample_ts = _float(sample.get("sample_ts"))
    accepted_ts = _float(sample.get("engine_tick_ts"))
    if sample_ts <= 0.0 or accepted_ts <= 0.0 or accepted_ts < sample_ts:
        return "accepted_generation_timing_invalid"
    if _float(sample.get("sample_age_s"), -1.0) < 0.0:
        return "sample_age_invalid"
    if not bool(sample.get("radio_connected", False)):
        return "radio_not_connected"
    if sample.get("health_ok") is False or sample.get("health_overall_ok") is False:
        return "sample_health_degraded"
    band = _int(sample.get("serving_band") or sample.get("primary_band"))
    cell = _int(sample.get("pci") or sample.get("cell_id"))
    if band <= 0 or cell <= 0:
        return "radio_context_incomplete"
    for key, low, high in (
        ("sinr_db", -50.0, 80.0),
        ("rsrp_dbm", -170.0, -20.0),
        ("rsrq_db", -50.0, 10.0),
        ("rtt_ms", 0.0, 120000.0),
        ("throughput_dl_mbps", 0.0, 100000.0),
        ("throughput_ul_mbps", 0.0, 100000.0),
    ):
        value = _float(sample.get(key), float("nan"))
        if not math.isfinite(value) or value < low or value > high:
            return f"sample_metric_invalid:{key}"
    return ""


def build_calibration_observation(
    *,
    sample: Mapping[str, Any] | None,
    calibration_status: Mapping[str, Any] | None,
    learning_gate: Mapping[str, Any] | None,
    identity_code: str,
    identity_status: str,
    identity_catalog_version: str,
    identity_catalog_checksum: str,
    session_scope_key: str,
    source_epoch: str,
    provider_plmn: str = "",
    access_type_id: str = "",
    sim_scope_hash: str = "",
    site_scope_id: str = "",
    action_shape_key: str = "",
    generation_confirmed: bool,
    now: float,
) -> dict[str, Any]:
    payload = _mapping(sample)
    status = _mapping(calibration_status)
    gate = _mapping(learning_gate)
    reason = ""
    if gate.get("learning_allowed") is not True:
        reason = _text(gate.get("block_reason")) or "learning_gate_blocked"
    scope: dict[str, Any] = {}
    if not reason:
        try:
            scope = evidence_scope_from_mapping(
                {
                    **payload,
                    "identity_code": _text(identity_code),
                    "identity_status": _text(identity_status),
                    "identity_catalog_version": _text(identity_catalog_version),
                    "identity_catalog_checksum": _text(identity_catalog_checksum),
                    "session_scope_key": _text(session_scope_key),
                    "source_epoch": _text(source_epoch),
                    "provider_plmn": _text(provider_plmn),
                    "access_type_id": _text(access_type_id),
                    "sim_scope_hash": _text(sim_scope_hash),
                    "site_scope_id": _text(site_scope_id),
                    "action_shape_key": _text(action_shape_key),
                },
                formula_vector=calibration_formula_vector(),
                confidence=_float(status.get("confidence"), 1.0),
                evidence_count=1,
            ).as_dict()
        except ValueError as exc:
            reason = str(exc)
    if not reason:
        reason = _sample_error(payload, generation_confirmed=bool(generation_confirmed))
    identity = {
        "evidence_scope_id": _text(scope.get("evidence_scope_id")),
        "sample_sequence": _int(payload.get("sample_sequence")),
        "sample_ts": _float(payload.get("sample_ts")),
        "sample_digest": _digest(payload),
        "calibration_status_digest": _digest(status),
    }
    observation_id = _digest(identity) if not reason else ""
    return {
        "schema": CALIBRATION_OBSERVATION_SCHEMA,
        "owner": CALIBRATION_OBSERVATION_OWNER,
        "observation_id": observation_id,
        "accepted": not bool(reason),
        "reason": reason,
        "observed_at": _float(now),
        "evidence_scope": scope,
        "generation": {
            "confirmed": bool(generation_confirmed),
            "sequence": _int(payload.get("sample_sequence")),
            "sample_ts": _float(payload.get("sample_ts")),
            "accepted_ts": _float(payload.get("engine_tick_ts")),
            "age_s": _float(payload.get("sample_age_s")),
            "source": _text(payload.get("sample_source")),
        },
        "learning_gate": gate,
        "calibration_status": status if not reason else {},
        "sample": payload if not reason else {},
    }


def calibration_observation_error(observation: Mapping[str, Any] | None) -> str:
    data = _mapping(observation)
    if _text(data.get("schema")) != CALIBRATION_OBSERVATION_SCHEMA:
        return "calibration_observation_schema_required"
    if _text(data.get("owner")) != CALIBRATION_OBSERVATION_OWNER:
        return "calibration_observation_owner_mismatch"
    if data.get("accepted") is not True:
        return _text(data.get("reason")) or "calibration_observation_not_admitted"
    scope_error = evidence_scope_error(_mapping(data.get("evidence_scope")))
    if scope_error:
        return scope_error
    sample = _mapping(data.get("sample"))
    generation = _mapping(data.get("generation"))
    if _int(generation.get("sequence")) != _int(sample.get("sample_sequence")):
        return "observation_generation_sequence_mismatch"
    sample_error = _sample_error(
        sample,
        generation_confirmed=bool(generation.get("confirmed", False)),
    )
    if sample_error:
        return sample_error
    expected_id = _digest(
        {
            "evidence_scope_id": _text(
                _mapping(data.get("evidence_scope")).get("evidence_scope_id")
            ),
            "sample_sequence": _int(sample.get("sample_sequence")),
            "sample_ts": _float(sample.get("sample_ts")),
            "sample_digest": _digest(sample),
            "calibration_status_digest": _digest(_mapping(data.get("calibration_status"))),
        }
    )
    if not _text(data.get("observation_id")) or _text(data.get("observation_id")) != expected_id:
        return "observation_id_mismatch"
    return ""


def observation_sample(observation: Mapping[str, Any]) -> dict[str, Any]:
    data = _mapping(observation)
    sample = copy.deepcopy(_mapping(data.get("sample")))
    sample["observation_id"] = _text(data.get("observation_id"))
    sample["calibration_observation_schema"] = CALIBRATION_OBSERVATION_SCHEMA
    sample["evidence_scope"] = copy.deepcopy(_mapping(data.get("evidence_scope")))
    sample["calibration_status"] = copy.deepcopy(_mapping(data.get("calibration_status")))
    return sample


def _calibration_proposal_identity(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return every fact that makes one proposal distinct and replay-safe."""

    row = _mapping(data)
    return {
        "candidate_id": _text(row.get("candidate_id")),
        "candidate_version": _int(row.get("candidate_version")),
        "evidence_scope": copy.deepcopy(_mapping(row.get("evidence_scope"))),
        "formula_vector": copy.deepcopy(_mapping(row.get("formula_vector"))),
        "evidence": copy.deepcopy(_mapping(row.get("evidence"))),
        "deltas": copy.deepcopy(list(row.get("deltas") or [])),
        "rollback": copy.deepcopy(_mapping(row.get("rollback"))),
        "proposed_values": copy.deepcopy(_mapping(row.get("proposed_values"))),
        "applicability": copy.deepcopy(_mapping(row.get("applicability"))),
        "provenance": copy.deepcopy(_mapping(row.get("provenance"))),
    }


def build_calibration_proposal(
    *,
    candidate_id: str,
    candidate_version: int,
    base_values: Mapping[str, Any],
    raw_samples: Sequence[Mapping[str, Any]],
    sample_summary: Mapping[str, Any],
    analysis_report: Mapping[str, Any],
    evidence_scope: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build learned deltas only from the existing tuned drift evidence.

    The old engine's sustained, quiet, same-context drift logic already emits a
    bounded dB hint.  Reusing that hint avoids inventing a second calibration
    formula.  A proposal with no qualifying hint is explicit and cannot change
    runtime policy.
    """

    scope = _mapping(evidence_scope)
    scope_error = evidence_scope_error(scope)
    base = copy.deepcopy(_mapping(base_values))
    eligible: list[float] = []
    observation_ids: list[str] = []
    for row in raw_samples:
        sample = _mapping(row)
        row_scope = _mapping(sample.get("evidence_scope"))
        if not evidence_scopes_match(scope, row_scope):
            continue
        if bool(sample.get("network_load_biased", False)):
            continue
        if not bool(sample.get("health_ok", True)) or not bool(
            sample.get("health_overall_ok", True)
        ):
            continue
        hint = _float(sample.get("threshold_adjust_hint_db"))
        if hint > 0.0:
            eligible.append(hint)
        observation_id = _text(sample.get("observation_id"))
        if observation_id:
            observation_ids.append(observation_id)

    deltas: list[dict[str, Any]] = []
    proposed_values = copy.deepcopy(base)
    if not scope_error and len(eligible) >= 4 and "sinr_feasibility_gate_db" in base:
        learned_hint = min(3.0, max(0.0, float(statistics.median(eligible))))
        rollback = _float(base.get("sinr_feasibility_gate_db"))
        proposed = round(max(-5.0, min(30.0, rollback - learned_hint)), 3)
        if proposed != rollback:
            deltas.append(
                {
                    "parameter": "sinr_feasibility_gate_db",
                    "rollback": rollback,
                    "proposed": proposed,
                    "delta": round(proposed - rollback, 3),
                    "source_field": "threshold_adjust_hint_db",
                    "formula": CALIBRATION_NUMERIC_FORMULA_EPOCH,
                    "evidence_count": len(eligible),
                }
            )
            proposed_values["sinr_feasibility_gate_db"] = proposed

    evidence = {
        "observation_ids": sorted(set(observation_ids)),
        "observation_count": len(set(observation_ids)),
        "eligible_drift_hint_count": len(eligible),
        "sample_summary": copy.deepcopy(_mapping(sample_summary)),
    }
    formula_vector = copy.deepcopy(_mapping(scope.get("formula_vector")))
    applicability = {
        "policy": "exact_current_runtime",
        "evidence_scope_id": _text(scope.get("evidence_scope_id")),
        "identity_code": _text(scope.get("identity_code")),
        "identity_status": _text(scope.get("identity_status")),
        "identity_catalog_version": _text(scope.get("identity_catalog_version")),
        "identity_catalog_checksum": _text(scope.get("identity_catalog_checksum")),
        "session_scope_key": _text(scope.get("session_scope_key")),
        "source_epoch": _text(scope.get("source_epoch")),
        "provider_plmn": _text(scope.get("provider_plmn")),
        "cell_scope_key": _text(scope.get("cell_scope_key")),
        "serving_band": _int(scope.get("serving_band")),
        "formula_vector_id": _text(formula_vector.get("formula_vector_id")),
    }
    rollback_values = {row["parameter"]: row["rollback"] for row in deltas}
    rollback = {
        "strategy": "restore_previous_committed_generation",
        "values": copy.deepcopy(rollback_values),
        "required_on": [
            "consumer_ack_failure",
            "calibration_authority_ack_failure",
            "publication_failure",
        ],
    }
    provenance = {
        "producer": CALIBRATION_PROPOSAL_OWNER,
        "numeric_formula_epoch": CALIBRATION_NUMERIC_FORMULA_EPOCH,
        "formula_vector_id": _text(formula_vector.get("formula_vector_id")),
        "analysis_report_digest": _digest(_mapping(analysis_report)),
        "sample_summary_digest": _digest(_mapping(sample_summary)),
        "source_observation_count": len(set(observation_ids)),
    }
    proposal_identity = _calibration_proposal_identity(
        {
            "candidate_id": _text(candidate_id),
            "candidate_version": _int(candidate_version),
            "evidence_scope": scope,
            "formula_vector": formula_vector,
            "evidence": evidence,
            "deltas": deltas,
            "rollback": rollback,
            "proposed_values": proposed_values,
            "applicability": applicability,
            "provenance": provenance,
        }
    )
    proposal_id = _digest(proposal_identity) if not scope_error else ""
    approval_ready = bool(_mapping(analysis_report).get("approval_ready", False))
    return {
        "schema": CALIBRATION_PROPOSAL_SCHEMA,
        "owner": CALIBRATION_PROPOSAL_OWNER,
        "proposal_id": proposal_id,
        "candidate_id": _text(candidate_id),
        "candidate_version": _int(candidate_version),
        "status": (
            "invalid_scope"
            if scope_error
            else ("ready" if approval_ready else "hold")
        ),
        "reason": scope_error or ("approval_ready" if approval_ready else "analysis_not_approval_ready"),
        "evidence_scope": copy.deepcopy(scope),
        "formula_vector": formula_vector,
        "evidence": evidence,
        "deltas": deltas,
        "rollback": rollback,
        # Kept as a public projection for existing operator/report surfaces;
        # the canonical rollback identity is the structured object above.
        "rollback_values": copy.deepcopy(rollback_values),
        "proposed_values": proposed_values,
        "applicability": applicability,
        "provenance": provenance,
        "changes_policy": bool(deltas),
    }


def calibration_proposal_error(proposal: Mapping[str, Any] | None) -> str:
    data = _mapping(proposal)
    if _text(data.get("schema")) != CALIBRATION_PROPOSAL_SCHEMA:
        return "calibration_proposal_schema_required"
    if _text(data.get("owner")) != CALIBRATION_PROPOSAL_OWNER:
        return "calibration_proposal_owner_mismatch"
    if not _text(data.get("proposal_id")):
        return "calibration_proposal_id_required"
    scope_error = evidence_scope_error(_mapping(data.get("evidence_scope")))
    if scope_error:
        return scope_error
    if _text(data.get("status")) != "ready":
        return "calibration_proposal_not_ready"
    deltas = list(data.get("deltas") or [])
    evidence = _mapping(data.get("evidence"))
    observation_ids = sorted(
        {
            _text(value)
            for value in list(evidence.get("observation_ids") or [])
            if _text(value)
        }
    )
    expected_id = _digest(_calibration_proposal_identity(data))
    if _text(data.get("proposal_id")) != expected_id:
        return "calibration_proposal_id_mismatch"
    scope = _mapping(data.get("evidence_scope"))
    formula_vector = _mapping(data.get("formula_vector"))
    if formula_vector != _mapping(scope.get("formula_vector")):
        return "calibration_proposal_formula_vector_mismatch"
    applicability = _mapping(data.get("applicability"))
    if _text(applicability.get("policy")) != "exact_current_runtime":
        return "calibration_proposal_applicability_policy_invalid"
    for key in (
        "evidence_scope_id",
        "identity_code",
        "identity_status",
        "identity_catalog_version",
        "identity_catalog_checksum",
        "session_scope_key",
        "source_epoch",
        "provider_plmn",
        "cell_scope_key",
        "serving_band",
    ):
        if applicability.get(key) != scope.get(key):
            return f"calibration_proposal_applicability_mismatch:{key}"
    if _text(applicability.get("formula_vector_id")) != _text(
        formula_vector.get("formula_vector_id")
    ):
        return "calibration_proposal_applicability_formula_mismatch"
    if _int(evidence.get("observation_count")) != len(observation_ids):
        return "calibration_proposal_evidence_count_mismatch"
    provenance = _mapping(data.get("provenance"))
    if (
        _text(provenance.get("producer")) != CALIBRATION_PROPOSAL_OWNER
        or _text(provenance.get("numeric_formula_epoch"))
        != CALIBRATION_NUMERIC_FORMULA_EPOCH
        or _text(provenance.get("formula_vector_id"))
        != _text(formula_vector.get("formula_vector_id"))
        or _int(provenance.get("source_observation_count")) != len(observation_ids)
    ):
        return "calibration_proposal_provenance_invalid"
    if bool(data.get("changes_policy", False)) != bool(deltas):
        return "calibration_proposal_change_flag_mismatch"
    rollback = _mapping(data.get("rollback"))
    if _text(rollback.get("strategy")) != "restore_previous_committed_generation":
        return "calibration_proposal_rollback_strategy_invalid"
    rollback_values = _mapping(rollback.get("values"))
    if rollback_values != _mapping(data.get("rollback_values")):
        return "calibration_proposal_rollback_projection_mismatch"
    proposed_values = _mapping(data.get("proposed_values"))
    for row in deltas:
        delta = _mapping(row)
        parameter = _text(delta.get("parameter"))
        if parameter != "sinr_feasibility_gate_db":
            return "calibration_proposal_parameter_not_owned"
        if _text(delta.get("formula")) != CALIBRATION_NUMERIC_FORMULA_EPOCH:
            return "calibration_proposal_formula_mismatch"
        rollback = _float(delta.get("rollback"), float("nan"))
        proposed = _float(delta.get("proposed"), float("nan"))
        change = _float(delta.get("delta"), float("nan"))
        if not all(math.isfinite(value) for value in (rollback, proposed, change)):
            return "calibration_proposal_delta_invalid"
        if round(proposed - rollback, 3) != round(change, 3):
            return "calibration_proposal_delta_mismatch"
        if _int(delta.get("evidence_count")) < 4:
            return "calibration_proposal_evidence_insufficient"
        if _float(rollback_values.get(parameter), float("nan")) != rollback:
            return "calibration_proposal_rollback_mismatch"
        if _float(proposed_values.get(parameter), float("nan")) != proposed:
            return "calibration_proposal_value_mismatch"
    return ""


__all__ = [
    "CALIBRATION_NUMERIC_FORMULA_EPOCH",
    "CALIBRATION_OBSERVATION_OWNER",
    "CALIBRATION_OBSERVATION_SCHEMA",
    "CALIBRATION_PROPOSAL_OWNER",
    "CALIBRATION_PROPOSAL_SCHEMA",
    "build_calibration_evidence_scope",
    "build_calibration_observation",
    "build_calibration_proposal",
    "calibration_formula_vector",
    "calibration_observation_error",
    "calibration_proposal_error",
    "evidence_scope_error",
    "evidence_scopes_match",
    "observation_sample",
]
