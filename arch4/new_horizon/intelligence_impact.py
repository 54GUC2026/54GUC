"""Show the measured customer impact of New Horizon decisions.

This module does not grade radio results. It combines the Brain decision,
Matrix transition, and final Referee evidence for customer presentation.
"""

from __future__ import annotations
from arch4.new_horizon.history_presentation import saved_nr_label

import hashlib
import math
from time import localtime, time
from typing import Any, Iterable, Mapping

from arch4.action_origin import is_brain_decision_origin
from arch4.controller_frames import RadioSelectionFrame, RadioTransitionFrame
from arch4.core_contracts.intelligence_boundary import (
    INTELLIGENCE_CONTROL_AUTOPILOT,
)
from arch4.core_contracts.command_targets import core_lte_pband_signature
from arch4.new_horizon.contracts import (
    AdvisoryPublication,
    DECISION_TERMINAL_SCHEMA,
    HOLD_ATTRIBUTION_KIND,
    HOLD_ATTRIBUTION_SCHEMA,
    PROPOSAL_RESULT_EVIDENCE_SCHEMA,
)
from arch4.new_horizon.history import AdvisoryHistory
from arch4.new_horizon.decision_history import (
    forecast_result_attribution,
    DecisionLifecycleHistorySnapshot,
    legacy_deadline_terminal_is_superseded,
)
from arch4.new_horizon.proof import DECISION_LIFECYCLE_TIMEOUT_S, REFEREE_PROOF_EVIDENCE_SCHEMA, RETIRED_LTE_PROOF_SCHEMA, referee_history_vector, current_context_target
from arch4.new_horizon.service_quality import normalized_referee_service_value
from arch4.intelligence_presentation_contract import (
    IMPACT_SCHEMA,
    project_intelligence_display_grade,
)


INTELLIGENCE_IMPACT_SCHEMA = IMPACT_SCHEMA
INTELLIGENCE_IMPACT_WINDOW_S = 14.0 * 24.0 * 60.0 * 60.0
# User-approved reporting reset: 2026-09-16 06:09:29 UTC. Never reset learning.
INTELLIGENCE_IMPACT_STARTED_AT = 1789538969.0
HOLD_VALIDITY_S = 600.0


def _reporting_start(now: float) -> float:
    return max(INTELLIGENCE_IMPACT_STARTED_AT, now - INTELLIGENCE_IMPACT_WINDOW_S)


