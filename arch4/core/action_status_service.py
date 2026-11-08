"""The only Core publisher for current action readiness and lock readback."""

from __future__ import annotations

from collections import deque
from typing import Any, Mapping

from arch4.command_transactions import (
    CommandAuthority,
    CommandLifecycle,
    CommandTerminalReceipt,
)
from arch4.core.action_reachability import CoreReachabilityTimeline
from arch4.core.action_runtime import radio_mutation_window_status
from arch4.core.action_source_state_authority import (
    require_action_source_state_authority,
)
from arch4.core_contracts.accepted_runtime_view import (
    CORE_ACTION_READBACK_TO_CAPABILITY,
    CoreAcceptedRuntimeView,
)
from arch4.runtime_boundary_contracts import RadioMutationWindow
from arch4.core_contracts.physical_commands import PhysicalCommandReceipt
from arch4.runtime_registers import RuntimeRegisterBank


OWNER = "54guc.core.action_status"
INTERNET_PAUSE_OWNER = "54guc.core.action_internet_pause"


def _reachability_enabled(candidate: Any) -> bool:
    completion = (
        candidate.completion
        if isinstance(candidate, CommandLifecycle)
        else candidate.transaction.cellular_completion
    )
    return bool(
        completion is not None
        and completion.reachability_measurement_enabled
    )


def _candidate_created_at(candidate: Any) -> float:
    transaction = (
        candidate.lease.transaction
        if isinstance(candidate, CommandLifecycle)
        else candidate.transaction
    )
    return float(transaction.created_ts)


def _latest_radio_action(authority: CommandAuthority) -> Any | None:
    """Return the newest cellular action, even when it has no pause measurement. Choosing
    only pause-enabled actions left an older band action visible after a newer command
    finished.
    """

    candidates: list[Any] = []
    active = authority.active_lifecycle()
    if active is not None:
        candidates.append(active)
    candidates.extend(authority.terminal_receipts())
    return max(candidates, key=_candidate_created_at) if candidates else None


def _reconcile_planned_actions(core: Any, view: CoreAcceptedRuntimeView) -> None:
    """Close every terminal action, including an older unmeasurable pause."""

    authority = core.command_authority
    timeline = core.reachability_timeline
    if not isinstance(authority, CommandAuthority):
        raise TypeError("core_action_pause_command_authority_required")
    if not isinstance(timeline, CoreReachabilityTimeline):
        raise TypeError("core_action_pause_reachability_timeline_required")
    active = authority.active_lifecycle()
    if (
        active is not None
        and _reachability_enabled(active)
        and active.key.matrix_identity.namespace == view.matrix_identity.namespace
    ):
        timeline.begin_planned_action(
            key=active.key,
            identity_code=view.identity_code,
            started_at=_candidate_created_at(active),
        )
    for terminal in authority.terminal_receipts():
        if (
            _reachability_enabled(terminal)
            and terminal.action_key.matrix_identity.namespace == view.matrix_identity.namespace
        ):
            timeline.end_planned_action(
                key=terminal.action_key,
                identity_code=view.identity_code,
                started_at=_candidate_created_at(terminal),
                ended_at=float(terminal.terminal_ts),
            )


def _internet_pause(core: Any, view: CoreAcceptedRuntimeView) -> dict[str, Any]:
    authority = getattr(core, "command_authority", None)
    timeline = getattr(core, "reachability_timeline", None)
    if not isinstance(authority, CommandAuthority):
        raise TypeError("core_action_pause_command_authority_required")
    if not isinstance(timeline, CoreReachabilityTimeline):
        raise TypeError("core_action_pause_reachability_timeline_required")
    candidate = _latest_radio_action(authority)
    if candidate is None:
        return {
            "schema": "CoreActionInternetPause.v1",
            "owner": INTERNET_PAUSE_OWNER,
            "action_id": "",
            "state": "idle",
            "value_ms": None,
        }
    lifecycle = candidate if isinstance(candidate, CommandLifecycle) else None
    terminal = (
        candidate if isinstance(candidate, CommandTerminalReceipt) else None
    )
    key = lifecycle.key if lifecycle is not None else terminal.action_key
    if key.matrix_identity.namespace != view.matrix_identity.namespace:
        return {
            "schema": "CoreActionInternetPause.v1",
            "owner": INTERNET_PAUSE_OWNER,
            "action_id": "",
            "state": "idle",
            "value_ms": None,
        }
    if not _reachability_enabled(candidate):
        return {
            "schema": "CoreActionInternetPause.v1",
            "owner": INTERNET_PAUSE_OWNER,
            "action_id": key.command_id,
            "state": "idle",
            "value_ms": None,
        }
    # A physical stage can restore reachability before Core has reached the
    # terminal command state. Publishing that early result made a later outage
    # invisible because action status cached it. Active always means measuring.
    if lifecycle is not None:
        return {
            "schema": "CoreActionInternetPause.v1",
            "owner": INTERNET_PAUSE_OWNER,
            "action_id": key.command_id,
            "state": "connecting",
            "value_ms": None,
        }
    payload = candidate.as_dict()
    receipts = payload.get("receipts")
    receipts = receipts if isinstance(receipts, Mapping) else {}
    raw_physical = receipts.get("modem")
    measured = None
    if isinstance(raw_physical, Mapping):
        physical = PhysicalCommandReceipt.from_mapping(raw_physical)
        measured = timeline.receipt_for_action(
            key=key,
            identity_code=view.identity_code,
            physical_receipt=physical,
            coverage_required_at=float(terminal.terminal_ts),
        )
    elif terminal is not None:
        started_at = float(terminal.transaction.created_ts)
        measured = timeline.receipt_for_interval(
            key=key,
            identity_code=view.identity_code,
            measurement_started_at=started_at,
            coverage_required_at=float(terminal.terminal_ts),
            physical_completed_at=float(terminal.terminal_ts),
        )
    total_outage_s = measured.total_outage_s if measured is not None else None
    return {
        "schema": "CoreActionInternetPause.v1",
        "owner": INTERNET_PAUSE_OWNER,
        "action_id": key.command_id,
        "state": "measured" if total_outage_s is not None else "connecting",
        "value_ms": (
            float(total_outage_s) * 1000.0
            if total_outage_s is not None
            else None
        ),
    }


