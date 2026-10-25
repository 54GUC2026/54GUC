"""Typed, owner-scoped NGUI extension publication boundary.

This adapter owns sequence and immutable frame construction only.  It does not
read Core registers, execute customer policy, or synthesize domain truth.
"""

from __future__ import annotations

import math
from threading import RLock
from time import time
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    RuntimeGenerationNamespace,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.runtime_snapshot import NGUIExtensionFrame


class CustomerNGUIExtensionPublisher:
    """Publish one exact customer source catalog as immutable frames."""

    def __init__(
        self,
        *,
        hook_id: str,
        payload_schema: str,
        provider_id: str,
        source_ids: frozenset[str],
    ) -> None:
        self.hook_id = str(hook_id or "").strip()
        self.payload_schema = str(payload_schema or "").strip()
        self.provider_id = str(provider_id or "").strip()
        self.source_ids = frozenset(
            str(source_id or "").strip() for source_id in source_ids
        )
        if not self.hook_id:
            raise ValueError("customer_ngui_extension_hook_id_required")
        if not self.payload_schema:
            raise ValueError("customer_ngui_extension_payload_schema_required")
        if not self.provider_id:
            raise ValueError("customer_ngui_extension_provider_id_required")
        if not self.source_ids or "" in self.source_ids:
            raise ValueError("customer_ngui_extension_source_catalog_required")
        self._lock = RLock()
        self._namespace: RuntimeGenerationNamespace | None = None
        self._sequence = 0
        self._latest: NGUIExtensionFrame | None = None

    def bind_runtime_namespace(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> bool:
        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError("customer_ngui_extension_namespace_required")
        with self._lock:
            if self._namespace == namespace:
                return False
            self._namespace = namespace
            self._sequence = 0
            self._latest = None
            return True

    def publish(
        self,
        values: Mapping[str, Any],
        *,
        namespace: RuntimeGenerationNamespace,
        validity: Mapping[str, Any] | None = None,
        freshness: Mapping[str, Any] | None = None,
        reasons: Mapping[str, Any] | None = None,
        sampled_at: Mapping[str, Any] | None = None,
        sample_sequences: Mapping[str, Any] | None = None,
        published_at: float | None = None,
    ) -> NGUIExtensionFrame:
        current_values = dict(values or {})
        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError("customer_ngui_extension_namespace_required")
        if set(current_values) != set(self.source_ids):
            missing = sorted(self.source_ids - set(current_values))
            extra = sorted(set(current_values) - self.source_ids)
            raise ValueError(
                "customer_ngui_extension_source_catalog_mismatch:"
                f"missing={missing};extra={extra}"
            )
        now = float(time() if published_at is None else published_at)
        if not math.isfinite(now) or now <= 0.0:
            raise ValueError("customer_ngui_extension_positive_timestamp_required")
        validity_values = self._complete_metadata(
            validity,
            default=lambda source_id: current_values[source_id] is not None,
        )
        freshness_values = self._complete_metadata(
            freshness,
            default=lambda source_id: (
                "fresh" if bool(validity_values[source_id]) else "unknown"
            ),
        )
        reason_values = self._complete_metadata(
            reasons,
            default=lambda source_id: (
                "" if bool(validity_values[source_id]) else "source_unavailable"
            ),
        )
        sampled_values = self._required_metadata(
            sampled_at,
            label="sampled_at",
        )
        with self._lock:
            if self._namespace != namespace:
                raise RuntimeError(
                    "customer_ngui_extension_namespace_not_active"
                )
            next_sequence = self._sequence + 1
            sample_sequence_values = self._required_metadata(
                sample_sequences,
                label="sample_sequences",
            )
            frame = NGUIExtensionFrame(
                namespace=namespace,
                hook_id=self.hook_id,
                payload_schema=self.payload_schema,
                provider_id=self.provider_id,
                sequence=next_sequence,
                published_at=now,
                values=CoreFrozenPayload.from_mapping(current_values),
                validity=CoreFrozenPayload.from_mapping(validity_values),
                freshness=CoreFrozenPayload.from_mapping(freshness_values),
                reasons=CoreFrozenPayload.from_mapping(reason_values),
                sampled_at=CoreFrozenPayload.from_mapping(sampled_values),
                sample_sequences=CoreFrozenPayload.from_mapping(
                    sample_sequence_values
                ),
            )
            self._sequence = next_sequence
            self._latest = frame
            return frame

    def next_sequence(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> int:
        """Return the next full-frame sequence for an owner that supplies all metadata."""

        with self._lock:
            if self._namespace != namespace:
                raise RuntimeError(
                    "customer_ngui_extension_namespace_not_active"
                )
            return self._sequence + 1

    def publish_unavailable(
        self,
        reason: str,
        *,
        namespace: RuntimeGenerationNamespace,
        published_at: float | None = None,
    ) -> NGUIExtensionFrame:
        unavailable_reason = str(reason or "customer_unattached").strip()
        now = float(time() if published_at is None else published_at)
        if not math.isfinite(now) or now <= 0.0:
            raise ValueError("customer_ngui_extension_positive_timestamp_required")
        return self.publish(
            {source_id: None for source_id in self.source_ids},
            namespace=namespace,
            validity={source_id: False for source_id in self.source_ids},
            freshness={source_id: "unknown" for source_id in self.source_ids},
            reasons={source_id: unavailable_reason for source_id in self.source_ids},
            sampled_at={source_id: 0.0 for source_id in self.source_ids},
            sample_sequences={source_id: 0 for source_id in self.source_ids},
            published_at=now,
        )

    def latest_for(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> NGUIExtensionFrame:
        with self._lock:
            if self._namespace != namespace:
                raise RuntimeError(
                    "customer_ngui_extension_namespace_not_active"
                )
            if self._latest is None:
                raise RuntimeError(
                    "customer_ngui_extension_current_frame_unavailable"
                )
            return self._latest

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": "CustomerNGUIExtensionPublisherStatus.v1",
                "owner": self.provider_id,
                "hook_id": self.hook_id,
                "payload_schema": self.payload_schema,
                "runtime_namespace": (
                    self._namespace.as_dict()
                    if self._namespace is not None
                    else {}
                ),
                "sequence": self._sequence,
                "published": self._latest is not None,
                "source_count": len(self.source_ids),
            }

    def _complete_metadata(
        self,
        supplied: Mapping[str, Any] | None,
        *,
        default,
    ) -> dict[str, Any]:
        current = dict(supplied or {})
        extra = sorted(set(current) - self.source_ids)
        if extra:
            raise ValueError(
                "customer_ngui_extension_metadata_unknown_source:" + ",".join(extra)
            )
        return {
            source_id: current.get(source_id, default(source_id))
            for source_id in sorted(self.source_ids)
        }

    def _required_metadata(
        self,
        supplied: Mapping[str, Any] | None,
        *,
        label: str,
    ) -> dict[str, Any]:
        current = dict(supplied or {})
        extra = sorted(set(current) - self.source_ids)
        missing = sorted(self.source_ids - set(current))
        if extra or missing:
            raise ValueError(
                f"customer_ngui_extension_{label}_catalog_mismatch:"
                f"missing={missing};extra={extra}"
            )
        return {source_id: current[source_id] for source_id in sorted(self.source_ids)}


__all__ = ("CustomerNGUIExtensionPublisher",)
