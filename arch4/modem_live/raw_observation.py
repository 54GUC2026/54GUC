"""Typed observation captured at the Modem Live backend boundary.

Backends may use vendor-specific data, but they cannot write Matrix cells. The
translator registry accepts this detached object as its input.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    RuntimeGenerationNamespace,
)
from arch4.modem.backend_acquisition import BackendAcquisitionReceipt
from .truth_matrix import MODEM_LIVE_TRUTH_FIELD_CATALOG

RAW_MODEM_OBSERVATION_SCHEMA = "RawModemObservation.v3"
RAW_MODEM_OBSERVATION_OWNER = "54guc.modem_live.backend_boundary"


_DECLARED_MATRIX_CELLS = frozenset(
    f"{line}.{field}"
    for line, fields in MODEM_LIVE_TRUTH_FIELD_CATALOG.items()
    for field in fields
)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                copy.deepcopy(key): _deep_freeze(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return copy.deepcopy(value)


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            copy.deepcopy(key): _deep_thaw(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return {_deep_thaw(item) for item in value}
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class ModemLiveAcquisitionManifest:
    namespace: RuntimeGenerationNamespace
    backend: str
    profile_id: str
    source_lane: str
    receipts: tuple[BackendAcquisitionReceipt, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError(
                "modem_live_acquisition_manifest_namespace_required"
            )
        for field_name in ("backend", "profile_id", "source_lane"):
            value = getattr(self, field_name)
            if (
                type(value) is not str
                or not value
                or value != value.strip()
            ):
                raise ValueError(
                    f"modem_live_acquisition_manifest_{field_name}_required"
                )
        if (
            not isinstance(self.receipts, tuple)
            or not self.receipts
            or any(
                not isinstance(receipt, BackendAcquisitionReceipt)
                for receipt in self.receipts
            )
        ):
            raise TypeError(
                "modem_live_acquisition_manifest_receipts_required"
            )
        acquired_cells: set[str] = set()
        for receipt in self.receipts:
            if not receipt.succeeded:
                raise ValueError(
                    "modem_live_acquisition_manifest_success_receipt_required"
                )
            if receipt.backend != self.backend:
                raise ValueError(
                    "modem_live_acquisition_manifest_backend_mismatch"
                )
            if receipt.profile_id != self.profile_id:
                raise ValueError(
                    "modem_live_acquisition_manifest_profile_mismatch"
                )
            overlap = acquired_cells.intersection(receipt.acquired_cells)
            if overlap:
                raise ValueError(
                    "modem_live_acquisition_manifest_duplicate_cell_evidence"
                )
            acquired_cells.update(receipt.acquired_cells)
        if (
            not acquired_cells
            or any(
                type(path) is not str for path in acquired_cells
            )
            or not acquired_cells.issubset(_DECLARED_MATRIX_CELLS)
        ):
            raise ValueError(
                "modem_live_acquisition_manifest_cells_invalid"
            )

    @property
    def acquired_cells(self) -> frozenset[str]:
        return frozenset(
            cell
            for receipt in self.receipts
            for cell in receipt.acquired_cells
        )

    def receipt_for_cell(
        self,
        cell_path: str,
    ) -> BackendAcquisitionReceipt:
        if type(cell_path) is not str or cell_path not in self.acquired_cells:
            raise KeyError("modem_live_acquisition_cell_not_acquired")
        matches = tuple(
            receipt
            for receipt in self.receipts
            if cell_path in receipt.acquired_cells
        )
        if len(matches) != 1:
            raise RuntimeError(
                "modem_live_acquisition_cell_receipt_not_exact"
            )
        return matches[0]


@dataclass(frozen=True, slots=True, init=False)
class RawModemObservation:
    """One atomic backend read and its same-generation public status."""

    sampled_at: float
    source: str
    acquisition_attempt_id: str
    manifest: ModemLiveAcquisitionManifest
    _modem: Any
    _status: Mapping[str, Any]
    schema: str = RAW_MODEM_OBSERVATION_SCHEMA
    owner: str = RAW_MODEM_OBSERVATION_OWNER

    def __init__(
        self,
        *,
        sampled_at: float,
        source: str,
        acquisition_attempt_id: str,
        manifest: ModemLiveAcquisitionManifest,
        modem: Any,
        status: Mapping[str, Any],
    ) -> None:
        if type(sampled_at) is not float:
            raise TypeError("raw_modem_observation_sampled_at_invalid")
        if not math.isfinite(sampled_at) or sampled_at <= 0.0:
            raise ValueError("raw_modem_observation_sampled_at_invalid")
        for value, error in (
            (source, "raw_modem_observation_source_required"),
            (
                acquisition_attempt_id,
                "raw_modem_observation_acquisition_attempt_id_required",
            ),
        ):
            if type(value) is not str:
                raise TypeError(error)
            if not value or value != value.strip():
                raise ValueError(error)
        if not isinstance(manifest, ModemLiveAcquisitionManifest):
            raise TypeError(
                "raw_modem_observation_acquisition_manifest_required"
            )
        if modem is None:
            raise ValueError("raw_modem_observation_modem_required")
        if not isinstance(status, Mapping):
            raise TypeError("raw_modem_observation_status_mapping_required")
        object.__setattr__(self, "sampled_at", sampled_at)
        object.__setattr__(self, "source", source)
        object.__setattr__(
            self,
            "acquisition_attempt_id",
            acquisition_attempt_id,
        )
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "_modem", copy.deepcopy(modem))
        object.__setattr__(
            self,
            "_status",
            _deep_freeze(dict(status)),
        )
        object.__setattr__(self, "schema", RAW_MODEM_OBSERVATION_SCHEMA)
        object.__setattr__(self, "owner", RAW_MODEM_OBSERVATION_OWNER)

    @classmethod
    def capture(
        cls,
        *,
        modem: Any,
        status: Mapping[str, Any],
        sampled_at: float,
        source: str,
        acquisition_attempt_id: str,
        manifest: ModemLiveAcquisitionManifest,
    ) -> "RawModemObservation":
        detached_modem = copy.deepcopy(modem)
        detached_status = copy.deepcopy(dict(status or {}))
        return cls(
            sampled_at=sampled_at,
            source=source,
            acquisition_attempt_id=acquisition_attempt_id,
            manifest=manifest,
            modem=detached_modem,
            status=detached_status,
        )

    @property
    def modem(self) -> Any:
        return copy.deepcopy(self._modem)

    @property
    def status(self) -> Mapping[str, Any]:
        return MappingProxyType(_deep_thaw(self._status))

    def modem_copy(self) -> Any:
        return copy.deepcopy(self._modem)

    def status_copy(self) -> dict[str, Any]:
        return _deep_thaw(self._status)

    @property
    def source_lane(self) -> str:
        return self.manifest.source_lane

    @property
    def acquired_cells(self) -> frozenset[str]:
        return self.manifest.acquired_cells

    @property
    def acquisition_namespace(self) -> RuntimeGenerationNamespace:
        return self.manifest.namespace

    def acquisition_receipt_for_cell(
        self,
        cell_path: str,
    ) -> BackendAcquisitionReceipt:
        return self.manifest.receipt_for_cell(cell_path)

    @property
    def backend(self) -> str:
        return self.manifest.backend


def publish_backend_observation(
    store: Any,
    *,
    modem: Any,
    base_status: Mapping[str, Any],
    updated_at: float,
    source: str,
    acquisition_attempt_id: str,
    source_lane: str,
    receipts: tuple[BackendAcquisitionReceipt, ...],
    acquisition_namespace: RuntimeGenerationNamespace,
    backend: str,
    profile_id: str,
) -> Any:
    """Backend adapter used by replay and contract tests."""

    commit = getattr(store, "commit_observation", None)
    if not callable(commit):
        raise TypeError("raw_modem_observation_store_required")
    return commit(
        observation=RawModemObservation.capture(
            modem=modem,
            status=base_status,
            sampled_at=updated_at,
            source=source,
            acquisition_attempt_id=acquisition_attempt_id,
            manifest=ModemLiveAcquisitionManifest(
                namespace=acquisition_namespace,
                backend=backend,
                profile_id=profile_id,
                source_lane=source_lane,
                receipts=receipts,
            ),
        )
    )


__all__ = (
    "RAW_MODEM_OBSERVATION_OWNER",
    "RAW_MODEM_OBSERVATION_SCHEMA",
    "ModemLiveAcquisitionManifest",
    "RawModemObservation",
    "publish_backend_observation",
)
