"""Intelligence-owned presentation vocabulary at the Product/NGUI boundary."""

from __future__ import annotations
from arch4.core_contracts.intelligence_proposals import valid_intelligence_grade, intelligence_grade_max
from arch4.action_origin import LEGAL_ACTION_ORIGIN_NAMES, require_action_origin_name
from arch4.core_contracts.nr_identity import nr_identity_values_valid

import math
import re
from collections.abc import Mapping


def saved_nr_identity_text(identity: Mapping[str, object]) -> str:
    """Display recorded NR identity without inventing absent NCI support.

    Historical rows do not carry field support states. Omit absent NCI rather
    than label it temporarily missing or borrow today's firmware capability.
    """
    if all(identity[name] is None for name in ("nr_band", "nr_cell_id", "nr_pci", "nr_arfcn")):
        return "----"
    parts = [f"n{identity['nr_band'] if identity['nr_band'] is not None else '----'}"]
    if identity["nr_cell_id"] is not None:
        parts.append(f"Cell {identity['nr_cell_id']}")
    parts.extend(f"{label} {identity[name] if identity[name] is not None else '----'}"
                 for label, name in (("PCI", "nr_pci"), ("ARFCN", "nr_arfcn")))
    return " / ".join(parts)


_ACTION_ACTOR_LABELS = {
    "smart_loop": "Brain",
    "explore": "Brain",
    "fault_recovery": "Recovery",
    "operator_ngui": "User",
    "operator_sms": "User",
    "schedule": "Schedule",
    "watchdog": "Watchdog",
    "lease_expiry": "Safety policy",
    "lower_controller": "Modem",
    "lower_controller_during_54guc_command": "Network during a request",
}
if set(_ACTION_ACTOR_LABELS) != LEGAL_ACTION_ORIGIN_NAMES | {
    "lower_controller", "lower_controller_during_54guc_command"
}:
    raise RuntimeError("intelligence_presentation_actor_map_incomplete")


def action_actor_label(value: object, *, allow_external: bool = False) -> str:
    """Name the proven action origin consistently at presentation boundaries."""

    if not allow_external:
        return _ACTION_ACTOR_LABELS[require_action_origin_name(value)]
    origin = str(getattr(value, "value", value) or "").strip()
    try:
        return _ACTION_ACTOR_LABELS[origin]
    except KeyError as exc:
        raise ValueError("intelligence_presentation_actor_unclassified") from exc


INTELLIGENCE_NGUI_EXTENSION_HOOK_ID = "intelligence_ngui_extension"
INTELLIGENCE_NGUI_EXTENSION_SCHEMA = "IntelligenceNGUIExtension.v2"

BAND_CONTEXT_OUTCOME_DIMENSIONS = (
    "performance_capacity",
    "performance_latency",
    "performance_jitter",
    "performance_communication_quality",
    "performance_internet_availability",
    "performance_overall_outcome",
)
BAND_CONTEXT_DIMENSIONS = BAND_CONTEXT_OUTCOME_DIMENSIONS
BAND_CONTEXT_SOURCE_ID = "intelligence.band_context_evidence"
BAND_CONTEXT_SCHEMA = "NewHorizonBandContextPresentation.v14"
FORECAST_SOURCE_ID = "intelligence.forecast"
FORECAST_SCHEMA = "NewHorizonForecastPresentation.v12"
IMPACT_SOURCE_ID = "intelligence.impact"
IMPACT_SCHEMA = "NewHorizonIntelligenceImpact.v3"
LEARNING_CYCLE_HISTORY_SOURCE_ID = "intelligence.learning_cycle_history"
LEARNING_CYCLE_HISTORY_SCHEMA = "NewHorizonLearningCycleHistory.v12"
REFEREE_SUMMARY_SOURCE_ID = "intelligence.referee_summary"
REFEREE_SUMMARY_SCHEMA = "NewHorizonRefereeSummary.v6"
BAND_CONTEXT_TIMELINE_ROLES = ("current",)
BAND_CONTEXT_RECENT_LIMIT = 8
BAND_CONTEXT_RECENT_ROLES = tuple(
    f"recent_{index}" for index in range(1, BAND_CONTEXT_RECENT_LIMIT + 1)
)

