"""Typed cellular action builders used at the Modem Live command boundary."""

from __future__ import annotations

from dataclasses import dataclass

from arch4.action_admission import (
    ACTION_DESCRIPTORS,
    ActionExecutionBoundary,
    ActionFamily,
    ActionVerification,
)
from arch4.control.band_resolver import (
    PHYSICAL_ALL_BAND_RELEASE_CONFIRM_TOKEN,
    auto_band_release_rejection_reason,
    is_auto_band_release,
)
from arch4.action_shape_legality import validate_action_shape
from arch4.interfaces import LeverAction, LeverType



@dataclass(frozen=True)
class BandSelectionParams:
    bands: tuple[int, ...] = ()
    auto: bool = False

    def merge(self, raw: dict) -> dict:
        out = dict(raw or {})
        out["bands"] = list(self.bands)
        if self.auto:
            out["auto"] = True
        elif "auto" in out:
            out["auto"] = False
        return out


@dataclass(frozen=True)
class PBandParams:
    pband: int
    ca_pool: tuple[int, ...] = ()
    dl_bands: tuple[int, ...] = ()

    def merge(self, raw: dict) -> dict:
        out = dict(raw or {})
        out["pband"] = int(self.pband)
        if "ca_pool" in out or self.ca_pool:
            out["ca_pool"] = list(self.ca_pool)
        if "dl_bands" in out or self.dl_bands:
            out["dl_bands"] = list(self.dl_bands)
        return out


@dataclass(frozen=True)
class CellLockParams:
    unlock: bool = False
    pci: int = 0
    earfcn: int = 0
    band: int = 0
    auto_unlock_after_s: float | None = None
    lock_duration_s: float | None = None

    def merge(self, raw: dict) -> dict:
        out = dict(raw or {})
        out["unlock"] = bool(self.unlock)
        if self.unlock:
            out.pop("pci", None)
            out.pop("earfcn", None)
            out.pop("band", None)
        else:
            out["pci"] = int(self.pci)
            out["earfcn"] = int(self.earfcn)
            if self.band > 0 or "band" in out:
                out["band"] = int(self.band)
        if self.auto_unlock_after_s is not None:
            out["auto_unlock_after_s"] = float(self.auto_unlock_after_s)
        if self.lock_duration_s is not None:
            out["lock_duration_s"] = float(self.lock_duration_s)
        return out


