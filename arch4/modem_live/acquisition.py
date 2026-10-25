"""Main Modem Live acquisition implementation.

This module reads the modem, sends raw data to the translator, publishes the
first live Matrix generation, and provides detached snapshots to consumers.
"""

from __future__ import annotations

from contextlib import nullcontext
from arch4.modem_live.startup_trace import StartupModemTrace

from arch4.modem.utils import RttProbeResult
from arch4.modem.network_inspection import NetworkSettingsReadIncomplete
from arch4.modem_live.service_confirmation import (
    CONFIRMATION_MIN_INTERVAL_S, ServiceConfirmationReceipt, confirm_external_response,
)
from arch4.modem_live.profile_catalog import modem_connection_config_fields

import copy
from dataclasses import dataclass, replace
from contextlib import contextmanager, nullcontext
import logging
import math
import socket
import threading
import time
from types import SimpleNamespace
import urllib.request
import uuid
import weakref
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from arch4.action_admission import action_expects_modem_interruption, runtime_action_unsupported_reason

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    ModemLiveReadyReceipt,
    RuntimeGenerationNamespace,
    StartupAcquisitionSweepReceipt,
)
from arch4.architecture_contracts.application_lifecycle import (
    ProductRuntimeWorkerReplacementRequired,
    ProductRuntimeWorkerReplacementReason,
)
from arch4.interfaces import (
    Arch4Config,
    Constraints,
    ComputedSignals,
    LeverAction,
    LeverResult,
    LeverType,
    ModemInput,
    NeighborCell,
    band_signature,
    get_capabilities as get_static_capabilities,
    input_diagnostics,
    normalize_band_list,
    set_input_diagnostics,
)
from arch4.accepted_modem_generation import (
    AcceptedModemGeneration,
    RawModemGeneration,
)
from arch4.modem.base import (
    AuthenticationError, LiveAcquisitionContract, ModemInterface,
    SessionExpiredError, TargetGuardError,
)
from arch4.modem.backend_acquisition import (
    BackendAcquisitionReceipt,
    BackendAcquisitionRequest,
    DECLARED_CANONICAL_SOURCE_FIELDS,
    capture_backend_acquisition,
    matrix_cells_for_canonical_fields,
)
from arch4.modem.backend_acquisition_cells import (
    DERIVED_HEALTH_CELLS,
    TIMING_CELLS,
)
from arch4.modem.backend_contracts import (
    BackendFeatureResult,
    backend_failed,
    backend_ok,
)
from arch4.modem.catalog import get_backend_support_level
from arch4.modem_live.access_contract import (
    LIVE_ACQUISITION_BOOL_OVERRIDES,
    enforce_live_access_hard_rules,
)
from arch4.modem_live.access_scheduler import ModemLiveAccessScheduler
from arch4.modem_live.acquisition_lane_state import (
    AcquisitionLaneState,
    AcquisitionLaneStateAuthority,
)
from arch4.modem_live.faults import BackendFaultStore
from arch4.modem_live.io_gate import ModemIoGate
from arch4.modem_live.lane_status import ProtectedLiveLaneStatus
from arch4.modem_live.maintenance import (
    live_register_has_maintenance_margin,
    maintenance_refresh_lock_policy,
    neighbor_read_lock_policy,
    neighbor_scan_due,
)
from arch4.modem_live.register_status import (
    build_live_register_status,
    live_register_stale_cause,
)
from arch4.modem_live.publication import (
    ModemLivePublicationAdmissionReceipt,
    ModemLivePublicationNotice,
    ModemLivePublicationSink,
    ModemLivePublicationSinkDetachReceipt,
    ModemLiveRuntimeSupervisionPort,
    ModemLiveRuntimeSupervisionReceipt,
    ModemLiveRuntimeSupervisionSchedule,
)
from arch4.modem_live.physical_command import (
    ModemLivePhysicalStagePlan,
    ModemLivePhysicalStageResult,
    physical_stage_plans,
)
from arch4.modem_live.raw_observation import (
    ModemLiveAcquisitionManifest,
    RawModemObservation,
)
from arch4.modem_live.register_store import (
    LiveRegisterCommittedGeneration,
    LiveRegisterStore,
)
from arch4.modem_live.snapshots import InputSnapshot, LiveModemRegisterSnapshot
from arch4.modem_live.access_profile_owner import resolve_modem_live_access_profile
from arch4.modem_live.compatibility_identity import (
    DecodeCatalogSnapshot,
    ModemLiveIdentityMatrix,
    build_modem_live_identity_publication,
    DERIVED_IDENTITY_FIELDS,
)
from arch4.modem_live.reviewed_compatibility_registry import (
    PRODUCTION_REVIEWED_IDENTITY_REGISTRY,
)
from arch4.modem_live.rtt_observation_authority import (
    ModemLiveRttObservationAuthority,
)
from arch4.modem_live.rtt_register import ModemLiveRttRegisterSnapshot
from arch4.core_contracts.emergency_reachability import (
    ModemLiveInternetProbe,
    ModemLiveInternetProbeRound,
)
from arch4.modem_live.session_band_capability import (
    build_session_band_capability,
)
from arch4.modem_live.scout import ModemLiveScout
from arch4.modem_live.timing import (
    resolve_live_maintenance_interval,
    resolve_live_register_timing,
)
from arch4.modem.connectivity import (
    ConnectivityModule,
    ModemLiveRadioAvailabilityObservation,
    ModemLiveSessionRepairRequest,
    ModemLiveSessionRepairReceipt,
    PreparedConnectivityAttempt,
)
from arch4.runtime_identity import (
    build_runtime_identity,
    build_runtime_session_scope,
    merge_runtime_session_scope,
    runtime_device_namespace_key,
    runtime_identity_from_sample_sources,
)
from arch4.modem_live.runtime_identity_projection import (
    build_resolved_identity_card_section,
    runtime_identity_scope_lock_status,
)
from arch4.runtime_band_scope import resolve_available_band_values
from arch4.runtime_capability import capability_contract_action_meta
from arch4.runtime_support_tier import build_support_tier, publish_support_authority_fields
from arch4.config_views import build_input_module_config, build_signal_contract_config
from arch4.coerce import safe_bool
from arch4.effective_policy_snapshot import build_effective_policy_snapshot
from arch4.modem_live.capability_report import (
    build_capabilities_report,
    capability_report_from_matrix,
)
from arch4.modem_live.capability_matrix import (
    build_explicit_capability_cells,
)
from arch4.control.manual_queue import (
    build_band_selection_translation_action,
    build_pband_release_band_selection_action,
)
from arch4.runtime_clock import (
    ClockedJob,
    ClockedJobOutcome,
    ClockedLaneSpec,
    ClockedScheduler,
    ClockedSkipPolicy,
    MAX_CLOCK_MS,
)
from arch4.rtt_observation import (
    FRESH as RTT_FRESH,
    MatrixBoundRttObservation,
    PROBE_OK as RTT_PROBE_OK,
    RttObservation,
)
from arch4.live_seed_contract import (
    build_live_seed_payload,
    coerce_contract_list,
    extract_live_runtime_capabilities,
)
from arch4.core_contracts.command_targets import (
    CoreBandAutoTarget,
    CORE_COMMAND_TARGET_TYPES,
    CoreBandSetTarget,
    CoreNrBandTarget,
    CoreRadioModeTarget,
    validate_radio_mode_observation,
    CoreCommandTarget,
    CoreNetworkSettingsTarget,
    CorePBandTarget,
)
from arch4.core_contracts.physical_commands import (
    PhysicalCommandDispatchEvidence,
    PhysicalCommandFailureDisposition,
    PhysicalCommandReceipt,
)
from arch4.core_contracts.current_health import (
    ModemLiveCurrentHealthAcceptanceReceipt,
    ModemLiveCurrentHealthPublication,
    ModemLiveCurrentHealthSinkDetachReceipt,
)
from arch4.modem_live.truth_matrix import (
    MODEM_LIVE_DEVICE_FACT_ADDRESSES,
    MODEM_LIVE_TRUTH_FIELD_CATALOG,
    ValidatedTruthMatrixReader,
)


AUTOMATIC_RTT_PROBE_TIMEOUT_S = 1.0

log = logging.getLogger(__name__)

def _arch4_config_default_float(name: str, default_value: float) -> float:
    try:
        field = Arch4Config.__dataclass_fields__.get(name)  # type: ignore[attr-defined]
        if field is not None:
            return float(field.default)
    except Exception:
        pass
    return float(default_value)


def _optional_config_bool(config: Any, name: str) -> Optional[bool]:
    if config is None or not hasattr(config, name):
        return None
    value = getattr(config, name)
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return bool(safe_bool(value, default=False))


_LIVE_INPUT_MODULES: "weakref.WeakSet[ModemLiveModule]" = weakref.WeakSet()


