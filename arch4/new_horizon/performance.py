"""Store New Horizon performance evidence and band summaries."""

from __future__ import annotations
from arch4.core_contracts.intelligence_proposals import valid_intelligence_grade
from arch4.core_contracts.nr_identity import nr_identity_values_valid

from dataclasses import dataclass
from datetime import datetime
import math
from time import time
from typing import Any, Mapping

from arch4.core_contracts.command_targets import (
    CoreBandSetTarget,
    CorePBandTarget,
    core_command_target_from_payload,
    core_lte_pband_signature,
)
from arch4.intelligence_presentation_contract import (
    BAND_CONTEXT_DIMENSIONS,
    BAND_CONTEXT_RECENT_LIMIT,
    BAND_CONTEXT_SCHEMA,
    project_intelligence_display_grade,
)
from arch4.new_horizon.contracts import (
    IntelligenceContext,
    RefereeProof,
    RefereeSubject,
    RefereeTarget,
)
from arch4.new_horizon.history import AdvisoryHistory
from arch4.new_horizon.proof import (
    context_scope_key,
    current_context_target,
)


PERFORMANCE_EVIDENCE_SCHEMA = "NewHorizonPerformanceEvidence.v2"
BAND_EVIDENCE_SCHEMA = "NewHorizonBandEvidenceSummary.v12"
BAND_EVIDENCE_CHECKPOINT = "band_evidence_summary_v12"
FACTUAL_BAND_EVIDENCE_MIGRATION = "band_evidence_factual_continuity_to_v12_v1"
FACTUAL_BAND_EVIDENCE_MIGRATION_SCHEMA = "NewHorizonBandEvidenceFactualMigration.v8"
PERFORMANCE_RECORD_TYPES = frozenset(
    {"passive_high_water", "explore_probe", "explore_best_of_three"}
)

_FACTUAL_BAND_ROW_FIELDS = frozenset(
    {
        "target_signature",
        "scope_key",
        "network_type",
        "pband",
        "ca_pool",
        "cell_id",
        "earfcn",
        "pci",
        "nr_band",
        "nr_cell_id",
        "nr_arfcn",
        "nr_pci",
        "max_dl_mbps",
        "best_ca_active_pct",
        "best_ri_stability_pct",
        "last_performance_at",
    }
)

_PHYSICAL_DECISION_COUNT_FIELDS = frozenset(
    {
        "result_count",
        "accepted_count",
        "confirmed_count",
        "settled_count",
        "failed_count",
        "rejected_count",
        "timed_out_count",
        "cancelled_count",
        "held_count",
        "reverted_count",
        "pause_count",
    }
)
_PHYSICAL_DECISION_VALUE_FIELDS = frozenset(
    {
        "latest_terminal_status",
        "latest_terminal_reason",
        "latest_terminal_at",
        "latest_retention",
        "latest_retention_at",
        "latest_pause_state",
        "latest_pause_ms",
        "pause_total_ms",
        "pause_average_ms",
    }
)
_PHYSICAL_ACTION_FIELDS = frozenset(
    {
        "target_signature",
        "terminal_status",
        "terminal_at",
        "pause_state",
        "pause_ms",
        "pause_observed_at",
        "retention_proofs",
    }
)


@dataclass(frozen=True, slots=True)
class BandPhysicalActionAudit:
    """Bounded physical-command data for support diagnostics."""

    target_signature: str
    result_count: int
    settled_count: int
    recent_terminal_statuses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BandPhysicalActionAuditView:
    """Support data kept outside the decision contract."""

    targets: tuple[BandPhysicalActionAudit, ...]


@dataclass(frozen=True, slots=True)
class BandPhysicalTargetEligibility:
    """Retry eligibility for one target, derived from a Core result."""

    target_signature: str
    eligible: bool
    terminal_status: str
    terminal_at: float
    retry_remaining_s: float


def _fact_value(context: IntelligenceContext, path: str) -> Any | None:
    fact = context.fact(path)
    return (
        fact.value.get("value")
        if fact.disposition == "available" and fact.retention is None
        else None
    )


def _stable_fact_value(context: IntelligenceContext, path: str) -> Any | None:
    """Read confirmed stable state even when this frame retained it."""

    fact = context.fact(path)
    return fact.value.get("value") if fact.disposition == "available" else None


def _surface_number(
    context: IntelligenceContext,
    surface_name: str,
    field: str,
) -> float | None:
    surface = context.surface(surface_name)
    if not surface.valid or surface.stale:
        return None
    value = surface.value.get(field)
    if isinstance(value, bool) or type(value) not in {int, float}:
        return None
    return float(value)


def _positive_finite_number(value: object) -> float | None:
    """Return a valid capacity value, not an idle zero."""

    if isinstance(value, bool) or type(value) not in {int, float}:
        return None
    measured = float(value)
    return measured if math.isfinite(measured) and measured > 0.0 else None


def _bounded_percentage(value: object) -> float | None:
    """Return a reported percentage, including a valid zero."""

    if isinstance(value, bool) or type(value) not in {int, float}:
        return None
    measured = float(value)
    return measured if math.isfinite(measured) and 0.0 <= measured <= 100.0 else None


def _current_target_signature(context: IntelligenceContext) -> str:
    target = current_context_target(context)
    return target.target_signature if target is not None else ""


