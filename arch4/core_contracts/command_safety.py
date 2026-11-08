"""Immutable mechanical-safety evidence for one Core cellular command."""

from __future__ import annotations

from dataclasses import dataclass
import math

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.current_health import (
    MODEM_LIVE_CURRENT_HEALTH_OWNER,
    ModemLiveHealthState,
)


CORE_COMMAND_SAFETY_SCHEMA = "CoreCommandSafetyEvidence.v1"
CORE_COMMAND_SAFETY_OWNER = "54guc.core.command_safety_adapter"
CORE_COMMAND_OPINION_SCHEMA = "CoreCommandOpinion.v1"
CORE_COMMAND_OPINION_OWNER = "54guc.core.action_admission"
MODEM_HEALTH_OWNER = MODEM_LIVE_CURRENT_HEALTH_OWNER
MODEM_HEALTH_STATES = frozenset(state.value for state in ModemLiveHealthState)


def _required(value: object, reason: str) -> str:
    if not isinstance(value, str):
        raise TypeError(reason)
    if not value or value != value.strip():
        raise ValueError(reason)
    return value


@dataclass(frozen=True, slots=True)
class CoreCommandSafetyEvidence:
    """Request-bound projection of Core-readable modem safety truth.

    The contract owns no policy and performs no modem I/O.  It prevents the
    Bouncer from reconstructing health or identity from unrelated mutable
    runtime surfaces. Physical action capability is admitted earlier by Core
    from the accepted Matrix view.
    """

    request_id: str
    matrix_identity: MatrixGenerationIdentity
    identity_code: str
    identity_key: str
    modem_health_owner: str
    modem_health_state: str
    observed_at: float
    schema: str = CORE_COMMAND_SAFETY_SCHEMA
    owner: str = CORE_COMMAND_SAFETY_OWNER

    def __post_init__(self) -> None:
        for field_name in (
            "request_id",
            "identity_code",
            "identity_key",
        ):
            object.__setattr__(
                self,
                field_name,
                _required(
                    getattr(self, field_name),
                    f"core_command_safety_{field_name}_required",
                ),
            )
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError(
                "core_command_safety_matrix_identity_required"
            )
        if self.modem_health_owner != MODEM_HEALTH_OWNER:
            raise ValueError("core_command_safety_modem_health_owner_invalid")
        if (
            not isinstance(self.modem_health_state, str)
            or self.modem_health_state not in MODEM_HEALTH_STATES
        ):
            raise ValueError("core_command_safety_modem_health_state_invalid")
        if (
            type(self.observed_at) is not float
            or not math.isfinite(self.observed_at)
            or self.observed_at <= 0.0
        ):
            raise ValueError("core_command_safety_positive_observed_at_required")
        if self.schema != CORE_COMMAND_SAFETY_SCHEMA:
            raise ValueError("core_command_safety_schema_invalid")
        if self.owner != CORE_COMMAND_SAFETY_OWNER:
            raise ValueError("core_command_safety_owner_invalid")

    @property
    def session_id(self) -> str:
        return self.matrix_identity.session_scope_key

    @property
    def source_epoch(self) -> str:
        return self.matrix_identity.source_epoch

    @property
    def source_sequence(self) -> int:
        return self.matrix_identity.matrix_sequence


@dataclass(frozen=True, slots=True)
class CoreCommandOpinion:
    """Non-binding Core execution opinion supplied to the sole Bouncer."""

    request_id: str
    matrix_identity: MatrixGenerationIdentity
    allowed: bool
    reason: str
    observed_at: float
    critical: bool = False
    schema: str = CORE_COMMAND_OPINION_SCHEMA
    owner: str = CORE_COMMAND_OPINION_OWNER

    def __post_init__(self) -> None:
        _required(self.request_id, "core_command_opinion_request_id_required")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("core_command_opinion_matrix_identity_required")
        if type(self.allowed) is not bool:
            raise TypeError("core_command_opinion_allowed_required")
        if type(self.critical) is not bool:
            raise TypeError("core_command_opinion_critical_required")
        if self.allowed and self.critical:
            raise ValueError("core_command_opinion_allowed_critical_invalid")
        _required(self.reason, "core_command_opinion_reason_required")
        if (
            type(self.observed_at) is not float
            or not math.isfinite(self.observed_at)
            or self.observed_at <= 0.0
        ):
            raise ValueError("core_command_opinion_timestamp_invalid")
        if self.schema != CORE_COMMAND_OPINION_SCHEMA:
            raise ValueError("core_command_opinion_schema_invalid")
        if self.owner != CORE_COMMAND_OPINION_OWNER:
            raise ValueError("core_command_opinion_owner_invalid")


__all__ = (
    "CORE_COMMAND_SAFETY_OWNER",
    "CORE_COMMAND_SAFETY_SCHEMA",
    "CORE_COMMAND_OPINION_OWNER",
    "CORE_COMMAND_OPINION_SCHEMA",
    "CoreCommandOpinion",
    "CoreCommandSafetyEvidence",
    "MODEM_HEALTH_OWNER",
    "MODEM_HEALTH_STATES",
)
