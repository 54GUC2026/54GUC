"""Admission rule for Foundation housekeeping dispatch."""

from __future__ import annotations

from dataclasses import dataclass


FOUNDATION_SERVICE_LIVENESS_INTERVAL_S = 1.0


@dataclass(slots=True)
class FoundationServiceAdmission:
    """Run meaningful changes immediately and quiet liveness work once per second."""

    last_register_sequence: int = -1
    last_service_at: float = 0.0

    def admit(
        self,
        *,
        register_sequence: int,
        observed_at: float,
        force: bool = False,
    ) -> bool:
        if type(register_sequence) is not int or register_sequence < 0:
            raise TypeError("foundation_service_register_sequence_required")
        now = float(observed_at)
        if now < 0.0:
            raise ValueError("foundation_service_observed_time_invalid")
        if register_sequence < self.last_register_sequence:
            raise RuntimeError("foundation_service_register_sequence_regressed")
        due = bool(
            force
            or register_sequence > self.last_register_sequence
            or self.last_service_at <= 0.0
            or now - self.last_service_at
            >= FOUNDATION_SERVICE_LIVENESS_INTERVAL_S
        )
        if due:
            self.last_register_sequence = register_sequence
            self.last_service_at = now
        return due


__all__ = (
    "FOUNDATION_SERVICE_LIVENESS_INTERVAL_S",
    "FoundationServiceAdmission",
)
