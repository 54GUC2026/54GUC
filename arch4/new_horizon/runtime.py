"""Run New Horizon through the Product Matrix boundary."""

from __future__ import annotations

from collections import deque
import logging
import math
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from time import localtime, monotonic, strftime, time
from typing import Any, Mapping

from arch4.action_origin import (
    is_brain_decision_origin,
    require_action_origin_name,
)
from arch4.architecture_contracts.application_lifecycle import (
    ProductStartupReceipt,
)
from arch4.core_contracts.intelligence_boundary import (
    INTELLIGENCE_CONTROL_AUTOPILOT,
    INTELLIGENCE_CONTROL_OBSERVE,
    INTELLIGENCE_CONTROL_STANDALONE,
    IntelligenceHealthSnapshot,
    IntelligenceMatrixInputSnapshot,
    IntelligencePresentationSnapshot,
)
from arch4.new_horizon.advisory import AdvisoryEngine
from arch4.atomic_io import atomic_write_json
from arch4.new_horizon.capacity import ExploreCapacityProbe
from arch4.new_horizon.proof import context_radio_identity
from arch4.new_horizon.service_quality import normalized_referee_service_value
from arch4.new_horizon.intelligence_impact import project_change_grade
from arch4.new_horizon.history_presentation import saved_nr_label
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.intelligence_presentation_contract import (
    FORECAST_SCHEMA,
    FORECAST_SOURCE_ID,
    IMPACT_SCHEMA,
    IMPACT_SOURCE_ID,
    LEARNING_CYCLE_HISTORY_SCHEMA,
    LEARNING_CYCLE_HISTORY_SOURCE_ID,
    NEW_HORIZON_NGUI_SOURCE_IDS,
    REFEREE_SUMMARY_SCHEMA,
    REFEREE_SUMMARY_SOURCE_ID,
    project_intelligence_display_grade,
    action_actor_label,
    saved_nr_identity_text,
)


log = logging.getLogger(__name__)
_RECOVERY_INTERVAL_S = 0.025
_INPUT_RETRY_INTERVAL_S = 0.25
_DIAGNOSTIC_INTERVAL_S = 30.0
_STATUS_MESSAGE_LIMIT = 20
_OPERATOR_FAULT_THRESHOLD = 3
_OPERATOR_RECOVERY_THRESHOLD = 5
_AUTONOMOUS_OUTCOME_ORIGINS = frozenset({
    "smart_loop", "explore", "fault_recovery", "schedule", "watchdog",
    "lease_expiry",
})


def _learning_result_parts(baseline, observed, aq):
    """Project new values and comparison tones; retain the existing AQ owner."""
    old_aq = normalized_referee_service_value(baseline)
    parts = []
    for label, before, after, digits, unit, higher_better in (
        ("ECI", baseline.get("eci_points"), observed.get("eci_points"), 1, "", True),
        ("Lat", baseline.get("latency_ms"), observed.get("latency_ms"), 0, " ms", False),
        ("Jit", baseline.get("jitter_ms"), observed.get("jitter_ms"), 1, " ms", False),
        ("CQ", baseline.get("cqb_cell"), observed.get("cqb_cell"), 2, "", True),
        ("AQ", None if old_aq is None else old_aq * 3.0, aq, 1, "", True),
    ):
        valid = lambda v: type(v) in {int, float} and math.isfinite(v)
        new = round(float(after), digits) if valid(after) else None
        old = round(float(before), digits) if valid(before) else None
        text = "N/A" if new is None else format(new, ("+" if label == "AQ" else "") + f".{digits}f") + unit
        outcome = "unavailable"
        if new is not None and old is not None:
            outcome = "neutral" if new == old else "improved" if (new > old) == higher_better else "worse"
        parts.append({"text": f"{label} {text}", "outcome": outcome})
    return parts

_DECISION_REASON_TEXT = {
    "current_context_non_actionable": "Keeping the current connection; no eligible change is justified.",
    "current_context_incomplete_no_action": "Waiting for complete live connection facts.",
    "mandatory_safe_alternative": "A proven safer alternative is available.",
    "active_user_traffic": "User traffic is active; the current connection is protected.",
    "qualified_primary_destination_history": "History is available for this primary band.",
    "three_same_scope_samples_required": "Collecting enough matching observations.",
    "bounded_same_scope_observation_forecast": "Forecast uses the current connection history.",
    "scoped_bandit_learned_brain_selection": (
        "Selected using completed assessments for this exact context."
    ),
    "scoped_bandit_prior_brain_selection": (
        "Selected using starting provider knowledge; direct evidence is still limited."
    ),
    "scoped_bandit_unknown_brain_selection": (
        "Selected as the safest current option while direct evidence is limited."
    ),
    "user_authorized_explore_brain_selection": (
        "Explore selected this option from the user-authorized test plan."
    ),
    "brain_safe_idle_information_gain": (
        "Traffic is quiet, so Brain selected one untested legal option."
    ),
    "brain_historical_pool_rescue": (
        "Internet is unavailable; add one proven band to restore service."
    ),
    "brain_wait_next_hour": (
        "Keep this connection for now; recheck the better option when the next hour starts."
    ),
}

def _operator_reason_text(value: object) -> str:
    """Return operator text without exposing internal identifiers."""

    reason = str(value or "").strip()
    if reason in _DECISION_REASON_TEXT:
        return _DECISION_REASON_TEXT[reason]
    if reason.startswith("current_context_incomplete:"):
        return "Waiting for complete live connection facts."
    if reason == "do_nothing_hysteresis_hold":
        return "Keeping the current connection within the stability margin."
    if reason.startswith("brain_candidate_beats_do_nothing_margin="):
        return "A better option cleared its disruption and uncertainty cost."
    return "Details are available in the support log."


def _operator_action_actor(origin: object) -> str:
    try:
        name = require_action_origin_name(origin)
        if name not in _AUTONOMOUS_OUTCOME_ORIGINS:
            raise ValueError("not_an_autonomous_outcome")
        return action_actor_label(name)
    except ValueError as exc:
        raise ValueError("new_horizon_action_origin_unclassified") from exc


def _operator_advisory_text(value: object) -> str:
    """Describe the selected opportunity without exposing its internal key."""

    advisory = str(value or "").strip()
    if advisory == "do_nothing":
        return "Keep the current connection"
    if advisory.startswith("learned_lte:"):
        return "A learned band combination is available."
    if advisory.startswith("lte_anchor:B"):
        return "Consider " + advisory.removeprefix("lte_anchor:") + " as the primary band."
    if advisory.startswith("best_effort_anchor:B"):
        return (
            "Test "
            + advisory.removeprefix("best_effort_anchor:")
            + " as primary while keeping other LTE bands allowed."
        )
    if advisory.startswith("best_effort_expand:B"):
        return (
            "Add "
            + advisory.removeprefix("best_effort_expand:")
            + " as the best eligible CA companions."
        )
    if advisory.startswith("emergency_pool_rescue:"):
        return "Restore Internet using all allowed LTE bands."
    if advisory.startswith("explore:"):
        return "Explore is evaluating another band combination."
    return "No user action is required."


def _history_band(signature: object) -> str:
    """Return the primary radio band in a form the operator can recognize."""

    value = str(signature or "").strip()
    if value.startswith("PBAND=B"):
        return value.split("|", 1)[0].removeprefix("PBAND=")
    if value.startswith("NR=n"):
        return value.removeprefix("NR=")
    return "---"


