"""Contextual Bandit forecast, selection, and terminal-learning owner.

The Bandit ranks future opportunities; it never grades actual outcomes and it
never executes a command.  Only an identity/session-matched terminal Referee
grade may update its posterior.  Selection is stochastic but reproducible from
the recorded decision seed, and every arm's propensity is published.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from arch4.action_opportunity_set import action_opportunity_set_error
from arch4.action_selection_receipts import (
    CONTEXTUAL_BANDIT_OWNER as OWNER,
    CONTEXTUAL_BANDIT_RECEIPT_SCHEMA as RECEIPT_SCHEMA,
    contextual_bandit_selection_receipt_error,
)
from arch4.terminal_reward_contract import (
    terminal_reward_entry_error as _terminal_reward_entry_error,
)


STATE_SCHEMA = "ContextualBanditState.v3"
FORECAST_SCHEMA = "SmartLoopCandidateForecast.v1"
LEARNING_RECEIPT_SCHEMA = "ContextualBanditLearningReceipt.v2"
PREDICTION_CORRECTION_SCHEMA = "ContextualBanditPredictionCorrection.v1"
POLICY_EFFECT_SCHEMA = "ContextualBanditPolicyEffect.v1"


@dataclass(slots=True)
class ArmPosterior:
    pulls: int = 0
    mean_grade: float = 0.0
    m2: float = 0.0
    negative_count: int = 0
    strong_negative_count: int = 0
    last_grade: int = 0

    def observe(self, grade: int) -> None:
        value = int(grade)
        self.pulls += 1
        delta = value - self.mean_grade
        self.mean_grade += delta / self.pulls
        self.m2 += delta * (value - self.mean_grade)
        self.negative_count += int(value < 0)
        self.strong_negative_count += int(value <= -2)
        self.last_grade = value

    @property
    def empirical_variance(self) -> float:
        return self.m2 / (self.pulls - 1) if self.pulls > 1 else 4.0

    @property
    def uncertainty(self) -> float:
        evidence = 1.0 / math.sqrt(self.pulls + 1.0)
        variance = min(1.0, math.sqrt(max(0.0, self.empirical_variance)) / 3.0)
        return _bounded((evidence + variance) / 2.0)

    @property
    def downside_risk(self) -> float:
        if self.pulls <= 0:
            return 0.5
        empirical = self.negative_count / self.pulls
        severe = self.strong_negative_count / self.pulls
        return _bounded((0.7 * empirical) + (0.3 * severe))

    def as_dict(self) -> dict[str, Any]:
        return {
            "pulls": int(self.pulls),
            "mean_grade": float(self.mean_grade),
            "m2": float(self.m2),
            "negative_count": int(self.negative_count),
            "strong_negative_count": int(self.strong_negative_count),
            "last_grade": int(self.last_grade),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ArmPosterior":
        return cls(
            pulls=max(0, _int(payload.get("pulls"), 0)),
            mean_grade=max(-3.0, min(3.0, _float(payload.get("mean_grade"), 0.0))),
            m2=max(0.0, _float(payload.get("m2"), 0.0)),
            negative_count=max(0, _int(payload.get("negative_count"), 0)),
            strong_negative_count=max(0, _int(payload.get("strong_negative_count"), 0)),
            last_grade=max(-3, min(3, _int(payload.get("last_grade"), 0))),
        )


class ContextualBanditState:
    def __init__(self) -> None:
        self._arms: dict[str, ArmPosterior] = {}
        self._consumed_reward_ledger_keys: set[str] = set()
        self._prediction_corrections: dict[str, dict[str, Any]] = {}
        self._observed_selection_receipt_ids: set[str] = set()

    @staticmethod
    def arm_key(*, context_key: str, opportunity_id: str) -> str:
        return f"{str(context_key)}|{str(opportunity_id)}"

    def posterior(self, *, context_key: str, opportunity_id: str) -> ArmPosterior:
        return self._arms.get(
            self.arm_key(context_key=context_key, opportunity_id=opportunity_id),
            ArmPosterior(),
        )

    def consume_terminal_reward(
        self,
        entry: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Update one arm only from a validated terminal RewardLedger entry."""

        row = _mapping(entry)
        invalid_reason = _terminal_reward_entry_error(row)
        if invalid_reason:
            return {
                "schema": LEARNING_RECEIPT_SCHEMA,
                "owner": OWNER,
                "admitted": False,
                "duplicate": False,
                "reason": invalid_reason,
            }
        ledger_key = str(row["ledger_key"])
        context_key = str(row["context_key"])
        opportunity_id = str(row["opportunity_id"])
        grade = row["grade"]
        if not opportunity_id:
            return {
                "schema": LEARNING_RECEIPT_SCHEMA,
                "owner": OWNER,
                "admitted": False,
                "duplicate": False,
                "reason": "terminal_reward_opportunity_id_missing_for_bandit",
                "ledger_key": ledger_key,
            }
        if ledger_key in self._consumed_reward_ledger_keys:
            return {
                "schema": LEARNING_RECEIPT_SCHEMA,
                "owner": OWNER,
                "admitted": False,
                "duplicate": True,
                "reason": "reward_ledger_entry_already_consumed",
                "ledger_key": ledger_key,
            }
        key = self.arm_key(context_key=context_key, opportunity_id=opportunity_id)
        posterior = self._arms.setdefault(key, ArmPosterior())
        before = posterior.as_dict()
        posterior.observe(grade)
        prediction = _mapping(row.get("prediction"))
        correction = {
            "schema": PREDICTION_CORRECTION_SCHEMA,
            "owner": OWNER,
            "reward_ledger_key": ledger_key,
            "action_root_id": str(row.get("action_root_id") or ""),
            "context_key": context_key,
            "opportunity_id": opportunity_id,
            "selection_receipt_id": str(
                prediction.get("selection_receipt_id") or ""
            ),
            "expected_referee_grade": prediction.get(
                "expected_referee_grade"
            ),
            "actual_referee_grade": int(grade),
            "signed_error": prediction.get("signed_error"),
            "absolute_error": prediction.get("absolute_error"),
            "pre_update_posterior": before,
            "post_update_posterior": posterior.as_dict(),
            "updated_ts": round(
                float(time.time() if now is None else now),
                6,
            ),
            "future_policy_effect_status": "pending",
            "future_policy_effect": {},
        }
        self._consumed_reward_ledger_keys.add(ledger_key)
        self._prediction_corrections[ledger_key] = correction
        return {
            "schema": LEARNING_RECEIPT_SCHEMA,
            "owner": OWNER,
            "admitted": True,
            "duplicate": False,
            "reason": "terminal_reward_ledger_entry_admitted",
            "ledger_key": ledger_key,
            "context_key": context_key,
            "opportunity_id": opportunity_id,
            "grade": grade,
            "arm_key": key,
            "posterior": posterior.as_dict(),
            "prediction_error_recorded": True,
            "policy_update_applied": True,
            "future_policy_effect_status": "pending",
            "prediction_correction": correction,
        }

    def pending_policy_effects(
        self, *, context_key: str
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._prediction_corrections.values()
            if str(row.get("context_key") or "") == str(context_key or "")
            and str(row.get("future_policy_effect_status") or "") == "pending"
        ]

    def admit_policy_effects(
        self,
        selection_receipt: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        row = _mapping(selection_receipt)
        receipt_error = contextual_bandit_selection_receipt_error(row)
        if receipt_error:
            return {
                "admitted": False,
                "duplicate": False,
                "reason": receipt_error,
            }
        selection_receipt_id = str(row.get("selection_receipt_id") or "")
        if not selection_receipt_id:
            return {
                "admitted": False,
                "duplicate": False,
                "reason": "selection_receipt_id_missing",
            }
        if selection_receipt_id in self._observed_selection_receipt_ids:
            return {
                "admitted": False,
                "duplicate": True,
                "reason": "selection_policy_effects_already_admitted",
                "selection_receipt_id": selection_receipt_id,
            }
        observations = [
            dict(value)
            for value in row.get("policy_effect_observations", [])
            if isinstance(value, Mapping)
        ]
        admitted_keys: list[str] = []
        for observation in observations:
            ledger_key = str(observation.get("reward_ledger_key") or "")
            correction = self._prediction_corrections.get(ledger_key)
            if not isinstance(correction, dict):
                continue
            if str(correction.get("future_policy_effect_status") or "") != "pending":
                continue
            if (
                observation.get("schema") != POLICY_EFFECT_SCHEMA
                or observation.get("owner") != OWNER
                or str(observation.get("context_key") or "")
                != str(correction.get("context_key") or "")
                or str(observation.get("affected_opportunity_id") or "")
                != str(correction.get("opportunity_id") or "")
            ):
                continue
            effect = {
                **observation,
                "selection_receipt_id": selection_receipt_id,
                "observed_ts": round(
                    float(time.time() if now is None else now),
                    6,
                ),
            }
            correction["future_policy_effect_status"] = "observed"
            correction["future_policy_effect"] = effect
            admitted_keys.append(ledger_key)
        self._observed_selection_receipt_ids.add(selection_receipt_id)
        return {
            "admitted": bool(admitted_keys),
            "duplicate": False,
            "reason": (
                "selection_policy_effects_admitted"
                if admitted_keys
                else "selection_has_no_eligible_pending_policy_effect"
            ),
            "selection_receipt_id": selection_receipt_id,
            "reward_ledger_keys": admitted_keys,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": STATE_SCHEMA,
            "owner": OWNER,
            "arms": {key: value.as_dict() for key, value in sorted(self._arms.items())},
            "consumed_reward_ledger_keys": sorted(
                self._consumed_reward_ledger_keys
            ),
            "prediction_corrections": {
                key: dict(value)
                for key, value in sorted(self._prediction_corrections.items())
            },
            "observed_selection_receipt_ids": sorted(
                self._observed_selection_receipt_ids
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ContextualBanditState":
        state = cls()
        if payload.get("schema") != STATE_SCHEMA or payload.get("owner") != OWNER:
            raise ValueError("contextual_bandit_state_contract_invalid")
        arms = payload.get("arms")
        if isinstance(arms, Mapping):
            state._arms = {
                str(key): ArmPosterior.from_dict(value)
                for key, value in arms.items()
                if isinstance(value, Mapping)
            }
        ledger_keys = payload.get("consumed_reward_ledger_keys")
        if isinstance(ledger_keys, Sequence) and not isinstance(
            ledger_keys, (str, bytes)
        ):
            state._consumed_reward_ledger_keys = {
                str(value) for value in ledger_keys if str(value)
            }
        corrections = payload.get("prediction_corrections")
        if isinstance(corrections, Mapping):
            state._prediction_corrections = {
                str(key): dict(value)
                for key, value in corrections.items()
                if isinstance(value, Mapping)
            }
        selection_ids = payload.get("observed_selection_receipt_ids")
        if isinstance(selection_ids, Sequence) and not isinstance(
            selection_ids, (str, bytes)
        ):
            state._observed_selection_receipt_ids = {
                str(value) for value in selection_ids if str(value)
            }
        return state


def select_contextual_bandit_candidate(
    *,
    opportunity_set: Mapping[str, Any],
    state: ContextualBanditState,
    decision_seed: str,
    future_context: Mapping[str, Any] | None = None,
    exploration_floor: float = 0.08,
) -> dict[str, Any]:
    opportunity_error = action_opportunity_set_error(opportunity_set)
    if opportunity_error:
        return _selection_rejection(opportunity_error)
    context_key = str(opportunity_set.get("learning_context_key") or "")
    if not context_key:
        return _selection_rejection("learning_context_key_missing")
    if not str(decision_seed or ""):
        return _selection_rejection("decision_seed_missing")
    authoritative_scope = _mapping(opportunity_set.get("evidence_scope"))
    scoped_future_context = dict(future_context or {})
    scoped_future_context["evidence_scope"] = authoritative_scope
    opportunities = [
        dict(row)
        for row in opportunity_set.get("opportunities", [])
        if isinstance(row, Mapping)
        and bool(row.get("legal", False))
        and bool(row.get("executable", False))
    ]
    if not opportunities:
        return _selection_rejection("no_legal_executable_opportunity")

    forecasts = [
        _forecast(
            context_key=context_key,
            opportunity=row,
            posterior=state.posterior(
                context_key=context_key,
                opportunity_id=str(row.get("opportunity_id") or ""),
            ),
            future_context=scoped_future_context,
        )
        for row in opportunities
    ]
    utilities = [float(row["selection_utility"]) for row in forecasts]
    temperature = max(0.15, min(1.0, sum(row["uncertainty"] for row in forecasts) / len(forecasts)))
    propensities = _mixed_softmax(
        utilities,
        temperature=temperature,
        exploration_floor=exploration_floor,
    )
    for forecast, propensity in zip(forecasts, propensities):
        forecast["selection_propensity"] = propensity
    draw = _deterministic_unit_interval(
        f"{decision_seed}|{context_key}|{','.join(str(row['opportunity_id']) for row in forecasts)}"
    )
    selected_index = _weighted_index(propensities, draw)
    selected = forecasts[selected_index]
    best_forecast = max(
        forecasts,
        key=lambda row: (
            float(row["selection_utility"]),
            str(row["opportunity_id"]),
        ),
    )
    predicted_regret = max(
        0.0,
        float(best_forecast["selection_utility"])
        - float(selected["selection_utility"]),
    )
    policy_effect_observations = _policy_effect_observations(
        state=state,
        context_key=context_key,
        opportunities=opportunities,
        future_context=scoped_future_context,
        exploration_floor=exploration_floor,
        draw=draw,
        selected_opportunity_id=str(selected["opportunity_id"]),
    )
    content = {
        "schema": RECEIPT_SCHEMA,
        "owner": OWNER,
        "selected": True,
        "reason": "contextual_bandit_propensity_draw",
        "learning_context_key": context_key,
        "opportunity_set_id": str(opportunity_set.get("opportunity_set_id") or ""),
        "identity_code": str(opportunity_set.get("identity_code") or ""),
        "session_scope_key": str(opportunity_set.get("session_scope_key") or ""),
        "source_epoch": str(opportunity_set.get("source_epoch") or ""),
        "source_sequence": int(opportunity_set.get("source_sequence") or 0),
        "evidence_scope": authoritative_scope,
        "decision_seed_sha256": hashlib.sha256(str(decision_seed).encode("utf-8")).hexdigest(),
        "draw": draw,
        "temperature": temperature,
        "exploration_floor": max(0.0, min(1.0, exploration_floor)),
        "selected_opportunity_id": selected["opportunity_id"],
        "selection_probability": selected["selection_propensity"],
        "selected_forecast": selected,
        "best_forecast_opportunity_id": str(best_forecast["opportunity_id"]),
        "predicted_selection_regret": predicted_regret,
        "candidate_forecasts": forecasts,
        "policy_effect_observations": policy_effect_observations,
        "propensity_sum": sum(propensities),
    }
    return {**content, "selection_receipt_id": _digest(content)}


def _forecast(
    *,
    context_key: str,
    opportunity: Mapping[str, Any],
    posterior: ArmPosterior,
    future_context: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = _mapping(opportunity.get("evidence"))
    evidence_count = max(posterior.pulls, _int(evidence.get("evidence_count"), 0))
    evidence_confidence = _bounded(_float(evidence.get("confidence"), 0.0))
    confidence = _bounded(
        max(evidence_confidence, 1.0 - (1.0 / math.sqrt(evidence_count + 1.0)))
    )
    prior_grade = max(-3.0, min(3.0, _float(evidence.get("expected_referee_grade"), 0.0)))
    expected_grade = posterior.mean_grade if posterior.pulls else prior_grade
    uncertainty = _bounded(
        max(
            posterior.uncertainty,
            _float(opportunity.get("uncertainty"), 0.0),
        )
    )
    downside = posterior.downside_risk
    disruption = _bounded(opportunity.get("disruption_cost"))
    future_fit = _future_context_fit(opportunity, future_context)
    exploration_value = _bounded(uncertainty * (1.0 - downside))
    utility = (
        (expected_grade / 3.0) * 0.45
        + confidence * 0.10
        + future_fit * 0.15
        + exploration_value * 0.20
        - downside * 0.25
        - disruption * 0.20
    )
    if str(opportunity.get("action") or "") == "do_nothing":
        utility += 0.08
    return {
        "schema": FORECAST_SCHEMA,
        "owner": OWNER,
        "context_key": context_key,
        "opportunity_id": str(opportunity.get("opportunity_id") or ""),
        "lever": str(opportunity.get("lever") or ""),
        "action": str(opportunity.get("action") or ""),
        "params": _mapping(opportunity.get("params")),
        "owner_class": str(opportunity.get("owner_class") or ""),
        "expected_referee_grade": expected_grade,
        "confidence": confidence,
        "downside_risk": downside,
        "evidence_count": evidence_count,
        "evidence_scope": _mapping(future_context.get("evidence_scope")),
        "future_context_fit": future_fit,
        "exploration_value": exploration_value,
        "disruption_cost": disruption,
        "uncertainty": uncertainty,
        "selection_utility": utility,
        "posterior": posterior.as_dict(),
    }


def _policy_effect_observations(
    *,
    state: ContextualBanditState,
    context_key: str,
    opportunities: Sequence[Mapping[str, Any]],
    future_context: Mapping[str, Any],
    exploration_floor: float,
    draw: float,
    selected_opportunity_id: str,
) -> list[dict[str, Any]]:
    opportunity_ids = {
        str(row.get("opportunity_id") or "") for row in opportunities
    }
    observations: list[dict[str, Any]] = []
    for correction in state.pending_policy_effects(context_key=context_key):
        affected = str(correction.get("opportunity_id") or "")
        if not affected or affected not in opportunity_ids:
            continue
        forecasts: list[dict[str, Any]] = []
        for opportunity in opportunities:
            opportunity_id = str(opportunity.get("opportunity_id") or "")
            posterior = state.posterior(
                context_key=context_key,
                opportunity_id=opportunity_id,
            )
            if opportunity_id == affected:
                posterior = ArmPosterior.from_dict(
                    _mapping(correction.get("pre_update_posterior"))
                )
            forecasts.append(
                _forecast(
                    context_key=context_key,
                    opportunity=opportunity,
                    posterior=posterior,
                    future_context=future_context,
                )
            )
        utilities = [
            float(forecast["selection_utility"]) for forecast in forecasts
        ]
        temperature = max(
            0.15,
            min(
                1.0,
                sum(forecast["uncertainty"] for forecast in forecasts)
                / len(forecasts),
            ),
        )
        propensities = _mixed_softmax(
            utilities,
            temperature=temperature,
            exploration_floor=exploration_floor,
        )
        counterfactual = forecasts[
            _weighted_index(propensities, draw)
        ]
        observations.append(
            {
                "schema": POLICY_EFFECT_SCHEMA,
                "owner": OWNER,
                "reward_ledger_key": str(
                    correction.get("reward_ledger_key") or ""
                ),
                "action_root_id": str(
                    correction.get("action_root_id") or ""
                ),
                "context_key": context_key,
                "affected_opportunity_id": affected,
                "current_selected_opportunity_id": str(
                    selected_opportunity_id or ""
                ),
                "counterfactual_selected_opportunity_id": str(
                    counterfactual.get("opportunity_id") or ""
                ),
                "selection_changed": bool(
                    str(counterfactual.get("opportunity_id") or "")
                    != str(selected_opportunity_id or "")
                ),
                "same_deterministic_draw": float(draw),
            }
        )
    return observations


def _future_context_fit(
    opportunity: Mapping[str, Any], future_context: Mapping[str, Any]
) -> float:
    desired = _mapping(future_context.get("opportunity_fit"))
    opportunity_id = str(opportunity.get("opportunity_id") or "")
    if opportunity_id in desired:
        return _bounded(desired[opportunity_id])
    evidence = _mapping(opportunity.get("evidence"))
    return _bounded(evidence.get("future_context_fit", 0.5))


def _mixed_softmax(
    utilities: Sequence[float], *, temperature: float, exploration_floor: float
) -> list[float]:
    if not utilities:
        return []
    peak = max(utilities)
    scaled = [math.exp((value - peak) / max(0.01, temperature)) for value in utilities]
    total = sum(scaled) or 1.0
    base = [value / total for value in scaled]
    epsilon = max(0.0, min(1.0, _float(exploration_floor, 0.08)))
    uniform = 1.0 / len(base)
    mixed = [((1.0 - epsilon) * value) + (epsilon * uniform) for value in base]
    correction = 1.0 - sum(mixed)
    mixed[-1] += correction
    return mixed


def _weighted_index(weights: Sequence[float], draw: float) -> int:
    cumulative = 0.0
    for index, weight in enumerate(weights):
        cumulative += float(weight)
        if draw <= cumulative:
            return index
    return max(0, len(weights) - 1)


def _deterministic_unit_interval(seed: str) -> float:
    raw = hashlib.sha256(str(seed).encode("utf-8")).digest()[:8]
    return int.from_bytes(raw, "big") / float(2**64)


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _selection_rejection(reason: str) -> dict[str, Any]:
    content = {
        "schema": RECEIPT_SCHEMA,
        "owner": OWNER,
        "selected": False,
        "reason": str(reason),
        "candidate_forecasts": [],
        "propensity_sum": 0.0,
    }
    return {**content, "selection_receipt_id": _digest(content)}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _bounded(value: Any) -> float:
    return max(0.0, min(1.0, _float(value, 0.0)))


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if math.isfinite(parsed) else float(default)


__all__ = [
    "ArmPosterior",
    "ContextualBanditState",
    "FORECAST_SCHEMA",
    "LEARNING_RECEIPT_SCHEMA",
    "OWNER",
    "POLICY_EFFECT_SCHEMA",
    "PREDICTION_CORRECTION_SCHEMA",
    "RECEIPT_SCHEMA",
    "STATE_SCHEMA",
    "select_contextual_bandit_candidate",
]