NEW_HORIZON_NGUI_SOURCE_IDS = frozenset(
    {
        "intelligence.state",
        "intelligence.evidence_count",
        "intelligence.selected_advisory",
        "intelligence.decision_reason",
        FORECAST_SOURCE_ID,
        IMPACT_SOURCE_ID,
        LEARNING_CYCLE_HISTORY_SOURCE_ID,
        REFEREE_SUMMARY_SOURCE_ID,
        "intelligence.learning",
        "intelligence.history_health",
        "intelligence.history_continuity",
        "intelligence.status_messages",
        "intelligence.recovery_status",
        BAND_CONTEXT_SOURCE_ID,
        "intelligence.bandit_status",
        "intelligence.referee_status",
    }
)

PRODUCT_INTELLIGENCE_NGUI_SOURCE_IDS = frozenset(
    {
        "intelligence.autopilot_requested",
        "intelligence.explore_requested",
        "intelligence.explore_session_id",
        "intelligence.explore_deadline_at",
        "intelligence.explore_reason",
        "intelligence.control_available",
        "intelligence.control_mode",
        "intelligence.control_reason",
        "intelligence.control_status_message",
        "intelligence.qualification_remaining_s",
        "intelligence.supervision",
        "intelligence.bouncer_status",
    }
)

INTELLIGENCE_NGUI_SOURCE_IDS = (
    NEW_HORIZON_NGUI_SOURCE_IDS | PRODUCT_INTELLIGENCE_NGUI_SOURCE_IDS
)


def project_intelligence_display_grade(value: object, *, dimension: str = "component") -> float | None:
    """Project one Referee grade for display without changing learning truth."""

    if (
        isinstance(value, bool)
        or type(value) not in {int, float}
        or not math.isfinite(float(value))
        or not valid_intelligence_grade(value,dimension=dimension)
    ):
        return None
    projected = round(float(value), 1)
    return 0.0 if projected == 0.0 else projected


def _presentation_text(value: object, *, error: str) -> None:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise ValueError(error)


_FORECAST_PROJECTION_FIELDS = {
        "state",
        "comparison",
        "meaning",
        "metrics",
        "evidence_count",
        "confidence_pct",
        "accuracy_pct",
        "accuracy_assessments",
}


def _validate_forecast_projection(value: dict[str, object]) -> None:
    if value["state"] not in {"predicted", "collecting"}:
        raise ValueError("intelligence_forecast_presentation_state_invalid")
    _presentation_text(
        value["comparison"],
        error="intelligence_forecast_comparison_invalid",
    )
    _presentation_text(
        value["meaning"],
        error="intelligence_forecast_meaning_invalid",
    )
    metrics = value["metrics"]
    metric_names = {
        "capacity_points",
        "latency_ms",
        "jitter_ms",
        "communication_quality",
        "internet_availability_pct",
        "estimated_aq",
    }
    if not isinstance(metrics, dict) or set(metrics) != metric_names:
        raise ValueError("intelligence_forecast_metrics_invalid")
    for name, metric in metrics.items():
        if metric is not None and (
            type(metric) not in {int, float} or not math.isfinite(float(metric))
        ):
            raise ValueError("intelligence_forecast_metric_invalid:" + name)
    cq = metrics["communication_quality"]
    aq = metrics["estimated_aq"]
    if aq is not None and not valid_intelligence_grade(aq, dimension="service_outcome"):
        raise ValueError("intelligence_forecast_aq_invalid")
    if cq is not None and not 0.0 <= float(cq) <= 1.0:
        raise ValueError("intelligence_forecast_cq_invalid")
    availability = metrics["internet_availability_pct"]
    if availability is not None and not 0.0 <= float(availability) <= 100.0:
        raise ValueError("intelligence_forecast_availability_invalid")
    if type(value["evidence_count"]) is not int or value["evidence_count"] < 0:
        raise ValueError("intelligence_forecast_evidence_count_invalid")
    confidence = value["confidence_pct"]
    if (
        type(confidence) not in {int, float}
        or not math.isfinite(float(confidence))
        or not 0.0 <= float(confidence) <= 100.0
    ):
        raise ValueError("intelligence_forecast_confidence_invalid")
    accuracy = value["accuracy_pct"]
    if accuracy is not None and (
        type(accuracy) not in {int, float}
        or not math.isfinite(float(accuracy))
        or not 0.0 <= float(accuracy) <= 100.0
    ):
        raise ValueError("intelligence_forecast_accuracy_invalid")
    if type(value["accuracy_assessments"]) is not int or value["accuracy_assessments"] < 0:
        raise ValueError("intelligence_forecast_accuracy_count_invalid")
    if (accuracy is None) != (value["accuracy_assessments"] == 0):
        raise ValueError("intelligence_forecast_accuracy_state_invalid")


