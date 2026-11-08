"""Coherent Foundation evidence snapshot over stable owner cursors."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Mapping

from arch4.atomic_io import atomic_write_json
from arch4.foundation.live_artifact_reader import (
    LiveArtifactReadError,
    read_live_json_object,
)
from arch4.foundation.runtime_artifacts import (
    CGF_EVIDENCE_FILENAME,
    CURRENT_STATE_FILENAME,
    CURRENT_STATE_OWNER,
    CURRENT_STATE_SCHEMA,
    EVENT_OWNER,
    EVENT_SCHEMA,
    RUNTIME_HEALTH_FILENAME,
    RUNTIME_HEALTH_OWNER,
    RUNTIME_HEALTH_SCHEMA,
    foundation_diagnostic_event_record_errors,
    read_cgf_evidence_events,
)


FOUNDATION_EVIDENCE_SNAPSHOT_FILENAME = "foundation_evidence_snapshot.json"
FOUNDATION_EVIDENCE_SNAPSHOT_SCHEMA = "FoundationEvidenceSnapshot.v1"
FOUNDATION_EVIDENCE_SNAPSHOT_OWNER = "54guc.foundation.evidence_snapshot"
FOUNDATION_EVIDENCE_SNAPSHOT_MAX_EVENTS = 4096
FOUNDATION_EVIDENCE_FAILURE_RECEIPTS = (
    "site_report_failure_receipt.json",
    "support_bundle_failure_receipt.json",
)


def _canonical_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _source_status(
    path: Path,
    *,
    schema: str,
    owner: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    try:
        payload = read_live_json_object(
            path,
            timeout_s=0.25,
            retry_interval_s=0.01,
        )
    except LiveArtifactReadError as exc:
        disposition = "missing" if not path.exists() else "unavailable"
        return None, {
            "path": path.name,
            "disposition": disposition,
            "reason": f"{type(exc).__name__}: {exc}",
            "sequence": 0,
            "sha256": "",
        }
    if payload.get("schema") != schema or payload.get("owner") != owner:
        return None, {
            "path": path.name,
            "disposition": "invalid_contract",
            "reason": "schema_or_owner_invalid",
            "sequence": 0,
            "sha256": "",
        }
    try:
        sequence = int(payload.get("sequence") or 0)
    except (TypeError, ValueError):
        sequence = 0
    if sequence <= 0:
        return None, {
            "path": path.name,
            "disposition": "invalid_contract",
            "reason": "positive_sequence_required",
            "sequence": 0,
            "sha256": "",
        }
    digest = _canonical_digest(payload)
    return payload, {
        "path": path.name,
        "disposition": "captured",
        "reason": "",
        "sequence": sequence,
        "sha256": digest,
    }


def _failure_receipts(root: Path) -> tuple[dict[str, Any], ...]:
    receipts: list[dict[str, Any]] = []
    for name in FOUNDATION_EVIDENCE_FAILURE_RECEIPTS:
        path = root / name
        if not path.is_file():
            continue
        try:
            payload = read_live_json_object(
                path,
                timeout_s=0.25,
                retry_interval_s=0.01,
            )
        except LiveArtifactReadError:
            continue
        receipts.append(
            {
                "artifact": name,
                "payload": payload,
                "sha256": _canonical_digest(payload),
            }
        )
    return tuple(receipts)


def foundation_evidence_snapshot_contract_errors(
    payload: object,
) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ("top_level_object_required",)
    expected = {
        "schema",
        "owner",
        "snapshot_id",
        "captured_at",
        "complete",
        "coherence_attempts",
        "source_status",
        "current_state",
        "runtime_health",
        "events",
        "failure_receipts",
        "omissions",
        "content_sha256",
    }
    errors: list[str] = []
    if set(payload) != expected:
        errors.append("fields_mismatch")
    if payload.get("schema") != FOUNDATION_EVIDENCE_SNAPSHOT_SCHEMA:
        errors.append("schema_invalid")
    if payload.get("owner") != FOUNDATION_EVIDENCE_SNAPSHOT_OWNER:
        errors.append("owner_invalid")
    if type(payload.get("complete")) is not bool:
        errors.append("complete_boolean_required")
    attempts = payload.get("coherence_attempts")
    if type(attempts) is not int or attempts <= 0:
        errors.append("coherence_attempts_invalid")
    try:
        captured_at = float(payload.get("captured_at") or 0.0)
    except (TypeError, ValueError):
        captured_at = 0.0
    if not math.isfinite(captured_at) or captured_at <= 0.0:
        errors.append("captured_at_invalid")
    source_status = payload.get("source_status")
    if not isinstance(source_status, dict):
        errors.append("source_status_invalid")
    elif set(source_status) != {"current_state", "runtime_health", "events"}:
        errors.append("source_status_fields_invalid")
    elif not all(isinstance(value, dict) for value in source_status.values()):
        errors.append("source_status_entry_invalid")
    current_state = payload.get("current_state")
    runtime_health = payload.get("runtime_health")
    if current_state is not None and not isinstance(current_state, dict):
        errors.append("current_state_invalid")
    if runtime_health is not None and not isinstance(runtime_health, dict):
        errors.append("runtime_health_invalid")
    for field in (
        "events",
        "failure_receipts",
        "omissions",
    ):
        if not isinstance(payload.get(field), list):
            errors.append(f"{field}_invalid")
    events = payload.get("events")
    if isinstance(events, list):
        previous_sequence = 0
        for index, record in enumerate(events, start=1):
            row_errors = foundation_diagnostic_event_record_errors(
                record,
                previous_sequence=previous_sequence,
            )
            errors.extend(
                f"event_{index}:{error}" for error in row_errors
            )
            if isinstance(record, dict) and type(record.get("sequence")) is int:
                previous_sequence = int(record["sequence"])
    omissions = payload.get("omissions")
    if isinstance(omissions, list) and not all(
        isinstance(item, str) and item for item in omissions
    ):
        errors.append("omissions_entry_invalid")
    if payload.get("complete") is True:
        if current_state is None or runtime_health is None or omissions:
            errors.append("complete_state_inconsistent")
    content = {
        key: value
        for key, value in payload.items()
        if key not in {"content_sha256", "snapshot_id"}
    }
    if str(payload.get("content_sha256") or "") != _canonical_digest(content):
        errors.append("content_sha256_invalid")
    snapshot_seed = {
        "captured_at": payload.get("captured_at"),
        "source_status": payload.get("source_status"),
        "content_sha256": payload.get("content_sha256"),
    }
    expected_snapshot_id = (
        "foundation-snapshot:" + _canonical_digest(snapshot_seed)[:24]
    )
    if payload.get("snapshot_id") != expected_snapshot_id:
        errors.append("snapshot_id_invalid")
    return tuple(errors)


def seal_foundation_evidence_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Seal newly captured or explicitly transformed content in one place."""
    content = dict(payload)
    content["content_sha256"] = _canonical_digest({
        key: value for key, value in content.items()
        if key not in {"content_sha256", "snapshot_id"}
    })
    content["snapshot_id"] = "foundation-snapshot:" + _canonical_digest({
        "captured_at": content["captured_at"],
        "source_status": content["source_status"],
        "content_sha256": content["content_sha256"],
    })[:24]
    errors = foundation_evidence_snapshot_contract_errors(content)
    if errors:
        raise ValueError("foundation_snapshot_seal_invalid:" + ",".join(errors))
    return content


