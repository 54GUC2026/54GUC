"""Continuous Core interpretation of the Modem Live RTT register."""

from __future__ import annotations

import math
from dataclasses import replace
from threading import RLock
from typing import Any

from arch4.action_transaction import ActionTransactionKey
from arch4.core_contracts.action_reachability import (
    ACTION_REACHABILITY_OWNER,
    ActionReachabilityDisposition,
    ActionReachabilityReceipt,
    CoreCurrentReachabilityView,
    CoreReachabilityEvidenceView,
    CurrentReachabilityDisposition,
    ReachabilityOutageInterval,
    ReachabilityCoverageGap,
    ReachabilityPlannedActionInterval,
    ReachabilityProbeSample,
    ReachabilityActivityProof,
    ReachabilityServiceState,
    reachability_probe_gap_is_covered,
)
from arch4.core_contracts.physical_commands import (
    PHYSICAL_COORDINATION_OPERATIONS,
    PhysicalCommandReceipt,
)
from arch4.modem_live.rtt_register import ModemLiveRttRegisterSnapshot
from arch4.rtt_observation import (
    PROBE_FAILED,
    PROBE_OK,
    PROBE_REACHABLE_ONLY,
    PROBE_UNKNOWN,
    RttObservation,
)
from arch4.core_contracts.accepted_generation_root import CoreAcceptedGenerationRoot
from arch4.core_contracts.emergency_reachability import ModemLiveInternetProbeRound
from arch4.core_contracts.current_health import CoreCurrentHealthView, ModemLiveHealthState


DEFAULT_REACHABILITY_MAX_PROBE_GAP_S = 5.0
DEFAULT_REACHABILITY_RETENTION_S = 900.0
DEFAULT_REACHABILITY_MAX_SAMPLES = 2048


