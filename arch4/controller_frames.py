"""Immutable domain facts admitted above the protected Modem Live boundary.

``ModemInput`` and mutable ``ComputedSignals`` are legal only below
``ControllerDomainMaterializer``.  The materializer creates one complete
accepted generation plus a cellular-intelligence view that structurally omits
device-slow and presentation-only facts.
"""

from __future__ import annotations

import copy
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Mapping, TypeVar

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.canonical_delta_display import build_delta_display_surface
from arch4.coerce import safe_bool, safe_float, safe_int
from arch4.interfaces import (
    INPUT_DIAGNOSTIC_KEYS,
    ComputedSignals,
    ModemInput,
    band_signature,
    input_diagnostics,
    normalize_band_fields,
    normalize_band_list,
)
from arch4.modem.live_translation import build_modem_live_radio_surface, build_modem_live_traffic_surface


_DEVICE_CONTROL_PREFIXES = ("wifi", "wlan", "wireless", "device_control")
_MAX_MATERIALIZED_NAMESPACES = 32
COMPUTED_SIGNAL_CELLULAR_DISPOSITIONS = frozenset(
    {
        "nr_band",
        "nr_rsrp_dbm",
        "nr_rsrq_db",
        "nr_sinr_db",
        "nsa_active",
        "sa_active",
    }
)


def _reject_device_control_fields(values: Mapping[str, Any]) -> None:
    forbidden = sorted(
        str(key)
        for key in values
        if str(key).strip().lower().startswith(_DEVICE_CONTROL_PREFIXES)
    )
    if forbidden:
        raise ValueError(
            "cellular_frame_rejects_device_control_fields:"
            + ",".join(forbidden)
        )


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(
            sorted(
                (
                    (str(key), _freeze_value(item))
                    for key, item in value.items()
                ),
                key=lambda row: row[0],
            )
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze_value(item) for item in value), key=repr))
    try:
        return copy.deepcopy(value)
    except Exception as exc:
        raise TypeError(
            f"controller_domain_value_not_copyable:{type(exc).__name__}:{exc}"
        ) from exc


def _thaw_value(value: Any) -> Any:
    if isinstance(value, tuple):
        if all(
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            for item in value
        ):
            return {
                item[0]: _thaw_value(item[1])
                for item in value
            }
        return [_thaw_value(item) for item in value]
    return copy.deepcopy(value)


def controller_frame_value_to_mutable(value: Any) -> Any:
    """Return a detached mutable value from an immutable controller frame.

    This is the explicit adapter for durable/export boundaries.  Callers must
    not guess whether a frozen tuple represents a mapping or a sequence.
    """

    return _thaw_value(value)


@dataclass(frozen=True, slots=True)
class CarrierFrame:
    band: int
    earfcn: int | None
    pci: int | None
    bandwidth_mhz: float | None
    sinr_db: float | None
    rsrp_dbm: float | None
    state: int
    is_pcell: bool

    @classmethod
    def from_carrier(cls, carrier: Any) -> "CarrierFrame":
        def optional(field, convert):
            value=getattr(carrier,field,None)
            return convert(value) if value is not None else None
        return cls(
            band=safe_int(getattr(carrier, "band", 0)),
            earfcn=optional('earfcn',safe_int),
            pci=optional('pci',safe_int),
            bandwidth_mhz=optional('bandwidth_mhz',safe_float),
            sinr_db=optional('sinr_db',safe_float),
            rsrp_dbm=optional('rsrp_dbm',safe_float),
            state=safe_int(getattr(carrier, "state", 0)),
            is_pcell=safe_bool(getattr(carrier, "is_pcell", False), False),
        )


@dataclass(frozen=True, slots=True)
class NeighborFrame:
    band: int
    earfcn: int
    pci: int
    rsrp_dbm: float
    rsrq_db: float

    @classmethod
    def from_neighbor(cls, neighbor: Any) -> "NeighborFrame":
        return cls(
            band=safe_int(getattr(neighbor, "band", 0)),
            earfcn=safe_int(getattr(neighbor, "earfcn", 0)),
            pci=safe_int(getattr(neighbor, "pci", 0)),
            rsrp_dbm=safe_float(getattr(neighbor, "rsrp_dbm", 0.0), 0.0),
            rsrq_db=safe_float(getattr(neighbor, "rsrq_db", 0.0), 0.0),
        )


@dataclass(frozen=True, slots=True)
class GenerationFrame:
    generation: int
    sample_ts: float
    accepted_ts: float
    sample_source: str
    sample_fresh: bool
    sample_age_s: float
    session_scope_key: str
    source_epoch: str


@dataclass(frozen=True, slots=True)
class CellularRadioFrame:
    sinr_db: float
    rsrp_dbm: float
    rsrq_db: float
    rsrq_source: str
    rssi_dbm: float
    mcs_dl: int
    mcs_ul: int
    mcs_dl0: int
    mcs_dl1: int
    cqi: int
    cqi0: int
    cqi1: int
    cqi_source: str
    ri: int
    ri_valid: bool
    ri_source: str
    qam_dl0: Any
    qam_dl1: Any
    qam_dl_primary: int
    qam_dl_secondary: int
    qam_ul: Any
    qm_dl0: int
    qm_dl1: int
    qm_ul: int
    mod0: int
    mod1: int
    mcs_ul_raw: int
    mcs_ul_supported: bool
    qam_source: str
    qam_confidence: float
    tx_power: Any
    tbs_dl0: int
    tbs_dl1: int
    tbs_ul: int
    mcs_dl_retx: bool
    mcs_dl1_retx: bool
    mcs_ul_retx: bool
    primary_band: int
    serving_band: int
    serving_band_signature: str
    selected_band_set: tuple[int, ...]
    selected_band_set_signature: str
    active_band_set: tuple[int, ...]
    active_band_set_signature: str
    band_set: tuple[int, ...]
    band_set_signature: str
    ca_active: bool
    ca_band_signature: str
    ca_component_band_set: tuple[int, ...]
    ca_component_band_signature: str
    ca_component_count: int
    ca_composition_signature: str
    ca_source: str
    carrier_count: int
    primary_bw_mhz: float
    pci: int
    earfcn_dl: int
    earfcn_ul: int
    enb_id: int
    cell_id: int
    tac: int
    rrc: str
    trans_mode: str
    radio_connected: bool
    dl_bandwidth: str
    ul_bandwidth: str
    dl_bandwidth_mhz: float
    ul_bandwidth_mhz: float
    dl_freq_mhz: float
    ul_freq_mhz: float
    carriers: tuple[CarrierFrame, ...]
    neighbors: tuple[NeighborFrame, ...]
    nsa_active: bool
    sa_active: bool
    nr_band: int
    nr_pci: int
    nr_arfcn: int
    nr_cell_id: int
    nr_rsrp_dbm: float
    nr_rsrq_db: float
    nr_sinr_db: float
    nr_bandwidth_mhz: float
    nr_scs_khz: float
    nr_mode: str
    nr_cells: tuple[Any, ...]

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "CellularRadioFrame":
        _reject_device_control_fields(values)
        return cls(**dict(values))


