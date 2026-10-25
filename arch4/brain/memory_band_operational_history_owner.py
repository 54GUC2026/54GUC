"""Operational band-history mutation owner for Memory-backed stores.

Memory owns durable containers and journal primitives. This module owns the
mutation rules for provider/context band suppression, raw band-attempt replay,
and anchor-stability history.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from arch4.brain.memory_namespaces import set_provider_context as memory_set_provider_context
from arch4.interfaces import BandKnowledge

OWNER = "arch4.brain.memory_band_operational_history_owner"

log = logging.getLogger("arch4.brain.memory_band_operational_history_owner")


def band_suppression_context_key(memory: Any) -> str:
    marker = str(getattr(memory, "current_calibration_marker", "") or "").strip()
    return marker or "C0"


def record_band_attempt(
    memory: Any,
    *,
    band: int,
    appeared_in_ca: bool,
    sinr: float = 0.0,
    ca_sig: str = "",
    throughput: float = 0.0,
    outcome: str = "",
    is_primary: bool = False,
    weak_unusable: bool = False,
) -> None:
    ts = float(time.time())
    record = {
        "ts": ts,
        "provider_plmn": str(getattr(memory, "_current_provider_plmn", "") or ""),
        "band": int(band),
        "appeared_in_ca": bool(appeared_in_ca),
        "sinr": float(sinr or 0.0),
        "ca_sig": str(ca_sig or ""),
        "throughput": float(throughput or 0.0),
        "outcome": str(outcome or ""),
        "is_primary": bool(is_primary),
        "weak_unusable": bool(weak_unusable),
    }
    apply_band_attempt_record(memory, record, emit_logs=True)
    _append_operational_journal(memory, "band_attempt", record)


def apply_band_attempt_record(memory: Any, record: Dict[str, Any], *, emit_logs: bool) -> None:
    _bind_provider(memory, str(record.get("provider_plmn", "") or getattr(memory, "_current_provider_plmn", "") or ""))
    band = int(record.get("band", 0) or 0)
    if band <= 0:
        return
    ts = float(record.get("ts", time.time()) or time.time())
    appeared_in_ca = bool(record.get("appeared_in_ca", False))
    sinr = float(record.get("sinr", 0.0) or 0.0)
    ca_sig = str(record.get("ca_sig", "") or "")
    throughput = float(record.get("throughput", 0.0) or 0.0)
    outcome = str(record.get("outcome", "") or "")
    is_primary = bool(record.get("is_primary", False))
    weak_unusable = bool(record.get("weak_unusable", False))

    bands = getattr(memory, "bands", {})
    if band not in bands:
        bands[band] = BandKnowledge(band=band)

    bk = bands[band]
    bk.times_tried += 1
    bk.last_tried = ts
    bk.last_sinr = sinr
    if outcome:
        bk.last_outcome = outcome

    if appeared_in_ca:
        bk.times_appeared_in_ca += 1
        bk.last_ca_signature_with = ca_sig
        if is_primary:
            bk.times_primary_attach += 1
        if bk.avg_throughput_with > 0:
            bk.avg_throughput_with = 0.8 * bk.avg_throughput_with + 0.2 * throughput
        else:
            bk.avg_throughput_with = throughput
        bk.never_works = False
    else:
        if outcome == "requested_absent_fallback_attach":
            bk.times_fallback_attach += 1
        learning_cfg = getattr(memory, "learning_cfg", None)
        never_works_threshold = int(getattr(learning_cfg, "band_never_works_threshold", 5) or 5)
        if bk.times_tried >= never_works_threshold and bk.times_appeared_in_ca == 0:
            bk.never_works = True
            if emit_logs:
                log.info("Band B%d marked as never_works after %d attempts", band, bk.times_tried)
    if weak_unusable:
        bk.times_unusable_attach += 1


def is_band_blacklisted(
    memory: Any,
    band: int,
    *,
    context_key: str | None = None,
    now: float | None = None,
) -> bool:
    store = getattr(memory, "_band_learning_store", None)
    if store is None:
        raise RuntimeError("memory_missing_band_learning_store")
    return store.is_blacklisted(
        int(band or 0),
        context_key=context_key or band_suppression_context_key(memory),
        now=now,
    )


def blacklist_band(
    memory: Any,
    band: int,
    *,
    ttl_s: float | None = None,
    context_key: str = "*",
    reason: str = "manual_band_blacklist",
    now: float | None = None,
) -> None:
    ts = float(time.time() if now is None else now)
    ttl = 0.0 if ttl_s is None else max(0.0, float(ttl_s))
    record = {
        "ts": ts,
        "created_ts": ts,
        "expires_ts": 0.0 if ttl <= 0.0 else ts + ttl,
        "provider_plmn": str(getattr(memory, "_current_provider_plmn", "") or ""),
        "band": int(band),
        "context_key": str(context_key or "*").strip() or "*",
        "reason": str(reason or "band_suppression"),
    }
    if apply_blacklist_band_record(memory, record, emit_logs=True):
        _append_operational_journal(memory, "blacklist_band", record)


def check_and_blacklist(memory: Any, band: int, *, threshold: int) -> None:
    bands = getattr(memory, "bands", {})
    bk = bands.get(int(band or 0))
    if bk is None:
        return
    if (
        bk.times_tried >= int(threshold)
        and bk.times_appeared_in_ca == 0
        and not is_band_blacklisted(memory, band)
    ):
        learning_cfg = getattr(memory, "learning_cfg", None)
        blacklist_band(
            memory,
            band,
            ttl_s=float(getattr(learning_cfg, "band_retry_interval_s", 3600.0) or 3600.0),
            context_key=band_suppression_context_key(memory),
            reason="auto_failed_absent_retry_later",
        )


def band_has_ca_throughput_evidence(
    memory: Any,
    band: int,
    *,
    min_avg_throughput_mbps: float = 0.5,
) -> bool:
    """True when learned band history has enough CA evidence for rotation."""
    bands = getattr(memory, "bands", {})
    bk = bands.get(int(band or 0))
    if bk is None:
        return False
    return (
        int(getattr(bk, "times_appeared_in_ca", 0) or 0) > 0
        and float(getattr(bk, "avg_throughput_with", 0.0) or 0.0)
        >= float(min_avg_throughput_mbps)
    )


def band_ca_throughput_evidence(memory: Any) -> dict[int, dict[str, float | int]]:
    """Return the immutable per-band evidence used by family rotation policy."""

    return {
        int(band): {
            "times_appeared_in_ca": int(
                getattr(value, "times_appeared_in_ca", 0) or 0
            ),
            "avg_throughput_with": float(
                getattr(value, "avg_throughput_with", 0.0) or 0.0
            ),
        }
        for band, value in sorted(getattr(memory, "bands", {}).items())
        if int(band or 0) > 0
    }


def record_anchor_stability(
    memory: Any,
    band: int,
    stuck: bool,
    *,
    projection_key: str = "",
) -> bool:
    key = str(projection_key or "").strip()
    completed = getattr(memory, "_anchor_stability_projection_keys", set())
    if key and key in completed:
        return False
    record = {
        "ts": float(time.time()),
        "provider_plmn": str(getattr(memory, "_current_provider_plmn", "") or ""),
        "band": int(band),
        "stuck": bool(stuck),
        "projection_key": key,
    }
    applied = apply_anchor_stability_record(memory, record, emit_logs=True)
    if not applied:
        return False
    _append_operational_journal(memory, "anchor_stability", record)
    return True


def anchor_stability_projection_completed(memory: Any, projection_key: str) -> bool:
    """Return exact durable completion truth for one anchor observation."""

    key = str(projection_key or "").strip()
    if not key:
        raise ValueError("anchor_stability_projection_key_required")
    return key in getattr(memory, "_anchor_stability_projection_keys", set())


def apply_blacklist_band_record(memory: Any, record: Dict[str, Any], *, emit_logs: bool) -> bool:
    provider = str(record.get("provider_plmn", "") or getattr(memory, "_current_provider_plmn", "") or "")
    _bind_provider(memory, provider)
    band = int(record.get("band", 0) or 0)
    if band <= 0:
        return False
    created_ts = float(record.get("created_ts", record.get("ts", 0.0)) or 0.0)
    expires_ts = float(record.get("expires_ts", 0.0) or 0.0)
    context_key = str(record.get("context_key", record.get("context", "*")) or "*").strip() or "*"
    reason = str(record.get("reason", "band_suppression") or "band_suppression")
    store = getattr(memory, "_band_learning_store", None)
    if store is None:
        raise RuntimeError("memory_missing_band_learning_store")
    if not store.add_blacklist_record(
        band=band,
        context_key=context_key,
        created_ts=created_ts,
        expires_ts=expires_ts,
        reason=reason,
        provider_plmn=provider,
        now=created_ts if created_ts > 0.0 else time.time(),
    ):
        return False
    if emit_logs:
        if expires_ts > 0.0:
            log.info("Band B%d suppressed for context %s until %.0f", band, context_key, expires_ts)
        else:
            log.info("Band B%d added to provider blacklist context=%s", band, context_key)
    return True


def apply_anchor_stability_record(memory: Any, record: Dict[str, Any], *, emit_logs: bool) -> bool:
    _bind_provider(memory, str(record.get("provider_plmn", "") or getattr(memory, "_current_provider_plmn", "") or ""))
    key = str(record.get("projection_key") or "").strip()
    completed = getattr(memory, "_anchor_stability_projection_keys", None)
    if completed is None:
        completed = set()
        memory._anchor_stability_projection_keys = completed
    if key and key in completed:
        return False
    band = int(record.get("band", 0) or 0)
    if band <= 0:
        return False
    stuck = bool(record.get("stuck", False))
    stats_by_band = getattr(memory, "band_anchor_stability", {})
    if band not in stats_by_band:
        stats_by_band[band] = {"attempts": 0, "stuck": 0}
    stats = stats_by_band[band]
    stats["attempts"] += 1
    if stuck:
        stats["stuck"] += 1
    if key:
        completed.add(key)
        if len(completed) > 1024:
            retained = sorted(completed)[-1024:]
            completed.clear()
            completed.update(retained)
    if emit_logs:
        stuck_rate = stats["stuck"] / stats["attempts"]
        log.info(
            "Anchor stability B%d: %d/%d stuck (%.0f%% hold rate)",
            band,
            stats["stuck"],
            stats["attempts"],
            stuck_rate * 100,
        )
    return True


def _bind_provider(memory: Any, provider: str) -> None:
    if provider != getattr(memory, "_current_provider_plmn", ""):
        memory_set_provider_context(memory, provider)


def _append_operational_journal(memory: Any, kind: str, record: Dict[str, Any]) -> None:
    memory._append_journal_event({"kind": kind, "ts": float(record["ts"]), "record": record})
    memory._mark_dirty(events=1)


__all__ = [
    "OWNER",
    "apply_anchor_stability_record",
    "apply_band_attempt_record",
    "apply_blacklist_band_record",
    "anchor_stability_projection_completed",
    "band_has_ca_throughput_evidence",
    "band_suppression_context_key",
    "blacklist_band",
    "check_and_blacklist",
    "is_band_blacklisted",
    "record_anchor_stability",
    "record_band_attempt",
]
