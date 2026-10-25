"""Interactive antenna guide. It reads accepted Core observations but never changes the
modem. The workflow and tuning rules work without optional Intelligence.
"""

from __future__ import annotations

import copy
import math
from threading import RLock
from time import time
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.config_views import build_signal_contract_config
from arch4.core.accepted_root_outbox import (
    require_core_accepted_root_outbox,
)
from arch4.core_contracts.accepted_generation_root import (
    CoreAcceptedGenerationRoot,
)
from arch4.core_contracts.accepted_current_facts import CoreAcceptedCurrentFacts
from arch4.core_contracts.observations import CoreObservationFrame
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.runtime import CoreRuntimePort
from arch4.foundation.runtime_artifacts import core_current_line_values
from arch4.operational_service_contracts import (
    AntennaGuideControlRequest,
    OperationalDisposition,
    OperationalKind,
    OperationalServiceReceipt,
    ProductOperationalEffectPermit,
    require_product_operational_effect_permit,
)


OWNER = "54guc.foundation.antenna_guide"
FOUNDATION_ANTENNA_GUIDE_CLIENT_ID = "54guc.foundation.antenna_guide.observation"
STATE_SCHEMA = "FoundationAntennaGuideState.v2"
SESSION_SCHEMA = "FoundationAntennaGuideSession.v2"
FORMULA_EPOCH = "dual_radio_available_inputs_no_ca_v1"
_LANES = ("RF", "RI", "CA", "LAT")
_ZERO_DELTAS = {lane: 0 for lane in _LANES}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _required_positive_sample_ts(sample: Mapping[str, Any]) -> float:
    row = dict(sample or {})
    value = row.get("sample_ts")
    if value in (None, ""):
        value = row.get("ts")
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        raise ValueError("antenna_guide_positive_sample_timestamp_required") from None
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValueError("antenna_guide_positive_sample_timestamp_required")
    return normalized


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _normalized_radio_context(
    context: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    row = dict(context or {})
    serving_available = bool(row.get("serving_band_available", True))
    primary_band = _int(row.get("primary_band"))
    nr = row.get("rat") == "NR"
    pci = row.get("pci")
    if type(pci) is not int or not 0 <= pci <= (1007 if nr else 503):
        return None
    earfcn = _int(row.get("earfcn"))
    if not serving_available or primary_band <= 0 or earfcn <= 0:
        return None
    result = {
        "primary_band": primary_band,
        "pci": pci,
        "earfcn": earfcn,
    }
    if nr:
        result["rat"] = "NR"
        if row.get("cell_id") is not None:
            result["cell_id"] = row["cell_id"]
    return result


def _clamp(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _clamp01(value: float) -> float:
    return _clamp(value, 0.0, 1.0)


def _role(value: int) -> str:
    if value > 1:
        return "positive"
    if value < -1:
        return "negative"
    return "neutral"


def _empty_session() -> dict[str, Any]:
    return {
        "schema": SESSION_SCHEMA,
        "owner": OWNER,
        "formula_epoch": FORMULA_EPOCH,
        "namespace": {},
        "context": {},
        "closed_segments": [],
        "last_source_sequence": 0,
        "last_sample_ts": 0.0,
        "baseline_samples": [],
        "movement_history": [],
        "baseline": {},
        "baseline_ready": False,
        "sample_count": 0,
        "deltas": dict(_ZERO_DELTAS),
        "score": 0,
    }


def _empty_state() -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "owner": OWNER,
        "active_action": "",
        "status": "idle",
        "command_status": "Antenna Guide ready.",
        "updated_at": 0.0,
        "session": _empty_session(),
        "nr_session": _empty_session(),
    }


def _sample_lanes(sample: Mapping[str, Any]) -> dict[str, float | None]:
    row = dict(sample or {})
    sinr = _float(row.get("sinr_db"))
    rsrp = _float(row.get("rsrp_dbm"))
    rsrq = _float(row.get("rsrq_db"))
    rf = (
        0.45 * _clamp01((sinr + 5.0) / 25.0)
        + 0.30 * _clamp01((rsrp + 120.0) / 60.0)
        + 0.25 * _clamp01((rsrq + 19.0) / 16.0)
    )
    ri_value = _int(row.get("ri"))
    ri = (
        (1.0 if ri_value >= 2 else 0.5)
        if row.get("ri_valid", ri_value > 0) and ri_value > 0 else None
    )
    ca_valid = bool(row.get("ca_stat_ld_valid", False))
    ca = (
        1.0
        if ca_valid and _float(row.get("ca_stat_ld_pct")) > 0.0
        else (0.0 if ca_valid else None)
    )
    rtt = _float(row.get("rtt_ms"))
    rtt_n = 0.5 if rtt <= 0.0 else 1.0 - _clamp01((rtt - 20.0) / 180.0)
    jitter_n = (
        1.0 - _clamp01(max(0.0, _float(row.get("rtt_dl_jitter_pct"))) / 50.0)
        if bool(row.get("rtt_dl_jitter_valid", False))
        else 0.5
    )
    return {
        "ts": _required_positive_sample_ts(row),
        "RF": float(rf),
        "RI": ri,
        "CA": ca,
        "LAT": (
            float(0.75 * rtt_n + 0.25 * jitter_n)
            if rtt > 0 or row.get("rtt_dl_jitter_valid", False) else None
        ),
    }


def update_antenna_guide_session(
    state: Mapping[str, Any] | None,
    *,
    sample: Mapping[str, Any],
    namespace: Mapping[str, Any],
    context: Mapping[str, Any],
    source_sequence: int,
) -> dict[str, Any]:
    """Apply one accepted Core generation to the antenna guide."""

    sequence = int(source_sequence)
    if sequence <= 0:
        raise ValueError("antenna_guide_positive_source_sequence_required")
    row = copy.deepcopy(dict(state or {}))
    if (
        row.get("schema") != SESSION_SCHEMA
        or row.get("owner") != OWNER
        or row.get("formula_epoch") != FORMULA_EPOCH
    ):
        row = _empty_session()
    normalized_namespace = {
        "identity_code": str(namespace.get("identity_code") or ""),
        "session_id": str(namespace.get("session_id") or ""),
        "source_epoch": str(namespace.get("source_epoch") or ""),
    }
    normalized_context = _normalized_radio_context(context)
    if not all(normalized_namespace.values()):
        raise ValueError("antenna_guide_observation_namespace_incomplete")
    if normalized_context is None:
        raise ValueError("antenna_guide_radio_context_unavailable")
    if row.get("namespace") != normalized_namespace:
        row = _empty_session()
        row["namespace"] = normalized_namespace
        row["context"] = normalized_context
    elif row.get("context") != normalized_context:
        closed = list(row.get("closed_segments") or [])
        if row.get("context") and _int(row.get("sample_count")) > 0:
            closed.append(
                {
                    "context": copy.deepcopy(dict(row.get("context") or {})),
                    "closed_at": _required_positive_sample_ts(sample),
                    "reason": "radio_context_changed",
                    "sample_count": _int(row.get("sample_count")),
                    "baseline_ready": bool(row.get("baseline_ready", False)),
                    "baseline": copy.deepcopy(dict(row.get("baseline") or {})),
                    "deltas": copy.deepcopy(dict(row.get("deltas") or {})),
                    "score": _int(row.get("score")),
                    "last_source_sequence": _int(row.get("last_source_sequence")),
                    "last_sample_ts": _float(row.get("last_sample_ts")),
                }
            )
        row = _empty_session()
        row["namespace"] = normalized_namespace
        row["context"] = normalized_context
        row["closed_segments"] = closed[-24:]
    if sequence <= _int(row.get("last_source_sequence")):
        return row

    lanes = _sample_lanes(sample)
    row["last_source_sequence"] = sequence
    row["last_sample_ts"] = float(lanes["ts"])
    row["sample_count"] = _int(row.get("sample_count")) + 1
    if not bool(row.get("baseline_ready", False)):
        samples = list(row.get("baseline_samples") or [])
        samples.append(lanes)
        samples = samples[-240:]
        row["baseline_samples"] = samples
        span = samples[-1]["ts"] - samples[0]["ts"] if len(samples) >= 2 else 0.0
        if len(samples) < 8 or span < 60.0:
            return row
        baseline = {}
        for lane in _LANES:
            available = [item for item in samples if item[lane] is not None]
            baseline[lane] = (
                sum(float(item[lane]) for item in available) / len(available)
                if len(available) >= 8 and available[-1]["ts"] - available[0]["ts"] >= 60.0
                else None
            )
        row["baseline"] = baseline
        row["baseline_ready"] = True
        row["movement_history"] = []

    baseline = dict(row.get("baseline") or {})
    scales = {
        "RF": (40.0, 20.0),
        "RI": (10.0, 8.0),
        "CA": (8.0, 6.0),
        "LAT": (8.0, 5.0),
    }
    deltas = {
        lane: int(
            round(
                _clamp(
                    (lanes[lane] - baseline[lane]) * scale,
                    -limit,
                    limit,
                )
            )
        ) if lanes[lane] is not None and baseline.get(lane) is not None else None
        for lane, (scale, limit) in scales.items()
    }
    # CA is informational. Use only this radio's qualified, referenced inputs.
    weights = {"RF": 0.55, "RI": 0.20, "LAT": 0.10}
    available_weights = {lane: weight for lane, weight in weights.items() if deltas[lane] is not None}
    combined = sum(deltas[lane] * weight for lane, weight in available_weights.items()) / sum(available_weights.values())
    history = list(row.get("movement_history") or [])
    history.append(float(combined))
    history = history[-10:]
    row["movement_history"] = history
    row["deltas"] = deltas
    row["score"] = int(round(_clamp(sum(history) / len(history), -20.0, 20.0)))
    return row


def antenna_guide_measurement(
    state: Mapping[str, Any] | None,
    *,
    active: bool,
    now: float | None = None,
) -> dict[str, Any]:
    if type(active) is not bool:
        raise TypeError("antenna_guide_active_state_required")
    row = dict(state or {})
    ts = float(time() if now is None else now)
    valid = bool(
        row.get("schema") == SESSION_SCHEMA
        and row.get("owner") == OWNER
        and row.get("formula_epoch") == FORMULA_EPOCH
    )
    live = bool(active and valid and 0.0 <= ts - _float(row.get("last_sample_ts")) <= 10.0)
    ready = bool(live and row.get("baseline_ready", False))
    score = _int(row.get("score")) if ready else 0
    deltas = dict(row.get("deltas") or {}) if ready else dict(_ZERO_DELTAS)
    contributors = {
        lane: {
            "value": _int(deltas.get(lane)),
            "text": (
                "----" if not live or (ready and deltas.get(lane) is None)
                else f"{_int(deltas.get(lane)):+d}" if ready else "0"
            ),
            "bar_pct": max(0, min(100, 50 + _int(deltas.get(lane)) * 2)),
            "role": _role(_int(deltas.get(lane))),
            "available": bool(ready and deltas.get(lane) is not None),
            "scored": bool(ready and lane != "CA" and deltas.get(lane) is not None),
        }
        for lane in _LANES
    }
    confidence_pct = int(
        round(
            100.0
            * _clamp01(
                0.70 * _clamp01(_int(row.get("sample_count")) / 60.0)
                + 0.10 * (1.0 if row.get("baseline_ready", False) else 0.45)
                + 0.20 * (1.0 if live else 0.0)
            )
        )
    )
    confidence_text = (
        "Samples: enough"
        if confidence_pct >= 85
        else ("Samples: building" if confidence_pct >= 50 else "Samples: starting")
    )
    if not active:
        confidence_pct, confidence_text = 0, "Samples: none"
        score_text, score_role = "N/A", "neutral"
        guidance, trend, trend_role, meter_pct = (
            "Press Start Guide, then hold the first position for 60 seconds.",
            "Guide idle", "neutral", 0,
        )
    elif not live:
        score_text, score_role = "N/A", "neutral"
        guidance, trend, trend_role, meter_pct = (
            "Waiting for a usable modem reading. Normal monitoring continues.",
            "Waiting for modem data",
            "neutral",
            0,
        )
    elif not ready:
        score_text, score_role = "0", "neutral"
        guidance, trend, trend_role, meter_pct = (
            "Hold steady.",
            "Collecting baseline",
            "neutral",
            50,
        )
    else:
        score_text, score_role = f"{score:+d}", _role(score)
        meter_pct = max(0, min(100, 50 + score * 2))
        if score >= 4:
            guidance, trend, trend_role = "Keep steady.", "Better position", "positive"
        elif score >= 1:
            guidance, trend, trend_role = "Small move, then hold.", "A little better", "positive"
        elif score <= -4:
            guidance, trend, trend_role = "Move back, then hold.", "Getting worse", "negative"
        elif score <= -1:
            guidance, trend, trend_role = "Reverse last move.", "A little worse", "warning"
        else:
            guidance, trend, trend_role = "Hold steady.", "No clear change", "neutral"
    filled = max(0, min(10, int(round(meter_pct / 10.0))))
    return {
        "live": live,
        "baseline_ready": ready,
        "score": score,
        "score_text": score_text,
        "score_role": score_role,
        "confidence_pct": confidence_pct,
        "confidence_text": confidence_text,
        "guidance": guidance,
        "trend": trend,
        "trend_role": trend_role,
        "contributors": contributors,
        "meter_pct": meter_pct,
        "meter_segments": [step <= filled for step in range(10, 0, -1)],
        "closed_segment_count": len(list(row.get("closed_segments") or [])),
    }


class FoundationAntennaGuideService:
    """The only Foundation workflow and state owner for the Dashboard guide."""

    def __init__(self, core: CoreRuntimePort) -> None:
        if core is None:
            raise TypeError("foundation_antenna_guide_core_port_required")
        self._accepted_root_outbox = require_core_accepted_root_outbox(core)
        self._signal_contract = build_signal_contract_config(
            getattr(core, "cfg", None)
        )
        self._lock = RLock()
        self._state = _empty_state()

    def control(
        self,
        request: AntennaGuideControlRequest,
        *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt:
        if not isinstance(request, AntennaGuideControlRequest):
            raise TypeError("foundation_antenna_guide_typed_request_required")
        require_product_operational_effect_permit(
            permit,
            request,
            expected_kind=OperationalKind.ANTENNA_GUIDE,
        )
        action = request.action
        now = float(request.context.created_at)
        with self._lock:
            self._require_request_root(request)
            state = copy.deepcopy(self._state)
            if action == "stop":
                state["active_action"] = ""
                state["status"] = "idle"
                state["session"] = _empty_session()
                state["nr_session"] = _empty_session()
                message = "Antenna Guide stopped."
            else:
                state["active_action"] = "start"
                state["status"] = "collecting_reference"
                state["session"] = _empty_session()
                state["nr_session"] = _empty_session()
                message = (
                    "Antenna Guide started. Hold the antenna steady for "
                    "60 seconds."
                )
            state["command_status"] = message
            state["last_control_request_id"] = (
                request.context.request_id
            )
            state["last_control_action"] = request.action
            state["last_control_matrix_identity"] = (
                request.context.matrix_identity.as_dict()
            )
            state["updated_at"] = now
            self._commit_locked(state)
            return self._receipt(
                request,
                OperationalDisposition.CONFIRMED,
                message,
                now=now,
            )

    def reconcile_control(
        self,
        request: AntennaGuideControlRequest,
    ) -> OperationalServiceReceipt | None:
        state = self.state()
        matrix = state.get("last_control_matrix_identity")
        if (
            state.get("last_control_request_id")
            != request.context.request_id
            or state.get("last_control_action") != request.action
            or not isinstance(matrix, Mapping)
            or MatrixGenerationIdentity.from_mapping(matrix)
            != request.context.matrix_identity
        ):
            return None
        return self._receipt(
            request,
            OperationalDisposition.CONFIRMED,
            str(
                state.get("command_status")
                or "Antenna Guide control recovered."
            ),
            now=time(),
        )

    def service_once(self, *, now: float | None = None) -> dict[str, Any]:
        observed_at = float(time() if now is None else now)
        consumed = 0
        skipped = 0
        while True:
            record = self._accepted_root_outbox.claim_next(
                FOUNDATION_ANTENNA_GUIDE_CLIENT_ID
            )
            if record is None:
                break
            try:
                root = record.root
                if not isinstance(root, CoreAcceptedGenerationRoot):
                    raise TypeError(
                        "foundation_antenna_guide_accepted_root_required"
                    )
                frame = root.observation_frame
                rf = self._accepted_rf_values(frame, root.current_facts)
                nr_context, nr = self._accepted_nr_values(frame, root.current_facts)
                with self._lock:
                    state = copy.deepcopy(self._state)
                    lte_context = self._accepted_lte_context(frame)
                    applicable = lte_context is not None
                    if (rf is None and nr is None) or not applicable:
                        skipped += 1
                    if str(state.get("active_action") or ""):
                        changed_context = False
                        for key, context, values in (
                            ("session", lte_context, rf),
                            ("nr_session", nr_context if applicable else None, nr),
                        ):
                            prior = dict(state.get(key) or {})
                            previous_context = _normalized_radio_context(prior.get("context"))
                            changed = previous_context is not None and context != previous_context
                            changed_context = changed_context or changed
                            if context is None or (changed and values is None):
                                state[key] = _empty_session()
                            elif values is not None:
                                state[key] = self._updated_session(prior, frame, rf=values, context=context)
                            # A brief RF miss in the same cell uses the same 10s
                            # live limit for LTE and NR; it never invents a sample.
                        ready = any(state[key].get("baseline_ready", False) for key in ("session", "nr_session"))
                        state["status"] = "comparing" if ready else "collecting_reference"
                        state["command_status"] = (
                            "Band or cell changed. Collecting a new 60-second reference."
                            if changed_context else "Move slowly, then hold each position."
                            if ready else "Hold steady for the 60-second reference."
                        )
                    if applicable or state != self._state:
                        state["updated_at"] = observed_at
                        self._commit_locked(state)
                self._accepted_root_outbox.acknowledge(
                    FOUNDATION_ANTENNA_GUIDE_CLIENT_ID,
                    key=record.key,
                )
                consumed += 1
            except Exception as exc:
                try:
                    self._accepted_root_outbox.release(
                        FOUNDATION_ANTENNA_GUIDE_CLIENT_ID,
                        key=record.key,
                        error=exc,
                    )
                except Exception as recovery_error:
                    raise RuntimeError(
                        "foundation_antenna_guide_observation_release_failed:"
                        f"{type(recovery_error).__name__}:{recovery_error}"
                    ) from exc
                raise
        return {
            "owner": OWNER,
            "status": str(self.surface(now=observed_at)["antenna_guide.status"]),
            "observations_consumed": consumed,
            "observations_skipped": skipped,
        }

    def surface(self, *, now: float | None = None) -> dict[str, Any]:
        observed_at = float(time() if now is None else now)
        with self._lock:
            state = copy.deepcopy(self._state)
        session = dict(state.get("session") or {})
        active = str(state.get("active_action") or "")
        explicit_measurement = antenna_guide_measurement(
            session, active=bool(active), now=observed_at
        )
        measurement = explicit_measurement
        nr_measurement = antenna_guide_measurement(
            state.get("nr_session"), active=bool(active), now=observed_at
        )
        contributors = dict(measurement["contributors"])
        contributors.update({f"NR_{lane}": nr_measurement["contributors"][lane] for lane in ("RF", "RI", "LAT")})
        def comparison_label(item: Mapping[str, Any]) -> str:
            if not item["live"]:
                return "unavailable"
            if not item["baseline_ready"]:
                return "reference"
            return "better" if item["score"] > 0 else "worse" if item["score"] < 0 else "steady"
        trend = (
            f"LTE: {comparison_label(measurement)}  |  5G: {comparison_label(nr_measurement)}"
            if active else measurement["trend"]
        )
        trend_role = (
            measurement["trend_role"] if measurement["trend_role"] == nr_measurement["trend_role"] else "neutral"
        )
        context = dict(session.get("context") or {})
        label = ""
        normalized_context = _normalized_radio_context(context)
        if normalized_context is not None:
            label = (
                f"B{_int(normalized_context.get('primary_band'))} "
                f"PCI {_int(normalized_context.get('pci'))}"
            ).strip()
        status = str(state.get("status") or "idle")
        baseline_samples = list(session.get("baseline_samples") or [])
        span = (
            _float(baseline_samples[-1].get("ts"))
            - _float(baseline_samples[0].get("ts"))
            if len(baseline_samples) >= 2
            else 0.0
        )
        baseline_progress = min(
            min(100.0, len(baseline_samples) / 8.0 * 100.0),
            min(100.0, span / 60.0 * 100.0),
        )
        hint = (
            "Move slowly, then hold each position for 20-30 seconds."
            if active and measurement["baseline_ready"]
            else "Hold the antenna steady while the 60-second reference is collected."
            if active
            else "Press Start Guide, then hold the first position for 60 seconds."
        )
        return {
            "antenna_guide.status": status,
            "antenna_guide.guidance": measurement["guidance"],
            "antenna_guide.active_action": active,
            "antenna_guide.actions_enabled": True,
            "antenna_guide.antenna_score_text": measurement["score_text"],
            "antenna_guide.antenna_score_role": measurement["score_role"],
            "antenna_guide.nr_score_text": nr_measurement["score_text"],
            "antenna_guide.nr_score_role": nr_measurement["score_role"],
            "antenna_guide.confidence": f"LTE {measurement['confidence_text']}",
            "antenna_guide.trend": trend,
            "antenna_guide.antenna_trend_role": trend_role,
            "antenna_guide.hint": hint,
            "antenna_guide.monitor_state": (
                f"{active}: {status}" if active else status.capitalize()
            ),
            "antenna_guide.contributors": contributors,
            "antenna_guide.antenna_meter_segments": measurement["meter_segments"],
            "antenna_guide.current_check": status,
            "antenna_guide.saved_setup": (
                label if explicit_measurement["baseline_ready"] else "Not saved"
            ),
            "antenna_guide.new_result": trend,
            "antenna_guide.saved_setup_ready": explicit_measurement[
                "baseline_ready"
            ],
            "antenna_guide.action": active or "none",
            "antenna_guide.check_status": status,
            "antenna_guide.check_progress": (
                measurement["meter_pct"]
                if measurement["baseline_ready"]
                else baseline_progress
            ),
            "antenna_guide.next_step": hint,
            "antenna_guide.check_trust": measurement["confidence_text"],
            "antenna_guide.summary": measurement["guidance"],
            "antenna_guide.sinr_scale": self._signal_contract.sinr_scale,
            "antenna_guide.rsrp_scale": self._signal_contract.rsrp_scale,
            "antenna_guide.rsrq_scale": self._signal_contract.rsrq_scale,
            "antenna_guide.rssi_scale": self._signal_contract.rssi_scale,
            "command.antenna_guide_status": str(
                state.get("command_status") or "Antenna Guide ready."
            ),
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def _updated_session(
        self,
        current: Mapping[str, Any] | None,
        frame: CoreObservationFrame,
        *,
        rf: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(frame, CoreObservationFrame):
            raise TypeError("foundation_antenna_guide_core_observation_required")
        derived = frame.derived_facts.to_mapping()
        computed = derived.get("signal_compute")
        computed = dict(computed) if isinstance(computed, Mapping) else {}
        matrix_rtt = frame.matrix_rtt
        if matrix_rtt.matrix_identity != frame.matrix_identity:
            raise RuntimeError(
                "foundation_antenna_guide_matrix_rtt_identity_mismatch"
            )
        rtt = matrix_rtt.observation
        radio = derived.get("radio_target_facts")
        radio = dict(radio) if isinstance(radio, Mapping) else {}

        def fact(name: str, default: Any = 0.0) -> Any:
            return computed.get(name, default)

        jitter = fact("rtt_dl_jitter_pct", 0.0)
        jitter_valid = fact("rtt_dl_jitter_valid", jitter is not None)
        rtt_value = (
            rtt.value_ms
            if rtt.freshness == "fresh" and rtt.probe_status == "ok"
            else None
        )
        sample = {
            "sample_ts": frame.sampled_at,
            "sinr_db": rf["sinr_db"],
            "rsrp_dbm": rf["rsrp_dbm"],
            "rsrq_db": rf["rsrq_db"],
            "ri": rf.get("ri"),
            "ri_valid": rf.get("ri_valid", False),
            "ca_stat_ld_pct": fact("ca_stat_ld_pct", 0.0),
            "ca_stat_ld_valid": bool(fact("ca_stat_ld_valid", False)),
            "rtt_ms": rtt_value,
            "rtt_dl_jitter_pct": jitter,
            "rtt_dl_jitter_valid": bool(jitter_valid),
        }
        return update_antenna_guide_session(
            current,
            sample=sample,
            namespace={
                "identity_code": frame.identity_code,
                "session_id": frame.session_id,
                "source_epoch": frame.source_epoch,
            },
            context=context if context is not None else {
                "primary_band": radio.get("primary_band"),
                "pci": radio.get("pci"),
                "earfcn": radio.get("earfcn"),
                "serving_band_available": radio.get(
                    "serving_band_available"
                ),
            },
            source_sequence=frame.source_sequence,
        )

    @staticmethod
    def _accepted_rf_values(
        frame: CoreObservationFrame,
        current_facts: CoreAcceptedCurrentFacts,
    ) -> dict[str, Any] | None:
        """Read LTE RF/rank from the same accepted root, never ComputedSignals."""
        if not isinstance(current_facts, CoreAcceptedCurrentFacts):
            raise TypeError("foundation_antenna_guide_current_facts_required")
        if current_facts.matrix_identity != frame.matrix_identity:
            raise RuntimeError("foundation_antenna_guide_current_facts_identity_mismatch")
        rf = core_current_line_values(current_facts, "rf")
        for name in ("sinr_db", "rsrp_dbm", "rsrq_db"):
            fact = current_facts.exact(f"rf.{name}")
            if name not in rf or not 0.0 <= frame.sampled_at - fact.acquired_at <= 10.0:
                return None
        rf["ri_valid"] = bool(
            rf.get("ri_valid") is True and _int(rf.get("ri")) > 0
            and all(0 <= frame.sampled_at - current_facts.exact(f"rf.{name}").acquired_at <= 10 for name in ("ri", "ri_valid"))
        )
        return rf

    @staticmethod
    def _accepted_nr_values(
        frame: CoreObservationFrame,
        current_facts: CoreAcceptedCurrentFacts,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if current_facts.matrix_identity != frame.matrix_identity:
            raise RuntimeError("foundation_antenna_guide_current_facts_identity_mismatch")
        radio = core_current_line_values(current_facts, "radio")
        required = ("nr_mode", "nr_band", "nr_pci", "nr_arfcn")
        if radio.get("nr_mode") != "NSA":
            return None, None
        if any(name not in radio or not 0 <= frame.sampled_at - current_facts.exact(f"radio.{name}").acquired_at <= 10 for name in required):
            return None, None
        context = _normalized_radio_context({
            "rat": "NR", "primary_band": radio["nr_band"], "pci": radio["nr_pci"],
            "earfcn": radio["nr_arfcn"], "cell_id": radio.get("nr_cell_id"),
        })
        if context is None:
            return None, None
        if "nr_cell_id" in radio and not 0 <= frame.sampled_at - current_facts.exact("radio.nr_cell_id").acquired_at <= 10:
            return None, None
        if any(name not in radio or not 0 <= frame.sampled_at - current_facts.exact(f"radio.{name}").acquired_at <= 10 for name in ("nr_sinr_db", "nr_rsrp_dbm", "nr_rsrq_db")):
            return context, None
        result = dict(radio)
        result.update({name: radio[f"nr_{name}"] for name in ("sinr_db", "rsrp_dbm", "rsrq_db")})
        result["ri"] = radio.get("nr_ri")
        result["ri_valid"] = bool(
            _int(result["ri"]) > 0
            and 0 <= frame.sampled_at - current_facts.exact("radio.nr_ri").acquired_at <= 10
        )
        return context, result

    @staticmethod
    def _accepted_lte_context(frame: CoreObservationFrame) -> dict[str, Any] | None:
        if not isinstance(frame, CoreObservationFrame):
            raise TypeError("foundation_antenna_guide_core_observation_required")
        derived = frame.derived_facts.to_mapping()
        radio = derived.get("radio_target_facts")
        radio = dict(radio) if isinstance(radio, Mapping) else {}
        return (
            _normalized_radio_context(
                {
                    "primary_band": radio.get("primary_band"),
                    "pci": radio.get("pci"),
                    "earfcn": radio.get("earfcn"),
                    "serving_band_available": radio.get(
                        "serving_band_available"
                    ),
                }
            ) if frame.radio_connected else None
        )

    def _receipt(
        self,
        request: AntennaGuideControlRequest,
        disposition: OperationalDisposition,
        message: str,
        *,
        now: float,
    ) -> OperationalServiceReceipt:
        state = self.state()
        detail = self.surface(now=now)
        if disposition is OperationalDisposition.REJECTED:
            detail["reason"] = message
        else:
            if (
                state.get("last_control_request_id")
                != request.context.request_id
                or state.get("last_control_action") != request.action
            ):
                raise RuntimeError(
                    "foundation_antenna_guide_effect_proof_missing"
                )
            detail.update(
                operational_request_id=request.context.request_id,
                operational_action=request.action,
                state_content_sha256=CoreFrozenPayload.from_mapping(
                    state
                ).sha256,
            )
        return OperationalServiceReceipt(
            request_id=request.context.request_id,
            kind=OperationalKind.ANTENNA_GUIDE,
            owner=OWNER,
            disposition=disposition,
            message=message,
            occurred_at=now,
            runtime_namespace=request.context.runtime_namespace,
            matrix_identity=request.context.matrix_identity,
            detail=CoreFrozenPayload.from_mapping(detail),
        )

    def _require_request_root(
        self,
        request: AntennaGuideControlRequest,
    ) -> CoreAcceptedGenerationRoot:
        root = self._accepted_root_outbox.current()
        if not isinstance(root, CoreAcceptedGenerationRoot):
            raise RuntimeError(
                "foundation_antenna_guide_accepted_root_unavailable"
            )
        if root.matrix_identity.namespace != request.context.runtime_namespace:
            raise RuntimeError(
                "foundation_antenna_guide_matrix_identity_not_active"
            )
        return root

    def _commit_locked(
        self,
        state: Mapping[str, Any],
    ) -> None:
        candidate = copy.deepcopy(dict(state))
        candidate["schema"] = STATE_SCHEMA
        candidate["owner"] = OWNER
        self._state = candidate


__all__ = (
    "FORMULA_EPOCH",
    "FOUNDATION_ANTENNA_GUIDE_CLIENT_ID",
    "FoundationAntennaGuideService",
    "OWNER",
    "SESSION_SCHEMA",
    "STATE_SCHEMA",
    "antenna_guide_measurement",
    "update_antenna_guide_session",
)
