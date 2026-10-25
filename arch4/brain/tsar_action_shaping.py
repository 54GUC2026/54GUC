"""TSAR candidate action shaping helpers."""

from __future__ import annotations

from typing import Any, Callable, Iterable

from arch4.brain.band_action_learning_guard import band_blacklisted
from arch4.runtime_band_scope import normalize_runtime_band_values, remembered_runtime_bands


SERBIA_PROVIDER_PLMNS = {"22001", "22003", "22005"}
SERBIA_PROVIDER_NAMES = ("mts", "yettel", "serbia", "srbija")
SERBIA_HIGH_UE_SAFE_LTE_CA_POOL = (1, 3, 7, 20, 28)
SERBIA_LOW_UE_SAFE_LTE_CA_POOL = (1, 3, 20)
LOW_UE_CATEGORIES = {6, 7, 13}


def tsar_authority_band_list(*, memory: Any, cfg: Any) -> list[int]:
    """Band universe TSAR may use for automatic candidates."""

    remembered = normalize_runtime_band_values(remembered_runtime_bands(memory))
    if remembered:
        return remembered
    cfg_bands = normalize_runtime_band_values(getattr(cfg, "deployed_bands", None))
    if cfg_bands:
        return cfg_bands
    return []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _ue_category_value(*sources: Any) -> int:
    for source in sources:
        try:
            category = int(str(source or "0").strip())
        except (TypeError, ValueError):
            category = 0
        if category > 0:
            return category
    return 0


def _serbia_provider_context(provider_plmn: str = "", provider_name: str = "") -> bool:
    plmn = _text(provider_plmn)
    if plmn in SERBIA_PROVIDER_PLMNS:
        return True
    name = _text(provider_name).lower()
    return any(token in name for token in SERBIA_PROVIDER_NAMES)


def _serbia_safe_ca_pool_for_ue(ue_category: int) -> list[int]:
    if ue_category in LOW_UE_CATEGORIES:
        return list(SERBIA_LOW_UE_SAFE_LTE_CA_POOL)
    if ue_category >= 19:
        return list(SERBIA_HIGH_UE_SAFE_LTE_CA_POOL)
    return list(SERBIA_LOW_UE_SAFE_LTE_CA_POOL)


def provider_ue_safe_ca_pool(
    *,
    provider_plmn: str = "",
    provider_name: str = "",
    ue_category: Any = 0,
) -> list[int]:
    """Maximum automatic TSAR CA pool allowed for a provider/router context."""

    category = _ue_category_value(ue_category)
    if _serbia_provider_context(provider_plmn, provider_name):
        return _serbia_safe_ca_pool_for_ue(category)
    return []


def current_anchor_pool(modem: Any) -> list[int]:
    """Best current radio pool estimate, without adding authority bands."""

    for values in (
        getattr(modem.identity, 'configured_band_pool', []),
        getattr(modem.identity, 'configured_band_pool_signature', ""),
        getattr(modem.cellular, 'selected_band_set', []),
        getattr(modem.cellular, 'band_set', []),
        getattr(modem.cellular, 'active_band_set', []),
        getattr(modem.cellular, 'ca_band_signature', ""),
        [getattr(modem.cellular, 'primary_band', 0)],
    ):
        bands = normalize_runtime_band_values(values)
        if bands:
            return sorted(set(bands))
    return []


def current_selected_ca_pool(modem: Any) -> list[int]:
    """Canonical current selected CA pool for PBAND-only anchor changes."""

    return sorted(set(normalize_runtime_band_values(getattr(modem.cellular, 'selected_band_set', []))))


def preserved_ca_pool_for_pband_anchor(
    *,
    modem: Any,
    anchor_band: int,
    memory: Any = None,
    restrict_filter: Callable[[list[int]], Iterable[int]] | None = None,
) -> list[int]:
    """Keep the current CA pool for a PBAND change, adding only the PBAND."""

    try:
        anchor = int(anchor_band or 0)
    except (TypeError, ValueError):
        anchor = 0
    if anchor <= 0:
        return []

    if band_blacklisted(memory, anchor):
        return []

    pool = list(current_selected_ca_pool(modem))
    if not pool:
        return []
    if anchor not in pool:
        pool.append(anchor)

    if restrict_filter is not None:
        try:
            pool = normalize_runtime_band_values(restrict_filter(pool))
        except Exception:
            pass

    if anchor not in pool:
        return []
    return sorted(set(pool))