def _commanded_target_signature(
    target: RefereeTarget,
    context: IntelligenceContext,
) -> str:
    """Return the commanded LTE context when evidence proves it."""

    typed_target = core_command_target_from_payload(target.target.to_mapping())
    if isinstance(typed_target, CorePBandTarget):
        return core_lte_pband_signature(
            typed_target.pband,
            typed_target.ca_pool,
        )
    if not isinstance(typed_target, CoreBandSetTarget):
        return target.target_signature
    pband = _stable_fact_value(context, "radio.serving_band")
    pool = typed_target.bands
    if (
        type(pband) is int
        and pool
        and all(type(band) is int and band > 0 for band in pool)
        and pband in pool
    ):
        return core_lte_pband_signature(pband, pool)
    return target.target_signature


def _context_row(
    context: IntelligenceContext,
    *,
    target_signature: str,
) -> tuple[str, dict[str, Any]] | None:
    pband = _stable_fact_value(context, "radio.serving_band")
    pool = _stable_fact_value(context, "radio.configured_band_pool")
    cell_id = _stable_fact_value(context, "rf.cell_id")
    earfcn = _stable_fact_value(context, "rf.earfcn_dl")
    pci = _stable_fact_value(context, "rf.pci")
    network_type = _stable_fact_value(context, "radio.network_type")
    nr_band = _stable_fact_value(context, "radio.nr_band")
    nr_cell_id = _stable_fact_value(context, "radio.nr_cell_id")
    nr_arfcn = _stable_fact_value(context, "radio.nr_arfcn")
    nr_pci = _stable_fact_value(context, "radio.nr_pci")
    lte_available = bool(
        network_type != "NR SA (5G)"
        and type(pband) is int
        and isinstance(pool, (list, tuple))
        and pool
        and all(type(band) is int and band > 0 for band in pool)
        and type(cell_id) is int
        and cell_id > 0
        and type(earfcn) is int
        and earfcn >= 0
        and type(pci) is int
        and pci >= 0
    )
    nr_available = bool(
        network_type == "NR NSA (5G)"
        and nr_identity_values_valid(band=nr_band, arfcn=nr_arfcn, pci=nr_pci, nci=nr_cell_id)
    )
    quality = context.surface("quality").value
    if (
        not target_signature
        or not (lte_available or nr_available)
        or (network_type == "NR NSA (5G)" and not (lte_available and nr_available))
        or network_type == "NR SA (5G)"
        or quality.get("nr_transition_dirty") is True
    ):
        return None
    scope_key = context_scope_key(context)
    return (
        target_signature + "|" + scope_key,
        {
            "target_signature": target_signature,
            "scope_key": scope_key,
            "network_type": str(network_type or ""),
            "pband": pband if lte_available else None,
            "ca_pool": sorted(set(pool)) if lte_available else [],
            "cell_id": cell_id if lte_available else None,
            "earfcn": earfcn if lte_available else None,
            "pci": pci if lte_available else None,
            "nr_band": nr_band if nr_available else None,
            "nr_cell_id": nr_cell_id if nr_available else None,
            "nr_arfcn": nr_arfcn if nr_available else None,
            "nr_pci": nr_pci if nr_available else None,
        },
    )


def _empty_summary() -> dict[str, Any]:
    return {
        "schema": BAND_EVIDENCE_SCHEMA,
        "targets": {},
        "decisions": {},
        "actions": {},
    }


def _merge_factual_row(
    targets: dict[str, Any],
    *,
    row_key: str,
    raw_row: Mapping[str, Any],
) -> bool:
    fact_row = {
        key: value for key, value in raw_row.items() if key in _FACTUAL_BAND_ROW_FIELDS
    }
    if not all(
        fact_row.get(name) is not None
        for name in (
            "target_signature",
            "scope_key",
            "pband",
            "ca_pool",
            "cell_id",
            "earfcn",
            "pci",
        )
    ):
        return False
    row = dict(targets.get(row_key) or {})
    for key, value in fact_row.items():
        if key in {
            "max_dl_mbps",
            "best_ca_active_pct",
            "best_ri_stability_pct",
            "last_performance_at",
        }:
            previous = _finite_number(row.get(key))
            measured = _finite_number(value)
            if measured is not None and (previous is None or measured > previous):
                row[key] = measured
        else:
            row[key] = value
    targets[row_key] = row
    return True


def _merge_physical_decision(
    decisions: dict[str, Any],
    *,
    target_signature: str,
    raw_row: Mapping[str, Any],
    include_retention: bool,
) -> bool:
    physical = {
        key: value
        for key, value in raw_row.items()
        if key in _PHYSICAL_DECISION_COUNT_FIELDS
        or key in _PHYSICAL_DECISION_VALUE_FIELDS
    }
    if not target_signature or not physical:
        return False
    row = dict(decisions.get(target_signature) or {})
    retained_count_fields = (
        _PHYSICAL_DECISION_COUNT_FIELDS
        if include_retention
        else _PHYSICAL_DECISION_COUNT_FIELDS - {"held_count", "reverted_count"}
    )
    for key in retained_count_fields:
        value = physical.get(key)
        if type(value) is int and value >= 0:
            row[key] = max(int(row.get(key, 0) or 0), value)
    prior_terminal_at = _finite_number(row.get("latest_terminal_at")) or 0.0
    source_terminal_at = _finite_number(physical.get("latest_terminal_at")) or 0.0
    if source_terminal_at >= prior_terminal_at:
        for key in (
            "latest_terminal_status",
            "latest_terminal_reason",
            "latest_terminal_at",
        ):
            if key in physical:
                row[key] = physical[key]
    if include_retention:
        prior_retention_at = _finite_number(row.get("latest_retention_at")) or 0.0
        source_retention_at = _finite_number(physical.get("latest_retention_at")) or 0.0
        if source_retention_at >= prior_retention_at:
            for key in ("latest_retention", "latest_retention_at"):
                if key in physical:
                    row[key] = physical[key]
    for key in (
        "latest_pause_state",
        "latest_pause_ms",
        "pause_total_ms",
        "pause_average_ms",
    ):
        if key in physical:
            row[key] = physical[key]
    decisions[target_signature] = row
    return True


