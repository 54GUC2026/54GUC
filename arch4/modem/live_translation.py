"""Canonical modem-live materialization boundary.

Backends may expose the same physical modem fact under different native names.
The alias registry and value-language owner is
``arch4.modem_live.canonical_aliases``.  This boundary consumes that registry
to materialize typed ``ModemInput`` and radio/status surfaces; it must not
declare a second alias dictionary.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping
import logging
import math
import re

from arch4.coerce import safe_float as _safe_float, safe_int as _safe_int, safe_getattr as _safe_getattr
from arch4.interfaces import ModemInput, band_signature, normalize_band_list, parse_band_signature
from arch4.lte_mcs import canonical_qam_label, canonical_qam_label_from_value_or_mcs
from arch4.modem.catalog import (
    export_field_trust_from_overlay,
    export_field_trust_from_profile,
    firmware_profile_by_id,
)
from arch4.modem_live.canonical_aliases import CANONICAL_LIVE_FIELD_ALIASES


log = logging.getLogger("arch4.modem.live_translation")


SCHEDULER_TRUST_FIELDS: tuple[str, ...] = (
    "cqi",
    "cqi0",
    "cqi1",
    "mcs_dl0",
    "mcs_dl1",
    "mcs_ul",
    "qam_dl0",
    "qam_dl1",
    "qam_ul",
    "ri",
    "trans_mode",
)


def canonical_neighbor_rows(value: Any) -> list[dict[str, Any]]:
    """Translate typed/backend neighbor rows into strict Matrix language."""

    if not isinstance(value, (list, tuple)):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        source = (
            item
            if isinstance(item, Mapping)
            else {
                field: getattr(item, field, None)
                for field in (
                    "pci",
                    "band",
                    "earfcn",
                    "rsrp_dbm",
                    "rsrq_db",
                )
            }
        )
        if source.get("pci") is None:
            continue
        pci = _safe_int(source.get("pci"), -1)
        if pci < 0 or pci > 1007:
            continue
        row: dict[str, Any] = {"pci": pci}
        band = _safe_int(source.get("band"), 0)
        if band > 0:
            row["band"] = band
        earfcn = _safe_int(source.get("earfcn"), 0)
        if earfcn > 0:
            row["earfcn"] = earfcn
        rsrp = _safe_float(source.get("rsrp_dbm"), float("nan"))
        if math.isfinite(rsrp) and -200.0 <= rsrp < 0.0:
            row["rsrp_dbm"] = rsrp
        rsrq = _safe_float(source.get("rsrq_db"), float("nan"))
        if math.isfinite(rsrq) and -100.0 <= rsrq < 0.0:
            row["rsrq_db"] = rsrq
        rows.append(row)
    return rows


def _single_observed_active_band(modem: ModemInput) -> int:
    """Recover serving-band only from observed active carrier evidence.

    Configured/selected band pools are modem configuration, not PBAND truth.
    This helper intentionally ignores them.
    """

    bands: list[int] = []
    try:
        bands = normalize_band_list(list(getattr(modem, "active_band_set", []) or []))
    except Exception:
        bands = []
    if len(bands) == 1:
        try:
            band = int(bands[0])
            return band if band > 0 else 0
        except Exception:
            return 0
    return 0


_NUMERIC_CANONICAL_FIELDS = {
    "serving_band",
    "pci",
    "earfcn_dl",
    "earfcn_ul",
    "enb_id",
    "cell_id",
    "tac",
    "sinr_db",
    "rsrp_dbm",
    "rsrq_db",
    "rssi_dbm",
    "cqi",
    "cqi0",
    "cqi1",
    "mcs_dl",
    "mcs_dl0",
    "mcs_dl1",
    "mcs_ul",
    "ri",
    "dl_bandwidth_mhz",
    "ul_bandwidth_mhz",
    "dl_freq_mhz",
    "ul_freq_mhz",
}

_BAND_LIST_CANONICAL_FIELDS = {"configured_band_pool"}

_TRAFFIC_INT_FIELDS: tuple[str, ...] = (
    "traffic_day_download_bytes",
    "traffic_day_upload_bytes",
    "traffic_day_used_bytes",
    "traffic_session_download_bytes",
    "traffic_session_upload_bytes",
    "traffic_month_download_bytes",
    "traffic_month_upload_bytes",
    "traffic_total_download_bytes",
    "traffic_total_upload_bytes",
)

_TRAFFIC_FLOAT_FIELDS: tuple[str, ...] = (
    "traffic_current_download_rate_bps",
    "traffic_current_upload_rate_bps",
    "traffic_session_duration_s",
    "traffic_day_duration_s",
    "traffic_month_duration_s",
    "traffic_total_duration_s",
    "traffic_volume_poll_interval_s",
    "traffic_volume_last_sample_ts",
    "traffic_volume_next_sample_ts",
)

_TRAFFIC_OPTIONAL_INT_FIELDS: tuple[str, ...] = (
    "traffic_device_download_counter_bytes",
    "traffic_device_upload_counter_bytes",
    "traffic_quota_start_day",
    "traffic_quota_left_bytes",
    "traffic_quota_limit_bytes",
)

_TRAFFIC_OPTIONAL_FLOAT_FIELDS: tuple[str, ...] = (
    "traffic_counter_period_started_at",
    "traffic_counter_interface_started_at",
    "traffic_volume_age_s",
    "traffic_quota_month_threshold_pct",
    "traffic_quota_day_threshold_pct",
)

_TRAFFIC_TEXT_FIELDS: tuple[str, ...] = (
    "traffic_rate_source",
    "traffic_accounting_source",
    "traffic_accounting_status",
    "traffic_accounting_month_key",
    "traffic_accounting_day_key",
    "traffic_quota_limit_label",
)


def _first_positive_int(*values: Any) -> int:
    for value in values:
        parsed = _safe_int(value, 0)
        if parsed > 0:
            return parsed
    return 0


def _first_positive_base_int(base: Mapping[str, Any] | None, canonical_key: str) -> int:
    if not isinstance(base, Mapping):
        return 0
    aliases = CANONICAL_LIVE_FIELD_ALIASES.get(canonical_key, (canonical_key,))
    return _first_positive_int(*(base.get(alias) for alias in aliases))


def _first_positive_modem_base_int(
    modem: Any,
    base: Mapping[str, Any] | None,
    canonical_key: str,
    *modem_keys: str,
) -> int:
    modem_values = [_safe_getattr(modem, key, 0) for key in modem_keys]
    return _first_positive_int(*modem_values, _first_positive_base_int(base, canonical_key))


def _best_identity_text(modem: Any, *keys: str) -> str:
    for key in keys:
        try:
            value = getattr(modem, key, None)
        except Exception:
            value = None
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _non_empty_text(value: Any) -> bool:
    return bool(str(value or "").strip())


def _has_modem_traffic_counter_evidence(modem: ModemInput) -> bool:
    for key in _TRAFFIC_INT_FIELDS + _TRAFFIC_FLOAT_FIELDS:
        if _safe_float(_safe_getattr(modem, key, 0.0), 0.0) != 0.0:
            return True
    for key in _TRAFFIC_OPTIONAL_INT_FIELDS + _TRAFFIC_OPTIONAL_FLOAT_FIELDS:
        if _safe_getattr(modem, key, None) is not None:
            return True
    for key in _TRAFFIC_TEXT_FIELDS:
        if _non_empty_text(_safe_getattr(modem, key, "")):
            return True
    packet_connected = _safe_getattr(modem, "packet_session_connected", None)
    return isinstance(packet_connected, bool)


def _apply_traffic_counter_surface(
    out: dict[str, Any],
    *,
    modem: ModemInput,
    raw_base_status: Mapping[str, Any] | None,
) -> None:
    """Project modem-owned traffic counters without erasing cached truth.

    Some Huawei HiLink-like backends expose volume counters through the modem status
    cache, while MikroTik owns hourly accounting in ModemLive. A freshly built
    ModemInput may still carry default zero values, so zero/default modem fields
    are not allowed to overwrite a nonzero cached status value.
    """

    base = raw_base_status or {}
    modem_has_evidence = _has_modem_traffic_counter_evidence(modem)

    def _should_project(key: str, value: Any) -> bool:
        if modem_has_evidence:
            return True
        if key not in base:
            return False
        base_value = base.get(key)
        if isinstance(base_value, str):
            return bool(base_value.strip())
        return base_value is not None

    for key in _TRAFFIC_INT_FIELDS:
        value = _safe_getattr(modem, key, None)
        if _should_project(key, value):
            modem_value = _safe_int(value, 0)
            base_value = _safe_int(base.get(key, 0), 0)
            field_has_evidence = value is not None and (modem_value != 0 or key not in base)
            out[key] = modem_value if field_has_evidence else base_value

    for key in _TRAFFIC_FLOAT_FIELDS:
        value = _safe_getattr(modem, key, None)
        if _should_project(key, value):
            modem_value = _safe_float(value, 0.0)
            base_value = _safe_float(base.get(key, 0.0), 0.0)
            field_has_evidence = value is not None and (modem_value != 0.0 or key not in base)
            out[key] = modem_value if field_has_evidence else base_value

    for key in _TRAFFIC_OPTIONAL_INT_FIELDS:
        value = _safe_getattr(modem, key, None)
        if value is not None and (_safe_int(value, 0) != 0 or key not in base):
            out[key] = _safe_int(value, 0)
        elif key in base:
            out[key] = base.get(key)

    for key in _TRAFFIC_OPTIONAL_FLOAT_FIELDS:
        value = _safe_getattr(modem, key, None)
        if value is not None and (_safe_float(value, 0.0) != 0.0 or key not in base):
            out[key] = _safe_float(value, 0.0)
        elif key in base:
            out[key] = base.get(key)

    for key in _TRAFFIC_TEXT_FIELDS:
        value = str(_safe_getattr(modem, key, "") or "").strip()
        if value:
            out[key] = value
        elif key in base:
            out[key] = str(base.get(key) or "")


def _source_mappings(raw_base_status: Mapping[str, Any] | None) -> Iterable[Mapping[str, Any]]:
    base = raw_base_status if isinstance(raw_base_status, Mapping) else {}
    yield base
    # Alias translation is allowed only from the current modem-live capture
    # lane. Presentation mirrors such as current_state.operator may be
    # older than the current read and must not recreate live band/CA truth.
    for key in (
        "live_capture",
        "live_capture_summary",
        "modem_live",
        "native_fields",
        "raw_fields",
    ):
        value = base.get(key)
        if isinstance(value, Mapping):
            yield value


def _alias_value(raw_base_status: Mapping[str, Any] | None, canonical_field: str) -> Any:
    aliases = CANONICAL_LIVE_FIELD_ALIASES.get(canonical_field, ())
    for source in _source_mappings(raw_base_status):
        for alias in aliases:
            if alias in source and source.get(alias) not in (None, "", [], {}):
                return source.get(alias)
    return None


def _empty_canonical_value(value: Any) -> bool:
    return value in (None, "", [], {})


def _canonical_bandwidth_text(value: Any, *, mhz: Any = 0.0) -> str:
    """Return Modem Live's canonical human bandwidth text for matrix cells."""

    candidates = (value, mhz)
    for candidate in candidates:
        if candidate in (None, "", [], {}):
            continue
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            number = float(candidate)
        else:
            match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(candidate))
            if match is None:
                continue
            number = _safe_float(match.group(1), 0.0)
        if number <= 0:
            continue
        shown = str(int(number)) if float(number).is_integer() else f"{number:g}"
        return f"{shown} MHz"
    return ""