@dataclass(frozen=True, slots=True)
class TrafficFrame:
    dl_mbps: float
    ul_mbps: float
    rx_bytes_per_sec: float
    tx_bytes_per_sec: float
    has_traffic: bool
    traffic_valid: bool
    traffic_state: str
    traffic_state_detail: str
    traffic_invalid_reason: str
    traffic_day_download_bytes: int
    traffic_day_upload_bytes: int
    traffic_day_used_bytes: int
    traffic_month_download_bytes: int
    traffic_month_upload_bytes: int
    traffic_total_download_bytes: int
    traffic_total_upload_bytes: int
    traffic_session_download_bytes: int
    traffic_session_upload_bytes: int
    traffic_device_download_counter_bytes: int | None
    traffic_device_upload_counter_bytes: int | None
    traffic_accounting_source: str
    traffic_accounting_status: str
    traffic_accounting_month_key: str
    traffic_accounting_day_key: str
    traffic_volume_poll_interval_s: float
    traffic_volume_last_sample_ts: float
    traffic_volume_next_sample_ts: float
    traffic_volume_age_s: float | None
    traffic_rate_source: str
    traffic_quota_start_day: int | None
    traffic_quota_limit_label: str
    traffic_quota_left_bytes: int | None
    traffic_quota_limit_bytes: int | None
    traffic_quota_month_threshold_pct: float | None
    traffic_quota_day_threshold_pct: float | None


@dataclass(frozen=True, slots=True)
class LatencyFrame:
    rtt_ms: float
    rtt_ema: float
    rtt_sliding_avg: float
    rtt_baseline: float
    rtt_effective_baseline: float
    rtt_delta: float
    rtt_delta_valid: bool
    rtt_elevated: bool
    rtt_alarm: bool
    rtt_probe_ok: bool
    rtt_pressure_level: str
    rtt_quiet_delta: float
    rtt_quiet_held: bool
    rtt_delta_block_reason: str
    rtt_congestion_avg: float
    rtt_recent_median: float
    rtt_active_traffic_class: str
    rtt_active_load_mbps: float
    rtt_active_jitter_mad_ms: float
    rtt_active_jitter_step_p95_ms: float
    rtt_active_jitter_samples: int
    rtt_active_jitter_valid: bool
    rtt_dl_jitter_bucket: str
    rtt_dl_jitter_trimmed_ms: float
    rtt_dl_jitter_baseline_ms: float
    rtt_dl_jitter_delta_ms: float
    rtt_dl_jitter_pct: float
    rtt_dl_jitter_valid: bool
    rtt_dl_jitter_reason: str


@dataclass(frozen=True, slots=True)
class QualityFrame:
    quality_model: str
    quality_capacity_fraction: float | None
    quality_total_bw_mhz: float | None
    quality_radio_inputs: Any
    quality_valid: bool
    quality_reason: str
    bq_cell: float
    bq_cell_valid: bool
    bq_cell_source: str
    cq_cell: float
    cq_cell_source: str
    cq_coverage: float
    cq_vector_a_valid: bool
    cqb_cell: float
    cqb_cell_valid: bool
    cqb_cell_source: str
    cqb_coverage: float
    ca_raw_state: int
    cart: int
    cast: float
    ca_stat_scope_key: str
    cart_ld: int
    cart_ld_valid: bool
    cast_ld: float
    cast_ld_valid: bool
    cart_activity_per_hour: float
    cart_ld_activity: float
    ri_stability_pct: float
    ri_activity_per_hour: float
    ri_stability_valid: bool
    ca_stat_pct: float
    ca_stat_ld_pct: float
    ca_stat_ld_valid: bool
    congestion_qam_gap: float
    congestion_qualifier_high: bool
    congestion_qualifier_mod: bool
    rsrq_slope: float
    rsrq_degrading: bool
    rsrq_baseline: float
    rsrq_baseline_valid: bool
    rsrq_baseline_samples: int
    rsrq_gap: float
    rsrq_gap_valid: bool
    congestion_supported: bool
    congestion_support_reason: str
    congestion_pct: float
    congestion_pct_valid: bool
    congestion_internal_bucket: str
    congestion_bucket: str
    congestion_state_avg_pct: float
    congestion_state_bucket: str
    congestion_raw_pct: float
    congestion_source: str
    congestion_rsrq_held: bool
    congestion_rsrq_available: bool
    congestion_rsrq_contributes: bool
    congestion_rsrq_reason: str
    congestion_rsrq_pct: float
    congestion_rsrq_gap_db: float
    congestion_rsrq_gap_threshold_db: float
    congestion_fast_mode: str
    congestion_hold_mode: str
    congestion_trusted_rtt: bool
    congestion_rtt_base_pct: float
    congestion_trusted_rsrq: bool
    congestion_trusted_active_jitter: bool
    congestion_active_jitter_pct: float
    congestion_slope: float
    congestion_rising: bool
    congestion_flags_valid: bool
    congestion_bad_now: bool
    congestion_pressure_high: bool
    congestion_pressure_moderate: bool
    congestion_kind_hint: str
    delivered_capacity_valid: bool
    delivered_capacity_ratio: float
    delivered_capacity_best_dl_mbps: float
    delivered_capacity_total_bw_mhz: float
    delivered_capacity_ceiling_mbps: float
    delivered_capacity_sample_count: int
    delivered_capacity_source: str
    dl_bottleneck_state: str
    dl_bottleneck_reason: str
    dl_bottleneck_proof_level: str
    dl_bottleneck_missing_evidence: tuple[str, ...]
    dl_bottleneck_rejected_explanations: tuple[str, ...]
    dl_bottleneck_evidence: Any
    dl_bottleneck_owner: str
    dl_bottleneck_schema_version: str
    cell_structural_change: bool
    cell_structural_change_fresh: bool
    cell_structural_change_kind: str
    cell_structural_change_class: str
    cell_structural_change_service_impact: bool
    cell_change_pci_before: int
    cell_change_earfcn_before: int
    cell_change_band_before: int
    cell_change_ca_sig_before: str
    input_freeze_samples: int
    nr_available: bool
    nr_rsrp_ema: float
    nr_rsrp_ema_valid: bool
    nr_recovering: bool
    nr_just_dropped: bool
    nr_secondary_active: bool
    nr_transition_state: str
    nr_transition_dirty: bool
    nr_identity_completeness: str
    nsa_snapshot: Any
    nr_capacity_evidence: Any


