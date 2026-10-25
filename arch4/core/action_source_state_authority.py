"""Mechanical timing protection for radio actions. This module owns the user
anti-double-click delay, the automation stability hold after an accepted radio change,
and the optional cell-lock lease. Optimization and Intelligence policy stay outside
Core.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import threading
import time
from typing import Any, Mapping, Sequence

from arch4.action_origin import (
    ActionOriginKind,
    HUMAN_OPERATOR_ORIGIN_NAMES,
    is_human_operator_origin,
    require_action_origin_name,
)
from arch4.core.band_action_policy import (
    EMERGENCY_RADIO_STABILITY_BYPASS_INTENTS,
    OPERATOR_BAND_COOLDOWN_S,
    RADIO_STABILITY_HOLD_S,
    BandActionPolicyDecision,
    is_band_control_action,
    normalized_lever,
    normalized_name,
)
from arch4.controller_frames import RadioSelectionFrame, RadioTransitionFrame
from arch4.core_contracts.command_targets import CoreBandSetTarget, CoreRadioModeTarget, core_command_target_from_payload, core_command_target_payload


OWNER = "arch4.core.action_source_state_authority"
SCHEMA = "CoreActionProtectionState.v6"
CHECKPOINT_SCHEMA = "CoreActionProtectionCheckpoint.v6"

_RADIO_LEVERS = frozenset(
    {"BAND_SELECTION", "BAND_LOCK", "PBAND", "CELL_LOCK", "NR_BAND_LOCK", "RADIO_MODE"}
)
_STATE_KEYS = frozenset(
    {
        "band_action_policy",
        "cell_lock_lease",
        "emergency_band_rescue",
        "emergency_recovery_hold",
        "applied_event_ids",
        "radio_mode_lease",
    }
)


def band_action_target_signature(
    lever: Any,
    params: Mapping[str, Any] | None,
) -> str:
    """Return the Core-owned stable target label for a band command receipt."""

    lever_name = normalized_lever(lever)
    row = dict(params or {})
    if lever_name == "PBAND":
        try:
            pband = int(row.get("pband", 0) or 0)
        except (TypeError, ValueError):
            pband = 0
        values = list(row.get("ca_pool") or row.get("dl_bands") or ())
        if pband > 0:
            values.append(pband)
        pool_signature = "+".join(f"B{band}" for band in _bands(values))
        return (
            f"B{pband}@pband:{pool_signature}"
            if pband > 0 and pool_signature
            else pool_signature
        )
    if lever_name == "CELL_LOCK":
        return (
            "CELL_LOCK:"
            f"pci={int(row.get('pci', 0) or 0)}:"
            f"earfcn={int(row.get('earfcn', 0) or 0)}"
        )
    values = row.get("resolved_target_bands", row.get("bands", ()))
    return "+".join(f"B{band}" for band in _bands(values))


class ActionSourceStateAuthority:
    """Receipt-aware owner for mechanical action protection clocks only."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = _initial_state()

    def note_operator_band_physical_acceptance(
        self,
        *,
        lever: str,
        params: Mapping[str, Any] | None,
        origin: str,
        target_signature: str,
        observed_at: float,
        receipt_id: str,
    ) -> bool:
        """Advance only the human anti-double-click clock from one write."""

        with self._lock:
            return self._note_operator_band_physical_acceptance(
                lever=lever,
                params=params,
                origin=origin,
                target_signature=target_signature,
                observed_at=observed_at,
                receipt_id=receipt_id,
            )

    def _note_operator_band_physical_acceptance(
        self,
        *,
        lever: str,
        params: Mapping[str, Any] | None,
        origin: str,
        target_signature: str,
        observed_at: float,
        receipt_id: str,
    ) -> bool:
        """Apply one human receipt while holding the owner lock."""

        if not is_band_control_action(lever, params):
            return False
        origin_name = normalized_name(origin)
        if not is_human_operator_origin(origin_name):
            raise ValueError("core_action_protection_human_origin_required")
        receipt_key = str(receipt_id or "").strip()
        if not receipt_key:
            raise ValueError("core_action_protection_receipt_id_required")
        if self._receipt_seen(receipt_key):
            return False
        now = float(observed_at)
        if now <= 0.0:
            raise ValueError("core_action_protection_positive_time_required")
        policy = deepcopy(self._state["band_action_policy"])
        current = dict(policy["operator_cooldown"])
        if now + 0.001 < _float(current.get("started_ts")):
            raise ValueError("core_action_protection_receipt_time_rollback")
        policy["operator_cooldown"] = {
            "until_ts": now + OPERATOR_BAND_COOLDOWN_S,
            "target_signature": str(target_signature or ""),
            "started_ts": now,
            "receipt_id": receipt_key,
            "origin": origin_name,
            "lever": normalized_lever(lever),
        }
        self._state["band_action_policy"] = policy
        self._state["radio_mode_lease"] = _initial_radio_mode_lease()
        self._record_receipt(receipt_key)
        return True

    def note_accepted_radio_transition(
        self,
        transition: RadioTransitionFrame,
        *,
        observed_at: float,
    ) -> bool:
        """Start the only routine hold from one accepted physical transition."""

        with self._lock:
            return self._note_accepted_radio_transition(
                transition,
                observed_at=observed_at,
            )

    def _note_accepted_radio_transition(
        self,
        transition: RadioTransitionFrame,
        *,
        observed_at: float,
    ) -> bool:
        """Apply one Matrix transition while holding the owner lock."""

        if not isinstance(transition, RadioTransitionFrame):
            raise TypeError("core_radio_stability_typed_transition_required")
        if not transition.attribution_available or not transition.fresh:
            return False
        if str(transition.source or "") == "lower_controller_initial":
            return False
        transition_id = str(transition.transition_id or "").strip()
        if not transition_id:
            raise ValueError("core_radio_stability_transition_id_required")
        event_key = f"radio_transition:{transition_id}"
        if self._receipt_seen(event_key):
            return False
        before = transition.before
        after = transition.after
        if not isinstance(before, RadioSelectionFrame) or not isinstance(
            after,
            RadioSelectionFrame,
        ):
            raise TypeError("core_radio_stability_selection_frame_required")
        pband_changed = bool(
            before.primary_band > 0
            and after.primary_band > 0
            and before.primary_band != after.primary_band
        )
        pool_changed = bool(
            before.configured_band_pool
            and after.configured_band_pool
            and before.configured_band_pool != after.configured_band_pool
        )
        transition_parts = frozenset(
            part
            for part in str(transition.change_kind or "").split("+")
            if part
        )
        radio_recovered = "radio_recovery" in transition_parts
        if not pband_changed and not pool_changed and not radio_recovered:
            return False
        now = float(observed_at)
        if now <= 0.0:
            raise ValueError("core_radio_stability_positive_time_required")
        policy = deepcopy(self._state["band_action_policy"])
        current = dict(policy["radio_stability_hold"])
        if now + 0.001 < _float(current.get("started_ts")):
            raise ValueError("core_radio_stability_transition_time_rollback")
        change_parts = []
        if pband_changed:
            change_parts.append("pband")
        if pool_changed:
            change_parts.append("configured_band_pool")
        if radio_recovered:
            change_parts.append("radio_recovery")
        policy["radio_stability_hold"] = {
            "until_ts": now + RADIO_STABILITY_HOLD_S,
            "started_ts": now,
            "transition_id": transition_id,
            "source": str(transition.source or ""),
            "change_kind": "+".join(change_parts),
            "source_generation": int(transition.source_generation),
            "before_signature": _radio_selection_signature(before),
            "after_signature": _radio_selection_signature(after),
        }
        self._state["band_action_policy"] = policy
        self._record_receipt(event_key)
        return True

    def band_action_policy_decision(
        self,
        *,
        lever: str,
        params: Mapping[str, Any] | None,
        origin: str,
        intent: str = "",
        now: float,
    ) -> BandActionPolicyDecision:
        """Return the only mechanical admission decision for a band action."""

        ts = float(now)
        lever_name = normalized_lever(lever)
        origin_name = require_action_origin_name(origin)
        intent_name = str(intent or "").strip()
        status = self.band_action_policy_status(now=ts)
        operator = is_human_operator_origin(origin_name)
        emergency = bool(
            origin_name == ActionOriginKind.FAULT_RECOVERY.value
            or intent_name in EMERGENCY_RADIO_STABILITY_BYPASS_INTENTS
        )
        stability_bypass = bool(
            operator or emergency or origin_name == ActionOriginKind.SCHEDULE.value
            or (lever_name == "RADIO_MODE" and intent_name == "nr_service_lte_fallback")
        )
        governed = is_band_control_action(lever_name, params)
        operator_active = bool(status["operator_cooldown_active"])
        stability_active = bool(status["radio_stability_hold_active"])
        recovery_active = bool(status["emergency_recovery_hold_active"])
        emergency_rescue = intent_name in EMERGENCY_RADIO_STABILITY_BYPASS_INTENTS
        if not governed:
            allowed = True
            reason = "not_band_control_action"
        elif operator and operator_active:
            allowed = False
            reason = "operator_band_cooldown_active"
        elif lever_name == "RADIO_MODE" and not operator and operator_active:
            allowed = False
            reason = "operator_band_cooldown_active"
        elif not operator and recovery_active:
            allowed = False
            reason = "emergency_recovery_hold_active"
        elif emergency_rescue:
            allowed, reason = self._emergency_band_rescue_eligibility(
                now=ts,
            )
        elif (not operator and status["emergency_band_rescue_incident_id"]
              and ts < status["emergency_band_rescue_deadline_at"]):
            allowed = False
            reason = "emergency_band_rescue_in_progress"
        elif not stability_bypass and stability_active:
            allowed = False
            reason = "radio_stability_hold_active"
        else:
            allowed = True
            reason = "band_action_policy_admitted"
        return BandActionPolicyDecision(
            lever=lever_name,
            origin=origin_name,
            allowed=allowed,
            reason=reason,
            operator_cooldown_active=operator_active,
            operator_cooldown_remaining_s=float(
                status["operator_cooldown_remaining_s"]
            ),
            radio_stability_hold_active=stability_active,
            radio_stability_hold_remaining_s=float(
                status["radio_stability_hold_remaining_s"]
            ),
            radio_stability_bypass=stability_bypass,
            evaluated_at_ts=ts,
        )

    def band_action_policy_status(
        self,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = float(time.time() if now is None else now)
        with self._lock:
            policy = deepcopy(self._state["band_action_policy"])
        operator = dict(policy["operator_cooldown"])
        stability = dict(policy["radio_stability_hold"])
        operator_until = _float(operator.get("until_ts"))
        stability_until = _float(stability.get("until_ts"))
        return {
            "owner": OWNER,
            "operator_cooldown_active": ts < operator_until,
            "operator_cooldown_until_ts": operator_until,
            "operator_cooldown_remaining_s": max(0.0, operator_until - ts),
            "operator_target_signature": str(
                operator.get("target_signature") or ""
            ),
            "operator_started_ts": _float(operator.get("started_ts")),
            "operator_receipt_id": str(operator.get("receipt_id") or ""),
            "operator_origin": str(operator.get("origin") or ""),
            "operator_lever": str(operator.get("lever") or ""),
            "radio_stability_hold_active": ts < stability_until,
            "radio_stability_hold_until_ts": stability_until,
            "radio_stability_hold_remaining_s": max(
                0.0,
                stability_until - ts,
            ),
            "radio_stability_started_ts": _float(stability.get("started_ts")),
            "radio_stability_transition_id": str(
                stability.get("transition_id") or ""
            ),
            "radio_stability_source": str(stability.get("source") or ""),
            "radio_stability_change_kind": str(
                stability.get("change_kind") or ""
            ),
            "radio_stability_source_generation": int(
                stability.get("source_generation", 0) or 0
            ),
            "radio_stability_before_signature": str(
                stability.get("before_signature") or ""
            ),
            "radio_stability_after_signature": str(
                stability.get("after_signature") or ""
            ),
            **self.emergency_recovery_hold_status(now=ts),
            **self.emergency_band_rescue_status(now=ts),
        }

    def open_emergency_band_rescue(
        self,
        *,
        incident_id: str,
        deadline_at: float,
        observed_at: float,
    ) -> None:
        """Open one idempotent band-rescue command slot for an incident."""

        incident = str(incident_id or "").strip()
        now = float(observed_at)
        deadline = float(deadline_at)
        if not incident or now <= 0.0 or deadline <= now:
            raise ValueError("core_emergency_band_rescue_invalid")
        with self._lock:
            current = dict(self._state["emergency_band_rescue"])
            if str(current.get("incident_id") or "") == incident:
                current["deadline_at"] = max(
                    float(current.get("deadline_at", 0.0) or 0.0),
                    deadline,
                )
                self._state["emergency_band_rescue"] = current
                return
            self._state["emergency_band_rescue"] = {
                "incident_id": incident,
                "opened_at": now,
                "deadline_at": deadline,
                "selected_command_id": "",
            }

    def close_emergency_band_rescue(self, *, incident_id: str) -> bool:
        incident = str(incident_id or "").strip()
        with self._lock:
            current = dict(self._state["emergency_band_rescue"])
            if str(current.get("incident_id") or "") != incident:
                return False
            self._state["emergency_band_rescue"] = (
                _initial_emergency_band_rescue()
            )
            return True

    def emergency_band_rescue_status(
        self,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = float(time.time() if now is None else now)
        with self._lock:
            rescue = dict(self._state["emergency_band_rescue"])
        deadline = _float(rescue.get("deadline_at"))
        selected = str(rescue.get("selected_command_id") or "")
        return {
            "emergency_band_rescue_active": bool(ts < deadline and not selected),
            "emergency_band_rescue_opened_at": _float(rescue.get("opened_at")),
            "emergency_band_rescue_deadline_at": deadline,
            "emergency_band_rescue_incident_id": str(
                rescue.get("incident_id") or ""
            ),
            "emergency_band_rescue_selected_command_id": selected,
        }

    def rearm_undispatched_emergency_rescue(self, *, incident_id: str, command_id: str, observed_at: float) -> bool:
        """Called by command lifecycle only after no-write and fresh-contact proof."""
        with self._lock:
            rescue = dict(self._state["emergency_band_rescue"])
            if (not command_id or rescue["incident_id"] != incident_id
                    or rescue["selected_command_id"] != command_id
                    or not rescue["opened_at"] < observed_at < rescue["deadline_at"]):
                return False
            rescue.update(selected_command_id="", opened_at=float(observed_at))
            self._state["emergency_band_rescue"] = rescue
            return True

    def _emergency_band_rescue_eligibility(
        self,
        *,
        now: float,
    ) -> tuple[bool, str]:
        with self._lock:
            rescue = dict(self._state["emergency_band_rescue"])
            deadline = _float(rescue.get("deadline_at"))
            selected = str(rescue.get("selected_command_id") or "")
            if now >= deadline or not str(rescue.get("incident_id") or ""):
                return False, "emergency_band_rescue_not_open"
            if selected:
                return False, "emergency_band_rescue_already_used"
            return True, "emergency_band_rescue_eligible"

    def reserve_emergency_band_rescue(
        self,
        *,
        command_id: str,
        now: float,
    ) -> tuple[bool, str]:
        """Reserve the one rescue only after canonical Core admission."""

        command = str(command_id or "").strip()
        if not command:
            return False, "emergency_band_rescue_command_id_required"
        ts = float(now)
        with self._lock:
            rescue = dict(self._state["emergency_band_rescue"])
            deadline = _float(rescue.get("deadline_at"))
            selected = str(rescue.get("selected_command_id") or "")
            if ts >= deadline or not str(rescue.get("incident_id") or ""):
                return False, "emergency_band_rescue_not_open"
            if selected and selected != command:
                return False, "emergency_band_rescue_already_used"
            if not selected:
                rescue["selected_command_id"] = command
                self._state["emergency_band_rescue"] = rescue
                self._record_receipt("emergency-band-rescue-reserved:" + command)
            return True, "emergency_band_rescue_admitted"

    def owns_emergency_band_rescue(self, command_id: str) -> bool:
        """Keep reservation proof after Watchdog closes its recovered incident."""
        with self._lock:
            return bool(command_id) and (
                self._state["emergency_band_rescue"]["selected_command_id"] == command_id
                or self._receipt_seen("emergency-band-rescue-reserved:" + command_id))

    def set_emergency_recovery_hold(
        self,
        *,
        until_ts: float,
        incident_id: str,
        reason: str,
        observed_at: float,
    ) -> None:
        """Block autonomous radio actions during one recovery hold."""

        now = float(observed_at)
        until = float(until_ts)
        incident = str(incident_id or "").strip()
        why = str(reason or "").strip()
        if now <= 0.0 or until <= now or not incident or not why:
            raise ValueError("core_emergency_recovery_hold_invalid")
        with self._lock:
            self._state["emergency_recovery_hold"] = {
                "until_ts": until,
                "started_ts": now,
                "incident_id": incident,
                "reason": why,
            }

    def clear_emergency_recovery_hold(
        self,
        *,
        incident_id: str,
    ) -> bool:
        incident = str(incident_id or "").strip()
        with self._lock:
            current = dict(self._state["emergency_recovery_hold"])
            if str(current.get("incident_id") or "") != incident:
                return False
            self._state["emergency_recovery_hold"] = (
                _initial_emergency_recovery_hold()
            )
            return True

    def emergency_recovery_hold_status(
        self,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = float(time.time() if now is None else now)
        with self._lock:
            hold = dict(self._state["emergency_recovery_hold"])
        until = _float(hold.get("until_ts"))
        return {
            "emergency_recovery_hold_active": ts < until,
            "emergency_recovery_hold_until_ts": until,
            "emergency_recovery_hold_remaining_s": max(0.0, until - ts),
            "emergency_recovery_incident_id": str(
                hold.get("incident_id") or ""
            ),
            "emergency_recovery_reason": str(hold.get("reason") or ""),
        }

    def note_cell_lock_result(
        self,
        *,
        params: Mapping[str, Any],
        success: bool,
        observed_at: float,
        receipt_id: str,
        origin_kind: str,
    ) -> bool:
        """Update the timed cell-lock lease from the action receipt."""

        external_receipt_id = str(receipt_id or "").strip()
        if not external_receipt_id:
            raise ValueError("core_cell_lock_receipt_id_required")
        receipt_key = f"cell_lock:{external_receipt_id}"
        if self._receipt_seen(receipt_key):
            return False
        row = dict(params or {})
        now = float(observed_at)
        if now <= 0.0:
            raise ValueError("core_cell_lock_positive_time_required")
        lease = dict(self._state["cell_lock_lease"])
        unlock = bool(row.get("unlock", False))
        auto_release = bool(row.get("auto_unlock", False))

        if unlock:
            if success:
                self._state["cell_lock_lease"] = {
                    **_initial_cell_lock_lease(),
                    "last_release_receipt_id": external_receipt_id,
                    "last_result_ts": now,
                }
            elif auto_release and lease.get("active") is True:
                lease.update(
                    {
                        "inflight": False,
                        "selected_command_id": "",
                        "release_dispatched_ts": 0.0,
                        "due_ts": now + 60.0,
                        "last_failure_receipt_id": external_receipt_id,
                        "last_result_ts": now,
                    }
                )
                self._state["cell_lock_lease"] = lease
            self._record_receipt(receipt_key)
            return True

        if not is_human_operator_origin(origin_kind):
            self._record_receipt(receipt_key)
            return False
        if not success:
            self._state["cell_lock_lease"] = {
                **_initial_cell_lock_lease(),
                "last_failure_receipt_id": external_receipt_id,
                "last_result_ts": now,
            }
            self._record_receipt(receipt_key)
            return True
        duration_s = min(
            7 * 24 * 3600.0,
            max(
                0.0,
                _float(
                    row.get("auto_unlock_after_s")
                    or row.get("lock_duration_s")
                ),
            ),
        )
        next_lease = _initial_cell_lock_lease()
        next_lease.update(
            {
                "last_lock_receipt_id": external_receipt_id,
                "last_result_ts": now,
            }
        )
        if duration_s > 0.0:
            next_lease.update(
                {
                    "active": True,
                    "due_ts": now + duration_s,
                    "duration_s": duration_s,
                    "pci": int(row.get("pci", 0) or 0),
                    "earfcn": int(row.get("earfcn", 0) or 0),
                }
            )
        self._state["cell_lock_lease"] = next_lease
        self._record_receipt(receipt_key)
        return True

    def note_cell_lock_release_dispatched(
        self,
        *,
        command_id: str,
        due_ts: float,
        observed_at: float,
    ) -> bool:
        """Mark a due lease release before the mutation owner receives it."""

        lease = dict(self._state["cell_lock_lease"])
        if lease.get("active") is not True:
            raise RuntimeError("core_cell_lock_lease_missing")
        expected_due = _float(lease.get("due_ts"))
        if expected_due <= 0.0 or abs(expected_due - float(due_ts)) > 0.001:
            raise RuntimeError("core_cell_lock_lease_due_mismatch")
        now = float(observed_at)
        if now + 0.001 < expected_due:
            raise RuntimeError("core_cell_lock_lease_not_due")
        selected_id = str(command_id or "").strip()
        if not selected_id:
            raise ValueError("core_cell_lock_selected_command_id_required")
        if lease.get("inflight") is True:
            if str(lease.get("selected_command_id") or "") == selected_id:
                return False
            raise RuntimeError("core_cell_lock_release_already_inflight")
        lease.update(
            {
                "inflight": True,
                "selected_command_id": selected_id,
                "release_dispatched_ts": now,
            }
        )
        self._state["cell_lock_lease"] = lease
        return True

    def cell_lock_lease_status(
        self,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = float(time.time() if now is None else now)
        lease = dict(self._state["cell_lock_lease"])
        due_ts = _float(lease.get("due_ts"))
        return {
            **lease,
            "active": bool(lease.get("active") is True),
            "due": bool(
                lease.get("active") is True
                and due_ts > 0.0
                and ts >= due_ts
            ),
            "remaining_s": max(0.0, due_ts - ts) if due_ts > 0.0 else 0.0,
        }

    def note_radio_mode_dispatch(self, *, target: CoreRadioModeTarget, root_id: str,
                                 identity_key: str, observed_at: float) -> bool:
        """Retain one dispatched automatic command, never infer its success."""
        if not isinstance(target, CoreRadioModeTarget) or not root_id or not identity_key:
            raise ValueError("core_radio_mode_dispatch_identity_required")
        stamp = float(observed_at)
        if type(observed_at) not in {int, float} or not 0 < stamp < float("inf"):
            raise ValueError("core_radio_mode_dispatch_time_invalid")
        with self._lock:
            event = "radio-mode-dispatch:" + root_id
            if self._receipt_seen(event):
                return False
            self._state["radio_mode_lease"] = dict(state="pending", root_id=root_id,
                identity_key=identity_key, target=core_command_target_payload(target),
                dispatched_at=stamp, settled_at=0.0, retry_at=0.0, terminal_reason="")
            self._record_receipt(event)
            return True

    def note_radio_mode_terminal(self, *, root_id: str, settled: bool,
                                 observed_at: float, reason: str) -> bool:
        """Latch the existing Command Authority terminal into the same lease."""
        if type(settled) is not bool:
            raise TypeError("core_radio_mode_terminal_boolean_required")
        with self._lock:
            lease = dict(self._state["radio_mode_lease"])
            if lease["root_id"] != root_id or lease["state"] != "pending":
                return False
            stamp = float(observed_at)
            if type(observed_at) not in {int, float} or not lease["dispatched_at"] <= stamp < float("inf") or not reason:
                raise ValueError("core_radio_mode_terminal_invalid")
            target = core_command_target_from_payload(lease["target"])
            lease.update(state=("holding_lte" if target.mode == "lte_only" else "trial") if settled else "suspended",
                settled_at=stamp, terminal_reason=reason,
                retry_at=stamp + target.retry_delay_s if settled and target.mode == "lte_only" else 0.0)
            self._state["radio_mode_lease"] = lease
            return True

    def radio_mode_lease_status(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state["radio_mode_lease"])

    def cancel_radio_mode_lease(self, *, receipt_id: str) -> bool:
        if not receipt_id:
            raise ValueError("core_radio_mode_override_receipt_required")
        with self._lock:
            if self._state["radio_mode_lease"]["state"] == "inactive":
                return False
            self._state["radio_mode_lease"] = _initial_radio_mode_lease()
            self._record_receipt("radio-mode-manual-override:" + receipt_id)
            return True

    def reconcile_radio_mode_configuration(self, *, identity_key: str, observation: Mapping[str, Any] | None,
                                           acquired_at: float, own_rescue_target: CoreBandSetTarget | None = None,
                                           own_rescue_command_id: str = "") -> bool:
        """Cancel a lease after a different device or a fresh settings override.

        This compares receipts/configuration only. It never decides RF health,
        retry strategy or whether NR has recovered.
        """
        with self._lock:
            lease = self._state["radio_mode_lease"]
            if lease["state"] == "inactive":
                return False
            changed_device = identity_key != lease["identity_key"]
            if not changed_device:
                if lease["state"] not in {"holding_lte", "trial"} or observation is None or acquired_at <= lease["settled_at"]:
                    return False
                target = core_command_target_from_payload(lease["target"])
                expected = target.configuration()
                from arch4.core_contracts.command_targets import validate_radio_mode_observation
                actual = validate_radio_mode_observation(observation)
                if actual == expected:
                    return False
                # Core supplies an accepted native rescue plus its matching fresh
                # readback. Only the reserved rescue's LTE pool/selection may
                # change; all mode, NR, device/SIM/profile guards remain exact.
                if (isinstance(own_rescue_target, CoreBandSetTarget)
                        and own_rescue_command_id
                        and self.owns_emergency_band_rescue(own_rescue_command_id)):
                    recovered = {**expected, "lte_bands": list(own_rescue_target.bands),
                                 "band_selection_mode": "manual"}
                    selection = {}
                    if "lte_selection_mode" in recovered:
                        selection["lte_selection_mode"] = "manual"
                        recovered.update(selection)
                    nr_fields = {"nr_bands", "nr_selection_mode"}
                    nr_only_change = (actual != recovered and
                        {key: value for key, value in actual.items() if key not in nr_fields} ==
                        {key: value for key, value in recovered.items() if key not in nr_fields})
                    if actual == recovered or nr_only_change:
                        lease["target"] = core_command_target_payload(replace(target,
                            lte_bands=own_rescue_target.bands, band_selection_mode="manual", **selection))
                        if nr_only_change:
                            # Keep the saved intent, but never infer that a coupled
                            # native change was authorized to replace its NR settings.
                            lease.update(state="suspended", retry_at=0.0,
                                terminal_reason="nr_settings_changed_after_lte_rescue")
                        self._record_receipt("radio-mode-own-lte-rescue:" + own_rescue_command_id)
                        return False  # Retain the original retry clock and backoff.
            self._record_receipt("radio-mode-cancel:" + lease["root_id"])
            self._state["radio_mode_lease"] = _initial_radio_mode_lease()
            return True

    def checkpoint_payload(self) -> dict[str, Any]:
        with self._lock:
            content = {
                "schema": CHECKPOINT_SCHEMA,
                "owner": OWNER,
                "state": deepcopy(self._state),
            }
        return {**content, "checkpoint_id": _digest(content)}

    def restore_checkpoint(self, value: Mapping[str, Any] | None) -> bool:
        row = dict(value or {})
        if row.get("schema") not in {CHECKPOINT_SCHEMA, "CoreActionProtectionCheckpoint.v5"} or row.get("owner") != OWNER:
            raise ValueError("core_action_protection_checkpoint_contract_invalid")
        if set(row) != {"schema", "owner", "state", "checkpoint_id"}:
            raise ValueError("core_action_protection_checkpoint_shape_invalid")
        content = {key: item for key, item in row.items() if key != "checkpoint_id"}
        if str(row.get("checkpoint_id") or "") != _digest(content):
            raise ValueError("core_action_protection_checkpoint_checksum_invalid")
        state = row.get("state")
        if not isinstance(state, Mapping):
            raise ValueError("core_action_protection_checkpoint_state_invalid")
        restored = deepcopy(dict(state))
        if row["schema"] == "CoreActionProtectionCheckpoint.v5":
            if "radio_mode_lease" in restored:
                raise ValueError("core_prior_action_checkpoint_mode_lease_forbidden")
            restored["radio_mode_lease"] = _initial_radio_mode_lease()
        _validate_state(restored)
        restored["applied_event_ids"] = [
            str(item)
            for item in restored["applied_event_ids"]
            if str(item)
        ][-256:]
        with self._lock:
            self._state = restored
        return True

    def status(self, *, now: float | None = None) -> dict[str, Any]:
        ts = float(time.time() if now is None else now)
        with self._lock:
            return {
                "schema": SCHEMA,
                "owner": OWNER,
                "radio_mode_lease": self.radio_mode_lease_status(),
                "band_action_policy": self.band_action_policy_status(now=ts),
                "cell_lock_lease": self.cell_lock_lease_status(now=ts),
                "emergency_band_rescue": (
                    self.emergency_band_rescue_status(now=ts)
                ),
                "emergency_recovery_hold": (
                    self.emergency_recovery_hold_status(now=ts)
                ),
                "applied_event_count": len(self._state["applied_event_ids"]),
            }

    def _receipt_seen(self, receipt_id: str) -> bool:
        with self._lock:
            return receipt_id in set(self._state["applied_event_ids"])

    def _record_receipt(self, receipt_id: str) -> None:
        with self._lock:
            values = [
                str(value)
                for value in self._state["applied_event_ids"]
                if str(value)
            ]
            values.append(receipt_id)
            self._state["applied_event_ids"] = values[-256:]


def action_source_state_authority_for_engine(
    engine: Any,
) -> ActionSourceStateAuthority | None:
    value = getattr(engine, "action_source_state_authority", None)
    return value if isinstance(value, ActionSourceStateAuthority) else None


def require_action_source_state_authority(engine: Any) -> ActionSourceStateAuthority:
    authority = action_source_state_authority_for_engine(engine)
    if authority is None:
        raise RuntimeError("action_source_state_authority_missing")
    return authority


def _initial_operator_cooldown() -> dict[str, Any]:
    return {
        "until_ts": 0.0,
        "target_signature": "",
        "started_ts": 0.0,
        "receipt_id": "",
        "origin": "",
        "lever": "",
    }


def _initial_radio_stability_hold() -> dict[str, Any]:
    return {
        "until_ts": 0.0,
        "started_ts": 0.0,
        "transition_id": "",
        "source": "",
        "change_kind": "",
        "source_generation": 0,
        "before_signature": "",
        "after_signature": "",
    }


def _initial_band_action_policy() -> dict[str, Any]:
    return {
        "operator_cooldown": _initial_operator_cooldown(),
        "radio_stability_hold": _initial_radio_stability_hold(),
    }


def _initial_cell_lock_lease() -> dict[str, Any]:
    return {
        "active": False,
        "due_ts": 0.0,
        "duration_s": 0.0,
        "pci": 0,
        "earfcn": 0,
        "inflight": False,
        "selected_command_id": "",
        "release_dispatched_ts": 0.0,
        "last_lock_receipt_id": "",
        "last_release_receipt_id": "",
        "last_failure_receipt_id": "",
        "last_result_ts": 0.0,
    }


def _initial_emergency_recovery_hold() -> dict[str, Any]:
    return {
        "until_ts": 0.0,
        "started_ts": 0.0,
        "incident_id": "",
        "reason": "",
    }


def _initial_emergency_band_rescue() -> dict[str, Any]:
    return {
        "incident_id": "",
        "opened_at": 0.0,
        "deadline_at": 0.0,
        "selected_command_id": "",
    }


def _initial_state() -> dict[str, Any]:
    return {
        "band_action_policy": _initial_band_action_policy(),
        "cell_lock_lease": _initial_cell_lock_lease(),
        "emergency_band_rescue": _initial_emergency_band_rescue(),
        "emergency_recovery_hold": _initial_emergency_recovery_hold(),
        "applied_event_ids": [],
        "radio_mode_lease": _initial_radio_mode_lease(),
    }


def _initial_radio_mode_lease():
    return dict(state="inactive", root_id="", identity_key="", target=None,
                dispatched_at=0.0, settled_at=0.0, retry_at=0.0, terminal_reason="")


def _validate_state(state: Mapping[str, Any]) -> None:
    if set(state) != _STATE_KEYS:
        raise ValueError("core_action_protection_state_shape_invalid")
    mode = state.get("radio_mode_lease")
    if not isinstance(mode, Mapping) or set(mode) != set(_initial_radio_mode_lease()):
        raise ValueError("core_radio_mode_lease_shape_invalid")
    if mode["state"] == "inactive":
        if dict(mode) != _initial_radio_mode_lease():
            raise ValueError("core_radio_mode_inactive_lease_invalid")
    elif mode["state"] in {"pending", "holding_lte", "trial", "suspended"}:
        target = core_command_target_from_payload(mode["target"])
        if not isinstance(target, CoreRadioModeTarget) or not mode["root_id"] or not mode["identity_key"]:
            raise ValueError("core_radio_mode_lease_identity_invalid")
        for name in ("dispatched_at", "settled_at", "retry_at"):
            value = mode[name]
            if type(value) not in {int, float} or not 0 <= value < float("inf"):
                raise ValueError("core_radio_mode_lease_time_invalid")
        if mode["dispatched_at"] <= 0:
            raise ValueError("core_radio_mode_lease_dispatch_required")
        if mode["state"] == "pending":
            if mode["settled_at"] or mode["retry_at"] or mode["terminal_reason"]:
                raise ValueError("core_radio_mode_pending_lease_invalid")
        elif mode["settled_at"] < mode["dispatched_at"] or not mode["terminal_reason"]:
            raise ValueError("core_radio_mode_terminal_lease_invalid")
        if mode["state"] == "holding_lte":
            if target.mode != "lte_only" or mode["retry_at"] != mode["settled_at"] + target.retry_delay_s:
                raise ValueError("core_radio_mode_hold_lease_invalid")
        elif mode["retry_at"]:
            raise ValueError("core_radio_mode_unexpected_retry_time")
        if mode["state"] == "trial" and target.mode != "nsa_preferred":
            raise ValueError("core_radio_mode_trial_target_invalid")
    else:
        raise ValueError("core_radio_mode_lease_state_invalid")
    policy_value = state.get("band_action_policy")
    lease_value = state.get("cell_lock_lease")
    emergency_rescue_value = state.get("emergency_band_rescue")
    recovery_hold_value = state.get("emergency_recovery_hold")
    receipts = state.get("applied_event_ids")
    if not isinstance(policy_value, Mapping):
        raise ValueError("core_action_protection_band_policy_missing")
    if not isinstance(lease_value, Mapping):
        raise ValueError("core_action_protection_cell_lease_missing")
    if not isinstance(emergency_rescue_value, Mapping):
        raise ValueError("core_emergency_band_rescue_missing")
    if not isinstance(recovery_hold_value, Mapping):
        raise ValueError("core_emergency_recovery_hold_missing")
    if not isinstance(receipts, list) or any(not isinstance(item, str) for item in receipts):
        raise ValueError("core_action_protection_receipts_invalid")
    _validate_band_action_policy(dict(policy_value))
    _validate_cell_lock_lease(dict(lease_value))
    _validate_emergency_band_rescue(dict(emergency_rescue_value))
    _validate_emergency_recovery_hold(dict(recovery_hold_value))


def _validate_band_action_policy(policy: Mapping[str, Any]) -> None:
    if set(policy) != set(_initial_band_action_policy()):
        raise ValueError("core_action_protection_band_policy_shape_invalid")
    operator_value = policy.get("operator_cooldown")
    stability_value = policy.get("radio_stability_hold")
    if not isinstance(operator_value, Mapping):
        raise ValueError("core_action_protection_operator_cooldown_missing")
    if not isinstance(stability_value, Mapping):
        raise ValueError("core_action_protection_stability_hold_missing")
    operator = dict(operator_value)
    stability = dict(stability_value)
    if set(operator) != set(_initial_operator_cooldown()):
        raise ValueError("core_action_protection_operator_cooldown_shape_invalid")
    if set(stability) != set(_initial_radio_stability_hold()):
        raise ValueError("core_action_protection_stability_hold_shape_invalid")
    started = _float(operator.get("started_ts"))
    operator_until = _float(operator.get("until_ts"))
    receipt_id = str(operator.get("receipt_id") or "").strip()
    origin = normalized_name(operator.get("origin"))
    lever = normalized_lever(operator.get("lever"))
    if not receipt_id:
        if any((started, operator_until, origin, lever)):
            raise ValueError("core_action_protection_band_identity_missing")
    else:
        if origin not in HUMAN_OPERATOR_ORIGIN_NAMES:
            raise ValueError("core_action_protection_band_origin_invalid")
        if lever not in _RADIO_LEVERS:
            raise ValueError("core_action_protection_band_lever_invalid")
        if started <= 0.0:
            raise ValueError("core_action_protection_band_time_invalid")
        if abs(operator_until - started - OPERATOR_BAND_COOLDOWN_S) > 0.001:
            raise ValueError("core_action_protection_band_cooldown_invalid")
    stability_started = _float(stability.get("started_ts"))
    stability_until = _float(stability.get("until_ts"))
    transition_id = str(stability.get("transition_id") or "").strip()
    if not transition_id:
        if any(
            (
                stability_started,
                stability_until,
                stability.get("source"),
                stability.get("change_kind"),
                stability.get("source_generation"),
                stability.get("before_signature"),
                stability.get("after_signature"),
            )
        ):
            raise ValueError("core_radio_stability_identity_missing")
    else:
        if stability_started <= 0.0:
            raise ValueError("core_radio_stability_time_invalid")
        if abs(stability_until - stability_started - RADIO_STABILITY_HOLD_S) > 0.001:
            raise ValueError("core_radio_stability_deadline_invalid")
        if int(stability.get("source_generation", 0) or 0) <= 0:
            raise ValueError("core_radio_stability_generation_invalid")
        change_parts = tuple(
            part
            for part in str(stability.get("change_kind") or "").split("+")
            if part
        )
        if (
            not change_parts
            or len(change_parts) != len(set(change_parts))
            or not set(change_parts).issubset(
                {"pband", "configured_band_pool", "radio_recovery"}
            )
            or change_parts
            != tuple(
                part
                for part in (
                    "pband",
                    "configured_band_pool",
                    "radio_recovery",
                )
                if part in set(change_parts)
            )
        ):
            raise ValueError("core_radio_stability_change_kind_invalid")
        if not str(stability.get("before_signature") or "") or not str(
            stability.get("after_signature") or ""
        ):
            raise ValueError("core_radio_stability_signature_missing")


def _validate_cell_lock_lease(lease: Mapping[str, Any]) -> None:
    if set(lease) != set(_initial_cell_lock_lease()):
        raise ValueError("core_action_protection_cell_lease_shape_invalid")
    if lease.get("active") is True:
        if _float(lease.get("due_ts")) <= 0.0 or _float(lease.get("duration_s")) <= 0.0:
            raise ValueError("core_action_protection_cell_lease_time_invalid")
        if not str(lease.get("last_lock_receipt_id") or ""):
            raise ValueError("core_action_protection_cell_lease_receipt_missing")


def _validate_emergency_band_rescue(emergency_rescue: Mapping[str, Any]) -> None:
    if set(emergency_rescue) != set(_initial_emergency_band_rescue()):
        raise ValueError("core_emergency_band_rescue_shape_invalid")
    rescue_incident = str(emergency_rescue.get("incident_id") or "").strip()
    rescue_opened = _float(emergency_rescue.get("opened_at"))
    rescue_deadline = _float(emergency_rescue.get("deadline_at"))
    rescue_command = str(
        emergency_rescue.get("selected_command_id") or ""
    ).strip()
    if rescue_incident:
        if rescue_opened <= 0.0 or rescue_deadline <= rescue_opened:
            raise ValueError("core_emergency_band_rescue_state_invalid")
    elif any((rescue_opened, rescue_deadline, rescue_command)):
        raise ValueError("core_emergency_band_rescue_identity_invalid")


def _validate_emergency_recovery_hold(recovery_hold: Mapping[str, Any]) -> None:
    if set(recovery_hold) != set(_initial_emergency_recovery_hold()):
        raise ValueError("core_emergency_recovery_hold_shape_invalid")
    recovery_until = _float(recovery_hold.get("until_ts"))
    recovery_started = _float(recovery_hold.get("started_ts"))
    recovery_incident = str(recovery_hold.get("incident_id") or "").strip()
    recovery_reason = str(recovery_hold.get("reason") or "").strip()
    if recovery_until > 0.0:
        if (
            recovery_started <= 0.0
            or recovery_until <= recovery_started
            or not recovery_incident
            or not recovery_reason
        ):
            raise ValueError("core_emergency_recovery_hold_state_invalid")
    elif any((recovery_started, recovery_incident, recovery_reason)):
        raise ValueError("core_emergency_recovery_hold_identity_invalid")


def _bands(values: Sequence[Any]) -> tuple[int, ...]:
    result: set[int] = set()
    for value in values or ():
        try:
            band = int(value)
        except (TypeError, ValueError):
            continue
        if band > 0:
            result.add(band)
    return tuple(sorted(result))


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value or {}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _radio_selection_signature(value: RadioSelectionFrame) -> str:
    if not isinstance(value, RadioSelectionFrame):
        raise TypeError("core_radio_stability_selection_frame_required")
    pool = "+".join(f"B{band}" for band in value.configured_band_pool)
    return f"B{int(value.primary_band or 0)}|CA={pool or 'NA'}"


__all__ = (
    "ActionSourceStateAuthority",
    "CHECKPOINT_SCHEMA",
    "OWNER",
    "SCHEMA",
    "action_source_state_authority_for_engine",
    "band_action_target_signature",
    "require_action_source_state_authority",
)