def _coerce_alias_value(canonical_field: str, value: Any) -> Any:
    if canonical_field in _BAND_LIST_CANONICAL_FIELDS:
        if isinstance(value, str):
            return parse_band_signature(value)
        return normalize_band_list(value)
    if canonical_field in {"dl_bandwidth", "ul_bandwidth"}:
        return _canonical_bandwidth_text(value)
    if canonical_field not in _NUMERIC_CANONICAL_FIELDS:
        return value
    if canonical_field in {"sinr_db", "rsrp_dbm", "rsrq_db", "rssi_dbm", "dl_bandwidth_mhz", "ul_bandwidth_mhz", "dl_freq_mhz", "ul_freq_mhz"}:
        return _safe_float(value, 0.0)
    return _safe_int(value, 0)


def apply_live_alias_translation(
    out: dict[str, Any],
    raw_base_status: Mapping[str, Any] | None,
    *,
    direct_modem_qam_fields: Iterable[str] = (),
) -> None:
    """Fill canonical 54GUC fields from recognized native/live aliases.

    This is deliberately profile-independent. Profiles decide trust/support;
    this function only translates recognizable facts into fixed 54GUC names.
    """

    direct_qam = {str(name) for name in direct_modem_qam_fields}
    for canonical_field in CANONICAL_LIVE_FIELD_ALIASES:
        current = out.get(canonical_field)
        value = _alias_value(raw_base_status, canonical_field)
        qam_unknown = (
            canonical_field.startswith("qam_")
            and canonical_field not in direct_qam
            and str(out.get("qam_source") or "").strip().lower() == "unknown"
            and _safe_float(out.get("qam_confidence"), 0.0) <= 0.0
        )
        if value in (None, "", [], {}):
            continue
        current_is_default_zero = current == 0 or current == 0.0
        if not _empty_canonical_value(current) and not current_is_default_zero and not qam_unknown:
            continue
        out[canonical_field] = _coerce_alias_value(canonical_field, value)
        if canonical_field.startswith("qam_"):
            out["qam_source"] = "reported"
            out["qam_confidence"] = max(_safe_float(out.get("qam_confidence"), 0.0), 1.0)

    if out.get("cqi0") in (None, "", [], {}) and not _empty_canonical_value(out.get("cqi")):
        out["cqi0"] = _safe_int(out.get("cqi"), 0)
    if out.get("mcs_dl0") in (None, "", [], {}) and not _empty_canonical_value(out.get("mcs_dl")):
        out["mcs_dl0"] = _safe_int(out.get("mcs_dl"), 0)


