"""BAND AUTO policy for each radio type. This module defines the allowed band limit. It
does not publish GUI state, admit commands, read modem state, or perform modem I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable
import re

from arch4.core_contracts.accepted_runtime_view import CoreAcceptedRuntimeView
from arch4.customer_contracts.network_suggestions import NetworkSuggestions
from arch4.foundation.network_country_reference import country_verdict, name_token


BAND_AUTO_POLICY_OWNER = "54guc.foundation.band_auto_policy"
BAND_AUTO_POLICY_REVISION = "network_evidence_v2"


class BandAutoDomain(str, Enum):
    LTE_ALLOCATED = "lte_allocated_auto"
    NR_APPROVED = "nr_approved_auto"


@dataclass(frozen=True, slots=True)
class BandAutoTarget:
    domain: BandAutoDomain
    bands: tuple[int, ...]
    region_key: str
    provider_plmn: str
    owner: str = BAND_AUTO_POLICY_OWNER
    revision: str = BAND_AUTO_POLICY_REVISION

    def as_contract(self) -> dict[str, Any]:
        return {
            "schema": "FoundationProviderBandProfile.v1",
            "owner": self.owner,
            "domain": self.domain.value,
            "rat": (
                "lte"
                if self.domain is BandAutoDomain.LTE_ALLOCATED
                else "nr"
            ),
            "bands": list(self.bands),
            "restore_pool_bands": list(self.bands),
            "band_auto_domain": self.domain.value,
            "band_auto_policy_owner": self.owner,
            "band_auto_policy_revision": self.revision,
            "band_auto_region_key": self.region_key,
            "band_auto_provider_plmn": self.provider_plmn,
            "observed_history_allowed": True,
        }

    def action_metadata(self) -> dict[str, Any]:
        """Return non-target evidence for the admitted physical action."""

        contract = self.as_contract()
        return {
            key: contract[key]
            for key in (
                "band_auto_domain",
                "band_auto_policy_owner",
                "band_auto_policy_revision",
                "band_auto_region_key",
                "band_auto_provider_plmn",
            )
        }


# Spectrum assignments for the configured regional operator context.
# These are researched entries, not a country admission whitelist. Missing
# entries use observed evidence; they never borrow another provider's list.
_PROVIDER_ALLOCATIONS = {
    plmn: {BandAutoDomain.LTE_ALLOCATED: (1, 3, 7, 20, 28),
           BandAutoDomain.NR_APPROVED: (7, 28, 78)}
    for plmn in ("22001", "22003", "22005")
}


def _domain(value: BandAutoDomain | str) -> BandAutoDomain:
    if isinstance(value, BandAutoDomain):
        return value
    text = str(value or "").strip().lower()
    aliases = {
        "4g": BandAutoDomain.LTE_ALLOCATED,
        "lte": BandAutoDomain.LTE_ALLOCATED,
        BandAutoDomain.LTE_ALLOCATED.value: BandAutoDomain.LTE_ALLOCATED,
        "5g": BandAutoDomain.NR_APPROVED,
        "nr": BandAutoDomain.NR_APPROVED,
        BandAutoDomain.NR_APPROVED.value: BandAutoDomain.NR_APPROVED,
    }
    try:
        return aliases[text]
    except KeyError as exc:
        raise ValueError("band_auto_domain_unsupported") from exc


def _supported(values: Iterable[Any]) -> frozenset[int]:
    normalized: set[int] = set()
    for value in values:
        try:
            band = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("band_auto_supported_band_invalid") from exc
        if band <= 0:
            raise ValueError("band_auto_supported_band_invalid")
        normalized.add(band)
    return frozenset(normalized)


def resolve_band_auto_target(
    domain: BandAutoDomain | str,
    *,
    provider_plmn: str = "",
    supported_bands: Iterable[Any] | None = None,
    observed_bands: Iterable[Any] = (),
) -> BandAutoTarget:
    """Resolve one RAT-specific policy ceiling to an explicit band target."""

    resolved_domain = _domain(domain)
    plmn = str(provider_plmn or "").strip()
    if not re.fullmatch(r"[0-9]{5,6}", plmn):
        raise LookupError("band_auto_provider_identity_required")
    allocated = _PROVIDER_ALLOCATIONS.get(plmn, {}).get(resolved_domain, ())
    ceiling = tuple(sorted(set(allocated) | _supported(observed_bands)))
    bands = tuple(ceiling)
    if supported_bands is not None:
        supported = _supported(supported_bands)
        if not supported:
            raise ValueError("band_auto_supported_bands_empty")
        bands = tuple(band for band in ceiling if band in supported)
        if not bands:
            raise ValueError("band_auto_policy_device_intersection_empty")

    return BandAutoTarget(
        domain=resolved_domain,
        bands=bands,
        region_key="mcc_" + plmn[:3],
        provider_plmn=plmn,
    )


class ProviderBandProfileAuthority:
    """Foundation owner exposed to Core through one explicit read-only port."""

    def __init__(self, suggestions: NetworkSuggestions | None = None):
        self.suggestions = suggestions or NetworkSuggestions()

    @staticmethod
    def _observed(domain, view):
        if view is None:
            return ()
        if not isinstance(view, CoreAcceptedRuntimeView):
            raise TypeError("provider_policy_accepted_runtime_view_required")
        if view.identity_status != "confirmed" or not view.radio_connected:
            return ()
        radio = view.radio_target
        if _domain(domain) is BandAutoDomain.LTE_ALLOCATED and radio.serving_band_available:
            return (radio.primary_band,)
        # nr_bands is a configured mask, not proof of an active NR carrier.
        return ()

    def resolve_contract(
        self,
        *,
        domain: BandAutoDomain | str,
        provider_plmn: str,
        runtime_view: CoreAcceptedRuntimeView | None = None,
    ) -> dict[str, Any]:
        plmn = str(provider_plmn or "").strip()
        if not plmn:
            raise LookupError("band_auto_provider_identity_required")
        if runtime_view is not None:
            if not isinstance(runtime_view, CoreAcceptedRuntimeView):
                raise TypeError("provider_policy_accepted_runtime_view_required")
            if (runtime_view.identity_status != "confirmed"
                    or str(runtime_view.capability_facts.to_mapping().get("provider_plmn") or "") != plmn):
                raise ValueError("provider_policy_current_network_mismatch")
        policy = resolve_band_auto_target(
            domain,
            provider_plmn=plmn,
            observed_bands=self._observed(domain, runtime_view),
        ).as_contract()
        if runtime_view is not None:
            radio = runtime_view.radio_target
            configured = (radio.selected_bands if radio.selected_bands_available else ()) if _domain(domain) is BandAutoDomain.LTE_ALLOCATED else radio.nr_bands
            # AUTO may preserve a configured option, but it does not prove that
            # option has served traffic or qualify it as a primary candidate.
            policy["restore_pool_bands"] = sorted(set(policy["bands"]) | set(configured))
        return policy

    def verify_suggestions(self, view: CoreAcceptedRuntimeView) -> dict[str, Any]:
        if not isinstance(view, CoreAcceptedRuntimeView):
            raise TypeError("provider_policy_accepted_runtime_view_required")
        facts = view.capability_facts.to_mapping()
        reported_plmn = str(facts.get("provider_plmn") or "").strip()
        plmn = reported_plmn if view.identity_status == "confirmed" and re.fullmatch(r"[0-9]{5,6}", reported_plmn) else ""
        provider = str(facts.get("provider_name") or "") if plmn else ""
        country_state, country = country_verdict(self.suggestions.country, plmn)
        hint = self.suggestions.provider
        provider_state = "not_requested" if not hint else "not_verified"
        if hint and plmn:
            if hint == plmn or (name_token(hint) and name_token(hint) == name_token(provider)):
                provider_state = "confirmed"
            elif re.fullmatch(r"[0-9]{5,6}", hint):
                provider_state = "mismatch"
        capability = facts.get("session_band_capability") or {}
        rows = []
        for rat, band in self.suggestions.band_items():
            device_bands = capability.get(f"supported_{rat}_bands", ())
            device_status = capability.get(f"{rat}_status")
            state = "not_verified"
            if device_status == "unsupported" or (device_status in {"proven", "seeded"} and band not in device_bands):
                state = "unsupported"
            elif plmn and band in device_bands:
                domain = "lte" if rat == "lte" else "nr"
                if band in self.resolve_contract(domain=domain, provider_plmn=plmn, runtime_view=view)["bands"]:
                    state = "confirmed"
            rows.append({"band": ("B" if rat == "lte" else "n") + str(band), "state": state})
        labels = {"confirmed": "confirmed", "mismatch": "does not match", "not_verified": "not verified",
                  "not_requested": "detected", "unsupported": "not supported by router"}
        lines = [f"Country: {country} ({labels[country_state]})" if country else "Country: not verified",
                 f"Provider: {provider or plmn} ({labels[provider_state]})" if plmn else "Provider: not verified"]
        if rows:
            lines.append("Bands: " + "; ".join(f"{r['band']} {labels[r['state']]}" for r in rows))
        return {"schema": "FoundationNetworkVerification.v1", "owner": BAND_AUTO_POLICY_OWNER,
                "provider_plmn": plmn, "country": country_state, "provider": provider_state,
                "bands": rows, "text": "\n".join(lines)}


__all__ = (
    "BAND_AUTO_POLICY_OWNER",
    "BAND_AUTO_POLICY_REVISION",
    "BandAutoDomain",
    "BandAutoTarget",
    "ProviderBandProfileAuthority",
    "resolve_band_auto_target",
)
