from __future__ import annotations

from typing import Any

from arch4.interfaces import LeverAction
from arch4.policy.mikrotik_objective_policy import (
    check_mikrotik_explore_objective_block,
    is_mikrotik_cell_lock_objective,
    note_mikrotik_cell_lock_retry,
    note_mikrotik_explore_result,
)
from arch4.runtime_identity import runtime_backend_family_for_engine

ROUTEROS_AT_LIKE = "routeros_at_like"


def resolve_runtime_backend_family(engine: Any) -> str:
    return runtime_backend_family_for_engine(engine)


def _match_family(engine: Any, family: str | None = None) -> str:
    runtime_family = resolve_runtime_backend_family(engine)
    if family:
        return family if runtime_family == family else runtime_family
    return runtime_family


def check_family_explore_objective_block(
    engine: Any,
    *,
    action: LeverAction,
    now: float,
    family: str | None = None,
) -> tuple[bool, str]:
    family_name = _match_family(engine, family)
    if family_name == ROUTEROS_AT_LIKE:
        return check_mikrotik_explore_objective_block(engine, action=action, now=now)
    return False, ""


def note_family_explore_result(
    engine: Any,
    *,
    action: LeverAction,
    attached: bool,
    now: float,
    target_sig: str,
    family: str | None = None,
) -> None:
    family_name = _match_family(engine, family)
    if family_name == ROUTEROS_AT_LIKE:
        note_mikrotik_explore_result(
            engine,
            action=action,
            attached=attached,
            now=now,
            target_sig=target_sig,
        )


def is_family_cell_lock_objective(
    action: LeverAction,
    *,
    family: str | None = None,
) -> bool:
    family_name = str(family or "").strip().lower()
    if family_name == ROUTEROS_AT_LIKE:
        return is_mikrotik_cell_lock_objective(action)
    return False


def note_family_cell_lock_retry(
    engine: Any,
    *,
    action: LeverAction,
    objective_id: str,
    requested_pci: int,
    requested_earfcn: int,
    now: float,
    family: str | None = None,
) -> None:
    family_name = _match_family(engine, family)
    if family_name == ROUTEROS_AT_LIKE:
        note_mikrotik_cell_lock_retry(
            engine,
            action=action,
            objective_id=objective_id,
            requested_pci=requested_pci,
            requested_earfcn=requested_earfcn,
            now=now,
        )
