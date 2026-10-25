"""
arch4/brain/referee.py
=================================
Post-action strategic judge for Brain TSAR.

Referee is not a fuse and not a decision-maker.
It grades what TSAR chose after execution has settled.
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

from dataclasses import dataclass, field
import time
from typing import Any, Dict, Mapping, Optional

from arch4.brain.congestion_signals import quiet_rtt_avg
from arch4.brain.ca_contract import cart_ld_active
from arch4.action_transaction import action_transaction_key_from_action
from arch4.interfaces import (
    Arch4Config,
    LeverAction,
    LeverResult,
    LeverType,
)


@dataclass
class RefereeVerdict:
    level: str
    code: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
            "ts": float(self.ts),
        }


class RefereeBrain:
    """
    Strategic post-action judge for TSAR.

    It receives the same pre-action surfaces TSAR had plus the actual
    post-action outcome, then grades the choice as good / neutral / bad.
    """

    def __init__(self, cfg: Arch4Config):
        self.cfg = cfg

    def refresh_thresholds(self) -> None:
        """ACK the immutable generation; Referee reads fields on demand."""

    @staticmethod
    def _pcell_bandwidth(modem: Optional[CellularIntelligenceGeneration]) -> float:
        if modem is None:
            return 0.0
        for carrier in list(getattr(modem.cellular, 'carriers', []) or []):
            if bool(getattr(carrier, "is_pcell", False)):
                return float(getattr(carrier, "bandwidth_mhz", 0.0) or 0.0)
        return 0.0

    @staticmethod
    def _advisor_feedback(
        *,
        brain_vectors: Mapping[str, Any] | None,
        grade: str,
    ) -> Dict[str, str]:
        vectors = brain_vectors if isinstance(brain_vectors, Mapping) else {}
        out: Dict[str, str] = {"tsar": "reward" if grade == "good" else "punish" if grade == "bad" else "hold"}
        calibration = vectors.get("calibration_vector") if isinstance(vectors.get("calibration_vector"), Mapping) else {}
        learning = vectors.get("learning_vector") if isinstance(vectors.get("learning_vector"), Mapping) else {}
        prediction = vectors.get("prediction_vector") if isinstance(vectors.get("prediction_vector"), Mapping) else {}
        constraint = vectors.get("constraint_vector") if isinstance(vectors.get("constraint_vector"), Mapping) else {}

        if calibration:
            out["calibration"] = "helped" if grade == "good" else "misled" if grade == "bad" else "neutral"
        if learning:
            out["learning"] = "helped" if grade == "good" else "misled" if grade == "bad" else "neutral"
        if prediction and bool(prediction.get("ready", False)):
            out["prediction"] = "helped" if grade == "good" else "misled" if grade == "bad" else "neutral"
        if constraint:
            out["constraint"] = "guarded" if grade != "good" else "neutral"
        return out

    @staticmethod
    def _is_band_control_action(action: LeverAction) -> bool:
        return action.lever in {
            LeverType.BAND_SELECTION,
            LeverType.BAND_LOCK,
            LeverType.CELL_LOCK,
            LeverType.PBAND,
            LeverType.NR_BAND_LOCK,
        }

    @staticmethod
    def _raw_rf_collapse(modem: Optional[CellularIntelligenceGeneration]) -> bool:
        if modem is None or not bool(getattr(modem.cellular, 'radio_connected', False)):
            return False
        try:
            sinr_db = float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0)
            rsrp_dbm = float(getattr(modem.cellular, 'rsrp_dbm', 0.0) or 0.0)
            rsrq_db = float(getattr(modem.cellular, 'rsrq_db', 0.0) or 0.0)
        except Exception:
            return False
        return bool(sinr_db <= 3.0 and rsrp_dbm <= -88.0 and rsrq_db <= -12.0)

    def _post_action_rf_collapse(
        self,
        *,
        action: LeverAction,
        modem_after: Optional[CellularIntelligenceGeneration],
        computed_after: Optional[DerivedSignalFrames],
        dl_after: float,
        rtt_after: float,
    ) -> bool:
        if not self._is_band_control_action(action):
            return False
        if not self._raw_rf_collapse(modem_after):
            return False

        capacity_floor = float(getattr(self.cfg, "bad_rf_capacity_min_dl_mbps", 20.0) or 20.0)
        rtt_ceiling = float(getattr(self.cfg, "bad_rf_capacity_max_rtt_ms", 120.0) or 120.0)
        ca_after = cart_ld_active(computed_after) if computed_after is not None else False
        cq_after = float(getattr(computed_after.quality, 'cq_cell', 0.0) or 0.0) if computed_after is not None else 0.0
        cq_valid = bool(getattr(computed_after.quality, 'cq_vector_a_valid', False)) if computed_after is not None else False
        capacity_winner = bool(
            dl_after >= capacity_floor
            or (ca_after and dl_after >= 5.0 and (rtt_after <= 0.0 or rtt_after <= rtt_ceiling))
        )
        if capacity_winner:
            return False
        if cq_valid:
            return cq_after <= 0.45
        return True

    def evaluate_outcome(
        self,
        *,
        action: Optional[LeverAction],
        action_result: Optional[LeverResult],
        action_suppressed: bool,
        modem_before: Optional[CellularIntelligenceGeneration],
        modem_after: Optional[CellularIntelligenceGeneration],
        computed_before: Optional[DerivedSignalFrames],
        computed_after: Optional[DerivedSignalFrames],
        bouncer_outcome: Optional[Mapping[str, Any]] = None,
        decision_trace: Optional[Mapping[str, Any]] = None,
        brain_vectors: Optional[Mapping[str, Any]] = None,
    ) -> RefereeVerdict:
        bouncer = bouncer_outcome if isinstance(bouncer_outcome, Mapping) else {}
        _trace = decision_trace if isinstance(decision_trace, Mapping) else {}

        if action is None:
            return RefereeVerdict(
                level="deferred",
                code="no_action",
                message="No TSAR action to grade",
                details={"advisor_feedback": {"tsar": "hold"}},
            )

        if action_suppressed:
            return RefereeVerdict(
                level="deferred",
                code="observe_monitor",
                message="Action stayed in observe/monitor mode",
                details={
                    "advisor_feedback": {"tsar": "hold"},
                    "bouncer_code": str(bouncer.get("code", "") or ""),
                },
            )

        if self._is_band_control_action(action) and modem_after is not None:
            transition = modem_after.radio_transition
            action_key = action_transaction_key_from_action(action)
            expected_root = str(action_key.root_id if action_key is not None else "")
            conflicting_source = bool(
                not transition.attribution_available
                or transition.source.startswith("lower_controller")
                or transition.source != "54guc_command"
                or not expected_root
                or transition.command_root_id != expected_root
            )
            if conflicting_source:
                return RefereeVerdict(
                    level="neutral",
                    code="radio_selection_causality_not_owned",
                    message=(
                        "Outcome radio selection is not attributable to this "
                        "54GUC action; strategic grade held neutral"
                    ),
                    details={
                        "score": 0,
                        "selection_source": str(transition.source or ""),
                        "selection_episode_id": str(transition.episode_id or ""),
                        "transition_id": str(transition.transition_id or ""),
                        "expected_action_root_id": expected_root,
                        "observed_command_root_id": str(
                            transition.command_root_id or ""
                        ),
                        "attribution_available": bool(
                            transition.attribution_available
                        ),
                        "attribution_error": str(
                            transition.attribution_error or ""
                        ),
                        "advisor_feedback": self._advisor_feedback(
                            brain_vectors=brain_vectors,
                            grade="neutral",
                        ),
                    },
                )

        bouncer_code = str(bouncer.get("code", "") or "")
        if bouncer_code == "radio_lost_after_action":
            return RefereeVerdict(
                level="bad",
                code="radio_lost_after_action",
                message="TSAR action caused radio loss",
                details={
                    "advisor_feedback": self._advisor_feedback(brain_vectors=brain_vectors, grade="bad"),
                    "bouncer_code": bouncer_code,
                },
            )
        if bouncer_code == "band_action_no_effect":
            return RefereeVerdict(
                level="bad",
                code="band_action_no_effect",
                message="TSAR action changed nothing useful",
                details={
                    "advisor_feedback": self._advisor_feedback(brain_vectors=brain_vectors, grade="bad"),
                    "bouncer_code": bouncer_code,
                },
            )
        if bouncer_code in {"action_failed", "missing_action_result", "action_suppressed"}:
            return RefereeVerdict(
                level="neutral",
                code=bouncer_code or "execution_unclear",
                message="Execution failed or stayed unclear, so TSAR grade is deferred",
                details={
                    "advisor_feedback": self._advisor_feedback(brain_vectors=brain_vectors, grade="neutral"),
                    "bouncer_code": bouncer_code,
                },
            )

        if action_result is None or not bool(getattr(action_result, "success", False)):
            return RefereeVerdict(
                level="neutral",
                code="execution_unclear",
                message="No reliable execution result exists for strategic grading",
                details={"advisor_feedback": self._advisor_feedback(brain_vectors=brain_vectors, grade="neutral")},
            )

        bandwidth_before = self._pcell_bandwidth(modem_before)
        bandwidth_after = self._pcell_bandwidth(modem_after)
        ca_before = cart_ld_active(computed_before) if computed_before is not None else False
        ca_after = cart_ld_active(computed_after) if computed_after is not None else False
        nsa_before = bool(getattr(modem_before.cellular, 'nsa_active', False))
        nsa_after = bool(getattr(modem_after.cellular, 'nsa_active', False))
        sinr_before = float(getattr(modem_before.cellular, 'sinr_db', 0.0) or 0.0)
        sinr_after = float(getattr(modem_after.cellular, 'sinr_db', 0.0) or 0.0)
        rsrp_before = float(getattr(modem_before.cellular, 'rsrp_dbm', 0.0) or 0.0)
        rsrp_after = float(getattr(modem_after.cellular, 'rsrp_dbm', 0.0) or 0.0)
        dl_before = float(getattr(modem_before.traffic, 'dl_mbps', 0.0) or 0.0)
        dl_after = float(getattr(modem_after.traffic, 'dl_mbps', 0.0) or 0.0)
        cq_before = float(getattr(computed_before.quality, 'cq_cell', 0.0) or 0.0)
        cq_after = float(getattr(computed_after.quality, 'cq_cell', 0.0) or 0.0)
        rtt_before = float(quiet_rtt_avg(computed_before) if computed_before is not None else 0.0)
        rtt_after = float(quiet_rtt_avg(computed_after) if computed_after is not None else 0.0)

        if nsa_before != nsa_after:
            direction = "attached" if nsa_after else "detached"
            return RefereeVerdict(
                level="neutral",
                code="nr_state_changed_during_eval",
                message=f"NR {direction} during outcome evaluation; strategic grade held neutral",
                details={
                    "nsa_before": nsa_before,
                    "nsa_after": nsa_after,
                    "advisor_feedback": self._advisor_feedback(brain_vectors=brain_vectors, grade="neutral"),
                    "bouncer_code": bouncer_code,
                },
            )

        if self._post_action_rf_collapse(
            action=action,
            modem_after=modem_after,
            computed_after=computed_after,
            dl_after=dl_after,
            rtt_after=rtt_after,
        ):
            return RefereeVerdict(
                level="bad",
                code="post_action_rf_collapse",
                message="TSAR band action landed in corroborated bad RF",
                details={
                    "score": -3,
                    "good_reasons": [],
                    "bad_reasons": ["post_action_rf_collapse"],
                    "capacity_gain": False,
                    "sinr_after": sinr_after,
                    "rsrp_after": rsrp_after,
                    "rsrq_after": float(getattr(modem_after.cellular, 'rsrq_db', 0.0) or 0.0),
                    "dl_after_mbps": dl_after,
                    "rtt_after_ms": rtt_after,
                    "serving_band_after": int(getattr(modem_after.cellular, 'primary_band', 0) or 0),
                    "pci_after": int(getattr(modem_after.cellular, 'pci', 0) or 0),
                    "cell_id_after": int(getattr(modem_after.cellular, 'cell_id', 0) or 0),
                    "advisor_feedback": self._advisor_feedback(brain_vectors=brain_vectors, grade="bad"),
                    "decision_source": str(_trace.get("source", "") or ""),
                    "bouncer_code": bouncer_code,
                },
            )

        score = 0
        reasons_good: list[str] = []
        reasons_bad: list[str] = []
        capacity_gain = False

        if bandwidth_after > bandwidth_before + 1.0:
            score += 2
            capacity_gain = True
            reasons_good.append("wider_pcell")
        if ca_after and not ca_before:
            score += 2
            capacity_gain = True
            reasons_good.append("ca_gained")
        if dl_after >= max(dl_before + 5.0, dl_before * 1.25):
            score += 2
            reasons_good.append("throughput_up")
        elif dl_before >= 5.0 and dl_after <= dl_before * 0.75:
            score -= 2
            reasons_bad.append("throughput_down")

        if rtt_before > 0.0 and rtt_after > 0.0:
            if rtt_after <= min(rtt_before - 15.0, rtt_before * 0.8):
                score += 1
                reasons_good.append("quiet_rtt_better")
            elif rtt_after >= max(rtt_before + 25.0, rtt_before * 1.3):
                score -= 2
                reasons_bad.append("quiet_rtt_worse")

        if cq_after >= cq_before + 0.08:
            score += 1
            reasons_good.append("cq_better")
        elif cq_after <= cq_before - 0.10:
            score -= 1
            reasons_bad.append("cq_worse")

        if sinr_after >= sinr_before + 2.0:
            score += 1
            reasons_good.append("sinr_better")
        elif not capacity_gain and sinr_after <= sinr_before - 3.0:
            score -= 1
            reasons_bad.append("sinr_worse")

        if rsrp_after >= rsrp_before + 3.0:
            score += 1
            reasons_good.append("rsrp_better")
        elif not capacity_gain and rsrp_after <= rsrp_before - 4.0:
            score -= 1
            reasons_bad.append("rsrp_worse")

        if score >= 2:
            grade = "good"
            code = "outcome_ok"
            message = "TSAR action improved the operating result"
        elif score <= -2:
            grade = "bad"
            code = "post_action_regression"
            message = "TSAR action made the operating result worse"
        else:
            grade = "neutral"
            code = "neutral_outcome"
            message = "TSAR action closed neutral in the timed evaluation window"

        return RefereeVerdict(
            level=grade,
            code=code,
            message=message,
            details={
                "score": int(score),
                "good_reasons": reasons_good,
                "bad_reasons": reasons_bad,
                "capacity_gain": bool(capacity_gain),
                "advisor_feedback": self._advisor_feedback(brain_vectors=brain_vectors, grade=grade),
                "decision_source": str(_trace.get("source", "") or ""),
                "bouncer_code": bouncer_code,
            },
        )
