"""Combine an LTE anchor and NR secondary data into one physical snapshot.

Modem Live translates vendor data, and Matrix owns current values. This module
only combines those values. It does not parse aliases, guess missing values,
create actions, or calculate quality.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from arch4.core_contracts.nr_identity import nr_identity_values_valid


NSA_PHYSICAL_SNAPSHOT_SCHEMA = "NsaPhysicalSnapshot.v1"
NSA_PHYSICAL_SNAPSHOT_OWNER = "54guc.modem_live.nsa_snapshot"

RAT_MODE_LTE = "LTE"
RAT_MODE_NSA = "NR_NSA"
RAT_MODE_SA_UNSUPPORTED = "NR_SA_UNSUPPORTED"
RAT_MODE_UNKNOWN = "UNKNOWN"

IDENTITY_EXACT = "exact"
IDENTITY_PARTIAL = "partial"
IDENTITY_ABSENT = "absent"


class _Cell(Protocol):
    state: str
    value: Any
    sampled_at: float
    acquired_at: float
    source_sequence: int

    @property
    def available(self) -> bool: ...


class _MatrixReader(Protocol):
    @property
    def identity(self) -> Any: ...

    def cell(self, line: str, field: str) -> _Cell: ...


@dataclass(frozen=True, slots=True)
class NsaObservedField:
    state: str
    value: Any
    sampled_at: float
    acquired_at: float
    source_sequence: int

    @property
    def available(self) -> bool:
        return self.state == "available"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "available": self.available,
            "value": self.value if self.available else None,
            "sampled_at": float(self.sampled_at),
            "acquired_at": float(self.acquired_at),
            "source_sequence": int(self.source_sequence),
        }


@dataclass(frozen=True, slots=True)
class NsaPhysicalSnapshot:
    matrix_identity: Mapping[str, Any]
    profile_id: str
    rat_mode: str
    secondary_active: bool
    secondary_active_observed: bool
    sa_detected: bool
    identity_completeness: str
    identity_key: str
    coherent: bool
    fields: Mapping[str, NsaObservedField]

    @classmethod
    def from_matrix(cls, reader: _MatrixReader) -> "NsaPhysicalSnapshot":
        addresses = {
            "lte.band": ("radio", "serving_band"),
            "lte.pool": ("radio", "configured_band_pool"),
            "lte.cell_id": ("rf", "cell_id"),
            "lte.earfcn": ("rf", "earfcn_dl"),
            "lte.pci": ("rf", "pci"),
            "lte.bandwidth_mhz": ("slow", "dl_bandwidth_mhz"),
            "nr.nsa_active": ("radio", "nsa_active"),
            "nr.sa_active": ("radio", "sa_active"),
            "nr.mode": ("radio", "nr_mode"),
            "nr.band": ("radio", "nr_band"),
            "nr.arfcn": ("radio", "nr_arfcn"),
            "nr.pci": ("radio", "nr_pci"),
            "nr.nci": ("radio", "nr_cell_id"),
            "nr.bandwidth_mhz": ("slow", "nr_bandwidth_mhz"),
            "nr.scs_khz": ("slow", "nr_scs_khz"),
            "nr.ss_rsrp_dbm": ("radio", "nr_rsrp_dbm"),
            "nr.ss_rsrq_db": ("radio", "nr_rsrq_db"),
            "nr.ss_sinr_db": ("radio", "nr_sinr_db"),
            "nr.cells": ("slow", "nr_cells"),
            "radio.network_type": ("radio", "network_type"),
            "identity.profile_id": ("identity", "profile_id"),
        }
        fields: dict[str, NsaObservedField] = {}
        for name, (line, field) in addresses.items():
            cell = reader.cell(line, field)
            fields[name] = NsaObservedField(
                state=str(cell.state),
                value=cell.value if cell.available else None,
                sampled_at=float(cell.sampled_at),
                acquired_at=float(cell.acquired_at),
                source_sequence=int(cell.source_sequence),
            )

        rat_mode, secondary_active, active_observed, sa_detected = (
            _resolve_mode(fields)
        )
        identity_fields = tuple(
            fields[name] for name in ("nr.band", "nr.arfcn", "nr.pci")
        )
        identity_available = tuple(item.available for item in identity_fields)
        if all(identity_available) and nr_identity_values_valid(
            band=identity_fields[0].value, arfcn=identity_fields[1].value,
            pci=identity_fields[2].value,
        ):
            completeness = IDENTITY_EXACT
            identity_key = (
                f"n{identity_fields[0].value}|"
                f"arfcn={identity_fields[1].value}|"
                f"pci={identity_fields[2].value}"
            )
        elif any(identity_available):
            completeness = IDENTITY_PARTIAL
            identity_key = ""
        else:
            completeness = IDENTITY_ABSENT
            identity_key = ""
        coherent = bool(
            rat_mode == RAT_MODE_NSA
            and secondary_active
            and completeness == IDENTITY_EXACT
        )
        profile = fields["identity.profile_id"]
        return cls(
            matrix_identity=MappingProxyType(
                dict(reader.identity.as_dict())
            ),
            profile_id=(str(profile.value) if profile.available else ""),
            rat_mode=rat_mode,
            secondary_active=secondary_active,
            secondary_active_observed=active_observed,
            sa_detected=sa_detected,
            identity_completeness=completeness,
            identity_key=identity_key,
            coherent=coherent,
            fields=MappingProxyType(fields),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": NSA_PHYSICAL_SNAPSHOT_SCHEMA,
            "owner": NSA_PHYSICAL_SNAPSHOT_OWNER,
            "matrix_identity": dict(self.matrix_identity),
            "profile_id": self.profile_id,
            "rat_mode": self.rat_mode,
            "secondary_active": self.secondary_active,
            "secondary_active_observed": self.secondary_active_observed,
            "sa_detected": self.sa_detected,
            "identity_completeness": self.identity_completeness,
            "identity_key": self.identity_key,
            "coherent": self.coherent,
            "coherent_samples_required": 2,
            "fields": {
                name: field.as_dict()
                for name, field in self.fields.items()
            },
        }


def _resolve_mode(
    fields: Mapping[str, NsaObservedField],
) -> tuple[str, bool, bool, bool]:
    """Resolve the mode from translated evidence, using the newest fact first."""

    candidates: list[tuple[float, int, str, bool | None]] = []

    def add(
        name: str,
        priority: int,
        mode: str,
        active: bool | None,
    ) -> None:
        field = fields[name]
        if field.available:
            candidates.append((field.acquired_at, priority, mode, active))

    sa = fields["nr.sa_active"]
    if sa.available and sa.value is True:
        add(
            "nr.sa_active",
            50,
            RAT_MODE_SA_UNSUPPORTED,
            True,
        )
    nsa = fields["nr.nsa_active"]
    if nsa.available and type(nsa.value) is bool:
        add(
            "nr.nsa_active",
            40,
            RAT_MODE_NSA if nsa.value else RAT_MODE_LTE,
            bool(nsa.value),
        )
    mode_field = fields["nr.mode"]
    if mode_field.available:
        token = str(mode_field.value or "").strip().upper()
        if token == "SA":
            add("nr.mode", 30, RAT_MODE_SA_UNSUPPORTED, None)
        elif token == "NSA":
            add("nr.mode", 30, RAT_MODE_NSA, None)
    network = fields["radio.network_type"]
    if network.available:
        token = str(network.value or "").strip().upper()
        if "NSA" in token:
            add("radio.network_type", 20, RAT_MODE_NSA, None)
        elif "SA" in token and "NSA" not in token:
            add("radio.network_type", 20, RAT_MODE_SA_UNSUPPORTED, None)
        elif "LTE" in token or "4G" in token or "NO SERVICE" in token:
            add("radio.network_type", 20, RAT_MODE_LTE, False)

    if not candidates:
        return RAT_MODE_UNKNOWN, False, False, False
    _when, _priority, mode, active = max(candidates)
    explicit_active = bool(
        nsa.available and type(nsa.value) is bool
    )
    secondary_active = bool(
        mode == RAT_MODE_NSA
        and active is not False
        and nsa.available
        and nsa.value is True
    )
    sa_detected = bool(
        mode == RAT_MODE_SA_UNSUPPORTED
        or (sa.available and sa.value is True)
    )
    return mode, secondary_active, explicit_active, sa_detected


def validate_nsa_physical_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("nsa_physical_snapshot_mapping_required")
    row = dict(value)
    if row.get("schema") != NSA_PHYSICAL_SNAPSHOT_SCHEMA:
        raise ValueError("nsa_physical_snapshot_schema_invalid")
    if row.get("owner") != NSA_PHYSICAL_SNAPSHOT_OWNER:
        raise ValueError("nsa_physical_snapshot_owner_invalid")
    if row.get("rat_mode") not in {
        RAT_MODE_LTE,
        RAT_MODE_NSA,
        RAT_MODE_SA_UNSUPPORTED,
        RAT_MODE_UNKNOWN,
    }:
        raise ValueError("nsa_physical_snapshot_mode_invalid")
    if row.get("identity_completeness") not in {
        IDENTITY_EXACT,
        IDENTITY_PARTIAL,
        IDENTITY_ABSENT,
    }:
        raise ValueError("nsa_physical_snapshot_identity_invalid")
    if not isinstance(row.get("fields"), Mapping):
        raise TypeError("nsa_physical_snapshot_fields_required")
    return row


__all__ = (
    "IDENTITY_ABSENT",
    "IDENTITY_EXACT",
    "IDENTITY_PARTIAL",
    "NSA_PHYSICAL_SNAPSHOT_OWNER",
    "NSA_PHYSICAL_SNAPSHOT_SCHEMA",
    "NsaObservedField",
    "NsaPhysicalSnapshot",
    "RAT_MODE_LTE",
    "RAT_MODE_NSA",
    "RAT_MODE_SA_UNSUPPORTED",
    "RAT_MODE_UNKNOWN",
    "validate_nsa_physical_snapshot",
)
