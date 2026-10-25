"""Backend-family priority-rotate helpers.

This module provides a vendor-neutral routing layer over family-specific rotate
policies. Legacy backend/model helpers remain in place, but new runtime code can
ask for family behavior first and keep backend-specific knowledge out of the
brain call sites.
"""

from __future__ import annotations

from typing import Any

from arch4.runtime_identity import runtime_backend_family_for_engine
from arch4.policy.hilink_rotate_policy import (
    allow_hilink_priority_rotate,
    hilink_rotation_has_confidence,
    note_hilink_priority_rotate_reverted,
    note_hilink_priority_rotate_stable,
)
from arch4.policy.mikrotik_rotate_policy import (
    allow_mikrotik_priority_rotate,
    mikrotik_priority_rotate_order,
    note_mikrotik_priority_rotate_reverted,
    note_mikrotik_priority_rotate_stable,
    record_mikrotik_priority_rotate_attempt,
)


HUAWEI_HILINK_LIKE = "huawei_hilink_like"
ROUTEROS_AT_LIKE = "routeros_at_like"


def resolve_runtime_backend_family(engine: Any) -> str:
    return runtime_backend_family_for_engine(engine)


def allow_family_priority_rotate(engine: Any, *, family: str | None = None, now: float, modem: Any = None, computed: Any = None) -> bool:
    fam = str(family or resolve_runtime_backend_family(engine) or "").strip().lower()
    if fam == HUAWEI_HILINK_LIKE:
        return allow_hilink_priority_rotate(engine, now=now)
    if fam == ROUTEROS_AT_LIKE and modem is not None and computed is not None:
        return allow_mikrotik_priority_rotate(engine, modem=modem, computed=computed, now=now)
    return False


def family_rotation_has_confidence(engine: Any, target_band: int, *, family: str | None = None) -> bool:
    fam = str(family or resolve_runtime_backend_family(engine) or "").strip().lower()
    if fam == HUAWEI_HILINK_LIKE:
        return hilink_rotation_has_confidence(engine, target_band)
    return False


def note_family_priority_rotate_stable(
    engine: Any,
    *,
    target_band: int,
    now: float,
    family: str | None = None,
) -> None:
    fam = str(family or resolve_runtime_backend_family(engine) or "").strip().lower()
    if fam == HUAWEI_HILINK_LIKE:
        note_hilink_priority_rotate_stable(engine, target_band=target_band, now=now)
        return
    if fam == ROUTEROS_AT_LIKE:
        note_mikrotik_priority_rotate_stable(engine, target_band=target_band, now=now)



def note_family_priority_rotate_reverted(
    engine: Any,
    *,
    target_band: int,
    current_band: int,
    now: float,
    family: str | None = None,
) -> None:
    fam = str(family or resolve_runtime_backend_family(engine) or "").strip().lower()
    if fam == HUAWEI_HILINK_LIKE:
        note_hilink_priority_rotate_reverted(
            engine,
            target_band=target_band,
            current_band=current_band,
            now=now,
        )
        return
    if fam == ROUTEROS_AT_LIKE:
        note_mikrotik_priority_rotate_reverted(
            engine,
            target_band=target_band,
            current_band=current_band,
            now=now,
        )


def family_priority_rotate_order(engine: Any, active_bands: list[int], current_primary: int, *, family: str | None = None) -> list[int]:
    fam = str(family or resolve_runtime_backend_family(engine) or "").strip().lower()
    if fam == ROUTEROS_AT_LIKE:
        return mikrotik_priority_rotate_order(active_bands, current_primary)
    return [band for band in active_bands if int(band) != int(current_primary or 0)] + [band for band in active_bands if int(band) == int(current_primary or 0)]


def record_family_priority_rotate_attempt(engine: Any, *, now: float, family: str | None = None) -> None:
    fam = str(family or resolve_runtime_backend_family(engine) or "").strip().lower()
    if fam == ROUTEROS_AT_LIKE:
        record_mikrotik_priority_rotate_attempt(engine, now=now)