def _history_ca_pool(signature: object) -> str:
    """Return a target that changes only the CA pool."""

    value = str(signature or "").strip()
    if value.startswith("CA="):
        value = value.removeprefix("CA=")
    bands = value.split("+") if value else []
    if not bands or any(
        not band.startswith("B")
        or not band[1:].isdigit()
        or int(band[1:]) <= 0
        for band in bands
    ):
        return ""
    if len(set(bands)) != len(bands):
        return ""
    return "+".join(bands)


def _history_lte_selection(
    signature: object,
    identity: object,
) -> tuple[int, tuple[int, ...]] | None:
    """Return the exact LTE selection used only to explain a saved change."""

    values = identity if isinstance(identity, Mapping) else {}
    exact_signature = str(values.get("target_signature") or signature or "").strip()
    pband = values.get("pband")
    pool = values.get("ca_pool")
    if type(pband) is not int and exact_signature.startswith("PBAND=B"):
        raw = _history_band(exact_signature).removeprefix("B")
        pband = int(raw) if raw.isdigit() else None
    if not isinstance(pool, (list, tuple)) and "|CA=" in exact_signature:
        raw_pool = exact_signature.split("|CA=", 1)[1]
        parsed = _history_ca_pool(raw_pool)
        pool = (
            tuple(int(item.removeprefix("B")) for item in parsed.split("+"))
            if parsed
            else ()
        )
    if (
        type(pband) is not int
        or pband <= 0
        or not isinstance(pool, (list, tuple))
    ):
        return None
    if any(type(band) is not int for band in pool):
        return None
    normalized_pool = tuple(pool)
    if (
        not normalized_pool
        or any(band <= 0 for band in normalized_pool)
        or len(set(normalized_pool)) != len(normalized_pool)
        or pband not in normalized_pool
    ):
        return None
    return pband, normalized_pool


def _history_learning_target_label(
    *,
    source_signature: object,
    target_signature: object,
    baseline_identity: object,
    result_identity: object,
) -> str:
    """Name the changed radio part instead of guessing from the destination."""

    before = _history_lte_selection(source_signature, baseline_identity)
    after = _history_lte_selection(target_signature, result_identity)
    if before is not None and after is not None:
        before_band, before_pool = before
        after_band, after_pool = after
        band_changed = before_band != after_band
        pool_changed = before_pool != after_pool
        pool_text = "+".join(f"B{band}" for band in after_pool)
        if pool_changed and not band_changed:
            return pool_text + " CA pool"
        if band_changed and not pool_changed:
            return f"B{after_band}"
        return f"B{after_band}/{pool_text} configuration"

    before_nr = str(
        (baseline_identity if isinstance(baseline_identity, Mapping) else {}).get(
            "target_signature"
        )
        or source_signature
        or ""
    ).strip()
    after_nr = str(
        (result_identity if isinstance(result_identity, Mapping) else {}).get(
            "target_signature"
        )
        or target_signature
        or ""
    ).strip()
    if after_nr.startswith("NR=n"):
        band = _history_band(after_nr)
        return band if before_nr != after_nr else band + " configuration"

    if after is not None:
        band, bands = after
        pool_text = "+".join(f"B{value}" for value in bands)
        return f"B{band}/{pool_text} configuration"

    # A CA-only command can retain a compact target signature while the factual
    # identities carry the complete selection. Keep this branch for typed old
    # assessments that have not yet gained a complete target signature.
    pool = _history_ca_pool(target_signature)
    if pool and before is not None:
        return pool + " CA pool"
    raise ValueError("new_horizon_learning_transition_unrecognized")


def _forecast_candidate_radio(
    forecast: object,
    *,
    current_identity: Mapping[str, Any],
    result_context: object | None = None,
) -> dict[str, str]:
    """Project named display fields from the selected target and evidence."""

    signature = str(getattr(forecast, "target_signature", "") or "").strip()
    evidence = getattr(result_context, "hourly_evidence", ()) if result_context is not None else ()

    def selected_number(name: str) -> int | None:
        if result_context is not None:
            values = {row["identity"].get(name) for row in evidence}
            value = next(iter(values)) if len(values) == 1 else None
        elif signature == str(current_identity.get("target_signature") or ""):
            value = current_identity.get(name)
        else:
            value = None
        return value if type(value) is int else None

    nr_target = signature.startswith("NR=n")
    if result_context is not None:
        cell_id = getattr(result_context, "evidence_nr_cell_id" if nr_target else "evidence_cell_id", None)
        pci = getattr(result_context, "evidence_nr_pci" if nr_target else "evidence_pci", None)
    elif signature == str(current_identity.get("target_signature") or ""):
        cell_id = current_identity.get("nr_cell_id" if nr_target else "cell_id")
        pci = current_identity.get("nr_pci" if nr_target else "pci")
    else:
        cell_id = getattr(forecast, "evidence_nr_cell_id" if nr_target else "evidence_cell_id", None)
        pci = getattr(forecast, "evidence_nr_pci" if nr_target else "evidence_pci", None)
    earfcn = None if nr_target else selected_number("earfcn")
    if signature.startswith("PBAND=B"):
        parts = {
            key: value
            for item in signature.split("|")
            if "=" in item
            for key, value in (item.split("=", 1),)
        }
        band = parts.get("PBAND", "---")
        ca_pool = parts.get("CA", "---")
    elif nr_target:
        band = signature.split("|", 1)[0].removeprefix("NR=")
        ca_pool = "---"
    elif ca_pool := _history_ca_pool(signature):
        # A pool request does not choose the primary band. Only the selected
        # physical outcome can supply it; never borrow another live context.
        pband = selected_number("pband")
        band = f"B{pband}" if pband is not None else "---"
    else:
        band = ca_pool = "---"
        cell_id = pci = None
    return {
        "band": band,
        "ca_pool": ca_pool,
        "cell_id": str(cell_id) if type(cell_id) is int else "---",
        "pci": str(pci) if type(pci) is int else "---",
        "earfcn": str(earfcn) if earfcn is not None else "---",
    }


def _forecast_candidate_nr(forecast: object, *, result_context: object | None = None) -> str:
    """Project the selected evidence condition, separately from the LTE target."""

    evidence = getattr(result_context, "hourly_evidence", ()) if result_context is not None else ()
    if evidence:
        from arch4.new_horizon.hourly_aq import nr_evidence_condition
        conditions = {nr_evidence_condition(row["identity"]) for row in evidence}
        if len(conditions) != 1:
            raise ValueError("forecast_display_mixed_nr_conditions")
        condition = next(iter(conditions))
    else:
        condition = getattr(forecast, "nr_condition", None)
    if condition and condition != ("no_nr",):
        return condition[1]
    if condition == ("no_nr",) and getattr(forecast, "nr_device", False):
        return "NR off"
    return "---"