def _merge_physical_action(
    actions: dict[str, Any],
    *,
    action_id: str,
    raw_row: Mapping[str, Any],
) -> bool:
    physical = {
        key: value for key, value in raw_row.items() if key in _PHYSICAL_ACTION_FIELDS
    }
    if (
        not action_id
        or not str(physical.get("target_signature") or "")
        or not str(physical.get("terminal_status") or "")
    ):
        return False
    current = dict(actions.get(action_id) or {})
    current_at = max(
        _finite_number(current.get("terminal_at")) or 0.0,
        _finite_number(current.get("pause_observed_at")) or 0.0,
    )
    source_at = max(
        _finite_number(physical.get("terminal_at")) or 0.0,
        _finite_number(physical.get("pause_observed_at")) or 0.0,
    )
    if not current or source_at >= current_at:
        current.update(physical)
        actions[action_id] = current
        return True
    return False


def _migrate_factual_summary(
    history: AdvisoryHistory,
    *,
    continuity_key: str,
    current: Mapping[str, Any] | None,
) -> dict[str, Any]:
    base = _empty_summary() if current is None else dict(current)
    targets = dict(base.get("targets") or {})
    decisions = dict(base.get("decisions") or {})
    actions = dict(base.get("actions") or {})
    source_scopes = {continuity_key}
    source_scopes.update(history.confirmed_legacy_scopes(continuity_key=continuity_key))
    sources = (
        ("band_evidence_summary_v2", "NewHorizonBandEvidenceSummary.v2"),
        ("band_evidence_summary_v3", "NewHorizonBandEvidenceSummary.v3"),
        ("band_evidence_summary_v4", "NewHorizonBandEvidenceSummary.v4"),
        ("band_evidence_summary_v5", "NewHorizonBandEvidenceSummary.v5"),
        ("band_evidence_summary_v6", "NewHorizonBandEvidenceSummary.v6"),
        ("band_evidence_summary_v7", "NewHorizonBandEvidenceSummary.v7"),
        ("band_evidence_summary_v8", "NewHorizonBandEvidenceSummary.v8"),
        ("band_evidence_summary_v9", "NewHorizonBandEvidenceSummary.v9"),
        ("band_evidence_summary_v10", "NewHorizonBandEvidenceSummary.v10"),
        ("band_evidence_summary_v11", "NewHorizonBandEvidenceSummary.v11"),
        ("band_evidence_summary_v12", "NewHorizonBandEvidenceSummary.v12"),
    )
    imported_rows = 0
    imported_decisions = 0
    imported_actions = 0
    for source_scope in sorted(source_scopes):
        for checkpoint_name, expected_schema in sources:
            prior = (history.load_checkpoint(scope_key=source_scope, name=checkpoint_name)
                if source_scope == continuity_key else history.read_confirmed_factual_checkpoint(
                    continuity_key=continuity_key, source_scope=source_scope, name=checkpoint_name))
            if prior is None:
                continue
            if prior.get("schema") != expected_schema or not isinstance(
                prior.get("targets"), Mapping
            ):
                raise ValueError(
                    "new_horizon_factual_band_evidence_migration_source_invalid"
                )
            for row_key, raw_row in prior["targets"].items():
                if not isinstance(row_key, str) or not isinstance(raw_row, Mapping):
                    raise TypeError(
                        "new_horizon_factual_band_evidence_migration_row_invalid"
                    )
                imported_rows += int(
                    _merge_factual_row(
                        targets,
                        row_key=row_key,
                        raw_row=raw_row,
                    )
                )
            prior_decisions = prior.get("decisions", {})
            prior_actions = prior.get("actions", {})
            if not isinstance(prior_decisions, Mapping) or not isinstance(
                prior_actions,
                Mapping,
            ):
                raise ValueError(
                    "new_horizon_physical_band_evidence_migration_source_invalid"
                )
            for signature, raw_row in prior_decisions.items():
                if not isinstance(signature, str) or not isinstance(raw_row, Mapping):
                    raise TypeError(
                        "new_horizon_physical_decision_migration_row_invalid"
                    )
                imported_decisions += int(
                    _merge_physical_decision(
                        decisions,
                        target_signature=signature,
                        raw_row=raw_row,
                        include_retention=checkpoint_name == BAND_EVIDENCE_CHECKPOINT,
                    )
                )
            for action_id, raw_row in prior_actions.items():
                if not isinstance(action_id, str) or not isinstance(raw_row, Mapping):
                    raise TypeError("new_horizon_physical_action_migration_row_invalid")
                imported_actions += int(
                    _merge_physical_action(
                        actions,
                        action_id=action_id,
                        raw_row=raw_row,
                    )
                )
    result = {
        "schema": BAND_EVIDENCE_SCHEMA,
        "targets": targets,
        "decisions": decisions,
        "actions": actions,
    }
    migrated_at = (
        max(
            (
                float(row.get("last_performance_at", 0.0) or 0.0)
                for row in targets.values()
                if isinstance(row, Mapping)
            ),
            default=0.0,
        )
        or time()
    )
    history.save_checkpoint(
        scope_key=continuity_key,
        name=BAND_EVIDENCE_CHECKPOINT,
        updated_at=migrated_at,
        payload=result,
    )
    history.save_checkpoint(
        scope_key=continuity_key,
        name=FACTUAL_BAND_EVIDENCE_MIGRATION,
        updated_at=migrated_at,
        payload={
            "schema": FACTUAL_BAND_EVIDENCE_MIGRATION_SCHEMA,
            "imported_rows": imported_rows,
            "imported_physical_decisions": imported_decisions,
            "imported_physical_actions": imported_actions,
            "source_checkpoints": [name for name, _schema in sources],
            "source_scope_count": len(source_scopes),
            "current_generation_grades_imported": False,
        },
    )
    return result


