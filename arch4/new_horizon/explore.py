"""Run one bounded radio exploration session authorized by the user."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from itertools import combinations
from time import time
from typing import Any, Mapping

from arch4.core_contracts.command_targets import (
    CorePBandTarget,
    core_command_target_payload,
    core_lte_pband_signature,
)
from arch4.core_contracts.intelligence_proposals import ActionTargetEvidence
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.new_horizon.contracts import AdvisoryOpportunity, IntelligenceContext
from arch4.new_horizon.history import AdvisoryHistory
from arch4.new_horizon.performance import record_performance_evidence
from arch4.new_horizon.proof import context_continuity_key, context_scope_key


EXPLORE_BANDS = (3, 1, 20)
EXPLORE_OBSERVATION_WINDOWS_S = (60.0, 360.0, 600.0)


def _current_fact(context: IntelligenceContext, path: str) -> Any | None:
    fact = context.fact(path)
    return (
        fact.value.get("value")
        if fact.disposition == "available"
        else None
    )


def _fresh_fact(context: IntelligenceContext, path: str) -> Any | None:
    fact = context.fact(path)
    return (
        fact.value.get("value")
        if fact.disposition == "available" and fact.retention is None
        else None
    )


def _surface(context: IntelligenceContext, name: str) -> Mapping[str, Any]:
    surface = context.surface(name)
    if not surface.valid or surface.stale:
        return {}
    return surface.value.to_mapping()


def _bands(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    parsed = tuple(sorted({item for item in value if type(item) is int and item > 0}))
    return parsed if len(parsed) == len(set(value)) else ()


def _target(pband: int, pool: tuple[int, ...]) -> ActionTargetEvidence:
    target = CorePBandTarget(pband=pband, ca_pool=pool)
    return ActionTargetEvidence(
        action_family="lte_radio_selection",
        command_kind=target.command_kind,
        target_signature=core_lte_pband_signature(pband, pool),
        target=CoreFrozenPayload.from_mapping(core_command_target_payload(target)),
    )


def _target_value(target: ActionTargetEvidence) -> Mapping[str, Any]:
    value = target.target.get("value")
    if not isinstance(value, Mapping):
        raise ValueError("explore_target_value_missing")
    return value


@dataclass(frozen=True, slots=True)
class ExploreProbeRequest:
    request_id: str
    session_id: str
    scope_key: str
    target_signature: str
    ordinal: int
    requested_at: float


class ExploreCoordinator:
    """Run one finite Explore plan without writing the modem or choosing policy."""

    def __init__(self, history: AdvisoryHistory) -> None:
        if not isinstance(history, AdvisoryHistory):
            raise TypeError("explore_history_owner_required")
        self._history = history
        self._session_id = ""
        self._phase = "idle"
        self._reason = "not_requested"
        self._identity_code = ""
        self._network_type = ""
        self._targets: tuple[ActionTargetEvidence, ...] = ()
        self._target_index = 0
        self._current: ActionTargetEvidence | None = None
        self._current_started_at = 0.0
        self._proposal_id = ""
        self._skipped_bands: set[int] = set()
        self._skipped_targets: set[str] = set()
        self._completed: set[str] = set()
        self._probe_results: list[dict[str, Any]] = []
        self._probe_inflight: ExploreProbeRequest | None = None
        self._probe_scope_changed = False
        self._consecutive_probe_failures = 0
        self._consecutive_incomplete = 0
        self._last_band = 0
        self._unsolicited_transitions: deque[tuple[float, int]] = deque()
        self._reversals: dict[tuple[int, int], int] = {}
        self._stop_requested = False
        self._outcome_receipts: OrderedDict[str, tuple[str, str, float]] = OrderedDict()

    def _record(self, kind: str, context: IntelligenceContext, payload: Mapping[str, Any]) -> None:
        stamp = float(context.accepted_at or time())
        identity = "|".join(
            (
                self._session_id or "none",
                kind,
                str(context.matrix_identity.matrix_sequence),
                str(len(self._completed)),
                str(self._target_index),
            )
        )
        self._history.record(
            event_key="explore:" + identity,
            scope_key=context_scope_key(context),
            kind="explore_" + kind,
            observed_at=stamp,
            payload={
                "schema": "NewHorizonExploreEvidence.v1",
                "session_id": self._session_id,
                "phase": self._phase,
                "reason": self._reason,
                **dict(payload),
            },
        )

    @staticmethod
    def _plan(context: IntelligenceContext) -> tuple[ActionTargetEvidence, ...]:
        control = context.product_control.to_mapping()
        provider = context.foundation_band_policy.get("policy")
        policy_bands = _bands(provider.get("bands")) if isinstance(provider, Mapping) else ()
        session_bands = _bands(
            _current_fact(context, "capability.session_lte_bands")
        )
        legal = tuple(
            band
            for band in EXPLORE_BANDS
            if band in policy_bands and band in session_bands
        )
        if len(legal) != len(EXPLORE_BANDS):
            return ()
        values: list[ActionTargetEvidence] = []
        for pband in legal:
            others = tuple(band for band in legal if band != pband)
            for count in range(1, len(others) + 1):
                for selected in combinations(others, count):
                    values.append(_target(pband, tuple(sorted((pband, *selected)))))
        if len(values) != 9 or not control.get("explore_session_id"):
            raise RuntimeError("explore_plan_denominator_invalid")
        return tuple(values)

    def _start(self, context: IntelligenceContext) -> None:
        self._outcome_receipts.clear()
        control = context.product_control.to_mapping()
        self._session_id = str(control["explore_session_id"])
        self._identity_code = context.identity_code
        self._network_type = str(
            _current_fact(context, "radio.network_type") or ""
        )
        self._targets = self._plan(context)
        self._target_index = 0
        self._current = None
        self._current_started_at = 0.0
        self._proposal_id = ""
        self._skipped_bands.clear()
        self._skipped_targets.clear()
        self._completed.clear()
        self._probe_results.clear()
        self._probe_inflight = None
        self._consecutive_probe_failures = 0
        self._consecutive_incomplete = 0
        self._last_band = int(
            _current_fact(context, "radio.serving_band") or 0
        )
        self._unsolicited_transitions.clear()
        self._reversals.clear()
        self._stop_requested = False
        if not self._targets:
            self._stop("required_bands_or_capability_unavailable")
        else:
            self._phase = "ready"
            self._reason = "user_authorized"
        self._record("started", context, {"target_count": len(self._targets)})

    def _stop(self, reason: str) -> None:
        self._phase = "stopped"
        self._reason = str(reason)
        self._stop_requested = True
        self._probe_inflight = None

    def _reset(self, reason: str) -> None:
        self._outcome_receipts.clear()
        self._session_id = ""
        self._phase = "idle"
        self._reason = reason
        self._targets = ()
        self._current = None
        self._current_started_at = 0.0
        self._proposal_id = ""
        self._probe_results.clear()
        self._probe_inflight = None
        self._stop_requested = False

    def _advance(self, context: IntelligenceContext) -> None:
        self._current = None
        self._current_started_at = 0.0
        self._proposal_id = ""
        self._probe_results.clear()
        self._probe_inflight = None
        while self._target_index < len(self._targets):
            candidate = self._targets[self._target_index]
            self._target_index += 1
            target_value = _target_value(candidate)
            pband = int(target_value["pband"])
            if (
                pband in self._skipped_bands
                or candidate.target_signature in self._skipped_targets
                or candidate.target_signature in self._completed
            ):
                continue
            self._current = candidate
            current_pband = _current_fact(context, "radio.serving_band")
            current_pool = _bands(
                _current_fact(context, "radio.configured_band_pool")
            )
            if (
                current_pband == target_value["pband"]
                and current_pool == _bands(target_value["ca_pool"])
            ):
                self._begin_observation(context, source="already_active")
            else:
                self._phase = "ready"
                self._reason = "awaiting_brain_selection"
            return
        self._phase = "complete"
        self._reason = "all_targets_completed_or_skipped"
        self._stop_requested = True
        self._record(
            "completed",
            context,
            {
                "completed_targets": len(self._completed),
                "skipped_bands": sorted(self._skipped_bands),
            },
        )

    def _begin_observation(self, context: IntelligenceContext, *, source: str) -> None:
        self._phase = "observing"
        self._reason = source
        self._current_started_at = context.accepted_at
        self._probe_results.clear()
        self._probe_inflight = None
        self._consecutive_probe_failures = 0
        self._consecutive_incomplete = 0
        if self._current is not None:
            self._last_band = int(_target_value(self._current)["pband"])
        self._record(
            "target_started",
            context,
            {
                "target_signature": self._current.target_signature if self._current else "",
                "source": source,
            },
        )

    def synchronize(self, context: IntelligenceContext) -> AdvisoryOpportunity | None:
        control = context.product_control.to_mapping()
        requested = control.get("explore_requested") is True
        session_id = str(control.get("explore_session_id") or "")
        if not requested:
            if self._session_id:
                if self._phase != "complete":
                    self._record(
                        "stopped",
                        context,
                        {"reason": "user_or_pim_stopped"},
                    )
            self._reset("not_requested")
            return None
        if session_id != self._session_id:
            self._start(context)
        if self._phase in {"stopped", "complete"}:
            return None
        if context.identity_code != self._identity_code:
            self._stop("device_identity_changed")
            return None
        network_type = str(
            _current_fact(context, "radio.network_type") or ""
        )
        if network_type and self._network_type and network_type != self._network_type:
            self._stop("radio_access_type_changed")
            return None
        if self._current is None:
            self._advance(context)
        if self._current is None or self._phase in {"stopped", "complete"}:
            return None
        if self._phase == "observing":
            if (self._probe_inflight is not None
                    and (not context.complete
                         or self._probe_inflight.scope_key != context_scope_key(context))):
                # Keep the worker reserved until drained, even if the cell returns.
                self._probe_scope_changed = True
            current_band = int(
                _current_fact(context, "radio.serving_band") or 0
            )
            target_band = int(_target_value(self._current)["pband"])
            if current_band and self._last_band and current_band != self._last_band:
                now = context.accepted_at
                self._unsolicited_transitions.append((now, current_band))
                while self._unsolicited_transitions and now - self._unsolicited_transitions[0][0] > 120.0:
                    self._unsolicited_transitions.popleft()
                pair = tuple(sorted((self._last_band, current_band)))
                history = [band for _, band in self._unsolicited_transitions]
                if len(history) >= 3 and history[-3] == history[-1] != history[-2]:
                    self._reversals[pair] = self._reversals.get(pair, 0) + 1
                if self._reversals.get(pair, 0) >= 2 or len(self._unsolicited_transitions) >= 3:
                    self._stop("repeated_unsolicited_band_flipping")
                    return None
            self._last_band = current_band or self._last_band
            if current_band and current_band != target_band:
                self._skipped_bands.add(target_band)
                self._record(
                    "band_reverted",
                    context,
                    {"target_band": target_band, "observed_band": current_band},
                )
                self._advance(context)
                return self.opportunity()
            current_pool = _bands(
                _current_fact(context, "radio.configured_band_pool")
            )
            target_pool = _bands(_target_value(self._current)["ca_pool"])
            if current_pool and current_pool != target_pool:
                self._skipped_targets.add(self._current.target_signature)
                self._record(
                    "pool_reverted",
                    context,
                    {
                        "target_signature": self._current.target_signature,
                        "observed_pool": list(current_pool),
                    },
                )
                self._advance(context)
                return self.opportunity()
            self._consecutive_incomplete = (
                0 if context.complete else self._consecutive_incomplete + 1
            )
            if self._consecutive_incomplete >= 3:
                self._stop("three_consecutive_incomplete_contexts")
                return None
            if (
                context.complete
                and context.accepted_at >= self._current_started_at + EXPLORE_OBSERVATION_WINDOWS_S[-1]
                and len(self._probe_results) == len(EXPLORE_OBSERVATION_WINDOWS_S)
            ):
                if any(row["scope_key"] != context_scope_key(context) for row in self._probe_results):
                    self._skipped_targets.add(self._current.target_signature)
                    self._record("target_scope_discarded", context, {
                        "target_signature": self._current.target_signature,
                        "reason": "probe_scopes_do_not_match_current_context",
                    })
                    self._advance(context)
                    return self.opportunity()
                self._completed.add(self._current.target_signature)
                self._record(
                    "target_completed",
                    context,
                    {
                        "target_signature": self._current.target_signature,
                        "probe_count": len(self._probe_results),
                        "best_dl_mbps": max(
                            float(row["dl_mbps"]) for row in self._probe_results
                        ),
                    },
                )
                best = max(
                    self._probe_results,
                    key=lambda row: float(row["dl_mbps"]),
                )
                record_performance_evidence(
                    self._history,
                    context,
                    event_key=(
                        "performance-v2:explore-best:"
                        + self._session_id
                        + ":"
                        + self._current.target_signature
                    ),
                    scope_key=context_scope_key(context),
                    continuity_key=context_continuity_key(context),
                    record_type="explore_best_of_three",
                    target_signature=self._current.target_signature,
                    dl_mbps=float(best["dl_mbps"]),
                    observed_at=context.accepted_at,
                    details={
                        "session_id": self._session_id,
                        "sample_count": len(self._probe_results),
                        "sample_event_keys": [
                            "performance-v2:explore-probe:"
                            + str(row["request_id"])
                            for row in self._probe_results
                        ],
                        "best_ordinal": int(best["ordinal"]),
                    },
                )
                self._advance(context)
        return self.opportunity()

    def opportunity(self) -> AdvisoryOpportunity | None:
        if self._phase != "ready" or self._current is None:
            return None
        target = self._current
        return AdvisoryOpportunity(
            opportunity_id="explore:" + self._session_id + ":" + target.target_signature,
            kind="advisory",
            target=target.target_signature,
            action_target=target,
            reason="user_authorized_bounded_explore_target",
            eligible=True,
            uncertainty=1.0,
            disruption_cost=0.4,
            evidence_refs=(
                "explore-session:" + self._session_id,
                "explore-plan:serbia-major-lte-v1",
            ),
        )

    def admit_proposal_result(
        self,
        proposal_id: str,
        opportunity_id: str,
        submitted: bool,
        reason: str,
        context: IntelligenceContext,
    ) -> bool:
        if not opportunity_id.startswith("explore:") or self._current is None:
            return False
        self._proposal_id = proposal_id
        if submitted:
            self._phase = "waiting_outcome"
            self._reason = "command_submitted"
        else:
            # A dispatch refusal describes timing or context, not target quality.
            # Keep the target neutral. Brain decides when one retry is allowed.
            # Only a modem failure or reversion may skip the target.
            self._phase = "ready"
            self._proposal_id = ""
            self._reason = "command_not_submitted:" + str(reason or "unknown")
        return True

    def admit_outcome(self, *, command_id: str, origin: str, target_signature: str, terminal_status: str, terminal_at: float, context: IntelligenceContext) -> bool:
        if not command_id:
            raise ValueError("explore_outcome_command_id_required")
        terminal = (target_signature, terminal_status, float(terminal_at))
        if command_id in self._outcome_receipts:
            if self._outcome_receipts[command_id] != terminal:
                raise ValueError("explore_outcome_identity_conflict")
            return False
        if origin != "explore" or self._current is None:
            return False
        # These are exactly the fields changed by this outcome, _advance,
        # _begin_observation and _stop. Evidence keys are stable across retry.
        before = (
            self._phase, self._reason, self._current, self._target_index,
            self._current_started_at, self._proposal_id, self._skipped_bands.copy(),
            self._probe_results.copy(), self._probe_inflight,
            self._consecutive_probe_failures, self._consecutive_incomplete,
            self._last_band, self._stop_requested,
        )
        try:
            if target_signature != self._current.target_signature:
                self._stop("explore_outcome_target_mismatch")
            elif terminal_status == "settled":
                self._begin_observation(context, source="target_settled")
            else:
                self._skipped_bands.add(int(_target_value(self._current)["pband"]))
                self._record(
                    "target_failed", context,
                    {"terminal_status": terminal_status, "terminal_at": terminal_at},
                )
                self._advance(context)
        except BaseException:
            (
                self._phase, self._reason, self._current, self._target_index,
                self._current_started_at, self._proposal_id, self._skipped_bands,
                self._probe_results, self._probe_inflight,
                self._consecutive_probe_failures, self._consecutive_incomplete,
                self._last_band, self._stop_requested,
            ) = before
            raise
        self._outcome_receipts[command_id] = terminal
        if len(self._outcome_receipts) > 64:
            self._outcome_receipts.popitem(last=False)
        return True

    def claim_probe(self, context: IntelligenceContext) -> ExploreProbeRequest | None:
        if (
            self._phase != "observing"
            or self._current is None
            or self._probe_inflight is not None
            or not context.complete
        ):
            return None
        ordinal = len(self._probe_results)
        if ordinal >= len(EXPLORE_OBSERVATION_WINDOWS_S):
            return None
        if context.accepted_at < self._current_started_at + EXPLORE_OBSERVATION_WINDOWS_S[ordinal]:
            return None
        request = ExploreProbeRequest(
            request_id=f"{self._session_id}:{self._current.target_signature}:{ordinal + 1}",
            session_id=self._session_id,
            scope_key=context_scope_key(context),
            target_signature=self._current.target_signature,
            ordinal=ordinal + 1,
            requested_at=context.accepted_at,
        )
        self._probe_inflight = request
        self._probe_scope_changed = False
        return request

    def admit_probe_result(self, request: ExploreProbeRequest, result: Mapping[str, Any], context: IntelligenceContext) -> bool:
        if self._probe_inflight != request or request.session_id != self._session_id:
            return False
        self._probe_inflight = None
        if (self._probe_scope_changed or not context.complete
                or request.scope_key != context_scope_key(context)):
            self._record("probe_discarded", context,
                         {"ordinal": request.ordinal, "reason": "radio_scope_changed_or_incomplete"})
            return False
        success = result.get("status") == "measured"
        if not success:
            self._consecutive_probe_failures += 1
            self._record("probe_failed", context, {"ordinal": request.ordinal, "reason": str(result.get("reason") or "unknown")})
            if self._consecutive_probe_failures >= 2:
                self._stop("two_consecutive_capacity_probe_failures")
            return True
        self._consecutive_probe_failures = 0
        row = dict(result)
        row["request_id"] = request.request_id
        row["scope_key"] = request.scope_key
        row["ordinal"] = request.ordinal
        row["evidence"] = {
            "matrix_sequence": context.matrix_identity.matrix_sequence,
            "accepted_at": context.accepted_at,
            "serving_band": _current_fact(context, "radio.serving_band"),
            "ca_pool": _current_fact(
                context,
                "radio.configured_band_pool",
            ),
            "rsrp_dbm": _fresh_fact(context, "rf.rsrp_dbm"),
            "rsrq_db": _fresh_fact(context, "rf.rsrq_db"),
            "sinr_db": _fresh_fact(context, "rf.sinr_db"),
            "quality": dict(_surface(context, "quality")),
            "latency": dict(_surface(context, "latency")),
        }
        self._probe_results.append(row)
        record_performance_evidence(
            self._history,
            context,
            event_key="performance-v2:explore-probe:" + request.request_id,
            scope_key=request.scope_key,
            continuity_key=context_continuity_key(context),
            record_type="explore_probe",
            target_signature=request.target_signature,
            dl_mbps=float(row["dl_mbps"]),
            observed_at=float(row["completed_at"]),
            details={
                "session_id": request.session_id,
                "ordinal": request.ordinal,
                "measurement": row,
            },
        )
        return True

    def health(self) -> dict[str, Any]:
        return {
            "active": self._phase not in {"idle", "stopped", "complete"},
            "session_id": self._session_id,
            "stop_requested": self._stop_requested,
            "reason": self._reason,
            "phase": self._phase,
            "target_count": len(self._targets),
            "completed_count": len(self._completed),
            "skipped_bands": tuple(sorted(self._skipped_bands)),
            "skipped_targets": tuple(sorted(self._skipped_targets)),
            "current_target": self._current.target_signature if self._current else "",
        }


__all__ = (
    "EXPLORE_BANDS",
    "EXPLORE_OBSERVATION_WINDOWS_S",
    "ExploreCoordinator",
    "ExploreProbeRequest",
)
