"""Map Modem Profile fields to Matrix cells and dispositions.

The accepted profile record belongs to Modem Live. This is the only mapping
from profile names to Matrix names. Scout may inspect it, but no other code may
create another mapping.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from .truth_matrix import MODEM_LIVE_TRUTH_FIELD_CATALOG


PROFILE_MATRIX_SUPPORTED = "supported"
PROFILE_MATRIX_SYNTHETIC = "synthetic"
PROFILE_MATRIX_UNSUPPORTED = "unsupported"
PROFILE_MATRIX_UNTESTED = "untested"
PROFILE_MATRIX_DISPOSITIONS = frozenset(
    {
        PROFILE_MATRIX_SUPPORTED,
        PROFILE_MATRIX_SYNTHETIC,
        PROFILE_MATRIX_UNSUPPORTED,
        PROFILE_MATRIX_UNTESTED,
    }
)

# These lines describe modem and profile observations. If an exact profile does
# not classify a cell, it is untested rather than silently missing. Runtime
# health and timing are excluded because they describe CGF, not modem ability.
PROFILE_MATRIX_LINES = frozenset(
    {"radio", "rf", "traffic", "slow", "identity", "capability"}
)

# The selected profile capability contract defines feature availability. These
# fields must not also appear in ``field_trust`` because two declarations can
# disagree.
PROFILE_CAPABILITY_MATRIX_ADDRESSES: Mapping[str, tuple[str, ...]] = (
    MappingProxyType(
        {
            "supports_band_selection": ("capability.ca_pool_control",),
            "supports_pband": ("capability.pband_control",),
            "supports_band_lock": ("capability.band_lock",),
            "supports_cell_lock": (
                "capability.cell_lock",
                "capability.pci_lock",
            ),
            "supports_restart": ("capability.restart",),
            "supports_ue_capability": ("capability.ue_info",),
            "supports_ca_details": (
                "radio.carrier_details",
                "capability.ca_details",
            ),
            "supports_neighbors": (
                "slow.neighbors",
                "slow.neighbor_count",
                "capability.neighbors_support",
            ),
            "supports_band_priority": ("capability.band_priority",),
        }
    )
)
PROFILE_CAPABILITY_OWNED_FIELDS = frozenset(
    {
        "pband_control",
        "ca_pool_control",
        "band_lock",
        "cell_lock",
        "restart",
        "ue_info",
        "carrier_details",
        "neighbors",
        "band_priority",
        "pci_lock_available",
    }
)


def _canonical_field_addresses() -> dict[str, str]:
    addresses: dict[str, str] = {}
    for line, fields in MODEM_LIVE_TRUTH_FIELD_CATALOG.items():
        for field in fields:
            address = f"{line}.{field}"
            prior = addresses.get(field)
            if prior is not None and prior != address:
                raise RuntimeError(
                    f"profile_matrix_field_address_ambiguous:{field}"
                )
            addresses[field] = address
    return addresses


_DIRECT_FIELD_ADDRESSES = _canonical_field_addresses()

# Profile names whose Matrix spelling differs. These are CGF names, not vendor
# aliases. Vendor aliases are translated before this step.
PROFILE_FIELD_MATRIX_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "plmn": "identity.provider_plmn",
        "earfcn_ul": "rf.earfcn_ul",
        "dl_bandwidth": "slow.dl_bandwidth_mhz",
        "ul_bandwidth": "slow.ul_bandwidth_mhz",
        "dl_bw_mhz": "slow.dl_bandwidth_mhz",
        "ul_bw_mhz": "slow.ul_bandwidth_mhz",
        "downlink_bandwidth_mhz": "slow.dl_bandwidth_mhz",
        "uplink_bandwidth_mhz": "slow.ul_bandwidth_mhz",
        "monthly_rx_bytes": "traffic.traffic_month_download_bytes",
        "monthly_tx_bytes": "traffic.traffic_month_upload_bytes",
        "nr5g_mode": "radio.nr_mode",
        "nr5g_band": "radio.nr_band",
        "nr5g_pci": "radio.nr_pci",
        "nr5g_arfcn": "radio.nr_arfcn",
        "nr5g_cell_id": "radio.nr_cell_id",
        "nr5g_cells": "slow.nr_cells",
        "nr5g_rsrp_dbm": "radio.nr_rsrp_dbm",
        "nr5g_rsrq_db": "radio.nr_rsrq_db",
        "nr5g_sinr_db": "radio.nr_sinr_db",
        "dl_256qam": "capability.dl_256qam",
        "ul_64qam": "capability.ul_64qam",
        "ue_category": "capability.ue_category",
    }
)

# These profile facts do not describe a current Matrix value. Listing them here
# prevents unknown fields from being ignored while keeping actions and catalog
# metadata out of the observation Matrix.
PROFILE_ONLY_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "band_selection": "action_contract",
        "bridge_mode": "action_contract",
        "config_version": "identity_profile_metadata",
        "nr5g_band_set": "profile_capability_metadata",
        "nr5g_rx0_rsrp_dbm": "profile_diagnostic_metadata",
        "nr5g_rx1_rsrp_dbm": "profile_diagnostic_metadata",
        "nr_band_lock": "action_contract",
        "pband": "action_contract",
        "temperature_ambient_c": "profile_diagnostic_metadata",
        "temperature_modem_4g_c": "profile_diagnostic_metadata",
        "temperature_pa1_c": "profile_diagnostic_metadata",
        "temperature_wifi_c": "profile_diagnostic_metadata",
        "tx_ppusch": "transmit_component_support",
        "tx_ppucch": "transmit_component_support",
        "tx_psrs": "transmit_component_support",
        "tx_pprach": "transmit_component_support",
        "traffic_presence": "read_capability_contract",
        "ue_capability": "read_capability_contract",
    }
)


def profile_field_matrix_address(field_name: str) -> str | None:
    """Return a Matrix address, or ``None`` for a profile-only fact."""

    if type(field_name) is not str or not field_name or field_name != field_name.strip():
        raise ValueError("profile_matrix_field_name_required")
    if field_name in PROFILE_CAPABILITY_OWNED_FIELDS:
        raise ValueError(
            f"profile_matrix_capability_field_must_use_capability:{field_name}"
        )
    if field_name in PROFILE_ONLY_FIELDS:
        return None
    address = PROFILE_FIELD_MATRIX_ALIASES.get(field_name)
    if address is None:
        address = _DIRECT_FIELD_ADDRESSES.get(field_name)
    if address is None:
        raise ValueError(f"profile_matrix_field_unclassified:{field_name}")
    line, separator, field = address.partition(".")
    if not separator or field not in MODEM_LIVE_TRUTH_FIELD_CATALOG.get(line, ()):
        raise RuntimeError(f"profile_matrix_address_invalid:{field_name}:{address}")
    return address


def _normalized_profile_disposition(value: Any) -> str:
    trust = str(value or "").strip().lower()
    if trust in {"confirmed", "expected", "supported"}:
        return PROFILE_MATRIX_SUPPORTED
    if trust == "synthetic":
        return PROFILE_MATRIX_SYNTHETIC
    if trust in {"unsupported", "non_useful"}:
        return PROFILE_MATRIX_UNSUPPORTED
    if trust == "untested":
        return PROFILE_MATRIX_UNTESTED
    raise ValueError(f"profile_matrix_field_trust_invalid:{trust or '<empty>'}")


def profile_matrix_dispositions(
    profile_record: Mapping[str, Any] | None,
    *,
    nr_mode: str | None = None,
) -> dict[str, str]:
    """Map one exact profile record to Matrix cells."""

    record = dict(profile_record) if isinstance(profile_record, Mapping) else {}
    if not record:
        return {}
    field_trust = record.get("field_trust")
    if not isinstance(field_trust, Mapping):
        raise TypeError("profile_matrix_field_trust_mapping_required")

    dispositions: dict[str, str] = {
        f"{line}.{field}": PROFILE_MATRIX_UNTESTED
        for line, fields in MODEM_LIVE_TRUTH_FIELD_CATALOG.items()
        if line in PROFILE_MATRIX_LINES
        for field in fields
    }
    explicitly_classified: set[str] = set()
    for raw_name, raw_trust in field_trust.items():
        if type(raw_name) is not str or not raw_name or raw_name != raw_name.strip():
            raise ValueError("profile_matrix_field_name_invalid")
        if raw_name in PROFILE_CAPABILITY_OWNED_FIELDS:
            raise ValueError(
                f"profile_matrix_capability_field_duplicate:{raw_name}"
            )
        address = profile_field_matrix_address(raw_name)
        if address is None:
            continue
        disposition = _normalized_profile_disposition(raw_trust)
        prior = dispositions.get(address)
        if address in explicitly_classified and prior != disposition:
            raise ValueError(
                f"profile_matrix_disposition_conflict:{address}:{prior}:{disposition}"
            )
        dispositions[address] = disposition
        explicitly_classified.add(address)

    capabilities = record.get("capabilities")
    if not isinstance(capabilities, Mapping):
        raise TypeError("profile_matrix_capabilities_mapping_required")
    for capability, addresses in PROFILE_CAPABILITY_MATRIX_ADDRESSES.items():
        enabled = capabilities.get(capability)
        if type(enabled) is not bool:
            raise TypeError(
                f"profile_matrix_capability_boolean_required:{capability}"
            )
        disposition = (
            PROFILE_MATRIX_SUPPORTED
            if enabled
            else PROFILE_MATRIX_UNSUPPORTED
        )
        for address in addresses:
            dispositions[address] = disposition
    # NR-only additions inherit the profile's explicit absence of NR. Keep
    # acquired evidence and explicit supported/synthetic classifications authoritative.
    if dispositions.get("radio.nr_mode") == PROFILE_MATRIX_UNSUPPORTED:
        for address, disposition in dispositions.items():
            if address.partition(".")[2].startswith("nr_") and disposition == PROFILE_MATRIX_UNTESTED:
                dispositions[address] = PROFILE_MATRIX_UNSUPPORTED
    if record.get("nr_cell_id_in_nsa") is False and str(nr_mode or "").upper() == "NSA":
        dispositions["radio.nr_cell_id"] = PROFILE_MATRIX_UNSUPPORTED
    return dispositions


__all__ = (
    "PROFILE_FIELD_MATRIX_ALIASES",
    "PROFILE_CAPABILITY_MATRIX_ADDRESSES",
    "PROFILE_CAPABILITY_OWNED_FIELDS",
    "PROFILE_MATRIX_DISPOSITIONS",
    "PROFILE_MATRIX_LINES",
    "PROFILE_MATRIX_SUPPORTED",
    "PROFILE_MATRIX_SYNTHETIC",
    "PROFILE_MATRIX_UNSUPPORTED",
    "PROFILE_MATRIX_UNTESTED",
    "PROFILE_ONLY_FIELDS",
    "profile_field_matrix_address",
    "profile_matrix_dispositions",
)
