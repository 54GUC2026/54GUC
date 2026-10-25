"""Smartness input frame built from canonical runtime registers."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import hashlib
import json
import time
from typing import Any, Mapping

from arch4.action_opportunity_source_facts import (
    ActionOpportunitySourceFactSet,
    action_opportunity_source_fact_set_from_mapping,
)
from arch4.coerce import safe_bool
from arch4.controller_frames import CellularIntelligenceGeneration
from arch4.runtime_registers import (
    REGISTER_CA,
    REGISTER_CAPACITY,
    REGISTER_DECISION,
    REGISTER_HEALTH,
    REGISTER_IDENTITY,
    REGISTER_LATENCY,
    REGISTER_LEARNING,
    REGISTER_MODEM_SLOW,
    REGISTER_MUTATION,
    REGISTER_QUALITY,
    REGISTER_RADIO,
    REGISTER_SMARTNESS,
    REGISTER_TRAFFIC,
    RuntimeRegisterBank,
)
from arch4.runtime_decision_projection import (
    normalize_runtime_decision_projection,
)

SCHEMA = "smartness.fact_register.v1"
OWNER = "smartness_fact_register"
SOURCE = "runtime_register_fabric"
MAX_SMARTNESS_FACT_FRAME_BYTES = 512 * 1024

SMARTNESS_FACT_SECTIONS: tuple[str, ...] = (
    REGISTER_IDENTITY,
    REGISTER_RADIO,
    REGISTER_MODEM_SLOW,
    REGISTER_LATENCY,
    REGISTER_QUALITY,
    REGISTER_CA,
    REGISTER_TRAFFIC,
    REGISTER_LEARNING,
    REGISTER_CAPACITY,
    REGISTER_MUTATION,
    REGISTER_DECISION,
    REGISTER_HEALTH,
)

SMARTNESS_ACCEPTED_FRAME_SECTIONS: tuple[str, ...] = (
    REGISTER_IDENTITY,
    REGISTER_RADIO,
    REGISTER_MODEM_SLOW,
    REGISTER_LATENCY,
    REGISTER_QUALITY,
    REGISTER_CA,
    REGISTER_TRAFFIC,
)

SMARTNESS_OWNER_SNAPSHOT_SECTIONS: tuple[str, ...] = (
    REGISTER_LEARNING,
    REGISTER_CAPACITY,
    REGISTER_MUTATION,
    REGISTER_DECISION,
    REGISTER_HEALTH,
)


_SECTION_FIELD_ALLOWLIST: Mapping[str, tuple[str, ...]] = {
    REGISTER_IDENTITY: (
        "identity_code",
        "identity_status",
        "manufacturer",
        "model",
        "hardware_sku",
        "software_version",
        "firmware",
        "profile_id",
        "access_type",
        "access_type_id",
        "backend",
        "backend_family",
        "provider_name",
        "provider_plmn",
        "identity_catalog_version",
        "identity_catalog_checksum",
        "catalog_version",
        "catalog_checksum",
        "accepted_generation_session_scope_key",
        "session_scope_key",
        "source_epoch",
        "source_sequence",
        "sample_ts",
    ),
    REGISTER_RADIO: (
        "sample_sequence",
        "sample_ts",
        "accepted_at",
        "accepted_ts",
        "source_epoch",
        "session_scope_key",
        "source_sequence",
        "generation",
        "sample_age_s",
        "sample_fresh",
        "radio_connected",
        "packet_session_connected",
        "backend",
        "provider_plmn",
        "serving_band",
        "primary_band",
        "selected_band_set",
        "selected_bands",
        "active_band_set",
        "active_bands",
        "configured_band_pool",
        "configured_band_pool_signature",
        "serving_band_signature",
        "carrier_details",
        "pci",
        "earfcn",
        "earfcn_dl",
        "enb_id",
        "cell_id",
        "sinr_db",
        "rsrp_dbm",
        "nr_band",
        "nr5g_band",
        "nr_pci",
        "nr5g_pci",
        "nr_arfcn",
        "nr5g_arfcn",
        "nr_cell_id",
        "nr5g_cell_id",
        "nr_lock_active",
        "nr_locked_bands",
        "cell_lock_active",
        "cell_locked",
        "cell_lock_pci",
        "cell_lock_earfcn",
        "session_uptime_s",
    ),
    REGISTER_MODEM_SLOW: (
        "rrc",
        "neighbors",
        "neighbor_count",
        "nr_cells",
        "nr5g_cells",
        "dl_bandwidth",
        "ul_bandwidth",
        "dl_bandwidth_mhz",
        "ul_bandwidth_mhz",
        "nr_bandwidth_mhz",
        "nr_scs_khz",
    ),
    REGISTER_LATENCY: (
        "rtt_baseline",
        "rtt_ema",
        "rtt_sliding_avg",
        "sample_sequence",
        "sample_ts",
        "accepted_at",
        "source_epoch",
        "session_scope_key",
    ),
    REGISTER_QUALITY: (
        "cq_cell",
        "cq_cell_display",
        "cqb_cell",
        "cqb_cell_valid",
        "cqb_cell_source",
        "congestion_pct",
        "sinr_db",
        "nr_just_dropped",
        "nr_recovering",
        "sample_sequence",
        "sample_ts",
        "accepted_at",
        "source_epoch",
        "session_scope_key",
    ),
    REGISTER_CA: (
        "ca_active",
        "ca_signature",
        "ca_band_signature",
        "ca_component_band_set",
        "ca_component_band_signature",
        "ca_component_count",
        "ca_composition_signature",
        "sample_sequence",
        "sample_ts",
        "accepted_at",
        "source_epoch",
        "session_scope_key",
    ),
    REGISTER_TRAFFIC: (
        "has_traffic",
        "traffic_active",
        "traffic_valid",
        "dl_mbps",
        "ul_mbps",
        "traffic_presence_active",
        "traffic_presence_interval_s",
        "session_uptime_s",
        "sample_sequence",
        "sample_ts",
        "source_epoch",
        "session_scope_key",
    ),
    REGISTER_LEARNING: (
        "current_hour",
        "ca_active",
        "proof_pending",
        "calibration_fact_package",
        "sample_ts",
        "updated_at",
    ),
    REGISTER_CAPACITY: (
        "schema",
        "owner",
        "active_capacity_fact",
        "passive_dl_peak_fact",
        "sample_ts",
        "updated_at",
    ),
    REGISTER_MUTATION: (
        "active",
        "learning_allowed",
        "reason",
        "state",
        "cooldown_s",
        "safety_block",
        "updated_at",
    ),
}


@dataclass(frozen=True, slots=True)
class SmartnessSourceTransaction:
    """Exact accepted-generation source facts for one immutable frame."""

    accepted_generation: CellularIntelligenceGeneration
    source_facts: ActionOpportunitySourceFactSet

    def __post_init__(self) -> None:
        if not isinstance(
            self.accepted_generation,
            CellularIntelligenceGeneration,
        ):
            raise TypeError("smartness_accepted_cellular_generation_required")
        scope = self.frame_scope()
        _require_frame_scope(scope)
        if not isinstance(self.source_facts, ActionOpportunitySourceFactSet):
            raise TypeError("smartness_source_fact_set_required")
        if _fact_set_scope(self.source_facts) != scope:
            raise RuntimeError("smartness_source_transaction_scope_mismatch")

    def frame_scope(self) -> dict[str, Any]:
        generation = self.accepted_generation.generation
        identity = self.accepted_generation.identity
        return {
            "identity_code": str(identity.identity_code or ""),
            "session_scope_key": str(generation.session_scope_key or ""),
            "source_epoch": str(generation.source_epoch or ""),
            "source_sequence": int(generation.generation or 0),
        }

    @property
    def accepted_ts(self) -> float:
        generation = self.accepted_generation.generation
        return float(generation.accepted_ts or generation.sample_ts or 0.0)


def publish_smartness_fact_register(
    bank: RuntimeRegisterBank,
    *,
    transaction: SmartnessSourceTransaction,
    now: float | None = None,
) -> dict[str, Any]:
    """Publish the one coherent input frame for SmartLoop/Brain/Referee readers."""

    if not isinstance(transaction, SmartnessSourceTransaction):
        raise TypeError("smartness_source_transaction_required")
    now_ts = float(time.time() if now is None else now)
    snapshot = bank.snapshot(SMARTNESS_OWNER_SNAPSHOT_SECTIONS)
    accepted_sections = _accepted_frame_sections(
        transaction.accepted_generation
    )
    sections: dict[str, dict[str, Any]] = {}
    section_meta: dict[str, dict[str, Any]] = {}
    missing_sections: list[str] = []
    stale_sections: list[str] = []
    dirty_sections: list[str] = []
    max_sequence = 0

    for name in SMARTNESS_FACT_SECTIONS:
        accepted_owned = name in SMARTNESS_ACCEPTED_FRAME_SECTIONS
        row = _mapping(snapshot.get(name)) if not accepted_owned else {}
        value = (
            _section_projection(name, _mapping(accepted_sections.get(name)))
            if accepted_owned
            else _section_projection(name, _mapping(row.get("value")))
        )
        sections[name] = value
        if accepted_owned:
            valid = True
            stale = not bool(transaction.accepted_generation.generation.sample_fresh)
            dirty = False
            sequence = int(
                transaction.accepted_generation.generation.generation or 0
            )
            owner = "accepted_controller_generation"
            source = "smartness_source_transaction"
            updated_at = float(
                transaction.accepted_generation.generation.accepted_ts or 0.0
            )
            trust = "accepted"
            source_kind = "accepted"
            validity = "stale" if stale else "valid"
            transition = False
        else:
            valid = safe_bool(row.get("valid"), False)
            stale = safe_bool(row.get("stale"), False)
            dirty = safe_bool(row.get("dirty"), False)
            sequence = _int(row.get("sequence"), 0)
            owner = str(row.get("owner") or "")
            source = str(row.get("source") or "")
            updated_at = _float(row.get("updated_at"), 0.0)
            trust = str(row.get("trust") or "")
            source_kind = str(row.get("source_kind") or "")
            validity = str(row.get("validity") or "")
            transition = safe_bool(row.get("transition"), False)
        max_sequence = max(max_sequence, sequence)
        section_meta[name] = {
            "owner": owner,
            "source": source,
            "updated_at": updated_at,
            "valid": valid,
            "stale": stale,
            "dirty": dirty,
            "trust": trust,
            "source_kind": source_kind,
            "validity": validity,
            "transition": transition,
            "sequence": sequence,
        }
        # A valid owner may have no currently active Smartness fields (for
        # example a neutral Decision register).  That is a legal empty
        # projection, not a missing owner publication.
        source_present = (
            bool(value)
            if accepted_owned
            else bool(_mapping(row.get("value")))
        )
        if not valid or not source_present:
            missing_sections.append(name)
        if stale:
            stale_sections.append(name)
        if dirty:
            dirty_sections.append(name)

    radio = sections.get(REGISTER_RADIO, {})
    identity = sections.get(REGISTER_IDENTITY, {})
    frame_scope = transaction.frame_scope()
    projected_scope = _projected_frame_scope(identity=identity, radio=radio)
    if projected_scope != frame_scope:
        raise RuntimeError("smartness_accepted_frame_projection_scope_mismatch")
    source_facts = _smartness_boundary_value(
        transaction.source_facts.as_dict()
    )
    if not isinstance(source_facts, dict):
        raise TypeError("smartness_source_fact_projection_invalid")
    signature_payload = _semantic_signature_payload(
        identity=identity,
        radio=radio,
        section_meta=section_meta,
    )
    signature_payload["frame_scope"] = frame_scope
    signature_payload["source_fact_set_id"] = str(
        source_facts.get("fact_set_id") or ""
    )
    semantic_signature = _semantic_signature(signature_payload)
    value = {
        "schema": SCHEMA,
        "owner": OWNER,
        "source": SOURCE,
        "computed_at_ts": round(now_ts, 3),
        "register_sequence": max_sequence,
        "semantic_signature": semantic_signature,
        "frame_scope": frame_scope,
        "source_facts": source_facts,
        "identity_code": str(identity.get("identity_code") or ""),
        "model": str(identity.get("model") or ""),
        "provider_plmn": str(radio.get("provider_plmn") or ""),
        "current_pband": radio.get("serving_band") if "serving_band" in radio else radio.get("primary_band"),
        "ca_pool": list(_int_tuple(radio.get("configured_band_pool") or ())),
        "sections": sections,
        "section_meta": section_meta,
        "missing_sections": missing_sections,
        "stale_sections": stale_sections,
        "dirty_sections": dirty_sections,
        "projection_limit_bytes": MAX_SMARTNESS_FACT_FRAME_BYTES,
        "serialized_size_bytes": 0,
    }
    serialized_size = _stabilized_serialized_size(value)
    value["serialized_size_bytes"] = serialized_size
    if serialized_size > MAX_SMARTNESS_FACT_FRAME_BYTES:
        raise RuntimeError(
            "smartness_fact_frame_size_exceeded:"
            f"{serialized_size}>{MAX_SMARTNESS_FACT_FRAME_BYTES}"
        )
    register = bank.write_smartness(
        value=value,
        source=SOURCE,
        updated_at=now_ts,
        valid=True,
        stale=False,
        dirty=bool(dirty_sections),
        trust="transition" if dirty_sections else "derived",
        source_kind="derived",
        validity="dirty" if dirty_sections else "valid",
        transition=bool(dirty_sections),
        signature=semantic_signature,
    )
    return register.as_dict()


def smartness_source_transaction_from_accepted_generation(
    *,
    accepted_generation: CellularIntelligenceGeneration,
    source_facts: ActionOpportunitySourceFactSet,
) -> SmartnessSourceTransaction:
    """Build the sole typed Smartness publication transaction."""

    if not isinstance(accepted_generation, CellularIntelligenceGeneration):
        raise TypeError("smartness_accepted_cellular_generation_required")
    if not isinstance(source_facts, ActionOpportunitySourceFactSet):
        raise TypeError("smartness_source_fact_set_required")
    return SmartnessSourceTransaction(
        accepted_generation=accepted_generation,
        source_facts=source_facts,
    )


def smartness_fact_sections(register_value: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    value = _mapping(register_value)
    sections = _mapping(value.get("sections"))
    return {name: _mapping(sections.get(name)) for name in SMARTNESS_FACT_SECTIONS}


def smartness_fact_source_facts(
    register_value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate the frozen source-fact set retained by one frame."""

    value = _mapping(register_value)
    frame_scope = _mapping(value.get("frame_scope"))
    _require_frame_scope(frame_scope)
    fact_set = action_opportunity_source_fact_set_from_mapping(
        _mapping(value.get("source_facts"))
    )
    if _fact_set_scope(fact_set) != frame_scope:
        raise RuntimeError("smartness_retained_source_fact_scope_mismatch")
    return fact_set.as_dict()


