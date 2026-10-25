"""Sole process-lifetime Application lease owner."""

from __future__ import annotations

import ctypes
import json
import math
import os
from pathlib import Path
import tempfile
from threading import RLock
from time import time
from types import MappingProxyType
from typing import Any, BinaryIO, Mapping
from uuid import uuid4

from arch4.architecture_contracts.application_lifecycle import (
    ApplicationLeaseIdentity,
    ApplicationLeaseReceipt,
    ApplicationLeaseState,
    ProductStartupDisposition,
    ProductStartupReceipt,
)
from arch4.architecture_contracts.runtime_generation import (
    ModemLiveReadyReceipt,
)


APPLICATION_LEASE_OWNER = "54guc.application_lease"
APPLICATION_LEASE_LOCK_FILENAME = ".guc_application.lock"
APPLICATION_LEASE_RECEIPT_FILENAME = "application_lease.json"
_CURRENT_APPLICATION_LEASE: ApplicationLease | None = None
_CURRENT_APPLICATION_LEASE_LOCK = RLock()
_APPLICATION_LEASE_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "owner",
        "lease_id",
        "generation",
        "process_id",
        "process_started_at_unix_ms",
        "profile",
        "executable_path",
        "stage_dir",
        "state",
        "terminal",
        "sequence",
        "claimed_at",
        "updated_at",
        "detail",
    }
)
_APPLICATION_LEASE_ENVIRONMENT_FIELDS = (
    "ARCH_APPLICATION_LEASE_ID",
    "ARCH_APPLICATION_LEASE_GENERATION",
    "ARCH_APPLICATION_LEASE_PROCESS_ID",
    "ARCH_APPLICATION_LEASE_PROCESS_BIRTH_MS",
    "ARCH_APPLICATION_LEASE_PROFILE",
    "ARCH_APPLICATION_LEASE_EXECUTABLE_PATH",
    "ARCH_APPLICATION_LEASE_STAGE_DIR",
)