class CoreReachabilityTimeline:
    """Own RTT history plus exact planned radio-action exclusion intervals.

    This class never probes the modem. Core action status announces the start
    of a planned action and later closes it with the physical receipt.
    """

    owner = ACTION_REACHABILITY_OWNER

    def __init__(
        self,
        *,
        max_probe_gap_s: float = DEFAULT_REACHABILITY_MAX_PROBE_GAP_S,
        retention_s: float = DEFAULT_REACHABILITY_RETENTION_S,
        max_samples: int = DEFAULT_REACHABILITY_MAX_SAMPLES,
    ) -> None:
        max_gap = float(max_probe_gap_s)
        retention = float(retention_s)
        sample_limit = int(max_samples)
        if max_gap <= 0.0:
            raise ValueError("reachability_timeline_max_probe_gap_invalid")
        if retention <= max_gap:
            raise ValueError("reachability_timeline_retention_invalid")
        if sample_limit < 2:
            raise ValueError("reachability_timeline_sample_limit_invalid")
        self._lock = RLock()
        self._max_probe_gap_s = max_gap
        self._retention_s = retention
        self._max_samples = sample_limit
        self._session_scope_key = ""
        self._namespace = None
        self._identity_code = ""
        self._observer_id = ""
        self._last_register_sequence = 0
        self._samples: tuple[ReachabilityProbeSample, ...] = ()
        self._activity_proofs: tuple[ReachabilityActivityProof, ...] = ()
        self._last_activity_at = 0.0
        self._pending_loss_at = 0.0
        self._loss_confirmed_at = 0.0
        self._service_states: tuple[ReachabilityServiceState, ...] = ()
        self._service_was_up = False
        self._last_confirmation_id = ""
        self._latest_completed_outage: ReachabilityOutageInterval | None = None
        self._latest_coverage_gap: ReachabilityCoverageGap | None = None
        self._coverage_anchor_at = 0.0
        self._traffic_acquired_at = 0.0
        self._traffic_attempt_id = ""
        self._traffic_namespace = None
        self._received_bytes_per_sec = 0.0
        self._completed_actions: dict[str, ActionReachabilityReceipt] = {}
        # Planned command lifetime is separate from whether its pause can be
        # measured. A recovery often starts with an already failed ping.
        self._planned_actions: dict[str, tuple[str, float, float | None]] = {}
        self._failure_count = 0
        self._last_error = ""
        self._last_error_operation = ""
        self._modem_health: CoreCurrentHealthView | None = None
        self._host_recovery_not_before = 0.0

    def observe(self, snapshot: ModemLiveRttRegisterSnapshot) -> bool:
        """Accept one new immutable RTT-register revision.

        Empty pre-session snapshots are ignored. A different complete scope is
        an atomic device/session replacement and clears the old timeline.
        """

        if not isinstance(snapshot, ModemLiveRttRegisterSnapshot):
            raise TypeError("reachability_timeline_rtt_register_required")
        session_scope_key = str(snapshot.session_scope_key or "").strip()
        identity_code = str(snapshot.identity_code or "").strip()
        if not session_scope_key or not identity_code:
            return False
        namespace = snapshot.observation.namespace
        if namespace.session_scope_key != session_scope_key:
            raise ValueError("reachability_rtt_namespace_scope_mismatch")
        sample = self._probe_sample(snapshot.observation)
        with self._lock:
            current_scope = (
                self._session_scope_key,
                self._identity_code,
            )
            incoming_scope = (session_scope_key, identity_code)
            if current_scope != ("", "") and incoming_scope != current_scope:
                self._reset_scope_locked(
                    session_scope_key=session_scope_key,
                    identity_code=identity_code,
                )
            elif current_scope == ("", ""):
                self._reset_scope_locked(
                    session_scope_key=session_scope_key,
                    identity_code=identity_code,
                )
            if snapshot.sequence < self._last_register_sequence:
                raise RuntimeError(
                    "reachability_timeline_register_sequence_regression"
                )
            if self._namespace is not None and self._namespace != namespace:
                raise RuntimeError("reachability_rtt_namespace_changed_without_scope")
            # Bind the existing observation owner even before its first ping
            # finishes. Fresh Matrix RX can already prove service at startup.
            self._namespace = namespace
            if not self._observer_id:
                self._observer_id = snapshot.observation.source
            if snapshot.sequence == self._last_register_sequence:
                self._observe_confirmation_locked(snapshot)
                self._last_error = ""
                self._last_error_operation = ""
                return False
            self._last_register_sequence = int(snapshot.sequence)
            if sample is None:
                self._last_error = ""
                self._last_error_operation = ""
                return False
            if self._observer_id and sample.source != self._observer_id:
                raise RuntimeError("reachability_timeline_probe_owner_changed")
            if self._samples and sample.probed_at <= self._samples[-1].probed_at:
                if (
                    sample.probed_at == self._samples[-1].probed_at
                    and sample == self._samples[-1]
                ):
                    self._last_error = ""
                    self._last_error_operation = ""
                    return False
                raise RuntimeError("reachability_timeline_probe_regression")
            self._observer_id = sample.source
            # Close elapsed loss before a new positive sample replaces it.
            # The interval must not depend on how often a GUI asks for NET.
            if sample.reachable is not True:
                self._note_loss_observation_locked(sample.probed_at)
            if not (sample.reachable is True
                    and self._host_reply_is_verified_locked(sample.probed_at)
                    and sample.probed_at <= self._last_activity_at + self._max_probe_gap_s):
                self._refresh_confirmation_locked(now=sample.probed_at)
            sample = self._resolve_service_sample_locked(sample)
            if sample.reachable is True and self._host_reply_is_verified_locked(sample.probed_at):
                self._last_activity_at = max(self._last_activity_at, sample.probed_at)
                self._pending_loss_at = 0.0
            # An unknown attempt does not resume checks. Keep the last usable
            # check (or first unknown at startup) until a known result arrives.
            # One scalar retains the episode even if old samples are pruned.
            if self._coverage_anchor_at == 0.0:
                self._coverage_anchor_at = sample.probed_at
            elif sample.reachable is not None:
                if (
                    not self._samples or self._samples[-1].reachable is None
                    or sample.probed_at - self._samples[-1].probed_at > self._max_probe_gap_s
                ):
                    if sample.probed_at - self._coverage_anchor_at >= self._max_probe_gap_s:
                        self._latest_coverage_gap = ReachabilityCoverageGap(
                            last_probe_at=self._coverage_anchor_at,
                            resumed_at=sample.probed_at,
                        )
                self._coverage_anchor_at = sample.probed_at
            self._samples = (*self._samples, sample)
            self._prune_locked(reference_ts=sample.probed_at)
            self._refresh_confirmation_locked(now=snapshot.observation.snapshot_at)
            self._observe_confirmation_locked(snapshot)
            self._last_error = ""
            self._last_error_operation = ""
            return True

    def receipt_for_action(
        self,
        *,
        key: ActionTransactionKey,
        identity_code: str,
        physical_receipt: PhysicalCommandReceipt,
        coverage_required_at: float,
    ) -> ActionReachabilityReceipt | None:
        """Select a complete command interval from the continuous timeline."""

        if not isinstance(key, ActionTransactionKey):
            raise TypeError("reachability_timeline_transaction_key_required")
        if not isinstance(physical_receipt, PhysicalCommandReceipt):
            raise TypeError("reachability_timeline_physical_receipt_required")
        identity = str(identity_code or "").strip()
        if not identity:
            raise ValueError("reachability_timeline_identity_code_required")
        if (
            physical_receipt.request_id != key.command_id
            or physical_receipt.transaction_root_id != key.root_id
            or physical_receipt.target != key.target
        ):
            raise RuntimeError("reachability_timeline_physical_receipt_mismatch")
        coverage_anchor = float(coverage_required_at)
        if coverage_anchor <= 0.0:
            raise ValueError(
                "reachability_timeline_coverage_timestamp_required"
            )
        cellular_stages = tuple(
            stage
            for stage in physical_receipt.stages
            if stage.stage_kind not in PHYSICAL_COORDINATION_OPERATIONS
        )
        physical_started_at = (
            min(stage.request_dispatched_at for stage in cellular_stages)
            if cellular_stages
            else physical_receipt.execution_started_at
        )
        physical_completed_at = (
            max(stage.transport_returned_at for stage in cellular_stages)
            if cellular_stages
            else physical_receipt.transaction_completed_at
        )
        # Physical evidence selects the interval; it cannot rewrite past NET.
        return self.receipt_for_interval(
            key=key,
            identity_code=identity,
            measurement_started_at=physical_started_at,
            coverage_required_at=max(physical_completed_at, coverage_anchor),
            physical_completed_at=physical_completed_at,
        )

    def begin_planned_action(
        self,
        *,
        key: ActionTransactionKey,
        identity_code: str,
        started_at: float,
    ) -> None:
        """Mark an intentional radio-action interval before its first probe."""

        if not isinstance(key, ActionTransactionKey):
            raise TypeError("reachability_timeline_transaction_key_required")
        identity = str(identity_code or "").strip()
        if not identity:
            raise ValueError("reachability_timeline_identity_code_required")
        action_started_at = float(started_at)
        if not math.isfinite(action_started_at) or action_started_at <= 0.0:
            raise ValueError("reachability_timeline_interval_timestamp_required")
        with self._lock:
            if (
                self._session_scope_key
                != key.matrix_identity.session_scope_key
                or self._identity_code != identity
            ):
                raise RuntimeError("reachability_timeline_planned_action_scope_mismatch")
            current = self._planned_actions.get(key.root_id)
            candidate = (key.command_id, action_started_at)
            if current is not None and current[:2] != candidate:
                # A complete physical receipt may precede the first status
                # publication. Authority then supplies the earlier request
                # start; accept that exact command without reopening it.
                if (
                    current[0] != key.command_id
                    or current[2] is None
                    or action_started_at > current[1]
                ):
                    raise RuntimeError("reachability_timeline_planned_action_identity_mismatch")
                self._planned_actions[key.root_id] = (
                    key.command_id, action_started_at, current[2]
                )
            if current is None:
                self._planned_actions[key.root_id] = (*candidate, None)

    def end_planned_action(
        self,
        *,
        key: ActionTransactionKey,
        identity_code: str,
        started_at: float,
        ended_at: float,
    ) -> None:
        """Close from Command Authority even when pause coverage is missing."""

        end = float(ended_at)
        start = float(started_at)
        if not math.isfinite(end) or end < start:
            raise ValueError("reachability_timeline_planned_action_end_invalid")
        with self._lock:
            self.begin_planned_action(
                key=key, identity_code=identity_code, started_at=start
            )
            command, opened, previous_end = self._planned_actions[key.root_id]
            cutoff = (
                self._samples[-1].probed_at - self._retention_s
                if self._samples else float("-inf")
            )
            closed = max(end, previous_end or end)
            if closed < cutoff:
                self._planned_actions.pop(key.root_id)
                return
            self._planned_actions[key.root_id] = (
                command, opened, closed
            )

    def receipt_for_interval(
        self,
        *,
        key: ActionTransactionKey,
        identity_code: str,
        measurement_started_at: float,
        coverage_required_at: float,
        physical_completed_at: float,
    ) -> ActionReachabilityReceipt | None:
        """Measure one action interval without interpreting its outcome."""

        if not isinstance(key, ActionTransactionKey):
            raise TypeError("reachability_timeline_transaction_key_required")
        identity = str(identity_code or "").strip()
        if not identity:
            raise ValueError("reachability_timeline_identity_code_required")
        started_at = float(measurement_started_at)
        coverage_anchor = float(coverage_required_at)
        physical_end = float(physical_completed_at)
        if started_at <= 0.0 or coverage_anchor <= 0.0 or physical_end <= 0.0:
            raise ValueError("reachability_timeline_interval_timestamp_required")
        if physical_end < started_at or coverage_anchor < physical_end:
            raise ValueError("reachability_timeline_interval_order_invalid")
        with self._lock:
            completed = self._completed_actions.get(key.root_id)
            if completed is not None:
                if (
                    completed.request_id != key.command_id
                    or completed.matrix_identity != key.matrix_identity
                    or completed.identity_key != key.identity_key
                ):
                    raise RuntimeError(
                        "reachability_timeline_completed_action_identity_mismatch"
                    )
                return completed
            if (
                self._session_scope_key
                != key.matrix_identity.session_scope_key
                or self._identity_code != identity
            ):
                return None
            samples = tuple(self._samples)
            service_states = tuple(self._service_states)
            observer_id = self._observer_id
            max_probe_gap_s = self._max_probe_gap_s
        selected = self._covered_interval(
            samples=samples,
            service_states=service_states,
            measurement_started_at=started_at,
            coverage_required_at=coverage_anchor,
            max_probe_gap_s=max_probe_gap_s,
        )
        if selected is None or not observer_id:
            return None
        measurement_samples, measurement_completed_at = selected
        intervals = self._outage_intervals(
            states=service_states,
            measurement_started_at=started_at,
            measurement_completed_at=measurement_completed_at,
        )
        if intervals is None:
            return None
        receipt = ActionReachabilityReceipt(
            request_id=key.command_id,
            transaction_root_id=key.root_id,
            matrix_identity=key.matrix_identity,
            identity_key=key.identity_key,
            identity_code=identity,
            observer_id=observer_id,
            measurement_started_at=started_at,
            measurement_completed_at=measurement_completed_at,
            physical_completed_at=physical_end,
            max_probe_gap_s=max_probe_gap_s,
            coverage_complete=True,
            samples=measurement_samples,
            outage_intervals=intervals,
            complete=True,
            disposition=ActionReachabilityDisposition.MEASURED,
        )
        with self._lock:
            if (
                self._session_scope_key == key.matrix_identity.session_scope_key
                and self._identity_code == identity
            ):
                self._completed_actions[key.root_id] = receipt
                planned = self._planned_actions.get(key.root_id)
                # A requested physical receipt is also exact planned-action
                # evidence, even if no active-status publication preceded it.
                command, opened, closed = (
                    planned
                    if planned is not None
                    else (key.command_id, started_at, None)
                )
                self._planned_actions[key.root_id] = (
                    command,
                    opened,
                    max(measurement_completed_at, closed or measurement_completed_at),
                )
                while len(self._completed_actions) > 128:
                    oldest = next(iter(self._completed_actions))
                    self._completed_actions.pop(oldest, None)
        return receipt

    def note_failure(
        self,
        *,
        operation: str,
        error: BaseException,
    ) -> None:
        if not isinstance(error, BaseException):
            raise TypeError("reachability_timeline_intake_error_required")
        operation_name = str(operation or "").strip()
        if not operation_name:
            raise ValueError("reachability_timeline_failure_operation_required")
        with self._lock:
            self._failure_count += 1
            self._last_error = f"{type(error).__name__}: {error}"
            self._last_error_operation = operation_name

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "owner": self.owner,
                "session_scope_key": self._session_scope_key,
                "identity_code": self._identity_code,
                "observer_id": self._observer_id,
                "last_register_sequence": self._last_register_sequence,
                "sample_count": len(self._samples),
                "completed_action_count": len(self._completed_actions),
                "active_planned_action_count": sum(
                    closed is None
                    for _command, _start, closed in self._planned_actions.values()
                ),
                "oldest_probe_at": (
                    self._samples[0].probed_at if self._samples else 0.0
                ),
                "latest_probe_at": (
                    self._samples[-1].probed_at if self._samples else 0.0
                ),
                "latest_activity_at": self._latest_activity_at_locked(),
                "activity_proof_count": len(self._activity_proofs),
                "failure_count": self._failure_count,
                "last_error": self._last_error,
                "last_error_operation": self._last_error_operation,
            }

    def observe_traffic_context(self, root: CoreAcceptedGenerationRoot) -> None:
        """Consume acquired traffic facts; never turn traffic into a ping reply."""

        if not isinstance(root, CoreAcceptedGenerationRoot):
            raise TypeError("reachability_accepted_root_required")
        namespace = root.matrix_identity.namespace
        facts = root.current_facts
        try:
            rx_rate = facts.exact("traffic.rx_bytes_per_sec")
        except KeyError:
            # Missing receive evidence cannot renew service.
            return
        # The RX fact owns its validity and capture time. Packet-session status
        # and TX may arrive on other lanes; neither can invalidate fresh RX.
        if (
            rx_rate.disposition != "available"
            or rx_rate.acquisition_namespace != namespace
            or rx_rate.retention is not None
            or not rx_rate.acquisition_attempt_id
            or rx_rate.acquired_at <= 0.0
        ):
            return
        rx_value = rx_rate.value.to_mapping()["value"]
        if (
            type(rx_value) not in (int, float)
            or not math.isfinite(rx_value)
            or rx_value < 0.0
        ):
            return
        with self._lock:
            if namespace != self._namespace or not self._observer_id:
                return
            if self._traffic_namespace not in (None, namespace):
                return
            if (
                rx_rate.acquisition_attempt_id == self._traffic_attempt_id
                or rx_rate.acquired_at <= self._traffic_acquired_at
                or rx_rate.acquired_at > root.commit.committed_at
            ):
                return
            self._traffic_namespace = namespace
            self._traffic_attempt_id = rx_rate.acquisition_attempt_id
            self._traffic_acquired_at = rx_rate.acquired_at
            self._received_bytes_per_sec = float(rx_value)
            if self._traffic_is_fresh_and_active_locked(now=root.commit.committed_at):
                self._record_activity_locked(ReachabilityActivityProof(
                    confirmed_at=rx_rate.acquired_at,
                    source="wan_rx", evidence_id=rx_rate.acquisition_attempt_id,
                ), observed_at=root.commit.committed_at)
            self._refresh_confirmation_locked(now=root.commit.committed_at)

    def observe_modem_health(self, health: CoreCurrentHealthView) -> None:
        """Independent contact evidence; a router reply is not Internet proof."""
        if not isinstance(health, CoreCurrentHealthView):
            raise TypeError("reachability_current_health_view_required")
        with self._lock:
            if health.namespace != self._namespace:
                return
            if self._modem_health is not None and health.register_sequence < self._modem_health.register_sequence:
                raise RuntimeError("reachability_modem_health_regressed")
            previous = self._modem_health.facts if self._modem_health is not None else None
            if (health.facts is not None and health.facts.state is ModemLiveHealthState.OK
                    and health.facts.last_failure_ts > 0.0
                    and (previous is None or previous.state is not ModemLiveHealthState.OK
                         or health.facts.last_failure_ts > previous.last_failure_ts)):
                self._host_recovery_not_before = max(
                    self._host_recovery_not_before, health.facts.last_success_ts)
            self._modem_health = health
            if (health.facts is not None and health.facts.state is not ModemLiveHealthState.OK
                    and not any(start <= health.facts.last_failure_ts and
                                (end is None or health.facts.last_failure_ts <= end)
                                for _command, start, end in self._planned_actions.values())):
                self._note_loss_observation_locked(health.facts.last_failure_ts)

    def _note_loss_observation_locked(self, stamp: float) -> None:
        # Confirmation uses the positive-evidence timer; duration begins at
        # the first observed failure, not when that timer expires.
        if stamp > self._last_activity_at and not self._pending_loss_at:
            self._pending_loss_at = stamp

    def _host_reply_is_verified_locked(self, stamp: float) -> bool:
        """PC Internet may belong to another Wi-Fi after the modem disappears."""
        health = self._modem_health.facts if self._modem_health is not None else None
        if health is None:
            return True  # Initial namespace is bound by the verified startup Matrix.
        if health.state is not ModemLiveHealthState.OK:
            # A new accepted modem RX fact has already passed identity checks,
            # even when the separate health publication has not caught up yet.
            return self._traffic_acquired_at > health.last_failure_ts and stamp >= self._traffic_acquired_at
        return stamp >= self._host_recovery_not_before and stamp > health.last_failure_ts

    def _traffic_is_fresh_and_active_locked(self, *, now: float) -> bool:
        return bool(
            self._received_bytes_per_sec > 0.0
            and 0.0 <= now - self._traffic_acquired_at < self._max_probe_gap_s
        )

    def _latest_activity_at_locked(self) -> float:
        # The timer anchor must survive pruning of detailed debug samples.
        return self._last_activity_at

    def _activity_is_fresh_locked(self, *, now: float) -> bool:
        stamp = self._latest_activity_at_locked()
        # A producer/reader can arrive with an older timestamp than a proof
        # already accepted here. Older time cannot expire that newer proof.
        return stamp > 0.0 and now < stamp + self._max_probe_gap_s

    def _record_activity_locked(self, proof: ReachabilityActivityProof, *, observed_at: float) -> None:
        if not math.isfinite(observed_at) or observed_at <= 0.0 or proof.confirmed_at > observed_at:
            raise RuntimeError("reachability_activity_after_observation")
        if observed_at - proof.confirmed_at >= self._max_probe_gap_s:
            return
        if proof.source != "wan_rx" and not self._host_reply_is_verified_locked(proof.confirmed_at):
            return  # Raw confirmation remains in its source register for diagnosis.
        if any(p.source == proof.source and p.evidence_id == proof.evidence_id
               for p in self._activity_proofs):
            return
        if self._activity_proofs and proof.confirmed_at < self._activity_proofs[-1].confirmed_at:
            return
        self._refresh_confirmation_locked(now=proof.confirmed_at)
        self._last_activity_at = max(self._last_activity_at, proof.confirmed_at)
        self._pending_loss_at = 0.0
        # RX changes NET, not the raw ping history.
        self._set_service_locked(proof.confirmed_at, True, observed_at=observed_at)
        if self._coverage_anchor_at and proof.confirmed_at - self._coverage_anchor_at > self._max_probe_gap_s:
            self._latest_coverage_gap = ReachabilityCoverageGap(
                last_probe_at=self._coverage_anchor_at, resumed_at=proof.confirmed_at)
        self._coverage_anchor_at = max(self._coverage_anchor_at, proof.confirmed_at)
        self._activity_proofs = (*self._activity_proofs, proof)[-self._max_samples:]
        self._prune_locked(reference_ts=proof.confirmed_at)

    def _resolve_service_sample_locked(
        self,
        sample: ReachabilityProbeSample,
    ) -> ReachabilityProbeSample:
        """Classify one raw attempt without creating another NET owner."""

        if sample.reachable is True:
            if self._host_reply_is_verified_locked(sample.probed_at):
                return replace(sample, service_reachable=True)
            # Keep the raw reply, but it cannot close a modem interruption or
            # count towards recovery while its route belongs to an unknown device.
            sample = replace(sample, service_reachable=None)
        if (self._activity_proofs and
                0.0 <= sample.probed_at - self._activity_proofs[-1].confirmed_at < self._max_probe_gap_s):
            return replace(sample, service_reachable=True)
        if sample.reachable is not False:
            return sample
        if sample.reachable is False and any(
            started <= sample.probed_at and (ended is None or sample.probed_at <= ended)
            for _command, started, ended in self._planned_actions.values()
        ):
            return replace(sample, service_reachable=False)
        # Unplanned loss uses elapsed positive-evidence time, never a count
        # of failed destinations and never a rewrite of an earlier sample.
        if not self._activity_is_fresh_locked(now=sample.probed_at):
            return replace(sample, service_reachable=False)
        return sample

    def _confirmation_reason_locked(self, *, now: float) -> str:
        if self._activity_is_fresh_locked(now=now):
            return ""
        if not self._samples:
            return "reachability_probe_not_available"
        latest = self._samples[-1]
        if now - latest.probed_at > self._max_probe_gap_s:
            return "reachability_probe_stale"
        if latest.reachable is None:
            return "reachability_check_unknown"
        if latest.reachable is True:
            return ""
        if latest.service_reachable is not False:
            return "reachability_failure_awaiting_confirmation"
        return ""

    def _refresh_confirmation_locked(self, *, now: float) -> None:
        latest = self._samples[-1] if self._samples else None
        if latest is not None and now < latest.probed_at:
            return  # A stale clock/view cannot reinterpret a newer probe.
        planned = latest is not None and latest.service_reachable is False and any(
            started <= latest.probed_at and (ended is None or latest.probed_at <= ended)
            for _command, started, ended in self._planned_actions.values()
        )
        if planned and not (self._activity_proofs and self._activity_proofs[-1].confirmed_at >= latest.probed_at):
            self._set_service_locked(latest.probed_at, False, observed_at=now)
            return
        if self._activity_is_fresh_locked(now=now):
            activity_at = self._latest_activity_at_locked()
            self._set_service_locked(activity_at, True, observed_at=max(now, activity_at))
            return
        activity_at = self._latest_activity_at_locked()
        deadline = activity_at + self._max_probe_gap_s if activity_at else 0.0
        health = self._modem_health.facts if self._modem_health is not None else None
        if (health is not None and health.state is not ModemLiveHealthState.OK
                and activity_at <= health.last_failure_ts <= now and deadline
                and now >= max(deadline, health.last_success_ts)):
            # This clock path must not wait for a stuck Windows ping process.
            self._set_service_locked(max(deadline, health.last_success_ts), False, observed_at=now)
        elif latest is not None and latest.reachable is False and deadline and now >= deadline:
            self._set_service_locked(max(deadline, latest.probed_at), False, observed_at=now)
        elif not self._service_states and latest is not None:
            # Monitoring has not yet received its first positive proof.
            self._set_service_locked(latest.probed_at, False, observed_at=now)

    def _set_service_locked(self, stamp: float, reachable: bool, *, observed_at: float) -> None:
        if not math.isfinite(observed_at) or observed_at <= 0.0 or stamp > observed_at:
            raise RuntimeError("reachability_service_state_after_observation")
        previous = self._service_states[-1] if self._service_states else None
        if previous and previous.reachable == reachable:
            return
        if (previous and not previous.reachable and reachable
                and stamp < self._loss_confirmed_at):
            # A later-delivered proof must not erase an already published loss.
            planned_loss = any(start <= previous.observed_at and (end is None or previous.observed_at <= end)
                               for _command, start, end in self._planned_actions.values())
            if (observed_at < self._loss_confirmed_at
                    or (planned_loss and stamp < previous.observed_at)
                    or not self._activity_is_fresh_locked(now=observed_at)
                    or not self._host_reply_is_verified_locked(stamp)):
                return
            stamp = observed_at
        if not reachable and self._service_was_up:
            planned = any(start <= stamp and (end is None or stamp <= end)
                          for _command, start, end in self._planned_actions.values())
            if not planned and observed_at < self._latest_activity_at_locked() + self._max_probe_gap_s:
                raise RuntimeError("reachability_unplanned_loss_before_activity_deadline")
            if self._pending_loss_at and self._pending_loss_at <= stamp:
                stamp = self._pending_loss_at
            self._loss_confirmed_at = observed_at
        if previous and stamp < previous.observed_at:
            planned_loss = any(start <= previous.observed_at and (end is None or previous.observed_at <= end)
                               for _command, start, end in self._planned_actions.values())
            if (not reachable or observed_at < previous.observed_at or planned_loss
                    or not self._activity_is_fresh_locked(now=observed_at)
                    or not self._host_reply_is_verified_locked(stamp)):
                return
            # Fresh proof arrived after expiry was published. Resume now, not
            # before the saved loss, and do not renew the proof's original time.
            stamp = observed_at
        if previous and stamp == previous.observed_at:
            # Equal-time positive evidence wins; do not create a zero-length
            # outage or let delivery order produce two opposing states.
            if not reachable:
                return
            self._service_states = self._service_states[:-1]
            if self._service_states and self._service_states[-1].reachable:
                return
            previous = None
        if previous and not previous.reachable and reachable and self._service_was_up:
            self._latest_completed_outage = ReachabilityOutageInterval(
                started_at=previous.observed_at, restored_at=stamp)
        self._service_was_up = self._service_was_up or reachable
        self._service_states = (*self._service_states,
                                ReachabilityServiceState(stamp, reachable))[-self._max_samples:]

    def _observe_confirmation_locked(self, snapshot: ModemLiveRttRegisterSnapshot) -> None:
        result = snapshot.confirmation
        if (result is None or result.request_id == self._last_confirmation_id
                or result.namespace != self._namespace or not self._observer_id
                or not 0.0 <= snapshot.observation.snapshot_at - result.completed_at < self._max_probe_gap_s):
            return
        self._last_confirmation_id = result.request_id
        if result.response_received:
            self._record_activity_locked(ReachabilityActivityProof(
                confirmed_at=result.completed_at, source="external_response",
                evidence_id=result.request_id), observed_at=snapshot.observation.snapshot_at)
        elif result.requested_at >= self._latest_activity_at_locked() + self._max_probe_gap_s:
            # Expired evidence AND an unsuccessful independent check. A reply
            # or RX after the request wins, even if this negative result is late.
            self._set_service_locked(result.completed_at, False,
                                     observed_at=snapshot.observation.snapshot_at)

    def needs_service_confirmation(self, *, now: float) -> bool:
        """Verification need is private policy, not a third customer NET state."""
        with self._lock:
            return bool(self._observer_id and self._confirmation_reason_locked(now=now) in {
                "reachability_probe_stale", "reachability_check_unknown",
                "reachability_probe_not_available",
            })

    def observe_verification(self, result: ModemLiveInternetProbeRound, *, namespace, now: float) -> None:
        """A real recovery-check reply is activity too, not a quality RTT."""
        if not isinstance(result, ModemLiveInternetProbeRound):
            raise TypeError("reachability_verification_round_required")
        with self._lock:
            if namespace != self._namespace or not self._observer_id:
                return
            reply = next((p for p in reversed(result.probes) if p.reachable is True), None)
            if reply is not None and 0.0 <= now - reply.probed_at < self._max_probe_gap_s:
                self._record_activity_locked(ReachabilityActivityProof(
                    confirmed_at=reply.probed_at, source="verification_reply",
                    evidence_id=result.round_id + ":" + reply.target), observed_at=now)

    def activity_success_streak(self, *, now: float) -> int:
        """Distinct service proofs can establish recovery without inventing pings."""
        with self._lock:
            if not self._activity_proofs or not (
                0 <= now - self._activity_proofs[-1].confirmed_at < self._max_probe_gap_s
            ):
                return 0
            last_loss = next((sample.probed_at for sample in reversed(self._samples)
                              if sample.service_reachable is False), 0.0)
            count = 0
            right = float(now)
            for proof in reversed(self._activity_proofs):
                if proof.confirmed_at <= last_loss or right - proof.confirmed_at > self._max_probe_gap_s:
                    break
                # A repeated timestamp or simultaneous RX/TCP result is one
                # observation, not two consecutive recovery confirmations.
                if not count or proof.confirmed_at < right:
                    count += 1
                right = proof.confirmed_at
            return count

    def primary_reachability_streaks(self, *, now: float) -> tuple[int, float, int]:
        """Return the one canonical primary failure/success streak view."""

        with self._lock:
            samples = tuple(self._samples)
            reason = self._confirmation_reason_locked(now=now)
            if self._activity_proofs and 0.0 <= now - self._activity_proofs[-1].confirmed_at < self._max_probe_gap_s:
                # Valid traffic cancels a recovery permit but is not five
                # successful pings. Recovery still follows the existing policy.
                return 0, 0.0, 0
        if reason and reason != "reachability_failure_awaiting_confirmation":
            return 0, 0.0, 0
        failure_count = 0
        success_count = 0
        started_at = 0.0
        right = samples[-1] if samples else None
        if samples and samples[-1].reachable is True and samples[-1].service_reachable is True:
            for sample in reversed(samples):
                if sample.reachable is not True or sample.service_reachable is not True or not reachability_probe_gap_is_covered(
                    sample, right, max_probe_gap_s=self._max_probe_gap_s,
                ):
                    break
                success_count += 1
                right = sample
        else:
            for sample in reversed(samples):
                if sample.reachable is not False or sample.service_reachable is True or not reachability_probe_gap_is_covered(
                    sample, right, max_probe_gap_s=self._max_probe_gap_s,
                ):
                    break
                failure_count += 1
                started_at = sample.probed_at
                right = sample
        return failure_count, started_at, success_count

    def current_view(self, *, now: float) -> CoreCurrentReachabilityView:
        """Publish the last Core binary decision while confirmation is pending."""

        if not math.isfinite(now) or now <= 0.0:
            raise ValueError("reachability_timeline_current_timestamp_required")
        with self._lock:
            self._refresh_confirmation_locked(now=now)
            state = self._service_states[-1] if self._service_states else None
            latest_probe_at = self._samples[-1].probed_at if self._samples else 0.0
            observed_at = max(float(now), latest_probe_at, self._loss_confirmed_at,
                              state.observed_at if state else 0.0,
                              self._activity_proofs[-1].confirmed_at if self._activity_proofs else 0.0)
            return CoreCurrentReachabilityView(
                session_scope_key=self._session_scope_key, identity_code=self._identity_code,
                observer_id=self._observer_id, register_sequence=self._last_register_sequence,
                observed_at=observed_at, latest_probe_at=latest_probe_at,
                disposition=(CurrentReachabilityDisposition.UNAVAILABLE if state is None else
                             CurrentReachabilityDisposition.REACHABLE if state.reachable else
                             CurrentReachabilityDisposition.OUTAGE),
                outage_started_at=state.observed_at if state and not state.reachable else 0.0,
                outage_s=observed_at - state.observed_at if state and not state.reachable else 0.0,
                latest_completed_outage=self._latest_completed_outage,
                latest_coverage_gap=self._latest_coverage_gap,
                reason="reachability_probe_not_available" if state is None else "",
                max_probe_gap_s=self._max_probe_gap_s,
            )

    def evidence_view(self, *, now: float) -> CoreReachabilityEvidenceView:
        """Publish the bounded timeline without giving its ownership away."""

        observed_at = float(now)
        if observed_at <= 0.0:
            raise ValueError("reachability_timeline_evidence_timestamp_required")
        with self._lock:
            self._refresh_confirmation_locked(now=observed_at)
            samples = tuple(self._samples)
            latest_probe_at = samples[-1].probed_at if samples else 0.0
            oldest_probe_at = samples[0].probed_at if samples else 0.0
            planned_action_intervals = tuple(
                ReachabilityPlannedActionInterval(
                    action_id=root_id,
                    started_at=started_at,
                    ended_at=(
                        max(observed_at, started_at) if ended_at is None else ended_at
                    ),
                )
                for root_id, (_command_id, started_at, ended_at) in sorted(
                    self._planned_actions.items(),
                    key=lambda item: item[1][1],
                )
                if ended_at is None or not oldest_probe_at or ended_at >= oldest_probe_at
            )
            return CoreReachabilityEvidenceView(
                session_scope_key=self._session_scope_key,
                identity_code=self._identity_code,
                observer_id=self._observer_id,
                register_sequence=self._last_register_sequence,
                observed_at=max(observed_at, latest_probe_at, self._loss_confirmed_at,
                                self._service_states[-1].observed_at if self._service_states else 0.0,
                                self._activity_proofs[-1].confirmed_at if self._activity_proofs else 0.0),
                max_probe_gap_s=self._max_probe_gap_s,
                samples=samples,
                activity_proofs=tuple(self._activity_proofs),
                service_states=tuple(self._service_states),
                planned_action_intervals=planned_action_intervals,
            )

    def _reset_scope_locked(
        self,
        *,
        session_scope_key: str,
        identity_code: str,
    ) -> None:
        self._session_scope_key = session_scope_key
        self._namespace = None
        self._identity_code = identity_code
        self._observer_id = ""
        self._last_register_sequence = 0
        self._samples = ()
        self._activity_proofs = ()
        self._last_activity_at = 0.0
        self._pending_loss_at = 0.0
        self._loss_confirmed_at = 0.0
        self._service_states = ()
        self._service_was_up = False
        self._modem_health = None
        self._host_recovery_not_before = 0.0
        self._last_confirmation_id = ""
        self._latest_completed_outage = None
        self._latest_coverage_gap = None
        self._coverage_anchor_at = 0.0
        self._traffic_acquired_at = 0.0
        self._traffic_attempt_id = ""
        self._traffic_namespace = None
        self._received_bytes_per_sec = 0.0
        self._completed_actions = {}
        self._planned_actions = {}
        self._last_error = ""
        self._last_error_operation = ""

    def _prune_locked(self, *, reference_ts: float) -> None:
        cutoff = float(reference_ts) - self._retention_s
        retained = tuple(
            sample for sample in self._samples if sample.probed_at >= cutoff
        )
        if len(retained) > self._max_samples:
            retained = retained[-self._max_samples :]
        self._samples = retained
        self._activity_proofs = tuple(p for p in self._activity_proofs if p.confirmed_at >= cutoff)
        before = tuple(s for s in self._service_states if s.observed_at < cutoff)
        self._service_states = (*before[-1:], *(s for s in self._service_states if s.observed_at >= cutoff))
        self._planned_actions = {
            root: interval
            for root, interval in self._planned_actions.items()
            if interval[2] is None or interval[2] >= cutoff
        }

    @staticmethod
    def _probe_sample(
        observation: RttObservation,
    ) -> ReachabilityProbeSample | None:
        if not isinstance(observation, RttObservation):
            raise TypeError("reachability_timeline_rtt_observation_required")
        if observation.last_probe_at <= 0.0:
            return None
        if observation.probe_status == PROBE_OK:
            return ReachabilityProbeSample(
                probed_at=observation.last_probe_at,
                reachable=True,
                source=observation.source,
                target=observation.target,
                service_reachable=True,
            )
        if observation.probe_status == PROBE_REACHABLE_ONLY:
            return ReachabilityProbeSample(
                probed_at=observation.last_probe_at,
                reachable=True,
                source=observation.source,
                target=observation.target,
                service_reachable=True,
            )
        if observation.probe_status in (PROBE_FAILED, PROBE_UNKNOWN):
            return ReachabilityProbeSample(
                probed_at=observation.last_probe_at,
                reachable=False if observation.probe_status == PROBE_FAILED else None,
                source=observation.source,
                target=observation.target,
                service_reachable=None,
                failure_reason=observation.failure_reason,
            )
        return None

    @staticmethod
    def _covered_interval(
        *,
        samples: tuple[ReachabilityProbeSample, ...],
        service_states: tuple[ReachabilityServiceState, ...],
        measurement_started_at: float,
        coverage_required_at: float,
        max_probe_gap_s: float,
    ) -> tuple[tuple[ReachabilityProbeSample, ...], float] | None:
        before = [
            sample
            for sample in samples
            if sample.probed_at <= measurement_started_at
        ]
        if not before:
            return None
        baseline = before[-1]
        if (
            baseline.service_reachable is not True
            or measurement_started_at - baseline.probed_at > max_probe_gap_s
        ):
            return None
        after_required_coverage = [
            sample
            for sample in samples
            if sample.probed_at >= coverage_required_at
            and sample.service_reachable is True
            # RX can close the canonical outage after this successful ping.
            # Select an endpoint covering that closure instead of repeatedly
            # selecting an earlier ping and leaving the action unfinished.
            and CoreReachabilityTimeline._outage_intervals(
                states=service_states,
                measurement_started_at=measurement_started_at,
                measurement_completed_at=sample.probed_at,
            ) is not None
        ]
        if not after_required_coverage:
            return None
        restored = after_required_coverage[0]
        selected = tuple(
            sample
            for sample in samples
            if baseline.probed_at <= sample.probed_at <= restored.probed_at
        )
        if not selected or selected[-1] != restored:
            return None
        if any(
            not reachability_probe_gap_is_covered(
                left,
                right,
                max_probe_gap_s=max_probe_gap_s,
            )
            for left, right in zip(selected, selected[1:])
        ):
            return None
        return selected, restored.probed_at

    @staticmethod
    def _outage_intervals(
        *,
        states: tuple[ReachabilityServiceState, ...],
        measurement_started_at: float,
        measurement_completed_at: float,
    ) -> tuple[ReachabilityOutageInterval, ...] | None:
        """Select command downtime from the same NET states used by all views."""
        before = tuple(state for state in states if state.observed_at <= measurement_started_at)
        if not before:
            return None
        open_at = measurement_started_at if not before[-1].reachable else 0.0
        intervals: list[ReachabilityOutageInterval] = []
        for state in states:
            if state.observed_at <= measurement_started_at:
                continue
            if state.observed_at > measurement_completed_at:
                break
            if not state.reachable and open_at <= 0.0:
                open_at = state.observed_at
            elif state.reachable and open_at > 0.0:
                intervals.append(
                    ReachabilityOutageInterval(
                        started_at=open_at,
                        restored_at=state.observed_at,
                    )
                )
                open_at = 0.0
        if open_at:
            return None  # A host reply cannot close an unverified modem loss.
        return tuple(intervals)


__all__ = (
    "CoreReachabilityTimeline",
    "DEFAULT_REACHABILITY_MAX_PROBE_GAP_S",
    "DEFAULT_REACHABILITY_MAX_SAMPLES",
    "DEFAULT_REACHABILITY_RETENTION_S",
)
