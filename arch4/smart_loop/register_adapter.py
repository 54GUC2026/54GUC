"""Pure register-snapshot adapter for the clean smart loop."""

from __future__ import annotations

from typing import Any, Mapping

from arch4.smart_loop.closed_loop import LearningFacts, ModemContext


SCHEMA = "smart_loop.register_adapter.v1"
OWNER = "arch4.smart_loop.register_adapter"


def smart_loop_inputs_from_registers(
    *,
    identity: Mapping[str, Any] | None = None,
    radio: Mapping[str, Any],
    quality: Mapping[str, Any],
    ca: Mapping[str, Any],
    learning: Mapping[str, Any],
    mutation: Mapping[str, Any] | None = None,
    availability: Mapping[str, Any] | None = None,
    capacity: Mapping[str, Any] | None = None,
) -> tuple[ModemContext, LearningFacts]:
    """Adapt owner-stamped runtime register payloads into smart-loop inputs."""

    identity_map = _mapping(identity)
    mutation_map = _mapping(mutation)
    availability_map = _mapping(availability)
    identity_provider_plmn = _text(identity_map.get("provider_plmn"))
    radio_provider_plmn = _text(radio.get("provider_plmn"))
    if (
        radio_provider_plmn
        and identity_provider_plmn
        and radio_provider_plmn != identity_provider_plmn
    ):
        raise ValueError("smart_loop_provider_owner_conflict")
    context = ModemContext(
        identity_code=_identity_code(identity_map),
        identity_status=_text(identity_map.get("identity_status")) or "unknown",
        model=_text(identity_map.get("model")),
        provider_plmn=identity_provider_plmn,
        cell_id=_int(radio.get("cell_id")),
        pci=_int(radio.get("pci")),
        earfcn=_int(radio.get("earfcn_dl") if "earfcn_dl" in radio else radio.get("earfcn")),
        nr_band=_text(radio.get("nr_band") if "nr_band" in radio else radio.get("nr5g_band")),
        nr_pci=_int(radio.get("nr_pci") if "nr_pci" in radio else radio.get("nr5g_pci")),
        nr_arfcn=_int(radio.get("nr_arfcn") if "nr_arfcn" in radio else radio.get("nr5g_arfcn")),
        nr_cell_id=_text(radio.get("nr_cell_id") if "nr_cell_id" in radio else radio.get("nr5g_cell_id")),
        hour=_int(learning.get("current_hour")),
        current_pband=_int(
            radio.get("serving_band") if "serving_band" in radio else radio.get("primary_band")
        ),
        allowed_bands=_authority_bands(learning),
        ca_pool=_int_tuple(radio.get("configured_band_pool") or ()),
        cq=_float_or_none(quality.get("cq_cell_display") if "cq_cell_display" in quality else quality.get("cq_cell")),
        congestion_pct=_float_or_none(quality.get("congestion_pct")),
        ca_active=_bool_or_none(ca.get("ca_active")),
        link_valid=_bool(radio.get("radio_connected"), False),
        cooldown_s=_float(
            availability_map.get("cooldown_s")
            if "cooldown_s" in availability_map
            else mutation_map.get("cooldown_s")
        ),
        proof_pending=_bool(learning.get("proof_pending"), False),
        safety_block=_text(
            availability_map.get("safety_block")
            if "safety_block" in availability_map
            else mutation_map.get("safety_block")
        ),
        profile_id=_text(identity_map.get("profile_id")),
        firmware=_text(identity_map.get("firmware")),
        observed_ts=_observed_ts(radio=radio, quality=quality, learning=learning),
    )
    facts = LearningFacts(
        band_scores=_band_score_map(learning.get("band_scores")),
        evidence_counts=_band_count_map(learning.get("evidence_counts")),
        profile_confidence=_float(learning.get("profile_confidence")),
        sample_count=_int(learning.get("sample_count")),
        reason=_text(learning.get("reason")),
        action_roi=_mapping(learning.get("action_roi")),
        calibration_fact_package=_calibration_fact_package(learning),
        prediction_context=_prediction_context(learning),
        measured_capacity_fact=_measured_capacity_fact(capacity),
        passive_dl_peak_fact=_passive_dl_peak_fact(capacity),
    )
    return context, facts


def _calibration_fact_package(learning: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(learning.get("calibration_fact_package"))


def _prediction_context(learning: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(learning.get("prediction_context"))
    if row.get("schema") != "crystal_ball_horizon_context.v1":
        return {}
    return row


def _measured_capacity_fact(capacity: Mapping[str, Any] | None) -> dict[str, Any]:
    row = _mapping(capacity)
    if row.get("schema") != "capacity_evidence_register.v1":
        return {}
    if row.get("owner") != "arch4.smart_loop.capacity_probe":
        return {}
    row = _mapping(row.get("active_capacity_fact"))
    if row.get("schema") != "measured_capacity_fact.v1":
        return {}
    if row.get("owner") != "arch4.smart_loop.capacity_probe":
        return {}
    if not bool(row.get("measured", False)):
        return {}
    return row


def _passive_dl_peak_fact(capacity: Mapping[str, Any] | None) -> dict[str, Any]:
    row = _mapping(capacity)
    if row.get("schema") != "capacity_evidence_register.v1":
        return {}
    if row.get("owner") != "arch4.smart_loop.capacity_probe":
        return {}
    row = _mapping(row.get("passive_dl_peak_fact"))
    if row.get("schema") != "passive_dl_peak_fact.v1":
        return {}
    if row.get("owner") != "arch4.smart_loop.capacity_probe":
        return {}
    if not bool(row.get("observed", False)):
        return {}
    return row


def _observed_ts(*, radio: Mapping[str, Any], quality: Mapping[str, Any], learning: Mapping[str, Any]) -> float:
    for source in (radio, quality, learning):
        for key in ("sample_ts", "engine_tick_ts", "_register_updated_at", "updated_at", "timestamp", "ts"):
            value = source.get(key)
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0.0:
                return parsed
    return 0.0


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _identity_code(identity: Mapping[str, Any]) -> str:
    text = _text(identity.get("identity_code")).upper()
    if text.startswith("0X") and len(text) == 18:
        return "0x" + text[2:]
    return "0x0000000000000000"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    return _bool(value, False)


def _int_tuple(value: Any) -> tuple[int, ...]:
    if isinstance(value, str):
        value = value.replace("+", ",").split(",")
    if not isinstance(value, (list, tuple, set)):
        return ()
    out = []
    for item in value:
        parsed = _int(item)
        if parsed > 0 and parsed not in out:
            out.append(parsed)
    return tuple(out)


def _authority_bands(learning: Mapping[str, Any]) -> tuple[int, ...]:
    """Return the single normalized band authority surface for smart actions."""

    for key in ("control_authority_bands", "authority_bands"):
        bands = _int_tuple(learning.get(key))
        if bands:
            return bands
    return ()


def _band_score_map(value: Any) -> dict[int, float]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[int, float] = {}
    for key, score in value.items():
        band = _int(key)
        if band > 0:
            out[band] = max(0.0, min(1.0, _float(score)))
    return out


def _band_count_map(value: Any) -> dict[int, int]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[int, int] = {}
    for key, count in value.items():
        band = _int(key)
        if band > 0:
            out[band] = max(0, _int(count))
    return out


__all__ = ["OWNER", "SCHEMA", "smart_loop_inputs_from_registers"]