def _summary(
    history: AdvisoryHistory,
    *,
    continuity_key: str,
) -> dict[str, Any]:
    current = history.load_checkpoint(
        scope_key=continuity_key,
        name=BAND_EVIDENCE_CHECKPOINT,
    )
    if current is not None and (
        current.get("schema") != BAND_EVIDENCE_SCHEMA
        or not isinstance(current.get("targets"), Mapping)
        or not isinstance(current.get("decisions", {}), Mapping)
        or not isinstance(current.get("actions", {}), Mapping)
    ):
        raise ValueError("new_horizon_band_evidence_schema_invalid")
    migration = history.load_checkpoint(
        scope_key=continuity_key,
        name=FACTUAL_BAND_EVIDENCE_MIGRATION,
    )
    if migration is None:
        current = _migrate_factual_summary(
            history,
            continuity_key=continuity_key,
            current=current,
        )
    elif (
        migration is not None
        and migration.get("schema") != FACTUAL_BAND_EVIDENCE_MIGRATION_SCHEMA
    ):
        raise ValueError("new_horizon_factual_band_evidence_migration_invalid")
    if current is None:
        return {
            "schema": BAND_EVIDENCE_SCHEMA,
            "targets": {},
            "decisions": {},
            "actions": {},
        }
    if current.get("schema") != BAND_EVIDENCE_SCHEMA:
        raise ValueError("new_horizon_band_evidence_schema_invalid")
    targets = current.get("targets")
    if not isinstance(targets, Mapping):
        raise TypeError("new_horizon_band_evidence_targets_required")
    decisions = current.get("decisions", {})
    if not isinstance(decisions, Mapping):
        raise TypeError("new_horizon_band_evidence_decisions_required")
    actions = current.get("actions", {})
    if not isinstance(actions, Mapping):
        raise TypeError("new_horizon_band_evidence_actions_required")
    return {
        "schema": BAND_EVIDENCE_SCHEMA,
        "targets": dict(targets),
        "decisions": dict(decisions),
        "actions": dict(actions),
    }


def _save_summary(
    history: AdvisoryHistory,
    *,
    continuity_key: str,
    observed_at: float,
    summary: Mapping[str, Any],
) -> None:
    history.save_checkpoint(
        scope_key=continuity_key,
        name=BAND_EVIDENCE_CHECKPOINT,
        updated_at=observed_at,
        payload=summary,
    )


def _update_context_maxima(
    history: AdvisoryHistory,
    context: IntelligenceContext,
    *,
    continuity_key: str,
    target_signature: str,
    observed_at: float,
    dl_mbps: float | None,
    ca_active_pct: float | None,
    ri_stability_pct: float | None,
) -> bool:
    context_row = _context_row(
        context,
        target_signature=target_signature,
    )
    if context_row is None:
        return False
    row_key, identity = context_row
    summary = _summary(
        history,
        continuity_key=continuity_key,
    )
    targets = dict(summary["targets"])
    row = dict(targets.get(row_key) or identity)
    changed = any(row.get(key) != value for key, value in identity.items())
    for key, value in (
        ("max_dl_mbps", dl_mbps),
        ("best_ca_active_pct", ca_active_pct),
        ("best_ri_stability_pct", ri_stability_pct),
    ):
        if value is None:
            continue
        measured = float(value)
        if not math.isfinite(measured):
            continue
        if key == "max_dl_mbps" and measured <= 0.0:
            continue
        if key not in row or measured > float(row[key]):
            row[key] = measured
            changed = True
    if not changed:
        return False
    row["last_performance_at"] = float(observed_at)
    row.update(identity)
    targets[row_key] = row
    _save_summary(
        history,
        continuity_key=continuity_key,
        observed_at=observed_at,
        summary={
            "schema": BAND_EVIDENCE_SCHEMA,
            "targets": targets,
            "decisions": summary["decisions"],
            "actions": summary["actions"],
        },
    )
    return True


def update_band_observation_maxima(
    history: AdvisoryHistory,
    context: IntelligenceContext,
    *,
    continuity_key: str,
) -> bool:
    """Update the best live values without recording another event."""

    if not context.complete:
        return False
    dl = _fact_value(context, "traffic.dl_mbps")
    return _update_context_maxima(
        history,
        context,
        continuity_key=continuity_key,
        target_signature=_current_target_signature(context),
        observed_at=context.accepted_at,
        dl_mbps=(
            float(dl) if not isinstance(dl, bool) and type(dl) in {int, float} else None
        ),
        ca_active_pct=_surface_number(context, "ca", "ca_active_pct"),
        ri_stability_pct=_surface_number(
            context,
            "quality",
            "ri_stability_pct",
        ),
    )


