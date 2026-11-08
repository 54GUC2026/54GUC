"""Immutable accepted-generation evidence for durable terminal rewards.

This is a bounded historical observation captured from the exact terminal
``CellularIntelligenceGeneration`` and matching ``DerivedSignalFrames``.  It is
not a live modem facade and cannot be converted back into controller domains.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import copy
import hashlib
import json
from typing import Any, Mapping

from arch4.controller_frames import (
    CellularIntelligenceGeneration,
    DerivedSignalFrames,
    controller_frame_value_to_mutable,
)
from arch4.rat_model import build_rat_snapshot, rat_learning_context


SCHEMA = "TerminalRewardObservation.v1"
OWNER = "arch4.terminal_reward_observation"


@dataclass(frozen=True, slots=True)
class TerminalGenerationObservation:
    generation: int
    sample_ts: float
    accepted_ts: float
    sample_fresh: bool
    sample_age_s: float
    session_scope_key: str
    source_epoch: str


@dataclass(frozen=True, slots=True)
class TerminalIdentityObservation:
    identity_code: str
    identity_status: str
    identity_catalog_version: str
    identity_catalog_checksum: str
    provider_plmn: str
    provider_name: str
    access_type: str
    access_type_id: str
    configured_band_pool: tuple[int, ...]
    configured_band_pool_signature: str


@dataclass(frozen=True, slots=True)
class TerminalCellularObservation:
    primary_band: int
    serving_band: int
    serving_band_signature: str
    selected_band_set: tuple[int, ...]
    selected_band_set_signature: str
    active_band_set: tuple[int, ...]
    active_band_set_signature: str
    band_set: tuple[int, ...]
    band_set_signature: str
    pci: int
    earfcn_dl: int
    cell_id: int
    enb_id: int
    sinr_db: float
    rsrp_dbm: float
    rsrq_db: float
    rssi_dbm: float
    primary_bw_mhz: float
    dl_bandwidth_mhz: float
    ul_bandwidth_mhz: float
    ca_active: bool
    ca_band_signature: str
    carrier_count: int
    ri: int
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
    nr_mode: str
    nsa_snapshot: Any = None


@dataclass(frozen=True, slots=True)
class TerminalTrafficObservation:
    dl_mbps: float
    ul_mbps: float
    traffic_valid: bool


@dataclass(frozen=True, slots=True)
class TerminalLatencyObservation:
    rtt_ms: float
    rtt_sliding_avg: float
    rtt_dl_jitter_pct: float


@dataclass(frozen=True, slots=True)
class TerminalQualityObservation:
    bq_cell: float
    bq_cell_valid: bool
    cq_cell: float
    cq_vector_a_valid: bool
    congestion_pct: float
    congestion_pct_valid: bool
    delivered_capacity_valid: bool
    delivered_capacity_ratio: float
    delivered_capacity_best_dl_mbps: float
    delivered_capacity_total_bw_mhz: float
    delivered_capacity_ceiling_mbps: float
    delivered_capacity_sample_count: int
    delivered_capacity_source: str
    nr_secondary_active: bool = False
    nr_transition_state: str = ""
    nr_transition_dirty: bool = False
    nr_identity_completeness: str = "absent"


@dataclass(frozen=True, slots=True)
class TerminalRewardObservation:
    generation: TerminalGenerationObservation
    identity: TerminalIdentityObservation
    cellular: TerminalCellularObservation
    traffic: TerminalTrafficObservation
    latency: TerminalLatencyObservation
    quality: TerminalQualityObservation
    observation_id: str

    def __post_init__(self) -> None:
        if self.generation.generation <= 0:
            raise ValueError("terminal_observation_generation_missing")
        if (
            not self.generation.session_scope_key
            or not self.generation.source_epoch
        ):
            raise ValueError("terminal_observation_generation_scope_missing")
        if not self.identity.identity_code:
            raise ValueError("terminal_observation_identity_code_missing")
        expected = _digest(self._content())
        if not self.observation_id or self.observation_id != expected:
            raise ValueError("terminal_observation_checksum_invalid")

    @classmethod
    def capture(
        cls,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
    ) -> "TerminalRewardObservation":
        if not isinstance(modem, CellularIntelligenceGeneration):
            raise TypeError(
                "terminal_observation_requires_cellular_intelligence"
            )
        if not isinstance(computed, DerivedSignalFrames):
            raise TypeError("terminal_observation_requires_derived_frames")
        generation = TerminalGenerationObservation(
            generation=int(modem.generation.generation),
            sample_ts=float(modem.generation.sample_ts),
            accepted_ts=float(modem.generation.accepted_ts),
            sample_fresh=bool(modem.generation.sample_fresh),
            sample_age_s=float(modem.generation.sample_age_s),
            session_scope_key=str(modem.generation.session_scope_key),
            source_epoch=str(modem.generation.source_epoch),
        )
        identity = TerminalIdentityObservation(
            identity_code=str(modem.identity.identity_code),
            identity_status=str(modem.identity.identity_status),
            identity_catalog_version=str(
                modem.identity.identity_catalog_version
            ),
            identity_catalog_checksum=str(
                modem.identity.identity_catalog_checksum
            ),
            provider_plmn=str(modem.identity.provider_plmn),
            provider_name=str(modem.identity.provider_name),
            access_type=str(modem.identity.access_type),
            access_type_id=str(modem.identity.access_type_id),
            configured_band_pool=tuple(
                int(value) for value in modem.identity.configured_band_pool
            ),
            configured_band_pool_signature=str(
                modem.identity.configured_band_pool_signature
            ),
        )
        cellular = TerminalCellularObservation(
            primary_band=int(modem.cellular.primary_band),
            serving_band=int(modem.cellular.serving_band),
            serving_band_signature=str(
                modem.cellular.serving_band_signature
            ),
            selected_band_set=tuple(
                int(value) for value in modem.cellular.selected_band_set
            ),
            selected_band_set_signature=str(
                modem.cellular.selected_band_set_signature
            ),
            active_band_set=tuple(
                int(value) for value in modem.cellular.active_band_set
            ),
            active_band_set_signature=str(
                modem.cellular.active_band_set_signature
            ),
            band_set=tuple(int(value) for value in modem.cellular.band_set),
            band_set_signature=str(modem.cellular.band_set_signature),
            pci=int(modem.cellular.pci),
            earfcn_dl=int(modem.cellular.earfcn_dl),
            cell_id=int(modem.cellular.cell_id),
            enb_id=int(modem.cellular.enb_id),
            sinr_db=float(modem.cellular.sinr_db),
            rsrp_dbm=float(modem.cellular.rsrp_dbm),
            rsrq_db=float(modem.cellular.rsrq_db),
            rssi_dbm=float(modem.cellular.rssi_dbm),
            primary_bw_mhz=float(modem.cellular.primary_bw_mhz),
            dl_bandwidth_mhz=float(modem.cellular.dl_bandwidth_mhz),
            ul_bandwidth_mhz=float(modem.cellular.ul_bandwidth_mhz),
            ca_active=bool(modem.cellular.ca_active),
            ca_band_signature=str(modem.cellular.ca_band_signature),
            carrier_count=int(modem.cellular.carrier_count),
            ri=int(modem.cellular.ri),
            nsa_active=bool(modem.cellular.nsa_active),
            sa_active=bool(modem.cellular.sa_active),
            nr_band=int(modem.cellular.nr_band),
            nr_pci=int(modem.cellular.nr_pci),
            nr_arfcn=int(modem.cellular.nr_arfcn),
            nr_cell_id=int(modem.cellular.nr_cell_id),
            nr_rsrp_dbm=float(modem.cellular.nr_rsrp_dbm),
            nr_rsrq_db=float(modem.cellular.nr_rsrq_db),
            nr_sinr_db=float(modem.cellular.nr_sinr_db),
            nr_bandwidth_mhz=float(modem.cellular.nr_bandwidth_mhz),
            nr_mode=str(modem.cellular.nr_mode),
            nsa_snapshot=controller_frame_value_to_mutable(
                computed.quality.nsa_snapshot
            ),
        )
        traffic = TerminalTrafficObservation(
            dl_mbps=float(modem.traffic.dl_mbps),
            ul_mbps=float(modem.traffic.ul_mbps),
            traffic_valid=bool(modem.traffic.traffic_valid),
        )
        latency = TerminalLatencyObservation(
            rtt_ms=float(computed.latency.rtt_ms),
            rtt_sliding_avg=float(computed.latency.rtt_sliding_avg),
            rtt_dl_jitter_pct=float(computed.latency.rtt_dl_jitter_pct),
        )
        quality = TerminalQualityObservation(
            bq_cell=float(computed.quality.bq_cell),
            bq_cell_valid=bool(computed.quality.bq_cell_valid),
            cq_cell=float(computed.quality.cq_cell),
            cq_vector_a_valid=bool(computed.quality.cq_vector_a_valid),
            congestion_pct=float(computed.quality.congestion_pct),
            congestion_pct_valid=bool(
                computed.quality.congestion_pct_valid
            ),
            delivered_capacity_valid=bool(
                computed.quality.delivered_capacity_valid
            ),
            delivered_capacity_ratio=float(
                computed.quality.delivered_capacity_ratio
            ),
            delivered_capacity_best_dl_mbps=float(
                computed.quality.delivered_capacity_best_dl_mbps
            ),
            delivered_capacity_total_bw_mhz=float(
                computed.quality.delivered_capacity_total_bw_mhz
            ),
            delivered_capacity_ceiling_mbps=float(
                computed.quality.delivered_capacity_ceiling_mbps
            ),
            delivered_capacity_sample_count=int(
                computed.quality.delivered_capacity_sample_count
            ),
            delivered_capacity_source=str(
                computed.quality.delivered_capacity_source
            ),
            nr_secondary_active=bool(
                computed.quality.nr_secondary_active
            ),
            nr_transition_state=str(
                computed.quality.nr_transition_state
            ),
            nr_transition_dirty=bool(
                computed.quality.nr_transition_dirty
            ),
            nr_identity_completeness=str(
                computed.quality.nr_identity_completeness
            ),
        )
        content = {
            "generation": asdict(generation),
            "identity": _json_value(asdict(identity)),
            "cellular": _json_value(asdict(cellular)),
            "traffic": asdict(traffic),
            "latency": asdict(latency),
            "quality": asdict(quality),
        }
        return cls(
            generation=generation,
            identity=identity,
            cellular=cellular,
            traffic=traffic,
            latency=latency,
            quality=quality,
            observation_id=_digest(content),
        )

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "TerminalRewardObservation":
        if not isinstance(raw, Mapping):
            raise TypeError("terminal_observation_mapping_required")
        row = dict(raw)
        if row.get("schema") != SCHEMA or row.get("owner") != OWNER:
            raise ValueError("terminal_observation_owner_schema_invalid")
        expected_keys = {
            "schema",
            "owner",
            "generation",
            "identity",
            "cellular",
            "traffic",
            "latency",
            "quality",
            "observation_id",
        }
        if set(row) != expected_keys:
            raise ValueError("terminal_observation_fields_invalid")
        row = _upgrade_pre_nsa_observation(row)
        return cls(
            generation=TerminalGenerationObservation(
                **_required_mapping(row, "generation")
            ),
            identity=TerminalIdentityObservation(
                **_tuple_fields(
                    _required_mapping(row, "identity"),
                    "configured_band_pool",
                )
            ),
            cellular=TerminalCellularObservation(
                **_tuple_fields(
                    _required_mapping(row, "cellular"),
                    "selected_band_set",
                    "active_band_set",
                    "band_set",
                )
            ),
            traffic=TerminalTrafficObservation(
                **_required_mapping(row, "traffic")
            ),
            latency=TerminalLatencyObservation(
                **_required_mapping(row, "latency")
            ),
            quality=TerminalQualityObservation(
                **_required_mapping(row, "quality")
            ),
            observation_id=str(row.get("observation_id") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            **self._content(),
            "observation_id": self.observation_id,
        }

    def rat_learning_context(
        self, *, profile: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        cellular = self.cellular
        identity = self.identity
        traffic = self.traffic
        quality = self.quality
        latency = self.latency
        payload = {
            "serving_band": cellular.serving_band or cellular.primary_band,
            "serving_band_signature": cellular.serving_band_signature,
            "configured_band_pool": list(identity.configured_band_pool),
            "configured_band_pool_signature": (
                identity.configured_band_pool_signature
            ),
            "active_band_set": list(cellular.active_band_set),
            "active_band_set_signature": cellular.active_band_set_signature,
            "pci": cellular.pci,
            "earfcn_dl": cellular.earfcn_dl,
            "cell_id": cellular.cell_id,
            "enb_id": cellular.enb_id,
            "rsrp_dbm": cellular.rsrp_dbm,
            "rsrq_db": cellular.rsrq_db,
            "sinr_db": cellular.sinr_db,
            "rssi_dbm": cellular.rssi_dbm,
            "dl_bandwidth_mhz": cellular.dl_bandwidth_mhz,
            "ul_bandwidth_mhz": cellular.ul_bandwidth_mhz,
            "ca_active": cellular.ca_active,
            "carrier_count": cellular.carrier_count,
            "nr_band": cellular.nr_band,
            "nr_pci": cellular.nr_pci,
            "nr_arfcn": cellular.nr_arfcn,
            "nr_cell_id": cellular.nr_cell_id,
            "nr_rsrp_dbm": cellular.nr_rsrp_dbm,
            "nr_rsrq_db": cellular.nr_rsrq_db,
            "nr_sinr_db": cellular.nr_sinr_db,
            "nr_bandwidth_mhz": cellular.nr_bandwidth_mhz,
            "nr_mode": cellular.nr_mode,
            "nsa_snapshot": copy.deepcopy(cellular.nsa_snapshot),
            "nr_secondary_active": quality.nr_secondary_active,
            "nr_transition_state": quality.nr_transition_state,
            "nr_transition_dirty": quality.nr_transition_dirty,
            "nr_identity_completeness": quality.nr_identity_completeness,
            "nsa_active": cellular.nsa_active,
            "sa_active": cellular.sa_active,
            "dl_mbps": traffic.dl_mbps,
            "ul_mbps": traffic.ul_mbps,
            "traffic_valid": traffic.traffic_valid,
            "rtt_ms": latency.rtt_ms or latency.rtt_sliding_avg,
            "rtt_jitter_pct": latency.rtt_dl_jitter_pct,
            "congestion_pct": quality.congestion_pct,
            "cq_norm_pct": (
                max(0.0, min(100.0, quality.cq_cell * 100.0))
                if quality.cq_cell <= 1.0
                else max(0.0, min(100.0, quality.cq_cell))
            )
            if quality.cq_vector_a_valid
            else 0.0,
        }
        return rat_learning_context(
            build_rat_snapshot(payload, profile=dict(profile or {}))
        )

    def _content(self) -> dict[str, Any]:
        return {
            "generation": asdict(self.generation),
            "identity": _json_value(asdict(self.identity)),
            "cellular": _json_value(asdict(self.cellular)),
            "traffic": asdict(self.traffic),
            "latency": asdict(self.latency),
            "quality": asdict(self.quality),
        }


def terminal_reward_observation_error(
    raw: Mapping[str, Any] | None,
    *,
    evidence_scope: Mapping[str, Any] | None = None,
) -> str:
    try:
        observation = TerminalRewardObservation.from_mapping(raw or {})
    except (TypeError, ValueError) as exc:
        return str(exc)
    scope = dict(evidence_scope or {})
    if not scope:
        return ""
    generation = observation.generation
    identity = observation.identity
    comparisons = (
        ("session_scope_key", generation.session_scope_key),
        ("source_epoch", generation.source_epoch),
        ("source_sequence", generation.generation),
        ("identity_code", identity.identity_code),
    )
    for field, expected in comparisons:
        current = scope.get(field)
        if current not in (None, "") and current != expected:
            return f"terminal_observation_scope_{field}_mismatch"
    return ""


def _required_mapping(row: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = row.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"terminal_observation_{key}_invalid")
    return dict(value)


def _tuple_fields(
    row: Mapping[str, Any], *keys: str
) -> dict[str, Any]:
    out = dict(row)
    for key in keys:
        value = out.get(key)
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"terminal_observation_{key}_invalid")
        out[key] = tuple(value)
    return out


def _upgrade_pre_nsa_observation(
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    """Permanent history adapter for pre-NSA terminal observations.

    Old rows are first authenticated with their original digest.  Only then are
    explicit unavailable NSA fields added and the canonical digest replaced.
    No legacy radio value is promoted into the new physical snapshot.
    """

    row = copy.deepcopy(dict(raw))
    cellular = _required_mapping(row, "cellular")
    quality = _required_mapping(row, "quality")
    if "nsa_snapshot" in cellular:
        return row
    old_content = {
        "generation": row["generation"],
        "identity": row["identity"],
        "cellular": row["cellular"],
        "traffic": row["traffic"],
        "latency": row["latency"],
        "quality": row["quality"],
    }
    if str(row.get("observation_id") or "") != _digest(old_content):
        raise ValueError("terminal_observation_checksum_invalid")
    cellular["nsa_snapshot"] = None
    quality.update(
        {
            "nr_secondary_active": False,
            "nr_transition_state": "",
            "nr_transition_dirty": False,
            "nr_identity_completeness": "absent",
        }
    )
    row["cellular"] = cellular
    row["quality"] = quality
    new_content = {
        "generation": row["generation"],
        "identity": row["identity"],
        "cellular": row["cellular"],
        "traffic": row["traffic"],
        "latency": row["latency"],
        "quality": row["quality"],
    }
    row["observation_id"] = _digest(new_content)
    return row


def _json_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in value.items()
        }
    return value


def _digest(content: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(content),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "OWNER",
    "SCHEMA",
    "TerminalRewardObservation",
    "terminal_reward_observation_error",
]
