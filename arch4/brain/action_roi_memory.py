"""Brain action-ROI projection over canonical terminal rewards."""

from __future__ import annotations

from typing import Any

from arch4.brain.action_shape_owner import action_shape_from_record, ca_pool_delta_key
from arch4.brain.action_roi_context_owner import remember_outcome_metadata
from arch4.brain.memory_io import dict_to_record
from arch4.brain.reward_ledger import (
    projection_is_completed,
    reward_projection_entries,
)
from arch4.evidence_scope import EvidenceScope
from arch4.interfaces import LeverType
from arch4.terminal_reward_contract import terminal_reward_entry_error


_ROI_ALLOWED_COMMIT_STATUSES = frozenset({"recorded", "committed", "classified", "proof_closed"})
_ROI_BLOCKED_COMMIT_STATUSES = frozenset({"blocked", "skipped", "pending"})


def _action_record_learning_allowed(rec: Any) -> bool:
    """ROI memory may only learn from records admitted by the learning gate."""

    gate = getattr(rec, "learning_gate", None)
    if not isinstance(gate, dict):
        return False
    if not bool(gate.get("learning_allowed", False)):
        return False
    status = str(getattr(rec, "learning_commit_status", "") or "").strip().lower()
    if status in _ROI_BLOCKED_COMMIT_STATUSES:
        return False
    if status and status not in _ROI_ALLOWED_COMMIT_STATUSES:
        return False
    return True


