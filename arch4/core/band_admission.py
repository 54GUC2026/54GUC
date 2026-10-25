"""The only Core admission owner for LTE/NR band targets from every origin."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from arch4.core.lever_action_adapter import (
    CoreLeverActionAdapter,
    lever_action_authority,
)
from arch4.core_contracts.commands import (
    BAND_AUTO_COMMAND_KIND,
    CoreCommandRequest,
    CoreCommandOrigin,
)
from arch4.core_contracts.accepted_runtime_view import (
    CoreAcceptedRuntimeView,
)
from arch4.core_contracts.command_targets import (
    CoreBandAutoTarget,
    CoreBandSetTarget,
    CoreNrBandTarget,
    CorePBandTarget,
)
from arch4.interfaces import LeverAction, LeverType
from arch4.modem_live.session_band_capability import SessionBandCapability


CORE_BAND_ADMISSION_OWNER = "54guc.core.band_admission"
FOUNDATION_PROVIDER_BAND_PROFILE_SCHEMA = "FoundationProviderBandProfile.v1"
FOUNDATION_PROVIDER_BAND_PROFILE_OWNER = "54guc.foundation.band_auto_policy"

ProviderBandPolicyResolver = Callable[..., Mapping[str, Any]]


class CoreBandAdmissionError(ValueError):
    def __init__(self, code: str, *, detail: Mapping[str, Any] | None = None) -> None:
        self.code = str(code or "core_band_admission_rejected")
        self.detail = dict(detail or {})
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class CoreBandAdmissionDecision:
    action: LeverAction
    detail: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CoreBandCeiling:
    """Bands that Core may send in this session. Prefer bands proven live by Modem Live. If
    that probe is temporarily missing, use the reviewed profile limit. This keeps manual
    control available without allowing unknown bands.
    """

    rat: str
    bands: tuple[int, ...]
    source: str
    raw_status: str
    capability_id: str


def _positive_bands(value: Any, *, field_name: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise CoreBandAdmissionError(
            "core_band_target_list_required", detail={"field": field_name}
        )
    out: set[int] = set()
    for raw in value:
        if isinstance(raw, bool):
            raise CoreBandAdmissionError(
                "core_band_target_value_invalid", detail={"field": field_name}
            )
        try:
            band = int(raw)
        except (TypeError, ValueError) as exc:
            raise CoreBandAdmissionError(
                "core_band_target_value_invalid", detail={"field": field_name}
            ) from exc
        if band <= 0:
            raise CoreBandAdmissionError(
                "core_band_target_value_invalid", detail={"field": field_name}
            )
        out.add(band)
    return tuple(sorted(out))


def _session_capability(
    view: CoreAcceptedRuntimeView,
) -> SessionBandCapability:
    facts = view.capability_facts.to_mapping()
    raw = facts.get("session_band_capability")
    if not isinstance(raw, Mapping):
        raise CoreBandAdmissionError(
            "core_band_session_capability_missing"
        )
    capability = SessionBandCapability.from_mapping(raw)
    if capability.session_scope_key != view.matrix_identity.session_scope_key:
        raise CoreBandAdmissionError(
            "core_band_session_capability_scope_mismatch"
        )
    return capability


def _effective_ceiling_for_rat(
    capability: SessionBandCapability,
    *,
    rat: str,
) -> CoreBandCeiling:
    if rat == "lte":
        status = capability.lte_status
        bands = capability.supported_lte_bands
    elif rat == "nr":
        status = capability.nr_status
        bands = capability.supported_nr_bands
    else:
        raise CoreBandAdmissionError("core_band_rat_invalid")
    if status in {"proven", "seeded"} and bands:
        return CoreBandCeiling(
            rat=rat,
            bands=tuple(bands),
            source=(
                "modem_live_proven"
                if status == "proven"
                else "modem_live_profile_seed"
            ),
            raw_status=status,
            capability_id=capability.capability_id,
        )
    if status == "unsupported":
        raise CoreBandAdmissionError(
            f"core_band_{rat}_unsupported_by_session",
            detail={
                "rat": rat,
                "status": status,
                "capability_id": capability.capability_id,
            },
        )
    raise CoreBandAdmissionError(
        f"core_band_{rat}_effective_ceiling_unavailable",
        detail={
            "rat": rat,
            "status": status,
            "capability_id": capability.capability_id,
        },
    )


def _unsupported_target_error(
    *,
    rat: str,
    requested: tuple[int, ...],
    unsupported: tuple[int, ...],
    ceiling: CoreBandCeiling,
) -> CoreBandAdmissionError:
    return CoreBandAdmissionError(
        "core_band_target_unsupported_by_effective_ceiling",
        detail={
            "rat": rat,
            "requested_bands": list(requested),
            "unsupported_bands": list(unsupported),
            "effective_bands": list(ceiling.bands),
            "ceiling_source": ceiling.source,
            "raw_capability_status": ceiling.raw_status,
            "capability_id": ceiling.capability_id,
        },
    )


def _ceiling_detail(ceiling: CoreBandCeiling) -> dict[str, Any]:
    return {
        "rat": ceiling.rat,
        "effective_bands": list(ceiling.bands),
        "ceiling_source": ceiling.source,
        "raw_capability_status": ceiling.raw_status,
        "capability_id": ceiling.capability_id,
    }


def _requested_targets(
    request: CoreCommandRequest,
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    target = request.target
    if isinstance(target, CoreBandSetTarget):
        return (("lte", target.bands),)
    if isinstance(target, CorePBandTarget):
        values = tuple(
            sorted(
                {
                    *((target.pband,) if target.pband > 0 else ()),
                    *target.ca_pool,
                }
            )
        )
        return (("lte", values),)
    if isinstance(target, CoreNrBandTarget):
        return (("nr", target.nr_bands),)
    return ()


class CoreBandAdmission:
    """Resolve BAND AUTO and validate each explicit band target once."""

    def __init__(self, adapter: CoreLeverActionAdapter) -> None:
        if not isinstance(adapter, CoreLeverActionAdapter):
            raise TypeError("core_band_admission_adapter_required")
        self._adapter = adapter
        self._provider_policy: ProviderBandPolicyResolver | None = None

    def bind_provider_policy(self, resolver: ProviderBandPolicyResolver) -> None:
        if not callable(resolver):
            raise TypeError("core_band_provider_policy_callable_required")
        if self._provider_policy is not None:
            raise RuntimeError("core_band_provider_policy_already_bound")
        self._provider_policy = resolver

    def _provider_contract(self, view, *, domain: str, rat: str):
        facts = view.capability_facts.to_mapping()
        provider_plmn = str(facts.get("provider_plmn") or "").strip()
        if not provider_plmn:
            raise CoreBandAdmissionError(
                "core_band_auto_provider_identity_required"
            )
        resolver = self._provider_policy
        if resolver is None:
            raise CoreBandAdmissionError(
                "core_band_auto_provider_policy_unbound"
            )
        try:
            policy = dict(
                resolver(domain=domain, provider_plmn=provider_plmn, runtime_view=view) or {}
            )
        except (LookupError, ValueError) as exc:
            raise CoreBandAdmissionError(str(exc)) from exc
        if (
            policy.get("schema") != FOUNDATION_PROVIDER_BAND_PROFILE_SCHEMA
            or policy.get("owner") != FOUNDATION_PROVIDER_BAND_PROFILE_OWNER
            or str(policy.get("domain") or "") != domain
            or str(policy.get("rat") or "") != rat
            or str(policy.get("band_auto_provider_plmn") or "") != provider_plmn
        ):
            raise CoreBandAdmissionError(
                "core_band_auto_provider_policy_contract_invalid"
            )
        return policy, provider_plmn

    def _auto_action(
        self,
        request: CoreCommandRequest,
        view: CoreAcceptedRuntimeView,
    ) -> CoreBandAdmissionDecision:
        target = request.target
        if not isinstance(target, CoreBandAutoTarget):
            raise CoreBandAdmissionError("core_band_auto_target_kind_mismatch")
        domain = target.domain
        domain_rat = {
            "lte_allocated_auto": "lte",
            "nr_approved_auto": "nr",
        }
        rat = domain_rat.get(domain)
        if rat is None:
            raise CoreBandAdmissionError("core_band_auto_domain_invalid")
        policy, provider_plmn = self._provider_contract(view, domain=domain, rat=rat)
        intended = _positive_bands(policy.get("restore_pool_bands"), field_name="policy.restore_pool_bands")
        if not intended:
            raise CoreBandAdmissionError("core_band_auto_provider_policy_empty")
        capability = _session_capability(view)
        ceiling = _effective_ceiling_for_rat(capability, rat=rat)
        executable = tuple(band for band in intended if band in set(ceiling.bands))
        excluded = tuple(band for band in intended if band not in set(ceiling.bands))
        if not executable:
            raise CoreBandAdmissionError(
                "core_band_auto_no_supported_provider_band",
                detail={
                    "domain": domain,
                    "provider_bands": list(intended),
                    "effective_bands": list(ceiling.bands),
                    "ceiling_source": ceiling.source,
                    "raw_capability_status": ceiling.raw_status,
                    "capability_id": ceiling.capability_id,
                },
            )
        physical_lever = (
            LeverType.BAND_SELECTION if rat == "lte" else LeverType.NR_BAND_LOCK
        )
        required_capability = lever_action_authority(
            physical_lever
        ).capability_key
        if request.capability_reference != required_capability:
            raise CoreBandAdmissionError(
                "core_band_auto_capability_reference_mismatch"
            )
        physical_target = (
            CoreBandSetTarget(
                command_kind=physical_lever.name,
                bands=tuple(executable),
                release_manual=True,
            )
            if rat == "lte"
            else CoreNrBandTarget(nr_bands=tuple(executable), release_manual=True)
        )
        if not isinstance(request.parameters.to_mapping().get("action_parameters", {}), Mapping):
            raise CoreBandAdmissionError("core_band_auto_action_parameters_invalid")
        admitted_evidence = {
            "band_auto_domain": domain,
            "band_auto_policy_owner": str(policy.get("owner") or ""),
            "band_auto_policy_revision": str(
                policy.get("band_auto_policy_revision") or ""
            ),
            "band_auto_provider_plmn": provider_plmn,
            "band_auto_provider_target": list(intended),
            "band_auto_device_excluded": list(excluded),
            "session_band_capability_id": ceiling.capability_id,
            "band_auto_ceiling_source": ceiling.source,
            "band_auto_raw_capability_status": ceiling.raw_status,
        }
        # AUTO resolves a physical target, not a new public request. The
        # adapter keeps Command Authority's original identity and origin.
        action = self._adapter.materialize(
            request,
            resolved_auto_target=physical_target,
            admitted_auto_evidence=admitted_evidence,
        )
        return CoreBandAdmissionDecision(
            action=action,
            detail={
                "owner": CORE_BAND_ADMISSION_OWNER,
                "mode": "provider_auto",
                "rat": rat,
                "domain": domain,
                "provider_target": list(intended),
                "supported_target": list(executable),
                "excluded_provider_bands": list(excluded),
                "ceiling": _ceiling_detail(ceiling),
                "capability_id": ceiling.capability_id,
            },
        )

    def admit(
        self,
        request: CoreCommandRequest,
        view: CoreAcceptedRuntimeView,
    ) -> CoreBandAdmissionDecision:
        if request.command_kind == BAND_AUTO_COMMAND_KIND:
            return self._auto_action(request, view)
        action = self._adapter.materialize(request)
        targets = tuple(
            (rat, requested)
            for rat, requested in _requested_targets(request)
            if requested
        )
        if not targets:
            return CoreBandAdmissionDecision(
                action=action,
                detail={
                    "owner": CORE_BAND_ADMISSION_OWNER,
                    "mode": "not_band_target",
                },
            )
        capability = _session_capability(view)
        admitted_targets: list[dict[str, Any]] = []
        emergency = request.parameters.to_mapping().get("intent") == "emergency_radio_down_recover"
        if emergency:
            policy, _ = self._provider_contract(view, domain="lte_allocated_auto", rat="lte")
            ceiling = _effective_ceiling_for_rat(capability, rat="lte")
            allowed = tuple(b for b in _positive_bands(policy.get("bands"), field_name="policy.bands") if b in ceiling.bands)
            if not isinstance(request.target, CoreBandSetTarget) or request.target.bands != allowed or not allowed:
                raise CoreBandAdmissionError("core_emergency_rescue_requires_all_allowed_lte_bands")
        for rat, requested in targets:
            if (
                request.origin is CoreCommandOrigin.AUTONOMOUS_INTELLIGENCE
                and rat == "lte"
                and len(requested) < 2
                and not emergency
            ):
                raise CoreBandAdmissionError("core_brain_lte_pool_requires_two_bands")
            ceiling = _effective_ceiling_for_rat(capability, rat=rat)
            unsupported = tuple(
                band for band in requested if band not in set(ceiling.bands)
            )
            if unsupported:
                raise _unsupported_target_error(
                    rat=rat,
                    requested=requested,
                    unsupported=unsupported,
                    ceiling=ceiling,
                )
            admitted_targets.append(
                {
                    "rat": rat,
                    "requested_bands": list(requested),
                    "ceiling_source": ceiling.source,
                    "raw_capability_status": ceiling.raw_status,
                }
            )
        return CoreBandAdmissionDecision(
            action=action,
            detail={
                "owner": CORE_BAND_ADMISSION_OWNER,
                "mode": "explicit_target",
                "targets": admitted_targets,
                "capability_id": capability.capability_id,
            },
        )


__all__ = (
    "BAND_AUTO_COMMAND_KIND",
    "CORE_BAND_ADMISSION_OWNER",
    "CoreBandAdmission",
    "CoreBandAdmissionDecision",
    "CoreBandAdmissionError",
)
