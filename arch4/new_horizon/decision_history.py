"""Own the canonical New Horizon decision-lifecycle history snapshot."""

from __future__ import annotations
from arch4.core_contracts.intelligence_proposals import valid_intelligence_grade

from dataclasses import dataclass, field, replace
import math
from threading import RLock
from typing import Any, Iterable, Mapping

from arch4.core_contracts.intelligence_proposals import GRADE_DIMENSIONS
from arch4.new_horizon.contracts import (
    DECISION_LEDGER_KIND,
    DECISION_LEDGER_SCHEMA,
    DECISION_TERMINAL_KIND,
    DECISION_TERMINAL_SCHEMA,
    HOLD_ATTRIBUTION_KIND,
    HOLD_ATTRIBUTION_SCHEMA,
    PRIOR_DECISION_LEDGER_CONTRACTS,
    PROPOSAL_RESULT_EVIDENCE_SCHEMA,
    PROPOSAL_RESULT_HISTORY_KIND,
)
from arch4.new_horizon.history import AdvisoryHistory, evidence_time_equivalent
from arch4.new_horizon.hourly_aq import nr_evidence_condition


_CONTRACTS = {
    DECISION_LEDGER_KIND: DECISION_LEDGER_SCHEMA,
    **dict(PRIOR_DECISION_LEDGER_CONTRACTS),
    DECISION_TERMINAL_KIND: DECISION_TERMINAL_SCHEMA,
    PROPOSAL_RESULT_HISTORY_KIND: PROPOSAL_RESULT_EVIDENCE_SCHEMA,
    HOLD_ATTRIBUTION_KIND: HOLD_ATTRIBUTION_SCHEMA,
}
DECISION_HISTORY_KINDS = tuple(_CONTRACTS)


def forecast_result_attribution(
    forecast: object, result_identity: object,
) -> dict[str, Any]:
    """Join a frozen cell prediction to the physical result, never just its band.

    The command origin is unchanged. An unmatched cell still supplies measured
    AQ; it cannot assess a prediction made for a different serving cell.
    """
    unavailable = {"eligible": False, "reason": "cell_prediction_unavailable",
                   "expected_aq": None, "result_scope_key": ""}
    if (not isinstance(forecast, Mapping) or forecast.get("status") != "predicted"
            or forecast.get("time_basis") == "legacy_six_hour"
            or not isinstance(result_identity, Mapping)):
        return unavailable
    contexts = forecast.get("result_contexts")
    if not isinstance(contexts, (list, tuple)):
        return unavailable
    fields = ("network_type", "pband", "cell_id", "pci", "earfcn",
              "nr_band", "nr_cell_id", "nr_pci", "nr_arfcn")
    matches = []
    different = False
    for context in contexts:
        if not isinstance(context, Mapping):
            raise ValueError("new_horizon_forecast_result_context_invalid")
        evidence = context.get("hourly_evidence")
        if not isinstance(evidence, (list, tuple)) or not evidence:
            continue
        identities = [item.get("identity") for item in evidence
                      if isinstance(item, Mapping)]
        if len(identities) != len(evidence) or any(
                not isinstance(item, Mapping) for item in identities):
            raise ValueError("new_horizon_forecast_cell_evidence_invalid")
        expected = identities[0]
        if nr_evidence_condition(expected) != nr_evidence_condition(result_identity):
            different = True
            continue
        physical = tuple(expected.get(key) for key in fields)
        if any(tuple(item.get(key) for key in fields) != physical for item in identities):
            raise ValueError("new_horizon_forecast_mixed_physical_context")
        cell_key = "nr_cell_id" if expected.get("network_type") == "NR SA (5G)" else "cell_id"
        band_key = "nr_band" if cell_key == "nr_cell_id" else "pband"
        if (type(expected.get(cell_key)) is not int or expected[cell_key] <= 0
                or type(expected.get(band_key)) is not int
                or not expected.get("network_type")):
            continue
        different |= any(value is not None and result_identity.get(key) is not None
                         and result_identity[key] != value
                         for key, value in zip(fields, physical, strict=True))
        if any(value is not None and result_identity.get(key) != value
               for key, value in zip(fields, physical, strict=True)):
            continue
        grade = context.get("expected_aq")
        if type(grade) not in {int, float} or not math.isfinite(grade) or not valid_intelligence_grade(grade,dimension="service_outcome"):
            continue
        matches.append({"eligible": True, "reason": "forecast_cell_reached",
                        "expected_aq": float(grade),
                        "result_scope_key": str(context.get("result_scope_key") or "")})
    if len(matches) > 1:
        raise ValueError("new_horizon_forecast_duplicate_physical_context")
    if matches:
        return matches[0]
    return {**unavailable, "reason": "different_serving_cell"} if different else unavailable