def empty_intelligence_impact(*, now: float | None = None) -> dict[str, Any]:
    generated_at = float(time() if now is None else now)
    return {
        "schema": INTELLIGENCE_IMPACT_SCHEMA,
        "window_started_at": min(generated_at, _reporting_start(generated_at)),
        "generated_at": generated_at,
        "counts": {
            "improved": 0,
            "neutral": 0,
            "worse": 0,
            "not_proven": 0,
        },
        "forecast_direction": {
            "correct": 0,
            "assessed": 0,
            "accuracy_pct": None,
        },
        "recent_outcomes": [],
        "positive_contexts": [],
        "negative_contexts": [],
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or type(value) not in {int, float}:
        return None
    result = float(value)
    return result if math.isfinite(result) else None

def project_change_grade(
    baseline: Mapping[str, Any], observed: Mapping[str, Any], *, completed_transition: bool,
) -> float | None:
    """Customer-only delta of AQ; never a learning reward or stored IQ."""
    if type(completed_transition) is not bool:
        raise TypeError("intelligence_change_completion_bool_required")
    if not completed_transition:
        return None
    if baseline and observed and baseline["quality_model"] != observed["quality_model"]:
        raise ValueError("intelligence_change_quality_models_mixed")
    old = normalized_referee_service_value(baseline)
    new = normalized_referee_service_value(observed)
    return None if old is None or new is None else round((new - old) * 1.5, 2)


def _selection_signature(selection: RadioSelectionFrame) -> str:
    pool = tuple(selection.configured_band_pool)
    if (
        type(selection.primary_band) is not int
        or selection.primary_band <= 0
        or not pool
        or selection.primary_band not in pool
        or any(type(item) is not int or item <= 0 for item in pool)
    ):
        return ""
    return core_lte_pband_signature(selection.primary_band, pool)


def record_hold_attribution(
    history: AdvisoryHistory,
    *,
    publication: AdvisoryPublication | None,
    transition: RadioTransitionFrame,
    subject_key: str,
    continuity_key: str,
    observed_at: float,
) -> bool:
    """Save a valid HOLD only when an external transition occurs."""

    if not isinstance(history, AdvisoryHistory):
        raise TypeError("intelligence_impact_history_required")
    if not isinstance(transition, RadioTransitionFrame):
        raise TypeError("intelligence_impact_transition_required")
    if publication is None:
        return False
    if not isinstance(publication, AdvisoryPublication):
        raise TypeError("intelligence_impact_publication_required")
    if not publication.context.complete:
        return False
    decision = publication.decision
    if (
        decision.selected_opportunity_id != "do_nothing"
        or decision.reason != "do_nothing_hysteresis_hold"
    ):
        return False
    control = publication.context.product_control.to_mapping()
    if (
        control.get("control_mode") != INTELLIGENCE_CONTROL_AUTOPILOT
        or control.get("control_available") is not True
    ):
        return False
    if (
        publication.context.matrix_identity.namespace
        != transition.matrix_identity.namespace
        or transition.attribution_state != "valid"
        or not transition.fresh
        or transition.source != "lower_controller"
    ):
        return False
    age_s = float(observed_at) - float(publication.published_at)
    if not 0.0 <= age_s <= HOLD_VALIDITY_S:
        return False
    source = current_context_target(publication.context)
    before_signature = _selection_signature(transition.before)
    if (
        source is None
        or not before_signature
        or source.target_signature != before_signature
    ):
        return False
    service_prediction = decision.predicted_grades.component("service_outcome")
    hold_id = hashlib.sha256(
        (
            subject_key
            + "|"
            + source.target_signature
            + "|"
            + format(float(publication.published_at), ".6f")
        ).encode("utf-8")
    ).hexdigest()
    return history.record(
        event_key="hold-attribution:" + subject_key,
        scope_key=continuity_key,
        kind=HOLD_ATTRIBUTION_KIND,
        observed_at=float(observed_at),
        payload={
            "schema": HOLD_ATTRIBUTION_SCHEMA,
            "hold_id": hold_id,
            "subject_key": subject_key,
            "continuity_key": continuity_key,
            "decided_at": float(publication.published_at),
            "expires_at": float(publication.published_at) + HOLD_VALIDITY_S,
            "observed_at": float(observed_at),
            "source_signature": source.target_signature,
            "before_signature": before_signature,
            "after_signature": _selection_signature(transition.after),
            "predicted_service_grade": service_prediction.grade,
            "reason": decision.reason,
            "hold_opportunities": [item.to_mapping() for item in decision.hold_opportunities],
        },
    )


def _negative_hold_is_proven(hold: Mapping[str, Any], proof: Mapping[str, Any]) -> bool:
    """Accept only Brain's frozen eligible alternatives and the actual cell."""
    alternatives = hold.get("hold_opportunities", ())
    if not isinstance(alternatives, (list, tuple)):
        raise ValueError("intelligence_impact_hold_opportunities_invalid")
    for item in alternatives:
        if not isinstance(item, Mapping) or item.get("schema") != "BrainHoldOpportunity.v1":
            raise ValueError("intelligence_impact_hold_opportunity_invalid")
        forecast = item.get("forecast")
        if not isinstance(forecast, Mapping):
            raise ValueError("intelligence_impact_hold_forecast_required")
        decided = _number(hold.get("decided_at"))
        observed = _number(hold.get("observed_at"))
        if (decided is not None and observed is not None
                and 0 <= observed - decided <= HOLD_VALIDITY_S
                and forecast.get("prediction_hour") == localtime(decided).tm_hour
                and forecast.get("prediction_hour") == localtime(observed).tm_hour
                and forecast.get("target_signature") == hold.get("after_signature")
                and forecast_result_attribution(forecast, proof.get("result_identity"))["eligible"]):
            return True
    # Older links have no decision-time alternative evidence. Preserve them
    # for diagnosis, but do not manufacture a missed opportunity from hindsight.
    return False


def _payloads(
    rows: Iterable[Mapping[str, Any]],
    *,
    schema: str,
    continuity_key: str,
    since: float,
    until: float,
) -> tuple[dict[str, Any], ...]:
    accepted: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload")
        observed_at = _number(row.get("observed_at"))
        scope_key = str(row.get("scope_key") or "")
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema") != schema
            or observed_at is None
            or observed_at < since
            or observed_at > until
            or scope_key != continuity_key
        ):
            continue
        current = dict(payload)
        current["_observed_at"] = observed_at
        accepted.append(current)
    return tuple(accepted)