def record_performance_evidence(
    history: AdvisoryHistory,
    context: IntelligenceContext,
    *,
    event_key: str,
    scope_key: str,
    continuity_key: str,
    record_type: str,
    dl_mbps: float,
    observed_at: float,
    target_signature: str = "",
    details: Mapping[str, Any] | None = None,
) -> bool:
    """Record one performance sample and update its bounded summary."""

    if record_type not in PERFORMANCE_RECORD_TYPES:
        raise ValueError("new_horizon_performance_record_type_invalid")
    measured = float(dl_mbps)
    if not math.isfinite(measured) or measured < 0.0:
        raise ValueError("new_horizon_performance_dl_invalid")
    signature = str(target_signature or _current_target_signature(context))
    payload = {
        "schema": PERFORMANCE_EVIDENCE_SCHEMA,
        "record_type": record_type,
        "identity_code": context.identity_code,
        "continuity_key": continuity_key,
        "target_signature": signature,
        "observed_at": float(observed_at),
        "dl_mbps": measured,
        "serving_band": _fact_value(context, "radio.serving_band"),
        "nr_band": _fact_value(context, "radio.nr_band"),
        "nr_cell_id": _fact_value(context, "radio.nr_cell_id"),
        "nr_pci": _fact_value(context, "radio.nr_pci"),
        "nr_arfcn": _fact_value(context, "radio.nr_arfcn"),
        "ca_pool_signature": _stable_fact_value(
            context,
            "radio.configured_band_pool_signature",
        ),
        "ca_active_pct": _surface_number(context, "ca", "ca_active_pct"),
        "ri_stability_pct": _surface_number(
            context,
            "quality",
            "ri_stability_pct",
        ),
        "details": dict(details or {}),
    }
    recorded = history.record(
        event_key=event_key,
        scope_key=scope_key,
        kind="performance_proof",
        observed_at=observed_at,
        payload=payload,
    )
    _update_context_maxima(
        history,
        context,
        continuity_key=continuity_key,
        target_signature=signature,
        observed_at=observed_at,
        dl_mbps=measured,
        ca_active_pct=payload["ca_active_pct"],
        ri_stability_pct=payload["ri_stability_pct"],
    )
    return recorded


def record_physical_result(
    history: AdvisoryHistory,
    subject: RefereeSubject,
    context: IntelligenceContext,
    *,
    continuity_key: str,
) -> bool:
    """Store final Core command facts without creating a grade."""

    if subject.subject_kind != "core_command":
        raise ValueError("new_horizon_physical_result_core_command_required")
    summary = _summary(
        history,
        continuity_key=continuity_key,
    )
    decisions = dict(summary["decisions"])
    actions = dict(summary["actions"])
    # Core's terminal receipt states which target was attempted. Context only
    # proves device and provider continuity, so it may still show the previous
    # radio configuration when the receipt is read.
    signature = _commanded_target_signature(subject.target, context)
    action_id = subject.key.removeprefix("core-command:")
    retained = actions.get(action_id, {})
    terminal = {
        "target_signature": signature,
        "terminal_status": subject.terminal_status,
        "terminal_at": float(subject.terminal_at),
    }
    if "terminal_status" in retained:
        if any(retained.get(key) != value for key, value in terminal.items()):
            raise ValueError("new_horizon_physical_result_identity_conflict")
        return False
    decision = dict(decisions.get(signature) or {})
    decision["result_count"] = int(decision.get("result_count", 0) or 0) + 1
    state_key = subject.terminal_status + "_count"
    decision[state_key] = int(decision.get(state_key, 0) or 0) + 1
    decision["latest_terminal_status"] = subject.terminal_status
    decision["latest_terminal_reason"] = subject.reason
    decision["latest_terminal_at"] = float(subject.terminal_at)
    decisions[signature] = decision
    actions[action_id] = {
        **actions.get(action_id, {}),
        **terminal,
    }
    _save_summary(
        history,
        continuity_key=continuity_key,
        observed_at=subject.terminal_at,
        summary={
            "schema": BAND_EVIDENCE_SCHEMA,
            "targets": summary["targets"],
            "decisions": decisions,
            "actions": actions,
        },
    )
    return True


def record_target_retention(
    history: AdvisoryHistory,
    proof: RefereeProof,
    context: IntelligenceContext,
    *,
    continuity_key: str,
) -> bool:
    """Store whether the target held or reverted, separate from grades."""

    if proof.subject_kind == "active_context_window" or proof.scope_key != continuity_key:
        return False

    reverted = any(reason.startswith("target_did_not_hold") for reason in proof.reasons)
    held = bool(
        proof.dimension == "service_outcome"
        and proof.status == "graded"
        and proof.final_assessment
    )
    if not (reverted or held):
        return False
    summary = _summary(
        history,
        continuity_key=continuity_key,
    )
    decisions = dict(summary["decisions"])
    actions = dict(summary["actions"])
    action_id = proof.decision_id.removeprefix("core-command:")
    action = dict(actions.get(action_id) or {})
    retention_key = proof.assessment_id + (":reverted" if reverted else ":held")
    recorded = list(action.get("retention_proofs", ()))
    if retention_key in recorded:
        return False
    signature = (
        _commanded_target_signature(proof.target, context)
        if reverted
        else _current_target_signature(context)
        or _commanded_target_signature(proof.target, context)
    )
    decision = dict(decisions.get(signature) or {})
    key = "reverted_count" if reverted else "held_count"
    decision[key] = int(decision.get(key, 0) or 0) + 1
    decision["latest_retention"] = "reverted" if reverted else "held"
    decision["latest_retention_at"] = float(proof.closed_at)
    decisions[signature] = decision
    action["target_signature"] = signature
    action["retention_proofs"] = [*recorded, retention_key]
    actions[action_id] = action
    _save_summary(
        history,
        continuity_key=continuity_key,
        observed_at=proof.closed_at,
        summary={
            "schema": BAND_EVIDENCE_SCHEMA,
            "targets": summary["targets"],
            "decisions": decisions,
            "actions": actions,
        },
    )
    return True