def _history_context(
    signature: object,
    identity: object,
) -> str | None:
    """Return a compact, complete Referee radio context for Page 7."""

    value = str(signature or "").strip()
    values = identity if isinstance(identity, Mapping) else {}
    if not any(
        key in values for key in ("target_signature", "pband", "nr_band")
    ):
        return None
    exact_signature = str(values.get("target_signature") or value).strip()
    nr_band = values.get("nr_band")
    if type(nr_band) is not int and exact_signature.startswith("NR=n"):
        raw_band = exact_signature.split("|", 1)[0].removeprefix("NR=n")
        nr_band = int(raw_band) if raw_band.isdigit() else None
    if exact_signature.startswith("NR=n"):
        from arch4.core_contracts.nr_identity import nr_identity_values_valid
        if not nr_identity_values_valid(band=nr_band, arfcn=values.get("nr_arfcn"),
                                        pci=values.get("nr_pci"), nci=values.get("nr_cell_id")):
            return None
        band_text = f"n{nr_band}"
        pool_text = f"ARFCN {values['nr_arfcn']}"
        cell = values.get("nr_cell_id")
        pci = values.get("nr_pci")
        return "/".join((band_text, pool_text, "---" if cell is None else str(cell), str(pci)))
    else:
        pband = values.get("pband")
        pool = values.get("ca_pool")
        if type(pband) is not int and exact_signature.startswith("PBAND=B"):
            band = _history_band(exact_signature).removeprefix("B")
            pband = int(band) if band.isdigit() else None
        if not isinstance(pool, (list, tuple)) and "|CA=" in exact_signature:
            raw_pool = exact_signature.split("|CA=", 1)[1]
            parsed_pool = _history_ca_pool(raw_pool)
            pool = (
                tuple(int(item.removeprefix("B")) for item in parsed_pool.split("+"))
                if parsed_pool
                else ()
            )
        pool = tuple(pool) if isinstance(pool, (list, tuple)) else ()
        if (
            type(pband) is not int
            or pband <= 0
            or not pool
            or any(type(band) is not int or band <= 0 for band in pool)
            or pband not in pool
        ):
            return None
        band_text = "B" + str(pband)
        pool_text = "+".join(
            "B" + str(band) for band in pool
        )
        cell = values.get("cell_id")
        pci = values.get("pci")
    if (
        type(cell) is not int
        or cell <= 0
        or type(pci) is not int
        or pci < 0
    ):
        return None
    return "/".join(
        (
            band_text,
            pool_text,
            str(cell),
            str(pci),
        )
    )


def _history_explanation(value: object) -> str:
    """Translate the saved Brain reason into short operator text."""

    text = str(value or "").strip()
    lowered = text.lower()
    if not text or "prior release" in lowered or "evidence unavailable" in lowered:
        return "Reason not retained."
    if text.startswith("Keep the current connection:"):
        return "No better qualified target is known."
    if text.startswith("Explore selected"):
        return "User requested this test."
    if text.startswith("Test the unknown target:"):
        return "Testing an unproven band."
    if text.startswith(("Select the target:", "Select using hourly cell support:", "Select the recovery target:")):
        if "completed target evidence" in text:
            return "Completed evidence predicts improvement."
        if "forecast" in text:
            return "Forecast predicts improvement."
        if "provider prior" in text:
            return "Provider history favors this band."
        return "Live conditions favor this band."
    if text in {"Details unavailable.", "Reason not retained."}:
        return "Reason not retained."
    return "Decision evidence retained."


def _intelligence_state_text(control_mode: object) -> str:
    """Describe PIM control without contradicting Autopilot state."""

    mode = str(control_mode or "").strip()
    if mode == INTELLIGENCE_CONTROL_AUTOPILOT:
        return "Brain active"
    if mode == INTELLIGENCE_CONTROL_OBSERVE:
        return "Observe only"
    if mode == INTELLIGENCE_CONTROL_STANDALONE:
        return "CGF standalone"
    return "Brain state unavailable"


def _learning_text(
    learned_proof_count: int,
) -> str:
    """Show the useful global learning fact without interpretation."""

    return (
        f"{learned_proof_count} final assessment"
        + ("s" if learned_proof_count != 1 else "")
        + " available."
        if learned_proof_count
        else "No final assessments available yet."
    )


def _forecast_presentation(
    forecast: object,
    *,
    candidates: tuple[object, ...],
    selected_opportunity_id: str,
    current_identity: Mapping[str, Any],
) -> dict[str, object]:
    """Show selected and candidate forecasts in one frame."""

    def projection(value: object) -> dict[str, object]:
        predicted = str(getattr(value, "status", "")) == "predicted"
        match_tier = str(getattr(value, "match_tier", "") or "")
        comparison, meaning = {
            "exact": (
                "Exact-context comparison",
                "Same selected option, radio context and clock hour.",
            ),
            "related_context": (
                "Related-context comparison",
                "Same selected option and clock hour; radio context differs.",
            ),
        }.get(
            match_tier if predicted else "",
            (
                "Collecting evidence",
                "No comparable completed assessment for this selected option yet.",
            ),
        )

        def metric(name: str) -> float | None:
            item = getattr(value, name, None)
            return float(item) if type(item) in {int, float} else None

        return {
            "state": "predicted" if predicted else "collecting",
            "comparison": comparison,
            "meaning": meaning,
            "metrics": {
                "capacity_points": metric("expected_eci_points"),
                "latency_ms": metric("expected_latency_ms"),
                "jitter_ms": metric("expected_jitter_ms"),
                "communication_quality": metric("expected_cqb"),
                "internet_availability_pct": metric(
                    "expected_internet_availability_pct"
                ),
                "estimated_aq": metric("expected_aq"),
            },
            "evidence_count": max(
                0,
                int(
                    getattr(
                        value,
                        "evidence_samples",
                        getattr(value, "evidence_assessments", 0),
                    )
                    or 0
                ),
            ),
            "confidence_pct": max(
                0.0,
                min(100.0, float(getattr(value, "confidence", 0.0)) * 100.0),
            ),
            "accuracy_pct": (
                float(value.accuracy_factor) * 100.0
                if getattr(value, "accuracy_factor", None) is not None
                else None
            ),
            "accuracy_assessments": max(
                0,
                int(getattr(value, "accuracy_assessments", 0) or 0),
            ),
        }

    candidate_groups: list[
        tuple[tuple[int, float, int, int], list[dict[str, object]]]
    ] = []
    for candidate_index, item in enumerate(candidates):
        result_contexts = tuple(getattr(item, "result_contexts", ()) or ())
        variants: tuple[object | None, ...] = result_contexts or (None,)
        selected_target = item.opportunity_id == selected_opportunity_id
        rows: list[dict[str, object]] = []
        for variant_index, variant in enumerate(variants):
            projected = projection(variant if variant is not None else item)
            service = getattr(item, "service_evidence", None)
            evidence_state = service["state"] if service is not None else None
            if evidence_state in {"NW", "NE"}:
                projected["meaning"] = (
                    "NW: observed not working in this clock hour."
                    if evidence_state == "NW" else "NE: no usable observation for this clock hour."
                )
            role = (
                "selected"
                if selected_target and variant_index == 0
                else "outcome"
                if variant_index > 0
                else evidence_state.lower()
                if evidence_state in {"NW", "NE"}
                else "unproven"
                if projected["state"] == "collecting"
                else "option"
            )
            rows.append(
                {
                    **_forecast_candidate_radio(
                        item,
                        current_identity=current_identity,
                        result_context=variant,
                    ),
                    "nr": _forecast_candidate_nr(item, result_context=variant),
                    "role": role,
                    **projected,
                }
            )
        predicted = any(row["state"] == "predicted" for row in rows)
        rank = 0 if selected_target else 1 if predicted else 2
        confidence = max(float(row["confidence_pct"]) for row in rows)
        evidence = max(int(row["evidence_count"]) for row in rows)
        candidate_groups.append(
            ((rank, -confidence, -evidence, candidate_index), rows)
        )

    candidate_rows = [
        row
        for _key, rows in sorted(candidate_groups, key=lambda item: item[0])[:8]
        for row in rows
    ]

    selected = projection(forecast)
    return {
        "schema": FORECAST_SCHEMA,
        **selected,
        "candidates": candidate_rows,
    }


