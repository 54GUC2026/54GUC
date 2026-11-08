from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping

from arch4.coerce import safe_bool


_TRUE_TEXT = {"1", "true", "yes", "on"}
_FALSE_TEXT = {"0", "false", "no", "off", ""}
LEARNING_GATE_SCHEMA = "learning_gate_publication.v1"
LEARNING_GATE_OWNER = "arch4.learning_gate_contract"
_MUTATION_WINDOW_CONTRACT_FIELDS = (
    "active",
    "learning_allowed",
    "action_allowed",
    "reason",
    "telemetry_trust",
)


def _valid_gate_bool(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return value in (0, 1)
    if isinstance(value, float):
        return value in (0.0, 1.0)
    if isinstance(value, str):
        return value.strip().lower() in (_TRUE_TEXT | _FALSE_TEXT)
    return False


def _malformed_bool_fields(surface: str, payload: Mapping[str, Any], fields: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        f"{surface}.{field}"
        for field in fields
        if field in payload and not _valid_gate_bool(payload.get(field))
    )


@dataclass(frozen=True)
class LearningGateDecision:
    """Canonical runtime learning admission contract."""

    learning_allowed: bool
    block_reason: str = ""
    block_detail: str = ""
    radio_mutation_window: Dict[str, Any] | None = None
    sample_stale: bool = False
    gate_input_complete: bool = True
    missing_inputs: tuple[str, ...] = ()
    malformed_inputs: tuple[str, ...] = ()

    @classmethod
    def from_sources(
        cls,
        *,
        radio_mutation_window: Mapping[str, Any] | None = None,
        modem_health: Mapping[str, Any] | None = None,
        base_status: Mapping[str, Any] | None = None,
        runtime_recovery: Mapping[str, Any] | None = None,
        action_context: Mapping[str, Any] | None = None,
        calibration_handoff: Mapping[str, Any] | None = None,
        require_complete: bool = True,
    ) -> "LearningGateDecision":
        window_present = isinstance(radio_mutation_window, Mapping)
        health_present = isinstance(modem_health, Mapping)
        base_present = isinstance(base_status, Mapping)
        window = dict(radio_mutation_window or {}) if window_present else {}
        health = dict(modem_health or {}) if health_present else {}
        base = dict(base_status or {}) if base_present else {}
        recovery = dict(runtime_recovery or {}) if isinstance(runtime_recovery, Mapping) else {}
        action = dict(action_context or {}) if isinstance(action_context, Mapping) else {}
        handoff = dict(calibration_handoff or {}) if isinstance(calibration_handoff, Mapping) else {}
        missing_inputs = tuple(
            name
            for name, present in (
                ("radio_mutation_window", window_present),
                ("modem_health", health_present),
                ("base_status", base_present),
            )
            if not present
        )
        malformed_inputs = (
            _malformed_bool_fields(
                "radio_mutation_window",
                window,
                ("active", "learning_allowed", "action_allowed", "workflow_active", "sequence_active"),
            )
            + _malformed_bool_fields(
                "modem_health",
                health,
                (
                    "stale",
                    "stale_rf",
                    "live_register_stale",
                    "live_register_cached_stale",
                    "register_cached_stale",
                    "cached_stale",
                    "modem_read_missed",
                    "read_missed",
                    "degraded",
                    "reconnecting",
                    "reconnect_recent",
                    "recent_reconnect",
                    "traffic_dirty",
                    "rsrq_dirty",
                    "rtt_dirty",
                ),
            )
            + _malformed_bool_fields(
                "base_status",
                base,
                (
                    "stale",
                    "stale_rf",
                    "live_register_stale",
                    "live_register_cached_stale",
                    "register_cached_stale",
                    "cached_stale",
                    "modem_read_missed",
                    "traffic_dirty",
                    "traffic_delta_dirty",
                    "dirty_traffic_delta",
                    "rsrq_dirty",
                    "rsrq_delta_dirty",
                    "dirty_rsrq_delta",
                    "rtt_dirty",
                    "rtt_delta_dirty",
                    "dirty_rtt_delta",
                ),
            )
            + _malformed_bool_fields(
                "runtime_recovery",
                recovery,
                ("active", "degraded", "reconnecting", "reconnect_recent", "recent_reconnect"),
            )
            + _malformed_bool_fields(
                "action_context",
                action,
                (
                    "post_action_settle",
                    "verification_pending",
                    "operator_action_in_progress",
                    "failed_missing_modem_after",
                ),
            )
        )
        gate_input_complete = not missing_inputs and not malformed_inputs

        window_active = safe_bool(window.get("active", False))
        window_learning_allowed = safe_bool(
            window.get("learning_allowed", not window_active),
            default=not window_active,
        )
        stale_sample = any(
            safe_bool(value)
            for value in (
                base.get("stale"),
                base.get("stale_rf"),
                base.get("live_register_stale"),
                base.get("live_register_cached_stale"),
                base.get("register_cached_stale"),
                base.get("cached_stale"),
                base.get("modem_read_missed"),
                health.get("stale"),
                health.get("stale_rf"),
                health.get("live_register_stale"),
                health.get("live_register_cached_stale"),
                health.get("register_cached_stale"),
                health.get("cached_stale"),
                health.get("modem_read_missed"),
                health.get("read_missed"),
            )
        )
        runtime_recovery_active = any(
            safe_bool(value)
            for value in (
                recovery.get("active"),
                recovery.get("degraded"),
                recovery.get("reconnecting"),
                recovery.get("reconnect_recent"),
                recovery.get("recent_reconnect"),
                health.get("degraded"),
                health.get("reconnecting"),
                health.get("reconnect_recent"),
                health.get("recent_reconnect"),
            )
        )
        action_dirty = any(
            safe_bool(value)
            for value in (
                action.get("post_action_settle"),
                action.get("verification_pending"),
                action.get("operator_action_in_progress"),
                action.get("failed_missing_modem_after"),
            )
        )
        baseline_state = str(handoff.get("baseline_state") or "").strip().upper()
        session_state = str(handoff.get("session_state") or "").strip().upper()
        calibration_handoff_rejected = baseline_state in {"INVALID", "REJECTED"} or session_state in {
            "FAILED",
            "REJECTED",
        }
        dirty_metric_delta = any(
            safe_bool(value)
            for value in (
                base.get("traffic_dirty"),
                base.get("traffic_delta_dirty"),
                base.get("dirty_traffic_delta"),
                base.get("rsrq_dirty"),
                base.get("rsrq_delta_dirty"),
                base.get("dirty_rsrq_delta"),
                base.get("rtt_dirty"),
                base.get("rtt_delta_dirty"),
                base.get("dirty_rtt_delta"),
                health.get("traffic_dirty"),
                health.get("rsrq_dirty"),
                health.get("rtt_dirty"),
            )
        )
        learning_allowed = (
            window_learning_allowed
            and not stale_sample
            and not runtime_recovery_active
            and not action_dirty
            and not calibration_handoff_rejected
            and not dirty_metric_delta
        )
        if require_complete and not gate_input_complete:
            learning_allowed = False

        block_reason = ""
        block_detail = ""
        if not learning_allowed:
            if require_complete and malformed_inputs:
                block_reason = "learning_gate_input_malformed"
            elif stale_sample:
                block_detail = str(
                    base.get("learning_block_detail")
                    or health.get("learning_block_detail")
                    or base.get("stale_detail")
                    or health.get("stale_detail")
                    or base.get("live_register_stale_cause")
                    or health.get("live_register_stale_cause")
                    or ""
                ).strip()
                block_reason = str(
                    health.get("reason")
                    or base.get("reason")
                    or (
                        "modem_read_missed"
                        if (
                            safe_bool(base.get("modem_read_missed"))
                            or safe_bool(health.get("modem_read_missed"))
                            or safe_bool(health.get("read_missed"))
                        )
                        else "stale_sample"
                    )
                )
            elif runtime_recovery_active:
                block_reason = str(
                    recovery.get("reason")
                    or health.get("reason")
                    or "runtime_recovery"
                )
            elif action_dirty:
                if safe_bool(action.get("failed_missing_modem_after"), False):
                    block_reason = "failed_action_missing_modem_after"
                elif safe_bool(action.get("verification_pending"), False):
                    block_reason = "verification_pending"
                elif safe_bool(action.get("operator_action_in_progress"), False):
                    block_reason = "operator_action_in_progress"
                else:
                    block_reason = "post_action_settle"
            elif calibration_handoff_rejected:
                block_reason = "low_calibration_trust"
            elif dirty_metric_delta:
                block_reason = "dirty_metric_delta"
            elif not window_learning_allowed or (
                window_active
                and str(window.get("telemetry_trust") or "").strip().lower() == "transition"
            ):
                block_reason = str(
                    window.get("reason")
                    or ",".join(str(item) for item in (window.get("reasons") or []) if item)
                    or ("radio_mutation_window" if window_active else "learning_gate")
                )
            elif require_complete and not gate_input_complete:
                block_reason = (
                    "learning_gate_input_malformed"
                    if malformed_inputs
                    else "learning_gate_input_incomplete"
                )
            else:
                block_reason = str(
                    window.get("reason")
                    or ",".join(str(item) for item in (window.get("reasons") or []) if item)
                    or ("radio_mutation_window" if window_active else "learning_gate")
                )

        compact_window = {
            key: window.get(key)
            for key in (
                "reason",
                "reasons",
                "until_ts",
                "remaining_s",
                "telemetry_trust",
            )
            if key in window
        }
        if window_present and window:
            compact_window["active"] = window_active
            compact_window["learning_allowed"] = window_learning_allowed
            compact_window["action_allowed"] = safe_bool(
                window.get("action_allowed", not window_active),
                default=not window_active,
            )
        return cls(
            learning_allowed=learning_allowed,
            block_reason=block_reason,
            block_detail=block_detail,
            radio_mutation_window=compact_window,
            sample_stale=stale_sample,
            gate_input_complete=gate_input_complete,
            missing_inputs=missing_inputs,
            malformed_inputs=malformed_inputs,
        )

    @classmethod
    def from_radio_mutation_window(cls, mutation_window: Mapping[str, Any] | None) -> "LearningGateDecision":
        return cls.from_sources(radio_mutation_window=mutation_window, require_complete=False)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "LearningGateDecision":
        data = dict(payload or {})
        missing_inputs = tuple(str(item or "") for item in data.get("missing_inputs", ()) if str(item or ""))
        malformed_input_list = [
            str(item or "") for item in data.get("malformed_inputs", ()) if str(item or "")
        ]
        for field in ("learning_allowed", "sample_stale", "gate_input_complete"):
            if field in data and not _valid_gate_bool(data.get(field)):
                malformed_input_list.append(f"learning_gate.{field}")
        malformed_inputs = tuple(malformed_input_list)
        gate_input_complete = (
            safe_bool(data.get("gate_input_complete", True), default=True)
            and not missing_inputs
            and not malformed_inputs
        )
        sample_stale = safe_bool(data.get("sample_stale", False), default=False)
        learning_allowed = (
            safe_bool(data.get("learning_allowed", False), default=False)
            and gate_input_complete
            and not sample_stale
        )
        block_reason = str(data.get("block_reason") or "")
        block_detail = str(data.get("block_detail") or "")
        if not learning_allowed and not block_reason:
            if malformed_inputs:
                block_reason = "learning_gate_input_malformed"
            elif missing_inputs or not gate_input_complete:
                block_reason = "learning_gate_input_incomplete"
            elif sample_stale:
                block_reason = "sample_stale"
            else:
                block_reason = "learning_gate_blocked"
        radio_mutation_window = (
            dict(data.get("radio_mutation_window") or {})
            if isinstance(data.get("radio_mutation_window"), Mapping)
            else {}
        )
        return cls(
            learning_allowed=learning_allowed,
            block_reason=block_reason,
            block_detail=block_detail,
            radio_mutation_window=radio_mutation_window,
            sample_stale=sample_stale,
            gate_input_complete=gate_input_complete,
            missing_inputs=missing_inputs,
            malformed_inputs=malformed_inputs,
        )

    def as_dict(self) -> dict[str, Any]:
        data = {
            "learning_allowed": self.learning_allowed,
            "block_reason": self.block_reason,
            "radio_mutation_window": dict(self.radio_mutation_window or {}),
            "sample_stale": self.sample_stale,
            "gate_input_complete": self.gate_input_complete,
            "missing_inputs": list(self.missing_inputs),
            "malformed_inputs": list(self.malformed_inputs),
        }
        if self.block_detail:
            data["block_detail"] = self.block_detail
        return data


def build_learning_gate_publication(
    gate: LearningGateDecision | Mapping[str, Any],
    *,
    source: str,
    consumer: str = "",
) -> dict[str, Any]:
    """Build the only legal public learning-gate publication."""

    if isinstance(gate, LearningGateDecision):
        decision = gate
    else:
        data = dict(gate or {}) if isinstance(gate, Mapping) else {}
        if data.get("schema") == LEARNING_GATE_SCHEMA and isinstance(data.get("decision"), Mapping):
            data = dict(data.get("decision") or {})
        decision = LearningGateDecision.from_mapping(data)
    return {
        "schema": LEARNING_GATE_SCHEMA,
        "owner": LEARNING_GATE_OWNER,
        "source": str(source or ""),
        "consumer": str(consumer or ""),
        "decision": decision.as_dict(),
        "learning_allowed": bool(decision.learning_allowed),
        "block_reason": str(decision.block_reason or ""),
        "block_detail": str(decision.block_detail or ""),
        "sample_stale": bool(decision.sample_stale),
        "gate_input_complete": bool(decision.gate_input_complete),
        "missing_inputs": list(decision.missing_inputs),
        "malformed_inputs": list(decision.malformed_inputs),
        "radio_mutation_window": dict(decision.radio_mutation_window or {}),
    }


def learning_gate_source_inputs_present(
    *,
    radio_mutation_window: Mapping[str, Any] | None = None,
    modem_health: Mapping[str, Any] | None = None,
    base_status: Mapping[str, Any] | None = None,
) -> bool:
    """Return true when the current tick has the full primary gate source set."""

    return (
        isinstance(radio_mutation_window, Mapping)
        and isinstance(modem_health, Mapping)
        and isinstance(base_status, Mapping)
    )


def _mutation_window_contract_value(field: str, value: Any) -> Any:
    if field in {"active", "learning_allowed", "action_allowed"}:
        return safe_bool(value, default=False)
    return str(value or "").strip()


def _mutation_window_conflicts(
    register_value: Mapping[str, Any],
    tick_value: Mapping[str, Any],
) -> tuple[str, ...]:
    conflicts: list[str] = []
    for field in _MUTATION_WINDOW_CONTRACT_FIELDS:
        if field not in register_value or field not in tick_value:
            continue
        left = _mutation_window_contract_value(field, register_value.get(field))
        right = _mutation_window_contract_value(field, tick_value.get(field))
        if left != right:
            conflicts.append(field)
    return tuple(conflicts)


def select_learning_gate_mutation_window(payload: Mapping[str, Any] | None) -> Dict[str, Any] | None:
    """Return the canonical mutation source for learning-gate decisions.

    The runtime mutation register is the authoritative mutation surface for
    learning-gate consumers. If the raw owner input is also present, it must
    agree on the contract fields, but it cannot replace the register.
    """

    data = payload if isinstance(payload, Mapping) else {}
    registers = data.get("runtime_registers")
    mutation_register = registers.get("mutation") if isinstance(registers, Mapping) else None
    register_value = mutation_register.get("value") if isinstance(mutation_register, Mapping) else None
    tick_window = data.get("radio_mutation_window")
    if isinstance(register_value, Mapping):
        if isinstance(tick_window, Mapping):
            conflicts = _mutation_window_conflicts(register_value, tick_window)
            if conflicts:
                raise RuntimeError(
                    "conflicting learning-gate mutation sources: "
                    f"fields={','.join(conflicts)}"
                )
        return dict(register_value)
    return None


def resolve_learning_gate_decision(
    *,
    radio_mutation_window: Mapping[str, Any] | None = None,
    modem_health: Mapping[str, Any] | None = None,
    base_status: Mapping[str, Any] | None = None,
    runtime_recovery: Mapping[str, Any] | None = None,
    action_context: Mapping[str, Any] | None = None,
    calibration_handoff: Mapping[str, Any] | None = None,
    require_complete: bool = True,
) -> tuple[LearningGateDecision, str]:
    """Resolve the learning gate only from current authoritative sources."""

    if learning_gate_source_inputs_present(
        radio_mutation_window=radio_mutation_window,
        modem_health=modem_health,
        base_status=base_status,
    ):
        return (
            LearningGateDecision.from_sources(
                radio_mutation_window=radio_mutation_window,
                modem_health=modem_health,
                base_status=base_status,
                runtime_recovery=runtime_recovery,
                action_context=action_context,
                calibration_handoff=calibration_handoff,
                require_complete=require_complete,
            ),
            "current_sources",
        )
    return (
        LearningGateDecision.from_sources(
            radio_mutation_window=radio_mutation_window,
            modem_health=modem_health,
            base_status=base_status,
            runtime_recovery=runtime_recovery,
            action_context=action_context,
            calibration_handoff=calibration_handoff,
            require_complete=require_complete,
        ),
        "current_sources",
    )


def build_post_action_learning_gate(
    *,
    verification_pending: bool,
    post_action_read_error: str,
    radio_mutation_window: Mapping[str, Any] | None = None,
    modem_health: Mapping[str, Any] | None = None,
    base_status: Mapping[str, Any] | None = None,
    runtime_recovery: Mapping[str, Any] | None = None,
    action_context: Mapping[str, Any] | None = None,
    calibration_handoff: Mapping[str, Any] | None = None,
) -> LearningGateDecision:
    """Build the post-action learning gate inside the learning-gate owner."""

    if bool(verification_pending):
        return LearningGateDecision(
            learning_allowed=False,
            block_reason="post_action_verification_pending",
            radio_mutation_window={},
            sample_stale=False,
        )
    if str(post_action_read_error or "").strip():
        return LearningGateDecision(
            learning_allowed=False,
            block_reason="post_action_read_failed",
            radio_mutation_window={},
            sample_stale=True,
        )
    gate, _source = resolve_learning_gate_decision(
        radio_mutation_window=radio_mutation_window,
        modem_health=modem_health,
        base_status=base_status,
        runtime_recovery=runtime_recovery,
        action_context=action_context,
        calibration_handoff=calibration_handoff,
        require_complete=True,
    )
    return gate