def ca_flap_recovery_ca_pool_for_anchor(
    *,
    modem: Any,
    anchor_band: int,
    memory: Any,
    authority_bands: Iterable[int],
    deployed_bands: Iterable[int],
    cfg_deployed_bands: Any,
    provider_plmn: str = "",
    provider_name: str = "",
    ue_category: Any = 0,
    widen_to_capability: bool = False,
    restrict_filter: Callable[[list[int]], Iterable[int]] | None = None,
) -> list[int]:
    """Build CA-flap recovery pool without using broad global masks by default."""

    try:
        anchor = int(anchor_band or 0)
    except (TypeError, ValueError):
        anchor = 0
    if anchor <= 0:
        return []

    if band_blacklisted(memory, anchor):
        return []

    safe_provider_pool = provider_ue_safe_ca_pool(
        provider_plmn=provider_plmn or getattr(modem.identity, 'provider_plmn', ""),
        provider_name=provider_name or getattr(modem.identity, 'provider_name', ""),
        ue_category=_ue_category_value(ue_category, 0),
    )
    safe_provider_set = set(safe_provider_pool)
    if safe_provider_set and anchor not in safe_provider_set:
        return []

    pool: list[int] = []

    def add(values: Any) -> None:
        for band in normalize_runtime_band_values(values):
            if band <= 0 or band in pool:
                continue
            if safe_provider_set and band not in safe_provider_set:
                continue
            if band_blacklisted(memory, band):
                continue
            pool.append(int(band))

    add(current_anchor_pool(modem))
    add([anchor])
    if widen_to_capability:
        before_widen = list(pool)
        if safe_provider_pool:
            add(safe_provider_pool)
        elif not pool:
            add(getattr(modem.identity, 'configured_band_pool', []))
            add(getattr(modem.identity, 'configured_band_pool_signature', ""))
            add(authority_bands)
            add(deployed_bands)
            add(cfg_deployed_bands)
        if len(before_widen) < 4:
            missing = [band for band in pool if band not in set(before_widen)]
            if missing:
                pool = [*before_widen, missing[0]]

    if restrict_filter is not None:
        try:
            pool = normalize_runtime_band_values(restrict_filter(pool))
        except Exception:
            pass

    if anchor not in pool:
        return []
    return sorted(set(pool))


def largest_safe_ca_pool_for_anchor(
    *,
    modem: Any,
    anchor_band: int,
    memory: Any,
    authority_bands: Iterable[int],
    deployed_bands: Iterable[int],
    cfg_deployed_bands: Any,
    provider_plmn: str = "",
    provider_name: str = "",
    ue_category: Any = 0,
    restrict_filter: Callable[[list[int]], Iterable[int]] | None = None,
) -> list[int]:
    """Build the widest safe CA pool for a deliberate PBAND anchor."""

    try:
        anchor = int(anchor_band or 0)
    except (TypeError, ValueError):
        anchor = 0
    if anchor <= 0:
        return []

    if band_blacklisted(memory, anchor):
        return []

    safe_provider_pool = provider_ue_safe_ca_pool(
        provider_plmn=provider_plmn or getattr(modem.identity, 'provider_plmn', ""),
        provider_name=provider_name or getattr(modem.identity, 'provider_name', ""),
        ue_category=_ue_category_value(ue_category, 0),
    )
    safe_provider_set = set(safe_provider_pool)
    if safe_provider_set and anchor not in safe_provider_set:
        return []

    pool: list[int] = []

    def add(values: Any) -> None:
        for band in normalize_runtime_band_values(values):
            if band <= 0 or band in pool:
                continue
            if safe_provider_set and band not in safe_provider_set:
                continue
            if band_blacklisted(memory, band):
                continue
            pool.append(int(band))

    add(getattr(modem.identity, 'configured_band_pool', []))
    add(getattr(modem.identity, 'configured_band_pool_signature', ""))
    add(authority_bands)
    if safe_provider_pool:
        add(safe_provider_pool)
    else:
        add(getattr(modem.cellular, 'selected_band_set', []))
        add(deployed_bands)
        add(cfg_deployed_bands)
    add(getattr(modem.cellular, 'band_set', []))
    add(getattr(modem.cellular, 'active_band_set', []))
    add(getattr(modem.cellular, 'ca_band_signature', ""))
    add([anchor])

    if restrict_filter is not None:
        try:
            pool = normalize_runtime_band_values(restrict_filter(pool))
        except Exception:
            pass

    if anchor not in pool:
        return []
    return sorted(set(pool))
