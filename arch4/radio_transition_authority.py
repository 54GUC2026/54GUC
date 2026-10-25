"""Canonical radio-transition attribution above the Modem Live boundary."""

from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import math
import threading
from typing import Any

from arch4.accepted_modem_generation import AcceptedModemGeneration
from arch4.core_contracts.accepted_current_facts import (
    CoreAcceptedCurrentFacts,
)
from arch4.command_transactions import (
    CommandAuthority,
    CommandLifecycle,
    CommandTerminalStatus,
)
from arch4.controller_frames import RadioSelectionFrame, RadioTransitionFrame
from arch4.core.signal_computer import classify_cell_structural_change
from arch4.radio_target_contract import (
    accepted_serving_band_at_matrix_boundary,
    build_radio_target_facts_at_accepted_matrix_boundary,
    match_radio_target,
)


LOWER_CONTROLLER_INITIAL = "lower_controller_initial"
LOWER_CONTROLLER = "lower_controller"
LOWER_CONTROLLER_DURING_COMMAND = "lower_controller_during_54guc_command"
GUC_COMMAND = "54guc_command"
ATTRIBUTION_UNAVAILABLE = "attribution_unavailable"

_SELECTION_COMMAND_LEVERS = frozenset(
    {"BAND_SELECTION", "BAND_LOCK", "CELL_LOCK", "PBAND", "NR_BAND_LOCK", "RADIO_MODE"}
)


def radio_selection_from_accepted_current_facts(
    current_facts: CoreAcceptedCurrentFacts,
) -> RadioSelectionFrame:
    if not isinstance(current_facts, CoreAcceptedCurrentFacts):
        raise TypeError("radio_transition_requires_accepted_current_facts")

    def exact(line: str, field: str):
        return current_facts.exact(f"{line}.{field}")

    serving_band_available, serving_band, reason = accepted_serving_band_at_matrix_boundary(current_facts)

    def exact_int(line: str, field: str) -> int:
        fact = exact(line, field)
        if fact.disposition != "available":
            return 0
        value = fact.value.to_mapping()["value"]
        if type(value) is not int:
            raise TypeError(
                f"radio_transition_integer_cell_invalid:{line}.{field}"
            )
        return value

    def exact_band_tuple(field: str) -> tuple[int, ...]:
        fact = exact("radio", field)
        if fact.disposition != "available":
            return ()
        value = fact.value.to_mapping()["value"]
        if (
            type(value) is not list
            or any(type(item) is not int or item <= 0 for item in value)
            or value != sorted(set(value))
        ):
            raise TypeError(
                f"radio_transition_band_list_invalid:radio.{field}"
            )
        return tuple(value)

    def optional_int(line: str, field: str) -> int | None:
        if exact(line, field).disposition != "available":
            return None
        return exact_int(line, field)

    network_fact = exact("radio", "network_type")
    network_type = (network_fact.value.to_mapping()["value"]
                    if network_fact.disposition == "available" else "")
    if type(network_type) is not str:
        raise TypeError("radio_transition_network_type_invalid")
    nr_active = network_type == "NR NSA (5G)"

    signature_fact = exact("radio", "ca_band_signature")
    if signature_fact.disposition == "available":
        signature_value = signature_fact.value.to_mapping()["value"]
        if type(signature_value) is not str:
            raise TypeError("radio_transition_ca_signature_invalid")
        ca_signature = signature_value
    else:
        ca_signature = ""
    return RadioSelectionFrame(
        serving_band_available=serving_band_available,
        serving_band_unavailable_reason=reason,
        primary_band=serving_band,
        pci=optional_int("rf", "pci"),
        earfcn=exact_int("rf", "earfcn_dl"),
        selected_band_set=exact_band_tuple("selected_band_set"),
        configured_band_pool=exact_band_tuple("configured_band_pool"),
        active_band_set=exact_band_tuple("active_band_set"),
        ca_signature=ca_signature,
        cell_id=optional_int("rf", "cell_id"),
        network_type=network_type,
        nr_band=optional_int("radio", "nr_band") if nr_active else None,
        nr_pci=optional_int("radio", "nr_pci") if nr_active else None,
        nr_arfcn=optional_int("radio", "nr_arfcn") if nr_active else None,
        nr_cell_id=optional_int("radio", "nr_cell_id") if nr_active else None,
    )


_PHYSICAL_SELECTION_FIELDS = (
    ("primary_band", "band"), ("pci", "pci"), ("earfcn", "earfcn"),
    ("cell_id", "cell_id"), ("network_type", "network_type"),
    ("nr_band", "nr_band"), ("nr_pci", "nr_pci"),
    ("nr_arfcn", "nr_arfcn"), ("nr_cell_id", "nr_cell_id"),
)


