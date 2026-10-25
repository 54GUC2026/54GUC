"""Bounded admission owner for finite CGF jobs."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from types import MappingProxyType
from typing import Mapping


class ActiveJobAdmissionState(str, Enum):
    ADMITTED = "admitted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ActiveJobPolicy:
    max_active: int = 1
    max_queued: int = 0

    def __post_init__(self) -> None:
        if self.max_active != 1:
            raise ValueError("CGF finite-job lanes require exactly one active slot")
        if self.max_queued != 0:
            raise ValueError("CGF finite-job lanes prohibit hidden queued work")


@dataclass(frozen=True, slots=True)
class ActiveJobAdmissionReceipt:
    request_id: str
    lane_id: str
    created_at: float
    decided_at: float
    state: ActiveJobAdmissionState
    reason: str = ""

    @property
    def admitted(self) -> bool:
        return self.state is ActiveJobAdmissionState.ADMITTED


@dataclass(frozen=True, slots=True)
class ActiveJobTerminalReceipt:
    request_id: str
    lane_id: str
    terminal_state: str
    terminal_at: float


@dataclass(frozen=True, slots=True)
class ActiveJobAdmissionSnapshot:
    active_by_lane: Mapping[str, str]
    decisions_by_request: Mapping[str, ActiveJobAdmissionReceipt]
    terminals_by_request: Mapping[str, ActiveJobTerminalReceipt]


class ActiveJobAdmissionAuthority:
    """Owns finite-job admission and release for the whole composed process."""

    def __init__(
        self,
        policies: Mapping[str, ActiveJobPolicy],
        *,
        retained_terminal_limit: int = 512,
    ) -> None:
        normalized = {
            str(lane_id).strip(): policy for lane_id, policy in policies.items()
        }
        if not normalized or any(not lane_id for lane_id in normalized):
            raise ValueError("active-job lanes must be explicitly declared")
        if set(normalized.values()) != {ActiveJobPolicy()}:
            raise ValueError("active-job lanes require the canonical bounded policy")
        if retained_terminal_limit <= 0:
            raise ValueError("active-job terminal retention must be positive")
        self._policies = MappingProxyType(dict(normalized))
        self._retained_terminal_limit = int(retained_terminal_limit)
        self._lock = RLock()
        self._active_by_lane: dict[str, str] = {}
        self._decisions: OrderedDict[str, ActiveJobAdmissionReceipt] = OrderedDict()
        self._terminals: OrderedDict[str, ActiveJobTerminalReceipt] = OrderedDict()

    @property
    def declared_lanes(self) -> frozenset[str]:
        return frozenset(self._policies)

    def admit(
        self,
        *,
        request_id: str,
        lane_id: str,
        created_at: float,
        decided_at: float,
    ) -> ActiveJobAdmissionReceipt:
        request_key = str(request_id or "").strip()
        lane_key = str(lane_id or "").strip()
        if not request_key:
            raise ValueError("active-job request identity required")
        if lane_key not in self._policies:
            raise RuntimeError(f"undeclared active-job lane: {lane_key}")
        if created_at <= 0 or decided_at <= 0:
            raise ValueError("active-job timestamps must be positive")
        with self._lock:
            if request_key in self._decisions:
                raise RuntimeError("active-job request identity reused")
            current = self._active_by_lane.get(lane_key)
            if current is None:
                state = ActiveJobAdmissionState.ADMITTED
                reason = ""
                self._active_by_lane[lane_key] = request_key
            else:
                state = ActiveJobAdmissionState.REJECTED
                reason = f"active_job_inflight:{current}"
            receipt = ActiveJobAdmissionReceipt(
                request_id=request_key,
                lane_id=lane_key,
                created_at=float(created_at),
                decided_at=float(decided_at),
                state=state,
                reason=reason,
            )
            self._decisions[request_key] = receipt
            self._trim_completed_unlocked()
            return receipt

    def terminalize(
        self,
        *,
        request_id: str,
        lane_id: str,
        terminal_state: str,
        terminal_at: float,
    ) -> ActiveJobTerminalReceipt:
        request_key = str(request_id or "").strip()
        lane_key = str(lane_id or "").strip()
        state = str(terminal_state or "").strip().lower()
        if not request_key or not lane_key or not state:
            raise ValueError("active-job terminal identity and state required")
        if terminal_at <= 0:
            raise ValueError("active-job terminal timestamp must be positive")
        with self._lock:
            decision = self._decisions.get(request_key)
            if decision is None or decision.lane_id != lane_key:
                raise RuntimeError("active-job terminal lacks exact admission receipt")
            retained = self._terminals.get(request_key)
            candidate = ActiveJobTerminalReceipt(
                request_id=request_key,
                lane_id=lane_key,
                terminal_state=state,
                terminal_at=float(terminal_at),
            )
            if retained is not None:
                if retained != candidate:
                    raise RuntimeError("active-job terminal receipt replacement")
                return retained
            if decision.admitted:
                if self._active_by_lane.get(lane_key) != request_key:
                    raise RuntimeError("active-job terminal does not own active lane")
                del self._active_by_lane[lane_key]
            elif state != "rejected":
                raise RuntimeError("rejected active-job must terminalize as rejected")
            self._terminals[request_key] = candidate
            self._trim_completed_unlocked()
            return candidate

    def snapshot(self) -> ActiveJobAdmissionSnapshot:
        with self._lock:
            return ActiveJobAdmissionSnapshot(
                active_by_lane=MappingProxyType(dict(self._active_by_lane)),
                decisions_by_request=MappingProxyType(dict(self._decisions)),
                terminals_by_request=MappingProxyType(dict(self._terminals)),
            )

    def _trim_completed_unlocked(self) -> None:
        while len(self._terminals) > self._retained_terminal_limit:
            request_id, _terminal = self._terminals.popitem(last=False)
            self._decisions.pop(request_id, None)


def ngui_utility_job_policies(
    *,
    heavy_kinds: tuple[str, ...],
    external_kinds: tuple[str, ...],
) -> Mapping[str, ActiveJobPolicy]:
    lanes = {
        **{
            f"ngui.heavy.{str(kind)}": ActiveJobPolicy()
            for kind in heavy_kinds
        },
        **{
            f"ngui.external.{str(kind)}": ActiveJobPolicy()
            for kind in external_kinds
        },
    }
    return MappingProxyType(lanes)


__all__ = [
    "ActiveJobAdmissionAuthority",
    "ActiveJobAdmissionReceipt",
    "ActiveJobAdmissionSnapshot",
    "ActiveJobAdmissionState",
    "ActiveJobPolicy",
    "ActiveJobTerminalReceipt",
    "ngui_utility_job_policies",
]
