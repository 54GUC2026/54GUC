"""
arch4/brain/brain_posture.py
=============================
BrainPosture — the brain's current operational stance.

Computed once per tick from all available signals and passed into the
decision pipeline alongside existing inputs. Represents a unified view
of what the brain knows about its recent quality and current context.

Design constraints:
  - Emergency and fault recovery behavior is NEVER modulated by posture
  - posture_modifier is always in [MODIFIER_FLOOR, MODIFIER_CEILING]
  - Advisory only: posture never blocks actions or changes safety thresholds
  - Backward compatible: all callers that don't pass posture are unchanged
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

# ── Modifier bounds (hard floor and ceiling, never exceeded) ──────────────────
MODIFIER_FLOOR = 0.3
MODIFIER_CEILING = 1.5

# ── Pilot presence default threshold ─────────────────────────────────────────
_DEFAULT_PILOT_PRESENCE_THRESHOLD_S = 300.0   # 5 minutes


@dataclass
class BrainPosture:
    """
    The brain's current operational stance, derived from all available signals.

    Passed into AggressivenessEngine.get_profile_with_posture() to modulate
    exploration rate. Does not affect emergency or fault recovery behavior.
    """

    # ── Live supervisor signal (this tick) ───────────────────────────────────
    last_intent_code: str = "no_action"
    last_outcome_code: str = "no_outcome"
    consecutive_verdict_warnings: int = 0
    ticks_since_last_action: int = 0

    # ── Cross-session quality (from journal) ─────────────────────────────────
    hour_quality: float = 0.5          # 0–1 quality score for current hour
    band_quality: Dict[int, float] = field(default_factory=dict)
    recent_outcome_ok_rate: float = 0.5
    recent_churn_rate: float = 0.0
    recent_regression_count: int = 0
    recent_warning_rate: float = 0.0
    recent_rejected_rate: float = 0.0
    recent_observe_monitor_rate: float = 0.0
    recent_authority_observe_monitor_rate: float = 0.0
    quality_confidence: float = 0.0    # 0–1, low when history is thin

    # ── Pilot presence ───────────────────────────────────────────────────────
    pilot_last_active_s: float = 0.0   # seconds since last GUI interaction
    pilot_present: bool = False

    # ── Derived modifier ─────────────────────────────────────────────────────
    posture_modifier: float = 1.0      # multiplier on probe_interval_s (1/modifier)
    prediction_modifier: float = 1.0   # owned by prediction/learning evidence, not Referee outcome rates
    prediction_posture: str = "normal"  # boosted / normal / cautious / conservative
    prediction_feedback_kind: str = ""  # reserved for prediction/learning-owned feedback

    def to_dict(self) -> Dict[str, Any]:
        return {
            "last_intent_code": str(self.last_intent_code),
            "last_outcome_code": str(self.last_outcome_code),
            "consecutive_verdict_warnings": int(self.consecutive_verdict_warnings),
            "ticks_since_last_action": int(self.ticks_since_last_action),
            "hour_quality": float(self.hour_quality),
            "recent_outcome_ok_rate": float(self.recent_outcome_ok_rate),
            "recent_churn_rate": float(self.recent_churn_rate),
            "recent_regression_count": int(self.recent_regression_count),
            "recent_warning_rate": float(self.recent_warning_rate),
            "recent_rejected_rate": float(self.recent_rejected_rate),
            "recent_observe_monitor_rate": float(self.recent_observe_monitor_rate),
            "recent_authority_observe_monitor_rate": float(self.recent_authority_observe_monitor_rate),
            "quality_confidence": float(self.quality_confidence),
            "pilot_last_active_s": float(self.pilot_last_active_s),
            "pilot_present": bool(self.pilot_present),
            "posture_modifier": float(self.posture_modifier),
            "prediction_modifier": float(self.prediction_modifier),
            "prediction_posture": str(self.prediction_posture),
            "prediction_feedback_kind": str(self.prediction_feedback_kind),
        }


# ── Posture builder ───────────────────────────────────────────────────────────

def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def posture_outcome_from_brain_referee(publication: Any) -> Dict[str, str]:
    """Project public Brain Referee output into posture outcome inputs.

    Production posture must not consume raw ``referee_outcome`` levels. The
    public ``brain_referee`` publication is the owner that decides whether an
    executed action was accepted, rejected, pending, or unprovable.
    """

    pub = _as_mapping(publication)
    verdict = _as_mapping(pub.get("verdict"))
    state = str(pub.get("state", "") or "").strip().lower()
    grade = str(pub.get("grade", "") or "").strip().lower()
    code = str(verdict.get("code", "") or "").strip().lower()

    if state == "executed" and grade == "rejected":
        return {
            "outcome_code": code or "post_action_regression",
            "outcome_level": "conflict",
        }
    if state == "executed" and grade in {"unavailable", "unknown"}:
        return {
            "outcome_code": code or "canonical_action_outcome_unavailable",
            "outcome_level": "warning",
        }
    if grade == "deferred":
        return {
            "outcome_code": code or "observe_monitor",
            "outcome_level": "ok",
        }
    if grade == "accepted":
        return {
            "outcome_code": code or "outcome_ok",
            "outcome_level": "ok",
        }
    if grade == "neutral":
        return {
            "outcome_code": code or "neutral_outcome",
            "outcome_level": "ok",
        }
    return {
        "outcome_code": code or "no_outcome",
        "outcome_level": "ok",
    }


def _learning_quality_mapping_value(profile: Any, key: str) -> Mapping[Any, Any]:
    source = _as_mapping(profile)
    if source:
        value = source.get(key)
    else:
        value = getattr(profile, key, {})
    return value if isinstance(value, Mapping) else {}


def _learning_quality_float(profile: Any, key: str, default: float = 0.0) -> float:
    source = _as_mapping(profile)
    value = source.get(key, default) if source else getattr(profile, key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _learning_quality_int(profile: Any, key: str, default: int = 0) -> int:
    source = _as_mapping(profile)
    value = source.get(key, default) if source else getattr(profile, key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _learning_quality_confidence(profile: Any) -> float:
    return _learning_quality_float(profile, "profile_confidence", 0.0)


def _learning_quality_hour_quality(profile: Any, hour: int) -> float:
    values = _learning_quality_mapping_value(profile, "hour_quality")
    for key in (int(hour), str(int(hour))):
        if key in values:
            try:
                return float(values[key])
            except (TypeError, ValueError):
                return 0.5
    return 0.5


def _learning_quality_band_quality(profile: Any) -> Dict[int, float]:
    out: Dict[int, float] = {}
    for raw_band, raw_quality in _learning_quality_mapping_value(profile, "band_quality").items():
        try:
            band = int(raw_band)
            quality = float(raw_quality)
        except (TypeError, ValueError):
            continue
        if band > 0:
            out[band] = quality
    return out


class BrainPostureBuilder:
    """
    Computes BrainPosture from available signals each tick.

    Stateful: tracks consecutive warnings across ticks.
    """

    def __init__(
        self,
        pilot_presence_threshold_s: float = _DEFAULT_PILOT_PRESENCE_THRESHOLD_S,
    ) -> None:
        self._pilot_presence_threshold_s = float(
            pilot_presence_threshold_s or _DEFAULT_PILOT_PRESENCE_THRESHOLD_S
        )
        self._consecutive_warnings: int = 0
        self._ticks_since_last_action: int = 0
        self._last_posture: Optional[BrainPosture] = None

    def compute(
        self,
        *,
        intent_code: str = "no_action",
        outcome_code: str = "no_outcome",
        intent_disposition: Any = None,
        intent_level: str = "ok",
        outcome_level: str = "ok",
        action_taken_this_tick: bool = False,
        learning_quality: Optional[Any] = None,
        pilot_last_active_s: float = 0.0,
        current_band: int = 0,
        current_hour: Optional[int] = None,
    ) -> BrainPosture:
        """
        Compute posture for this tick. Call once per tick after supervisor
        evaluation and before aggressiveness lookup.
        """
        now_hour = current_hour if current_hour is not None else time.localtime().tm_hour

        # ── Track consecutive warnings ────────────────────────────────────────
        disposition = str(getattr(intent_disposition, "value", intent_disposition) or "").upper()
        is_warning = (
            disposition in {"WARN", "HOLD", "HARD_STOP"}
            if disposition
            else intent_level in ("warning", "conflict", "block_recommended")
        ) or outcome_level in ("warning", "conflict")
        if is_warning:
            self._consecutive_warnings += 1
        else:
            self._consecutive_warnings = 0

        # ── Track ticks since last action ─────────────────────────────────────
        if action_taken_this_tick:
            self._ticks_since_last_action = 0
        else:
            self._ticks_since_last_action += 1

        # ── Extract quality signals ───────────────────────────────────────────
        confidence = _learning_quality_confidence(learning_quality)
        if learning_quality is not None and confidence > 0.0:
            hour_quality = _learning_quality_hour_quality(learning_quality, now_hour)
            band_quality = _learning_quality_band_quality(learning_quality)
            recent_ok = _learning_quality_float(learning_quality, "recent_outcome_ok_rate", 0.0)
            recent_churn = _learning_quality_float(learning_quality, "recent_churn_rate", 0.0)
            recent_regressions = _learning_quality_int(learning_quality, "recent_regression_count", 0)
            recent_warning_rate = _learning_quality_float(learning_quality, "recent_warning_rate", 0.0)
            recent_rejected_rate = _learning_quality_float(learning_quality, "recent_rejected_rate", 0.0)
            recent_observe_monitor_rate = _learning_quality_float(
                learning_quality, "recent_observe_monitor_rate", 0.0
            )
            recent_authority_observe_monitor_rate = _learning_quality_float(
                learning_quality, "recent_authority_observe_monitor_rate", 0.0
            )
        else:
            hour_quality = 0.5
            band_quality = {}
            recent_ok = 0.5
            recent_churn = 0.0
            recent_regressions = 0
            recent_warning_rate = 0.0
            recent_rejected_rate = 0.0
            recent_observe_monitor_rate = 0.0
            recent_authority_observe_monitor_rate = 0.0
            confidence = 0.0

        # ── Pilot presence ────────────────────────────────────────────────────
        pilot_present = float(pilot_last_active_s) < self._pilot_presence_threshold_s

        # ── Compute posture modifier ──────────────────────────────────────────
        modifier = _compute_modifier(
            intent_code=intent_code,
            outcome_code=outcome_code,
            consecutive_warnings=self._consecutive_warnings,
            recent_ok_rate=recent_ok,
            recent_churn_rate=recent_churn,
            recent_regressions=recent_regressions,
            hour_quality=hour_quality,
            confidence=confidence,
            pilot_present=pilot_present,
            pilot_last_active_s=float(pilot_last_active_s),
        )
        modifier = max(MODIFIER_FLOOR, min(MODIFIER_CEILING, modifier))

        prediction_modifier, prediction_posture, prediction_feedback_kind = _compute_prediction_modifier(
            recent_ok_rate=recent_ok,
            recent_churn_rate=recent_churn,
            recent_regressions=recent_regressions,
            recent_warning_rate=recent_warning_rate,
            recent_rejected_rate=recent_rejected_rate,
            recent_observe_monitor_rate=recent_observe_monitor_rate,
            recent_authority_observe_monitor_rate=recent_authority_observe_monitor_rate,
            confidence=confidence,
        )

        posture = BrainPosture(
            last_intent_code=str(intent_code or "no_action"),
            last_outcome_code=str(outcome_code or "no_outcome"),
            consecutive_verdict_warnings=self._consecutive_warnings,
            ticks_since_last_action=self._ticks_since_last_action,
            hour_quality=float(hour_quality),
            band_quality=band_quality,
            recent_outcome_ok_rate=float(recent_ok),
            recent_churn_rate=float(recent_churn),
            recent_regression_count=int(recent_regressions),
            recent_warning_rate=float(recent_warning_rate),
            recent_rejected_rate=float(recent_rejected_rate),
            recent_observe_monitor_rate=float(recent_observe_monitor_rate),
            recent_authority_observe_monitor_rate=float(recent_authority_observe_monitor_rate),
            quality_confidence=float(confidence),
            pilot_last_active_s=float(pilot_last_active_s),
            pilot_present=pilot_present,
            posture_modifier=float(modifier),
            prediction_modifier=float(prediction_modifier),
            prediction_posture=str(prediction_posture),
            prediction_feedback_kind=str(prediction_feedback_kind),
        )
        self._last_posture = posture
        return posture

    @property
    def last_posture(self) -> Optional[BrainPosture]:
        return self._last_posture


def _compute_modifier(
    *,
    intent_code: str,
    outcome_code: str,
    consecutive_warnings: int,
    recent_ok_rate: float,
    recent_churn_rate: float,
    recent_regressions: int,
    hour_quality: float,
    confidence: float,
    pilot_present: bool,
    pilot_last_active_s: float,
) -> float:
    """
    Compute the posture modifier from all available signals.

    Returns a float in [MODIFIER_FLOOR, MODIFIER_CEILING].
    Higher = more exploratory (shorter probe intervals).
    Lower = more conservative (longer probe intervals).

    Rules applied in order, multiplicative:
      Conservative pressure reduces modifier below 1.0
      Exploratory pressure raises modifier above 1.0
      Confidence gates how much history is trusted
    """
    modifier = 1.0

    # ── Immediate conservative pressure (high weight, no confidence gating) ──

    # Post-action regression: strong conservative signal, no history needed
    if outcome_code == "post_action_regression":
        modifier *= 0.5

    # Repeated supervisor warnings: escalating conservative pressure
    if consecutive_warnings >= 5:
        modifier *= 0.6
    elif consecutive_warnings >= 3:
        modifier *= 0.75
    elif consecutive_warnings >= 1:
        modifier *= 0.9

    # ── History-based pressure (gated by confidence) ──────────────────────────
    if confidence > 0.2:
        weight = min(1.0, confidence)  # scale effect with confidence

        # Recent regressions in 24h window
        if recent_regressions >= 3:
            modifier *= (1.0 - 0.25 * weight)
        elif recent_regressions >= 1:
            modifier *= (1.0 - 0.1 * weight)

        # High churn rate
        if recent_churn_rate >= 0.3:
            modifier *= (1.0 - 0.2 * weight)

        # This hour is historically bad
        if hour_quality < 0.4:
            modifier *= (1.0 - 0.2 * weight)

        # Recent poor outcome rate
        if recent_ok_rate < 0.5:
            modifier *= (1.0 - 0.15 * weight)

        # ── Exploratory pressure ──────────────────────────────────────────────

        # Long run of good outcomes
        if recent_ok_rate >= 0.9 and recent_regressions == 0:
            modifier *= (1.0 + 0.15 * weight)

        # This hour is historically good
        if hour_quality >= 0.8:
            modifier *= (1.0 + 0.1 * weight)

        # Low churn rate
        if recent_churn_rate < 0.05 and recent_ok_rate >= 0.8:
            modifier *= (1.0 + 0.1 * weight)

    # ── Pilot presence modulation ─────────────────────────────────────────────

    # Pilot just sat down — be conservative, let them observe first
    if pilot_present and pilot_last_active_s < 60.0:
        modifier *= 0.8

    # Pilot away for extended period — brain has full autonomy, slightly more exploratory
    if not pilot_present and pilot_last_active_s > 600.0:  # 10 minutes
        modifier *= 1.1

    return modifier


def _compute_prediction_modifier(
    *,
    recent_ok_rate: float,
    recent_churn_rate: float,
    recent_regressions: int,
    recent_warning_rate: float,
    recent_rejected_rate: float,
    recent_observe_monitor_rate: float,
    recent_authority_observe_monitor_rate: float,
    confidence: float,
) -> tuple[float, str, str]:
    """Return neutral prediction posture.

    Referee outcome rates describe recent action quality, but they do not own
    prediction authority. Crystal Ball and self-learning consume outcome
    history through their own stores and must decide whether prediction has a
    better candidate. Referee can still affect the general exploration posture
    through ``posture_modifier``; it must not directly cool, boost, or suppress
    prediction-driven candidates.
    """
    del (
        recent_ok_rate,
        recent_churn_rate,
        recent_regressions,
        recent_warning_rate,
        recent_rejected_rate,
        recent_observe_monitor_rate,
        recent_authority_observe_monitor_rate,
        confidence,
    )
    return 1.0, "normal", ""
