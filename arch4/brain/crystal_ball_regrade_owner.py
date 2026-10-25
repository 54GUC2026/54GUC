"""Brain owner for Crystal Ball action regrade and staged-ROI projection."""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping

from arch4.brain.proof_referee_context_owner import int_or_zero
from arch4.brain.memory_action_history_owner import action_roi_formula_vector
from arch4.evidence_scope import validated_evidence_scope
from arch4.terminal_reward_contract import terminal_reward_entry_error
from arch4.terminal_reward_observation import TerminalRewardObservation


OWNER = "arch4.brain.crystal_ball_regrade_owner"

log = logging.getLogger("arch4.brain.crystal_ball_regrade_owner")


def log_action_regrade(
    logger: Any,
    *,
    hour: int,
    day_type: str,
    band: int,
    cell_id: int = 0,
    drift_reason: str = "",
    confidence_penalty: int = 1,
    evidence_scope: Mapping[str, Any] | None = None,
) -> None:
    """Write a forecast regrade through the telemetry evidence owner."""
    logger.write_regrade_record(
        hour=hour,
        day_type=day_type,
        band=band,
        cell_id=cell_id,
        drift_reason=drift_reason,
        confidence_penalty=confidence_penalty,
        evidence_scope=dict(evidence_scope or {}),
    )