def validated_prediction(value: object) -> dict[str, Any]:
    """Validate saved prediction semantics at the lifecycle boundary."""

    if not isinstance(value, Mapping) or set(value) != {"basis", "components"}:
        raise ValueError("new_horizon_decision_prediction_invalid")
    components = value["components"]
    if (
        value["basis"] != "prediction"
        or not isinstance(components, list)
        or len(components) != len(GRADE_DIMENSIONS)
        or tuple(
            item.get("dimension") for item in components if isinstance(item, Mapping)
        )
        != GRADE_DIMENSIONS
    ):
        raise ValueError("new_horizon_decision_prediction_invalid")
    for item in components:
        if not isinstance(item, Mapping) or set(item) != {
            "dimension",
            "grade",
            "confidence",
            "reason",
        }:
            raise ValueError("new_horizon_decision_prediction_invalid")
        grade, confidence = item["grade"], item["confidence"]
        if (
            grade is not None
            and (
                type(grade) not in {int, float}
                or not math.isfinite(float(grade))
                or not valid_intelligence_grade(grade,dimension=item["dimension"])
            )
            or type(confidence) not in {int, float}
            or not math.isfinite(float(confidence))
            or not 0.0 <= confidence <= 1.0
            or type(item["reason"]) is not str
        ):
            raise ValueError("new_horizon_decision_prediction_invalid")
    return {"basis": "prediction", "components": [dict(item) for item in components]}