def _accepted_frame_sections(
    frame: CellularIntelligenceGeneration,
) -> dict[str, dict[str, Any]]:
    """Project the seven physical sections from one immutable generation."""

    if not isinstance(frame, CellularIntelligenceGeneration):
        raise TypeError("smartness_accepted_cellular_generation_required")
    generation = frame.generation
    cellular = frame.cellular
    identity = frame.identity
    action = frame.action
    fault = frame.fault
    scope_fields = {
        "sample_sequence": int(generation.generation or 0),
        "source_sequence": int(generation.generation or 0),
        "generation": int(generation.generation or 0),
        "sample_ts": float(generation.sample_ts or 0.0),
        "accepted_at": float(generation.accepted_ts or 0.0),
        "accepted_ts": float(generation.accepted_ts or 0.0),
        "source_epoch": str(generation.source_epoch or ""),
        "session_scope_key": str(generation.session_scope_key or ""),
        "sample_age_s": float(generation.sample_age_s or 0.0),
        "sample_fresh": bool(generation.sample_fresh),
    }
    identity_section = {
        "identity_code": str(identity.identity_code or ""),
        "identity_status": str(identity.identity_status or ""),
        "manufacturer": str(identity.vendor or ""),
        "model": str(identity.model or ""),
        "hardware_sku": str(identity.hardware_sku or ""),
        "software_version": str(identity.software_version or ""),
        "firmware": str(identity.firmware or ""),
        "profile_id": str(identity.profile_id or ""),
        "access_type": str(identity.access_type or ""),
        "access_type_id": str(identity.access_type_id or ""),
        "backend": str(identity.backend or ""),
        "backend_family": str(identity.backend_family or ""),
        "provider_name": str(identity.provider_name or ""),
        "provider_plmn": str(identity.provider_plmn or ""),
        "identity_catalog_version": str(identity.identity_catalog_version or ""),
        "identity_catalog_checksum": str(identity.identity_catalog_checksum or ""),
        "catalog_version": str(identity.identity_catalog_version or ""),
        "catalog_checksum": str(identity.identity_catalog_checksum or ""),
        "accepted_generation_session_scope_key": str(
            generation.session_scope_key or ""
        ),
        **scope_fields,
    }
    radio_section = {
        **_accepted_object_projection(cellular, _SECTION_FIELD_ALLOWLIST[REGISTER_RADIO]),
        **scope_fields,
        "provider_plmn": str(identity.provider_plmn or ""),
        "provider_name": str(identity.provider_name or ""),
        "configured_band_pool": list(identity.configured_band_pool),
        "configured_band_pool_signature": str(
            identity.configured_band_pool_signature or ""
        ),
        "selected_bands": list(cellular.selected_band_set),
        "active_bands": list(cellular.active_band_set),
        "earfcn": int(cellular.earfcn_dl or 0),
        "packet_session_connected": fault.packet_session_connected,
        "carrier_details": _smartness_boundary_value(cellular.carriers),
        "cell_lock_active": bool(action.cell_locked),
        "cell_locked": bool(action.cell_locked),
        "cell_lock_pci": int(action.cell_lock_pci or 0),
        "cell_lock_earfcn": int(action.cell_lock_earfcn or 0),
        "session_uptime_s": float(action.session_uptime_s or 0.0),
        "nr5g_band": int(cellular.nr_band or 0),
        "nr5g_pci": int(cellular.nr_pci or 0),
        "nr5g_arfcn": int(cellular.nr_arfcn or 0),
        "nr5g_cell_id": int(cellular.nr_cell_id or 0),
    }
    modem_slow_section = _accepted_object_projection(
        cellular,
        _SECTION_FIELD_ALLOWLIST[REGISTER_MODEM_SLOW],
    )
    latency_section = {
        **_accepted_object_projection(
            frame.latency,
            _SECTION_FIELD_ALLOWLIST[REGISTER_LATENCY],
        ),
        **scope_fields,
    }
    quality_section = {
        **_accepted_object_projection(
            frame.quality,
            _SECTION_FIELD_ALLOWLIST[REGISTER_QUALITY],
        ),
        **scope_fields,
        "sinr_db": float(cellular.sinr_db or 0.0),
    }
    ca_section = {
        **_accepted_object_projection(
            cellular,
            _SECTION_FIELD_ALLOWLIST[REGISTER_CA],
        ),
        **scope_fields,
        "ca_signature": str(cellular.ca_band_signature or ""),
    }
    traffic_section = {
        **_accepted_object_projection(
            frame.traffic,
            _SECTION_FIELD_ALLOWLIST[REGISTER_TRAFFIC],
        ),
        **scope_fields,
        "traffic_presence_active": bool(action.traffic_presence_active),
        "traffic_presence_interval_s": float(
            action.traffic_presence_interval_s or 0.0
        ),
        "session_uptime_s": float(action.session_uptime_s or 0.0),
    }
    return {
        REGISTER_IDENTITY: _section_projection(REGISTER_IDENTITY, identity_section),
        REGISTER_RADIO: _section_projection(REGISTER_RADIO, radio_section),
        REGISTER_MODEM_SLOW: _section_projection(
            REGISTER_MODEM_SLOW,
            modem_slow_section,
        ),
        REGISTER_LATENCY: _section_projection(REGISTER_LATENCY, latency_section),
        REGISTER_QUALITY: _section_projection(REGISTER_QUALITY, quality_section),
        REGISTER_CA: _section_projection(REGISTER_CA, ca_section),
        REGISTER_TRAFFIC: _section_projection(REGISTER_TRAFFIC, traffic_section),
    }


