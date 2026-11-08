"""The only Core owner for diagnostics configuration consumed by Core health."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from threading import RLock
from time import time
from typing import Any, Mapping

from arch4.atomic_io import atomic_write_json
from arch4.canonical_json import canonical_json_bytes
from arch4.core.operational_guards import core_operational_rejection
from arch4.core.downstream_health_monitor import DownstreamHealthMonitor
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.operational_service_contracts import (
    DownstreamHealthConfigRequest,
    OperationalDisposition,
    OperationalKind,
    OperationalServiceReceipt,
    ProductOperationalEffectPermit,
    require_product_operational_effect_permit,
)
from arch4.runtime_status_register_publishers import (
    publish_downstream_health_status,
)


OWNER = "54guc.core.diagnostics"
CONFIG_SCHEMA = "CoreDownstreamHealthConfiguration.v1"
CONFIG_FILENAME = "core_downstream_health_configuration.json"


class CoreDiagnosticsService:
    def __init__(self, core: Any, *, persistence_dir: str | Path) -> None:
        if core is None:
            raise TypeError("core_diagnostics_owner_required")
        self._core = core
        self._monitor = DownstreamHealthMonitor(core.cfg)
        self._lock = RLock()
        self._last_published_status: dict[str, Any] = {}
        self._configuration_path = (
            Path(persistence_dir).resolve() / CONFIG_FILENAME
        )
        restored = self._restore_configuration()
        if restored is not None:
            self._monitor.configure(
                enabled=restored["enabled"],
                target=restored["target"],
                label=restored["label"],
                ping_count=restored["ping_count"],
                interval_s=restored["interval_s"],
            )

    def configure_downstream_health(
        self,
        request: DownstreamHealthConfigRequest,
        *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt:
        if not isinstance(request, DownstreamHealthConfigRequest):
            raise TypeError("core_diagnostics_typed_request_required")
        require_product_operational_effect_permit(
            permit,
            request,
            expected_kind=OperationalKind.SET_DOWNSTREAM_HEALTH,
        )
        reason = core_operational_rejection(self._core, request.context)
        if reason:
            return OperationalServiceReceipt(
                request_id=request.context.request_id,
                kind=OperationalKind.SET_DOWNSTREAM_HEALTH,
                owner=OWNER,
                disposition=OperationalDisposition.REJECTED,
                message=f"Downstream health configuration rejected: {reason}",
                occurred_at=time(),
                runtime_namespace=(
                    request.context.ingress.matrix_identity.namespace
                ),
                matrix_identity=request.context.ingress.matrix_identity,
                detail=CoreFrozenPayload.from_mapping({"reason": reason}),
            )
        with self._lock:
            configuration = self._persist_configuration(request)
            status = self._monitor.configure(
                enabled=request.enabled,
                target=request.target,
                label=request.label or None,
                ping_count=request.ping_count,
                interval_s=request.interval_s,
            )
            if request.enabled and request.target:
                status = self._monitor.update(now=time())
            self._publish_if_changed(status)
            status = {
                **dict(status or {}),
                "configuration_request_id": (
                    request.context.request_id
                ),
                "configuration_content_sha256": (
                    configuration["content_sha256"]
                ),
            }
        return OperationalServiceReceipt(
            request_id=request.context.request_id,
            kind=OperationalKind.SET_DOWNSTREAM_HEALTH,
            owner=OWNER,
            disposition=OperationalDisposition.CONFIRMED,
            message=str(status.get("message", "") or "Downstream health updated."),
            occurred_at=time(),
            runtime_namespace=(
                request.context.ingress.matrix_identity.namespace
            ),
            matrix_identity=request.context.ingress.matrix_identity,
            detail=CoreFrozenPayload.from_mapping(dict(status or {})),
        )

    def reconcile_downstream_health(
        self,
        request: DownstreamHealthConfigRequest,
    ) -> OperationalServiceReceipt | None:
        if not isinstance(request, DownstreamHealthConfigRequest):
            raise TypeError("core_diagnostics_typed_request_required")
        with self._lock:
            restored = self._restore_configuration()
            expected = self._configuration_values(request)
            if (
                restored is None
                or restored["request_id"] != request.context.request_id
                or any(
                    restored[field_name] != expected[field_name]
                    for field_name in expected
                )
            ):
                return None
            status = self._monitor.configure(
                enabled=request.enabled,
                target=request.target,
                label=request.label or None,
                ping_count=request.ping_count,
                interval_s=request.interval_s,
            )
            self._publish_if_changed(status)
            status = {
                **dict(status or {}),
                "configuration_request_id": (
                    request.context.request_id
                ),
                "configuration_content_sha256": (
                    restored["content_sha256"]
                ),
            }
        return OperationalServiceReceipt(
            request_id=request.context.request_id,
            kind=OperationalKind.SET_DOWNSTREAM_HEALTH,
            owner=OWNER,
            disposition=OperationalDisposition.CONFIRMED,
            message=str(
                status.get("message", "")
                or "Downstream health configuration recovered."
            ),
            occurred_at=time(),
            runtime_namespace=request.context.runtime_namespace,
            matrix_identity=request.context.matrix_identity,
            detail=CoreFrozenPayload.from_mapping(dict(status)),
        )

    @staticmethod
    def _configuration_values(
        request: DownstreamHealthConfigRequest,
    ) -> dict[str, object]:
        return {
            "enabled": request.enabled,
            "target": request.target,
            "label": request.label,
            "ping_count": request.ping_count,
            "interval_s": request.interval_s,
        }

    def _persist_configuration(
        self,
        request: DownstreamHealthConfigRequest,
    ) -> dict[str, object]:
        content: dict[str, object] = {
            "schema": CONFIG_SCHEMA,
            "owner": OWNER,
            "request_id": request.context.request_id,
            "matrix_identity": request.context.matrix_identity.as_dict(),
            "updated_at": time(),
            **self._configuration_values(request),
        }
        content["content_sha256"] = hashlib.sha256(
            canonical_json_bytes(content)
        ).hexdigest()
        atomic_write_json(
            self._configuration_path,
            content,
            indent=2,
            sort_keys=True,
            trailing_newline=True,
        )
        return content

    def _restore_configuration(self) -> dict[str, object] | None:
        if not self._configuration_path.exists():
            return None
        try:
            raw = json.loads(
                self._configuration_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "core_downstream_health_configuration_corrupt"
            ) from exc
        if not isinstance(raw, Mapping):
            raise RuntimeError(
                "core_downstream_health_configuration_mapping_required"
            )
        row = dict(raw)
        if set(row) != {
            "schema",
            "owner",
            "request_id",
            "matrix_identity",
            "updated_at",
            "enabled",
            "target",
            "label",
            "ping_count",
            "interval_s",
            "content_sha256",
        }:
            raise RuntimeError(
                "core_downstream_health_configuration_fields_invalid"
            )
        checksum = row.pop("content_sha256")
        if (
            row["schema"] != CONFIG_SCHEMA
            or row["owner"] != OWNER
            or type(checksum) is not str
            or checksum
            != hashlib.sha256(canonical_json_bytes(row)).hexdigest()
        ):
            raise RuntimeError(
                "core_downstream_health_configuration_contract_invalid"
            )
        row["content_sha256"] = checksum
        return row

    def bind_config_generation(
        self,
        config: Any,
        *,
        generation_id: str,
        config_fingerprint: str,
    ) -> None:
        if str(getattr(config, "generation_id", "") or "") != str(
            generation_id or ""
        ):
            raise RuntimeError("core_diagnostics_config_generation_mismatch")
        if str(getattr(config, "fingerprint", "") or "") != str(
            config_fingerprint or ""
        ):
            raise RuntimeError("core_diagnostics_config_fingerprint_mismatch")
        with self._lock:
            self._monitor.cfg = config
            self._monitor.refresh_thresholds()

    def service_once(self, *, now: float | None = None) -> dict[str, Any]:
        with self._lock:
            status = dict(self._monitor.update(now=now))
            published = self._publish_if_changed(status)
        return {
            "owner": OWNER,
            "status": status,
            "published": published,
        }

    def status(self, *, now: float | None = None) -> dict[str, Any]:
        with self._lock:
            return dict(self._monitor.get_status(now=now))

    def drain_runtime_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._monitor.drain_runtime_events())

    def _publish_if_changed(self, status: dict[str, Any]) -> bool:
        comparable = {key: value for key, value in status.items() if key != "ts"}
        if comparable == self._last_published_status:
            return False
        status_ts = float(status.get("ts", 0.0) or 0.0)
        if not math.isfinite(status_ts) or status_ts <= 0.0:
            raise ValueError("core_diagnostics_status_timestamp_required")
        publish_downstream_health_status(
            self._core.register_bank,
            {"downstream_health": status},
            now=status_ts,
        )
        self._last_published_status = dict(comparable)
        return True


__all__ = ("CoreDiagnosticsService",)
