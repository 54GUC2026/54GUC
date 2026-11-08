"""Explicit contracts for cross-module runtime boundaries.

These contracts are intentionally small data envelopes. They name the seams
called out by architecture review so modules can exchange one typed object
instead of raw dicts with locally invented substitution rules.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from arch4.coerce import safe_bool, safe_float, safe_int


CONTRACT_TRUST_LEVELS: frozenset[str] = frozenset(
    {"live", "cached", "derived", "unsupported", "unknown", "dirty", "stale", "transition"}
)
BACKEND_IO_STATUSES: frozenset[str] = frozenset(
    {
        "ok",
        "empty_valid",
        "timeout",
        "auth_failed",
        "transport_error",
        "malformed_response",
        "unsupported",
        "stale_cache",
        "failed",
    }
)
_BACKEND_IO_STATUS_ALIASES: dict[str, str] = {
    "empty": "empty_valid",
    "fail": "failed",
    "failure": "failed",
    "error": "failed",
    "transport": "transport_error",
    "malformed": "malformed_response",
    "stale": "stale_cache",
}
BACKEND_IO_ERROR_KINDS: frozenset[str] = frozenset(
    {
        "none",
        "empty_valid",
        "timeout",
        "auth_failed",
        "transport_error",
        "malformed_response",
        "unsupported",
        "stale_cache",
        "failed",
    }
)
BACKEND_IO_STATUS_ERROR_KIND: dict[str, str] = {
    "ok": "none",
    "empty_valid": "empty_valid",
    "timeout": "timeout",
    "auth_failed": "auth_failed",
    "transport_error": "transport_error",
    "malformed_response": "malformed_response",
    "unsupported": "unsupported",
    "stale_cache": "stale_cache",
    "failed": "failed",
}
MODEM_READ_STATUSES: frozenset[str] = frozenset({"ok", "missed", "failed", "unsupported"})
FAULT_SEVERITIES: frozenset[str] = frozenset({"info", "warning", "error", "critical"})
REGISTER_VALIDITIES: frozenset[str] = frozenset({"valid", "stale", "dirty", "unknown"})
RADIO_MUTATION_PHASES: frozenset[str] = frozenset(
    {"", "submitted", "started", "command_sent", "awaiting_confirmation", "verified"}
)
_TRUE_TEXT = frozenset({"1", "true", "yes", "on"})
_FALSE_TEXT = frozenset({"0", "false", "no", "off", ""})


def _valid_bool_literal(value: Any) -> bool:
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


def _text_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item or "") for item in value if str(item or "").strip())
    return ()


@dataclass(frozen=True, slots=True)
class BackendIoResult:
    status: str
    value: Any = None
    reason: str = ""
    source: str = ""
    error_kind: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        raw_status = str(self.status or "").strip().lower()
        status = _normalize_choice(
            _BACKEND_IO_STATUS_ALIASES.get(raw_status, raw_status),
            BACKEND_IO_STATUSES,
            default="failed",
        )
        raw_error_kind = str(self.error_kind or BACKEND_IO_STATUS_ERROR_KIND.get(status, "failed")).strip().lower()
        error_kind = _normalize_choice(
            _BACKEND_IO_STATUS_ALIASES.get(raw_error_kind, raw_error_kind),
            BACKEND_IO_ERROR_KINDS,
            default=BACKEND_IO_STATUS_ERROR_KIND.get(status, "failed"),
        )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason", str(self.reason or ""))
        object.__setattr__(self, "source", str(self.source or ""))
        object.__setattr__(self, "error_kind", error_kind)
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "empty_valid"}

    @property
    def supported(self) -> bool:
        return self.status != "unsupported"

    @property
    def retryable(self) -> bool:
        return self.status in {"timeout", "transport_error", "stale_cache"}

    @property
    def terminal_error(self) -> bool:
        return self.status in {"auth_failed", "unsupported", "malformed_response"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "value": self.value,
            "reason": self.reason,
            "source": self.source,
            "error_kind": self.error_kind,
            "metadata": dict(self.metadata or {}),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None, *, source: str = "") -> "BackendIoResult":
        data = value if isinstance(value, Mapping) else {}
        return cls(
            status=data.get("status", "failed"),
            value=data.get("value"),
            reason=str(data.get("reason") or data.get("error") or ""),
            source=str(data.get("source") or source),
            error_kind=str(data.get("error_kind") or ""),
            metadata=data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {},
        )


@dataclass(frozen=True, slots=True)
class ModemReadResult:
    status: str
    modem: Any = None
    base_status: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""
    error: str = ""
    stale: bool = False
    trust: str = "unknown"
    received_at: float = 0.0

    def __post_init__(self) -> None:
        status = _normalize_choice(self.status, MODEM_READ_STATUSES, default="failed")
        trust = _normalize_choice(self.trust, CONTRACT_TRUST_LEVELS, default="unknown")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "base_status", dict(self.base_status or {}))
        object.__setattr__(self, "source", str(self.source or ""))
        object.__setattr__(self, "error", str(self.error or ""))
        object.__setattr__(self, "stale", safe_bool(self.stale, False) or status != "ok")
        object.__setattr__(self, "trust", trust)
        object.__setattr__(self, "received_at", float(self.received_at or 0.0))

    @property
    def ok(self) -> bool:
        return self.status == "ok" and not self.stale

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "base_status": dict(self.base_status or {}),
            "source": self.source,
            "error": self.error,
            "stale": self.stale,
            "trust": self.trust,
            "received_at": self.received_at,
        }


@dataclass(frozen=True, slots=True)
class RadioMutationWindow:
    active: bool = False
    reason: str = ""
    reasons: tuple[str, ...] = ()
    phase: str = ""
    started_at: float = 0.0
    until_at: float = 0.0
    remaining_s: float = 0.0
    verification_due_at: float = 0.0
    telemetry_trust: str = "live"
    telemetry_transition_active: bool = False
    learning_allowed: bool = True
    action_allowed: bool = True
    watchdog_pause_required: bool = False
    source: str = ""
    workflow_id: str = ""
    workflow_active: bool = False
    sequence: str = ""
    sequence_step: int = 0
    sequence_total: int = 0
    sequence_active: bool = False
    sources: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        telemetry_trust = _normalize_choice(self.telemetry_trust, CONTRACT_TRUST_LEVELS, default="live")
        active = safe_bool(self.active)
        object.__setattr__(self, "active", active)
        object.__setattr__(self, "reason", str(self.reason or ""))
        reasons: list[str] = []
        for item in self.reasons or ():
            text = str(item or "").strip()
            if text and text not in reasons:
                reasons.append(text)
        if self.reason and self.reason not in reasons:
            reasons.insert(0, self.reason)
        object.__setattr__(self, "reasons", tuple(reasons))
        phase = str(self.phase or "").strip().lower()
        object.__setattr__(self, "phase", phase if phase in RADIO_MUTATION_PHASES else "")
        object.__setattr__(self, "started_at", float(self.started_at or 0.0))
        object.__setattr__(self, "until_at", float(self.until_at or 0.0))
        object.__setattr__(self, "remaining_s", max(0.0, float(self.remaining_s or 0.0)))
        object.__setattr__(self, "verification_due_at", float(self.verification_due_at or 0.0))
        object.__setattr__(self, "telemetry_trust", telemetry_trust)
        object.__setattr__(
            self,
            "telemetry_transition_active",
            safe_bool(self.telemetry_transition_active) and active,
        )
        object.__setattr__(self, "learning_allowed", safe_bool(self.learning_allowed) if active else True)
        object.__setattr__(
            self,
            "action_allowed",
            safe_bool(self.action_allowed, default=not active),
        )
        object.__setattr__(self, "watchdog_pause_required", safe_bool(self.watchdog_pause_required) and active)
        object.__setattr__(self, "source", str(self.source or ""))
        object.__setattr__(self, "workflow_id", str(self.workflow_id or ""))
        object.__setattr__(self, "workflow_active", safe_bool(self.workflow_active) and active)
        object.__setattr__(self, "sequence", str(self.sequence or ""))
        object.__setattr__(self, "sequence_step", max(0, int(self.sequence_step or 0)))
        object.__setattr__(self, "sequence_total", max(0, int(self.sequence_total or 0)))
        object.__setattr__(self, "sequence_active", safe_bool(self.sequence_active) and active)
        object.__setattr__(self, "sources", dict(self.sources or {}))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "RadioMutationWindow":
        data = value if isinstance(value, Mapping) else {}
        active = safe_bool(data.get("active", False))
        reason = str(data.get("reason") or data.get("block_reason") or data.get("quiet_reason") or "")
        reasons_raw = data.get("reasons")
        if isinstance(reasons_raw, (list, tuple, set)):
            reasons = tuple(str(item or "").strip() for item in reasons_raw if str(item or "").strip())
        elif reason:
            reasons = (reason,)
        else:
            reasons = ()
        telemetry_trust = str(data.get("telemetry_trust") or data.get("sample_trust") or "")
        if not telemetry_trust:
            telemetry_trust = "transition" if active else "live"
        learning_allowed = data.get("learning_allowed")
        if learning_allowed is None:
            learning_allowed = not active
        action_allowed = data.get("action_allowed")
        if action_allowed is None:
            action_allowed = not active
        return cls(
            active=active,
            reason=reason,
            reasons=reasons,
            phase=str(data.get("phase") or ""),
            started_at=_float_first(data.get("started_at"), data.get("start_ts")),
            until_at=_float_first(data.get("until_at"), data.get("until_ts")),
            remaining_s=_float_first(data.get("remaining_s")),
            verification_due_at=_float_first(data.get("verification_due_at"), data.get("verify_at")),
            telemetry_trust=telemetry_trust,
            telemetry_transition_active=safe_bool(
                data.get("telemetry_transition_active"),
                default=(telemetry_trust in {"transition", "dirty", "stale"} and active),
            ),
            learning_allowed=safe_bool(learning_allowed, default=False),
            action_allowed=safe_bool(action_allowed, default=False),
            watchdog_pause_required=safe_bool(data.get("watchdog_pause_required", active), default=active),
            source=str(data.get("source") or ""),
            workflow_id=str(data.get("workflow_id") or ""),
            workflow_active=safe_bool(data.get("workflow_active", False), default=False),
            sequence=str(data.get("sequence") or data.get("workflow_id") or ""),
            sequence_step=int(data.get("sequence_step", 0) or 0),
            sequence_total=int(data.get("sequence_total", 0) or 0),
            sequence_active=safe_bool(data.get("sequence_active", False), default=False),
            sources=data.get("sources") if isinstance(data.get("sources"), Mapping) else {},
        )

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        payload["sources"] = dict(self.sources or {})
        payload["until_ts"] = self.until_at
        payload["started_ts"] = self.started_at
        payload["verification_due_ts"] = self.verification_due_at
        return payload


@dataclass(frozen=True, slots=True)
class LearningGateValue:
    learning_allowed: bool = False
    block_reason: str = ""
    block_detail: str = ""
    sample_stale: bool = False
    gate_input_complete: bool = True
    missing_inputs: tuple[str, ...] = ()
    malformed_inputs: tuple[str, ...] = ()
    radio_mutation_window: Mapping[str, Any] = field(default_factory=dict)
    source: str = "learning_gate"

    def __post_init__(self) -> None:
        malformed_inputs = tuple(self.malformed_inputs or ())
        missing_inputs = tuple(self.missing_inputs or ())
        gate_input_complete = safe_bool(self.gate_input_complete, True) and not missing_inputs and not malformed_inputs
        sample_stale = safe_bool(self.sample_stale, False)
        learning_allowed = safe_bool(self.learning_allowed, False) and gate_input_complete and not sample_stale
        block_reason = str(self.block_reason or "")
        block_detail = str(self.block_detail or "")
        if not learning_allowed and not block_reason:
            if malformed_inputs:
                block_reason = "learning_gate_input_malformed"
            elif missing_inputs or not gate_input_complete:
                block_reason = "learning_gate_input_incomplete"
            elif sample_stale:
                block_reason = "sample_stale"
            else:
                block_reason = "learning_gate_blocked"
        object.__setattr__(self, "learning_allowed", learning_allowed)
        object.__setattr__(self, "block_reason", block_reason)
        object.__setattr__(self, "block_detail", block_detail)
        object.__setattr__(self, "sample_stale", sample_stale)
        object.__setattr__(self, "gate_input_complete", gate_input_complete)
        object.__setattr__(self, "missing_inputs", missing_inputs)
        object.__setattr__(self, "malformed_inputs", malformed_inputs)
        object.__setattr__(self, "radio_mutation_window", dict(self.radio_mutation_window or {}))
        object.__setattr__(self, "source", str(self.source or "learning_gate"))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "LearningGateValue":
        if not isinstance(payload, Mapping):
            return cls(
                learning_allowed=False,
                block_reason="learning_gate_missing",
                gate_input_complete=False,
                missing_inputs=("learning_gate",),
            )
        data = dict(payload)
        malformed_inputs = list(_text_tuple(data.get("malformed_inputs")))
        for field in ("learning_allowed", "sample_stale", "gate_input_complete"):
            if field in data and not _valid_bool_literal(data.get(field)):
                malformed_inputs.append(f"learning_gate.{field}")
        return cls(
            learning_allowed=safe_bool(data.get("learning_allowed"), False),
            block_reason=str(data.get("block_reason") or ""),
            block_detail=str(data.get("block_detail") or ""),
            sample_stale=safe_bool(data.get("sample_stale"), False),
            gate_input_complete=safe_bool(data.get("gate_input_complete"), True),
            missing_inputs=_text_tuple(data.get("missing_inputs")),
            malformed_inputs=tuple(malformed_inputs),
            radio_mutation_window=(
                data.get("radio_mutation_window")
                if isinstance(data.get("radio_mutation_window"), Mapping)
                else {}
            ),
            source=str(data.get("source") or "learning_gate"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "learning_allowed": self.learning_allowed,
            "block_reason": self.block_reason,
            "block_detail": self.block_detail,
            "sample_stale": self.sample_stale,
            "gate_input_complete": self.gate_input_complete,
            "missing_inputs": list(self.missing_inputs),
            "malformed_inputs": list(self.malformed_inputs),
            "radio_mutation_window": dict(self.radio_mutation_window or {}),
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class RadioSummarySurface:
    provider_name: str = ""
    provider_plmn: str = ""
    serving_band: int = 0
    serving_band_signature: str = ""
    radio_connected: bool = False
    source: str = "runtime_registers"
    stale: bool = False
    validity: str = "unknown"
    owner: str = "radio_register"

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_name", str(self.provider_name or ""))
        object.__setattr__(self, "provider_plmn", str(self.provider_plmn or ""))
        object.__setattr__(self, "serving_band", max(0, safe_int(self.serving_band, 0)))
        object.__setattr__(self, "serving_band_signature", str(self.serving_band_signature or ""))
        object.__setattr__(self, "radio_connected", safe_bool(self.radio_connected, False))
        object.__setattr__(self, "source", str(self.source or "runtime_registers"))
        object.__setattr__(self, "stale", safe_bool(self.stale, False))
        object.__setattr__(self, "validity", _normalize_choice(self.validity, REGISTER_VALIDITIES, default="unknown"))
        object.__setattr__(self, "owner", str(self.owner or "radio_register"))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "RadioSummarySurface":
        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            provider_name=data.get("provider_name", ""),
            provider_plmn=data.get("provider_plmn", ""),
            serving_band=data.get("serving_band", 0),
            serving_band_signature=data.get("serving_band_signature", ""),
            radio_connected=data.get("radio_connected", False),
            source=data.get("source", "runtime_registers"),
            stale=data.get("stale", False),
            validity=data.get("validity", "unknown"),
            owner=data.get("owner", "radio_register"),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CellIdentitySurface:
    cell_id: int = 0
    enb_id: int = 0
    pci: int = 0
    earfcn_dl: int = 0
    source: str = "runtime_registers"
    stale: bool = False
    validity: str = "unknown"
    owner: str = "radio_register"

    def __post_init__(self) -> None:
        object.__setattr__(self, "cell_id", max(0, safe_int(self.cell_id, 0)))
        object.__setattr__(self, "enb_id", max(0, safe_int(self.enb_id, 0)))
        object.__setattr__(self, "pci", max(0, safe_int(self.pci, 0)))
        object.__setattr__(self, "earfcn_dl", max(0, safe_int(self.earfcn_dl, 0)))
        object.__setattr__(self, "source", str(self.source or "runtime_registers"))
        object.__setattr__(self, "stale", safe_bool(self.stale, False))
        object.__setattr__(self, "validity", _normalize_choice(self.validity, REGISTER_VALIDITIES, default="unknown"))
        object.__setattr__(self, "owner", str(self.owner or "radio_register"))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "CellIdentitySurface":
        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            cell_id=data.get("cell_id", 0),
            enb_id=data.get("enb_id", 0),
            pci=data.get("pci", 0),
            earfcn_dl=data.get("earfcn_dl") or data.get("earfcn", 0),
            source=data.get("source", "runtime_registers"),
            stale=data.get("stale", False),
            validity=data.get("validity", "unknown"),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CaDisplaySurface:
    ca_active: bool = False
    carrier_count: int = 0
    carrier_count_known: bool = True
    carrier_count_source: str = "carrier_rows"
    ca_band_signature: str = ""
    configured_band_pool_signature: str = ""
    source: str = "runtime_registers"
    stale: bool = False
    validity: str = "unknown"
    owner: str = "ca_register"

    def __post_init__(self) -> None:
        object.__setattr__(self, "ca_active", safe_bool(self.ca_active, False))
        object.__setattr__(self, "carrier_count", max(0, safe_int(self.carrier_count, 0)))
        object.__setattr__(self, "carrier_count_known", safe_bool(self.carrier_count_known, True))
        object.__setattr__(self, "carrier_count_source", str(self.carrier_count_source or "carrier_rows"))
        object.__setattr__(self, "ca_band_signature", str(self.ca_band_signature or ""))
        object.__setattr__(self, "configured_band_pool_signature", str(self.configured_band_pool_signature or ""))
        object.__setattr__(self, "source", str(self.source or "runtime_registers"))
        object.__setattr__(self, "stale", safe_bool(self.stale, False))
        object.__setattr__(self, "validity", _normalize_choice(self.validity, REGISTER_VALIDITIES, default="unknown"))
        object.__setattr__(self, "owner", str(self.owner or "ca_register"))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "CaDisplaySurface":
        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            ca_active=data.get("ca_active", False),
            carrier_count=data.get("carrier_count", 0),
            carrier_count_known=data.get("carrier_count_known", True),
            carrier_count_source=data.get("carrier_count_source", "carrier_rows"),
            ca_band_signature=data.get("ca_band_signature", ""),
            configured_band_pool_signature=data.get("configured_band_pool_signature", ""),
            source=data.get("source", "runtime_registers"),
            stale=data.get("stale", False),
            validity=data.get("validity", "unknown"),
            owner=data.get("owner", "ca_register"),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DecisionSummarySurface:
    decision_verdict: str = ""
    decision_reason: str = ""
    decision_summary: str = ""
    explicit_no_go: bool = False
    authority_verdict: str = ""
    operating_stance: str = ""
    source: str = "brain_tsar"
    stale: bool = False
    validity: str = "unknown"
    owner: str = "brain_runtime"

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_verdict", str(self.decision_verdict or "").strip().lower())
        object.__setattr__(self, "decision_reason", str(self.decision_reason or ""))
        object.__setattr__(self, "decision_summary", str(self.decision_summary or ""))
        object.__setattr__(self, "explicit_no_go", safe_bool(self.explicit_no_go, False))
        object.__setattr__(self, "authority_verdict", str(self.authority_verdict or "").strip().lower())
        object.__setattr__(self, "operating_stance", str(self.operating_stance or "").strip().lower())
        object.__setattr__(self, "source", str(self.source or "brain_tsar"))
        object.__setattr__(self, "stale", safe_bool(self.stale, False))
        object.__setattr__(self, "validity", _normalize_choice(self.validity, REGISTER_VALIDITIES, default="unknown"))
        object.__setattr__(self, "owner", str(self.owner or "brain_runtime"))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "DecisionSummarySurface":
        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            decision_verdict=data.get("decision_verdict", ""),
            decision_reason=data.get("decision_reason", ""),
            decision_summary=data.get("decision_summary") or data.get("summary", ""),
            explicit_no_go=data.get("explicit_no_go", False),
            authority_verdict=data.get("authority_verdict", ""),
            operating_stance=data.get("operating_stance", ""),
            source=data.get("source", "brain_tsar"),
            stale=data.get("stale", False),
            validity=data.get("validity", "valid" if data else "unknown"),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WatchdogSurface:
    enabled: bool = False
    status: str = ""
    last_action: str = ""
    message: str = ""
    degraded: bool = False
    outage_s: float = 0.0
    threshold_s: float = 0.0
    backend_family: str = ""
    routeros_passive: bool = False
    drives_radio_actions: bool = False
    source: str = "engine_recovery"
    stale: bool = False
    validity: str = "unknown"
    owner: str = "recovery_runtime"

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", safe_bool(self.enabled, False))
        object.__setattr__(self, "status", str(self.status or ""))
        object.__setattr__(self, "last_action", str(self.last_action or ""))
        object.__setattr__(self, "message", str(self.message or ""))
        object.__setattr__(self, "degraded", safe_bool(self.degraded, False))
        object.__setattr__(self, "outage_s", max(0.0, safe_float(self.outage_s, 0.0)))
        object.__setattr__(self, "threshold_s", max(0.0, safe_float(self.threshold_s, 0.0)))
        object.__setattr__(self, "backend_family", str(self.backend_family or ""))
        object.__setattr__(self, "routeros_passive", safe_bool(self.routeros_passive, False))
        object.__setattr__(self, "drives_radio_actions", safe_bool(self.drives_radio_actions, False))
        object.__setattr__(self, "source", str(self.source or "engine_recovery"))
        object.__setattr__(self, "stale", safe_bool(self.stale, False))
        object.__setattr__(self, "validity", _normalize_choice(self.validity, REGISTER_VALIDITIES, default="unknown"))
        object.__setattr__(self, "owner", str(self.owner or "recovery_runtime"))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "WatchdogSurface":
        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            enabled=data.get("enabled", False),
            status=data.get("status", ""),
            last_action=data.get("last_action", ""),
            message=data.get("message", ""),
            degraded=data.get("degraded", False),
            outage_s=data.get("outage_s", 0.0),
            threshold_s=data.get("threshold_s", 0.0),
            backend_family=data.get("backend_family", ""),
            routeros_passive=data.get("routeros_passive", False),
            drives_radio_actions=data.get("drives_radio_actions", False),
            source=data.get("source", "engine_recovery"),
            stale=data.get("stale", False),
            validity=data.get("validity", "valid" if data else "unknown"),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FaultEvent:
    category: str
    source: str = ""
    severity: str = "warning"
    detail: str = ""
    ts: float = 0.0
    recoverable: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        severity = _normalize_choice(self.severity, FAULT_SEVERITIES, default="warning")
        object.__setattr__(self, "category", str(self.category or "unknown"))
        object.__setattr__(self, "source", str(self.source or ""))
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "detail", str(self.detail or ""))
        object.__setattr__(self, "ts", float(self.ts or 0.0))
        object.__setattr__(self, "recoverable", safe_bool(self.recoverable, True))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FaultPayload:
    total: int = 0
    by_category: Mapping[str, int] = field(default_factory=dict)
    last: Mapping[str, Any] = field(default_factory=dict)
    last_fault_event: Mapping[str, Any] = field(default_factory=dict)
    events: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "total", max(0, safe_int(self.total, 0)))
        object.__setattr__(
            self,
            "by_category",
            {str(key or ""): max(0, safe_int(value, 0)) for key, value in dict(self.by_category or {}).items()},
        )
        object.__setattr__(self, "last", dict(self.last or {}))
        object.__setattr__(self, "last_fault_event", dict(self.last_fault_event or {}))
        object.__setattr__(self, "events", tuple(dict(item or {}) for item in (self.events or ()) if isinstance(item, Mapping)))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "FaultPayload":
        data = dict(payload or {}) if isinstance(payload, Mapping) else {}
        events = data.get("events")
        if not isinstance(events, (list, tuple)):
            events = ()
        return cls(
            total=data.get("total", 0),
            by_category=data.get("by_category") if isinstance(data.get("by_category"), Mapping) else {},
            last=data.get("last") if isinstance(data.get("last"), Mapping) else {},
            last_fault_event=(
                data.get("last_fault_event") if isinstance(data.get("last_fault_event"), Mapping) else {}
            ),
            events=tuple(events),
        )

    def append_event(self, event: FaultEvent | Mapping[str, Any], *, max_events: int = 32) -> "FaultPayload":
        event_dict = event.as_dict() if isinstance(event, FaultEvent) else dict(event or {})
        category = str(event_dict.get("category") or "unknown")
        by_category = dict(self.by_category or {})
        by_category[category] = max(0, safe_int(by_category.get(category), 0)) + 1
        events = tuple(list(self.events or ()) + [event_dict])
        if len(events) > max(0, int(max_events or 0)):
            events = events[-int(max_events):]
        return FaultPayload(
            total=self.total + 1,
            by_category=by_category,
            last={
                "category": category,
                "source": str(event_dict.get("source") or ""),
                "error_type": str((event_dict.get("metadata") or {}).get("error_type") or ""),
                "ts": safe_float(event_dict.get("ts"), 0.0),
            },
            last_fault_event=event_dict,
            events=events,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_category": dict(self.by_category or {}),
            "last": dict(self.last or {}),
            "last_fault_event": dict(self.last_fault_event or {}),
            "events": [dict(item or {}) for item in (self.events or ())],
        }


def _normalize_choice(value: Any, choices: frozenset[str], *, default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in choices else default


def _float_first(*values: Any) -> float:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except Exception:
            continue
    return 0.0


def _freeze_boundary_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return MappingProxyType({})
    return MappingProxyType({str(key): _freeze_boundary_value(raw) for key, raw in value.items()})


def _freeze_boundary_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_boundary_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_boundary_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_boundary_value(item) for item in value)
    if isinstance(value, set):
        return tuple(_freeze_boundary_value(item) for item in sorted(value, key=lambda item: str(item)))
    try:
        return deepcopy(value)
    except Exception:
        return value


def _thaw_boundary_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_boundary_value(raw) for key, raw in dict(value or {}).items()}
    if isinstance(value, tuple):
        return [_thaw_boundary_value(item) for item in value]
    if isinstance(value, list):
        return [_thaw_boundary_value(item) for item in value]
    try:
        return deepcopy(value)
    except Exception:
        return value
