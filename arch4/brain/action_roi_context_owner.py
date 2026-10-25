"""Canonical action-ROI context identity owner."""

from __future__ import annotations

import time
from typing import Any

OWNER = "arch4.brain.action_roi_context_owner"
SCHEMA = "action_roi_context.v1"


def action_record_context(rec: Any) -> dict[str, Any]:
    params = dict(getattr(rec, "action_params", {}) or {})
    try:
        ts = float(getattr(rec, "timestamp", 0.0) or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    hour = _int_from_any(params.get("_tsar_context_hour"), -1)
    if hour < 0 and ts > 0.0:
        hour = int(time.localtime(ts).tm_hour)
    provider_plmn = str(
        params.get("_tsar_context_provider_plmn")
        or getattr(rec, "provider_plmn", "")
        or ""
    ).strip()
    provider_name = str(
        params.get("_tsar_context_provider_name")
        or getattr(rec, "provider_name", "")
        or ""
    ).strip()
    identity_code = str(
        params.get("_tsar_context_identity_code")
        or getattr(rec, "identity_code", "")
        or ""
    ).strip()
    identity_status = str(
        params.get("_tsar_context_identity_status")
        or getattr(rec, "identity_status", "")
        or ""
    ).strip()
    model = str(
        params.get("_tsar_context_model")
        or getattr(rec, "model", "")
        or ""
    ).strip()
    profile_id = str(
        params.get("_tsar_context_profile_id")
        or getattr(rec, "profile_id", "")
        or ""
    ).strip()
    firmware = str(
        params.get("_tsar_context_firmware")
        or getattr(rec, "firmware", "")
        or ""
    ).strip()
    cell_id = _int_from_any(params.get("_tsar_context_cell_id"), 0)
    pci = _int_from_any(params.get("_tsar_context_pci"), 0)
    earfcn = _int_from_any(params.get("_tsar_context_earfcn"), 0)
    key_parts = []
    if provider_plmn:
        key_parts.append(f"plmn={provider_plmn}")
    if provider_name:
        key_parts.append(f"provider={provider_name.lower()}")
    if identity_code:
        key_parts.append(f"id={identity_code}")
    if model:
        key_parts.append(f"model={model.lower()}")
    if profile_id:
        key_parts.append(f"profile={profile_id}")
    if firmware:
        key_parts.append(f"fw={firmware}")
    if cell_id > 0:
        key_parts.append(f"cell={cell_id}")
    if pci > 0:
        key_parts.append(f"pci={pci}")
    if earfcn > 0:
        key_parts.append(f"earfcn={earfcn}")
    if hour >= 0:
        key_parts.append(f"hour={hour}")
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "provider_plmn": provider_plmn,
        "provider_name": provider_name,
        "identity_code": identity_code,
        "identity_status": identity_status,
        "model": model,
        "profile_id": profile_id,
        "firmware": firmware,
        "cell_id": cell_id,
        "pci": pci,
        "earfcn": earfcn,
        "hour": hour,
        "context_key": "|".join(key_parts),
    }


def remember_outcome_metadata(
    bucket: dict[str, Any],
    *,
    rec: Any,
    outcome: str,
    pband: int,
    ca_pool: list[int],
    level: str,
    code: str,
    label: str,
) -> None:
    prefix = "last_bad" if outcome == "bad" else "last_good"
    try:
        ts = float(getattr(rec, "timestamp", 0.0) or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    existing_ts = float(bucket.get(f"{prefix}_ts", 0.0) or 0.0)
    if existing_ts and ts and ts < existing_ts:
        return
    bucket[f"{prefix}_ts"] = round(ts, 3) if ts > 0.0 else 0.0
    bucket[f"{prefix}_level"] = level
    bucket[f"{prefix}_code"] = code
    bucket[f"{prefix}_label"] = label
    bucket[f"{prefix}_pband"] = int(pband or 0)
    bucket[f"{prefix}_ca_pool"] = list(ca_pool or [])
    bucket[f"{prefix}_provider_plmn"] = str(getattr(rec, "provider_plmn", "") or "").strip()
    bucket[f"{prefix}_provider_name"] = str(getattr(rec, "provider_name", "") or "").strip()
    bucket[f"{prefix}_decision_trace_id"] = str(getattr(rec, "decision_trace_id", "") or "").strip()
    bucket[f"{prefix}_parent_trace_id"] = str(
        getattr(rec, "roi_parent_trace_id", "") or ""
    ).strip()
    bucket[f"{prefix}_proposal_reason"] = str(getattr(rec, "proposal_reason", "") or "").strip()
    bucket[f"{prefix}_verdict_stage"] = str(getattr(rec, "outcome_verdict_stage", "") or "").strip()
    context = action_record_context(rec)
    bucket[f"{prefix}_identity_code"] = str(context.get("identity_code", "") or "")
    bucket[f"{prefix}_identity_status"] = str(context.get("identity_status", "") or "")
    bucket[f"{prefix}_model"] = str(context.get("model", "") or "")
    bucket[f"{prefix}_profile_id"] = str(context.get("profile_id", "") or "")
    bucket[f"{prefix}_firmware"] = str(context.get("firmware", "") or "")
    bucket[f"{prefix}_cell_id"] = int(context.get("cell_id", 0) or 0)
    bucket[f"{prefix}_pci"] = int(context.get("pci", 0) or 0)
    bucket[f"{prefix}_earfcn"] = int(context.get("earfcn", 0) or 0)
    bucket[f"{prefix}_hour"] = _int_from_any(context.get("hour"), -1)
    bucket[f"{prefix}_context_key"] = str(context.get("context_key", "") or "")
    remember_bucket_context(bucket, context=context, outcome=outcome)


def remember_bucket_context(
    bucket: dict[str, Any],
    *,
    context: dict[str, Any],
    outcome: str,
) -> None:
    key = str(context.get("context_key", "") or "")
    if not key:
        return
    contexts = bucket.get("contexts")
    if not isinstance(contexts, dict):
        contexts = {}
    row = dict(contexts.get(key) or {})
    row["provider_plmn"] = str(context.get("provider_plmn", "") or "")
    row["provider_name"] = str(context.get("provider_name", "") or "")
    row["identity_code"] = str(context.get("identity_code", "") or "")
    row["identity_status"] = str(context.get("identity_status", "") or "")
    row["model"] = str(context.get("model", "") or "")
    row["profile_id"] = str(context.get("profile_id", "") or "")
    row["firmware"] = str(context.get("firmware", "") or "")
    row["cell_id"] = int(context.get("cell_id", 0) or 0)
    row["pci"] = int(context.get("pci", 0) or 0)
    row["earfcn"] = int(context.get("earfcn", 0) or 0)
    row["hour"] = _int_from_any(context.get("hour"), -1)
    row["attempts"] = int(row.get("attempts", 0) or 0) + 1
    if outcome == "bad":
        row["bad"] = int(row.get("bad", 0) or 0) + 1
    elif outcome == "good":
        row["good"] = int(row.get("good", 0) or 0) + 1
    contexts[key] = row
    bucket["contexts"] = contexts


def _int_from_any(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "OWNER",
    "SCHEMA",
    "action_record_context",
    "remember_bucket_context",
    "remember_outcome_metadata",
]
