"""Typed profile discovery suggestion owned by NGUI."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from arch4.modem_live.profile_catalog import ModemProfileSelection


PROFILE_DISCOVERY_SUGGESTION_SCHEMA = "NGUIProfileDiscoverySuggestion.v1"
PROFILE_DISCOVERY_SUGGESTION_OWNER = "54guc.ngui.profile_discovery"


@dataclass(frozen=True, slots=True)
class ProfileDiscoverySuggestion:
    observed_at: float
    host: str
    backend: str
    profile_id: str
    manufacturer: str
    model: str
    confidence: float

    def __post_init__(self) -> None:
        if self.observed_at <= 0:
            raise ValueError("profile discovery timestamp must be positive")
        if not self.host:
            raise ValueError("profile discovery host required")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("profile discovery confidence must be within 0..1")

    def as_dict(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schema": PROFILE_DISCOVERY_SUGGESTION_SCHEMA,
                "owner": PROFILE_DISCOVERY_SUGGESTION_OWNER,
                "observed_at": float(self.observed_at),
                "host": self.host,
                "backend": self.backend,
                "profile_id": self.profile_id,
                "manufacturer": self.manufacturer,
                "model": self.model,
                "confidence": float(self.confidence),
            }
        )


def discovery_suggestion_from_result(
    result: Mapping[str, Any],
    *,
    contracts: Iterable[ModemProfileSelection],
    observed_at: float,
) -> ProfileDiscoverySuggestion | None:
    entries = result.get("entries")
    if not isinstance(entries, (list, tuple)):
        raise TypeError("profile discovery result entries required")
    candidate = next(
        (
            entry
            for entry in entries
            if isinstance(entry, Mapping)
            and str(entry.get("category") or "")
            in {"supported_modem", "possible_modem"}
        ),
        None,
    )
    if candidate is None:
        return None
    host = str(candidate.get("host") or "").strip()
    backend = str(candidate.get("detected_backend") or "").strip().lower()
    matches = tuple(
        contract
        for contract in contracts
        if str(contract.backend or "").strip().lower() == backend
    )
    exact = matches[0] if len(matches) == 1 else None
    return ProfileDiscoverySuggestion(
        observed_at=float(observed_at),
        host=host,
        backend=backend,
        profile_id=exact.profile_id if exact is not None else "",
        manufacturer=exact.manufacturer if exact is not None else "",
        model=exact.label if exact is not None else "",
        confidence=float(candidate.get("confidence") or 0.0),
    )


__all__ = [
    "PROFILE_DISCOVERY_SUGGESTION_OWNER",
    "PROFILE_DISCOVERY_SUGGESTION_SCHEMA",
    "ProfileDiscoverySuggestion",
    "discovery_suggestion_from_result",
]