def _accepted_object_projection(value: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {
        name: _smartness_boundary_value(getattr(value, name))
        for name in names
        if hasattr(value, name)
    }


def _smartness_boundary_value(value: Any) -> Any:
    """Convert one allow-listed accepted value into JSON boundary primitives."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _smartness_boundary_value(item)
            for key, item in value.items()
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _smartness_boundary_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, tuple) and all(
        isinstance(item, tuple)
        and len(item) == 2
        and isinstance(item[0], str)
        for item in value
    ):
        return {
            item[0]: _smartness_boundary_value(item[1])
            for item in value
        }
    if isinstance(value, (list, tuple)):
        return [_smartness_boundary_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [
            _smartness_boundary_value(item)
            for item in sorted(value, key=repr)
        ]
    raise TypeError(
        "smartness_boundary_value_unsupported:"
        f"{type(value).__module__}.{type(value).__qualname__}"
    )


def _section_projection(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Project only declared Smartness inputs from a general runtime value."""

    source = _mapping(value)
    if name == REGISTER_DECISION:
        return _decision_projection(source)
    if name == REGISTER_HEALTH:
        return _health_projection(source)
    fields = _SECTION_FIELD_ALLOWLIST.get(name)
    if fields is None:
        raise RuntimeError(f"smartness_section_projection_unknown:{name}")
    return {key: source[key] for key in fields if key in source}


def _decision_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    source = normalize_runtime_decision_projection(value)
    return {
        "schema": source["schema"],
        "owner": source["owner"],
        "action_availability": dict(source["action_availability"]),
        "prediction": dict(source["prediction"]),
    }


def _health_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    source = _mapping(value)
    out = {
        key: source[key]
        for key in (
            "state",
            "action_hold",
            "hold_action",
            "reason",
            "stage",
            "sample_ts",
            "updated_at",
        )
        if key in source
    }
    posture = _named_projection(
        source.get("controller_posture"),
        (
            "state",
            "time_in_state_s",
            "progress_scope_id",
            "bad_rf_recovery_step",
            "degraded_actions_tried",
            "congested_action_taken",
            "congested_last_check",
            "congestion_kind",
            "recovered_lane_bands",
            "recovered_lane_until",
        ),
    )
    if posture:
        out["controller_posture"] = posture
    return out


def _named_projection(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    source = _mapping(value)
    return {key: source[key] for key in fields if key in source}


def _projected_frame_scope(
    *, identity: Mapping[str, Any], radio: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "identity_code": str(identity.get("identity_code") or ""),
        "session_scope_key": str(
            radio.get("session_scope_key")
            or identity.get("accepted_generation_session_scope_key")
            or identity.get("session_scope_key")
            or ""
        ),
        "source_epoch": str(
            radio.get("source_epoch") or identity.get("source_epoch") or ""
        ),
        "source_sequence": _int(
            radio.get("sample_sequence")
            or radio.get("source_sequence")
            or radio.get("generation"),
            0,
        ),
    }


def _fact_set_scope(value: ActionOpportunitySourceFactSet) -> dict[str, Any]:
    return {
        "identity_code": str(value.identity_code or ""),
        "session_scope_key": str(value.session_scope_key or ""),
        "source_epoch": str(value.source_epoch or ""),
        "source_sequence": int(value.source_sequence or 0),
    }


def _require_frame_scope(scope: Mapping[str, Any]) -> None:
    if not str(scope.get("identity_code") or ""):
        raise ValueError("smartness_frame_identity_code_required")
    if not str(scope.get("session_scope_key") or ""):
        raise ValueError("smartness_frame_session_scope_key_required")
    if not str(scope.get("source_epoch") or ""):
        raise ValueError("smartness_frame_source_epoch_required")
    if _int(scope.get("source_sequence"), 0) <= 0:
        raise ValueError("smartness_frame_source_sequence_invalid")


def _stabilized_serialized_size(value: dict[str, Any]) -> int:
    previous = -1
    for _ in range(4):
        current = len(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        )
        if current == previous:
            return current
        value["serialized_size_bytes"] = current
        previous = current
    return previous


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _int_tuple(value: Any) -> tuple[int, ...]:
    if isinstance(value, str):
        value = value.replace("+", ",").split(",")
    if not isinstance(value, (list, tuple, set)):
        return ()
    out: list[int] = []
    for item in value:
        parsed = _int(item, 0)
        if parsed > 0 and parsed not in out:
            out.append(parsed)
    return tuple(out)


def _semantic_signature_payload(
    *,
    identity: Mapping[str, Any],
    radio: Mapping[str, Any],
    section_meta: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    sections: dict[str, dict[str, Any]] = {}
    for name in SMARTNESS_FACT_SECTIONS:
        meta = _mapping(section_meta.get(name))
        sections[name] = {
            "sequence": _int(meta.get("sequence"), 0),
            "valid": safe_bool(meta.get("valid"), False),
            "stale": safe_bool(meta.get("stale"), False),
            "dirty": safe_bool(meta.get("dirty"), False),
            "source_kind": str(meta.get("source_kind") or ""),
            "validity": str(meta.get("validity") or ""),
            "transition": safe_bool(meta.get("transition"), False),
        }
    return {
        "identity_code": str(identity.get("identity_code") or ""),
        "profile_id": str(identity.get("profile_id") or ""),
        "model": str(identity.get("model") or ""),
        "firmware": str(identity.get("firmware") or ""),
        "provider_plmn": str(radio.get("provider_plmn") or ""),
        "provider_name": str(radio.get("provider_name") or ""),
        "current_pband": _int(
            radio.get("serving_band") if "serving_band" in radio else radio.get("primary_band"),
            0,
        ),
        "ca_pool": list(_int_tuple(radio.get("configured_band_pool") or ())),
        "sections": sections,
    }


def _semantic_signature(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "MAX_SMARTNESS_FACT_FRAME_BYTES",
    "OWNER",
    "SCHEMA",
    "SMARTNESS_ACCEPTED_FRAME_SECTIONS",
    "SMARTNESS_FACT_SECTIONS",
    "SMARTNESS_OWNER_SNAPSHOT_SECTIONS",
    "SmartnessSourceTransaction",
    "publish_smartness_fact_register",
    "smartness_fact_sections",
    "smartness_fact_source_facts",
    "smartness_source_transaction_from_accepted_generation",
]