def validated_contribution(value: object) -> dict[str, Any]:
    """Own the adviser contribution contract for history and feedback."""

    required = {
        "adviser",
        "dimension",
        "stance",
        "used",
        "predicted_grade",
        "confidence",
        "evidence_refs",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("new_horizon_adviser_contribution_invalid")
    adviser, dimension, stance = value["adviser"], value["dimension"], value["stance"]
    used, grade = value["used"], value["predicted_grade"]
    confidence, refs = value["confidence"], value["evidence_refs"]
    if (
        type(adviser) is not str
        or not adviser
        or adviser != adviser.strip()
        or dimension not in GRADE_DIMENSIONS
        or type(stance) is not str
        or stance not in {"support", "oppose", "abstain"}
        or type(used) is not bool
        or type(grade) not in {int, float}
        or not math.isfinite(float(grade))
        or not valid_intelligence_grade(grade,dimension=dimension)
        or type(confidence) not in {int, float}
        or not math.isfinite(float(confidence))
        or not 0.0 <= confidence <= 1.0
        or not isinstance(refs, list)
        or any(type(item) is not str or not item for item in refs)
        or len(set(refs)) != len(refs)
        or used
        and not refs
    ):
        raise ValueError("new_horizon_adviser_contribution_invalid")
    expected_stance = "support" if grade > 0 else "oppose" if grade < 0 else "abstain"
    if stance != expected_stance:
        raise ValueError("new_horizon_adviser_contribution_invalid")
    return {
        "adviser": adviser,
        "dimension": dimension,
        "stance": stance,
        "used": used,
        "predicted_grade": float(grade),
        "confidence": float(confidence),
        "evidence_refs": list(refs),
    }


_LEGACY_TRANSITION_REPLAY = "complete_legacy_transition_vector_to_v17"
_LEGACY_DEADLINE_REASON = "decision_lifecycle_deadline_expired"


def legacy_deadline_terminal_is_superseded(
    *,
    terminal_record: Mapping[str, Any],
    proof_payload: Mapping[str, Any],
) -> bool:
    """Resolve one proven old timeout without weakening terminal ownership."""

    if not isinstance(terminal_record, Mapping) or not isinstance(
        proof_payload, Mapping
    ):
        return False
    terminal = terminal_record.get("payload")
    terminal_at = terminal_record.get("observed_at")
    if (
        not isinstance(terminal, Mapping)
        or terminal.get("schema") != DECISION_TERMINAL_SCHEMA
        or terminal.get("reason") != _LEGACY_DEADLINE_REASON
        or isinstance(terminal_at, bool)
        or type(terminal_at) not in {int, float}
        or not math.isfinite(float(terminal_at))
    ):
        return False
    proposal_id = str(terminal.get("proposal_id") or "")
    closed_at = proof_payload.get("closed_at")
    provenance = proof_payload.get("replay_provenance")
    return bool(
        proposal_id
        and proof_payload.get("decision_id") == "core-command:" + proposal_id
        and proof_payload.get("dimension") == "service_outcome"
        and proof_payload.get("assessment_mode") == "transition"
        and proof_payload.get("status") == "graded"
        and proof_payload.get("final_assessment") is True
        and type(closed_at) in {int, float}
        and not isinstance(closed_at, bool)
        and math.isfinite(float(closed_at))
        and float(closed_at) <= float(terminal_at)
        and isinstance(provenance, Mapping)
        and provenance.get("vector_migration") == _LEGACY_TRANSITION_REPLAY
        and type(provenance.get("source_schema")) is str
        and bool(provenance.get("source_schema"))
        and type(provenance.get("source_assessment_id")) is str
        and bool(provenance.get("source_assessment_id"))
        and provenance.get("formula_schema") == proof_payload.get("schema")
    )


@dataclass(frozen=True, slots=True)
class DecisionLifecycleHistorySnapshot:
    """One validated decision-lifecycle view shared by every consumer."""

    continuity_key: str
    current_decisions: tuple[dict[str, Any], ...]
    prior_decisions: tuple[dict[str, Any], ...]
    proposal_results: tuple[dict[str, Any], ...]
    terminals: tuple[dict[str, Any], ...]
    holds: tuple[dict[str, Any], ...]
    rejected: tuple[dict[str, str], ...]
    unjoined: tuple[dict[str, Any], ...] = ()
    oldest_observed_at: float | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "oldest_observed_at",
            min((row["observed_at"] for row in self.retained_records), default=None),
        )

    @property
    def retained_records(self) -> tuple[dict[str, Any], ...]:
        """Keep unmatched facts for diagnostics and a later explicit join."""

        return (*self.records, *self.unjoined)

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            sorted(
                (
                    *self.current_decisions,
                    *self.prior_decisions,
                    *self.proposal_results,
                    *self.terminals,
                    *self.holds,
                ),
                key=lambda row: (float(row["observed_at"]), str(row["event_key"])),
            )
        )

    @property
    def attributable_decisions(self) -> tuple[dict[str, Any], ...]:
        """Return all owner-validated decision generations usable for attribution."""

        # Older generations come first so the current contract wins if an
        # explicit migration retained the same proposal identifier.
        return (*self.prior_decisions, *self.current_decisions)

    def records_for(self, kind: str) -> tuple[dict[str, Any], ...]:
        if kind == DECISION_LEDGER_KIND:
            return self.current_decisions
        if kind in dict(PRIOR_DECISION_LEDGER_CONTRACTS):
            return tuple(row for row in self.prior_decisions if row["kind"] == kind)
        if kind == PROPOSAL_RESULT_HISTORY_KIND:
            return self.proposal_results
        if kind == DECISION_TERMINAL_KIND:
            return self.terminals
        if kind == HOLD_ATTRIBUTION_KIND:
            return self.holds
        raise ValueError("new_horizon_decision_history_kind_invalid")

    def evidence_time_equivalent(self, previous_at: float, now: float) -> bool:
        """Expose time eligibility through this canonical decision snapshot."""
        return evidence_time_equivalent(previous_at, now, (
            row["observed_at"] for group in (self.current_decisions, self.prior_decisions,
                self.proposal_results, self.terminals, self.holds) for row in group
        ))

    def event(self, event_key: str) -> dict[str, Any] | None:
        return next(
            (row for row in self.records if row["event_key"] == event_key),
            None,
        )


