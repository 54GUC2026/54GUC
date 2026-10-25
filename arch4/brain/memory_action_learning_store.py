"""Terminal-reward Memory projection.

Memory owns durable containers and journal primitives. This module accepts
only one already-admitted terminal reward observation and its layered
``-3..+3`` grade. Generic classified action rows are not strategic learning
inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from arch4.signal_processing.band_quality import score_band_quality
from arch4.brain.delivered_load_memory import normalize_dlst
from arch4.brain.memory_band_learning_store import record_band_learning
from arch4.brain.memory_band_operational_history_owner import (
    record_band_attempt as record_operational_band_attempt,
)
from arch4.interfaces import LeverType
from arch4.learning_gate_contract import LearningGateDecision
from arch4.terminal_reward_observation import TerminalRewardObservation

OWNER = "arch4.brain.memory_action_learning_store"


@dataclass(frozen=True, slots=True)
class _ActionLearningFacts:
    rat_context: dict[str, Any]
    primary_after: int
    sinr_after: float
    primary_bw_mhz: float
    ca_active: bool
    ri: int
    bq_cell: float
    cq_cell: float
    nr_anchor: bool
    dlst: dict[str, Any]


def commit_terminal_reward_learning_to_memory_store(
    memory: Any,
    action_record: Any,
    *,
    terminal_observation: TerminalRewardObservation,
    terminal_grade_score: int,
) -> dict[str, Any]:
    """Commit one admitted terminal reward through its durable observation."""

    if memory is None:
        return {"committed": False, "reason": "missing_memory_store"}
    if action_record is None:
        return {"committed": False, "reason": "missing_action_record"}
    if not isinstance(terminal_observation, TerminalRewardObservation):
        raise TypeError(
            "memory_terminal_reward_requires_terminal_observation"
        )
    facts = _terminal_action_learning_facts(
        memory,
        action_record=action_record,
        observation=terminal_observation,
    )
    return _commit_action_learning_facts(
        memory,
        action_record,
        facts=facts,
        terminal_grade_score=terminal_grade_score,
    )


def _commit_action_learning_facts(
    memory: Any,
    action_record: Any,
    *,
    facts: _ActionLearningFacts,
    terminal_grade_score: int,
) -> dict[str, Any]:
    if facts.rat_context:
        action_record.rat_context = dict(facts.rat_context)
    action_record.DLST = dict(facts.dlst)
    status = str(getattr(action_record, "learning_commit_status", "") or "").strip().lower()
    if status == "committed":
        return {"committed": False, "reason": "already_committed"}

    gate_decision = LearningGateDecision.from_mapping(
        getattr(action_record, "learning_gate", None)
        if isinstance(getattr(action_record, "learning_gate", None), dict)
        else {}
    )
    if not gate_decision.learning_allowed:
        reason = str(gate_decision.block_reason or "learning_gate_blocked")
        action_record.learning_ltm_applied = False
        action_record.learning_commit_status = "blocked"
        action_record.learning_policy_reason = _append_reason(
            str(getattr(action_record, "learning_policy_reason", "") or ""),
            reason,
        )
        action_record.learning_gate = gate_decision.as_dict()
        return {"committed": False, "reason": reason}

    if not str(getattr(action_record, "outcome_label", "") or "").strip():
        action_record.learning_ltm_applied = False
        action_record.learning_commit_status = "skipped"
        action_record.learning_policy_reason = _append_reason(
            str(getattr(action_record, "learning_policy_reason", "") or ""),
            "missing_referee_classification",
        )
        return {"committed": False, "reason": "missing_referee_classification"}

    if type(terminal_grade_score) is not int or not -3 <= terminal_grade_score <= 3:
        return {"committed": False, "reason": "terminal_grade_score_invalid"}
    outcome_label = str(getattr(action_record, "outcome_label", "") or "").strip().lower()
    if terminal_grade_score == 0 or (
        terminal_grade_score > 0 and outcome_label != "good"
    ) or (
        terminal_grade_score < 0 and outcome_label != "bad"
    ):
        return {"committed": False, "reason": "terminal_grade_score_label_mismatch"}
    grade_weight = float(abs(terminal_grade_score))

    if action_record.lever != LeverType.BAND_SELECTION:
        action_record.learning_commit_status = "committed"
        return {"committed": True, "reason": "non_band_action"}

    bands = list(getattr(action_record, "action_params", {}).get("bands", []) or [])
    if not bands:
        action_record.learning_commit_status = "skipped"
        action_record.learning_policy_reason = _append_reason(
            str(getattr(action_record, "learning_policy_reason", "") or ""),
            "classified_commit:no_bands",
        )
        return {"committed": False, "reason": "no_bands"}

    allow_ltm = bool(getattr(action_record, "learning_ltm_applied", True))
    incident_assessment = {
        "incident_class": str(getattr(action_record, "incident_class", "") or ""),
        "recommended_next_step": str(getattr(action_record, "incident_next_step", "") or ""),
        "self_induced": bool(getattr(action_record, "incident_self_induced", False)),
    }

    learning_meta = record_band_learning(
        memory,
        timestamp=float(getattr(action_record, "timestamp", time.time()) or time.time()),
        bands=bands,
        outcome_label=str(getattr(action_record, "outcome_label", "") or ""),
        hard_fail=bool(getattr(action_record, "hard_fail", False)),
        primary_after=facts.primary_after,
        sinr_after=facts.sinr_after,
        primary_bw_mhz=facts.primary_bw_mhz,
        incident_assessment=incident_assessment,
        ca_active=facts.ca_active,
        ri=facts.ri,
        bq_cell=facts.bq_cell,
        cq_cell=facts.cq_cell,
        nr_anchor=facts.nr_anchor,
        use_ltm=allow_ltm,
        dlst=facts.dlst,
        grade_weight=grade_weight,
    )
    action_record.learning_ltm_applied = bool(learning_meta.get("use_ltm", False))
    action_record.learning_policy_reason = _append_reason(
        str(getattr(action_record, "learning_policy_reason", "") or ""),
        str(learning_meta.get("reason", "") or ""),
    )
    action_record.learning_commit_status = "committed"
    return {
        "committed": True,
        "reason": str(learning_meta.get("reason", "") or ""),
        "grade_weight": float(learning_meta.get("grade_weight", grade_weight) or grade_weight),
    }


def _terminal_action_learning_facts(
    memory: Any,
    *,
    action_record: Any,
    observation: TerminalRewardObservation,
) -> _ActionLearningFacts:
    cellular = observation.cellular
    cfg = getattr(memory, "cfg", None)
    profile = {
        "profile_id": str(getattr(cfg, "modem_backend", "") or ""),
        "supports_nr": bool(
            getattr(cfg, "supports_nr", False)
            or getattr(cfg, "supports_5g", False)
            or getattr(cfg, "supports_nr_band_lock", False)
        ),
    }
    rat_context = observation.rat_learning_context(profile=profile)
    return _ActionLearningFacts(
        rat_context=rat_context,
        primary_after=int(
            cellular.primary_band
            or infer_primary_band_after(action_record)
        ),
        sinr_after=float(cellular.sinr_db),
        primary_bw_mhz=float(cellular.primary_bw_mhz),
        ca_active=bool(cellular.ca_active),
        ri=int(cellular.ri),
        bq_cell=_score_bq(
            memory,
            sinr_db=float(cellular.sinr_db),
            rsrp_dbm=float(cellular.rsrp_dbm),
            ri=int(cellular.ri),
            ca_active=bool(cellular.ca_active),
            primary_bw_mhz=float(cellular.primary_bw_mhz),
        ),
        cq_cell=float(observation.quality.cq_cell),
        nr_anchor=bool(rat_context.get("nr_active", False)),
        dlst=normalize_dlst(getattr(action_record, "DLST", None)),
    )


def _score_bq(
    memory: Any,
    *,
    sinr_db: float,
    rsrp_dbm: float,
    ri: int,
    ca_active: bool,
    primary_bw_mhz: float,
) -> float:
    return float(
        score_band_quality(
            sinr_db=sinr_db,
            rsrp_db=rsrp_dbm,
            ri=ri,
            ca_value=1.0 if ca_active else 0.0,
            bw_mhz=primary_bw_mhz,
        ).get("quality_score", 0.0)
        or 0.0
    )


def infer_primary_band_after(action_record: Any) -> int:
    params = getattr(action_record, "action_params", {}) or {}
    for key in ("pband", "band", "primary_band", "_ca_pool_anchor_band", "_tsar_candidate_band"):
        try:
            band = int(params.get(key, 0) or 0)
        except (TypeError, ValueError):
            band = 0
        if band > 0:
            return band
    text = str(getattr(action_record, "ca_signature_after", "") or "")
    for part in text.replace("+", " ").split():
        part = part.strip().upper()
        if part.startswith("B"):
            part = part[1:]
        try:
            band = int(part)
        except (TypeError, ValueError):
            continue
        if band > 0:
            return band
    return 0


def record_band_attempt_to_memory_store(
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
    record_operational_band_attempt(
        memory,
        band=band,
        appeared_in_ca=appeared_in_ca,
        sinr=sinr,
        ca_sig=ca_sig,
        throughput=throughput,
        outcome=outcome,
        is_primary=is_primary,
        weak_unusable=weak_unusable,
    )


def _append_reason(existing: str, addition: str) -> str:
    existing = str(existing or "").strip(";")
    addition = str(addition or "").strip(";")
    if not addition:
        return existing
    if not existing:
        return addition
    parts = [p for p in existing.split(";") if p]
    if addition in parts:
        return existing
    return f"{existing};{addition}"


__all__ = [
    "OWNER",
    "commit_terminal_reward_learning_to_memory_store",
    "infer_primary_band_after",
    "record_band_attempt_to_memory_store",
]
