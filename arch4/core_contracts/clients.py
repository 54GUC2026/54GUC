"""Immutable identity and capability declaration for one Core customer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


CORE_CLIENT_MANIFEST_SCHEMA = "CoreClientManifest.v1"
CORE_CLIENT_MANIFEST_OWNER = "54guc.core.client_manifest"


class CoreClientRole(str, Enum):
    OBSERVER = "observer"
    OPERATOR = "operator"
    OPERATOR_AUTOMATION = "operator_automation"
    AUTONOMOUS_INTELLIGENCE = "autonomous_intelligence"
    PRODUCT_BOUNDARY = "product_boundary"
    FOUNDATION_SERVICE = "foundation_service"
    DIAGNOSTIC = "diagnostic"


def _required_text(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"core_client_{field_name}_required")
    return text


def _contract_versions(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        candidates = (values,)
    else:
        try:
            candidates = tuple(values or ())
        except TypeError as exc:
            raise TypeError("core_client_contract_versions_must_be_iterable") from exc
    normalized: list[str] = []
    for value in candidates:
        text = str(value or "").strip()
        if not text:
            raise ValueError("core_client_contract_version_required")
        if text in normalized:
            raise ValueError(f"core_client_contract_version_duplicate:{text}")
        normalized.append(text)
    if not normalized:
        raise ValueError("core_client_contract_versions_required")
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class CoreClientManifest:
    client_id: str
    role: CoreClientRole
    contract_versions: tuple[str, ...]
    queue_budget: int
    required: bool = False
    schema: str = CORE_CLIENT_MANIFEST_SCHEMA
    owner: str = CORE_CLIENT_MANIFEST_OWNER

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "client_id",
            _required_text(self.client_id, field_name="id"),
        )
        if not isinstance(self.role, CoreClientRole):
            raise TypeError("core_client_typed_role_required")
        object.__setattr__(
            self,
            "contract_versions",
            _contract_versions(self.contract_versions),
        )
        queue_budget = int(self.queue_budget)
        if queue_budget <= 0:
            raise ValueError("core_client_positive_queue_budget_required")
        object.__setattr__(self, "queue_budget", queue_budget)
        if type(self.required) is not bool:
            raise TypeError("core_client_required_must_be_bool")
        if self.schema != CORE_CLIENT_MANIFEST_SCHEMA:
            raise ValueError("core_client_manifest_schema_invalid")
        if self.owner != CORE_CLIENT_MANIFEST_OWNER:
            raise ValueError("core_client_manifest_owner_invalid")

    def supports(self, contract_version: str) -> bool:
        return str(contract_version or "").strip() in self.contract_versions

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "client_id": self.client_id,
            "role": self.role.value,
            "contract_versions": list(self.contract_versions),
            "queue_budget": self.queue_budget,
            "required": self.required,
        }


__all__ = (
    "CORE_CLIENT_MANIFEST_OWNER",
    "CORE_CLIENT_MANIFEST_SCHEMA",
    "CoreClientManifest",
    "CoreClientRole",
)