@dataclass(frozen=True, slots=True)
class IdentityCapabilityFrame:
    provider_name: str
    provider_plmn: str
    vendor: str
    model: str
    hardware_sku: str
    hardware_version: str
    firmware: str
    software_version: str
    webui: str
    profile_id: str
    backend: str
    access_type: str
    access_type_id: str
    backend_family: str
    network_type: str
    network_type_ex: str
    configured_band_pool: tuple[int, ...]
    configured_band_pool_signature: str
    configured_band_pool_source: str
    configured_band_pool_status: str
    configured_band_pool_age_s: float | None
    configured_band_pool_last_sample_ts: float
    identity_key: str
    identity_code: str
    identity_status: str
    identity_catalog_version: str
    identity_catalog_checksum: str


@dataclass(frozen=True, slots=True)
class DeviceSlowFrame:
    temperature_modem_5g_c: float
    antenna: str
    antenna_mode: str
    antenna_mode_source: str
    antenna_switch_api_supported: bool | None
    external_antenna_ports_present: bool | None


@dataclass(frozen=True, slots=True)
class ActionTransactionFrame:
    modem_live_io_quiet_active: bool
    modem_live_io_quiet_remaining_s: float
    live_register_quiet_hold: bool
    live_register_hold_reason: str
    session_uptime_s: float
    session_uptime_source: str
    traffic_presence_active: bool
    traffic_presence_interval_s: float
    cell_locked: bool
    cell_lock_pci: int
    cell_lock_earfcn: int


@dataclass(frozen=True, slots=True)
class FaultFrame:
    connection_status_code: str
    packet_session_connected: bool | None
    lte_interface_disabled: bool
    thermal_emergency_active: bool
    diagnostics: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PresentationFrame:
    ca_state_text: str
    radio_state_text: str
    nr_state_text: str
    rtt_delta_display_ms: float | None
    rtt_delta_display_valid: bool
    rtt_delta_display_held: bool
    rtt_delta_display_live_ts: float
    cq_cell_display: float
    cq_cell_display_valid: bool
    cq_cell_display_held: bool
    cart_ld_display: int
    cart_ld_display_valid: bool
    cq_cqi_source: str
    cqi_mcs_gap_ratio: float
    cqi_mcs_gap_smoothed: float
    cqi_mcs_gap_valid: bool
    cqi_mcs_expected_mcs: int
    cqi_mcs_observed_mcs: int
    cqi_mcs_gap_idx: int
    mcs_fast_ema: float
    mcs_slow_ema: float
    mcs_collapse: bool
    rsrq_display_gap: float
    rsrq_display_gap_valid: bool
    rsrq_display_gap_held: bool
    rsrq_delta_display_db: float | None
    rsrq_delta_display_valid: bool
    rsrq_delta_display_held: bool
    rsrq_delta_display_live_ts: float
    rsrq_baseline_display_db: float | None
    rsrq_baseline_display_valid: bool
    rsrq_baseline_display_held: bool
    rsrq_baseline_display_live_ts: float
    congestion_display_pct: float | None
    congestion_display_valid: bool
    congestion_trend_display: str


@dataclass(frozen=True, slots=True)
class DerivedSignalFrames:
    latency: LatencyFrame
    quality: QualityFrame


@dataclass(frozen=True, slots=True)
class RadioSelectionFrame:
    """One accepted lower-radio selection snapshot.

    This is observation truth, not a command request.  Selected/configured
    pools are kept separate from the active CA composition because the latter
    may legitimately change from tick to tick without a selection transition.
    """

    serving_band_available: bool = False
    serving_band_unavailable_reason: str = "not_observed"
    primary_band: int = 0
    pci: int | None = None
    earfcn: int = 0
    selected_band_set: tuple[int, ...] = ()
    configured_band_pool: tuple[int, ...] = ()
    active_band_set: tuple[int, ...] = ()
    ca_signature: str = ""
    cell_id: int | None = None
    network_type: str = ""
    nr_band: int | None = None
    nr_pci: int | None = None
    nr_arfcn: int | None = None
    nr_cell_id: int | None = None


@dataclass(frozen=True, slots=True)
class RadioTransitionFrame:
    """Canonical attribution for the current radio-selection episode."""

    matrix_identity: MatrixGenerationIdentity
    schema: str = "radio_transition.v1"
    owner: str = "arch4.radio_transition_authority.RadioTransitionAuthority"
    attribution_state: str = "unavailable"
    attribution_available: bool = False
    attribution_error: str = "not_observed"
    source: str = "attribution_unavailable"
    episode_id: str = ""
    transition_id: str = ""
    source_generation: int = 0
    episode_started_generation: int = 0
    fresh: bool = False
    structural_fresh: bool = False
    lower_controller_learning_opportunity: bool = False
    change_kind: str = ""
    change_class: str = ""
    service_impact: bool = False
    command_root_id: str = ""
    command_id: str = ""
    command_origin: str = ""
    command_lever: str = ""
    command_target_generation: int = 0
    before: RadioSelectionFrame = field(default_factory=RadioSelectionFrame)
    after: RadioSelectionFrame = field(default_factory=RadioSelectionFrame)

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("radio_transition_matrix_identity_required")
        if (
            self.schema != "radio_transition.v1"
            or self.owner
            != "arch4.radio_transition_authority.RadioTransitionAuthority"
        ):
            raise ValueError("radio_transition_owner_schema_invalid")
        if self.attribution_state not in {
            "valid",
            "unavailable",
            "quarantined",
        }:
            raise ValueError("radio_transition_attribution_state_invalid")
        if (
            type(self.source_generation) is not int
            or self.source_generation
            != self.matrix_identity.matrix_sequence
        ):
            raise ValueError("radio_transition_matrix_identity_mismatch")
        if self.attribution_state == "valid":
            if not self.attribution_available or self.attribution_error:
                raise ValueError("radio_transition_valid_state_invalid")
        elif self.attribution_available or not self.attribution_error:
            raise ValueError("radio_transition_invalid_state_evidence_required")


def radio_transition_frame_to_mapping(
    frame: RadioTransitionFrame,
) -> dict[str, Any]:
    """Encode one transition with the canonical flat Matrix identity."""

    if not isinstance(frame, RadioTransitionFrame):
        raise TypeError("radio_transition_frame_required")
    payload = asdict(frame)
    payload["matrix_identity"] = frame.matrix_identity.as_dict()
    return payload


