from __future__ import annotations

from typing import Any, Iterable, Mapping

from arch4.lte_mcs import canonical_qam_label_from_value_or_mcs


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, "", 0, "0", "false", "False", "no", "No"):
        return False
    return bool(value)


def _safe_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _unsupported_fields(
    *,
    unsupported_fields: Iterable[Any] | None = None,
    field_trust: Mapping[str, Any] | None = None,
) -> frozenset[str]:
    unsupported: set[str] = set()
    if isinstance(unsupported_fields, (list, tuple, set, frozenset)):
        unsupported.update(str(field) for field in unsupported_fields if str(field or "").strip())
    if isinstance(field_trust, Mapping):
        for field, trust in field_trust.items():
            if str(trust or "").strip().lower() in {"unsupported", "non_useful"}:
                unsupported.add(str(field or ""))
    return frozenset(field for field in unsupported if field)


def _raw_int_alias(raw: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in raw:
            value = _safe_int_or_none(raw.get(key))
            if value is not None:
                return value
    return None


def _scheduler_zero_authoritative(raw: Mapping[str, Any], unsupported: frozenset[str]) -> bool:
    explicit = raw.get("scheduler_zero_authoritative")
    if explicit not in (None, ""):
        return _bool(explicit)
    if not _bool(raw.get("radio_connected")):
        return False
    scheduler_fields = {"cqi", "cqi0", "cqi1", "mcs_dl0", "mcs_dl1", "mcs_ul", "qam_dl0", "qam_dl1", "qam_ul"}
    if scheduler_fields.issubset(set(unsupported)):
        return False
    for key_group in (
        ("ri",),
        ("mcs_dl0", "mcs_dl"),
        ("mcs_dl1",),
        ("mcs_ul",),
        ("cqi0", "cqi"),
        ("cqi1",),
    ):
        field = key_group[0]
        if field in unsupported:
            continue
        value = _raw_int_alias(raw, *key_group)
        if value is not None and value > 0:
            return True
    for field in ("qam_dl0", "qam_dl1", "qam_ul"):
        if field in unsupported:
            continue
        text = _text(raw.get(field))
        if text and text.lower() not in {"unknown", "none", "n/a", "na", "0", "0.0"}:
            return True
    source = _text(raw.get("scheduler_source") or raw.get("cqi_source")).lower()
    return source not in {"", "unknown", "none", "warming"}


def _stream1_available(raw: Mapping[str, Any], unsupported: frozenset[str]) -> bool:
    explicit = raw.get("scheduler_stream1_available")
    if explicit not in (None, ""):
        return _bool(explicit)
    ri = _raw_int_alias(raw, "ri") or 0
    if ri == 1:
        return False
    if all(field in unsupported for field in ("mcs_dl1", "cqi1", "qam_dl1")):
        return False
    for key in ("mcs_dl1", "cqi1"):
        if key in unsupported:
            continue
        value = _raw_int_alias(raw, key) or 0
        if value > 0:
            return True
    qam1 = _text(raw.get("qam_dl1"))
    return bool(qam1 and qam1.lower() not in {"unknown", "none", "n/a", "na", "0", "0.0"})


def _ri_raw_value(raw: Mapping[str, Any]) -> int | None:
    if raw.get("ri_valid") is False:
        return None
    return _raw_int_alias(raw, "ri")


def build_scheduler_display_contract(
    raw: Mapping[str, Any] | None,
    *,
    unsupported_fields: Iterable[Any] | None = None,
    field_trust: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    raw_map = raw if isinstance(raw, Mapping) else {}
    unsupported = _unsupported_fields(
        unsupported_fields=unsupported_fields,
        field_trust=field_trust,
    )
    return {
        "scheduler_zero_authoritative": bool(_scheduler_zero_authoritative(raw_map, unsupported)),
        "scheduler_stream1_available": bool(_stream1_available(raw_map, unsupported)),
        "scheduler_ri_raw": _ri_raw_value(raw_map),
        "scheduler_ri_valid": bool(raw_map.get("ri_valid")) if "ri_valid" in raw_map else (_ri_raw_value(raw_map) or 0) > 0,
        "scheduler_ri_source": _text(raw_map.get("ri_source")),
        "scheduler_mcs_dl0_raw": _raw_int_alias(raw_map, "mcs_dl0", "mcs_dl"),
        "scheduler_mcs_dl1_raw": _raw_int_alias(raw_map, "mcs_dl1"),
        "scheduler_mcs_ul_raw": _raw_int_alias(raw_map, "mcs_ul"),
        "scheduler_cqi0_raw": _raw_int_alias(raw_map, "cqi0", "cqi"),
        "scheduler_cqi1_raw": _raw_int_alias(raw_map, "cqi1"),
        "scheduler_qam0_raw": raw_map.get("qam_dl0", raw_map.get("qam_dl")),
        "scheduler_qam1_raw": raw_map.get("qam_dl1"),
        "scheduler_qam_ul_raw": raw_map.get("qam_ul"),
        "scheduler_unsupported_fields": sorted(str(field) for field in unsupported if str(field or "").strip()),
    }


def _display_int(value: int | None, *, zero_authoritative: bool) -> int | None:
    if value is None:
        return None
    if value == 0 and not zero_authoritative:
        return None
    return int(value)


def modulation_efficiency_from_qam(qam_value: Any) -> int | None:
    """Return modulation efficiency from the already-resolved QAM surface.

    The old GUI contract was MCS -> ME -> QAM.  The canonical owner now also
    accepts native QAM when a modem does not expose MCS, while keeping the
    visible ME and QAM values inseparable.
    """

    qam = str(qam_value or "").strip().upper()
    return {
        "QPSK": 2,
        "16QAM": 4,
        "64QAM": 6,
        "256QAM": 8,
    }.get(qam)


def _display_qam(
    raw_value: Any,
    mcs_value: int | None,
    *,
    zero_authoritative: bool,
) -> str:
    if raw_value not in (None, "") and str(raw_value).strip().lower() in {"unknown", "none", "n/a", "na"}:
        return ""
    if raw_value in (None, "") or str(raw_value).strip().lower() in {"0", "0.0"}:
        mcs = _safe_int_or_none(mcs_value)
        if mcs is None:
            return ""
        if mcs == 0 and not zero_authoritative:
            return ""
        return str(canonical_qam_label_from_value_or_mcs("", mcs) or "")
    return str(canonical_qam_label_from_value_or_mcs(raw_value, _safe_int_or_none(mcs_value) or 0) or "")


def build_scheduler_display_surface(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    contract_map = contract if isinstance(contract, Mapping) else {}
    unsupported = _unsupported_fields(
        unsupported_fields=contract_map.get("scheduler_unsupported_fields"),
    )

    zero_authoritative = _bool(contract_map.get("scheduler_zero_authoritative"))
    stream1_available = _bool(contract_map.get("scheduler_stream1_available"))

    mcs_dl0_raw = _safe_int_or_none(contract_map.get("scheduler_mcs_dl0_raw"))
    mcs_dl1_raw = _safe_int_or_none(contract_map.get("scheduler_mcs_dl1_raw"))
    mcs_ul_raw = _safe_int_or_none(contract_map.get("scheduler_mcs_ul_raw"))
    cqi0_raw = _safe_int_or_none(contract_map.get("scheduler_cqi0_raw"))
    cqi1_raw = _safe_int_or_none(contract_map.get("scheduler_cqi1_raw"))
    ri_raw = _safe_int_or_none(contract_map.get("scheduler_ri_raw"))
    ri_valid = _bool(contract_map.get("scheduler_ri_valid"))
    ri_source = _text(contract_map.get("scheduler_ri_source"))

    mcs_dl0 = None if "mcs_dl0" in unsupported else _display_int(mcs_dl0_raw, zero_authoritative=zero_authoritative)
    mcs_dl1 = None if "mcs_dl1" in unsupported or not stream1_available else _display_int(mcs_dl1_raw, zero_authoritative=zero_authoritative)
    mcs_ul = None if "mcs_ul" in unsupported else _display_int(mcs_ul_raw, zero_authoritative=zero_authoritative)
    cqi0 = None if "cqi0" in unsupported and "cqi" in unsupported else _display_int(cqi0_raw, zero_authoritative=zero_authoritative)
    cqi1 = None if "cqi1" in unsupported or not stream1_available else _display_int(cqi1_raw, zero_authoritative=zero_authoritative)
    ri = _display_int(ri_raw, zero_authoritative=zero_authoritative)

    qam0 = "" if "qam_dl0" in unsupported else _display_qam(contract_map.get("scheduler_qam0_raw"), mcs_dl0_raw, zero_authoritative=zero_authoritative)
    qam1 = "" if "qam_dl1" in unsupported or not stream1_available else _display_qam(contract_map.get("scheduler_qam1_raw"), mcs_dl1_raw, zero_authoritative=zero_authoritative)
    qam_ul = "" if "qam_ul" in unsupported else _display_qam(contract_map.get("scheduler_qam_ul_raw"), mcs_ul_raw, zero_authoritative=zero_authoritative)

    return {
        "scheduler_zero_authoritative": bool(zero_authoritative),
        "scheduler_stream1_available": bool(stream1_available),
        "scheduler_ri_valid": bool(ri_valid and ri is not None),
        "scheduler_ri_source": ri_source if ri is not None else "",
        "scheduler_ri_display": ri,
        "scheduler_mcs_dl0_display": mcs_dl0,
        "scheduler_mcs_dl1_display": mcs_dl1,
        "scheduler_mcs_ul_display": mcs_ul,
        "scheduler_cqi0_display": cqi0,
        "scheduler_cqi1_display": cqi1,
        "scheduler_me0_display": modulation_efficiency_from_qam(qam0),
        "scheduler_me1_display": modulation_efficiency_from_qam(qam1),
        "scheduler_me_ul_display": modulation_efficiency_from_qam(qam_ul),
        "scheduler_qam0_display": qam0,
        "scheduler_qam1_display": qam1,
        "scheduler_qam_ul_display": qam_ul,
    }
