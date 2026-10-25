"""Runtime register contracts for the clocked-runtime migration.

This module owns runtime register metadata and the first enforced register
authority boundary: radio identity.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Iterable, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from arch4.accepted_modem_generation import (
    AcceptedModemGeneration,
    RawModemGeneration,
)
from arch4.canonical_scheduler_display import (
    build_scheduler_display_contract,
    build_scheduler_display_surface,
)
from arch4.coerce import safe_bool, safe_float, safe_int
from arch4.core_contracts.current_state import (
    CoreCurrentField,
    CoreCurrentFieldState,
)
from arch4.core_contracts.current_health import (
    CoreCurrentHealthView,
    MODEM_LIVE_CURRENT_HEALTH_OWNER,
    ModemLiveCurrentHealthPublication,
    RuntimeHealthNamespaceBindingReceipt,
)
from arch4.core_contracts.action_reachability import (
    ACTION_REACHABILITY_OWNER,
    CoreCurrentReachabilityView,
    validate_current_reachability_register,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.identity_decode_replica_store import IdentityDecodeReplicaStore
from arch4.modem_live.compatibility_identity import (
    DecodeCatalogReplica,
    DecodeCatalogSnapshot,
    EXACT_STATUS as EXACT_IDENTITY_STATUS,
    UNKNOWN_IDENTITY_CODE,
    validated_identity_publication_copy,
)
from arch4.modem_live.truth_matrix import (
    MODEM_LIVE_TRUTH_MATRIX_OWNER,
    ValidatedTruthMatrixReader,
    validate_truth_matrix,
)
from arch4.operational_service_status_contract import (
    normalize_operational_service_status,
)
from arch4.runtime_boundary_contracts import RadioMutationWindow
from arch4.payload_budget import retained_payload_bytes
from arch4.runtime_register_contract_names import (
    SIGNAL_PROCESSING_REGISTER_NAMES,
    REGISTER_ACTION,
    REGISTER_ACTION_AUXILIARY,
    REGISTER_CA,
    REGISTER_CAPABILITY,
    REGISTER_OPERATIONAL_STATUS,
    REGISTER_DOWNSTREAM_HEALTH,
    REGISTER_INTERNET_REACHABILITY,
    REGISTER_HEALTH,
    REGISTER_IDENTITY,
    REGISTER_LATENCY,
    REGISTER_MODEM_LIVE_MATRIX,
    REGISTER_MODEM_SLOW,
    REGISTER_MODEM_TIMING,
    REGISTER_MUTATION,
    REGISTER_QUALITY,
    REGISTER_RADIO,
    REGISTER_RUNTIME_CLOCK,
    REGISTER_TRAFFIC,
)

MAX_RUNTIME_REGISTER_VALUE_BYTES = 2 * 1024 * 1024

RADIO_IDENTITY_KEYS: tuple[str, ...] = (
    "serving_band",
    "serving_band_signature",
    "cell_id",
    "pci",
    "earfcn_dl",
)
RADIO_IDENTITY_INT_KEYS: frozenset[str] = frozenset({"serving_band", "cell_id", "pci", "earfcn_dl"})
RADIO_IDENTITY_TRUSTED: frozenset[str] = frozenset({"live", "cached", "derived"})
RADIO_IDENTITY_SOURCE_KIND_TRUSTED: frozenset[str] = frozenset({"live", "cached", "derived"})
RADIO_IDENTITY_REQUIRED_ANCHOR = "serving_band"

RUNTIME_REGISTER_NAMES: tuple[str, ...] = (
    REGISTER_MODEM_LIVE_MATRIX,
    REGISTER_LATENCY,
    REGISTER_QUALITY,
    REGISTER_CA,
    REGISTER_HEALTH,
    REGISTER_RUNTIME_CLOCK,
    REGISTER_MUTATION,
    REGISTER_ACTION,
    REGISTER_ACTION_AUXILIARY,
    REGISTER_OPERATIONAL_STATUS,
    REGISTER_DOWNSTREAM_HEALTH,
    REGISTER_INTERNET_REACHABILITY,
)

RUNTIME_REGISTER_OWNER_MAP: dict[str, str] = {
    REGISTER_MODEM_LIVE_MATRIX: MODEM_LIVE_TRUTH_MATRIX_OWNER,
    REGISTER_LATENCY: "signal_processing_matrix",
    REGISTER_QUALITY: "signal_processing_matrix",
    REGISTER_CA: "signal_processing_matrix",
    REGISTER_HEALTH: MODEM_LIVE_CURRENT_HEALTH_OWNER,
    REGISTER_RUNTIME_CLOCK: "runtime_clock",
    REGISTER_MUTATION: "action_runtime",
    REGISTER_ACTION: "command_authority_projection",
    REGISTER_ACTION_AUXILIARY: "action_runtime_auxiliary",
    REGISTER_OPERATIONAL_STATUS: "operational_service_status",
    REGISTER_DOWNSTREAM_HEALTH: "downstream_health",
    REGISTER_INTERNET_REACHABILITY: ACTION_REACHABILITY_OWNER,
}

RUNTIME_REGISTER_PUBLIC_FIELD_PROJECTION: dict[str, dict[str, tuple[str, ...]]] = {
    REGISTER_LATENCY: {
        "rtt_raw_ms": ("rtt_raw_ms",),
        "rtt_raw_available": ("rtt_raw_available",),
        "rtt_raw_failure_reason": ("rtt_raw_failure_reason",),
        "rtt_raw_observed_at": ("rtt_raw_observed_at",),
        "rtt_raw_sample_sequence": ("rtt_raw_sample_sequence",),
        "rtt_raw_freshness": ("rtt_raw_freshness",),
        "rtt_raw_probe_status": ("rtt_raw_probe_status",),
        "rtt_probe_ok": ("rtt_probe_ok",),
        "rtt_low_load_latency_valid": ("rtt_low_load_latency_valid",),
        "rtt_low_load_latency_reason": ("rtt_low_load_latency_reason",),
        "rtt_low_load_latency_mbps": ("rtt_low_load_latency_mbps",),
        "jitter_formula": ("jitter_formula",),
        "rtt_ms": ("rtt_ms",),
        "rtt_sliding_avg": ("rtt_sliding_avg",),
        "rtt_baseline": ("rtt_baseline",),
        "rtt_baseline_ms": ("rtt_baseline_ms",),
        "rtt_effective_baseline": ("rtt_effective_baseline",),
        "rtt_quiet_delta": ("rtt_quiet_delta",),
        "rtt_quiet_held": ("rtt_quiet_held",),
        "rtt_delta": ("rtt_delta",),
        "rtt_delta_valid": ("rtt_delta_valid",),
        "rtt_delta_display_ms": ("rtt_delta_display_ms",),
        "rtt_delta_display_valid": ("rtt_delta_display_valid",),
        "rtt_delta_display_state": ("rtt_delta_display_state",),
        "rtt_delta_display_reason": ("rtt_delta_display_reason",),
        "rtt_delta_display_held": ("rtt_delta_display_held",),
        "rtt_delta_display_live_ts": ("rtt_delta_display_live_ts",),
        "rtt_dl_jitter_pct": ("rtt_dl_jitter_pct",),
        "rtt_dl_jitter_valid": ("rtt_dl_jitter_valid",),
        "rtt_dl_jitter_reason": ("rtt_dl_jitter_reason",),
        "rtt_dl_jitter_trimmed_ms": ("rtt_dl_jitter_trimmed_ms",),
        "rtt_dl_jitter_bucket": ("rtt_dl_jitter_bucket",),
        "rtt_dl_jitter_baseline_ms": ("rtt_dl_jitter_baseline_ms",),
        "rtt_dl_jitter_delta_ms": ("rtt_dl_jitter_delta_ms",),
        "rtt_active_jitter_mad_ms": ("rtt_active_jitter_mad_ms",),
    },
    REGISTER_QUALITY: {
        "congestion_weights": ("congestion_weights",),
        "congestion_pct": ("congestion_pct",),
        "congestion_pct_valid": ("congestion_pct_valid",),
        "congestion_display_pct": ("congestion_display_pct",),
        "congestion_display_valid": ("congestion_display_valid",),
        "congestion_supported": ("congestion_supported",),
        "congestion_support_reason": ("congestion_support_reason",),
        "congestion_bucket": ("congestion_bucket",),
        "congestion_state_bucket": ("congestion_state_bucket",),
        "congestion_state_avg_pct": ("congestion_state_avg_pct",),
        "congestion_kind_hint": ("congestion_kind_hint",),
        "congestion_raw_pct": ("congestion_raw_pct",),
        "congestion_trend_display": ("congestion_trend_display",),
        "congestion_flags_valid": ("congestion_flags_valid",),
        "congestion_bad_now": ("congestion_bad_now",),
        "congestion_pressure_high": ("congestion_pressure_high",),
        "congestion_pressure_moderate": ("congestion_pressure_moderate",),
        "congestion_fast_mode": ("congestion_fast_mode",),
        "congestion_hold_mode": ("congestion_hold_mode",),
        "congestion_trusted_rtt": ("congestion_trusted_rtt",),
        "congestion_rtt_base_pct": ("congestion_rtt_base_pct",),
        "congestion_trusted_rsrq": ("congestion_trusted_rsrq",),
        "congestion_trusted_active_jitter": (
            "congestion_trusted_active_jitter",
        ),
        "congestion_active_jitter_pct": ("congestion_active_jitter_pct",),
        "congestion_rsrq_available": ("congestion_rsrq_available",),
        "congestion_rsrq_contributes": ("congestion_rsrq_contributes",),
        "congestion_rsrq_reason": ("congestion_rsrq_reason",),
        "congestion_rsrq_pct": ("congestion_rsrq_pct",),
        "congestion_rsrq_gap_db": ("congestion_rsrq_gap_db",),
        "congestion_rsrq_gap_threshold_db": ("congestion_rsrq_gap_threshold_db",),
        "congestion_internal_bucket": ("congestion_internal_bucket",),
        "congestion_qam_gap": ("congestion_qam_gap",),
        "congestion_slope": ("congestion_slope",),
        "congestion_rising": ("congestion_rising",),
        "congestion_source": ("congestion_source",),
        "quality_model": ("quality_model",),
        "quality_capacity_fraction": ("quality_capacity_fraction",),
        "quality_total_bw_mhz": ("quality_total_bw_mhz",),
        "quality_radio_inputs": ("quality_radio_inputs",),
        "quality_valid": ("quality_valid",),
        "quality_reason": ("quality_reason",),
        "bq_cell": ("bq_cell",),
        "bq_cell_valid": ("bq_cell_valid",),
        "bq_cell_source": ("bq_cell_source",),
        "cq_cell": ("cq_cell",),
        "cq_cell_valid": ("cq_cell_valid",),
        "cq_cell_source": ("cq_cell_source",),
        "cq_cell_display": ("cq_cell_display",),
        "cq_cell_display_valid": ("cq_cell_display_valid",),
        "cqb_cell": ("cqb_cell",),
        "cqb_cell_valid": ("cqb_cell_valid",),
        "cqb_cell_source": ("cqb_cell_source",),
        "cqb_coverage": ("cqb_coverage",),
        "rsrq_baseline": ("rsrq_baseline",),
        "rsrq_baseline_valid": ("rsrq_baseline_valid",),
        "rsrq_gap": ("rsrq_gap",),
        "rsrq_gap_valid": ("rsrq_gap_valid",),
        "rsrq_baseline_display_db": ("rsrq_baseline_display_db",),
        "rsrq_baseline_display_valid": ("rsrq_baseline_display_valid",),
        "rsrq_baseline_display_held": ("rsrq_baseline_display_held",),
        "rsrq_baseline_display_live_ts": ("rsrq_baseline_display_live_ts",),
        "rsrq_delta_display_db": ("rsrq_delta_display_db",),
        "rsrq_delta_display_valid": ("rsrq_delta_display_valid",),
        "rsrq_delta_display_held": ("rsrq_delta_display_held",),
        "rsrq_delta_display_live_ts": ("rsrq_delta_display_live_ts",),
        "cqi": ("cqi",),
        "cqi0": ("cqi0",),
        "cqi1": ("cqi1",),
        "mcs_dl0": ("mcs_dl0",),
        "mcs_dl1": ("mcs_dl1",),
        "mcs_ul": ("mcs_ul",),
        "mcs_collapse": ("mcs_collapse",),
        "qam_dl0": ("qam_dl0",),
        "qam_dl1": ("qam_dl1",),
        "qam_ul": ("qam_ul",),
        "me0": ("me0",),
        "me1": ("me1",),
        "me_ul": ("me_ul",),
        "ri": ("ri",),
        "ri_valid": ("ri_valid",),
        "ri_source": ("ri_source",),
        "scheduler_zero_authoritative": ("scheduler_zero_authoritative",),
        "scheduler_stream1_available": ("scheduler_stream1_available",),
        "ri_stability_pct": ("ri_stability_pct",),
        "ri_stability_valid": ("ri_stability_valid",),
        "ri_activity_per_hour": ("ri_activity_per_hour",),
        "nr_secondary_active": ("nr_secondary_active",),
        "nr_transition_state": ("nr_transition_state",),
    },
    REGISTER_CA: {
        "ca_signature": ("ca_signature",),
        "ca_band_signature": ("ca_band_signature",),
        "ca_realtime_state": ("ca_realtime_state",),
        "ca_stat_pct": ("ca_stat_pct",),
        "ca_stat_ld_pct": ("ca_stat_ld_pct",),
        "ca_stat_ld_valid": ("ca_stat_ld_valid",),
        "ca_stat_scope_key": ("ca_stat_scope_key",),
        "ca_change_per_hour": ("ca_change_per_hour",),
        "ca_ld_change_per_hour": ("ca_ld_change_per_hour",),
        "cart": ("cart",),
        "cast": ("cast",),
        "cart_ld": ("cart_ld",),
        "cart_ld_state": ("cart_ld_state",),
        "cart_ld_reason": ("cart_ld_reason",),
        "cart_ld_capability": ("cart_ld_capability",),
        "cart_activity_per_hour": ("cart_activity_per_hour",),
        "cart_ld_activity": ("cart_ld_activity",),
        "ca_component_band_set": ("ca_component_band_set",),
        "ca_component_band_signature": ("ca_component_band_signature",),
        "ca_component_count": ("ca_component_count",),
        "ca_composition_state": ("ca_composition_state",),
        "ca_composition_reason": ("ca_composition_reason",),
        "ca_composition_capability": ("ca_composition_capability",),
        "ca_composition_signature": ("ca_composition_signature",),
        "ca_source": ("ca_source",),
    },
    REGISTER_ACTION: {
        "core_command_status": ("core_command_status",),
    },
    REGISTER_ACTION_AUXILIARY: {
        "action_capabilities": ("action_capabilities",),
        "band_locked": ("band_locked",),
        "band_locked_valid": ("band_locked_valid",),
        "band_locked_reason": ("band_locked_reason",),
        "cell_locked": ("cell_locked",),
        "cell_locked_valid": ("cell_locked_valid",),
        "cell_locked_reason": ("cell_locked_reason",),
        "manual_cell_lock_timer": ("manual_cell_lock_timer",),
        "internet_pause": ("internet_pause",),
    },
    REGISTER_RUNTIME_CLOCK: {
        "uptime_s": ("uptime_s",),
        "clock_ms": ("clock_ms",),
        "started_at": ("started_at",),
    },
    REGISTER_HEALTH: {
        "facts": ("facts",),
    },
    REGISTER_OPERATIONAL_STATUS: {
        "latest": ("latest",),
        "by_operation": ("by_operation",),
    },
    REGISTER_MUTATION: {
        "action_allowed": ("action_allowed",),
        "reason": ("reason",),
        "sources": ("sources",),
    },
    REGISTER_DOWNSTREAM_HEALTH: {
        "message": ("message",),
        "enabled": ("enabled",),
        "ok": ("ok",),
        "target": ("target",),
        "ping_count": ("ping_count",),
        "interval_s": ("interval_s",),
        "last_rtt_ms": ("last_rtt_ms",),
    },
    REGISTER_INTERNET_REACHABILITY: {
        "disposition": ("disposition",),
        "outage_s": ("outage_s",),
        "outage_started_at": ("outage_started_at",),
        "latest_completed_outage": ("latest_completed_outage",),
        "latest_coverage_gap": ("latest_coverage_gap",),
        "latest_probe_at": ("latest_probe_at",),
        "register_sequence": ("register_sequence",),
        "reason": ("reason",),
    },
}

REGISTER_TRUST_LEVELS: frozenset[str] = frozenset(
    {
        "live",
        "cached",
        "derived",
        "unsupported",
        "unknown",
        "dirty",
        "stale",
        "transition",
        "validated_copy",
    }
)
REGISTER_SOURCE_KINDS: frozenset[str] = frozenset(
    {"live", "cached", "derived", "unsupported", "unknown", "modem_live_replica"}
)
REGISTER_VALIDITIES: frozenset[str] = frozenset({"valid", "stale", "dirty", "unknown"})


class RuntimeRegisterError(ValueError):
    """Base exception for runtime register contract violations."""


class RuntimeRegisterOwnershipError(RuntimeRegisterError):
    """Raised when a non-owner attempts to write a register."""


class AcceptedSignalGenerationSuperseded(RuntimeRegisterError):
    """Raised when derived truth belongs to an older raw runtime generation."""


@dataclass(frozen=True)
class RuntimeRegister:
    name: str
    value: Any = None
    updated_at: float = 0.0
    source: str = ""
    valid: bool = True
    stale: bool = False
    dirty: bool = False
    trust: str = "unknown"
    source_kind: str = "unknown"
    validity: str = "unknown"
    transition: bool = False
    owner: str = ""
    error: str = ""
    sequence: int = 0
    signature: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": _freeze_register_value(self.value),
            "updated_at": self.updated_at,
            "source": self.source,
            "valid": self.valid,
            "stale": self.stale,
            "dirty": self.dirty,
            "trust": self.trust,
            "source_kind": self.source_kind,
            "validity": self.validity,
            "transition": self.transition,
            "owner": self.owner,
            "error": self.error,
            "sequence": self.sequence,
            "signature": str(self.signature or ""),
        }


@dataclass(frozen=True)
class RuntimeRegisterWrite:
    name: str
    owner: str
    value: Any
    source: str
    updated_at: float
    valid: bool = True
    stale: bool = False
    dirty: bool = False
    trust: str = "live"
    source_kind: str = ""
    validity: str = ""
    transition: bool | None = None
    error: str = ""
    signature: str = ""
    force: bool = False


@dataclass(frozen=True)
class AcceptedGenerationRegisterWrite:
    """Deep-immutable accepted-root register projection."""

    name: str
    owner: str
    value: CoreFrozenPayload
    source: str
    updated_at: float
    valid: bool = True
    stale: bool = False
    dirty: bool = False
    trust: str = "live"
    source_kind: str = ""
    validity: str = ""
    transition: bool | None = None
    error: str = ""
    signature: str = ""
    force: bool = False

    @classmethod
    def from_runtime_write(
        cls,
        write: RuntimeRegisterWrite,
    ) -> "AcceptedGenerationRegisterWrite":
        if not isinstance(write, RuntimeRegisterWrite):
            raise TypeError("accepted_register_runtime_write_required")
        if not isinstance(write.value, Mapping):
            raise TypeError("accepted_register_mapping_value_required")
        return cls(
            name=write.name,
            owner=write.owner,
            value=CoreFrozenPayload.from_mapping(write.value),
            source=write.source,
            updated_at=write.updated_at,
            valid=write.valid,
            stale=write.stale,
            dirty=write.dirty,
            trust=write.trust,
            source_kind=write.source_kind,
            validity=write.validity,
            transition=write.transition,
            error=write.error,
            signature=write.signature,
            force=write.force,
        )

    def thaw(self) -> RuntimeRegisterWrite:
        return RuntimeRegisterWrite(
            name=self.name,
            owner=self.owner,
            value=self.value.to_mapping(),
            source=self.source,
            updated_at=self.updated_at,
            valid=self.valid,
            stale=self.stale,
            dirty=self.dirty,
            trust=self.trust,
            source_kind=self.source_kind,
            validity=self.validity,
            transition=self.transition,
            error=self.error,
            signature=self.signature,
            force=self.force,
        )


@dataclass(frozen=True)
class RuntimeRegisterPublicationReceipt:
    """Small acknowledgement for one owner publication batch."""

    sequence: int
    published_names: tuple[str, ...]
    changed_names: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": int(self.sequence),
            "published_names": list(self.published_names),
            "changed_names": list(self.changed_names),
        }


@dataclass(frozen=True, slots=True)
class RuntimeHealthNamespaceBindingPlan:
    namespace: RuntimeGenerationNamespace
    previous_namespace: RuntimeGenerationNamespace | None
    previous_health: RuntimeRegister | None

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError(
                "runtime_health_binding_plan_namespace_required"
            )
        if self.previous_namespace is not None and not isinstance(
            self.previous_namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError(
                "runtime_health_binding_plan_previous_namespace_invalid"
            )
        if self.previous_health is not None and not isinstance(
            self.previous_health,
            RuntimeRegister,
        ):
            raise TypeError(
                "runtime_health_binding_plan_register_invalid"
            )


@dataclass(frozen=True)
class SignalProcessingRegisterTransaction:
    """One immutable SPM output batch for one source Matrix identity."""

    commit_id: str
    matrix_identity: MatrixGenerationIdentity
    writes: tuple[AcceptedGenerationRegisterWrite, ...]

    def __post_init__(self) -> None:
        if not str(self.commit_id or "").strip():
            raise ValueError("accepted register transaction commit id required")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError(
                "accepted register transaction Matrix identity required"
            )
        names = tuple(write.name for write in self.writes)
        if any(
            not isinstance(write, AcceptedGenerationRegisterWrite)
            for write in self.writes
        ):
            raise TypeError(
                "accepted_register_transaction_frozen_writes_required"
            )
        if names != SIGNAL_PROCESSING_REGISTER_NAMES:
            raise ValueError("signal processing register transaction is incomplete")
        if any(str(write.signature or "") != self.commit_id for write in self.writes):
            raise ValueError("accepted register transaction signature mismatch")

    def prepared_derived_evidence(self) -> dict[str, Any]:
        rows = {
            write.name: {
                "name": write.name,
                "owner": write.owner,
                "source": write.source,
                "updated_at": write.updated_at,
                "valid": write.valid,
                "stale": write.stale,
                "dirty": write.dirty,
                "trust": write.trust,
                "source_kind": write.source_kind,
                "validity": write.validity,
                "transition": bool(write.transition),
                "error": write.error,
                "signature": write.signature,
                "value": _freeze_register_value(
                    write.value.to_mapping()
                ),
            }
            for write in self.writes
            if write.name != REGISTER_MODEM_LIVE_MATRIX
        }
        return {
            "latency": rows[REGISTER_LATENCY],
            "quality": rows[REGISTER_QUALITY],
            "ca": rows[REGISTER_CA],
            "prepared_registers": {
                "names": [
                    REGISTER_LATENCY,
                    REGISTER_QUALITY,
                    REGISTER_CA,
                ],
            },
        }

    def derived_evidence(self, *, bank_sequence: int) -> dict[str, Any]:
        del bank_sequence
        raise RuntimeRegisterOwnershipError(
            "prepublication_register_receipt_forbidden:"
            "use_prepared_derived_evidence_and_outbox_cursor"
        )


@dataclass
class RuntimeRegisterBank:
    owner_map: Mapping[str, str] = field(default_factory=lambda: dict(RUNTIME_REGISTER_OWNER_MAP))
    identity_replica_path: str | None = None
    _registers: dict[str, RuntimeRegister] = field(default_factory=dict, init=False)
    _sequence: int = field(default=0, init=False)
    _last_changed: tuple[str, ...] = field(default_factory=tuple, init=False)
    _identity_catalog_replica: DecodeCatalogReplica = field(
        default_factory=DecodeCatalogReplica,
        init=False,
        repr=False,
        compare=False,
    )
    _identity_replica_store: IdentityDecodeReplicaStore | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _identity_replica_error: str = field(default="", init=False, repr=False, compare=False)
    _current_health_namespace: RuntimeGenerationNamespace | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.owner_map = dict(self.owner_map)
        for name in self.owner_map:
            _validate_register_name(name, self.owner_map)
        if self.identity_replica_path:
            store = IdentityDecodeReplicaStore(self.identity_replica_path)
            self._identity_replica_store = store
            self._identity_catalog_replica = store.replica
            if store.path.exists():
                try:
                    store.load()
                except Exception as exc:
                    # The corrupt disk candidate is quarantined.  The bank may
                    # accept a later live Modem Live publication, but it never
                    # exposes the rejected decode catalog as previous-good.
                    self._identity_replica_error = str(exc)

    def identity_decode_catalog_for_owner_restore(self) -> DecodeCatalogSnapshot | None:
        """Return the immutable validated catalog, never previous live identity."""
        return self._identity_catalog_replica.current

    def identity_replica_status(self) -> dict[str, Any]:
        current = self._identity_catalog_replica.current
        return {
            "configured": self._identity_replica_store is not None,
            "path": str(self.identity_replica_path or ""),
            "catalog_version": current.catalog_version if current is not None else 0,
            "catalog_checksum": current.catalog_checksum if current is not None else "",
            "valid": current is not None,
            "last_error": str(
                self._identity_replica_error
                or (
                    self._identity_replica_store.last_error
                    if self._identity_replica_store is not None
                    else self._identity_catalog_replica.last_error
                )
            ),
        }

    def install_identity_decode_replica(
        self,
        publication: Mapping[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Install a validated Modem Live publication for offline decoding.

        This does not publish runtime identity truth.  Replay/startup readers
        gain only an immutable decode replica; a live identity register still
        requires the typed Modem Live register-generation path.
        """

        if self._identity_replica_store is not None:
            try:
                validated = self._identity_replica_store.accept(
                    publication,
                    persist=persist,
                )
                self._identity_replica_error = ""
                return validated
            except Exception as exc:
                self._identity_replica_error = str(exc)
                raise
        return validated_identity_publication_copy(
            publication,
            replica=self._identity_catalog_replica,
        )

    def read_exact_identity_publication(self) -> dict[str, Any]:
        """Return exact durable identity only from current Modem Live session truth.

        The durable catalog replica is decode-only.  It cannot make a modem
        identity current by itself.  Current-session identity may be valid for
        GUI and licensing while still provisional; this reader deliberately
        rejects that case before returning durable evidence.
        """

        register = self.read(REGISTER_MODEM_LIVE_MATRIX)
        if (
            not safe_bool(register.valid, False)
            or safe_bool(register.stale, False)
            or safe_bool(register.dirty, False)
            or str(register.validity or "").strip().lower() != "valid"
            or str(register.owner or "")
            != str(self.owner_map[REGISTER_MODEM_LIVE_MATRIX])
            or not isinstance(register.value, Mapping)
        ):
            return {}
        try:
            value = ValidatedTruthMatrixReader(register.value).line_values(
                "identity"
            )
        except Exception:
            return {}
        identity_code = str(value.get("identity_code") or "").strip()
        if (
            str(value.get("identity_status") or "").strip()
            != EXACT_IDENTITY_STATUS
            or not identity_code
            or identity_code == UNKNOWN_IDENTITY_CODE
            or not str(register.value.get("session_scope_key") or "").strip()
            or not str(register.value.get("source_epoch") or "").strip()
        ):
            return {}
        return validated_identity_publication_copy(
            value,
            replica=self._identity_catalog_replica,
        )

    @property
    def sequence(self) -> int:
        with self._lock:
            return int(self._sequence)

    def write_modem_live_matrix(self, **kwargs: Any) -> RuntimeRegister:
        return self._write_owned(
            REGISTER_MODEM_LIVE_MATRIX,
            owner=str(self.owner_map[REGISTER_MODEM_LIVE_MATRIX]),
            **kwargs,
        )

    def write_modem_slow(self, **kwargs: Any) -> RuntimeRegister:
        raise RuntimeRegisterOwnershipError(
            "split Modem Live slow register is retired; publish the truth matrix"
        )

    def write_modem_timing(self, **kwargs: Any) -> RuntimeRegister:
        raise RuntimeRegisterOwnershipError(
            "split Modem Live timing register is retired; publish the truth matrix"
        )

    def write_traffic(self, **kwargs: Any) -> RuntimeRegister:
        raise RuntimeRegisterOwnershipError(
            "split Modem Live traffic register is retired; publish the truth matrix"
        )

    def write_radio(self, **kwargs: Any) -> RuntimeRegister:
        raise RuntimeRegisterOwnershipError(
            "split Modem Live radio register is retired; publish the truth matrix"
        )

    def write_latency(self, **kwargs: Any) -> RuntimeRegister:
        raise RuntimeRegisterOwnershipError(
            "latency register requires accepted Signal Compute generation publication"
        )

    def write_quality(self, **kwargs: Any) -> RuntimeRegister:
        raise RuntimeRegisterOwnershipError(
            "quality register requires accepted Signal Compute generation publication"
        )

    def write_ca(self, **kwargs: Any) -> RuntimeRegister:
        raise RuntimeRegisterOwnershipError(
            "ca register requires accepted Signal Compute generation publication"
        )

    def write_health(self, **kwargs: Any) -> RuntimeRegister:
        del kwargs
        raise RuntimeRegisterOwnershipError(
            "generic_health_write_illegal:"
            "use_typed_modem_live_current_health_publication"
        )

    def prepare_current_health_namespace_binding(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> RuntimeHealthNamespaceBindingPlan:
        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError(
                "runtime_register_health_namespace_required"
            )
        with self._lock:
            health = self._registers.get(REGISTER_HEALTH)
            return RuntimeHealthNamespaceBindingPlan(
                namespace=namespace,
                previous_namespace=self._current_health_namespace,
                previous_health=(
                    _copy_register(health)
                    if health is not None
                    else None
                ),
            )

    def commit_current_health_namespace_binding(
        self,
        plan: RuntimeHealthNamespaceBindingPlan,
    ) -> RuntimeHealthNamespaceBindingReceipt:
        if not isinstance(plan, RuntimeHealthNamespaceBindingPlan):
            raise TypeError(
                "runtime_health_namespace_binding_plan_required"
            )
        namespace = plan.namespace
        with self._lock:
            health = self._registers.get(REGISTER_HEALTH)
            expected_health = plan.previous_health
            if (
                self._current_health_namespace
                != plan.previous_namespace
                or (
                    (health is None) != (expected_health is None)
                    or (
                        health is not None
                        and expected_health is not None
                        and health != expected_health
                    )
                )
            ):
                raise RuntimeError(
                    "runtime_health_namespace_binding_plan_stale"
                )
            changed = self._current_health_namespace != namespace
            cleared = False
            if changed:
                cleared = self._registers.pop(
                    REGISTER_HEALTH,
                    None,
                ) is not None
                self._current_health_namespace = namespace
            return RuntimeHealthNamespaceBindingReceipt(
                namespace=namespace,
                changed=changed,
                cleared_previous_health=cleared,
            )

    def rollback_current_health_namespace_binding(
        self,
        plan: RuntimeHealthNamespaceBindingPlan,
    ) -> None:
        if not isinstance(plan, RuntimeHealthNamespaceBindingPlan):
            raise TypeError(
                "runtime_health_namespace_binding_plan_required"
            )
        with self._lock:
            current_health = self._registers.get(REGISTER_HEALTH)
            if (
                self._current_health_namespace
                == plan.previous_namespace
                and (
                    (current_health is None)
                    == (plan.previous_health is None)
                )
                and (
                    current_health is None
                    or plan.previous_health is None
                    or current_health == plan.previous_health
                )
            ):
                return
            if self._current_health_namespace != plan.namespace:
                raise RuntimeError(
                    "runtime_health_namespace_rollback_state_mismatch"
                )
            self._current_health_namespace = plan.previous_namespace
            if plan.previous_health is None:
                self._registers.pop(REGISTER_HEALTH, None)
            else:
                self._registers[REGISTER_HEALTH] = _copy_register(
                    plan.previous_health
                )

    def bind_current_health_namespace(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> dict[str, Any]:
        del namespace
        raise RuntimeError(
            "direct_health_namespace_binding_illegal:"
            "use_modem_sample_publication_atomic_bind"
        )

    def write_modem_live_current_health(
        self,
        publication: ModemLiveCurrentHealthPublication,
    ) -> RuntimeRegister:
        if not isinstance(
            publication,
            ModemLiveCurrentHealthPublication,
        ):
            raise TypeError(
                "typed_modem_live_current_health_publication_required"
            )
        with self._lock:
            if publication.namespace != self._current_health_namespace:
                raise RuntimeError(
                    "modem_live_current_health_namespace_not_bound"
                )
            return self._write_owned_unlocked(
                REGISTER_HEALTH,
                owner=str(self.owner_map[REGISTER_HEALTH]),
                value=publication.to_register_value(),
                source=publication.owner,
                updated_at=publication.observed_at,
                valid=True,
                stale=False,
                dirty=False,
                trust="live",
                source_kind="live",
                validity="valid",
                transition=False,
            )

    def read_current_health(
        self,
        *,
        accepted_namespace: RuntimeGenerationNamespace,
    ) -> CoreCurrentHealthView:
        """Read current health only when it matches accepted-root truth."""

        if not isinstance(accepted_namespace, RuntimeGenerationNamespace):
            raise TypeError("core_current_health_accepted_namespace_required")
        register = self.read(REGISTER_HEALTH)
        if register.sequence == 0:
            return CoreCurrentHealthView.pending(
                namespace=accepted_namespace,
            )
        if (
            register.owner != self.owner_map[REGISTER_HEALTH]
            or not register.valid
            or register.dirty
            or register.transition
            or not isinstance(register.value, Mapping)
        ):
            raise RuntimeError("core_current_health_register_unavailable")
        view = CoreCurrentHealthView.from_register_value(
            register.value,
            register_sequence=register.sequence,
            register_updated_at=register.updated_at,
            expected_namespace=accepted_namespace,
        )
        if register.stale or register.validity != "valid":
            raise RuntimeError(
                "core_current_health_live_metadata_invalid"
            )
        return view

    def write_runtime_clock(self, **kwargs: Any) -> RuntimeRegister:
        return self._write_owned(
            REGISTER_RUNTIME_CLOCK,
            owner=str(self.owner_map[REGISTER_RUNTIME_CLOCK]),
            **kwargs,
        )

    def write_mutation(self, **kwargs: Any) -> RuntimeRegister:
        return self._write_owned(REGISTER_MUTATION, owner=str(self.owner_map[REGISTER_MUTATION]), **kwargs)

    def write_action(self, **kwargs: Any) -> RuntimeRegister:
        return self._write_owned(REGISTER_ACTION, owner=str(self.owner_map[REGISTER_ACTION]), **kwargs)

    def write_action_auxiliary(self, **kwargs: Any) -> RuntimeRegister:
        return self._write_owned(
            REGISTER_ACTION_AUXILIARY,
            owner=str(self.owner_map[REGISTER_ACTION_AUXILIARY]),
            **kwargs,
        )

    def write_operational_status(self, **kwargs: Any) -> RuntimeRegister:
        return self._write_owned(REGISTER_OPERATIONAL_STATUS, owner=str(self.owner_map[REGISTER_OPERATIONAL_STATUS]), **kwargs)

    def write_downstream_health(self, **kwargs: Any) -> RuntimeRegister:
        return self._write_owned(
            REGISTER_DOWNSTREAM_HEALTH,
            owner=str(self.owner_map[REGISTER_DOWNSTREAM_HEALTH]),
            **kwargs,
        )

    def write_internet_reachability(
        self,
        view: CoreCurrentReachabilityView,
    ) -> RuntimeRegister:
        if not isinstance(view, CoreCurrentReachabilityView):
            raise TypeError("typed_core_reachability_view_required")
        return self._write_owned(
            REGISTER_INTERNET_REACHABILITY,
            owner=str(self.owner_map[REGISTER_INTERNET_REACHABILITY]),
            value=view.as_register_dict(),
            source=view.owner,
            updated_at=view.observed_at,
            valid=True,
            stale=False,
            dirty=False,
            trust="live",
            source_kind="derived",
            validity="valid",
            transition=False,
        )

    def read(self, name: str) -> RuntimeRegister:
        with self._lock:
            return self._read_unlocked(name)

    def _read_unlocked(self, name: str) -> RuntimeRegister:
        normalized = _validate_register_name(name, self.owner_map)
        if normalized in self._registers:
            return _copy_register(self._registers[normalized])
        return RuntimeRegister(
            name=normalized,
            owner=str(self.owner_map.get(normalized, "") or ""),
            valid=False,
            stale=True,
            trust="unknown",
            source_kind="unknown",
            validity="stale",
            transition=False,
        )

    def read_metadata(self, name: str) -> dict[str, Any]:
        """Read register metadata without copying the register value payload."""

        with self._lock:
            return self._read_metadata_unlocked(name)

    def _read_metadata_unlocked(self, name: str) -> dict[str, Any]:

        normalized = _validate_register_name(name, self.owner_map)
        register = self._registers.get(normalized)
        if register is None:
            return {
                "name": normalized,
                "updated_at": 0.0,
                "source": "",
                "valid": False,
                "stale": True,
                "dirty": False,
                "trust": "unknown",
                "source_kind": "unknown",
                "validity": "stale",
                "transition": False,
                "owner": str(self.owner_map.get(normalized, "") or ""),
                "error": "",
                "sequence": 0,
                "signature": "",
            }
        return {
            "name": register.name,
            "updated_at": register.updated_at,
            "source": register.source,
            "valid": register.valid,
            "stale": register.stale,
            "dirty": register.dirty,
            "trust": register.trust,
            "source_kind": register.source_kind,
            "validity": register.validity,
            "transition": register.transition,
            "owner": register.owner,
            "error": register.error,
            "sequence": register.sequence,
            "signature": str(register.signature or ""),
        }

    def write(
        self,
        name: str,
        *,
        owner: str,
        value: Any,
        source: str,
        updated_at: float | None = None,
        valid: bool = True,
        stale: bool = False,
        dirty: bool = False,
        trust: str = "live",
        source_kind: str = "",
        validity: str = "",
        transition: bool | None = None,
        error: str = "",
        force: bool = False,
        diagnostic: bool = False,
    ) -> RuntimeRegister:
        raise RuntimeRegisterOwnershipError(
            "generic runtime register writes are disabled; use the typed owner publisher"
        )

    def _write_owned(self, name: str, *, owner: str, **kwargs: Any) -> RuntimeRegister:
        with self._lock:
            return self._write_owned_unlocked(name, owner=owner, **kwargs)

    def _write_owned_unlocked(
        self,
        name: str,
        *,
        owner: str,
        value: Any,
        source: str,
        updated_at: float | None = None,
        valid: bool = True,
        stale: bool = False,
        dirty: bool = False,
        trust: str = "live",
        source_kind: str = "",
        validity: str = "",
        transition: bool | None = None,
        error: str = "",
        signature: str = "",
        force: bool = False,
    ) -> RuntimeRegister:
        normalized = _validate_register_name(name, self.owner_map)
        owner_text = str(owner or "").strip()
        expected = str(self.owner_map.get(normalized, "") or "")
        if owner_text != expected:
            raise RuntimeRegisterOwnershipError(
                f"register {normalized!r} is owned by {expected!r}, not {owner_text!r}"
            )
        trust_text = _validate_trust(trust)
        previous = self._registers.get(normalized)
        retained_payload_bytes(
            value,
            max_bytes=MAX_RUNTIME_REGISTER_VALUE_BYTES,
            label=f"runtime register {normalized}",
        )
        candidate_value = _normalize_register_value(normalized, value)
        candidate_updated_at = float(time.time() if updated_at is None else updated_at)
        valid_bool = safe_bool(valid, True)
        stale_bool = safe_bool(stale, False)
        dirty_bool = safe_bool(dirty, False)
        axes = _resolve_register_axes(
            trust_text,
            stale=stale_bool,
            dirty=dirty_bool,
            valid=valid_bool,
            source_kind=source_kind,
            validity=validity,
            transition=transition,
        )
        if (
            previous is not None
            and not safe_bool(force, False)
            and previous.value == candidate_value
            and previous.source == str(source or "")
            and previous.valid == valid_bool
            and previous.stale == stale_bool
            and previous.dirty == dirty_bool
            and previous.trust == trust_text
            and previous.source_kind == str(axes["source_kind"])
            and previous.validity == str(axes["validity"])
            and previous.transition == safe_bool(axes["transition"], False)
            and previous.error == str(error or "")
            and previous.signature == str(signature or "")
        ):
            self._last_changed = ()
            return _copy_register(previous)
        self._sequence += 1
        register = RuntimeRegister(
            name=normalized,
            value=candidate_value,
            updated_at=candidate_updated_at,
            source=str(source or ""),
            valid=valid_bool,
            stale=stale_bool,
            dirty=dirty_bool,
            trust=trust_text,
            source_kind=str(axes["source_kind"]),
            validity=str(axes["validity"]),
            transition=safe_bool(axes["transition"], False),
            owner=expected,
            error=str(error or ""),
            sequence=int(self._sequence),
            signature=str(signature or ""),
        )
        self._registers[normalized] = register
        self._last_changed = (normalized,)
        return _copy_register(register)

    def apply(self, write: RuntimeRegisterWrite) -> RuntimeRegister:
        raise RuntimeRegisterOwnershipError(
            "generic runtime register write envelopes are disabled; use the typed owner publisher"
        )

    def apply_batch(self, writes: list[RuntimeRegisterWrite] | tuple[RuntimeRegisterWrite, ...]) -> dict[str, RuntimeRegister]:
        raise RuntimeRegisterOwnershipError(
            "generic runtime register batches are disabled; use the typed owner publisher"
        )

    def _apply_projection_batch_unlocked(
        self,
        writes: list[RuntimeRegisterWrite] | tuple[RuntimeRegisterWrite, ...],
    ) -> RuntimeRegisterPublicationReceipt:
        prepared: list[tuple[RuntimeRegisterWrite, str, str, str, dict[str, Any], Any, float]] = []
        seen_registers: set[str] = set()
        for submitted_write in writes:
            write = submitted_write
            normalized = _validate_register_name(write.name, self.owner_map)
            if normalized in seen_registers:
                raise RuntimeRegisterError(f"duplicate register in projection batch: {normalized!r}")
            seen_registers.add(normalized)
            owner_text = str(write.owner or "").strip()
            expected = str(self.owner_map.get(normalized, "") or "")
            if owner_text != expected:
                raise RuntimeRegisterOwnershipError(
                    f"register {normalized!r} is owned by {expected!r}, not {owner_text!r}"
                )
            candidate_input = write.value
            trust_text = _validate_trust(write.trust)
            valid_bool = safe_bool(write.valid, True)
            stale_bool = safe_bool(write.stale, False)
            dirty_bool = safe_bool(write.dirty, False)
            axes = _resolve_register_axes(
                trust_text,
                stale=stale_bool,
                dirty=dirty_bool,
                valid=valid_bool,
                source_kind=write.source_kind,
                validity=write.validity,
                transition=write.transition,
            )
            retained_payload_bytes(
                candidate_input,
                max_bytes=MAX_RUNTIME_REGISTER_VALUE_BYTES,
                label=f"runtime register {normalized}",
            )
            prepared.append(
                (
                    write,
                    normalized,
                    expected,
                    trust_text,
                    axes,
                    _normalize_register_value(normalized, candidate_input),
                    float(write.updated_at if write.updated_at is not None else time.time()),
                )
            )

        changed: list[tuple[RuntimeRegisterWrite, str, str, str, dict[str, Any], Any, float, bool, bool, bool]] = []
        for write, normalized, expected, trust_text, axes, candidate_value, candidate_updated_at in prepared:
            previous = self._registers.get(normalized)
            valid_bool = safe_bool(write.valid, True)
            stale_bool = safe_bool(write.stale, False)
            dirty_bool = safe_bool(write.dirty, False)
            if (
                previous is not None
                and not safe_bool(write.force, False)
                and previous.value == candidate_value
                and previous.source == str(write.source or "")
                and previous.valid == valid_bool
                and previous.stale == stale_bool
                and previous.dirty == dirty_bool
                and previous.trust == trust_text
                and previous.source_kind == str(axes["source_kind"])
                and previous.validity == str(axes["validity"])
                and previous.transition == safe_bool(axes["transition"], False)
                and previous.error == str(write.error or "")
                and previous.signature == str(write.signature or "")
            ):
                continue
            changed.append(
                (
                    write,
                    normalized,
                    expected,
                    trust_text,
                    axes,
                    candidate_value,
                    candidate_updated_at,
                    valid_bool,
                    stale_bool,
                    dirty_bool,
                )
            )

        if not changed:
            self._last_changed = ()
            return RuntimeRegisterPublicationReceipt(
                sequence=int(self._sequence),
                published_names=tuple(item[1] for item in prepared),
                changed_names=(),
            )

        self._sequence += 1
        sequence = int(self._sequence)
        changed_names: list[str] = []
        for (
            write,
            normalized,
            expected,
            trust_text,
            axes,
            candidate_value,
            candidate_updated_at,
            valid_bool,
            stale_bool,
            dirty_bool,
        ) in changed:
            register = RuntimeRegister(
                name=normalized,
                value=candidate_value,
                updated_at=candidate_updated_at,
                source=str(write.source or ""),
                valid=valid_bool,
                stale=stale_bool,
                dirty=dirty_bool,
                trust=trust_text,
                source_kind=str(axes["source_kind"]),
                validity=str(axes["validity"]),
                transition=safe_bool(axes["transition"], False),
                owner=expected,
                error=str(write.error or ""),
                sequence=sequence,
                signature=str(write.signature or ""),
            )
            self._registers[normalized] = register
            changed_names.append(normalized)
        self._last_changed = tuple(changed_names)
        return RuntimeRegisterPublicationReceipt(
            sequence=sequence,
            published_names=tuple(item[1] for item in prepared),
            changed_names=tuple(changed_names),
        )

    def snapshot(self, names: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
        """Read an explicitly declared register set.

        An unnamed full-bank snapshot is an illegal production boundary: it
        hides caller ownership and can recursively copy the complete runtime
        state. Callers must name the exact registers they consume.
        """

        if names is None:
            raise RuntimeRegisterError("runtime register snapshot requires explicit names")
        with self._lock:
            selected = tuple(names)
            out: dict[str, dict[str, Any]] = {}
            for name in selected:
                normalized = _validate_register_name(name, self.owner_map)
                register = self._registers.get(normalized)
                if register is None:
                    out[normalized] = {
                        **self._read_metadata_unlocked(normalized),
                        "value": None,
                    }
                else:
                    out[normalized] = register.as_dict()
            return out

    def snapshot_generation(
        self,
        names: Iterable[str],
    ) -> tuple[int, dict[str, dict[str, Any]]]:
        """Read one declared set and its exact bank sequence atomically."""

        if names is None:
            raise RuntimeRegisterError(
                "runtime register snapshot requires explicit names"
            )
        with self._lock:
            selected = tuple(names)
            out: dict[str, dict[str, Any]] = {}
            for name in selected:
                normalized = _validate_register_name(name, self.owner_map)
                register = self._registers.get(normalized)
                if register is None:
                    out[normalized] = {
                        **self._read_metadata_unlocked(normalized),
                        "value": None,
                    }
                else:
                    out[normalized] = register.as_dict()
            return int(self._sequence), out

    def publication_receipt(self, published_names: Iterable[str]) -> RuntimeRegisterPublicationReceipt:
        with self._lock:
            return RuntimeRegisterPublicationReceipt(
                sequence=int(self._sequence),
                published_names=tuple(str(name) for name in published_names),
                changed_names=tuple(self._last_changed),
            )

    def delta_snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {name: self._read_unlocked(name).as_dict() for name in self._last_changed}


def require_core_runtime_register_bank(owner: Any) -> RuntimeRegisterBank:
    """Return the sole direct Core register owner or fail loudly."""

    register_bank = getattr(owner, "register_bank", None)
    if not isinstance(register_bank, RuntimeRegisterBank):
        raise RuntimeError("core_runtime_register_bank_unavailable")
    return register_bank


def promote_modem_live_truth_matrix(
    bank: RuntimeRegisterBank,
    matrix: Mapping[str, Any],
    *,
    session_scope_key: str,
) -> RuntimeRegisterPublicationReceipt:
    """Hard-failed precommit Matrix publication path."""

    del bank, matrix, session_scope_key
    raise RuntimeRegisterOwnershipError(
        "precommit_modem_live_matrix_publication_illegal:"
        "publish_one_accepted_generation_batch"
    )


def promote_modem_live_register_lines(
    bank: RuntimeRegisterBank,
    line_snapshot: Mapping[str, Any],
    *,
    session_scope_key: str,
) -> RuntimeRegisterPublicationReceipt:
    """Hard-failed displaced split-register publication path."""

    del bank, line_snapshot, session_scope_key
    raise RuntimeRegisterOwnershipError(
        "split Modem Live register promotion is retired; publish truth matrix"
    )



_ACCEPTED_LATENCY_FIELDS: tuple[str, ...] = (
    "rtt_ms",
    "rtt_sliding_avg",
    "rtt_baseline",
    "rtt_effective_baseline",
    "rtt_quiet_delta",
    "rtt_quiet_held",
    "rtt_delta_block_reason",
    "rtt_delta",
    "rtt_delta_valid",
    "rtt_delta_display_ms",
    "rtt_delta_display_valid",
    "rtt_delta_display_held",
    "rtt_delta_display_live_ts",
    "rtt_dl_jitter_bucket",
    "rtt_dl_jitter_trimmed_ms",
    "rtt_dl_jitter_baseline_ms",
    "rtt_dl_jitter_delta_ms",
    "rtt_dl_jitter_pct",
    "rtt_dl_jitter_valid",
    "rtt_dl_jitter_reason",
    "rtt_active_jitter_mad_ms",
    "rtt_active_jitter_step_p95_ms",
    "rtt_active_jitter_samples",
    "rtt_active_jitter_valid",
    "rtt_probe_ok",
    "rtt_pressure_level",
    "rtt_low_load_latency_valid",
    "rtt_low_load_latency_reason",
    "rtt_low_load_latency_mbps",
)

_ACCEPTED_QUALITY_FIELDS: tuple[str, ...] = (
    "quality_model", "quality_capacity_fraction", "quality_total_bw_mhz",
    "quality_radio_inputs", "quality_valid", "quality_reason",
    "bq_cell",
    "bq_cell_valid",
    "bq_cell_source",
    "cq_cell",
    "cq_cell_source",
    "cq_cell_display",
    "cq_cell_display_valid",
    "cq_cell_display_held",
    "cqb_cell",
    "cqb_cell_valid",
    "cqb_cell_source",
    "cqb_coverage",
    "cq_vector_a_valid",
    "congestion_supported",
    "congestion_support_reason",
    "congestion_pct",
    "congestion_pct_valid",
    "congestion_display_pct",
    "congestion_display_valid",
    "congestion_raw_pct",
    "congestion_internal_bucket",
    "congestion_bucket",
    "congestion_state_avg_pct",
    "congestion_state_bucket",
    "congestion_source",
    "congestion_qam_gap",
    "congestion_slope",
    "congestion_rising",
    "congestion_trend_display",
    "congestion_fast_mode",
    "congestion_hold_mode",
    "congestion_trusted_rtt",
    "congestion_rtt_base_pct",
    "congestion_trusted_rsrq",
    "congestion_trusted_active_jitter",
    "congestion_active_jitter_pct",
    "congestion_rsrq_available",
    "congestion_rsrq_contributes",
    "congestion_rsrq_reason",
    "congestion_rsrq_pct",
    "congestion_rsrq_gap_db",
    "congestion_rsrq_gap_threshold_db",
    "congestion_flags_valid",
    "congestion_bad_now",
    "congestion_pressure_high",
    "congestion_pressure_moderate",
    "congestion_kind_hint",
    "rsrq_baseline",
    "rsrq_baseline_valid",
    "rsrq_gap",
    "rsrq_gap_valid",
    "rsrq_delta_display_db",
    "rsrq_delta_display_valid",
    "rsrq_delta_display_held",
    "rsrq_delta_display_live_ts",
    "rsrq_baseline_display_db",
    "rsrq_baseline_display_valid",
    "rsrq_baseline_display_held",
    "rsrq_baseline_display_live_ts",
    "mcs_collapse",
    "ri_stability_pct",
    "ri_stability_valid",
    "ri_activity_per_hour",
    "nr_recovering",
    "nr_just_dropped",
    "nr_secondary_active",
    "nr_transition_state",
    "nr_transition_dirty",
    "nr_identity_completeness",
    "nsa_snapshot",
    "nr_capacity_evidence",
)

_ACCEPTED_CA_FIELDS: tuple[str, ...] = (
    "carrier_count",
    "carrier_details",
    "ca_raw_state",
    "cart",
    "cast",
    "ca_stat_scope_key",
    "cart_ld",
    "cart_ld_valid",
    "cart_ld_display",
    "cart_ld_display_valid",
    "cast_ld",
    "cast_ld_valid",
    "cart_activity_per_hour",
    "cart_ld_activity",
    "ri_stability_pct",
    "ri_activity_per_hour",
    "ri_stability_valid",
    "ca_stat_pct",
    "ca_stat_ld_pct",
    "ca_stat_ld_valid",
)

_SCHEDULER_RAW_FIELDS: tuple[str, ...] = (
    "radio_connected",
    "ri",
    "ri_valid",
    "ri_source",
    "mcs_dl",
    "mcs_dl0",
    "mcs_dl1",
    "mcs_ul",
    "cqi",
    "cqi0",
    "cqi1",
    "qam_dl0",
    "qam_dl1",
    "qam_ul",
    "scheduler_zero_authoritative",
    "scheduler_stream1_available",
    "scheduler_source",
    "cqi_source",
)


def _accepted_payload_value(payload: Any, name: str) -> tuple[Any, bool]:
    if isinstance(payload, Mapping):
        return payload.get(name), name in payload
    if hasattr(payload, name):
        return getattr(payload, name), True
    return None, False


def _accepted_payload_projection(payload: Any, fields: Iterable[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in fields:
        value, present = _accepted_payload_value(payload, name)
        if present:
            out[str(name)] = value
    return out


def _normalize_ca_component_bands(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    bands: set[int] = set()
    for raw in value:
        band = safe_int(raw, 0)
        if band > 0 and band != 999:
            bands.add(band)
    return tuple(sorted(bands))


def _ca_composition_fact(
    *,
    ca_active: bool,
    component_bands: tuple[int, ...],
    declared_count: int,
) -> CoreCurrentField:
    if not ca_active:
        return CoreCurrentField(
            (),
            CoreCurrentFieldState.NOT_APPLICABLE,
            "ca_inactive_single_carrier",
            "not_applicable",
        )
    if not component_bands:
        return CoreCurrentField(
            (),
            CoreCurrentFieldState.PENDING,
            "ca_active_components_unavailable",
            "unproven",
        )
    if declared_count > 0 and declared_count != len(component_bands):
        return CoreCurrentField(
            (),
            CoreCurrentFieldState.INVALID,
            "ca_component_count_mismatch",
            "unproven",
        )
    return CoreCurrentField(
        component_bands,
        CoreCurrentFieldState.AVAILABLE,
        "ca_components_proven",
        "observed",
    )


def _accepted_signal_register_writes(
    bank: RuntimeRegisterBank,
    accepted: AcceptedModemGeneration,
    *,
    signal_tuning: Mapping[str, Any] | None = None,
    commit_id: str,
) -> tuple[RuntimeRegisterWrite, ...]:
    """Prepare the private derived lanes for one accepted commit."""

    if not isinstance(accepted, AcceptedModemGeneration):
        raise TypeError("accepted generation contract required")
    identity = accepted.delivery_identity
    if (
        type(commit_id) is not str
        or not commit_id
        or commit_id != commit_id.strip()
    ):
        raise RuntimeRegisterError("accepted generation commit id required")
    commit = commit_id
    matrix_reader = accepted.raw.truth_matrix_reader()
    if matrix_reader.identity != accepted.raw.matrix_identity:
        raise RuntimeRegisterError(
            "accepted signal generation does not match its private Matrix identity"
        )
    radio = {
        **matrix_reader.line_values("radio"),
        **matrix_reader.line_values("rf"),
    }

    computed = accepted.computed_copy()
    latency_value = _accepted_payload_projection(computed, _ACCEPTED_LATENCY_FIELDS)
    quality_value = _accepted_payload_projection(computed, _ACCEPTED_QUALITY_FIELDS)
    ca_value = _accepted_payload_projection(computed, _ACCEPTED_CA_FIELDS)
    if not latency_value or not quality_value or not ca_value:
        raise RuntimeRegisterError("accepted signal generation is missing a derived register lane")

    rtt_observation = accepted.matrix_rtt.observation
    latency_value["rtt_observation"] = rtt_observation.as_dict()
    latency_value["rtt_raw_ms"] = rtt_observation.value_ms
    latency_value["rtt_raw_observed_at"] = rtt_observation.observed_at
    latency_value["rtt_raw_freshness"] = rtt_observation.freshness
    latency_value["rtt_raw_probe_status"] = rtt_observation.probe_status
    latency_value["rtt_raw_last_probe_at"] = rtt_observation.last_probe_at
    latency_value["rtt_raw_failure_reason"] = rtt_observation.failure_reason
    latency_value["rtt_raw_available"] = rtt_observation.value_ms is not None
    latency_value["rtt_raw_sample_sequence"] = (
        max(1, int(round(rtt_observation.observed_at * 1000.0)))
        if rtt_observation.value_ms is not None
        else 0
    )
    rtt_current = bool(
        rtt_observation.freshness == "fresh"
        and rtt_observation.probe_status == "ok"
    )
    latency_value["rtt_probe_ok"] = rtt_current
    if not rtt_current:
        latency_value["rtt_pressure_level"] = (
            "probe_failed"
            if rtt_observation.probe_status == "failed"
            else (
                "stale"
                if rtt_observation.freshness == "stale"
                else "probe_missing"
            )
        )

    rtt_delta_display_valid = safe_bool(
        latency_value.get("rtt_delta_display_valid"),
        False,
    )
    rtt_delta_block_reason = str(
        latency_value.get("rtt_delta_block_reason") or ""
    ).strip().lower()
    if rtt_delta_display_valid:
        latency_value["rtt_delta_display_state"] = (
            CoreCurrentFieldState.AVAILABLE.value
        )
        latency_value["rtt_delta_display_reason"] = ""
    elif rtt_delta_block_reason in {"traffic_busy", "ul_contamination"}:
        latency_value["rtt_delta_display_state"] = (
            CoreCurrentFieldState.NOT_APPLICABLE.value
        )
        latency_value["rtt_delta_display_reason"] = rtt_delta_block_reason
    else:
        latency_value["rtt_delta_display_state"] = (
            CoreCurrentFieldState.PENDING.value
        )
        latency_value["rtt_delta_display_reason"] = (
            rtt_delta_block_reason or "rtt_delta_warming"
        )

    quality_value["cq_cell_valid"] = safe_bool(
        quality_value.get("cq_vector_a_valid"), False
    )

    scheduler_raw = dict(radio)
    scheduler = build_scheduler_display_surface(
        build_scheduler_display_contract(
            scheduler_raw,
            unsupported_fields=scheduler_raw.get(
                "scheduler_unsupported_fields"
            ),
            field_trust=scheduler_raw.get("scheduler_field_trust"),
        )
    )
    quality_value.update(
        {
            "scheduler_zero_authoritative": scheduler["scheduler_zero_authoritative"],
            "scheduler_stream1_available": scheduler["scheduler_stream1_available"],
            "ri": scheduler["scheduler_ri_display"],
            "ri_valid": scheduler["scheduler_ri_valid"],
            "ri_source": scheduler["scheduler_ri_source"],
            "cqi0": scheduler["scheduler_cqi0_display"],
            "cqi1": scheduler["scheduler_cqi1_display"],
            "cqi": scheduler["scheduler_cqi0_display"],
            "mcs_dl0": scheduler["scheduler_mcs_dl0_display"],
            "mcs_dl1": scheduler["scheduler_mcs_dl1_display"],
            "mcs_ul": scheduler["scheduler_mcs_ul_display"],
            "me0": scheduler["scheduler_me0_display"],
            "me1": scheduler["scheduler_me1_display"],
            "me_ul": scheduler["scheduler_me_ul_display"],
            "qam_dl0": scheduler["scheduler_qam0_display"],
            "qam_dl1": scheduler["scheduler_qam1_display"],
            "qam_ul": scheduler["scheduler_qam_ul_display"],
        }
    )

    tuning = dict(signal_tuning or {}) if isinstance(signal_tuning, Mapping) else {}
    jitter_formula = _mapping(tuning.get("jitter_formula"))
    congestion_weights = _mapping(tuning.get("congestion_weights"))
    tuning_status = _mapping(tuning.get("signal_tuning_status"))
    if jitter_formula:
        latency_value["jitter_formula"] = jitter_formula
    if congestion_weights:
        quality_value["congestion_weights"] = congestion_weights
    if tuning_status:
        latency_value["signal_tuning_status"] = tuning_status
        quality_value["signal_tuning_status"] = tuning_status

    for name in (
        "ca_signature",
        "ca_band_signature",
        "ca_state_text",
        "ca_source",
        "ca_component_band_set",
        "ca_component_band_signature",
        "ca_component_count",
        "ca_composition_signature",
    ):
        if name in radio:
            ca_value[name] = radio[name]
    ca_value["ca_active"] = bool(safe_int(ca_value.get("cart"), 0))
    ca_value["ca_realtime_state"] = safe_int(ca_value.get("cart"), 0)
    ca_value["ca_stat_pct"] = safe_float(ca_value.get("cast"), 0.0)
    ca_value["ca_realtime_ld_state"] = safe_int(ca_value.get("cart_ld"), 0)
    ca_value["ca_realtime_ld_valid"] = safe_bool(ca_value.get("cart_ld_valid"), False)
    ca_value["ca_active_pct"] = safe_float(ca_value.get("cast"), 0.0)
    ca_value["ca_active_session_pct"] = safe_float(ca_value.get("cast"), 0.0)
    ca_value["ca_change_per_hour"] = safe_float(ca_value.get("cart_activity_per_hour"), 0.0)
    ca_value["ca_ld_change_per_hour"] = safe_float(ca_value.get("cart_ld_activity"), 0.0)
    cart_ld_present = "cart_ld" in ca_value
    cart_ld_valid = safe_bool(ca_value.get("cart_ld_valid"), False)
    cart_ld_fact = CoreCurrentField(
        safe_int(ca_value.get("cart_ld"), 0) if cart_ld_present else None,
        (
            CoreCurrentFieldState.AVAILABLE
            if cart_ld_present and cart_ld_valid
            else CoreCurrentFieldState.NOT_APPLICABLE
            if cart_ld_present
            else CoreCurrentFieldState.MISSING
        ),
        (
            "cart_ld_state_observed"
            if cart_ld_present and cart_ld_valid
            else "cart_ld_not_load_qualified"
            if cart_ld_present
            else "cart_ld_state_missing"
        ),
        "observed"
        if cart_ld_present and cart_ld_valid
        else "not_applicable"
        if cart_ld_present
        else "unproven",
    )
    ca_value["cart_ld"] = cart_ld_fact.value
    ca_value["cart_ld_state"] = cart_ld_fact.state.value
    ca_value["cart_ld_reason"] = cart_ld_fact.reason
    ca_value["cart_ld_capability"] = cart_ld_fact.capability

    component_bands = _normalize_ca_component_bands(
        ca_value.get("ca_component_band_set")
    )
    composition = _ca_composition_fact(
        ca_active=bool(ca_value["ca_active"]),
        component_bands=component_bands,
        declared_count=max(0, safe_int(ca_value.get("ca_component_count"), 0)),
    )
    ca_value["ca_component_band_set"] = list(composition.value or ())
    ca_value["ca_component_count"] = len(ca_value["ca_component_band_set"])
    ca_value["ca_component_band_signature"] = "+".join(
        f"B{band}" for band in ca_value["ca_component_band_set"]
    )
    ca_value["ca_composition_state"] = composition.state.value
    ca_value["ca_composition_reason"] = composition.reason
    ca_value["ca_composition_capability"] = composition.capability
    ca_value["ca_state_text"] = "ACTIVE" if ca_value["ca_active"] else "INACTIVE"

    source = (
        "signal_processing_matrix:"
        f"{identity.session_scope_key}:{identity.source_epoch}:{identity.source_sequence}"
    )
    common = {
        "sample_sequence": identity.source_sequence,
        "sample_ts": float(accepted.raw.sampled_at),
        "accepted_at": float(accepted.accepted_at),
        "source_epoch": identity.source_epoch,
        "session_scope_key": identity.session_scope_key,
        "lifecycle_generation": identity.lifecycle_generation,
        "matrix_revision": identity.matrix_revision,
    }
    latency_value.update(common)
    quality_value.update(common)
    ca_value.update(common)
    owner_map = bank.owner_map
    writes = tuple(
        RuntimeRegisterWrite(
            name,
            str(owner_map[name]),
            value,
            source,
            float(accepted.accepted_at),
            valid=True,
            stale=False,
            dirty=False,
            trust="derived",
            source_kind="derived",
            validity="valid",
            transition=False,
            signature=commit,
            force=True,
        )
        for name, value in (
            (REGISTER_LATENCY, latency_value),
            (REGISTER_QUALITY, quality_value),
            (REGISTER_CA, ca_value),
        )
    )
    return writes


def prepare_signal_processing_registers(
    bank: RuntimeRegisterBank,
    accepted: AcceptedModemGeneration,
    *,
    commit_id: str,
    signal_tuning: Mapping[str, Any] | None = None,
) -> SignalProcessingRegisterTransaction:
    """Prepare the three SPM output lanes without republishing raw truth."""

    if not isinstance(accepted, AcceptedModemGeneration):
        raise TypeError("accepted generation contract required")
    if (
        type(commit_id) is not str
        or not commit_id
        or commit_id != commit_id.strip()
    ):
        raise RuntimeRegisterError("accepted generation commit id required")
    commit = commit_id
    matrix_reader = accepted.raw.truth_matrix_reader()
    identity = accepted.delivery_identity
    if matrix_reader.identity != accepted.raw.matrix_identity:
        raise RuntimeRegisterError(
            "accepted generation Matrix identity mismatch"
        )
    derived_writes = _accepted_signal_register_writes(
        bank,
        accepted,
        signal_tuning=signal_tuning,
        commit_id=commit,
    )
    return SignalProcessingRegisterTransaction(
        commit_id=commit,
        matrix_identity=accepted.raw.matrix_identity,
        writes=tuple(
            AcceptedGenerationRegisterWrite.from_runtime_write(write)
            for write in derived_writes
        ),
    )


def publish_signal_processing_registers(
    bank: RuntimeRegisterBank,
    transaction: SignalProcessingRegisterTransaction,
) -> RuntimeRegisterPublicationReceipt:
    """Publish SPM outputs only after their exact source Matrix is public."""

    if not isinstance(transaction, SignalProcessingRegisterTransaction):
        raise TypeError("signal processing register transaction required")
    identity_sequence = transaction.matrix_identity.matrix_sequence
    with bank._lock:
        matrix_register = bank._registers.get(REGISTER_MODEM_LIVE_MATRIX)
        if matrix_register is None or not isinstance(matrix_register.value, Mapping):
            raise RuntimeRegisterError("signal processing source Matrix is unavailable")
        matrix_identity = MatrixGenerationIdentity.from_truth_matrix(
            matrix_register.value
        )
        if matrix_identity != transaction.matrix_identity:
            if (
                matrix_identity.namespace == transaction.matrix_identity.namespace
                and identity_sequence < matrix_identity.matrix_sequence
            ):
                raise AcceptedSignalGenerationSuperseded(
                    "signal processing generation is older than the public Matrix"
                )
            raise RuntimeRegisterError("signal processing source Matrix identity mismatch")
        current_outputs = tuple(
            bank._registers.get(name)
            for name in SIGNAL_PROCESSING_REGISTER_NAMES
        )
        if all(
            current is not None
            and current.signature == transaction.commit_id
            and isinstance(current.value, Mapping)
            and current.value.get("sample_sequence") == identity_sequence
            and current.value.get("source_epoch")
            == transaction.matrix_identity.source_epoch
            for current in current_outputs
        ):
            return RuntimeRegisterPublicationReceipt(
                sequence=bank._sequence,
                published_names=(),
                changed_names=(),
            )
        return bank._apply_projection_batch_unlocked(
            tuple(write.thaw() for write in transaction.writes)
        )


def publish_modem_live_matrix_generation(
    bank: RuntimeRegisterBank,
    raw: RawModemGeneration,
    *,
    commit_id: str,
) -> RuntimeRegisterPublicationReceipt:
    """Publish one accepted raw Matrix without waiting for SPM."""

    if not isinstance(raw, RawModemGeneration):
        raise TypeError("raw Matrix generation required")
    if type(commit_id) is not str or not commit_id or commit_id != commit_id.strip():
        raise RuntimeRegisterError("accepted Matrix commit id required")
    matrix_reader = raw.truth_matrix_reader()
    identity = raw.matrix_identity
    if matrix_reader.identity != identity:
        raise RuntimeRegisterError("accepted Matrix identity mismatch")
    write = RuntimeRegisterWrite(
        REGISTER_MODEM_LIVE_MATRIX,
        str(bank.owner_map[REGISTER_MODEM_LIVE_MATRIX]),
        matrix_reader,
        (
            "modem_live.matrix:"
            f"{raw.session_scope_key}:{raw.source_epoch}:"
            f"{raw.source_sequence}"
        ),
        float(raw.sampled_at),
        valid=True,
        stale=False,
        dirty=False,
        trust="live",
        source_kind="live",
        validity="valid",
        transition=False,
        signature=commit_id,
        force=True,
    )
    with bank._lock:
        previous = bank._registers.get(REGISTER_MODEM_LIVE_MATRIX)
        if previous is not None and isinstance(previous.value, Mapping):
            previous_identity = MatrixGenerationIdentity.from_truth_matrix(previous.value)
            if previous_identity.namespace == identity.namespace:
                if previous_identity.matrix_sequence > raw.source_sequence:
                    raise AcceptedSignalGenerationSuperseded(
                        "accepted Matrix generation is older than public truth"
                    )
                if previous_identity == identity:
                    if previous.signature != commit_id:
                        raise RuntimeRegisterError("accepted Matrix identity changed")
                    return RuntimeRegisterPublicationReceipt(
                        sequence=bank._sequence,
                        published_names=(),
                        changed_names=(),
                    )
        if matrix_reader.cell("identity", "decode_catalog_snapshot").available:
            bank.install_identity_decode_replica(matrix_reader.line_values("identity"))
        return bank._apply_projection_batch_unlocked((write,))


def prepare_accepted_generation_registers(*args: Any, **kwargs: Any) -> None:
    """Hard-fail the deleted raw-plus-derived publication path."""

    del args, kwargs
    raise RuntimeRegisterOwnershipError(
        "combined_accepted_generation_register_publication_deleted:"
        "publish_modem_live_matrix_then_signal_processing"
    )


def publish_accepted_generation_registers(*args: Any, **kwargs: Any) -> None:
    """Hard-fail the deleted raw-plus-derived publication path."""

    del args, kwargs
    raise RuntimeRegisterOwnershipError(
        "combined_accepted_generation_register_publication_deleted:"
        "publish_modem_live_matrix_then_signal_processing"
    )


def promote_accepted_signal_registers(
    bank: RuntimeRegisterBank,
    accepted: AcceptedModemGeneration,
    *,
    signal_tuning: Mapping[str, Any] | None = None,
) -> RuntimeRegisterPublicationReceipt:
    """Hard-failed split derived-register publication path."""

    del bank, accepted, signal_tuning
    raise RuntimeRegisterOwnershipError(
        "split_accepted_signal_publication_illegal:"
        "publish_one_accepted_generation_batch"
    )


def build_runtime_register_owner_map() -> dict[str, str]:
    return dict(RUNTIME_REGISTER_OWNER_MAP)


def extract_radio_identity_value(status: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = status if isinstance(status, Mapping) else {}
    out: dict[str, Any] = {}
    for key in RADIO_IDENTITY_KEYS:
        value = payload.get(key)
        if key == "earfcn_dl" and value in (None, ""):
            value = payload.get("earfcn")
        if key in RADIO_IDENTITY_INT_KEYS:
            coerced = _positive_int_or_zero(value)
            if coerced > 0:
                out[key] = coerced
            continue
        text = str(value or "").strip()
        if text:
            out[key] = text
    return out


def _radio_identity_has_required_anchor(identity: Mapping[str, Any] | None) -> bool:
    payload = identity if isinstance(identity, Mapping) else {}
    return _positive_int_or_zero(payload.get(RADIO_IDENTITY_REQUIRED_ANCHOR)) > 0


def _radio_identity_register_display_value(value: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = value if isinstance(value, Mapping) else {}
    if not _radio_identity_has_required_anchor(payload):
        return {}
    return {
        key: payload.get(key, 0 if key in RADIO_IDENTITY_INT_KEYS else "")
        for key in RADIO_IDENTITY_KEYS
    }


def apply_radio_identity_register_to_status(
    status: Mapping[str, Any] | None,
    register: RuntimeRegister,
) -> dict[str, Any]:
    payload = dict(status or {})
    value = register.value if isinstance(register.value, Mapping) else {}
    register_valid = safe_bool(register.valid, False)
    register_stale = safe_bool(register.stale, False)
    register_dirty = safe_bool(register.dirty, False)
    source_kind = str(register.source_kind or "").strip().lower()
    validity = str(register.validity or "").strip().lower()
    transition = safe_bool(register.transition, False)
    usable = (
        register_valid
        and not register_stale
        and not register_dirty
        and not transition
        and validity == "valid"
        and source_kind in RADIO_IDENTITY_SOURCE_KIND_TRUSTED
    )
    payload["radio_identity_register"] = {
        "owner": register.owner,
        "valid": register_valid,
        "stale": register_stale,
        "dirty": register_dirty,
        "trust": register.trust,
        "source_kind": source_kind,
        "validity": validity,
        "transition": transition,
        "sequence": int(register.sequence),
        "source": register.source,
        "error": register.error,
    }
    if usable:
        for key in RADIO_IDENTITY_KEYS:
            if key in value:
                payload[key] = value[key]
            else:
                payload[key] = 0 if key in RADIO_IDENTITY_INT_KEYS else ""
        return payload

    display_value = _radio_identity_register_display_value(value)
    if display_value:
        payload["radio_identity_display"] = dict(display_value)
        payload["radio_identity_display_stale"] = True
        payload["radio_identity_display_source"] = register.source or "runtime_registers.radio"

    for key in RADIO_IDENTITY_KEYS:
        payload[key] = 0 if key in RADIO_IDENTITY_INT_KEYS else ""
    payload["radio_identity_unavailable"] = True
    return payload


def _runtime_register_public_value_present(value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    if isinstance(value, str) and value.strip().lower() in {
        "",
        "-",
        "--",
        "---",
        "n/a",
        "na",
        "none",
        "null",
        "unknown",
        "miss",
    }:
        return False
    return True


def _runtime_register_public_trusted(register: Mapping[str, Any]) -> bool:
    return (
        safe_bool(register.get("valid"), False)
        and not safe_bool(register.get("stale"), False)
        and not safe_bool(register.get("dirty"), False)
        and str(register.get("validity") or "valid").strip().lower() != "stale"
    )


def runtime_register_public_field_projection(
    registers: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return top-level public fields owned by trusted runtime registers.

    This is the single register-to-public-surface contract used by current-state
    export and operator surfaces. Stale or dirty registers do not publish fields.
    """

    register_map = registers if isinstance(registers, Mapping) else {}
    fields: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for register_name, projection in RUNTIME_REGISTER_PUBLIC_FIELD_PROJECTION.items():
        register = register_map.get(register_name)
        if not isinstance(register, Mapping) or not _runtime_register_public_trusted(register):
            continue
        value = register.get("value")
        if not isinstance(value, Mapping):
            continue
        for target, source_keys in projection.items():
            for source_key in source_keys:
                candidate = value.get(source_key)
                if not _runtime_register_public_value_present(candidate):
                    continue
                fields[target] = _freeze_register_value(candidate)
                sources[target] = f"runtime_registers.{register_name}"
                break
    return fields, sources


def _radio_identity_register_unavailable(base: Mapping[str, Any]) -> bool:
    if safe_bool(base.get("radio_identity_unavailable"), False):
        return True
    register = _mapping(base.get("radio_identity_register"))
    if not register:
        return False
    trust = str(register.get("trust") or "").strip().lower()
    source_kind = str(register.get("source_kind") or "").strip().lower()
    validity = str(register.get("validity") or "").strip().lower()
    transition_present = "transition" in register
    transition = safe_bool(register.get("transition"), False)
    if source_kind or validity or transition_present:
        return (
            not safe_bool(register.get("valid"), False)
            or safe_bool(register.get("stale"), False)
            or safe_bool(register.get("dirty"), False)
            or transition
            or validity != "valid"
            or source_kind not in RADIO_IDENTITY_SOURCE_KIND_TRUSTED
        )
    return (
        not safe_bool(register.get("valid"), False)
        or safe_bool(register.get("stale"), False)
        or safe_bool(register.get("dirty"), False)
        or trust not in RADIO_IDENTITY_TRUSTED
    )


def _radio_identity_stale_for_register_projection(base: Mapping[str, Any]) -> bool:
    """Return True only when the current canonical sample cannot prove identity.

    A stale ``radio_identity_register`` marker is an input-contract warning, not
    a global GUI truth owner. If the current canonical payload carries a serving
    anchor, final register projection must publish that fresh sample instead of
    poisoning radio/traffic/latency/quality/CA as stale.
    """

    if not _radio_identity_register_unavailable(base):
        return False
    return not _radio_identity_has_required_anchor(extract_radio_identity_value(base))


def runtime_sample_is_stale(
    payload: Mapping[str, Any],
    base: Mapping[str, Any],
    modem_health: Mapping[str, Any],
    *,
    now_ts: float,
) -> bool:
    """Return True when a held modem sample has crossed the live stale budget."""

    sample_fresh_present = "sample_fresh" in payload or "sample_fresh" in base
    sample_fresh = safe_bool(payload.get("sample_fresh", base.get("sample_fresh", True)), True)
    if sample_fresh:
        return False

    age_s = _first_float(payload.get("sample_age_s"), base.get("sample_age_s"))
    if age_s is None:
        sample_ts = _first_float(payload.get("sample_ts"), base.get("sample_ts"))
        if sample_ts is not None and sample_ts > 0.0:
            age_s = max(0.0, now_ts - sample_ts)
    if age_s is None and sample_fresh_present:
        return False

    stale_after_s = runtime_sample_stale_after_s(payload, base, modem_health)
    return bool(age_s is not None and age_s > stale_after_s)


def runtime_sample_stale_after_s(
    payload: Mapping[str, Any],
    base: Mapping[str, Any],
    modem_health: Mapping[str, Any],
) -> float:
    for source in (payload, base, modem_health):
        live_register = _mapping(source.get("live_register"))
        value = _first_float(live_register.get("stale_after_s"))
        if value is not None and value > 0.0:
            return value
        value = _first_float(source.get("live_register_stale_after_s"))
        if value is not None and value > 0.0:
            return value
    fast_lane = _mapping(payload.get("modem_live_fast_lane"))
    register_read = _mapping(fast_lane.get("register_read"))
    value = _first_float(register_read.get("stale_after_s"))
    if value is not None and value > 0.0:
        return value
    return 6.0


def _stale_runtime_sample(
    payload: Mapping[str, Any],
    base: Mapping[str, Any],
    modem_health: Mapping[str, Any],
    *,
    now_ts: float,
) -> bool:
    return runtime_sample_is_stale(payload, base, modem_health, now_ts=now_ts)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _normalize_register_value(name: str, value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _freeze_register_value(value)
    if name == REGISTER_MODEM_LIVE_MATRIX:
        if isinstance(value, ValidatedTruthMatrixReader):
            return _freeze_register_value(value.to_mapping())
        validate_truth_matrix(value)
        return _freeze_register_value(value)
    if name in {
        REGISTER_RADIO,
        REGISTER_MODEM_SLOW,
        REGISTER_MODEM_TIMING,
        REGISTER_TRAFFIC,
        REGISTER_IDENTITY,
        REGISTER_CAPABILITY,
    }:
        raise RuntimeRegisterOwnershipError(
            f"split Modem Live register is retired: {name}"
        )
    if name == REGISTER_LATENCY:
        if "rtt_dl_jitter_display_valid" in value:
            raise RuntimeRegisterError(
                "latency_secondary_jitter_validity_rejected"
            )
        _require_live_boolean_field(
            name,
            value,
            "rtt_probe_ok",
        )
        return _freeze_register_value(value)
    if name == REGISTER_QUALITY:
        _require_live_boolean_field(
            name,
            value,
            "congestion_trusted_rtt",
        )
        return _freeze_register_value(value)
    if name == REGISTER_CA:
        if "cart_ld_pct" in value:
            raise RuntimeRegisterError("ca_retired_cart_ld_pct_rejected")
        return _freeze_register_value(value)
    if name == REGISTER_MUTATION:
        return RadioMutationWindow.from_mapping(value).as_dict()
    if name == REGISTER_OPERATIONAL_STATUS:
        return normalize_operational_service_status(value)
    if name == REGISTER_INTERNET_REACHABILITY:
        return validate_current_reachability_register(value)
    return _freeze_register_value(value)


def _require_live_boolean_field(
    register_name: str,
    value: Mapping[str, Any],
    field_name: str,
) -> None:
    """Reject incomplete accepted live generations at their owner boundary."""

    is_live_generation = bool(
        safe_int(value.get("sample_sequence"), 0) > 0
        or str(value.get("source_epoch") or "").strip()
        or str(value.get("session_scope_key") or "").strip()
    )
    if not is_live_generation:
        return
    if field_name not in value:
        raise RuntimeRegisterError(
            f"{register_name}_{field_name}_required"
        )
    if type(value.get(field_name)) is not bool:
        raise RuntimeRegisterError(
            f"{register_name}_{field_name}_bool_required"
        )


def _freeze_register_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _freeze_register_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_freeze_register_value(item) for item in value]
    if isinstance(value, list):
        return [_freeze_register_value(item) for item in value]
    if isinstance(value, set):
        return [_freeze_register_value(item) for item in sorted(value, key=str)]
    return value


def _copy_register(register: RuntimeRegister) -> RuntimeRegister:
    return RuntimeRegister(
        name=register.name,
        value=_freeze_register_value(register.value),
        updated_at=register.updated_at,
        source=register.source,
        valid=register.valid,
        stale=register.stale,
        dirty=register.dirty,
        trust=register.trust,
        source_kind=register.source_kind,
        validity=register.validity,
        transition=register.transition,
        owner=register.owner,
        error=register.error,
        sequence=register.sequence,
        signature=str(register.signature or ""),
    )


def _compact_value(source: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: source[key] for key in keys if key in source}


def _first_int(*values: Any) -> int | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return int(value)
        except Exception:
            continue
    return None


def _first_float(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def _trust_from_state(*, has_value: bool, stale: bool, dirty: bool) -> str:
    if dirty:
        return "transition"
    if stale:
        return "stale"
    if has_value:
        return "live"
    return "unknown"


def _positive_number(value: Any) -> bool:
    try:
        return float(value) > 0
    except Exception:
        return False


def _positive_int_or_zero(value: Any) -> int:
    try:
        out = int(float(value or 0))
        return out if out > 0 else 0
    except Exception:
        return 0


def _register_projection_write(
    name: str,
    *,
    value: Any,
    source: str,
    updated_at: float,
    valid: bool,
    stale: bool,
    dirty: bool,
    trust: str,
    source_kind: str = "",
    validity: str = "",
    transition: bool | None = None,
) -> RuntimeRegisterWrite:
    normalized = _validate_register_name(name, RUNTIME_REGISTER_OWNER_MAP)
    return RuntimeRegisterWrite(
        name,
        owner=RUNTIME_REGISTER_OWNER_MAP[normalized],
        value=value,
        source=source,
        updated_at=updated_at,
        valid=valid,
        stale=stale,
        dirty=dirty,
        trust=trust,
        source_kind=source_kind,
        validity=validity,
        transition=transition,
    )


def _validate_register_name(name: str, owner_map: Mapping[str, str]) -> str:
    normalized = str(name or "").strip().lower()
    if normalized not in owner_map:
        raise RuntimeRegisterError(f"unknown runtime register: {name!r}")
    return normalized


def _validate_trust(trust: str) -> str:
    trust_text = str(trust or "").strip().lower()
    if trust_text not in REGISTER_TRUST_LEVELS:
        raise RuntimeRegisterError(f"unknown runtime register trust level: {trust!r}")
    return trust_text


def _validate_source_kind(source_kind: str) -> str:
    source_text = str(source_kind or "").strip().lower()
    if source_text not in REGISTER_SOURCE_KINDS:
        raise RuntimeRegisterError(f"unknown runtime register source kind: {source_kind!r}")
    return source_text


def _validate_validity(validity: str) -> str:
    validity_text = str(validity or "").strip().lower()
    if validity_text not in REGISTER_VALIDITIES:
        raise RuntimeRegisterError(f"unknown runtime register validity: {validity!r}")
    return validity_text


def _resolve_register_axes(
    trust: str,
    *,
    stale: bool,
    dirty: bool,
    valid: bool,
    source_kind: str = "",
    validity: str = "",
    transition: bool | None = None,
) -> dict[str, Any]:
    axes = _split_register_trust(trust, stale=stale, dirty=dirty, valid=valid)
    if str(source_kind or "").strip():
        axes["source_kind"] = _validate_source_kind(source_kind)
    if str(validity or "").strip():
        axes["validity"] = _validate_validity(validity)
    if transition is not None:
        axes["transition"] = safe_bool(transition, False)
    return axes


def _split_register_trust(trust: str, *, stale: bool, dirty: bool, valid: bool) -> dict[str, Any]:
    trust_text = str(trust or "unknown").strip().lower()
    if trust_text == "unsupported":
        source_kind = "unsupported"
    elif trust_text == "cached":
        source_kind = "cached"
    elif trust_text == "live":
        source_kind = "live"
    elif trust_text in {"derived", "transition", "dirty", "stale"}:
        source_kind = "derived"
    else:
        source_kind = "unknown"

    if safe_bool(stale, False) or trust_text == "stale":
        validity = "stale"
    elif safe_bool(dirty, False) or trust_text in {"dirty", "transition"}:
        validity = "dirty"
    elif safe_bool(valid, False):
        validity = "valid"
    else:
        validity = "unknown"

    return {
        "source_kind": source_kind,
        "validity": validity,
        "transition": trust_text == "transition",
    }
