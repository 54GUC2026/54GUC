"""Foundation-owned billing-period accounting from accepted raw counters."""

from __future__ import annotations

import calendar
from datetime import date, datetime
import hashlib
import json
import logging
import math
import os
from pathlib import Path
from threading import RLock
from typing import Any

from arch4.architecture_contracts.runtime_generation import (
    RuntimeGenerationNamespace,
)
from arch4.atomic_io import atomic_write_json
from arch4.foundation.data_budget import TrafficCounterObservation
from arch4.core_contracts.traffic_counter_transition import counter_pair_increment


FOUNDATION_TRAFFIC_ACCOUNTING_SCHEMA = "FoundationTrafficAccounting.v3"
FOUNDATION_TRAFFIC_ACCOUNTING_OWNER = "54guc.foundation.traffic_accounting"
FOUNDATION_TRAFFIC_ACCOUNTING_FILENAME = "foundation_traffic_accounting.json"
FOUNDATION_TRAFFIC_ACCOUNTING_ROOT_ENV = (
    "ARCH_FOUNDATION_TRAFFIC_ACCOUNTING_ROOT"
)
FOUNDATION_TRAFFIC_ACCOUNTING_DIRNAME = "_foundation_traffic_accounting"
FOUNDATION_TRAFFIC_ACCOUNTING_PRODUCT_DIRNAME = "foundation_traffic_accounting"
FOUNDATION_TRAFFIC_ACCOUNTING_MIGRATION_FILENAME = (
    "foundation_traffic_accounting_migration.json"
)
FOUNDATION_TRAFFIC_ACCOUNTING_MIGRATION_SCHEMA = (
    "FoundationTrafficAccountingMigration.v1"
)

_SCOPE_FIELDS = {
    "accounting_key",
    "billing_scope_id",
    "device_identity_key",
    "session_scope_key",
    "source_epoch",
    "lifecycle_generation",
    "cycle_start_day",
    "cycle_key",
    "day_key",
    "cycle_download_bytes",
    "cycle_upload_bytes",
    "day_used_bytes",
    "total_download_bytes",
    "total_upload_bytes",
    "last_download_counter_bytes",
    "last_upload_counter_bytes",
    "counter_source",
    "observed_at",
    "sample_sequence",
    "status",
}


def _cycle_date(year: int, month: int, cycle_day: int) -> date:
    return date(
        int(year),
        int(month),
        min(int(cycle_day), calendar.monthrange(int(year), int(month))[1]),
    )


def _period_keys(observed_at: float, cycle_day: int) -> tuple[str, str]:
    current = datetime.fromtimestamp(float(observed_at)).date()
    this_start = _cycle_date(current.year, current.month, cycle_day)
    if current < this_start:
        year = current.year if current.month > 1 else current.year - 1
        month = current.month - 1 if current.month > 1 else 12
        cycle_start = _cycle_date(year, month, cycle_day)
    else:
        cycle_start = this_start
    return cycle_start.isoformat(), current.isoformat()


def _counter(value: object, *, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"foundation_traffic_accounting_{field}_required") from exc
    if result < 0:
        raise ValueError(f"foundation_traffic_accounting_{field}_negative")
    return result


def product_traffic_accounting_root() -> Path:
    """Return the version-independent Windows product accounting root."""

    for env_name in ("LOCALAPPDATA", "APPDATA"):
        selected = str(os.environ.get(env_name, "") or "").strip()
        if selected:
            return (
                Path(selected).expanduser().resolve()
                / "54GUC"
                / FOUNDATION_TRAFFIC_ACCOUNTING_PRODUCT_DIRNAME
            )
    return (
        Path.home().resolve()
        / ".54guc"
        / FOUNDATION_TRAFFIC_ACCOUNTING_PRODUCT_DIRNAME
    )


