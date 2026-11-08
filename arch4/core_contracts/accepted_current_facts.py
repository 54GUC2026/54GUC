"""Finite immutable current-fact projection of one accepted Modem Live Matrix."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from arch4.core_contracts.payload import CoreFrozenPayload


ACCEPTED_CURRENT_FACTS_SCHEMA = "CoreAcceptedCurrentFacts.v1"
ACCEPTED_CURRENT_FACTS_OWNER = "54guc.core.accepted_current_facts"
_DISPOSITIONS = frozenset(
    {"available", "missing", "invalid", "unsupported"}
)


@dataclass(frozen=True, slots=True)
class CoreAcceptedCurrentFact:
    line: str
    field: str
    disposition: str
    value: CoreFrozenPayload
    reason: str
    sampled_at: float
    acquired_at: float
    acquisition_attempt_id: str
    source_lane: str
    acquisition_namespace: RuntimeGenerationNamespace | None
    retention: CoreFrozenPayload | None
    line_sequence: int
    source_sequence: int
    source: str

    def __post_init__(self) -> None:
        for name in ("line", "field"):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"accepted_current_fact_{name}_required")
        if self.disposition not in _DISPOSITIONS:
            raise ValueError("accepted_current_fact_disposition_invalid")
        if not isinstance(self.value, CoreFrozenPayload):
            raise TypeError("accepted_current_fact_value_required")
        value_payload = self.value.to_mapping()
        if set(value_payload) != {"present", "value"}:
            raise ValueError("accepted_current_fact_value_envelope_invalid")
        if type(value_payload["present"]) is not bool:
            raise TypeError("accepted_current_fact_value_presence_invalid")
        if value_payload["present"] is not (
            self.disposition == "available"
        ):
            raise ValueError("accepted_current_fact_value_disposition_mismatch")
        if (
            self.disposition != "available"
            and value_payload["value"] is not None
        ):
            raise ValueError(
                "accepted_current_fact_unavailable_value_must_be_null"
            )
        if type(self.reason) is not str or self.reason != self.reason.strip():
            raise TypeError("accepted_current_fact_reason_invalid")
        if self.disposition == "available" and self.reason:
            raise ValueError("accepted_current_fact_available_reason_invalid")
        if self.disposition != "available" and not self.reason:
            raise ValueError("accepted_current_fact_unavailable_reason_required")
        for name in ("sampled_at", "acquired_at"):
            value = getattr(self, name)
            if type(value) is not float:
                raise TypeError(f"accepted_current_fact_{name}_invalid")
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"accepted_current_fact_{name}_invalid")
        for name in ("acquisition_attempt_id", "source_lane", "source"):
            value = getattr(self, name)
            if type(value) is not str or value != value.strip():
                raise TypeError(f"accepted_current_fact_{name}_invalid")
        for name in ("line_sequence", "source_sequence"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TypeError(f"accepted_current_fact_{name}_invalid")
        if (
            self.acquisition_namespace is not None
            and not isinstance(
                self.acquisition_namespace,
                RuntimeGenerationNamespace,
            )
        ):
            raise TypeError(
                "accepted_current_fact_acquisition_namespace_invalid"
            )
        if self.retention is not None and not isinstance(
            self.retention,
            CoreFrozenPayload,
        ):
            raise TypeError("accepted_current_fact_retention_invalid")

    @property
    def path(self) -> str:
        return f"{self.line}.{self.field}"

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
    ) -> "CoreAcceptedCurrentFact":
        if not isinstance(payload, dict):
            raise TypeError("accepted_current_fact_payload_required")
        expected = {
            "path",
            "line",
            "field",
            "disposition",
            "value",
            "reason",
            "sampled_at",
            "acquired_at",
            "acquisition_attempt_id",
            "source_lane",
            "acquisition_namespace",
            "retention",
            "line_sequence",
            "source_sequence",
            "source",
        }
        if set(payload) != expected:
            raise ValueError("accepted_current_fact_payload_contract_invalid")
        acquisition_namespace = payload["acquisition_namespace"]
        retention = payload["retention"]
        fact = cls(
            line=payload["line"],
            field=payload["field"],
            disposition=payload["disposition"],
            value=CoreFrozenPayload.from_mapping(payload["value"]),
            reason=payload["reason"],
            sampled_at=payload["sampled_at"],
            acquired_at=payload["acquired_at"],
            acquisition_attempt_id=payload["acquisition_attempt_id"],
            source_lane=payload["source_lane"],
            acquisition_namespace=(
                None
                if acquisition_namespace is None
                else RuntimeGenerationNamespace.from_mapping(
                    acquisition_namespace
                )
            ),
            retention=(
                None
                if retention is None
                else CoreFrozenPayload.from_mapping(retention)
            ),
            line_sequence=payload["line_sequence"],
            source_sequence=payload["source_sequence"],
            source=payload["source"],
        )
        if payload["path"] != fact.path:
            raise ValueError("accepted_current_fact_payload_path_mismatch")
        return fact

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "field": self.field,
            "disposition": self.disposition,
            "value": self.value.to_payload(),
            "reason": self.reason,
            "sampled_at": self.sampled_at,
            "acquired_at": self.acquired_at,
            "acquisition_attempt_id": self.acquisition_attempt_id,
            "source_lane": self.source_lane,
            "acquisition_namespace": (
                self.acquisition_namespace.as_dict()
                if self.acquisition_namespace is not None
                else None
            ),
            "retention": (
                self.retention.to_payload()
                if self.retention is not None
                else None
            ),
            "line_sequence": self.line_sequence,
            "source_sequence": self.source_sequence,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class CoreAcceptedCurrentFacts:
    matrix_identity: MatrixGenerationIdentity
    declared_paths: tuple[str, ...]
    facts: tuple[CoreAcceptedCurrentFact, ...]
    denominator_digest: str
    facts_digest: str
    schema: str = ACCEPTED_CURRENT_FACTS_SCHEMA
    owner: str = ACCEPTED_CURRENT_FACTS_OWNER

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("accepted_current_facts_matrix_identity_required")
        if self.schema != ACCEPTED_CURRENT_FACTS_SCHEMA:
            raise ValueError("accepted_current_facts_schema_invalid")
        if self.owner != ACCEPTED_CURRENT_FACTS_OWNER:
            raise ValueError("accepted_current_facts_owner_invalid")
        if (
            type(self.declared_paths) is not tuple
            or not self.declared_paths
            or any(
                type(path) is not str
                or not path
                or path != path.strip()
                for path in self.declared_paths
            )
            or tuple(sorted(self.declared_paths)) != self.declared_paths
            or len(set(self.declared_paths)) != len(self.declared_paths)
        ):
            raise ValueError("accepted_current_facts_denominator_invalid")
        if (
            type(self.facts) is not tuple
            or any(
                not isinstance(fact, CoreAcceptedCurrentFact)
                for fact in self.facts
            )
        ):
            raise TypeError("accepted_current_facts_typed_facts_required")
        fact_paths = tuple(fact.path for fact in self.facts)
        if fact_paths != self.declared_paths:
            raise ValueError("accepted_current_facts_denominator_incomplete")
        denominator_digest = hashlib.sha256(
            json.dumps(
                self.declared_paths,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        facts_digest = hashlib.sha256(
            json.dumps(
                [fact.to_payload() for fact in self.facts],
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if self.denominator_digest != denominator_digest:
            raise ValueError(
                "accepted_current_facts_denominator_digest_mismatch"
            )
        if self.facts_digest != facts_digest:
            raise ValueError("accepted_current_facts_digest_mismatch")
        for fact in self.facts:
            if (
                fact.source_sequence
                > self.matrix_identity.matrix_sequence
                or (
                    fact.acquisition_namespace is not None
                    and fact.acquisition_namespace
                    != self.matrix_identity.namespace
                )
            ):
                raise ValueError(
                    "accepted_current_fact_matrix_identity_mismatch"
                )

    @classmethod
    def prepare(
        cls,
        *,
        matrix_identity: MatrixGenerationIdentity,
        facts: tuple[CoreAcceptedCurrentFact, ...],
    ) -> "CoreAcceptedCurrentFacts":
        ordered = tuple(sorted(facts, key=lambda fact: fact.path))
        paths = tuple(fact.path for fact in ordered)
        denominator_digest = hashlib.sha256(
            json.dumps(
                paths,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        facts_digest = hashlib.sha256(
            json.dumps(
                [fact.to_payload() for fact in ordered],
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            matrix_identity=matrix_identity,
            declared_paths=paths,
            facts=ordered,
            denominator_digest=denominator_digest,
            facts_digest=facts_digest,
        )

    def exact(self, path: str) -> CoreAcceptedCurrentFact:
        if (
            type(path) is not str
            or not path
            or path != path.strip()
        ):
            raise TypeError("accepted_current_fact_path_invalid")
        for fact in self.facts:
            if fact.path == path:
                return fact
        raise KeyError(f"accepted_current_fact_not_declared:{path}")

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
    ) -> "CoreAcceptedCurrentFacts":
        if not isinstance(payload, dict):
            raise TypeError("accepted_current_facts_payload_required")
        if set(payload) != {
            "schema",
            "owner",
            "matrix_identity",
            "declared_paths",
            "denominator_digest",
            "facts_digest",
            "facts",
        }:
            raise ValueError("accepted_current_facts_payload_contract_invalid")
        raw_facts = payload["facts"]
        if not isinstance(raw_facts, list):
            raise TypeError("accepted_current_facts_payload_list_required")
        return cls(
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                payload["matrix_identity"]
            ),
            declared_paths=tuple(payload["declared_paths"]),
            facts=tuple(
                CoreAcceptedCurrentFact.from_payload(fact)
                for fact in raw_facts
            ),
            denominator_digest=payload["denominator_digest"],
            facts_digest=payload["facts_digest"],
            schema=payload["schema"],
            owner=payload["owner"],
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "matrix_identity": self.matrix_identity.as_dict(),
            "declared_paths": list(self.declared_paths),
            "denominator_digest": self.denominator_digest,
            "facts_digest": self.facts_digest,
            "facts": [fact.to_payload() for fact in self.facts],
        }


__all__ = (
    "ACCEPTED_CURRENT_FACTS_OWNER",
    "ACCEPTED_CURRENT_FACTS_SCHEMA",
    "CoreAcceptedCurrentFact",
    "CoreAcceptedCurrentFacts",
)