def _int_from_any(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_from_any(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pband_action_roi_from_rewards(
    memory: Any,
    *,
    provider_plmn: str = "",
    provider_name: str = "",
    max_records: int = 80,
) -> dict:
    history = _terminal_reward_records(memory, max_records=max_records)
    if not history:
        return {}
    provider_plmn = str(provider_plmn or "").strip()
    provider_name = str(provider_name or "").strip().lower()
    by_pband: dict[str, dict] = {}
    by_ca_pool: dict[str, dict] = {}
    by_ca_pool_anchor: dict[str, dict] = {}
    by_ca_pool_delta: dict[str, dict] = {}
    scanned = 0
    for rec in history:
        if scanned >= max(1, int(max_records)):
            break
        scanned += 1
        if not _action_record_learning_allowed(rec):
            continue
        lever = getattr(rec, "lever", None)
        if lever not in {LeverType.PBAND, LeverType.BAND_SELECTION}:
            continue
        if str(getattr(rec, "proposal_action_origin", "") or "") in {
            "operator_ngui",
            "operator_sms",
        }:
            continue
        shape = action_shape_from_record(rec)
        if not bool(shape.get("valid", False)):
            continue
        pband = _int_from_any(shape.get("pband"), 0)
        pool = list(shape.get("ca_pool") or [])
        key = str(shape.get("pband_action_key") or "")
        ca_pool_key = str(shape.get("ca_pool_key") or "")
        ca_pool_anchor = _int_from_any(shape.get("anchor_band"), 0)
        if (lever == LeverType.PBAND and (pband <= 0 or not key)) or (
            lever == LeverType.BAND_SELECTION and not ca_pool_key
        ):
            continue

        rec_plmn = str(getattr(rec, "provider_plmn", "") or "").strip()
        rec_provider = str(getattr(rec, "provider_name", "") or "").strip().lower()
        if provider_plmn and rec_plmn and rec_plmn != provider_plmn:
            continue
        if provider_name and rec_provider and rec_provider != provider_name:
            continue

        grade = _int_from_any(
            dict(getattr(rec, "objective_metadata", {}) or {}).get(
                "terminal_reward_grade"
            ),
            0,
        )
        if grade == 0:
            continue
        label = "good" if grade > 0 else "bad"
        level = label
        code = str(
            getattr(rec, "referee_outcome_code", "") or ""
        ).strip().lower()
        objective_metadata = dict(getattr(rec, "objective_metadata", {}) or {})
        if str(objective_metadata.get("staged_roi_parent_status", "") or "").strip().lower() == "proof_closed":
            continue
        staged_evidence = (
            objective_metadata.get("staged_roi_evidence")
            if isinstance(objective_metadata.get("staged_roi_evidence"), dict)
            else {}
        )
        verdict_stage = str(getattr(rec, "outcome_verdict_stage", "") or "").strip().lower()
        staged_roi = verdict_stage == "staged_roi" or code.startswith("staged_roi_")
        if staged_roi:
            context_status = str(staged_evidence.get("grade_context_status", "") or "").strip().lower()
            evidence_valid = staged_evidence.get("grade_evidence_valid")
            if (
                label not in {"good", "success", "bad", "fail"}
                or code
                in {
                    "staged_roi_context_changed",
                    "staged_roi_invalid_evidence",
                    "staged_roi_inconclusive",
                    "staged_roi_neutral",
                }
                or (context_status and context_status != "same")
                or evidence_valid is False
            ):
                continue
        bad = label == "bad"
        good = label == "good"
        if lever == LeverType.BAND_SELECTION and ca_pool_key:
            bucket = by_ca_pool.setdefault(ca_pool_key, {"attempts": 0, "bad": 0, "good": 0})
            anchor_bucket = (
                by_ca_pool_anchor.setdefault(str(ca_pool_anchor), {"attempts": 0, "bad": 0, "good": 0})
                if ca_pool_anchor > 0
                else None
            )
            delta_buckets: list[tuple[dict, str, int]] = []
            added_for_delta = [int(b) for b in list(shape.get("added_bands") or [])]
            removed_for_delta = [int(b) for b in list(shape.get("removed_bands") or [])]
            for added_band in sorted(set(added_for_delta)):
                delta_key = ca_pool_delta_key(anchor=ca_pool_anchor, direction="add", band=added_band)
                if delta_key:
                    delta_buckets.append(
                        (
                            by_ca_pool_delta.setdefault(delta_key, {"attempts": 0, "bad": 0, "good": 0}),
                            "add",
                            int(added_band),
                        )
                    )
            for removed_band in sorted(set(removed_for_delta)):
                delta_key = ca_pool_delta_key(anchor=ca_pool_anchor, direction="remove", band=removed_band)
                if delta_key:
                    delta_buckets.append(
                        (
                            by_ca_pool_delta.setdefault(delta_key, {"attempts": 0, "bad": 0, "good": 0}),
                            "remove",
                            int(removed_band),
                        )
                    )
        else:
            bucket = by_pband.setdefault(str(pband), {"attempts": 0, "bad": 0, "good": 0, "keys": {}})
            anchor_bucket = None
            delta_buckets = []
        weight = abs(int(grade))
        bucket["attempts"] += weight
        bucket["reward_count"] = int(bucket.get("reward_count", 0) or 0) + 1
        bucket["grade_total"] = int(bucket.get("grade_total", 0) or 0) + int(grade)
        if anchor_bucket is not None:
            anchor_bucket["attempts"] += weight
            anchor_bucket["reward_count"] = int(
                anchor_bucket.get("reward_count", 0) or 0
            ) + 1
            anchor_bucket["grade_total"] = int(
                anchor_bucket.get("grade_total", 0) or 0
            ) + int(grade)
        for delta_bucket, _direction, _band in delta_buckets:
            delta_bucket["attempts"] += weight
            delta_bucket["reward_count"] = int(
                delta_bucket.get("reward_count", 0) or 0
            ) + 1
            delta_bucket["grade_total"] = int(
                delta_bucket.get("grade_total", 0) or 0
            ) + int(grade)
        if bad:
            bucket["bad"] += weight
            remember_outcome_metadata(
                bucket,
                rec=rec,
                outcome="bad",
                pband=pband,
                ca_pool=list(pool or []),
                level=level,
                code=code,
                label=label,
            )
            if anchor_bucket is not None:
                anchor_bucket["bad"] += weight
                remember_outcome_metadata(
                    anchor_bucket,
                    rec=rec,
                    outcome="bad",
                    pband=pband,
                    ca_pool=list(pool or []),
                    level=level,
                    code=code,
                    label=label,
                )
            for delta_bucket, direction, band in delta_buckets:
                delta_bucket["bad"] += weight
                delta_bucket["last_bad_direction"] = direction
                delta_bucket["last_bad_band"] = int(band)
                delta_bucket["last_bad_anchor"] = int(ca_pool_anchor)
                remember_outcome_metadata(
                    delta_bucket,
                    rec=rec,
                    outcome="bad",
                    pband=pband,
                    ca_pool=list(pool or []),
                    level=level,
                    code=code,
                    label=label,
                )
            if lever == LeverType.BAND_SELECTION and ca_pool_key:
                previous_pool = [int(b) for b in list(shape.get("previous_pool") or [])]
                added_bands = [int(b) for b in list(shape.get("added_bands") or [])]
                removed_bands = [int(b) for b in list(shape.get("removed_bands") or [])]
                current_pool = [int(b) for b in list(shape.get("target_pool") or pool or [])]
                if previous_pool:
                    bucket["last_bad_previous_pool"] = previous_pool
                    bucket["last_bad_current_pool"] = current_pool
                    bucket["last_bad_added_bands"] = added_bands
                    bucket["last_bad_removed_bands"] = sorted(set(removed_bands))
                    if anchor_bucket is not None:
                        anchor_bucket["last_bad_previous_pool"] = previous_pool
                        anchor_bucket["last_bad_current_pool"] = current_pool
                        anchor_bucket["last_bad_added_bands"] = added_bands
                        anchor_bucket["last_bad_removed_bands"] = sorted(set(removed_bands))
            elif lever == LeverType.PBAND:
                previous_pband = _int_from_any(shape.get("previous_pband"), 0)
                if previous_pband > 0 and previous_pband != int(pband or 0):
                    bucket["last_bad_previous_pband"] = int(previous_pband)
                    bucket["last_bad_current_pband"] = int(pband or 0)
            if "keys" in bucket:
                bucket["keys"][key] = int(bucket["keys"].get(key, 0) or 0) + 1
        elif good:
            bucket["good"] += weight
            remember_outcome_metadata(
                bucket,
                rec=rec,
                outcome="good",
                pband=pband,
                ca_pool=list(pool or []),
                level=level,
                code=code,
                label=label,
            )
            if anchor_bucket is not None:
                anchor_bucket["good"] += weight
                remember_outcome_metadata(
                    anchor_bucket,
                    rec=rec,
                    outcome="good",
                    pband=pband,
                    ca_pool=list(pool or []),
                    level=level,
                    code=code,
                    label=label,
                )
            for delta_bucket, direction, band in delta_buckets:
                delta_bucket["good"] += weight
                delta_bucket["last_good_direction"] = direction
                delta_bucket["last_good_band"] = int(band)
                delta_bucket["last_good_anchor"] = int(ca_pool_anchor)
                remember_outcome_metadata(
                    delta_bucket,
                    rec=rec,
                    outcome="good",
                    pband=pband,
                    ca_pool=list(pool or []),
                    level=level,
                    code=code,
                    label=label,
                )

    for pband, bucket in list(by_pband.items()):
        attempts = int(bucket.get("attempts", 0) or 0)
        bad = int(bucket.get("bad", 0) or 0)
        good = int(bucket.get("good", 0) or 0)
        if attempts <= 0 or (bad <= 0 and good <= 0):
            by_pband.pop(pband, None)
            continue
        bucket["keys"] = dict(bucket.get("keys") or {})
        if bad > 0:
            bad_rate = bad / max(1, attempts)
            penalty = 0.35
            if attempts >= 2 and bad_rate >= 0.50:
                penalty = 0.55
            if good > bad:
                penalty = max(0.0, penalty - 0.25)
            if penalty > 0.0:
                bucket["penalty"] = round(penalty, 4)
                bucket["roi_direction"] = "negative"
                bucket["reason"] = (
                    f"Brain learned recent PBAND B{pband}+CA outcome ROI is negative "
                    f"({bad}/{attempts} bad)"
                )
                continue
        if good > bad:
            good_rate = good / max(1, attempts)
            benefit = 0.15
            if attempts >= 2 and good_rate >= 0.67:
                benefit = 0.25
            bucket["benefit"] = round(benefit, 4)
            bucket["roi_direction"] = "positive"
            bucket["reason"] = (
                f"Brain learned recent PBAND B{pband}+CA outcome ROI is positive "
                f"({good}/{attempts} good)"
            )
        if _float_from_any(bucket.get("penalty"), 0.0) <= 0.0 and _float_from_any(bucket.get("benefit"), 0.0) <= 0.0:
            by_pband.pop(pband, None)

    for key, bucket in list(by_ca_pool.items()):
        attempts = int(bucket.get("attempts", 0) or 0)
        bad = int(bucket.get("bad", 0) or 0)
        good = int(bucket.get("good", 0) or 0)
        if attempts <= 0 or (bad <= 0 and good <= 0):
            by_ca_pool.pop(key, None)
            continue
        if bad > 0:
            bad_rate = bad / max(1, attempts)
            penalty = 0.25
            if attempts >= 2 and bad_rate >= 0.50:
                penalty = 0.45
            if good > bad:
                penalty = max(0.0, penalty - 0.20)
            if penalty > 0.0:
                bucket["penalty"] = round(penalty, 4)
                bucket["roi_direction"] = "negative"
                bucket["reason"] = f"Brain learned recent CA pool expansion ROI is negative ({bad}/{attempts} bad)"
                continue
        if good > bad:
            good_rate = good / max(1, attempts)
            benefit = 0.10
            if attempts >= 2 and good_rate >= 0.67:
                benefit = 0.20
            bucket["benefit"] = round(benefit, 4)
            bucket["roi_direction"] = "positive"
            bucket["reason"] = f"Brain learned recent CA pool expansion ROI is positive ({good}/{attempts} good)"
        if _float_from_any(bucket.get("penalty"), 0.0) <= 0.0 and _float_from_any(bucket.get("benefit"), 0.0) <= 0.0:
            by_ca_pool.pop(key, None)

    for anchor, bucket in list(by_ca_pool_anchor.items()):
        attempts = int(bucket.get("attempts", 0) or 0)
        bad = int(bucket.get("bad", 0) or 0)
        good = int(bucket.get("good", 0) or 0)
        if attempts <= 0 or (bad <= 0 and good <= 0):
            by_ca_pool_anchor.pop(anchor, None)
            continue
        if bad > 0:
            bad_rate = bad / max(1, attempts)
            penalty = 0.25
            if attempts >= 2 and bad_rate >= 0.50:
                penalty = 0.45
            if good > bad:
                penalty = max(0.0, penalty - 0.20)
            if penalty > 0.0:
                bucket["penalty"] = round(penalty, 4)
                bucket["roi_direction"] = "negative"
                bucket["reason"] = (
                    f"Brain learned recent CA pool expansion on B{anchor} anchor ROI is negative "
                    f"({bad}/{attempts} bad)"
                )
                continue
        if good > bad:
            good_rate = good / max(1, attempts)
            benefit = 0.10
            if attempts >= 2 and good_rate >= 0.67:
                benefit = 0.20
            bucket["benefit"] = round(benefit, 4)
            bucket["roi_direction"] = "positive"
            bucket["reason"] = (
                f"Brain learned recent CA pool expansion on B{anchor} anchor ROI is positive "
                f"({good}/{attempts} good)"
            )
        if _float_from_any(bucket.get("penalty"), 0.0) <= 0.0 and _float_from_any(bucket.get("benefit"), 0.0) <= 0.0:
            by_ca_pool_anchor.pop(anchor, None)

    for key, bucket in list(by_ca_pool_delta.items()):
        attempts = int(bucket.get("attempts", 0) or 0)
        bad = int(bucket.get("bad", 0) or 0)
        good = int(bucket.get("good", 0) or 0)
        if attempts <= 0 or (bad <= 0 and good <= 0):
            by_ca_pool_delta.pop(key, None)
            continue
        direction = str(bucket.get("last_bad_direction", bucket.get("last_good_direction", "")) or "").strip().lower()
        band = int(bucket.get("last_bad_band", bucket.get("last_good_band", 0)) or 0)
        anchor = int(bucket.get("last_bad_anchor", bucket.get("last_good_anchor", 0)) or 0)
        direction_text = "adding" if direction == "add" else "removing" if direction == "remove" else "changing"
        if bad > 0:
            bad_rate = bad / max(1, attempts)
            penalty = 0.35
            if attempts >= 2 and bad_rate >= 0.50:
                penalty = 0.60
            if good > bad:
                penalty = max(0.0, penalty - 0.25)
            if penalty > 0.0:
                bucket["penalty"] = round(penalty, 4)
                bucket["roi_direction"] = "negative"
                bucket["reason"] = (
                    f"Brain learned recent CA pool delta ROI is negative for {direction_text} "
                    f"B{band} on B{anchor} anchor ({bad}/{attempts} bad)"
                )
                continue
        if good > bad:
            good_rate = good / max(1, attempts)
            benefit = 0.10
            if attempts >= 2 and good_rate >= 0.67:
                benefit = 0.20
            bucket["benefit"] = round(benefit, 4)
            bucket["roi_direction"] = "positive"
            bucket["reason"] = (
                f"Brain learned recent CA pool delta ROI is positive for {direction_text} "
                f"B{band} on B{anchor} anchor ({good}/{attempts} good)"
            )
        if _float_from_any(bucket.get("penalty"), 0.0) <= 0.0 and _float_from_any(bucket.get("benefit"), 0.0) <= 0.0:
            by_ca_pool_delta.pop(key, None)

    out: dict[str, dict] = {}
    if by_pband:
        out["by_pband"] = by_pband
    if by_ca_pool:
        out["by_ca_pool"] = by_ca_pool
    if by_ca_pool_anchor:
        out["by_ca_pool_anchor"] = by_ca_pool_anchor
    if by_ca_pool_delta:
        out["by_ca_pool_delta"] = by_ca_pool_delta
    return out


def _terminal_reward_records(memory: Any, *, max_records: int) -> list[Any]:
    if memory is None:
        return []
    try:
        current = EvidenceScope.from_mapping(
            dict(getattr(memory, "_current_action_evidence_scope", {}) or {})
        )
    except (TypeError, ValueError):
        return []
    rows = sorted(
        reward_projection_entries(memory),
        key=lambda item: float(
            dict(item.get("ledger_entry") or {}).get("admitted_ts", 0.0)
            or 0.0
        ),
        reverse=True,
    )
    records: list[Any] = []
    for item in rows:
        if len(records) >= max(1, int(max_records)):
            break
        entry = dict(item.get("ledger_entry") or {})
        ledger_key = str(item.get("ledger_key") or "")
        if terminal_reward_entry_error(entry):
            continue
        if not projection_is_completed(memory, ledger_key, "memory"):
            continue
        try:
            scope = EvidenceScope.from_mapping(
                dict(entry.get("evidence_scope") or {})
            )
        except (TypeError, ValueError):
            continue
        if not scope.same_physical_namespace(current):
            continue
        payload = (
            dict(entry.get("projection_payload") or {})
            if isinstance(entry.get("projection_payload"), dict)
            else {}
        )
        record_raw = payload.get("record")
        if not isinstance(record_raw, dict):
            continue
        try:
            record = dict_to_record(record_raw)
        except (TypeError, ValueError):
            continue
        metadata = dict(getattr(record, "objective_metadata", {}) or {})
        metadata["terminal_reward_grade"] = int(entry.get("grade", 0) or 0)
        metadata["terminal_reward_ledger_key"] = ledger_key
        metadata["terminal_reward_evidence_scope"] = scope.as_dict()
        record.objective_metadata = metadata
        records.append(record)
    return records


__all__ = ["pband_action_roi_from_rewards"]