def _learning_cycle_history(
    rows: tuple[Mapping[str, Any], ...],
    *,
    intelligence_impact: Mapping[str, Any],
) -> dict[str, object]:
    """Project exact completed learning proof; retain debris elsewhere."""

    dimensions = ("service_outcome",)

    def predicted(mapping: object) -> dict[str, float | None]:
        result = dict.fromkeys(dimensions)
        if not isinstance(mapping, Mapping):
            return result
        components = mapping.get("components")
        if not isinstance(components, list):
            return result
        for component in components:
            if not isinstance(component, Mapping):
                continue
            dimension = component.get("dimension")
            grade = component.get("grade")
            if dimension in result and (
                grade is None
                or (
                    not isinstance(grade, bool)
                    and type(grade) in {int, float}
                )
            ):
                result[str(dimension)] = (
                    float(grade) if grade is not None else None
                )
        return result

    def grade_text(
        grades: Mapping[str, float | None],
        *,
        label: str,
        unavailable: str,
    ) -> str:
        grade = grades.get("service_outcome")
        displayed = project_intelligence_display_grade(grade,dimension="service_outcome")
        return (
            f"{label}: {displayed:+.1f}"
            if displayed is not None
            else unavailable
        )

    if intelligence_impact.get("schema") != IMPACT_SCHEMA:
        raise ValueError("new_horizon_learning_impact_schema_invalid")
    recent_outcomes = intelligence_impact.get("recent_outcomes")
    if not isinstance(recent_outcomes, list):
        raise ValueError("new_horizon_learning_impact_outcomes_required")
    impact_by_assessment: dict[str, Mapping[str, Any]] = {}
    for impact in recent_outcomes:
        if not isinstance(impact, Mapping):
            raise ValueError("new_horizon_learning_impact_outcome_invalid")
        assessment_id = str(impact.get("assessment_id") or "")
        if not assessment_id or assessment_id in impact_by_assessment:
            raise ValueError("new_horizon_learning_impact_identity_invalid")
        impact_by_assessment[assessment_id] = impact

    projected = []
    for row in sorted(rows, key=lambda item: float(item["decided_at"])):
        referee_state = str(row["referee_state"])
        if referee_state != "complete":
            raise ValueError("new_horizon_customer_learning_proof_not_complete")
        before_context = _history_context(
            row.get("source_signature"),
            row.get("baseline_identity"),
        )
        after_context = _history_context(
            row["target_signature"],
            row.get("result_identity"),
        )
        if before_context is None or after_context is None:
            raise ValueError("new_horizon_complete_learning_context_required")
        assessment_id = str(row.get("assessment_id") or "")
        completed_at = row.get("completed_at")
        baseline_vector = row.get("baseline_vector")
        observed_vector = row.get("observed_vector")
        if (
            not assessment_id
            or type(completed_at) not in {int, float}
            or isinstance(completed_at, bool)
            or float(completed_at) < float(row["decided_at"])
            or not isinstance(baseline_vector, Mapping)
            or not isinstance(observed_vector, Mapping)
        ):
            raise ValueError(
                "new_horizon_complete_learning_proof_link_required"
            )
        origin = str(row.get("origin") or "")
        try:
            actor = action_actor_label(origin, allow_external=True)
        except ValueError as exc:
            raise ValueError("new_horizon_learning_cycle_origin_unclassified") from exc
        why = f"{actor} selected this configuration."
        if origin == "lower_controller_during_54guc_command":
            why = "The network selected this result during a request; no Brain credit."
        learned = row["learned_grades"]
        predicted_mapping = row["predicted_grades"]
        predicted_grades = predicted(predicted_mapping)
        cell_result = row.get("result_attribution")
        if is_brain_decision_origin(origin) and isinstance(cell_result, Mapping):
            if cell_result["eligible"]:
                predicted_grades["service_outcome"] = cell_result["expected_aq"]
            else:
                why = (
                    "Brain requested the band; the network selected another cell."
                    if cell_result["reason"] == "different_serving_cell" else
                    "Brain requested the band; a matching cell prediction is unavailable."
                )
        learned_grades = {
            dimension: (
                learned.get(dimension)
                if type(learned.get(dimension)) in {int, float}
                and not isinstance(learned.get(dimension), bool)
                else None
            )
            for dimension in dimensions
        }
        learned_count = sum(value is not None for value in learned_grades.values())
        if referee_state == "complete" and learned_count != len(dimensions):
            raise ValueError("new_horizon_complete_decision_grade_set_required")
        target = _history_learning_target_label(
            source_signature=row.get("source_signature"),
            target_signature=row["target_signature"],
            baseline_identity=row.get("baseline_identity"),
            result_identity=row.get("result_identity"),
        )
        mode = row.get("assessment_mode")
        if mode not in {"transition", "context_quality"}:
            raise ValueError("new_horizon_learning_assessment_mode_required")
        aq = project_intelligence_display_grade(learned_grades["service_outcome"],dimension="service_outcome")
        aq_text = "AQ N/A" if aq is None else f"AQ {aq:+.1f}"
        change_value = project_intelligence_display_grade(project_change_grade(
            baseline_vector, observed_vector, completed_transition=mode == "transition",
        ), dimension="service_change")
        change_text = "Change N/A" if change_value is None else f"Change {change_value:+.1f}"
        actual_grade = aq_text + (" · " + change_text if mode == "transition" else "")
        learning_effect = f"{target}: {aq_text}."
        next_consequence = (
            "Hourly AQ guides future choices."
            if aq is not None else "No qualified absolute quality available."
        )
        linked_impact = impact_by_assessment.get(assessment_id)
        before_nr = saved_nr_label(row["baseline_identity"], baseline_vector)
        after_nr = saved_nr_label(row["result_identity"], observed_vector)
        grade_parts = [{
            "text": aq_text,
            "outcome": "unavailable" if aq is None else
            "improved" if aq > 0 else "worse" if aq < 0 else "neutral",
        }]
        if mode == "transition":
            grade_parts.append({
                "text": change_text,
                "outcome": "unavailable" if change_value is None else
                "improved" if change_value > 0 else "worse" if change_value < 0 else "neutral",
            })
        impact_result = None
        if linked_impact is not None:
            if (
                float(linked_impact["completed_at"]) != float(completed_at)
                or str(linked_impact["before_text"]) != before_context
                or str(linked_impact["after_text"]) != after_context
                or linked_impact["before_nr"] != before_nr
                or linked_impact["after_nr"] != after_nr
                or dict(linked_impact["baseline_vector"])
                != dict(baseline_vector)
                or dict(linked_impact["observed_vector"])
                != dict(observed_vector)
                or float(linked_impact["observed_service_grade"])
                != float(learned_grades["service_outcome"])
            ):
                raise ValueError(
                    "new_horizon_learning_impact_proof_mismatch"
                )
            impact_grade = project_intelligence_display_grade(
                linked_impact["impact_grade"], dimension="service_change"
            )
            if impact_grade is None:
                raise ValueError(
                    "new_horizon_learning_impact_grade_required"
                )
            attribution = str(linked_impact["attribution"])
            expected_impact_grade = (
                change_value
                if attribution == "brain_action"
                else -change_value
                if attribution == "validated_hold"
                and change_value is not None
                else None
            )
            if (
                expected_impact_grade is None
                or impact_grade != expected_impact_grade
                or (attribution == "brain_action")
                != is_brain_decision_origin(origin)
                or (
                    attribution == "validated_hold"
                    and origin != "lower_controller"
                )
            ):
                raise ValueError(
                    "new_horizon_learning_impact_attribution_mismatch"
                )
            impact_result = (
                "Decision to stay: "
                if attribution == "validated_hold"
                else "Brain action: "
            ) + f"{impact_grade:+.1f}"
            grade_parts.append({
                "text": impact_result,
                "outcome": "improved" if impact_grade > 0 else "worse" if impact_grade < 0 else "neutral",
            })
        result_parts = _learning_result_parts(baseline_vector, observed_vector, aq)
        projected.append(
            {
                "assessment_id": assessment_id,
                "decided_at": float(row["decided_at"]),
                "completed_at": float(completed_at),
                "before_context": before_context,
                "after_context": after_context,
                "before_nr": before_nr,
                "after_nr": after_nr,
                "why": why,
                "expected": grade_text(
                    predicted_grades,
                    label="Expected AQ",
                    unavailable=(
                        "Not forecast" if origin else "Prediction unavailable"
                    ),
                ) if not isinstance(cell_result, Mapping) or cell_result["eligible"]
                else "Cell prediction not assessed",
                "actual_grade": actual_grade,
                "grade_parts": grade_parts,
                "impact_result": impact_result,
                "result_detail": " · ".join(part["text"] for part in result_parts),
                "result_parts": result_parts,
                "learning_effect": learning_effect,
                "next_consequence": next_consequence,
            }
        )
    return {
        "schema": LEARNING_CYCLE_HISTORY_SCHEMA,
        "rows": projected,
    }


