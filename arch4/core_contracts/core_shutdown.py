"""Typed aggregate proof for the ordered Core shutdown transaction.

Each participant owns its own termination receipt.  This aggregate is the
only owner allowed to interpret those receipts as a Core-wide terminal or
degraded result.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Any, Mapping

from arch4.architecture_contracts.application_lifecycle import (
    ProductChildTerminationReceipt,
    ProductTerminationDisposition,
)
from arch4.core_contracts.commands import (
    CoreCommandGatewayShutdownReceipt,
)
from arch4.core_contracts.current_health import (
    ModemLiveCurrentHealthSinkDetachReceipt,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.modem_live.acquisition import ModemLiveTerminationReceipt
from arch4.runtime_clock_worker_fabric import (
    ClockJobWorkerTerminationReceipt,
)
from arch4.runtime_modem_sample_publication import (
    ModemSamplePublicationTerminationReceipt,
)


class CoreCommandWorkerTerminationDisposition(StrEnum):
    STOPPED = "stopped"
    ALREADY_STOPPED = "already_stopped"
    NOT_STARTED = "not_started"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class CoreCommandWorkerTerminationReceipt:
    disposition: CoreCommandWorkerTerminationDisposition
    thread_present: bool
    alive: bool
    shutdown_started_at: float
    finished_at: float

    def __post_init__(self) -> None:
        if not isinstance(
            self.disposition,
            CoreCommandWorkerTerminationDisposition,
        ):
            raise TypeError(
                "core_command_worker_termination_disposition_typed_required"
            )
        if type(self.thread_present) is not bool or type(self.alive) is not bool:
            raise TypeError(
                "core_command_worker_termination_state_bool_required"
            )
        for field_name in ("shutdown_started_at", "finished_at"):
            value = getattr(self, field_name)
            if type(value) is not float or not math.isfinite(value) or value <= 0.0:
                raise ValueError(
                    f"core_command_worker_termination_{field_name}_invalid"
                )
        if self.finished_at < self.shutdown_started_at:
            raise ValueError(
                "core_command_worker_termination_time_order_invalid"
            )
        expected = (
            CoreCommandWorkerTerminationDisposition.DEGRADED
            if self.alive
            else CoreCommandWorkerTerminationDisposition.NOT_STARTED
            if not self.thread_present
            else self.disposition
        )
        if self.alive and self.disposition is not expected:
            raise ValueError(
                "core_command_worker_termination_disposition_inconsistent"
            )
        if (
            not self.thread_present
            and self.disposition
            is not CoreCommandWorkerTerminationDisposition.NOT_STARTED
        ):
            raise ValueError(
                "core_command_worker_termination_absence_inconsistent"
            )
        if (
            self.thread_present
            and not self.alive
            and self.disposition
            not in {
                CoreCommandWorkerTerminationDisposition.STOPPED,
                CoreCommandWorkerTerminationDisposition.ALREADY_STOPPED,
            }
        ):
            raise ValueError(
                "core_command_worker_termination_terminal_inconsistent"
            )

    @property
    def terminated(self) -> bool:
        return not self.alive

    @property
    def survivors(self) -> tuple[str, ...]:
        return ("core_command_worker",) if self.alive else ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "CoreCommandWorkerTerminationReceipt.v1",
            "owner": "54guc.core.composition",
            "disposition": self.disposition.value,
            "thread_present": self.thread_present,
            "alive": self.alive,
            "shutdown_started_at": self.shutdown_started_at,
            "finished_at": self.finished_at,
            "terminated": self.terminated,
            "survivors": list(self.survivors),
        }


@dataclass(frozen=True, slots=True)
class CoreShutdownReceipt:
    """One ordered, exact interpretation of every Core participant."""

    lifecycle_generation: int
    command_gateway: CoreCommandGatewayShutdownReceipt
    command_worker: CoreCommandWorkerTerminationReceipt
    clock_workers: ClockJobWorkerTerminationReceipt | None
    current_health_sink: ModemLiveCurrentHealthSinkDetachReceipt | None
    modem_sample_publication: ModemSamplePublicationTerminationReceipt | None
    modem_live: ModemLiveTerminationReceipt | None
    persistence: CoreFrozenPayload
    shutdown_started_at: float
    finished_at: float

    def __post_init__(self) -> None:
        if type(self.lifecycle_generation) is not int or self.lifecycle_generation <= 0:
            raise ValueError("core_shutdown_lifecycle_generation_invalid")
        if not isinstance(
            self.command_gateway,
            CoreCommandGatewayShutdownReceipt,
        ):
            raise TypeError("core_shutdown_command_gateway_receipt_required")
        if not isinstance(
            self.command_worker,
            CoreCommandWorkerTerminationReceipt,
        ):
            raise TypeError("core_shutdown_command_worker_receipt_required")
        optional_receipts = (
            (self.clock_workers, ClockJobWorkerTerminationReceipt),
            (
                self.current_health_sink,
                ModemLiveCurrentHealthSinkDetachReceipt,
            ),
            (
                self.modem_sample_publication,
                ModemSamplePublicationTerminationReceipt,
            ),
            (self.modem_live, ModemLiveTerminationReceipt),
        )
        for value, expected_type in optional_receipts:
            if value is not None and not isinstance(value, expected_type):
                raise TypeError(
                    "core_shutdown_participant_receipt_type_invalid"
                )
        if not isinstance(self.persistence, CoreFrozenPayload):
            raise TypeError("core_shutdown_persistence_payload_required")
        for field_name in ("shutdown_started_at", "finished_at"):
            value = getattr(self, field_name)
            if type(value) is not float or not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"core_shutdown_{field_name}_invalid")
        if self.finished_at < self.shutdown_started_at:
            raise ValueError("core_shutdown_time_order_invalid")
        self._validate_ordered_dependency_receipts()

    def _validate_ordered_dependency_receipts(self) -> None:
        prior_complete = bool(
            self.command_gateway.terminated
            and self.command_worker.terminated
        )
        if (self.clock_workers is not None) is not prior_complete:
            raise ValueError("core_shutdown_clock_receipt_order_invalid")
        prior_complete = bool(
            prior_complete
            and self.clock_workers is not None
            and self.clock_workers.terminated
        )
        if (self.current_health_sink is not None) is not prior_complete:
            raise ValueError("core_shutdown_health_sink_receipt_order_invalid")
        prior_complete = bool(
            prior_complete
            and self.current_health_sink is not None
            and self.current_health_sink.detached
            and self.current_health_sink.inflight_callbacks == 0
        )
        if (self.modem_sample_publication is not None) is not prior_complete:
            raise ValueError(
                "core_shutdown_publication_receipt_order_invalid"
            )
        prior_complete = bool(
            prior_complete
            and self.modem_sample_publication is not None
            and self.modem_sample_publication.terminated
        )
        if (self.modem_live is not None) is not prior_complete:
            raise ValueError("core_shutdown_modem_live_receipt_order_invalid")

    @property
    def terminated(self) -> bool:
        return bool(
            self.command_gateway.terminated
            and self.command_worker.terminated
            and self.clock_workers is not None
            and self.clock_workers.terminated
            and self.current_health_sink is not None
            and self.current_health_sink.detached
            and self.current_health_sink.inflight_callbacks == 0
            and self.modem_sample_publication is not None
            and self.modem_sample_publication.terminated
            and self.modem_live is not None
            and self.modem_live.terminated
        )

    @property
    def survivors(self) -> tuple[str, ...]:
        survivors: list[str] = []
        if not self.command_gateway.terminated:
            survivors.append("core_command_gateway")
        survivors.extend(self.command_worker.survivors)
        if self.clock_workers is None:
            survivors.append("core_clock_workers_not_stopped")
        elif not self.clock_workers.terminated:
            survivors.extend(
                self.clock_workers.survivors
                or ("core_clock_workers_termination_incomplete",)
            )
        if self.current_health_sink is None:
            survivors.append(
                "core_modem_live_current_health_sink_not_detached"
            )
        elif (
            not self.current_health_sink.detached
            or self.current_health_sink.inflight_callbacks != 0
        ):
            survivors.append(
                "core_modem_live_current_health_sink_not_detached"
            )
        if self.modem_sample_publication is None:
            survivors.append(
                "core_modem_sample_publication_not_terminated"
            )
        elif not self.modem_sample_publication.terminated:
            survivors.append(
                "core_modem_sample_publication_not_terminated"
            )
        if self.modem_live is None:
            survivors.append("core_modem_live_not_stopped")
        elif not self.modem_live.terminated:
            survivors.extend(
                self.modem_live.survivors
                or ("core_modem_live_termination_incomplete",)
            )
        return tuple(sorted(set(survivors)))

    @property
    def blocked_dependencies(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, receipt in (
                ("clock_workers", self.clock_workers),
                ("current_health_sink", self.current_health_sink),
                ("modem_sample_publication", self.modem_sample_publication),
                ("modem_live", self.modem_live),
            )
            if receipt is None
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "CoreShutdownReceipt.v1",
            "owner": "54guc.core.composition",
            "lifecycle_generation": self.lifecycle_generation,
            "shutdown_started_at": self.shutdown_started_at,
            "finished_at": self.finished_at,
            "terminated": self.terminated,
            "survivors": list(self.survivors),
            "blocked_dependencies": list(self.blocked_dependencies),
            "command_gateway": self.command_gateway.to_payload(),
            "command_worker": self.command_worker.to_payload(),
            "clock_workers": (
                self.clock_workers.to_payload()
                if self.clock_workers is not None
                else None
            ),
            "current_health_sink": (
                self.current_health_sink.to_payload()
                if self.current_health_sink is not None
                else None
            ),
            "modem_sample_publication": (
                self.modem_sample_publication.as_dict()
                if self.modem_sample_publication is not None
                else None
            ),
            "modem_live": (
                self.modem_live.as_dict()
                if self.modem_live is not None
                else None
            ),
            "persistence": self.persistence.to_payload(),
        }

    def to_product_child_receipt(self) -> ProductChildTerminationReceipt:
        survivors = self.survivors
        return ProductChildTerminationReceipt(
            child_id="core",
            lifecycle_generation=self.lifecycle_generation,
            disposition=(
                ProductTerminationDisposition.COMPLETE
                if self.terminated
                else ProductTerminationDisposition.DEGRADED
            ),
            survivors=survivors,
            errors=tuple(
                f"required_survivor:{item}" for item in survivors
            ),
            evidence={"core_shutdown": self.to_payload()},
        )


__all__ = (
    "CoreCommandWorkerTerminationDisposition",
    "CoreCommandWorkerTerminationReceipt",
    "CoreShutdownReceipt",
)
