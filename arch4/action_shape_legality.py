"""Dependency-neutral legality owner for canonical action parameter shapes.

This module answers one question only: does a proposed cellular/traffic action
have a complete, unambiguous parameter shape?  It does not inspect live modem
state, capabilities, policy, safety, confirmation, or execution readiness.

The rules are mined from the exact vB5.3 commit
``9b4e2c60c21b41c803051ab273d3749a9aeb2e85`` manual-action normalization
and proposal-validity paths, then tightened only at the canonical internal
boundary (finite JSON values and typed booleans/numbers). New owners use them
before an action can enter a selection set; old runtime no-effect checks remain
separate until their callers are migrated. Inputs at this boundary are already
canonical Python values -- UI strings must be parsed before reaching it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from arch4.interfaces import LeverType
from arch4.core_contracts.command_targets import core_lte_pci


OWNER = "arch4.action_shape_legality"
SCHEMA = "CanonicalActionShapeLegality.v1"


@dataclass(frozen=True, slots=True)
class ActionShapeLegality:
    legal: bool
    code: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "legal": bool(self.legal),
            "code": str(self.code or ""),
            "reason": str(self.reason or ""),
        }


def validate_action_shape(
    lever: LeverType | str,
    params: Mapping[str, Any] | None,
) -> ActionShapeLegality:
    """Validate one already-normalized action without consulting runtime state."""

    lever_value = _lever(lever)
    if lever_value is None:
        return _invalid("unsupported_lever", "action lever is not canonical")
    if not isinstance(params, Mapping):
        return _invalid("params_not_mapping", "action params must be a mapping")
    row = dict(params)
    if row.get("_quarantined_non_json_mapping") is True:
        return _invalid(
            "params_not_canonical_json",
            "action params contain a non-canonical JSON value",
        )
    validators = {
        LeverType.RADIO_MODE: _radio_mode,
        LeverType.MOBILE_DATA: _mobile_data,
        LeverType.BAND_SELECTION: _band_selection,
        LeverType.BAND_LOCK: _band_lock,
        LeverType.PBAND: _pband,
        LeverType.CELL_LOCK: _cell_lock,
        LeverType.NR_BAND_LOCK: _nr_band_lock,
        LeverType.TRAFFIC_PRESENCE: _traffic_presence,
        LeverType.TRAFFIC_VOLUME: _traffic_volume,
        LeverType.UE_CAPABILITY: _ue_capability,
        LeverType.RESTART: _restart,
        LeverType.WIFI_RADIO: _wifi_radio,
        LeverType.NETWORK_SETTINGS: _network_settings,
    }
    validator = validators.get(lever_value)
    if validator is None:
        return _invalid("unsupported_lever", f"unsupported lever: {lever_value.name}")
    return validator(row)


def _radio_mode(params: dict[str, Any]) -> ActionShapeLegality:
    from arch4.core_contracts.command_targets import core_command_target_from_mapping
    fields = {"mode", "expected_mode", "lte_bands", "nr_bands", "band_selection_mode", "subscription_key", "profile_definition_hash"}
    try:
        core_command_target_from_mapping({"lever": "RADIO_MODE", "retry_delay_s": params.get("retry_delay_s", 1800.0), **{key: params.get(key) for key in fields}})
    except (TypeError, ValueError):
        return _invalid("radio_mode_target_invalid", "radio mode needs exact original mode, bands and subscription")
    return _valid()


def _mobile_data(params: dict[str, Any]) -> ActionShapeLegality:
    _enabled, error = _required_bool(params.get("enabled"), field="enabled")
    return error or _valid()


def _band_selection(params: dict[str, Any]) -> ActionShapeLegality:
    bands, error = _band_aliases(params, "bands", "resolved_target_bands")
    if error:
        return error
    auto, error = _optional_bool(params, "auto", default=False)
    if error:
        return error
    if auto and bands:
        return _invalid(
            "auto_with_explicit_bands", "auto release cannot include explicit bands"
        )
    if not auto and not bands:
        return _invalid(
            "band_selection_target_missing",
            "band selection requires positive bands or explicit auto release",
        )
    return _valid()


def _band_lock(params: dict[str, Any]) -> ActionShapeLegality:
    bands, error = _band_aliases(params, "bands", "resolved_target_bands")
    if error:
        return error
    auto, error = _optional_bool(params, "auto", default=False)
    if error:
        return error
    if auto:
        return _invalid("band_lock_auto", "band lock cannot request auto release")
    if len(bands) != 1:
        return _invalid(
            "band_lock_target_invalid", "band lock requires exactly one positive band"
        )
    return _valid()


def _pband(params: dict[str, Any]) -> ActionShapeLegality:
    if "pband" not in params:
        return _invalid("pband_missing", "PBAND requires pband")
    pband, error = _non_negative_int(params.get("pband"), field="pband")
    if error:
        return error
    pools: dict[str, tuple[int, ...]] = {}
    for key in ("ca_pool", "dl_bands"):
        if key not in params:
            continue
        values, item_error = _positive_bands(
            params.get(key), field=key, allow_empty=True
        )
        if item_error:
            return item_error
        pools[key] = values
    if pband == 0 and any(pools.values()):
        return _invalid(
            "pband_release_with_pool", "PBAND release cannot include CA bands"
        )
    ca_pool = pools.get("ca_pool", ())
    if pband > 0 and ca_pool and pband not in ca_pool:
        return _invalid(
            "pband_not_in_ca_pool", "PBAND anchor must be included in ca_pool"
        )
    return _valid()


def _cell_lock(params: dict[str, Any]) -> ActionShapeLegality:
    unlock, error = _optional_bool(params, "unlock", default=False)
    if error:
        return error
    earfcn, earfcn_error = _optional_positive_int(params, "earfcn")
    if earfcn_error:
        return earfcn_error
    if unlock:
        if params.get("pci") not in (None, 0) or earfcn > 0:
            return _invalid(
                "cell_unlock_with_target", "cell unlock conflicts with target cell"
            )
        return _valid()
    if params.get("pci") is None or earfcn <= 0:
        return _invalid(
            "cell_target_incomplete", "cell lock requires PCI (0 to 503) and a positive EARFCN"
        )
    try:
        core_lte_pci(params["pci"])
    except (TypeError, ValueError):
        return _invalid("pci_invalid", "PCI must be an integer from 0 to 503")
    if "band" in params:
        _band, band_error = _positive_int(params.get("band"), field="band")
        if band_error:
            return band_error
    for key in ("auto_unlock_after_s", "lock_duration_s"):
        if key not in params or params.get(key) in (None, ""):
            continue
        _value, timer_error = _non_negative_number(params.get(key), field=key)
        if timer_error:
            return timer_error
    return _valid()


def _nr_band_lock(params: dict[str, Any]) -> ActionShapeLegality:
    if "nr_bands" not in params:
        return _invalid(
            "nr_bands_missing", "NR band lock requires nr_bands; use [] for unlock"
        )
    _bands, error = _positive_bands(
        params.get("nr_bands"), field="nr_bands", allow_empty=True
    )
    return error or _valid()


def _traffic_presence(params: dict[str, Any]) -> ActionShapeLegality:
    if "keepalive_interval_s" not in params:
        return _invalid(
            "keepalive_interval_missing",
            "traffic presence requires keepalive_interval_s; use 0 to disable",
        )
    _interval, error = _non_negative_number(
        params.get("keepalive_interval_s"), field="keepalive_interval_s"
    )
    return error or _valid()


def _traffic_volume(params: dict[str, Any]) -> ActionShapeLegality:
    fields = ("background_mbps", "sqm_limit_mbps")
    present = [key for key in fields if key in params and params.get(key) is not None]
    if not present:
        return _invalid(
            "traffic_volume_target_missing",
            "traffic volume requires background_mbps or sqm_limit_mbps",
        )
    for key in present:
        _value, error = _non_negative_number(params.get(key), field=key)
        if error:
            return error
    return _valid()


def _ue_capability(params: dict[str, Any]) -> ActionShapeLegality:
    present = False
    for key in ("dl_256qam", "ul_64qam", "restart"):
        if key not in params or params.get(key) is None:
            continue
        present = True
        _value, error = _required_bool(params.get(key), field=key)
        if error:
            return error
    if not present:
        return _invalid(
            "ue_capability_target_missing",
            "UE capability requires dl_256qam, ul_64qam, or restart",
        )
    return _valid()


def _restart(params: dict[str, Any]) -> ActionShapeLegality:
    if "restart" in params:
        restart, error = _required_bool(params.get("restart"), field="restart")
        if error:
            return error
        if not restart:
            return _invalid(
                "restart_false", "RESTART cannot carry restart=false"
            )
    if "emergency_fast" in params:
        _flag, error = _required_bool(
            params.get("emergency_fast"), field="emergency_fast"
        )
        if error:
            return error
    return _valid()


def _wifi_radio(params: dict[str, Any]) -> ActionShapeLegality:
    public_fields = {key for key in params if not str(key).startswith("_")}
    if public_fields != {"band", "enabled"}:
        return _invalid(
            "wifi_radio_target_shape_invalid",
            "Wi-Fi radio requires exactly band and enabled",
        )
    if params.get("band") not in {"2.4ghz", "5ghz", "6ghz"}:
        return _invalid(
            "wifi_radio_band_invalid",
            "Wi-Fi radio band must be canonical",
        )
    _enabled, error = _required_bool(
        params.get("enabled"),
        field="enabled",
    )
    return error or _valid()


def _band_aliases(
    params: Mapping[str, Any], *keys: str
) -> tuple[tuple[int, ...], ActionShapeLegality | None]:
    present: list[tuple[str, tuple[int, ...]]] = []
    for key in keys:
        if key not in params:
            continue
        values, error = _positive_bands(params.get(key), field=key, allow_empty=True)
        if error:
            return (), error
        present.append((key, values))
    nonempty = [values for _key, values in present if values]
    if len({values for values in nonempty}) > 1:
        return (), _invalid(
            "band_alias_conflict", "band target aliases describe different targets"
        )
    return (nonempty[0] if nonempty else ()), None


def _positive_bands(
    value: Any, *, field: str, allow_empty: bool = False
) -> tuple[tuple[int, ...], ActionShapeLegality | None]:
    if not isinstance(value, (list, tuple)):
        return (), _invalid(f"{field}_not_list", f"{field} must be a list")
    if not value and not allow_empty:
        return (), _invalid(f"{field}_empty", f"{field} must not be empty")
    bands: list[int] = []
    for item in value:
        band, error = _positive_int(item, field=field)
        if error:
            return (), error
        if band not in bands:
            bands.append(band)
    return tuple(sorted(bands)), None


def _optional_positive_int(
    params: Mapping[str, Any], field: str
) -> tuple[int, ActionShapeLegality | None]:
    if field not in params or params.get(field) in (None, ""):
        return 0, None
    return _positive_int(params.get(field), field=field)


def _positive_int(
    value: Any, *, field: str
) -> tuple[int, ActionShapeLegality | None]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return 0, _invalid(
            f"{field}_not_positive_integer", f"{field} must be a positive integer"
        )
    return int(value), None


def _non_negative_int(
    value: Any, *, field: str
) -> tuple[int, ActionShapeLegality | None]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0, _invalid(
            f"{field}_not_non_negative_integer",
            f"{field} must be a non-negative integer",
        )
    return int(value), None


def _non_negative_number(
    value: Any, *, field: str
) -> tuple[float, ActionShapeLegality | None]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0, _invalid(
            f"{field}_not_numeric", f"{field} must be numeric"
        )
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        return 0.0, _invalid(
            f"{field}_not_non_negative", f"{field} must be finite and non-negative"
        )
    return number, None


def _optional_bool(
    params: Mapping[str, Any], field: str, *, default: bool
) -> tuple[bool, ActionShapeLegality | None]:
    if field not in params or params.get(field) is None:
        return bool(default), None
    return _required_bool(params.get(field), field=field)


def _required_bool(
    value: Any, *, field: str
) -> tuple[bool, ActionShapeLegality | None]:
    if not isinstance(value, bool):
        return False, _invalid(
            f"{field}_not_boolean", f"{field} must be a canonical boolean"
        )
    return bool(value), None


def _lever(value: LeverType | str) -> LeverType | None:
    if isinstance(value, LeverType):
        return value
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    try:
        return LeverType[text]
    except KeyError:
        return None


def _valid() -> ActionShapeLegality:
    return ActionShapeLegality(legal=True)


def _invalid(code: str, reason: str) -> ActionShapeLegality:
    return ActionShapeLegality(legal=False, code=str(code), reason=str(reason))


__all__ = [
    "ActionShapeLegality",
    "OWNER",
    "SCHEMA",
    "validate_action_shape",
]


def _network_settings(params: dict[str, Any]) -> ActionShapeLegality:
    from arch4.core_contracts.network_settings import network_target_from_mapping
    public = {key: value for key, value in params.items() if not str(key).startswith("_")}
    try:
        network_target_from_mapping({"lever": "NETWORK_SETTINGS", **public})
    except (ValueError, TypeError) as exc:
        return _invalid("network_settings_target_invalid", str(exc))
    return _valid()
