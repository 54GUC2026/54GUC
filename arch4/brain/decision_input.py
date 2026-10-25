"""Decision input contract built from runtime registers.

This is the Phase 9 boundary object between runtime registers and Brain. It is
behavior-neutral for action selection, but it gives decision code one stable
shape for register readiness, trust, dirty/stale state, and blocker reasons.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from arch4.coerce import safe_bool
from arch4.runtime_registers import (
    REGISTER_ACTION,
    REGISTER_FAULTS,
    REGISTER_HEALTH,
    REGISTER_LATENCY,
    REGISTER_LEARNING,
    REGISTER_MUTATION,
    REGISTER_RADIO,
    REGISTER_TRAFFIC,
    RUNTIME_REGISTER_OWNER_MAP,
)


DECISION_INPUT_LANES: tuple[str, ...] = (
    REGISTER_RADIO,
    REGISTER_TRAFFIC,
    REGISTER_LATENCY,
    REGISTER_HEALTH,
    REGISTER_MUTATION,
    REGISTER_LEARNING,
    REGISTER_ACTION,
    REGISTER_FAULTS,
)

PHYSICAL_GENERATION_LANES: tuple[str, ...] = (
    REGISTER_RADIO,
    REGISTER_TRAFFIC,
)

_SEVERITY_RANK: dict[str, int] = {
    "ready": 0,
    "advisory": 1,
    "stale": 2,
    "transition": 3,
    "fault": 4,
}


@dataclass(frozen=True)
class DecisionInputLane:
    name: str
    value: Any = None
    valid: bool = False
    stale: bool = True
    dirty: bool = False
    trust: str = "unknown"
    source_kind: str = "unknown"
    validity: str = "unknown"
    transition: bool = False
    source: str = ""
    owner: str = ""
    error: str = ""
    sequence: int = 0
    sample_sequence: int = 0
    source_epoch: str = ""
    generation_match: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DecisionInput:
    lanes: Mapping[str, DecisionInputLane] = field(default_factory=dict)
    blocking_reasons: tuple[str, ...] = ()
    trust_reasons: tuple[str, ...] = ()
    expected_sample_sequence: int = 0
    expected_source_epoch: str = ""

    def lane(self, name: str) -> DecisionInputLane:
        normalized = str(name or "").strip().lower()
        lane = self.lanes.get(normalized)
        if lane is not None:
            return lane
        return _missing_lane(normalized)

    @property
    def ready(self) -> bool:
        return self.severity == "ready"

    @property
    def severity(self) -> str:
        severity = "ready"
        for name in DECISION_INPUT_LANES:
            severity = _max_severity(severity, _lane_severity(self.lane(name)))
        return severity

    @property
    def primary_blocking_reason(self) -> str:
        return self.blocking_reasons[0] if self.blocking_reasons else ""

    def constraint_summary(self, lane_names: tuple[str, ...] = DECISION_INPUT_LANES) -> dict[str, Any]:
        lanes: dict[str, dict[str, Any]] = {}
        for name in lane_names:
            lane = self.lane(name)
            lanes[name] = {
                "valid": bool(lane.valid),
                "stale": bool(lane.stale),
                "dirty": bool(lane.dirty),
                "trust": str(lane.trust or "unknown"),
                "source_kind": str(lane.source_kind or "unknown"),
                "validity": str(lane.validity or "unknown"),
                "transition": bool(lane.transition),
                "owner": str(lane.owner or ""),
                "source": str(lane.source or ""),
                "error": str(lane.error or ""),
                "sequence": int(lane.sequence or 0),
                "sample_sequence": int(lane.sample_sequence or 0),
                "source_epoch": str(lane.source_epoch or ""),
                "generation_match": bool(lane.generation_match),
            }
        return {
            "ready": bool(self.ready),
            "severity": str(self.severity),
            "primary_blocking_reason": self.primary_blocking_reason,
            "lanes": lanes,
            "blocking_reasons": list(self.blocking_reasons),
            "trust_reasons": list(self.trust_reasons),
            "expected_sample_sequence": self.expected_sample_sequence,
            "expected_source_epoch": self.expected_source_epoch,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "lanes": {
                name: self.lanes[name].as_dict()
                for name in sorted(self.lanes)
            },
            "ready": bool(self.ready),
            "severity": str(self.severity),
            "primary_blocking_reason": self.primary_blocking_reason,
            "blocking_reasons": list(self.blocking_reasons),
            "trust_reasons": list(self.trust_reasons),
            "expected_sample_sequence": self.expected_sample_sequence,
            "expected_source_epoch": self.expected_source_epoch,
        }


def build_decision_input_from_registers(
    registers: Mapping[str, Any] | None,
    *,
    expected_sample_sequence: int = 0,
    expected_source_epoch: str = "",
) -> DecisionInput:
    """Build one decision-facing input object from a register snapshot."""

    snapshot = registers if isinstance(registers, Mapping) else {}
    lanes: dict[str, DecisionInputLane] = {}
    blocking: list[str] = []
    trust: list[str] = []

    for name in DECISION_INPUT_LANES:
        raw = snapshot.get(name)
        lane = _lane_from_raw(
            name,
            raw,
            expected_sample_sequence=int(expected_sample_sequence or 0),
            expected_source_epoch=str(expected_source_epoch or ""),
        )
        lanes[name] = lane
        blocking.extend(_blocking_reasons(lane))
        trust.extend(_trust_reasons(lane))

    return DecisionInput(
        lanes=lanes,
        blocking_reasons=tuple(sorted(dict.fromkeys(blocking))),
        trust_reasons=tuple(sorted(dict.fromkeys(trust))),
        expected_sample_sequence=int(expected_sample_sequence or 0),
        expected_source_epoch=str(expected_source_epoch or ""),
    )


def _lane_from_raw(
    name: str,
    raw: Any,
    *,
    expected_sample_sequence: int,
    expected_source_epoch: str,
) -> DecisionInputLane:
    if hasattr(raw, "as_dict") and callable(raw.as_dict):
        raw = raw.as_dict()
    data = raw if isinstance(raw, Mapping) else {}
    if not data:
        return _missing_lane(name)
    declared_owner = str(RUNTIME_REGISTER_OWNER_MAP.get(name, "") or "")
    raw_owner = str(data.get("owner") or "")
    error = str(data.get("error") or "")
    if raw_owner and declared_owner and raw_owner != declared_owner:
        error = _append_error(error, f"owner_mismatch:{raw_owner}")
    value = data.get("value")
    value_row = value if isinstance(value, Mapping) else {}
    sample_sequence = _safe_int(value_row.get("sample_sequence"))
    source_epoch = str(value_row.get("source_epoch") or "")
    generation_match = True
    if name in PHYSICAL_GENERATION_LANES and (
        expected_sample_sequence > 0 or expected_source_epoch
    ):
        generation_match = bool(
            sample_sequence == expected_sample_sequence
            and source_epoch
            and source_epoch == expected_source_epoch
        )
        if not generation_match:
            error = _append_error(
                error,
                "generation_mismatch:"
                f"expected={expected_source_epoch}:{expected_sample_sequence}:"
                f"actual={source_epoch}:{sample_sequence}",
            )
    return DecisionInputLane(
        name=str(data.get("name") or name),
        value=value,
        valid=safe_bool(data.get("valid", False), False),
        stale=safe_bool(data.get("stale", False), False),
        dirty=safe_bool(data.get("dirty", False), False),
        trust=str(data.get("trust") or "unknown"),
        source_kind=str(data.get("source_kind") or "unknown"),
        validity=str(data.get("validity") or "unknown"),
        transition=safe_bool(data.get("transition", False), False),
        source=str(data.get("source") or ""),
        owner=declared_owner,
        error=error,
        sequence=_safe_int(data.get("sequence")),
        sample_sequence=sample_sequence,
        source_epoch=source_epoch,
        generation_match=generation_match,
    )


def _missing_lane(name: str) -> DecisionInputLane:
    return DecisionInputLane(
        name=name,
        owner=str(RUNTIME_REGISTER_OWNER_MAP.get(name, "") or ""),
        valid=False,
        stale=True,
        trust="unknown",
        source_kind="unknown",
        validity="stale",
        transition=False,
        error="missing_register",
    )


def _blocking_reasons(lane: DecisionInputLane) -> list[str]:
    reasons: list[str] = []
    if not lane.valid:
        reasons.append(f"{lane.name}:invalid")
    if lane.stale:
        reasons.append(f"{lane.name}:stale")
    if lane.dirty:
        reasons.append(f"{lane.name}:dirty")
    if lane.transition:
        reasons.append(f"{lane.name}:transition")
    if lane.validity in {"stale", "dirty", "unknown"}:
        reasons.append(f"{lane.name}:validity:{lane.validity}")
    if lane.source_kind in {"unsupported", "unknown"}:
        reasons.append(f"{lane.name}:source_kind:{lane.source_kind}")
    if lane.trust in {"unknown", "unsupported"}:
        reasons.append(f"{lane.name}:{lane.trust}")
    if lane.error:
        reasons.append(f"{lane.name}:error:{lane.error}")
    return reasons


def _trust_reasons(lane: DecisionInputLane) -> list[str]:
    reasons = [f"{lane.name}:trust:{lane.trust}"]
    if lane.source_kind:
        reasons.append(f"{lane.name}:source_kind:{lane.source_kind}")
    if lane.validity:
        reasons.append(f"{lane.name}:validity:{lane.validity}")
    if lane.transition:
        reasons.append(f"{lane.name}:transition:true")
    if lane.source:
        reasons.append(f"{lane.name}:source:{lane.source}")
    if lane.owner:
        reasons.append(f"{lane.name}:owner:{lane.owner}")
    return reasons


def _lane_severity(lane: DecisionInputLane) -> str:
    name = str(lane.name or "")
    if not lane.generation_match:
        return "fault"
    if name == REGISTER_FAULTS and (lane.dirty or lane.error):
        return "fault"
    if lane.error:
        return "fault" if name in {REGISTER_RADIO, REGISTER_HEALTH} else "stale"
    if lane.dirty or lane.transition or lane.trust in {"dirty", "transition"} or lane.validity == "dirty":
        return "transition"
    if not lane.valid or lane.stale or lane.trust == "stale" or lane.validity == "stale":
        return "stale"
    if lane.trust in {"unknown", "unsupported"} or lane.source_kind in {"unknown", "unsupported"} or lane.validity == "unknown":
        return "advisory"
    return "ready"


def _max_severity(left: str, right: str) -> str:
    return left if _SEVERITY_RANK.get(left, 0) >= _SEVERITY_RANK.get(right, 0) else right


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _append_error(existing: str, addition: str) -> str:
    if not existing:
        return addition
    if addition in existing.split(";"):
        return existing
    return f"{existing};{addition}"
