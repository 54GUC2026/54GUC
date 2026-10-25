"""Exact typed evidence for one Modem Live backend acquisition.

Backends own vendor protocol timing and parsing.  They do not own Matrix
freshness.  This boundary carries one physical read and the exact canonical
source fields/cells that read proved so Modem Live can commit truth without
inferring freshness from a lane name.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import math
import time
from types import MappingProxyType
from typing import Any, Mapping

from arch4.modem_live.truth_matrix import MODEM_LIVE_TRUTH_FIELD_CATALOG
from arch4.modem.lte_frequency import lte_band_from_dl_earfcn


BACKEND_ACQUISITION_LANES = frozenset(
    {
        "startup_full",
        "fast_live",
        "slow_live",
        "post_write",
        "neighbor",
    }
)

DECLARED_MATRIX_CELLS = frozenset(
    f"{line}.{field}"
    for line, fields in MODEM_LIVE_TRUTH_FIELD_CATALOG.items()
    for field in fields
)
_CANONICAL_BACKEND_SOURCE_ALIASES = frozenset(
    {
        # Canonical backend/public-status names whose Matrix cells use the
        # shorter cross-vendor spelling.  Vendor-native names are forbidden
        # here; only Modem Live's declared source vocabulary is accepted.
        "band",
        "primary_band",
        "earfcn",
        "nr5g_band",
        "nr5g_pci",
        "nr5g_arfcn",
        "nr5g_cell_id",
        "nr5g_cells",
        "nr5g_mode",
        "nr5g_rsrp_dbm",
        "nr5g_rsrq_db",
        "nr5g_sinr_db",
        "nr5g_state_text",
        "dl_bandwidth",
        "ul_bandwidth",
        "dl_bw_mhz",
        "ul_bw_mhz",
        "downlink_bandwidth_mhz",
        "uplink_bandwidth_mhz",
    }
)
_CANONICAL_BACKEND_SOURCE_ONLY = frozenset(
    {
        # Physical backend evidence used by Modem Live to derive the public
        # session-capability cells.  These are not direct Matrix cells.
        "supported_lte_bands",
        "supported_lte_bands_source",
        "supported_lte_bands_observed_at",
        "supported_nr_bands",
        "supported_nr_bands_source",
        "supported_nr_bands_observed_at",
        "nr5g_band_set",
    }
)
_CANONICAL_BACKEND_SOURCE_TARGET = {
    "band": "serving_band",
    "primary_band": "serving_band",
    "earfcn": "earfcn_dl",
    "nr5g_band": "nr_band",
    "nr5g_pci": "nr_pci",
    "nr5g_arfcn": "nr_arfcn",
    "nr5g_cell_id": "nr_cell_id",
    "nr5g_cells": "nr_cells",
    "nr5g_mode": "nr_mode",
    "nr5g_rsrp_dbm": "nr_rsrp_dbm",
    "nr5g_rsrq_db": "nr_rsrq_db",
    "nr5g_sinr_db": "nr_sinr_db",
    "nr5g_state_text": "nr_state_text",
    "dl_bandwidth": "dl_bandwidth_mhz",
    "ul_bandwidth": "ul_bandwidth_mhz",
    "dl_bw_mhz": "dl_bandwidth_mhz",
    "ul_bw_mhz": "ul_bandwidth_mhz",
    "downlink_bandwidth_mhz": "dl_bandwidth_mhz",
    "uplink_bandwidth_mhz": "ul_bandwidth_mhz",
}
DECLARED_CANONICAL_SOURCE_FIELDS = (
    frozenset(
        cell.partition(".")[2]
        for cell in DECLARED_MATRIX_CELLS
    )
    | _CANONICAL_BACKEND_SOURCE_ALIASES
    | _CANONICAL_BACKEND_SOURCE_ONLY
)


def matrix_cells(
    line: str,
    fields: tuple[str, ...],
) -> frozenset[str]:
    line_name = _exact_text(line, field_name="matrix_line")
    declared_fields = MODEM_LIVE_TRUTH_FIELD_CATALOG.get(line_name)
    if declared_fields is None:
        raise ValueError("backend_acquisition_matrix_line_undeclared")
    if (
        not isinstance(fields, tuple)
        or any(type(field) is not str for field in fields)
        or not set(fields).issubset(set(declared_fields))
    ):
        raise ValueError("backend_acquisition_matrix_fields_undeclared")
    return frozenset(f"{line_name}.{field}" for field in fields)


def matrix_cells_for_canonical_fields(
    fields: frozenset[str],
) -> frozenset[str]:
    source_fields = _exact_field_set(
        fields,
        field_name="canonical_source_fields",
        declared=DECLARED_CANONICAL_SOURCE_FIELDS,
    )
    target_fields = set(
        _CANONICAL_BACKEND_SOURCE_TARGET.get(field, field)
        for field in source_fields
    )
    if source_fields.intersection(
        {"band", "primary_band", "serving_band"}
    ):
        target_fields.update(
            {
                "serving_band_signature",
                "serving_band_available",
                "serving_band_unavailable_reason",
            }
        )
    return frozenset(
        f"{line}.{field}"
        for line, declared_fields in MODEM_LIVE_TRUTH_FIELD_CATALOG.items()
        for field in declared_fields
        if field in target_fields
    )


def _exact_text(value: Any, *, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"backend_acquisition_{field_name}_required")
    return value


def _exact_field_set(
    value: Any,
    *,
    field_name: str,
    declared: frozenset[str] | None = None,
) -> frozenset[str]:
    if type(value) is not frozenset:
        raise TypeError(f"backend_acquisition_{field_name}_frozenset_required")
    if any(
        type(item) is not str
        or not item
        or item != item.strip()
        for item in value
    ):
        raise ValueError(f"backend_acquisition_{field_name}_invalid")
    if declared is not None and not value.issubset(declared):
        raise ValueError(f"backend_acquisition_{field_name}_undeclared")
    return value


@dataclass(frozen=True, slots=True)
class BackendAcquisitionRequest:
    attempt_id: str
    lane: str
    started_at: float

    def __post_init__(self) -> None:
        _exact_text(self.attempt_id, field_name="attempt_id")
        if self.lane not in BACKEND_ACQUISITION_LANES:
            raise ValueError("backend_acquisition_lane_invalid")
        if (
            type(self.started_at) is not float
            or not math.isfinite(self.started_at)
            or self.started_at <= 0.0
        ):
            raise ValueError("backend_acquisition_started_at_invalid")


@dataclass(frozen=True, slots=True)
class BackendAcquisitionReceipt:
    request: BackendAcquisitionRequest
    backend: str
    profile_id: str
    finished_at: float
    succeeded: bool
    modem: Any
    status: Mapping[str, Any]
    acquired_cells: frozenset[str]
    failure_reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.request, BackendAcquisitionRequest):
            raise TypeError("backend_acquisition_request_required")
        _exact_text(self.backend, field_name="backend")
        _exact_text(self.profile_id, field_name="profile_id")
        if (
            type(self.finished_at) is not float
            or not math.isfinite(self.finished_at)
            or self.finished_at < self.request.started_at
        ):
            raise ValueError("backend_acquisition_finished_at_invalid")
        if type(self.succeeded) is not bool:
            raise TypeError("backend_acquisition_succeeded_bool_required")
        if not isinstance(self.status, Mapping):
            raise TypeError("backend_acquisition_status_mapping_required")
        acquired_cells = _exact_field_set(
            self.acquired_cells,
            field_name="acquired_cells",
            declared=DECLARED_MATRIX_CELLS,
        )
        if self.succeeded:
            if not acquired_cells:
                raise ValueError("backend_acquisition_success_cells_required")
            if self.failure_reason:
                raise ValueError("backend_acquisition_success_reason_forbidden")
        else:
            if acquired_cells:
                raise ValueError("backend_acquisition_failure_evidence_forbidden")
            _exact_text(
                self.failure_reason,
                field_name="failure_reason",
            )
        object.__setattr__(
            self,
            "modem",
            copy.deepcopy(self.modem),
        )
        object.__setattr__(
            self,
            "status",
            MappingProxyType(copy.deepcopy(dict(self.status))),
        )

    def status_copy(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.status))

    def acquired_modem_copy(self) -> Any:
        return copy.deepcopy(self.modem)

    def restricted_to_cells(
        self,
        cells: frozenset[str],
    ) -> "BackendAcquisitionReceipt":
        selected = _exact_field_set(
            cells,
            field_name="restricted_cells",
            declared=DECLARED_MATRIX_CELLS,
        )
        if not selected.issubset(self.acquired_cells):
            raise ValueError(
                "backend_acquisition_restricted_cells_not_acquired"
            )
        if not selected:
            raise ValueError(
                "backend_acquisition_restricted_cells_required"
            )
        return BackendAcquisitionReceipt(
            request=self.request,
            backend=self.backend,
            profile_id=self.profile_id,
            finished_at=self.finished_at,
            succeeded=True,
            modem=self.acquired_modem_copy(),
            status=self.status_copy(),
            acquired_cells=selected,
        )


def capture_backend_acquisition(
    *,
    request: BackendAcquisitionRequest,
    backend: str,
    profile_id: str,
    modem: Any,
    status: Mapping[str, Any],
    acquired_cells: frozenset[str],
    failure_reason: str = "",
) -> BackendAcquisitionReceipt:
    succeeded = bool(modem is not None or acquired_cells)
    if succeeded and isinstance(status, Mapping) and type(acquired_cells) is frozenset:
        from arch4.modem_live.radio_identity_audit import audit_backend_identity
        modem, status, acquired_cells = audit_backend_identity(
            request=request, backend=backend, profile_id=profile_id,
            modem=modem, status=status, acquired_cells=acquired_cells)
    if (succeeded and isinstance(status, Mapping)
            and type(acquired_cells) is frozenset
            and "rf.earfcn_dl" in acquired_cells
            and "radio.serving_band" not in acquired_cells):
        # Derive only from this receipt's fresh channel, never a retained one.
        channel = (getattr(modem, "earfcn_dl", None) if modem is not None
                   else status.get("earfcn_dl", status.get("earfcn")))
        band = lte_band_from_dl_earfcn(channel)
        if band:
            modem = copy.deepcopy(modem)
            if modem is not None:
                modem.primary_band = band
            status = dict(status)
            status.update(serving_band=band, primary_band=band, band=band,
                          serving_band_signature=f"B{band}")
            acquired_cells = acquired_cells | matrix_cells_for_canonical_fields(
                frozenset({"serving_band"}))
    return BackendAcquisitionReceipt(
        request=request,
        backend=backend,
        profile_id=profile_id,
        finished_at=float(time.time()),
        succeeded=succeeded,
        modem=modem,
        status=status,
        acquired_cells=acquired_cells if succeeded else frozenset(),
        failure_reason=(
            ""
            if succeeded
            else str(failure_reason or "backend_acquisition_unavailable")
        ),
    )


__all__ = (
    "BACKEND_ACQUISITION_LANES",
    "DECLARED_CANONICAL_SOURCE_FIELDS",
    "DECLARED_MATRIX_CELLS",
    "BackendAcquisitionReceipt",
    "BackendAcquisitionRequest",
    "capture_backend_acquisition",
    "matrix_cells",
    "matrix_cells_for_canonical_fields",
)