def _current_accepted_view(core: Any) -> CoreAcceptedRuntimeView | None:
    outbox = getattr(core, "accepted_root_outbox", None)
    current = getattr(outbox, "current", None)
    if not callable(current):
        raise RuntimeError("core_action_status_accepted_root_unavailable")
    root = current()
    if root is None:
        return None
    view = getattr(root, "runtime_view", None)
    if not isinstance(view, CoreAcceptedRuntimeView):
        raise RuntimeError("core_action_status_accepted_view_invalid")
    return view


def _latest_accepted_readback(core: Any) -> dict[str, Any]:
    """Read the one action interpretation retained on the accepted root."""

    unavailable = {
        "band_locked": False,
        "band_locked_valid": False,
        "band_locked_reason": "core_accepted_view_unavailable",
        "cell_locked": False,
        "cell_locked_valid": False,
        "cell_locked_reason": "core_accepted_view_unavailable",
        "action_capabilities": {
            "schema": "CoreActionCapabilities.v1",
            "owner": OWNER,
            "source_sequence": 0,
            "ready": False,
            "actions": {
                key: {
                    "enabled": None,
                    "valid": False,
                    "reason": "core_accepted_view_unavailable",
                }
                for key, _capability_key in (
                    CORE_ACTION_READBACK_TO_CAPABILITY
                )
            },
        },
    }
    view = _current_accepted_view(core)
    if view is None:
        return unavailable
    return view.action_readback.to_mapping()


class CoreActionStatusService:
    """Publish action status from Core owners; NGUI only renders the result."""

    def __init__(self, core: Any) -> None:
        bank = getattr(core, "register_bank", None)
        if not isinstance(bank, RuntimeRegisterBank):
            raise TypeError("core_action_status_register_bank_required")
        if getattr(core, "command_authority", None) is None:
            raise TypeError("core_action_status_command_authority_required")
        require_action_source_state_authority(core)
        self._core = core
        self._bank = bank
        self._recorded_pause_actions: deque[str] = deque(maxlen=256)
        self._recorded_pause_action_set: set[str] = set()

    def _record_final_pause(self, value: Mapping[str, Any]) -> None:
        """Keep the final current-run pause measurement for this action."""

        action_id = str(value.get("action_id") or "")
        if value.get("state") != "measured" or not action_id:
            return
        if action_id in self._recorded_pause_action_set:
            return
        delivered = self._core.diagnostics.log_event(
            "action_internet_pause",
            transaction_id=action_id,
            event_owner=INTERNET_PAUSE_OWNER,
            operation="reachability_measurement_complete",
            measurement_state="measured",
            value_ms=value.get("value_ms"),
        )
        if delivered is False:
            return
        if len(self._recorded_pause_actions) == self._recorded_pause_actions.maxlen:
            evicted = self._recorded_pause_actions.popleft()
            self._recorded_pause_action_set.discard(evicted)
        self._recorded_pause_actions.append(action_id)
        self._recorded_pause_action_set.add(action_id)

    def publish(
        self,
        *,
        now: float,
        modem_health: Mapping[str, Any],
    ) -> dict[str, Any]:
        observed_at = float(now)
        if observed_at <= 0.0:
            raise ValueError("core_action_status_positive_timestamp_required")
        if not isinstance(modem_health, Mapping):
            raise TypeError("core_action_status_modem_health_mapping_required")

        window = RadioMutationWindow.from_mapping(
            radio_mutation_window_status(
                self._core,
                now=observed_at,
                modem_health=dict(modem_health),
            )
        )
        mutation = self._bank.write_mutation(
            value=window.as_dict(),
            source=OWNER,
            updated_at=observed_at,
            valid=True,
            stale=False,
            dirty=window.active,
            trust="transition" if window.active else "live",
            source_kind="derived",
            validity="dirty" if window.active else "valid",
            transition=window.active,
        )

        view = _current_accepted_view(self._core)
        if view is not None:
            _reconcile_planned_actions(self._core, view)
        internet_pause = (
            _internet_pause(self._core, view)
            if view is not None
            else {
                "schema": "CoreActionInternetPause.v1",
                "owner": INTERNET_PAUSE_OWNER,
                "action_id": "",
                "state": "idle",
                "value_ms": None,
            }
        )
        self._record_final_pause(internet_pause)
        auxiliary_value = {
            "owner": OWNER,
            "internet_pause": internet_pause,
            "manual_cell_lock_timer": require_action_source_state_authority(
                self._core
            ).cell_lock_lease_status(now=observed_at),
            **_latest_accepted_readback(self._core),
        }
        auxiliary = self._bank.write_action_auxiliary(
            value=auxiliary_value,
            source=OWNER,
            updated_at=observed_at,
            valid=True,
            stale=False,
            dirty=window.active,
            trust="transition" if window.active else "live",
            source_kind="derived",
            validity="dirty" if window.active else "valid",
            transition=window.active,
        )
        return {
            "owner": OWNER,
            "mutation": mutation.as_dict(),
            "action_auxiliary": auxiliary.as_dict(),
        }


__all__ = ("CoreActionStatusService", "OWNER")
