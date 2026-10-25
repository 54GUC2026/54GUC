"""Immutable JSON object used by transport-neutral Core contracts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping


def _canonical_object(value: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise TypeError("core_payload_mapping_required")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"core_payload_not_json_serializable:{exc}") from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise TypeError("core_payload_json_object_required")
    return encoded, decoded


@dataclass(frozen=True, slots=True)
class CoreFrozenPayload:
    canonical_json: str
    sha256: str
    size_bytes: int
    _decoded: dict[str, Any] = field(
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        if type(self.canonical_json) is not str or not self.canonical_json:
            raise TypeError("core_payload_canonical_json_type_invalid")
        if (
            type(self.sha256) is not str
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise TypeError("core_payload_sha256_type_invalid")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise TypeError("core_payload_size_type_invalid")
        encoded = self.canonical_json
        try:
            decoded = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise ValueError("core_payload_canonical_json_invalid") from exc
        if not isinstance(decoded, dict):
            raise TypeError("core_payload_json_object_required")
        canonical, decoded = _canonical_object(decoded)
        if canonical != encoded:
            raise ValueError("core_payload_json_not_canonical")
        raw = canonical.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        if self.sha256 != digest:
            raise ValueError("core_payload_sha256_mismatch")
        if self.size_bytes != len(raw):
            raise ValueError("core_payload_size_mismatch")
        object.__setattr__(self, "canonical_json", canonical)
        object.__setattr__(self, "sha256", digest)
        object.__setattr__(self, "size_bytes", len(raw))
        object.__setattr__(self, "_decoded", decoded)

    @classmethod
    def _from_canonical(
        cls,
        canonical: str,
        decoded: dict[str, Any],
    ) -> "CoreFrozenPayload":
        """Construct one owner-validated payload without validating it twice."""

        raw = canonical.encode("utf-8")
        value = object.__new__(cls)
        object.__setattr__(value, "canonical_json", canonical)
        object.__setattr__(value, "sha256", hashlib.sha256(raw).hexdigest())
        object.__setattr__(value, "size_bytes", len(raw))
        object.__setattr__(value, "_decoded", decoded)
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CoreFrozenPayload":
        canonical, decoded = _canonical_object(value)
        return cls._from_canonical(canonical, decoded)

    @classmethod
    def empty(cls) -> "CoreFrozenPayload":
        return cls.from_mapping({})

    def to_mapping(self) -> dict[str, Any]:
        return deepcopy(self._decoded)

    def get(self, key: str, default: Any = None) -> Any:
        """Read one field without copying unrelated immutable payload data.

        JSON scalars are immutable and can be returned directly.  Containers
        remain defensively copied, preserving the same mutation boundary as
        ``to_mapping`` while avoiding a full-object deepcopy for scalar reads.
        """

        value = self._decoded.get(key, default)
        return deepcopy(value) if isinstance(value, (dict, list)) else value

    def require(self, key: str) -> Any:
        """Read one required field with normal mapping KeyError semantics."""

        value = self._decoded[key]
        return deepcopy(value) if isinstance(value, (dict, list)) else value

    def to_payload(self) -> dict[str, Any]:
        return self.to_mapping()


__all__ = ("CoreFrozenPayload",)
