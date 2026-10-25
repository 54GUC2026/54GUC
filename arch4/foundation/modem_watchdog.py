"""Foundation owner for one verified Internet-emergency recovery sequence."""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any

from arch4.atomic_io import atomic_write_text
from arch4.core.command_context import require_core_command_ingress_context
from arch4.core.command_gateway import require_core_command_gateway
from arch4.core.command_request_builder import (
    build_core_command_request,
    build_core_command_target,
)
from arch4.core.lever_action_adapter import lever_action_authority
from arch4.core_contracts.clients import CoreClientRole
from arch4.core_contracts.commands import CoreCommandOrigin
from arch4.core_contracts.emergency_reachability import (
    CoreEmergencyReachabilityView,
    EmergencyReachabilityDisposition,
    BACKGROUND_CHECK_AFTER_S, PREPARE_RECOVERY_AFTER_S, TAKEOVER_AFTER_S,
)
from arch4.core_contracts.runtime import CoreRuntimePort
from arch4.runtime_identity import parse_runtime_session_scope_key, runtime_physical_device_changed


FOUNDATION_MODEM_WATCHDOG_OWNER = "54guc.foundation.modem_watchdog"
STATE_SCHEMA = "FoundationInternetEmergencyState.v3"
VERIFICATION_INTERVAL_S = 2.5
POST_RESTART_HOLD_S = 3600.0


def _initial_state() -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "owner": FOUNDATION_MODEM_WATCHDOG_OWNER,
        "identity_code": "",
        "physical_scope": "",
        "incident_id": "",
        "phase": "armed",
        "rescue_deadline_at": 0.0,
        "last_verification_at": 0.0,
        "restart_dispatched_at": 0.0,
        "restart_hold_until": 0.0,
        "active_request_id": "",
        "active_root_id": "",
        "active_physical_root_id": "",
        "terminal_reason": "",
    }