def radio_transition_frame_from_mapping(
    payload: Mapping[str, Any],
) -> RadioTransitionFrame:
    """Decode the immutable transition carried by a Core observation."""

    if not isinstance(payload, Mapping):
        raise TypeError("radio_transition_mapping_required")
    row = dict(payload)
    matrix_identity_raw = row.pop("matrix_identity", None)
    before_raw = row.pop("before", {})
    after_raw = row.pop("after", {})
    if not isinstance(before_raw, Mapping) or not isinstance(after_raw, Mapping):
        raise ValueError("radio_transition_selection_mapping_required")
    try:
        frame = RadioTransitionFrame(
            **row,
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                matrix_identity_raw
            ),
            before=RadioSelectionFrame(**dict(before_raw)),
            after=RadioSelectionFrame(**dict(after_raw)),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("radio_transition_mapping_invalid") from exc
    if (
        frame.schema != "radio_transition.v1"
        or frame.owner
        != "arch4.radio_transition_authority.RadioTransitionAuthority"
        or frame.source_generation <= 0
    ):
        raise ValueError("radio_transition_owner_schema_invalid")
    return frame


@dataclass(frozen=True, slots=True)
class CoreCellularObservationGeneration:
    generation: GenerationFrame
    cellular: CellularRadioFrame
    traffic: TrafficFrame
    latency: LatencyFrame
    quality: QualityFrame
    identity: IdentityCapabilityFrame
    action: ActionTransactionFrame
    fault: FaultFrame
    radio_transition: RadioTransitionFrame


CellularIntelligenceGeneration = CoreCellularObservationGeneration


@dataclass(frozen=True, slots=True)
class AcceptedControllerGeneration:
    intelligence: CoreCellularObservationGeneration
    derived: DerivedSignalFrames
    device_slow: DeviceSlowFrame
    presentation: PresentationFrame


_FrameT = TypeVar("_FrameT")


def _frame_from_computed(frame_type: type[_FrameT], computed: ComputedSignals) -> _FrameT:
    values = {
        item.name: _freeze_value(getattr(computed, item.name))
        for item in fields(frame_type)
    }
    return frame_type(**values)


def _owner_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _identity_owner_value(
    owner_values: Mapping[str, Any],
    key: str,
    *,
    modem: ModemInput,
    default: Any = "",
) -> Any:
    identity_key = _owner_mapping(owner_values.get("modem_identity_key"))
    verified_identity = _owner_mapping(
        owner_values.get("verified_modem_identity")
    )
    runtime_identity = _owner_mapping(owner_values.get("runtime_identity"))
    runtime_identity_key = _owner_mapping(runtime_identity.get("modem_identity_key"))
    for source in (
        owner_values,
        identity_key,
        verified_identity,
        runtime_identity,
        runtime_identity_key,
    ):
        value = source.get(key)
        if value not in (None, ""):
            return value
    value = getattr(modem, key, default)
    return default if value in (None, "") else value