def record_internet_pause(
    history: AdvisoryHistory,
    *,
    continuity_key: str,
    observed_at: float,
    pause: Mapping[str, Any],
) -> bool:
    """Match Core action reachability with the exact target."""

    if (
        pause.get("schema") != "CoreActionInternetPause.v1"
        or pause.get("owner") != "54guc.core.action_internet_pause"
    ):
        raise ValueError("new_horizon_internet_pause_contract_invalid")
    action_id = str(pause.get("action_id") or "")
    state = str(pause.get("state") or "")
    value = pause.get("value_ms")
    if not action_id or state not in {"measured", "connecting", "idle"}:
        return False
    if state == "measured" and (
        isinstance(value, bool)
        or type(value) not in {int, float}
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise ValueError("new_horizon_internet_pause_value_invalid")
    summary = _summary(history, continuity_key=continuity_key)
    actions = dict(summary["actions"])
    action = dict(actions.get(action_id) or {})
    signature = str(action.get("target_signature") or "")
    if not signature:
        return False
    if action.get("pause_state") == state and action.get("pause_ms") == value:
        return False
    action["pause_state"] = state
    action["pause_ms"] = float(value) if state == "measured" else None
    action["pause_observed_at"] = float(observed_at)
    actions[action_id] = action
    decisions = dict(summary["decisions"])
    decision = dict(decisions.get(signature) or {})
    decision["latest_pause_state"] = state
    decision["latest_pause_ms"] = action["pause_ms"]
    if state == "measured":
        count = int(decision.get("pause_count", 0) or 0) + 1
        total = float(decision.get("pause_total_ms", 0.0) or 0.0) + float(value)
        decision["pause_count"] = count
        decision["pause_total_ms"] = total
        decision["pause_average_ms"] = total / count
    decisions[signature] = decision
    _save_summary(
        history,
        continuity_key=continuity_key,
        observed_at=observed_at,
        summary={
            "schema": BAND_EVIDENCE_SCHEMA,
            "targets": summary["targets"],
            "decisions": decisions,
            "actions": actions,
        },
    )
    return True


def band_physical_action_audit(
    history: AdvisoryHistory,
    context: IntelligenceContext,
    *,
    continuity_key: str,
) -> BandPhysicalActionAuditView:
    """Return physical action history for diagnostics, not selection."""

    summary = _summary(
        history,
        continuity_key=continuity_key,
    )
    decisions = summary["decisions"]
    action_statuses: dict[str, list[tuple[float, str]]] = {}
    for action in summary["actions"].values():
        if not isinstance(action, Mapping):
            continue
        signature = str(action.get("target_signature") or "")
        status = str(action.get("terminal_status") or "")
        if signature and status:
            action_statuses.setdefault(signature, []).append(
                (float(action.get("terminal_at", 0.0) or 0.0), status)
            )
    signatures = sorted(set(decisions) | set(action_statuses))
    targets = tuple(
        BandPhysicalActionAudit(
            target_signature=signature,
            result_count=int(
                dict(decisions.get(signature) or {}).get("result_count", 0) or 0
            ),
            settled_count=int(
                dict(decisions.get(signature) or {}).get("settled_count", 0) or 0
            ),
            recent_terminal_statuses=tuple(
                status
                for _at, status in sorted(
                    action_statuses.get(signature, ()),
                    key=lambda item: item[0],
                )[-4:]
            ),
        )
        for signature in signatures
    )
    return BandPhysicalActionAuditView(targets=targets)


def band_physical_target_eligibility(
    history: AdvisoryHistory,
    context: IntelligenceContext,
    *,
    continuity_key: str,
    target_signature: str,
    observed_at: float,
    retry_window_s: float,
) -> BandPhysicalTargetEligibility:
    """Return temporary retry eligibility for one exact target.

    Core terminal results are not service grades, forecasts, or disruption cost.
    This only answers whether a failed autonomous target may be tried again.
    """

    signature = str(target_signature or "").strip()
    if not signature:
        raise ValueError("new_horizon_physical_target_signature_required")
    now = float(observed_at)
    window = float(retry_window_s)
    if not math.isfinite(now) or now <= 0.0:
        raise ValueError("new_horizon_physical_eligibility_time_invalid")
    if not math.isfinite(window) or window <= 0.0:
        raise ValueError("new_horizon_physical_retry_window_invalid")
    summary = _summary(
        history,
        continuity_key=continuity_key,
    )
    decision = dict(summary["decisions"].get(signature) or {})
    status = str(decision.get("latest_terminal_status") or "").strip().lower()
    terminal_at = _finite_number(decision.get("latest_terminal_at")) or 0.0
    failed = status in {"failed", "rejected", "timed_out", "cancelled"}
    remaining = (
        max(0.0, terminal_at + window - now) if failed and terminal_at > 0.0 else 0.0
    )
    return BandPhysicalTargetEligibility(
        target_signature=signature,
        eligible=remaining <= 0.0,
        terminal_status=status,
        terminal_at=float(terminal_at),
        retry_remaining_s=float(remaining),
    )


def _vector_cell(
    value: object | None,
    *,
    text: str | None = None,
    grade: float | None = None,
    dimension: str = "component",
) -> dict[str, object]:
    if value is None:
        return {"state": "unavailable", "text": "N/A", "grade": None}
    if grade is not None and (
        isinstance(grade, bool)
        or type(grade) not in {int, float}
        or not math.isfinite(float(grade))
        or not valid_intelligence_grade(grade,dimension=dimension)
    ):
        raise ValueError("new_horizon_presentation_grade_invalid")
    rendered = str(value) if text is None else text
    return {"state": "available", "text": rendered, "grade": grade}


def _row_target_label(row: Mapping[str, Any]) -> str:
    parts: list[str] = []
    pband = row.get("pband")
    bands = tuple(row.get("ca_pool") or ())
    if type(pband) is int and bands:
        pool = "+".join(f"B{band}" for band in bands)
        parts.append(f"B{pband} / {pool}")
    nr_band = row.get("nr_band")
    if type(nr_band) is int and nr_band > 0:
        parts.append(f"n{nr_band}")
    return " + ".join(parts) or "N/A"


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or type(value) not in {int, float}:
        return None
    measured = float(value)
    return measured if math.isfinite(measured) else None


def _grade_value(
    grades: Mapping[str, Any],
    dimension: str,
) -> float | None:
    row = grades.get(dimension)
    if (
        type(row) in {int, float}
        and not isinstance(row, bool)
        and math.isfinite(float(row))
        and valid_intelligence_grade(row,dimension=dimension)
    ):
        return float(row)
    if not isinstance(row, Mapping) or row.get("status") != "graded":
        return None
    grade = row.get("grade")
    return (
        float(grade)
        if type(grade) in {int, float}
        and not isinstance(grade, bool)
        and math.isfinite(float(grade))
        and valid_intelligence_grade(grade,dimension=dimension)
        else None
    )


def _performance_cells(
    observed: Mapping[str, Any],
    *,
    grades: Mapping[str, Any] | None = None,
    baseline: Mapping[str, Any] | None = None,
    comparison: bool = False,
) -> dict[str, dict[str, object]]:
    grades = grades or {}
    baseline = baseline or {}

    def numeric_text(
        name: str,
        *,
        decimals: int,
        percentage: bool = False,
    ) -> str | None:
        after = (
            _bounded_percentage(observed.get(name))
            if percentage
            else _finite_number(observed.get(name))
        )
        if not comparison:
            return format(after, f".{decimals}f") if after is not None else None
        before = (
            _bounded_percentage(baseline.get(name))
            if percentage
            else _finite_number(baseline.get(name))
        )
        if before is None or after is None:
            return None
        return format(before, f".{decimals}f") + " -> " + format(after, f".{decimals}f")

    capacity_text = numeric_text("eci_points", decimals=1)
    latency_text = numeric_text("latency_ms", decimals=1)
    jitter_text = numeric_text("jitter_ms", decimals=1)
    communication_text = numeric_text("cqb_cell", decimals=2)
    internet_text = numeric_text(
        "internet_availability_pct",
        decimals=1,
        percentage=True,
    )
    overall_grade = _grade_value(grades, "performance_overall_outcome")
    displayed_overall_grade = project_intelligence_display_grade(overall_grade,dimension="service_outcome")
    overall_text = (
        f"{displayed_overall_grade:+.1f}"
        if displayed_overall_grade is not None
        else None
    )

    dimensions = {
        "performance_capacity": _vector_cell(
            capacity_text,
            text=capacity_text,
            grade=_grade_value(grades, "performance_capacity"),
        ),
        "performance_latency": _vector_cell(
            latency_text,
            text=latency_text,
            grade=_grade_value(grades, "performance_latency"),
        ),
        "performance_jitter": _vector_cell(
            jitter_text,
            text=jitter_text,
            grade=_grade_value(grades, "performance_jitter"),
        ),
        "performance_communication_quality": _vector_cell(
            communication_text,
            text=communication_text,
            grade=_grade_value(
                grades,
                "performance_communication_quality",
            ),
        ),
        "performance_internet_availability": _vector_cell(
            internet_text,
            text=internet_text,
            grade=None,
        ),
        "performance_overall_outcome": _vector_cell(
            overall_text,
            text=overall_text,
            grade=overall_grade,
            dimension="service_outcome",
        ),
    }
    if tuple(dimensions) != BAND_CONTEXT_DIMENSIONS:
        raise RuntimeError("new_horizon_band_context_dimension_mismatch")
    return dimensions


def _presentation_column(
    identity: Mapping[str, Any],
    *,
    role: str,
    label: str,
    observed: Mapping[str, Any],
    grades: Mapping[str, Any],
    maturity: str,
    reason: str,
    evidence_count: int,
    opened_at: float,
    due_at: float,
    closed_at: float,
    baseline: Mapping[str, Any] | None = None,
    comparison: bool = False,
) -> dict[str, Any]:
    return {
        "role": role,
        "label": label,
        "target": _row_target_label(identity),
        "scope_key": str(identity["target_signature"])
        + "|"
        + str(identity["scope_key"]),
        "cell_id": identity.get("cell_id"),
        "pci": identity.get("pci"),
        "earfcn": identity.get("earfcn"),
        "nr_band": identity.get("nr_band"),
        "nr_cell_id": identity.get("nr_cell_id"),
        "nr_pci": identity.get("nr_pci"),
        "nr_arfcn": identity.get("nr_arfcn"),
        "evidence_count": max(0, int(evidence_count)),
        "opened_at": float(opened_at),
        "due_at": float(due_at),
        "closed_at": float(closed_at),
        "maturity": maturity,
        "reason": reason,
        "dimensions": _performance_cells(
            observed,
            grades=grades,
            baseline=baseline,
            comparison=comparison,
        ),
    }


def _required_stage_identity(
    value: object,
    *,
    stage: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("new_horizon_referee_stage_identity_missing:" + stage)
    identity = dict(value)
    if not str(identity.get("target_signature") or "") or not str(
        identity.get("scope_key") or ""
    ):
        raise ValueError("new_horizon_referee_stage_identity_invalid:" + stage)
    return identity


def _window_label(opened_at: float, due_at: float) -> str:
    opened = datetime.fromtimestamp(float(opened_at))
    due = datetime.fromtimestamp(float(due_at))
    return (opened.strftime("%d %b %H:%M") + "–" + due.strftime("%H:%M")).upper()


def _elapsed_label(seconds: float) -> str:
    elapsed = max(0, int(seconds))
    minutes, remainder = divmod(elapsed, 60)
    return f"{minutes}m {remainder:02d}s"


def _timeline_columns(
    referee_timeline: Mapping[str, Any],
) -> list[dict[str, Any]]:
    identity = _required_stage_identity(
        referee_timeline.get("current_identity"),
        stage="current",
    )
    observed = dict(referee_timeline.get("current_window") or {})
    opened_at = float(referee_timeline.get("opened_at") or 0.0)
    due_at = float(referee_timeline.get("due_at") or 0.0)
    evaluated_at = min(
        due_at,
        max(opened_at, float(referee_timeline.get("evaluated_at") or opened_at)),
    )
    return [
        _presentation_column(
            identity,
            role="current",
            label=(
                "CURRENT · "
                + _window_label(opened_at, evaluated_at)
                + " · "
                + _elapsed_label(evaluated_at - opened_at)
            ),
            observed=observed,
            grades={},
            maturity="collecting",
            reason="Collecting this 10-minute context; no grade yet",
            evidence_count=int(observed.get("sample_count", 0) or 0),
            opened_at=opened_at,
            due_at=due_at,
            closed_at=0.0,
        )
    ]


def _recent_assessment_columns(
    referee_timeline: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_rows = referee_timeline.get("recent_assessments")
    if not isinstance(raw_rows, list):
        return []
    columns: list[dict[str, Any]] = []
    for index, raw in enumerate(
        reversed(raw_rows[:BAND_CONTEXT_RECENT_LIMIT]),
        start=1,
    ):
        if not isinstance(raw, Mapping):
            raise TypeError("new_horizon_recent_assessment_required")
        identity = _required_stage_identity(
            raw.get("identity"),
            stage=f"recent_{index}",
        )
        observed = raw.get("observed_vector")
        grades = raw.get("component_grades")
        if not isinstance(observed, Mapping) or not isinstance(grades, Mapping):
            raise TypeError("new_horizon_recent_assessment_vector_required")
        sample_count = int(observed.get("sample_count", 0) or 0)
        if sample_count == 0:
            continue  # Retained in diagnostic History, not a measured assessment.
        opened_at = float(raw.get("opened_at") or 0.0)
        due_at = float(raw.get("due_at") or 0.0)
        closed_at = float(raw.get("closed_at") or 0.0)
        maturity = str(raw.get("maturity") or "completed")
        if maturity not in {"completed", "partial"}:
            raise ValueError("new_horizon_recent_assessment_maturity_invalid")
        columns.append(
            _presentation_column(
                identity,
                role=f"recent_{len(columns) + 1}",
                label=_window_label(
                    opened_at,
                    closed_at if maturity == "partial" else due_at,
                ),
                observed=observed,
                grades=grades,
                maturity=maturity,
                reason=(
                    (
                        "Ten-minute context had no qualified samples; no grade"
                        if closed_at == due_at
                        else "Context ended before 10 minutes; no grade"
                    )
                    if maturity == "partial"
                    else "Absolute quality during this exact 10-minute window"
                ),
                evidence_count=sample_count,
                opened_at=opened_at,
                due_at=due_at,
                closed_at=closed_at,
            )
        )
    return columns


def band_presentation(
    *,
    referee_timeline: Mapping[str, Any],
) -> dict[str, Any]:
    """Build completed windows in time order, followed by the live window."""

    if not referee_timeline:
        return {
            "schema": BAND_CONTEXT_SCHEMA,
            "current_timeline": [],
            "recent_assessments": [],
            "history_note": "Current exact context is unavailable.",
        }
    timeline = _timeline_columns(referee_timeline)
    recent = _recent_assessment_columns(referee_timeline)
    return {
        "schema": BAND_CONTEXT_SCHEMA,
        "current_timeline": timeline,
        "recent_assessments": recent,
        "history_note": (
            f"{sum(row['maturity'] == 'completed' for row in recent)} completed"
            f" and {sum(row['maturity'] == 'partial' for row in recent)} partial"
            " window(s) · oldest to newest."
            if recent
            else "No completed or partial window yet."
        ),
    }


__all__ = (
    "BAND_EVIDENCE_CHECKPOINT",
    "BAND_EVIDENCE_SCHEMA",
    "BandPhysicalActionAudit",
    "BandPhysicalActionAuditView",
    "BandPhysicalTargetEligibility",
    "PERFORMANCE_EVIDENCE_SCHEMA",
    "PERFORMANCE_RECORD_TYPES",
    "band_physical_action_audit",
    "band_physical_target_eligibility",
    "band_presentation",
    "record_performance_evidence",
    "record_internet_pause",
    "record_physical_result",
    "record_target_retention",
    "update_band_observation_maxima",
)