def _freeze_json_value(value: Any, *, path: str) -> Any:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path}_finite_number_required")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(f"{path}_string_key_required")
            frozen[key] = _freeze_json_value(
                item,
                path=f"{path}.{key}",
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    raise TypeError(f"{path}_json_value_required")


def _freeze_detail(
    detail: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    value: Mapping[str, Any] = {} if detail is None else detail
    if not isinstance(value, Mapping):
        raise TypeError("application_lease_receipt_detail_mapping_required")
    frozen = _freeze_json_value(value, path="application_lease_receipt_detail")
    if not isinstance(frozen, Mapping):
        raise TypeError("application_lease_receipt_detail_mapping_required")
    return frozen


def _thaw_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _thaw_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _require_exact_int(value: Any, *, error: str, positive: bool = True) -> int:
    if type(value) is not int:
        raise TypeError(error)
    normalized = int(value)
    if positive and normalized <= 0:
        raise ValueError(error)
    return normalized


def _require_finite_timestamp(value: Any, *, error: str) -> float:
    if type(value) not in {int, float}:
        raise TypeError(error)
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValueError(error)
    return normalized


def _require_environment_identity_string(
    source: Mapping[str, str],
    key: str,
) -> str:
    if key not in source:
        raise ValueError(f"application_lease_environment_{key}_required")
    value = source[key]
    if type(value) is not str:
        raise TypeError(f"application_lease_environment_{key}_string_required")
    if not value.strip():
        raise ValueError(f"application_lease_environment_{key}_required")
    return value


def _require_environment_identity_int(
    source: Mapping[str, str],
    key: str,
) -> int:
    value = _require_environment_identity_string(source, key)
    if (
        not value.isascii()
        or not value.isdigit()
        or value.startswith("0")
    ):
        raise ValueError(
            f"application_lease_environment_{key}_canonical_int_required"
        )
    parsed = int(value)
    if parsed <= 0 or str(parsed) != value:
        raise ValueError(
            f"application_lease_environment_{key}_canonical_int_required"
        )
    return parsed


def _product_ready_proof(
    receipt: ProductStartupReceipt,
) -> Mapping[str, Any]:
    if not isinstance(receipt, ProductStartupReceipt):
        raise TypeError(
            "application_lease_product_ready_product_receipt_required"
        )
    if receipt.disposition is not ProductStartupDisposition.READY:
        raise ValueError(
            "application_lease_product_ready_disposition_required"
        )
    lifecycle_generation = int(receipt.lifecycle_generation)
    if lifecycle_generation <= 0:
        raise ValueError(
            "application_lease_product_ready_lifecycle_generation_invalid"
        )
    modem_live_ready = receipt.matrix_readiness
    if not isinstance(
        modem_live_ready,
        ModemLiveReadyReceipt,
    ):
        raise TypeError(
            "application_lease_product_ready_modem_live_receipt_required"
        )
    if modem_live_ready.lifecycle_generation != lifecycle_generation:
        raise ValueError(
            "application_lease_product_ready_lifecycle_mismatch"
        )
    if not modem_live_ready.ready:
        raise ValueError(
            "application_lease_product_ready_startup_not_admitted"
        )
    return _freeze_detail(
        {
            "product_startup_receipt": receipt.as_dict(),
        }
    )


def application_lifecycle_authority_dir() -> Path:
    """Return the one local per-user lifecycle authority directory."""

    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        return (Path(root) / "54GUC" / "application_lifecycle").resolve()
    runtime_root = os.environ.get("XDG_RUNTIME_DIR")
    if (
        runtime_root
        and Path(runtime_root).is_dir()
        and os.access(runtime_root, os.W_OK)
    ):
        return (
            Path(runtime_root) / "54guc" / "application_lifecycle"
        ).resolve()
    user_id = getattr(os, "getuid", lambda: 0)()
    return (
        Path(tempfile.gettempdir())
        / f"54guc-{user_id}"
        / "application_lifecycle"
    ).resolve()


def application_lease_receipt_path(
    authority_dir: str | Path | None = None,
) -> Path:
    root = Path(
        authority_dir or application_lifecycle_authority_dir()
    ).resolve()
    return root / APPLICATION_LEASE_RECEIPT_FILENAME


def current_process_started_at_unix_ms() -> int:
    """Return a process-birth identity independently verifiable on Windows."""

    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.argtypes = []
        get_current_process.restype = wintypes.HANDLE
        get_process_times = kernel32.GetProcessTimes
        get_process_times.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        get_process_times.restype = wintypes.BOOL
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = get_process_times(
            get_current_process(),
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            raise OSError(
                ctypes.get_last_error(),
                "application_lease_process_birth_query_failed",
            )
        created_100ns = (
            int(creation.dwHighDateTime) << 32
        ) | int(creation.dwLowDateTime)
        unix_100ns = created_100ns - 116444736000000000
        if unix_100ns <= 0:
            raise RuntimeError(
                "application_lease_process_birth_invalid"
            )
        return unix_100ns // 10_000
    if os.name == "posix":
        stat_path = Path(f"/proc/{os.getpid()}/stat")
        if stat_path.is_file():
            try:
                fields = stat_path.read_text(encoding="ascii").split()
                clock_ticks = int(os.sysconf("SC_CLK_TCK"))
                boot_seconds = float(
                    Path("/proc/stat")
                    .read_text(encoding="ascii")
                    .split("btime ", 1)[1]
                    .splitlines()[0]
                )
                return max(
                    1,
                    int(
                        (
                            boot_seconds
                            + (int(fields[21]) / float(clock_ticks))
                        )
                        * 1000.0
                    ),
                )
            except Exception:
                pass
    raise RuntimeError("application_lease_process_birth_unavailable")


class ApplicationLease:
    """Hold one OS lock and publish its exact process identity atomically."""

    def __init__(
        self,
        *,
        app_root: str | Path,
        profile: str,
        authority_dir: str | Path | None = None,
        lock_path: str | Path | None = None,
        executable_path: str | Path | None = None,
        process_id: int | None = None,
        process_started_at_unix_ms: int | None = None,
    ) -> None:
        root = Path(app_root).resolve()
        self.app_root = root
        self.authority_dir = Path(
            authority_dir or application_lifecycle_authority_dir()
        ).resolve()
        self.lock_path = Path(
            lock_path
            or (self.authority_dir / APPLICATION_LEASE_LOCK_FILENAME)
        ).resolve()
        if self.lock_path.parent != self.authority_dir:
            raise ValueError("application_lease_authority_split_forbidden")
        self.receipt_path = application_lease_receipt_path(
            self.authority_dir
        )
        if type(profile) is not str:
            raise TypeError("application_lease_profile_string_required")
        self._profile = profile.strip()
        if not self._profile:
            raise ValueError("application_lease_profile_required")
        self._process_id = _require_exact_int(
            os.getpid() if process_id is None else process_id,
            error="application_lease_process_id_integer_required",
        )
        self._process_started_at_unix_ms = _require_exact_int(
            (
                current_process_started_at_unix_ms()
                if process_started_at_unix_ms is None
                else process_started_at_unix_ms
            ),
            error="application_lease_process_birth_integer_required",
        )
        self._executable_path = Path(
            executable_path or os.path.abspath(os.sys.executable)
        ).resolve()
        self._handle: BinaryIO | None = None
        self._identity: ApplicationLeaseIdentity | None = None
        self._claimed_at = 0.0
        self._sequence = 0
        self._state: ApplicationLeaseState | None = None
        self._product_ready_proof: Mapping[str, Any] | None = None
        self._lock = RLock()

    @property
    def acquired(self) -> bool:
        with self._lock:
            return self._handle is not None

    @property
    def identity(self) -> ApplicationLeaseIdentity:
        with self._lock:
            identity = self._identity
        if identity is None:
            raise RuntimeError("application_lease_not_acquired")
        return identity

    @property
    def state(self) -> ApplicationLeaseState | None:
        with self._lock:
            return self._state

    def acquire(self) -> bool:
        global _CURRENT_APPLICATION_LEASE
        with self._lock:
            if self._handle is not None:
                raise RuntimeError("application_lease_already_acquired")
            with _CURRENT_APPLICATION_LEASE_LOCK:
                if _CURRENT_APPLICATION_LEASE is not None:
                    return False
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            if self.lock_path.is_dir():
                try:
                    self.lock_path.rmdir()
                except OSError:
                    return False
            handle = open(self.lock_path, "a+b")
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(
                        handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
            except (OSError, IOError):
                handle.close()
                return False
            claimed_at = time()
            prior_generation = 0
            prior_detail: dict[str, Any] = {
                "prior_receipt_status": "missing",
                "cross_lease_ordering": "unknown",
            }
            try:
                prior = read_application_lease_receipt(self.receipt_path)
                prior_generation = int(
                    prior.get("generation", 0) or 0
                )
                prior_detail = {
                    "prior_receipt_status": "validated",
                    "prior_lease_id": str(prior.get("lease_id") or ""),
                    "prior_generation": prior_generation,
                    "prior_state": str(prior.get("state") or ""),
                    "cross_lease_ordering": "known",
                }
            except FileNotFoundError:
                pass
            except Exception as exc:
                prior_detail = {
                    "prior_receipt_status": "corrupt",
                    "prior_receipt_error": (
                        f"{type(exc).__name__}: {exc}"
                    ),
                    "cross_lease_ordering": "unknown",
                }
            self._handle = handle
            self._claimed_at = claimed_at
            self._product_ready_proof = None
            self._identity = ApplicationLeaseIdentity(
                lease_id=uuid4().hex,
                generation=prior_generation + 1,
                process_id=self._process_id,
                process_started_at_unix_ms=(
                    self._process_started_at_unix_ms
                ),
                profile=self._profile,
                executable_path=str(self._executable_path),
                stage_dir=str(self.app_root),
            )
            with _CURRENT_APPLICATION_LEASE_LOCK:
                if _CURRENT_APPLICATION_LEASE is not None:
                    self._handle = None
                    self._unlock_handle_unlocked(handle)
                    return False
                _CURRENT_APPLICATION_LEASE = self
            try:
                self._publish_unlocked(
                    ApplicationLeaseState.CLAIMED,
                    detail=prior_detail,
                )
            except BaseException:
                self._handle = None
                with _CURRENT_APPLICATION_LEASE_LOCK:
                    if _CURRENT_APPLICATION_LEASE is self:
                        _CURRENT_APPLICATION_LEASE = None
                self._unlock_handle_unlocked(handle)
                raise
            return True

    def transition(
        self,
        state: ApplicationLeaseState,
        **detail: Any,
    ) -> ApplicationLeaseReceipt:
        with self._lock:
            if self._handle is None:
                raise RuntimeError("application_lease_not_acquired")
            next_state = ApplicationLeaseState(state)
            current = self._state
            allowed = {
                ApplicationLeaseState.CLAIMED: {
                    ApplicationLeaseState.STARTING,
                    ApplicationLeaseState.STOPPING,
                    ApplicationLeaseState.FAILED,
                },
                ApplicationLeaseState.STARTING: {
                    ApplicationLeaseState.PROCESS_RUNNING,
                    ApplicationLeaseState.STOPPING,
                    ApplicationLeaseState.FAILED,
                },
                ApplicationLeaseState.PROCESS_RUNNING: {
                    ApplicationLeaseState.PRODUCT_READY,
                    ApplicationLeaseState.STOPPING,
                    ApplicationLeaseState.FAILED,
                },
                ApplicationLeaseState.PRODUCT_READY: {
                    ApplicationLeaseState.STOPPING,
                    ApplicationLeaseState.FAILED,
                },
                ApplicationLeaseState.STOPPING: {
                    ApplicationLeaseState.STOPPED,
                    ApplicationLeaseState.FAILED,
                },
                ApplicationLeaseState.STOPPED: set(),
                ApplicationLeaseState.FAILED: set(),
            }
            if current is None or next_state not in allowed[current]:
                raise RuntimeError(
                    "application_lease_transition_invalid:"
                    f"{getattr(current, 'value', 'missing')}->{next_state.value}"
                )
            if next_state is ApplicationLeaseState.PRODUCT_READY:
                raise RuntimeError(
                    "application_lease_product_ready_requires_typed_receipt"
                )
            return self._publish_unlocked(next_state, detail=detail)

    def promote_product_ready(
        self,
        receipt: ProductStartupReceipt,
    ) -> bool:
        proof = _product_ready_proof(receipt)
        with self._lock:
            if self._handle is None:
                return False
            if self._state is ApplicationLeaseState.PRODUCT_READY:
                if self._product_ready_proof != proof:
                    raise RuntimeError(
                        "application_lease_product_ready_identity_conflict"
                    )
                return True
            if self._state is not ApplicationLeaseState.PROCESS_RUNNING:
                raise RuntimeError(
                    "application_lease_product_ready_before_process_running"
                )
            published = self._publish_unlocked(
                ApplicationLeaseState.PRODUCT_READY,
                detail=proof,
            )
            self._product_ready_proof = proof
            return published.state is ApplicationLeaseState.PRODUCT_READY

    def release(
        self,
        *,
        failed: bool = False,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            handle = self._handle
            if handle is None:
                return
            terminal = (
                ApplicationLeaseState.FAILED
                if failed
                else ApplicationLeaseState.STOPPED
            )
            current = self._state
            publication_error: BaseException | None = None
            try:
                if current not in {
                    ApplicationLeaseState.STOPPED,
                    ApplicationLeaseState.FAILED,
                }:
                    if (
                        current is not ApplicationLeaseState.STOPPING
                        and not failed
                    ):
                        self._publish_unlocked(
                            ApplicationLeaseState.STOPPING
                        )
                    self._publish_unlocked(
                        terminal,
                        detail=dict(detail or {}),
                    )
            except BaseException as exc:
                publication_error = exc
            finally:
                self._handle = None
                global _CURRENT_APPLICATION_LEASE
                with _CURRENT_APPLICATION_LEASE_LOCK:
                    if _CURRENT_APPLICATION_LEASE is self:
                        _CURRENT_APPLICATION_LEASE = None
                self._unlock_handle_unlocked(handle)
            if publication_error is not None:
                raise publication_error

    @staticmethod
    def _unlock_handle_unlocked(handle: BinaryIO) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _publish_unlocked(
        self,
        state: ApplicationLeaseState,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> ApplicationLeaseReceipt:
        identity = self._identity
        if identity is None:
            raise RuntimeError("application_lease_identity_unavailable")
        frozen_detail = _freeze_detail(detail)
        next_sequence = self._sequence + 1
        receipt = ApplicationLeaseReceipt(
            identity=identity,
            state=state,
            sequence=next_sequence,
            claimed_at=self._claimed_at,
            updated_at=max(self._claimed_at, time()),
            detail=frozen_detail,
        )
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.receipt_path.name}.",
            suffix=".tmp",
            dir=str(self.receipt_path.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    _thaw_json_value(receipt.to_payload()),
                    stream,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.receipt_path)
        finally:
            if temporary.exists():
                temporary.unlink()
        self._sequence = next_sequence
        self._state = state
        return receipt


def read_application_lease_receipt(
    path: str | Path,
) -> Mapping[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("application_lease_receipt_mapping_required")
    if frozenset(payload) != _APPLICATION_LEASE_RECEIPT_KEYS:
        raise ValueError("application_lease_receipt_keys_invalid")
    if payload.get("schema") != "ApplicationLeaseReceipt.v1":
        raise ValueError("application_lease_receipt_schema_invalid")
    if payload.get("owner") != APPLICATION_LEASE_OWNER:
        raise ValueError("application_lease_receipt_owner_invalid")
    for key in (
        "lease_id",
        "profile",
        "executable_path",
        "stage_dir",
        "state",
    ):
        if type(payload.get(key)) is not str:
            raise TypeError(
                f"application_lease_receipt_{key}_string_required"
            )
    generation = _require_exact_int(
        payload.get("generation"),
        error="application_lease_receipt_generation_integer_required",
    )
    process_id = _require_exact_int(
        payload.get("process_id"),
        error="application_lease_receipt_process_id_integer_required",
    )
    process_started_at_unix_ms = _require_exact_int(
        payload.get("process_started_at_unix_ms"),
        error="application_lease_receipt_process_birth_integer_required",
    )
    sequence = _require_exact_int(
        payload.get("sequence"),
        error="application_lease_receipt_sequence_integer_required",
    )
    claimed_at = _require_finite_timestamp(
        payload.get("claimed_at"),
        error="application_lease_receipt_claimed_at_invalid",
    )
    updated_at = _require_finite_timestamp(
        payload.get("updated_at"),
        error="application_lease_receipt_updated_at_invalid",
    )
    if updated_at < claimed_at:
        raise ValueError("application_lease_receipt_timestamp_order_invalid")
    if type(payload.get("terminal")) is not bool:
        raise TypeError("application_lease_receipt_terminal_bool_required")
    raw_detail = payload.get("detail")
    if not isinstance(raw_detail, Mapping):
        raise ValueError("application_lease_receipt_detail_invalid")
    frozen_detail = _freeze_detail(raw_detail)
    identity = ApplicationLeaseIdentity(
        lease_id=payload["lease_id"],
        generation=generation,
        process_id=process_id,
        process_started_at_unix_ms=process_started_at_unix_ms,
        profile=payload["profile"],
        executable_path=payload["executable_path"],
        stage_dir=payload["stage_dir"],
    )
    receipt = ApplicationLeaseReceipt(
        identity=identity,
        state=ApplicationLeaseState(payload["state"]),
        sequence=sequence,
        claimed_at=claimed_at,
        updated_at=updated_at,
        detail=frozen_detail,
        schema=payload["schema"],
        owner=payload["owner"],
    )
    if payload["terminal"] is not receipt.state.terminal:
        raise ValueError("application_lease_terminal_flag_mismatch")
    return _freeze_json_value(
        receipt.to_payload(),
        path="application_lease_receipt",
    )


def current_application_lease_identity() -> Mapping[str, Any]:
    """Return the exact in-process lease identity, or an empty mapping."""

    with _CURRENT_APPLICATION_LEASE_LOCK:
        lease = _CURRENT_APPLICATION_LEASE
    if lease is None:
        return {}
    identity = lease.identity
    with _CURRENT_APPLICATION_LEASE_LOCK:
        if _CURRENT_APPLICATION_LEASE is not lease:
            return {}
    return MappingProxyType(identity.to_payload())


def mark_current_application_product_ready(
    *,
    receipt: ProductStartupReceipt,
) -> bool:
    """Promote only from the exact typed Product/Matrix readiness proof."""

    _product_ready_proof(receipt)
    with _CURRENT_APPLICATION_LEASE_LOCK:
        lease = _CURRENT_APPLICATION_LEASE
    if lease is None:
        return False
    return lease.promote_product_ready(receipt)


def application_lease_identity_from_environment(
    environ: Mapping[str, str] | None = None,
) -> ApplicationLeaseIdentity | None:
    """Validate the launcher identity inherited by NGUI evidence writers."""

    source = environ if environ is not None else os.environ
    if "ARCH_APPLICATION_LEASE_ID" not in source:
        return None
    lease_id = _require_environment_identity_string(
        source,
        "ARCH_APPLICATION_LEASE_ID",
    )
    for key in _APPLICATION_LEASE_ENVIRONMENT_FIELDS:
        if key not in source:
            raise ValueError(f"application_lease_environment_{key}_required")
    return ApplicationLeaseIdentity(
        lease_id=lease_id,
        generation=_require_environment_identity_int(
            source,
            "ARCH_APPLICATION_LEASE_GENERATION",
        ),
        process_id=_require_environment_identity_int(
            source,
            "ARCH_APPLICATION_LEASE_PROCESS_ID",
        ),
        process_started_at_unix_ms=_require_environment_identity_int(
            source,
            "ARCH_APPLICATION_LEASE_PROCESS_BIRTH_MS",
        ),
        profile=_require_environment_identity_string(
            source,
            "ARCH_APPLICATION_LEASE_PROFILE",
        ),
        executable_path=_require_environment_identity_string(
            source,
            "ARCH_APPLICATION_LEASE_EXECUTABLE_PATH",
        ),
        stage_dir=_require_environment_identity_string(
            source,
            "ARCH_APPLICATION_LEASE_STAGE_DIR",
        ),
    )


__all__ = (
    "APPLICATION_LEASE_LOCK_FILENAME",
    "APPLICATION_LEASE_OWNER",
    "APPLICATION_LEASE_RECEIPT_FILENAME",
    "ApplicationLease",
    "application_lease_identity_from_environment",
    "application_lifecycle_authority_dir",
    "application_lease_receipt_path",
    "current_application_lease_identity",
    "current_process_started_at_unix_ms",
    "mark_current_application_product_ready",
    "read_application_lease_receipt",
)