def _direct_modem_qam_fields(modem: Any) -> set[str]:
    fields: set[str] = set()
    mapping = {
        "qam_dl0": ("qam_dl0", "qam_dl_primary", "mod0"),
        "qam_dl1": ("qam_dl1", "qam_dl_secondary", "mod1"),
        "qam_ul": ("qam_ul",),
    }
    for canonical_field, attrs in mapping.items():
        for attr in attrs:
            if canonical_qam_label(_safe_getattr(modem, attr, "")):
                fields.add(canonical_field)
                break
    return fields


def _identity_sources(raw_base_status: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for key in ("resolved_identity_card", "runtime_identity", "runtime_identity_display"):
        value = raw_base_status.get(key)
        if isinstance(value, Mapping):
            out.append(value)
    return out


def _identity_value(raw_base_status: Mapping[str, Any], field: str, *aliases: str) -> str:
    for source in _identity_sources(raw_base_status):
        for key in (field, *aliases):
            value = source.get(key)
            if value not in (None, ""):
                return str(value)
    value = raw_base_status.get(field)
    if value not in (None, ""):
        return str(value)
    for alias in aliases:
        value = raw_base_status.get(alias)
        if value not in (None, ""):
            return str(value)
    return ""


def scheduler_field_trust(
    *,
    raw_base_status: Mapping[str, Any],
    modem: ModemInput,
) -> dict[str, str]:
    """Resolve scheduler field authority from capability owner identity."""

    modem_profile = (
        raw_base_status.get("modem_profile")
        if isinstance(raw_base_status.get("modem_profile"), Mapping)
        else {}
    )
    profile_trust = (
        modem_profile.get("field_trust")
        if isinstance(modem_profile.get("field_trust"), Mapping)
        else {}
    )
    authoritative_profile_trust = {
        field: str(profile_trust.get(field) or "").strip().lower()
        for field in SCHEDULER_TRUST_FIELDS
        if str(profile_trust.get(field) or "").strip()
    }
    profile_id = str(modem_profile.get("profile_id") or "").strip()
    exact_profile = firmware_profile_by_id(profile_id) if profile_id else None
    if profile_id and exact_profile is None:
        raise RuntimeError("modem_live_scheduler_exact_profile_unknown")
    backend = str(raw_base_status.get("backend", "") or raw_base_status.get("modem_backend", "") or "").strip().lower()
    owner_vendor = _identity_value(raw_base_status, "vendor")
    owner_model = _identity_value(raw_base_status, "model", "router_model")
    owner_firmware = _identity_value(raw_base_status, "firmware", "software_version")
    owner_provider_name = _identity_value(raw_base_status, "provider_name", "provider")
    owner_provider_plmn = _identity_value(raw_base_status, "provider_plmn", "plmn")
    reported_qam_fields = (
        _direct_modem_qam_fields(modem)
        if str(getattr(modem, "qam_source", "") or "").strip().lower() == "reported"
        else set()
    )
    if authoritative_profile_trust:
        trust = dict(authoritative_profile_trust)
    elif exact_profile is not None:
        trust = export_field_trust_from_profile(
            exact_profile,
            fields=SCHEDULER_TRUST_FIELDS,
        )
    else:
        trust = export_field_trust_from_overlay(
            backend=backend,
            vendor=owner_vendor or str(getattr(modem, "vendor", "") or ""),
            model=owner_model or str(getattr(modem, "model", "") or ""),
            firmware=owner_firmware or str(getattr(modem, "firmware", "") or ""),
            provider_name=owner_provider_name or str(getattr(modem, "provider_name", "") or ""),
            provider_plmn=owner_provider_plmn or str(getattr(modem, "provider_plmn", "") or ""),
            fields=SCHEDULER_TRUST_FIELDS,
        )

    if backend == "mikrotik":
        # RouterOS/Quectel exposes one DL CQI for the serving/PBAND carrier.
        # Modem Live maps that native field to canonical cqi0; cqi1 is not
        # available unless a future backend proves a real per-stream source.
        trust.update({
            "cqi": "confirmed",
            "cqi0": "confirmed",
            "cqi1": "unsupported",
            "mcs_dl1": "unsupported",
            "qam_dl1": "unsupported",
        })
        # A directly reported optional RouterOS modulation is stronger than a
        # historical profile absence.  It does not imply that the associated
        # MCS lane exists.
        for field in reported_qam_fields.intersection({"qam_dl0", "qam_ul"}):
            trust[field] = "confirmed"
    elif not authoritative_profile_trust:
        direct_qam_fields = _direct_modem_qam_fields(modem)
        for field in direct_qam_fields:
            if str(trust.get(field) or "").strip().lower() not in {"unsupported", "non_useful"}:
                trust[field] = "confirmed"
        direct_numeric_fields = {
            "cqi": ("cqi",),
            "cqi0": ("cqi0", "cqi"),
            "cqi1": ("cqi1",),
            "mcs_dl0": ("mcs_dl0", "mcs_dl"),
            "mcs_dl1": ("mcs_dl1",),
            "mcs_ul": ("mcs_ul",),
            "ri": ("ri",),
        }
        for field, attrs in direct_numeric_fields.items():
            for attr in attrs:
                if _safe_int(getattr(modem, attr, 0), 0) > 0:
                    if str(trust.get(field) or "").strip().lower() not in {"unsupported", "non_useful"}:
                        trust[field] = "confirmed"
                    break
    if not bool(getattr(modem, "mcs_ul_supported", True)):
        trust["mcs_ul"] = "unsupported"
        if not (backend == "mikrotik" and "qam_ul" in reported_qam_fields):
            trust["qam_ul"] = "unsupported"
    return trust


def build_modem_live_traffic_surface(
    *,
    modem: ModemInput | None,
    raw_base_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project only traffic facts from one typed or status-only receipt.

    Status-only reads do not have a ModemInput: key presence, including false,
    zero and unknown, must survive without typed-sample defaults. The registry
    supplies its receipt-scoped discovery result; frames/captures supply their
    accepted typed sample and its native counter cache.
    """
    from arch4.modem_live.truth_matrix import MODEM_LIVE_TRUTH_FIELD_CATALOG

    fields = MODEM_LIVE_TRUTH_FIELD_CATALOG["traffic"]
    base = raw_base_status or {}
    out = {key: base[key] for key in fields if key in base}
    if modem is not None:
        counter_fields = frozenset(
            _TRAFFIC_INT_FIELDS + _TRAFFIC_FLOAT_FIELDS
            + _TRAFFIC_OPTIONAL_INT_FIELDS + _TRAFFIC_OPTIONAL_FLOAT_FIELDS
            + _TRAFFIC_TEXT_FIELDS
        )
        for key in fields:
            if key not in counter_fields and hasattr(modem, key):
                out[key] = getattr(modem, key)
        _apply_traffic_counter_surface(out, modem=modem, raw_base_status=base)
    for direction, rate in (("dl", "rx"), ("ul", "tx")):
        typed_key = f"throughput_{direction}_mbps"
        public_key = f"{direction}_mbps"
        byte_key = f"{rate}_bytes_per_sec"
        if typed_key in out:
            out[public_key] = out[typed_key]
        elif public_key in out:
            out[typed_key] = out[public_key]
        elif byte_key in out and out[byte_key] is not None:
            out[typed_key] = out[public_key] = _safe_float(out[byte_key], 0.0) / 125_000.0
    return out


def build_modem_live_radio_surface(
    *,
    modem: ModemInput,
    raw_base_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Translate backend modem fields into canonical modem-live radio fields."""

    out: dict[str, Any] = {}
    base = raw_base_status if isinstance(raw_base_status, Mapping) else {}
    radio_connected = bool(getattr(modem, "radio_connected", False))
    ca_active = bool(getattr(modem, "ca_active", False))
    out["radio_connected"] = radio_connected
    out["ca_active"] = ca_active
    out["ca_state_text"] = "ACTIVE" if ca_active else "INACTIVE"
    # A configured CA Pool is command context, never evidence of a serving
    # cell or PBAND transition. Actual active composition remains separately
    # named optional evidence below.

    serving_band = _first_positive_modem_base_int(
        modem,
        base,
        "serving_band",
        "serving_band",
        "primary_band",
    )
    if serving_band <= 0:
        serving_band = _single_observed_active_band(modem)
    serving_band_available = serving_band > 0
    out["serving_band"] = serving_band
    out["serving_band_signature"] = f"B{serving_band}" if serving_band_available else ""
    out["serving_band_available"] = serving_band_available
    out["serving_band_unavailable_reason"] = (
        ""
        if serving_band_available
        else "modem_live_serving_band_not_observed"
    )
    selected_band_set = normalize_band_list(
        _safe_getattr(modem, "selected_band_set", [])
        or base.get("selected_band_set")
        or []
    )
    out["selected_band_set"] = selected_band_set
    out["selected_band_set_signature"] = str(
        getattr(modem, "selected_band_set_signature", "")
        or band_signature(selected_band_set)
    )
    configured_band_pool = normalize_band_list(
        _safe_getattr(modem, "configured_band_pool", [])
        or base.get("configured_band_pool")
        or []
    )
    if not configured_band_pool:
        configured_band_pool = list(selected_band_set)
    out["configured_band_pool"] = configured_band_pool
    out["configured_band_pool_signature"] = band_signature(configured_band_pool)
    out["ca_signature"] = out["configured_band_pool_signature"]
    out["ca_band_signature"] = out["configured_band_pool_signature"]
    out["carrier_count"] = len(out["configured_band_pool"])
    out["configured_band_pool_source"] = str(
        getattr(modem, "configured_band_pool_source", "")
        or base.get("configured_band_pool_source")
        or ("selected_band_set" if configured_band_pool else "")
    )
    out["configured_band_pool_status"] = str(getattr(modem, "configured_band_pool_status", "") or "")
    pool_age = getattr(modem, "configured_band_pool_age_s", None)
    out["configured_band_pool_age_s"] = pool_age if isinstance(pool_age, (int, float)) else None
    out["configured_band_pool_last_sample_ts"] = _safe_float(
        getattr(modem, "configured_band_pool_last_sample_ts", 0.0),
        0.0,
    )
    active_band_set = normalize_band_list(
        _safe_getattr(modem, "active_band_set", [])
        or base.get("active_band_set")
        or []
    )
    out["active_band_set"] = active_band_set
    out["active_band_set_signature"] = str(
        getattr(modem, "active_band_set_signature", "")
        or band_signature(active_band_set)
    )
    band_set = normalize_band_list(
        _safe_getattr(modem, "band_set", [])
        or base.get("band_set")
        or []
    )
    out["band_set"] = band_set
    out["band_set_signature"] = str(
        getattr(modem, "band_set_signature", "")
        or band_signature(band_set)
    )
    out["pci"] = _first_positive_modem_base_int(modem, base, "pci", "pci")
    out["earfcn_dl"] = _first_positive_modem_base_int(modem, base, "earfcn_dl", "earfcn_dl", "earfcn")
    out["earfcn_ul"] = _first_positive_modem_base_int(modem, base, "earfcn_ul", "earfcn_ul")
    out["enb_id"] = _first_positive_modem_base_int(modem, base, "enb_id", "enb_id")
    out["cell_id"] = _first_positive_modem_base_int(modem, base, "cell_id", "cell_id")
    out["tac"] = _first_positive_modem_base_int(modem, base, "tac", "tac")
    if int(out.get("cell_id", 0) or 0) <= 0:
        try:
            enb = int(out.get("enb_id", 0) or 0)
            pci = int(out.get("pci", 0) or 0)
            if enb > 0 and pci > 0:
                out["cell_id"] = (enb << 8) + pci
        except Exception as exc:
            log.debug("build_modem_live_radio_surface: cell_id derive suppressed %s", exc)

    out["sinr_db"] = _safe_float(getattr(modem, "sinr_db", 0.0), 0.0)
    out["rsrp_dbm"] = _safe_float(getattr(modem, "rsrp_dbm", 0.0), 0.0)
    out["rsrq_db"] = _safe_float(getattr(modem, "rsrq_db", 0.0), 0.0)
    out["rsrq_source"] = str(getattr(modem, "rsrq_source", "reported") or "reported")
    out["rssi_dbm"] = _safe_float(getattr(modem, "rssi_dbm", 0.0), 0.0)

    out["mcs_dl"] = _safe_int(getattr(modem, "mcs_dl", 0), 0)
    out["mcs_ul"] = _safe_int(getattr(modem, "mcs_ul", 0), 0)
    out["mcs_dl0"] = _safe_int(getattr(modem, "mcs_dl0", getattr(modem, "mcs_dl", 0)), 0)
    out["mcs_dl1"] = _safe_int(getattr(modem, "mcs_dl1", 0), 0)
    out["qm_dl0"] = _safe_int(getattr(modem, "qm_dl0", getattr(modem, "mod0", getattr(modem, "qam_dl_primary", 0))), 0)
    out["qm_dl1"] = _safe_int(getattr(modem, "qm_dl1", getattr(modem, "mod1", getattr(modem, "qam_dl_secondary", 0))), 0)
    out["qm_ul"] = _safe_int(getattr(modem, "qm_ul", getattr(modem, "qam_ul", 0)), 0)
    out["mod0"] = _safe_int(getattr(modem, "mod0", 0), 0)
    out["mod1"] = _safe_int(getattr(modem, "mod1", 0), 0)
    out["mcs_ul_raw"] = _safe_int(getattr(modem, "mcs_ul", 0), 0)
    out["mcs_ul_supported"] = bool(getattr(modem, "mcs_ul_supported", True))
    out["cqi"] = _safe_int(getattr(modem, "cqi", 0), 0)
    out["cqi0"] = _safe_int(getattr(modem, "cqi0", getattr(modem, "cqi", 0)), 0)
    out["cqi1"] = _safe_int(getattr(modem, "cqi1", 0), 0)
    out["cqi_source"] = str(getattr(modem, "cqi_source", "reported") or "reported")
    out["ri"] = _safe_int(getattr(modem, "ri", 0), 0)
    out["ri_valid"] = bool(getattr(modem, "ri_valid", out["ri"] > 0))
    out["ri_source"] = str(getattr(modem, "ri_source", "") or ("legacy_positive_ri" if out["ri"] > 0 else ""))
    out["qam_dl0"] = canonical_qam_label_from_value_or_mcs(
        getattr(modem, "qam_dl0", getattr(modem, "qam_dl_primary", 0)),
        out["mcs_dl0"],
    )
    out["qam_dl1"] = canonical_qam_label_from_value_or_mcs(
        getattr(modem, "qam_dl1", getattr(modem, "qam_dl_secondary", 0)),
        out["mcs_dl1"],
    )
    out["qam_dl_primary"] = _safe_int(getattr(modem, "qam_dl_primary", 0), 0)
    out["qam_dl_secondary"] = _safe_int(getattr(modem, "qam_dl_secondary", 0), 0)
    out["qam_ul"] = canonical_qam_label_from_value_or_mcs(getattr(modem, "qam_ul", ""), out["mcs_ul"])
    out["qam_source"] = str(getattr(modem, "qam_source", "unknown") or "unknown")
    out["qam_confidence"] = _safe_float(getattr(modem, "qam_confidence", 0.0), 0.0)

    out["tx_power"] = getattr(modem, "tx_power", "") or ""
    out["dl_bandwidth"] = getattr(modem, "dl_bandwidth", "") or ""
    out["ul_bandwidth"] = getattr(modem, "ul_bandwidth", "") or ""
    out["dl_bandwidth_mhz"] = _safe_float(getattr(modem, "dl_bandwidth_mhz", 0.0), 0.0)
    out["ul_bandwidth_mhz"] = _safe_float(getattr(modem, "ul_bandwidth_mhz", 0.0), 0.0)
    out["dl_freq_mhz"] = _safe_float(getattr(modem, "dl_freq_mhz", 0.0), 0.0)
    out["ul_freq_mhz"] = _safe_float(getattr(modem, "ul_freq_mhz", 0.0), 0.0)
    out["antenna_mode"] = _best_identity_text(modem, "antenna_mode", "antenna", "selected_antenna")
    out["antenna_mode_source"] = str(getattr(modem, "antenna_mode_source", "") or "")
    antenna_switch_api_supported = getattr(modem, "antenna_switch_api_supported", None)
    if antenna_switch_api_supported is not None:
        out["antenna_switch_api_supported"] = bool(antenna_switch_api_supported)
    external_antenna_ports_present = getattr(modem, "external_antenna_ports_present", None)
    if external_antenna_ports_present is not None:
        out["external_antenna_ports_present"] = bool(external_antenna_ports_present)
    out["antenna"] = out["antenna_mode"]
    out["tbs_dl0"] = _safe_int(getattr(modem, "tbs_dl0", 0), 0)
    out["tbs_dl1"] = _safe_int(getattr(modem, "tbs_dl1", 0), 0)
    out["tbs_ul"] = _safe_int(getattr(modem, "tbs_ul", 0), 0)
    out["mcs_dl_retx"] = bool(getattr(modem, "mcs_dl_retx", False))
    out["mcs_dl1_retx"] = bool(getattr(modem, "mcs_dl1_retx", False))
    out["mcs_ul_retx"] = bool(getattr(modem, "mcs_ul_retx", False))

    out["nsa_active"] = bool(getattr(modem, "nsa_active", False))
    out["sa_active"] = bool(getattr(modem, "sa_active", False))
    out["nr_band"] = _safe_int(getattr(modem, "nr_band", 0), 0)
    out["nr_pci"] = _safe_int(getattr(modem, "nr_pci", 0), 0)
    out["nr_arfcn"] = _safe_int(getattr(modem, "nr_arfcn", 0), 0)
    # Explicit backend missingness owns this receipt; it cannot become a
    # measured zero through the typed object's default. Alias owner validates it.
    out["nr_cell_id"] = base["nr_cell_id"] if "nr_cell_id" in base else getattr(modem, "nr_cell_id", None)
    out["nr_mode"] = str(getattr(modem, "nr_mode", "") or "")
    out["nr_state_text"] = str(getattr(modem, "nr_state_text", "") or "")
    out["nr_cells"] = list(getattr(modem, "nr_cells", []) or [])
    out["nr_bandwidth_mhz"] = _safe_float(
        getattr(modem, "nr_bandwidth_mhz", 0.0), 0.0
    )
    out["nr_scs_khz"] = _safe_float(
        getattr(modem, "nr_scs_khz", 0.0), 0.0
    )
    out["nr_rsrp_dbm"] = _safe_float(getattr(modem, "nr_rsrp_dbm", 0.0), 0.0)
    out["nr_rsrq_db"] = _safe_float(getattr(modem, "nr_rsrq_db", 0.0), 0.0)
    out["nr_sinr_db"] = _safe_float(getattr(modem, "nr_sinr_db", 0.0), 0.0)

    apply_live_alias_translation(
        out,
        raw_base_status,
        direct_modem_qam_fields=_direct_modem_qam_fields(modem),
    )
    # The alias boundary also recognizes native packet status codes. Traffic
    # projection owns that fact; a radio-only surface cannot publish it.
    out.pop("connection_status_code", None)
    if not str(out.get("network_type") or "").strip():
        if bool(out.get("sa_active")):
            out["network_type"] = "NR SA (5G)"
        elif bool(out.get("nsa_active")) or bool(out.get("nr5g_active")):
            out["network_type"] = "NR NSA (5G)"
        elif radio_connected:
            out["network_type"] = "LTE (4G)"
        else:
            out["network_type"] = "No Service"
    out["dl_bandwidth"] = _canonical_bandwidth_text(
        out.get("dl_bandwidth"),
        mhz=out.get("dl_bandwidth_mhz"),
    )
    out["ul_bandwidth"] = _canonical_bandwidth_text(
        out.get("ul_bandwidth"),
        mhz=out.get("ul_bandwidth_mhz"),
    )
    final_serving_band = _safe_int(out.get("serving_band"), 0)
    if final_serving_band > 0:
        out["serving_band"] = final_serving_band
        out["serving_band_signature"] = f"B{final_serving_band}"
        out["serving_band_available"] = True
        out["serving_band_unavailable_reason"] = ""
    else:
        out.pop("serving_band", None)
        out.pop("serving_band_signature", None)
        out["serving_band_available"] = False
        out["serving_band_unavailable_reason"] = (
            "modem_live_serving_band_not_observed"
        )
    return out


def build_modem_live_capture_surface(
    *,
    modem: ModemInput,
    raw_base_status: Mapping[str, Any] | None = None,
    capture_scope: str = "startup_probe",
    capture_source: str = "modem_snapshot",
) -> dict[str, Any]:
    """Build the canonical live-capture surface exported by Modem Live."""

    out = build_modem_live_radio_surface(modem=modem, raw_base_status=raw_base_status)
    out.update(build_modem_live_traffic_surface(modem=modem, raw_base_status=raw_base_status))
    serving_band = _safe_int(out.get("serving_band"), 0)
    configured_band_pool = normalize_band_list(out.get("configured_band_pool"))
    configured_pool_signature = band_signature(configured_band_pool)
    active_band_set = normalize_band_list(out.get("active_band_set"))

    out.update({
        "capture_scope": str(capture_scope or ""),
        "capture_source": str(capture_source or ""),
        "radio_connected": bool(getattr(modem, "radio_connected", False)),
        "radio_state_text": str(
            getattr(modem, "radio_state_text", "")
            or ("Connected" if bool(getattr(modem, "radio_connected", False)) else "Disconnected")
        ),
        "serving_band": serving_band,
        "serving_band_signature": str(
            getattr(modem, "serving_band_signature", "")
            or (f"B{serving_band}" if serving_band > 0 else "")
        ),
        "ca_active": bool(getattr(modem, "ca_active", False)),
        "ca_signature": configured_pool_signature,
        "ca_band_signature": configured_pool_signature,
        "carrier_count": len(configured_band_pool),
        "configured_band_pool": configured_band_pool,
        "configured_band_pool_signature": str(
            configured_pool_signature
        ),
        "configured_band_pool_source": str(getattr(modem, "configured_band_pool_source", "") or ""),
        "active_band_set": active_band_set,
        "ca_state_text": str(getattr(modem, "ca_state_text", "") or ""),
        "neighbors": [],
    })
    for field in (
        "selected_band_set",
        "selected_band_set_signature",
        "active_band_set_signature",
        "band_set",
        "band_set_signature",
    ):
        out.pop(field, None)
    out["neighbors"] = canonical_neighbor_rows(
        getattr(modem, "neighbors", None)
    )
    return out


def apply_scheduler_field_trust(out: dict[str, Any], field_trust: Mapping[str, str]) -> None:
    """Clear unsupported scheduler fields before any display surface is built."""

    if str(field_trust.get("qam_dl0") or "") == "unsupported":
        out["qam_dl0"] = ""
        out["qam_dl_primary"] = 0
        out["qm_dl0"] = 0
        out["mod0"] = 0
    if str(field_trust.get("qam_dl1") or "") == "unsupported":
        out["qam_dl1"] = ""
        out["qam_dl_secondary"] = 0
        out["qm_dl1"] = 0
        out["mod1"] = 0
    if str(field_trust.get("qam_ul") or "") == "unsupported":
        out["qam_ul"] = ""
        out["qm_ul"] = 0
    if str(field_trust.get("cqi0") or "") == "unsupported":
        out["cqi0"] = 0
    if str(field_trust.get("cqi") or "") == "unsupported":
        out["cqi"] = 0
    if str(field_trust.get("cqi1") or "") == "unsupported":
        out["cqi1"] = 0
    if str(field_trust.get("mcs_dl0") or "") == "unsupported":
        out["mcs_dl0"] = 0
        out["mcs_dl"] = 0
        out["tbs_dl0"] = 0
        out["mcs_dl_retx"] = False
    if str(field_trust.get("mcs_dl1") or "") == "unsupported":
        out["mcs_dl1"] = 0
        out["tbs_dl1"] = 0
        out["mcs_dl1_retx"] = False
    if str(field_trust.get("mcs_ul") or "") == "unsupported":
        out["mcs_ul"] = 0
        out["mcs_ul_raw"] = 0
        out["tbs_ul"] = 0
        out["mcs_ul_retx"] = False
    if str(field_trust.get("ri") or "") in {"unsupported", "non_useful"}:
        out["ri"] = 0
        out["ri_valid"] = False
        out["ri_source"] = ""

    if all(str(field_trust.get(name) or "") == "unsupported" for name in ("qam_dl0", "qam_dl1", "qam_ul")):
        out["qam_source"] = "unknown"
        out["qam_confidence"] = 0.0
