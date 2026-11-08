"""Durable progress for normal SmartLoop optimization behavior.

This module owns pacing, repeated-target suppression, priority-anchor
verification and NR rollback observations.  It consumes immutable selected-
action terminal receipts and accepted runtime observations.  It never builds,
ranks, selects, executes or grades an action.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from typing import Any, Mapping


OWNER = "arch4.smart_loop.normal_behavior_progress"
SCHEMA = "NormalBehaviorProgress.v1"
MAX_PROCESSED_COMMANDS = 256
MAX_RESULTS = 32

_NORMAL_BEHAVIORS = frozenset(
    {
        "working_restore_known_good_ca",
        "anchor_enforcement",
        "degraded_best_live_neighbor",
        "degraded_open_all_deployed_bands",
        "congested_neighbor_escape",
        "congested_restore_known_good_ca",
        "systematic_bad_rf_band_probe",
        "cell_exploration_and_optimization",
        "band_priority_rotation",
        "nr_capacity_coverage_preference",
    }
)

_SUCCESS_PACING_S = {
    "working_restore_known_good_ca": 300.0,
    "anchor_enforcement": 1200.0,
    "degraded_best_live_neighbor": 30.0,
    "degraded_open_all_deployed_bands": 30.0,
    "congested_neighbor_escape": 120.0,
    "congested_restore_known_good_ca": 120.0,
    "systematic_bad_rf_band_probe": 1800.0,
    "cell_exploration_and_optimization": 1800.0,
    "band_priority_rotation": 1200.0,
    "nr_capacity_coverage_preference": 300.0,
}


@dataclass(frozen=True, slots=True)
class AnchorRotationProgress:
    command_id: str
    target_band: int
    verify_at: float
    backend_family: str
    status: str = "pending"
    actual_band: int = 0
    observed_at: float = 0.0
    learning_projected: bool = False

    def __post_init__(self) -> None:
        if not self.command_id or self.target_band <= 0 or self.verify_at <= 0.0:
            raise ValueError("normal_behavior_anchor_progress_invalid")
        if self.status not in {"pending", "held", "reverted", "escalated"}:
            raise ValueError("normal_behavior_anchor_status_invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "target_band": int(self.target_band),
            "verify_at": round(float(self.verify_at), 6),
            "backend_family": self.backend_family,
            "status": self.status,
            "actual_band": int(self.actual_band),
            "observed_at": round(float(self.observed_at), 6),
            "learning_projected": bool(self.learning_projected),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AnchorRotationProgress":
        row = dict(raw or {})
        return cls(
            command_id=str(row.get("command_id") or ""),
            target_band=_int(row.get("target_band")),
            verify_at=_float(row.get("verify_at")),
            backend_family=str(row.get("backend_family") or ""),
            status=str(row.get("status") or "pending"),
            actual_band=_int(row.get("actual_band")),
            observed_at=_float(row.get("observed_at")),
            learning_projected=bool(row.get("learning_projected", False)),
        )


@dataclass(frozen=True, slots=True)
class NormalBehaviorResult:
    command_id: str
    behavior_id: str
    lever: str
    target_shape: Mapping[str, Any]
    terminal_status: str
    success: bool
    result_ts: float
    progress_scope_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "behavior_id": self.behavior_id,
            "lever": self.lever,
            "target_shape": _json_mapping(self.target_shape),
            "terminal_status": self.terminal_status,
            "success": bool(self.success),
            "result_ts": round(float(self.result_ts), 6),
            "progress_scope_id": self.progress_scope_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NormalBehaviorResult":
        row = dict(raw or {})
        value = cls(
            command_id=str(row.get("command_id") or ""),
            behavior_id=str(row.get("behavior_id") or ""),
            lever=str(row.get("lever") or ""),
            target_shape=_json_mapping(row.get("target_shape")),
            terminal_status=str(row.get("terminal_status") or ""),
            success=bool(row.get("success", False)),
            result_ts=_float(row.get("result_ts")),
            progress_scope_id=str(row.get("progress_scope_id") or ""),
        )
        if not value.command_id or value.behavior_id not in _NORMAL_BEHAVIORS:
            raise ValueError("normal_behavior_result_invalid")
        return value


@dataclass(frozen=True, slots=True)
class NormalBehaviorProgress:
    processed_command_ids: tuple[str, ...] = ()
    behavior_next_allowed_ts: Mapping[str, float] = field(default_factory=dict)
    recent_results: tuple[NormalBehaviorResult, ...] = ()
    anchor: AnchorRotationProgress | None = None
    cell_last_target_pci: int = 0
    cell_next_allowed_ts: float = 0.0
    nr_lock_mode: str = ""
    nr_lock_set_at: float = 0.0
    nr_bad_sinr_observations: int = 0
    nr_last_observation_key: str = ""

    def __post_init__(self) -> None:
        if self.nr_lock_mode not in {"", "capacity", "coverage", "all"}:
            raise ValueError("normal_behavior_nr_lock_mode_invalid")
        if self.nr_bad_sinr_observations < 0:
            raise ValueError("normal_behavior_nr_bad_observations_invalid")
        unknown = set(self.behavior_next_allowed_ts) - set(_NORMAL_BEHAVIORS)
        if unknown:
            raise ValueError("normal_behavior_pacing_behavior_invalid")

    def behavior_available(self, behavior_id: str, *, now: float) -> bool:
        return float(now) >= float(
            self.behavior_next_allowed_ts.get(str(behavior_id or ""), 0.0)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "processed_command_ids": list(self.processed_command_ids),
            "behavior_next_allowed_ts": {
                key: round(float(value), 6)
                for key, value in sorted(self.behavior_next_allowed_ts.items())
            },
            "recent_results": [value.as_dict() for value in self.recent_results],
            "anchor": self.anchor.as_dict() if self.anchor is not None else None,
            "cell_last_target_pci": int(self.cell_last_target_pci),
            "cell_next_allowed_ts": round(float(self.cell_next_allowed_ts), 6),
            "nr_lock_mode": self.nr_lock_mode,
            "nr_lock_set_at": round(float(self.nr_lock_set_at), 6),
            "nr_bad_sinr_observations": int(self.nr_bad_sinr_observations),
            "nr_last_observation_key": self.nr_last_observation_key,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NormalBehaviorProgress":
        row = dict(raw or {})
        if row and (
            row.get("schema") != SCHEMA or row.get("owner") != OWNER
        ):
            raise ValueError("normal_behavior_progress_contract_invalid")
        anchor_raw = row.get("anchor")
        return cls(
            processed_command_ids=tuple(
                str(value)
                for value in list(row.get("processed_command_ids") or [])
                if str(value or "")
            )[-MAX_PROCESSED_COMMANDS:],
            behavior_next_allowed_ts={
                str(key): _float(value)
                for key, value in dict(
                    row.get("behavior_next_allowed_ts") or {}
                ).items()
            },
            recent_results=tuple(
                NormalBehaviorResult.from_dict(value)
                for value in list(row.get("recent_results") or [])
                if isinstance(value, Mapping)
            )[-MAX_RESULTS:],
            anchor=(
                AnchorRotationProgress.from_dict(anchor_raw)
                if isinstance(anchor_raw, Mapping)
                else None
            ),
            cell_last_target_pci=_int(row.get("cell_last_target_pci")),
            cell_next_allowed_ts=_float(row.get("cell_next_allowed_ts")),
            nr_lock_mode=str(row.get("nr_lock_mode") or ""),
            nr_lock_set_at=_float(row.get("nr_lock_set_at")),
            nr_bad_sinr_observations=max(
                0, _int(row.get("nr_bad_sinr_observations"))
            ),
            nr_last_observation_key=str(row.get("nr_last_observation_key") or ""),
        )


def project_normal_behavior_terminal(
    progress: NormalBehaviorProgress,
    *,
    selected_action: Mapping[str, Any],
    command_receipt: Mapping[str, Any] | None,
    terminal_stage: str,
    terminal_reason: str,
    now: float,
) -> tuple[NormalBehaviorProgress, NormalBehaviorResult | None]:
    """Idempotently project one terminal result from the immutable action."""

    if not isinstance(progress, NormalBehaviorProgress):
        raise TypeError("normal_behavior_progress_required")
    action = dict(selected_action or {})
    brain = _mapping(action.get("brain_decision_receipt"))
    opportunity = _mapping(brain.get("selected_opportunity"))
    evidence = _mapping(opportunity.get("evidence"))
    behavior = str(evidence.get("behavior_id") or "")
    if behavior not in _NORMAL_BEHAVIORS:
        return progress, None
    command_id = str(action.get("command_id") or "").strip()
    if not command_id:
        raise ValueError("normal_behavior_terminal_command_id_missing")
    if command_id in progress.processed_command_ids:
        existing = next(
            (
                value
                for value in reversed(progress.recent_results)
                if value.command_id == command_id
            ),
            None,
        )
        return progress, existing

    receipt = dict(command_receipt or {})
    status = str(
        receipt.get("terminal_status")
        or terminal_stage
        or terminal_reason
        or "unknown"
    ).lower()
    success = bool(
        str(terminal_stage or "").lower() == "terminal"
        and status == "settled"
        and receipt.get("proof_ready") is True
    )
    result_ts = _float(
        receipt.get("settled_ts")
        or receipt.get("terminal_ts")
        or now,
        float(now),
    )
    target_shape = _mapping(opportunity.get("params"))
    result = NormalBehaviorResult(
        command_id=command_id,
        behavior_id=behavior,
        lever=str(opportunity.get("lever") or action.get("lever") or ""),
        target_shape=target_shape,
        terminal_status=status,
        success=success,
        result_ts=result_ts,
        progress_scope_id=str(evidence.get("progress_scope_id") or ""),
    )
    processed = tuple((*progress.processed_command_ids, command_id))[
        -MAX_PROCESSED_COMMANDS:
    ]
    results = tuple((*progress.recent_results, result))[-MAX_RESULTS:]
    pacing = dict(progress.behavior_next_allowed_ts)
    delay = _SUCCESS_PACING_S[behavior] if success else 60.0
    pacing[behavior] = max(float(pacing.get(behavior, 0.0)), result_ts + delay)
    updated = replace(
        progress,
        processed_command_ids=processed,
        recent_results=results,
        behavior_next_allowed_ts=pacing,
    )

    if behavior == "band_priority_rotation" and success:
        target_band = _target_band(target_shape)
        if target_band <= 0:
            raise ValueError("normal_behavior_priority_target_band_missing")
        family = str(
            evidence.get("backend_family")
            or evidence.get("access_type")
            or ""
        ).lower()
        verify_s = (
            600.0
            if any(token in family for token in ("huawei", "hilink", "b818"))
            else 300.0
        )
        updated = replace(
            updated,
            anchor=AnchorRotationProgress(
                command_id=command_id,
                target_band=target_band,
                verify_at=result_ts + verify_s,
                backend_family=family,
            ),
        )
    elif behavior == "anchor_enforcement" and success and updated.anchor is not None:
        updated = replace(
            updated,
            anchor=replace(
                updated.anchor,
                status="escalated",
                observed_at=result_ts,
            ),
        )
    elif behavior == "cell_exploration_and_optimization" and success:
        pci = _int(
            target_shape.get("pci")
            or evidence.get("cell_opt_target_pci")
            or evidence.get("pci")
        )
        updated = replace(
            updated,
            cell_last_target_pci=max(0, pci),
            cell_next_allowed_ts=result_ts + 1800.0,
        )
    elif behavior == "nr_capacity_coverage_preference" and success:
        nr_bands = _bands(target_shape.get("nr_bands") or ())
        mode = _nr_mode(nr_bands)
        updated = replace(
            updated,
            nr_lock_mode=mode,
            nr_lock_set_at=result_ts if mode else 0.0,
            nr_bad_sinr_observations=0,
            nr_last_observation_key="",
        )
    return updated, result


def observe_normal_behavior_progress(
    progress: NormalBehaviorProgress,
    *,
    radio: Mapping[str, Any],
    quality: Mapping[str, Any],
    now: float,
) -> NormalBehaviorProgress:
    """Project accepted observed state without constructing an action."""

    if not isinstance(progress, NormalBehaviorProgress):
        raise TypeError("normal_behavior_progress_required")
    updated = progress
    anchor = progress.anchor
    if anchor is not None and anchor.status == "pending" and now >= anchor.verify_at:
        current_band = _int(
            radio.get("serving_band", radio.get("primary_band"))
        )
        if current_band > 0:
            anchor_status = (
                "held" if current_band == anchor.target_band else "reverted"
            )
            family = str(anchor.backend_family or "").lower()
            hilink = any(
                token in family for token in ("huawei", "hilink", "b818")
            )
            routeros = any(
                token in family for token in ("routeros", "mikrotik")
            )
            if anchor_status == "held":
                hold_s = 7200.0 if routeros else 3600.0 if hilink else 1200.0
            else:
                hold_s = 7200.0 if (hilink or routeros) else 1200.0
            pacing = dict(updated.behavior_next_allowed_ts)
            pacing["band_priority_rotation"] = max(
                float(pacing.get("band_priority_rotation", 0.0)),
                float(now) + hold_s,
            )
            updated = replace(
                updated,
                behavior_next_allowed_ts=pacing,
                anchor=replace(
                    anchor,
                    status=anchor_status,
                    actual_band=current_band,
                    observed_at=float(now),
                ),
            )

    if not updated.nr_lock_mode:
        return updated
    observation_key = str(
        radio.get("source_generation")
        or radio.get("generation")
        or radio.get("sample_ts")
        or ""
    )
    if not observation_key or observation_key == updated.nr_last_observation_key:
        return updated
    if quality.get("nr_just_dropped") is True or quality.get("nr_recovering") is True:
        return replace(updated, nr_last_observation_key=observation_key)
    nsa_active = radio.get("nsa_active") is True
    nr_sinr = _optional_float(radio.get("nr_sinr_db"))
    bad = bool(nsa_active and nr_sinr is not None and nr_sinr < 0.0)
    return replace(
        updated,
        nr_bad_sinr_observations=(
            updated.nr_bad_sinr_observations + 1 if bad else 0
        ),
        nr_last_observation_key=observation_key,
    )


def mark_anchor_learning_projected(
    progress: NormalBehaviorProgress,
    *,
    command_id: str,
) -> NormalBehaviorProgress:
    """Complete one idempotent anchor-observation projection outbox item."""

    if not isinstance(progress, NormalBehaviorProgress):
        raise TypeError("normal_behavior_progress_required")
    anchor = progress.anchor
    if anchor is None or anchor.command_id != str(command_id or ""):
        raise ValueError("normal_behavior_anchor_projection_identity_mismatch")
    if anchor.status not in {"held", "reverted"}:
        raise ValueError("normal_behavior_anchor_projection_not_observed")
    if anchor.learning_projected:
        return progress
    return replace(
        progress,
        anchor=replace(anchor, learning_projected=True),
    )


def _target_band(target_shape: Mapping[str, Any]) -> int:
    pband = _int(target_shape.get("pband"))
    if pband > 0:
        return pband
    bands = _bands(target_shape.get("bands") or ())
    return bands[0] if bands else 0


def _nr_mode(bands: tuple[int, ...]) -> str:
    values = set(bands)
    if not values:
        return ""
    if values == {28}:
        return "coverage"
    if 28 in values and values & {7, 78}:
        return "all"
    if values & {7, 78}:
        return "capacity"
    return ""


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _json_value(item) for key, item in value.items()}


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _json_mapping(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


def _bands(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted({_int(item) for item in value if _int(item) > 0}))


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


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    parsed = _float(value, float("nan"))
    return parsed if math.isfinite(parsed) else None


__all__ = [
    "AnchorRotationProgress",
    "NormalBehaviorProgress",
    "NormalBehaviorResult",
    "observe_normal_behavior_progress",
    "mark_anchor_learning_projected",
    "project_normal_behavior_terminal",
]
