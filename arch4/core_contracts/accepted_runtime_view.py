"""One typed Core interpretation of one exact accepted Matrix generation."""

from __future__ import annotations

from dataclasses import dataclass
import math

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.core_contracts.commands import CoreCommandIngressContext
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.radio_target_contract import RadioTargetFacts
from arch4.rtt_observation import MatrixBoundRttObservation


CORE_ACTION_READBACK_TO_CAPABILITY = (
    ("band_selection", "band_select"),
    ("pband", "pband"),
    ("nr_band_lock", "nr_band_lock"),
    ("band_lock", "band_lock"),
    ("cell_lock", "cell_lock"),
    ("restart", "restart"),
    ("wifi_control", "wifi_control"),
    ("network_settings", "network_settings"),
    ("mobile_data", "mobile_data"),
)


@dataclass(frozen=True, slots=True)
class CoreAcceptedRuntimeView:
    """The only legal post-Matrix interpretation consumed inside Core."""

    matrix_identity: MatrixGenerationIdentity
    accepted_commit_id: str
    matrix_rtt: MatrixBoundRttObservation
    identity_code: str
    identity_status: str
    access_type: str
    sampled_at: float
    accepted_at: float
    published_at: float
    radio_connected: bool | None
    radio_target: RadioTargetFacts
    capability_facts: CoreFrozenPayload
    runtime_capabilities: CoreFrozenPayload
    action_readback: CoreFrozenPayload

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("core_accepted_view_matrix_identity_required")
        if (
            type(self.accepted_commit_id) is not str
            or not self.accepted_commit_id
            or self.accepted_commit_id != self.accepted_commit_id.strip()
        ):
            raise ValueError("core_accepted_view_commit_id_required")
        if not isinstance(self.matrix_rtt, MatrixBoundRttObservation):
            raise TypeError("core_accepted_view_matrix_rtt_required")
        if self.matrix_rtt.matrix_identity != self.matrix_identity:
            raise ValueError("core_accepted_view_rtt_identity_mismatch")
        for field_name in (
            "identity_code",
            "identity_status",
            "access_type",
        ):
            value = getattr(self, field_name)
            if (
                type(value) is not str
                or not value
                or value != value.strip()
            ):
                raise ValueError(
                    f"core_accepted_view_{field_name}_required"
                )
        if self.identity_status not in {"provisional", "confirmed"}:
            raise ValueError("core_accepted_view_identity_status_invalid")
        timestamps = []
        for field_name in ("sampled_at", "accepted_at", "published_at"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(
                    f"core_accepted_view_{field_name}_invalid"
                )
            normalized = float(value)
            if not math.isfinite(normalized) or normalized <= 0.0:
                raise ValueError(
                    f"core_accepted_view_{field_name}_invalid"
                )
            timestamps.append(normalized)
            object.__setattr__(self, field_name, normalized)
        if timestamps != sorted(timestamps):
            raise ValueError("core_accepted_view_timestamp_order_invalid")
        if self.radio_connected is not None and type(self.radio_connected) is not bool:
            raise TypeError("core_accepted_view_radio_connected_invalid")
        if not isinstance(self.radio_target, RadioTargetFacts):
            raise TypeError("core_accepted_view_radio_target_required")
        if (
            self.radio_target.source_sequence
            != self.matrix_identity.matrix_sequence
        ):
            raise ValueError("core_accepted_view_radio_target_mismatch")
        for field_name in (
            "capability_facts",
            "runtime_capabilities",
            "action_readback",
        ):
            if not isinstance(getattr(self, field_name), CoreFrozenPayload):
                raise TypeError(
                    f"core_accepted_view_{field_name}_required"
                )

    @property
    def cellular_automatic_actions_allowed(self) -> bool:
        """Native OFF, or missing readback on a controllable profile, holds automation."""
        if self.radio_target.mobile_data_enabled is not None:
            return self.radio_target.mobile_data_enabled
        actions = self.action_readback.to_mapping().get("action_capabilities", {}).get("actions", {})
        return actions.get("mobile_data", {}).get("enabled") is not True

    def command_ingress_context(self) -> CoreCommandIngressContext:
        return self.project_command_ingress_context(
            matrix_identity=self.matrix_identity,
            identity_code=self.identity_code,
            observed_at=self.sampled_at,
        )

    @staticmethod
    def project_command_ingress_context(
        *,
        matrix_identity: MatrixGenerationIdentity,
        identity_code: str,
        observed_at: float,
    ) -> CoreCommandIngressContext:
        """Project the exact runtime-view fields across a typed DTO boundary."""

        return CoreCommandIngressContext(
            matrix_identity=matrix_identity,
            identity_code=identity_code,
            observed_at=observed_at,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "matrix_identity": self.matrix_identity.as_dict(),
            "accepted_commit_id": self.accepted_commit_id,
            "matrix_rtt": self.matrix_rtt.as_dict(),
            "identity_code": self.identity_code,
            "identity_status": self.identity_status,
            "access_type": self.access_type,
            "sampled_at": self.sampled_at,
            "accepted_at": self.accepted_at,
            "published_at": self.published_at,
            "radio_connected": self.radio_connected,
            "radio_target": self.radio_target.as_mapping(),
            "capability_facts": self.capability_facts.to_payload(),
            "runtime_capabilities": self.runtime_capabilities.to_payload(),
            "action_readback": self.action_readback.to_payload(),
        }


__all__ = ("CoreAcceptedRuntimeView",)