def _positive_int(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _non_negative_float(value: object) -> tuple[bool, float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False, 0.0
    return number >= 0.0, number


def _bool_param(value: object, *, default: bool = False) -> tuple[bool, bool]:
    if value in (None, ""):
        return True, bool(default)
    if isinstance(value, bool):
        return True, value
    if isinstance(value, (int, float)) and value in (0, 1):
        return True, bool(value)
    text = str(value).strip().lower()
    if text in {"true", "yes", "1", "on"}:
        return True, True
    if text in {"false", "no", "0", "off"}:
        return True, False
    return False, False


def _positive_band_list(value: object) -> tuple[bool, list[int]]:
    if not isinstance(value, (list, tuple, set)):
        return False, []
    bands: list[int] = []
    for item in value:
        band = _positive_int(item)
        if band <= 0:
            return False, []
        if band not in bands:
            bands.append(band)
    return True, bands


def _canonical_manual_params(
    lever: LeverType,
    params: dict,
) -> tuple[bool, dict, str]:
    """Admit parsed manual input through the shared internal shape owner."""

    normalized = dict(params or {})
    verdict = validate_action_shape(lever, normalized)
    if not verdict.legal:
        return False, {}, str(verdict.reason or verdict.code or "action shape invalid")
    return True, normalized, ""


def normalize_manual_action_params(action: LeverAction) -> tuple[bool, dict, str]:
    params = action.params if isinstance(action.params, dict) else {}
    lever = action.lever
    if lever == LeverType.BAND_SELECTION:
        raw_bands = params.get("bands", [])
        ok, bands = _positive_band_list(raw_bands) if raw_bands else (True, [])
        if not ok:
            return False, {}, "band selection requires numeric positive bands"
        ok_auto, auto = _bool_param(params.get("auto", False))
        if not ok_auto:
            return False, {}, "band selection auto must be boolean"
        if auto and bands:
            return False, {}, "auto release cannot include explicit bands"
        if auto:
            reason = auto_band_release_rejection_reason(params)
            if reason:
                return False, {}, reason
        return _canonical_manual_params(
            lever,
            BandSelectionParams(bands=tuple(bands), auto=auto).merge(params),
        )
    if lever == LeverType.BAND_LOCK:
        ok_auto, auto = _bool_param(params.get("auto", False))
        if not ok_auto:
            return False, {}, "band lock auto must be boolean"
        if auto:
            return False, {}, "band lock cannot request auto release"
        ok, bands = _positive_band_list(params.get("bands", []))
        if not ok or len(bands) != 1:
            return False, {}, "band lock requires exactly one numeric positive band"
        return _canonical_manual_params(
            lever,
            BandSelectionParams(bands=tuple(bands), auto=False).merge(params),
        )
    if lever == LeverType.PBAND:
        try:
            pband = int(params.get("pband", 0) or 0)
        except (TypeError, ValueError):
            return False, {}, "PBAND requires a numeric pband"
        if pband < 0:
            return False, {}, "PBAND must be non-negative"
        normalized_lists: dict[str, list[int]] = {}
        for key in ("ca_pool", "dl_bands"):
            raw = params.get(key, [])
            if raw:
                ok, _bands = _positive_band_list(raw)
                if not ok:
                    return False, {}, f"{key} must contain only numeric positive bands"
                normalized_lists[key] = list(_bands)
        if pband <= 0 and (params.get("ca_pool") or params.get("dl_bands")):
            return False, {}, "PBAND release cannot include CA bands"
        return _canonical_manual_params(
            lever,
            PBandParams(
                pband=pband,
                ca_pool=tuple(normalized_lists.get("ca_pool", [])),
                dl_bands=tuple(normalized_lists.get("dl_bands", [])),
            ).merge(params),
        )
    if lever == LeverType.CELL_LOCK:
        ok_unlock, unlock = _bool_param(params.get("unlock", False))
        if not ok_unlock:
            return False, {}, "cell lock unlock must be boolean"
        earfcn = _positive_int(params.get("earfcn", 0))
        if unlock:
            if params.get("pci") not in (None, 0, "0", "") or earfcn > 0:
                return False, {}, "cell lock unlock conflicts with target cell"
            return _canonical_manual_params(
                lever, CellLockParams(unlock=True).merge(params)
            )
        if params.get("pci") in (None, "") or earfcn <= 0:
            return False, {}, "cell lock requires PCI (0 to 503) and a positive EARFCN"
        try:
            if isinstance(params["pci"], bool):
                raise ValueError("boolean PCI")
            pci = int(str(params["pci"]).strip())
        except (TypeError, ValueError):
            return False, {}, "PCI must be an integer from 0 to 503"
        band = 0
        if "band" in params and params.get("band") not in (None, "") and _positive_int(params.get("band")) <= 0:
            return False, {}, "cell lock band must be a numeric positive band"
        if "band" in params and params.get("band") not in (None, ""):
            band = _positive_int(params.get("band"))
        timers: dict[str, float] = {}
        for key in ("auto_unlock_after_s", "lock_duration_s"):
            if key in params and params.get(key) not in (None, ""):
                ok, _value = _non_negative_float(params.get(key))
                if not ok:
                    return False, {}, f"{key} must be non-negative"
                timers[key] = _value
        return _canonical_manual_params(
            lever,
            CellLockParams(
                unlock=False,
                pci=pci,
                earfcn=earfcn,
                band=band,
                auto_unlock_after_s=timers.get("auto_unlock_after_s"),
                lock_duration_s=timers.get("lock_duration_s"),
            ).merge(params),
        )
    if lever == LeverType.NR_BAND_LOCK:
        if "nr_bands" not in params:
            return False, {}, "nr band lock requires nr_bands; use [] for unlock"
        raw = params.get("nr_bands")
        if not isinstance(raw, (list, tuple, set)):
            return False, {}, "nr band lock requires nr_bands list"
        bands: list[int] = []
        if raw:
            ok, bands = _positive_band_list(raw)
            if not ok or not bands:
                return False, {}, "nr band lock requires numeric positive bands or explicit empty unlock"
        out = dict(params)
        out["nr_bands"] = list(bands)
        return _canonical_manual_params(lever, out)
    if lever in {LeverType.RESTART, LeverType.TRAFFIC_PRESENCE, LeverType.TRAFFIC_VOLUME, LeverType.UE_CAPABILITY}:
        return _canonical_manual_params(lever, dict(params))
    return False, {}, f"unsupported manual lever: {getattr(lever, 'name', lever)}"


def validate_manual_action_schema(action: LeverAction) -> tuple[bool, str]:
    ok, _params, reason = normalize_manual_action_params(action)
    return ok, reason


def build_authority_lever_action(
    *,
    lever: LeverType | str,
    params: dict | None = None,
    reason: str = "",
    intent: str = "",
    objective_id: str = "",
    source: str = "",
) -> LeverAction:
    """Build a LeverAction through the command/action authority module."""

    lever_value = lever if isinstance(lever, LeverType) else LeverType[str(lever or "").strip().upper()]
    action = LeverAction(
        lever=lever_value,
        params=dict(params or {}),
        reason=str(reason or ""),
        intent=str(intent or ""),
        objective_id=str(objective_id or ""),
    )
    if source:
        setattr(action, "source", str(source))
    return action


def _stamp_physical_all_band_release_confirmation(params: dict | None) -> dict:
    """Attach the private one-shot marker for a named physical release."""
    out = dict(params or {})
    if is_auto_band_release(out):
        out["auto_release_confirm"] = PHYSICAL_ALL_BAND_RELEASE_CONFIRM_TOKEN
    return out


def build_band_selection_translation_action(
    *,
    bands: list[int],
    auto: bool,
    reason: str,
    intent: str = "",
    objective_id: str = "",
    source: str = "band_selection_translation",
) -> LeverAction:
    params: dict[str, object] = {
        "bands": list(bands or []),
        "auto": bool(auto),
        "sequential": False,
    }
    if bool(auto) and not list(bands or []):
        params = _stamp_physical_all_band_release_confirmation(params)
    action = LeverAction(
        lever=LeverType.BAND_SELECTION,
        params=params,
        reason=str(reason or ""),
        intent=str(intent or ""),
        objective_id=str(objective_id or ""),
    )
    setattr(action, "source", str(source or "band_selection_translation"))
    return action


def build_pband_release_band_selection_action(
    *,
    reason: str,
    intent: str = "",
    objective_id: str = "",
    source: str = "pband_release_translation",
) -> LeverAction:
    """Translate a PBAND release into the canonical all-band auto-release action."""

    return build_band_selection_translation_action(
        bands=[],
        auto=True,
        reason=str(reason or "PBAND anchor release via auto band restore"),
        intent=intent,
        objective_id=objective_id,
        source=source,
    )