def _referee_summary_presentation(
    band_context: Mapping[str, object],
) -> dict[str, object]:
    """Show the latest completed Referee proof."""

    timeline = tuple(band_context.get("current_timeline") or ())
    current = (
        "Collecting current 10-minute window"
        if timeline
        else "Waiting for Referee evidence"
    )
    recent = tuple(band_context.get("recent_assessments") or ())
    completed = tuple(
        row
        for row in recent
        if isinstance(row, Mapping) and row.get("maturity") == "completed"
    )
    latest = completed[-1] if completed else None
    latest_completed = None
    if latest is not None:
        dimensions = latest.get("dimensions")
        dimensions = dimensions if isinstance(dimensions, Mapping) else {}
        service = dimensions.get("performance_overall_outcome")
        communication = dimensions.get("performance_communication_quality")
        service = service if isinstance(service, Mapping) else {}
        communication = communication if isinstance(communication, Mapping) else {}
        identity_parts = []
        if latest.get("cell_id") is not None:
            identity_parts.append(
                f"Cell {latest['cell_id']} / PCI {latest['pci']} / "
                f"EARFCN {latest['earfcn']}"
            )
        if any(latest.get(name) is not None for name in ("nr_band", "nr_cell_id", "nr_pci", "nr_arfcn")):
            identity_parts.append(f"NR {saved_nr_identity_text(latest)}")
        identity = " + ".join(identity_parts)
        latest_completed = {
            "window": str(latest["label"]),
            "target": str(latest["target"]),
            "identity": identity,
            "service_text": str(service.get("text") or "N/A"),
            "service_grade": service.get("grade"),
            "communication_text": str(communication.get("text") or "N/A"),
            "communication_grade": communication.get("grade"),
        }
    return {
        "schema": REFEREE_SUMMARY_SCHEMA,
        "current": current,
        "latest_completed": latest_completed,
        "history_note": str(band_context.get("history_note") or ""),
    }


def _decision_reason_presentation(*, explanation: object) -> str:
    """Format the adviser's receipt without reconstructing its comparison."""
    text = str(explanation or "").strip()
    if text == (
        "Keep the current connection: no eligible target has enough completed "
        "evidence or forecast value to justify disruption."
    ):
        return "No better-proven target justifies disruption."
    return text or "Reason not retained."