class FoundationEvidenceSnapshotService:
    """Capture stable current-state/health cursors and atomic journal evidence."""

    def __init__(
        self,
        runtime_dir: str | Path,
        *,
        max_events: int = FOUNDATION_EVIDENCE_SNAPSHOT_MAX_EVENTS,
        coherence_retries: int = 3,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).resolve()
        self.path = self.runtime_dir / FOUNDATION_EVIDENCE_SNAPSHOT_FILENAME
        self.max_events = max(1, int(max_events))
        self.coherence_retries = max(1, int(coherence_retries))

    def capture(
        self,
        *,
        now: float | None = None,
        require_complete: bool = False,
    ) -> dict[str, Any]:
        captured_at = float(time.time() if now is None else now)
        if not math.isfinite(captured_at) or captured_at <= 0.0:
            raise ValueError("foundation_evidence_positive_timestamp_required")
        current: dict[str, Any] | None = None
        health: dict[str, Any] | None = None
        current_status: dict[str, Any] = {}
        health_status: dict[str, Any] = {}
        events: tuple[dict[str, Any], ...] = ()
        events_status: dict[str, Any] = {}
        coherent = False
        attempts = 0
        for attempts in range(1, self.coherence_retries + 1):
            current_before, current_before_status = _source_status(
                self.runtime_dir / CURRENT_STATE_FILENAME,
                schema=CURRENT_STATE_SCHEMA,
                owner=CURRENT_STATE_OWNER,
            )
            health_before, health_before_status = _source_status(
                self.runtime_dir / RUNTIME_HEALTH_FILENAME,
                schema=RUNTIME_HEALTH_SCHEMA,
                owner=RUNTIME_HEALTH_OWNER,
            )
            events_before, events_before_status = read_cgf_evidence_events(
                self.runtime_dir / CGF_EVIDENCE_FILENAME,
                max_events=self.max_events,
            )
            current_after, current_after_status = _source_status(
                self.runtime_dir / CURRENT_STATE_FILENAME,
                schema=CURRENT_STATE_SCHEMA,
                owner=CURRENT_STATE_OWNER,
            )
            health_after, health_after_status = _source_status(
                self.runtime_dir / RUNTIME_HEALTH_FILENAME,
                schema=RUNTIME_HEALTH_SCHEMA,
                owner=RUNTIME_HEALTH_OWNER,
            )
            events_after, events_after_status = read_cgf_evidence_events(
                self.runtime_dir / CGF_EVIDENCE_FILENAME,
                max_events=self.max_events,
            )
            current = current_before
            health = health_before
            current_status = current_before_status
            health_status = health_before_status
            events = events_before
            events_status = events_before_status
            coherent = (
                current_before_status == current_after_status
                and health_before_status == health_after_status
                and events_before_status == events_after_status
                and current_before == current_after
                and health_before == health_after
                and events_before == events_after
            )
            if coherent:
                break
        complete = bool(
            coherent
            and current is not None
            and health is not None
            and events_status.get("disposition") in {"captured", "missing"}
        )
        omissions: list[str] = []
        if not coherent:
            omissions.append("owner_cursors_changed_during_capture")
        if current is None:
            omissions.append(
                "current_state:" + str(current_status.get("disposition") or "missing")
            )
        if health is None:
            omissions.append(
                "runtime_health:" + str(health_status.get("disposition") or "missing")
            )
        if events_status.get("disposition") not in {"captured", "missing"}:
            omissions.append(
                "events:" + str(events_status.get("disposition") or "unavailable")
            )
        if require_complete and not complete:
            raise RuntimeError(
                "foundation_evidence_snapshot_incomplete:" + ",".join(omissions)
            )
        content: dict[str, Any] = {
            "schema": FOUNDATION_EVIDENCE_SNAPSHOT_SCHEMA,
            "owner": FOUNDATION_EVIDENCE_SNAPSHOT_OWNER,
            "snapshot_id": "",
            "captured_at": captured_at,
            "complete": complete,
            "coherence_attempts": attempts,
            "source_status": {
                "current_state": current_status,
                "runtime_health": health_status,
                "events": events_status,
            },
            "current_state": current,
            "runtime_health": health,
            "events": list(events),
            "failure_receipts": list(_failure_receipts(self.runtime_dir)),
            "omissions": omissions,
        }
        content = seal_foundation_evidence_snapshot(content)
        atomic_write_json(
            self.path,
            content,
            sort_keys=True,
            trailing_newline=True,
        )
        return content

    def load(self) -> dict[str, Any]:
        payload = read_live_json_object(self.path)
        errors = foundation_evidence_snapshot_contract_errors(payload)
        if errors:
            raise RuntimeError(
                "foundation_evidence_snapshot_invalid:" + ",".join(errors)
            )
        return payload


def capture_foundation_evidence_snapshot(
    runtime_dir: str | Path,
    *,
    now: float | None = None,
    max_events: int = FOUNDATION_EVIDENCE_SNAPSHOT_MAX_EVENTS,
    require_complete: bool = False,
) -> dict[str, Any]:
    return FoundationEvidenceSnapshotService(
        runtime_dir,
        max_events=max_events,
    ).capture(now=now, require_complete=require_complete)


__all__ = (
    "FOUNDATION_EVIDENCE_FAILURE_RECEIPTS",
    "FOUNDATION_EVIDENCE_SNAPSHOT_FILENAME",
    "FOUNDATION_EVIDENCE_SNAPSHOT_MAX_EVENTS",
    "FOUNDATION_EVIDENCE_SNAPSHOT_OWNER",
    "FOUNDATION_EVIDENCE_SNAPSHOT_SCHEMA",
    "FoundationEvidenceSnapshotService",
    "capture_foundation_evidence_snapshot",
    "foundation_evidence_snapshot_contract_errors",
    "seal_foundation_evidence_snapshot",
)