def _stable_selection_key(selection: RadioSelectionFrame) -> tuple[Any, ...]:
    # Optional active CA composition and its evidence are deliberately
    # excluded. The configured pool remains explicit command context; it does
    # not reveal which carriers are active.
    return (
        *(getattr(selection, field) for field, _ in _PHYSICAL_SELECTION_FIELDS),
        tuple(selection.selected_band_set),
        tuple(selection.configured_band_pool),
    )


def _transition_id(
    *,
    scope: str,
    epoch: str,
    generation: int,
    source: str,
    before: RadioSelectionFrame,
    after: RadioSelectionFrame,
    command_root_id: str,
) -> str:
    material = {
        "scope": scope,
        "epoch": epoch,
        "generation": generation,
        "source": source,
        "before": asdict(before),
        "after": asdict(after),
        "command_root_id": command_root_id,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]


def _selection_change_kind(
    before: RadioSelectionFrame,
    after: RadioSelectionFrame,
) -> str:
    changed = [kind for field, kind in _PHYSICAL_SELECTION_FIELDS
               if getattr(before, field) != getattr(after, field)]
    if before.selected_band_set != after.selected_band_set:
        changed.append("selected_band_pool")
    if before.configured_band_pool != after.configured_band_pool:
        changed.append("configured_band_pool")
    return "+".join(changed)