def commit_staged_roi_crystal_ball_projection(
    *,
    result: dict[str, Any],
    logger: Any,
    crystal_ball: Any,
    terminal_observation: TerminalRewardObservation,
    proof_identity: Mapping[str, Any],
    original: Any,
    record: Any,
    evidence: dict[str, Any],
    label: str,
    reason: str,
    grade_score: int,
    ts: float,
    reward_entry: Mapping[str, Any],
    reward_ledger_key: str = "",
) -> None:
    """Project a staged-ROI grade into Crystal Ball forecast memory."""
    if crystal_ball is None:
        return
    projection_key = str(reward_ledger_key or "")
    reward_row = dict(reward_entry or {})
    reward_error = terminal_reward_entry_error(reward_row)
    if reward_error:
        result["crystal_ball_error"] = (
            f"terminal_reward_invalid:{reward_error}"
        )
        return
    if projection_key != str(reward_row.get("ledger_key") or ""):
        result["crystal_ball_error"] = (
            "terminal_reward_projection_key_mismatch"
        )
        return
    projection_keys = getattr(crystal_ball, "_delayed_reward_projection_keys", None)
    if not isinstance(projection_keys, set):
        projection_keys = set()
        crystal_ball._delayed_reward_projection_keys = projection_keys
    if projection_key and projection_key in projection_keys:
        result["crystal_ball_updated"] = True
        result["crystal_ball_projection_duplicate"] = True
        return
    try:
        from arch4.brain.crystal_ball import _day_type_from_ts

        day_type = _day_type_from_ts(float(ts))
        hour = time.localtime(float(ts)).tm_hour
        if not isinstance(
            terminal_observation, TerminalRewardObservation
        ):
            result["crystal_ball_error"] = (
                "terminal_reward_observation_required"
            )
            return
        cellular = terminal_observation.cellular
        traffic = terminal_observation.traffic
        latency = terminal_observation.latency
        quality = terminal_observation.quality
        band = int_or_zero(cellular.primary_band)
        cell_id = int_or_zero(cellular.cell_id)
        identity = dict(proof_identity or {})
        result["crystal_ball_projection_identity"] = dict(identity)
        if not bool(identity.get("exact_identity_complete", False)):
            result["crystal_ball_error"] = "terminal_proof_identity_incomplete"
            return
        if band <= 0:
            result["crystal_ball_error"] = "post_action_band_missing"
            return
        try:
            source_scope = validated_evidence_scope(
                dict(getattr(record, "evidence_scope", {}) or {})
            )
            reward_scope = validated_evidence_scope(
                dict(reward_row.get("evidence_scope") or {})
            )
        except (TypeError, ValueError) as exc:
            result["crystal_ball_error"] = f"outcome_evidence_scope_invalid:{exc}"
            return
        if (
            not reward_scope.same_physical_namespace(source_scope)
            or reward_scope.source_sequence != source_scope.source_sequence
        ):
            result["crystal_ball_error"] = (
                "terminal_reward_evidence_scope_mismatch"
            )
            return
        if source_scope.formula_vector != action_roi_formula_vector():
            result["crystal_ball_error"] = "outcome_evidence_formula_mismatch"
            return
        update = getattr(crystal_ball, "update_from_live_hour", None)
        if not callable(update):
            result["crystal_ball_error"] = "post_action_observation_unavailable"
            return
        update(
            hour,
            day_type,
            {
                (band, cell_id): {
                    "congestion": float(quality.congestion_pct or 0.0),
                    "dl_mbps": float(traffic.dl_mbps or 0.0),
                    "ul_mbps": float(traffic.ul_mbps or 0.0),
                    "rtt_ms": float(
                        latency.rtt_ms
                        or latency.rtt_sliding_avg
                        or 0.0
                    ),
                    "sinr_db": float(cellular.sinr_db or 0.0),
                    "rsrq_db": float(cellular.rsrq_db or 0.0),
                    "ca_rate": 1.0 if cellular.ca_active else 0.0,
                    "count": 1,
                }
            },
            evidence_scope=source_scope.as_dict(),
        )
        try:
            crystal_scope = validated_evidence_scope(
                dict(getattr(crystal_ball, "_evidence_scope", {}) or {})
            )
            from arch4.brain.crystal_ball import crystal_ball_formula_vector

            if crystal_scope.formula_vector != crystal_ball_formula_vector():
                raise ValueError("crystal_ball_formula_vector_mismatch")
            if not source_scope.same_physical_namespace(crystal_scope):
                raise ValueError("crystal_ball_physical_namespace_mismatch")
            if source_scope.source_sequence != crystal_scope.source_sequence:
                raise ValueError("crystal_ball_generation_mismatch")
            if source_scope.provider_plmn != crystal_scope.provider_plmn:
                raise ValueError("crystal_ball_provider_scope_mismatch")
        except (TypeError, ValueError) as exc:
            result["crystal_ball_error"] = f"crystal_ball_projection_scope_invalid:{exc}"
            return
        crystal_ball._last_delayed_reward_projection = {
            "schema": "crystal_ball_delayed_reward_projection.v1",
            "owner": OWNER,
            "proof_identity": dict(identity),
            "terminal_grade_score": max(-3, min(3, int(grade_score or 0))),
            "label": str(label or "neutral"),
            "reason": str(reason or ""),
            "ts": float(ts),
            "source_evidence_scope": source_scope.as_dict(),
            "evidence_scope": crystal_scope.as_dict(),
        }
        result["crystal_ball_updated"] = True
        if int(grade_score or 0) < 0:
            drift_reason = (
                f"staged_roi_negative:{reason}:"
                f"proof={identity.get('selected_action_proof_id') or identity.get('parent_trace_id')}:"
                f"context={identity.get('smart_loop_context_key') or identity.get('grade_context_status')}"
            )
            if logger is not None and hasattr(logger, "write_regrade_record") and band > 0:
                log_action_regrade(
                    logger,
                    hour=hour,
                    day_type=day_type,
                    band=band,
                    cell_id=cell_id,
                    drift_reason=drift_reason,
                    confidence_penalty=1,
                    evidence_scope=crystal_scope.as_dict(),
                )
            regrade = getattr(crystal_ball, "apply_live_regrade", None)
            if callable(regrade) and band > 0:
                result["crystal_ball_regraded"] = bool(
                    regrade(
                        hour=hour,
                        day_type=day_type,
                        band=band,
                        cell_id=cell_id,
                        confidence_penalty=1,
                    )
                )
        if projection_key:
            projection_keys.add(projection_key)
    except Exception as exc:
        if projection_key:
            projection_keys.discard(projection_key)
        result["crystal_ball_error"] = type(exc).__name__
        log.debug("staged ROI crystal-ball projection suppressed: %s", exc)


__all__ = ["OWNER", "commit_staged_roi_crystal_ball_projection", "log_action_regrade"]
