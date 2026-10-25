"""Brain input/fused register bank.

External feeders update their own registers at their natural cadence. Brain's
runtime-clock lane reads those immutable inputs and publishes one fused Brain
register without touching modem I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Mapping

from arch4.coerce import safe_bool
from arch4.controller_frames import (
    CellularIntelligenceGeneration,
    DerivedSignalFrames,
)
from arch4.runtime_registers import (
    REGISTER_CA,
    REGISTER_CAPACITY,
    REGISTER_DECISION,
    REGISTER_HEALTH,
    REGISTER_IDENTITY,
    REGISTER_LEARNING,
    REGISTER_MUTATION,
    REGISTER_QUALITY,
    REGISTER_RADIO,
    REGISTER_TRAFFIC,
    require_core_runtime_register_bank,
)


FEED_MODEM_TICK = "modem_tick"
FEED_RUNTIME_REGISTERS = "runtime_registers"
FEED_BRAIN_EVENTS = "brain_events"
FEED_LEARNING_GATE = "learning.gate"
FEED_LEARNING_QUALITY = "learning.quality"
FEED_SELF_LEARNING_STRATEGY = "self_learning.strategy"
FEED_REFEREE_PUBLICATION = "referee.publication"
FEED_DECISION_QUALITY = "brain.decision_quality"
FEED_BRAIN_LEARNING_EFFECT = "brain.learning_effect"
FEED_ACTION_PROOF_CLOSURES = "brain.action_proof_closures"
FEED_MODEM_LIVE_RADIO = "modem_live.radio"
FEED_MODEM_LIVE_RF = "modem_live.rf"
FEED_MODEM_LIVE_TRAFFIC = "modem_live.traffic"
FEED_MODEM_LIVE_HEALTH = "modem_live.health"
FEED_MODEM_LIVE_IDENTITY = "modem_live.identity"
OWNER_ENGINE_FINALIZE_FEED_REGISTERS = "engine_finalize_feed_registers"
OWNER_RUNTIME_REGISTERS = "runtime_registers"
OWNER_BRAIN_EVENT_INBOX = "brain_event_inbox"
OWNER_LEARNING_GATE = "arch4.learning_gate_contract"
OWNER_LEARNING_QUALITY = "arch4.brain.learning_quality"
OWNER_MODEM_LIVE = "modem_live"
OWNER_SELF_LEARNING_STRATEGY = "arch4.smart_loop.closed_loop"
OWNER_REFEREE_PUBLICATION = "arch4.brain.referee_publication"
OWNER_DECISION_QUALITY = "arch4.brain.decision_quality_owner"
OWNER_BRAIN_LEARNING_EFFECT = "arch4.brain.learning_effect"
OWNER_ACTION_PROOF_REGISTER = "arch4.brain.action_proof_register"

MODEM_LIVE_LINE_FEEDS: dict[str, str] = {
    "radio": FEED_MODEM_LIVE_RADIO,
    "rf": FEED_MODEM_LIVE_RF,
    "traffic": FEED_MODEM_LIVE_TRAFFIC,
    "health": FEED_MODEM_LIVE_HEALTH,
    "identity": FEED_MODEM_LIVE_IDENTITY,
}

DEFAULT_FEED_OWNER_MAP: dict[str, str] = {
    FEED_MODEM_TICK: OWNER_ENGINE_FINALIZE_FEED_REGISTERS,
    FEED_RUNTIME_REGISTERS: OWNER_RUNTIME_REGISTERS,
    FEED_BRAIN_EVENTS: OWNER_BRAIN_EVENT_INBOX,
    FEED_LEARNING_GATE: OWNER_LEARNING_GATE,
    FEED_LEARNING_QUALITY: OWNER_LEARNING_QUALITY,
    FEED_SELF_LEARNING_STRATEGY: OWNER_SELF_LEARNING_STRATEGY,
    FEED_REFEREE_PUBLICATION: OWNER_REFEREE_PUBLICATION,
    FEED_DECISION_QUALITY: OWNER_DECISION_QUALITY,
    FEED_BRAIN_LEARNING_EFFECT: OWNER_BRAIN_LEARNING_EFFECT,
    FEED_ACTION_PROOF_CLOSURES: OWNER_ACTION_PROOF_REGISTER,
    FEED_MODEM_LIVE_RADIO: OWNER_MODEM_LIVE,
    FEED_MODEM_LIVE_RF: OWNER_MODEM_LIVE,
    FEED_MODEM_LIVE_TRAFFIC: OWNER_MODEM_LIVE,
    FEED_MODEM_LIVE_HEALTH: OWNER_MODEM_LIVE,
    FEED_MODEM_LIVE_IDENTITY: OWNER_MODEM_LIVE,
}
DEFAULT_REQUIRED_FEEDS: tuple[str, ...] = (FEED_MODEM_TICK, FEED_RUNTIME_REGISTERS)
_BRAIN_FEED_REGISTERS_STATUS_ATTR = "_brain_feed_registers_status"
_BRAIN_FUSED_REGISTER_ATTR = "_brain_fused_register"
BRAIN_RUNTIME_REGISTER_FEED_NAMES: tuple[str, ...] = (
    REGISTER_IDENTITY,
    REGISTER_RADIO,
    REGISTER_QUALITY,
    REGISTER_CA,
    REGISTER_TRAFFIC,
    REGISTER_LEARNING,
    REGISTER_CAPACITY,
    REGISTER_MUTATION,
    REGISTER_DECISION,
    REGISTER_HEALTH,
)


class BrainFeedRegisterError(ValueError):
    """Raised when a Brain feeder register contract is violated."""


class BrainFeedOwnershipError(BrainFeedRegisterError):
    """Raised when a non-owner writes a feeder register."""


@dataclass(frozen=True)
class BrainFeedRegister:
    feeder: str
    owner: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""
    updated_at: float = 0.0
    valid: bool = True
    stale_after_s: float = 0.0
    sequence: int = 0
    error: str = ""

    def is_stale(self, *, now: float | None = None) -> bool:
        if not self.valid:
            return True
        if self.stale_after_s <= 0.0 or self.updated_at <= 0.0:
            return False
        now_ts = time.time() if now is None else float(now)
        return max(0.0, now_ts - float(self.updated_at)) > float(self.stale_after_s)

    def as_dict(self, *, now: float | None = None, include_payload: bool = True) -> dict[str, Any]:
        now_ts = time.time() if now is None else float(now)
        age_s = max(0.0, now_ts - float(self.updated_at or now_ts))
        out = {
            "feeder": self.feeder,
            "owner": self.owner,
            "source": self.source,
            "updated_at": round(float(self.updated_at or 0.0), 3),
            "age_s": round(age_s, 3),
            "valid": bool(self.valid),
            "stale": self.is_stale(now=now_ts),
            "stale_after_s": round(float(self.stale_after_s or 0.0), 3),
            "sequence": int(self.sequence),
            "error": self.error,
        }
        if include_payload:
            out["payload"] = _freeze(self.payload)
        else:
            out.update(_payload_summary(self.payload))
        return out


@dataclass(frozen=True)
class BrainFusedRegister:
    payload: Mapping[str, Any]
    updated_at: float
    ready: bool
    missing_feeds: tuple[str, ...]
    stale_feeds: tuple[str, ...]
    sequence: int

    def as_dict(self, *, include_payload: bool = True) -> dict[str, Any]:
        out = {
            "schema_version": "brain_fused_register.v1",
            "updated_at": round(float(self.updated_at or 0.0), 3),
            "ready": bool(self.ready),
            "missing_feeds": list(self.missing_feeds),
            "stale_feeds": list(self.stale_feeds),
            "sequence": int(self.sequence),
        }
        if include_payload:
            out["payload"] = _freeze(self.payload)
        else:
            out.update(_payload_summary(self.payload))
        return out


class BrainFeedRegisterBank:
    """Thread-safe per-feeder inputs plus a Brain-owned fused register."""

    def __init__(
        self,
        *,
        owner_map: Mapping[str, str] | None = None,
        required_feeds: tuple[str, ...] = DEFAULT_REQUIRED_FEEDS,
    ):
        self.owner_map = dict(owner_map or DEFAULT_FEED_OWNER_MAP)
        self.required_feeds = tuple(_normalize_feeder(feed) for feed in required_feeds)
        self._lock = threading.RLock()
        self._feeders: dict[str, BrainFeedRegister] = {}
        self._fused = BrainFusedRegister(
            payload={},
            updated_at=0.0,
            ready=False,
            missing_feeds=tuple(self.required_feeds),
            stale_feeds=(),
            sequence=0,
        )
        self._sequence = 0
        self._fused_sequence = 0
        for feeder in self.owner_map:
            _validate_feeder(feeder, self.owner_map)
        for feeder in self.required_feeds:
            _validate_feeder(feeder, self.owner_map)

    def write_feeder(
        self,
        feeder: str,
        *,
        owner: str,
        payload: Mapping[str, Any] | None,
        source: str,
        updated_at: float | None = None,
        valid: bool = True,
        stale_after_s: float = 0.0,
        error: str = "",
    ) -> BrainFeedRegister:
        normalized = _validate_feeder(feeder, self.owner_map)
        owner_text = str(owner or "").strip()
        expected = str(self.owner_map.get(normalized, "") or "")
        if owner_text != expected:
            raise BrainFeedOwnershipError(
                f"brain feeder {normalized!r} is owned by {expected!r}, not {owner_text!r}"
            )
        now_ts = time.time() if updated_at is None else float(updated_at)
        with self._lock:
            self._sequence += 1
            register = BrainFeedRegister(
                feeder=normalized,
                owner=expected,
                payload=_freeze(dict(payload or {})),
                source=str(source or ""),
                updated_at=now_ts,
                valid=safe_bool(valid, True),
                stale_after_s=max(0.0, float(stale_after_s or 0.0)),
                sequence=int(self._sequence),
                error=str(error or ""),
            )
            self._feeders[normalized] = register
            return register

    def touch_feeder(
        self,
        feeder: str,
        *,
        owner: str,
        source: str = "",
        updated_at: float | None = None,
        valid: bool | None = None,
        stale_after_s: float | None = None,
        error: str | None = None,
    ) -> BrainFeedRegister:
        normalized = _validate_feeder(feeder, self.owner_map)
        owner_text = str(owner or "").strip()
        expected = str(self.owner_map.get(normalized, "") or "")
        if owner_text != expected:
            raise BrainFeedOwnershipError(
                f"brain feeder {normalized!r} is owned by {expected!r}, not {owner_text!r}"
            )
        now_ts = time.time() if updated_at is None else float(updated_at)
        with self._lock:
            existing = self._feeders.get(normalized)
            if existing is None:
                raise BrainFeedRegisterError(f"brain feeder {normalized!r} cannot be touched before publication")
            register = BrainFeedRegister(
                feeder=normalized,
                owner=expected,
                payload=existing.payload,
                source=str(source or existing.source or ""),
                updated_at=now_ts,
                valid=existing.valid if valid is None else safe_bool(valid, True),
                stale_after_s=(
                    float(existing.stale_after_s or 0.0)
                    if stale_after_s is None
                    else max(0.0, float(stale_after_s or 0.0))
                ),
                sequence=int(existing.sequence),
                error=existing.error if error is None else str(error or ""),
            )
            self._feeders[normalized] = register
            return register

    def read_feeder(self, feeder: str) -> BrainFeedRegister:
        normalized = _validate_feeder(feeder, self.owner_map)
        with self._lock:
            existing = self._feeders.get(normalized)
            if existing is not None:
                return _copy_feed_register(existing)
            return BrainFeedRegister(
                feeder=normalized,
                owner=str(self.owner_map.get(normalized, "") or ""),
                valid=False,
                stale_after_s=0.0,
                error="missing",
            )

    def snapshot_feeders(
        self,
        *,
        now: float | None = None,
        include_payload: bool = True,
    ) -> dict[str, dict[str, Any]]:
        now_ts = time.time() if now is None else float(now)
        with self._lock:
            names = sorted(self.owner_map)
            return {
                name: register.as_dict(now=now_ts, include_payload=include_payload)
                if isinstance((register := self._feeders.get(name)), BrainFeedRegister)
                else BrainFeedRegister(
                    feeder=name,
                    owner=str(self.owner_map.get(name, "") or ""),
                    valid=False,
                    stale_after_s=0.0,
                    error="missing",
                ).as_dict(now=now_ts, include_payload=include_payload)
                for name in names
            }

    def fuse(self, *, now: float | None = None) -> BrainFusedRegister:
        now_ts = time.time() if now is None else float(now)
        with self._lock:
            registers = {name: self._feeders.get(name) for name in sorted(self.owner_map)}
            missing = tuple(
                name
                for name in self.required_feeds
                if registers.get(name) is None or str(getattr(registers.get(name), "error", "") or "") == "missing"
            )
            stale = tuple(
                name
                for name in self.required_feeds
                if name not in missing
                and (
                    bool(registers[name].is_stale(now=now_ts) if registers.get(name) is not None else True)
                    or not bool(getattr(registers.get(name), "valid", False))
                )
            )
            payload = {
                "feeds": {
                    name: register.as_dict(now=now_ts, include_payload=False)
                    for name, register in registers.items()
                    if register is not None
                },
                "modem_live_lines": {
                    line_name: _register_payload(registers.get(feeder))
                    for line_name, feeder in MODEM_LIVE_LINE_FEEDS.items()
                    if _register_payload(registers.get(feeder))
                },
                "modem_tick": _register_payload(registers.get(FEED_MODEM_TICK)),
                "runtime_registers": _register_payload(registers.get(FEED_RUNTIME_REGISTERS)).get(
                    "runtime_registers",
                    {},
                ),
                "decision_input": _register_payload(registers.get(FEED_RUNTIME_REGISTERS)).get("decision_input", {}),
                "brain_events": _register_payload(registers.get(FEED_BRAIN_EVENTS)),
                "learning_gate": _register_payload(registers.get(FEED_LEARNING_GATE)),
                "learning_quality": _register_payload(registers.get(FEED_LEARNING_QUALITY)),
                "self_learning_strategy": _register_payload(registers.get(FEED_SELF_LEARNING_STRATEGY)),
                "referee_publication": _register_payload(registers.get(FEED_REFEREE_PUBLICATION)),
                "required_feeds": list(self.required_feeds),
            }
            self._fused_sequence += 1
            self._fused = BrainFusedRegister(
                payload=payload,
                updated_at=now_ts,
                ready=not missing and not stale,
                missing_feeds=missing,
                stale_feeds=stale,
                sequence=int(self._fused_sequence),
            )
            return self._copy_fused_locked()

    def fused(self) -> BrainFusedRegister:
        with self._lock:
            return self._copy_fused_locked()

    def status(self, *, now: float | None = None, include_payload: bool = False) -> dict[str, Any]:
        now_ts = time.time() if now is None else float(now)
        with self._lock:
            return {
                "schema_version": "brain_feed_register_bank.v1",
                "required_feeds": list(self.required_feeds),
                "feeders": self.snapshot_feeders(now=now_ts, include_payload=include_payload),
                "fused": self._fused.as_dict(include_payload=include_payload),
            }

    def _copy_fused_locked(self) -> BrainFusedRegister:
        return self._fused


def require_brain_feed_register_bank(engine: Any) -> BrainFeedRegisterBank:
    """Return the sole composition-root Brain feed owner or fail loudly."""

    controller = getattr(engine, "controller", None)
    brain = getattr(controller, "brain", None)
    bank = getattr(brain, "feed_registers", None)
    if not isinstance(bank, BrainFeedRegisterBank):
        raise RuntimeError("controller brain feed register bank is not composed")
    return bank


def build_tick_feed_payload(
    *,
    modem: CellularIntelligenceGeneration,
    computed: DerivedSignalFrames,
    extra: Mapping[str, Any] | None,
    runtime_registers: Mapping[str, Any],
    incident_assessment: Mapping[str, Any] | None = None,
    decision_trace_id: str = "",
) -> dict[str, Any]:
    if not isinstance(modem, CellularIntelligenceGeneration):
        raise TypeError(
            "brain_modem_tick_feed_requires_cellular_intelligence_generation"
        )
    if not isinstance(computed, DerivedSignalFrames):
        raise TypeError("brain_modem_tick_feed_requires_derived_signal_frames")
    extra_map = dict(extra or {}) if isinstance(extra, Mapping) else {}
    register_status = _runtime_register_status(runtime_registers)
    modem_compact = _compact_object(
        modem.cellular,
        (
            "primary_band",
            "ca_band_signature",
            "selected_band_set_signature",
            "cell_id",
            "pci",
            "earfcn_dl",
            "sinr_db",
            "rsrq_db",
            "rsrp_dbm",
            "rssi_dbm",
            "radio_connected",
        ),
    )
    modem_compact.update(
        _compact_object(
            modem.identity,
            (
                "configured_band_pool",
                "configured_band_pool_signature",
                "provider_plmn",
                "provider_name",
            ),
        )
    )
    modem_compact.update(
        _compact_object(
            modem.traffic,
            (
                "dl_mbps",
                "ul_mbps",
                "has_traffic",
            ),
        )
    )
    computed_compact = _compact_object(
        computed.quality,
        (
            "cq_cell",
            "cart_activity_per_hour",
            "cast",
            "cart_ld_activity",
            "cast_ld",
            "cast_ld_valid",
            "congestion_pct",
            "congestion_pct_valid",
        ),
    )
    computed_compact.update(
        _compact_object(
            computed.latency,
            (
                "rtt_ms",
                "rtt_sliding_avg",
            ),
        )
    )
    return {
        "schema_version": "brain_modem_tick_feed.v1",
        "decision_trace_id": str(decision_trace_id or ""),
        "register_status": register_status,
        "cellular_intelligence": modem,
        "derived_signals": computed,
        "modem": modem_compact,
        "computed": computed_compact,
        "modem_health": _mapping(extra_map.get("modem_health")),
        "radio_mutation_window": _mapping(extra_map.get("radio_mutation_window")),
        "learning_gate": _mapping(extra_map.get("learning_gate")),
        "extra": {
            "health_stage": str(extra_map.get("health_stage", "") or ""),
            "sample_stale": safe_bool(extra_map.get("sample_stale"), False),
            "live_register_stale": safe_bool(extra_map.get("live_register_stale"), False),
            "runtime_health": _mapping(extra_map.get("runtime_health")),
            "health_checks": _mapping(extra_map.get("health_checks")),
            "incident_assessment": _mapping(extra_map.get("incident_assessment")),
        },
        "staged_roi_updates": _freeze(extra_map.get("staged_roi_updates", [])),
        "incident_assessment": _mapping(incident_assessment),
    }


def publish_tick_feed_registers(
    engine: Any,
    *,
    modem: CellularIntelligenceGeneration,
    computed: DerivedSignalFrames,
    extra: Mapping[str, Any] | None,
    incident_assessment: Mapping[str, Any] | None = None,
    decision_trace_id: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    brain_bank = require_brain_feed_register_bank(engine)
    now_ts = time.time() if now is None else float(now)
    extra_map = dict(extra or {}) if isinstance(extra, Mapping) else {}
    runtime_registers = require_core_runtime_register_bank(engine).snapshot(
        BRAIN_RUNTIME_REGISTER_FEED_NAMES
    )
    register_status = _runtime_register_status(runtime_registers)
    sample_interval = float(getattr(getattr(engine, "cfg", None), "sample_interval_s", 2.0) or 2.0)
    stale_after = max(6.0, sample_interval * 3.0)
    stale = safe_bool(register_status.get("stale"), False)
    out = {"published": True, "feeds": []}
    modem_register = brain_bank.write_feeder(
        FEED_MODEM_TICK,
        owner=OWNER_ENGINE_FINALIZE_FEED_REGISTERS,
        payload=build_tick_feed_payload(
            modem=modem,
            computed=computed,
            extra=extra_map,
            runtime_registers=runtime_registers,
            incident_assessment=incident_assessment,
            decision_trace_id=decision_trace_id,
        ),
        source="engine_finalize_feed_registers",
        updated_at=_first_float(register_status.get("ts"), now_ts) or now_ts,
        valid=not stale,
        stale_after_s=stale_after,
    )
    out["feeds"].append(modem_register.as_dict(now=now_ts, include_payload=False))
    runtime_register = brain_bank.write_feeder(
        FEED_RUNTIME_REGISTERS,
        owner=OWNER_RUNTIME_REGISTERS,
        payload={
            "runtime_registers": _compact_runtime_registers_for_brain_feed(runtime_registers),
            "decision_input": _mapping(extra_map.get("decision_input")),
            "decision_input_blocking_reasons": list(extra_map.get("decision_input_blocking_reasons", []) or []),
            "decision_input_trust_reasons": list(extra_map.get("decision_input_trust_reasons", []) or []),
        },
        source="runtime_register_bank.named_brain_feed",
        updated_at=now_ts,
        valid=True,
        stale_after_s=stale_after,
    )
    out["feeds"].append(runtime_register.as_dict(now=now_ts, include_payload=False))
    return out


def publish_modem_live_line_feeds(
    engine: Any,
    *,
    line_snapshot: Mapping[str, Any] | None,
    now: float | None = None,
) -> dict[str, Any]:
    bank = require_brain_feed_register_bank(engine)
    snapshot = dict(line_snapshot or {}) if isinstance(line_snapshot, Mapping) else {}
    lines = snapshot.get("lines") if isinstance(snapshot.get("lines"), Mapping) else {}
    if not lines:
        return {"published": False, "reason": "modem_live_lines_unavailable", "feeds": []}
    now_ts = time.time() if now is None else float(now)
    stale_after_s = max(0.0, float(snapshot.get("stale_after_s", 0.0) or 0.0))
    sequence_cache = getattr(engine, "_brain_modem_live_line_sequences", None)
    if not isinstance(sequence_cache, dict):
        sequence_cache = {}
        setattr(engine, "_brain_modem_live_line_sequences", sequence_cache)

    out = {"published": True, "feeds": [], "skipped": []}
    for line_name, feeder in MODEM_LIVE_LINE_FEEDS.items():
        line = lines.get(line_name) if isinstance(lines, Mapping) else None
        if not isinstance(line, Mapping):
            continue
        sequence = int(line.get("sequence", 0) or 0)
        if sequence <= 0:
            continue
        if int(sequence_cache.get(line_name, 0) or 0) == sequence:
            out["skipped"].append(line_name)
            continue
        updated_at = _first_float(line.get("updated_at"), snapshot.get("updated_at"), now_ts) or now_ts
        register = bank.write_feeder(
            feeder,
            owner=OWNER_MODEM_LIVE,
            payload=_freeze(dict(line)),
            source=str(line.get("source") or snapshot.get("source") or "modem_live_register_lines"),
            updated_at=updated_at,
            valid=safe_bool(line.get("valid"), True),
            stale_after_s=0.0 if line_name == "identity" else stale_after_s,
            error=str(line.get("error", "") or ""),
        )
        sequence_cache[line_name] = sequence
        out["feeds"].append(register.as_dict(now=now_ts))
    if not out["feeds"]:
        out["published"] = False
        out["reason"] = "no_changed_modem_live_lines"
    return out


def read_brain_feed_registers_status(engine: Any, *, now: float | None = None) -> dict[str, Any]:
    """Return the canonical Brain feed-register status surface."""

    bank = require_brain_feed_register_bank(engine)
    status = bank.status(now=now, include_payload=False)
    return dict(status or {}) if isinstance(status, dict) else {}


def publish_brain_feed_registers_status(
    engine: Any,
    *,
    now: float | None = None,
    refresh_fused: bool = False,
) -> dict[str, Any]:
    """Publish Brain feed-register status onto the engine export surface.

    The feed bank owns the register data. This function owns the private engine
    mirror used by health/current-state/export surfaces.
    """

    bank = require_brain_feed_register_bank(engine)
    now_ts = time.time() if now is None else float(now)
    if refresh_fused:
        fused = bank.fuse(now=now_ts)
        setattr(engine, _BRAIN_FUSED_REGISTER_ATTR, fused.as_dict(include_payload=True))
    status = bank.status(now=now_ts, include_payload=False)
    out = dict(status or {}) if isinstance(status, dict) else {}
    setattr(engine, _BRAIN_FEED_REGISTERS_STATUS_ATTR, out)
    return out


def publish_brain_feed_registers_status_failure(
    engine: Any,
    *,
    last_error: str,
    reason: str = "brain_feed_registers_status_publish_failed",
) -> dict[str, Any]:
    out = {
        "status_failed": True,
        "reason": str(reason or "brain_feed_registers_status_publish_failed"),
        "last_error": str(last_error or ""),
    }
    setattr(engine, _BRAIN_FEED_REGISTERS_STATUS_ATTR, dict(out))
    return out


def _copy_feed_register(register: BrainFeedRegister) -> BrainFeedRegister:
    return BrainFeedRegister(
        feeder=register.feeder,
        owner=register.owner,
        payload=_freeze(register.payload),
        source=register.source,
        updated_at=register.updated_at,
        valid=register.valid,
        stale_after_s=register.stale_after_s,
        sequence=register.sequence,
        error=register.error,
    )


def _feed_payload(feed_rows: Mapping[str, Any], feeder: str) -> dict[str, Any]:
    row = feed_rows.get(feeder) if isinstance(feed_rows, Mapping) else {}
    payload = row.get("payload") if isinstance(row, Mapping) else {}
    return dict(payload or {}) if isinstance(payload, Mapping) else {}


def _register_payload(register: BrainFeedRegister | None) -> dict[str, Any]:
    payload = register.payload if isinstance(register, BrainFeedRegister) else {}
    return dict(payload or {}) if isinstance(payload, Mapping) else {}


def _normalize_feeder(feeder: str) -> str:
    return str(feeder or "").strip().lower()


def _validate_feeder(feeder: str, owner_map: Mapping[str, str]) -> str:
    normalized = _normalize_feeder(feeder)
    if normalized not in owner_map:
        raise BrainFeedRegisterError(f"unknown brain feeder register: {feeder!r}")
    return normalized


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, Mapping) else {}


def _register_row(registers: Mapping[str, Any], name: str) -> dict[str, Any]:
    row = registers.get(name) if isinstance(registers, Mapping) else {}
    return dict(row or {}) if isinstance(row, Mapping) else {}


def _register_value(registers: Mapping[str, Any], name: str) -> dict[str, Any]:
    row = _register_row(registers, name)
    value = row.get("value") if isinstance(row, Mapping) else {}
    return dict(value or {}) if isinstance(value, Mapping) else {}


def _runtime_register_status(runtime_registers: Mapping[str, Any]) -> dict[str, Any]:
    """Compact modem-tick status from runtime registers only."""
    radio_row = _register_row(runtime_registers, REGISTER_RADIO)
    ca_row = _register_row(runtime_registers, REGISTER_CA)
    radio = _register_value(runtime_registers, REGISTER_RADIO)
    ca = _register_value(runtime_registers, REGISTER_CA)
    status_source = {**radio}
    for key in (
        "active_band_set_signature",
        "configured_band_pool_signature",
        "ca_component_band_signature",
    ):
        if key not in status_source and key in ca:
            status_source[key] = ca[key]
    stale = any(
        safe_bool(value, False)
        for value in (
            radio_row.get("stale"),
            ca_row.get("stale"),
            not safe_bool(radio_row.get("valid"), True),
            not safe_bool(ca_row.get("valid"), True) if ca_row else False,
            radio.get("modem_read_missed"),
            radio.get("live_register_stale"),
            radio.get("live_register_cached_stale"),
        )
    )
    compact = _compact_mapping(
        status_source,
        (
            "backend",
            "provider_name",
            "provider_plmn",
            "serving_band",
            "serving_band_signature",
            "active_band_set_signature",
            "configured_band_pool_signature",
            "cell_id",
            "pci",
            "earfcn_dl",
            "modem_read_missed",
            "live_register_stale",
            "live_register_cached_stale",
        ),
    )
    ts = _first_float(radio_row.get("updated_at"), ca_row.get("updated_at"))
    if ts is not None:
        compact["ts"] = ts
    compact["stale"] = bool(stale)
    compact["source"] = "runtime_registers"
    return compact


def _compact_runtime_registers_for_brain_feed(runtime_registers: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in BRAIN_RUNTIME_REGISTER_FEED_NAMES:
        row = _register_row(runtime_registers, name)
        if not row:
            continue
        compact = {
            "name": str(row.get("name") or name),
            "owner": str(row.get("owner") or ""),
            "source": str(row.get("source") or ""),
            "updated_at": _first_float(row.get("updated_at"), 0.0) or 0.0,
            "valid": safe_bool(row.get("valid"), False),
            "stale": safe_bool(row.get("stale"), False),
            "dirty": safe_bool(row.get("dirty"), False),
            "trust": str(row.get("trust") or ""),
            "source_kind": str(row.get("source_kind") or ""),
            "validity": str(row.get("validity") or ""),
            "transition": safe_bool(row.get("transition"), False),
            "sequence": int(row.get("sequence", 0) or 0),
        }
        value = row.get("value")
        if isinstance(value, Mapping):
            compact["value"] = _freeze(value)
        else:
            compact["value"] = {}
        out[name] = compact
    return out


def _compact_mapping(value: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: _freeze(value[key]) for key in keys if key in value}


def _compact_object(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: _freeze(getattr(value, key)) for key in keys if hasattr(value, key)}


def _first_float(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _freeze(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_freeze(item) for item in value]
    if isinstance(value, list):
        return [_freeze(item) for item in value]
    if isinstance(value, set):
        return [_freeze(item) for item in sorted(value, key=str)]
    return value


def _payload_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {
            "payload_schema": "",
            "payload_keys": [],
            "payload_key_count": 0,
        }
    keys = [str(key) for key in value.keys()]
    return {
        "payload_schema": str(value.get("schema") or value.get("schema_version") or ""),
        "payload_keys": keys[:24],
        "payload_key_count": len(keys),
        "payload_truncated": len(keys) > 24,
    }
