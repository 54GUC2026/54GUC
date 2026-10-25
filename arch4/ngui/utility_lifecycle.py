"""Present typed utility state from heavy-work and external-action results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from .external_actions import ExternalActionKind, ExternalActionResult
from .heavy_work import (
    HeavyWorkKind,
    HeavyWorkRegisterSnapshot,
    HeavyWorkTerminalReceipt,
)


class UtilityKind(str, Enum):
    SUPPORT_BUNDLE = "create_support_bundle"
    SCAN_NETWORK = "scan_network"
    BUY_LICENSE = "open_buy"
    LICENSE = "update_license"
    VERSION = "open_info"
    UPDATES = "check_updates"
    SITE_REPORT = "create_site_report"


class UtilityTerminalState(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


HEAVY_UTILITY_KINDS: Mapping[HeavyWorkKind, UtilityKind] = MappingProxyType(
    {
        HeavyWorkKind.SITE_REPORT: UtilityKind.SITE_REPORT,
        HeavyWorkKind.SUPPORT_BUNDLE: UtilityKind.SUPPORT_BUNDLE,
        HeavyWorkKind.SCAN: UtilityKind.SCAN_NETWORK,
    }
)

EXTERNAL_UTILITY_KINDS: Mapping[ExternalActionKind, UtilityKind] = MappingProxyType(
    {
        ExternalActionKind.OPEN_BUY: UtilityKind.BUY_LICENSE,
        ExternalActionKind.OPEN_INFO: UtilityKind.VERSION,
        ExternalActionKind.CHECK_UPDATES: UtilityKind.UPDATES,
        ExternalActionKind.SAVE_LICENSE: UtilityKind.LICENSE,
    }
)

UTILITY_LABELS: Mapping[UtilityKind, str] = MappingProxyType(
    {
        UtilityKind.SUPPORT_BUNDLE: "Support bundle",
        UtilityKind.SCAN_NETWORK: "Network scan",
        UtilityKind.BUY_LICENSE: "Buy license",
        UtilityKind.LICENSE: "License",
        UtilityKind.VERSION: "Version",
        UtilityKind.UPDATES: "Update check",
        UtilityKind.SITE_REPORT: "Site report",
    }
)


@dataclass(frozen=True, slots=True)
class NGUIUtilityTerminal:
    kind: UtilityKind
    state: UtilityTerminalState
    sequence: int
    completed_at: float
    result: Mapping[str, Any]
    message: str = ""
    request_id: str = ""

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("utility terminal sequence must be positive")
        if self.completed_at <= 0:
            raise ValueError("utility terminal timestamp must be positive")
        if self.kind in {
            UtilityKind.SUPPORT_BUNDLE,
            UtilityKind.SCAN_NETWORK,
            UtilityKind.SITE_REPORT,
        } and not self.request_id:
            raise ValueError("heavy utility terminal requires request identity")
        object.__setattr__(self, "result", _freeze_mapping(self.result))


def project_utility_lifecycle(
    heavy: HeavyWorkRegisterSnapshot,
    external: Mapping[ExternalActionKind, ExternalActionResult],
) -> Mapping[str, Mapping[str, Any]]:
    """Build current utility state without keeping a second result owner."""

    projected: dict[str, Mapping[str, Any]] = {}
    for heavy_kind, utility_kind in HEAVY_UTILITY_KINDS.items():
        progress = heavy.progress.get(heavy_kind)
        if progress is None:
            continue
        terminal = heavy.terminals.get(heavy_kind)
        terminal_matches = bool(
            terminal is not None and terminal.sequence == progress.sequence
        )
        request_id = terminal.request_id if terminal_matches else ""
        projected[utility_kind.value] = MappingProxyType(
            {
                "kind": utility_kind.value,
                "label": UTILITY_LABELS[utility_kind],
                "state": progress.state.value,
                "sequence": int(progress.sequence),
                "updated_at": float(progress.updated_at),
                "progress_pct": float(progress.progress_pct),
                "message": str(progress.message or ""),
                "request_id": request_id,
                "terminal_at": (
                    float(terminal.terminal_at)
                    if terminal_matches
                    else 0.0
                ),
                "result": (
                    _freeze_mapping(terminal.result)
                    if terminal_matches
                    else MappingProxyType({})
                ),
            }
        )
    for external_kind, utility_kind in EXTERNAL_UTILITY_KINDS.items():
        result = external.get(external_kind)
        if result is None:
            continue
        projected[utility_kind.value] = MappingProxyType(
            {
                "kind": utility_kind.value,
                "label": UTILITY_LABELS[utility_kind],
                "state": result.state.value,
                "sequence": int(result.sequence),
                "updated_at": float(result.completed_at),
                "progress_pct": (
                    100.0
                    if result.state.value
                    in {"completed", "failed", "rejected", "cancelled"}
                    else 0.0
                ),
                "message": str(result.message or ""),
                "request_id": result.request_id,
            }
        )
    return MappingProxyType(projected)


def terminal_from_external(
    result: ExternalActionResult,
    *,
    kind: UtilityKind,
) -> NGUIUtilityTerminal:
    if result.state.value not in {
        "completed",
        "failed",
        "rejected",
        "cancelled",
    }:
        raise ValueError("external utility result is not terminal")
    return NGUIUtilityTerminal(
        kind=kind,
        state=UtilityTerminalState(result.state.value),
        sequence=result.sequence,
        completed_at=result.completed_at,
        result=result.result,
        message=result.message,
        request_id=result.request_id,
    )


def terminal_from_heavy(
    receipt: HeavyWorkTerminalReceipt,
    *,
    kind: UtilityKind,
) -> NGUIUtilityTerminal:
    return NGUIUtilityTerminal(
        kind=kind,
        state=UtilityTerminalState(receipt.state.value),
        sequence=receipt.sequence,
        completed_at=receipt.terminal_at,
        result=receipt.result,
        message=receipt.message,
        request_id=receipt.request_id,
    )


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


__all__ = [
    "EXTERNAL_UTILITY_KINDS",
    "HEAVY_UTILITY_KINDS",
    "NGUIUtilityTerminal",
    "UTILITY_LABELS",
    "UtilityKind",
    "UtilityTerminalState",
    "project_utility_lifecycle",
    "terminal_from_external",
    "terminal_from_heavy",
]
