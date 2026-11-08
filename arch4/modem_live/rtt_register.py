"""Immutable register that exposes Modem Live RTT evidence to many readers."""

from __future__ import annotations

from dataclasses import dataclass

from arch4.rtt_observation import RttObservation
from arch4.modem_live.service_confirmation import ServiceConfirmationReceipt


SCHEMA = "ModemLiveRttRegisterSnapshot.v1"
OWNER = "modem_live"


@dataclass(frozen=True, slots=True)
class ModemLiveRttRegisterSnapshot:
    """One session-bound read of Modem Live RTT evidence."""

    sequence: int
    session_scope_key: str
    identity_code: str
    observation: RttObservation
    confirmation: ServiceConfirmationReceipt | None = None
    schema: str = SCHEMA
    owner: str = OWNER

    def __post_init__(self) -> None:
        sequence = int(self.sequence)
        if sequence < 0:
            raise ValueError("modem_live_rtt_register_sequence_invalid")
        if not isinstance(self.observation, RttObservation):
            raise TypeError("modem_live_rtt_register_observation_required")
        if self.schema != SCHEMA or self.owner != OWNER:
            raise ValueError("modem_live_rtt_register_owner_schema_invalid")
        if self.confirmation is not None and (
            not isinstance(self.confirmation, ServiceConfirmationReceipt)
            or self.confirmation.namespace != self.observation.namespace
        ):
            raise ValueError("modem_live_confirmation_namespace_invalid")
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(
            self,
            "session_scope_key",
            str(self.session_scope_key or "").strip(),
        )
        object.__setattr__(
            self,
            "identity_code",
            str(self.identity_code or "").strip(),
        )


__all__ = (
    "ModemLiveRttRegisterSnapshot",
    "OWNER",
    "SCHEMA",
)
