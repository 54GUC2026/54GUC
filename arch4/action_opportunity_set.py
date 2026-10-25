"""Capability-derived action opportunity owner.

This owner enumerates legal action shapes for Brain selection.  It neither
scores candidates nor executes them.  Every opportunity is explicit about
ownership, capability, evidence, uncertainty, disruption and safety; an
explicit do-nothing arm is always present.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from arch4.action_admission import action_descriptor
from arch4.action_opportunity_source_facts import (
    action_opportunity_source_fact_set_error,
)
from arch4.action_session_scope import CurrentSessionActionScope
from arch4.action_applicability import (
    canonical_applicability_state,
    evaluate_action_applicability,
)
from arch4.action_shape_legality import validate_action_shape
from arch4.core.behavior_disposition import (
    behavior_disposition_catalog,
    behavior_disposition_catalog_error,
)
from arch4.evidence_scope import EvidenceScope
from arch4.interfaces import LeverType


SCHEMA = "ActionOpportunitySet.v1"
OWNER = "arch4.action_opportunity_set"
OPPORTUNITY_SCHEMA = "ActionOpportunity.v1"
SOURCE_SNAPSHOT_SCHEMA = "ActionOpportunitySourceSnapshot.v1"

_OWNER_CLASSES = frozenset(
    {"optimization", "policy", "maintenance", "recovery", "service", "operator"}
)


class _FrozenDict(dict[str, Any]):
    """JSON-compatible mapping which rejects every mutation path."""

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("action_opportunity_source_snapshot_is_immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


@dataclass(frozen=True, slots=True)
class ActionOpportunitySourceSnapshot:
    """One complete, immutable input bundle for the opportunity ledger."""

    capabilities: Mapping[str, Any]
    current: Mapping[str, Any]
    source_facts: Mapping[str, Any]
    optimization_candidates: Sequence[Mapping[str, Any]] = ()
    neighbor_cells: Sequence[Mapping[str, Any]] = ()
    nr_candidates: Sequence[Mapping[str, Any]] = ()
    requested_actions: Sequence[Mapping[str, Any]] = ()
    behavior_catalog: Mapping[str, Any] = field(
        default_factory=behavior_disposition_catalog
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "capabilities",
            _freeze_json_mapping(_jsonable_mapping(self.capabilities)),
        )
        object.__setattr__(
            self,
            "current",
            _freeze_json_mapping(_jsonable_mapping(self.current)),
        )
        object.__setattr__(
            self,
            "source_facts",
            _freeze_json_mapping(_jsonable_mapping(self.source_facts)),
        )
        for field_name in (
            "optimization_candidates",
            "neighbor_cells",
            "nr_candidates",
            "requested_actions",
        ):
            values = _jsonable_mapping_sequence(getattr(self, field_name))
            object.__setattr__(
                self,
                field_name,
                tuple(_freeze_json_mapping(value) for value in values),
            )
        object.__setattr__(
            self,
            "behavior_catalog",
            _freeze_json_mapping(_jsonable_mapping(self.behavior_catalog)),
        )

    def as_dict(self) -> dict[str, Any]:
        content = {
            "schema": SOURCE_SNAPSHOT_SCHEMA,
            "owner": OWNER,
            "capabilities": _jsonable_mapping(self.capabilities),
            "current": _jsonable_mapping(self.current),
            "source_facts": _jsonable_mapping(self.source_facts),
            "optimization_candidates": _jsonable_mapping_sequence(
                self.optimization_candidates
            ),
            "neighbor_cells": _jsonable_mapping_sequence(self.neighbor_cells),
            "nr_candidates": _jsonable_mapping_sequence(self.nr_candidates),
            "requested_actions": _jsonable_mapping_sequence(
                self.requested_actions
            ),
            "behavior_catalog": _jsonable_mapping(self.behavior_catalog),
        }
        return {**content, "source_snapshot_id": _content_digest(content)}


@dataclass(frozen=True, slots=True)
class ActionOpportunity:
    lever: str
    action: str
    params: Mapping[str, Any]
    owner_class: str
    source: str
    reason: str
    capability_key: str
    legal: bool
    executable: bool
    disruption_cost: float
    uncertainty: float
    safety_applicability: str
    capability_admitted: bool = True
    shape_valid: bool = True
    shape_error: str = ""
    source_admitted: bool = True
    source_unavailability_reason: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    schedule: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        action_shape = {
            "schema": OPPORTUNITY_SCHEMA,
            "owner": OWNER,
            "lever": self.lever,
            "action": self.action,
            "params": _jsonable_mapping(self.params),
            "owner_class": self.owner_class,
            "capability_key": self.capability_key,
        }
        opportunity_id = _opportunity_shape_id(action_shape)
        content = {
            "schema": OPPORTUNITY_SCHEMA,
            "owner": OWNER,
            "opportunity_id": opportunity_id,
            "lever": self.lever,
            "action": self.action,
            "params": _jsonable_mapping(self.params),
            "owner_class": self.owner_class,
            "source": self.source,
            "reason": self.reason,
            "capability_key": self.capability_key,
            "legal": bool(self.legal),
            "executable": bool(self.executable),
            "disruption_cost": _bounded(self.disruption_cost),
            "uncertainty": _bounded(self.uncertainty),
            "safety_applicability": self.safety_applicability,
            "capability_admitted": bool(self.capability_admitted),
            "shape_valid": bool(self.shape_valid),
            "shape_error": str(self.shape_error or ""),
            "source_admitted": bool(self.source_admitted),
            "source_unavailability_reason": str(
                self.source_unavailability_reason or ""
            ),
            "evidence": _jsonable_mapping(self.evidence),
            "schedule": _jsonable_mapping(self.schedule),
        }
        digest = hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {**content, "opportunity_record_id": digest}


def build_action_opportunity_set(
    *,
    learning_context_key: str,
    action_context_key: str,
    evidence_scope: Mapping[str, Any],
    source_snapshot: ActionOpportunitySourceSnapshot,
) -> dict[str, Any]:
    """Build the complete known opportunity set for one accepted context."""

    if not str(learning_context_key or "").strip():
        raise ValueError("action_opportunity_learning_context_required")
    if not str(action_context_key or "").strip():
        raise ValueError("action_opportunity_action_context_required")
    if not isinstance(source_snapshot, ActionOpportunitySourceSnapshot):
        raise TypeError("action_opportunity_source_snapshot_required")
    source_row = source_snapshot.as_dict()
    source_error = action_opportunity_source_snapshot_error(source_row)
    if source_error:
        raise ValueError(f"action_opportunity_source_snapshot_invalid:{source_error}")
    capabilities = _mapping(source_row.get("capabilities"))
    current = _mapping(source_row.get("current"))
    optimization_candidates = _mapping_sequence(
        source_row.get("optimization_candidates")
    )
    neighbor_cells = _mapping_sequence(source_row.get("neighbor_cells"))
    nr_candidates = _mapping_sequence(source_row.get("nr_candidates"))
    requested_actions = _mapping_sequence(source_row.get("requested_actions"))
    source_fact_set = _mapping(source_row.get("source_facts"))
    source_facts = _mapping_sequence(source_fact_set.get("facts"))

    scope_error = ""
    scope_kind = ""
    accepted_scope: EvidenceScope | CurrentSessionActionScope | None
    try:
        accepted_scope = EvidenceScope.from_mapping(evidence_scope)
        scope_kind = "durable_evidence"
    except (TypeError, ValueError) as exc:
        durable_error = str(exc or "evidence_scope_invalid")
        try:
            accepted_scope = CurrentSessionActionScope.from_mapping(evidence_scope)
            scope_kind = "current_session_action"
        except (TypeError, ValueError) as session_exc:
            accepted_scope = None
            scope_error = (
                f"durable={durable_error};"
                f"session={str(session_exc or 'action_session_scope_invalid')}"
            )
    if accepted_scope is not None:
        fact_scope_error = _source_fact_scope_error(
            source_fact_set,
            accepted_scope=accepted_scope,
        )
        if fact_scope_error:
            raise ValueError(
                f"action_opportunity_source_fact_scope_invalid:{fact_scope_error}"
            )
    applicability_state = canonical_applicability_state(current)

    do_nothing = _do_nothing_opportunity()
    capability_counterfactuals = _capability_radio_counterfactuals(
        capabilities=capabilities,
        current=current,
    )
    optimization_rows = _optimization_opportunities(
        capabilities=capabilities,
        candidates=optimization_candidates,
    )
    restoration_rows = _restoration_opportunities(
        capabilities=capabilities,
        current=current,
    )
    cell_rows = _cell_opportunities(
        capabilities=capabilities,
        current=current,
        neighbors=neighbor_cells,
    )
    nr_rows = _nr_opportunities(
        capabilities=capabilities,
        current=current,
        candidates=nr_candidates,
    )
    requested_rows = _requested_opportunities(
        capabilities=capabilities,
        requests=requested_actions,
    )
    source_fact_rows = _source_fact_opportunities(
        capabilities=capabilities,
        facts=source_facts,
    )
    rows = [
        do_nothing,
        *capability_counterfactuals,
        *optimization_rows,
        *restoration_rows,
        *cell_rows,
        *nr_rows,
        *requested_rows,
        *source_fact_rows,
    ]
    rows = _apply_source_constraints(rows, facts=source_facts)
    values = _materialize_opportunity_rows(
        rows,
        accepted_scope=accepted_scope,
        scope_kind=scope_kind,
        scope_error=scope_error,
        applicability_state=applicability_state,
    )
    selection_rows = [
        do_nothing,
        *optimization_rows,
        *[
            row
            for row in (
                *cell_rows,
                *nr_rows,
                *requested_rows,
                *source_fact_rows,
            )
            if row.owner_class == "optimization" and row.source_admitted
        ],
    ]
    selection_rows = _apply_source_constraints(
        selection_rows,
        facts=source_facts,
    )
    content = {
        **_base_opportunity_set_content(
            learning_context_key=learning_context_key,
            action_context_key=action_context_key,
            accepted_scope=accepted_scope,
            scope_kind=scope_kind,
            scope_error=scope_error,
            applicability_state=applicability_state,
            values=values,
        ),
        "selection_space_id": _selection_space_id(
            learning_context_key=learning_context_key,
            action_context_key=action_context_key,
            accepted_scope=accepted_scope,
            scope_kind=scope_kind,
            scope_error=scope_error,
            applicability_state=applicability_state,
            rows=selection_rows,
        ),
        "source_snapshot_id": str(source_row.get("source_snapshot_id") or ""),
        "source_snapshot": source_row,
        "behavior_catalog": _mapping(source_row.get("behavior_catalog")),
        "capability_action_coverage": _capability_action_coverage(capabilities),
    }
    return {**content, "opportunity_set_id": _row_record_id(content)}


def _do_nothing_opportunity() -> ActionOpportunity:
    return ActionOpportunity(
        lever="NONE",
        action="do_nothing",
        params={},
        owner_class="policy",
        source="mandatory_counterfactual",
        reason="retain_current_configuration",
        capability_key="",
        legal=True,
        executable=True,
        disruption_cost=0.0,
        uncertainty=0.0,
        safety_applicability="bouncer_observes_no_mutation",
    )


def _selection_space_id(
    *,
    learning_context_key: str,
    action_context_key: str,
    accepted_scope: EvidenceScope | CurrentSessionActionScope | None,
    scope_kind: str,
    scope_error: str,
    applicability_state: Mapping[str, Any],
    rows: Sequence[ActionOpportunity],
) -> str:
    values = _materialize_opportunity_rows(
        rows,
        accepted_scope=accepted_scope,
        scope_kind=scope_kind,
        scope_error=scope_error,
        applicability_state=applicability_state,
    )
    content = _base_opportunity_set_content(
        learning_context_key=learning_context_key,
        action_context_key=action_context_key,
        accepted_scope=accepted_scope,
        scope_kind=scope_kind,
        scope_error=scope_error,
        applicability_state=applicability_state,
        values=values,
    )
    # The contextual-Bandit draw is keyed to the learning selection space, not
    # to the later physical-action claim scope. Preserve that established draw
    # epoch while the serialized owner contract names both contexts explicitly.
    seed_content = {
        key: value
        for key, value in content.items()
        if key not in {"learning_context_key", "action_context_key"}
    }
    seed_content["context_key"] = str(learning_context_key or "")
    return _row_record_id(seed_content)


def _materialize_opportunity_rows(
    rows: Sequence[ActionOpportunity],
    *,
    accepted_scope: EvidenceScope | CurrentSessionActionScope | None,
    scope_kind: str,
    scope_error: str,
    applicability_state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for opportunity in rows:
        row = opportunity.as_dict()
        applicability = evaluate_action_applicability(
            str(row.get("lever") or ""),
            _mapping(row.get("params")),
            applicability_state,
            action=str(row.get("action") or ""),
        )
        row["applicability"] = applicability.as_dict()
        if not applicability.applicable:
            row["executable"] = False
        row["provenance"] = [
            {
                "source": str(row.get("source") or ""),
                "reason": str(row.get("reason") or ""),
                "evidence": _mapping(row.get("evidence")),
                "schedule": _mapping(row.get("schedule")),
            }
        ]
        availability_reasons: list[str] = []
        if row.get("shape_valid") is not True:
            availability_reasons.append(
                str(row.get("shape_error") or "action_shape_invalid")
            )
        if row.get("capability_admitted") is not True:
            capability_key = str(row.get("capability_key") or "unknown")
            availability_reasons.append(f"capability_not_admitted:{capability_key}")
        if row.get("source_admitted") is not True:
            availability_reasons.append(
                str(
                    row.get("source_unavailability_reason")
                    or "source_candidate_not_admitted"
                )
            )
        if not applicability.applicable:
            availability_reasons.append(
                "no_effect:"
                + str(applicability.comparison_basis or "accepted_current_state")
            )
        if accepted_scope is None and row["action"] != "do_nothing":
            row["legal"] = False
            row["executable"] = False
            availability_reasons.append(scope_error or "evidence_scope_invalid")
        if (
            scope_kind == "current_session_action"
            and str(row.get("owner_class") or "") == "optimization"
        ):
            row["legal"] = False
            row["executable"] = False
            availability_reasons.append(
                "durable_evidence_scope_required_for_optimization"
            )
        row["availability_reasons"] = sorted(set(availability_reasons))
        row["opportunity_record_id"] = _row_record_id(row)
        opportunity_id = str(row["opportunity_id"])
        existing = deduped.get(opportunity_id)
        deduped[opportunity_id] = (
            _merge_opportunity_rows(existing, row) if existing else row
        )
    return sorted(
        deduped.values(),
        key=lambda row: (
            0 if row.get("action") == "do_nothing" else 1,
            str(row.get("opportunity_id") or ""),
        ),
    )


def _base_opportunity_set_content(
    *,
    learning_context_key: str,
    action_context_key: str,
    accepted_scope: EvidenceScope | CurrentSessionActionScope | None,
    scope_kind: str,
    scope_error: str,
    applicability_state: Mapping[str, Any],
    values: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "learning_context_key": str(learning_context_key or ""),
        "action_context_key": str(action_context_key or ""),
        "identity_code": str(accepted_scope.identity_code if accepted_scope else ""),
        "session_scope_key": str(
            accepted_scope.session_scope_key if accepted_scope else ""
        ),
        "source_epoch": str(accepted_scope.source_epoch if accepted_scope else ""),
        "source_sequence": int(
            accepted_scope.source_sequence if accepted_scope else 0
        ),
        "applicability_state": applicability_state,
        "scope_kind": scope_kind,
        "evidence_scope": (
            accepted_scope.as_dict()
            if isinstance(accepted_scope, EvidenceScope)
            else {}
        ),
        "action_session_scope": (
            accepted_scope.as_dict()
            if isinstance(accepted_scope, CurrentSessionActionScope)
            else {}
        ),
        "scope_admitted": accepted_scope is not None,
        "scope_rejection_reason": scope_error,
        "opportunities": [dict(row) for row in values],
        "opportunity_count": len(values),
        "legal_count": sum(1 for row in values if row["legal"]),
        "executable_count": sum(1 for row in values if row["executable"]),
        "do_nothing_opportunity_id": str(values[0]["opportunity_id"]),
        "known_action_coverage": sorted(
            {str(row["lever"]) for row in values if row["lever"] != "NONE"}
        ),
    }


def action_opportunity_source_snapshot_error(
    value: Mapping[str, Any] | None,
) -> str:
    row = _mapping(value)
    if (
        row.get("schema") != SOURCE_SNAPSHOT_SCHEMA
        or row.get("owner") != OWNER
    ):
        return "opportunity_source_snapshot_owner_schema_invalid"
    content = {
        key: item for key, item in row.items() if key != "source_snapshot_id"
    }
    if str(row.get("source_snapshot_id") or "") != _content_digest(content):
        return "opportunity_source_snapshot_checksum_invalid"
    for key in (
        "capabilities",
        "current",
        "source_facts",
        "behavior_catalog",
    ):
        if not isinstance(row.get(key), Mapping):
            return f"opportunity_source_snapshot_{key}_invalid"
    for key in (
        "optimization_candidates",
        "neighbor_cells",
        "nr_candidates",
        "requested_actions",
    ):
        value_rows = row.get(key)
        if not isinstance(value_rows, list) or any(
            not isinstance(item, Mapping) for item in value_rows
        ):
            return f"opportunity_source_snapshot_{key}_invalid"
    catalog_error = behavior_disposition_catalog_error(
        _mapping(row.get("behavior_catalog"))
    )
    if catalog_error:
        return f"opportunity_source_snapshot_behavior_catalog_invalid:{catalog_error}"
    fact_error = action_opportunity_source_fact_set_error(
        _mapping(row.get("source_facts"))
    )
    if fact_error:
        return f"opportunity_source_snapshot_source_facts_invalid:{fact_error}"
    return ""


def action_opportunity_set_error(value: Mapping[str, Any] | None) -> str:
    row = _mapping(value)
    if row.get("schema") != SCHEMA or row.get("owner") != OWNER:
        return "opportunity_set_owner_schema_invalid"
    content = {
        key: item for key, item in row.items() if key != "opportunity_set_id"
    }
    if not str(row.get("opportunity_set_id") or "") or str(
        row.get("opportunity_set_id")
    ) != _row_record_id(content):
        return "opportunity_set_checksum_invalid"
    if "context_key" in row:
        return "opportunity_set_ambiguous_context_forbidden"
    if not str(row.get("learning_context_key") or ""):
        return "opportunity_set_learning_context_missing"
    if not str(row.get("action_context_key") or ""):
        return "opportunity_set_action_context_missing"
    source_snapshot = _mapping(row.get("source_snapshot"))
    source_error = action_opportunity_source_snapshot_error(source_snapshot)
    if source_error:
        return f"opportunity_set_source_snapshot_invalid:{source_error}"
    if str(row.get("source_snapshot_id") or "") != str(
        source_snapshot.get("source_snapshot_id") or ""
    ):
        return "opportunity_set_source_snapshot_identity_mismatch"
    if _mapping(row.get("behavior_catalog")) != _mapping(
        source_snapshot.get("behavior_catalog")
    ):
        return "opportunity_set_behavior_catalog_projection_mismatch"
    if list(row.get("capability_action_coverage") or []) != _capability_action_coverage(
        _mapping(source_snapshot.get("capabilities"))
    ):
        return "opportunity_set_capability_coverage_mismatch"
    values = row.get("opportunities")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return "opportunity_set_rows_invalid"
    opportunities = [dict(item) for item in values if isinstance(item, Mapping)]
    if len(opportunities) != len(values):
        return "opportunity_set_row_invalid"
    identifiers: list[str] = []
    applicability_state = _mapping(row.get("applicability_state"))
    if applicability_state != canonical_applicability_state(applicability_state):
        return "opportunity_applicability_state_invalid"
    for opportunity in opportunities:
        opportunity_id = str(opportunity.get("opportunity_id") or "")
        if not opportunity_id or opportunity_id != _opportunity_shape_id(opportunity):
            return "opportunity_id_invalid"
        if str(opportunity.get("opportunity_record_id") or "") != _row_record_id(
            opportunity
        ):
            return "opportunity_record_checksum_invalid"
        if opportunity.get("executable") is True and opportunity.get("legal") is not True:
            return "executable_opportunity_not_legal"
        if opportunity.get("action") != "do_nothing":
            verdict = validate_action_shape(
                str(opportunity.get("lever") or ""),
                _mapping(opportunity.get("params")),
            )
            if opportunity.get("shape_valid") is not verdict.legal:
                return "opportunity_shape_projection_mismatch"
            expected_shape_error = "" if verdict.legal else str(verdict.code or verdict.reason)
            if str(opportunity.get("shape_error") or "") != expected_shape_error:
                return "opportunity_shape_error_mismatch"
            applicability = evaluate_action_applicability(
                str(opportunity.get("lever") or ""),
                _mapping(opportunity.get("params")),
                applicability_state,
                action=str(opportunity.get("action") or ""),
            )
            if _mapping(opportunity.get("applicability")) != applicability.as_dict():
                return "opportunity_applicability_projection_mismatch"
            expected_legal = bool(
                verdict.legal
                and opportunity.get("capability_admitted") is True
                and row.get("scope_admitted") is True
                and not (
                    row.get("scope_kind") == "current_session_action"
                    and str(opportunity.get("owner_class") or "") == "optimization"
                )
            )
            if opportunity.get("legal") is not expected_legal:
                return "opportunity_legality_projection_mismatch"
            if not applicability.applicable and opportunity.get("executable") is True:
                return "inapplicable_opportunity_executable"
            if (
                opportunity.get("source_admitted") is not True
                and opportunity.get("executable") is True
            ):
                return "source_blocked_opportunity_executable"
        identifiers.append(opportunity_id)
    if len(set(identifiers)) != len(identifiers):
        return "opportunity_id_duplicate"
    do_nothing = [
        item for item in opportunities if item.get("action") == "do_nothing"
    ]
    if len(do_nothing) != 1 or do_nothing[0].get("executable") is not True:
        return "do_nothing_arm_invalid"
    if str(row.get("do_nothing_opportunity_id") or "") != str(
        do_nothing[0].get("opportunity_id") or ""
    ):
        return "do_nothing_arm_identity_mismatch"
    if int(row.get("opportunity_count") or -1) != len(opportunities):
        return "opportunity_count_mismatch"
    if int(row.get("legal_count") or -1) != sum(
        item.get("legal") is True for item in opportunities
    ):
        return "opportunity_legal_count_mismatch"
    if int(row.get("executable_count") or -1) != sum(
        item.get("executable") is True for item in opportunities
    ):
        return "opportunity_executable_count_mismatch"
    scope: EvidenceScope | CurrentSessionActionScope | None = None
    if row.get("scope_admitted") is True:
        try:
            if row.get("scope_kind") == "durable_evidence":
                scope = (
                    EvidenceScope.from_mapping(_mapping(row.get("evidence_scope")))
                )
            elif row.get("scope_kind") == "current_session_action":
                scope = CurrentSessionActionScope.from_mapping(
                    _mapping(row.get("action_session_scope"))
                )
            else:
                return "opportunity_scope_kind_invalid"
        except (TypeError, ValueError):
            return "opportunity_scope_invalid"
        if (
            scope.identity_code != str(row.get("identity_code") or "")
            or scope.session_scope_key != str(row.get("session_scope_key") or "")
            or scope.source_epoch != str(row.get("source_epoch") or "")
            or scope.source_sequence != int(row.get("source_sequence") or 0)
        ):
            return "opportunity_evidence_scope_projection_mismatch"
        fact_scope_error = _source_fact_scope_error(
            _mapping(source_snapshot.get("source_facts")),
            accepted_scope=scope,
        )
        if fact_scope_error:
            return f"opportunity_source_fact_scope_invalid:{fact_scope_error}"
    elif any(
        item.get("action") != "do_nothing" and item.get("executable") is True
        for item in opportunities
    ):
        return "unscoped_mutation_opportunity_executable"
    capabilities = _mapping(source_snapshot.get("capabilities"))
    current = _mapping(source_snapshot.get("current"))
    selection_rows: list[ActionOpportunity] = [
        _do_nothing_opportunity(),
        *_optimization_opportunities(
            capabilities=capabilities,
            candidates=_mapping_sequence(
                source_snapshot.get("optimization_candidates")
            ),
        ),
    ]
    source_cell_rows = _cell_opportunities(
        capabilities=capabilities,
        current=current,
        neighbors=_mapping_sequence(source_snapshot.get("neighbor_cells")),
    )
    source_nr_rows = _nr_opportunities(
        capabilities=capabilities,
        current=current,
        candidates=_mapping_sequence(source_snapshot.get("nr_candidates")),
    )
    source_requested_rows = _requested_opportunities(
        capabilities=capabilities,
        requests=_mapping_sequence(source_snapshot.get("requested_actions")),
    )
    source_fact_rows = _source_fact_opportunities(
        capabilities=capabilities,
        facts=_mapping_sequence(
            _mapping(source_snapshot.get("source_facts")).get("facts")
        ),
    )
    selection_rows.extend(
        value
        for value in (
            *source_cell_rows,
            *source_nr_rows,
            *source_requested_rows,
            *source_fact_rows,
        )
        if value.owner_class == "optimization" and value.source_admitted
    )
    selection_rows = _apply_source_constraints(
        selection_rows,
        facts=_mapping_sequence(
            _mapping(source_snapshot.get("source_facts")).get("facts")
        ),
    )
    expected_selection_space_id = _selection_space_id(
        learning_context_key=str(row.get("learning_context_key") or ""),
        action_context_key=str(row.get("action_context_key") or ""),
        accepted_scope=scope,
        scope_kind=str(row.get("scope_kind") or ""),
        scope_error=str(row.get("scope_rejection_reason") or ""),
        applicability_state=applicability_state,
        rows=selection_rows,
    )
    if str(row.get("selection_space_id") or "") != expected_selection_space_id:
        return "opportunity_selection_space_identity_mismatch"
    return ""


def _capability_radio_counterfactuals(
    *,
    capabilities: Mapping[str, Any],
    current: Mapping[str, Any],
) -> list[ActionOpportunity]:
    authority = _bands(current.get("authority_bands"))
    if not authority:
        return []
    lever = _radio_configuration_lever(capabilities)
    current_anchor = _positive_int(
        current.get("current_pband") or current.get("primary_band")
    )
    current_pool = _bands(
        current.get("ca_pool")
        or current.get("selected_bands")
        or current.get("active_bands")
    )
    shapes: set[tuple[int, tuple[int, ...]]] = set()
    for anchor in authority:
        shapes.add((anchor, (anchor,)))
        shapes.add((anchor, authority))
        if current_pool:
            adjusted = _bands((anchor, *current_pool))
            shapes.add((anchor, adjusted))
    if current_anchor > 0 and current_anchor in authority:
        base = current_pool or (current_anchor,)
        if current_anchor not in base:
            base = _bands((current_anchor, *base))
        for band in authority:
            if band not in base:
                shapes.add((current_anchor, _bands((*base, band))))
        for band in base:
            if band != current_anchor:
                shapes.add(
                    (
                        current_anchor,
                        tuple(item for item in base if item != band),
                    )
                )
    rows: list[ActionOpportunity] = []
    for anchor, pool in sorted(shapes, key=lambda item: (item[0], item[1])):
        params = (
            {
                "pband": anchor,
                "ca_pool": list(pool),
            }
            if lever is LeverType.PBAND
            else {"bands": list(pool)}
        )
        rows.append(
            _opportunity(
                lever=lever,
                action="apply_radio_configuration",
                params=params,
                owner_class="optimization",
                source="capability_legal_radio_shape",
                reason="counterfactual_radio_shape_requires_ranked_evidence",
                capabilities=capabilities,
                disruption_cost=0.65,
                uncertainty=1.0,
                source_admitted=False,
                source_unavailability_reason=(
                    "counterfactual_requires_smart_loop_or_policy_evidence"
                ),
                evidence={
                    "counterfactual": True,
                    "authority_bands": list(authority),
                    "current_anchor": current_anchor,
                    "current_pool": list(current_pool),
                },
            )
        )
    return rows


def _optimization_opportunities(
    *,
    capabilities: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> list[ActionOpportunity]:
    rows: list[ActionOpportunity] = []
    lever = _radio_configuration_lever(capabilities)
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        target = _positive_int(candidate.get("target_band") or candidate.get("band"))
        bands = _bands(
            candidate.get("target_bands")
            or candidate.get("bands")
            or candidate.get("ca_pool")
        )
        if target <= 0 and bands:
            target = bands[0]
        if target <= 0:
            continue
        if target not in bands:
            bands = (target, *bands)
        params = (
            {
                "pband": target,
                "ca_pool": list(_bands(bands)),
            }
            if lever is LeverType.PBAND
            else {"bands": list(_bands(bands))}
        )
        rows.append(
            _opportunity(
                lever=lever,
                action="apply_radio_configuration",
                params=params,
                owner_class="optimization",
                source=str(candidate.get("source") or "optimization_candidate"),
                reason=str(candidate.get("reason") or candidate.get("reason_code") or "candidate"),
                capabilities=capabilities,
                disruption_cost=_float(candidate.get("disruption_cost"), 0.65),
                uncertainty=_float(candidate.get("uncertainty"), 1.0 - _float(candidate.get("confidence"), 0.0)),
                source_admitted=bool(candidate.get("source_admitted", True)),
                source_unavailability_reason=str(
                    candidate.get("source_unavailability_reason") or ""
                ),
                evidence=_mapping(candidate.get("evidence"))
                or {
                    "confidence": _float(candidate.get("confidence"), 0.0),
                    "evidence_count": _int(candidate.get("evidence_count"), 0),
                },
            )
        )
    return rows


def _restoration_opportunities(
    *, capabilities: Mapping[str, Any], current: Mapping[str, Any]
) -> list[ActionOpportunity]:
    required = current.get("radio_restoration_required") is True
    authority = _bands(
        current.get("known_good_bandset") or current.get("authority_bands")
    )
    if not required and not authority:
        return []
    lever = _radio_configuration_lever(capabilities)
    preferred_pband = _positive_int(
        current.get("preferred_pband") or current.get("current_pband")
    )
    if preferred_pband <= 0 and authority:
        preferred_pband = authority[0]
    if lever is LeverType.PBAND:
        params = {
            "pband": preferred_pband,
            "ca_pool": list(authority),
            "restore": True,
        }
    elif authority:
        params = {"bands": list(authority), "restore": True}
    else:
        params = {"bands": [], "auto": True, "restore": True}
    return [
        _opportunity(
            lever=lever,
            action="restore_radio_configuration",
            params=params,
            owner_class="recovery",
            source="explicit_radio_restoration_requirement",
            reason=(
                "restore_unrestricted_radio_configuration"
                if required
                else "counterfactual_known_good_radio_restoration"
            ),
            capabilities=capabilities,
            disruption_cost=0.55,
            uncertainty=0.15,
            evidence={
                "packages": [
                    {
                        "required": bool(required),
                        "radio_restoration_required": bool(required),
                        "radio_restricted": current.get("radio_restricted") is True,
                    }
                ],
                "radio_restoration_required": bool(required),
                "radio_restricted": current.get("radio_restricted") is True,
            },
            source_admitted=required,
            source_unavailability_reason=(
                "" if required else "radio_restoration_not_required"
            ),
        )
    ]


def _cell_opportunities(
    *,
    capabilities: Mapping[str, Any],
    current: Mapping[str, Any],
    neighbors: Sequence[Mapping[str, Any]],
) -> list[ActionOpportunity]:
    rows: list[ActionOpportunity] = []
    release_due = current.get("cell_lock_release_due") is True
    if release_due or current.get("cell_lock_active") is True:
        rows.append(
            _opportunity(
                lever=LeverType.CELL_LOCK,
                action="release_cell_lock",
                params={"unlock": True},
                owner_class="maintenance",
                source="explicit_cell_lock_release_due",
                reason="release_or_expire_cell_lock",
                capabilities=capabilities,
                disruption_cost=0.45,
                uncertainty=0.10,
                evidence={
                    "cell_lock_release_due": bool(release_due),
                    "cell_lock_active": current.get("cell_lock_active") is True,
                },
                source_admitted=release_due,
                source_unavailability_reason=(
                    "" if release_due else "cell_lock_release_not_due"
                ),
            )
        )
    for neighbor in neighbors:
        if not isinstance(neighbor, Mapping):
            continue
        pci = _positive_int(neighbor.get("pci"))
        earfcn = _positive_int(neighbor.get("earfcn"))
        if pci <= 0 or earfcn <= 0:
            continue
        params = {"pci": pci, "earfcn": earfcn}
        band = _positive_int(neighbor.get("band"))
        if band > 0:
            params["band"] = band
        rows.append(
            _opportunity(
                lever=LeverType.CELL_LOCK,
                action="apply_cell_lock",
                params=params,
                owner_class="optimization",
                source=str(neighbor.get("source") or "neighbor_cell_evidence"),
                reason=str(neighbor.get("reason") or "candidate_cell"),
                capabilities=capabilities,
                disruption_cost=0.80,
                uncertainty=_float(neighbor.get("uncertainty"), 0.65),
                evidence=_mapping(neighbor.get("evidence")),
                source_admitted=bool(neighbor.get("source_admitted", False)),
                source_unavailability_reason=str(
                    neighbor.get("source_unavailability_reason")
                    or "neighbor_evidence_not_admitted"
                ),
            )
        )
    return rows


def _nr_opportunities(
    *,
    capabilities: Mapping[str, Any],
    current: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> list[ActionOpportunity]:
    del capabilities, current, candidates
    return []


def _requested_opportunities(
    *,
    capabilities: Mapping[str, Any],
    requests: Sequence[Mapping[str, Any]],
) -> list[ActionOpportunity]:
    rows: list[ActionOpportunity] = []
    for request in requests:
        if not isinstance(request, Mapping):
            continue
        try:
            lever = LeverType[str(request.get("lever") or "").strip().upper()]
        except KeyError:
            continue
        owner_class = str(request.get("owner_class") or "service").strip().lower()
        if owner_class not in _OWNER_CLASSES:
            owner_class = "service"
        rows.append(
            _opportunity(
                lever=lever,
                action=str(request.get("action") or lever.name.lower()),
                params=_mapping(request.get("params")),
                owner_class=owner_class,
                source=str(request.get("source") or "explicit_request"),
                reason=str(request.get("reason") or "requested_action"),
                capabilities=capabilities,
                disruption_cost=_float(request.get("disruption_cost"), 0.5),
                uncertainty=_float(request.get("uncertainty"), 0.25),
                evidence=_mapping(request.get("evidence")),
                schedule=_mapping(request.get("schedule")),
                source_admitted=bool(request.get("source_admitted", True)),
                source_unavailability_reason=str(
                    request.get("source_unavailability_reason") or ""
                ),
            )
        )
    return rows


def _source_fact_opportunities(
    *,
    capabilities: Mapping[str, Any],
    facts: Sequence[Mapping[str, Any]],
) -> list[ActionOpportunity]:
    rows: list[ActionOpportunity] = []
    for fact in facts:
        kind = str(fact.get("fact_kind") or "")
        if kind not in {"candidate", "schedule", "lease"}:
            continue
        try:
            lever = LeverType[str(fact.get("lever") or "").strip().upper()]
        except KeyError:
            continue
        active = fact.get("active") is True
        source_admitted = bool(
            active and fact.get("source_admitted") is True
        )
        evidence = _mapping(fact.get("evidence"))
        evidence.update(
            {
                "behavior_id": str(fact.get("behavior_id") or ""),
                "fact_id": str(fact.get("fact_id") or ""),
                "fact_kind": kind,
                "fact_state_id": str(fact.get("state_id") or ""),
                "fact_active": active,
                "fact_source_owner": str(fact.get("source_owner") or ""),
            }
        )
        rows.append(
            _opportunity(
                lever=lever,
                action=str(fact.get("action") or lever.name.lower()),
                params=_mapping(fact.get("params")),
                owner_class=str(fact.get("owner_class") or "service"),
                source=str(fact.get("source_owner") or "source_fact"),
                reason=str(fact.get("reason") or "source_fact"),
                capabilities=capabilities,
                disruption_cost=_float(
                    evidence.get("disruption_cost"),
                    0.5,
                ),
                uncertainty=_float(evidence.get("uncertainty"), 0.25),
                evidence=evidence,
                schedule=_mapping(fact.get("schedule")),
                source_admitted=source_admitted,
                source_unavailability_reason=(
                    ""
                    if source_admitted
                    else str(
                        fact.get("source_unavailability_reason")
                        or (
                            "source_fact_inactive"
                            if not active
                            else "source_fact_not_admitted"
                        )
                    )
                ),
            )
        )
    return rows


def _apply_source_constraints(
    rows: Sequence[ActionOpportunity],
    *,
    facts: Sequence[Mapping[str, Any]],
) -> list[ActionOpportunity]:
    constraints = [
        dict(fact)
        for fact in facts
        if str(fact.get("fact_kind") or "") == "constraint"
        and fact.get("active") is True
        and fact.get("source_admitted") is True
    ]
    if not constraints:
        return list(rows)
    out: list[ActionOpportunity] = []
    for row in rows:
        blocked_by = [
            fact for fact in constraints if _constraint_blocks(row, fact)
        ]
        if not blocked_by:
            out.append(row)
            continue
        reasons = sorted(
            {
                str(
                    _mapping(fact.get("applies_to")).get("blocked_reason")
                    or fact.get("reason")
                    or "source_constraint_active"
                )
                for fact in blocked_by
            }
        )
        evidence = _mapping(row.evidence)
        evidence["source_constraints"] = [
            {
                "behavior_id": str(fact.get("behavior_id") or ""),
                "fact_id": str(fact.get("fact_id") or ""),
                "state_id": str(fact.get("state_id") or ""),
                "reason": str(
                    _mapping(fact.get("applies_to")).get("blocked_reason")
                    or fact.get("reason")
                    or "source_constraint_active"
                ),
            }
            for fact in blocked_by
        ]
        out.append(
            replace(
                row,
                executable=False,
                source_admitted=False,
                source_unavailability_reason=";".join(reasons),
                evidence=evidence,
            )
        )
    return out


def _constraint_blocks(
    opportunity: ActionOpportunity,
    fact: Mapping[str, Any],
) -> bool:
    if opportunity.action == "do_nothing":
        return False
    applies = _mapping(fact.get("applies_to"))
    levers = {
        str(value).upper()
        for value in applies.get("levers", [])
        if str(value)
    }
    if levers and opportunity.lever.upper() not in levers:
        return False
    owner_classes = {
        str(value).lower()
        for value in applies.get("owner_classes", [])
        if str(value)
    }
    if owner_classes and opportunity.owner_class.lower() not in owner_classes:
        return False
    opportunity_origin = str(
        _mapping(opportunity.evidence).get("origin") or ""
    ).lower()
    origins = {
        str(value).lower()
        for value in applies.get("origins", [])
        if str(value)
    }
    if origins and opportunity_origin not in origins:
        return False
    excluded_origins = {
        str(value).lower()
        for value in applies.get("excluded_origins", [])
        if str(value)
    }
    if excluded_origins and opportunity_origin in excluded_origins:
        return False
    allowed_behavior_ids = {
        str(value)
        for value in applies.get("allowed_behavior_ids", [])
        if str(value)
    }
    opportunity_behavior_id = str(
        _mapping(opportunity.evidence).get("behavior_id") or ""
    )
    if (
        allowed_behavior_ids
        and opportunity_behavior_id in allowed_behavior_ids
    ):
        return False
    allowed_bands = set(_bands(applies.get("allowed_bands")))
    if allowed_bands:
        target_bands = set(
            _bands(
                opportunity.params.get("ca_pool")
                or opportunity.params.get("bands")
                or (
                    [opportunity.params.get("pband")]
                    if _positive_int(opportunity.params.get("pband")) > 0
                    else []
                )
            )
        )
        if not target_bands:
            return False
        return not target_bands.issubset(allowed_bands)
    return True


def _opportunity(
    *,
    lever: LeverType,
    action: str,
    params: Mapping[str, Any],
    owner_class: str,
    source: str,
    reason: str,
    capabilities: Mapping[str, Any],
    disruption_cost: float,
    uncertainty: float,
    evidence: Mapping[str, Any],
    schedule: Mapping[str, Any] | None = None,
    source_admitted: bool = True,
    source_unavailability_reason: str = "",
) -> ActionOpportunity:
    descriptor = action_descriptor(lever)
    supported = (
        not descriptor.required_capability
        or _capability(capabilities, descriptor.required_capability)
    ) and _control_mode_supports_shape(
        lever,
        params,
        capabilities,
    )
    canonical_params = _jsonable_mapping(params)
    shape_verdict = validate_action_shape(lever, canonical_params)
    admitted = bool(supported and shape_verdict.legal)
    return ActionOpportunity(
        lever=lever.name,
        action=str(action),
        params=canonical_params,
        owner_class=owner_class if owner_class in _OWNER_CLASSES else "service",
        source=str(source),
        reason=str(reason),
        capability_key=str(descriptor.required_capability or ""),
        legal=admitted,
        executable=bool(
            admitted and source_admitted and descriptor.requires_modem_write
        ),
        disruption_cost=disruption_cost,
        uncertainty=uncertainty,
        safety_applicability=(
            "bouncer_required" if descriptor.requires_modem_write else "read_only_safety_review"
        ),
        capability_admitted=bool(supported),
        shape_valid=bool(shape_verdict.legal),
        shape_error=(
            ""
            if shape_verdict.legal
            else str(shape_verdict.code or shape_verdict.reason)
        ),
        source_admitted=bool(source_admitted),
        source_unavailability_reason=str(source_unavailability_reason or ""),
        evidence=evidence,
        schedule=schedule or {},
    )


def _capability(capabilities: Mapping[str, Any], key: str) -> bool:
    name = str(key)
    return bool(capabilities.get(name, capabilities.get(f"supports_{name}", False)))


def _radio_configuration_lever(
    capabilities: Mapping[str, Any],
) -> LeverType:
    if _capability(capabilities, "supports_pband") and str(
        capabilities.get("pband_control_mode") or "none"
    ) != "none":
        return LeverType.PBAND
    return LeverType.BAND_SELECTION


def _control_mode_supports_shape(
    lever: LeverType,
    params: Mapping[str, Any],
    capabilities: Mapping[str, Any],
) -> bool:
    if lever is LeverType.PBAND:
        if str(capabilities.get("pband_control_mode") or "none") == "none":
            return False
        pool = _bands(params.get("ca_pool"))
        if len(pool) > 1 and str(
            capabilities.get("ca_pool_control_mode") or "none"
        ) == "none":
            return False
    if (
        lever is LeverType.CELL_LOCK
        and params.get("unlock") is not True
        and str(capabilities.get("cell_lock_control_mode") or "none") == "none"
    ):
        return False
    return True


def _capability_action_coverage(
    capabilities: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lever in LeverType:
        descriptor = action_descriptor(lever)
        supported = (
            not descriptor.required_capability
            or _capability(capabilities, descriptor.required_capability)
        )
        mode_reason = ""
        if lever is LeverType.PBAND and str(
            capabilities.get("pband_control_mode") or "none"
        ) == "none":
            supported = False
            mode_reason = "pband_control_mode_none"
        elif lever is LeverType.CELL_LOCK and str(
            capabilities.get("cell_lock_control_mode") or "none"
        ) == "none":
            supported = False
            mode_reason = "cell_lock_control_mode_none"
        rows.append(
            {
                "lever": lever.name,
                "capability_key": str(descriptor.required_capability or ""),
                "supported": bool(supported),
                "reason": (
                    ""
                    if supported
                    else mode_reason
                    or (
                        "capability_not_admitted:"
                        + str(descriptor.required_capability or "unknown")
                    )
                ),
            }
        )
    return rows


def _bands(value: Any) -> tuple[int, ...]:
    if isinstance(value, str):
        value = value.replace("+", ",").split(",")
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted({_positive_int(item) for item in value if _positive_int(item) > 0}))


def _source_fact_scope_error(
    fact_set: Mapping[str, Any],
    *,
    accepted_scope: EvidenceScope | CurrentSessionActionScope,
) -> str:
    row = _mapping(fact_set)
    expected = {
        "identity_code": str(accepted_scope.identity_code or ""),
        "session_scope_key": str(accepted_scope.session_scope_key or ""),
        "source_epoch": str(accepted_scope.source_epoch or ""),
        "source_sequence": int(accepted_scope.source_sequence or 0),
    }
    actual = {
        "identity_code": str(row.get("identity_code") or ""),
        "session_scope_key": str(row.get("session_scope_key") or ""),
        "source_epoch": str(row.get("source_epoch") or ""),
        "source_sequence": int(row.get("source_sequence") or 0),
    }
    for key in (
        "identity_code",
        "session_scope_key",
        "source_epoch",
        "source_sequence",
    ):
        if actual[key] != expected[key]:
            return f"{key}_mismatch"
    return ""


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_sequence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _jsonable_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value or {})
    error = _json_contract_error(row)
    if error:
        return {
            "_quarantined_non_json_mapping": True,
            "reason": error,
            "original_keys": sorted(str(key) for key in row),
        }
    return json.loads(
        json.dumps(
            row,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    )


def _jsonable_mapping_sequence(
    value: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        normalized = {
            str(key): (
                _jsonable_mapping(raw_value)
                if isinstance(raw_value, Mapping)
                else raw_value
            )
            for key, raw_value in item.items()
        }
        rows.append(_jsonable_mapping(normalized))
    return sorted(rows, key=_content_digest)


def _freeze_json_mapping(value: Mapping[str, Any]) -> _FrozenDict:
    return _FrozenDict(
        {
            str(key): _freeze_json_value(item)
            for key, item in dict(value or {}).items()
        }
    )


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_json_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _json_contract_error(value: Any, *, path: str = "$") -> str:
    if value is None or isinstance(value, (str, bool, int)):
        return ""
    if isinstance(value, float):
        return "" if math.isfinite(value) else f"{path}:non_finite_float"
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                return f"{path}:non_string_key:{type(key).__name__}"
            error = _json_contract_error(item, path=f"{path}.{key}")
            if error:
                return error
        return ""
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            error = _json_contract_error(item, path=f"{path}[{index}]")
            if error:
                return error
        return ""
    return f"{path}:unsupported_type:{type(value).__name__}"


def _row_record_id(row: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in dict(row or {}).items()
        if key != "opportunity_record_id"
    }
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _content_digest(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(row or {}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _opportunity_shape_id(row: Mapping[str, Any]) -> str:
    content = {
        "schema": OPPORTUNITY_SCHEMA,
        "owner": OWNER,
        "lever": str(row.get("lever") or ""),
        "action": str(row.get("action") or ""),
        "params": _jsonable_mapping(_mapping(row.get("params"))),
        "owner_class": str(row.get("owner_class") or ""),
        "capability_key": str(row.get("capability_key") or ""),
    }
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _merge_opportunity_rows(
    existing: Mapping[str, Any], incoming: Mapping[str, Any]
) -> dict[str, Any]:
    left = dict(existing or {})
    right = dict(incoming or {})
    provenance: dict[str, dict[str, Any]] = {}
    for value in list(left.get("provenance") or []) + list(
        right.get("provenance") or []
    ):
        if not isinstance(value, Mapping):
            continue
        row = dict(value)
        provenance.setdefault(_row_record_id(row), row)
    provenance_rows = [provenance[key] for key in sorted(provenance)]
    packages = [
        dict(value.get("evidence") or {})
        for value in provenance_rows
        if isinstance(value.get("evidence"), Mapping)
    ]
    evidence_count = sum(max(0, _int(value.get("evidence_count"), 0)) for value in packages)
    confidences = [_bounded(value.get("confidence")) for value in packages]
    future_fits = [
        _bounded(value.get("future_context_fit"))
        for value in packages
        if value.get("future_context_fit") is not None
    ]
    merged_evidence: dict[str, Any] = {
        "packages": packages,
        "evidence_count": evidence_count,
        "confidence": max(confidences, default=0.0),
        "source_count": len(provenance),
    }
    if future_fits:
        merged_evidence["future_context_fit"] = sum(future_fits) / len(future_fits)
    expected_grades = [
        max(-3.0, min(3.0, _float(value.get("expected_referee_grade"), 0.0)))
        for value in packages
        if value.get("expected_referee_grade") is not None
    ]
    if expected_grades:
        merged_evidence["expected_referee_grade"] = sum(expected_grades) / len(
            expected_grades
        )
    source_admitted = bool(
        left.get("source_admitted", True) or right.get("source_admitted", True)
    )
    availability_reasons = {
        str(value)
        for value in list(left.get("availability_reasons") or [])
        + list(right.get("availability_reasons") or [])
        if str(value)
    }
    if source_admitted:
        availability_reasons.discard(
            str(left.get("source_unavailability_reason") or "")
        )
        availability_reasons.discard(
            str(right.get("source_unavailability_reason") or "")
        )
    merged = dict(left)
    merged.update(
        {
            "source": (
                str(left.get("source") or "")
                if len(provenance) == 1
                else "convergent_evidence"
            ),
            "reason": (
                str(left.get("reason") or "")
                if len(provenance) == 1
                else "multiple_sources_support_same_action_shape"
            ),
            "evidence": merged_evidence,
            "provenance": provenance_rows,
            "legal": bool(left.get("legal", False) and right.get("legal", False)),
            "executable": bool(
                left.get("executable", False) or right.get("executable", False)
            ),
            "source_admitted": source_admitted,
            "source_unavailability_reason": ""
            if source_admitted
            else str(
                left.get("source_unavailability_reason")
                or right.get("source_unavailability_reason")
                or "source_candidate_not_admitted"
            ),
            "disruption_cost": max(
                _bounded(left.get("disruption_cost")),
                _bounded(right.get("disruption_cost")),
            ),
            "uncertainty": max(
                _bounded(left.get("uncertainty")),
                _bounded(right.get("uncertainty")),
            ),
            "schedule": _earliest_schedule(
                _mapping(left.get("schedule")), _mapping(right.get("schedule"))
            ),
            "availability_reasons": sorted(availability_reasons),
        }
    )
    merged["opportunity_record_id"] = _row_record_id(merged)
    return merged


def _earliest_schedule(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> dict[str, Any]:
    candidates = [dict(value) for value in (left, right) if value]
    if not candidates:
        return {}
    return min(
        candidates,
        key=lambda row: (
            _float(row.get("due_at"), float("inf")),
            _row_record_id(row),
        ),
    )


def _bounded(value: Any) -> float:
    return max(0.0, min(1.0, _float(value, 0.0)))


def _positive_int(value: Any) -> int:
    parsed = _int(value, 0)
    return parsed if parsed > 0 else 0


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


__all__ = [
    "ActionOpportunity",
    "ActionOpportunitySourceSnapshot",
    "OPPORTUNITY_SCHEMA",
    "OWNER",
    "SCHEMA",
    "SOURCE_SNAPSHOT_SCHEMA",
    "action_opportunity_set_error",
    "action_opportunity_source_snapshot_error",
    "build_action_opportunity_set",
]