@dataclass(frozen=True, slots=True)
class ModemLiveTerminationReceipt:
    namespace: RuntimeGenerationNamespace | None
    lifecycle_generation: int
    shutdown_started_at: float
    publication_sink_bound: bool
    health_sink_bound: bool
    publication_inflight_callbacks: int
    health_inflight_callbacks: int
    survivors: tuple[str, ...]
    finished_at: float

    def __post_init__(self) -> None:
        if self.namespace is not None and not isinstance(
            self.namespace,
            RuntimeGenerationNamespace,
        ):
            raise TypeError("modem_live_termination_namespace_invalid")
        if (
            type(self.lifecycle_generation) is not int
            or self.lifecycle_generation < 0
        ):
            raise ValueError("modem_live_termination_lifecycle_invalid")
        if self.namespace is not None and (
            self.lifecycle_generation
            != self.namespace.lifecycle_generation
        ):
            raise ValueError(
                "modem_live_termination_namespace_lifecycle_mismatch"
            )
        for field_name in (
            "shutdown_started_at",
            "finished_at",
        ):
            value = getattr(self, field_name)
            if (
                type(value) is not float
                or not math.isfinite(value)
                or value <= 0.0
            ):
                raise ValueError(
                    f"modem_live_termination_{field_name}_invalid"
                )
        if self.finished_at < self.shutdown_started_at:
            raise ValueError("modem_live_termination_time_order_invalid")
        for field_name in (
            "publication_sink_bound",
            "health_sink_bound",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(
                    f"modem_live_termination_{field_name}_invalid"
                )
        for field_name in (
            "publication_inflight_callbacks",
            "health_inflight_callbacks",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"modem_live_termination_{field_name}_invalid"
                )
        if (
            not isinstance(self.survivors, tuple)
            or any(
                type(name) is not str
                or not name
                or name != name.strip()
                for name in self.survivors
            )
        ):
            raise TypeError("modem_live_termination_survivors_invalid")

    @property
    def terminated(self) -> bool:
        return bool(
            not self.publication_sink_bound
            and not self.health_sink_bound
            and self.publication_inflight_callbacks == 0
            and self.health_inflight_callbacks == 0
            and not self.survivors
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "ModemLiveTerminationReceipt.v2",
            "owner": "54guc.modem_live",
            "runtime_namespace": (
                self.namespace.as_dict()
                if self.namespace is not None
                else {}
            ),
            "lifecycle_generation": self.lifecycle_generation,
            "shutdown_started_at": self.shutdown_started_at,
            "publication_sink_bound": self.publication_sink_bound,
            "health_sink_bound": self.health_sink_bound,
            "publication_inflight_callbacks": (
                self.publication_inflight_callbacks
            ),
            "health_inflight_callbacks": (
                self.health_inflight_callbacks
            ),
            "survivors": list(self.survivors),
            "finished_at": self.finished_at,
            "terminated": self.terminated,
        }


def shutdown_all_live_input_modules_for_tests(*, join_timeout_s: float = 1.0) -> int:
    count = 0
    failures = []
    for module in list(_LIVE_INPUT_MODULES):
        try:
            receipt = module.shutdown(join_timeout_s=join_timeout_s)
            if not isinstance(receipt, ModemLiveTerminationReceipt):
                raise TypeError("modem_live_termination_receipt_required")
            if not receipt.terminated:
                raise RuntimeError(f"modem_live_termination_incomplete:{receipt.as_dict()}")
            count += 1
        except Exception as exc:
            failures.append(f"{type(exc).__name__}:{exc}")
    if failures:
        raise RuntimeError("modem_live_test_cleanup_failed: " + "; ".join(failures))
    return count


def _truthy_capability_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {
        "1",
        "true",
        "yes",
        "enabled",
        "available",
        "ok",
        "cat12_implied",
    }


def _build_bootstrap_constraints(
    *,
    provider_plmn: str = "",
    provider_name: str = "",
    static_config: Optional[Dict[str, Any]] = None,
    ue_capability: Optional[Dict[str, Any]] = None,
) -> Constraints:
    constraints = Constraints()
    constraints.provider_plmn = str(provider_plmn or "")
    constraints.provider_name = str(provider_name or "")
    evidence_keys: set[str] = set()

    static = dict(static_config or {})
    retired = {"dns_cache", "fasttrack"}.intersection(static)
    if retired:
        raise ValueError("retired_static_capability:" + ",".join(sorted(retired)))
    for key in ("ttl_ipv4", "ttl_ipv6", "mss_ipv4", "mss_ipv6", "keepalive"):
        if key not in static:
            continue
        setattr(constraints, key, bool(static.get(key)))
        evidence_keys.add(key)
    for key, value in static.items():
        if key in {"ttl_ipv4", "ttl_ipv6", "mss_ipv4", "mss_ipv6", "keepalive"}:
            continue
        setattr(constraints, str(key), value)
        evidence_keys.add(str(key))

    ue = dict(ue_capability or {})
    if "dl_256qam" in ue:
        constraints.dl_256qam = _truthy_capability_flag(ue.get("dl_256qam"))
        evidence_keys.add("dl_256qam")
    if "ul_64qam" in ue:
        ul_64_raw = str(ue.get("ul_64qam", "") or "").strip().lower()
        constraints.ul_64qam = (
            ul_64_raw == "cat12_implied"
            or _truthy_capability_flag(ue.get("ul_64qam"))
            or ("1" in ul_64_raw and ul_64_raw not in {"0", "not_supported"})
        )
        evidence_keys.add("ul_64qam")
    if "pci_lock_available" in ue or "pci_lock_status" in ue:
        pci_status = str(ue.get("pci_lock_status", "") or "").strip().lower()
        constraints.pci_lock_available = (
            bool(ue.get("pci_lock_available"))
            or _truthy_capability_flag(ue.get("pci_lock_status"))
            or ("1" in pci_status and pci_status not in {"0", "0,0"})
        )
        evidence_keys.add("pci_lock_available")
    if "ue_category" in ue:
        try:
            constraints.ue_category = int(str(ue.get("ue_category", "0") or "0"))
        except Exception:
            constraints.ue_category = 0
        evidence_keys.add("ue_category")
    setattr(constraints, "_capability_static_evidence_keys", frozenset(evidence_keys))
    return constraints


def _build_bootstrap_live_snapshot(
    *,
    modem_input: Optional[ModemInput] = None,
    public_status: Optional[Dict[str, Any]] = None,
    modem_info: Optional[Dict[str, Any]] = None,
    provider_plmn: str = "",
    provider_name: str = "",
) -> Dict[str, Any]:
    snapshot = dict(public_status or {})
    if modem_input is not None:
        modem_payload = build_live_seed_payload_from_modem(
            modem_input,
            provider_plmn=provider_plmn,
            provider_name=provider_name,
            modem_info=modem_info,
        ) or {}
        snapshot.update({k: v for k, v in modem_payload.items() if v not in (None, "", [], {})})
    if modem_info:
        snapshot.setdefault("vendor", str(modem_info.get("vendor", "") or ""))
        snapshot.setdefault("model", str(modem_info.get("model", "") or snapshot.get("model", "") or ""))
        snapshot.setdefault("firmware", str(modem_info.get("firmware", "") or snapshot.get("firmware", "") or ""))
        snapshot.setdefault("imei", str(modem_info.get("imei", "") or snapshot.get("imei", "") or ""))
    if provider_plmn:
        snapshot.setdefault("provider_plmn", str(provider_plmn))
        snapshot.setdefault("plmn", str(provider_plmn))
    if provider_name:
        snapshot.setdefault("provider_name", str(provider_name))
        snapshot.setdefault("provider", str(provider_name))
    return snapshot


def _build_bootstrap_report_modem(
    *,
    modem_input: Optional[ModemInput] = None,
    public_status: Optional[Dict[str, Any]] = None,
    modem_info: Optional[Dict[str, Any]] = None,
    provider_plmn: str = "",
    provider_name: str = "",
) -> Any:
    snapshot = _build_bootstrap_live_snapshot(
        modem_input=modem_input,
        public_status=public_status,
        modem_info=modem_info,
        provider_plmn=provider_plmn,
        provider_name=provider_name,
    )
    return SimpleNamespace(
        get_report_info=lambda: dict(modem_info or {}),
        get_report_status=lambda: dict(public_status or {}),
        get_modem_info=lambda: dict(modem_info or {}),
        get_last_status=lambda: dict(public_status or {}),
        radio_connected=bool(snapshot.get("radio_connected", False)),
        radio_state_text=str(snapshot.get("radio_state_text", "") or ""),
        serving_band=int(snapshot.get("serving_band", 0) or snapshot.get("primary_band", 0) or 0),
        serving_band_signature=str(snapshot.get("serving_band_signature", "") or ""),
        ca_active=bool(snapshot.get("ca_active", False)),
        configured_band_pool=list(snapshot.get("configured_band_pool", []) or []),
        configured_band_pool_signature=str(snapshot.get("configured_band_pool_signature", "") or ""),
        configured_band_pool_source=str(snapshot.get("configured_band_pool_source", "") or ""),
        active_band_set=list(snapshot.get("active_band_set", []) or []),
        ca_state_text=str(snapshot.get("ca_state_text", "") or ""),
        pci=int(snapshot.get("pci", 0) or 0),
        session_uptime_s=float(snapshot.get("session_uptime_s", 0.0) or 0.0),
        session_uptime_source=str(
            snapshot.get("session_uptime_source", "") or ""
        ),
        sinr_db=float(snapshot.get("sinr_db", 0.0) or 0.0),
        rsrp_dbm=float(snapshot.get("rsrp_dbm", 0.0) or 0.0),
        rsrq_db=float(snapshot.get("rsrq_db", 0.0) or 0.0),
        mcs_dl=int(snapshot.get("mcs_dl", snapshot.get("mcs_dl0", 0)) or 0),
        mcs_ul=int(snapshot.get("mcs_ul", 0) or 0),
        throughput_dl_mbps=float(snapshot.get("throughput_dl_mbps", snapshot.get("dl_mbps", 0.0)) or 0.0),
        throughput_ul_mbps=float(snapshot.get("throughput_ul_mbps", snapshot.get("ul_mbps", 0.0)) or 0.0),
        neighbors=coerce_contract_list(snapshot.get("neighbors")),
        _lte_id=str(snapshot.get("iface_id", "") or ""),
    )


def _merge_live_seed_context(
    payload: Dict[str, Any],
    *,
    selected_access_profile,
    modem_input: Optional[ModemInput] = None,
    public_status: Optional[Dict[str, Any]] = None,
    modem_info: Optional[Dict[str, Any]] = None,
    provider_plmn: str = "",
    provider_name: str = "",
    capabilities: Optional[Any] = None,
    static_config: Optional[Dict[str, Any]] = None,
    ue_capability: Optional[Dict[str, Any]] = None,
    config: Optional[Arch4Config] = None,
) -> Dict[str, Any]:
    merged = dict(payload or {})
    status = dict(public_status or {})
    info = dict(modem_info or {})
    runtime_identity = (
        dict(merged.get("runtime_identity"))
        if isinstance(merged.get("runtime_identity"), dict)
        else {}
    )
    live_capture = (
        dict(merged.get("live_capture"))
        if isinstance(merged.get("live_capture"), dict)
        else {}
    )
    merged["runtime_identity"] = runtime_identity
    merged["live_capture"] = live_capture

    if capabilities is not None and config is not None:
        runtime_caps = build_live_runtime_capabilities_payload(
            config=config,
            selected_access_profile=selected_access_profile,
            capabilities=capabilities,
            modem_info=info,
            modem_input=modem_input,
            public_status=status,
            provider_plmn=str(merged.get("provider_plmn", "") or provider_plmn or ""),
            provider_name=str(merged.get("provider_name", "") or provider_name or ""),
            static_config=static_config,
            ue_capability=ue_capability,
        )
        if runtime_caps:
            merged["runtime_capabilities"] = dict(runtime_caps)
    return merged


def build_live_runtime_capabilities_payload(
    *,
    config: Arch4Config,
    capabilities: Any,
    selected_access_profile,
    modem_info: Optional[Dict[str, Any]] = None,
    modem_input: Optional[ModemInput] = None,
    public_status: Optional[Dict[str, Any]] = None,
    provider_plmn: str = "",
    provider_name: str = "",
    static_config: Optional[Dict[str, Any]] = None,
    ue_capability: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    from arch4.modem.live_lane_contract import ModemLiveAccessProfile
    if not isinstance(selected_access_profile, ModemLiveAccessProfile):
        raise TypeError("runtime_capability_selected_access_profile_required")
    selected_profile = selected_access_profile.as_dict()
    if capabilities is None:
        return None

    if isinstance(capabilities, dict) and "capability_contract" in capabilities:
        payload = dict(capabilities)
        published = payload["capability_contract"].get("backend_io_lane_contract", {}).get("access_profile")
        if published != selected_profile or payload.get("modem_live_access_profile") != selected_profile:
            raise RuntimeError("runtime_capability_selected_access_profile_mismatch")
        if "support_tier" in payload:
            publish_support_authority_fields(payload)
        return payload
    if isinstance(capabilities, dict):
        raise RuntimeError("runtime_capability_dict_without_contract_illegal")

    info = dict(modem_info or {})
    status = dict(public_status or {})
    status_runtime_identity = (
        status.get("runtime_identity")
        if isinstance(status.get("runtime_identity"), dict)
        else {}
    )
    backend = str(
        status.get("backend")
        or status_runtime_identity.get("backend")
        or info.get("backend")
        or getattr(config, "modem_backend", "")
        or ""
    ).strip().lower()
    report_config = config
    if backend and backend != str(getattr(config, "modem_backend", "") or "").strip().lower():
        report_config = copy.copy(config)
        report_config.modem_backend = backend
    resolver = getattr(config, "connection_defaults_for_backend", None)
    host, user = "", ""
    if callable(resolver):
        try:
            resolved = resolver(backend)
            if isinstance(resolved, (list, tuple)) and len(resolved) >= 2:
                host, user = str(resolved[0] or ""), str(resolved[1] or "")
        except Exception:
            host, user = "", ""
    transport_resolver = getattr(config, "connection_transport_for_backend", None)
    iface = (
        str(transport_resolver(backend) or "unknown")
        if callable(transport_resolver)
        else "unknown"
    )
    vendor = str(status.get("vendor", "") or info.get("vendor", "") or "")
    model = str(status.get("model", "") or status.get("router_model", "") or info.get("model", "") or "")
    firmware = str(status.get("firmware", "") or info.get("firmware", "") or "")
    imei = str(info.get("imei", "") or status.get("imei", "") or "")
    primary_band = getattr(modem_input, "primary_band", 0)
    deployed_bands = resolve_available_band_values(
        selected_band_set=getattr(modem_input, "selected_band_set", ()),
        band_set=getattr(modem_input, "band_set", ()),
        active_band_set=getattr(modem_input, "active_band_set", ()),
        ca_band_signature=getattr(modem_input, "ca_band_signature", ""),
        primary_band=primary_band,
        serving_band=getattr(modem_input, "serving_band", primary_band),
        status=status if isinstance(status, dict) else {},
    )
    constraints = _build_bootstrap_constraints(
        provider_plmn=provider_plmn,
        provider_name=provider_name,
        static_config=static_config,
        ue_capability=ue_capability,
    )
    stub_modem = _build_bootstrap_report_modem(
        modem_input=modem_input,
        public_status=status,
        modem_info=info,
        provider_plmn=provider_plmn,
        provider_name=provider_name,
    )
    live_snapshot = _build_bootstrap_live_snapshot(
        modem_input=modem_input,
        public_status=status,
        modem_info=info,
        provider_plmn=provider_plmn,
        provider_name=provider_name,
    )
    if ("modem_live_access_profile" in live_snapshot
            and live_snapshot["modem_live_access_profile"] != selected_profile):
        raise RuntimeError("runtime_capability_selected_access_profile_mismatch")
    live_snapshot["modem_live_access_profile"] = selected_profile
    report = build_capabilities_report(
        cfg=report_config,
        capabilities=capabilities,
        constraints=constraints,
        modem=stub_modem,
        deployed_bands=deployed_bands,
        effective_policy=build_effective_policy_snapshot(report_config, deployed_bands),
        live_snapshot=live_snapshot,
    )
    report["support"].setdefault("support_level", get_backend_support_level(backend))
    # Capability reports keep raw connection identity for support. Current live
    # identity is stored in ``report["runtime_identity"]``.
    report["identity"]["target"] = str(host or report["identity"].get("target", "") or "")
    report["identity"]["user"] = str(user or report["identity"].get("user", "") or "")
    report["identity"]["iface"] = str(iface or report["identity"].get("iface", "") or "unknown")
    report["identity"]["vendor"] = vendor or str(report["identity"].get("vendor", "") or "")
    report["identity"]["model"] = model or str(report["identity"].get("model", "") or "")
    report["identity"]["firmware"] = firmware or str(report["identity"].get("firmware", "") or "")
    report["identity"]["imei"] = imei or str(report["identity"].get("imei", "") or "")
    report["identity"]["provider"] = str(provider_name or report["identity"].get("provider", "") or "")
    report["identity"]["plmn"] = str(provider_plmn or report["identity"].get("plmn", "") or "")
    runtime_identity = build_runtime_identity(
        backend=backend,
        vendor=str(report["identity"].get("vendor", "") or ""),
        model=str(report["identity"].get("model", "") or ""),
        firmware=str(report["identity"].get("firmware", "") or ""),
        provider_name=str(report["identity"].get("provider", "") or provider_name or ""),
        provider_plmn=str(report["identity"].get("plmn", "") or provider_plmn or ""),
        profile_name=str(getattr(config, "model_profile_name", "") or ""),
    ).to_dict()
    for key in (
        "target",
        "iface",
        "serial",
        "imei",
        "imsi",
        "hardware_version",
        "software_version",
        "webui",
        "config_version",
        "lan_mac",
        "wan_ip",
        "antenna",
    ):
        value = str(report["identity"].get(key) or "")
        if value:
            runtime_identity[key] = value
    report["runtime_identity"] = runtime_identity
    tier = build_support_tier(report)
    report["support_tier"] = dict(tier)
    publish_support_authority_fields(report)
    return report
def build_live_seed_payload_from_modem(
    modem: Optional[ModemInput],
    *,
    provider_plmn: str = "",
    provider_name: str = "",
    modem_info: Optional[Dict[str, Any]] = None,
    acquired_cells: Optional[Iterable[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Build initial live values directly from a modem read."""
    if modem is None:
        return None

    info = modem_info if isinstance(modem_info, dict) else {}
    configured_band_pool = normalize_band_list(
        getattr(modem, "configured_band_pool", [])
        or (getattr(modem, "selected_band_set", [])
            if not str(getattr(modem, "configured_band_pool_status", "") or "") else [])
        or []
    )
    configured_pool_signature = band_signature(configured_band_pool)
    active_band_set = list(getattr(modem, "active_band_set", []) or [])
    acquired = frozenset(str(cell) for cell in (acquired_cells or ()))

    def observed(*paths: str, value: Any) -> bool:
        if acquired:
            return any(path in acquired for path in paths)
        return value not in (None, "", 0, 0.0, [], ())

    live_capture: Dict[str, Any] = {
        "radio_connected": bool(getattr(modem, "radio_connected", False)),
        "ca_active": bool(getattr(modem, "ca_active", False)),
        "ca_state_text": str(getattr(modem, "ca_state_text", "") or ""),
        "radio_state_text": "Connected" if bool(getattr(modem, "radio_connected", False)) else "Disconnected",
    }
    optional_values = (
        (("radio.serving_band",), "serving_band", int(getattr(modem, "serving_band", 0) or 0)),
        (("radio.serving_band",), "primary_band", int(getattr(modem, "primary_band", 0) or 0)),
        (("radio.serving_band",), "serving_band_signature", str(getattr(modem, "serving_band_signature", "") or "")),
        (("rf.pci",), "pci", int(getattr(modem, "pci", 0) or 0)),
        (("rf.earfcn_dl", "rf.earfcn"), "earfcn", int(getattr(modem, "earfcn_dl", getattr(modem, "earfcn", 0)) or 0)),
        (("rf.cell_id",), "cell_id", int(getattr(modem, "cell_id", 0) or 0)),
        (("rf.sinr_db",), "sinr_db", float(getattr(modem, "sinr_db", 0.0) or 0.0)),
        (("rf.rsrp_dbm",), "rsrp_dbm", float(getattr(modem, "rsrp_dbm", 0.0) or 0.0)),
        (("rf.rsrq_db",), "rsrq_db", float(getattr(modem, "rsrq_db", 0.0) or 0.0)),
        (("scheduler.mcs_dl",), "mcs_dl", int(getattr(modem, "mcs_dl", 0) or 0)),
        (("scheduler.mcs_dl0",), "mcs_dl0", int(getattr(modem, "mcs_dl0", 0) or 0)),
        (("scheduler.mcs_dl1",), "mcs_dl1", int(getattr(modem, "mcs_dl1", 0) or 0)),
        (("scheduler.mcs_ul",), "mcs_ul", int(getattr(modem, "mcs_ul", 0) or 0)),
        (("scheduler.cqi",), "cqi", int(getattr(modem, "cqi", 0) or 0)),
        (("scheduler.cqi0",), "cqi0", int(getattr(modem, "cqi0", 0) or 0)),
        (("scheduler.cqi1",), "cqi1", int(getattr(modem, "cqi1", 0) or 0)),
        (("scheduler.ri",), "ri", int(getattr(modem, "ri", 0) or 0)),
        (
            ("traffic.session_uptime_source",),
            "session_uptime_source",
            str(getattr(modem, "session_uptime_source", "") or ""),
        ),
        (("radio.configured_band_pool", "radio.selected_band_set"), "configured_band_pool", configured_band_pool),
        (("radio.configured_band_pool", "radio.selected_band_set"), "configured_band_pool_signature", configured_pool_signature),
        (("radio.configured_band_pool", "radio.selected_band_set"), "configured_band_pool_source", str(getattr(modem, "configured_band_pool_source", "") or "")),
        (("radio.active_band_set",), "active_band_set", active_band_set),
        (("radio.configured_band_pool", "radio.selected_band_set"), "carrier_count", len(configured_band_pool)),
        (("rf.neighbors",), "neighbors", coerce_contract_list(getattr(modem, "neighbors", []))),
    )
    for paths, name, value in optional_values:
        if observed(*paths, value=value):
            live_capture[name] = value
    if configured_pool_signature and (
        "configured_band_pool" in live_capture
    ):
        live_capture["ca_signature"] = configured_pool_signature

    payload: Dict[str, Any] = {
        "ts": float(getattr(modem, "timestamp", time.time()) or time.time()),
        "source_kind": "startup_live_modem",
        "runtime_identity": {
            "vendor": str(getattr(modem, "vendor", "") or info.get("vendor", "") or ""),
            "model": str(getattr(modem, "model", "") or info.get("model", "") or ""),
            "router_model": str(getattr(modem, "model", "") or info.get("model", "") or ""),
            "firmware": str(getattr(modem, "firmware", "") or info.get("firmware", "") or ""),
            "serial": str(info.get("serial", "") or ""),
            "imei": str(info.get("imei", "") or ""),
            "provider_plmn": str(provider_plmn or ""),
            "provider_name": str(provider_name or ""),
        },
        "live_capture": live_capture,
    }
    return build_live_seed_payload(payload)


def _derived_traffic_unit_receipts(
    receipts: tuple[BackendAcquisitionReceipt, ...],
) -> tuple[BackendAcquisitionReceipt, ...]:
    """Keep each conversion with its input; combine only proven directions."""

    acquired = frozenset(
        cell
        for receipt in receipts
        for cell in receipt.acquired_cells
    )
    derived: list[BackendAcquisitionReceipt] = []
    rates: dict[str, tuple[float, BackendAcquisitionReceipt]] = {}
    for receipt in receipts:
        cells: set[str] = set()
        for field, outputs in (
            ("rx_bytes_per_sec", {"traffic.dl_mbps", "traffic.throughput_dl_mbps"}),
            ("tx_bytes_per_sec", {"traffic.ul_mbps", "traffic.throughput_ul_mbps"}),
        ):
            if f"traffic.{field}" not in receipt.acquired_cells:
                continue
            value = getattr(receipt.modem, field, None)
            if value is None:
                value = receipt.status.get(field)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                continue
            rates[field] = (float(value), receipt)
            cells.update(outputs.difference(acquired))
        if cells:
            derived.append(replace(receipt, acquired_cells=frozenset(cells)))
    if len(rates) == 2 and "traffic.has_traffic" not in acquired:
        contributor = max(
            (item[1] for item in rates.values()),
            key=lambda item: (item.finished_at, item.request.attempt_id),
        )
        combined = ModemInput.safe_defaults(
            timestamp=contributor.finished_at,
            rx_bytes_per_sec=rates["rx_bytes_per_sec"][0],
            tx_bytes_per_sec=rates["tx_bytes_per_sec"][0],
            traffic_valid=all(
                bool(getattr(item[1].modem, "traffic_valid", True))
                for item in rates.values()
            ),
        )
        derived.append(replace(
            contributor, modem=combined, status={},
            acquired_cells=frozenset({"traffic.has_traffic"}),
        ))
    return tuple(derived)


def _fresh_radio_availability_observation_from_receipts(
    receipts: tuple[BackendAcquisitionReceipt, ...],
) -> ModemLiveRadioAvailabilityObservation | None:
    """Return the newest explicitly acquired backend radio observation."""

    candidates = tuple(
        receipt
        for receipt in receipts
        if "health.radio_connected" in receipt.acquired_cells
    )
    if not candidates:
        return None
    receipt = max(
        candidates,
        key=lambda item: (
            float(item.finished_at),
            str(item.request.attempt_id),
        ),
    )
    missing = object()
    modem = receipt.acquired_modem_copy()
    value = (
        getattr(modem, "radio_connected", missing)
        if modem is not None
        else missing
    )
    if value is missing:
        value = receipt.status_copy().get("radio_connected", missing)
    if type(value) is not bool:
        raise TypeError("fresh_radio_connected_boolean_required")
    return ModemLiveRadioAvailabilityObservation(
        available=value,
        started_at=float(receipt.request.started_at),
        observed_at=float(receipt.finished_at),
    )


def _derived_ca_pool_context_receipt(
    receipts: tuple[BackendAcquisitionReceipt, ...],
) -> BackendAcquisitionReceipt | None:
    """Bind normalized CA context to the configured band pool read from modem."""

    acquired = frozenset(
        cell
        for receipt in receipts
        for cell in receipt.acquired_cells
    )
    if not {
        "radio.configured_band_pool",
        "radio.selected_band_set",
    }.intersection(acquired):
        return None
    cells = {
        "radio.ca_signature",
        "radio.ca_band_signature",
        "radio.carrier_count",
    }.difference(acquired)
    if not cells:
        return None
    contributor = max(
        (receipt for receipt in receipts if receipt.acquired_cells.intersection({
            "radio.configured_band_pool", "radio.selected_band_set",
        })),
        key=lambda item: (item.finished_at, item.request.attempt_id),
    )
    field = (
        "configured_band_pool"
        if "radio.configured_band_pool" in contributor.acquired_cells
        else "selected_band_set"
    )
    pool = getattr(contributor.modem, field, None) or contributor.status.get(field)
    derived_modem = ModemInput.safe_defaults(
        timestamp=contributor.finished_at,
        configured_band_pool=normalize_band_list(pool or []),
    )
    return replace(
        contributor,
        modem=derived_modem,
        status={},
        acquired_cells=frozenset(cells),
    )


class ModemLiveModule:
    """Runtime implementation of Modem Live acquisition.

    Modem Live owns the register, scheduling, I/O locks, RTT timing, and neighbor
    timing. Backends expose hardware data and optional read methods through
    ``get_live_acquisition_contract()``. They do not bypass the live register.
    """

    def bind_provider_pool_policy(self, resolver) -> None:
        """Explicit Foundation policy port for opted-in boundary adapters."""
        if not callable(resolver):
            raise TypeError("modem_live_provider_pool_policy_required")
        if getattr(self._modem_backend, "requires_provider_pool_policy", False):
            self._modem_backend.bind_provider_pool_policy(resolver)

    @property
    def _live_register_snapshot(self) -> LiveModemRegisterSnapshot:
        raise RuntimeError(
            "live_register_snapshot_read_forbidden:"
            "use_committed_generation_or_current_status"
        )

    @_live_register_snapshot.setter
    def _live_register_snapshot(self, value: LiveModemRegisterSnapshot) -> None:
        del value
        raise RuntimeError(
            "modem_live_snapshot_assignment_forbidden_use_commit_observation"
        )

    @property
    def _protected_live_lane_status(self) -> Dict[str, Any]:
        return self._ensure_protected_live_lane().current()

    @_protected_live_lane_status.setter
    def _protected_live_lane_status(self, value: Dict[str, Any]) -> None:
        lane_status = getattr(self, "_protected_live_lane", None)
        if isinstance(lane_status, ProtectedLiveLaneStatus):
            lane_status.set(value)
            return
        self._protected_live_lane = ProtectedLiveLaneStatus(value)

    def _ensure_protected_live_lane(self) -> ProtectedLiveLaneStatus:
        lane_status = getattr(self, "_protected_live_lane", None)
        if isinstance(lane_status, ProtectedLiveLaneStatus):
            return lane_status
        lane_status = ProtectedLiveLaneStatus()
        self._protected_live_lane = lane_status
        return lane_status

    @property
    def _backend_fault_counts(self) -> Dict[str, Dict[str, Any]]:
        store = getattr(self, "_backend_fault_store", None)
        if isinstance(store, BackendFaultStore):
            return store.counts
        return {}

    @_backend_fault_counts.setter
    def _backend_fault_counts(self, value: Dict[str, Dict[str, Any]]) -> None:
        store = getattr(self, "_backend_fault_store", None)
        if isinstance(store, BackendFaultStore):
            store.counts = {str(key): dict(item or {}) for key, item in dict(value or {}).items()}
            return
        self._backend_fault_store = BackendFaultStore(value)

    def _ensure_backend_fault_store(self) -> BackendFaultStore:
        store = getattr(self, "_backend_fault_store", None)
        if isinstance(store, BackendFaultStore):
            return store
        store = BackendFaultStore()
        self._backend_fault_store = store
        return store

    def __init__(
        self,
        config: Arch4Config,
        modem: ModemInterface,
        health_checks: Optional[Any] = None,
        ops_root_dir: str | None = None,
        startup_wire_trace: StartupModemTrace | None = None,
        wire_trace_sink: Any = None,
        restored_identity_catalog: DecodeCatalogSnapshot | None = None,
        device_association_reader=None,
        native_read_event_sink: Any = None,
    ) -> None:
        self.cfg = config
        self.input_cfg = build_input_module_config(config)
        self.signal_contract_cfg = build_signal_contract_config(config)
        # The backend is private to Modem Live. Every physical operation uses
        # ``_call_modem`` or ``_modem_io`` so access stays serialized and follows
        # profile timing with recorded evidence.
        self._modem_backend = modem
        self.health_checks = health_checks
        self.ops_root_dir = ops_root_dir
        self._startup_wire_trace = startup_wire_trace
        self._wire_trace_sink = wire_trace_sink
        self._native_read_event_sink = native_read_event_sink
        self._device_association_reader=device_association_reader
        self._startup_device_associations=()
        # Sequence values may restart with the process or module. Consumers must
        # pair each value with this epoch instead of treating the number as
        # globally unique.
        self._publication_source_epoch = uuid.uuid4().hex
        self._product_lifecycle_generation = 0
        self._modem_live_ready_receipt: ModemLiveReadyReceipt | None = None

        self._io_lock = threading.RLock()
        now = time.time()
        self._last_neighbor_scan: float = 0.0
        self._neighbor_scan_interval: float = float(self.input_cfg.neighbor_scan_interval_s)
        self._cached_neighbors: List[NeighborCell] = []
        self._rtt_observation_authority = ModemLiveRttObservationAuthority()
        self._rtt_probe_execution_lock = threading.Lock()
        self._rtt_cadence_lock = threading.Lock()
        self._rtt_cadence_stop: Optional[threading.Event] = None
        self._rtt_cadence_thread: Optional[threading.Thread] = None
        self._rtt_cadence_last_attempt_ts = 0.0
        self._rtt_cadence_last_success_ts = 0.0
        self._rtt_cadence_last_error = ""
        self._rtt_confirmation_active = False
        self._rtt_confirmation_index = 0
        self._service_confirmation_request = None
        self._service_confirmation_receipt = None
        self._last_service_confirmation_at = 0.0
        # A split-lane backend publishes the first fast cycle before starting
        # slow enrichment.
        self._last_maintenance_refresh_ts: float = now
        self._network_inspection_demand = None
        self._read_diagnostic_demand = None
        self._maintenance_lock = threading.Lock()
        self._maintenance_thread: Optional[threading.Thread] = None
        self._read_lane_lock = threading.RLock()
        self._read_lane_changed = threading.Condition(self._read_lane_lock)
        self._read_lane_stop = threading.Event()
        self._read_lane_thread: Optional[threading.Thread] = None
        self._read_lane_request: Optional[
            tuple[int, bool, BackendAcquisitionRequest]
        ] = None
        self._read_lane_sequence = 0
        self._read_lane_completed_sequence = 0
        self._read_lane_result: Optional[
            BackendAcquisitionReceipt
        ] = None
        self._read_lane_error: Optional[BaseException] = None
        self._acquisition_lane_state = AcquisitionLaneStateAuthority()
        self._last_non_ping_probe_ts: float = 0.0
        self._cached_non_ping_probe_results: Dict[str, bool] = {}
        self._non_ping_probe_lock = threading.Lock()
        self._non_ping_probe_lifecycle_lock = threading.RLock()
        self._non_ping_probe_running: bool = False
        self._non_ping_probe_thread: Optional[threading.Thread] = None
        self._health_connect_settle_connected_since: float = 0.0
        self._traffic_presence_lock = threading.Lock()
        self._traffic_presence_lifecycle_lock = threading.RLock()
        self._traffic_presence_thread: Optional[threading.Thread] = None
        self._traffic_presence_stop: Optional[threading.Event] = None
        self._traffic_presence_active: bool = False
        self._traffic_presence_interval_s: float = 0.0
        self._traffic_presence_target: str = str(self.input_cfg.rtt_probe_target or "8.8.8.8")
        self._connectivity = ConnectivityModule(
            reconnect_interval_s=self._modem_reconnect_interval_s(),
            read_miss_operator_min_s=self._modem_read_missed_operator_min_s(),
        )
        self._last_runtime_session_scope: Dict[str, str] = {}
        self._last_runtime_session_scope_key: str = ""
        self._active_device_identity_key: str = ""
        self._active_device_identity: Dict[str, str] = {}
        self._pending_device_replacement: tuple[RuntimeGenerationNamespace, RuntimeGenerationNamespace, ProductRuntimeWorkerReplacementReason] | None = None
        self._reconnection_identity_pending = False
        self._device_cache_lock = threading.RLock()
        self._last_runtime_identity: Dict[str, Any] = {}
        self._last_verified_modem_identity: Dict[str, Any] = {}
        self._last_resolved_identity_card: Dict[str, Any] = {}
        self._cached_runtime_capabilities: Dict[str, Any] = {}
        self._session_band_capability_scope = ""
        self._session_band_capability_status: Dict[str, Any] = {}
        self._session_band_capability_observed_at = 0.0
        self._session_band_capability_reported_id = ""
        self._cached_modem_info: Dict[str, Any] = {}
        self._cached_provider_plmn: str = ""
        self._cached_provider_name: str = ""
        self._backend_fault_store = BackendFaultStore()
        self._identity_matrix = ModemLiveIdentityMatrix(
            PRODUCTION_REVIEWED_IDENTITY_REGISTRY,
            restored_catalog=restored_identity_catalog,
        )
        self._scout = ModemLiveScout(self.ops_root_dir)
        self._access_scheduler = ModemLiveAccessScheduler()
        self._modem_io_gate = ModemIoGate()
        self._last_fast_modem_read_ms: float = 0.0
        self._cached_maintenance_requested: bool = False
        self._cached_maintenance_requested_ts: float = 0.0
        self._cached_maintenance_requested_source: str = "initial"
        self._startup_live_baseline_status: Dict[str, Any] = {
            "owner": "modem_live",
            "completed": False,
            "attempted": False,
            "source": "",
            "started_at": 0.0,
            "finished_at": 0.0,
            "duration_ms": 0.0,
            "last_error": "",
        }
        self._event_write_pending_lock = threading.Condition()
        self._event_write_pending_tokens: set[str] = set()
        self._event_write_pending_since_ts: float = 0.0
        self._event_write_pending_last: Dict[str, Any] = {}
        self._live_acquisition_contract: LiveAcquisitionContract = (
            self._coerce_live_acquisition_contract()
        )
        self._live_register_lock = threading.RLock()
        self._modem_live_shutdown = threading.Event()
        self._live_register_thread: Optional[threading.Thread] = None
        self._live_register_worker_stop: Optional[threading.Event] = None
        self._live_register_restart_requested: bool = False
        self._runtime_clock_observes_live_register: bool = False
        self._live_register_store = LiveRegisterStore()
        self._pending_acquisition_receipts: tuple[
            BackendAcquisitionReceipt,
            ...,
        ] = ()
        self.runtime_supervision_port = ModemLiveRuntimeSupervisionPort(
            self._runtime_supervision_schedule,
            self._run_runtime_supervision,
        )
        self._live_register_refreshing: bool = False
        self._live_register_refresh_active_count: int = 0
        self._live_register_worker_refreshing: bool = False
        self._live_register_worker_attempt_started_ts: float = 0.0
        self._live_register_worker_last_finished_ts: float = 0.0
        self._priority_live_register_refreshing: bool = False
        self._priority_live_register_lifecycle_lock = threading.RLock()
        self._priority_live_register_refresh_thread: Optional[threading.Thread] = None
        self._priority_live_register_refresh_status: Dict[str, Any] = {}
        self._live_register_clock_status: Dict[str, Any] = {}
        self._live_register_last_attempt_ts: float = 0.0
        self._live_register_physical_attempt_sequence: int = 0
        self._live_register_last_miss_ts: float = 0.0
        self._live_register_last_reason: str = ""
        self._live_register_publication_sink: Optional[ModemLivePublicationSink] = None
        self._current_health_publication_sink: Optional[
            Callable[
                [ModemLiveCurrentHealthPublication],
                ModemLiveCurrentHealthAcceptanceReceipt,
            ]
        ] = None
        self._current_health_publication_inflight = 0
        self._live_register_publication_inflight: int = 0
        self._live_register_publication_closed = False
        self._live_register_pending_publication_notice: (
            ModemLivePublicationNotice | None
        ) = None
        self._startup_publication_admission_open = False
        self._startup_publication_notice: (
            ModemLivePublicationNotice | None
        ) = None
        self._startup_publication_primary_receipt: (
            tuple[MatrixGenerationIdentity, BackendAcquisitionReceipt] | None
        ) = None
        self._live_register_publication_changed = threading.Condition(
            self._live_register_lock
        )
        self._live_register_publication_sink_error: str = ""
        self._live_register_read_miss_count: int = 0
        self._live_register_intake_sequences: Dict[str, int] = {"engine_cycle": 0}
        self._live_register_intake_sample_ts: Dict[str, float] = {"engine_cycle": 0.0}
        self._last_engine_consumed_live_register_sequence: int = 0
        self._last_engine_consumed_live_register_sample_ts: float = 0.0
        self._engine_intake_turbo_mode: str = "engine_latest_register_view"
        self._engine_intake_turbo_missing_total: int = 0
        self._last_register_intake_gap_warning_sequence: int = 0
        self._last_modem_sample_slo_warning_sequence: int = 0
        self._live_register_connectivity_status: Dict[str, Any] = {}
        self._live_register_clock_ms = MAX_CLOCK_MS
        self._live_register_interval_s = 2.0
        self._live_register_stale_s = 6.0
        self._live_register_cadence_cycles = 20
        self._refresh_live_register_timing_from_config()
        self._live_maintenance_interval_s = self._resolve_live_maintenance_interval_s()
        self._protected_live_lane = ProtectedLiveLaneStatus()
        _LIVE_INPUT_MODULES.add(self)

    def bind_startup_identity_prompt(
        self,
        prompt: Callable[[], str] | None,
    ) -> None:
        bind = getattr(self._modem_backend, "bind_startup_identity_prompt", None)
        if callable(bind):
            bind(prompt)

    def bind_product_lifecycle_generation(
        self,
        lifecycle_generation: int,
    ) -> int:
        generation = int(lifecycle_generation or 0)
        if generation <= 0:
            raise ValueError("modem_live_product_lifecycle_positive_required")
        current = int(self._product_lifecycle_generation or 0)
        if current:
            if current != generation:
                raise RuntimeError(
                    "modem_live_product_lifecycle_rebind_forbidden"
                )
            return current
        if self._live_register_store.sequence() > 0:
            raise RuntimeError(
                "modem_live_lifecycle_bind_after_matrix_forbidden"
            )
        self._product_lifecycle_generation = generation
        return generation

    def _bind_live_register_store_for_session(
        self,
        session_scope_key: str,
    ) -> bool:
        replacement = self.pending_device_replacement()
        if replacement is not None:
            raise replacement
        scope = str(session_scope_key or "").strip()
        lifecycle = int(self._product_lifecycle_generation or 0)
        if not scope:
            raise RuntimeError("modem_live_session_scope_required_before_matrix")
        if lifecycle <= 0:
            raise RuntimeError(
                "modem_live_product_lifecycle_required_before_matrix"
            )
        namespace = RuntimeGenerationNamespace(
            session_scope_key=scope,
            source_epoch=self._publication_source_epoch,
            lifecycle_generation=lifecycle,
        )
        store = self._live_register_store
        rtt_authority = self._ensure_rtt_observation_authority()
        try:
            current = store.runtime_namespace
        except RuntimeError:
            store_plan = store.prepare_runtime_namespace_binding(namespace)
            rtt_plan = rtt_authority.prepare_runtime_namespace_binding(
                namespace
            )
            store_receipt = store.commit_runtime_namespace_binding(store_plan)
            try:
                rtt_authority.commit_runtime_namespace_binding(rtt_plan)
            except Exception:
                store.rollback_runtime_namespace_binding(
                    store_plan,
                    store_receipt,
                )
                raise
            return False
        if current == namespace:
            store_plan = store.prepare_runtime_namespace_binding(namespace)
            rtt_plan = rtt_authority.prepare_runtime_namespace_binding(
                namespace
            )
            store.commit_runtime_namespace_binding(store_plan)
            rtt_authority.commit_runtime_namespace_binding(rtt_plan)
            return False
        with self._device_cache_lock:
            self._pending_device_replacement = (
                current, namespace,
                ProductRuntimeWorkerReplacementReason.FOREIGN_RUNTIME_NAMESPACE,
            )
        raise self.pending_device_replacement()
    def _remember_provider_identity(
        self,
        *,
        provider_plmn: Any = "",
        provider_name: Any = "",
    ) -> tuple[str, str]:
        """Own the public provider pair for this Modem Live session.

        PLMN comes from the modem. Known provider names use the existing operator
        vocabulary, not firmware spelling. Raw names remain in profile evidence;
        Engine, GUI and Intelligence must not resolve them again.
        """
        cache_lock = getattr(self, "_device_cache_lock", None)
        if cache_lock is None:
            cache_lock = threading.RLock()
            self._device_cache_lock = cache_lock
        with cache_lock:
            cached_plmn = str(
                getattr(self, "_cached_provider_plmn", "") or ""
            ).strip()
            cached_name = str(
                getattr(self, "_cached_provider_name", "") or ""
            ).strip()
            if cached_name.lower() == "unknown":
                cached_name = ""
            plmn = str(provider_plmn or "").strip() or cached_plmn
            supplied_name = str(provider_name or "").strip()
            if supplied_name.lower() == "unknown":
                supplied_name = ""
            from arch4.operator_profiles import (
                resolve_provider_name,
                resolve_provider_plmn_from_name,
            )

            name = supplied_name or (cached_name if plmn == cached_plmn else "")
            # A known alias may resolve its name without inventing a measured
            # PLMN. An explicit foreign PLMN must never use a regional alias.
            name_plmn = plmn or resolve_provider_plmn_from_name(name)
            resolved_name = str(resolve_provider_name(name_plmn) or "").strip()
            if resolved_name and resolved_name.lower() != "unknown":
                name = resolved_name
            self._cached_provider_plmn = plmn
            self._cached_provider_name = name
            return plmn, name

    def _ensure_connectivity_module(self) -> ConnectivityModule:
        connectivity = getattr(self, "_connectivity", None)
        if isinstance(connectivity, ConnectivityModule):
            return connectivity
        connectivity = ConnectivityModule(
            reconnect_interval_s=self._modem_reconnect_interval_s(),
            read_miss_operator_min_s=self._modem_read_missed_operator_min_s(),
        )
        self._connectivity = connectivity
        return connectivity

    def _modem_read_missed_operator_min_s(self) -> float:
        raw = getattr(self.cfg, "modem_read_missed_operator_min_s", 20.0)
        value = float(raw)
        if not math.isfinite(value) or value < 0.0:
            raise ValueError("modem_read_missed_operator_min_s_invalid")
        return value

    def _modem_reconnect_interval_s(self) -> float:
        value = float(getattr(self.cfg, "modem_reconnect_interval_s", 15.0))
        if not math.isfinite(value) or value < 5.0:
            raise ValueError("modem_reconnect_interval_s_invalid")
        return value

    def _modem_is_connected_direct(self) -> bool:
        return bool(
            self._call_modem(
                "is_connected",
                source="connectivity",
                operation="is_connected",
                lock_policy="always",
            )
        )

    def _modem_connect_direct(self) -> bool:
        return bool(
            self._call_modem(
                "connect",
                source="connectivity",
                operation="connect",
                lock_policy="always",
            )
        )

    def _coerce_live_acquisition_contract(self) -> LiveAcquisitionContract:
        reader = getattr(
            self._modem_backend,
            "get_live_acquisition_contract",
            None,
        )
        if not callable(reader):
            raise RuntimeError("modem_live_acquisition_contract_reader_required")
        try:
            contract = reader()
        except Exception as exc:
            raise RuntimeError(
                "modem_live_acquisition_contract_read_failed"
            ) from exc
        if not isinstance(contract, LiveAcquisitionContract):
            raise TypeError("modem_live_acquisition_contract_type_invalid")
        return self._apply_live_acquisition_contract_overrides(contract)

    def _apply_live_acquisition_contract_overrides(
        self, contract: LiveAcquisitionContract
    ) -> LiveAcquisitionContract:
        updates: Dict[str, bool] = {}
        for field_name, config_name in LIVE_ACQUISITION_BOOL_OVERRIDES.items():
            value = _optional_config_bool(getattr(self, "cfg", None), config_name)
            if value is not None:
                updates[field_name] = value
        self._live_acquisition_contract_overrides = dict(updates)
        if updates:
            contract = replace(contract, **updates)
        return self._enforce_live_access_hard_rules(contract)

    def _enforce_live_access_hard_rules(
        self, contract: LiveAcquisitionContract
    ) -> LiveAcquisitionContract:
        access_profile = self._live_access_profile()
        backend = str(
            getattr(access_profile, "backend", "")
            or getattr(getattr(self, "cfg", None), "modem_backend", "")
            or ""
        ).strip()
        family = str(getattr(access_profile, "backend_family", "") or "").strip()
        access_type = str(getattr(access_profile, "access_type", "") or "").strip()
        try:
            from arch4.operator_profiles import infer_backend_family

            family = family or infer_backend_family(backend)
        except Exception:
            family = family or ""
        contract, hard_rules, clamps = enforce_live_access_hard_rules(
            contract,
            backend=backend,
            backend_family=family,
            access_type=access_type,
        )
        self._live_acquisition_contract_hard_rules = dict(hard_rules or {})
        self._live_acquisition_contract_hard_rule_clamps = dict(clamps or {})
        return contract

    def _refresh_live_acquisition_contract_from_config(self) -> LiveAcquisitionContract:
        contract = self._coerce_live_acquisition_contract()
        self._live_acquisition_contract = contract
        return contract

    def _live_access_profile(self):
        backend = str(getattr(self.cfg, "modem_backend", "") or "")
        family = ""
        try:
            from arch4.operator_profiles import infer_backend_family

            family = infer_backend_family(backend)
        except Exception:
            family = ""
        card = getattr(self, "_last_resolved_identity_card", {})
        if not isinstance(card, dict):
            card = {}
        identity = getattr(self, "_last_runtime_identity", {})
        if not isinstance(identity, dict):
            identity = {}
        return resolve_modem_live_access_profile(
            backend=backend,
            backend_family=family,
            profile_id=str(card.get("profile_id") or identity.get("resolved_profile_id")
                           or self.input_cfg.modem_profile_id),
            model=str(card.get("model") or identity.get("model") or ""),
            firmware=str(card.get("firmware") or identity.get("firmware") or ""),
            vendor=str(card.get("vendor") or identity.get("vendor") or ""),
        )

    def modem_live_access_profile(self):
        """Return the immutable access profile classified by Modem Live."""

        return self._live_access_profile()

    def identity_decode_catalog_snapshot(self) -> Dict[str, Any]:
        """Publish the immutable decode catalog for read replicas."""

        return self._identity_matrix.catalog_snapshot.as_dict()

    def _resolve_live_register_timing(self) -> tuple[float, float, int]:
        contract = getattr(self, "_live_acquisition_contract", LiveAcquisitionContract())
        access_profile = self._live_access_profile()
        return resolve_live_register_timing(
            contract=contract,
            access_profile=access_profile,
            cfg_interval=getattr(self.input_cfg, "modem_live_register_interval_s", 2.0),
            cfg_stale=getattr(self.input_cfg, "modem_live_register_stale_s", 6.0),
            default_interval=_arch4_config_default_float("modem_live_register_interval_s", 2.0),
            default_stale=_arch4_config_default_float("modem_live_register_stale_s", 6.0),
            clock_ms=int(self._live_register_clock_ms),
        )

    def _resolve_live_maintenance_interval_s(self) -> float:
        initialized = hasattr(self, "cfg") and hasattr(self, "input_cfg")
        access_profile = self._live_access_profile() if initialized else None
        interval_s, source = resolve_live_maintenance_interval(
            initialized=initialized,
            current_interval_s=getattr(self, "_live_maintenance_interval_s", 10.0),
            cfg_interval=getattr(getattr(self, "input_cfg", None), "live_maintenance_interval_s", 10.0),
            default_interval=_arch4_config_default_float("live_maintenance_interval_s", 10.0),
            access_profile=access_profile,
        )
        self._live_maintenance_interval_s = interval_s
        self._live_maintenance_interval_source = source
        return interval_s

    def _refresh_live_register_timing_from_config(self, *, restart_worker: bool = False) -> None:
        interval_s, stale_s, cadence_cycles = self._resolve_live_register_timing()
        changed = (
            abs(float(getattr(self, "_live_register_interval_s", 0.0) or 0.0) - interval_s) > 1e-9
            or abs(float(getattr(self, "_live_register_stale_s", 0.0) or 0.0) - stale_s) > 1e-9
            or int(getattr(self, "_live_register_cadence_cycles", 0) or 0) != cadence_cycles
        )
        self._live_register_interval_s = interval_s
        self._live_register_stale_s = stale_s
        self._live_register_cadence_cycles = cadence_cycles
        if restart_worker and changed:
            self._restart_live_register_clock()

    def _restart_live_register_clock(self) -> None:
        if (
            not self._clocked_live_register_enabled()
            or self._modem_live_shutdown.is_set()
        ):
            return
        current = threading.current_thread()
        with self._live_register_lock:
            thread = self._live_register_thread
            running = bool(thread is not None and thread.is_alive())
            if thread is current:
                return
            if running:
                stop_event = self._live_register_worker_stop
                if not isinstance(stop_event, threading.Event):
                    raise RuntimeError(
                        "live_register_worker_stop_identity_missing"
                    )
                self._live_register_restart_requested = True
                stop_event.set()
        if running and thread is not None:
            thread.join(timeout=0.5)
        with self._live_register_lock:
            if thread is not None and thread.is_alive():
                self._live_register_last_reason = (
                    "restart_deferred_worker_still_alive"
                )
                return
            if self._live_register_thread is thread:
                self._live_register_thread = None
                self._live_register_worker_stop = None
        self._start_live_register_clock()

    def set_runtime_clock_live_register_observer(self, enabled: bool) -> None:
        # Runtime clock may observe register health, but Modem Live controls
        # modem I/O timing for every profile.
        self._runtime_clock_observes_live_register = bool(enabled)
        if enabled:
            self._start_rtt_cadence_worker()
            self._start_live_register_clock()

    def set_live_register_publication_sink(
        self,
        sink: Optional[ModemLivePublicationSink],
    ) -> None:
        """Bind the runtime publication sink. It cannot be replaced."""

        if sink is not None and not callable(sink):
            raise TypeError("Modem Live publication sink must be callable")
        notice: ModemLivePublicationNotice | None = None
        with self._live_register_lock:
            current = self._live_register_publication_sink
            if current is not None and sink is not None and current is not sink:
                raise RuntimeError("Modem Live publication sink is already bound")
            self._live_register_publication_sink = sink
            if sink is not None:
                self._live_register_publication_closed = False
            if sink is not None:
                sequence = self._live_register_store.sequence()
                if sequence > 0:
                    committed_generation = (
                        self._live_register_store.committed_generation()
                    )
                    matrix = committed_generation.matrix_reader()
                    matrix_sampled_at = matrix.metadata("sampled_at")
                    matrix_source = matrix.metadata("source")
                    published_at = max(
                        float(matrix_sampled_at),
                        float(time.time()),
                    )
                    if (
                        type(sequence) is not int
                        or type(published_at) is not float
                        or type(matrix_sampled_at) is not float
                        or type(matrix_source) is not str
                    ):
                        raise TypeError(
                            "modem_live_committed_publication_contract_invalid"
                        )
                    notice = ModemLivePublicationNotice(
                        matrix_identity=committed_generation.identity,
                        matrix_rtt=self._matrix_bound_rtt_observation(
                            matrix_identity=committed_generation.identity,
                            bound_at=published_at,
                        ),
                        sampled_at=matrix_sampled_at,
                        published_at=published_at,
                        source=matrix_source,
                        truth_matrix=matrix,
                    )
        if notice is not None:
            self._emit_live_register_publication(notice)

    def set_current_health_publication_sink(
        self,
        sink: Callable[
            [ModemLiveCurrentHealthPublication],
            ModemLiveCurrentHealthAcceptanceReceipt,
        ],
    ) -> None:
        if not callable(sink):
            raise TypeError(
                "modem_live_current_health_sink_callable_required"
            )
        with self._live_register_lock:
            current = self._current_health_publication_sink
            if current is not None and current is not sink:
                raise RuntimeError(
                    "modem_live_current_health_sink_already_bound"
                )
            self._current_health_publication_sink = sink

    def detach_current_health_publication_sink(
        self,
        sink: Callable[
            [ModemLiveCurrentHealthPublication],
            ModemLiveCurrentHealthAcceptanceReceipt,
        ],
        *,
        timeout_s: float = 1.0,
    ) -> ModemLiveCurrentHealthSinkDetachReceipt:
        if not callable(sink):
            raise TypeError(
                "modem_live_current_health_sink_callable_required"
            )
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._live_register_publication_changed:
            current = self._current_health_publication_sink
            if current is not None and current != sink:
                raise RuntimeError(
                    "modem_live_current_health_sink_identity_mismatch"
                )
            self._current_health_publication_sink = None
            while self._current_health_publication_inflight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                self._live_register_publication_changed.wait(remaining)
            inflight = self._current_health_publication_inflight
            namespace = (
                self._live_register_store.runtime_namespace
                if self._live_register_store.sequence() > 0
                else None
            )
        return ModemLiveCurrentHealthSinkDetachReceipt(
            namespace=namespace,
            inflight_callbacks=inflight,
            detached=inflight == 0,
            finished_at=time.time(),
        )

    def detach_live_register_publication_sink(
        self,
        sink: ModemLivePublicationSink,
        *,
        timeout_s: float,
    ) -> ModemLivePublicationSinkDetachReceipt:
        """Detach the matching sink after all callbacks release it."""

        if not callable(sink):
            raise TypeError("Modem Live publication sink must be callable")
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._live_register_publication_changed:
            current = self._live_register_publication_sink
            if current is not None and current != sink:
                raise RuntimeError(
                    "Modem Live publication sink identity mismatch"
                )
            self._live_register_publication_sink = None
            self._live_register_publication_closed = True
            while self._live_register_publication_inflight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                self._live_register_publication_changed.wait(remaining)
            inflight = self._live_register_publication_inflight
            retained = self._live_register_pending_publication_notice
            namespace = (
                self._live_register_store.runtime_namespace
                if self._live_register_store.sequence() > 0
                else None
            )
        return ModemLivePublicationSinkDetachReceipt(
            namespace=namespace,
            detached=inflight == 0 and retained is None,
            inflight_callbacks=inflight,
            retained_notice_identity=(
                retained.matrix_identity
                if retained is not None
                else None
            ),
            finished_at=float(time.time()),
        )

    def _emit_live_register_publication(
        self,
        notice: ModemLivePublicationNotice,
    ) -> None:
        with self._live_register_publication_changed:
            if self._live_register_publication_closed:
                return
            if (
                not bool(self._startup_publication_admission_open)
                and self._startup_publication_notice is None
            ):
                self._startup_publication_notice = notice
            pending = self._live_register_pending_publication_notice
            if (
                pending is None
                or pending.source_epoch != notice.source_epoch
                or notice.sequence >= pending.sequence
            ):
                self._live_register_pending_publication_notice = notice
            sink = self._live_register_publication_sink
            if (
                sink is None
                or not bool(self._startup_publication_admission_open)
            ):
                return
            self._live_register_publication_inflight += 1
        try:
            receipt = sink(notice)
            if (
                not isinstance(
                    receipt,
                    ModemLivePublicationAdmissionReceipt,
                )
                or receipt.matrix_identity != notice.matrix_identity
            ):
                raise RuntimeError(
                    "Modem Live publication admission receipt mismatch"
                )
            with self._live_register_lock:
                self._live_register_publication_sink_error = ""
                pending = self._live_register_pending_publication_notice
                if (
                    pending is not None
                    and pending.source_epoch == notice.source_epoch
                    and pending.sequence == notice.sequence
                    and pending.matrix_revision == notice.matrix_revision
                ):
                    self._live_register_pending_publication_notice = None
        except Exception as exc:
            with self._live_register_lock:
                self._live_register_publication_sink_error = (
                    f"{type(exc).__name__}: {exc}"
                )
            log.exception(
                "Modem Live publication sink failed for sequence %s",
                notice.sequence,
            )
        finally:
            with self._live_register_publication_changed:
                self._live_register_publication_inflight -= 1
                self._live_register_publication_changed.notify_all()

    def _retry_pending_live_register_publication(self) -> None:
        with self._live_register_publication_changed:
            notice = self._live_register_pending_publication_notice
            sink = self._live_register_publication_sink
            inflight = self._live_register_publication_inflight
        if (
            notice is not None
            and sink is not None
            and inflight == 0
            and bool(self._startup_publication_admission_open)
        ):
            self._emit_live_register_publication(notice)

    def release_startup_publication(
        self,
        receipt: ModemLiveReadyReceipt,
    ) -> ModemLivePublicationNotice:
        """Publish the Matrix generation completed by Modem Live."""

        if not isinstance(receipt, ModemLiveReadyReceipt):
            raise TypeError("modem_live_ready_receipt_required")
        if not receipt.ready:
            raise RuntimeError("modem_live_startup_not_ready_for_publication")
        with self._live_register_publication_changed:
            notice = self._startup_publication_notice
            if notice is None:
                raise RuntimeError(
                    "modem_live_startup_publication_notice_missing"
                )
            if notice.matrix_identity != receipt.matrix_identity:
                raise RuntimeError(
                    "modem_live_startup_publication_identity_mismatch"
                )
            self._startup_publication_admission_open = True
            self._startup_publication_notice = None
        self._emit_live_register_publication(notice)
        return notice

    def supervise_live_register_clock(self) -> Dict[str, Any]:
        raise RuntimeError(
            "direct_modem_live_supervision_illegal:"
            "use_runtime_supervision_port"
        )

    def _runtime_supervision_schedule(
        self,
    ) -> ModemLiveRuntimeSupervisionSchedule:
        timeout_s = float(
            self.input_cfg.actionable_modem_read_timeout_s
        )
        return ModemLiveRuntimeSupervisionSchedule(
            enabled=self._clocked_live_register_enabled(),
            cadence_cycles=max(
                1,
                int(self._live_register_cadence_cycles),
            ),
            budget_ms=max(1.0, timeout_s * 1000.0),
        )

    def _run_runtime_supervision(
        self,
    ) -> ModemLiveRuntimeSupervisionReceipt:
        """Run Modem Live supervision and publish typed health state."""

        status = self._supervise_live_register_clock()
        with self._live_register_publication_changed:
            sink = self._current_health_publication_sink
            if sink is None:
                raise RuntimeError(
                    "modem_live_current_health_publication_sink_unbound"
                )
            self._current_health_publication_inflight += 1
        try:
            namespace = self._live_register_store.runtime_namespace
            observed_at = float(time.time())
            publication = ModemLiveCurrentHealthPublication.capture(
                namespace=namespace,
                observed_at=observed_at,
                modem_health=dict(self.get_modem_health_status() or {}),
                modem_live_fast_lane=status,
            )
            receipt = sink(publication)
            if (
                not isinstance(
                    receipt,
                    ModemLiveCurrentHealthAcceptanceReceipt,
                )
                or receipt.namespace != namespace
                or receipt.observed_at != observed_at
            ):
                raise RuntimeError(
                    "modem_live_current_health_publication_receipt_mismatch"
                )
        finally:
            with self._live_register_publication_changed:
                self._current_health_publication_inflight -= 1
                self._live_register_publication_changed.notify_all()
        return ModemLiveRuntimeSupervisionReceipt(
            namespace=namespace,
            observed_at=observed_at,
            accepted=True,
        )

    def _supervise_live_register_clock(self) -> Dict[str, Any]:
        """Recover the worker and update Modem Live internal status."""

        self._retry_pending_live_register_publication()
        self._start_rtt_cadence_worker()
        now = time.time()
        status = self._live_register_status(now=now)
        if not bool(status.get("enabled", False)):
            return status
        worker_alive = bool(status.get("worker_alive", False))
        refreshing = bool(status.get("worker_refreshing", False))
        stale = bool(status.get("stale", False))
        stale_after_s = max(0.0, float(status.get("stale_after_s", 0.0) or 0.0))
        worker_attempt_age_s = max(
            0.0,
            float(status.get("worker_attempt_age_s", 0.0) or 0.0),
        )
        worker_last_finished_ts = max(
            0.0,
            float(status.get("worker_last_finished_ts", 0.0) or 0.0),
        )
        attempt_hard_limit_s = max(
            60.0,
            stale_after_s * 4.0,
            float(self.input_cfg.actionable_modem_read_timeout_s) * 8.0,
            float(self._live_register_interval_s) * 4.0,
        )
        status["worker_attempt_hard_limit_s"] = attempt_hard_limit_s

        restart_reason = ""
        if not worker_alive:
            restart_reason = "worker_not_alive"
        elif (
            stale
            and refreshing
            and worker_attempt_age_s > attempt_hard_limit_s
        ):
            restart_reason = "worker_attempt_over_hard_limit"
        elif stale and not refreshing:
            last_progress_ts = max(
                worker_last_finished_ts,
                float(status.get("last_attempt_ts", 0.0) or 0.0),
            )
            if (
                last_progress_ts <= 0.0
                or now - last_progress_ts > attempt_hard_limit_s
            ):
                restart_reason = "worker_idle_over_hard_limit"

        if restart_reason:
            self._restart_live_register_clock()
            status = self._live_register_status(now=time.time())
            status["worker_attempt_hard_limit_s"] = attempt_hard_limit_s
            status["supervisor_action"] = (
                "restart_deferred"
                if bool(status.get("worker_alive", False))
                and bool(status.get("restart_requested", False))
                else "restart_requested"
            )
            status["supervisor_reason"] = restart_reason
            return status

        status["supervisor_action"] = "observe"
        status["supervisor_reason"] = ""
        return status

    def _backend_io_context(self, *, force_lock: bool = False):
        contract = self._refresh_live_acquisition_contract_from_config()
        serial_required = (
            contract.effective_io_serial_required()
            if callable(getattr(contract, "effective_io_serial_required", None))
            else not bool(getattr(contract, "backend_io_parallel_safe", False))
        )
        if bool(force_lock) or bool(serial_required):
            return self._ensure_io_lock()
        return nullcontext()

    def _runtime_action_io_context(self):
        contract = self._refresh_live_acquisition_contract_from_config()
        action_requires_lock = (
            contract.effective_runtime_action_requires_io_lock()
            if callable(getattr(contract, "effective_runtime_action_requires_io_lock", None))
            else True
        )
        if bool(action_requires_lock):
            return self._ensure_io_lock()
        return nullcontext()

    def _ensure_io_lock(self):
        lock = getattr(self, "_io_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._io_lock = lock
        return lock

    def _modem_io_context_for_policy(self, policy: str):
        policy = str(policy or "backend").strip().lower()
        if policy == "none":
            raise RuntimeError("modem_live_unlocked_physical_io_forbidden")
        if policy == "always":
            return self._ensure_io_lock(), True
        if policy == "action":
            contract = self._refresh_live_acquisition_contract_from_config()
            locked = bool(contract.effective_runtime_action_requires_io_lock())
            return self._runtime_action_io_context(), locked
        if policy == "sms":
            return self._sms_modem_io_context(), True
        if policy == "backend_force":
            return self._backend_io_context(force_lock=True), True
        if policy == "rtt":
            locked = bool(getattr(self._live_acquisition_contract, "rtt_holds_io_lock", False))
            return (self._ensure_io_lock() if locked else nullcontext()), locked
        if policy != "backend":
            raise ValueError(
                f"modem_live_physical_io_policy_unclassified:{policy or '<empty>'}"
            )
        contract = self._refresh_live_acquisition_contract_from_config()
        locked = bool(contract.effective_io_serial_required())
        return self._backend_io_context(), locked

    @contextmanager
    def _sms_modem_io_context(self):
        """Allow SMS only when no Core action uses or waits for modem I/O."""

        condition = self._event_write_pending_lock
        with condition:
            while self._event_write_pending_tokens:
                condition.wait()
            # Take the I/O lock while still holding the pending-state condition.
            # Core either marks its action first or waits behind this SMS call.
            # This avoids a race between the check and lock acquisition.
            io_lock = self._ensure_io_lock()
            io_lock.acquire()
        try:
            yield
        finally:
            io_lock.release()

    def _record_modem_io_event(
        self,
        *,
        source: str,
        operation: str,
        lock_policy: str,
        locked: bool,
        started_ts: float,
        success: bool,
        call_completed: bool = False,
        result_present: bool = False,
        result_type: str = "",
        error: Optional[BaseException] = None,
        caller_id: str = "",
        lane: str = "",
    ) -> None:
        self._ensure_modem_io_tracking()
        self._modem_io_gate.record(
            source=source,
            operation=operation,
            caller_id=caller_id or source,
            lane=lane or self._access_lane_for(source=source, operation=operation),
            lock_policy=lock_policy,
            locked=locked,
            started_ts=started_ts,
            success=success,
            call_completed=call_completed,
            result_present=result_present,
            result_type=result_type,
            error=error,
        )

    @staticmethod
    def _modem_io_semantic_success(*, source: str, operation: str, result: Any) -> bool:
        source_key = str(source or "").strip().lower()
        operation_key = str(operation or "").strip().lower()
        if isinstance(result, BackendAcquisitionReceipt):
            return result.succeeded
        if isinstance(result, ModemLivePhysicalStageResult):
            return bool(result.permits_target_verification)
        if operation_key in {"prepare_radio_mode", "radio_mode_interstage_readback"}:
            # False is a completed native read: no disconnect needed, or not
            # settled yet. It is not an unclassified/failed I/O operation.
            return type(result) is bool
        if operation_key == "manual_nr_mode_preference":
            # A qualified option may be absent; keep the user's band-only
            # request in that case rather than inventing a mode preference.
            return result is None or (type(result) is str and bool(result))
        if operation_key in {
            "read_actionable_modem_input",
            "read_modem_input",
            "acquire_actionable_modem_input",
            "acquire_post_write_modem_input",
            "acquire_live_maintenance",
            "iter_acquire_live_maintenance",
            "iter_acquire_network_settings",
            "iter_acquire_read_diagnostics",
            "acquire_live_maintenance_step",
            "acquire_neighbors",
            "read_band_priority",
            "get_capabilities",
            "verify_static_config",
            "verify_ue_capability",
            "read_plmn",
            "read_inbox",
            "read_messages",
            "get_last_status",
            "health_proof",
            "script_status",
            "measure_rtt",
            "secondary_ping",
            "verify_internet_reachability",
        }:
            return result is not None
        if operation_key in {
            "is_connected",
            "connect",
            "recover_session",
            "send",
            "delete",
            "send_message",
            "delete_message",
        }:
            return bool(result)
        if operation_key in {"disconnect", "close_live_maintenance_steps"}:
            return True
        if operation_key.startswith("execute_"):
            raise TypeError("modem_live_physical_stage_result_required")
        raise RuntimeError(
            "modem_live_io_semantic_operation_unclassified:"
            f"{source_key or '<empty>'}:{operation_key or '<empty>'}"
        )

    def modem_io_status(self) -> Dict[str, Any]:
        self._ensure_modem_io_tracking()
        return self._modem_io_gate.status()

    def _ensure_access_scheduler(self) -> ModemLiveAccessScheduler:
        scheduler = getattr(self, "_access_scheduler", None)
        if isinstance(scheduler, ModemLiveAccessScheduler):
            return scheduler
        scheduler = ModemLiveAccessScheduler()
        self._access_scheduler = scheduler
        return scheduler

    def _access_lane_for(self, *, source: str, operation: str = "") -> str:
        return self._ensure_access_scheduler().classify(source=source, operation=operation)

    def access_scheduler_status(self) -> Dict[str, Any]:
        return self._ensure_access_scheduler().status()

    def _event_write_token_for_action(self, action: Any, *, source: str) -> str:
        # One Core command keeps the same lease while queued, written, and given
        # a receipt. Modem Live rejects execution without the retained token.
        token = f"{str(source or 'event_write')}:{time.time_ns()}:{threading.get_ident()}"
        try:
            setattr(action, "_modem_live_event_write_token", token)
        except Exception:
            pass
        return token

    def note_event_write_pending(self, action: Any = None, *, source: str = "runtime_action") -> Dict[str, Any]:
        """Mark a write as queued before it reaches the I/O lock.

        This does not interrupt an active read. It prevents a new fast read from
        starting before the queued write.
        """

        token = self._event_write_token_for_action(action, source=source)
        now = time.time()
        action_name = (
            action.lever.name
            if isinstance(action, LeverAction) and getattr(action, "lever", None) is not None
            else str(
                getattr(getattr(action, "lever", None), "name", "")
                or getattr(action, "lever", "")
                or "UNKNOWN"
            )
        )
        with self._event_write_pending_lock:
            first = not self._event_write_pending_tokens
            self._event_write_pending_tokens.add(token)
            if first or self._event_write_pending_since_ts <= 0.0:
                self._event_write_pending_since_ts = now
            self._event_write_pending_last = {
                "token": token,
                "source": str(source or "runtime_action"),
                "lever": str(action_name),
                "ts": now,
            }
            count = len(self._event_write_pending_tokens)
            since = float(self._event_write_pending_since_ts or now)
        return {
            "active": True,
            "token": token,
            "pending_count": int(count),
            "pending_since_ts": since,
            "age_s": max(0.0, now - since),
            "owner": "modem_live",
        }

    def clear_event_write_pending(self, action: Any = None, *, token: str = "", source: str = "runtime_action") -> Dict[str, Any]:
        tok = str(token or getattr(action, "_modem_live_event_write_token", "") or "").strip()
        if not tok:
            raise ValueError("modem_live_event_write_token_required")
        now = time.time()
        with self._event_write_pending_lock:
            if tok not in self._event_write_pending_tokens:
                raise RuntimeError("modem_live_event_write_token_not_retained")
            self._event_write_pending_tokens.discard(tok)
            if not self._event_write_pending_tokens:
                self._event_write_pending_since_ts = 0.0
                self._event_write_pending_lock.notify_all()
            count = len(self._event_write_pending_tokens)
            since = float(self._event_write_pending_since_ts or 0.0)
            last = dict(self._event_write_pending_last or {})
        return {
            "active": bool(count),
            "token": tok,
            "pending_count": int(count),
            "pending_since_ts": since,
            "age_s": max(0.0, now - since) if since > 0.0 else 0.0,
            "last": last,
            "source": str(source or "runtime_action"),
            "owner": "modem_live",
        }

    def event_write_pending_status(self) -> Dict[str, Any]:
        now = time.time()
        with self._event_write_pending_lock:
            count = len(self._event_write_pending_tokens)
            since = float(self._event_write_pending_since_ts or 0.0)
            last = dict(self._event_write_pending_last or {})
        return {
            "active": bool(count),
            "pending_count": int(count),
            "pending_since_ts": since,
            "age_s": max(0.0, now - since) if since > 0.0 else 0.0,
            "last": last,
            "owner": "modem_live",
        }

    def _event_write_pending_active(self) -> bool:
        lock = getattr(self, "_event_write_pending_lock", None)
        if lock is None:
            return False
        with lock:
            return bool(self._event_write_pending_tokens)

    def _require_event_write_token(self, token: str) -> str:
        retained = str(token or "").strip()
        if not retained:
            raise RuntimeError("modem_live_event_write_token_required")
        with self._event_write_pending_lock:
            if retained not in self._event_write_pending_tokens:
                raise RuntimeError("modem_live_event_write_token_not_retained")
        return retained

    def _ensure_modem_io_tracking(self) -> None:
        if getattr(self, "_modem_io_gate", None) is None:
            self._modem_io_gate = ModemIoGate()

    def _modem_io(
        self,
        *,
        source: str,
        operation: str,
        caller_id: str = "",
        lane: str = "",
        fn: Callable[[], Any],
        lock_policy: str = "backend",
    ) -> Any:
        self._ensure_modem_io_tracking()
        scheduler = self._ensure_access_scheduler()
        access_lane = lane or scheduler.classify(source=source, operation=operation)
        lane_scope = getattr(
            self._modem_backend,
            "physical_request_lane_scope",
            None,
        )
        if bool(
            getattr(
                self._modem_backend,
                "physical_request_authority_required",
                False,
            )
        ) and not callable(lane_scope):
            raise RuntimeError(
                "modem_live_physical_request_authority_unavailable"
            )

        def invoke() -> Any:
            from arch4.privacy_scrub import private_field_alias_scope
            trace = getattr(self, "_startup_wire_trace", None)
            sink = getattr(self, "_wire_trace_sink", None)
            privacy_reader=getattr(self._modem_backend,'native_diagnostic_privacy',None)
            privacy=privacy_reader()if callable(privacy_reader)else{'keys':(),'opaque':False}
            context = (trace.scope(sink, access_lane, operation, private_keys=privacy['keys'], opaque_native=privacy['opaque'])
                       if trace is not None and callable(sink) else
                       private_field_alias_scope(privacy['keys'],omit_native_payload=privacy['opaque']))
            with context:
                if not callable(lane_scope):
                    return self._invoke_identity_checked(operation, fn, host_probe=lock_policy == "rtt")
                with lane_scope(access_lane):
                    return self._invoke_identity_checked(operation, fn, host_probe=lock_policy == "rtt")

        return self._modem_io_gate.call(
            source=source,
            operation=operation,
            caller_id=caller_id or source,
            lane=access_lane,
            endpoint_class=scheduler.endpoint_class(source=source, operation=operation),
            priority=scheduler.priority_for(access_lane),
            cooldown_s=self._access_cooldown_s_for(access_lane),
            fn=invoke,
            context_factory=self._modem_io_context_for_policy,
            semantic_success=self._modem_io_semantic_success,
            fault_recorder=self._record_backend_fault,
            lock_policy=lock_policy,
        )

    def pending_device_replacement(self) -> ProductRuntimeWorkerReplacementRequired | None:
        """One worker-retirement request, including failed automatic login."""
        with self._device_cache_lock:
            pending = self._pending_device_replacement
        if pending is None:
            return None
        return ProductRuntimeWorkerReplacementRequired(
            current_namespace=pending[0],
            requested_namespace=pending[1],
            reason=pending[2],
        )

    def sms_identity_ready(self, *, namespace: RuntimeGenerationNamespace) -> bool:
        """Pure SMS admission view of the existing device-confirmation owner.

        The in-lock identity guard still protects a readiness-to-read race.
        This query performs no native read and creates no second identity cache.
        """
        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError("sms_runtime_namespace_required")
        if not self._active_device_identity or self._reconnection_identity_pending:
            return False
        if self.pending_device_replacement() is not None:
            return False
        if (self._event_write_pending_active()
                or self.expected_interruption_status_payload()["active"]):
            return False
        return self._live_register_store.runtime_namespace == namespace

    def _invoke_identity_checked(
        self, operation: str, fn: Callable[[], Any], *, host_probe: bool = False,
    ) -> Any:
        """Runs inside the existing I/O lock, including work already queued."""
        replacement = self.pending_device_replacement()
        if replacement is not None and operation != "disconnect":
            raise replacement
        if self._reconnection_identity_pending and not host_probe and operation not in {
            "connect", "disconnect", "is_connected", "recover_session",
        }:
            identity_request = self._new_backend_acquisition_request(
                lane="startup_full", source="session_identity")
            raw_identity = self._modem_backend.read_fresh_device_identity()
            identity_finished_at = time.time()
            fresh = {
                key: self._permanent_identity_text(raw_identity.get(key))
                for key in ("serial", "imei")
            }
            active = dict(self._active_device_identity)
            comparable = tuple(
                key for key in ("serial", "imei")
                if str(active.get(key) or "").strip()
                and fresh[key]
            )
            if not comparable:
                raise ConnectionError("reconnection_permanent_identity_not_confirmed")
            self._cutover_device_cache_if_required(
                modem=None,
                base_status={"backend": active["backend"], **fresh},
            )
            metadata = {key: value for key, value in raw_identity.items()
                        if self._identity_value_present(value)}
            prior = dict(getattr(self, "_cached_modem_info", {}) or {})
            changed = any(prior.get(key) and metadata.get(key)
                          and str(prior[key]) != str(metadata[key])
                          for key in ("software_version", "firmware", "webui", "config_version"))
            self._merge_modem_info_cache(metadata)
            identity_fields = frozenset(metadata).intersection(
                MODEM_LIVE_TRUTH_FIELD_CATALOG["identity"])
            self._latch_acquisition_receipts((BackendAcquisitionReceipt(
                request=identity_request,
                backend=active["backend"],
                profile_id=str(self._live_acquisition_contract.profile_name
                               or type(self._modem_backend).__name__),
                finished_at=identity_finished_at, succeeded=True, modem=None,
                status={field: metadata[field] for field in identity_fields},
                acquired_cells=frozenset(f"identity.{field}" for field in identity_fields),
            ),))
            with self._live_register_lock:
                if changed:
                    # Physical identity was confirmed above. Drop the old derived
                    # capability projection, not the device namespace or its data.
                    self._cached_runtime_capabilities = {}
                    self._session_band_capability_status = {}
                    self._session_band_capability_reported_id = ""
            self._reconnection_identity_pending = False
        result = fn()
        if (
            operation.startswith("acquire_")
            and isinstance(result, BackendAcquisitionReceipt)
            and result.request.lane == "fast_live"
            and not result.succeeded
            and self._active_device_identity
        ):
            self._reconnection_identity_pending = True
        return result

    def _call_modem(
        self,
        method_name: str,
        *args: Any,
        source: str,
        caller_id: str = "",
        lane: str = "",
        operation: str = "",
        lock_policy: str = "backend",
        **kwargs: Any,
    ) -> Any:
        method = getattr(
            self._modem_backend,
            str(method_name or ""),
            None,
        )
        if not callable(method):
            raise RuntimeError(
                f"modem_live_backend_method_required:{str(method_name or '<empty>')}"
            )
        op = str(operation or method_name or "unknown")
        from arch4.modem.backend_diagnostic import native_read_diagnostic_scope
        request = next((arg for arg in args if isinstance(arg, BackendAcquisitionRequest)), None)
        with native_read_diagnostic_scope(
            getattr(self, "_native_read_event_sink", None),
            physical_attempt_id=request.attempt_id if request is not None else "",
            source_epoch=getattr(self, "_publication_source_epoch", ""),
            profile_id=str(getattr(getattr(self, "cfg", None), "modem_profile_id", "") or ""),
            operation=op, source=source,
        ):
            return self._modem_io(
                source=source,
                operation=op,
                caller_id=caller_id or source,
                lane=lane or self._access_lane_for(source=source, operation=op),
                lock_policy=lock_policy,
                fn=lambda: method(*args, **kwargs),
            )

    def _record_backend_fault(self, stage: str, exc: Exception) -> None:
        self._ensure_backend_fault_store().record(stage, exc)

    def backend_fault_health(self) -> Dict[str, Any]:
        return self._ensure_backend_fault_store().health()

    def _sync_live_lane_status_with_register(self, status: Dict[str, Any]) -> Dict[str, Any]:
        if not hasattr(self, "_live_register_lock"):
            return status
        try:
            with self._live_register_lock:
                current = self._live_register_store.current_status()
                sequence = int(current.sequence)
                updated_at = float(current.sampled_at)
        except Exception as exc:
            self._record_backend_fault("live_lane_status_sync", exc)
            return status
        if sequence <= 0:
            return status
        current_count = int(status.get("sample_count", 0) or 0)
        if sequence <= current_count:
            return status
        status["sample_count"] = sequence
        status["sample_count_source"] = "live_register_sequence"
        if updated_at > 0.0:
            status["last_success_ts"] = max(float(status.get("last_success_ts", 0.0) or 0.0), updated_at)
            status["last_finish_ts"] = max(float(status.get("last_finish_ts", 0.0) or 0.0), updated_at)
        scheduler = status.get("access_scheduler")
        if isinstance(scheduler, dict):
            lanes = scheduler.get("lanes")
            if isinstance(lanes, dict):
                fast_lane = lanes.get("fast_live")
                if isinstance(fast_lane, dict):
                    fast_lane["run_count"] = max(int(fast_lane.get("run_count", 0) or 0), sequence)
                    fast_lane["last_decision"] = str(fast_lane.get("last_decision") or "run")
                    fast_lane["last_reason"] = str(fast_lane.get("last_reason") or "sample_sequence_sync")
                    if updated_at > 0.0:
                        fast_lane["last_ts"] = max(float(fast_lane.get("last_ts", 0.0) or 0.0), updated_at)
                        fast_lane["last_run_ts"] = max(float(fast_lane.get("last_run_ts", 0.0) or 0.0), updated_at)
                    fast_lane["sample_count_source"] = "live_register_sequence"
            scheduler["sample_count_source"] = "live_register_sequence"
        return status

    def protected_live_lane_status(self) -> Dict[str, Any]:
        status = dict(getattr(self, "_protected_live_lane_status", {}) or {})
        if not status:
            status = {
                "owner": "modem_live",
                "lane": "fast_modem_sample",
                "period_s": 0.0,
                "work_ms": 0.0,
                "last_start_ts": 0.0,
                "sample_count": 0,
                "missed_count": 0,
                "maintenance_inline": False,
                "maintenance_skipped_count": 0,
                "last_maintenance_skip_reason": "",
                "source": "modem_live",
            }
        status["maintenance_interval_s"] = round(float(self._resolve_live_maintenance_interval_s()), 3)
        status["maintenance_interval_source"] = str(
            getattr(self, "_live_maintenance_interval_source", "") or ""
        )
        status["maintenance_requested"] = bool(getattr(self, "_cached_maintenance_requested", False))
        status["maintenance_requested_ts"] = float(getattr(self, "_cached_maintenance_requested_ts", 0.0) or 0.0)
        status["maintenance_requested_source"] = str(
            getattr(self, "_cached_maintenance_requested_source", "") or "unknown"
        )
        status["register_read"] = self._live_register_status(now=time.time())
        status["rtt_cadence"] = self._rtt_cadence_status()
        status["modem_io"] = self.modem_io_status()
        status["access_scheduler"] = self.access_scheduler_status()
        physical_status = getattr(
            self._modem_backend,
            "physical_request_authority_status",
            None,
        )
        if callable(physical_status):
            status["physical_request_authority"] = physical_status()
        lane_state = getattr(self, "_acquisition_lane_state", None)
        if not isinstance(lane_state, AcquisitionLaneStateAuthority):
            lane_state = AcquisitionLaneStateAuthority()
            self._acquisition_lane_state = lane_state
        status["acquisition_lane_state"] = lane_state.snapshot()
        status = self._sync_live_lane_status_with_register(status)
        return status

    def _publish_cached_maintenance_requested(
        self,
        value: bool,
        *,
        source: str,
        observed_ts: Optional[float] = None,
    ) -> None:
        self._cached_maintenance_requested = bool(value)
        self._cached_maintenance_requested_ts = float(observed_ts or time.time())
        self._cached_maintenance_requested_source = str(source or "modem_live")

    def _clocked_live_register_enabled(self) -> bool:
        return bool(getattr(self.input_cfg, "modem_live_register_enabled", True))

    def live_register_status(self) -> Dict[str, Any]:
        return self._live_register_status(now=time.time())

    def live_truth_matrix(self) -> Dict[str, Any]:
        raise RuntimeError(
            "live_truth_matrix_copy_forbidden:"
            "use_committed_live_generation"
        )

    def committed_live_generation(self):
        if not hasattr(self, "_live_register_lock"):
            raise RuntimeError(
                "modem_live_committed_generation_unavailable"
            )
        with self._live_register_lock:
            return self._live_register_store.committed_generation()

    def capture_raw_modem_generation(
        self,
        *,
        minimum_sequence: int = 0,
    ) -> RawModemGeneration:
        """Rejected old carrier boundary. Matrix is the only raw-data source."""

        del minimum_sequence
        raise RuntimeError(
            "detached_raw_modem_generation_path_illegal_use_live_truth_matrix"
        )

    def cached_generation_rtt_observation(
        self,
        *,
        now: Optional[float] = None,
    ) -> RttObservation:
        """Read the current RTT value without starting another probe."""

        max_age_s = max(
            float(
                getattr(self.input_cfg, "actionable_rtt_max_age_s", 0.0) or 0.0
            ),
            self._rtt_cadence_interval_s() * 1.5,
        )
        return self._ensure_rtt_observation_authority().snapshot(
            now=now,
            max_age_s=max_age_s,
        )

    def _matrix_bound_rtt_observation(
        self,
        *,
        matrix_identity: MatrixGenerationIdentity,
        bound_at: float,
    ) -> MatrixBoundRttObservation:
        """Bind the RTT snapshot to one Matrix publication."""

        observation = self.cached_generation_rtt_observation(
            now=bound_at,
        )
        return MatrixBoundRttObservation(
            matrix_identity=matrix_identity,
            observation=observation,
            bound_at=max(float(bound_at), observation.snapshot_at),
        )

    def rtt_observation_register_snapshot(
        self,
        *,
        now: Optional[float] = None,
    ) -> ModemLiveRttRegisterSnapshot:
        """Read the session RTT register without modem I/O."""

        max_age_s = max(
            float(
                getattr(self.input_cfg, "actionable_rtt_max_age_s", 0.0) or 0.0
            ),
            self._rtt_cadence_interval_s() * 1.5,
        )
        register_lock = getattr(self, "_live_register_lock", None)
        with register_lock if register_lock is not None else nullcontext():
            context = self._connectivity_attempt_context(
                physical_attempt_id=""
            )
            sequence, observation = (
                self._ensure_rtt_observation_authority().read_register(
                    now=now,
                    max_age_s=max_age_s,
                )
            )
            return ModemLiveRttRegisterSnapshot(
                sequence=sequence,
                session_scope_key=str(
                    context.get("session_scope_key") or ""
                ),
                identity_code=str(context.get("identity_code") or ""),
                observation=observation,
                confirmation=(
                    self._service_confirmation_receipt
                    if self._service_confirmation_receipt is not None
                    and self._service_confirmation_receipt.namespace == observation.namespace
                    else None
                ),
            )

    def request_service_confirmation(
        self, *, namespace: RuntimeGenerationNamespace, requested_at: float,
    ) -> None:
        """Queue one check. Core does not wait for network I/O here."""
        if not isinstance(namespace, RuntimeGenerationNamespace) or not math.isfinite(requested_at) or requested_at <= 0:
            raise ValueError("modem_live_service_confirmation_request_invalid")
        with self._live_register_lock:
            if self._modem_live_shutdown.is_set() or self._service_confirmation_request is not None:
                return
            _, current = self._ensure_rtt_observation_authority().read_register(
                now=requested_at, max_age_s=self._rtt_cadence_interval_s() * 1.5,
            )
            if current.namespace != namespace:
                return
            if requested_at - self._last_service_confirmation_at < CONFIRMATION_MIN_INTERVAL_S:
                return
            self._service_confirmation_request = (namespace, requested_at)

    def _run_service_confirmation(self) -> None:
        """Run only on the existing RTT worker, with one probe in flight."""
        with self._live_register_lock:
            request = self._service_confirmation_request
            if request is None or self._modem_live_shutdown.is_set():
                self._service_confirmation_request = None
                return
        if not self._rtt_probe_execution_lock.acquire(blocking=False):
            return
        try:
            namespace, requested_at = request
            now = float(time.time())
            _, current = self._ensure_rtt_observation_authority().read_register(
                now=now, max_age_s=self._rtt_cadence_interval_s() * 1.5,
            )
            if (current.namespace != namespace or (
                    current.probe_status in {"ok", "reachable_only"}
                    and current.last_probe_at >= requested_at)
                    or now - requested_at > CONFIRMATION_MIN_INTERVAL_S):
                return
            host_field, _user, _password, _ping = modem_connection_config_fields(self.input_cfg.modem_backend)
            if not host_field:
                raise ValueError("modem_live_service_confirmation_profile_required")
            router_host = str(getattr(self.input_cfg, host_field)).strip()
            target = self._scheduled_rtt_targets()[0]
            self._last_service_confirmation_at = now
            received, reason = confirm_external_response(router_host=router_host, target=target)
            receipt = ServiceConfirmationReceipt(
                request_id="service-confirmation:" + str(time.time_ns()),
                namespace=namespace, requested_at=requested_at,
                completed_at=max(requested_at, float(time.time())), target=target,
                response_received=received, reason=reason,
            )
            with self._live_register_lock:
                self._service_confirmation_receipt = receipt
        finally:
            with self._live_register_lock:
                self._service_confirmation_request = None
            self._rtt_probe_execution_lock.release()

    def _ensure_rtt_observation_authority(
        self,
    ) -> ModemLiveRttObservationAuthority:
        authority = getattr(self, "_rtt_observation_authority", None)
        if not isinstance(authority, ModemLiveRttObservationAuthority):
            authority = ModemLiveRttObservationAuthority()
            self._rtt_observation_authority = authority
        return authority

    def materialize_accepted_generation(
        self,
        accepted: AcceptedModemGeneration,
    ) -> InputSnapshot:
        """Rejected old path that rebuilt an accepted snapshot."""

        del accepted
        raise RuntimeError(
            "accepted_generation_materialization_illegal_use_matrix_commit"
        )

    def _live_register_status(self, *, now: float) -> Dict[str, Any]:
        if not hasattr(self, "_live_register_lock"):
            return {"enabled": False, "source": "modem_live_register"}
        with self._live_register_lock:
            current_status = self._live_register_store.current_status()
            thread = self._live_register_thread
            worker_alive = bool(thread is not None and thread.is_alive())
            contract = self._refresh_live_acquisition_contract_from_config()
            access_profile = self._live_access_profile()
            status = build_live_register_status(
                enabled=bool(self._clocked_live_register_enabled()),
                current_status=current_status,
                now=now,
                contract=contract,
                access_profile=access_profile,
                clock_ms=int(self._live_register_clock_ms),
                cadence_cycles=int(self._live_register_cadence_cycles),
                interval_s=float(self._live_register_interval_s),
                stale_after_s=float(self._live_register_stale_s),
                refreshing=bool(self._live_register_refreshing),
                worker_alive=worker_alive,
                last_attempt_ts=float(self._live_register_last_attempt_ts or 0.0),
                last_miss_ts=float(self._live_register_last_miss_ts or 0.0),
                miss_count=int(self._live_register_read_miss_count or 0),
                last_reason=str(self._live_register_last_reason or ""),
                connectivity=dict(self._live_register_connectivity_status or {}),
                clock=dict(self._live_register_clock_status or {}),
                modem_io=self.modem_io_status(),
                live_config_overrides=dict(getattr(self, "_live_acquisition_contract_overrides", {}) or {}),
                hard_rules=dict(getattr(self, "_live_acquisition_contract_hard_rules", {}) or {}),
                hard_rule_clamps=dict(getattr(self, "_live_acquisition_contract_hard_rule_clamps", {}) or {}),
            )
            status["sample_history_depth"] = 0
            status["sample_history_sequences"] = []
            status["restart_requested"] = bool(
                self._live_register_restart_requested
            )
            status["worker_refreshing"] = bool(
                self._live_register_worker_refreshing
            )
            worker_started = float(
                self._live_register_worker_attempt_started_ts or 0.0
            )
            worker_finished = float(
                self._live_register_worker_last_finished_ts or 0.0
            )
            status["worker_attempt_started_ts"] = worker_started
            status["worker_last_finished_ts"] = worker_finished
            status["worker_attempt_age_s"] = (
                max(0.0, float(now) - worker_started)
                if worker_started > 0.0
                else 0.0
            )
            status["active_refresh_count"] = int(
                self._live_register_refresh_active_count
            )
            status["engine_intake_mode"] = str(self._engine_intake_turbo_mode)
            status["engine_intake_missing_total"] = int(
                getattr(self, "_engine_intake_turbo_missing_total", 0) or 0
            )
            expected_interruption = (
                self._ensure_connectivity_module(
                ).expected_interruption_status_payload(now=now)
            )
            status["modem_live_io_quiet_active"] = bool(
                expected_interruption.get("active", False)
            )
            status["modem_live_io_quiet_remaining_s"] = float(
                expected_interruption.get("remaining_s", 0.0) or 0.0
            )
            status["modem_live_expected_interruption"] = expected_interruption
            status["publication_sink_bound"] = bool(
                self._live_register_publication_sink is not None
            )
            status["publication_sink_error"] = str(
                self._live_register_publication_sink_error or ""
            )
            return status

    @staticmethod
    def _live_register_stale_cause(register_status: Dict[str, Any], *, valid: bool, modem: Optional[ModemInput]) -> str:
        return live_register_stale_cause(register_status, valid=valid, modem=modem)

    @staticmethod
    def _live_register_session_expired(receipt: BackendAcquisitionReceipt) -> bool:
        if receipt.failure_reason == "modem_session_expired":
            return True
        session_state = receipt.status_copy().get("backend_session")
        if not isinstance(session_state, Mapping):
            return False
        observed_at = session_state.get("ts")
        return (
            session_state.get("expired") is True
            and type(observed_at) in (int, float)
            and receipt.request.started_at <= observed_at <= receipt.finished_at
        )

    def _live_register_expected_sample_interval_s(self) -> float:
        access_profile = self._live_access_profile()
        if hasattr(access_profile, "fast_sample_expected_interval_s"):
            return float(
                access_profile.fast_sample_expected_interval_s(
                    clock_ms=int(getattr(self, "_live_register_clock_ms", 100) or 100)
                )
            )
        interval_s = max(0.0, float(getattr(access_profile, "fast_live_interval_s", 0.0) or 0.0))
        if interval_s <= 0.0:
            interval_s = max(0.0, float(getattr(self, "_live_register_interval_s", 0.0) or 0.0))
        read_slo_s = max(0.0, float(getattr(access_profile, "fast_read_slo_s", 0.0) or 0.0))
        clock_margin_s = max(
            0.2,
            float(getattr(self, "_live_register_clock_ms", 100) or 100) / 1000.0 * 2.0,
        )
        return max(interval_s + read_slo_s + clock_margin_s, interval_s + 0.5)

    def _live_maintenance_required_margin_s(self) -> float:
        access_profile = self._live_access_profile()
        try:
            connect_s = max(
                0.0,
                float(getattr(access_profile, "maintenance_connect_timeout_s", 0.0) or 0.0),
            )
        except Exception:
            connect_s = 0.0
        try:
            read_s = max(
                0.0,
                float(getattr(access_profile, "maintenance_read_timeout_s", 0.0) or 0.0),
            )
        except Exception:
            read_s = 0.0
        try:
            cooldown_s = max(
                0.0,
                float(getattr(access_profile, "min_inter_request_gap_s", 0.0) or 0.0),
            )
        except Exception:
            cooldown_s = 0.0
        clock_margin_s = max(
            0.1,
            float(getattr(self, "_live_register_clock_ms", 100) or 100) / 1000.0,
        )
        if bool(getattr(getattr(self, "_live_acquisition_contract", None), "maintenance_step_requires_fast_margin", False)):
            return cooldown_s + connect_s + read_s + clock_margin_s
        return max(cooldown_s, connect_s + read_s + clock_margin_s)

    def _access_cooldown_s_for(self, lane: str) -> float:
        lane_key = str(lane or "").strip().lower()
        if lane_key == "export":
            return 0.0
        access_profile = self._live_access_profile()
        field_name = "min_inter_request_gap_s"
        if lane_key == "event_write":
            field_name = "event_write_min_quiet_s"
        elif lane_key == "readback":
            field_name = "post_write_readback_delay_s"
        try:
            return max(0.0, float(getattr(access_profile, field_name, 0.0) or 0.0))
        except Exception:
            return 0.0

    def _mark_live_maintenance_skipped(self, reason: str) -> Dict[str, Any]:
        return self._ensure_protected_live_lane().mark_maintenance_skipped(
            reason,
            base_status=self.protected_live_lane_status(),
        )

    def _record_protected_live_sample(
        self,
        *,
        started_ts: float,
        finished_ts: float,
        success: bool,
    ) -> Dict[str, Any]:
        return self._ensure_protected_live_lane().record_sample(
            started_ts=started_ts,
            finished_ts=finished_ts,
            success=success,
        )

    def _start_live_register_clock(self) -> None:
        self._start_rtt_cadence_worker()
        if (
            not self._clocked_live_register_enabled()
            or self._modem_live_shutdown.is_set()
        ):
            return
        with self._live_register_lock:
            if self._modem_live_shutdown.is_set():
                return
            thread = self._live_register_thread
            if thread is not None and thread.is_alive():
                return
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._live_register_clock_loop,
                args=(stop_event,),
                name="arch4-live-register-clock",
                daemon=True,
            )
            self._live_register_worker_stop = stop_event
            self._live_register_thread = thread
            self._live_register_restart_requested = False
            thread.start()

    def _live_register_clock_loop(self, stop_event: threading.Event) -> None:
        if not isinstance(stop_event, threading.Event):
            raise TypeError("live_register_worker_stop_event_required")
        maintenance_interval_s = self._resolve_live_maintenance_interval_s()
        maintenance_cadence_cycles = max(
            1,
            int(
                round(
                    maintenance_interval_s
                    / max(
                        0.001,
                        float(self._live_register_clock_ms) / 1000.0,
                    )
                )
            ),
        )
        clock = ClockedScheduler(
            clock_ms=self._live_register_clock_ms,
            lane_specs=(
                ClockedLaneSpec(
                    name="fast_modem_register",
                    owner="modem_live",
                    purpose="protected Modem Live register refresh",
                    default_cadence_cycles=self._live_register_cadence_cycles,
                    budget_ms=float(
                        getattr(
                            self.input_cfg,
                            "actionable_modem_read_timeout_s",
                            2.5,
                        )
                        or 2.5
                    )
                    * 1000.0,
                    priority=0,
                    skip_policy=ClockedSkipPolicy.NEVER_SKIP_CACHE_LAST_GOOD,
                    output_surface="live_modem_register",
                    recovery_permission="cache_last_good",
                    interrupt_policy="preempt_low_priority",
                ),
                ClockedLaneSpec(
                    name="slow_modem_maintenance",
                    owner="modem_live",
                    purpose="refresh profile-owned slow modem caches",
                    default_cadence_cycles=maintenance_cadence_cycles,
                    budget_ms=float(self._live_register_clock_ms),
                    priority=10,
                    skip_policy=ClockedSkipPolicy.SKIP_IF_LATE,
                    output_surface="live_modem_register_cache",
                    recovery_permission="retain_last_good_cache",
                    interrupt_policy="cooperative",
                ),
            ),
        )
        clock.register(
            ClockedJob(
                owner="modem_live",
                name="refresh_registers",
                cadence_cycles=self._live_register_cadence_cycles,
                budget_ms=float(getattr(self.input_cfg, "actionable_modem_read_timeout_s", 2.5) or 2.5) * 1000.0,
                lane="fast_modem_register",
                priority=0,
                output_surface="live_modem_register",
                recovery_permission="cache_last_good",
            )
        )
        clock.register(
            ClockedJob(
                owner="modem_live",
                name="refresh_maintenance",
                cadence_cycles=maintenance_cadence_cycles,
                budget_ms=float(self._live_register_clock_ms),
                lane="slow_modem_maintenance",
                priority=10,
                output_surface="live_modem_register_cache",
                recovery_permission="retain_last_good_cache",
            )
        )
        clock_s = max(0.001, float(clock.clock_ms) / 1000.0)
        try:
            while (
                not stop_event.is_set()
                and not self._modem_live_shutdown.is_set()
            ):
                clock.observe_wall()

                def _runner(_job: ClockedJob) -> ClockedJobOutcome:
                    if _job.name == "refresh_maintenance":
                        self._start_live_maintenance_cycle(
                            now=time.time(),
                            due_by_interval=True,
                            interval_s=maintenance_interval_s,
                        )
                        return ClockedJobOutcome(
                            ran=True,
                            backlog=0,
                        )
                    outcome = self._refresh_live_register_once()
                    return ClockedJobOutcome(
                        ran=True,
                        backlog=0,
                        last_error=str(outcome.get("last_error", "") or ""),
                        backpressure_reason=str(outcome.get("backpressure_reason", "") or ""),
                    )

                try:
                    clock.run_due(_runner)
                    if self._network_inspection_demand is not None or self._read_diagnostic_demand is not None:
                        self._start_live_maintenance_cycle(
                            now=time.time(), requested=True, due_by_interval=False,
                        )
                except ProductRuntimeWorkerReplacementRequired:
                    # Product receives the retained request; this producer must
                    # not continue reading or retry it as a clock fault.
                    return
                except Exception as exc:
                    self._record_backend_fault("live_register_clock", exc)
                    log.debug("LIVE MODULE register clock suppressed %s", exc)
                with self._live_register_lock:
                    self._live_register_clock_status = dict(clock.status() or {})
                stop_event.wait(clock_s)
        finally:
            current = threading.current_thread()
            with self._live_register_lock:
                if self._live_register_thread is current:
                    self._live_register_thread = None
                    self._live_register_worker_stop = None

    def _execute_admitted_live_register_session_repair(
        self,
        base_status: Optional[Dict[str, Any]],
        *,
        request: BackendAcquisitionRequest,
        cause: str,
        source_key: str,
        access_source: str = "live_register",
        post_write_stage: PhysicalCommandDispatchEvidence | None = None,
    ) -> tuple[
        ModemLiveSessionRepairReceipt | None,
        BackendAcquisitionReceipt | None,
    ]:
        """Execute one Connectivity-admitted repair under Modem Live I/O."""

        connectivity = self._ensure_connectivity_module()
        with self._backend_io_context(force_lock=True):
            admission_now = time.time()
            repair_request = ModemLiveSessionRepairRequest(
                request_id=request.attempt_id,
                cause=str(cause or "").strip(),
                source_epoch=str(self._publication_source_epoch or "").strip(),
                evidence_generation=self._live_register_sequence(),
                observed_at=admission_now,
            )
            admission = connectivity.admit_session_repair(
                request=repair_request,
                now=admission_now,
            )
            if not admission.admitted:
                return None, None
            kind = "generic_backend_session"
            transport_succeeded = False
            recovered = False
            error = ""
            retry_receipt: BackendAcquisitionReceipt | None = None
            invariant_error: Exception | None = None
            try:
                profile_repair = getattr(
                    self._modem_backend,
                    "recover_live_register_session",
                    None,
                )
                if callable(profile_repair):
                    kind = "profile_live_register_session"
                    transport_succeeded = bool(
                        self._call_modem(
                            "recover_live_register_session",
                            source=access_source,
                            operation="recover_session",
                            lock_policy="backend_force",
                            base_status=dict(base_status or {}),
                        )
                    )
                else:
                    connected = self._modem_is_connected_direct()
                    transport_succeeded = bool(
                        connected or self._modem_connect_direct()
                    )
                if not transport_succeeded:
                    error = (
                        "profile_session_repair_not_completed"
                        if kind == "profile_live_register_session"
                        else "generic_backend_session_not_connected"
                    )
                if transport_succeeded:
                    retry_request = self._new_backend_acquisition_request(
                        lane=request.lane,
                        source=f"{source_key}:session_repair",
                    )
                    retry_receipt = self._read_modem_register_direct(
                        access_source=access_source,
                        post_write_stage=post_write_stage,
                        request=retry_request,
                    )
                    recovered = bool(
                        retry_receipt.succeeded
                        and retry_receipt.modem is not None
                    )
                    if not recovered:
                        error = (
                            "session_repair_readback_failed:"
                            + str(
                                retry_receipt.failure_reason
                                or "actionable_modem_register_missing"
                            )
                        )
            except AuthenticationError as exc:
                # An actual authentication failure is terminal for automatic
                # recovery. Retire the worker through the existing Sign In
                # lifecycle; do not invent a different device namespace.
                error = "automatic_login_failed"
                if self._active_device_identity:
                    with self._device_cache_lock:
                        if self._pending_device_replacement is None:
                            namespace = self._live_register_store.runtime_namespace
                            self._pending_device_replacement = (
                                namespace, namespace,
                                ProductRuntimeWorkerReplacementReason.AUTOMATIC_LOGIN_FAILED,
                            )
                    invariant_error = self.pending_device_replacement()
                else:
                    invariant_error = exc  # Initial login retains its startup failure path.
            except (TargetGuardError, ProductRuntimeWorkerReplacementRequired) as exc:
                # A changed target is an admission invariant, never an offline
                # modem. Settle the existing repair lease before failing loudly.
                error = f"{type(exc).__name__}: {exc}"
                invariant_error = exc
            except (ConnectionError, TimeoutError, OSError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                self._record_backend_fault("live_register_session_recover", exc)
                log.debug(
                    "LIVE MODULE admitted session recovery failed %s",
                    exc,
                )
            except Exception as exc:
                # Connectivity owns the in-flight lease, so settle that lease
                # before propagating a programming or contract fault. Such a
                # fault is not transport unavailability and cannot be turned
                # into a quiet failed-read value.
                error = f"{type(exc).__name__}: {exc}"
                invariant_error = exc
            completed = connectivity.complete_session_repair(
                admission,
                kind=kind,
                transport_succeeded=transport_succeeded,
                recovered=recovered,
                error=error,
                now=time.time(),
            )
            if invariant_error is not None:
                raise invariant_error
            return completed, retry_receipt

    def _refresh_live_register_once(self) -> Dict[str, Any]:
        return self._refresh_live_register_once_from_source(source="clocked_modem_register")

    def _live_register_sequence(self) -> int:
        with self._live_register_lock:
            return int(self._live_register_store.sequence())

    def _new_backend_acquisition_request(
        self,
        *,
        lane: str,
        source: str,
        started_at: Optional[float] = None,
    ) -> BackendAcquisitionRequest:
        started = float(
            time.time() if started_at is None else started_at
        )
        with self._live_register_lock:
            self._live_register_physical_attempt_sequence += 1
            attempt_id = (
                f"{self._publication_source_epoch}:"
                f"{str(source or lane).strip()}:"
                f"{self._live_register_physical_attempt_sequence}"
            )
        return BackendAcquisitionRequest(
            attempt_id=attempt_id,
            lane=lane,
            started_at=started,
        )

    @staticmethod
    def _coalesce_latest_acquisition_receipts(
        receipts: tuple[BackendAcquisitionReceipt, ...],
    ) -> tuple[BackendAcquisitionReceipt, ...]:
        """Keep the newest physical receipt for each acquired Matrix cell."""

        successful = tuple(
            receipt
            for receipt in receipts
            if isinstance(receipt, BackendAcquisitionReceipt)
            and receipt.succeeded
        )
        claimed: set[str] = set()
        newest_first: list[BackendAcquisitionReceipt] = []
        for receipt in sorted(
            successful,
            key=lambda item: (item.finished_at, item.request.attempt_id),
            reverse=True,
        ):
            selected = receipt.acquired_cells.difference(claimed)
            if not selected:
                continue
            newest_first.append(
                receipt
                if selected == receipt.acquired_cells
                else receipt.restricted_to_cells(frozenset(selected))
            )
            claimed.update(selected)
        return tuple(reversed(newest_first))

    def _latch_acquisition_receipts(
        self,
        receipts: tuple[BackendAcquisitionReceipt, ...],
    ) -> None:
        successful = tuple(
            receipt
            for receipt in receipts
            if isinstance(receipt, BackendAcquisitionReceipt)
            and receipt.succeeded
        )
        if not successful:
            return
        with self._live_register_lock:
            self._pending_acquisition_receipts = (
                self._coalesce_latest_acquisition_receipts(
                    self._pending_acquisition_receipts + successful
                )
            )

    def _pending_receipts_snapshot(
        self,
    ) -> tuple[BackendAcquisitionReceipt, ...]:
        with self._live_register_lock:
            return tuple(self._pending_acquisition_receipts)

    def _acknowledge_committed_acquisition_receipts(
        self,
        receipts: tuple[BackendAcquisitionReceipt, ...],
    ) -> None:
        committed_ids = frozenset(
            receipt.request.attempt_id
            for receipt in receipts
        )
        if not committed_ids:
            return
        with self._live_register_lock:
            self._pending_acquisition_receipts = tuple(
                receipt
                for receipt in self._pending_acquisition_receipts
                if receipt.request.attempt_id not in committed_ids
            )

    @staticmethod
    def _merge_acquisition_receipt_payloads(
        receipts: tuple[BackendAcquisitionReceipt, ...],
        *,
        primary: BackendAcquisitionReceipt,
    ) -> tuple[ModemInput, Dict[str, Any]]:
        modem = primary.acquired_modem_copy()
        if modem is None:
            raise RuntimeError(
                "modem_live_primary_acquisition_modem_required"
            )
        status: Dict[str, Any] = {}
        for receipt in sorted(
            receipts,
            key=lambda item: (
                float(item.finished_at),
                str(item.request.attempt_id),
            ),
        ):
            for field, value in receipt.status_copy().items():
                cells = (
                    matrix_cells_for_canonical_fields(frozenset({field}))
                    if field in DECLARED_CANONICAL_SOURCE_FIELDS
                    else frozenset()
                )
                if cells and not cells.intersection(
                    receipt.acquired_cells | DERIVED_HEALTH_CELLS
                ):
                    continue
                status[field] = value
        return modem, status

    def _reset_startup_live_baseline_status(self) -> None:
        self._pending_acquisition_receipts = ()
        self._startup_live_baseline_status = {
            "owner": "modem_live",
            "completed": False,
            "attempted": False,
            "source": "",
            "started_at": 0.0,
            "finished_at": 0.0,
            "duration_ms": 0.0,
            "last_error": "",
            "acquisition_attempt_id": "",
        }

    def _establish_startup_live_baseline_from_receipt(
        self,
        *,
        receipt: BackendAcquisitionReceipt,
        source: str,
    ) -> Dict[str, Any]:
        """Bind startup baseline truth to the one completed full acquisition."""

        if not isinstance(receipt, BackendAcquisitionReceipt):
            raise TypeError("modem_live_startup_full_receipt_required")
        if receipt.request.lane != "startup_full":
            raise ValueError("modem_live_startup_full_receipt_lane_required")
        if not receipt.succeeded:
            raise RuntimeError(
                "modem_live_startup_full_receipt_failed:"
                f"{receipt.failure_reason or 'unknown'}"
            )
        status = {
            "owner": "modem_live",
            "completed": True,
            "attempted": True,
            "source": str(source or "startup_live_phase"),
            "started_at": float(receipt.request.started_at),
            "finished_at": float(receipt.finished_at),
            "duration_ms": round(
                max(
                    0.0,
                    float(receipt.finished_at)
                    - float(receipt.request.started_at),
                )
                * 1000.0,
                3,
            ),
            "last_error": "",
            "acquisition_attempt_id": receipt.request.attempt_id,
            "acquisition_receipt_ids": [receipt.request.attempt_id],
            "acquired_cells": sorted(receipt.acquired_cells),
        }
        with self._live_register_lock:
            current = dict(self._startup_live_baseline_status or {})
            if bool(current.get("attempted", False)):
                if (
                    str(current.get("acquisition_attempt_id") or "")
                    != receipt.request.attempt_id
                ):
                    raise RuntimeError(
                        "modem_live_startup_baseline_receipt_changed"
                    )
                return current
            self._startup_live_baseline_status = status
            return dict(status)

    def _establish_startup_live_register(
        self,
        *,
        acquisition_receipt: Optional[
            BackendAcquisitionReceipt
        ] = None,
        source: str = "startup_live_phase",
    ) -> Optional[ModemInput]:
        """Create the first live-register generation during startup.

        Consumers may read the register but cannot initialize it. This method
        publishes the receipt-bound first Matrix generation without another
        physical read.
        """

        if not self._clocked_live_register_enabled():
            return (
                acquisition_receipt.acquired_modem_copy()
                if acquisition_receipt is not None
                and acquisition_receipt.succeeded
                else None
            )
        if self._live_register_sequence() <= 0:
            if not isinstance(
                acquisition_receipt,
                BackendAcquisitionReceipt,
            ):
                raise TypeError(
                    "modem_live_startup_receipt_bound_publication_required"
                )
            baseline = dict(self._startup_live_baseline_status or {})
            if not bool(baseline.get("completed", False)):
                raise RuntimeError(
                    "modem_live_startup_receipt_baseline_required"
                )
            if (
                str(baseline.get("acquisition_attempt_id") or "")
                != acquisition_receipt.request.attempt_id
            ):
                raise RuntimeError(
                    "modem_live_startup_receipt_baseline_mismatch"
                )
            self._publish_live_register_snapshot(
                primary_receipt=acquisition_receipt,
                source=str(source or "startup_live_phase"),
                backend_repair=None,
            )
        with self._live_register_lock:
            committed_primary = self._startup_publication_primary_receipt
        if committed_primary is not None:
            return committed_primary[1].acquired_modem_copy()
        return (
            acquisition_receipt.acquired_modem_copy()
            if acquisition_receipt is not None
            and acquisition_receipt.succeeded
            else None
        )

    def _refresh_live_register_once_from_source(
        self,
        *,
        source: str,
        post_write_stage: PhysicalCommandDispatchEvidence | None = None,
    ) -> Dict[str, Any]:
        started_ts = time.time()
        source_key = str(source or "clocked_modem_register")
        with self._live_register_lock:
            first_publication = bool(self._live_register_store.sequence() <= 0)
        if first_publication:
            raise RuntimeError(
                "modem_live_startup_receipt_bound_publication_required"
            )
        priority_refresh = source_key.startswith("priority_event_write:")
        compound_event_sample = source_key.startswith(
            "compound_event_write_sample:"
        )
        if priority_refresh and not isinstance(
            post_write_stage,
            PhysicalCommandDispatchEvidence,
        ):
            raise TypeError("priority_refresh_exact_physical_stage_required")
        if compound_event_sample and post_write_stage is not None:
            raise TypeError("compound_event_sample_cannot_verify_target")
        clock_worker_refresh = source_key == "clocked_modem_register"
        access_source = "readback" if priority_refresh else "live_register"
        if (
            self._event_write_pending_active()
            and not priority_refresh
            and not compound_event_sample
        ):
            self._ensure_access_scheduler().record_defer(
                "fast_live",
                now=started_ts,
                reason="event_write_pending",
                requested=True,
                due=True,
                metadata=self.event_write_pending_status(),
            )
            with self._live_register_lock:
                self._live_register_last_reason = "event_write_pending"
            return {
                "last_error": "",
                "backpressure_reason": "event_write_pending",
                "owner": "modem_live",
            }
        self._ensure_access_scheduler().decide_fast_live(now=started_ts)
        with self._live_register_lock:
            self._live_register_refresh_active_count += 1
            self._live_register_refreshing = True
            if clock_worker_refresh:
                self._live_register_worker_refreshing = True
                self._live_register_worker_attempt_started_ts = started_ts
            self._live_register_last_attempt_ts = started_ts
        try:
            network_primary = None
            if post_write_stage is not None and isinstance(post_write_stage.target, CoreNetworkSettingsTarget):
                # Normal polling is paused during a write. Obtain the existing
                # fast recipe under its own admission, then inspect settings.
                # In particular RouterOS reuses that monitor's SIM evidence.
                live_request = self._new_backend_acquisition_request(
                    lane="fast_live", source=source_key,
                )
                network_primary = self._read_modem_register_direct(
                    access_source=access_source, request=live_request,
                )
                if not network_primary.succeeded or network_primary.modem is None:
                    raise RuntimeError("network_readback_live_snapshot_unavailable")
            request = self._new_backend_acquisition_request(
                lane=(
                    "post_write"
                    if post_write_stage is not None
                    else "fast_live"
                ),
                source=source_key,
                started_at=started_ts,
            )
            receipt = self._read_modem_register_direct(
                access_source=access_source,
                post_write_stage=post_write_stage,
                request=request,
            )
            modem = (
                receipt.acquired_modem_copy()
                if receipt.succeeded
                else None
            )
            base_status = receipt.status_copy()
            if (
                post_write_stage is not None
                and receipt.succeeded
                and modem is None
            ):
                self._latch_acquisition_receipts((receipt,))
                if network_primary is not None:
                    return {"published_sequence": self._publish_live_register_snapshot(
                        primary_receipt=network_primary, source=source_key, backend_repair=None,
                    )}

                return {
                    "target_evidence_latched": True,
                    "target_evidence_attempt_id": receipt.request.attempt_id,
                }
            finished_ts = time.time()
            live_lane_status = self._record_protected_live_sample(
                started_ts=started_ts,
                finished_ts=finished_ts,
                success=modem is not None,
            )
            session_expired = self._live_register_session_expired(receipt)
            backend_repair: ModemLiveSessionRepairReceipt | None = None
            if modem is None and (
                session_expired or not self._live_register_has_fresh_cached_sample(now=finished_ts)
            ):
                backend_repair, retry_receipt = (
                    self._execute_admitted_live_register_session_repair(
                        base_status,
                        request=request,
                        cause=(
                            "confirmed_session_expired"
                            if session_expired
                            else str(
                                receipt.failure_reason
                                or "actionable_modem_register_missing"
                            )
                        ),
                        source_key=source_key,
                        access_source=access_source,
                        post_write_stage=post_write_stage,
                    )
                )
                # Observe the combined attempt after repair completes, even
                # when login failed and produced no retry acquisition.
                finished_ts = time.time()
                if retry_receipt is not None:
                    receipt = retry_receipt
                    modem = (
                        receipt.acquired_modem_copy()
                        if receipt.succeeded
                        else None
                    )
                    base_status = receipt.status_copy()
                    live_lane_status = self._record_protected_live_sample(
                        started_ts=started_ts,
                        finished_ts=finished_ts,
                        success=modem is not None,
                    )
            if modem is None:
                connectivity_status: Dict[str, Any] = {}
                if self.health_checks:
                    try:
                        self.health_checks.on_modem_read_failed(now=finished_ts)
                    except Exception as exc:
                        self._record_backend_fault("live_register_health_failure", exc)
                        log.debug(
                            "LIVE MODULE register health failure update suppressed %s",
                            exc,
                        )
                try:
                    attempt_context = self._connectivity_attempt_context(
                        physical_attempt_id=receipt.request.attempt_id,
                    )
                    attempt_context["read_failure_reason"] = str(
                        receipt.failure_reason
                        or "modem_live_backend_acquisition_failed"
                    )
                    connectivity_status = self._record_connectivity_attempt(
                        succeeded=False,
                        now=finished_ts,
                        attempt_context=attempt_context,
                        backend_repair=backend_repair,
                    )
                except Exception as exc:
                    self._record_backend_fault("live_register_connectivity", exc)
                    raise RuntimeError(
                        "physical_attempt_connectivity_receipt_failed"
                    ) from exc
                with self._live_register_lock:
                    self._live_register_read_miss_count += 1
                    self._live_register_last_miss_ts = finished_ts
                    self._live_register_last_reason = "refresh_missed"
                    if connectivity_status:
                        self._live_register_connectivity_status = dict(connectivity_status or {})
                return {
                    "last_error": "modem_register_refresh_missed",
                    "backpressure_reason": "cache_last_good",
                }

            published_sequence = self._publish_live_register_snapshot(
                primary_receipt=receipt,
                source=str(source or "clocked_modem_register"),
                backend_repair=backend_repair,
            )
            return {
                "live_lane_status": dict(live_lane_status),
                "published_sequence": published_sequence,
            }
        finally:
            with self._live_register_lock:
                self._live_register_refresh_active_count = max(
                    0,
                    self._live_register_refresh_active_count - 1,
                )
                if clock_worker_refresh:
                    self._live_register_worker_refreshing = False
                    self._live_register_worker_attempt_started_ts = 0.0
                    self._live_register_worker_last_finished_ts = time.time()
                self._live_register_refreshing = bool(
                    self._live_register_refresh_active_count > 0
                )

    def request_priority_live_register_refresh(
        self,
        *,
        reason: str = "event_write",
        physical_stages: tuple[PhysicalCommandDispatchEvidence, ...],
        deadline_at: float,
    ) -> Dict[str, Any]:
        with self._priority_live_register_lifecycle_lock:
            return self._request_priority_live_register_refresh_locked(
                reason=reason,
                physical_stages=physical_stages,
                deadline_at=deadline_at,
            )

    def _request_priority_live_register_refresh_locked(
        self,
        *,
        reason: str = "event_write",
        physical_stages: tuple[PhysicalCommandDispatchEvidence, ...],
        deadline_at: float,
    ) -> Dict[str, Any]:
        """Retain bounded post-write inspection without blocking normal reads.

        Modem Live applies the remaining profile quiet time and performs the
        physical read. Core receives an admission receipt. An accepted Matrix
        generation confirms the command.
        """

        if not isinstance(physical_stages, tuple) or not physical_stages:
            raise TypeError("priority_refresh_physical_stages_required")
        if (isinstance(deadline_at, bool) or not isinstance(deadline_at, (int, float))
                or not math.isfinite(deadline_at) or deadline_at <= 0):
            raise ValueError("priority_refresh_core_deadline_required")
        if any(
            not isinstance(stage, PhysicalCommandDispatchEvidence)
            for stage in physical_stages
        ):
            raise TypeError("priority_refresh_physical_stage_evidence_required")
        if any(
            not stage.permits_target_verification
            for stage in physical_stages
        ):
            raise ValueError("priority_refresh_accepted_physical_stages_required")
        target_stages = tuple(
            stage
            for stage in physical_stages
            if isinstance(stage.target, CORE_COMMAND_TARGET_TYPES)
        )
        if not target_stages:
            raise ValueError("priority_refresh_target_stage_required")
        post_write_stage = target_stages[-1]
        dispatched_at = max(
            float(stage.request_dispatched_at) for stage in physical_stages
        )
        transport_returned_at = max(
            float(stage.transport_returned_at) for stage in physical_stages
        )
        if self._modem_live_shutdown.is_set():
            return {
                "accepted": False,
                "reason": "modem_live_shutdown_active",
                "owner": "modem_live",
            }
        profile = self._live_access_profile()
        quiet_s = max(
            0.0,
            float(getattr(profile, "post_write_readback_delay_s", 0.0) or 0.0),
        )
        requested_at = time.time()
        scheduled_at = max(requested_at, transport_returned_at + quiet_s)
        retry_s = max(quiet_s, float(profile.fast_live_interval_s))
        source_epoch = self._publication_source_epoch
        source = f"priority_event_write:{str(reason or 'event_write')}"

        with self._live_register_lock:
            if self._priority_live_register_refreshing:
                return {
                    "accepted": False,
                    "reason": "priority_refresh_already_running",
                    "owner": "modem_live",
                }
            self._priority_live_register_refreshing = True
            self._priority_live_register_refresh_status = {
                "owner": "modem_live",
                "accepted": True,
                "state": "scheduled",
                "reason": str(reason or "event_write"),
                "physical_write_dispatched_at": dispatched_at,
                "physical_transport_returned_at": transport_returned_at,
                "post_write_stage": post_write_stage.as_dict(),
                "requested_at": requested_at,
                "scheduled_at": scheduled_at,
                "remaining_delay_s": max(0.0, scheduled_at - requested_at),
                "deadline_at": deadline_at,
                "source_epoch": source_epoch,
            }
            admission = dict(self._priority_live_register_refresh_status)

        def _worker() -> None:
            result: Dict[str, Any] = {}
            try:
                # A timed wait may wake just before the requested boundary.
                # Recheck the same profile deadline before permitting a read.
                while (remaining_s := scheduled_at - time.time()) > 0.0:
                    if self._modem_live_shutdown.wait(remaining_s):
                        result = {"state": "stopped", "last_error": "runtime_stopping"}
                        return
                if self._modem_live_shutdown.is_set():
                    result = {"state": "stopped", "last_error": "runtime_stopping"}
                    return
                while not self._modem_live_shutdown.is_set():
                    if self._publication_source_epoch != source_epoch:
                        result = {"state": "cancelled", "last_error": "readback_namespace_changed"}
                        break
                    if time.time() >= deadline_at:
                        result = {**result, "state": "timed_out"}
                        break
                    result = dict(self._refresh_live_register_once_from_source(
                        source=source, post_write_stage=post_write_stage) or {})
                    if int(result.get("published_sequence", 0) or 0) > 0:
                        result["state"] = "published"
                        break
                    if bool(result.get("target_evidence_latched", False)):
                        result["state"] = "latched"
                        break
                    # The identity guard may have failed before the backend was
                    # entered. Keep this exact obligation; never resend a write
                    # or require a backend-specific retry flag to recover it.
                    with self._live_register_lock:
                        self._priority_live_register_refresh_status.update(
                            state="awaiting_readback", last_error=result.get("last_error", ""))
                    if self._modem_live_shutdown.wait(min(retry_s, max(0., deadline_at-time.time()))):
                        result = {"state": "stopped", "last_error": "runtime_stopping"}
                        break
                else:
                    result = {"state": "stopped", "last_error": "runtime_stopping"}
            except Exception as exc:
                self._record_backend_fault("priority_live_register_refresh", exc)
                log.debug("LIVE MODULE priority register refresh suppressed: %s", exc)
                result = {
                    "state": "failed",
                    "last_error": f"{type(exc).__name__}: {exc}",
                }
            finally:
                current = threading.current_thread()
                with self._live_register_lock:
                    self._priority_live_register_refresh_status = {
                        **dict(self._priority_live_register_refresh_status or {}),
                        **result,
                        "finished_at": time.time(),
                    }
                    self._priority_live_register_refreshing = False
                    if self._priority_live_register_refresh_thread is current:
                        self._priority_live_register_refresh_thread = None

        thread = threading.Thread(
            target=_worker,
            name="arch4-priority-live-register-refresh",
            daemon=True,
        )
        with self._live_register_lock:
            if self._priority_live_register_refresh_thread is not None:
                self._priority_live_register_refreshing = False
                raise RuntimeError("priority_live_register_worker_slot_occupied")
            self._priority_live_register_refresh_thread = thread
        try:
            thread.start()
        except Exception:
            with self._live_register_lock:
                if self._priority_live_register_refresh_thread is thread:
                    self._priority_live_register_refresh_thread = None
                    self._priority_live_register_refreshing = False
            raise
        return admission

    def _read_modem_register_direct(
        self,
        *,
        access_source: str = "live_register",
        post_write_stage: PhysicalCommandDispatchEvidence | None = None,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        """Read the fast live register through the selected modem profile."""
        lane_state = getattr(self, "_acquisition_lane_state", None)
        if not isinstance(lane_state, AcquisitionLaneStateAuthority):
            lane_state = AcquisitionLaneStateAuthority()
            self._acquisition_lane_state = lane_state
        if lane_state.state in {
            AcquisitionLaneState.TIMED_OUT,
            AcquisitionLaneState.WEDGED,
        }:
            lane_state.mark(
                AcquisitionLaneState.RECOVERING,
                "starting_direct_recovery_read",
            )
        operation = (
            "acquire_post_write_modem_input"
            if post_write_stage is not None
            else "acquire_actionable_modem_input"
        )
        try:
            if post_write_stage is not None:
                if not isinstance(
                    post_write_stage,
                    PhysicalCommandDispatchEvidence,
                ):
                    raise TypeError(
                        "post_write_physical_stage_evidence_required"
                    )
                modem = self._call_modem(
                    "acquire_post_write_modem_input",
                    request,
                    post_write_stage,
                    source=access_source,
                    operation="acquire_post_write_modem_input",
                    lock_policy="backend",
                )
            elif self._live_acquisition_contract.has_explicit_actionable_snapshot:
                modem = self._call_modem(
                    "acquire_actionable_modem_input",
                    request,
                    source=access_source,
                    operation="acquire_actionable_modem_input",
                    lock_policy="backend",
                )
            else:
                modem = self._call_modem(
                    "acquire_actionable_modem_input",
                    request,
                    source=access_source,
                    operation="acquire_actionable_modem_input",
                    lock_policy="backend",
                )
        except SessionExpiredError as exc:
            return capture_backend_acquisition(
                request=request,
                backend=type(self._modem_backend).__name__,
                profile_id=str(self._live_acquisition_contract.profile_name or type(self._modem_backend).__name__),
                modem=None, status={}, acquired_cells=frozenset(),
                failure_reason=("modem_session_expired" if exc.confirmed
                    else "modem_session_unavailable"),
            )
        except (TargetGuardError, ProductRuntimeWorkerReplacementRequired):
            raise
        except (ConnectionError, OSError) as exc:
            if not self._reconnection_identity_pending:
                raise
            return capture_backend_acquisition(
                request=request,
                backend=type(self._modem_backend).__name__,
                profile_id=str(self._live_acquisition_contract.profile_name or type(self._modem_backend).__name__),
                modem=None,
                status={},
                acquired_cells=frozenset(),
                failure_reason="reconnection_permanent_identity_not_confirmed",
            )
        except Exception as exc:
            exception_type_names = {
                cls.__name__.lower() for cls in type(exc).__mro__
            }
            if any("timeout" in name for name in exception_type_names):
                lane_state.mark(
                    AcquisitionLaneState.TIMED_OUT,
                    f"direct_read_timeout:{type(exc).__name__}",
                )
            else:
                lane_state.mark(
                    AcquisitionLaneState.RECOVERING,
                    f"direct_read_failed:{type(exc).__name__}",
                )
            self._record_backend_fault("live_register_read", exc)
            # A backend reports a physical miss as failed acquisition. An
            # exception is a code fault and must not be reported as modem
            # disconnection.
            raise
        if modem is None:
            return capture_backend_acquisition(
                request=request,
                backend=type(self._modem_backend).__name__,
                profile_id=str(
                    self._live_acquisition_contract.profile_name
                    or type(self._modem_backend).__name__
                ),
                modem=None,
                status={},
                acquired_cells=frozenset(),
                failure_reason="modem_live_backend_acquisition_failed",
            )
        if not isinstance(modem, BackendAcquisitionReceipt):
            raise TypeError(
                "modem_live_backend_acquisition_receipt_required"
            )
        if modem.request != request:
            raise RuntimeError(
                "modem_live_backend_acquisition_request_mismatch"
            )
        base_status = modem.status_copy()
        if not modem.succeeded:
            if base_status.get("target_guard_ok") is False:
                reason = str(base_status.get("target_guard_reason") or "target_guard_rejected")
                raise TargetGuardError(f"Target guard failed during modem read: {reason}", reason=reason)
            return modem
        call_evidence = self._modem_io_gate.last_call_event()
        if (
            call_evidence.get("source") != access_source
            or call_evidence.get("operation") != operation
            or not bool(call_evidence.get("call_completed", False))
        ):
            raise RuntimeError(
                "modem_live_fast_read_call_evidence_missing"
            )
        duration_ms = float(call_evidence.get("duration_ms", 0.0) or 0.0)
        if not math.isfinite(duration_ms) or duration_ms < 0.0:
            raise RuntimeError(
                "modem_live_fast_read_duration_invalid"
            )
        self._last_fast_modem_read_ms = duration_ms
        lane_state.mark(AcquisitionLaneState.HEALTHY)
        return modem

    def _publish_live_register_snapshot(
        self,
        *,
        primary_receipt: BackendAcquisitionReceipt,
        source: str,
        backend_repair: ModemLiveSessionRepairReceipt | None,
    ) -> int:
        if (
            not isinstance(
                primary_receipt,
                BackendAcquisitionReceipt,
            )
            or not primary_receipt.succeeded
            or primary_receipt.modem is None
        ):
            raise ValueError(
                "live_register_primary_acquisition_receipt_required"
            )
        pending_receipts = self._pending_receipts_snapshot()
        receipts = self._coalesce_latest_acquisition_receipts(
            pending_receipts + (primary_receipt,)
        )
        modem, base_status = self._merge_acquisition_receipt_payloads(
            receipts,
            primary=primary_receipt,
        )
        attempt_id = primary_receipt.request.attempt_id
        # This generation consumes all ``receipts`` together. Timestamp it after
        # the newest physical read so slow data cannot appear newer than Matrix.
        ts = max(
            float(receipt.finished_at)
            for receipt in receipts
        )
        if backend_repair is not None:
            # Recovery completes after its readback. Date the combined result
            # now; keep each field's read time in its original receipt. Do not
            # copy the repair's time here: a genuinely future receipt must fail.
            ts = max(ts, float(time.time()))
        receipt_lines = frozenset(
            str(cell).partition(".")[0]
            for receipt in receipts
            for cell in receipt.acquired_cells
            if str(cell).partition(".")[0]
            in MODEM_LIVE_TRUTH_FIELD_CATALOG
        )
        notice: ModemLivePublicationNotice | None = None
        scout_capability_submission: tuple[
            Any,
            Mapping[str, Any],
        ] | None = None
        scout_profile_submission: tuple[
            Mapping[str, Any],
            LiveRegisterCommittedGeneration,
            Mapping[str, Any],
            float,
        ] | None = None
        with self._live_register_lock:
            previous = self._live_register_store.current_status()
            previous_ts = float(previous.sampled_at)
            previous_sequence = int(previous.sequence)
            first_generation = previous_sequence == 0
            identity_refresh = bool(
                first_generation or "identity" in receipt_lines
            )
            capability_refresh = bool(
                first_generation or "capability" in receipt_lines
                or any(receipt.request.lane == "startup_full" for receipt in receipts)
            )
            sample_sequence = previous_sequence + 1
            sample_interval_s = max(0.0, ts - previous_ts) if previous_ts > 0.0 else 0.0
            sample_expected_interval_s = self._live_register_expected_sample_interval_s()
            warmup_samples = max(
                0,
                int(
                    getattr(
                        self._live_access_profile(),
                        "fast_sample_slo_warmup_samples",
                        0,
                    )
                    or 0
                ),
            )
            sample_slo_warmup = bool(sample_sequence <= warmup_samples)
            sample_slo_exceeded = bool(
                previous_ts > 0.0
                and not sample_slo_warmup
                and sample_interval_s > sample_expected_interval_s
            )
            set_input_diagnostics(
                modem,
                {
                    "modem_sample_interval_s": float(sample_interval_s),
                    "modem_sample_expected_interval_s": float(sample_expected_interval_s),
                    "modem_sample_slo_exceeded": bool(sample_slo_exceeded),
                },
            )
            status = dict(base_status or {})
            runtime_capabilities = dict(self._cached_runtime_capabilities or {})
            if runtime_capabilities:
                status["runtime_capabilities"] = runtime_capabilities
            startup_baseline_status = dict(
                getattr(self, "_startup_live_baseline_status", {}) or {}
            )
            if startup_baseline_status:
                status["startup_live_baseline"] = startup_baseline_status
            runtime_identity_context = self._build_live_runtime_identity_context(
                modem=modem,
                base_status=status,
            )
            if bool(runtime_identity_context.get("session_changed", False)):
                raise RuntimeError("modem_live_in_process_device_replacement_forbidden")
            status.update(runtime_identity_context)
            store_replaced = self._bind_live_register_store_for_session(
                str(status.get("session_scope_key") or ""),
            )
            if store_replaced:
                raise RuntimeError("modem_live_in_process_namespace_replacement_forbidden")
            prepared_connectivity: PreparedConnectivityAttempt
            connectivity = self._ensure_connectivity_module()
            radio_availability_observation = (
                _fresh_radio_availability_observation_from_receipts(
                    receipts,
                )
            )
            try:
                prepared_connectivity = connectivity.prepare_physical_attempt(
                    succeeded=True,
                    now=ts,
                    attempt_context={
                        "physical_attempt_id": attempt_id,
                        "source_epoch": self._publication_source_epoch,
                        "source_generation": sample_sequence,
                        "session_scope_key": str(
                            status.get("session_scope_key") or ""
                        ),
                        "identity_key": str(
                            status.get("session_scope_key") or ""
                        ),
                        "identity_code": str(
                            status.get("identity_code")
                            or (
                                dict(status.get("modem_identity_key") or {}).get(
                                    "identity_code"
                                )
                                if isinstance(
                                    status.get("modem_identity_key"),
                                    Mapping,
                                )
                                else ""
                            )
                            or ""
                        ),
                    },
                    backend_repair=backend_repair,
                    radio_availability_observation=(
                        radio_availability_observation
                    ),
                )
            except Exception as exc:
                self._record_backend_fault(
                    "live_register_connectivity_success",
                    exc,
                )
                raise RuntimeError(
                    "physical_attempt_connectivity_receipt_failed"
                ) from exc
            connectivity_status = dict(prepared_connectivity.receipt)
            status["connectivity_receipt"] = dict(connectivity_status)
            session_transition = (
                dict(status.get("session_transition") or {})
                if isinstance(status.get("session_transition"), dict)
                else {}
            )
            if session_transition:
                session_transition.update(
                    {
                        "source_epoch": self._publication_source_epoch,
                        "source_sequence": int(sample_sequence),
                        "replacement_session_scope_key": str(
                            status.get("session_scope_key") or ""
                        ),
                    }
                )
                status["session_transition"] = session_transition
            access_profile = self._live_access_profile()
            identity_publication: Dict[str, Any] = {}
            selected_profile_record: Mapping[str, Any] = {}
            if identity_refresh or capability_refresh:
                identity_publication = build_modem_live_identity_publication(
                    status,
                    access_type_classifier_key=str(
                        getattr(access_profile, "profile_id", "") or ""
                    ),
                    matrix=self._identity_matrix,
                )
                if identity_refresh:
                    status.update(identity_publication)
            session_scope_key = str(
                runtime_identity_context.get("session_scope_key") or ""
            ).strip()
            if capability_refresh:
                if session_scope_key != self._session_band_capability_scope:
                    self._session_band_capability_scope = session_scope_key
                    self._session_band_capability_status = {}
                    self._session_band_capability_observed_at = ts
                    self._session_band_capability_reported_id = ""
                supported_fields = (
                    "supported_lte_bands",
                    "supported_lte_bands_source",
                    "supported_lte_bands_observed_at",
                    "netmode_list_supported_bands",
                    "supported_nr_bands",
                    "supported_nr_bands_source",
                    "supported_nr_bands_observed_at",
                    "netmode_list_supported_nr_bands",
                )
                for field_name in supported_fields:
                    value = status.get(field_name)
                    if value not in (None, "", []):
                        self._session_band_capability_status[field_name] = copy.deepcopy(
                            value
                        )
                capability_observed_at = max(
                    (
                        float(
                            self._session_band_capability_status.get(field_name, 0.0)
                            or 0.0
                        )
                        for field_name in (
                            "supported_lte_bands_observed_at",
                            "supported_nr_bands_observed_at",
                        )
                    ),
                    default=0.0,
                )
                if capability_observed_at <= 0.0:
                    capability_observed_at = self._session_band_capability_observed_at
                else:
                    self._session_band_capability_observed_at = capability_observed_at
                profile_id = str(identity_publication.get("profile_id") or "")
                selected_profile_record = identity_publication.get(
                    "selected_profile_record"
                )
                seed_profile_capability = (
                    dict(selected_profile_record)
                    if isinstance(selected_profile_record, Mapping)
                    else {}
                )
                effective_profile_capability = self._scout.effective_profile(
                    profile_id,
                    seed_profile_capability,
                )
                session_band_capability = build_session_band_capability(
                    session_scope_key=session_scope_key,
                    status=self._session_band_capability_status,
                    profile_record=seed_profile_capability,
                    observed_at=capability_observed_at,
                    profile_capability_evidence=effective_profile_capability,
                )
                scout_decision = self._scout.status()
                if session_band_capability.comparison_status in {
                    "mismatch",
                    "profile_unavailable",
                }:
                    scout_capability_submission = (
                        session_band_capability,
                        dict(identity_publication),
                    )
                status["session_band_capability"] = session_band_capability.as_dict()
                status.update(
                    build_explicit_capability_cells(
                        runtime_capabilities=(
                            status.get("runtime_capabilities")
                            if isinstance(status.get("runtime_capabilities"), Mapping)
                            else {}
                        ),
                        session_band_capability=status["session_band_capability"],
                        profile_record=seed_profile_capability,
                    )
                )
                capability_issue = session_band_capability.comparison_status in {
                    "actual_invalid",
                    "mismatch",
                }
                status["session_band_capability_issue"] = capability_issue
                if (
                    capability_issue
                    and session_band_capability.capability_id
                    != self._session_band_capability_reported_id
                ):
                    self._session_band_capability_reported_id = (
                        session_band_capability.capability_id
                    )
                    log.warning(
                        "Modem Live session band capability discrepancy: %s",
                        session_band_capability.as_dict(),
                    )
            status["modem_live_scout"] = dict(self._scout.status())
            if identity_refresh:
                status["modem_live_access_profile"] = dict(access_profile.as_dict())
                # Diagnostic provenance belongs to the existing opaque profile
                # metadata, not a new Matrix cell or consumer contract.
                definition = getattr(self._modem_backend, "_definition", None)
                if definition is not None:
                    status["modem_live_access_profile"]["profile_definition"] = definition.provenance()
            live_lane_status = self.protected_live_lane_status()
            status["acquisition_lane_state"] = dict(
                live_lane_status.get("acquisition_lane_state") or {}
            )
            status["modem_comm_alive"] = True
            modem_read_ms = float(self._last_fast_modem_read_ms or 0.0)
            if not math.isfinite(modem_read_ms) or modem_read_ms < 0.0:
                raise RuntimeError("modem_live_fast_read_duration_invalid")
            status["modem_read_ms"] = round(modem_read_ms, 3)
            status["modem_sample_interval_s"] = round(float(sample_interval_s), 3)
            status["modem_sample_expected_interval_s"] = round(float(sample_expected_interval_s), 3)
            status["modem_sample_slo_exceeded"] = bool(sample_slo_exceeded)
            capability_cells = (
                frozenset(
                    f"capability.{field}"
                    for field in MODEM_LIVE_TRUTH_FIELD_CATALOG["capability"]
                    if field in status
                )
                if capability_refresh
                else frozenset()
            )
            identity_cells = (
                frozenset(
                    f"identity.{field}"
                    for field in MODEM_LIVE_TRUTH_FIELD_CATALOG["identity"]
                    if field in status and field in DERIVED_IDENTITY_FIELDS
                )
                if identity_refresh
                else frozenset()
            )
            derived_cells = (
                DERIVED_HEALTH_CELLS
                | TIMING_CELLS
                | capability_cells
                | identity_cells
            ).difference(
                {
                    cell
                    for receipt in receipts
                    for cell in receipt.acquired_cells
                }
            )
            derived_fields = frozenset(
                cell.partition(".")[2]
                for cell in derived_cells
                if cell.partition(".")[2] in status
            )
            derived_cells = frozenset(
                cell
                for cell in derived_cells
                if cell.partition(".")[2] in derived_fields
            )
            if derived_cells:
                receipts = receipts + (
                    BackendAcquisitionReceipt(
                        request=primary_receipt.request,
                        backend=primary_receipt.backend,
                        profile_id=primary_receipt.profile_id,
                        finished_at=ts,
                        succeeded=True,
                        modem=None,
                        status={
                            field: copy.deepcopy(status[field])
                            for field in derived_fields
                        },
                        acquired_cells=derived_cells,
                    ),
                )
            receipts = receipts + _derived_traffic_unit_receipts(receipts)
            ca_pool_context = _derived_ca_pool_context_receipt(receipts)
            if ca_pool_context is not None:
                receipts = receipts + (ca_pool_context,)
            observation = RawModemObservation.capture(
                modem=modem,
                status=status,
                sampled_at=ts,
                source=str(source or "clocked_modem_register"),
                acquisition_attempt_id=attempt_id,
                manifest=ModemLiveAcquisitionManifest(
                    namespace=self._live_register_store.runtime_namespace,
                    backend=primary_receipt.backend,
                    profile_id=primary_receipt.profile_id,
                    source_lane=str(
                        source or "clocked_modem_register"
                    ),
                    receipts=receipts,
                ),
            )
            def _prepare_same_generation_notice(
                committed_generation: LiveRegisterCommittedGeneration,
            ) -> tuple[
                LiveRegisterCommittedGeneration,
                ModemLivePublicationNotice,
            ]:
                committed_matrix = committed_generation.matrix_reader()
                sampled_at = committed_matrix.metadata("sampled_at")
                published_at = max(float(sampled_at), float(time.time()))
                if type(sampled_at) is not float:
                    raise TypeError(
                        "modem_live_committed_matrix_timestamp_invalid"
                    )
                notice_source = committed_matrix.metadata("source")
                if (
                    type(notice_source) is not str
                    or not notice_source
                    or notice_source != notice_source.strip()
                ):
                    raise TypeError(
                        "modem_live_committed_publication_source_invalid"
                    )
                prepared_notice = ModemLivePublicationNotice(
                    matrix_identity=committed_generation.identity,
                    matrix_rtt=self._matrix_bound_rtt_observation(
                        matrix_identity=committed_generation.identity,
                        bound_at=published_at,
                    ),
                    sampled_at=sampled_at,
                    published_at=published_at,
                    source=notice_source,
                    truth_matrix=committed_matrix,
                )
                return committed_generation, prepared_notice

            def _commit_same_generation() -> tuple[
                LiveRegisterCommittedGeneration,
                ModemLivePublicationNotice,
            ]:
                committed_generation = (
                    self._live_register_store.commit_observation(
                        observation=observation,
                    )
                )
                return _prepare_same_generation_notice(
                    committed_generation
                )

            committed_generation, notice = (
                connectivity.commit_prepared_attempt(
                    prepared_connectivity,
                    commit=_commit_same_generation,
                )
            )
            if (
                not self._startup_publication_admission_open
                and self._startup_publication_notice is None
                and self._startup_publication_primary_receipt is None
            ):
                self._startup_publication_primary_receipt = (
                    committed_generation.identity,
                    primary_receipt,
                )
            self._live_register_connectivity_status = dict(
                connectivity_status
            )
            if selected_profile_record:
                scout_profile_submission = (
                    dict(selected_profile_record),
                    committed_generation,
                    dict(identity_publication),
                    float(notice.published_at),
                )
            self._live_register_last_reason = ""
            self._acknowledge_committed_acquisition_receipts(
                pending_receipts
            )
            already_warned = (
                int(getattr(self, "_last_modem_sample_slo_warning_sequence", 0) or 0)
                == sample_sequence
            )
            if sample_slo_exceeded and not already_warned:
                self._last_modem_sample_slo_warning_sequence = sample_sequence
        if sample_slo_exceeded and not already_warned:
            log.warning(
                "Modem Live sample interval exceeded diagnostic warning interval: interval=%.3fs expected=%.3fs "
                "sequence=%s source=%s",
                sample_interval_s,
                sample_expected_interval_s,
                sample_sequence,
                str(source or "clocked_modem_register"),
            )
        if notice is not None:
            self._emit_live_register_publication(notice)
        if scout_capability_submission is not None:
            capability, scout_identity = scout_capability_submission
            self._scout.submit_session_capability(
                capability,
                identity=scout_identity,
            )
        if scout_profile_submission is not None:
            profile, generation, scout_identity, observed_at = (
                scout_profile_submission
            )
            self._scout.submit_profile_matrix_difference(
                profile_record=profile,
                truth_matrix=generation.matrix_reader(),
                identity=scout_identity,
                observed_at=observed_at,
            )
        return int(sample_sequence)

    def _live_register_history_entry(self, snap: LiveModemRegisterSnapshot) -> Dict[str, Any]:
        del snap
        raise RuntimeError(
            "live_register_history_entry_forbidden:"
            "use_accepted_root_outbox"
        )

    def _live_register_has_fresh_cached_sample(self, *, now: float) -> bool:
        if not hasattr(self, "_live_register_lock"):
            return False
        with self._live_register_lock:
            current = self._live_register_store.current_status()
            return bool(
                current.available
                and current.sampled_at > 0.0
                and max(0.0, float(now) - current.sampled_at)
                <= float(self._live_register_stale_s or 0.0)
            )

    def _read_live_register_snapshot(
        self,
        *,
        now: float,
        intake_id: str = "engine_cycle",
    ) -> tuple[Optional[ModemInput], Dict[str, Any], Dict[str, Any]]:
        del now, intake_id
        raise RuntimeError(
            "live_register_snapshot_intake_forbidden:"
            "use_accepted_root_outbox"
        )

    def modem_connect(self) -> bool:
        return self._connect_backend_session_under_one_lock(
            source="operator_connect",
        )

    def startup_modem_connect(self) -> bool:
        reader=self._device_association_reader
        if self.input_cfg.modem_backend != 'model' and not callable(reader):
            raise TargetGuardError('The saved router identity reader is unavailable. '
                'No settings or History were changed.', reason='selected_device_association_reader_required')
        self._startup_device_associations=tuple(reader()) if callable(reader) else ()
        if self.input_cfg.modem_backend=='mikrotik':
            bind=getattr(self._modem_backend,'bind_startup_device_admission',None)
            if not callable(bind):
                raise TypeError('routeros_startup_device_admission_port_required')
            bind(lambda observed: self._require_selected_device_admission(observed, discovery=True))
        return self._connect_backend_session_under_one_lock(
            source="startup_connect",
        )

    def _require_selected_device_admission(self, observed: Mapping, *, discovery: bool = False) -> None:
        from arch4.modem_live.device_admission import require_selected_device_admission
        require_selected_device_admission(backend=self.input_cfg.modem_backend,
            selected_profile_id=self.input_cfg.modem_profile_id, observed=observed,
            associations=self._startup_device_associations,
            expected_serial=self.input_cfg.target_guard_serial,
            expected_imei=self.input_cfg.target_guard_imei, discovery=discovery)

    def _connect_backend_session_under_one_lock(
        self,
        *,
        source: str,
    ) -> bool:
        """Check and establish one backend session without an access race."""

        with self._ensure_io_lock():
            connected = self._call_modem(
                "is_connected",
                source=source,
                operation="is_connected",
                lock_policy="always",
            )
            if isinstance(connected, bool) and connected:
                return True
            return bool(
                self._call_modem(
                    "connect",
                    source=source,
                    operation="connect",
                    lock_policy="always",
                )
            )

    def modem_is_connected(self) -> bool:
        return bool(
            self._call_modem(
                "is_connected",
                source="operator_status",
                operation="is_connected",
                lock_policy="always",
            )
        )

    def modem_recovery_health_proof(self) -> Dict[str, Any]:
        proof = self._call_modem(
            "recovery_health_proof",
            source="recovery",
            operation="health_proof",
            lock_policy="always",
        ) or {}
        return dict(proof) if isinstance(proof, dict) else {}

    def routeros_watchdog_script_status(self, script_name: str = "lte_watchdog") -> Dict[str, Any]:
        status = self._call_modem(
            "routeros_watchdog_script_status",
            script_name,
            source="routeros_watchdog",
            operation="script_status",
            lock_policy="always",
        ) or {}
        return dict(status) if isinstance(status, dict) else {}

    def run_routeros_watchdog_script(self, script_name: str = "lte_watchdog") -> Dict[str, Any]:
        del script_name
        raise RuntimeError(
            "direct_routeros_watchdog_execution_illegal_use_typed_command"
        )

    def configure_routeros_watchdog_globals(self, values: Dict[str, Any]) -> Dict[str, Any]:
        del values
        raise RuntimeError(
            "direct_routeros_watchdog_configuration_illegal_use_typed_command"
        )

    def clear_routeros_watchdog_runtime_cache(self) -> None:
        with self._ensure_io_lock():
            backend = getattr(self, "_modem_backend", None)
            clear = getattr(
                backend,
                "clear_routeros_watchdog_runtime_cache",
                None,
            )
            if clear is not None:
                if not callable(clear):
                    raise TypeError(
                        "routeros_watchdog_runtime_cache_clear_not_callable"
                    )
                clear()

    def bind_config_generation(
        self,
        config: Any,
        *,
        generation_id: str,
        config_fingerprint: str,
    ) -> None:
        """Bind derived Modem Live caches to the active Core configuration."""

        if config is None:
            raise TypeError("modem_live_config_generation_config_required")
        generation = str(generation_id or "").strip()
        fingerprint = str(config_fingerprint or "").strip()
        if not generation or not fingerprint:
            raise ValueError("modem_live_config_generation_identity_required")
        self.clear_routeros_watchdog_runtime_cache()
        self._active_config_generation_id = generation
        self._active_config_fingerprint = fingerprint

    def routeros_at_command(self, cmd: str, *, wait_all_output: bool = False) -> str:
        del cmd, wait_all_output
        raise RuntimeError(
            "direct_routeros_at_execution_illegal_use_typed_command"
        )

    def zte_goform_read(self, fields: Any, *, multi_data: bool = True) -> Dict[str, Any]:
        del fields, multi_data
        raise RuntimeError(
            "direct_zte_goform_read_forbidden:use_typed_backend_acquisition"
        )

    def backend_debug_state(self) -> Dict[str, Any]:
        with self._io_lock:
            try:
                cookies = {}
                session = getattr(self._modem_backend, "_session", None)
                jar = getattr(session, "cookies", None)
                get_dict = getattr(jar, "get_dict", None)
                if callable(get_dict):
                    cookies = dict(get_dict() or {})
                return {
                    "authenticated": getattr(
                        self._modem_backend,
                        "_authenticated",
                        None,
                    ),
                    "last_status": dict(
                        getattr(self._modem_backend, "_last_status", {}) or {}
                    ),
                    "last_raw": dict(
                        getattr(self._modem_backend, "_last_raw", {}) or {}
                    ),
                    "cookies": cookies,
                }
            except Exception as exc:
                self._record_backend_fault("backend_debug_state", exc)
                log.debug("LIVE MODULE backend_debug_state suppressed: %s", exc)
        return {}

    def modem_disconnect(self) -> None:
        self._call_modem(
            "disconnect",
            source="operator_disconnect",
            operation="disconnect",
            lock_policy="always",
        )

    def get_last_status(self) -> Dict[str, Any]:
        status = self._call_modem(
            "get_last_status",
            source="status_snapshot",
            operation="get_last_status",
            lock_policy="always",
        ) or {}
        return dict(status) if isinstance(status, dict) else {}

    def get_current_bands(self) -> List[int]:
        raise RuntimeError(
            "direct_current_band_read_illegal_use_committed_matrix"
        )

    def set_wireless_radio_result(self, *_: Any, **__: Any) -> None:
        raise RuntimeError(
            "direct_wifi_radio_execution_illegal_use_core_command_gateway"
        )

    def _matrix_confirms_pband_anchor(
        self,
        *,
        pband: int,
        minimum_sequence: int,
    ) -> bool:
        """Read committed Matrix values during a compound PBAND command."""

        if pband <= 0:
            return True
        reader = self.committed_live_generation().matrix_reader()
        if int(reader.metadata("sequence") or 0) <= int(minimum_sequence):
            return False
        health = reader.line_values("health")
        if not bool(health.get("radio_connected", False)):
            return False
        radio = reader.line_values("radio")
        selected = normalize_band_list(radio.get("selected_band_set") or [])
        primary = int(radio.get("serving_band", 0) or 0)
        return selected == [pband] or (
            primary == pband and (not selected or pband in selected)
        )

    def _wait_for_matrix_pband_anchor(
        self,
        *,
        pband: int,
        minimum_sequence: int,
        minimum_quiet_s: float,
        confirmation_timeout_s: float,
        physical_stage: PhysicalCommandDispatchEvidence,
    ) -> bool:
        if not isinstance(physical_stage, PhysicalCommandDispatchEvidence):
            raise TypeError("pband_anchor_physical_stage_required")
        if (
            physical_stage.stage_kind != "pband_anchor"
            or not physical_stage.permits_target_verification
        ):
            raise ValueError("pband_anchor_accepted_stage_required")
        quiet_s = max(0.0, float(minimum_quiet_s or 0.0))
        if quiet_s > 0.0:
            # Huawei HiLink may confirm PBAND before its radio is ready for a read
            # or CA-pool write. The profile requires this physical quiet period.
            # It is not a Core or Matrix delay.
            time.sleep(quiet_s)
        deadline = time.time() + max(
            0.0,
            float(confirmation_timeout_s or 0.0),
        )
        while True:
            refresh = self._refresh_live_register_once_from_source(
                source="priority_event_write:pband_anchor",
                post_write_stage=physical_stage,
            )
            if bool((refresh or {}).get("target_evidence_latched", False)):
                # Huawei reports the configured target separately from RF data.
                # This interstage sample writes the saved target to Matrix while
                # the compound-write lock stays held. Matrix must advance before
                # the CA-pool write.
                self._refresh_live_register_once_from_source(
                    source="compound_event_write_sample:pband_anchor",
                )
            try:
                if self._matrix_confirms_pband_anchor(
                    pband=pband,
                    minimum_sequence=minimum_sequence,
                ):
                    return True
            except Exception as exc:
                self._record_backend_fault("pband_matrix_stage_confirmation", exc)
            remaining = deadline - time.time()
            if remaining <= 0.0:
                return False
            time.sleep(min(0.25, remaining))

    def _traffic_presence_state(self) -> Dict[str, Any]:
        with self._traffic_presence_lock:
            return {
                "active": bool(self._traffic_presence_active),
                "interval_s": float(self._traffic_presence_interval_s or 0.0),
                "target": str(self._traffic_presence_target or self.input_cfg.rtt_probe_target or "8.8.8.8"),
            }

    def _stop_runtime_traffic_presence(self, *, join_timeout_s: float = 0.3) -> bool:
        with self._traffic_presence_lifecycle_lock:
            thread: Optional[threading.Thread] = None
            stop_event: Optional[threading.Event] = None
            with self._traffic_presence_lock:
                self._traffic_presence_active = False
                self._traffic_presence_interval_s = 0.0
                thread = self._traffic_presence_thread
                stop_event = self._traffic_presence_stop
            if stop_event is not None:
                stop_event.set()
            if thread is not None and thread.is_alive():
                thread.join(timeout=join_timeout_s)
            stopped = thread is None or not thread.is_alive()
            if stopped:
                with self._traffic_presence_lock:
                    if self._traffic_presence_thread is thread:
                        self._traffic_presence_thread = None
                        self._traffic_presence_stop = None
            return stopped

    def _runtime_traffic_presence_worker(
        self,
        *,
        stop_event: threading.Event,
        interval_s: float,
        target: str,
    ) -> None:
        current = threading.current_thread()
        try:
            first = True
            while (
                not stop_event.is_set()
                and not self._modem_live_shutdown.is_set()
            ):
                if not first and stop_event.wait(interval_s):
                    break
                first = False
                try:
                    self._probe_rtt_once(
                        source="traffic_presence",
                        operation="keepalive_rtt",
                        target=target,
                        count=1,
                        publish_observation=False,
                    )
                except Exception as exc:
                    log.debug("runtime traffic presence probe suppressed: %s", exc)
        finally:
            with self._traffic_presence_lock:
                if self._traffic_presence_thread is current:
                    self._traffic_presence_active = False
                    self._traffic_presence_thread = None
                    self._traffic_presence_stop = None

    def _execute_runtime_traffic_presence(self, action: LeverAction) -> LeverResult:
        with self._traffic_presence_lifecycle_lock:
            return self._execute_runtime_traffic_presence_locked(action)

    def _execute_runtime_traffic_presence_locked(
        self,
        action: LeverAction,
    ) -> LeverResult:
        params = action.params if isinstance(action.params, dict) else {}
        try:
            interval_s = float(params.get("keepalive_interval_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            interval_s = 0.0
        target = str(params.get("target") or self.input_cfg.rtt_probe_target or "8.8.8.8").strip()
        if not target:
            target = "8.8.8.8"

        if interval_s <= 0.0:
            stopped = self._stop_runtime_traffic_presence()
            return LeverResult(
                success=bool(stopped),
                lever=action.lever,
                error=(
                    "traffic_presence_worker_stop_timeout"
                    if not stopped
                    else ""
                ),
            )

        interval_s = max(1.0, interval_s)
        with self._traffic_presence_lock:
            current_active = bool(self._traffic_presence_active)
            current_interval = float(self._traffic_presence_interval_s or 0.0)
            current_target = str(self._traffic_presence_target or "")
            current_thread = self._traffic_presence_thread
            if (
                current_active
                and current_thread is not None
                and current_thread.is_alive()
                and abs(current_interval - interval_s) < 0.01
                and current_target == target
            ):
                return LeverResult(success=True, lever=action.lever)

        if not self._stop_runtime_traffic_presence():
            return LeverResult(
                success=False,
                lever=action.lever,
                error="traffic_presence_previous_worker_still_alive",
            )
        if self._modem_live_shutdown.is_set():
            return LeverResult(
                success=False,
                lever=action.lever,
                error="modem_live_shutdown_active",
            )
        stop_event = threading.Event()
        worker = threading.Thread(
            target=self._runtime_traffic_presence_worker,
            kwargs={
                "stop_event": stop_event,
                "interval_s": interval_s,
                "target": target,
            },
            daemon=True,
            name="arch4-traffic-presence",
        )
        with self._traffic_presence_lock:
            self._traffic_presence_active = True
            self._traffic_presence_interval_s = interval_s
            self._traffic_presence_target = target
            self._traffic_presence_stop = stop_event
            self._traffic_presence_thread = worker
        try:
            worker.start()
        except Exception:
            with self._traffic_presence_lock:
                if self._traffic_presence_thread is worker:
                    self._traffic_presence_active = False
                    self._traffic_presence_thread = None
                    self._traffic_presence_stop = None
            raise
        return LeverResult(success=True, lever=action.lever)

    def _execute_physical_stage(
        self,
        plan: ModemLivePhysicalStagePlan,
        *,
        source: str,
    ) -> ModemLivePhysicalStageResult:
        result = self._call_modem(
            "execute_physical_stage",
            plan,
            source=source,
            operation=f"execute_{plan.stage_kind}",
            lock_policy="action",
        )
        if not isinstance(result, ModemLivePhysicalStageResult):
            raise TypeError("modem_live_backend_physical_stage_result_required")
        if result.plan != plan:
            raise RuntimeError("modem_live_backend_physical_stage_plan_mismatch")
        if self._active_device_identity:
            self._live_register_store.note_configuration_write(result.dispatch_evidence(),
                namespace=self._live_register_store.runtime_namespace)
        return result

    def _physical_command_coordination_profile(self) -> str:
        """Select a typed physical-write plan from Modem Live access data."""

        access_profile = self._live_access_profile()
        if not bool(
            getattr(access_profile, "direct_modem_writes_allowed", False)
        ):
            raise RuntimeError(
                "modem_live_access_profile_forbids_physical_writes"
            )
        family = str(
            getattr(access_profile, "backend_family", "") or ""
        ).strip()
        coordination_by_family = {
            "huawei_hilink_like": "direct",
            "routeros_at_like": "routeros",
            "openwrt_modemmanager_like": "openwrt",
            "zte_goform_like": "zte_goform",
            "zte_zwrt_ubus_like": "zte_zwrt",
            "tplink_dev2_like": "tplink_dev2",
            "xiaomi_miwifi_like": "direct",
            "zyxel_webui_like": "direct",
            "modem_model_like": "direct",
        }
        coordination_profile = coordination_by_family.get(family)
        if coordination_profile is None:
            raise RuntimeError(
                "modem_live_physical_coordination_profile_unclassified:"
                f"{family or '<empty>'}"
            )
        return coordination_profile

    def _expected_interruption_seconds(self, action: LeverAction) -> float:
        """Return the interruption window from the selected profile."""

        access_profile = self._live_access_profile()
        profile_quiet_s = max(
            0.0,
            float(
                getattr(
                    access_profile,
                    "event_write_min_quiet_s",
                    0.0,
                )
                or 0.0
            ),
        )
        is_restart = bool(
            action.lever is LeverType.RESTART
            or (
                action.lever is LeverType.UE_CAPABILITY
                and isinstance(action.params, Mapping)
                and action.params.get("restart") is True
            )
        )
        if is_restart:
            return max(
                profile_quiet_s,
                float(
                    getattr(self.cfg, "unstable_action_lockout_s", 45.0)
                    or 45.0
                ),
                float(
                    getattr(self.cfg, "restart_expected_interruption_s", 75.0)
                    or 75.0
                ),
            )
        family = str(
            getattr(access_profile, "backend_family", "") or ""
        ).strip()
        configured_name = (
            "mikrotik_radio_settle_quiet_s"
            if family == "routeros_at_like"
            else "band_selection_radio_quiet_s"
        )
        configured_default = 12.0 if family == "routeros_at_like" else 8.0
        configured = max(
            0.0,
            float(
                getattr(
                    self.cfg,
                    configured_name,
                    profile_quiet_s or configured_default,
                )
                or 0.0
            ),
        )
        return max(profile_quiet_s, configured)

    def _confirm_native_auto_release(self, stage: ModemLivePhysicalStageResult) -> ModemLivePhysicalStageResult:
        """Huawei's explicit native-read adapter for a lost AUTO POST reply.

        Preserve the actual transport outcome. Only a fresh registered native
        AUTO configuration proves release; a cached empty pool cannot do so.
        Other native dialects retain their qualified transport requirement.
        """
        profile = self._live_access_profile()
        if (stage.outcome != "response_unavailable"
                or not isinstance(stage.plan.target, CoreBandAutoTarget)
                or stage.plan.target.domain != "lte_allocated_auto"
                or profile.backend_family != "huawei_hilink_like"):
            return stage
        deadline = stage.transport_returned_at + float(profile.event_write_min_quiet_s)
        interval = max(float(profile.post_write_readback_delay_s), float(profile.fast_live_interval_s))
        while time.time() < deadline:
            if self._modem_live_shutdown.wait(min(interval, max(0., deadline-time.time()))):
                return stage
            if time.time() >= deadline:
                return stage
            request = self._new_backend_acquisition_request(lane="post_write", source="native_auto_release")
            receipt = self._read_modem_register_direct(access_source="readback",
                post_write_stage=stage.dispatch_evidence(), request=request)
            if not receipt.succeeded:
                continue
            native = receipt.status_copy()
            native_proof = (native.get("backend_diagnostic") or {}).get("native_auto_release_readback") or {}
            if (native_proof.get("native_auto") is not True
                    or native_proof.get("acquisition_attempt_id") != receipt.request.attempt_id):
                continue  # A pre-settlement read does not close the existing window.
            proof = dict(schema="NativeAutoReadback.v1", owner="modem_live",
                domain=stage.plan.target.domain, started_at=receipt.request.started_at,
                finished_at=receipt.finished_at, source="huawei.api.net.net-mode",
                native_auto=True, acquisition_attempt_id=receipt.request.attempt_id)
            return replace(stage, detail={**dict(stage.detail or {}), "native_auto_readback": proof})
        return stage

    def _wait_for_radio_mode_stage(self, plan, previous):
        """Settle one acknowledged GoForm stage within its existing profile window."""
        profile = self._live_access_profile()
        delay = max(0., float(profile.post_write_readback_delay_s))
        deadline = previous.transport_returned_at + max(0., float(profile.post_write_readback_deadline_s))
        next_read = previous.transport_returned_at + delay
        while not self._modem_live_shutdown.is_set():
            now = time.time()
            if now >= deadline:
                return False
            if now < next_read:
                if self._modem_live_shutdown.wait(min(next_read - now, deadline - now)):
                    return False
                continue  # Early wakeups do not shorten the selected quiet time.
            read_started = time.time()
            ready = self._call_modem("radio_mode_stage_ready", plan, source="readback",
                operation="radio_mode_interstage_readback", lock_policy="action")
            if isinstance(ready, BackendAcquisitionReceipt):
                native = ready.status_copy()
                if (plan.stage_kind != "radio_reconnect" or plan.ordinal != 3
                        or previous.plan.stage_kind != "radio_mode" or previous.outcome != "accepted"
                        or ready.backend != self.input_cfg.modem_backend
                        or ready.profile_id != self.input_cfg.modem_profile_id
                        or not ready.succeeded or ready.failure_reason or ready.modem is not None
                        or ready.request.lane != "post_write"
                        or ready.request.started_at < max(read_started, previous.transport_returned_at)
                        or not ready.request.started_at <= ready.finished_at <= time.time()
                        or ready.acquired_cells != frozenset({"slow.radio_mode"})
                        or set(native) != {"radio_mode", "data_session_connected"}
                        or native["data_session_connected"] is not True
                        or native["radio_mode"] != plan.target.configuration()):
                    raise RuntimeError("radio_mode_native_data_session_proof_invalid")
                return ready if not self._modem_live_shutdown.is_set() and time.time() <= deadline else False
            if ready is True:
                return not self._modem_live_shutdown.is_set() and time.time() <= deadline
            next_read = time.time() + max(delay, .01)
        return False

    def _manual_nr_mode_target(self, action: LeverAction, target: CoreCommandTarget) -> CoreRadioModeTarget | None:
        if (not isinstance(target, CoreNrBandTarget) or not target.nr_bands
                or target.release_manual or action.intent != "manual"):
            return None
        # The accepted Matrix carries the selected firmware's permission;
        # generic backend defaults intentionally do not qualify its siblings.
        permission = capability_contract_action_meta(self.runtime_capabilities_snapshot(), "radio_mode") or {}
        if permission.get("state") != "supported" or permission.get("write_allowed") is not True:
            return None
        observed = self.committed_live_generation().matrix_reader().line_values("slow").get("radio_mode")
        if observed is None:
            return None
        configuration = validate_radio_mode_observation(observed)
        if configuration["mode"] != "lte_only":
            return None
        key = None
        if "mode_key" in configuration:
            backend = getattr(self, "_modem_backend", None)
            preferred = getattr(backend, "manual_nr_mode_preferred_key", None)
            key = (self._call_modem("manual_nr_mode_preferred_key", configuration,
                       source="runtime_action", operation="manual_nr_mode_preference",
                       lock_policy="action") if callable(preferred)
                   else getattr(backend, "radio_mode_preferred_key", None))
            if not key:
                return None  # No qualified default: retain the existing requested band stage only.
        return CoreRadioModeTarget.transition(configuration, mode="nsa_preferred", mode_key=key)

    def _execute_lever_result(
        self,
        action: LeverAction,
        *,
        event_write_token: str,
        command_target: CoreCommandTarget,
        transaction_root_id: str,
        request_id: str,
        network_progress=None,
    ) -> LeverResult:
        retained_token = self._require_event_write_token(event_write_token)
        self._ensure_access_scheduler().record_run(
            "event_write",
            now=time.time(),
            reason=f"execute_{command_target.command_kind.lower()}",
            requested=True,
            due=True,
            metadata={"lever": command_target.command_kind},
        )
        try:
            if isinstance(action, LeverAction) and action.lever == LeverType.TRAFFIC_PRESENCE:
                caps = self.get_modem_capabilities()
                owner = str(getattr(caps, "traffic_presence_owner", "runtime_54guc") or "runtime_54guc").strip().lower()
                if owner != "modem_native":
                    local = self._execute_runtime_traffic_presence(action)
                    details = dict(local.details or {})
                    details["modem_live_no_physical_command"] = True
                    return LeverResult(
                        success=local.success,
                        lever=local.lever,
                        executed_at=local.executed_at,
                        error=local.error,
                        radio_drop_detected=False,
                        recovery_time_s=0.0,
                        details=details,
                    )

            coordination_profile = self._physical_command_coordination_profile()
            nr_enable_mode = (self._manual_nr_mode_target(action, command_target)
                if isinstance(command_target, CoreNrBandTarget) and not command_target.release_manual
                and action.intent == "manual" else None)
            radio_mode_disconnect = False
            if isinstance(command_target, CoreRadioModeTarget) and coordination_profile == "zte_goform":
                prepare = getattr(self._modem_backend, "prepare_radio_mode_stages", None)
                if not callable(prepare):
                    raise RuntimeError("radio_mode_connection_preparation_required")
                radio_mode_disconnect = self._call_modem(
                    "prepare_radio_mode_stages", command_target, source="runtime_action",
                    operation="prepare_radio_mode", lock_policy="action")
                if type(radio_mode_disconnect) is not bool:
                    raise RuntimeError("radio_mode_connection_preparation_invalid")
            plans = physical_stage_plans(
                request_id=request_id,
                transaction_root_id=transaction_root_id,
                event_write_token=retained_token,
                target=command_target,
                coordination_profile=coordination_profile,
                nr_enable_mode=nr_enable_mode,
                radio_mode_disconnect=radio_mode_disconnect,
            )
            matrix_sequence_before = 0
            try:
                matrix_sequence_before = int(
                    self.committed_live_generation()
                    .matrix_reader()
                    .metadata("sequence")
                    or 0
                )
            except Exception:
                matrix_sequence_before = 0
            stage_results: list[ModemLivePhysicalStageResult] = []
            failure_reasons: list[str] = []
            failure_disposition: (
                PhysicalCommandFailureDisposition | None
            ) = None
            recovery_pause_accepted = False
            for plan in plans:
                is_recovery_resume = (
                    plan.stage_kind == "device_local_recovery_resume"
                )
                if failure_reasons and not is_recovery_resume:
                    continue
                if is_recovery_resume and not recovery_pause_accepted:
                    continue
                if radio_mode_disconnect and plan.stage_kind in {"radio_mode", "radio_reconnect"}:
                    disconnect = stage_results[0] if stage_results else None
                    previous = stage_results[-1] if stage_results else None
                    if (disconnect is None or disconnect.outcome != "accepted"
                            or (plan.stage_kind == "radio_reconnect" and (
                                previous is None or previous.plan.stage_kind != "radio_mode"
                                or previous.outcome != "accepted"))):
                        reason = "radio_mode_connection_stage_unconfirmed"
                        failure_reasons.append(reason)
                        failure_disposition = PhysicalCommandFailureDisposition(
                            phase="pre_dispatch", reason=reason, blocked_stage_kind=plan.stage_kind,
                            blocked_ordinal=plan.ordinal, after_stage_ordinal=plan.ordinal - 1)
                        continue
                    plan = replace(plan, preceding_disconnect=disconnect.dispatch_evidence(),
                        preceding_radio_mode=(previous.dispatch_evidence()
                            if plan.stage_kind == "radio_reconnect" else None))
                    try:
                        settled = self._wait_for_radio_mode_stage(plan, previous)
                        reason = "radio_mode_native_settlement_timeout_or_shutdown"
                    except TargetGuardError:
                        raise
                    except (RuntimeError, ConnectionError, TimeoutError) as exc:
                        settled, reason = False, str(exc) or type(exc).__name__
                    if not settled:
                        failure_reasons.append(reason)
                        failure_disposition = PhysicalCommandFailureDisposition(
                            phase="pre_dispatch", reason=reason, blocked_stage_kind=plan.stage_kind,
                            blocked_ordinal=plan.ordinal, after_stage_ordinal=plan.ordinal - 1)
                        continue
                    if isinstance(settled, BackendAcquisitionReceipt):
                        # These are the two writes actually sent. The firmware's
                        # fresh connected state replaces no write or native ACK.
                        proof = dict(schema="NativeDataSessionReadback.v1", owner="modem_live",
                            started_at=settled.request.started_at, finished_at=settled.finished_at,
                            source=settled.backend + ":post_write", profile_id=settled.profile_id,
                            acquisition_attempt_id=settled.request.attempt_id,
                            configuration=settled.status_copy()["radio_mode"], data_session_connected=True)
                        stage_results[-1] = replace(previous,
                            detail={**dict(previous.detail or {}), "native_data_session_readback": proof})
                        break
                if nr_enable_mode is not None and isinstance(plan.target, CoreNrBandTarget):
                    preceding = stage_results[-1] if stage_results else None
                    if preceding is None or preceding.outcome != "accepted":
                        reason = "nr_mode_response_unconfirmed_no_band_write"
                        failure_reasons.append(reason)
                        failure_disposition = PhysicalCommandFailureDisposition(
                            phase="pre_dispatch", reason=reason,
                            blocked_stage_kind=plan.stage_kind, blocked_ordinal=plan.ordinal,
                            after_stage_ordinal=plan.ordinal - 1)
                        continue
                    plan = replace(plan, preceding_mode=preceding.dispatch_evidence())
                if isinstance(plan.target, (CoreBandSetTarget, CoreNrBandTarget)) and plan.target.release_manual:
                    preceding = stage_results[-1] if stage_results else None
                    if (preceding is not None and preceding.plan.stage_kind == 'band_auto'
                            and preceding.outcome == 'response_unavailable'):
                        preceding = self._confirm_native_auto_release(preceding)
                        stage_results[-1] = preceding
                    if (preceding is None or preceding.plan.stage_kind != 'band_auto'
                            or not preceding.dispatch_evidence().native_auto_release_confirmed):
                        reason = 'native_auto_response_unconfirmed_no_pool_write'
                        failure_reasons.append(reason)
                        failure_disposition = PhysicalCommandFailureDisposition(
                            phase='pre_dispatch', reason=reason,
                            blocked_stage_kind=plan.stage_kind, blocked_ordinal=plan.ordinal,
                            after_stage_ordinal=plan.ordinal - 1,
                        )
                        continue
                    plan = replace(plan, preceding_auto=preceding.dispatch_evidence())
                if (
                    plan.stage_kind == "ca_pool"
                    and isinstance(command_target, CorePBandTarget)
                ):
                    pband_stage = next(
                        (
                            row.dispatch_evidence()
                            for row in reversed(stage_results)
                            if row.plan.stage_kind == "pband_anchor"
                            and row.permits_target_verification
                        ),
                        None,
                    )
                    if pband_stage is None:
                        raise RuntimeError(
                            "pband_anchor_physical_stage_evidence_missing"
                        )
                    access_profile = self._live_access_profile()
                    wait_s = max(
                        0.0,
                        float(
                            getattr(
                                access_profile,
                                "post_write_readback_delay_s",
                                0.0,
                            )
                            or 0.0
                        ),
                    )
                    confirmation_timeout_s = max(
                        0.0,
                        float(
                            getattr(
                                access_profile,
                                "post_write_readback_deadline_s",
                                0.0,
                            )
                            or 0.0
                        ),
                    )
                    backend_family = str(
                        getattr(access_profile, "backend_family", "") or ""
                    ).strip().lower()
                    huawei_hilink = backend_family == "huawei_hilink_like"
                    routeros = coordination_profile == "routeros"
                    tplink_best_effort = (
                        getattr(access_profile, "profile_id", "")
                        == "nx520_srba1_250902_dev2_access_v2"
                    )
                    if huawei_hilink or routeros or tplink_best_effort:
                        # These PBAND anchors are stages of one physical command,
                        # not separate Matrix targets. The profile defines the
                        # protected wait between stages. Core verifies only the
                        # final PBAND and CA-pool result.
                        time.sleep(wait_s)
                        anchor_confirmed = True
                    else:
                        anchor_confirmed = self._wait_for_matrix_pband_anchor(
                            pband=command_target.pband,
                            minimum_sequence=matrix_sequence_before,
                            minimum_quiet_s=0.0,
                            confirmation_timeout_s=confirmation_timeout_s,
                            physical_stage=pband_stage,
                        )
                    if not anchor_confirmed:
                        interstage_reason = (
                            "pband_anchor_matrix_confirmation_timeout"
                        )
                        failure_reasons.append(interstage_reason)
                        failure_disposition = (
                            PhysicalCommandFailureDisposition(
                                phase="interstage_confirmation",
                                reason=interstage_reason,
                                blocked_stage_kind=plan.stage_kind,
                                blocked_ordinal=plan.ordinal,
                                after_stage_ordinal=plan.ordinal - 1,
                            )
                        )
                        continue
                if isinstance(command_target, CoreNetworkSettingsTarget):
                    if not callable(network_progress):
                        raise RuntimeError("network_progress_owner_required")
                    network_progress(plan, coordination_profile)
                try:
                    stage_result = self._execute_physical_stage(
                        plan,
                        source="runtime_action",
                    )
                except TargetGuardError:
                    raise
                except (RuntimeError, ConnectionError, TimeoutError) as exc:
                    reason = str(exc).strip() or type(exc).__name__
                    if reason.startswith(
                        (
                            "modem_live_backend_method_required:",
                            "modem_live_backend_physical_stage_plan_mismatch",
                        )
                    ):
                        raise
                    failure_reasons.append(reason)
                    if failure_disposition is None:
                        failure_disposition = PhysicalCommandFailureDisposition(
                            phase="pre_dispatch",
                            reason=reason,
                            blocked_stage_kind=plan.stage_kind,
                            blocked_ordinal=plan.ordinal,
                            after_stage_ordinal=plan.ordinal - 1,
                        )
                    continue
                stage_results.append(stage_result)
                if (radio_mode_disconnect and stage_result.outcome == "response_unavailable"
                        and plan.ordinal < len(plans)):
                    failure_reasons.append(stage_result.reason)
                    next_plan = plans[plan.ordinal]
                    failure_disposition = PhysicalCommandFailureDisposition(
                        phase="pre_dispatch", reason=stage_result.reason,
                        blocked_stage_kind=next_plan.stage_kind, blocked_ordinal=next_plan.ordinal,
                        after_stage_ordinal=plan.ordinal)
                if (isinstance(command_target, CoreNetworkSettingsTarget)
                        and coordination_profile in {"zte_zwrt", "tplink_dev2"}
                        and stage_result.outcome == "response_unavailable"
                        and plan.ordinal < len(plans)):
                    # An unacknowledged selection must not open mobile data on
                    # an unverified operator. Existing partial-write inspection
                    # resolves what changed; no automatic resend or alternate.
                    failure_reasons.append(stage_result.reason)
                    next_plan = plans[plan.ordinal]
                    failure_disposition = PhysicalCommandFailureDisposition(
                        phase="pre_dispatch", reason=stage_result.reason,
                        blocked_stage_kind=next_plan.stage_kind,
                        blocked_ordinal=next_plan.ordinal, after_stage_ordinal=plan.ordinal)
                if plan.stage_kind == "device_local_recovery_pause":
                    recovery_pause_accepted = bool(
                        stage_result.permits_target_verification
                    )
                if not stage_result.permits_target_verification:
                    failure_reasons.append(stage_result.reason)
                    if plan.stage_kind == "device_local_recovery_pause":
                        break
            accepted = bool(
                not failure_reasons
                and (len(stage_results) == len(plans) or (
                    radio_mode_disconnect and len(plans) == 3 and len(stage_results) == 2
                    and stage_results[-1].dispatch_evidence().native_data_session_confirmed))
                and all(
                    row.permits_target_verification
                    for row in stage_results
                )
            )
            error = (
                ";".join(failure_reasons)
                if failure_reasons
                else (
                    ""
                    if accepted
                    else "modem_live_physical_stage_not_executed"
                )
            )
            return LeverResult(
                success=accepted,
                lever=action.lever,
                executed_at=(
                    stage_results[0].request_dispatched_at
                    if stage_results
                    else 0.0
                ),
                error=error,
                radio_drop_detected=any(
                    row.radio_drop_detected for row in stage_results
                ),
                recovery_time_s=sum(
                    row.recovery_time_s for row in stage_results
                ),
                details={
                    "modem_live_physical_stages": tuple(
                        row.dispatch_evidence() for row in stage_results
                    ),
                    "modem_live_stage_results": tuple(
                        {
                            "stage_kind": row.plan.stage_kind,
                            "ordinal": row.plan.ordinal,
                            "reason": row.reason,
                            "detail": dict(row.detail or {}),
                        }
                        for row in stage_results
                    ),
                    "modem_live_failure_disposition": failure_disposition,
                },
            )
        finally:
            self._require_event_write_token(retained_token)

    def execute_lever(
        self,
        action: LeverAction,
        *,
        command_target: CoreCommandTarget,
        transaction_root_id: str,
        request_id: str,
        event_write_token: str,
        network_progress=None,
    ) -> LeverResult:
        """Execute once and attach the typed physical receipt."""

        if not isinstance(action, LeverAction):
            raise TypeError("modem_live_execute_lever_action_required")
        unsupported_reason = runtime_action_unsupported_reason(
            action.lever, profile_id=self.input_cfg.modem_profile_id
        )
        if unsupported_reason:
            raise RuntimeError(unsupported_reason)
        if not isinstance(command_target, CORE_COMMAND_TARGET_TYPES):
            raise TypeError("modem_live_execute_lever_typed_target_required")
        if command_target.command_kind != action.lever.name:
            raise ValueError("modem_live_execute_lever_target_mismatch")
        root_id = str(transaction_root_id or "").strip()
        command_id = str(request_id or "").strip()
        if not root_id or not command_id:
            raise ValueError("modem_live_execute_lever_identity_required")
        retained_token = self._require_event_write_token(event_write_token)
        execution_started_at = time.time()
        try:
            # One Core action is one physical transaction. Keep the Modem Live
            # I/O lock through every stage and required quiet period. Nested
            # calls use the same RLock. SMS and other modem access must wait.
            with self._ensure_io_lock():
                if action_expects_modem_interruption(action):
                    self._ensure_connectivity_module(
                    ).open_expected_interruption(
                        operation_id=root_id,
                        lever=action.lever.name,
                        duration_s=self._expected_interruption_seconds(action),
                        reason="physical_radio_action",
                        now=execution_started_at,
                    )
                result = self._execute_lever_result(
                    action,
                    event_write_token=retained_token,
                    command_target=command_target,
                    transaction_root_id=root_id,
                    request_id=command_id,
                    network_progress=network_progress,
                )
        finally:
            if action_expects_modem_interruption(action):
                self._ensure_connectivity_module(
                ).complete_expected_interruption_operation(
                    operation_id=root_id,
                    now=time.time(),
                )
        transaction_completed_at = time.time()
        if not isinstance(result, LeverResult):
            raise TypeError("modem_live_execute_lever_result_required")
        details = (
            dict(result.details)
            if isinstance(result.details, Mapping)
            else {}
        )
        if type(result.success) is not bool:
            raise TypeError("modem_live_execute_lever_success_bool_required")
        raw_stages = details.get("modem_live_physical_stages")
        stages: list[PhysicalCommandDispatchEvidence] = []
        if raw_stages is not None:
            if not isinstance(raw_stages, tuple):
                raise TypeError("modem_live_physical_stages_tuple_required")
            if any(
                not isinstance(stage, PhysicalCommandDispatchEvidence)
                for stage in raw_stages
            ):
                raise TypeError("modem_live_physical_stage_evidence_required")
            stages.extend(raw_stages)
        no_physical_command = details.get("modem_live_no_physical_command")
        if no_physical_command not in {None, True}:
            raise TypeError("modem_live_no_physical_command_flag_invalid")
        if result.success and not stages and no_physical_command is not True:
            raise RuntimeError("modem_live_exact_physical_stage_evidence_missing")
        failure_disposition = details.get(
            "modem_live_failure_disposition"
        )
        if failure_disposition is not None and not isinstance(
            failure_disposition,
            PhysicalCommandFailureDisposition,
        ):
            raise TypeError(
                "modem_live_physical_failure_disposition_invalid"
            )
        response_unavailable = any(
            stage.outcome == "response_unavailable"
            for stage in stages
        )
        reason = (
            "physical_command_not_required_current_target"
            if result.success and no_physical_command is True
            else (
                result.error
                or (
                    (
                        "physical_response_unavailable_"
                        "target_verification_pending"
                    )
                    if result.success and response_unavailable
                    else "modem_live_executor_accepted"
                    if result.success
                    else "modem_live_executor_rejected"
                )
            )
        )
        receipt = PhysicalCommandReceipt(
            request_id=command_id,
            transaction_root_id=root_id,
            target=command_target,
            execution_started_at=execution_started_at,
            transaction_completed_at=transaction_completed_at,
            accepted=bool(result.success),
            reason=reason,
            stages=tuple(stages),
            failure=failure_disposition,
        )
        if (isinstance(command_target, CoreNetworkSettingsTarget)
                and not receipt.accepted and receipt.stages):
            # An earlier stage may have changed the router before a later
            # stage failed. Refresh truth once through maintenance admission;
            # the command remains failed and no mutation is replayed.
            self.request_network_settings_inspection(
                expires_at=transaction_completed_at + 60.0,
            )
        details["physical_command_receipt"] = receipt.as_dict()
        return LeverResult(
            success=result.success,
            lever=result.lever,
            executed_at=result.executed_at,
            error=result.error,
            radio_drop_detected=result.radio_drop_detected,
            recovery_time_s=result.recovery_time_s,
            details=details,
        )

    def read_band_priority(self) -> List[int]:
        out = self._call_modem(
            "read_band_priority",
            source="startup_config",
            operation="read_band_priority",
            lock_policy="always",
        )
        return [int(b) for b in list(out or []) if int(b or 0) > 0]

    def set_band_priority(self, bands: List[int]) -> bool:
        del bands
        raise RuntimeError(
            "direct_band_priority_write_illegal_use_core_command_gateway"
        )

    @staticmethod
    def _identity_value_present(value: Any) -> bool:
        return bool(str(value or "").strip())

    @classmethod
    def _has_stable_device_identity(cls, info: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(info, dict):
            return False
        for key in (
            "serial",
            "imei",
            "router_serial",
            "device_serial",
            "serial_number",
            "router_imei",
            "device_imei",
        ):
            if cls._identity_value_present(info.get(key)):
                return True
        return False

    @classmethod
    def _startup_modem_info_from_receipt(
        cls,
        receipt: BackendAcquisitionReceipt,
    ) -> Dict[str, Any]:
        """Project identity already acquired by the startup transaction."""

        if not isinstance(receipt, BackendAcquisitionReceipt):
            raise TypeError("modem_live_startup_identity_receipt_required")
        status = receipt.status_copy()
        aliases = {
            "model": ("model", "router_model", "device_model"),
            "router_model": ("router_model", "model", "device_model"),
            "device_model": ("device_model", "model", "router_model"),
            "firmware": ("firmware", "software_version", "software"),
            "software_version": (
                "software_version",
                "software",
                "firmware",
            ),
            "software": ("software", "software_version", "firmware"),
            "serial": (
                "serial",
                "serial_number",
                "router_serial",
                "device_serial",
            ),
            "imei": ("imei", "router_imei", "device_imei"),
            "imsi": ("imsi",),
            "webui": ("webui",),
            "hardware_version": ("hardware_version",),
            "config_version": (
                "config_version",
                "configuration_file_version",
            ),
            "lan_mac": ("lan_mac",),
            "wan_ip": ("wan_ip",),
            "antenna": ("antenna",),
            "antenna_mode": ("antenna_mode", "antenna"),
            "antenna_mode_source": ("antenna_mode_source",),
        }
        info: Dict[str, Any] = {}
        for target, source_names in aliases.items():
            for source_name in source_names:
                value = status.get(source_name)
                if cls._identity_value_present(value):
                    info[target] = value
                    break
        return info

    def _merge_modem_info_cache(
        self,
        info: Optional[Dict[str, Any]],
        *,
        replace: bool = False,
    ) -> Dict[str, Any]:
        cache_lock = getattr(self, "_device_cache_lock", None)
        if cache_lock is None:
            cache_lock = threading.RLock()
            self._device_cache_lock = cache_lock
        with cache_lock:
            fresh = dict(info or {}) if isinstance(info, dict) else {}
            if replace:
                self._cached_modem_info = dict(fresh)
                return dict(self._cached_modem_info)

            merged = dict(getattr(self, "_cached_modem_info", {}) or {})
            for key, value in fresh.items():
                if self._identity_value_present(value):
                    merged[key] = value
                elif key not in merged:
                    merged[key] = value
            self._cached_modem_info = dict(merged)
            return dict(self._cached_modem_info)

    def read_plmn(self) -> str:
        cache_lock = getattr(self, "_device_cache_lock", None)
        if cache_lock is None:
            cache_lock = threading.RLock()
            self._device_cache_lock = cache_lock
        with cache_lock:
            cached = str(
                getattr(self, "_cached_provider_plmn", "") or ""
            ).strip()
        if cached:
            return cached
        status = self.get_last_status()
        if isinstance(status, dict):
            plmn = str(status.get("provider_plmn") or status.get("plmn") or "").strip()
            name = str(status.get("provider_name") or status.get("provider") or "").strip()
            if plmn or name:
                remembered_plmn, _ = self._remember_provider_identity(
                    provider_plmn=plmn,
                    provider_name=name,
                )
                if remembered_plmn:
                    return remembered_plmn
        return ""

    def get_modem_capabilities(self) -> Any:
        return self._call_modem(
            "get_capabilities",
            source="capability",
            operation="get_capabilities",
            lock_policy="always",
        )

    def read_static_config(self) -> Dict[str, Any]:
        data = self._call_modem(
            "verify_static_config",
            source="startup_config",
            operation="verify_static_config",
            lock_policy="always",
        ) or {}
        return dict(data) if isinstance(data, dict) else {}

    def apply_static_config(self, key: str) -> bool:
        del key
        raise RuntimeError(
            "direct_static_config_write_illegal_use_core_command_gateway"
        )

    def read_ue_capability(self) -> Dict[str, Any]:
        data = self._call_modem(
            "verify_ue_capability",
            source="capability",
            operation="verify_ue_capability",
            lock_policy="always",
        ) or {}
        return dict(data) if isinstance(data, dict) else {}

    def set_ue_capability(self, key: str, enabled: bool) -> bool:
        del key, enabled
        raise RuntimeError(
            "direct_ue_capability_write_illegal_use_core_command_gateway"
        )

    def sms_read_inbox(self, xml_body: str) -> str:
        return str(
            self._call_modem(
                "_api_post_read",
                "/api/sms/sms-list",
                str(xml_body or ""),
                source="sms",
                operation="read_inbox",
                lock_policy="sms",
            )
            or ""
        )

    def sms_send(self, xml_body: str) -> bool:
        return bool(
            self._call_modem(
                "_api_post",
                "/api/sms/send-sms",
                str(xml_body or ""),
                source="sms",
                operation="send",
                lock_policy="sms",
            )
        )

    def sms_delete(self, xml_body: str) -> bool:
        return bool(
            self._call_modem(
                "_api_post",
                "/api/sms/delete-sms",
                str(xml_body or ""),
                source="sms",
                operation="delete",
                lock_policy="sms",
            )
        )

    def read_sms_messages(self, *, limit: int = 20, unread_only: bool = False) -> List[Dict[str, Any]]:
        messages = self._call_modem(
            "read_sms_messages",
            source="sms",
            operation="read_messages",
            lock_policy="sms",
            limit=int(limit or 20),
            unread_only=bool(unread_only),
        ) or []
        return [dict(item) for item in list(messages or []) if isinstance(item, dict)]

    def send_sms_message(self, number: str, text: str) -> bool:
        return bool(
            self._call_modem(
                "send_sms_message",
                str(number or ""),
                str(text or ""),
                source="sms",
                operation="send_message",
                lock_policy="sms",
            )
        )

    def delete_sms_message(self, message_id: str) -> bool:
        return bool(
            self._call_modem(
                "delete_sms_message",
                str(message_id or ""),
                source="sms",
                operation="delete_message",
                lock_policy="sms",
            )
        )

    def get_last_status_snapshot(self) -> Dict[str, Any]:
        status = self.get_last_status()
        state = self._traffic_presence_state()
        status["traffic_presence_active"] = bool(state["active"])
        status["traffic_presence_interval_s"] = float(state["interval_s"])
        return status

    @staticmethod
    def _require_startup_permanent_identity(
        matrix: ValidatedTruthMatrixReader | Mapping[str, Any],
    ) -> None:
        """Keep login pending until Modem Live publishes serial number or IMEI."""

        reader = (
            matrix
            if isinstance(matrix, ValidatedTruthMatrixReader)
            else ValidatedTruthMatrixReader(matrix)
        )
        for field in ("serial", "serial_number", "imei"):
            cell = reader.cell("identity", field)
            if cell.available and str(cell.value or "").strip():
                return
        raise RuntimeError("modem_live_startup_permanent_identity_unavailable")

    @staticmethod
    def _require_startup_matrix_guarantee(
        matrix: ValidatedTruthMatrixReader | Mapping[str, Any],
    ) -> None:
        """Do not report Ready before the complete Device Matrix exists."""

        reader = (
            matrix
            if isinstance(matrix, ValidatedTruthMatrixReader)
            else ValidatedTruthMatrixReader(matrix)
        )
        for line, field in MODEM_LIVE_DEVICE_FACT_ADDRESSES.values():
            reader.cell(line, field)
        cell = reader.cell(
            "health",
            "radio_connected",
        )
        # Local authentication and physical identity admit the router session.
        # Mobile attachment may be unobserved until the approved settings apply.
        if cell.available and type(cell.value) is not bool:
            raise TypeError(
                "modem_live_startup_radio_connected_boolean_required"
            )

    def run_startup_live_phase(
        self,
        *,
        constraints: Optional[Constraints] = None,
        fast: bool = False,
    ) -> ModemLiveReadyReceipt:
        from arch4.operator_profiles import infer_backend_family

        startup_transaction_started_at = float(time.time())
        with self._live_register_lock:
            if int(self._product_lifecycle_generation or 0) <= 0:
                raise RuntimeError(
                    "modem_live_product_lifecycle_required_before_startup"
                )
            # Startup begins a new capability read. Empty or failed data must not
            # leave action authority from the previous device active.
            self._cached_runtime_capabilities = {}
            self._live_register_connectivity_status = {}
            self._modem_live_ready_receipt = None
            self._startup_publication_admission_open = False
            self._startup_publication_notice = None
            self._startup_publication_primary_receipt = None
            self._live_register_pending_publication_notice = None
            self._reset_startup_live_baseline_status()
        # Target identification during authentication belongs to this same
        # physical startup transaction, even when the full read uses its cache.
        startup_request = self._new_backend_acquisition_request(
            lane="startup_full", source="startup_live_phase",
            started_at=startup_transaction_started_at,
        )
        connected = self.startup_modem_connect()
        if not connected:
            raise ConnectionError("modem_live_startup_connect_unavailable")
        backend = str(getattr(self.cfg, "modem_backend", "") or "")
        backend_family = infer_backend_family(backend)
        serial_at_startup = backend_family in {
            "routeros_at_like",
            "modem_model_like",
        }
        self._cached_modem_info = {}
        live_contract = self._refresh_live_acquisition_contract_from_config()
        startup_parallel_allowed = (
            live_contract.effective_startup_parallel_allowed()
            if callable(getattr(live_contract, "effective_startup_parallel_allowed", None))
            else bool(getattr(live_contract, "backend_io_parallel_safe", False))
        )
        protected_api_startup = (not serial_at_startup) and (not bool(startup_parallel_allowed))
        # One backend-owned full transaction supplies the first Matrix
        # generation. A backend may retry one missing required field inside
        # this transaction, but Modem Live must not start a second sweep.
        startup_acquisition_receipt = self._read_modem_with_timeout(
            request=startup_request,
        )
        if not isinstance(
            startup_acquisition_receipt,
            BackendAcquisitionReceipt,
        ):
            raise RuntimeError(
                "modem_live_startup_full_receipt_missing"
            )
        if not startup_acquisition_receipt.succeeded:
            raise RuntimeError(
                "modem_live_startup_full_receipt_failed:"
                f"{startup_acquisition_receipt.failure_reason or 'unknown'}"
            )
        modem_input = startup_acquisition_receipt.acquired_modem_copy()
        self._require_selected_device_admission(startup_acquisition_receipt.status_copy())
        startup_baseline_status = (
            self._establish_startup_live_baseline_from_receipt(
                receipt=startup_acquisition_receipt,
                source="startup_live_phase",
            )
        )
        base_status = startup_acquisition_receipt.status_copy()
        plmn, cached_provider = self._remember_provider_identity(
            provider_plmn=(
                base_status.get("provider_plmn")
                or base_status.get("plmn")
                or ""
            ),
            provider_name=(
                base_status.get("provider_name")
                or base_status.get("provider")
                or ""
            ),
        )
        info = self._startup_modem_info_from_receipt(
            startup_acquisition_receipt
        )
        if not self._has_stable_device_identity(info):
            raise RuntimeError(
                "modem_live_startup_receipt_permanent_identity_required"
            )
        info = self._merge_modem_info_cache(info, replace=True)
        caps = (
            get_static_capabilities(backend)
            if fast or serial_at_startup
            else self.get_modem_capabilities()
        )
        if fast or serial_at_startup or protected_api_startup:
            static_config = {}
            ue_capability = {}
        else:
            static_config = dict(self.read_static_config() or {})
            ue_capability = dict(self.read_ue_capability() or {})

        info = self._merge_modem_info_cache(info, replace=False)
        receipt_status = startup_acquisition_receipt.status_copy()
        plmn, cached_provider = self._remember_provider_identity(
            provider_plmn=(
                receipt_status.get("provider_plmn")
                or receipt_status.get("plmn")
                or plmn
                or ""
            ),
            provider_name=(
                receipt_status.get("provider_name")
                or receipt_status.get("provider")
                or cached_provider
                or ""
            ),
        )
        provider = cached_provider
        self._merge_modem_info_cache(info, replace=True)
        if constraints is not None:
            if plmn:
                constraints.provider_plmn = plmn
                constraints.provider_name = provider
            imei = str(info.get("imei", "") or "")
            constraints.imei_phone = (
                not any(imei.startswith(p) for p in ["8698", "8613", "8607"])
                if imei else False
            )
            bootstrap_constraints = _build_bootstrap_constraints(
                provider_plmn=plmn,
                provider_name=provider,
                static_config=static_config,
                ue_capability=ue_capability,
            )
            for key, value in vars(bootstrap_constraints).items():
                setattr(constraints, key, value)

        seed_payload = _merge_live_seed_context(
            build_live_seed_payload_from_modem(
                modem_input,
                provider_plmn=plmn,
                provider_name=provider,
                modem_info=info,
                acquired_cells=(
                    startup_acquisition_receipt.acquired_cells
                    if startup_acquisition_receipt is not None
                    and startup_acquisition_receipt.succeeded
                    else ()
                ),
            )
            or {},
            modem_input=modem_input,
            modem_info=info,
            provider_plmn=plmn,
            provider_name=provider,
            capabilities=caps,
            static_config=static_config,
            ue_capability=ue_capability,
            config=self.cfg,
            selected_access_profile=self.modem_live_access_profile(),
        )
        runtime_caps = extract_live_runtime_capabilities(seed_payload)
        with self._live_register_lock:
            self._cached_runtime_capabilities = (
                dict(runtime_caps) if isinstance(runtime_caps, dict) else {}
            )
        modem_input = self._establish_startup_live_register(
            acquisition_receipt=startup_acquisition_receipt,
            source="startup_live_phase",
        )
        if modem_input is not None:
            seed_payload = _merge_live_seed_context(
                build_live_seed_payload_from_modem(
                    modem_input,
                    provider_plmn=plmn,
                    provider_name=provider,
                    modem_info=info,
                    acquired_cells=(
                        set(startup_baseline_status.get("acquired_cells") or ())
                        | set(
                            startup_acquisition_receipt.acquired_cells
                            if startup_acquisition_receipt is not None
                            and startup_acquisition_receipt.succeeded
                            else ()
                        )
                    ),
                )
                or seed_payload,
                modem_input=modem_input,
                modem_info=info,
                provider_plmn=plmn,
                provider_name=provider,
                capabilities=caps,
                static_config=static_config,
                ue_capability=ue_capability,
                config=self.cfg,
                selected_access_profile=self.modem_live_access_profile(),
            )
        with self._live_register_lock:
            committed = self._live_register_store.committed_generation()
            committed_matrix = committed.matrix_reader()
            self._require_startup_permanent_identity(
                committed_matrix
            )
            self._require_startup_matrix_guarantee(
                committed_matrix
            )
            held_notice = self._startup_publication_notice
            if held_notice is None:
                raise RuntimeError(
                    "modem_live_startup_publication_notice_missing"
                )
            if held_notice.matrix_identity != committed.identity:
                raise RuntimeError(
                    "modem_live_startup_notice_commit_identity_mismatch"
                )
            committed_primary = self._startup_publication_primary_receipt
            if committed_primary is None:
                raise RuntimeError(
                    "modem_live_startup_committed_primary_receipt_missing"
                )
            primary_identity, primary_receipt = committed_primary
            if primary_identity != committed.identity:
                raise RuntimeError(
                    "modem_live_startup_primary_receipt_identity_mismatch"
                )
            if not primary_receipt.succeeded:
                raise RuntimeError(
                    "modem_live_startup_committed_primary_receipt_failed"
                )
            attempt_ids = tuple(
                dict.fromkeys(
                    (
                        primary_receipt.request.attempt_id,
                        *(
                            (startup_acquisition_receipt.request.attempt_id,)
                            if startup_acquisition_receipt is not None
                            else ()
                        ),
                        *tuple(
                            startup_baseline_status.get(
                                "acquisition_receipt_ids",
                                (),
                            )
                            or ()
                        ),
                    )
                )
            )
            if not bool(startup_baseline_status.get("completed", False)):
                baseline_error = str(
                    startup_baseline_status.get("last_error") or ""
                ).strip()
                raise RuntimeError(
                    "modem_live_startup_baseline_incomplete"
                    + (f":{baseline_error}" if baseline_error else "")
                )
            startup_sweep = StartupAcquisitionSweepReceipt(
                matrix_identity=committed.identity,
                acquisition_attempt_id=(
                    primary_receipt.request.attempt_id
                ),
                acquisition_attempt_ids=attempt_ids,
                source_lane="startup_transaction",
                started_at=startup_transaction_started_at,
                finished_at=float(
                    committed.matrix_reader().metadata("sampled_at")
                ),
                completed=True,
            )
            receipt = ModemLiveReadyReceipt(
                startup_sweep=startup_sweep,
                ready_at=max(float(time.time()), startup_sweep.finished_at),
            )
            if (
                receipt.lifecycle_generation
                != int(self._product_lifecycle_generation)
            ):
                raise RuntimeError(
                    "modem_live_ready_lifecycle_mismatch"
                )
            self._modem_live_ready_receipt = receipt
        return receipt

    def _restore_connectivity_state_for_test(
        self,
        *,
        consecutive_failures: int,
        last_failure_ts: float,
        last_reconnect_attempt_ts: float,
        reconnect_backoff_s: float,
        reconnect_backoff_max_s: float,
        degraded: bool,
        last_success_ts: float,
        intentional_switch: bool,
    ) -> None:
        """Test adapter. Production callers may only read connectivity state."""

        self._ensure_connectivity_module().import_state(
            consecutive_failures=consecutive_failures,
            last_failure_ts=last_failure_ts,
            last_reconnect_attempt_ts=last_reconnect_attempt_ts,
            reconnect_backoff_s=reconnect_backoff_s,
            reconnect_backoff_max_s=reconnect_backoff_max_s,
            degraded=degraded,
            last_success_ts=last_success_ts,
            intentional_switch=intentional_switch,
        )

    def export_recovery_state(self) -> Dict[str, Any]:
        return dict(self._ensure_connectivity_module().export_state())

    def _connectivity_attempt_context(
        self,
        *,
        physical_attempt_id: str,
        source_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        register_lock = getattr(self, "_live_register_lock", None)
        with register_lock if register_lock is not None else nullcontext():
            current = self._live_register_store.current_status()
            generation = int(
                source_generation
                if source_generation is not None
                else current.sequence
                or 0
            )
            base_status: Dict[str, Any] = {}
            if current.available:
                reader = (
                    self._live_register_store
                    .committed_generation()
                    .matrix_reader()
                )
                base_status.update(reader.line_values("identity"))
                base_status["session_scope_key"] = reader.metadata(
                    "session_scope_key"
                )
        identity_key = base_status
        runtime_identity = getattr(self, "_last_runtime_identity", {})
        if not isinstance(runtime_identity, dict):
            runtime_identity = {}
        session_scope_key = str(
            base_status.get("session_scope_key")
            or getattr(self, "_last_runtime_session_scope_key", "")
            or ""
        )
        return {
            "physical_attempt_id": str(physical_attempt_id or ""),
            "source_epoch": str(
                getattr(self, "_publication_source_epoch", "") or ""
            ),
            "source_generation": generation,
            "session_scope_key": session_scope_key,
            # Command and connectivity identity belong to one physical-device
            # namespace. Descriptive identity may include firmware, provider,
            # and SIM data, but it cannot scope or reject a modem command.
            "identity_key": session_scope_key,
            "identity_code": str(
                identity_key.get("identity_code")
                or base_status.get("identity_code")
                or runtime_identity.get("identity_code")
                or ""
            ),
        }

    def _record_connectivity_attempt(
        self,
        *,
        succeeded: bool,
        now: Optional[float] = None,
        attempt_context: Optional[Mapping[str, Any]] = None,
        backend_repair: ModemLiveSessionRepairReceipt | None = None,
    ) -> Dict[str, Any]:
        if succeeded:
            return self._ensure_connectivity_module().record_physical_attempt(
                succeeded=True,
                now=now,
                attempt_context=attempt_context,
                backend_repair=backend_repair,
            )
        return self._ensure_connectivity_module().record_physical_attempt(
            succeeded=False,
            now=now,
            attempt_context=attempt_context,
            backend_repair=backend_repair,
        )

    def get_modem_health_status(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        return self._ensure_connectivity_module().get_modem_health_status(now=now)

    def close_expected_interruption_from_command_terminal(
        self,
        *,
        operation_id: str,
        terminal_reason: str,
        now: Optional[float] = None,
    ) -> bool:
        """Close the matching physical interruption using Core state."""

        return self._ensure_connectivity_module(
        ).close_expected_interruption_from_command_terminal(
            operation_id=operation_id,
            terminal_reason=terminal_reason,
            now=now,
        )

    def expected_interruption_status_payload(
        self,
        *,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Expose only the typed Modem Live interruption identity to Core."""

        return self._ensure_connectivity_module(
        ).expected_interruption_status_payload(now=now)

    def runtime_capabilities_snapshot(self) -> Dict[str, Any]:
        """Read capabilities from the last committed Matrix generation."""

        try:
            return capability_report_from_matrix(
                self.committed_live_generation().matrix_reader()
            )
        except RuntimeError:
            return {}

    @staticmethod
    def _permanent_identity_text(value: Any) -> str:
        from arch4.runtime_identity import is_placeholder_identity_text

        if is_placeholder_identity_text(value):
            return ""
        text = str(value).strip()
        return "" if set(text) == {"0"} else text

    def _cutover_device_cache_if_required(
        self,
        *,
        modem: Optional[ModemInput],
        base_status: Mapping[str, Any],
    ) -> tuple[bool, str]:
        """Detect replacement without changing this worker's device caches."""

        cache_lock = getattr(self, "_device_cache_lock", None)
        if cache_lock is None:
            cache_lock = threading.RLock()
            self._device_cache_lock = cache_lock
        with cache_lock:
            replacement = self.pending_device_replacement()
            if replacement is not None:
                raise replacement
            backend = str(
                base_status.get("backend")
                or getattr(self.cfg, "modem_backend", "")
                or ""
            ).strip().lower()
            serial = self._permanent_identity_text(
                base_status.get("serial")
                or base_status.get("serial_number")
                or getattr(modem, "serial", "")
                or ""
            )
            imei = self._permanent_identity_text(
                base_status.get("imei")
                or getattr(modem, "imei", "")
                or ""
            )
            if not backend or not (serial or imei):
                return False, ""

            candidate_identity = {
                "backend": backend,
                "serial": serial,
                "imei": imei,
            }
            active_identity = dict(
                getattr(self, "_active_device_identity", {}) or {}
            )

            def _identity_key(identity: Mapping[str, Any]) -> str:
                parts = [f"backend={str(identity.get('backend') or '')}"]
                if str(identity.get("serial") or ""):
                    parts.append(f"serial={str(identity.get('serial'))}")
                if str(identity.get("imei") or ""):
                    parts.append(f"imei={str(identity.get('imei'))}")
                return "|".join(parts)

            if not active_identity:
                self._active_device_identity = dict(candidate_identity)
                self._active_device_identity_key = _identity_key(
                    candidate_identity
                )
                return False, ""

            previous = _identity_key(active_identity)
            if str(active_identity.get("backend") or "") == backend:
                comparable = tuple(
                    field
                    for field in ("serial", "imei")
                    if str(active_identity.get(field) or "")
                    and str(candidate_identity.get(field) or "")
                )
                matching = tuple(
                    field
                    for field in comparable
                    if str(active_identity.get(field))
                    == str(candidate_identity.get(field))
                )
                mismatching = tuple(
                    field
                    for field in comparable
                    if str(active_identity.get(field))
                    != str(candidate_identity.get(field))
                )
                if (matching and not mismatching) or not comparable:
                    # A matching permanent ID proves this is the same device. If
                    # another ID appears later, add it to the identity record.
                    # Without a comparable ID, keep the established device until
                    # enough evidence arrives.
                    if matching and not mismatching:
                        merged_identity = dict(active_identity)
                        for field in ("serial", "imei"):
                            if not str(merged_identity.get(field) or ""):
                                merged_identity[field] = str(
                                    candidate_identity.get(field) or ""
                                )
                        self._active_device_identity = merged_identity
                        self._active_device_identity_key = _identity_key(
                            merged_identity
                        )
                    return False, previous

            # A different backend or different comparable permanent ID proves
            # that the physical device changed.
            candidate = _identity_key(candidate_identity)
            if previous == candidate:
                return False, previous

            # This worker keeps one device namespace for its lifetime. A proven
            # device change is a Product lifecycle event. Keep the old worker
            # unchanged while the parent drains it and starts a new worker.
            current_namespace = self._live_register_store.runtime_namespace
            requested_scope = runtime_device_namespace_key(
                candidate_identity
            )
            if not requested_scope:
                raise RuntimeError(
                    "modem_live_replacement_namespace_unavailable"
                )
            self._pending_device_replacement = (
                current_namespace,
                RuntimeGenerationNamespace(
                    session_scope_key=requested_scope,
                    source_epoch=self._publication_source_epoch,
                    lifecycle_generation=self._product_lifecycle_generation,
                ),
                ProductRuntimeWorkerReplacementReason.PHYSICAL_DEVICE_CHANGED,
            )
            raise self.pending_device_replacement()

    def _build_live_runtime_identity_context(
        self,
        *,
        modem: Optional[ModemInput],
        base_status: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        base = dict(base_status or {})
        modem_obj = modem
        previous_scope = dict(self._last_runtime_session_scope or {})
        previous_scope_key = str(self._last_runtime_session_scope_key or "")
        _, replaced_device_key = (
            self._cutover_device_cache_if_required(
                modem=modem_obj,
                base_status=base,
            )
        )

        def _identity_text(*values: Any) -> str:
            for value in values:
                if value in (None, "") or isinstance(value, bool):
                    continue
                if not isinstance(value, (str, int, float)):
                    continue
                text = str(value).strip()
                if text and text.lower() != "unknown":
                    return text
            return ""

        caps_payload = (
            dict(self._cached_runtime_capabilities)
            if isinstance(self._cached_runtime_capabilities, dict)
            else {}
        )
        runtime_identity = runtime_identity_from_sample_sources(
            base_status=base,
            extra={},
            runtime_health={},
            capabilities=caps_payload,
            backend_hint=str(base.get("backend") or getattr(self.cfg, "modem_backend", "") or "").strip().lower(),
            backend_family_hint=str(base.get("backend_family", "") or ""),
            profile_name=str(getattr(self.cfg, "model_profile_name", "") or ""),
        )
        if not runtime_identity:
            runtime_identity = build_runtime_identity(
                backend=_identity_text(getattr(self.cfg, "modem_backend", ""), base.get("backend")).lower(),
                vendor=_identity_text(getattr(modem_obj, "vendor", ""), base.get("vendor")),
                model=_identity_text(getattr(modem_obj, "model", ""), base.get("model"), base.get("router_model")),
                firmware=_identity_text(
                    getattr(modem_obj, "firmware", ""),
                    base.get("firmware"),
                    base.get("software_version"),
                ),
                profile_name=_identity_text(getattr(self.cfg, "model_profile_name", "")),
            ).to_dict()
        for key in (
            "vendor",
            "model",
            "firmware",
            "target",
            "iface",
            "iface_id",
            "sim_id",
            "serial",
            "imei",
            "imsi",
            "hardware_version",
            "software_version",
            "webui",
            "config_version",
            "lan_mac",
            "wan_ip",
            "antenna",
            "antenna_mode",
            "antenna_mode_source",
        ):
            value = _identity_text(
                base.get(key)
                if key in base
                else None,
                getattr(modem_obj, key, ""),
                self._cached_modem_info.get(key, ""),
            )
            if value:
                runtime_identity[key] = value
        # Startup, fast reads and maintenance all publish the same provider
        # vocabulary. Do this before public identity/scope construction; private
        # backend/profile evidence in ``base`` is deliberately not rewritten.
        provider_plmn, provider_name = self._remember_provider_identity(
            provider_plmn=_identity_text(
                base.get("provider_plmn"), base.get("plmn"),
                getattr(modem_obj, "provider_plmn", ""),
            ),
            provider_name=_identity_text(
                base.get("provider_name"), base.get("provider"),
                getattr(modem_obj, "provider_name", ""),
            ),
        )
        runtime_identity["provider_plmn"] = provider_plmn
        runtime_identity["provider_name"] = provider_name
        scope = build_runtime_session_scope(runtime_identity)
        # A proven replacement has already raised and latched above. This
        # worker only enriches its existing device; it cannot change owners.
        stable_scope = dict(merge_runtime_session_scope(previous_scope, scope))
        candidate_scope_key = runtime_device_namespace_key(stable_scope)
        stable_scope_key = (
            candidate_scope_key
            if not self._last_runtime_session_scope_key
            else self._last_runtime_session_scope_key
        )
        self._last_runtime_session_scope = dict(stable_scope)
        self._last_runtime_session_scope_key = str(stable_scope_key or "")
        identity_base = dict(base)
        for key, value in runtime_identity.items():
            if value and key not in identity_base:
                identity_base[key] = value
        try:
            resolved_identity_card = build_resolved_identity_card_section(
                identity_base
            )
            identity_scope_lock = runtime_identity_scope_lock_status(
                runtime_identity=runtime_identity,
                resolved_identity_card=resolved_identity_card,
                previous_scope=previous_scope,
            )
        except Exception as exc:
            self._record_backend_fault("runtime_identity_projection", exc)
            resolved_identity_card = {}
            identity_scope_lock = {
                "schema": "runtime_identity_scope_lock.v1",
                "locked": False,
                "exact_identity": False,
                "provisional": True,
                "reason": (
                    "runtime_identity_projection_failed:"
                    + type(exc).__name__
                ),
                "profile_id": "",
                "has_strong_id": False,
                "scope": dict(stable_scope),
                "scope_key": str(stable_scope_key or ""),
                "model": str(stable_scope.get("model") or ""),
                "firmware": str(stable_scope.get("firmware") or ""),
                "provider": str(
                    stable_scope.get("provider_plmn")
                    or stable_scope.get("provider_name")
                    or ""
                ),
                "verified_modem_identity": {},
            }
        verified_modem_identity = dict(
            identity_scope_lock.get("verified_modem_identity") or {}
        )
        self._last_runtime_identity = dict(runtime_identity or {})
        self._last_verified_modem_identity = dict(verified_modem_identity)
        self._last_resolved_identity_card = dict(resolved_identity_card or {})
        return {
            "runtime_identity": runtime_identity,
            "verified_modem_identity": verified_modem_identity,
            "resolved_identity_card": dict(resolved_identity_card or {}),
            "identity_scope_lock": identity_scope_lock,
            "runtime_session_scope": dict(stable_scope),
            "session_scope_key": str(stable_scope_key or ""),
            "previous_session_scope_key": previous_scope_key,
            "previous_device_identity_key": replaced_device_key,
            "session_changed": False,
            "session_transition": {},
        }

    def _read_modem_with_timeout(
        self,
        *,
        request: BackendAcquisitionRequest,
    ) -> Optional[BackendAcquisitionReceipt]:
        if not isinstance(request, BackendAcquisitionRequest):
            raise TypeError("modem_live_read_lane_typed_request_required")
        if request.lane not in {"startup_full", "fast_live"}:
            raise ValueError("modem_live_read_lane_request_invalid")
        actionable = request.lane == "fast_live"
        if actionable:
            default_timeout_s = 2.5
            serial_actionable_read = False
            from arch4.operator_profiles import infer_backend_family

            backend_family = infer_backend_family(
                str(getattr(self.cfg, "modem_backend", "") or "")
            )
            if backend_family in {"routeros_at_like", "modem_model_like"}:
                serial_actionable_read = True
                default_timeout_s = 6.0
            timeout_s = float(
                self.input_cfg.actionable_modem_read_timeout_s or default_timeout_s
            )
            if serial_actionable_read:
                timeout_s = max(timeout_s, default_timeout_s)
        else:
            timeout_s = float(self.input_cfg.modem_read_timeout_s)

        with self._read_lane_changed:
            if self._modem_live_shutdown.is_set():
                return None
            thread = self._read_lane_thread
            if thread is None:
                thread = threading.Thread(
                    target=self._modem_read_lane_worker,
                    name="modem-live-owned-read-lane",
                    daemon=True,
                )
                self._read_lane_thread = thread
                thread.start()
            elif not thread.is_alive():
                raise RuntimeError("modem_live_read_lane_terminated")
            if self._read_lane_request is not None:
                self._acquisition_lane_state.mark(
                    AcquisitionLaneState.WEDGED,
                    "previous_modem_read_still_active",
                )
                return None
            if (
                self._acquisition_lane_state.state
                in {AcquisitionLaneState.TIMED_OUT, AcquisitionLaneState.WEDGED}
            ):
                self._acquisition_lane_state.mark(
                    AcquisitionLaneState.RECOVERING,
                    "starting_recovery_read",
                )
            self._read_lane_sequence += 1
            request_sequence = self._read_lane_sequence
            self._read_lane_request = (
                request_sequence,
                bool(actionable),
                request,
            )
            self._read_lane_result = None
            self._read_lane_error = None
            self._read_lane_changed.notify_all()
            deadline = time.monotonic() + max(0.0, timeout_s)
            while self._read_lane_completed_sequence < request_sequence:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                self._read_lane_changed.wait(remaining)
            completed = (
                self._read_lane_completed_sequence == request_sequence
            )
            receipt = self._read_lane_result if completed else None
            modem_exc = self._read_lane_error if completed else None

        if not completed:
            self._acquisition_lane_state.mark(
                AcquisitionLaneState.TIMED_OUT,
                f"read_timeout_s:{timeout_s:.3f}",
            )
            log.warning("Modem read timed out after %.1fs", timeout_s)
            return None

        if receipt is None or not receipt.succeeded:
            if modem_exc is not None:
                raise modem_exc
            base_status = (
                receipt.status_copy()
                if receipt is not None
                else {}
            )
            if base_status.get("target_guard_ok") is False:
                reason = str(base_status.get("target_guard_reason") or "target_guard_rejected")
                raise TargetGuardError(
                    f"Target guard failed during live modem read: {reason}",
                    reason=reason,
                )
            return receipt

        self._acquisition_lane_state.mark(AcquisitionLaneState.HEALTHY)
        return receipt

    def _modem_read_lane_worker(self) -> None:
        """Keep each startup or actionable read until the physical call returns."""

        while not self._read_lane_stop.is_set():
            with self._read_lane_changed:
                while (
                    self._read_lane_request is None
                    and not self._read_lane_stop.is_set()
                ):
                    self._read_lane_changed.wait(0.25)
                if self._read_lane_stop.is_set():
                    return
                request = self._read_lane_request
            if request is None:
                continue
            sequence, actionable, acquisition_request = request
            receipt: Optional[BackendAcquisitionReceipt] = None
            error: Optional[BaseException] = None
            try:
                if actionable:
                    receipt = self._call_modem(
                        "acquire_actionable_modem_input",
                        acquisition_request,
                        source="startup_live_phase",
                        caller_id="owned_read_lane",
                        operation="acquire_actionable_modem_input",
                        lock_policy="always",
                    )
                else:
                    receipt = self._call_modem(
                        "acquire_modem_input",
                        acquisition_request,
                        source="startup_live_phase",
                        caller_id="owned_read_lane",
                        operation="acquire_modem_input",
                        lock_policy="always",
                    )
            except Exception as exc:
                error = exc
            if receipt is not None and not isinstance(
                receipt,
                BackendAcquisitionReceipt,
            ):
                error = TypeError(
                    "modem_live_owned_read_receipt_required"
                )
                receipt = None
            with self._read_lane_changed:
                if self._read_lane_request != request:
                    raise RuntimeError(
                        "modem_live_read_lane_request_identity_changed"
                    )
                self._read_lane_result = receipt
                self._read_lane_error = error
                self._read_lane_completed_sequence = sequence
                self._read_lane_request = None
                self._read_lane_changed.notify_all()

    def _probe_rtt_once(
        self,
        *,
        source: str,
        operation: str,
        target: str,
        count: int,
        wait_for_active_probe: bool = True,
        publish_observation: bool = True,
        quality_sample: bool = True,
    ) -> Optional[float]:
        if publish_observation and source != "rtt_cadence":
            raise RuntimeError(
                "canonical_rtt_publication_requires_scheduled_cadence"
            )
        execution_lock = getattr(self, "_rtt_probe_execution_lock", None)
        if execution_lock is None:
            execution_lock = threading.Lock()
            self._rtt_probe_execution_lock = execution_lock
        acquired = execution_lock.acquire(blocking=bool(wait_for_active_probe))
        if not acquired:
            return None
        authority = (
            self._ensure_rtt_observation_authority()
            if publish_observation
            else None
        )
        try:
            try:
                result = self._call_modem(
                    "measure_rtt",
                    source=source,
                    operation=operation,
                    lock_policy="rtt",
                    target=target,
                    count=max(1, int(count or 1)),
                    timeout_s=AUTOMATIC_RTT_PROBE_TIMEOUT_S,
                )
                if not isinstance(result, RttProbeResult):
                    raise TypeError("modem_live_typed_rtt_result_required")
            except Exception as exc:
                if authority is not None:
                    authority.observe_unknown(
                        f"{type(exc).__name__}: {exc}", target=target
                    )
                raise
            if result.status == "failed":
                if authority is not None:
                    authority.observe_failure(
                        result.failure_reason, target=result.target
                    )
                return None
            if result.status == "unknown":
                if authority is not None:
                    authority.observe_unknown(
                        result.failure_reason, target=result.target
                    )
                return None
            if authority is None:
                return result.value_ms
            if quality_sample:
                return authority.observe_success(
                    result.value_ms, target=result.target
                )
            authority.observe_reachable_only(target=result.target)
            return result.value_ms
        finally:
            execution_lock.release()

    def measure_rtt(self, *, target: str = "", count: int = 1) -> Optional[float]:
        return self._probe_rtt_once(
            source="toolbox_rtt",
            operation="measure_rtt",
            target=str(target or self.input_cfg.rtt_probe_target or "8.8.8.8"),
            count=count,
            publish_observation=False,
        )

    def verify_internet_reachability(
        self,
        *,
        targets: tuple[str, ...],
        requested_at: float,
    ) -> ModemLiveInternetProbeRound:
        """Probe independent Internet targets without changing RTT cadence truth."""

        normalized = tuple(
            dict.fromkeys(str(target or "").strip() for target in targets)
        )
        normalized = tuple(target for target in normalized if target)
        if len(normalized) < 2:
            raise ValueError("modem_live_internet_verification_targets_required")
        if float(requested_at) <= 0.0:
            raise ValueError("modem_live_internet_verification_time_required")
        started_at = float(time.time())
        execution_lock = self._rtt_probe_execution_lock
        probes: list[ModemLiveInternetProbe] = []
        with execution_lock:
            for target in normalized:
                probed_at = float(time.time())
                try:
                    result = self._call_modem(
                        "measure_rtt",
                        source="internet_emergency_verification",
                        operation="verify_internet_reachability",
                        lock_policy="rtt",
                        target=target,
                        count=1,
                        timeout_s=AUTOMATIC_RTT_PROBE_TIMEOUT_S,
                    )
                    if not isinstance(result, RttProbeResult):
                        raise TypeError("modem_live_typed_rtt_result_required")
                    rtt = result.value_ms
                    probes.append(
                        ModemLiveInternetProbe(
                            target=target,
                            probed_at=probed_at,
                            reachable=(True if result.status == "ok" else
                                       False if result.status == "failed" else None),
                            rtt_ms=rtt,
                            failure_reason=result.failure_reason,
                        )
                    )
                except Exception as exc:
                    probes.append(
                        ModemLiveInternetProbe(
                            target=target,
                            probed_at=probed_at,
                            reachable=None,
                            failure_reason=f"{type(exc).__name__}: {exc}",
                        )
                    )
        completed_at = max(float(time.time()), probes[-1].probed_at)
        return ModemLiveInternetProbeRound(
            round_id=(
                "modem-live-internet-round:"
                + str(time.time_ns())
            ),
            started_at=started_at,
            completed_at=completed_at,
            probes=tuple(probes),
        )

    def _scheduled_rtt_targets(self) -> tuple[str, ...]:
        primary = str(self.input_cfg.rtt_probe_target or "8.8.8.8").strip()
        return tuple(dict.fromkeys(
            target
            for target in (
                primary,
                *(str(value or "").strip() for value in self.input_cfg.health_ping_targets),
            )
            if target
        ))

    def _run_scheduled_rtt_probe(self) -> Optional[float]:
        targets = self._scheduled_rtt_targets()
        primary = targets[0]
        if self._rtt_confirmation_active and len(targets) > 1:
            target = targets[self._rtt_confirmation_index % len(targets)]
        else:
            target = primary
        authority = self._ensure_rtt_observation_authority()
        previous_sequence = int(authority.status().get("sequence", 0) or 0)
        try:
            value = self._probe_rtt_once(
                source="rtt_cadence",
                operation="measure_rtt",
                target=target,
                count=max(1, int(self.input_cfg.actionable_rtt_probe_count or 1)),
                wait_for_active_probe=False,
                quality_sample=target == primary,
            )
        except Exception:
            self._update_rtt_confirmation_state(
                targets=targets,
                previous_sequence=previous_sequence,
            )
            self._run_service_confirmation()
            raise
        self._update_rtt_confirmation_state(
            targets=targets,
            previous_sequence=previous_sequence,
        )
        self._run_service_confirmation()
        return value

    def _update_rtt_confirmation_state(
        self,
        *,
        targets: tuple[str, ...],
        previous_sequence: int,
    ) -> None:
        status = self._ensure_rtt_observation_authority().status()
        # The scheduled attempt can lose the non-blocking I/O race to another
        # diagnostic probe.  No new canonical result means no ring movement.
        if int(status.get("sequence", 0) or 0) == previous_sequence:
            return
        if status.get("probe_status") in {"ok", "reachable_only"}:
            self._rtt_confirmation_active = False
            self._rtt_confirmation_index = 0
        else:
            if not self._rtt_confirmation_active:
                self._rtt_confirmation_active = True
                self._rtt_confirmation_index = 1
            else:
                self._rtt_confirmation_index = (
                    self._rtt_confirmation_index + 1
                ) % len(targets)

    def _rtt_cadence_allowed(self) -> bool:
        return getattr(self, "_live_acquisition_contract", None) is not None

    def _rtt_cadence_interval_s(self) -> float:
        input_cfg = getattr(self, "input_cfg", None)
        contract = getattr(self, "_live_acquisition_contract", None)
        return max(
            1.0,
            float(getattr(input_cfg, "actionable_rtt_max_age_s", 2.5) or 2.5),
            float(getattr(contract, "rtt_min_interval_s", 1.0) or 1.0),
        )

    def _start_rtt_cadence_worker(self) -> None:
        if (
            not self._rtt_cadence_allowed()
            or self._modem_live_shutdown.is_set()
        ):
            return
        cadence_lock = getattr(self, "_rtt_cadence_lock", None)
        if cadence_lock is None:
            cadence_lock = threading.Lock()
            self._rtt_cadence_lock = cadence_lock
        with cadence_lock:
            if self._modem_live_shutdown.is_set():
                return
            thread = getattr(self, "_rtt_cadence_thread", None)
            if thread is not None and thread.is_alive():
                return
            stop_event = threading.Event()
            self._rtt_cadence_stop = stop_event
            thread = threading.Thread(
                target=self._rtt_cadence_loop,
                args=(stop_event,),
                name="arch4-modem-live-rtt-cadence",
                daemon=True,
            )
            self._rtt_cadence_thread = thread
            thread.start()

    def _rtt_cadence_loop(self, stop_event: threading.Event) -> None:
        if not isinstance(stop_event, threading.Event):
            raise TypeError("rtt_cadence_stop_event_required")
        try:
            while (
                not stop_event.is_set()
                and not self._modem_live_shutdown.is_set()
            ):
                if not self._rtt_cadence_allowed():
                    self._rtt_cadence_last_error = "rtt_cadence_contract_not_eligible"
                    return
                status = self._ensure_rtt_observation_authority().status()
                interval_s = self._rtt_cadence_interval_s()
                last_probe_at = float(status.get("last_probe_at", 0.0) or 0.0)
                now = time.time()
                remaining_s = max(0.0, interval_s - max(0.0, now - last_probe_at))
                if last_probe_at > 0.0 and remaining_s > 0.0:
                    stop_event.wait(min(0.1, remaining_s))
                    continue
                self._rtt_cadence_last_attempt_ts = now
                try:
                    value = self._run_scheduled_rtt_probe()
                    if value is not None:
                        self._rtt_cadence_last_success_ts = time.time()
                        self._rtt_cadence_last_error = ""
                except Exception as exc:
                    self._rtt_cadence_last_error = f"{type(exc).__name__}: {exc}"
                    self._record_backend_fault("rtt_cadence", exc)
                    log.debug("Modem Live RTT cadence probe failed: %s", exc)
                stop_event.wait(0.05)
        finally:
            current = threading.current_thread()
            cadence_lock = getattr(self, "_rtt_cadence_lock", None)
            if cadence_lock is not None:
                with cadence_lock:
                    if self._rtt_cadence_thread is current:
                        self._rtt_cadence_thread = None
                        self._rtt_cadence_stop = None

    def _rtt_cadence_status(self) -> Dict[str, Any]:
        thread = getattr(self, "_rtt_cadence_thread", None)
        enabled = bool(self._rtt_cadence_allowed())
        return {
            "owner": "modem_live",
            "worker": "rtt_cadence",
            "enabled": enabled,
            "eligibility_reason": (
                (
                    "serialized_rtt_contract"
                    if bool(
                        getattr(
                            self._live_acquisition_contract,
                            "rtt_holds_io_lock",
                            False,
                        )
                    )
                    else "non_locking_rtt_contract"
                )
                if enabled
                else "live_acquisition_contract_unavailable"
            ),
            "worker_alive": bool(thread is not None and thread.is_alive()),
            "interval_s": self._rtt_cadence_interval_s(),
            "probe_timeout_s": AUTOMATIC_RTT_PROBE_TIMEOUT_S,
            "last_attempt_ts": float(
                getattr(self, "_rtt_cadence_last_attempt_ts", 0.0) or 0.0
            ),
            "last_success_ts": float(
                getattr(self, "_rtt_cadence_last_success_ts", 0.0) or 0.0
            ),
            "last_error": str(getattr(self, "_rtt_cadence_last_error", "") or ""),
            "confirmation_active": bool(
                getattr(self, "_rtt_confirmation_active", False)
            ),
            "confirmation_targets": list(self._scheduled_rtt_targets()),
            "observation": self._ensure_rtt_observation_authority().status(),
        }

    def _acquire_maintenance_steps(
        self, request: BackendAcquisitionRequest, *,
        inspection=None,
        diagnostic=None,
    ) -> tuple[BackendAcquisitionReceipt, ...]:
        """Release the existing I/O gate between optional maintenance steps."""
        write_receipt = self.event_write_pending_status()["last"]

        def preempted() -> bool:
            return self.event_write_pending_status()["last"] != write_receipt

        method = ("iter_acquire_read_diagnostics" if diagnostic else
                  "iter_acquire_network_settings" if inspection else "iter_acquire_live_maintenance")
        args = (request, diagnostic) if diagnostic else (request,)
        steps = self._call_modem(method, *args,
            source="maintenance", lane="slow_live", lock_policy="always")
        receipts: list[BackendAcquisitionReceipt] = []
        try:
            while not self._modem_live_shutdown.is_set():
                if diagnostic and (
                    diagnostic.cancelled.is_set() or time.time() >= diagnostic.deadline
                    or self._live_register_store.runtime_namespace != diagnostic.namespace
                ):
                    diagnostic.finish(
                        "cancelled" if diagnostic.cancelled.is_set() else "timed_out"
                        if time.time() >= diagnostic.deadline else "interrupted",
                        "Diagnostics stopped; completed checks are retained.")
                    return ()
                if preempted():
                    self._mark_live_maintenance_skipped("event_write_preempted_sweep")
                    return ()
                if inspection and (
                    time.time() >= inspection.attempt_deadline
                    or self._live_register_store.runtime_namespace != inspection.namespace
                ):
                    raise NetworkSettingsReadIncomplete("network_inspection_expired_or_session_changed")
                # These are the live owner's existing pending-work flags, not
                # a second queue. An in-flight request still finishes normally.
                with self._live_register_lock:
                    live_busy = bool(
                        self._live_register_refreshing
                        or self._priority_live_register_refreshing
                    )
                if (
                    live_busy
                    or self._read_lane_request is not None
                    or self._event_write_pending_active()
                    or (
                        (inspection is not None or diagnostic is not None or self._live_acquisition_contract.maintenance_step_requires_fast_margin)
                        and not self._live_register_has_maintenance_margin(now=time.time())
                    )
                ):
                    self._modem_live_shutdown.wait(0.01)
                    continue

                def advance() -> tuple[bool, object]:
                    # Recheck under the same I/O gate as physical writes. A
                    # partial multi-read observation cannot cross a mutation.
                    if preempted():
                        return True, None
                    try:
                        return False, next(steps)
                    except StopIteration:
                        return True, None

                finished, result = self._modem_io(
                    source="maintenance", lane="slow_live",
                    operation="acquire_live_maintenance_step",
                    fn=advance, lock_policy="always",
                )
                if preempted():
                    self._mark_live_maintenance_skipped("event_write_preempted_sweep")
                    return ()
                if finished:
                    return tuple(receipts)
                if isinstance(result, BackendAcquisitionReceipt):
                    if result.request != request:
                        raise RuntimeError("modem_live_maintenance_request_mismatch")
                    if diagnostic is not None:
                        self._latch_acquisition_receipts((result,))
                    else:
                        receipts.append(result)
                elif result is not None:
                    raise TypeError("modem_live_maintenance_step_invalid")
            return tuple(receipts)
        except Exception:
            # A later optional failure must not discard an earlier completed
            # read. The caller still records the failure explicitly.
            if inspection is None and not preempted():
                self._latch_acquisition_receipts(tuple(receipts))
            raise
        finally:
            self._modem_io(
                source="maintenance", lane="slow_live",
                operation="close_live_maintenance_steps",
                fn=steps.close, lock_policy="always",
            )

    def _run_live_maintenance_cycle(
        self,
        *,
        now: float,
        request: Optional[BackendAcquisitionRequest] = None,
    ) -> tuple[BackendAcquisitionReceipt, ...]:
        receipts: list[BackendAcquisitionReceipt] = []
        try:
            if self._live_acquisition_contract.has_explicit_maintenance_refresh:
                slow_request = (
                    request
                    if request is not None
                    else self._new_backend_acquisition_request(
                        lane="slow_live",
                        source="maintenance",
                        started_at=now,
                    )
                )
                if slow_request.lane != "slow_live":
                    raise ValueError(
                        "modem_live_maintenance_request_lane_invalid"
                    )
                if self._live_acquisition_contract.maintenance_stepwise:
                    maintenance_receipts = self._acquire_maintenance_steps(slow_request)
                else:
                    receipt = self._call_modem(
                        "acquire_live_maintenance",
                        slow_request,
                        source="maintenance",
                        operation="acquire_live_maintenance",
                        lock_policy=maintenance_refresh_lock_policy(self._live_acquisition_contract),
                    )
                    maintenance_receipts = (receipt,)
                for receipt in maintenance_receipts:
                    if not isinstance(receipt, BackendAcquisitionReceipt):
                        raise TypeError(
                            "modem_live_maintenance_receipt_required"
                        )
                    if receipt.request != slow_request:
                        raise RuntimeError(
                            "modem_live_maintenance_request_mismatch"
                        )
                    if receipt.succeeded:
                        receipts.append(receipt)
        except Exception as exc:
            self._record_backend_fault("maintenance_refresh", exc)
            log.debug("LIVE MODULE maintenance refresh suppressed %s", exc)

        if neighbor_scan_due(
            contract=self._live_acquisition_contract,
            now=now,
            last_neighbor_scan=self._last_neighbor_scan,
            neighbor_scan_interval=self._neighbor_scan_interval,
        ):
            self._last_neighbor_scan = now
            try:
                neighbor_request = (
                    self._new_backend_acquisition_request(
                        lane="neighbor",
                        source="neighbor_scan",
                    )
                )
                neighbor_receipt = self._call_modem(
                    "acquire_neighbors",
                    neighbor_request,
                    source="maintenance",
                    operation="acquire_neighbors",
                    lock_policy=neighbor_read_lock_policy(self._live_acquisition_contract),
                )
                if not isinstance(
                    neighbor_receipt,
                    BackendAcquisitionReceipt,
                ):
                    raise TypeError(
                        "modem_live_neighbor_receipt_required"
                    )
                if neighbor_receipt.request != neighbor_request:
                    raise RuntimeError(
                        "modem_live_neighbor_request_mismatch"
                    )
                if neighbor_receipt.succeeded:
                    receipts.append(neighbor_receipt)
                    self._cached_neighbors = list(
                        neighbor_receipt.status_copy().get(
                            "neighbors",
                            [],
                        )
                        or []
                    )
            except Exception as exc:
                self._record_backend_fault("neighbor_scan", exc)
                log.debug("LIVE MODULE neighbor scan suppressed %s", exc)

        self._last_maintenance_refresh_ts = time.time()
        completed = tuple(receipts)
        self._latch_acquisition_receipts(completed)
        maintenance_primary = next(
            (
                receipt
                for receipt in completed
                if receipt.request.lane == "slow_live"
                and receipt.succeeded
                and receipt.modem is not None
            ),
            None,
        )
        if (
            maintenance_primary is not None
            and self._live_register_sequence() > 0
        ):
            self._publish_live_register_snapshot(
                primary_receipt=maintenance_primary,
                source="maintenance_live_register",
                backend_repair=None,
            )
        return completed

    def _publish_read_diagnostic(self, run):
        if self._live_register_store.runtime_namespace != run.namespace or self._modem_live_shutdown.is_set():
            return
        request = self._new_backend_acquisition_request(
            lane="slow_live", source="maintenance", started_at=time.time())
        # No physical read here: publish only the run owner's diagnostic state.
        receipt = self._modem_backend._capture_backend_acquisition_receipt(
            request=request, modem=None, acquired_cells=frozenset({"slow.read_diagnostic"}),
            status_override={"read_diagnostic": run.envelope()})
        self._latch_acquisition_receipts((receipt,))

    def _finish_read_diagnostic(self, run):
        run.adapter.observing = False
        if self._read_diagnostic_demand is run:
            from arch4.modem.read_diagnostics import DiagnosticMatrixComparisonConsumer
            if (not self._modem_live_shutdown.is_set()
                    and self._live_register_store.runtime_namespace == run.namespace
                    and isinstance(run.adapter, DiagnosticMatrixComparisonConsumer)):
                reader = self.committed_live_generation().matrix_reader()
                run.bind_matrix_comparison(reader, profile_id=self.input_cfg.modem_profile_id)
            self._publish_read_diagnostic(run)
            self._read_diagnostic_demand = None

    def request_read_diagnostics(self, *, action, request_id, expires_at):
        from arch4.modem.read_diagnostics import ReadDiagnosticRun, ReadDiagnosticUnavailable
        if action not in {"start", "cancel"}:
            raise ValueError("read_diagnostic_action_invalid")
        with self._maintenance_lock:
            run = self._read_diagnostic_demand
            if action == "cancel":
                if run is not None:
                    run.cancelled.set()
                return {"message": "Cancellation requested; the current read can finish." if run else "No diagnostic is running."}
            if run is not None:
                return {"message": "Read diagnostics are already queued or running.", "run_id": run.run_id}
            now = time.time()
            namespace = self._live_register_store.runtime_namespace
            if namespace is None or self._modem_live_shutdown.is_set():
                raise ReadDiagnosticUnavailable("Connect to the router before running read diagnostics.")
            deadline = min(float(expires_at), now + 180.)
            if not math.isfinite(deadline) or deadline <= now:
                raise ReadDiagnosticUnavailable("The diagnostic request expired before it started. Please try again.")
            adapter = self._modem_backend.prepare_read_diagnostics()
            run = ReadDiagnosticRun(request_id, namespace, deadline, adapter)
            self._read_diagnostic_demand = run
            self._publish_read_diagnostic(run)
            return {"message": "Router check requested; normal monitoring continues.", "run_id": run.run_id}

    def request_network_settings_inspection(self, *, expires_at: float) -> str:
        from .network_settings_demand import NetworkSettingsDemand
        now = time.time()
        expiry = float(expires_at)
        if not math.isfinite(expiry) or expiry <= now or self._modem_live_shutdown.is_set():
            raise RuntimeError("network_inspection_expired_or_stopped")
        with self._maintenance_lock:
            namespace = self._live_register_store.runtime_namespace
            if namespace is None:
                raise RuntimeError("network_inspection_requires_connected_namespace")
            # Request freshness admits one session-bound goal. Duplicate UI
            # events cannot queue another sweep or reset a retry deadline.
            current = self._network_inspection_demand
            if current is None or current.namespace != namespace:
                self._network_inspection_demand = NetworkSettingsDemand(namespace)
            return self._network_inspection_demand.demand_id

    def _start_live_maintenance_cycle(
        self,
        *,
        now: float,
        requested: bool = False,
        due_by_interval: bool = True,
        interval_s: Optional[float] = None,
    ) -> None:
        if self._modem_live_shutdown.is_set():
            return
        if requested and not due_by_interval:
            with self._maintenance_lock:
                demand = self._network_inspection_demand
                diagnostic = self._read_diagnostic_demand
                running = self._maintenance_thread is not None and self._maintenance_thread.is_alive()
                if diagnostic is not None and not running and (
                    diagnostic.cancelled.is_set() or now >= diagnostic.deadline
                    or diagnostic.namespace != self._live_register_store.runtime_namespace
                ):
                    diagnostic.finish("cancelled" if diagnostic.cancelled.is_set() else
                                      "timed_out" if now >= diagnostic.deadline else "interrupted",
                                      "Diagnostics stopped before another read; completed checks are retained.")
                    self._finish_read_diagnostic(diagnostic)
                    diagnostic = None
                if demand is None and diagnostic is None:
                    return
                if demand is not None and demand.namespace != self._live_register_store.runtime_namespace:
                    self._network_inspection_demand = None
                    self._ensure_access_scheduler().record_defer(
                        "maintenance", now=now, reason="network_inspection_session_changed",
                        requested=True, due=False)
                    if diagnostic is None:
                        return
                if demand is not None and now < demand.next_attempt_at and diagnostic is None:
                    return
        try:
            age_s = max(0.0, float(now) - float(getattr(self, "_last_maintenance_refresh_ts", 0.0) or 0.0))
        except Exception:
            age_s = 0.0
        fast_margin_ok = self._live_register_has_maintenance_margin(now=now)
        event_write_pending = self._event_write_pending_active()
        try:
            effective_interval_s = float(
                interval_s if interval_s is not None else self._resolve_live_maintenance_interval_s()
            )
        except Exception:
            effective_interval_s = 10.0
        with self._maintenance_lock:
            if self._modem_live_shutdown.is_set():
                return
            thread = self._maintenance_thread
            already_running = bool(thread is not None and thread.is_alive())
        decision = self._ensure_access_scheduler().decide_maintenance(
            now=now,
            requested=bool(requested),
            due_by_interval=bool(due_by_interval),
            fast_margin_ok=bool(fast_margin_ok),
            already_running=bool(already_running),
            event_write_pending=bool(event_write_pending),
            interval_s=effective_interval_s,
            age_s=age_s,
        )
        if str(decision.get("decision") or "") != "run":
            reason = str(decision.get("reason") or "maintenance_not_run")
            if reason in {"fast_live_priority_margin", "maintenance_already_running"}:
                self._mark_live_maintenance_skipped(reason)
            return
        with self._maintenance_lock:
            thread = self._maintenance_thread
            if thread is not None and thread.is_alive():
                self._ensure_access_scheduler().record_skip(
                    "maintenance",
                    now=now,
                    reason="maintenance_already_running",
                    requested=bool(requested),
                    due=True,
                )
                self._mark_live_maintenance_skipped("maintenance_already_running")
                return
            inspection = self._network_inspection_demand if requested and not due_by_interval else None
            if inspection is not None and now < inspection.next_attempt_at:
                inspection = None
            diagnostic = self._read_diagnostic_demand if requested and not due_by_interval and inspection is None else None
            if requested and not due_by_interval and inspection is None and diagnostic is None:
                return
            if inspection is None and diagnostic is None:
                self._last_maintenance_refresh_ts = now
            if inspection is not None:
                inspection.begin_attempt(now)

            def _worker() -> None:
                inspection_completed = False
                inspection_retryable = False
                try:
                    if diagnostic is not None:
                        request = self._new_backend_acquisition_request(lane="slow_live", source="maintenance", started_at=now)
                        self._acquire_maintenance_steps(request, diagnostic=diagnostic)
                    elif inspection is None:
                        self._run_live_maintenance_cycle(now=now)
                    else:
                        request = self._new_backend_acquisition_request(
                            lane="slow_live", source="maintenance", started_at=now,
                        )
                        receipts = self._acquire_maintenance_steps(request, inspection=inspection)
                        if self._modem_live_shutdown.is_set():
                            return
                        if self._live_register_store.runtime_namespace != inspection.namespace:
                            raise RuntimeError("network_inspection_session_changed")
                        if not any(r.succeeded and "slow.network_settings" in r.acquired_cells
                                   for r in receipts):
                            raise NetworkSettingsReadIncomplete("network_inspection_no_valid_settings_receipt")
                        self._latch_acquisition_receipts(receipts)
                        inspection_completed = True
                except Exception as exc:
                    inspection_retryable = isinstance(exc, (NetworkSettingsReadIncomplete, OSError))
                    self._record_backend_fault("async_maintenance", exc)
                    log.debug("LIVE MODULE async maintenance suppressed %s", exc)
                finally:
                    current = threading.current_thread()
                    with self._maintenance_lock:
                        if diagnostic is not None:
                            if not diagnostic.terminal:
                                diagnostic.finish("interrupted", "Diagnostics interrupted by shutdown, a settings change or an execution failure; completed checks are retained.")
                            self._finish_read_diagnostic(diagnostic)
                        if inspection is not None and self._network_inspection_demand is inspection:
                            if (inspection_completed or not inspection_retryable or self._modem_live_shutdown.is_set()
                                    or self._live_register_store.runtime_namespace != inspection.namespace):
                                self._network_inspection_demand = None
                            else:
                                inspection.retry_after(time.time(), self._resolve_live_maintenance_interval_s())
                        if self._maintenance_thread is current:
                            self._maintenance_thread = None

            self._maintenance_thread = threading.Thread(
                target=_worker,
                name="arch4-modem-live-maintenance",
                daemon=True,
            )
            self._maintenance_thread.start()

    def _live_register_has_maintenance_margin(self, *, now: float) -> bool:
        if not hasattr(self, "input_cfg") or not hasattr(self, "_live_register_lock"):
            return True
        if not self._clocked_live_register_enabled():
            return True
        contract = getattr(self, "_live_acquisition_contract", LiveAcquisitionContract())
        with self._live_register_lock:
            current = self._live_register_store.current_status()
            updated_at = float(current.sampled_at)
            sequence = int(current.sequence)
            refreshing = bool(self._live_register_refreshing)
        return live_register_has_maintenance_margin(
            clocked_register_enabled=True,
            contract=contract,
            sequence=sequence,
            updated_at=updated_at,
            refreshing=refreshing,
            now=now,
            live_register_interval_s=self._live_register_interval_s,
            required_margin_s=self._live_maintenance_required_margin_s(),
            completion_ordered_steps=(
                contract.maintenance_stepwise
                and (bool(getattr(contract, "maintenance_completion_ordered", False))
                     or self._live_access_profile().dev2_state_read_gap_s == 0.0)
            ),
        )

    def read_live_modem_snapshot(self) -> Optional[ModemInput]:
        raise RuntimeError(
            "live_modem_snapshot_projection_forbidden:"
            "use_accepted_root_outbox"
        )

    def shutdown(
        self,
        *,
        join_timeout_s: float = 1.0,
    ) -> ModemLiveTerminationReceipt:
        shutdown_started_at = float(time.time())
        timeout_s = max(0.0, float(join_timeout_s or 0.0))
        deadline = time.monotonic() + timeout_s
        self._modem_live_shutdown.set()
        with self._priority_live_register_lifecycle_lock:
            with self._live_register_lock:
                register_stop = self._live_register_worker_stop
                if isinstance(register_stop, threading.Event):
                    register_stop.set()
                register_thread = self._live_register_thread
                priority_thread = self._priority_live_register_refresh_thread
        with self._rtt_cadence_lock:
            rtt_stop = self._rtt_cadence_stop
            if isinstance(rtt_stop, threading.Event):
                rtt_stop.set()
            rtt_thread = self._rtt_cadence_thread
        with self._read_lane_changed:
            self._read_lane_stop.set()
            read_lane_thread = self._read_lane_thread
            self._read_lane_changed.notify_all()
        with self._traffic_presence_lifecycle_lock:
            with self._traffic_presence_lock:
                self._traffic_presence_active = False
                self._traffic_presence_interval_s = 0.0
                traffic_stop = self._traffic_presence_stop
                traffic_thread = self._traffic_presence_thread
            if isinstance(traffic_stop, threading.Event):
                traffic_stop.set()
        with self._non_ping_probe_lifecycle_lock:
            with self._non_ping_probe_lock:
                non_ping_thread = self._non_ping_probe_thread
        with self._maintenance_lock:
            maintenance_thread = self._maintenance_thread
        threads = tuple(
            thread
            for thread in (
                register_thread,
                rtt_thread,
                read_lane_thread,
                priority_thread,
                non_ping_thread,
                traffic_thread,
                maintenance_thread,
            )
            if thread is not None
        )
        current = threading.current_thread()
        for thread in threads:
            if thread.is_alive() and thread is not current:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        scout_survivors = self._scout.shutdown(
            timeout_s=max(0.0, deadline - time.monotonic())
        )
        survivors = tuple(
            thread.name
            for thread in threads
            if thread.is_alive()
        ) + tuple(scout_survivors)
        with self._live_register_publication_changed:
            publication_sink_bound = (
                self._live_register_publication_sink is not None
            )
            health_sink_bound = (
                self._current_health_publication_sink is not None
            )
            publication_inflight = (
                self._live_register_publication_inflight
            )
            health_inflight = self._current_health_publication_inflight
            namespace = (
                self._live_register_store.runtime_namespace
                if self._live_register_store.sequence() > 0
                else None
            )
        receipt = ModemLiveTerminationReceipt(
            namespace=namespace,
            lifecycle_generation=int(
                self._product_lifecycle_generation
            ),
            shutdown_started_at=shutdown_started_at,
            publication_sink_bound=publication_sink_bound,
            health_sink_bound=health_sink_bound,
            publication_inflight_callbacks=publication_inflight,
            health_inflight_callbacks=health_inflight,
            survivors=survivors,
            finished_at=float(time.time()),
        )
        if receipt.terminated:
            with self._traffic_presence_lock:
                self._traffic_presence_thread = None
                self._traffic_presence_stop = None
            _LIVE_INPUT_MODULES.discard(self)
        return receipt

    def refresh_thresholds(self) -> None:
        """Rebuild configuration views after a profile update."""
        from arch4.config_views import build_input_module_config, build_signal_contract_config
        self.input_cfg = build_input_module_config(self.cfg)
        self.signal_contract_cfg = build_signal_contract_config(self.cfg)
        self._refresh_live_acquisition_contract_from_config()
        self._refresh_live_register_timing_from_config(restart_worker=True)

    def _maybe_run_multi_target_health_probes(
        self,
        primary_rtt: Optional[float],
        now: float,
        *,
        prefer_non_ping: bool = False,
        force_refresh_non_ping: bool = False,
    ) -> Dict[str, bool]:
        needs_non_ping = any(str(v).strip() for v in self.input_cfg.health_dns_targets) or any(
            str(v).strip() for v in self.input_cfg.health_http_targets
        )
        if not (
            self.input_cfg.health_multi_target_enabled
            or prefer_non_ping
            or force_refresh_non_ping
        ):
            return {}
        interval = max(0.0, self.input_cfg.health_probe_interval_s)
        timeout_s = max(0.3, self.input_cfg.health_probe_timeout_s)
        out: Dict[str, bool] = {}
        primary = self.input_cfg.rtt_probe_target
        out[f"ping:{primary}"] = bool(
            primary_rtt is not None and float(primary_rtt) <= self.input_cfg.health_ping_rtt_max_ms
        )

        # Secondary ping probes run synchronously through the backend. When DNS
        # or HTTP targets exist, keep those checks on the async lane and avoid
        # adding modem I/O to the fast cycle.
        should_probe_secondary_ping = not needs_non_ping
        if should_probe_secondary_ping:
            seen_ping = {primary}
            for target in self.input_cfg.health_ping_targets:
                t = str(target).strip()
                if not t or t in seen_ping:
                    continue
                seen_ping.add(t)
                rtt = self._call_modem(
                    "measure_rtt",
                    source="health_probe",
                    operation="secondary_ping",
                    lock_policy="rtt",
                    target=t,
                    count=1,
                )
                out[f"ping:{t}"] = bool(
                    rtt is not None and float(rtt) <= self.input_cfg.health_ping_rtt_max_ms
                )

        if not needs_non_ping:
            return out

        cached_non_ping: Dict[str, bool] = {}
        last_non_ping_ts = 0.0
        should_start = False
        with self._non_ping_probe_lifecycle_lock:
            with self._non_ping_probe_lock:
                cached_non_ping = dict(self._cached_non_ping_probe_results)
                last_non_ping_ts = float(self._last_non_ping_probe_ts)
                if force_refresh_non_ping:
                    cached_non_ping = {}
                if (
                    not self._modem_live_shutdown.is_set()
                    and not self._non_ping_probe_running
                    and (
                        force_refresh_non_ping
                        or not cached_non_ping
                        or (now - last_non_ping_ts) >= interval
                    )
                ):
                    self._non_ping_probe_running = True
                    should_start = True
            if should_start:
                worker = threading.Thread(
                    target=self._run_non_ping_health_probes,
                    kwargs={"timeout_s": timeout_s},
                    daemon=True,
                    name="arch4-health-probes",
                )
                with self._non_ping_probe_lock:
                    self._non_ping_probe_thread = worker
                try:
                    worker.start()
                except Exception:
                    with self._non_ping_probe_lock:
                        if self._non_ping_probe_thread is worker:
                            self._non_ping_probe_thread = None
                            self._non_ping_probe_running = False
                    raise

        if not cached_non_ping:
            if prefer_non_ping:
                for host in self.input_cfg.health_dns_targets:
                    h = str(host).strip()
                    if h:
                        out[f"dns:{h}"] = False
                for url in self.input_cfg.health_http_targets:
                    u = str(url).strip()
                    if u:
                        out[f"http:{u}"] = False
                return out
            # Use primary health state until the non-ping cache is ready.
            return {}

        out.update(cached_non_ping)
        return out

    def _run_non_ping_health_probes(self, *, timeout_s: float) -> None:
        out: Dict[str, bool] = {}
        current = threading.current_thread()
        try:
            for host in self.input_cfg.health_dns_targets:
                h = str(host).strip()
                if not h:
                    continue
                out[f"dns:{h}"] = self._probe_dns(h, timeout_s=timeout_s)

            for url in self.input_cfg.health_http_targets:
                u = str(url).strip()
                if not u:
                    continue
                out[f"http:{u}"] = self._probe_http(u, timeout_s=timeout_s)
        finally:
            with self._non_ping_probe_lock:
                self._cached_non_ping_probe_results = dict(out)
                self._last_non_ping_probe_ts = time.time()
                self._non_ping_probe_running = False
                if self._non_ping_probe_thread is current:
                    self._non_ping_probe_thread = None

    @staticmethod
    def _probe_dns(host: str, timeout_s: float) -> bool:
        del timeout_s
        try:
            info = socket.getaddrinfo(host, None)
            return bool(info)
        except Exception as exc:
            log.debug("_dns_resolves: suppressed %s", exc)
            return False

    @staticmethod
    def _probe_http(url: str, timeout_s: float) -> bool:
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                code = int(getattr(resp, "status", 200) or 200)
                return code < 500
        except Exception as exc:
            log.debug("_http_reachable: suppressed %s", exc)
            return False