class AdvisoryRuntime:
    """Read and publish Matrix cells without owning a CGF service."""

    def __init__(
        self,
        customer: Any,
        *,
        history_path: str | Path | None = None,
        diagnostic_path: str | Path | None = None,
        engine: AdvisoryEngine | None = None,
        identity_evidence: Any = None,
    ) -> None:
        required = {
            "attach",
            "detach",
            "read_inputs",
            "publish_presentation",
            "publish_proposal",
            "publish_health",
            "publish_signal_baselines",
        }
        if any(not callable(getattr(customer, name, None)) for name in required):
            raise TypeError("new_horizon_intelligence_matrix_port_required")
        self._customer = customer
        if engine is None and history_path is None:
            raise ValueError("new_horizon_history_path_required")
        # Constructing the customer is part of Product startup. Opening and
        # restoring History belongs to its worker, after monitoring starts.
        self._engine = engine
        self._history_path = history_path
        self._identity_evidence = identity_evidence
        self._diagnostic_path = (
            Path(diagnostic_path).expanduser().resolve()
            if diagnostic_path is not None
            else None
        )
        self._last_diagnostic_meaning = ""
        self._last_diagnostic_check_at = 0.0
        self._capacity_probe = ExploreCapacityProbe()
        self._lock = RLock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._root: ProductStartupReceipt | None = None
        self._attached = False
        self._read_revision = 0
        self._input_revision = 0
        self._observation_key = ""
        self._outcome_sequence = 0
        self._proposal_result_sequence = 0
        self._presentation_sequence = 0
        self._presentation_output_lock = Lock()
        self._health_output_lock = Lock()
        self._last_presentation_meaning: tuple[Any, ...] | None = None
        self._last_health_meaning: tuple[Any, ...] | None = None
        self._fault_count = 0
        self._last_error = ""
        self._operator_fault_count = 0
        self._operator_fault_announced = False
        self._operator_recovery_count = 0
        self._observations = 0
        self._outcomes = 0
        self._proposal_attempts = 0
        self._last_proposal_result: dict[str, Any] = {}
        self._status_messages: deque[tuple[float, str]] = deque(
            maxlen=_STATUS_MESSAGE_LIMIT
        )
        self._status_identity_by_category: dict[str, str] = {}

    def _record_status_message(
        self,
        category: str,
        identity: object,
        message: str,
        *,
        observed_at: float | None = None,
    ) -> bool:
        """Keep a bounded stream of Intelligence operator messages."""

        key = str(category or "").strip()
        event_identity = str(identity or "").strip()
        text = " ".join(str(message or "").split())
        if not key or not event_identity or not text:
            raise ValueError("new_horizon_status_message_contract_invalid")
        if key == "history":
            raise ValueError("new_horizon_history_status_requires_diagnostic_surface")
        timestamp = float(time() if observed_at is None else observed_at)
        with self._lock:
            if self._status_identity_by_category.get(key) == event_identity:
                return False
            self._status_identity_by_category[key] = event_identity
            self._status_messages.append((timestamp, text))
        return True

    def _status_message_text(self) -> str:
        with self._lock:
            messages = tuple(self._status_messages)
        return "\n".join(
            f"{strftime('%H:%M:%S', localtime(timestamp))} {message}"
            for timestamp, message in messages
        )

    def _health(
        self,
        state: str,
        *,
        control_risk: str = "normal",
    ) -> IntelligenceHealthSnapshot:
        explore = (
            self._engine.explore_health()
            if self._engine is not None
            else {
                "active": False, "session_id": "", "stop_requested": False,
                "reason": "History has not been opened yet.",
            }
        )
        with self._lock:
            return IntelligenceHealthSnapshot(
                state=state,
                fault_count=self._fault_count,
                last_error=self._last_error,
                input_revision=self._input_revision,
                observed_at=time(),
                control_risk=control_risk,
                explore_active=bool(explore["active"]),
                explore_session_id=str(explore["session_id"]),
                explore_stop_requested=bool(explore["stop_requested"]),
                explore_reason=str(explore["reason"]),
            )

    @staticmethod
    def _health_meaning(health: IntelligenceHealthSnapshot) -> tuple[Any, ...]:
        return (
            health.state,
            health.fault_count,
            health.last_error,
            health.input_revision,
            health.control_risk,
            health.explore_active,
            health.explore_session_id,
            health.explore_stop_requested,
            health.explore_reason,
        )

    def _publish_health(
        self,
        state: str,
        *,
        control_risk: str = "normal",
    ) -> bool:
        health = self._health(state, control_risk=control_risk)
        meaning = self._health_meaning(health)
        with self._health_output_lock:
            with self._lock:
                if meaning == self._last_health_meaning:
                    return False
            self._customer.publish_health(health)
            with self._lock:
                self._last_health_meaning = meaning
        return True

    def _service_capacity_probe(self) -> bool:
        health = self._engine.explore_health()
        if not health["active"]:
            self._capacity_probe.cancel()
        completed = self._capacity_probe.poll()
        progressed = False
        if completed is not None:
            request, result = completed
            progressed = self._engine.admit_capacity_probe(request, result)
            if progressed:
                outputs_ok = self._publish_current_outputs()
                self._publish_health("running")
                if outputs_ok:
                    if not self._record_recovery():
                        self._record_stable_processing()
        request = self._engine.claim_capacity_probe()
        if request is not None:
            progressed = self._capacity_probe.start(request) or progressed
        return progressed

    def start(self, root: ProductStartupReceipt) -> None:
        if not isinstance(root, ProductStartupReceipt):
            raise TypeError("new_horizon_product_startup_root_required")
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("new_horizon_runtime_already_started")
            self._customer.attach(root)
            self._attached = True
            self._root = root
            self._stop.clear()
            self._last_health_meaning = None
            self._last_presentation_meaning = None
        try:
            self._record_status_message(
                "runtime",
                "started",
                "Brain started and is observing the live connection.",
            )
            self._publish_health("starting")
            thread = Thread(
                target=self._run,
                name="54guc-new-horizon-advisory",
                daemon=True,
            )
            with self._lock:
                self._thread = thread
            thread.start()
        except BaseException:
            with self._lock:
                self._thread = None
                self._root = None
            try:
                self._customer.detach()
            finally:
                with self._lock:
                    self._attached = False
            raise

    def _record_error(self, operation: str, error: BaseException, *, persist: bool = True) -> None:
        fault_text = f"{operation}:{type(error).__name__}:{error}"
        with self._lock:
            self._fault_count += 1
            self._last_error = fault_text
            self._operator_fault_count += 1
            self._operator_recovery_count = 0
            announce = bool(
                not self._operator_fault_announced
                and self._operator_fault_count >= _OPERATOR_FAULT_THRESHOLD
            )
            if announce:
                self._operator_fault_announced = True
        try:
            if persist and self._engine is not None:
                self._engine.record_runtime_fault(
                    operation=operation,
                    error_type=type(error).__name__,
                    detail=str(error),
                    observed_at=time(),
                    occurrence=self._fault_count,
                )
        except Exception as evidence_error:
            log.error(
                "New Horizon fault evidence write failed: %s",
                evidence_error,
            )
        if announce:
            self._record_status_message(
                "health",
                f"degraded:{self._fault_count}",
                "Brain is temporarily degraded; monitoring continues "
                "with the last valid decision.",
            )
        log.warning("New Horizon advisory %s failed: %s", operation, error)
        if not persist:
            # Shutdown may be reporting a busy engine. Its local fault and
            # False stop result must not wait on that engine's health lock.
            return
        try:
            self._publish_health("degraded", control_risk="recoverable")
        except BaseException:
            pass

    def _record_recovery(self) -> bool:
        """Recover health immediately but report only a stable recovery."""

        with self._lock:
            if not self._last_error:
                return False
            self._last_error = ""
            self._operator_recovery_count += 1
        self._publish_health("running")
        return True

    def _record_stable_processing(self) -> bool:
        """Close a reported fault after valid input remains stable."""

        with self._lock:
            if self._last_error:
                return False
            self._operator_recovery_count += 1
            if self._operator_recovery_count < _OPERATOR_RECOVERY_THRESHOLD:
                return False
            if not self._operator_fault_announced:
                self._operator_fault_count = 0
                self._operator_recovery_count = 0
                return False
            recovery_identity = f"recovered:{self._fault_count}"
            self._operator_fault_announced = False
            self._operator_fault_count = 0
            self._operator_recovery_count = 0
        self._record_status_message(
            "health",
            recovery_identity,
            "Brain recovered and is processing live data normally.",
        )
        return True

    def _publish_current_presentation(self) -> None:
        current = self._engine.current()
        if current is None:
            return
        with self._presentation_output_lock:
            frame = self._build_presentation(
                current.context.matrix_identity.namespace
            )
            if frame is None:
                return
            meaning = (
                frame.namespace,
                frame.values,
                frame.validity,
                frame.freshness,
                frame.reasons,
            )
            with self._lock:
                if meaning == self._last_presentation_meaning:
                    return
            self._customer.publish_presentation(frame)
            with self._lock:
                self._presentation_sequence = frame.sequence
                self._last_presentation_meaning = meaning

    def _publish_current_proposal(self) -> None:
        current = self._engine.current()
        if current is None or current.proposal is None:
            return
        proposal = current.proposal
        reservation = self._engine.reserve_proposal_emission(proposal)
        if not reservation.reserved:
            return
        try:
            self._customer.publish_proposal(proposal)
        except BaseException as exc:
            self._engine.reject_proposal_emission(
                proposal=proposal,
                reason="matrix_publication_failed:" + type(exc).__name__,
                observed_at=time(),
            )
            raise
        # Product Matrix has accepted the proposal. A later evidence-write
        # failure must not release its reservation and allow another radio
        # command at the same time.
        if not self._engine.admit_proposal_emission(proposal):
            raise RuntimeError("new_horizon_proposal_emission_duplicate")

    def _publish_current_signal_baselines(self) -> None:
        snapshot = self._engine.signal_baseline_snapshot(published_at=time())
        if snapshot is not None:
            self._customer.publish_signal_baselines(snapshot)

    def _publish_current_outputs(self) -> bool:
        """Publish each Matrix output once with its real fault identity."""

        failed = False
        for operation, publisher in (
            ("signal_baselines", self._publish_current_signal_baselines),
            ("proposal_output", self._publish_current_proposal),
            ("presentation", self._publish_current_presentation),
        ):
            try:
                publisher()
            except BaseException as exc:
                failed = True
                self._record_error(operation, exc)
        return not failed

    def _publish_diagnostic_snapshot(self) -> bool:
        if self._diagnostic_path is None:
            return False
        checked_at = monotonic()
        if (
            self._last_diagnostic_check_at > 0.0
            and checked_at - self._last_diagnostic_check_at
            < _DIAGNOSTIC_INTERVAL_S
        ):
            return False
        snapshot = self._engine.diagnostic_snapshot(captured_at=time())
        meaning = repr(
            {
                key: value
                for key, value in snapshot.items()
                if key not in {"captured_at", "matrix_sequence", "history"}
            }
        )
        if meaning == self._last_diagnostic_meaning:
            self._last_diagnostic_check_at = checked_at
            return False
        atomic_write_json(
            self._diagnostic_path,
            snapshot,
            sort_keys=True,
            trailing_newline=True,
        )
        self._last_diagnostic_meaning = meaning
        self._last_diagnostic_check_at = checked_at
        return True

    def _consume_observation(
        self,
        snapshot: IntelligenceMatrixInputSnapshot,
    ) -> bool:
        record = snapshot.observation
        if record is None or record.key == self._observation_key:
            return False
        explore_before = self._engine.explore_health()
        publication = self._engine.admit_observation(record)
        # Advance only after the observation is accepted. Matrix revision still
        # advances after malformed input, so that input stays rejected and the
        # next generation can be used.
        self._observation_key = record.key
        explore_after = self._engine.explore_health()
        if (
            explore_after["active"]
            and explore_after["session_id"] != explore_before["session_id"]
        ):
            self._record_status_message(
                "explore",
                explore_after["session_id"],
                "Explore activated.",
                observed_at=time(),
            )
        elif explore_before["active"] and not explore_after["active"]:
            completed = (
                str(explore_after["reason"])
                == "all_targets_completed_or_skipped"
            )
            self._record_status_message(
                "explore",
                str(explore_before["session_id"])
                + (":completed" if completed else ":stopped"),
                (
                    "Explore completed."
                    if completed
                    else "Explore stopped: "
                        + _operator_reason_text(str(explore_after["reason"]))
                    + "."
                ),
                observed_at=time(),
            )
        with self._lock:
            self._observations += 1
            count = self._observations
        if publication is not None and count == 1:
            log.info(
                "New Horizon advisory active sequence=%d selected=%s eligible=%s",
                publication.context.matrix_identity.matrix_sequence,
                publication.decision.selected_opportunity_id,
                publication.decision.eligibility.eligible,
            )
        return True

    def _consume_outcomes(
        self,
        snapshot: IntelligenceMatrixInputSnapshot,
    ) -> bool:
        progressed = False
        if (
            snapshot.outcome_evicted_through_sequence
            > self._outcome_sequence
        ):
            lost_from = self._outcome_sequence + 1
            lost_through = snapshot.outcome_evicted_through_sequence
            self._outcome_sequence = lost_through
            self._record_error(
                "outcome_gap",
                RuntimeError(
                    f"new_horizon_matrix_outcome_gap:{lost_from}-{lost_through}"
                ),
            )
            progressed = True
        for record in snapshot.outcomes:
            sequence = record.receipt.receipt_sequence
            if sequence <= self._outcome_sequence:
                continue
            admitted = self._engine.admit_outcome(record)
            self._outcome_sequence = sequence
            if admitted:
                with self._lock:
                    self._outcomes += 1
            transaction = record.receipt.transaction
            if transaction.family.value == "cellular_autonomous":
                terminal = record.receipt.terminal_status.value
                actor = _operator_action_actor(transaction.origin_kind)
                self._record_status_message(
                    "autonomous_outcome",
                    f"{record.receipt.receipt_sequence}:{terminal}",
                    actor + " action "
                    + ("completed." if terminal == "settled" else "ended: ")
                    + (
                        ""
                        if terminal == "settled"
                        else _operator_reason_text(record.receipt.reason)
                    ),
                    observed_at=record.receipt.terminal_ts,
                )
            progressed = True
        return progressed

    def _consume_proposal_results(
        self,
        snapshot: IntelligenceMatrixInputSnapshot,
    ) -> bool:
        progressed = False
        if (
            snapshot.proposal_result_evicted_through_sequence
            > self._proposal_result_sequence
        ):
            lost_from = self._proposal_result_sequence + 1
            lost_through = snapshot.proposal_result_evicted_through_sequence
            self._proposal_result_sequence = lost_through
            self._record_error(
                "proposal_result_gap",
                RuntimeError(
                    "new_horizon_matrix_proposal_result_gap:"
                    f"{lost_from}-{lost_through}"
                ),
            )
            progressed = True
        for record in snapshot.proposal_results:
            if record.sequence <= self._proposal_result_sequence:
                continue
            admitted = self._engine.admit_proposal_result(record)
            with self._lock:
                self._proposal_attempts += 1
                self._last_proposal_result = record.result.to_mapping()
            self._proposal_result_sequence = record.sequence
            actor = action_actor_label(
                "explore"
                if record.proposal.opportunity_id.startswith("explore:")
                else "smart_loop"
            )
            self._record_status_message(
                "proposal_result",
                f"{record.proposal_id}:{record.sequence}",
                (
                    actor + " proposal sent to Core."
                    if record.submitted
                    else actor + " proposal not sent: "
                    + _operator_reason_text(record.reason or "not accepted")
                ),
                observed_at=record.published_at,
            )
            progressed = admitted or progressed
        return progressed

    def _consume(self, snapshot: IntelligenceMatrixInputSnapshot) -> bool:
        if not isinstance(snapshot, IntelligenceMatrixInputSnapshot):
            raise TypeError("new_horizon_matrix_input_snapshot_required")
        if snapshot.revision <= self._read_revision:
            return False
        progressed = False
        failed = False
        cold_bootstrap = bool(
            not self._engine.has_context()
            and snapshot.observation is not None
            and snapshot.observation.key != self._observation_key
        )
        operations = (
            (("observation", self._consume_observation),)
            if cold_bootstrap
            else (
                ("proposal_result", self._consume_proposal_results),
                ("outcome", self._consume_outcomes),
                ("observation", self._consume_observation),
            )
        )
        for operation, consumer in operations:
            try:
                progressed = consumer(snapshot) or progressed
            except BaseException as exc:
                failed = True
                self._record_error(operation, exc)
                if operation != "observation":
                    break
        if cold_bootstrap and self._engine.has_context():
            # A cold runtime has no context for queued receipts. The observation
            # in this Matrix snapshot establishes it. Receipts then keep their
            # normal proposal, result, and outcome order.
            for operation, consumer in (
                ("proposal_result", self._consume_proposal_results),
                ("outcome", self._consume_outcomes),
            ):
                try:
                    progressed = consumer(snapshot) or progressed
                except BaseException as exc:
                    failed = True
                    self._record_error(operation, exc)
                    break
        if not self._publish_current_outputs():
            failed = True
        try:
            self._publish_diagnostic_snapshot()
        except BaseException as exc:
            failed = True
            self._record_error("diagnostic_snapshot", exc)
        if not failed:
            self._read_revision = snapshot.revision
            # PIM may enable control only after every component has completed
            # this delivered revision.
            self._input_revision = snapshot.revision
            self._publish_health("running")
            if not self._record_recovery():
                self._record_stable_processing()
        return progressed

    def _run(self) -> None:
        try:
            if self._stop.is_set():
                return
            if self._engine is None:
                self._engine = AdvisoryEngine(self._history_path, identity_evidence=self._identity_evidence)
        except Exception as exc:
            self._record_error("history_start", exc)
            return
        if self._stop.is_set():
            return
        self._publish_health("running")
        while not self._stop.is_set():
            try:
                snapshot = self._customer.read_inputs(
                    after_revision=self._read_revision,
                    timeout_s=_RECOVERY_INTERVAL_S,
                )
                self._consume(snapshot)
                if self._read_revision < snapshot.revision:
                    self._stop.wait(_INPUT_RETRY_INTERVAL_S)
                else:
                    self._service_capacity_probe()
            except BaseException as exc:
                self._record_error("matrix_input", exc)
                self._stop.wait(_INPUT_RETRY_INTERVAL_S)

    def prepare_support_history(self, *, history_path: str, cutoff: float, timeout_s: float) -> None:
        """Explicit product boundary to the attached, exact History owner."""
        if Path(history_path).resolve() != Path(self._history_path).resolve():
            raise RuntimeError("support_history_path_mismatch")
        engine = self._engine
        if engine is None or self._stop.is_set():
            raise RuntimeError("support_history_owner_unavailable")
        engine.flush_for_support(cutoff=cutoff, timeout_s=timeout_s)

    def stop(self, *, timeout_s: float = 2.0) -> bool:
        timeout = max(0.0, float(timeout_s))
        deadline = monotonic() + timeout
        with self._lock:
            thread = self._thread
            attached = self._attached
            if thread is None and not attached:
                self._stop.set()
                self._capacity_probe.cancel()
                try:
                    if self._engine is not None:
                        self._engine.close(timeout_s=max(0.0, deadline - monotonic()))
                except BaseException as exc:
                    self._record_error("stop_inactive", exc, persist=False)
                    return False
                return True
            self._stop.set()
            self._capacity_probe.cancel()
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - monotonic()))
        if thread is not None and thread.is_alive():
            self._record_error(
                "stop",
                TimeoutError("new_horizon_runtime_thread_survived"),
                persist=False,
            )
            return False
        if attached:
            self._publish_health("stopping")
        if not self._capacity_probe.stop(
            timeout_s=max(0.0, deadline - monotonic())
        ):
            self._record_error(
                "stop",
                TimeoutError("new_horizon_capacity_probe_thread_survived"),
                persist=False,
            )
            return False
        try:
            if self._engine is not None:
                self._engine.close(timeout_s=max(0.0, deadline - monotonic()))
            self._publish_health("stopped")
            self._customer.detach()
        except BaseException as exc:
            # History may be blocked or already closing. Reentering that same
            # sink to save this failure would defeat the bounded drain. The
            # runtime health and product shutdown receipt retain the failure.
            self._record_error("detach", exc, persist=False)
            return False
        with self._lock:
            self._thread = None
            self._root = None
            self._attached = False
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            status = {
                "schema": "NewHorizonAdvisoryRuntimeStatus.v2",
                "owner": "54guc.new_horizon.advisory_runtime",
                "running": bool(thread is not None and thread.is_alive()),
                "read_revision": self._read_revision,
                "input_revision": self._input_revision,
                "observations": self._observations,
                "outcomes": self._outcomes,
                "proposal_attempts": self._proposal_attempts,
                "last_proposal_result": dict(self._last_proposal_result),
                "fault_count": self._fault_count,
                "last_error": self._last_error,
            }
        status["engine"] = (
            self._engine.status() if self._engine is not None
            else {"state": "not_started"}
        )
        return status

    def _build_presentation(
        self,
        namespace,
    ) -> IntelligencePresentationSnapshot | None:
        """Build one bounded presentation cell. NGUI does not call this method."""

        (
            publication,
            history,
            candidate_forecasts,
            candidate_selection_id,
        ) = self._engine.presentation_state()
        if publication is None:
            return None
        if publication.context.matrix_identity.namespace != namespace:
            return None
        recovery = self._engine.recovery_status(publication.context)
        band_context, intelligence_impact = (
            self._engine.presentation_projections()
        )
        referee = self._engine.referee_status()
        learning_history = self._engine.completed_learning_cycle_ring()
        learned_proof_count = publication.learned_proof_count
        product_control = publication.context.product_control.to_mapping()
        continuity_state = str(history["continuity_state"])
        matched_count = int(history["matched_assessment_count"])
        if continuity_state == "continued" and not matched_count:
            continuity_state = "opened_without_matching_assessments"
        continuity_text = (
            f"HISTORY READY · {matched_count} matching assessments"
            if continuity_state == "continued"
            else "History opened; no completed assessments match this router and network."
            if continuity_state == "opened_without_matching_assessments"
            else "RECOVERED CORRUPTION · previous store quarantined"
            if continuity_state == "recovered_corruption"
            else "EMPTY CONTINUITY STORE · no prior evidence"
            if continuity_state == "existing_empty"
            else "FRESH HISTORY · no prior New Horizon evidence"
        )
        values = {
            "intelligence.state": _intelligence_state_text(
                product_control.get("control_mode")
            ),
            "intelligence.evidence_count": int(history["evidence_count"]),
            "intelligence.selected_advisory": (
                _operator_advisory_text(
                    publication.decision.selected_opportunity_id
                )
            ),
            "intelligence.decision_reason": _decision_reason_presentation(
                explanation=publication.decision.explanation,
            ),
            FORECAST_SOURCE_ID: _forecast_presentation(
                publication.forecast,
                candidates=candidate_forecasts,
                selected_opportunity_id=candidate_selection_id,
                current_identity=context_radio_identity(
                    publication.context
                ),
            ),
            IMPACT_SOURCE_ID: intelligence_impact,
            LEARNING_CYCLE_HISTORY_SOURCE_ID: _learning_cycle_history(
                learning_history,
                intelligence_impact=intelligence_impact,
            ),
            REFEREE_SUMMARY_SOURCE_ID: _referee_summary_presentation(
                band_context,
            ),
            "intelligence.learning": _learning_text(
                learned_proof_count,
            ),
            "intelligence.history_health": (
                "OK"
                if not history["recovered_corruption"]
                else "RECOVERED CORRUPTION"
            ),
            "intelligence.history_continuity": continuity_text,
            "intelligence.status_messages": self._status_message_text(),
            "intelligence.recovery_status": recovery,
            "intelligence.band_context_evidence": band_context,
            "intelligence.bandit_status": (
                f"Learning history: {learned_proof_count} completed assessment"
                + ("s" if learned_proof_count != 1 else "")
                if learned_proof_count
                else "Learning history: waiting for the first completed assessment"
            ),
            "intelligence.referee_status": (
                f"Assessing {int(referee['pending_count'])} recent change"
                + ("s" if int(referee["pending_count"]) != 1 else "")
                if int(referee["pending_count"])
                else "Monitoring current band context"
            ),
        }
        if set(values) != set(NEW_HORIZON_NGUI_SOURCE_IDS):
            raise RuntimeError("new_horizon_presentation_denominator_mismatch")
        # Intelligence output has its own Product Matrix clock. Core Matrix
        # sequence is context evidence, but it cannot number GUI changes caused
        # by history, proof, or outcome input while Core data is unchanged.
        with self._lock:
            sequence = self._presentation_sequence + 1
        published_at = publication.published_at
        validity = {
            source_id: value is not None for source_id, value in values.items()
        }
        return IntelligencePresentationSnapshot(
            namespace=namespace,
            sequence=sequence,
            published_at=published_at,
            values=CoreFrozenPayload.from_mapping(values),
            validity=CoreFrozenPayload.from_mapping(validity),
            freshness=CoreFrozenPayload.from_mapping(
                {
                    source_id: "fresh" if validity[source_id] else "unknown"
                    for source_id in values
                }
            ),
            reasons=CoreFrozenPayload.from_mapping(
                {
                    source_id: (
                        "" if validity[source_id] else "not_observed_yet"
                    )
                    for source_id in values
                }
            ),
            sampled_at=CoreFrozenPayload.from_mapping(
                {
                    source_id: published_at if validity[source_id] else 0.0
                    for source_id in values
                }
            ),
            sample_sequences=CoreFrozenPayload.from_mapping(
                {
                    source_id: sequence if validity[source_id] else 0
                    for source_id in values
                }
            ),
        )


__all__ = ("AdvisoryRuntime",)
