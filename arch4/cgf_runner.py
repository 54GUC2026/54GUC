"""Headless lifecycle owner for the Core + NGUI suppliers + Foundation product."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from pathlib import Path
import signal
import threading
import time
from typing import Any

from arch4.interfaces import ARCH_SW_LABEL, Arch4Config
from arch4.foundation.service_admission import FoundationServiceAdmission
from arch4.modem.base import ModemInterface
from arch4.architecture_contracts.application_lifecycle import (
    ProductShutdownReceipt,
    ProductStartupReceipt,
)
from arch4.product_supervisor import (
    ProductHeadlessRuntimePort,
    ProductStartupDisposition,
    ProductSupervisor,
)
from arch4.runtime_paths import resolve_cgf_product_storage


log = logging.getLogger("arch4.cgf_runner")
CGF_RUNNER_OWNER = "54guc.cgf_runner"


@dataclass(frozen=True, slots=True)
class CGFRunnerTerminationReceipt:
    product: ProductShutdownReceipt | None
    errors: tuple[str, ...]
    finished_at: float
    schema: str = "CGFRunnerTerminationReceipt.v1"
    owner: str = CGF_RUNNER_OWNER

    def __post_init__(self) -> None:
        if self.product is not None and not isinstance(
            self.product,
            ProductShutdownReceipt,
        ):
            raise TypeError(
                "cgf_runner_termination_product_receipt_invalid"
            )
        if (
            not isinstance(self.errors, tuple)
            or any(
                type(item) is not str
                or not item
                or item != item.strip()
                for item in self.errors
            )
        ):
            raise TypeError(
                "cgf_runner_termination_errors_tuple_required"
            )
        if (
            type(self.finished_at) is not float
            or not math.isfinite(self.finished_at)
            or self.finished_at <= 0.0
        ):
            raise ValueError(
                "cgf_runner_termination_timestamp_invalid"
            )
        if self.schema != "CGFRunnerTerminationReceipt.v1":
            raise ValueError("cgf_runner_termination_schema_invalid")
        if self.owner != CGF_RUNNER_OWNER:
            raise ValueError("cgf_runner_termination_owner_invalid")
        if self.product is None and not self.errors:
            raise ValueError(
                "cgf_runner_missing_product_receipt_requires_error"
            )

    @property
    def terminated(self) -> bool:
        return bool(
            self.product is not None
            and self.product.terminated
            and not self.errors
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "terminated": self.terminated,
            "product": (
                self.product.as_dict()
                if self.product is not None
                else None
            ),
            "errors": list(self.errors),
            "finished_at": self.finished_at,
        }


class CGFRunner:
    """Run exactly one permanent CGF product without an Intelligence cycle."""

    def __init__(
        self,
        config: Arch4Config,
        *,
        modem: ModemInterface | None = None,
        verify_only: bool = True,
        check_only: bool = False,
        observe_only: bool = True,
        probe_mode: bool = False,
        runtime_dir: str | Path | None = None,
    ) -> None:
        if not isinstance(config, Arch4Config):
            raise TypeError("cgf_runner_arch4_config_required")
        self.cfg = config
        self.check_only = bool(check_only)
        self.observe_only = bool(observe_only)
        self.probe_mode = bool(probe_mode)
        self._running = False
        self._lifecycle_lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._stop_request_errors: list[str] = []
        self._shutdown_request_succeeded = False
        self._finalizing = False
        self._runtime_namespace = None
        runtime, state = resolve_cgf_product_storage(
            config.modem_backend,
            runtime_dir=runtime_dir,
        )
        runtime.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        self.product = ProductSupervisor(
            config,
            settings_dir=runtime,
            state_dir=state,
            runtime_dir=runtime,
            modem=modem,
            verify_only=verify_only,
            check_only=self.check_only,
            observe_only=self.observe_only,
            probe_mode=self.probe_mode,
        )
        self.runtime: ProductHeadlessRuntimePort = (
            self.product.headless_runtime_port()
        )
        self._last_termination_receipt: (
            CGFRunnerTerminationReceipt | None
        ) = None

    def run(self) -> None:
        self._install_signal_handlers()
        try:
            self._mark_health("startup_begin")
            startup_receipt = self._startup_until_ready()
            if startup_receipt is None:
                return
            if self._stop_requested.is_set():
                return
            if self.check_only:
                self._mark_health("check_complete")
                self.runtime.disconnect_physical()
                return
            with self._lifecycle_lock:
                if self._stop_requested.is_set():
                    return
                self._running = True
                self.runtime.activate(startup_receipt)
                self._runtime_namespace = (
                    startup_receipt.matrix_readiness.matrix_identity.namespace
                )
            mode = "OBSERVE ONLY" if self.observe_only else "LIVE"
            log.info("%s — CGF monitor running [%s]", ARCH_SW_LABEL, mode)
            self._service_loop()
        except Exception as exc:
            self._report_fault("run_uncaught", exc, retryable=False)
            self._mark_health("fatal_runner", error=exc)
            log.exception("CGF runner stopped after an uncaught lifecycle failure")
        finally:
            with self._lifecycle_lock:
                self._running = False
                self._finalizing = True
                self._request_product_shutdown_locked(
                    phase="final",
                )
                errors = list(self._stop_request_errors)
            product_receipt: ProductShutdownReceipt | None = None
            try:
                product_receipt = self.product.shutdown()
            except Exception as exc:
                errors.append(
                    f"product_shutdown:{type(exc).__name__}:{exc}"
                )
            receipt = CGFRunnerTerminationReceipt(
                product=product_receipt,
                errors=tuple(dict.fromkeys(errors)),
                finished_at=float(time.time()),
            )
            with self._lifecycle_lock:
                self._last_termination_receipt = receipt

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_requested.set()
            self._running = False
            if not self._finalizing:
                self._request_product_shutdown_locked(
                    phase="external",
                )

    def _request_product_shutdown_locked(self, *, phase: str) -> None:
        """Request Product stop once, retrying only a failed request."""

        if self._shutdown_request_succeeded:
            return
        try:
            self.product.request_shutdown()
        except Exception as exc:
            self._stop_request_errors.append(
                f"product_stop_request_{phase}:"
                f"{type(exc).__name__}:{exc}"
            )
        else:
            self._shutdown_request_succeeded = True

    def termination_receipt(
        self,
    ) -> CGFRunnerTerminationReceipt | None:
        with self._lifecycle_lock:
            return self._last_termination_receipt

    def _startup_until_ready(self) -> ProductStartupReceipt | None:
        delay_s = 4.0
        while not self._stop_requested.is_set():
            receipt = self.product.startup()
            if receipt.disposition is ProductStartupDisposition.READY:
                return (
                    receipt if not self._stop_requested.is_set() else None
                )
            error = RuntimeError(
                f"{receipt.error_type or 'StartupError'}: "
                f"{receipt.detail or receipt.reason}"
            )
            if (
                receipt.disposition is ProductStartupDisposition.TERMINAL
                or self.check_only
            ):
                self._mark_health(
                    "startup_blocked",
                    error=error,
                    reason=receipt.reason,
                )
                log.error("CGF startup blocked: %s", receipt.detail or receipt.reason)
                return None
            self._mark_health(
                "reconnecting",
                error=error,
                reconnect_delay_s=delay_s,
            )
            log.warning(
                "CGF startup unavailable; retrying in %.0fs: %s",
                delay_s,
                receipt.detail or receipt.reason,
            )
            if self._stop_requested.wait(delay_s):
                return None
            delay_s = min(60.0, delay_s * 2.0)
        return None

    def _service_loop(self) -> None:
        failure_streak = 0
        admission = FoundationServiceAdmission()
        force_service = False
        while self._running and not self._stop_requested.is_set():
            try:
                if self._runtime_namespace is None:
                    raise RuntimeError("cgf_runner_runtime_namespace_missing")
                observed_at = time.time()
                if admission.admit(
                    register_sequence=self.runtime.runtime_register_sequence(
                        self._runtime_namespace
                    ),
                    observed_at=time.monotonic(),
                    force=force_service,
                ):
                    self.runtime.service_once(now=observed_at)
                    force_service = False
                failure_streak = 0
                self._stop_requested.wait(0.1)
            except Exception as exc:
                force_service = True
                failure_streak += 1
                retry_s = min(5.0, 0.1 * (2 ** min(failure_streak, 6)))
                self._report_fault("foundation_service_cycle", exc, retryable=True)
                self._mark_health(
                    "degraded",
                    error=exc,
                    failure_streak=failure_streak,
                    retry_s=retry_s,
                )
                log.exception(
                    "CGF Foundation cycle failed; Core remains active and retry follows"
                )
                self._stop_requested.wait(retry_s)

    def _mark_health(
        self,
        phase: str,
        *,
        error: BaseException | str | None = None,
        cycle_count: int = 0,
        **detail: Any,
    ) -> None:
        try:
            self.runtime.mark_health(
                phase,
                cycle_count=cycle_count,
                error=error,
                **detail,
            )
        except Exception as exc:
            log.error("CGF runtime-health publication failed: %s", exc)

    def _report_fault(
        self,
        operation: str,
        error: BaseException,
        *,
        retryable: bool,
    ) -> None:
        try:
            self.runtime.report_fault(
                operation,
                error,
                retryable=retryable,
            )
        except Exception as report_error:
            log.error("CGF fault reporting failed: %s", report_error)

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        log.info("Signal %d received — stopping CGF", signum)
        self.stop()


__all__ = (
    "CGF_RUNNER_OWNER",
    "CGFRunner",
    "CGFRunnerTerminationReceipt",
)