class FoundationModemWatchdog:
    """Own recovery policy; consume only Core-confirmed emergency truth."""

    def __init__(
        self,
        core: CoreRuntimePort,
        *,
        persistence_dir: str | Path | None = None,
    ) -> None:
        if core is None:
            raise TypeError("foundation_watchdog_core_port_required")
        self._core = core
        self._lock = RLock()
        self._path = (
            Path(persistence_dir).resolve() / "internet_emergency_state.json"
            if persistence_dir is not None
            else None
        )
        self._state = self._load_state()

    def service_once(self, *, now: float) -> dict[str, Any]:
        ts = float(now)
        if ts <= 0.0:
            raise ValueError("foundation_watchdog_positive_timestamp_required")
        view = self._core.current_emergency_reachability_view(now=ts)
        if not isinstance(view, CoreEmergencyReachabilityView):
            raise TypeError("foundation_watchdog_core_emergency_view_required")
        with self._lock:
            self._bind_identity(view.identity_code, physical_scope=view.session_scope_key)
            self._restore_core_hold(now=ts)
            hold_until = float(self._state["restart_hold_until"])
            if hold_until > ts:
                return self._surface(
                    view,
                    status="recovery_hold",
                    message="Restart was attempted; recovery is observing only.",
                )
            if hold_until > 0.0:
                old_incident = str(self._state["incident_id"] or "")
                if (
                    view.disposition is not EmergencyReachabilityDisposition.NORMAL
                    and view.incident_id == old_incident
                ):
                    view = self._core.restart_emergency_reachability_verification(
                        incident_id=old_incident,
                        now=ts,
                    )
                self._clear_core_hold(old_incident)
                self._reset_action_state(keep_identity=True)

            if view.disposition is EmergencyReachabilityDisposition.NORMAL:
                recovered = self._state["phase"] != "armed"
                self._reset_action_state(keep_identity=True)
                return self._surface(
                    view,
                    status="recovered" if recovered else "armed",
                    message=(
                        "Internet reachability recovered."
                        if recovered
                        else "Internet emergency recovery is armed."
                    ),
                )

            device = self._core.current_device_recovery_state().to_mapping()
            if device.get("active") is True:
                self._close_core_rescue(view.incident_id)
                self._state["phase"] = "device_recovery"
                self._save_state()
                return self._surface(
                    view,
                    status="device_recovery",
                    message="The device is already recovering; CGF is observing.",
                    device_recovery=device,
                )

            if self._state["active_request_id"]:
                self._observe_restart(now=ts)
                if self._state["phase"] == "restart_rejected":
                    return self._surface(
                        view,
                        status="restart_rejected",
                        message=(
                            "Core rejected the recovery request; no modem "
                            "restart occurred."
                        ),
                    )
                if self._state["phase"] == "pending_physical_dispatch":
                    return self._surface(
                        view,
                        status="restart_pending",
                        message=(
                            "Restart request is waiting for exact modem "
                            "dispatch evidence."
                        ),
                    )
                return self._surface(
                    view,
                    status=(
                        "recovery_hold"
                        if self._state["restart_hold_until"] > ts
                        else "restarting"
                    ),
                    message=(
                        "Modem restart was dispatched; CGF will not repeat it."
                    ),
                )

            elapsed = max(0.0, ts - view.triggered_at)
            self._state["incident_id"] = view.incident_id
            if elapsed < BACKGROUND_CHECK_AFTER_S:
                self._state["phase"] = "settling"
                self._save_state()
                return self._surface(view, status="settling",
                                     message="Waiting briefly for Internet service to return.")

            if self._verification_due(ts):
                view = self._core.verify_emergency_reachability(now=ts)
                self._state["last_verification_at"] = ts
                self._save_state()
                if view.disposition is EmergencyReachabilityDisposition.NORMAL:
                    self._reset_action_state(keep_identity=True)
                    return self._surface(
                        view,
                        status="recovered",
                        message="Internet reachability recovered during verification.",
                    )

            if elapsed < TAKEOVER_AFTER_S:
                preparing = elapsed >= PREPARE_RECOVERY_AFTER_S
                self._state["phase"] = "preparing" if preparing else "checking"
                if preparing:
                    self._core.action_source_state_authority.set_emergency_recovery_hold(
                        incident_id=view.incident_id,
                        until_ts=view.triggered_at + TAKEOVER_AFTER_S + 30.0,
                        reason="preparing_emergency_band_rescue", observed_at=ts)
                self._save_state()
                return self._surface(view, status=self._state["phase"],
                                     message="Checking recovery in the background; monitoring continues.")

            if view.disposition is EmergencyReachabilityDisposition.VERIFYING:
                # Missing verification withdraws permission through Core's
                # live check; it must not erase an already used rescue slot.
                self._state["phase"] = "verifying"
                self._state["incident_id"] = view.incident_id
                self._save_state()
                return self._surface(
                    view,
                    status="verifying",
                    message="Possible outage; independent targets are being checked.",
                )

            if not self._state["rescue_deadline_at"]:
                if not self._core.command_lifecycle.prepare_emergency_band_rescue(
                    incident_id=view.incident_id, now=ts
                ):
                    self._state["phase"] = "waiting_for_command"
                    self._save_state()
                    return self._surface(view, status="waiting_for_command",
                                         message="Recovery is waiting for the current device request to finish.")
                self._clear_core_hold(view.incident_id)
                self._state.update(
                    incident_id=view.incident_id,
                    phase="rescue_window",
                    rescue_deadline_at=ts + self._core.current_recovery_timing().rescue_window_s,
                )
                self._save_state()
                self._open_core_rescue(view=view, now=ts)
                return self._surface(
                    view,
                    status="rescue_window",
                    message="Outage confirmed; Intelligence may restore all allowed bands.",
                )
            if ts < float(self._state["rescue_deadline_at"]):
                self._core.command_lifecycle.rearm_undispatched_emergency_rescue(
                    incident_id=view.incident_id, now=ts)
                return self._surface(
                    view,
                    status="rescue_window",
                    message="Outage confirmed; waiting for recovery after band rescue.",
                )

            self._close_core_rescue(view.incident_id)
            restart_submitted = self._submit_restart(view=view, now=ts)
            if not restart_submitted:
                return self._surface(
                    view,
                    status="restart_rejected",
                    message=(
                        "Core rejected the recovery request; no modem "
                        "restart occurred."
                    ),
                )
            return self._surface(
                view,
                status="restart_pending",
                message=(
                    "Core accepted the restart request; waiting for exact "
                    "modem dispatch evidence."
                ),
            )

    def _verification_due(self, now: float) -> bool:
        last = float(self._state["last_verification_at"])
        return last <= 0.0 or now - last >= VERIFICATION_INTERVAL_S

    def _bind_identity(self, identity_code: str, *, physical_scope: str) -> None:
        identity = str(identity_code or "").strip()
        current = str(self._state["identity_code"] or "")
        prior_scope = str(self._state["physical_scope"] or "")
        if not identity or (identity == current and physical_scope == prior_scope):
            return
        same_device = not prior_scope or physical_scope == prior_scope
        if not same_device:
            same_device = not runtime_physical_device_changed(
                parse_runtime_session_scope_key(prior_scope),
                parse_runtime_session_scope_key(physical_scope))
        if same_device:
            # A changed firmware hash cannot grant another restart attempt.
            # A v2 file lacked physical proof: conservatively keep its hold
            # while attaching the first newly confirmed physical namespace.
            self._state["identity_code"] = identity
            self._state["physical_scope"] = physical_scope
            self._save_state()
            return
        self._clear_core_hold(str(self._state["incident_id"] or ""))
        self._state = _initial_state()
        self._state["identity_code"] = identity
        self._state["physical_scope"] = physical_scope
        self._save_state()

    def _submit_restart(
        self,
        *,
        view: CoreEmergencyReachabilityView,
        now: float,
    ) -> bool:
        context = require_core_command_ingress_context(self._core)
        if (
            context.matrix_identity.session_scope_key != view.session_scope_key
            or context.identity_code != view.identity_code
        ):
            raise RuntimeError("foundation_watchdog_reachability_scope_mismatch")
        request_id = f"foundation-internet-recovery:{view.incident_id}"
        request = build_core_command_request(
            context,
            request_id=request_id,
            correlation_id=request_id,
            client_id="foundation.internet_emergency",
            client_role=CoreClientRole.OPERATOR_AUTOMATION,
            origin=CoreCommandOrigin.OPERATOR_AUTOMATION,
            command_kind="RESTART",
            target=build_core_command_target(
                "RESTART",
                {"emergency_fast": False},
            ),
            parameters={
                "automation_source": "watchdog",
                "intent": "modem_watchdog_recovery",
                "reason": "statistically confirmed Internet outage",
                "objective_id": request_id,
                "action_parameters": view.as_dict(),
            },
            capability_reference=lever_action_authority("RESTART").capability_key,
            created_at=now,
            expires_at=now + 120.0,
            confirmed=True,
        )
        result = dict(
            require_core_command_gateway(self._core).submit(request, now=now)
        )
        root_id = str(result.get("root_id") or "")
        if not root_id:
            raise RuntimeError("foundation_watchdog_command_root_unavailable")
        submitted = result.get("submitted") is True
        physical_root_id = str(
            result.get("physical_action_root_id") or ""
        ).strip()
        if submitted and not physical_root_id:
            raise RuntimeError(
                "foundation_watchdog_physical_action_root_unavailable"
            )
        if not submitted:
            self._state.update(
                phase="restart_rejected",
                active_request_id=request_id,
                active_root_id=root_id,
                active_physical_root_id="",
                restart_dispatched_at=0.0,
                restart_hold_until=0.0,
                terminal_reason=str(
                    result.get("reason") or "restart_not_submitted"
                ),
            )
            self._save_state()
            return False
        self._state.update(
            phase="pending_physical_dispatch",
            active_request_id=request_id,
            active_root_id=root_id,
            active_physical_root_id=physical_root_id,
            restart_dispatched_at=0.0,
            restart_hold_until=0.0,
            terminal_reason="",
        )
        self._save_state()
        return True

    def _observe_restart(self, *, now: float) -> None:
        if (
            self._state["terminal_reason"]
            and self._state["phase"] == "restart_rejected"
        ):
            return
        status = self._core.command_lifecycle.physical_status(
            request_id=str(self._state["active_request_id"]),
            command_root_id=str(self._state["active_root_id"]),
            physical_action_root_id=str(
                self._state["active_physical_root_id"]
            ),
        )
        if status.disposition == "pending_physical_dispatch":
            self._state["phase"] = "pending_physical_dispatch"
            self._save_state()
            return
        if status.disposition in {
            "physical_rejected",
            "terminal_without_physical_receipt",
        }:
            self._state.update(
                phase="restart_rejected",
                restart_dispatched_at=0.0,
                restart_hold_until=0.0,
                terminal_reason=(
                    status.receipt.reason
                    if status.receipt is not None
                    else status.terminal_reason
                ),
            )
            self._save_state()
            return
        receipt = status.receipt
        if receipt is None or not receipt.accepted:
            raise RuntimeError(
                "foundation_watchdog_physical_restart_receipt_required"
            )
        dispatched_at = receipt.execution_started_at
        expected_until = dispatched_at + POST_RESTART_HOLD_S
        existing_dispatch = float(self._state["restart_dispatched_at"])
        existing_until = float(self._state["restart_hold_until"])
        if existing_dispatch not in {0.0, dispatched_at}:
            raise RuntimeError(
                "foundation_watchdog_restart_dispatch_time_mismatch"
            )
        if existing_until not in {0.0, expected_until}:
            raise RuntimeError("foundation_watchdog_restart_hold_mismatch")
        self._state.update(
            phase="recovery_hold",
            restart_dispatched_at=dispatched_at,
            restart_hold_until=expected_until,
            terminal_reason="",
        )
        self._set_core_hold(now=now)
        self._save_state()

    def _set_core_hold(self, *, now: float) -> None:
        incident = str(self._state["incident_id"] or "")
        until = float(self._state["restart_hold_until"])
        if not incident or until <= now:
            return
        started = float(self._state["restart_dispatched_at"] or now)
        self._core.action_source_state_authority.set_emergency_recovery_hold(
            until_ts=until,
            incident_id=incident,
            reason="post_restart_observation",
            observed_at=min(now, started),
        )

    def _open_core_rescue(
        self,
        *,
        view: CoreEmergencyReachabilityView,
        now: float,
    ) -> None:
        deadline = float(self._state["rescue_deadline_at"]) + 60.0
        if deadline <= now:
            raise RuntimeError("foundation_rescue_deadline_required")
        self._core.action_source_state_authority.open_emergency_band_rescue(
            incident_id=view.incident_id,
            deadline_at=deadline,
            observed_at=now,
        )

    def _close_core_rescue(self, incident_id: str) -> None:
        if incident_id:
            self._core.action_source_state_authority.close_emergency_band_rescue(
                incident_id=incident_id,
            )

    def _restore_core_hold(self, *, now: float) -> None:
        if float(self._state["restart_hold_until"]) > now:
            self._set_core_hold(now=now)

    def _clear_core_hold(self, incident_id: str) -> None:
        if incident_id:
            self._core.action_source_state_authority.clear_emergency_recovery_hold(
                incident_id=incident_id
            )

    def _reset_action_state(self, *, keep_identity: bool) -> None:
        identity = str(self._state["identity_code"] or "") if keep_identity else ""
        physical_scope = str(self._state["physical_scope"] or "") if keep_identity else ""
        incident = str(self._state["incident_id"] or "")
        self._close_core_rescue(incident)
        self._clear_core_hold(incident)
        self._state = _initial_state()
        self._state["identity_code"] = identity
        self._state["physical_scope"] = physical_scope
        self._save_state()

    def _surface(
        self,
        view: CoreEmergencyReachabilityView,
        *,
        status: str,
        message: str,
        device_recovery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "enabled": True,
            "status": status,
            "last_action": str(self._state["phase"]),
            "message": message,
            "outage_s": max(0.0, view.observed_at - view.triggered_at)
            if view.triggered_at
            else 0.0,
            "threshold_s": 0.0,
            "run_interval_s": VERIFICATION_INTERVAL_S,
            "action_lockout_s": POST_RESTART_HOLD_S,
            "drives_radio_actions": True,
            "source": FOUNDATION_MODEM_WATCHDOG_OWNER,
            "ts": view.observed_at,
            "reachability": view.as_dict(),
            "command_request_id": str(self._state["active_request_id"]),
            "command_root_id": str(self._state["active_root_id"]),
            "command_terminal_reason": str(self._state["terminal_reason"]),
            "last_recovered_at": 0.0,
            "rescue_deadline_at": float(self._state["rescue_deadline_at"]),
            "restart_hold_until": float(self._state["restart_hold_until"]),
            "device_recovery": dict(device_recovery or {}),
        }

    def _load_state(self) -> dict[str, Any]:
        if self._path is None or not self._path.exists():
            return _initial_state()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("schema") == "FoundationInternetEmergencyState.v2":
                # Explicit persisted-state upgrade; no new recovery allowance.
                raw = {**raw, "schema": STATE_SCHEMA, "physical_scope": ""}
            if not isinstance(raw, dict) or set(raw) != set(_initial_state()):
                raise ValueError("foundation_emergency_state_shape_invalid")
            if raw.get("schema") != STATE_SCHEMA or raw.get("owner") != (
                FOUNDATION_MODEM_WATCHDOG_OWNER
            ):
                raise ValueError("foundation_emergency_state_contract_invalid")
            for name in (
                "rescue_deadline_at",
                "last_verification_at",
                "restart_dispatched_at",
                "restart_hold_until",
            ):
                raw[name] = max(0.0, float(raw[name]))
            return raw
        except Exception:
            safe = _initial_state()
            safe.update(
                identity_code="*",
                phase="recovery_state_invalid",
                terminal_reason="recovery_state_invalid",
            )
            return safe

    def _save_state(self) -> None:
        if self._path is None:
            return
        atomic_write_text(
            self._path,
            json.dumps(self._state, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )


__all__ = (
    "FOUNDATION_MODEM_WATCHDOG_OWNER",
    "POST_RESTART_HOLD_S",
    "STATE_SCHEMA",
    "VERIFICATION_INTERVAL_S",
    "FoundationModemWatchdog",
)