def _validated_state(payload: object) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "owner", "scopes"}
        or payload.get("schema") != FOUNDATION_TRAFFIC_ACCOUNTING_SCHEMA
        or payload.get("owner") != FOUNDATION_TRAFFIC_ACCOUNTING_OWNER
        or not isinstance(payload.get("scopes"), dict)
    ):
        raise RuntimeError("foundation_traffic_accounting_state_invalid")
    for accounting_key, row in payload["scopes"].items():
        if (
            not isinstance(row, dict)
            or set(row) != _SCOPE_FIELDS
            or row.get("accounting_key") != accounting_key
        ):
            raise RuntimeError("foundation_traffic_accounting_scope_state_invalid")
        day = _counter(row["day_used_bytes"], field="day_used_bytes")
        cycle = _counter(
            row["cycle_download_bytes"], field="cycle_download_bytes"
        ) + _counter(row["cycle_upload_bytes"], field="cycle_upload_bytes")
        total = _counter(
            row["total_download_bytes"], field="total_download_bytes"
        ) + _counter(row["total_upload_bytes"], field="total_upload_bytes")
        if day > cycle or cycle > total:
            raise RuntimeError(
                "foundation_traffic_accounting_arithmetic_invariant_invalid"
            )
    return payload


def _reconcile_accounting_rows(
    canonical_row: dict[str, Any],
    former_row: dict[str, Any],
) -> dict[str, Any]:
    """Preserve one proven cycle while adopting the newest counter baseline."""

    identity_fields = (
        "accounting_key",
        "billing_scope_id",
        "device_identity_key",
        "cycle_start_day",
        "counter_source",
    )
    if any(
        canonical_row.get(field) != former_row.get(field)
        for field in identity_fields
    ):
        return dict(canonical_row)
    newer, older = (
        (canonical_row, former_row)
        if float(canonical_row["observed_at"])
        >= float(former_row["observed_at"])
        else (former_row, canonical_row)
    )
    if newer["cycle_key"] != older["cycle_key"]:
        return dict(newer)
    if any(
        int(newer[field]) < int(older[field])
        for field in ("total_download_bytes", "total_upload_bytes")
    ):
        return dict(older)

    reconciled = dict(newer)
    for direction in ("download", "upload"):
        total_field = f"total_{direction}_bytes"
        cycle_field = f"cycle_{direction}_bytes"
        total_growth = int(newer[total_field]) - int(older[total_field])
        restored_cycle = int(older[cycle_field]) + total_growth
        reconciled[cycle_field] = min(
            int(newer[total_field]),
            max(int(newer[cycle_field]), restored_cycle),
        )
    return reconciled


def _reconcile_accounting_states(
    canonical: dict[str, Any],
    former: dict[str, Any],
) -> dict[str, Any]:
    """Merge accounting scopes without creating another live owner."""

    reconciled = {
        "schema": FOUNDATION_TRAFFIC_ACCOUNTING_SCHEMA,
        "owner": FOUNDATION_TRAFFIC_ACCOUNTING_OWNER,
        "scopes": dict(canonical["scopes"]),
    }
    for accounting_key, former_row in former["scopes"].items():
        current_row = reconciled["scopes"].get(accounting_key)
        reconciled["scopes"][accounting_key] = (
            dict(former_row)
            if current_row is None
            else _reconcile_accounting_rows(current_row, former_row)
        )
    return _validated_state(reconciled)


def prepare_portable_traffic_accounting_root(stage_dir: str | Path) -> Path:
    """Choose the main store; the named previous-store import runs once."""

    stage = Path(stage_dir).expanduser().resolve()
    canonical = product_traffic_accounting_root()
    canonical_state = canonical / FOUNDATION_TRAFFIC_ACCOUNTING_FILENAME
    former_state = (
        stage.parent
        / FOUNDATION_TRAFFIC_ACCOUNTING_DIRNAME
        / FOUNDATION_TRAFFIC_ACCOUNTING_FILENAME
    )
    canonical.mkdir(parents=True, exist_ok=True)
    migration_path = canonical / FOUNDATION_TRAFFIC_ACCOUNTING_MIGRATION_FILENAME
    if migration_path.is_file():
        receipt = json.loads(migration_path.read_text(encoding="utf-8"))
        if (receipt.get("schema") != FOUNDATION_TRAFFIC_ACCOUNTING_MIGRATION_SCHEMA
                or receipt.get("owner") != FOUNDATION_TRAFFIC_ACCOUNTING_OWNER
                or receipt.get("continuity") not in {
                    "reconciled_former_sibling_store", "selected_existing_canonical_store",
                    "new_canonical_store"}):
            raise RuntimeError("foundation_traffic_accounting_migration_receipt_invalid")
        return canonical
    if former_state.is_file():
        former = _validated_state(
            json.loads(former_state.read_text(encoding="utf-8"))
        )
        canonical_payload = (
            _validated_state(
                json.loads(canonical_state.read_text(encoding="utf-8"))
            )
            if canonical_state.is_file()
            else {
                "schema": FOUNDATION_TRAFFIC_ACCOUNTING_SCHEMA,
                "owner": FOUNDATION_TRAFFIC_ACCOUNTING_OWNER,
                "scopes": {},
            }
        )
        reconciled = _reconcile_accounting_states(canonical_payload, former)
        if reconciled != canonical_payload:
            atomic_write_json(
                canonical_state,
                reconciled,
                sort_keys=True,
                trailing_newline=True,
            )
        atomic_write_json(
            migration_path,
            {
                "schema": FOUNDATION_TRAFFIC_ACCOUNTING_MIGRATION_SCHEMA,
                "owner": FOUNDATION_TRAFFIC_ACCOUNTING_OWNER,
                "continuity": "reconciled_former_sibling_store",
            },
            sort_keys=True,
            trailing_newline=True,
        )
    elif not migration_path.is_file():
        atomic_write_json(
            migration_path,
            {
                "schema": FOUNDATION_TRAFFIC_ACCOUNTING_MIGRATION_SCHEMA,
                "owner": FOUNDATION_TRAFFIC_ACCOUNTING_OWNER,
                "continuity": (
                    "selected_existing_canonical_store"
                    if canonical_state.is_file()
                    else "new_canonical_store"
                ),
            },
            sort_keys=True,
            trailing_newline=True,
        )
    return canonical