def validate_forecast_presentation(value: object) -> None:
    """Validate one selected forecast plus its current Crystal Ball candidates."""

    expected = _FORECAST_PROJECTION_FIELDS | {"schema", "candidates"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("intelligence_forecast_presentation_invalid")
    if value["schema"] != FORECAST_SCHEMA:
        raise ValueError("intelligence_forecast_presentation_schema_invalid")
    _validate_forecast_projection(value)
    candidates = value["candidates"]
    candidate_expected = _FORECAST_PROJECTION_FIELDS | {
        "band", "ca_pool", "cell_id", "pci", "earfcn", "nr", "role"}
    if not isinstance(candidates, list) or len(candidates) > 256:
        raise ValueError("intelligence_forecast_candidates_invalid")
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != candidate_expected:
            raise ValueError("intelligence_forecast_candidate_invalid")
        for field, pattern in (("band", r"[Bn][0-9]+"),
                ("ca_pool", r"[Bn][0-9]+(?:\+[Bn][0-9]+)*"),
                ("cell_id", r"[0-9]+"), ("pci", r"[0-9]+"), ("earfcn", r"[0-9]+")):
            item = candidate[field]
            if type(item) is not str or not (item == "---" or re.fullmatch(pattern, item)):
                raise ValueError(f"intelligence_forecast_candidate_{field}_invalid")
        nr = candidate["nr"]
        if type(nr) is not str or not (nr in {"---", "NR off"}
                or re.fullmatch(r"n[1-9][0-9]*", nr)):
            raise ValueError("intelligence_forecast_candidate_nr_invalid")
        if candidate["role"] not in {
            "selected",
            "option",
            "outcome",
            "unproven",
            "nw",
            "ne",
        }:
            raise ValueError("intelligence_forecast_candidate_role_invalid")
        _validate_forecast_projection(candidate)
    if candidates and sum(row["role"] == "selected" for row in candidates) != 1:
        raise ValueError("intelligence_forecast_candidate_selected_count_invalid")


def _validate_saved_nr(value: object) -> None:
    if type(value) is not str or not (
        value in {"---", "NR off"} or re.fullmatch(r"n[1-9]\d*", value)
    ):
        raise ValueError("intelligence_saved_nr_invalid")


def saved_radio_label(context: str, nr: str) -> str:
    """Final human label only; structured consumers retain separate fields."""
    _validate_saved_nr(nr)
    return context if nr == "---" else f"{context} / {nr}"


def validate_learning_cycle_history(value: object) -> None:
    """Validate bounded exact-context customer learning proof."""

    if not isinstance(value, dict) or set(value) != {"schema", "rows"}:
        raise ValueError("intelligence_learning_cycle_history_invalid")
    if value["schema"] != LEARNING_CYCLE_HISTORY_SCHEMA:
        raise ValueError("intelligence_learning_cycle_history_schema_invalid")
    rows = value["rows"]
    if not isinstance(rows, list) or len(rows) > 8:
        raise ValueError("intelligence_learning_cycle_history_rows_invalid")
    expected = {
        "assessment_id",
        "decided_at",
        "completed_at",
        "before_context",
        "after_context",
        "before_nr",
        "after_nr",
        "why",
        "expected",
        "actual_grade",
        "grade_parts",
        "impact_result",
        "result_detail",
        "result_parts",
        "learning_effect",
        "next_consequence",
    }
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected:
            raise ValueError("intelligence_learning_cycle_history_row_invalid")
        for name in ("before_nr", "after_nr"):
            _validate_saved_nr(row[name])
        result_parts = row["result_parts"]
        if not isinstance(result_parts, list) or len(result_parts) != 5:
            raise ValueError("intelligence_learning_result_parts_invalid")
        for part, label, unit in zip(result_parts, ("ECI", "Lat", "Jit", "CQ", "AQ"), ("", " ms", " ms", "", "")):
            if (not isinstance(part, dict) or set(part) != {"text", "outcome"}
                or part["outcome"] not in {"improved", "neutral", "worse", "unavailable"}
                or not isinstance(part["text"], str)
                or re.fullmatch(label + r" (?:N/A|[+-]?\d+(?:\.\d+)?" + unit + ")", part["text"]) is None):
                raise ValueError("intelligence_learning_result_parts_invalid")
        if " · ".join(part["text"] for part in result_parts) != row["result_detail"]:
            raise ValueError("intelligence_learning_result_parts_mismatch")
        parts = row["grade_parts"]
        if not isinstance(parts, list) or not 1 <= len(parts) <= 3 or any(
            not isinstance(part, dict) or set(part) != {"text", "outcome"}
            or not isinstance(part["text"], str) or not part["text"]
            or part["outcome"] not in {"improved", "neutral", "worse", "unavailable"}
            for part in parts
        ):
            raise ValueError("intelligence_learning_grade_parts_invalid")
        result_parts = parts[:-1] if row["impact_result"] is not None else parts
        if (" · ".join(part["text"] for part in result_parts) != row["actual_grade"]
            or (row["impact_result"] is not None and parts[-1]["text"] != row["impact_result"])):
            raise ValueError("intelligence_learning_grade_parts_mismatch")
        _presentation_text(
            row["assessment_id"],
            error="intelligence_learning_cycle_history_assessment_id_invalid",
        )
        if any(
            type(row[name]) not in {int, float}
            or isinstance(row[name], bool)
            or not math.isfinite(float(row[name]))
            or float(row[name]) <= 0.0
            for name in ("decided_at", "completed_at")
        ):
            raise ValueError("intelligence_learning_cycle_history_time_invalid")
        if float(row["completed_at"]) < float(row["decided_at"]):
            raise ValueError(
                "intelligence_learning_cycle_history_time_order_invalid"
            )
        for name in (
            "before_context",
            "after_context",
            "why",
            "expected",
            "actual_grade",
            "result_detail",
            "learning_effect",
            "next_consequence",
        ):
            _presentation_text(
                row[name],
                error="intelligence_learning_cycle_history_text_invalid:" + name,
            )
        if row["impact_result"] is not None:
            _presentation_text(
                row["impact_result"],
                error="intelligence_learning_cycle_history_impact_invalid",
            )
        context = (
            r"(?:B[1-9]\d*/B[1-9]\d*(?:\+B[1-9]\d*)*|n[1-9]\d*/---)"
            r"/[1-9]\d*/\d+"
        )
        if re.fullmatch(context, row["before_context"]) is None:
            raise ValueError(
                "intelligence_learning_cycle_history_before_context_invalid"
            )
        after = row["after_context"]
        if re.fullmatch(context, after) is None:
            raise ValueError(
                "intelligence_learning_cycle_history_after_context_invalid"
            )
    assessment_ids = [row["assessment_id"] for row in rows]
    if len(set(assessment_ids)) != len(assessment_ids):
        raise ValueError(
            "intelligence_learning_cycle_history_assessment_duplicate"
        )


def validate_referee_summary(value: object) -> None:
    """Validate the Referee-owned current and latest completed windows."""

    expected = {
        "schema",
        "current",
        "latest_completed",
        "history_note",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("intelligence_referee_summary_invalid")
    if value["schema"] != REFEREE_SUMMARY_SCHEMA:
        raise ValueError("intelligence_referee_summary_schema_invalid")
    _presentation_text(
        value["current"],
        error="intelligence_referee_summary_current_invalid",
    )
    _presentation_text(
        value["history_note"],
        error="intelligence_referee_summary_history_note_invalid",
    )
    completed = value["latest_completed"]
    if completed is None:
        return
    result_keys = {
        "window",
        "target",
        "identity",
        "service_text",
        "service_grade",
        "communication_text",
        "communication_grade",
    }
    if not isinstance(completed, dict) or set(completed) != result_keys:
        raise ValueError("intelligence_referee_summary_result_invalid")
    for name in (
        "window",
        "target",
        "identity",
        "service_text",
        "communication_text",
    ):
        _presentation_text(
            completed[name],
            error="intelligence_referee_summary_result_text_invalid:" + name,
        )
    for name in ("service_grade", "communication_grade"):
        grade = completed[name]
        if grade is not None and (
            isinstance(grade, bool)
            or type(grade) not in {int, float}
            or not math.isfinite(float(grade))
            or not valid_intelligence_grade(grade,dimension="service_outcome" if name=="service_grade" else "component")
        ):
            raise ValueError("intelligence_referee_summary_grade_invalid:" + name)


def _validate_band_context_column(value: object) -> None:
    """Validate one Intelligence-owned Referee/history evidence row."""

    expected_column_keys = {
        "role",
        "label",
        "target",
        "scope_key",
        "cell_id",
        "pci",
        "earfcn",
        "nr_band",
        "nr_cell_id",
        "nr_pci",
        "nr_arfcn",
        "evidence_count",
        "opened_at",
        "due_at",
        "closed_at",
        "maturity",
        "reason",
        "dimensions",
    }
    if not isinstance(value, dict) or set(value) != expected_column_keys:
        raise ValueError("intelligence_band_context_column_invalid")
    for name in ("role", "label", "target", "scope_key", "maturity", "reason"):
        item = value[name]
        if type(item) is not str or not item.strip() or item != item.strip():
            raise ValueError("intelligence_band_context_text_invalid:" + name)
    lte_identity = tuple(value[name] for name in ("cell_id", "pci", "earfcn"))
    nr_identity = tuple(
        value[name]
        for name in ("nr_band", "nr_cell_id", "nr_pci", "nr_arfcn")
    )
    lte_available = all(type(item) is int for item in lte_identity)
    nr_available = nr_identity_values_valid(
        band=nr_identity[0], nci=nr_identity[1], pci=nr_identity[2], arfcn=nr_identity[3],
    )
    if not (
        all(item is None for item in lte_identity)
        or (
            lte_available
            and lte_identity[0] > 0
            and lte_identity[1] >= 0
            and lte_identity[2] >= 0
        )
    ):
        raise ValueError("intelligence_band_context_lte_identity_invalid")
    if not (
        all(item is None for item in nr_identity)
        or nr_available
    ):
        raise ValueError("intelligence_band_context_nr_identity_invalid")
    if not (lte_available or nr_available):
        raise ValueError("intelligence_band_context_radio_identity_required")
    if type(value["evidence_count"]) is not int or value["evidence_count"] < 0:
        raise ValueError("intelligence_band_context_evidence_count_invalid")
    for name in ("opened_at", "due_at", "closed_at"):
        stamp = value[name]
        if (
            type(stamp) not in {int, float}
            or not math.isfinite(float(stamp))
            or float(stamp) < 0.0
        ):
            raise ValueError("intelligence_band_context_timestamp_invalid:" + name)
    if float(value["due_at"]) <= float(value["opened_at"]):
        raise ValueError("intelligence_band_context_window_invalid")
    maturity = value["maturity"]
    if maturity not in {"collecting", "partial", "completed"}:
        raise ValueError("intelligence_band_context_maturity_invalid")
    closed_at = float(value["closed_at"])
    due_at = float(value["due_at"])
    opened_at = float(value["opened_at"])
    if (
        (maturity == "collecting" and closed_at != 0.0)
        or (
            maturity == "partial"
            and not opened_at < closed_at <= due_at
        )
        or (maturity == "completed" and closed_at < due_at)
    ):
        raise ValueError("intelligence_band_context_close_invalid")
    dimensions = value["dimensions"]
    if not isinstance(dimensions, dict) or set(dimensions) != set(
        BAND_CONTEXT_DIMENSIONS
    ):
        raise ValueError("intelligence_band_context_dimension_denominator_invalid")
    for dimension_id, cell in dimensions.items():
        if (
            not isinstance(cell, dict)
            or set(cell) != {"state", "text", "grade"}
            or cell.get("state") not in {"available", "unavailable"}
            or type(cell.get("text")) is not str
            or not cell["text"].strip()
            or (cell["state"] == "unavailable" and cell["text"] != "N/A")
            or (
                cell["grade"] is not None
                and (
                    isinstance(cell["grade"], bool)
                    or type(cell["grade"]) not in {int, float}
                    or not math.isfinite(float(cell["grade"]))
                    or not valid_intelligence_grade(cell["grade"],dimension=dimension_id)
                )
            )
            or (cell["state"] == "unavailable" and cell["grade"] is not None)
        ):
            raise ValueError("intelligence_band_context_cell_invalid:" + dimension_id)
        if maturity == "partial" and cell["grade"] is not None:
            raise ValueError("intelligence_band_context_partial_grade_forbidden")


def validate_band_context_evidence(value: object) -> None:
    """Validate the sole unified Referee/history presentation contract."""

    if not isinstance(value, dict) or set(value) != {
        "schema",
        "current_timeline",
        "recent_assessments",
        "history_note",
    }:
        raise ValueError("intelligence_band_context_contract_invalid")
    if value["schema"] != BAND_CONTEXT_SCHEMA:
        raise ValueError("intelligence_band_context_schema_invalid")
    timeline = value["current_timeline"]
    recent = value["recent_assessments"]
    reason = value["history_note"]
    if (
        not isinstance(timeline, list)
        or not isinstance(recent, list)
    ):
        raise ValueError("intelligence_band_context_columns_invalid")
    if type(reason) is not str or not reason.strip():
        raise ValueError("intelligence_band_context_reason_invalid")
    if not timeline:
        if recent:
            raise ValueError("intelligence_band_context_empty_timeline_has_history")
        return
    if len(timeline) != len(BAND_CONTEXT_TIMELINE_ROLES):
        raise ValueError("intelligence_band_context_timeline_count_invalid")
    for column in timeline:
        _validate_band_context_column(column)
    if tuple(column["role"] for column in timeline) != BAND_CONTEXT_TIMELINE_ROLES:
        raise ValueError("intelligence_band_context_timeline_roles_invalid")
    if len(recent) > BAND_CONTEXT_RECENT_LIMIT:
        raise ValueError("intelligence_band_context_recent_count_invalid")
    for column in recent:
        _validate_band_context_column(column)
    roles = tuple(column["role"] for column in recent)
    if roles != BAND_CONTEXT_RECENT_ROLES[: len(roles)]:
        raise ValueError("intelligence_band_context_recent_roles_invalid")
    closed = tuple(float(column["closed_at"]) for column in recent)
    if closed != tuple(sorted(closed)) or len(closed) != len(set(closed)):
        raise ValueError("intelligence_band_context_chronology_invalid")


def validate_intelligence_impact(value: object) -> None:
    """Validate the standalone customer-visible Intelligence Impact fact."""

    expected = {
        "schema",
        "window_started_at",
        "generated_at",
        "counts",
        "forecast_direction",
        "recent_outcomes",
        "positive_contexts",
        "negative_contexts",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("intelligence_impact_contract_invalid")
    if value["schema"] != IMPACT_SCHEMA:
        raise ValueError("intelligence_impact_schema_invalid")
    for name in ("window_started_at", "generated_at"):
        stamp = value[name]
        if (
            type(stamp) not in {int, float}
            or not math.isfinite(float(stamp))
            or (name == "generated_at" and float(stamp) <= 0.0)
        ):
            raise ValueError("intelligence_impact_time_invalid:" + name)
    if float(value["generated_at"]) < float(value["window_started_at"]):
        raise ValueError("intelligence_impact_window_invalid")
    counts = value["counts"]
    if not isinstance(counts, dict) or set(counts) != {
        "improved",
        "neutral",
        "worse",
        "not_proven",
    } or any(type(item) is not int or item < 0 for item in counts.values()):
        raise ValueError("intelligence_impact_counts_invalid")
    forecast = value["forecast_direction"]
    if not isinstance(forecast, dict) or set(forecast) != {
        "correct",
        "assessed",
        "accuracy_pct",
    }:
        raise ValueError("intelligence_impact_forecast_invalid")
    if (
        type(forecast["correct"]) is not int
        or type(forecast["assessed"]) is not int
        or not 0 <= forecast["correct"] <= forecast["assessed"]
    ):
        raise ValueError("intelligence_impact_forecast_count_invalid")
    accuracy = forecast["accuracy_pct"]
    if (forecast["assessed"] == 0) != (accuracy is None):
        raise ValueError("intelligence_impact_forecast_state_invalid")
    if accuracy is not None and (
        type(accuracy) not in {int, float}
        or not math.isfinite(float(accuracy))
        or not 0.0 <= float(accuracy) <= 100.0
    ):
        raise ValueError("intelligence_impact_forecast_accuracy_invalid")
    recent_outcomes = value["recent_outcomes"]
    if not isinstance(recent_outcomes, list) or len(recent_outcomes) > 32:
        raise ValueError("intelligence_impact_recent_outcomes_invalid")
    if value["generated_at"] == value["window_started_at"] and (
        any(counts.values()) or forecast["assessed"] or recent_outcomes
        or value["positive_contexts"] or value["negative_contexts"]
    ):
        raise ValueError("intelligence_impact_window_invalid")
    assessment_ids: list[str] = []
    completed_times: list[float] = []
    for latest in recent_outcomes:
        latest_expected = {
            "assessment_id",
            "attribution",
            "decided_at",
            "completed_at",
            "before",
            "after",
            "before_text",
            "after_text",
            "before_nr",
            "after_nr",
            "baseline_vector",
            "observed_vector",
            "observed_service_grade",
            "impact_grade",
        }
        if not isinstance(latest, dict) or set(latest) != latest_expected:
            raise ValueError("intelligence_impact_latest_invalid")
        for name in ("before_nr", "after_nr"):
            _validate_saved_nr(latest[name])
        _presentation_text(
            latest["assessment_id"],
            error="intelligence_impact_assessment_id_invalid",
        )
        assessment_ids.append(latest["assessment_id"])
        if latest["attribution"] not in {"brain_action", "validated_hold"}:
            raise ValueError("intelligence_impact_attribution_invalid")
        for name in ("decided_at", "completed_at"):
            stamp = latest[name]
            if (
                type(stamp) not in {int, float}
                or not math.isfinite(float(stamp))
                or float(stamp) <= 0.0
            ):
                raise ValueError("intelligence_impact_latest_time_invalid")
        if float(latest["completed_at"]) < float(latest["decided_at"]):
            raise ValueError("intelligence_impact_latest_order_invalid")
        completed_times.append(float(latest["completed_at"]))
        for name in ("before", "after", "baseline_vector", "observed_vector"):
            if not isinstance(latest[name], dict):
                raise ValueError("intelligence_impact_latest_mapping_invalid:" + name)
        for name in ("before_text", "after_text"):
            _presentation_text(
                latest[name],
                error="intelligence_impact_latest_text_invalid:" + name,
            )
        for name in ("observed_service_grade", "impact_grade"):
            grade = latest[name]
            if (
                isinstance(grade, bool)
                or type(grade) not in {int, float}
                or not math.isfinite(float(grade))
                or not valid_intelligence_grade(grade,dimension="service_outcome" if name=="observed_service_grade" else "service_change")
            ):
                raise ValueError("intelligence_impact_latest_grade_invalid:" + name)
    if len(set(assessment_ids)) != len(assessment_ids):
        raise ValueError("intelligence_impact_assessment_id_duplicate")
    if completed_times != sorted(completed_times):
        raise ValueError("intelligence_impact_chronology_invalid")
    context_expected = {"context", "nr", "mean_impact_grade", "completed_outcomes"}
    for name in ("positive_contexts", "negative_contexts"):
        rows = value[name]
        if not isinstance(rows, list) or len(rows) > 3:
            raise ValueError("intelligence_impact_contexts_invalid:" + name)
        for row in rows:
            if not isinstance(row, dict) or set(row) != context_expected:
                raise ValueError("intelligence_impact_context_invalid")
            _validate_saved_nr(row["nr"])
            _presentation_text(
                row["context"],
                error="intelligence_impact_context_text_invalid",
            )
            grade = row["mean_impact_grade"]
            if (
                isinstance(grade, bool)
                or type(grade) not in {int, float}
                or not math.isfinite(float(grade))
                or not valid_intelligence_grade(grade, dimension="service_change")
                or (name == "positive_contexts" and grade <= 0.0)
                or (name == "negative_contexts" and grade >= 0.0)
                or type(row["completed_outcomes"]) is not int
                or row["completed_outcomes"] <= 0
            ):
                raise ValueError("intelligence_impact_context_value_invalid")


__all__ = (
    "action_actor_label",
    "BAND_CONTEXT_DIMENSIONS",
    "BAND_CONTEXT_OUTCOME_DIMENSIONS",
    "BAND_CONTEXT_RECENT_LIMIT",
    "BAND_CONTEXT_RECENT_ROLES",
    "BAND_CONTEXT_SCHEMA",
    "BAND_CONTEXT_SOURCE_ID",
    "BAND_CONTEXT_TIMELINE_ROLES",
    "LEARNING_CYCLE_HISTORY_SCHEMA",
    "FORECAST_SOURCE_ID",
    "FORECAST_SCHEMA",
    "IMPACT_SOURCE_ID",
    "IMPACT_SCHEMA",
    "LEARNING_CYCLE_HISTORY_SOURCE_ID",
    "INTELLIGENCE_NGUI_EXTENSION_HOOK_ID",
    "INTELLIGENCE_NGUI_EXTENSION_SCHEMA",
    "INTELLIGENCE_NGUI_SOURCE_IDS",
    "NEW_HORIZON_NGUI_SOURCE_IDS",
    "PRODUCT_INTELLIGENCE_NGUI_SOURCE_IDS",
    "REFEREE_SUMMARY_SCHEMA",
    "REFEREE_SUMMARY_SOURCE_ID",
    "project_intelligence_display_grade",
    "validate_band_context_evidence",
    "validate_forecast_presentation",
    "validate_intelligence_impact",
    "validate_learning_cycle_history",
    "validate_referee_summary",
)