class DecisionLifecycleHistoryOwner:
    """Read and validate decision lifecycle evidence once per continuity scope."""

    def __init__(self, history: AdvisoryHistory) -> None:
        if not isinstance(history, AdvisoryHistory):
            raise TypeError("new_horizon_decision_history_owner_store_required")
        self._history = history
        self._snapshot: DecisionLifecycleHistorySnapshot | None = None
        self._lock = RLock()
        self._retention_now: float | None = None

    def _cutoff(self, now: float | None) -> float | None:
        if now is not None:
            if type(now) not in {int, float} or not math.isfinite(now) or now < 0:
                raise ValueError("new_horizon_decision_history_retention_time_invalid")
            self._retention_now = max(float(now), self._retention_now or 0.0)
        return (
            self._retention_now - self._history.retention_seconds
            if self._retention_now is not None
            else None
        )

    def _retained_snapshot(
        self, snapshot: DecisionLifecycleHistorySnapshot, *, now: float | None
    ) -> DecisionLifecycleHistorySnapshot:
        cutoff = self._cutoff(now)
        if (
            cutoff is None
            or snapshot.oldest_observed_at is None
            or snapshot.oldest_observed_at >= cutoff
        ):
            return snapshot
        records = snapshot.retained_records
        retained = tuple(row for row in records if row["observed_at"] >= cutoff)
        if len(retained) == len(records):
            return snapshot
        return replace(
            self.snapshot_from_records(snapshot.continuity_key, retained),
            rejected=snapshot.rejected,
        )

    @property
    def history(self) -> AdvisoryHistory:
        return self._history

    @staticmethod
    def _validated_record(
        continuity_key: str,
        raw: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise TypeError("decision_history_record_mapping_required")
        event_key = raw.get("event_key")
        scope_key = raw.get("scope_key")
        kind = raw.get("kind")
        observed_at = raw.get("observed_at")
        payload = raw.get("payload")
        if type(event_key) is not str or not event_key:
            raise ValueError("decision_history_event_key_required")
        if scope_key != continuity_key:
            raise ValueError("decision_history_scope_mismatch")
        if type(kind) is not str or kind not in _CONTRACTS:
            raise ValueError("decision_history_kind_invalid")
        if (
            isinstance(observed_at, bool)
            or type(observed_at) not in {int, float}
            or not math.isfinite(float(observed_at))
            or float(observed_at) < 0.0
        ):
            raise ValueError("decision_history_observed_at_invalid")
        if not isinstance(payload, Mapping):
            raise ValueError("decision_history_payload_required")
        copied_payload = dict(payload)
        if copied_payload.get("schema") != _CONTRACTS[kind]:
            raise ValueError("decision_history_schema_invalid")

        if kind in {DECISION_LEDGER_KIND, *dict(PRIOR_DECISION_LEDGER_CONTRACTS)}:
            if (
                type(copied_payload.get("proposal_id")) is not str
                or not copied_payload["proposal_id"]
                or not isinstance(copied_payload.get("forecast"), Mapping)
            ):
                raise ValueError("decision_history_decision_identity_invalid")
            if kind == DECISION_LEDGER_KIND and (
                type(copied_payload.get("source_signature")) is not str
                or not copied_payload["source_signature"]
            ):
                raise ValueError("decision_history_decision_source_required")
            if kind == DECISION_LEDGER_KIND:
                copied_payload["predicted_grades"] = validated_prediction(
                    copied_payload.get("predicted_grades")
                )
                contributions = copied_payload.get("contributions", ())
                if not isinstance(contributions, (list, tuple)):
                    raise ValueError("new_horizon_adviser_contribution_invalid")
                if "contributions" in copied_payload:
                    copied_payload["contributions"] = [
                        validated_contribution(item) for item in contributions
                    ]
            # Both generation adapters expose this count as a number. Do not
            # let malformed historical values crash a downstream projection.
            count = copied_payload["forecast"].get("evidence_assessments", 0)
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError("decision_history_forecast_count_invalid")
        elif kind == PROPOSAL_RESULT_HISTORY_KIND:
            if (
                type(copied_payload.get("proposal_id")) is not str
                or not copied_payload["proposal_id"]
                or type(copied_payload.get("submitted")) is not bool
                or type(copied_payload.get("reason")) is not str
                or not copied_payload["reason"]
            ):
                raise ValueError("decision_history_proposal_result_invalid")
        elif kind == DECISION_TERMINAL_KIND:
            if (
                type(copied_payload.get("proposal_id")) is not str
                or not copied_payload["proposal_id"]
                or type(copied_payload.get("reason")) is not str
                or not copied_payload["reason"]
            ):
                raise ValueError("decision_history_terminal_invalid")
        elif kind == HOLD_ATTRIBUTION_KIND:
            if (
                type(copied_payload.get("subject_key")) is not str
                or not copied_payload["subject_key"]
            ):
                raise ValueError("decision_history_hold_identity_invalid")
            decided_at = copied_payload.get("decided_at")
            if (
                type(decided_at) not in {int, float}
                or not math.isfinite(decided_at)
                or decided_at < 0
            ):
                raise ValueError("decision_history_hold_time_invalid")

        return {
            "event_key": event_key,
            "scope_key": continuity_key,
            "kind": kind,
            "observed_at": float(observed_at),
            "payload": copied_payload,
        }

    @classmethod
    def snapshot_from_records(
        cls,
        continuity_key: str,
        records: Iterable[Mapping[str, Any]],
    ) -> DecisionLifecycleHistorySnapshot:
        """Build the canonical snapshot at an explicit storage boundary."""

        if type(continuity_key) is not str or not continuity_key:
            raise ValueError("new_horizon_decision_history_owner_scope_required")
        by_kind: dict[str, list[dict[str, Any]]] = {kind: [] for kind in _CONTRACTS}
        rejected: list[dict[str, str]] = []
        for raw in records:
            try:
                row = cls._validated_record(continuity_key, raw)
            except (TypeError, ValueError) as exc:
                rejected.append(
                    {
                        "event_key": str(raw.get("event_key") or "")
                        if isinstance(raw, Mapping)
                        else "",
                        "reason": str(exc),
                    }
                )
                continue
            by_kind[row["kind"]].append(row)
        for kind, rows in by_kind.items():
            rows[:] = {row["event_key"]: row for row in rows}.values()
            rows.sort(key=lambda row: (row["observed_at"], row["event_key"]))
        prior_kinds = dict(PRIOR_DECISION_LEDGER_CONTRACTS)
        parents = {
            row["payload"]["proposal_id"]
            for kind in (DECISION_LEDGER_KIND, *prior_kinds)
            for row in by_kind[kind]
        }
        unjoined: list[dict[str, Any]] = []
        for kind in (DECISION_TERMINAL_KIND, PROPOSAL_RESULT_HISTORY_KIND):
            unjoined.extend(
                row
                for row in by_kind[kind]
                if row["payload"]["proposal_id"] not in parents
            )
            by_kind[kind] = [
                row for row in by_kind[kind] if row["payload"]["proposal_id"] in parents
            ]
        return DecisionLifecycleHistorySnapshot(
            continuity_key=continuity_key,
            current_decisions=tuple(by_kind[DECISION_LEDGER_KIND]),
            prior_decisions=tuple(row for kind in prior_kinds for row in by_kind[kind]),
            proposal_results=tuple(by_kind[PROPOSAL_RESULT_HISTORY_KIND]),
            terminals=tuple(by_kind[DECISION_TERMINAL_KIND]),
            holds=tuple(by_kind[HOLD_ATTRIBUTION_KIND]),
            rejected=tuple(rejected),
            unjoined=tuple(
                sorted(unjoined, key=lambda row: (row["observed_at"], row["event_key"]))
            ),
        )

    def refresh(
        self, continuity_key: str, *, now: float | None = None
    ) -> DecisionLifecycleHistorySnapshot:
        if type(continuity_key) is not str or not continuity_key:
            raise ValueError("new_horizon_decision_history_owner_scope_required")
        cutoff = self._cutoff(now)
        by_kind = self._history.query_scoped_kinds(
            scope_key=continuity_key,
            kinds=tuple(_CONTRACTS),
            limit_per_kind=None,
            since=cutoff,
        )
        records = [row for kind in _CONTRACTS for row in by_kind[kind]]
        snapshot = self.snapshot_from_records(continuity_key, records)
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def current(
        self, continuity_key: str, *, now: float | None = None
    ) -> DecisionLifecycleHistorySnapshot:
        with self._lock:
            snapshot = self._snapshot
            if snapshot is None or snapshot.continuity_key != continuity_key:
                raise RuntimeError("new_horizon_decision_history_snapshot_not_bound")
            self._snapshot = self._retained_snapshot(snapshot, now=now)
            return self._snapshot

    def admit_live(
        self,
        *,
        event_key: str,
        continuity_key: str,
        now: float | None = None,
    ) -> DecisionLifecycleHistorySnapshot:
        """Admit one just-written event through the same canonical validator."""

        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            raise RuntimeError("new_horizon_decision_history_snapshot_not_bound")
        if snapshot.continuity_key != continuity_key:
            raise RuntimeError("new_horizon_decision_history_live_scope_mismatch")
        record = self._history.load_evidence(event_key=event_key)
        if record is None:
            raise RuntimeError("new_horizon_decision_history_live_evidence_missing")
        row = self._validated_record(continuity_key, record)
        snapshot = self._retained_snapshot(
            snapshot, now=max(row["observed_at"], now or 0.0)
        )
        retained = {item["event_key"]: item for item in snapshot.retained_records}
        retained[row["event_key"]] = row
        cutoff = self._cutoff(now)
        retained = {
            key: item
            for key, item in retained.items()
            if cutoff is None or item["observed_at"] >= cutoff
        }
        updated = self.snapshot_from_records(continuity_key, retained.values())
        if snapshot.rejected:
            updated = replace(updated, rejected=snapshot.rejected)
        with self._lock:
            self._snapshot = updated
        return updated

    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self._snapshot
        return {
            "owner": "54guc.new_horizon.decision_lifecycle_history",
            "bound": snapshot is not None,
            "continuity_key": snapshot.continuity_key if snapshot else "",
            "record_count": len(snapshot.records) if snapshot else 0,
            "current_decision_count": len(snapshot.current_decisions)
            if snapshot
            else 0,
            "prior_decision_count": len(snapshot.prior_decisions) if snapshot else 0,
            "proposal_result_count": len(snapshot.proposal_results) if snapshot else 0,
            "terminal_count": len(snapshot.terminals) if snapshot else 0,
            "hold_count": len(snapshot.holds) if snapshot else 0,
            "rejected_count": len(snapshot.rejected) if snapshot else 0,
            "recent_rejections": list(snapshot.rejected[-8:]) if snapshot else [],
            "unjoined_count": len(snapshot.unjoined) if snapshot else 0,
            "recent_unjoined": [
                {
                    "event_key": row["event_key"],
                    "proposal_id": row["payload"]["proposal_id"],
                    "reason": "decision_history_retained_parent_unavailable",
                }
                for row in snapshot.unjoined[-8:]
            ]
            if snapshot
            else [],
        }


__all__ = (
    "DecisionLifecycleHistoryOwner",
    "DecisionLifecycleHistorySnapshot",
    "DECISION_HISTORY_KINDS",
    "validated_prediction",
    "validated_contribution",
    "forecast_result_attribution",
    "legacy_deadline_terminal_is_superseded",
)