class RadioTransitionAuthority:
    """Own attribution and the persistent source of the current selection."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._namespace: tuple[str, str] = ("", "")
        self._previous_selection: RadioSelectionFrame | None = None
        self._current_frame: RadioTransitionFrame | None = None
        self._accepted_radio_outage = False
        self._last_restart_recovery_root_id = ""
        self._previous_reported_session_uptime_s: float | None = None
        self._last_reported_uptime_source_sequence = 0

    def observe_accepted(
        self,
        *,
        accepted: AcceptedModemGeneration,
        current_facts: CoreAcceptedCurrentFacts,
        command_authority: CommandAuthority,
    ) -> RadioTransitionFrame:
        if not isinstance(accepted, AcceptedModemGeneration):
            raise TypeError("radio_transition_requires_accepted_modem_generation")
        if not isinstance(command_authority, CommandAuthority):
            raise TypeError("radio_transition_requires_command_authority")
        try:
            return self._observe_accepted_valid(
                accepted=accepted,
                current_facts=current_facts,
                command_authority=command_authority,
            )
        except Exception as exc:
            with self._lock:
                held = self._previous_selection or RadioSelectionFrame()
            return RadioTransitionFrame(
                matrix_identity=accepted.raw.matrix_identity,
                attribution_state="quarantined",
                attribution_available=False,
                attribution_error=(
                    f"{type(exc).__name__}:{exc}"
                ),
                source=ATTRIBUTION_UNAVAILABLE,
                source_generation=accepted.source_sequence,
                fresh=False,
                structural_fresh=False,
                lower_controller_learning_opportunity=False,
                before=held,
                after=held,
            )

    def _observe_accepted_valid(
        self,
        *,
        accepted: AcceptedModemGeneration,
        current_facts: CoreAcceptedCurrentFacts,
        command_authority: CommandAuthority,
    ) -> RadioTransitionFrame:
        if not isinstance(accepted, AcceptedModemGeneration):
            raise TypeError("radio_transition_requires_accepted_modem_generation")
        if not isinstance(command_authority, CommandAuthority):
            raise TypeError("radio_transition_requires_command_authority")
        if (
            not isinstance(current_facts, CoreAcceptedCurrentFacts)
            or current_facts.matrix_identity != accepted.raw.matrix_identity
        ):
            raise TypeError("radio_transition_requires_current_facts")

        sequence = accepted.source_sequence
        if type(sequence) is not int:
            raise TypeError("radio_transition_generation_type_invalid")
        if sequence <= 0:
            raise ValueError("radio_transition_generation_missing")
        namespace = (
            accepted.raw.session_scope_key,
            accepted.raw.source_epoch,
        )
        if not all(namespace):
            raise ValueError("radio_transition_namespace_incomplete")
        selection = radio_selection_from_accepted_current_facts(
            current_facts
        )
        radio_target_facts = build_radio_target_facts_at_accepted_matrix_boundary(
            current_facts
        )
        reported_uptime = self._reported_session_uptime(current_facts)

        with self._lock:
            if namespace != self._namespace:
                self._namespace = namespace
                self._previous_selection = None
                self._current_frame = None
                self._accepted_radio_outage = False
                self._last_restart_recovery_root_id = ""
                self._previous_reported_session_uptime_s = None
                self._last_reported_uptime_source_sequence = 0

            reported_uptime_reset = False
            if reported_uptime is not None:
                uptime_s, uptime_sequence = reported_uptime
                if uptime_sequence > self._last_reported_uptime_source_sequence:
                    previous_uptime = self._previous_reported_session_uptime_s
                    reported_uptime_reset = bool(
                        previous_uptime is not None
                        and uptime_s + 0.001 < previous_uptime
                    )
                    self._previous_reported_session_uptime_s = uptime_s
                    self._last_reported_uptime_source_sequence = uptime_sequence
            if reported_uptime_reset:
                self._accepted_radio_outage = True

            restart_recovery_root_id = (
                radio_target_facts.restart_recovery_root_id
                if radio_target_facts.restart_recovery_observed
                else ""
            )
            new_restart_recovery = bool(
                restart_recovery_root_id
                and restart_recovery_root_id
                != self._last_restart_recovery_root_id
            )
            if restart_recovery_root_id:
                self._last_restart_recovery_root_id = restart_recovery_root_id
            if self._previous_selection is not None and not radio_target_facts.connected:
                self._accepted_radio_outage = True

            if not selection.serving_band_available:
                held = self._previous_selection or RadioSelectionFrame()
                return RadioTransitionFrame(
                    matrix_identity=accepted.raw.matrix_identity,
                    attribution_state="unavailable",
                    attribution_available=False,
                    attribution_error=str(
                        selection.serving_band_unavailable_reason
                        or "modem_live_serving_band_not_observed"
                    ),
                    source=ATTRIBUTION_UNAVAILABLE,
                    source_generation=sequence,
                    fresh=False,
                    structural_fresh=False,
                    lower_controller_learning_opportunity=False,
                    before=held,
                    after=held,
                )

            before = self._previous_selection or RadioSelectionFrame()
            initial = self._previous_selection is None
            radio_recovery = bool(
                not initial
                and radio_target_facts.connected
                and (self._accepted_radio_outage or new_restart_recovery)
            )
            if radio_target_facts.connected and selection.serving_band_available:
                self._accepted_radio_outage = False
            stable_changed = bool(
                not initial
                and _stable_selection_key(before) != _stable_selection_key(selection)
            )
            change_kind = _selection_change_kind(before, selection)
            structural_fresh = bool(not initial and any(
                getattr(before, field) != getattr(selection, field)
                for field, _ in _PHYSICAL_SELECTION_FIELDS
            ))
            exact_command = self._exact_command_for_generation(
                accepted=accepted,
                current_facts=current_facts,
                command_authority=command_authority,
            )
            fresh = bool(
                initial
                or stable_changed
                or exact_command
                or radio_recovery
            )

            if not fresh and self._current_frame is not None:
                frame = replace(
                    self._current_frame,
                    matrix_identity=accepted.raw.matrix_identity,
                    source_generation=sequence,
                    fresh=False,
                    structural_fresh=False,
                    after=selection,
                )
                self._previous_selection = selection
                self._current_frame = frame
                return frame

            active = command_authority.active_lifecycle()
            command_root_id = ""
            command_id = ""
            command_origin = ""
            command_lever = ""
            command_target_generation = 0
            if exact_command is not None:
                key = exact_command[0]
                command_target_generation = exact_command[1]
                source = GUC_COMMAND
                command_root_id = key.root_id
                command_id = key.command_id
                command_origin = key.source
                command_lever = key.lever
            elif active is not None and fresh:
                source = LOWER_CONTROLLER_DURING_COMMAND
                command_root_id = active.key.root_id
                command_id = active.key.command_id
                command_origin = active.key.source
                command_lever = active.key.lever
            else:
                source = LOWER_CONTROLLER_INITIAL if initial else LOWER_CONTROLLER

            if initial and not change_kind:
                change_kind = "initial_selection"
            if exact_command is not None and not change_kind:
                change_kind = "command_target_confirmed"
            if radio_recovery:
                change_parts = [part for part in change_kind.split("+") if part]
                if "radio_recovery" not in change_parts:
                    change_parts.append("radio_recovery")
                change_kind = "+".join(change_parts)
            change_class, service_impact = ("", False)
            if structural_fresh:
                def observed_bool(path: str) -> bool:
                    fact = current_facts.exact(path)
                    return (fact.disposition == "available"
                            and fact.value.to_mapping()["value"] is True)

                change_class, service_impact = classify_cell_structural_change(
                    band_changed=before.primary_band != selection.primary_band,
                    pci_changed=structural_fresh,
                    earfcn_changed=before.earfcn != selection.earfcn,
                    radio_connected=radio_target_facts.connected,
                    has_traffic=observed_bool("traffic.has_traffic"),
                    ca_active=observed_bool("radio.ca_active"),
                )
            transition_id = _transition_id(
                scope=namespace[0],
                epoch=namespace[1],
                generation=sequence,
                source=source,
                before=before,
                after=selection,
                command_root_id=command_root_id,
            )
            frame = RadioTransitionFrame(
                matrix_identity=accepted.raw.matrix_identity,
                attribution_state="valid",
                attribution_available=True,
                attribution_error="",
                source=source,
                episode_id=transition_id,
                transition_id=transition_id,
                source_generation=sequence,
                episode_started_generation=sequence,
                fresh=True,
                structural_fresh=structural_fresh,
                lower_controller_learning_opportunity=bool(
                    source
                    in {
                        LOWER_CONTROLLER_INITIAL,
                        LOWER_CONTROLLER,
                        LOWER_CONTROLLER_DURING_COMMAND,
                    }
                    and not (
                        radio_recovery
                        and not stable_changed
                        and not structural_fresh
                    )
                ),
                change_kind=change_kind,
                change_class=change_class,
                service_impact=service_impact,
                command_root_id=command_root_id,
                command_id=command_id,
                command_origin=command_origin,
                command_lever=command_lever,
                command_target_generation=command_target_generation,
                before=before,
                after=selection,
            )
            self._previous_selection = selection
            self._current_frame = frame
            return frame

    @staticmethod
    def _reported_session_uptime(
        current_facts: CoreAcceptedCurrentFacts,
    ) -> tuple[float, int] | None:
        uptime_fact = current_facts.exact("traffic.session_uptime_s")
        source_fact = current_facts.exact("traffic.session_uptime_source")
        if (
            uptime_fact.disposition != "available"
            or source_fact.disposition != "available"
            or uptime_fact.source_sequence != source_fact.source_sequence
        ):
            return None
        source = source_fact.value.to_mapping()["value"]
        if source != "reported":
            return None
        uptime = uptime_fact.value.to_mapping()["value"]
        if (
            isinstance(uptime, bool)
            or type(uptime) not in {int, float}
            or not math.isfinite(float(uptime))
        ):
            raise TypeError("radio_transition_reported_uptime_invalid")
        parsed = float(uptime)
        if parsed <= 0.0:
            return None
        return parsed, uptime_fact.source_sequence

    @staticmethod
    def _exact_command_for_generation(
        *,
        accepted: AcceptedModemGeneration,
        current_facts: CoreAcceptedCurrentFacts,
        command_authority: CommandAuthority,
    ) -> tuple[Any, int] | None:
        sequence = accepted.source_sequence
        scope = accepted.raw.session_scope_key
        candidates: list[tuple[Any, int]] = []

        active: CommandLifecycle | None = command_authority.active_lifecycle()
        if (
            active is not None
            and active.key.lever in _SELECTION_COMMAND_LEVERS
            and active.key.matrix_identity.session_scope_key == scope
            and active.target_generation == sequence
        ):
            candidates.append((active.key, active.target_generation))

        for terminal in command_authority.terminal_receipts():
            if (
                terminal.terminal_status is CommandTerminalStatus.SETTLED
                and terminal.action_key.lever in _SELECTION_COMMAND_LEVERS
                and terminal.action_key.matrix_identity.session_scope_key
                == scope
                and terminal.target_generation <= sequence
                <= terminal.settle_generation
            ):
                candidates.append(
                    (terminal.action_key, terminal.target_generation)
                )

        by_root = {candidate[0].root_id: candidate for candidate in candidates}
        if len(by_root) > 1:
            raise RuntimeError("radio_transition_multiple_command_roots_for_generation")
        if not by_root:
            return None
        key, target_generation = next(iter(by_root.values()))
        target = match_radio_target(
            target=key.target,
            facts=build_radio_target_facts_at_accepted_matrix_boundary(
                current_facts
            ),
            before_source_generation=key.matrix_identity.matrix_sequence,
            expected_root_id=key.root_id,
        )
        if not target.matched:
            raise RuntimeError(
                "command_authority_target_generation_mismatch:"
                f"root={key.root_id}:reason={target.reason}"
            )
        return key, target_generation


__all__ = [
    "ATTRIBUTION_UNAVAILABLE",
    "GUC_COMMAND",
    "LOWER_CONTROLLER",
    "LOWER_CONTROLLER_DURING_COMMAND",
    "LOWER_CONTROLLER_INITIAL",
    "RadioTransitionAuthority",
    "radio_selection_from_accepted_current_facts",
]