class ControllerDomainMaterializer:
    """Sole stateful constructor for accepted upper-layer domain facts."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_sequence_by_namespace: OrderedDict[tuple[str, str], int] = (
            OrderedDict()
        )

    def materialize(
        self,
        *,
        modem: ModemInput,
        computed: ComputedSignals,
        radio_transition: RadioTransitionFrame,
        owner_values: Mapping[str, Any] | None,
        accepted_at: float,
        source_sequence: int,
        source_epoch: str,
        session_scope_key: str,
    ) -> AcceptedControllerGeneration:
        if not isinstance(modem, ModemInput):
            raise TypeError("controller_domain_materializer_requires_modem_input")
        if not isinstance(computed, ComputedSignals):
            raise TypeError(
                "controller_domain_materializer_requires_computed_signals"
            )
        if not isinstance(radio_transition, RadioTransitionFrame):
            raise TypeError(
                "controller_domain_materializer_requires_radio_transition"
            )
        sequence = int(source_sequence or 0)
        scope = str(session_scope_key or "").strip()
        epoch = str(source_epoch or "").strip()
        if sequence <= 0 or not scope or not epoch:
            raise ValueError("controller_domain_generation_identity_incomplete")
        if (
            radio_transition.source_generation != sequence
            or radio_transition.matrix_identity.matrix_sequence != sequence
            or radio_transition.matrix_identity.session_scope_key != scope
            or radio_transition.matrix_identity.source_epoch != epoch
        ):
            raise ValueError(
                "controller_domain_radio_transition_generation_mismatch"
            )
        raw_sequence = safe_int(getattr(modem, "sample_sequence", 0))
        if raw_sequence > 0 and raw_sequence != sequence:
            raise ValueError("controller_domain_generation_sequence_mismatch")
        namespace = (scope, epoch)
        with self._lock:
            previous = int(self._last_sequence_by_namespace.get(namespace, 0) or 0)
            if sequence <= previous:
                raise RuntimeError(
                    "controller_domain_generation_already_materialized"
                    if sequence == previous
                    else "controller_domain_generation_regression"
                )

        owner_values = (
            owner_values if isinstance(owner_values, Mapping) else {}
        )
        modem_surface = build_modem_live_radio_surface(
            modem=modem,
            raw_base_status=owner_values,
        )
        normalized_bands = normalize_band_fields(
            serving_band=safe_int(modem_surface.get("serving_band", 0))
            or safe_int(getattr(modem, "primary_band", 0)),
            selected_band_set=modem_surface.get("selected_band_set", ()),
            selected_band_set_signature=str(
                modem_surface.get("selected_band_set_signature", "") or ""
            ),
            active_band_set=getattr(modem, "active_band_set", []),
            active_band_set_signature=str(
                getattr(modem, "active_band_set_signature", "") or ""
            ),
            band_set=getattr(modem, "band_set", []),
            ca_active=safe_bool(getattr(modem, "ca_active", False), False),
            ca_state_text=str(getattr(modem, "ca_state_text", "") or ""),
        )
        configured_pool = normalize_band_list(
            modem_surface.get("configured_band_pool")
            or owner_values.get("configured_band_pool")
            or getattr(modem, "configured_band_pool", [])
            or []
        )
        configured_pool_signature = str(
            modem_surface.get("configured_band_pool_signature")
            or owner_values.get("configured_band_pool_signature")
            or getattr(modem, "configured_band_pool_signature", "")
            or ""
        )
        if not configured_pool_signature and configured_pool:
            configured_pool_signature = band_signature(configured_pool)
        sample_ts = safe_float(getattr(modem, "timestamp", 0.0), 0.0)
        accepted_ts = safe_float(accepted_at, 0.0)
        if sample_ts <= 0.0 or accepted_ts <= 0.0:
            raise ValueError("controller_domain_generation_timestamp_invalid")
        sample_age_s = safe_float(getattr(modem, "sample_age_s", 0.0), 0.0)
        if sample_age_s <= 0.0:
            sample_age_s = max(0.0, accepted_ts - sample_ts)
        nr_cells = tuple(
            _freeze_value(cell)
            for cell in (getattr(modem, "nr_cells", ()) or ())
            if isinstance(cell, Mapping)
        )
        nr_bandwidth_mhz = safe_float(
            getattr(modem, "nr_bandwidth_mhz", 0.0), 0.0
        )
        carriers = tuple(
            CarrierFrame.from_carrier(carrier)
            for carrier in (getattr(modem, "carriers", ()) or ())
        )
        neighbors = tuple(
            NeighborFrame.from_neighbor(neighbor)
            for neighbor in (getattr(modem, "neighbors", ()) or ())
        )
        temperature = safe_float(
            getattr(modem, "temperature_modem_5g_c", 0.0), 0.0
        )

        generation = GenerationFrame(
            generation=sequence,
            sample_ts=sample_ts,
            accepted_ts=accepted_ts,
            sample_source=str(getattr(modem, "sample_source", "") or ""),
            sample_fresh=safe_bool(getattr(modem, "sample_fresh", True), True),
            sample_age_s=sample_age_s,
            session_scope_key=scope,
            source_epoch=epoch,
        )
        cellular = CellularRadioFrame(
            sinr_db=safe_float(modem_surface.get("sinr_db", 0.0), 0.0),
            rsrp_dbm=safe_float(modem_surface.get("rsrp_dbm", 0.0), 0.0),
            rsrq_db=safe_float(modem_surface.get("rsrq_db", 0.0), 0.0),
            rsrq_source=str(
                modem_surface.get("rsrq_source", "reported") or "reported"
            ),
            rssi_dbm=safe_float(modem_surface.get("rssi_dbm", 0.0), 0.0),
            mcs_dl=safe_int(modem_surface.get("mcs_dl", 0)),
            mcs_ul=safe_int(modem_surface.get("mcs_ul", 0)),
            mcs_dl0=safe_int(modem_surface.get("mcs_dl0", 0)),
            mcs_dl1=safe_int(modem_surface.get("mcs_dl1", 0)),
            cqi=safe_int(modem_surface.get("cqi", 0)),
            cqi0=safe_int(modem_surface.get("cqi0", 0)),
            cqi1=safe_int(modem_surface.get("cqi1", 0)),
            cqi_source=str(
                modem_surface.get("cqi_source", "reported") or "reported"
            ),
            ri=safe_int(modem_surface.get("ri", 0)),
            ri_valid=safe_bool(
                modem_surface.get(
                    "ri_valid",
                    safe_int(modem_surface.get("ri", 0)) > 0,
                ),
                False,
            ),
            ri_source=str(modem_surface.get("ri_source", "") or ""),
            qam_dl0=_freeze_value(modem_surface.get("qam_dl0", "")),
            qam_dl1=_freeze_value(modem_surface.get("qam_dl1", "")),
            qam_dl_primary=safe_int(
                modem_surface.get("qam_dl_primary", 0)
            ),
            qam_dl_secondary=safe_int(
                modem_surface.get("qam_dl_secondary", 0)
            ),
            qam_ul=_freeze_value(modem_surface.get("qam_ul", "")),
            qm_dl0=safe_int(modem_surface.get("qm_dl0", 0)),
            qm_dl1=safe_int(modem_surface.get("qm_dl1", 0)),
            qm_ul=safe_int(modem_surface.get("qm_ul", 0)),
            mod0=safe_int(modem_surface.get("mod0", 0)),
            mod1=safe_int(modem_surface.get("mod1", 0)),
            mcs_ul_raw=safe_int(modem_surface.get("mcs_ul_raw", 0)),
            mcs_ul_supported=safe_bool(
                modem_surface.get("mcs_ul_supported", True),
                True,
            ),
            qam_source=str(modem_surface.get("qam_source", "") or ""),
            qam_confidence=safe_float(
                modem_surface.get("qam_confidence", 0.0),
                0.0,
            ),
            tx_power=_freeze_value(modem_surface.get("tx_power", "")),
            tbs_dl0=safe_int(modem_surface.get("tbs_dl0", 0)),
            tbs_dl1=safe_int(modem_surface.get("tbs_dl1", 0)),
            tbs_ul=safe_int(modem_surface.get("tbs_ul", 0)),
            mcs_dl_retx=safe_bool(
                modem_surface.get("mcs_dl_retx", False),
                False,
            ),
            mcs_dl1_retx=safe_bool(
                modem_surface.get("mcs_dl1_retx", False),
                False,
            ),
            mcs_ul_retx=safe_bool(
                modem_surface.get("mcs_ul_retx", False),
                False,
            ),
            primary_band=safe_int(getattr(modem, "primary_band", 0)),
            serving_band=safe_int(normalized_bands.serving_band),
            serving_band_signature=str(
                normalized_bands.serving_band_signature or ""
            ),
            selected_band_set=tuple(
                safe_int(value)
                for value in normalized_bands.selected_band_set
            ),
            selected_band_set_signature=str(
                normalized_bands.selected_band_set_signature or ""
            ),
            active_band_set=tuple(
                safe_int(value)
                for value in normalized_bands.active_band_set
            ),
            active_band_set_signature=str(
                normalized_bands.active_band_set_signature or ""
            ),
            band_set=tuple(
                safe_int(value) for value in normalized_bands.band_set
            ),
            band_set_signature=str(
                normalized_bands.band_set_signature or ""
            ),
            ca_active=safe_bool(getattr(modem, "ca_active", False), False),
            ca_band_signature=str(
                modem_surface.get("ca_signature", "") or ""
            ),
            ca_component_band_set=tuple(
                safe_int(value)
                for value in normalize_band_list(
                    getattr(modem, "ca_component_band_set", ()) or ()
                )
            ),
            ca_component_band_signature=str(
                getattr(modem, "ca_component_band_signature", "") or ""
            ),
            ca_component_count=safe_int(
                getattr(modem, "ca_component_count", 0)
            ),
            ca_composition_signature=str(
                getattr(modem, "ca_composition_signature", "") or ""
            ),
            ca_source=str(getattr(modem, "ca_source", "") or ""),
            carrier_count=safe_int(modem_surface.get("carrier_count", 0)),
            primary_bw_mhz=safe_float(
                getattr(modem, "primary_bw_mhz", 0.0), 0.0
            ),
            pci=safe_int(modem_surface.get("pci", 0)),
            earfcn_dl=safe_int(modem_surface.get("earfcn_dl", 0)),
            earfcn_ul=safe_int(modem_surface.get("earfcn_ul", 0)),
            enb_id=safe_int(modem_surface.get("enb_id", 0)),
            cell_id=safe_int(modem_surface.get("cell_id", 0)),
            tac=safe_int(modem_surface.get("tac", 0)),
            rrc=str(getattr(modem, "rrc", "") or ""),
            trans_mode=str(getattr(modem, "trans_mode", "") or ""),
            radio_connected=safe_bool(
                getattr(modem, "radio_connected", False), False
            ),
            dl_bandwidth=str(modem_surface.get("dl_bandwidth", "") or ""),
            ul_bandwidth=str(modem_surface.get("ul_bandwidth", "") or ""),
            dl_bandwidth_mhz=safe_float(
                modem_surface.get("dl_bandwidth_mhz", 0.0), 0.0
            ),
            ul_bandwidth_mhz=safe_float(
                modem_surface.get("ul_bandwidth_mhz", 0.0), 0.0
            ),
            dl_freq_mhz=safe_float(
                modem_surface.get("dl_freq_mhz", 0.0), 0.0
            ),
            ul_freq_mhz=safe_float(
                modem_surface.get("ul_freq_mhz", 0.0), 0.0
            ),
            carriers=carriers,
            neighbors=neighbors,
            nsa_active=safe_bool(getattr(modem, "nsa_active", False), False),
            sa_active=safe_bool(getattr(modem, "sa_active", False), False),
            nr_band=safe_int(getattr(modem, "nr_band", 0)),
            nr_pci=safe_int(getattr(modem, "nr_pci", 0)),
            nr_arfcn=safe_int(getattr(modem, "nr_arfcn", 0)),
            nr_cell_id=safe_int(getattr(modem, "nr_cell_id", 0)),
            nr_rsrp_dbm=safe_float(
                getattr(modem, "nr_rsrp_dbm", 0.0), 0.0
            ),
            nr_rsrq_db=safe_float(
                getattr(modem, "nr_rsrq_db", 0.0), 0.0
            ),
            nr_sinr_db=safe_float(
                getattr(modem, "nr_sinr_db", 0.0), 0.0
            ),
            nr_bandwidth_mhz=nr_bandwidth_mhz,
            nr_scs_khz=safe_float(
                getattr(modem, "nr_scs_khz", 0.0), 0.0
            ),
            nr_mode=str(getattr(modem, "nr_mode", "") or ""),
            nr_cells=nr_cells,
        )
        traffic_surface = build_modem_live_traffic_surface(
            modem=modem, raw_base_status=owner_values,
        )
        traffic = TrafficFrame(
            dl_mbps=safe_float(
                traffic_surface.get("dl_mbps", 0.0), 0.0
            ),
            ul_mbps=safe_float(
                traffic_surface.get("ul_mbps", 0.0), 0.0
            ),
            rx_bytes_per_sec=safe_float(
                traffic_surface.get("rx_bytes_per_sec", 0.0), 0.0
            ),
            tx_bytes_per_sec=safe_float(
                traffic_surface.get("tx_bytes_per_sec", 0.0), 0.0
            ),
            has_traffic=safe_bool(traffic_surface.get("has_traffic", False), False),
            traffic_valid=safe_bool(traffic_surface.get("traffic_valid", True), True),
            traffic_state=str(
                getattr(modem, "traffic_state", "idle") or "idle"
            ),
            traffic_state_detail=str(
                traffic_surface.get("traffic_state_detail", "") or ""
            ),
            traffic_invalid_reason=str(
                traffic_surface.get("traffic_invalid_reason", "") or ""
            ),
            traffic_day_download_bytes=safe_int(
                traffic_surface.get("traffic_day_download_bytes", 0)
            ),
            traffic_day_upload_bytes=safe_int(
                traffic_surface.get("traffic_day_upload_bytes", 0)
            ),
            traffic_day_used_bytes=safe_int(
                traffic_surface.get("traffic_day_used_bytes", 0)
            ),
            traffic_month_download_bytes=safe_int(
                traffic_surface.get("traffic_month_download_bytes", 0)
            ),
            traffic_month_upload_bytes=safe_int(
                traffic_surface.get("traffic_month_upload_bytes", 0)
            ),
            traffic_total_download_bytes=safe_int(
                traffic_surface.get("traffic_total_download_bytes", 0)
            ),
            traffic_total_upload_bytes=safe_int(
                traffic_surface.get("traffic_total_upload_bytes", 0)
            ),
            traffic_session_download_bytes=safe_int(
                traffic_surface.get("traffic_session_download_bytes", 0)
            ),
            traffic_session_upload_bytes=safe_int(
                traffic_surface.get("traffic_session_upload_bytes", 0)
            ),
            traffic_device_download_counter_bytes=(
                safe_int(
                    traffic_surface.get(
                        "traffic_device_download_counter_bytes",
                        0,
                    )
                )
                if traffic_surface.get(
                    "traffic_device_download_counter_bytes"
                ) is not None
                else None
            ),
            traffic_device_upload_counter_bytes=(
                safe_int(
                    traffic_surface.get(
                        "traffic_device_upload_counter_bytes",
                        0,
                    )
                )
                if traffic_surface.get(
                    "traffic_device_upload_counter_bytes"
                ) is not None
                else None
            ),
            traffic_accounting_source=str(
                traffic_surface.get("traffic_accounting_source", "") or ""
            ),
            traffic_accounting_status=str(
                traffic_surface.get("traffic_accounting_status", "") or ""
            ),
            traffic_accounting_month_key=str(
                traffic_surface.get("traffic_accounting_month_key", "") or ""
            ),
            traffic_accounting_day_key=str(
                traffic_surface.get("traffic_accounting_day_key", "") or ""
            ),
            traffic_volume_poll_interval_s=safe_float(
                traffic_surface.get("traffic_volume_poll_interval_s", 0.0),
                0.0,
            ),
            traffic_volume_last_sample_ts=safe_float(
                traffic_surface.get("traffic_volume_last_sample_ts", 0.0),
                0.0,
            ),
            traffic_volume_next_sample_ts=safe_float(
                traffic_surface.get("traffic_volume_next_sample_ts", 0.0),
                0.0,
            ),
            traffic_volume_age_s=(
                safe_float(
                    traffic_surface.get("traffic_volume_age_s", 0.0), 0.0
                )
                if traffic_surface.get("traffic_volume_age_s") is not None
                else None
            ),
            traffic_rate_source=str(
                traffic_surface.get("traffic_rate_source", "") or ""
            ),
            traffic_quota_start_day=(
                safe_int(traffic_surface.get("traffic_quota_start_day", 0))
                if traffic_surface.get("traffic_quota_start_day") is not None
                else None
            ),
            traffic_quota_limit_label=str(
                traffic_surface.get("traffic_quota_limit_label", "") or ""
            ),
            traffic_quota_left_bytes=(
                safe_int(traffic_surface.get("traffic_quota_left_bytes", 0))
                if traffic_surface.get("traffic_quota_left_bytes") is not None
                else None
            ),
            traffic_quota_limit_bytes=(
                safe_int(traffic_surface.get("traffic_quota_limit_bytes", 0))
                if traffic_surface.get("traffic_quota_limit_bytes") is not None
                else None
            ),
            traffic_quota_month_threshold_pct=(
                safe_float(
                    traffic_surface.get(
                        "traffic_quota_month_threshold_pct",
                        0.0,
                    ),
                    0.0,
                )
                if traffic_surface.get(
                    "traffic_quota_month_threshold_pct"
                ) is not None
                else None
            ),
            traffic_quota_day_threshold_pct=(
                safe_float(
                    traffic_surface.get(
                        "traffic_quota_day_threshold_pct",
                        0.0,
                    ),
                    0.0,
                )
                if traffic_surface.get(
                    "traffic_quota_day_threshold_pct"
                ) is not None
                else None
            ),
        )
        latency = _frame_from_computed(LatencyFrame, computed)
        quality = _frame_from_computed(QualityFrame, computed)
        identity = IdentityCapabilityFrame(
            provider_name=str(
                _identity_owner_value(
                    owner_values,
                    "provider_name",
                    modem=modem,
                )
                or ""
            ),
            provider_plmn=str(
                _identity_owner_value(
                    owner_values,
                    "provider_plmn",
                    modem=modem,
                )
                or ""
            ),
            vendor=str(
                _identity_owner_value(
                    owner_values, "vendor", modem=modem
                )
                or ""
            ),
            model=str(
                _identity_owner_value(
                    owner_values, "model", modem=modem
                )
                or ""
            ),
            hardware_sku=str(
                _identity_owner_value(
                    owner_values,
                    "hardware_sku",
                    modem=modem,
                )
                or _identity_owner_value(
                    owner_values,
                    "hardware_version",
                    modem=modem,
                )
                or ""
            ),
            hardware_version=str(
                _identity_owner_value(
                    owner_values,
                    "hardware_version",
                    modem=modem,
                )
                or ""
            ),
            firmware=str(
                _identity_owner_value(
                    owner_values, "firmware", modem=modem
                )
                or ""
            ),
            software_version=str(
                _identity_owner_value(
                    owner_values,
                    "software_version",
                    modem=modem,
                )
                or ""
            ),
            webui=str(
                _identity_owner_value(
                    owner_values, "webui", modem=modem
                )
                or ""
            ),
            profile_id=str(
                _identity_owner_value(
                    owner_values, "profile_id", modem=modem
                )
                or ""
            ),
            backend=str(owner_values.get("backend") or ""),
            access_type=str(
                _identity_owner_value(
                    owner_values, "access_type", modem=modem
                )
                or ""
            ),
            access_type_id=str(
                _identity_owner_value(
                    owner_values, "access_type_id", modem=modem
                )
                or ""
            ),
            backend_family=str(
                owner_values.get("backend_family")
                or owner_values.get("backend")
                or ""
            ),
            network_type=str(
                _identity_owner_value(
                    owner_values, "network_type", modem=modem
                )
                or ""
            ),
            network_type_ex=str(
                _identity_owner_value(
                    owner_values, "network_type_ex", modem=modem
                )
                or ""
            ),
            configured_band_pool=tuple(
                safe_int(value) for value in configured_pool
            ),
            configured_band_pool_signature=configured_pool_signature,
            configured_band_pool_source=str(
                modem_surface.get("configured_band_pool_source")
                or
                owner_values.get("configured_band_pool_source")
                or getattr(modem, "configured_band_pool_source", "")
                or ""
            ),
            configured_band_pool_status=str(
                modem_surface.get("configured_band_pool_status", "") or ""
            ),
            configured_band_pool_age_s=(
                safe_float(
                    modem_surface.get("configured_band_pool_age_s", 0.0),
                    0.0,
                )
                if modem_surface.get("configured_band_pool_age_s") is not None
                else None
            ),
            configured_band_pool_last_sample_ts=safe_float(
                modem_surface.get(
                    "configured_band_pool_last_sample_ts",
                    0.0,
                ),
                0.0,
            ),
            identity_key=str(
                _identity_owner_value(
                    owner_values, "identity_key", modem=modem
                )
                or ""
            ),
            identity_code=str(
                _identity_owner_value(
                    owner_values, "identity_code", modem=modem
                )
                or owner_values.get("modem_identity_code")
                or ""
            ),
            identity_status=str(
                _identity_owner_value(
                    owner_values, "identity_status", modem=modem
                )
                or ""
            ),
            identity_catalog_version=str(
                _identity_owner_value(
                    owner_values,
                    "identity_catalog_version",
                    modem=modem,
                )
                or owner_values.get("catalog_version")
                or ""
            ),
            identity_catalog_checksum=str(
                _identity_owner_value(
                    owner_values,
                    "identity_catalog_checksum",
                    modem=modem,
                )
                or owner_values.get("catalog_checksum")
                or ""
            ),
        )
        action = ActionTransactionFrame(
            modem_live_io_quiet_active=safe_bool(
                getattr(modem, "modem_live_io_quiet_active", False), False
            ),
            modem_live_io_quiet_remaining_s=safe_float(
                getattr(
                    modem,
                    "modem_live_io_quiet_remaining_s",
                    0.0,
                ),
                0.0,
            ),
            live_register_quiet_hold=safe_bool(
                getattr(modem, "live_register_quiet_hold", False), False
            ),
            live_register_hold_reason=str(
                getattr(modem, "live_register_hold_reason", "") or ""
            ),
            session_uptime_s=safe_float(
                getattr(modem, "session_uptime_s", 0.0), 0.0
            ),
            session_uptime_source=str(
                getattr(modem, "session_uptime_source", "") or ""
            ),
            traffic_presence_active=safe_bool(
                getattr(modem, "traffic_presence_active", False), False
            ),
            traffic_presence_interval_s=safe_float(
                getattr(modem, "traffic_presence_interval_s", 0.0),
                0.0,
            ),
            cell_locked=safe_bool(getattr(modem, "cell_locked", False), False),
            cell_lock_pci=safe_int(
                getattr(modem, "cell_lock_pci", 0)
            ),
            cell_lock_earfcn=safe_int(
                getattr(modem, "cell_lock_earfcn", 0)
            ),
        )
        diagnostics = tuple(
            sorted(
                (
                    (str(key), _freeze_value(value))
                    for key, value in input_diagnostics(modem).items()
                    if key in INPUT_DIAGNOSTIC_KEYS
                ),
                key=lambda row: row[0],
            )
        )
        fault = FaultFrame(
            connection_status_code=str(
                traffic_surface.get("connection_status_code", "") or ""
            ),
            packet_session_connected=(
                safe_bool(
                    traffic_surface.get("packet_session_connected", False),
                    False,
                )
                if traffic_surface.get("packet_session_connected")
                is not None
                else None
            ),
            lte_interface_disabled=safe_bool(
                getattr(modem, "lte_interface_disabled", False), False
            ),
            thermal_emergency_active=bool(temperature >= 65.0),
            diagnostics=diagnostics,
        )
        device_slow = DeviceSlowFrame(
            temperature_modem_5g_c=temperature,
            antenna=str(modem_surface.get("antenna", "") or ""),
            antenna_mode=str(modem_surface.get("antenna_mode", "") or ""),
            antenna_mode_source=str(
                modem_surface.get("antenna_mode_source", "") or ""
            ),
            antenna_switch_api_supported=(
                safe_bool(
                    modem_surface.get("antenna_switch_api_supported"),
                    False,
                )
                if modem_surface.get("antenna_switch_api_supported")
                is not None
                else None
            ),
            external_antenna_ports_present=(
                safe_bool(
                    modem_surface.get("external_antenna_ports_present"),
                    False,
                )
                if modem_surface.get("external_antenna_ports_present")
                is not None
                else None
            ),
        )
        presentation_values: dict[str, Any] = {
            item.name: _freeze_value(getattr(computed, item.name))
            for item in fields(PresentationFrame)
            if item.name
            not in {"ca_state_text", "radio_state_text", "nr_state_text"}
        }
        delta_display_keys = (
            "rtt_delta_display_ms",
            "rtt_delta_display_valid",
            "rtt_delta_display_held",
            "rtt_delta_display_live_ts",
            "rsrq_delta_display_db",
            "rsrq_delta_display_valid",
            "rsrq_delta_display_held",
            "rsrq_delta_display_live_ts",
            "rsrq_baseline_display_db",
            "rsrq_baseline_display_valid",
            "rsrq_baseline_display_held",
            "rsrq_baseline_display_live_ts",
        )
        delta_display_raw = {
            key: getattr(computed, key)
            for key in delta_display_keys
        }
        previous_delta_display = {
            key: owner_values.get(key)
            for key in delta_display_keys
        }
        presentation_values.update(
            build_delta_display_surface(
                raw=delta_display_raw,
                previous=previous_delta_display,
                now_ts=sample_ts,
                rsrq_supported=(
                    str(modem_surface.get("rsrq_source", "reported") or "reported")
                    .strip()
                    .lower()
                    != "unsupported"
                ),
            )
        )
        if not safe_bool(
            getattr(computed, "congestion_display_valid", False),
            False,
        ):
            presentation_values["congestion_display_pct"] = None
        presentation = PresentationFrame(
            ca_state_text=str(normalized_bands.ca_state_text or ""),
            radio_state_text=str(
                getattr(modem, "radio_state_text", "") or ""
            ),
            nr_state_text=str(
                getattr(modem, "nr_state_text", "") or ""
            ),
            **presentation_values,
        )
        intelligence = CoreCellularObservationGeneration(
            generation=generation,
            cellular=cellular,
            traffic=traffic,
            latency=latency,
            quality=quality,
            identity=identity,
            action=action,
            fault=fault,
            radio_transition=radio_transition,
        )
        accepted = AcceptedControllerGeneration(
            intelligence=intelligence,
            derived=DerivedSignalFrames(
                latency=latency,
                quality=quality,
            ),
            device_slow=device_slow,
            presentation=presentation,
        )
        with self._lock:
            self._last_sequence_by_namespace[namespace] = sequence
            self._last_sequence_by_namespace.move_to_end(namespace)
            while (
                len(self._last_sequence_by_namespace)
                > _MAX_MATERIALIZED_NAMESPACES
            ):
                self._last_sequence_by_namespace.popitem(last=False)
        return accepted


def project_controller_read_model(
    accepted: AcceptedControllerGeneration,
) -> dict[str, Any]:
    """Explicit flat projection for status, telemetry and forensic consumers."""

    if not isinstance(accepted, AcceptedControllerGeneration):
        raise TypeError("accepted_controller_generation_required")
    intelligence = accepted.intelligence
    row: dict[str, Any] = {}
    row.update(asdict(intelligence.cellular))
    row.update(asdict(intelligence.traffic))
    row.update(asdict(intelligence.latency))
    row.update(asdict(intelligence.quality))
    row.update(asdict(intelligence.identity))
    row.update(asdict(intelligence.action))
    row.update(asdict(intelligence.fault))
    row.update(asdict(accepted.device_slow))
    row.update(asdict(accepted.presentation))
    row.update(
        {
            "ts": intelligence.generation.sample_ts,
            "timestamp": intelligence.generation.sample_ts,
            "sample_ts": intelligence.generation.sample_ts,
            "engine_tick_ts": intelligence.generation.accepted_ts,
            "sample_fresh": intelligence.generation.sample_fresh,
            "sample_sequence": intelligence.generation.generation,
            "sample_age_s": intelligence.generation.sample_age_s,
            "sample_source": intelligence.generation.sample_source,
            "session_scope_key": intelligence.generation.session_scope_key,
            "source_epoch": intelligence.generation.source_epoch,
            "earfcn": intelligence.cellular.earfcn_dl,
            "ca_signature": intelligence.cellular.ca_band_signature,
            "dl_mbps": intelligence.traffic.dl_mbps,
            "ul_mbps": intelligence.traffic.ul_mbps,
            "throughput_dl_mbps": intelligence.traffic.dl_mbps,
            "throughput_ul_mbps": intelligence.traffic.ul_mbps,
            "provider": intelligence.identity.provider_name,
            "plmn": intelligence.identity.provider_plmn,
            "cq_cell_valid": intelligence.quality.cq_vector_a_valid,
            "input_diagnostics": {
                key: _thaw_value(value)
                for key, value in intelligence.fault.diagnostics
            },
        }
    )
    row["carriers"] = [
        asdict(carrier) for carrier in intelligence.cellular.carriers
    ]
    row["neighbors"] = [
        asdict(neighbor) for neighbor in intelligence.cellular.neighbors
    ]
    row["nr_cells"] = [
        _thaw_value(cell) for cell in intelligence.cellular.nr_cells
    ]
    row["radio_transition"] = radio_transition_frame_to_mapping(
        intelligence.radio_transition
    )
    row["dl_bottleneck_evidence"] = _thaw_value(
        intelligence.quality.dl_bottleneck_evidence
    )
    row["diagnostics"] = row.pop("diagnostics", ())
    return {
        key: _thaw_value(value)
        for key, value in row.items()
    }


__all__ = [
    "AcceptedControllerGeneration",
    "ActionTransactionFrame",
    "CarrierFrame",
    "CellularIntelligenceGeneration",
    "CoreCellularObservationGeneration",
    "CellularRadioFrame",
    "COMPUTED_SIGNAL_CELLULAR_DISPOSITIONS",
    "ControllerDomainMaterializer",
    "DerivedSignalFrames",
    "DeviceSlowFrame",
    "FaultFrame",
    "GenerationFrame",
    "IdentityCapabilityFrame",
    "LatencyFrame",
    "NeighborFrame",
    "PresentationFrame",
    "QualityFrame",
    "RadioSelectionFrame",
    "RadioTransitionFrame",
    "TrafficFrame",
    "controller_frame_value_to_mutable",
    "project_controller_read_model",
    "radio_transition_frame_from_mapping",
]