def resolve_traffic_accounting_root(default_root: str | Path) -> Path:
    """Resolve one live owner store; never depend on shutdown handoff."""

    selected = str(
        os.environ.get(FOUNDATION_TRAFFIC_ACCOUNTING_ROOT_ENV, "") or ""
    ).strip()
    return (
        Path(selected).expanduser().resolve()
        if selected
        else Path(default_root).expanduser().resolve()
    )


def _accounting_key(billing_scope_id: str, device_identity_key: str) -> str:
    material = f"{billing_scope_id}|{device_identity_key}".encode("utf-8")
    return "traffic-accounting:" + hashlib.sha256(material).hexdigest()[:32]


def read_accounted_traffic(
    storage_root: str | Path, *, billing_scope_id: str,
    device_identity_key: str, cycle_start_day: int,
    source_namespace: RuntimeGenerationNamespace, counter_source: str,
    as_of: float, router_quota_limit_bytes: int | None = None,
    router_quota_left_bytes: int | None = None,
) -> TrafficCounterObservation | None:
    """Project an existing exact-scope ledger without updating or quarantining it.

    A report is a reader of accounting, never another counter observer. The
    returned timestamp belongs to the saved ledger, independently of the radio
    snapshot. A previous day's record cannot supply today's usage.
    """
    if not isinstance(source_namespace, RuntimeGenerationNamespace):
        return None
    path = Path(storage_root) / FOUNDATION_TRAFFIC_ACCOUNTING_FILENAME
    try:
        with path.open("r", encoding="utf-8") as stream:
            content = stream.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            return None
        state = _validated_state(json.loads(content))
        key = _accounting_key(billing_scope_id, device_identity_key)
        row = state["scopes"].get(key)
        if not isinstance(row, dict):
            return None
        expected = {
            "accounting_key": key, "billing_scope_id": billing_scope_id,
            "device_identity_key": device_identity_key,
            "session_scope_key": source_namespace.session_scope_key,
            "source_epoch": source_namespace.source_epoch,
            "lifecycle_generation": source_namespace.lifecycle_generation,
            "cycle_start_day": cycle_start_day, "counter_source": counter_source,
        }
        if any(row.get(k) != v for k, v in expected.items()):
            return None
        observed_at = float(row["observed_at"])
        if not (math.isfinite(as_of) and math.isfinite(observed_at)
                and 0 < observed_at <= as_of and int(row["sample_sequence"]) > 0):
            return None
        cycle, day = _period_keys(as_of, cycle_start_day)
        recorded_cycle, recorded_day = _period_keys(observed_at, cycle_start_day)
        if (row["cycle_key"], row["day_key"]) != (recorded_cycle, recorded_day):
            return None
        if row["cycle_key"] != cycle:
            return None
        if row["status"] not in {
            "baseline", "updated", "plan_rebased", "counter_rebased",
            "counter_source_rebased", "subscription_rebased", "ok", "native",
        }:
            return None
        return TrafficCounterObservation(
            day_used_bytes=int(row["day_used_bytes"]) if row["day_key"] == day else None,
            current_cycle_download_bytes=int(row["cycle_download_bytes"]),
            current_cycle_upload_bytes=int(row["cycle_upload_bytes"]),
            total_download_bytes=int(row["total_download_bytes"]),
            total_upload_bytes=int(row["total_upload_bytes"]),
            router_quota_limit_bytes=router_quota_limit_bytes,
            router_quota_left_bytes=router_quota_left_bytes,
            observed_at=observed_at, sample_sequence=int(row["sample_sequence"]),
            period_accounting_qualified=True, period_accounting_status=str(row["status"]),
        )
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RuntimeError):
        return None