def _identity_key(identity: object) -> str:
    if not isinstance(identity, Mapping):
        return "not retained"
    network = str(identity.get("network_type") or "")
    if network == "NR SA (5G)":
        band = identity.get("nr_band")
        cell = identity.get("nr_cell_id")
        pci = identity.get("nr_pci")
        return f"n{band}/---/{cell}/{pci}"
    pband = identity.get("pband")
    pool = identity.get("ca_pool")
    cell = identity.get("cell_id")
    pci = identity.get("pci")
    if type(pband) is not int or not isinstance(pool, (list, tuple)):
        return "not retained"
    pool_text = "+".join(f"B{item}" for item in pool if type(item) is int)
    return f"B{pband}/{pool_text or '---'}/{cell or '---'}/{pci if type(pci) is int else '---'}"


def build_intelligence_impact(
    *,
    continuity_key: str,
    proof_records: Iterable[Mapping[str, Any]],
    decision_history: DecisionLifecycleHistorySnapshot,
    now: float | None = None,
) -> dict[str, Any]:
    """Build one bounded customer-impact view from accepted evidence."""

    if type(continuity_key) is not str or not continuity_key:
        raise ValueError("intelligence_impact_continuity_required")
    if (
        not isinstance(decision_history, DecisionLifecycleHistorySnapshot)
        or decision_history.continuity_key != continuity_key
    ):
        raise ValueError("intelligence_impact_decision_history_scope_mismatch")
    generated_at = float(time() if now is None else now)
    since = _reporting_start(generated_at)
    retained_proofs = tuple(proof_records)
    proofs = tuple(proof for schema in (REFEREE_PROOF_EVIDENCE_SCHEMA, RETIRED_LTE_PROOF_SCHEMA)
                   for proof in _payloads(retained_proofs, schema=schema,
                       continuity_key=continuity_key, since=since, until=generated_at))
    decisions: dict[str, dict[str, Any]] = {}
    for row in decision_history.attributable_decisions:
        observed_at = _number(row.get("observed_at"))
        payload = row.get("payload")
        if (
            observed_at is None
            or observed_at < max(INTELLIGENCE_IMPACT_STARTED_AT, since - DECISION_LIFECYCLE_TIMEOUT_S)
            or observed_at > generated_at
            or row.get("scope_key") != continuity_key
            or not isinstance(payload, Mapping)
        ):
            continue
        proposal_id = str(payload.get("proposal_id") or "")
        if proposal_id:
            decisions[proposal_id] = {**dict(payload), "_observed_at": observed_at}
    terminals: dict[str, dict[str, Any]] = {}
    for record in decision_history.terminals:
        observed_at = _number(record.get("observed_at"))
        payload = record.get("payload")
        if (
            observed_at is None
            or observed_at < since
            or observed_at > generated_at
            or record.get("scope_key") != continuity_key
            or not isinstance(payload, Mapping)
            or payload.get("schema") != DECISION_TERMINAL_SCHEMA
        ):
            continue
        proposal_id = str(payload.get("proposal_id") or "")
        if proposal_id:
            terminals[proposal_id] = dict(record)
    rejected = {
        str(row.get("proposal_id") or "")
        for row in _payloads(
            decision_history.proposal_results,
            schema=PROPOSAL_RESULT_EVIDENCE_SCHEMA,
            continuity_key=continuity_key,
            since=since,
            until=generated_at,
        )
        if str(row.get("proposal_id") or "") and row.get("submitted") is False
    }
    holds = {
        str(row.get("subject_key") or ""): row
        for row in _payloads(
            decision_history.holds,
            schema=HOLD_ATTRIBUTION_SCHEMA,
            continuity_key=continuity_key,
            since=max(INTELLIGENCE_IMPACT_STARTED_AT, since - HOLD_VALIDITY_S),
            until=generated_at,
        )
        if str(row.get("subject_key") or "")
        and (_number(row.get("decided_at")) is not None
             and INTELLIGENCE_IMPACT_STARTED_AT <= row["decided_at"] <= generated_at)
    }

    events: list[dict[str, Any]] = []
    invalid_decisions: set[str] = set()
    unproven_holds: set[str] = set()
    forecast_correct = 0
    forecast_total = 0
    superseded_terminals: set[str] = set()
    for proof in proofs:
        if (
            proof.get("dimension") != "service_outcome"
            or proof.get("assessment_mode") != "transition"
            or proof.get("final_assessment") is not True
        ):
            continue
        decision_key = str(proof.get("decision_id") or "")
        proposal_id = decision_key.removeprefix("core-command:")
        if (
            is_brain_decision_origin(proof.get("origin"))
            and proposal_id in decisions
            and proof.get("status") == "invalid"
        ):
            invalid_decisions.add(proposal_id)
            continue
        absolute_grade = _number(proof.get("grade"))
        if proof.get("status") != "graded" or absolute_grade is None:
            continue
        grade = project_change_grade(
            referee_history_vector(proof, "baseline_vector"), referee_history_vector(proof, "observed_vector"),
            completed_transition=True,
        )
        if grade is None:
            if is_brain_decision_origin(proof.get("origin")):
                invalid_decisions.add(proposal_id)
            continue
        attribution = ""
        impact_grade = grade
        decided_at = 0.0
        if is_brain_decision_origin(proof.get("origin")) and proposal_id in decisions:
            terminal = terminals.get(proposal_id)
            if terminal is not None:
                if not legacy_deadline_terminal_is_superseded(
                    terminal_record=terminal,
                    proof_payload=proof,
                ):
                    raise ValueError(
                        "intelligence_impact_completed_decision_terminal_conflict"
                    )
                superseded_terminals.add(proposal_id)
            attribution = "brain_action"
            decided_at = float(decisions[proposal_id]["_observed_at"])
            frozen_forecast = decisions[proposal_id].get("forecast")
            cell_result = forecast_result_attribution(
                frozen_forecast, proof.get("result_identity"))
            if not cell_result["eligible"]:
                invalid_decisions.add(proposal_id)
                continue
            if (
                isinstance(frozen_forecast, Mapping)
                and frozen_forecast.get("status") == "predicted"
                and str(frozen_forecast.get("target_signature") or "")
            ):
                forecast_total += 1
                # Direction is public change evidence, separate from AQ
                # prediction accuracy used by the advisers.
                expected_aq = cell_result["expected_aq"]
                before = normalized_referee_service_value(referee_history_vector(proof, "baseline_vector"))
                if expected_aq is None or before is None:
                    forecast_total -= 1
                else:
                    expected_delta = project_intelligence_display_grade((expected_aq - before * 3.0) / 2.0, dimension="service_change")
                    actual_delta = project_intelligence_display_grade(grade, dimension="service_change")
                    forecast_correct += int((expected_delta > 0) == (actual_delta > 0)
                                            and (expected_delta < 0) == (actual_delta < 0))
        elif (
            proof.get("subject_kind") == "external_radio_change"
            and decision_key in holds
        ):
            if grade > 0.0 and not _negative_hold_is_proven(holds[decision_key], proof):
                unproven_holds.add(decision_key)
                continue
            attribution = "validated_hold"
            impact_grade = -grade
            decided_at = float(holds[decision_key]["decided_at"])
        else:
            continue
        displayed_impact_grade = project_intelligence_display_grade(impact_grade, dimension="service_change")
        if displayed_impact_grade is None:
            continue
        events.append(
            {
                "assessment_id": str(proof["assessment_id"]),
                "attribution": attribution,
                "decided_at": decided_at,
                "completed_at": float(proof["closed_at"]),
                "before": dict(proof.get("baseline_identity") or {}),
                "after": dict(proof.get("result_identity") or {}),
                "before_text": _identity_key(proof.get("baseline_identity")),
                "after_text": _identity_key(proof.get("result_identity")),
                "before_nr": saved_nr_label(proof.get("baseline_identity") or {}, referee_history_vector(proof, "baseline_vector")),
                "after_nr": saved_nr_label(proof.get("result_identity") or {}, referee_history_vector(proof, "observed_vector")),
                "baseline_vector": referee_history_vector(proof, "baseline_vector"),
                "observed_vector": referee_history_vector(proof, "observed_vector"),
                "observed_service_grade": absolute_grade,
                "impact_grade": displayed_impact_grade,
            }
        )

    terminal_ids = set(terminals) - superseded_terminals
    not_proven = len(
        ((terminal_ids | rejected | invalid_decisions) & set(decisions))
    ) + len(unproven_holds)
    counts = {
        "improved": sum(event["impact_grade"] > 0.0 for event in events),
        "neutral": sum(event["impact_grade"] == 0.0 for event in events),
        "worse": sum(event["impact_grade"] < 0.0 for event in events),
        "not_proven": not_proven,
    }
    grouped: dict[tuple[str, str], list[float]] = {}
    for event in events:
        context = (
            (event["before_text"], event["before_nr"])
            if event["attribution"] == "validated_hold"
            else (event["after_text"], event["after_nr"])
        )
        grouped.setdefault(context, []).append(float(event["impact_grade"]))
    ranked = sorted(
        (
            {
                "context": context[0],
                "nr": context[1],
                "mean_impact_grade": sum(grades) / len(grades),
                "completed_outcomes": len(grades),
            }
            for context, grades in grouped.items()
        ),
        key=lambda row: (-float(row["mean_impact_grade"]), str(row["context"])),
    )
    events.sort(key=lambda row: float(row["completed_at"]))
    return {
        "schema": INTELLIGENCE_IMPACT_SCHEMA,
        "window_started_at": min(generated_at, since),
        "generated_at": generated_at,
        "counts": counts,
        "forecast_direction": {
            "correct": forecast_correct,
            "assessed": forecast_total,
            "accuracy_pct": (
                round(100.0 * forecast_correct / forecast_total, 1)
                if forecast_total
                else None
            ),
        },
        "recent_outcomes": events[-32:],
        "positive_contexts": [row for row in ranked if row["mean_impact_grade"] > 0.0][
            :3
        ],
        "negative_contexts": [
            row for row in reversed(ranked) if row["mean_impact_grade"] < 0.0
        ][:3],
    }


__all__ = (
    "HOLD_VALIDITY_S",
    "INTELLIGENCE_IMPACT_SCHEMA",
    "INTELLIGENCE_IMPACT_WINDOW_S",
    "build_intelligence_impact",
    "project_change_grade",
    "empty_intelligence_impact",
    "record_hold_attribution",
)