class FoundationTrafficAccountingAuthority:
    """The only mutable owner of user-cycle usage derived from raw byte counters."""

    def __init__(self, storage_root: str | Path) -> None:
        self.storage_root = Path(storage_root).resolve()
        self.path = self.storage_root / FOUNDATION_TRAFFIC_ACCOUNTING_FILENAME
        self._lock = RLock()
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "schema": FOUNDATION_TRAFFIC_ACCOUNTING_SCHEMA,
                "owner": FOUNDATION_TRAFFIC_ACCOUNTING_OWNER,
                "scopes": {},
            }
        try:
            return _validated_state(
                json.loads(self.path.read_text(encoding="utf-8"))
            )
        except Exception:
            quarantine = self.path.with_name(
                self.path.stem + ".invalid" + self.path.suffix
            )
            quarantine.unlink(missing_ok=True)
            self.path.replace(quarantine)
            return {
                "schema": FOUNDATION_TRAFFIC_ACCOUNTING_SCHEMA,
                "owner": FOUNDATION_TRAFFIC_ACCOUNTING_OWNER,
                "scopes": {},
            }

    def _write(self, state: dict[str, Any]) -> None:
        atomic_write_json(
            self.path,
            state,
            sort_keys=True,
            trailing_newline=True,
        )

    def observe(
        self,
        *,
        billing_scope_id: str,
        device_identity_key: str,
        cycle_start_day: int,
        download_counter_bytes: int,
        upload_counter_bytes: int,
        observed_at: float,
        sample_sequence: int,
        source_namespace: RuntimeGenerationNamespace,
        counter_source: str,
        router_quota_limit_bytes: int | None,
        router_quota_left_bytes: int | None,
        counter_period_started_at: float | None = None,
        counter_interface_started_at: float | None = None,
    ) -> TrafficCounterObservation:
        billing_scope = str(billing_scope_id or "").strip()
        device_identity = str(device_identity_key or "").strip()
        if not billing_scope:
            raise ValueError("foundation_traffic_accounting_billing_scope_required")
        if not device_identity:
            raise ValueError("foundation_traffic_accounting_device_identity_required")
        source = str(counter_source or "").strip()
        if not source:
            raise ValueError("foundation_traffic_accounting_counter_source_required")
        accounting_key = _accounting_key(billing_scope, device_identity)
        cycle_day = int(cycle_start_day)
        if not 1 <= cycle_day <= 31:
            raise ValueError("foundation_traffic_accounting_cycle_day_invalid")
        timestamp = float(observed_at)
        if timestamp <= 0.0:
            raise ValueError("foundation_traffic_accounting_timestamp_required")
        sequence = _counter(sample_sequence, field="sample_sequence")
        if sequence <= 0:
            raise ValueError("foundation_traffic_accounting_sequence_required")
        if not isinstance(source_namespace, RuntimeGenerationNamespace):
            raise TypeError(
                "foundation_traffic_accounting_source_namespace_required"
            )
        current_dl = _counter(
            download_counter_bytes,
            field="download_counter_bytes",
        )
        current_ul = _counter(
            upload_counter_bytes,
            field="upload_counter_bytes",
        )
        cycle_key, day_key = _period_keys(timestamp, cycle_day)

        with self._lock:
            scopes = self._state["scopes"]
            previous = scopes.get(accounting_key)
            if not isinstance(previous, dict):
                previous = {}
            # The modem has one cumulative counter stream. The latest accepted
            # row for that physical device identifies which billing scope last
            # owned it. A returning scope must not subtract its older counter.
            active = max((row for row in scopes.values()
                          if row["device_identity_key"] == device_identity),
                         key=lambda row: (float(row["observed_at"]), int(row["sample_sequence"])),
                         default={})
            owner_changed = bool(active and active["accounting_key"] != accounting_key)
            active_same_generation = bool(active
                and active["session_scope_key"] == source_namespace.session_scope_key
                and active["source_epoch"] == source_namespace.source_epoch
                and active["lifecycle_generation"] == source_namespace.lifecycle_generation)
            if owner_changed and (
                timestamp <= float(active["observed_at"])
                or active_same_generation and sequence <= int(active["sample_sequence"])
            ):
                return TrafficCounterObservation(
                    day_used_bytes=None,
                    current_cycle_download_bytes=None,
                    current_cycle_upload_bytes=None,
                    total_download_bytes=None,
                    total_upload_bytes=None,
                    router_quota_limit_bytes=router_quota_limit_bytes,
                    router_quota_left_bytes=router_quota_left_bytes,
                    observed_at=float(active["observed_at"]),
                    sample_sequence=int(active["sample_sequence"]),
                    period_accounting_qualified=False,
                    period_accounting_status="stale_scope_observation")
            previous_sequence = int(previous.get("sample_sequence") or 0)
            same_generation = bool(
                previous
                and previous.get("session_scope_key")
                == source_namespace.session_scope_key
                and previous.get("source_epoch") == source_namespace.source_epoch
                and int(previous.get("lifecycle_generation") or 0)
                == source_namespace.lifecycle_generation
            )
            stale_generation = bool(
                previous
                and not same_generation
                and timestamp <= float(previous.get("observed_at") or 0.0)
            )
            if previous and (
                (same_generation and (sequence <= previous_sequence
                    or timestamp <= float(previous["observed_at"])))
                or stale_generation
            ):
                return TrafficCounterObservation(
                    day_used_bytes=int(previous["day_used_bytes"]),
                    current_cycle_download_bytes=int(
                        previous["cycle_download_bytes"]
                    ),
                    current_cycle_upload_bytes=int(
                        previous["cycle_upload_bytes"]
                    ),
                    total_download_bytes=int(previous["total_download_bytes"]),
                    total_upload_bytes=int(previous["total_upload_bytes"]),
                    router_quota_limit_bytes=router_quota_limit_bytes,
                    router_quota_left_bytes=router_quota_left_bytes,
                    observed_at=float(previous["observed_at"]),
                    sample_sequence=previous_sequence,
                    period_accounting_qualified=previous["status"] != "counter_discontinuity",
                    period_accounting_status=str(previous["status"]),
                )
            same_device = bool(previous)
            prior_dl = (
                previous.get("last_download_counter_bytes")
                if same_device
                else None
            )
            prior_ul = (
                previous.get("last_upload_counter_bytes")
                if same_device
                else None
            )
            baseline = prior_dl is None and prior_ul is None
            plan_changed = bool(
                previous
                and int(previous.get("cycle_start_day") or 0) != cycle_day
            )
            source_changed = bool(
                previous and str(previous.get("counter_source") or "") != source
            )
            increment = counter_pair_increment(
                None if baseline or source_changed or owner_changed or plan_changed
                else (int(prior_dl), int(prior_ul)),
                (current_dl, current_ul),
                previous_at=float(previous.get("observed_at") or 0),
                observed_at=timestamp,
                period_started_at=counter_period_started_at,
                interface_started_at=counter_interface_started_at,
            )
            if increment is None:
                # Keep the actual accepted baseline and timestamp. Advancing
                # either here would destroy the evidence needed to distinguish
                # a returning old stream from a proved new counter period.
                if previous["status"] != "counter_discontinuity":
                    committed_state = {**self._state, "scopes": {**scopes, accounting_key: {**previous, "status": "counter_discontinuity"}}}
                    self._write(committed_state)
                    self._state = committed_state
                    logging.getLogger(__name__).warning(
                        "Traffic counter discontinuity: scope=%s source=%s previous=%s/%s at=%s "
                        "received=%s/%s at=%s sequence=%s; accepted baseline retained",
                        accounting_key, source, prior_dl, prior_ul, previous["observed_at"],
                        current_dl, current_ul, timestamp, sequence,
                    )
                return TrafficCounterObservation(
                    day_used_bytes=None,
                    current_cycle_download_bytes=None,
                    current_cycle_upload_bytes=None,
                    total_download_bytes=int(previous["total_download_bytes"]),
                    total_upload_bytes=int(previous["total_upload_bytes"]),
                    router_quota_limit_bytes=router_quota_limit_bytes,
                    router_quota_left_bytes=router_quota_left_bytes,
                    observed_at=timestamp,
                    sample_sequence=sequence,
                    period_accounting_qualified=False,
                    period_accounting_status="counter_discontinuity",
                )
            dl_delta, ul_delta = increment
            period_changed = bool(previous and counter_period_started_at is not None
                                  and float(previous["observed_at"]) < counter_period_started_at)
            interface_restarted = bool(previous and counter_interface_started_at is not None
                and float(previous["observed_at"]) < counter_interface_started_at
                and current_dl < int(prior_dl))
            cycle_changed = previous.get("cycle_key") != cycle_key
            day_changed = previous.get("day_key") != day_key
            cycle_dl = 0 if cycle_changed else int(previous.get("cycle_download_bytes") or 0)
            cycle_ul = 0 if cycle_changed else int(previous.get("cycle_upload_bytes") or 0)
            day_used = 0 if day_changed else int(previous.get("day_used_bytes") or 0)
            total_dl = int(previous.get("total_download_bytes") or 0)
            total_ul = int(previous.get("total_upload_bytes") or 0)
            if baseline:
                total_dl = current_dl
                total_ul = current_ul
            elif plan_changed:
                # A mid-day billing change starts a new plan period. Rebase
                # the day and billing cycle together.
                cycle_dl = 0
                cycle_ul = 0
                day_used = 0
            elif source_changed or owner_changed:
                # Counters from different sources cannot share a baseline.
                pass
            else:
                cycle_dl += dl_delta
                cycle_ul += ul_delta
                day_used += dl_delta + ul_delta
                total_dl += dl_delta
                total_ul += ul_delta
            status = (
                "baseline"
                if baseline
                else "plan_rebased"
                if plan_changed
                else "counter_source_rebased"
                if source_changed
                else "subscription_rebased"
                if owner_changed
                else "counter_rebased"
                if period_changed or interface_restarted
                else "updated"
            )
            next_state = {
                "accounting_key": accounting_key,
                "billing_scope_id": billing_scope,
                "device_identity_key": device_identity,
                "session_scope_key": source_namespace.session_scope_key,
                "source_epoch": source_namespace.source_epoch,
                "lifecycle_generation": source_namespace.lifecycle_generation,
                "cycle_start_day": cycle_day,
                "cycle_key": cycle_key,
                "day_key": day_key,
                "cycle_download_bytes": cycle_dl,
                "cycle_upload_bytes": cycle_ul,
                "day_used_bytes": day_used,
                "total_download_bytes": total_dl,
                "total_upload_bytes": total_ul,
                "last_download_counter_bytes": current_dl,
                "last_upload_counter_bytes": current_ul,
                "counter_source": source,
                "observed_at": timestamp,
                "sample_sequence": sequence,
                "status": status,
            }
            if next_state != previous:
                committed_state = {**self._state, "scopes": {**scopes, accounting_key: next_state}}
                self._write(committed_state)
                self._state = committed_state

        return TrafficCounterObservation(
            day_used_bytes=day_used,
            current_cycle_download_bytes=cycle_dl,
            current_cycle_upload_bytes=cycle_ul,
            total_download_bytes=total_dl,
            total_upload_bytes=total_ul,
            router_quota_limit_bytes=router_quota_limit_bytes,
            router_quota_left_bytes=router_quota_left_bytes,
            observed_at=timestamp,
            sample_sequence=sequence,
            period_accounting_qualified=True,
            period_accounting_status=status,
        )


__all__ = (
    "FOUNDATION_TRAFFIC_ACCOUNTING_FILENAME",
    "FOUNDATION_TRAFFIC_ACCOUNTING_DIRNAME",
    "FOUNDATION_TRAFFIC_ACCOUNTING_MIGRATION_FILENAME",
    "FOUNDATION_TRAFFIC_ACCOUNTING_MIGRATION_SCHEMA",
    "FOUNDATION_TRAFFIC_ACCOUNTING_ROOT_ENV",
    "FOUNDATION_TRAFFIC_ACCOUNTING_OWNER",
    "FOUNDATION_TRAFFIC_ACCOUNTING_SCHEMA",
    "FoundationTrafficAccountingAuthority",
    "product_traffic_accounting_root",
    "prepare_portable_traffic_accounting_root",
    "resolve_traffic_accounting_root",
    "read_accounted_traffic",
)
