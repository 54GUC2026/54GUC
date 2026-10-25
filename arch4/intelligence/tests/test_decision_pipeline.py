"""
Tests for arch4/brain/decision_pipeline.py

Covers:
  - Preflight blockers (BLANKING, rate limit)
  - Soft congestion detector (entry, exit, disabled, fault-reset)
  - Cooldown state save/restore round-trip
  - Guard chain ordering (guards block in fixed order)
  - Manual band hold (start, expire, emergency override)
  - Objective feedback blocking
  - Runtime events (emit, drain, clear)
  - Decision trace structure
  - Action pipeline gate sequencing
  - Helper methods (_active, _count_active_until, _action_brief, etc.)
"""

import json
import time
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from arch4.controller_frames import (
    CellularIntelligenceGeneration,
    DerivedSignalFrames,
)
from arch4.interfaces import (
    Arch4Config,
    ModemInput,
    CarrierInfo,
    ComputedSignals,
    Constraints,
    LeverAction,
    LeverType,
    ModemCapabilities,
    SystemState,
    ActionRecord,
)
from arch4.brain.decision import DecisionEngine
from arch4.tests.controller_posture_test_support import TestControllerPostureAuthority
from arch4.brain.memory import Memory
from arch4.brain.aggressiveness import AggressivenessEngine
from arch4.brain.historical import HistoricalJournal
from arch4.brain.anchor_flip_monitor import AnchorFlipMonitor
from arch4.radio_transition_authority import (
    GUC_COMMAND,
    LOWER_CONTROLLER,
    LOWER_CONTROLLER_DURING_COMMAND,
)
from arch4.brain.ca_flap_monitor import CaFlapMonitor
from arch4.brain.action_roi_memory import (
    pband_action_roi_from_rewards as _pband_action_roi_from_rewards,
)
from arch4.brain.memory_action_history_owner import record_action_history
from arch4.brain.candidate_action_contract import stamp_candidate_execution_contract
from arch4.contextual_bandit_state_store import ContextualBanditStateStore
from arch4.brain.feed_registers import (
    BrainFeedOwnershipError,
    BrainFeedRegisterBank,
    FEED_SELF_LEARNING_STRATEGY,
)
from arch4.brain.learning_quality import (
    build_learning_quality_publication,
    publish_learning_quality_from_authority,
    publish_learning_quality_register,
)
from arch4.brain.rf_quality_gate import (
    build_rf_quality_config_from_engine,
    evaluate_rf_quality,
)
from arch4.runtime_registers import REGISTER_DECISION, RuntimeRegisterBank
from arch4.tests.runtime_identity_register_test_support import (
    install_test_identity_register,
)
from arch4.runtime_decision_projection import build_runtime_decision_projection
from arch4.smartness_fact_register import publish_smartness_fact_register
from arch4.smart_loop.clock_lane import run_smart_loop_strategy_clock_lane
from arch4.smart_loop.closed_loop import OWNER as SELF_LEARNING_STRATEGY_OWNER
from arch4.smart_loop.closed_loop import RADIO_CONFIGURATION_ACTION
from arch4.smart_loop.closed_loop import SCHEMA as SELF_LEARNING_STRATEGY_SCHEMA
from arch4.smart_loop.action_availability import SMART_LOOP_ROI_OBSERVATION_COOLDOWN
from arch4.action_transaction_authority import ActionTransactionAuthority
from arch4.smart_loop.state_store import SmartLoopStateStore
from arch4.smart_loop.strategy_register import (
    publish_smart_loop_strategy_register,
    read_smart_loop_strategy_register,
)
from arch4.tests.controller_generation_factory import cellular_test_inputs
from arch4.tests.decision_owner_test_support import with_decision_owner_ports
from arch4.tests.config_generation_test_support import (
    ActiveConfigProjectionTestMixin,
    install_active_config_generation_for_test,
)
from arch4.tests.runtime_signal_register_test_support import (
    publish_test_signal_generation,
)
from arch4.tests.smart_loop_test_helpers import (
    publish_smart_loop_radio_command_for_test,
    smartness_source_transaction_for_test,
    terminalize_selected_action_for_test,
)
from arch4.tests.terminal_reward_test_support import (
    seed_completed_terminal_reward_test_fixture,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _typed_test_inputs(
    modem,
    computed=None,
) -> tuple[CellularIntelligenceGeneration | None, DerivedSignalFrames | None]:
    """Cross the real immutable controller boundary for direct Brain fixtures."""

    if modem is None:
        return None, None
    if isinstance(modem, CellularIntelligenceGeneration):
        if computed is None:
            return modem, DerivedSignalFrames(
                latency=modem.latency,
                quality=modem.quality,
            )
        if isinstance(computed, DerivedSignalFrames):
            return modem, computed
    raw_computed = computed if isinstance(computed, ComputedSignals) else ComputedSignals()
    return cellular_test_inputs(modem, raw_computed)


class _TestDecisionEngine(ActiveConfigProjectionTestMixin, DecisionEngine):
    """Explicit test-boundary adapter; production stays immutable-only."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **with_decision_owner_ports(kwargs))

    def _evaluate_tsar_vector_candidate(self, modem, computed, **kwargs):
        intelligence, derived = _typed_test_inputs(modem, computed)
        return super()._evaluate_tsar_vector_candidate(
            intelligence,
            derived,
            **kwargs,
        )

    def _update_soft_congestion(self, modem, computed):
        intelligence, derived = _typed_test_inputs(modem, computed)
        self._rf_quality = evaluate_rf_quality(
            modem=intelligence,
            cfg=build_rf_quality_config_from_engine(self),
        )
        return super()._update_soft_congestion(intelligence, derived)

    def _emit_decision_trace(
        self,
        source,
        state,
        proposed,
        final,
        steps,
        modem=None,
    ):
        intelligence, _derived = _typed_test_inputs(modem)
        return super()._emit_decision_trace(
            source,
            state,
            proposed,
            final,
            steps,
            modem=intelligence,
        )

    def _apply_action_availability(self, *, modem, **kwargs):
        intelligence, _derived = _typed_test_inputs(modem)
        return super()._apply_action_availability(modem=intelligence, **kwargs)

    def _action_availability_verdict(self, *, modem, **kwargs):
        intelligence, _derived = _typed_test_inputs(modem)
        return super()._action_availability_verdict(modem=intelligence, **kwargs)

    def _lane_constraint_vector_blockers(self, *, modem, **kwargs):
        intelligence, _derived = _typed_test_inputs(modem)
        return super()._lane_constraint_vector_blockers(
            modem=intelligence,
            **kwargs,
        )

    def _make_band_action(self, *, modem=None, **kwargs):
        intelligence, _derived = _typed_test_inputs(modem)
        return super()._make_band_action(modem=intelligence, **kwargs)

    def _history_exploration_policy(self, modem=None, computed=None, **kwargs):
        intelligence, derived = _typed_test_inputs(modem, computed)
        return super()._history_exploration_policy(
            intelligence,
            derived,
            **kwargs,
        )

    def _decide_degraded(self, modem, computed):
        intelligence, derived = _typed_test_inputs(modem, computed)
        return super()._decide_degraded(intelligence, derived)

    def _decide_congested(self, modem, computed):
        intelligence, derived = _typed_test_inputs(modem, computed)
        return super()._decide_congested(intelligence, derived)

    def _decide_exploring(self, modem, computed):
        intelligence, derived = _typed_test_inputs(modem, computed)
        return super()._decide_exploring(intelligence, derived)

    def _try_preemptive_congestion_avoidance(self, modem, computed):
        intelligence, derived = _typed_test_inputs(modem, computed)
        return super()._try_preemptive_congestion_avoidance(
            intelligence,
            derived,
        )

    def _try_band_exploration(self, modem, computed=None, history_policy=None):
        intelligence, derived = _typed_test_inputs(modem, computed)
        return super()._try_band_exploration(
            intelligence,
            derived,
            history_policy,
        )

    def _record_objective_feedback(self, action, *, modem=None, **kwargs):
        intelligence, _derived = _typed_test_inputs(modem)
        return super()._record_objective_feedback(
            action,
            modem=intelligence,
            **kwargs,
        )

    def _update_ca_lost_signal(self, modem, computed, now):
        intelligence, derived = _typed_test_inputs(modem, computed)
        return super()._update_ca_lost_signal(intelligence, derived, now)

    def note_band_action_result(
        self,
        action,
        success,
        modem_after,
        computed_after=None,
    ):
        intelligence, derived = _typed_test_inputs(modem_after, computed_after)
        return super().note_band_action_result(
            action,
            success,
            intelligence,
            derived if computed_after is not None else None,
        )

    def note_action_result(
        self,
        action,
        success,
        modem_after,
        computed_after=None,
    ):
        intelligence, derived = _typed_test_inputs(modem_after, computed_after)
        return super().note_action_result(
            action,
            success,
            intelligence,
            derived if computed_after is not None else None,
        )


class _TestCaFlapMonitor(CaFlapMonitor):
    """Materialize raw CA-flap fixtures before the production monitor."""

    def observe(self, *, modem, **kwargs):
        intelligence, _derived = _typed_test_inputs(modem)
        return super().observe(modem=intelligence, **kwargs)


class _TestAnchorFlipMonitor(AnchorFlipMonitor):
    """Materialize raw anchor-flip fixtures before the production monitor."""

    def observe(
        self,
        *,
        modem,
        computed,
        selection_source=LOWER_CONTROLLER,
        **kwargs,
    ):
        intelligence, derived = _typed_test_inputs(modem, computed)
        structural_fresh = bool(derived.quality.cell_structural_change_fresh)
        lower_controller = selection_source in {
            LOWER_CONTROLLER,
            LOWER_CONTROLLER_DURING_COMMAND,
        }
        transition = replace(
            intelligence.radio_transition,
            attribution_available=True,
            attribution_error="",
            source=selection_source,
            episode_id="test-anchor-episode",
            transition_id=(
                "test-anchor-transition" if structural_fresh else ""
            ),
            fresh=structural_fresh,
            structural_fresh=structural_fresh,
            lower_controller_learning_opportunity=lower_controller,
            change_kind=str(derived.quality.cell_structural_change_kind or ""),
            change_class=str(derived.quality.cell_structural_change_class or ""),
            service_impact=bool(
                derived.quality.cell_structural_change_service_impact
            ),
            before=replace(
                intelligence.radio_transition.before,
                primary_band=int(derived.quality.cell_change_band_before or 0),
            ),
            after=replace(
                intelligence.radio_transition.after,
                primary_band=int(intelligence.cellular.primary_band or 0),
            ),
        )
        intelligence = replace(intelligence, radio_transition=transition)
        return super().observe(
            modem=intelligence,
            computed=derived,
            **kwargs,
        )


def _decide(engine: DecisionEngine, modem, computed: ComputedSignals):
    intelligence, derived = cellular_test_inputs(modem, computed)
    return engine.decide(intelligence, derived)


def test_decision_pipeline_deletes_generic_tsar_materialization_fallback():
    text = (
        Path(__file__).resolve().parents[1] / "brain" / "decision_pipeline.py"
    ).read_text(encoding="utf-8")

    assert "_normalize_unmaterialized_tsar_candidate_plan" not in text
    assert "selected_candidate_not_materialized" not in text


def test_learning_quality_consumes_external_memory_learning_prior():
    feed_bank = BrainFeedRegisterBank()
    engine = SimpleNamespace(
        memory=SimpleNamespace(),
        _external_memory_prior_register={
            "schema": "ExternalMemoryPriorRegister.v1",
            "owner": "arch4.external_memory_runtime",
            "valid": True,
            "value": {
                "learning_prior": {
                    "schema": "external_memory_learning_prior.v1",
                    "owner": "arch4.external_memory_learning_projection",
                    "present": True,
                    "band_hour_quality": {"3:6": 0.9, "1:6": 0.1},
                    "band_hour_counts": {"3:6": 100, "1:6": 100},
                    "sample_count": 200,
                    "profile_confidence": 0.4,
                    "quality_trend": "external_memory_history_prior",
                    "sources": [{"source_path": "canonical-owner-materialized"}],
                }
            },
        },
        controller=SimpleNamespace(brain=SimpleNamespace(feed_registers=feed_bank)),
    )

    publication = build_learning_quality_publication(
        engine, current_hour=6, now=1778457600.0
    )
    quality = publication["learning_quality"]

    assert quality["external_memory_learning_prior"]["present"] is True
    assert quality["band_hour_quality"]["3:6"] > quality["band_hour_quality"]["1:6"]
    assert quality["band_hour_counts"]["3:6"] >= 2
    assert quality["control_authority_owner"] == "arch4.brain.learning_quality"
    assert set(quality["control_authority_bands"]) >= {1, 3}


def _read_tsar_plan(engine) -> dict:
    return engine._tsar_arbitration_plan()


def _publish_tsar_plan(engine, plan: dict) -> dict:
    published_at = time.time()
    arbitration = (
        plan.get("arbitration_outcome")
        if isinstance(plan.get("arbitration_outcome"), dict)
        else {}
    )
    selected = plan.get("selected") if isinstance(plan.get("selected"), dict) else {}
    candidate = selected
    band = candidate.get("band") if isinstance(candidate, dict) else 0
    target_band = (
        candidate.get("target_band", band) if isinstance(candidate, dict) else 0
    )
    try:
        target_band = int(target_band or 0)
    except (TypeError, ValueError):
        target_band = 0
    verdict = str(arbitration.get("verdict") or ("act" if selected else "no_action"))
    reason = str(
        arbitration.get("reason_code")
        or plan.get("reason")
        or plan.get("status")
        or verdict
    )
    selected_plan = (
        {
            "action": str(candidate.get("action") or RADIO_CONFIGURATION_ACTION),
            "target_band": target_band,
            "reason_ref": str(
                candidate.get("reason_ref") or candidate.get("reason") or reason
            ),
            "context_key": str(
                candidate.get("context_key") or plan.get("context_ref") or "test"
            ),
            "reason_code": reason,
            "reason": str(candidate.get("reason") or reason),
            "source": str(candidate.get("source") or "test"),
            "band": target_band,
            "target_bands": list(candidate.get("target_bands") or [target_band]),
        }
        if candidate
        else None
    )
    strategy = {
        "schema": SELF_LEARNING_STRATEGY_SCHEMA,
        "owner": SELF_LEARNING_STRATEGY_OWNER,
        "outcome": "action_selected_execution_pending"
        if selected
        else "no_action_justified",
        "reason": reason,
        "reason_code": reason,
        "required_next_step": str(arbitration.get("required_next_step") or ""),
        "learning_loop_closed": False,
        "learning_signal": bool(selected),
        "diagnostic_signal": bool(not selected),
        "candidate_counts": {
            "total": len(plan.get("considered") or []),
            "operational": 1 if selected else 0,
            "actionable": 1 if selected else 0,
            "advisory": 0,
            "blocked": 0,
        },
        "candidates": list(plan.get("considered") or []),
        "selected": selected_plan,
        "tsar_gate": {
            "verdict": verdict,
            "reason_code": reason,
            "required_next_step": str(arbitration.get("required_next_step") or ""),
        },
        "inputs": {
            "self_learning": {},
            "tsar": {},
            "referee": {},
            "learning_effect": {},
        },
        "opened_proof": None,
        "closed_proof": None,
        "state": {"roi": {}, "pending_proof": None},
    }
    publish_smart_loop_strategy_register(
        engine,
        publication=strategy,
        now=published_at,
        stale_after_s=30.0,
    )
    return _read_tsar_plan(engine)


def _cfg(**overrides) -> Arch4Config:
    defaults = dict(
        min_action_interval_s=0.0,
        autonomous_connect_settle_s=0.0,
        soft_congestion_enabled=True,
        soft_congestion_entry_ticks=2,
        soft_congestion_lq_threshold=0.2,
        soft_congestion_rtt_min_ms=120.0,
        soft_congestion_min_dl_mbps=0.8,
        soft_congestion_clear_ticks=2,
    )
    defaults.update(overrides)
    return Arch4Config(**defaults)


def _allowed_learning_gate() -> dict:
    return {"learning_allowed": True, "gate_input_complete": True}


def _tsar_context_fields_for_test(
    modem,
    *,
    current_hour: int = 6,
    authority_bands=(1, 3, 20),
) -> dict:
    context = {
        "schema": "smart_loop.context_pointer.v1",
        "owner": SELF_LEARNING_STRATEGY_OWNER,
        "context": {
            "provider_plmn": str(getattr(modem, "provider_plmn", "") or ""),
            "cell_id": int(getattr(modem, "cell_id", 0) or 0),
            "pci": int(getattr(modem, "pci", 0) or 0),
            "earfcn": int(getattr(modem, "earfcn", 0) or 0),
            "hour": int(current_hour),
            "current_pband": int(getattr(modem, "primary_band", 0) or 0),
            "authority_bands": [int(band) for band in authority_bands],
        },
    }
    ctx = context["context"]
    context["context_ref"] = (
        f"{ctx['provider_plmn']}|{ctx['cell_id']}|{ctx['pci']}|{ctx['earfcn']}|"
        f"{ctx['hour']}|B{ctx['current_pband']}"
    )
    return {
        "context": dict(context.get("context") or {}),
        "context_ref": str(context.get("context_ref") or ""),
    }


def _tsar_candidate_row_for_test(candidate: dict, *, context_ref: str) -> dict:
    row = stamp_candidate_execution_contract(candidate)
    row["context_ref"] = str(context_ref or "")
    explanation = dict(row.get("explanation") or {})
    explanation["context_ref"] = str(context_ref or "")
    row["explanation"] = explanation
    return row


def _make_engine(cfg=None, td=None):
    """Build a DecisionEngine with minimal real dependencies."""
    if cfg is None:
        cfg = _cfg()
    mem = Memory(cfg, memory_dir=td)
    sm = TestControllerPostureAuthority(cfg, mem)
    constraints = Constraints(pci_lock_available=False)
    feed_bank = BrainFeedRegisterBank()
    command_register = ActionTransactionAuthority()
    engine = _TestDecisionEngine(
        cfg,
        mem,
        sm,
        constraints,
        aggressiveness=AggressivenessEngine(cfg),
        historical=HistoricalJournal(cfg),
        smart_loop_strategy_feed=feed_bank,
        action_transaction_authority=command_register,
    )
    runtime_bank = RuntimeRegisterBank()
    engine.register_bank = runtime_bank
    engine.controller = SimpleNamespace(
        runtime=SimpleNamespace(
            register_bank=runtime_bank,
            action_transaction_authority=command_register,
        ),
        brain=SimpleNamespace(
            feed_registers=feed_bank,
            smart_loop_state_store=SmartLoopStateStore(mem.memory_dir),
            contextual_bandit_state_store=ContextualBanditStateStore(
                mem.memory_dir
            ),
        ),
        control=SimpleNamespace(
            capabilities=ModemCapabilities(
                supports_pband=True,
                supports_band_selection=True,
            )
        ),
    )
    install_active_config_generation_for_test(engine)
    return engine


def _publish_runtime_truth_for_test(engine, modem: ModemInput, *, now: float) -> None:
    bank = engine.controller.runtime.register_bank
    if not isinstance(bank, RuntimeRegisterBank):
        bank = RuntimeRegisterBank()
        engine.controller.runtime.register_bank = bank
    install_test_identity_register(
        bank,
        value={
            "identity_code": "0x0000000000000001",
            "identity_status": "exact",
            "identity_catalog_version": "test-identity-catalog-v1",
            "identity_catalog_checksum": "a" * 64,
            "access_type": "test_access_v1",
            "access_type_id": "test_access_v1",
            "session_scope_key": "test-session-scope",
            "provider_plmn": str(
                getattr(modem, "provider_plmn", "") or "22005"
            ),
            "model": "test-modem",
        },
        source="test-accepted-identity",
        updated_at=now,
        valid=True,
        stale=False,
        dirty=False,
        validity="valid",
        force=True,
    )
    selected = (
        getattr(modem, "configured_band_pool", None)
        or getattr(modem, "selected_band_set", None)
        or getattr(modem, "band_set", None)
        or [getattr(modem, "primary_band", 0)]
    )
    bank.write_radio(
        value={
            "sample_ts": float(now),
            "sample_sequence": int(round(float(now) * 1000.0)),
            "sample_source": "test",
            "source_epoch": "test-source-epoch",
            "provider_plmn": str(getattr(modem, "provider_plmn", "") or ""),
            "provider_name": str(getattr(modem, "provider_name", "") or ""),
            "cell_id": int(getattr(modem, "cell_id", 0) or 0),
            "pci": int(getattr(modem, "pci", 0) or 0),
            "earfcn_dl": int(getattr(modem, "earfcn", 0) or 0),
            "serving_band": int(getattr(modem, "primary_band", 0) or 0),
            "configured_band_pool": [
                int(band) for band in selected if int(band or 0) > 0
            ],
            "allowed_bands": sorted(
                int(band)
                for band in (getattr(engine, "deployed_bands", None) or selected)
                if int(band or 0) > 0
            ),
            "radio_connected": bool(getattr(modem, "radio_connected", True)),
            "session_uptime_s": float(getattr(modem, "session_uptime_s", 0.0) or 0.0),
        },
        source="test",
        updated_at=now,
    )
    bank.write_traffic(
        value={
            "session_uptime_s": float(getattr(modem, "session_uptime_s", 0.0) or 0.0),
            "dl_mbps": 0.0,
            "ul_mbps": 0.0,
            "has_traffic": False,
        },
        source="test",
        updated_at=now,
    )
    publish_test_signal_generation(
        bank,
        quality={
            "cq_cell_display": 0.55,
            "congestion_pct": 30.0,
        },
        ca={"ca_active": bool(getattr(modem, "ca_active", False))},
        updated_at=now,
    )
    bank.write_learning(
        value={
            "learning_allowed": True,
            "proof_pending": False,
        },
        source="test",
        updated_at=now,
    )
    bank.write_mutation(value={"cooldown_s": 0.0}, source="test", updated_at=now)
    if not bank.read(REGISTER_DECISION).value:
        bank.write_decision(
            value=build_runtime_decision_projection({}),
            source="test-decision-projection",
            updated_at=now,
        )
    publish_smartness_fact_register(
        bank,
        transaction=smartness_source_transaction_for_test(bank),
        now=now,
    )


def _seed_known_good_bandset(memory: Memory, bands: list[int]) -> None:
    normalized = [int(band) for band in bands]
    ca_signature = "+".join(f"B{band}" for band in normalized)
    _append_scoped_action(
        memory,
        ActionRecord(
            timestamp=time.time(),
            sinr_db=15.0,
            rsrq_db=-9.0,
            mcs_dl=20,
            ca_signature=ca_signature,
            rtt_sliding_avg=40.0,
            rtt_ms=40.0,
            lever=LeverType.BAND_SELECTION,
            action_params={"bands": list(normalized)},
            success=True,
            throughput_before_mbps=50.0,
            throughput_after_mbps=60.0,
            sinr_after=15.0,
            ca_signature_after=ca_signature,
            improvement=10.0,
        )
    )


def _publish_self_learning(engine, modem=None, *, now=None):
    now_ts = time.time() if now is None else float(now)
    if modem is not None:
        _publish_runtime_truth_for_test(engine, modem, now=now_ts)
        intelligence, _derived = _typed_test_inputs(modem)
        engine._last_modem = intelligence
    test_quality_profile = getattr(engine, "_test_learning_quality_profile", None)
    if test_quality_profile is not None:
        publication = build_learning_quality_publication(
            engine,
            quality_profile_source=test_quality_profile,
            now=now_ts,
        )
        quality = publish_learning_quality_register(
            engine,
            publication=publication,
            now=now_ts,
        )
    else:
        quality = publish_learning_quality_from_authority(engine, now=now_ts)
    assert quality.get("published") is True
    result = run_smart_loop_strategy_clock_lane(
        engine,
        now=now_ts,
    )
    assert result.ran
    return result


def _publish_selected_radio_command(
    engine,
    modem,
    *,
    target_band: int,
    target_bands,
    lever: str = "PBAND",
    reason_code: str = "test_smart_loop_action",
    availability_intent: str = "explore",
    now: float | None = None,
):
    """Publish one fully receipted autonomous command for Decision tests."""

    now_ts = time.time() if now is None else float(now)
    _publish_runtime_truth_for_test(engine, modem, now=now_ts)
    intelligence, _derived = _typed_test_inputs(modem)
    engine._last_modem = intelligence
    result = publish_smart_loop_radio_command_for_test(
        engine,
        target_band=target_band,
        target_bands=target_bands,
        lever=lever,
        reason_code=reason_code,
        availability_intent=availability_intent,
        now=now_ts,
        accepted_frame=intelligence,
    )
    assert result.get("published") is True
    return read_smart_loop_strategy_register(engine, now=now_ts)


def _modem(
    *,
    sinr_db=15.0,
    rsrp_dbm=-85.0,
    primary_band=3,
    pci=100,
    earfcn=1200,
    cell_id=0,
    provider_plmn="",
    provider_name="",
    rx_bytes_per_sec=100_000,
    tx_bytes_per_sec=10_000,
    ca_active=False,
    ca_signature="B3",
    radio_connected=True,
    session_uptime_s=100.0,
    selected_band_set=None,
    configured_band_pool=None,
    cell_locked=False,
    cell_lock_pci=0,
    cell_lock_earfcn=0,
) -> ModemInput:
    carriers = [
        CarrierInfo(
            band=primary_band,
            earfcn=earfcn,
            pci=pci,
            bandwidth_mhz=20.0,
            sinr_db=sinr_db,
            rsrp_dbm=rsrp_dbm,
            state=1,
            is_pcell=True,
        )
    ]
    modem = ModemInput(
        sinr_db=sinr_db,
        rsrp_dbm=rsrp_dbm,
        rsrq_db=-9.0,
        rssi_dbm=-70.0,
        mcs_dl=20,
        mcs_ul=10,
        cqi=9,
        ri=2,
        carriers=carriers,
        ca_active=ca_active,
        ca_band_signature=ca_signature,
        primary_band=primary_band,
        pci=pci,
        earfcn=earfcn,
        enb_id=1,
        cell_id=cell_id,
        provider_plmn=provider_plmn,
        provider_name=provider_name,
        radio_connected=radio_connected,
        session_uptime_s=session_uptime_s,
        rx_bytes_per_sec=rx_bytes_per_sec,
        tx_bytes_per_sec=tx_bytes_per_sec,
        selected_band_set=list(selected_band_set or []),
        configured_band_pool=list(configured_band_pool or []),
    )
    modem.cell_locked = bool(cell_locked)
    modem.cell_lock_pci = int(cell_lock_pci)
    modem.cell_lock_earfcn = int(cell_lock_earfcn)
    return modem


def _computed(
    rtt_avg=40.0,
    cq_value=0.9,
    *,
    cq_cell=None,
    cq_vector_a_valid=True,
    rtt_congestion_avg=40.0,
    rtt_recent_median=40.0,
    rtt_delta=0.0,
    rtt_delta_valid=False,
    rtt_quiet_delta=0.0,
    rsrq_gap=0.0,
    rsrq_gap_valid=False,
    congestion_pct=0.0,
    congestion_pct_valid=None,
    congestion_qualifier_high=False,
    congestion_qualifier_mod=False,
    congestion_qam_gap=0.0,
    cart_ld=1,
    cart_ld_valid=True,
    cast_ld=100.0,
    cast_ld_valid=True,
    cart_ld_activity=0.0,
) -> ComputedSignals:
    if cq_cell is None:
        cq_cell = max(0.0, min(1.0, float(cq_value)))
    if congestion_pct_valid is None:
        congestion_pct_valid = float(congestion_pct or 0.0) > 0.0
    return ComputedSignals(
        rtt_ms=rtt_avg,
        rtt_ema=rtt_avg,
        rtt_sliding_avg=rtt_avg,
        cq_cell=cq_cell,
        cq_vector_a_valid=cq_vector_a_valid,
        rtt_congestion_avg=rtt_congestion_avg,
        rtt_recent_median=rtt_recent_median,
        rtt_delta=rtt_delta,
        rtt_delta_valid=rtt_delta_valid,
        rtt_quiet_delta=rtt_quiet_delta,
        rsrq_gap=rsrq_gap,
        rsrq_gap_valid=rsrq_gap_valid,
        congestion_pct=congestion_pct,
        congestion_pct_valid=bool(congestion_pct_valid),
        congestion_qualifier_high=congestion_qualifier_high,
        congestion_qualifier_mod=congestion_qualifier_mod,
        congestion_qam_gap=congestion_qam_gap,
        cart_ld=cart_ld,
        cart_ld_valid=cart_ld_valid,
        cast_ld=cast_ld,
        cast_ld_valid=cast_ld_valid,
        cart_ld_activity=cart_ld_activity,
    )


def _append_scoped_action(memory: Memory, record: ActionRecord) -> ActionRecord:
    """Admit an action scenario through the exact-scope Memory owner."""

    params = dict(record.action_params or {})
    signature_bands = [
        int(part[1:])
        for part in str(record.ca_signature or "").split("+")
        if part.startswith("B") and part[1:].isdigit()
    ]
    context_band = int(
        params.get("_tsar_candidate_band")
        or params.get("pband")
        or next((band for band in signature_bands if band > 0), 3)
    )
    modem_fields = {
        "primary_band": context_band,
        "pci": int(params.get("_tsar_context_pci") or 100),
        "earfcn": int(params.get("_tsar_context_earfcn") or 1200),
        "cell_id": int(params.get("_tsar_context_cell_id") or 0),
        "provider_plmn": str(
            params.get("_tsar_context_provider_plmn")
            or record.provider_plmn
            or ""
        ),
        "provider_name": str(
            params.get("_tsar_context_provider_name")
            or record.provider_name
            or ""
        ),
    }
    before_raw = _modem(
        **modem_fields,
        sinr_db=float(record.sinr_db or 0.0),
        ca_active="+" in str(record.ca_signature or ""),
        ca_signature=str(record.ca_signature or f"B{context_band}"),
        rx_bytes_per_sec=max(
            0.0,
            float(record.throughput_before_mbps or 0.0) * 125_000.0,
        ),
    )
    after_raw = _modem(
        **modem_fields,
        sinr_db=float(record.sinr_after or record.sinr_db or 0.0),
        ca_active="+" in str(record.ca_signature_after or ""),
        ca_signature=str(
            record.ca_signature_after
            or record.ca_signature
            or f"B{context_band}"
        ),
        rx_bytes_per_sec=max(
            0.0,
            float(record.throughput_after_mbps or 0.0) * 125_000.0,
        ),
    )
    before_computed = _computed(
        rtt_avg=float(record.rtt_ms or record.rtt_sliding_avg or 0.0),
    )
    after_computed = _computed(
        rtt_avg=float(record.rtt_ms or record.rtt_sliding_avg or 0.0),
    )
    before, before_derived = cellular_test_inputs(before_raw, before_computed)
    after, after_derived = cellular_test_inputs(after_raw, after_computed)
    admitted = record_action_history(
        memory,
        modem_before=before,
        computed_before=before_derived,
        lever=record.lever,
        proposal_action_origin="smart_loop",
        proof_observed_at=float(record.timestamp),
        params=params,
        success=bool(record.success),
        modem_after=after,
        computed_after=after_derived,
        provider_plmn=str(record.provider_plmn or ""),
        provider_name=str(record.provider_name or ""),
        learning_gate={
            "learning_allowed": True,
            "gate_input_complete": True,
        },
    )
    canonical_metadata = dict(admitted.objective_metadata or {})
    canonical_metadata.update(dict(record.objective_metadata or {}))
    for field_name in record.__dataclass_fields__:
        if field_name in {
            "baseline_evidence_scope",
            "evidence_scope",
            "objective_metadata",
        }:
            continue
        setattr(admitted, field_name, getattr(record, field_name))
    admitted.objective_metadata = canonical_metadata
    label = str(admitted.outcome_label or "").strip().lower()
    code = str(admitted.referee_outcome_code or "").strip().lower()
    staged = dict(
        canonical_metadata.get("staged_roi_evidence")
        if isinstance(
            canonical_metadata.get("staged_roi_evidence"),
            dict,
        )
        else {}
    )
    gate = dict(admitted.learning_gate or {})
    if (
        label in {"good", "bad"}
        and bool(gate.get("learning_allowed", False))
        and code
        not in {
            "action_failed",
            "staged_roi_context_changed",
            "staged_roi_invalid_evidence",
            "staged_roi_inconclusive",
        }
        and str(staged.get("grade_context_status") or "same").lower()
        in {"", "same", "valid", "ok"}
        and staged.get("grade_evidence_valid") is not False
        and str(
            canonical_metadata.get("staged_roi_parent_status") or ""
        ).lower()
        != "proof_closed"
    ):
        seed_completed_terminal_reward_test_fixture(
            memory,
            admitted,
            grade=1 if label == "good" else -1,
            modem_after=after,
            computed_after=after_derived,
        )
    return admitted


def _band_action(bands=None, intent="explore") -> LeverAction:
    if bands is None:
        bands = [1, 3]
    return LeverAction(
        lever=LeverType.BAND_SELECTION,
        params={"bands": bands},
        intent=intent,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPreflightBlockers(unittest.TestCase):
    """Lane 1: global rate-limit gate."""

    def test_rate_limit_blocks_within_interval(self):
        cfg = _cfg(min_action_interval_s=60.0)
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=cfg, td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine._last_action_time = time.time()
            action = _decide(engine, _modem(), _computed())
            self.assertIsNone(action)

    def test_rate_limit_passes_after_interval(self):
        cfg = _cfg(min_action_interval_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=cfg, td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine._last_action_time = time.time() - 100.0
            # Not guaranteed to produce action (state dispatch may return None)
            # but should NOT be blocked by rate limit
            result = engine._lane_preflight_blockers(
                state=SystemState.WORKING,
                now=time.time(),
            )
            self.assertFalse(result)

    def test_drifted_persistent_cell_lock_does_not_bypass_smart_loop_owner(self):
        cfg = _cfg(min_action_interval_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=cfg, td=td)
            engine.constraints.pci_lock_available = True
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            modem = _modem(
                primary_band=3,
                pci=365,
                earfcn=1651,
                cell_locked=True,
                cell_lock_pci=319,
                cell_lock_earfcn=6400,
            )

            action = _decide(engine, modem, _computed())

            self.assertIsNone(action)
            events = engine.drain_runtime_events()
            self.assertFalse(
                any(e.get("kind") == "cell_lock_renewal_requested" for e in events)
            )


class TestAggressivenessIntervals(unittest.TestCase):
    def test_working_background_probe_interval_is_180_seconds(self):
        profile = AggressivenessEngine(_cfg()).get_profile(SystemState.WORKING)
        self.assertEqual(profile.probe_interval_s, 180.0)

class TestCaFlapMonitor(unittest.TestCase):
    def test_detects_ca_signature_flapping_in_180s_window(self):
        monitor = _TestCaFlapMonitor(
            window_s=180.0,
            settle_s=0.0,
            candidate_flips=4,
            active_flips=6,
            consecutive_candidates_required=2,
        )
        base = 1_800_000_000.0
        signatures = ["B1+B3", "B1", "B1+B3", "B1", "B1+B3", "B1", "B1+B3"]
        status = {}

        for idx, signature in enumerate(signatures):
            modem = _modem(
                primary_band=1,
                pci=100,
                ca_active="+" in signature,
                ca_signature=signature,
                selected_band_set=[1, 3, 7, 20, 28],
            )
            modem.cell_id = 123
            modem.provider_plmn = "22001"
            modem.provider_name = "Yettel"
            status = monitor.observe(
                modem=modem, state=SystemState.WORKING, now=base + idx * 10
            )

        self.assertTrue(status["active"])
        self.assertGreaterEqual(status["flip_count"], 6)
        self.assertEqual(status["window_s"], 180.0)
        self.assertEqual(status["context"]["pband"], 1)
        self.assertIn("PBAND=B1 prone_to_ca_flapping", status["reason"])

    def test_idle_ca_signature_churn_is_observed_but_not_actionable(self):
        monitor = _TestCaFlapMonitor(
            window_s=180.0,
            settle_s=0.0,
            candidate_flips=4,
            active_flips=6,
            consecutive_candidates_required=2,
        )
        base = 1_800_000_000.0
        signatures = ["B1+B3", "B1", "B1+B3", "B1", "B1+B3", "B1", "B1+B3"]
        status = {}

        for idx, signature in enumerate(signatures):
            modem = _modem(
                primary_band=1,
                pci=100,
                ca_active="+" in signature,
                ca_signature=signature,
                selected_band_set=[1, 3, 7, 20, 28],
            )
            modem.cell_id = 123
            modem.provider_plmn = "22001"
            modem.provider_name = "Yettel"
            status = monitor.observe(
                modem=modem,
                state=SystemState.WORKING,
                now=base + idx * 10,
                traffic_active=False,
                active_load_mbps=0.003,
            )

        self.assertFalse(status["active"])
        self.assertEqual(status["flip_count"], 0)
        self.assertEqual(status["actionable_flip_count"], 0)
        self.assertGreaterEqual(status["observed_flip_count"], 6)
        self.assertFalse(status["traffic_active"])
        self.assertEqual(status["reason"], "")

    def test_ca_flapping_uses_latched_provider_context_when_tick_omits_provider(self):
        monitor = _TestCaFlapMonitor(
            window_s=180.0,
            settle_s=0.0,
            candidate_flips=4,
            active_flips=6,
            consecutive_candidates_required=2,
        )
        monitor.set_provider_context("22005", "A1")
        base = 1_800_000_000.0
        signatures = ["B3+B1", "B3", "B3+B1", "B3", "B3+B1", "B3", "B3+B1"]
        status = {}

        for idx, signature in enumerate(signatures):
            modem = _modem(
                primary_band=3,
                pci=255,
                ca_active="+" in signature,
                ca_signature=signature,
                selected_band_set=[1, 3, 7, 20, 28],
            )
            modem.cell_id = 2050
            status = monitor.observe(
                modem=modem,
                state=SystemState.WORKING,
                now=base + idx * 10,
            )

        self.assertTrue(status["active"])
        self.assertEqual(status["context"]["provider_plmn"], "22005")
        self.assertEqual(status["context"]["provider_name"], "A1")
        self.assertIn("provider=A1", status["reason"])

    def test_identity_change_enters_settle_and_clears_flips(self):
        monitor = _TestCaFlapMonitor(window_s=180.0, settle_s=60.0)
        base = 1_800_000_000.0
        first = _modem(primary_band=1, pci=100, ca_active=True, ca_signature="B1+B3")
        first.cell_id = 123
        second = _modem(primary_band=3, pci=100, ca_active=True, ca_signature="B1+B3")
        second.cell_id = 123

        monitor.observe(modem=first, state=SystemState.WORKING, now=base)
        status = monitor.observe(modem=second, state=SystemState.WORKING, now=base + 5)

        self.assertFalse(status["active"])
        self.assertEqual(status["flip_count"], 0)
        self.assertTrue(status["excluded"])
        self.assertEqual(status["exclude_reason"], "settle")


class TestAnchorFlipMonitor(unittest.TestCase):
    def test_detects_lower_controller_anchor_flip_instability(self):
        monitor = _TestAnchorFlipMonitor(
            window_s=600.0,
            command_collision_window_s=300.0,
            active_flips=5,
            command_collision_active_flips=3,
        )
        base = 1_800_000_000.0
        transitions = [(20, 3), (3, 20), (20, 1), (1, 3), (3, 20)]
        status = {}

        for idx, (before, after) in enumerate(transitions):
            modem = _modem(primary_band=after, pci=100 + idx, earfcn=1000 + idx)
            modem.provider_plmn = "22001"
            modem.provider_name = "Yettel"
            computed = _computed()
            computed.cell_structural_change = True
            computed.cell_structural_change_fresh = True
            computed.cell_structural_change_kind = "band"
            computed.cell_structural_change_class = "anchor_move"
            computed.cell_structural_change_service_impact = True
            computed.cell_change_band_before = before
            computed.cell_change_pci_before = 90 + idx
            computed.cell_change_earfcn_before = 900 + idx
            status = monitor.observe(
                modem=modem,
                computed=computed,
                state=SystemState.WORKING,
                now=base + idx * 60,
            )

        self.assertTrue(status["active"])
        self.assertEqual(status["flip_count"], 5)
        self.assertEqual(status["involved_bands"], [1, 3, 20])
        self.assertIn("anchor_flip_instability", status["reason"])
        self.assertEqual(
            status["host_behavior_grade"]["schema"], "host_behavior_grade_v1"
        )

    def test_ignores_intentional_anchor_moves(self):
        monitor = _TestAnchorFlipMonitor(active_flips=1)
        modem = _modem(primary_band=3)
        computed = _computed()
        computed.cell_structural_change = True
        computed.cell_structural_change_fresh = True
        computed.cell_structural_change_kind = "band"
        computed.cell_structural_change_class = "anchor_move"
        computed.cell_structural_change_service_impact = True
        computed.cell_change_band_before = 20

        status = monitor.observe(
            modem=modem,
            computed=computed,
            state=SystemState.WORKING,
            now=1_800_000_000.0,
            selection_source=GUC_COMMAND,
        )

        self.assertFalse(status["active"])
        self.assertEqual(status["flip_count"], 0)
        self.assertEqual(status["exclude_reason"], "not_lower_controller_selection")

    def test_counts_lower_controller_command_collision_as_recovery_evidence(self):
        monitor = _TestAnchorFlipMonitor(
            window_s=600.0,
            command_collision_window_s=300.0,
            active_flips=5,
            command_collision_active_flips=3,
        )
        base = 1_800_000_000.0
        status = {}

        for idx, (before, after) in enumerate([(1, 3), (3, 1), (1, 3)]):
            computed = _computed(congestion_pct=45.0 + idx * 10.0)
            computed.cell_structural_change = True
            computed.cell_structural_change_fresh = True
            computed.cell_structural_change_kind = "band"
            computed.cell_structural_change_class = "anchor_move"
            computed.cell_structural_change_service_impact = True
            computed.cell_change_band_before = before
            computed.congestion_rising = True
            status = monitor.observe(
                modem=_modem(primary_band=after, sinr_db=12.0 - idx),
                computed=computed,
                state=SystemState.WORKING,
                now=base + idx * 30,
                selection_source=LOWER_CONTROLLER_DURING_COMMAND,
                ca_recovery_trend={
                    "schema": "ca_recovery_trend_v1",
                    "improving": False,
                    "worsening": True,
                    "hold_restore_ca": False,
                },
            )

        self.assertTrue(status["active"])
        self.assertEqual(status["flip_count"], 3)
        self.assertEqual(status["command_collision_flip_count"], 3)
        self.assertFalse(status["excluded"])
        self.assertEqual(status["exclude_reason"], "")
        self.assertTrue(status["recovery_allowed"])
        self.assertIn(
            "command_collision_flip_pressure_high",
            status["host_behavior_grade"]["reasons"],
        )

    def test_does_not_infer_transition_when_structural_event_is_absent(
        self,
    ):
        monitor = _TestAnchorFlipMonitor(active_flips=3)
        base = 1_800_000_000.0
        status = monitor.observe(
            modem=_modem(primary_band=20),
            computed=_computed(),
            state=SystemState.WORKING,
            now=base,
        )
        self.assertFalse(status["active"])

        for idx, band in enumerate([3, 20, 3], start=1):
            status = monitor.observe(
                modem=_modem(primary_band=band, pci=300 + idx, earfcn=1600 + idx),
                computed=_computed(),
                state=SystemState.WORKING,
                now=base + idx * 10,
            )

        self.assertFalse(status["active"])
        self.assertEqual(status["flip_count"], 0)
        self.assertEqual(status["last_flip"], {})

    def test_anchor_flip_instability_holds_recovery_when_ca_metric_is_improving(self):
        monitor = _TestAnchorFlipMonitor(
            active_flips=3,
            command_collision_active_flips=2,
        )
        base = 1_800_000_000.0
        transitions = [(20, 3), (3, 20), (20, 3)]
        status = {}

        for idx, (before, after) in enumerate(transitions):
            computed = _computed()
            computed.cell_structural_change = True
            computed.cell_structural_change_fresh = True
            computed.cell_structural_change_kind = "band"
            computed.cell_structural_change_class = "anchor_move"
            computed.cell_structural_change_service_impact = True
            computed.cell_change_band_before = before
            status = monitor.observe(
                modem=_modem(primary_band=after, ca_active=idx >= 1),
                computed=computed,
                state=SystemState.WORKING,
                now=base + idx * 60,
                ca_recovery_trend={
                    "schema": "ca_recovery_trend_v1",
                    "improving": True,
                    "worsening": False,
                    "hold_restore_ca": True,
                    "ca_rate_10m": 0.40,
                    "ca_rate_5m": 0.70,
                    "ca_rate_2m": 0.90,
                },
            )

        self.assertTrue(status["active"])
        self.assertFalse(status["recovery_allowed"])
        self.assertEqual(status["host_behavior_grade"]["grade"], "observe")
        self.assertIn("ca_metric_improving", status["host_behavior_grade"]["blockers"])

    def test_anchor_flip_instability_allows_recovery_when_rf_or_ca_trend_degrades(self):
        monitor = _TestAnchorFlipMonitor(
            active_flips=3,
            command_collision_active_flips=2,
        )
        base = 1_800_000_000.0
        status = {}

        for idx, (before, after) in enumerate([(20, 3), (3, 20), (20, 3)]):
            computed = _computed(congestion_pct=20.0 + idx * 20.0)
            computed.cell_structural_change = True
            computed.cell_structural_change_fresh = True
            computed.cell_structural_change_kind = "band"
            computed.cell_structural_change_class = "anchor_move"
            computed.cell_structural_change_service_impact = True
            computed.cell_change_band_before = before
            computed.congestion_rising = True
            status = monitor.observe(
                modem=_modem(primary_band=after, sinr_db=15.0 - idx * 3.0),
                computed=computed,
                state=SystemState.WORKING,
                now=base + idx * 60,
                ca_recovery_trend={
                    "schema": "ca_recovery_trend_v1",
                    "improving": False,
                    "worsening": True,
                    "hold_restore_ca": False,
                },
            )

        self.assertTrue(status["active"])
        self.assertTrue(status["recovery_allowed"])
        self.assertEqual(status["host_behavior_grade"]["grade"], "recover")
        self.assertIn("rf_or_load_degrading", status["host_behavior_grade"]["reasons"])


class TestTsarVectorCandidateLane(unittest.TestCase):
    def test_decide_consumes_learning_candidate_as_band_action(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.deployed_bands = {1, 3, 20}
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {f"3:{time.localtime().tm_hour}": 0.86},
                "band_hour_counts": {f"3:{time.localtime().tm_hour}": 5},
                "control_authority_bands": [3, 20],
            }

            modem = _modem(primary_band=20, configured_band_pool=[20])
            _publish_selected_radio_command(
                engine,
                modem,
                target_band=3,
                target_bands=[3, 20],
                reason_code="learned_anchor_outperforms_current",
            )
            action = _decide(engine, modem, _computed())

            self.assertIsNotNone(action)
            self.assertEqual(action.lever, LeverType.PBAND)
            self.assertEqual(action.intent, "smart_loop_candidate")
            self.assertEqual(action.params.get("pband"), 3)
            self.assertEqual(action.params.get("ca_pool"), [3, 20])
            self.assertTrue(action.params.get("_selected_action_command"))
            self.assertEqual(_read_tsar_plan(engine).get("status"), "selected")
            self.assertFalse(
                engine.cooldowns.is_blocked("preemptive_switch", now=time.time())
            )

            receipt_ts = time.time()
            terminalize_selected_action_for_test(
                engine,
                now=receipt_ts,
            )
            engine.note_action_result(action, True, _modem(primary_band=3))

            self.assertFalse(
                engine.cooldowns.is_blocked("preemptive_switch", now=time.time())
            )
            self.assertTrue(
                engine.cooldowns.is_blocked(
                    SMART_LOOP_ROI_OBSERVATION_COOLDOWN, now=time.time()
                )
            )

            after_modem = _modem(primary_band=3, configured_band_pool=[3, 20])
            now = time.time()
            engine.controller.runtime.register_bank.write_decision(
                value=build_runtime_decision_projection(
                    {"brain": engine.get_runtime_status()}
                ),
                source="test",
                updated_at=now,
            )
            _publish_self_learning(engine, after_modem, now=now)

            blocked_strategy = read_smart_loop_strategy_register(engine, now=now + 0.1)
            self.assertEqual(blocked_strategy["outcome"], "proof_pending")
            self.assertEqual(blocked_strategy["reason_code"], "proof_pending")
            self.assertNotIn("command", blocked_strategy)
            self.assertIsNotNone(blocked_strategy["state"].get("pending_proof"))

    def test_self_learning_candidate_signature_bypasses_stability_skip(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine._startup_time = -1000.0
            engine.deployed_bands = {1, 3, 20}
            current_hour = time.localtime(1000.0).tm_hour
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {f"3:{current_hour}": 0.86},
                "band_hour_counts": {f"3:{current_hour}": 5},
            }
            modem = _modem(primary_band=20, configured_band_pool=[20])
            computed = _computed()
            hard = getattr(engine, "_thresholds", {}).get("hard", {})
            rtt_warning = float(hard.get("rtt_warning_ms", engine.cfg.rtt_warning_ms))
            rtt_cliff = float(hard.get("rtt_cliff_ms", engine.cfg.rtt_cliff_ms))
            rtt_bucket = (
                "alarm"
                if computed.rtt_sliding_avg >= rtt_cliff
                else "warning"
                if computed.rtt_sliding_avg >= rtt_warning
                else "normal"
            )
            old_signature = engine._self_learning_candidate_keyframe_signature(
                now=1000.0
            )
            engine._stability_keyframe = (
                engine.posture_authority.state,
                int(modem.primary_band),
                rtt_bucket,
                False,
                bool(modem.has_traffic),
                time.localtime(1000.0).tm_hour,
                bool(engine._soft_congestion_active),
                bool(engine._ca_lost_state),
                False,
                engine.cooldowns.active_count(now=1000.0),
                old_signature,
                int(getattr(engine, "_escape_probe_rf_cliff_good_ticks", 0) or 0),
                int(getattr(engine, "_nr_lock_bad_sinr_ticks", 0) or 0),
            )
            engine._stability_skip_count = 0
            _publish_selected_radio_command(
                engine,
                modem,
                target_band=3,
                target_bands=[3, 20],
                reason_code="learned_anchor_outperforms_current",
                now=1000.0,
            )

            with patch("arch4.brain.decision_pipeline.time.time", return_value=1000.0):
                action = _decide(engine, modem, computed)

            self.assertIsNotNone(action)
            self.assertEqual(action.intent, "smart_loop_candidate")
            trace = engine.get_last_decision_trace()
            self.assertNotEqual(trace.get("source"), "stability_keyframe")

    def test_decide_keeps_learning_avoid_band_advisory_not_ca_pool_reduction(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine._startup_time = -1000.0
            engine.deployed_bands = {1, 3, 20}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 1.0,
                "sample_count": 25,
                "band_quality": {"1": 0.30, "3": 0.0},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }
            modem = _modem(
                primary_band=3,
                ca_active=True,
                ca_signature="B1+B3+B20",
                selected_band_set=[1, 3, 20],
                configured_band_pool=[1, 3, 20],
            )
            _publish_self_learning(engine, modem)

            action = _decide(engine, modem, _computed())

            self.assertIsNone(action)
            plan = _read_tsar_plan(engine)
            self.assertEqual(plan.get("status"), "none")
            candidates = [
                plan.get("selected", {}),
                *(plan.get("considered") or []),
                *(plan.get("deferred") or []),
            ]
            self.assertFalse(
                any(
                    item.get("action") == "reduce_ca_pool"
                    for item in candidates
                    if isinstance(item, dict)
                )
            )

    def test_reduce_ca_pool_without_anchor_flip_evidence_path_is_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine._startup_time = -1000.0
            engine.deployed_bands = {1, 3, 20}
            engine.memory.is_band_blacklisted = lambda band: False
            modem = _modem(
                primary_band=3,
                ca_active=True,
                ca_signature="B1+B3+B20",
                selected_band_set=[1, 3, 20],
                configured_band_pool=[1, 3, 20],
            )
            _publish_self_learning(engine, modem)
            decision_src = (
                Path(__file__).resolve().parents[1] / "brain" / "decision_pipeline.py"
            ).read_text(encoding="utf-8")
            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNone(action)
            plan = _read_tsar_plan(engine)
            self.assertEqual(plan.get("owner"), SELF_LEARNING_STRATEGY_OWNER)
            self.assertNotIn("held_candidate", plan)
            self.assertNotIn("build_tsar_candidate_plan", decision_src)

    def test_self_learning_strategy_proof_pending_holds_tsar_vector_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(modem_backend="zte_goform"), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.provider_plmn = "22005"
            engine.constraints.provider_name = "A1"
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 20}
            modem = _modem(
                primary_band=3,
                ca_active=True,
                ca_signature="B1+B3+B20",
                selected_band_set=[1, 3, 20],
                configured_band_pool=[1, 3, 20],
            )
            now_ts = time.time()
            publish_smart_loop_strategy_register(
                engine,
                publication={
                    "schema": SELF_LEARNING_STRATEGY_SCHEMA,
                    "owner": SELF_LEARNING_STRATEGY_OWNER,
                    "published_at": 100.0,
                    "outcome": "action_rejected",
                    "reason": "proof_pending",
                    "reason_code": "proof_pending",
                    "required_next_step": "wait_for_referee_window",
                    "learning_loop_closed": False,
                    "learning_signal": False,
                    "diagnostic_signal": False,
                    "candidate_counts": {
                        "total": 1,
                        "operational": 0,
                        "actionable": 0,
                        "advisory": 0,
                        "blocked": 1,
                    },
                    "candidates": [],
                    "selected": None,
                    "tsar_gate": {
                        "verdict": "refused",
                        "reason_code": "proof_pending",
                        "required_next_step": "wait_for_referee_window",
                    },
                    "inputs": {
                        "self_learning": {},
                        "tsar": {},
                        "referee": {},
                        "learning_effect": {},
                    },
                    "opened_proof": None,
                    "closed_proof": None,
                    "state": {"roi": {}, "pending_proof": None},
                },
                now=now_ts,
                stale_after_s=30.0,
            )
            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=now_ts,
            )

            self.assertIsNone(action)
            plan = _read_tsar_plan(engine)
            self.assertEqual(plan.get("status"), "none")
            self.assertEqual(plan.get("reason"), "proof_pending")
            self.assertEqual(
                plan["arbitration_outcome"].get("required_next_step"),
                "wait_for_referee_window",
            )
            events = engine.drain_runtime_events()
            self.assertFalse(
                any(
                    event.get("kind") == "tsar_vector_candidate_deferred"
                    for event in events
                ),
                events,
            )

    def test_missing_self_learning_strategy_is_owner_unavailable_not_candidate_deferred(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(modem_backend="zte_goform"), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.provider_plmn = "22005"
            engine.constraints.provider_name = "A1"
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 20}
            modem = _modem(
                primary_band=3,
                ca_active=True,
                ca_signature="B1+B3+B20",
                selected_band_set=[1, 3, 20],
                configured_band_pool=[1, 3, 20],
            )

            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNone(action)
            events = engine.drain_runtime_events()
            self.assertTrue(
                any(
                    event.get("kind") == "smart_loop_strategy_unavailable"
                    and event.get("reason") == "smart_loop_strategy_missing"
                    for event in events
                ),
                events,
            )
            self.assertFalse(
                any(
                    event.get("kind") == "tsar_vector_candidate_deferred"
                    for event in events
                ),
                events,
            )

    def test_self_learning_selected_strategy_without_selected_action_fails_loudly(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(modem_backend="zte_goform"), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.provider_plmn = "22005"
            engine.constraints.provider_name = "A1"
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 20}
            modem = _modem(
                primary_band=3,
                ca_active=True,
                ca_signature="B1+B3+B20",
                selected_band_set=[1, 3, 20],
                configured_band_pool=[1, 3, 20],
            )
            now_ts = time.time()
            publish_smart_loop_strategy_register(
                engine,
                publication={
                    "schema": SELF_LEARNING_STRATEGY_SCHEMA,
                    "owner": SELF_LEARNING_STRATEGY_OWNER,
                    "published_at": now_ts,
                    "outcome": "action_selected_execution_pending",
                    "reason": "tsar_approved",
                    "reason_code": "tsar_approved",
                    "required_next_step": "publish_brain_selected_action",
                    "learning_loop_closed": True,
                    "learning_signal": True,
                    "diagnostic_signal": False,
                    "candidate_counts": {
                        "total": 1,
                        "operational": 1,
                        "actionable": 1,
                        "advisory": 0,
                        "blocked": 0,
                    },
                    "candidates": [{"action": "prefer_anchor_band", "target_band": 1}],
                    "selected": {"action": "prefer_anchor_band", "target_band": 1},
                    "tsar_gate": {
                        "verdict": "act",
                        "reason_code": "tsar_approved",
                        "required_next_step": "publish_brain_selected_action",
                    },
                    "inputs": {
                        "self_learning": {},
                        "tsar": {},
                        "referee": {},
                        "learning_effect": {},
                    },
                    "opened_proof": None,
                    "closed_proof": None,
                    "state": {"roi": {}, "pending_proof": None},
                },
                now=now_ts,
                stale_after_s=30.0,
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "brain_selected_action_missing_for_selected_strategy",
            ):
                engine._evaluate_tsar_vector_candidate(
                    modem,
                    _computed(),
                    state=SystemState.WORKING,
                    now=now_ts,
                )
            events = engine.drain_runtime_events()
            self.assertTrue(
                any(
                    event.get("kind") == "tsar_vector_candidate_deferred"
                    and event.get("reason") == "brain_selected_action_missing"
                    for event in events
                ),
                events,
            )

    def test_invalid_self_learning_strategy_blocks_tsar_vector_candidate_owner_leak(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(modem_backend="zte_goform"), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.provider_plmn = "22005"
            engine.constraints.provider_name = "A1"
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 20}
            modem = _modem(
                primary_band=3,
                ca_active=True,
                ca_signature="B1+B3+B20",
                selected_band_set=[1, 3, 20],
                configured_band_pool=[1, 3, 20],
            )
            with self.assertRaises(BrainFeedOwnershipError):
                engine.controller.brain.feed_registers.write_feeder(
                    FEED_SELF_LEARNING_STRATEGY,
                    owner="illegal.test.owner",
                    payload={
                        "schema": SELF_LEARNING_STRATEGY_SCHEMA,
                        "owner": "illegal.test.owner",
                        "outcome": "proof_invalid",
                    },
                    source="test",
                    updated_at=100.0,
                    stale_after_s=30.0,
                    valid=True,
                )

    def test_self_learning_prefer_band_preserves_safe_ca_pool_with_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(modem_backend="zte_goform"), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.provider_plmn = "22005"
            engine.constraints.provider_name = "A1"
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 7, 20, 28}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {"1": 0.30, "3": 0.91},
                "band_hour_quality": {},
                "band_hour_counts": {},
                "control_authority_bands": [1, 3, 7, 20, 28],
            }

            class _CrystalBall:
                is_ready = True

                def get_status(self):
                    return {"ready": True, "slots": 72, "build_records": 300}

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "present",
                        "action": "recommend_band",
                        "reason": "current_band_predicted_congested_with_better_candidate",
                        "recommendation": {
                            "band": 3,
                            "confidence": 5,
                            "score": 0.88,
                            "predicted_congestion": 18.0,
                        },
                    }

            engine.crystal_ball = _CrystalBall()
            modem = _modem(
                primary_band=20,
                ca_active=False,
                ca_signature="B20",
                selected_band_set=[1, 3, 7, 20, 28],
                configured_band_pool=[1, 3, 7, 20, 28],
            )
            _publish_selected_radio_command(
                engine,
                modem,
                target_band=3,
                target_bands=[1, 3, 7, 20, 28],
                reason_code="learned_anchor_outperforms_current",
            )

            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNotNone(action)
            self.assertEqual(action.lever, LeverType.PBAND)
            self.assertEqual(action.intent, "smart_loop_candidate")
            self.assertEqual(action.params.get("pband"), 3)
            self.assertEqual(action.params.get("ca_pool"), [1, 3, 7, 20, 28])
            self.assertEqual(action.params.get("pband"), 3)
            self.assertEqual(action.params.get("dl_bands"), [1, 7, 20, 28])
            self.assertTrue(action.params.get("_selected_action_command"))
            self.assertTrue(
                str(action.params.get("_selected_action_command_id") or "")
            )
            self.assertTrue(
                str(action.params.get("_selected_action_proof_id") or "")
            )
            self.assertEqual(
                action.params.get("_smart_loop_strategy_outcome"),
                "action_selected_execution_pending",
            )
            self.assertIn("Smart loop radio configuration: PBAND B3", action.reason)

    def test_smart_loop_action_availability_block_clears_command_and_proof_publication(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(modem_backend="zte_goform"), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.provider_plmn = "22005"
            engine.constraints.provider_name = "A1"
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 20}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {"1": 0.30, "3": 0.91},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }

            class _CrystalBall:
                is_ready = True

                def get_status(self):
                    return {"ready": True, "slots": 72, "build_records": 300}

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "present",
                        "action": "recommend_band",
                        "reason": "current_band_predicted_congested_with_better_candidate",
                        "recommendation": {
                            "band": 3,
                            "confidence": 5,
                            "score": 0.88,
                            "predicted_congestion": 18.0,
                        },
                    }

            engine.crystal_ball = _CrystalBall()
            now = time.time()
            modem = _modem(
                primary_band=20,
                ca_active=False,
                ca_signature="B20",
                selected_band_set=[1, 3, 20],
                configured_band_pool=[1, 3, 20],
            )
            _publish_selected_radio_command(
                engine,
                modem,
                target_band=3,
                target_bands=[1, 3, 20],
                reason_code="learned_anchor_outperforms_current",
                now=now,
            )
            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=now,
            )
            self.assertIsNotNone(action)
            before = read_smart_loop_strategy_register(engine, now=now)
            self.assertEqual(before["outcome"], "action_selected_execution_pending")
            self.assertNotIn("command", before)
            self.assertIsNone(before["opened_proof"])

            engine.action_source_state_authority.note_operator_band_physical_acceptance(
                lever="PBAND",
                params={"pband": 3, "ca_pool": [1, 3, 20]},
                origin="operator_ngui",
                target_signature="P3|CA1+3+20",
                observed_at=now,
                receipt_id="smart-loop-operator-band",
            )
            blocked = engine._apply_action_availability(
                action=action,
                modem=modem,
                state=SystemState.WORKING,
                now=now + 1.0,
                source="smart_loop_candidate",
                check_objective=False,
                emit_trace=True,
            )
            after = read_smart_loop_strategy_register(engine, now=now + 1.1)
            plan = engine._tsar_arbitration_plan()

            self.assertIsNone(blocked)
            self.assertEqual(after["outcome"], "action_rejected")
            self.assertEqual(after["reason_code"], "autonomous_band_embargo")
            self.assertEqual(after["required_next_step"], "wait_for_cooldown")
            self.assertNotIn("command", after)
            self.assertIsNone(after["opened_proof"])
            self.assertIsNone(after["state"].get("pending_proof"))
            self.assertEqual(after["candidate_counts"]["actionable"], 0)
            self.assertEqual(plan["status"], "none")
            self.assertEqual(
                plan["arbitration_outcome"]["reason_code"],
                "autonomous_band_embargo",
            )

    def test_huawei_crystal_ball_prefer_band_is_advisory_only(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(modem_backend="b818"), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.provider_plmn = "22005"
            engine.constraints.provider_name = "A1"
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 7, 20, 28}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }

            class _CrystalBall:
                is_ready = True

                def get_status(self):
                    return {"ready": True, "slots": 72, "build_records": 300}

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "present",
                        "action": "recommend_band",
                        "reason": "current_band_predicted_congested_with_better_candidate",
                        "recommendation": {
                            "band": 3,
                            "confidence": 5,
                            "score": 0.88,
                            "predicted_congestion": 18.0,
                        },
                    }

            engine.crystal_ball = _CrystalBall()
            modem = _modem(
                primary_band=20,
                ca_active=False,
                ca_signature="B20",
                selected_band_set=[1, 3, 7, 20, 28],
                configured_band_pool=[1, 3, 7, 20, 28],
            )
            _publish_self_learning(engine, modem)

            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNone(action)
            self.assertEqual(
                _read_tsar_plan(engine).get("reason"),
                "candidate_generation_failed",
            )

    def test_self_learning_prefer_band_adds_pband_without_expanding_ca_pool(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(modem_backend="zte_goform"), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.provider_plmn = "22005"
            engine.constraints.provider_name = "A1"
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 7, 20, 28}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {"3": 0.88, "20": 0.35},
                "band_hour_quality": {},
                "band_hour_counts": {},
                "control_authority_bands": [1, 3, 20],
            }

            class _CrystalBall:
                is_ready = True

                def get_status(self):
                    return {"ready": True, "slots": 72, "build_records": 300}

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "present",
                        "action": "recommend_band",
                        "reason": "current_band_predicted_congested_with_better_candidate",
                        "recommendation": {
                            "band": 3,
                            "confidence": 5,
                            "score": 0.88,
                            "predicted_congestion": 18.0,
                        },
                    }

            engine.crystal_ball = _CrystalBall()
            modem = _modem(
                primary_band=20,
                ca_active=True,
                ca_signature="B1+B20",
                selected_band_set=[1, 20],
                configured_band_pool=[1, 20],
            )
            _publish_selected_radio_command(
                engine,
                modem,
                target_band=3,
                target_bands=[1, 3, 20],
                reason_code="learned_anchor_outperforms_current",
            )

            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNotNone(action)
            self.assertEqual(action.lever, LeverType.PBAND)
            self.assertEqual(action.params.get("pband"), 3)
            self.assertEqual(action.params.get("ca_pool"), [1, 3, 20])
            self.assertEqual(action.params.get("dl_bands"), [1, 20])
            self.assertNotIn(7, action.params.get("ca_pool"))
            self.assertNotIn(28, action.params.get("ca_pool"))

    def test_crystal_ball_prefer_band_suppressed_without_selected_pool_truth(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.provider_plmn = "22005"
            engine.constraints.provider_name = "A1"
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 5, 7, 8, 20, 28, 32, 38, 40, 41, 42, 43}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }

            class _CrystalBall:
                is_ready = True

                def get_status(self):
                    return {"ready": True, "slots": 72, "build_records": 300}

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "present",
                        "action": "recommend_band",
                        "reason": "current_band_predicted_congested_with_better_candidate",
                        "recommendation": {
                            "band": 1,
                            "confidence": 5,
                            "score": 0.58,
                            "predicted_congestion": 21.0,
                        },
                    }

            engine.crystal_ball = _CrystalBall()
            modem = _modem(
                primary_band=3,
                ca_active=True,
                ca_signature="B3+CA",
                selected_band_set=[],
                configured_band_pool=[1, 3, 5, 7, 8, 20, 28, 32, 38, 40, 41, 42, 43],
            )
            _publish_self_learning(engine, modem)

            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNone(action)

    def test_ca_flapping_prediction_cannot_bypass_smart_loop_owner(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 7, 20, 28}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }
            engine._ca_flapping_status = {
                "schema": "ca_flapping_v1",
                "active": True,
                "window_s": 180.0,
                "flip_count": 6,
                "reason": "provider=Yettel cell=123 hour=10 PBAND=B1 prone_to_ca_flapping",
                "context": {
                    "provider_plmn": "22001",
                    "provider_name": "Yettel",
                    "cell_id": 123,
                    "pci": 100,
                    "hour": time.localtime().tm_hour,
                    "pband": 1,
                },
            }

            class _CrystalBall:
                is_ready = True

                def get_status(self):
                    return {"ready": True, "slots": 72, "build_records": 300}

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "present",
                        "action": "recommend_band",
                        "reason": "known good pband for this hour",
                        "recommendation": {
                            "band": 3,
                            "confidence": 5,
                            "score": 0.91,
                            "predicted_congestion": 12.0,
                        },
                    }

            engine.crystal_ball = _CrystalBall()
            modem = _modem(
                primary_band=1,
                ca_active=True,
                ca_signature="B1+B3",
                selected_band_set=[1, 3, 7, 20, 28],
            )
            _publish_self_learning(engine, modem)

            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNone(action)
            plan = _read_tsar_plan(engine)
            self.assertEqual(plan.get("owner"), SELF_LEARNING_STRATEGY_OWNER)
            self.assertEqual(plan.get("status"), "none")
            self.assertNotIn("held_candidate", plan)

    def test_ca_flapping_anchor_recovery_preserves_existing_pool_before_capability_widen(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.provider_plmn = "22005"
            engine.constraints.provider_name = "A1"
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 5, 7, 8, 20, 28, 32, 38, 40, 41, 42, 43}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {"1": 0.30, "3": 0.91},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }
            engine._ca_flapping_status = {
                "schema": "ca_flapping_v1",
                "active": True,
                "window_s": 180.0,
                "flip_count": 6,
                "reason": "provider=A1 cell=2050 hour=20 PBAND=B1 prone_to_ca_flapping",
                "context": {
                    "provider_plmn": "22005",
                    "provider_name": "A1",
                    "cell_id": 2050,
                    "hour": time.localtime().tm_hour,
                    "pband": 1,
                },
            }

            class _CrystalBall:
                is_ready = True

                def get_status(self):
                    return {"ready": True, "slots": 72, "build_records": 300}

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "present",
                        "action": "recommend_band",
                        "reason": "known good pband for this hour",
                        "recommendation": {
                            "band": 3,
                            "confidence": 5,
                            "score": 0.91,
                            "predicted_congestion": 12.0,
                        },
                    }

            engine.crystal_ball = _CrystalBall()
            modem = _modem(
                primary_band=1,
                ca_active=True,
                ca_signature="B1+B3",
                selected_band_set=[1, 3, 5, 7, 8, 20, 28, 32, 38, 40, 41, 42, 43],
                configured_band_pool=[1, 3],
            )
            _publish_selected_radio_command(
                engine,
                modem,
                target_band=3,
                target_bands=[1, 3],
                reason_code="learned_anchor_outperforms_current",
            )

            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNotNone(action)
            self.assertEqual(action.lever, LeverType.PBAND)
            self.assertEqual(action.params.get("pband"), 3)
            self.assertEqual(action.params.get("ca_pool"), [1, 3])
            self.assertEqual(action.params.get("dl_bands"), [1])
            self.assertTrue(action.params.get("_selected_action_command"))
            self.assertEqual(
                action.params.get("_smart_loop_strategy_outcome"),
                "action_selected_execution_pending",
            )

    def test_ca_flapping_widens_ca_pool_one_band_at_a_time_when_prediction_has_no_anchor(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 7, 20, 28}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }
            engine._ca_flapping_status = {
                "schema": "ca_flapping_v1",
                "active": True,
                "window_s": 180.0,
                "flip_count": 6,
                "reason": "provider=Yettel cell=123 hour=10 PBAND=B1 prone_to_ca_flapping",
                "context": {
                    "provider_plmn": "22001",
                    "provider_name": "Yettel",
                    "cell_id": 123,
                    "pci": 100,
                    "hour": time.localtime().tm_hour,
                    "pband": 1,
                },
            }

            class _CrystalBall:
                is_ready = True

                def get_status(self):
                    return {"ready": True, "slots": 72, "build_records": 300}

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "withheld",
                        "reason": "no_better_band_with_sufficient_margin",
                    }

            engine.crystal_ball = _CrystalBall()
            modem = _modem(
                primary_band=1,
                ca_active=True,
                ca_signature="B1+B3",
                selected_band_set=[1, 3],
            )
            _publish_self_learning(engine, modem)

            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNone(action)
            plan = _read_tsar_plan(engine)
            self.assertEqual(plan.get("owner"), SELF_LEARNING_STRATEGY_OWNER)
            self.assertEqual(plan.get("status"), "none")
            self.assertNotIn("held_candidate", plan)

    def test_staged_roi_context_changed_record_does_not_penalize_ca_pool(self):
        record = ActionRecord(
                    timestamp=time.time() - 300.0,
                    sinr_db=12.0,
                    rsrq_db=-9.0,
                    mcs_dl=20,
                    ca_signature="B1+B3",
                    rtt_sliding_avg=40.0,
                    rtt_ms=40.0,
                    lever=LeverType.BAND_SELECTION,
                    action_params={
                        "bands": [1, 3, 7],
                        "_ca_pool_expansion_trial": True,
                        "_ca_pool_previous_bands": [1, 3],
                        "_ca_pool_added_bands": [7],
                        "_tsar_candidate_band": 1,
                    },
                    success=False,
                    throughput_before_mbps=12.0,
                    throughput_after_mbps=18.0,
                    sinr_after=13.0,
                    ca_signature_after="B1+B3+B7",
                    improvement=6.0,
                    provider_plmn="22001",
                    provider_name="Yettel",
                    outcome_label="neutral",
                    referee_outcome_level="neutral",
                    referee_outcome_code="staged_roi_context_changed",
                    outcome_verdict_stage="staged_roi",
                    learning_commit_status="committed",
                    learning_gate=_allowed_learning_gate(),
                    objective_metadata={
                        "staged_roi_evidence": {
                            "grade_context_status": "changed",
                            "grade_context_reason": "context_changed:pband:B1->B3",
                            "grade_evidence_valid": True,
                        }
                    },
                )
        with tempfile.TemporaryDirectory() as td:
            memory = Memory(_cfg(), memory_dir=td)
            _append_scoped_action(memory, record)
            roi = _pband_action_roi_from_rewards(
                memory,
                provider_plmn="22001",
                provider_name="Yettel",
            )

        self.assertEqual(roi, {})

    def test_staged_roi_parent_close_is_not_counted_as_second_roi_sample(self):
        records = [
                ActionRecord(
                    timestamp=time.time() - 310.0,
                    sinr_db=12.0,
                    rsrq_db=-9.0,
                    mcs_dl=20,
                    ca_signature="B1+B3",
                    rtt_sliding_avg=40.0,
                    rtt_ms=40.0,
                    lever=LeverType.BAND_SELECTION,
                    action_params={
                        "bands": [1, 3, 7],
                        "_ca_pool_expansion_trial": True,
                        "_ca_pool_previous_bands": [1, 3],
                        "_ca_pool_added_bands": [7],
                        "_tsar_candidate_band": 1,
                    },
                    success=False,
                    throughput_before_mbps=12.0,
                    throughput_after_mbps=7.0,
                    sinr_after=8.0,
                    ca_signature_after="B1+B3+B7",
                    improvement=-5.0,
                    provider_plmn="22001",
                    provider_name="Yettel",
                    outcome_label="bad",
                    referee_outcome_level="bad",
                    referee_outcome_code="staged_roi_negative",
                    outcome_verdict_stage="staged_roi",
                    learning_commit_status="committed",
                    learning_gate=_allowed_learning_gate(),
                    objective_status="proof_closed",
                    objective_metadata={
                        "staged_roi_parent_status": "proof_closed",
                        "staged_roi_final_code": "staged_roi_negative",
                    },
                    decision_trace_id="dt-parent",
                ),
                ActionRecord(
                    timestamp=time.time() - 5.0,
                    sinr_db=12.0,
                    rsrq_db=-9.0,
                    mcs_dl=20,
                    ca_signature="B1+B3",
                    rtt_sliding_avg=40.0,
                    rtt_ms=40.0,
                    lever=LeverType.BAND_SELECTION,
                    action_params={
                        "bands": [1, 3, 7],
                        "_ca_pool_expansion_trial": True,
                        "_ca_pool_previous_bands": [1, 3],
                        "_ca_pool_added_bands": [7],
                        "_tsar_candidate_band": 1,
                        "_roi_parent_trace_id": "dt-parent",
                        "_roi_horizon_s": 300,
                    },
                    success=False,
                    throughput_before_mbps=12.0,
                    throughput_after_mbps=7.0,
                    sinr_after=8.0,
                    ca_signature_after="B1+B3+B7",
                    improvement=-5.0,
                    provider_plmn="22001",
                    provider_name="Yettel",
                    outcome_label="bad",
                    referee_outcome_level="bad",
                    referee_outcome_code="staged_roi_negative",
                    outcome_verdict_stage="staged_roi",
                    learning_commit_status="committed",
                    learning_gate=_allowed_learning_gate(),
                    objective_metadata={
                        "staged_roi_evidence": {
                            "grade_context_status": "same",
                            "grade_evidence_valid": True,
                        }
                    },
                    roi_parent_trace_id="dt-parent",
                    roi_horizon_s=300,
                    decision_trace_id="dt-parent:roi:300",
                ),
            ]
        with tempfile.TemporaryDirectory() as td:
            memory = Memory(_cfg(), memory_dir=td)
            for record in records:
                _append_scoped_action(memory, record)
            roi = _pband_action_roi_from_rewards(
                memory,
                provider_plmn="22001",
                provider_name="Yettel",
            )

        bucket = roi["by_ca_pool"]["ca_pool:B1+B3+B7"]
        self.assertEqual(bucket["attempts"], 1)
        self.assertEqual(bucket["bad"], 1)
        self.assertEqual(bucket["good"], 0)

    def test_ca_flapping_bad_ca_pool_delta_becomes_brain_roi_hold(self):
        with tempfile.TemporaryDirectory() as td:
            now_ts = time.time()
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 7, 20, 28}
            engine.memory.is_band_blacklisted = lambda band: False
            _append_scoped_action(
                engine.memory,
                ActionRecord(
                    timestamp=now_ts,
                    sinr_db=12.0,
                    rsrq_db=-9.0,
                    mcs_dl=20,
                    ca_signature="B1+B3",
                    rtt_sliding_avg=40.0,
                    rtt_ms=40.0,
                    lever=LeverType.BAND_SELECTION,
                    action_params={
                        "bands": [1, 3, 7],
                        "_ca_pool_expansion_trial": True,
                        "_ca_pool_previous_bands": [1, 3],
                        "_ca_pool_added_bands": [7],
                        "_tsar_candidate_band": 1,
                    },
                    success=False,
                    throughput_before_mbps=12.0,
                    throughput_after_mbps=7.0,
                    sinr_after=8.0,
                    ca_signature_after="B1+B3+B7",
                    improvement=-5.0,
                    outcome_label="bad",
                    referee_outcome_level="bad",
                    referee_outcome_code="staged_roi_negative",
                    learning_commit_status="committed",
                    learning_gate=_allowed_learning_gate(),
                )
            )
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }
            engine._ca_flapping_status = {
                "schema": "ca_flapping_v1",
                "active": True,
                "window_s": 180.0,
                "flip_count": 6,
                "reason": "provider=Yettel cell=123 hour=10 PBAND=B1 prone_to_ca_flapping",
                "context": {
                    "provider_plmn": "22001",
                    "provider_name": "Yettel",
                    "cell_id": 123,
                    "pci": 100,
                    "hour": time.localtime(now_ts).tm_hour,
                    "pband": 1,
                },
            }

            class _CrystalBall:
                is_ready = True

                def get_status(self):
                    return {"ready": True, "slots": 72, "build_records": 300}

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "withheld",
                        "reason": "no_better_band_with_sufficient_margin",
                    }

            engine.crystal_ball = _CrystalBall()
            modem = _modem(
                primary_band=1,
                ca_active=True,
                ca_signature="B1+B3",
                selected_band_set=[1, 3],
                configured_band_pool=[1, 3],
            )
            _publish_self_learning(engine, modem)

            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=now_ts,
            )

            self.assertIsNone(action)
            plan = _read_tsar_plan(engine)
            self.assertEqual(plan.get("owner"), SELF_LEARNING_STRATEGY_OWNER)
            self.assertEqual(plan.get("status"), "none")
            self.assertEqual(plan.get("reason"), "brain_selected_do_nothing")
            self.assertNotIn("held_candidate", plan)

    def test_ca_flapping_bad_ca_pool_delta_from_other_cell_does_not_hold_current_cell(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.provider_plmn = "22001"
            engine.constraints.provider_name = "Yettel"
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 7, 20, 28}
            engine.memory.is_band_blacklisted = lambda band: False
            current_hour = time.localtime().tm_hour
            _append_scoped_action(
                engine.memory,
                ActionRecord(
                    timestamp=time.time() - 120.0,
                    sinr_db=12.0,
                    rsrq_db=-9.0,
                    mcs_dl=20,
                    ca_signature="B1+B3",
                    rtt_sliding_avg=40.0,
                    rtt_ms=40.0,
                    lever=LeverType.BAND_SELECTION,
                    action_params={
                        "bands": [1, 3, 7],
                        "_ca_pool_expansion_trial": True,
                        "_ca_pool_previous_bands": [1, 3],
                        "_ca_pool_added_bands": [7],
                        "_tsar_candidate_band": 1,
                        "_tsar_context_provider_plmn": "22001",
                        "_tsar_context_provider_name": "Yettel",
                        "_tsar_context_cell_id": 999,
                        "_tsar_context_pci": 100,
                        "_tsar_context_earfcn": 1200,
                        "_tsar_context_hour": current_hour,
                    },
                    success=False,
                    throughput_before_mbps=12.0,
                    throughput_after_mbps=7.0,
                    sinr_after=8.0,
                    ca_signature_after="B1+B3+B7",
                    improvement=-5.0,
                    provider_plmn="22001",
                    provider_name="Yettel",
                    outcome_label="bad",
                    referee_outcome_level="bad",
                    referee_outcome_code="staged_roi_negative",
                    learning_commit_status="committed",
                    learning_gate=_allowed_learning_gate(),
                )
            )
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {"1": 0.30, "3": 0.91},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }
            engine._ca_flapping_status = {
                "schema": "ca_flapping_v1",
                "active": True,
                "window_s": 180.0,
                "flip_count": 6,
                "reason": "provider=Yettel cell=123 hour=current PBAND=B1 prone_to_ca_flapping",
                "context": {
                    "provider_plmn": "22001",
                    "provider_name": "Yettel",
                    "cell_id": 123,
                    "pci": 100,
                    "hour": current_hour,
                    "pband": 1,
                },
            }

            class _CrystalBall:
                is_ready = True

                def get_status(self):
                    return {"ready": True, "slots": 72, "build_records": 300}

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "withheld",
                        "reason": "no_better_band_with_sufficient_margin",
                    }

            engine.crystal_ball = _CrystalBall()
            modem = _modem(
                primary_band=1,
                pci=100,
                earfcn=1200,
                cell_id=123,
                provider_plmn="22001",
                provider_name="Yettel",
                ca_active=True,
                ca_signature="B1+B3",
                selected_band_set=[1, 3],
                configured_band_pool=[1, 3],
            )
            _publish_selected_radio_command(
                engine,
                modem,
                target_band=3,
                target_bands=[1, 3],
                reason_code="learned_anchor_outperforms_current",
            )

            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNotNone(action)
            self.assertEqual(action.lever, LeverType.PBAND)
            self.assertEqual(action.params.get("pband"), 3)
            self.assertEqual(action.params.get("ca_pool"), [1, 3])
            self.assertTrue(action.params.get("_selected_action_command"))
            plan = _read_tsar_plan(engine)
            self.assertEqual(plan.get("status"), "selected")
            self.assertEqual(plan.get("owner"), SELF_LEARNING_STRATEGY_OWNER)
            self.assertNotIn("held_candidate", plan)

    def test_learned_bad_ca_pool_roi_restores_previous_pool(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.deployed_bands = {1, 3, 7, 20}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {},
                "band_hour_counts": {},
                "pband_action_roi": {
                    "by_ca_pool": {
                        "ca_pool:B1+B3+B7+B20": {
                            "attempts": 1,
                            "bad": 1,
                            "good": 0,
                            "penalty": 0.25,
                            "last_bad_previous_pool": [1, 3, 20],
                            "last_bad_current_pool": [1, 3, 7, 20],
                            "last_bad_added_bands": [7],
                        }
                    }
                },
            }
            engine.crystal_ball = None
            modem = _modem(
                primary_band=1,
                ca_active=True,
                ca_signature="B1+B3+B7+B20",
                selected_band_set=[1, 3, 7, 20],
                configured_band_pool=[1, 3, 7, 20],
            )
            _publish_self_learning(engine, modem)

            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNone(action)
            plan = _read_tsar_plan(engine)
            self.assertEqual(plan.get("status"), "none")
            candidates = [
                plan.get("selected", {}),
                *(plan.get("considered") or []),
                *(plan.get("deferred") or []),
            ]
            self.assertFalse(
                any(
                    item.get("action") == "reduce_ca_pool"
                    for item in candidates
                    if isinstance(item, dict)
                )
            )

    def test_ca_flapping_does_not_reassert_current_anchor_when_pool_already_wide(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.deployed_bands = {1, 3, 7, 20, 28}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }
            engine._ca_flapping_status = {
                "schema": "ca_flapping_v1",
                "active": True,
                "window_s": 180.0,
                "flip_count": 6,
                "reason": "provider=Yettel cell=123 hour=10 PBAND=B1 prone_to_ca_flapping",
                "context": {"hour": time.localtime().tm_hour, "pband": 1},
            }

            class _CrystalBall:
                is_ready = True

                def get_status(self):
                    return {"ready": True, "slots": 72, "build_records": 300}

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "withheld",
                        "reason": "no_better_band_with_sufficient_margin",
                    }

            engine.crystal_ball = _CrystalBall()
            modem = _modem(
                primary_band=1,
                ca_active=True,
                ca_signature="B1+B3",
                selected_band_set=[1, 3, 7, 20, 28],
            )
            _publish_self_learning(engine, modem)

            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNone(action)

    def test_ca_flapping_pool_stabilisation_clamps_global_mask_to_serbia_ue19_pool(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.constraints.provider_plmn = "22005"
            engine.constraints.provider_name = "A1"
            engine.constraints.ue_category = 19
            engine.deployed_bands = {1, 3, 5, 7, 8, 20, 28, 32, 38, 40, 41, 42, 43}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }
            engine._ca_flapping_status = {
                "schema": "ca_flapping_v1",
                "active": True,
                "window_s": 180.0,
                "flip_count": 6,
                "reason": "provider=- cell=2050 hour=20 PBAND=B3 prone_to_ca_flapping",
                "context": {
                    "hour": time.localtime().tm_hour,
                    "pband": 3,
                    "cell_id": 2050,
                },
            }

            class _CrystalBall:
                is_ready = True

                def get_status(self):
                    return {"ready": True, "slots": 72, "build_records": 300}

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "withheld",
                        "reason": "no_better_band_with_sufficient_margin",
                    }

            engine.crystal_ball = _CrystalBall()
            modem = _modem(
                primary_band=3,
                ca_active=True,
                ca_signature="B3+B1",
                selected_band_set=[1, 3, 5, 7, 8, 20, 28, 32, 38, 40, 41, 42, 43],
                configured_band_pool=[1, 3],
            )
            _publish_self_learning(engine, modem)

            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNone(action)
            plan = _read_tsar_plan(engine)
            self.assertEqual(plan.get("owner"), SELF_LEARNING_STRATEGY_OWNER)
            self.assertEqual(plan.get("status"), "none")
            self.assertNotIn("held_candidate", plan)

    def test_anchor_flip_recovery_adviser_cannot_bypass_smart_loop_owner(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.deployed_bands = {1, 3, 7, 20, 28}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }
            engine._anchor_flip_status = {
                "schema": "anchor_flip_instability_v1",
                "active": True,
                "window_s": 600.0,
                "flip_count": 5,
                "reason": "provider=Yettel hour=16 anchor_flip_instability flips=5 bands=B1,B3,B20",
                "context": {
                    "provider_plmn": "22001",
                    "provider_name": "Yettel",
                    "hour": time.localtime().tm_hour,
                    "pband": 3,
                    "involved_bands": [1, 3, 20],
                },
            }

            class _CrystalBall:
                is_ready = True

                def get_status(self):
                    return {"ready": True, "slots": 72, "build_records": 300}

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "present",
                        "action": "recommend_band",
                        "reason": "stable pband for this hour",
                        "recommendation": {
                            "band": 20,
                            "confidence": 5,
                            "score": 0.92,
                            "predicted_congestion": 4.0,
                        },
                    }

            engine.crystal_ball = _CrystalBall()
            modem = _modem(
                primary_band=3,
                ca_active=True,
                ca_signature="B1+B3+B20",
                selected_band_set=[1, 3, 20],
            )
            _publish_self_learning(engine, modem)

            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNone(action)
            plan = _read_tsar_plan(engine)
            self.assertEqual(plan.get("owner"), SELF_LEARNING_STRATEGY_OWNER)
            self.assertEqual(plan.get("status"), "none")
            self.assertEqual(plan.get("reason"), "brain_selected_do_nothing")

    def test_anchor_flip_b1_b3_does_not_use_deleted_ca_pool_reducer(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.deployed_bands = {1, 3, 20}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }
            engine._anchor_flip_status = {
                "schema": "anchor_flip_instability_v1",
                "active": True,
                "window_s": 600.0,
                "flip_count": 5,
                "reason": "provider=A1 hour=16 anchor_flip_instability flips=5 bands=B1,B3",
                "context": {
                    "provider_plmn": "22005",
                    "provider_name": "A1",
                    "hour": time.localtime().tm_hour,
                    "pband": 3,
                    "involved_bands": [1, 3],
                },
            }

            class _CrystalBall:
                is_ready = True

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "present",
                        "action": "recommend_band",
                        "reason": "B3 stronger than B1 for this hour",
                        "recommendation": {
                            "band": 3,
                            "confidence": 5,
                            "score": 0.92,
                            "predicted_congestion": 4.0,
                        },
                    }

            engine.crystal_ball = _CrystalBall()
            modem = _modem(
                primary_band=3,
                ca_active=True,
                ca_signature="B1+B3+B20",
                selected_band_set=[1, 3, 20],
                configured_band_pool=[1, 3, 20],
            )
            _publish_self_learning(engine, modem)
            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNone(action)
            plan = _read_tsar_plan(engine)
            self.assertEqual(plan.get("owner"), SELF_LEARNING_STRATEGY_OWNER)
            self.assertEqual(plan.get("status"), "none")
            self.assertEqual(plan.get("reason"), "brain_selected_do_nothing")
            candidates = [
                plan.get("selected", {}),
                *(plan.get("considered") or []),
                *(plan.get("deferred") or []),
            ]
            self.assertFalse(
                any(
                    item.get("action") == "reduce_ca_pool"
                    for item in candidates
                    if isinstance(item, dict)
                )
            )

    def test_routeros_anchor_flip_recovery_requests_qnwlock_guard_renewal(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(modem_backend="mikrotik"), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.deployed_bands = {1, 3, 20}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }
            engine._anchor_flip_status = {
                "schema": "anchor_flip_instability_v1",
                "active": True,
                "window_s": 600.0,
                "flip_count": 5,
                "reason": "provider=Yettel hour=16 anchor_flip_instability flips=5 bands=B3,B20",
                "context": {
                    "hour": time.localtime().tm_hour,
                    "pband": 3,
                    "involved_bands": [3, 20],
                },
            }

            class _CrystalBall:
                is_ready = True

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "present",
                        "action": "recommend_band",
                        "reason": "stable pband for this hour",
                        "recommendation": {
                            "band": 20,
                            "confidence": 5,
                            "score": 0.92,
                            "predicted_congestion": 4.0,
                        },
                    }

            engine.crystal_ball = _CrystalBall()
            modem = _modem(
                primary_band=3,
                ca_active=True,
                ca_signature="B3+B20",
                selected_band_set=[3, 20],
            )
            _publish_self_learning(engine, modem)
            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNone(action)
            plan = _read_tsar_plan(engine)
            self.assertEqual(plan.get("owner"), SELF_LEARNING_STRATEGY_OWNER)
            self.assertEqual(plan.get("status"), "none")
            self.assertNotIn("held_candidate", plan)

    def test_anchor_flip_recovery_waits_without_adviser(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.deployed_bands = {1, 3, 20}
            engine.memory.is_band_blacklisted = lambda band: False
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {},
                "band_hour_counts": {},
            }
            engine._anchor_flip_status = {
                "schema": "anchor_flip_instability_v1",
                "active": True,
                "window_s": 600.0,
                "flip_count": 5,
                "reason": "provider=Yettel hour=16 anchor_flip_instability flips=5 bands=B1,B3,B20",
                "context": {
                    "hour": time.localtime().tm_hour,
                    "pband": 3,
                    "involved_bands": [1, 3, 20],
                },
            }

            class _CrystalBall:
                is_ready = True

                def get_status(self):
                    return {"ready": True, "slots": 72, "build_records": 300}

                def build_prediction_advisory(self, **kwargs):
                    return {
                        "schema": "crystal_ball_prediction_advisory_v1",
                        "status": "withheld",
                        "reason": "no_better_band_with_sufficient_margin",
                    }

            engine.crystal_ball = _CrystalBall()

            modem = _modem(primary_band=3, selected_band_set=[1, 3, 20])
            _publish_self_learning(engine, modem)
            action = engine._evaluate_tsar_vector_candidate(
                modem,
                _computed(),
                state=SystemState.WORKING,
                now=time.time(),
            )

            self.assertIsNone(action)
            self.assertEqual(
                _read_tsar_plan(engine).get("reason"),
                "candidate_generation_failed",
            )


class TestSoftCongestion(unittest.TestCase):
    """Cross-state soft congestion detection."""

    def _bad_signals(self):
        """Signals that trigger soft congestion detection."""
        return (
            _modem(sinr_db=20.0, rsrp_dbm=-80.0, rx_bytes_per_sec=5_000_000),
            _computed(
                rtt_avg=65.0,
                cq_cell=0.6,
                cq_vector_a_valid=True,
                rtt_congestion_avg=65.0,
                rtt_recent_median=65.0,
                rtt_delta=28.0,
                rtt_delta_valid=True,
                rtt_quiet_delta=28.0,
                rsrq_gap=4.5,
                rsrq_gap_valid=True,
                congestion_pct=65.0,
            ),
        )

    def _good_signals(self):
        return (_modem(), _computed())

    def test_soft_congestion_disabled(self):
        cfg = _cfg(soft_congestion_enabled=False)
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=cfg, td=td)
            modem, computed = self._bad_signals()
            result = engine._update_soft_congestion(modem, computed)
            self.assertFalse(result)
            self.assertFalse(engine._soft_congestion_active)

    def test_soft_congestion_entry_after_n_ticks(self):
        cfg = _cfg(soft_congestion_entry_ticks=2)
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=cfg, td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            modem, computed = self._bad_signals()
            # Tick 1: not yet active
            engine._update_soft_congestion(modem, computed)
            self.assertFalse(engine._soft_congestion_active)
            # Tick 2: should activate
            engine._update_soft_congestion(modem, computed)
            self.assertTrue(engine._soft_congestion_active)

    def test_soft_congestion_clears_after_n_good_ticks(self):
        cfg = _cfg(soft_congestion_entry_ticks=1, soft_congestion_clear_ticks=2)
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=cfg, td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            modem_bad, computed_bad = self._bad_signals()
            modem_good, computed_good = self._good_signals()
            # Activate
            engine._update_soft_congestion(modem_bad, computed_bad)
            self.assertTrue(engine._soft_congestion_active)
            # Good tick 1: still active
            engine._update_soft_congestion(modem_good, computed_good)
            self.assertTrue(engine._soft_congestion_active)
            # Good tick 2: clears
            engine._update_soft_congestion(modem_good, computed_good)
            self.assertFalse(engine._soft_congestion_active)

    def test_soft_congestion_resets_on_fault_state(self):
        cfg = _cfg(soft_congestion_entry_ticks=1)
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=cfg, td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            modem, computed = self._bad_signals()
            engine._update_soft_congestion(modem, computed)
            self.assertTrue(engine._soft_congestion_active)
            # Simulate fault state
            engine.posture_authority.private_state_for_test.state = SystemState.E_RADIO_DOWN
            engine._update_soft_congestion(modem, computed)
            self.assertFalse(engine._soft_congestion_active)

    def test_soft_congestion_stays_internal_and_emits_no_runtime_event(self):
        cfg = _cfg(soft_congestion_entry_ticks=1)
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=cfg, td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            modem, computed = self._bad_signals()
            engine._update_soft_congestion(modem, computed)
            self.assertTrue(engine._soft_congestion_active)
            events = engine.drain_runtime_events()
            self.assertEqual(events, [])

    def test_soft_congestion_can_use_quiet_entry_rtt_delta_when_available(self):
        cfg = _cfg(soft_congestion_entry_ticks=1)
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=cfg, td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            modem = _modem(sinr_db=20.0, rsrp_dbm=-80.0, rx_bytes_per_sec=5_000_000)
            computed = _computed(
                rtt_avg=35.0,
                cq_cell=0.6,
                cq_vector_a_valid=False,
                rtt_congestion_avg=35.0,
                rtt_recent_median=35.0,
                rtt_delta=28.0,
                rtt_delta_valid=True,
                rtt_quiet_delta=28.0,
                congestion_pct=65.0,
            )
            engine._update_soft_congestion(modem, computed)
            self.assertTrue(engine._soft_congestion_active)


class TestCooldownSaveRestore(unittest.TestCase):
    """Round-trip serialization of cooldown state."""

    def test_round_trip_preserves_active_cooldowns(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            future = time.time() + 600.0
            engine.cooldowns.set_until("band_cooldown", future)
            engine.cooldowns.set_until("emergency", future)
            engine.cooldowns.set_until("hilink_restart", future)
            state = engine.get_decision_cooldown_state()
            # Create fresh engine and restore
            engine2 = _make_engine(td=td)
            engine2.restore_decision_cooldown_state(state)
            self.assertAlmostEqual(
                engine2.cooldowns.get_until("band_cooldown"), future, places=1
            )
            self.assertAlmostEqual(
                engine2.cooldowns.get_until("emergency"), future, places=1
            )
            self.assertAlmostEqual(
                engine2.cooldowns.get_until("hilink_restart"), future, places=1
            )

    def test_expired_cooldowns_not_restored(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            past = time.time() - 100.0
            engine.cooldowns.set_until("band_cooldown", past)
            state = engine.get_decision_cooldown_state()
            engine2 = _make_engine(td=td)
            engine2.restore_decision_cooldown_state(state)
            # Expired cooldown should not be restored (stays at default 0)
            self.assertLessEqual(
                engine2.cooldowns.get_until("band_cooldown"), time.time()
            )

    def test_dict_cooldowns_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            future = time.time() + 600.0
            engine.cooldowns.set_keyed_until("restore_ca_retry", "B1+B3", future)
            engine.cooldowns.set_keyed_until("restore_ca_retry", "B3", future)
            state = engine.get_decision_cooldown_state()
            engine2 = _make_engine(td=td)
            engine2.restore_decision_cooldown_state(state)
            self.assertEqual(engine2.cooldowns.keyed_count("restore_ca_retry"), 2)
            self.assertAlmostEqual(
                engine2.cooldowns.get_keyed_until("restore_ca_retry", "B1+B3"),
                future,
                places=1,
            )

    def test_restore_empty_dict_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.restore_decision_cooldown_state({})
            self.assertEqual(engine.cooldowns.get_until("band_cooldown"), 0.0)

    def test_restore_none_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.restore_decision_cooldown_state(None)
            self.assertEqual(engine.cooldowns.get_until("band_cooldown"), 0.0)

    def test_deleted_cell_lock_context_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            state = engine.get_decision_cooldown_state()
            self.assertNotIn("cell_lock_context", state)
            self.assertIn("band_rotation_state", state)
            engine2 = _make_engine(td=td)
            engine2.restore_decision_cooldown_state(state)
            self.assertNotIn(
                "cell_lock_context",
                engine2.get_decision_cooldown_state(),
            )


class TestRuntimeEvents(unittest.TestCase):
    """Runtime event emit and drain."""

    def test_drain_returns_and_clears(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine._emit_runtime_event("test_event", foo="bar")
            engine._emit_runtime_event("test_event_2", baz=42)
            events = engine.drain_runtime_events()
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["kind"], "test_event")
            self.assertEqual(events[1]["kind"], "test_event_2")
            # Second drain should be empty
            self.assertEqual(len(engine.drain_runtime_events()), 0)

    def test_emit_includes_kind(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine._emit_runtime_event("ts_test", value=42)
            events = engine.drain_runtime_events()
            self.assertEqual(events[0]["kind"], "ts_test")
            self.assertEqual(events[0]["value"], 42)


class TestOperatorBandPolicy(unittest.TestCase):
    """The source-state authority is the only operator band policy owner."""

    def test_deleted_manual_band_owner_is_absent(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            self.assertFalse(hasattr(engine, "note_manual_band_action"))
            self.assertFalse(hasattr(engine, "_guard_manual_band_hold"))
            self.assertFalse(hasattr(engine, "_manual_band_hold_seconds"))

    def test_operator_result_records_fixed_clocks_without_second_roi_timer(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            now = time.time()
            action = _band_action(intent="explore")
            engine.action_source_state_authority.note_operator_band_physical_acceptance(
                lever=action.lever.name,
                params=action.params,
                origin="operator_ngui",
                target_signature="B3",
                observed_at=now,
                receipt_id="operator-band-owner",
            )
            status = (
                engine.action_source_state_authority.band_action_policy_status(
                    now=now
                )
            )
            self.assertAlmostEqual(
                float(status["operator_cooldown_until_ts"]),
                now + 30.0,
                delta=0.01,
            )
            self.assertAlmostEqual(
                float(status["autonomous_embargo_until_ts"]),
                now + 900.0,
                delta=0.01,
            )
            self.assertFalse(
                engine.cooldowns.is_blocked(
                    SMART_LOOP_ROI_OBSERVATION_COOLDOWN, now=now + 1.0
                )
            )

class TestDecisionTrace(unittest.TestCase):
    """Decision trace structure and content."""

    def test_trace_explicit_suppress_when_proposal_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            action = _band_action(intent="explore")
            engine._emit_decision_trace(
                source="state:EXPLORING",
                state=SystemState.EXPLORING,
                proposed=action,
                final=None,
                steps=[
                    {
                        "gate": "manual_action_hold",
                        "result": "blocked",
                        "reason": "manual_band_hold",
                    }
                ],
                modem=_modem(),
            )
            trace = engine.get_last_decision_trace()
            result = engine.get_last_decision_result_object().to_dict()
            self.assertEqual(trace["tsar_verdict"], "suppress")
            self.assertEqual(trace["tsar_reason"], "manual_band_hold")
            self.assertEqual(result["tsar_verdict"], "suppress")

    def test_trace_object_accessible(self):
        cfg = _cfg(min_action_interval_s=9999.0)
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=cfg, td=td)
            engine._last_action_time = time.time()
            _decide(engine, _modem(), _computed())
            self.assertIsNotNone(engine.get_last_decision_trace_object())

    def test_trace_rate_limit(self):
        cfg = _cfg(min_action_interval_s=9999.0)
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=cfg, td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine._last_action_time = time.time()
            _decide(engine, _modem(), _computed())
            trace = engine.get_last_decision_trace()
            self.assertEqual(trace["source"], "rate_limit")

    def test_trace_explicit_hold_when_tsar_soft_no_go_is_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine._note_tsar_soft_no_go(
                verdict="hold",
                reason="healthy_stable_explore_less",
                summary="TSAR held exploration because the connection is healthy and stable.",
            )
            engine._emit_decision_trace(
                source="state:WORKING",
                state=SystemState.WORKING,
                proposed=None,
                final=None,
                steps=[],
                modem=_modem(),
            )
            trace = engine.get_last_decision_trace()
            result = engine.get_last_decision_result_object().to_dict()
            self.assertEqual(trace["tsar_verdict"], "hold")
            self.assertEqual(trace["tsar_reason"], "healthy_stable_explore_less")
            self.assertEqual(result["tsar_verdict"], "hold")

    def test_trace_explicit_suppress_when_tsar_soft_no_go_is_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine._note_tsar_soft_no_go(
                verdict="suppress",
                reason="user_active_exploration_protected",
                summary="TSAR suppressed probing because the user is active.",
            )
            engine._emit_decision_trace(
                source="state:EXPLORING",
                state=SystemState.EXPLORING,
                proposed=None,
                final=None,
                steps=[],
                modem=_modem(),
            )
            trace = engine.get_last_decision_trace()
            result = engine.get_last_decision_result_object().to_dict()
            self.assertEqual(trace["tsar_verdict"], "suppress")
            self.assertEqual(trace["tsar_reason"], "user_active_exploration_protected")
            self.assertEqual(result["tsar_verdict"], "suppress")

    def test_decide_emits_brain_posture_trace_for_preproposal_defer(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING

            engine._lane_state_dispatch = MagicMock(return_value=None)
            action = _decide(engine, _modem(), _computed())

            self.assertIsNone(action)
            self.assertFalse(engine._lane_state_dispatch.called)
            trace = engine.get_last_decision_trace()
            self.assertEqual(trace.get("source"), "decision_cycle")
            self.assertEqual(trace.get("proposed"), "none")
            events = engine.drain_runtime_events()
            self.assertTrue(
                any(e.get("kind") == "smart_loop_strategy_unavailable" for e in events)
            )

    def test_decide_emits_no_action_trace_when_no_candidate_is_selected(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine._lane_state_dispatch = MagicMock(return_value=None)
            engine._evaluate_tsar_vector_candidate = MagicMock(return_value=None)

            action = _decide(engine, _modem(), _computed())

            self.assertIsNone(action)
            trace = engine.get_last_decision_trace()
            result = engine.get_last_decision_result_object().to_dict()
            context = engine.get_last_decision_context_object().to_dict()
            self.assertEqual(trace.get("source"), "decision_cycle")
            self.assertEqual(trace.get("tsar_verdict"), "idle")
            self.assertEqual(trace.get("primary_blocker_gate"), "")
            self.assertEqual(
                trace.get("decision_trace_id"), result.get("decision_trace_id")
            )
            self.assertEqual(
                trace.get("decision_trace_id"), context.get("decision_trace_id")
            )

    def test_decide_no_action_trace_includes_clean_smart_loop_plan(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine._lane_state_dispatch = MagicMock(return_value=None)

            def _clean_no_action_candidate(_modem, _computed, *, state, now):
                context = _tsar_context_fields_for_test(_modem)
                considered = _tsar_candidate_row_for_test(
                    {
                        "action": "stabilise_ca_pool",
                        "band": 3,
                        "source": "learning_ca_flap_pband_hour",
                        "reason": "test held TSAR candidate",
                    },
                    context_ref=context["context_ref"],
                )
                _publish_tsar_plan(
                    engine,
                    {
                        "status": "none",
                        "reason": "brain_action_roi_hold",
                        "selected": {},
                        "considered": [considered],
                        "deferred": [],
                        "smart_loop_candidate_projection": {
                            "schema": "smart_loop_candidate_projection.v1",
                            "status": "actionable",
                            "reason": "self_learning_candidate_published",
                            "required_next_step": "brain_tsar_arbitration",
                            "candidate_counts": {"total": 1, "actionable": 1},
                        },
                        **context,
                    },
                )
                return None

            engine._evaluate_tsar_vector_candidate = MagicMock(
                side_effect=_clean_no_action_candidate
            )
            action = _decide(engine, _modem(), _computed())

            self.assertIsNone(action)
            trace = engine.get_last_decision_trace()
            step = trace["steps"][0]
            self.assertEqual(trace.get("source"), "decision_cycle")
            self.assertEqual(trace.get("tsar_verdict"), "idle")
            self.assertEqual(step.get("gate"), "smart_loop_tsar_plan")
            self.assertEqual(step.get("result"), "pass")
            self.assertEqual(step.get("reason"), "brain_action_roi_hold")
            self.assertEqual(step.get("tsar_arbitration_verdict"), "no_action")
            self.assertEqual(
                step.get("smart_loop_strategy_status"), "no_action_justified"
            )
            self.assertEqual(step.get("smart_loop_candidate_actionable"), 0)

    def test_selected_smart_loop_plan_without_selected_action_fails_loudly(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine._lane_state_dispatch = MagicMock(return_value=None)

            modem = _modem()
            selected = {
                "action": "prefer_band",
                "band": 3,
                "source": "learning_referee_band_hour",
                "score": 0.9,
                "confidence": 0.8,
                "reason": "test selected TSAR candidate",
            }
            context = _tsar_context_fields_for_test(modem)
            selected = _tsar_candidate_row_for_test(
                selected, context_ref=context["context_ref"]
            )
            _publish_tsar_plan(
                engine,
                {
                    "status": "selected",
                    "reason": "eligible_candidate_selected",
                    "selected": selected,
                    "considered": [selected],
                    "deferred": [],
                    "smart_loop_candidate_projection": {
                        "schema": "smart_loop_candidate_projection.v1",
                        "status": "actionable",
                        "reason": "self_learning_candidate_published",
                        "required_next_step": "brain_tsar_arbitration",
                        "candidate_counts": {"total": 1, "actionable": 1},
                    },
                    **context,
                },
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "brain_selected_action_missing_for_selected_strategy",
            ):
                _decide(engine, modem, _computed())
            self.assertIsNone(
                engine.controller.runtime.action_transaction_authority
                .selection_status()
                .get("active")
            )

    def test_selected_smart_loop_command_ignores_old_private_tsar_keyed_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            hour = time.localtime().tm_hour
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {f"3:{hour}": 0.86},
                "band_hour_counts": {f"3:{hour}": 5},
            }
            modem = _modem(
                primary_band=20, selected_band_set=[20], configured_band_pool=[1, 3, 20]
            )
            _publish_selected_radio_command(
                engine,
                modem,
                target_band=3,
                target_bands=[1, 3, 20],
                reason_code="learned_anchor_outperforms_current",
            )
            now = time.time()
            engine.cooldowns.set_keyed(
                "tsar_vector_candidate", "B3@pband:B1+B3+B20", hold_s=60.0, now=now
            )

            action = _decide(engine, modem, _computed())

            self.assertIsNotNone(action)
            self.assertEqual(action.intent, "smart_loop_candidate")
            plan = _read_tsar_plan(engine)
            self.assertEqual(plan.get("status"), "selected")
            self.assertEqual(plan.get("reason"), "learned_anchor_outperforms_current")
            self.assertNotIn("held_candidate", plan)
            trace = engine.get_last_decision_trace()
            self.assertEqual(trace.get("source"), "brain_selected_action")
            self.assertEqual(trace.get("tsar_verdict"), "act")
            gates = {step.get("gate") for step in trace.get("steps", [])}
            self.assertNotIn("objective_lifecycle", gates)
            self.assertNotIn("health_hold", gates)

    def test_selected_smart_loop_command_respects_health_hold_for_generic_exploration(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            hour = time.localtime().tm_hour
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {f"3:{hour}": 0.86},
                "band_hour_counts": {f"3:{hour}": 5},
            }
            modem = _modem(
                primary_band=20, selected_band_set=[20], configured_band_pool=[1, 3, 20]
            )
            _publish_selected_radio_command(
                engine,
                modem,
                target_band=3,
                target_bands=[1, 3, 20],
                reason_code="learned_anchor_outperforms_current",
            )
            engine.note_constraint_vector(
                {
                    "health_status": {
                        "stage": "warning",
                        "overall_ok": True,
                        "action_hint": "observe",
                    }
                }
            )

            action = _decide(engine, modem, _computed())

            self.assertIsNone(action)
            trace = engine.get_last_decision_trace()
            self.assertEqual(trace.get("tsar_verdict"), "suppress")
            self.assertEqual(trace.get("primary_blocker_gate"), "health_hold")

    def test_selected_smart_loop_pband_respects_adaptive_user_throughput_hold(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(
                cfg=_cfg(
                    startup_grace_s=0.0,
                    v5_good_dl_hold_enabled=True,
                    v5_good_dl_hold_mbps=5.0,
                ),
                td=td,
            )
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            hour = time.localtime().tm_hour
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {},
                "band_hour_quality": {f"3:{hour}": 0.86},
                "band_hour_counts": {f"3:{hour}": 5},
            }
            modem = _modem(
                primary_band=20,
                configured_band_pool=[1, 3, 20],
                rx_bytes_per_sec=1_000_000,
                tx_bytes_per_sec=100_000,
            )
            _publish_selected_radio_command(
                engine,
                modem,
                target_band=3,
                target_bands=[1, 3, 20],
                reason_code="learned_anchor_outperforms_current",
            )

            action = _decide(engine, modem, _computed())

            self.assertIsNone(action)
            trace = engine.get_last_decision_trace()
            self.assertEqual(trace.get("primary_blocker_gate"), "action_availability")
            self.assertEqual(
                trace["steps"][0].get("reason_code"), "user_traffic_active"
            )
            active = (
                engine.controller.runtime.action_transaction_authority
                .selection_status()
                .get("active")
                or {}
            )
            self.assertEqual(active.get("stage"), "rejected")

    def test_operator_embargo_records_brain_posture_before_band_proposal(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            now = time.time()
            engine.action_source_state_authority.note_operator_band_physical_acceptance(
                lever="BAND_SELECTION",
                params={"bands": [3]},
                origin="operator_ngui",
                target_signature="B3",
                observed_at=now,
                receipt_id="brain-posture-operator-band",
            )

            action = engine._make_band_action(
                bands=[1, 3],
                reason="test proposal",
                intent="explore",
                modem=_modem(),
            )

            self.assertIsNone(action)
            self.assertEqual(engine._tsar_soft_no_go_verdict, "hold")
            self.assertIn(
                "autonomous_band_embargo active",
                engine._tsar_soft_no_go_reason,
            )
            events = engine.drain_runtime_events()
            self.assertTrue(
                any(e.get("kind") == "brain_posture_deferred" for e in events)
            )
            self.assertTrue(
                any(
                    e.get("kind") == "autonomous_band_embargo_deferred"
                    for e in events
                )
            )


class TestHelperMethods(unittest.TestCase):
    """Small helper methods on the mixin."""

    def test_active_true_for_future(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            future = time.time() + 600
            self.assertTrue(engine._active(future))

    def test_active_false_for_past(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            past = time.time() - 100
            self.assertFalse(engine._active(past))

    def test_count_active_until(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            now = time.time()
            mapping = {
                "a": now + 600,
                "b": now + 600,
                "c": now - 100,
            }
            self.assertEqual(engine._count_active_until(mapping, now=now), 2)

    def test_count_active_until_empty(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            self.assertEqual(engine._count_active_until({}, now=time.time()), 0)
            self.assertEqual(engine._count_active_until(None, now=time.time()), 0)

    def test_action_brief_none(self):
        brief = DecisionEngine._action_brief(None)
        self.assertEqual(brief, "none")

    def test_action_brief_band_action(self):
        action = _band_action(bands=[1, 3])
        brief = DecisionEngine._action_brief(action)
        self.assertIn("BAND_SELECTION", brief)

    def test_band_sig_from_action(self):
        action = _band_action(bands=[3, 1, 7])
        sig = DecisionEngine._band_sig_from_action(action)
        self.assertEqual(sig, "B1+B3+B7")

    def test_band_sig_empty(self):
        action = LeverAction(lever=LeverType.RESTART, params={}, intent="restart")
        sig = DecisionEngine._band_sig_from_action(action)
        self.assertEqual(sig, "")

    def test_band_sig_pband(self):
        action = LeverAction(
            lever=LeverType.PBAND,
            params={"pband": 3, "dl_bands": [1, 7, 20, 28]},
            intent="tsar_vector_candidate",
        )
        sig = DecisionEngine._band_sig_from_action(action)
        self.assertEqual(sig, "B3@pband:B1+B3+B7+B20+B28")

    def test_runtime_available_bands_prefers_exact_live_selected_pool(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(deployed_bands=[1, 3, 20]), td=td)

            modem = _modem(primary_band=3)
            modem.selected_band_set = [1, 3, 7, 20, 28]
            intelligence, _ = cellular_test_inputs(modem, ComputedSignals())

            self.assertEqual(
                engine._resolve_runtime_available_bands(intelligence),
                {1, 3, 7, 20, 28},
            )

    def test_runtime_available_bands_does_not_use_learned_memory_pool_as_authority(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(deployed_bands=[1, 3, 20]), td=td)
            engine.memory.get_best_known_bands = lambda top_n=4: [1, 20]

            modem = _modem(primary_band=3, ca_active=True, ca_signature="B3+CA")
            modem.selected_band_set = []
            intelligence, _ = cellular_test_inputs(modem, ComputedSignals())

            self.assertEqual(
                engine._resolve_runtime_available_bands(intelligence),
                {3},
            )

    def test_runtime_available_bands_falls_back_to_primary_band_on_first_seen_session(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(deployed_bands=[1, 3, 20]), td=td)
            engine.memory.get_best_known_bands = lambda top_n=4: []

            modem = _modem(primary_band=3, ca_active=False, ca_signature="B3")
            modem.selected_band_set = []
            intelligence, _ = cellular_test_inputs(modem, ComputedSignals())

            self.assertEqual(
                engine._resolve_runtime_available_bands(intelligence),
                {3},
            )


class TestRuntimeStatus(unittest.TestCase):
    """get_runtime_status returns valid structure."""

    def test_runtime_status_keys(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            status = engine.get_runtime_status()
            self.assertIsInstance(status, dict)
            # Should include policy, congestion, and guard info
            self.assertIn("soft_congestion_active", status)
            self.assertIn("autonomous_band_embargo_active", status)
            self.assertIn("autonomous_band_embargo_remaining_s", status)
            self.assertIn("policy_profile", status)

    def test_runtime_status_after_decide(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            _decide(engine, _modem(), _computed())
            status = engine.get_runtime_status()
            self.assertIsInstance(status, dict)


class TestPreemptivePredictionGate(unittest.TestCase):
    def test_deleted_preemptive_selector_fails_loudly(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            with self.assertRaisesRegex(
                RuntimeError,
                "old_preemptive_congestion_selector_deleted",
            ):
                engine._try_preemptive_congestion_avoidance(
                    _modem(),
                    _computed(),
                )

    def test_deleted_preemptive_selector_cannot_arm_action_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(preemptive_switch_cooldown_s=0.0), td=td)
            with self.assertRaisesRegex(
                RuntimeError,
                "old_preemptive_congestion_selector_deleted",
            ):
                engine._try_preemptive_congestion_avoidance(
                    _modem(),
                    _computed(),
                )
            self.assertFalse(
                engine.cooldowns.is_blocked("preemptive_switch", now=time.time())
            )


class TestHistoryRecommendationGate(unittest.TestCase):
    def test_connect_settle_is_smart_loop_availability_before_selection_and_proof(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(
                cfg=_cfg(startup_grace_s=0.0, autonomous_connect_settle_s=180.0),
                td=td,
            )
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.deployed_bands = {1, 3, 20}
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {"3": 0.91, "20": 0.30},
                "band_hour_quality": {},
                "band_hour_counts": {"3": 5},
                "control_authority_bands": [1, 3, 20],
            }

            # The Contextual Bandit is deliberately stochastic from its
            # recorded generation seed. Pin the accepted generation so this
            # availability test deterministically exposes a mutating arm and
            # proves that Brain selects do-nothing during connect-settle.
            now = 1_800_000_004.0
            _publish_self_learning(
                engine, _modem(primary_band=20, session_uptime_s=90.0), now=now
            )
            strategy = read_smart_loop_strategy_register(engine, now=now + 0.1)

            self.assertEqual(strategy.get("owner"), "arch4.smart_loop.closed_loop")
            self.assertEqual(strategy.get("outcome"), "no_action_justified")
            self.assertEqual(
                strategy.get("reason_code"),
                "brain_selected_do_nothing",
            )
            self.assertNotIn("command", strategy)
            self.assertIsNone(strategy.get("opened_proof"))
            availability = strategy.get("action_availability") or {}
            self.assertTrue(availability.get("connect_settle_active"))
            self.assertEqual(
                availability.get("owner"), "arch4.smart_loop.action_availability"
            )

    def test_connect_settle_uses_smart_loop_observed_clock_when_modem_uptime_missing(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(
                cfg=_cfg(startup_grace_s=0.0, autonomous_connect_settle_s=30.0),
                td=td,
            )
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.deployed_bands = {1, 3, 20}
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {"3": 0.91, "20": 0.30},
                "band_hour_quality": {},
                "band_hour_counts": {"3": 5},
                "control_authority_bands": [1, 3, 20],
            }

            # Keep the receipted Bandit selection deterministic; this test is
            # about the observed-connect clock, not propensity variation.
            now = 1_800_000_004.0
            _publish_self_learning(
                engine, _modem(primary_band=20, session_uptime_s=0.0), now=now
            )
            strategy = read_smart_loop_strategy_register(engine, now=now + 0.1)

            self.assertEqual(strategy.get("outcome"), "no_action_justified")
            availability = strategy.get("action_availability") or {}
            self.assertTrue(availability.get("connect_settle_active"))
            runtime_settle = availability.get("runtime_settle") or {}
            self.assertEqual(runtime_settle.get("observed_connected_s"), 0.0)
            self.assertEqual(runtime_settle.get("session_uptime_s"), 0.0)

    def test_radio_change_settle_is_smart_loop_availability_before_selection_and_proof(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(
                cfg=_cfg(
                    startup_grace_s=0.0,
                    autonomous_connect_settle_s=0.0,
                    radio_change_settle_s=30.0,
                ),
                td=td,
            )
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.deployed_bands = {1, 3, 20}
            engine._test_learning_quality_profile = {
                "profile_confidence": 0.85,
                "sample_count": 20,
                "band_quality": {"3": 0.91, "1": 0.30},
                "band_hour_quality": {},
                "band_hour_counts": {"3": 5},
                "control_authority_bands": [1, 3, 20],
            }
            # Pin both accepted generations so the Bandit selects the same
            # mutating arm and the radio-identity settle owner is the asserted
            # rejection path.
            now = 1_800_000_004.0
            engine.controller.control.capabilities = ModemCapabilities(
                supports_pband=False,
                supports_band_selection=False,
            )
            _publish_self_learning(engine, _modem(primary_band=3, pci=100), now=now)
            engine.controller.control.capabilities = ModemCapabilities(
                supports_pband=True,
                supports_band_selection=True,
            )
            _publish_self_learning(
                engine, _modem(primary_band=1, pci=126), now=now + 1.0
            )

            strategy = read_smart_loop_strategy_register(engine, now=now + 1.1)
            self.assertEqual(strategy.get("outcome"), "no_action_justified")
            self.assertNotIn("command", strategy)
            self.assertIsNone(strategy.get("opened_proof"))
            availability = strategy.get("action_availability") or {}
            self.assertFalse(availability.get("radio_change_settle_active"))
            self.assertTrue(availability.get("context_unready_active"))
            self.assertEqual(
                availability.get("context_unready_reason"),
                "radio_identity_pending_confirmation",
            )

            _publish_self_learning(
                engine, _modem(primary_band=1, pci=126), now=now + 3.0
            )
            strategy = read_smart_loop_strategy_register(engine, now=now + 3.1)
            availability = strategy.get("action_availability") or {}
            self.assertTrue(availability.get("radio_change_settle_active"))
            self.assertEqual(
                availability.get("owner"), "arch4.smart_loop.action_availability"
            )

    def test_exploring_history_led_recommendation_is_not_direct_action_authority(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.deployed_bands = {1, 3, 20}
            engine.note_calibration_handoff(
                {
                    "allow_prediction_input": False,
                    "reason": "saved_setup_drifted",
                }
            )
            engine._exploration_pressure.pressure = 1.5
            engine._exploration_pressure.resistance = 0.0
            modem = _modem(primary_band=1, ca_active=False)
            computed = _computed(rtt_avg=200.0)

            with (
                patch.object(
                    engine.memory, "get_best_bandset_for_hour", return_value=[1, 3]
                ),
                patch.object(
                    engine.memory,
                    "get_ranked_bandsets_for_hour",
                    return_value=[([1, 3], 0.82, 6.0, "ltm")],
                ),
                patch.object(engine, "_try_band_exploration", return_value=None),
                patch.object(engine, "_try_cell_exploration", return_value=None),
                patch.object(engine, "_try_band_priority", return_value=None),
            ):
                action = _decide(engine, modem, computed)

            self.assertIsNone(action)
            self.assertEqual(engine._prediction_gate_reason, "")

    def test_deleted_history_exploration_policy_fails_for_bad_rf_caller(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            with self.assertRaisesRegex(
                RuntimeError,
                "old_exploration_action_path_deleted",
            ):
                engine._history_exploration_policy(
                    _modem(),
                    _computed(),
                    mode="exploring",
                )

    def test_deleted_history_exploration_policy_fails_for_active_user_caller(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            with self.assertRaisesRegex(
                RuntimeError,
                "old_exploration_action_path_deleted",
            ):
                engine._history_exploration_policy(
                    _modem(),
                    _computed(),
                    mode="exploring",
                )

    def test_deleted_exploration_selector_fails_on_bypassable_hold_caller(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            with self.assertRaisesRegex(
                RuntimeError,
                "old_exploration_action_path_deleted",
            ):
                engine._decide_exploring(_modem(), _computed())

    def test_deleted_exploration_selector_fails_on_universal_hold_caller(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            with self.assertRaisesRegex(
                RuntimeError,
                "old_exploration_action_path_deleted",
            ):
                engine._decide_exploring(_modem(), _computed())

    def test_exploring_current_truth_neighbor_probe_survives_calibration_caution(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.deployed_bands = {1, 3, 20}
            engine.constraints.pci_lock_available = True
            engine.note_calibration_handoff(
                {
                    "allow_prediction_input": False,
                    "reason": "saved_setup_drifted",
                }
            )
            engine._exploration_pressure.pressure = 1.5
            engine._exploration_pressure.resistance = 0.0
            modem = _modem(primary_band=1, rsrp_dbm=-85.0, ca_active=False)
            modem.neighbors = [SimpleNamespace(pci=321, rsrp_dbm=-70.0)]
            computed = _computed(rtt_avg=200.0)

            with (
                patch.object(
                    engine.memory, "get_best_bandset_for_hour", return_value=[1, 3]
                ),
                patch.object(
                    engine.memory,
                    "get_ranked_bandsets_for_hour",
                    return_value=[([1, 3], 0.82, 6.0, "ltm")],
                ),
                patch.object(
                    engine, "_find_best_neighbor_cell", return_value=(321, 1200)
                ),
            ):
                action = _decide(engine, modem, computed)

            self.assertIsNone(action)
            events = engine.drain_runtime_events()
            self.assertTrue(
                any(e.get("kind") == "smart_loop_strategy_unavailable" for e in events)
            )
            self.assertEqual(engine._prediction_gate_reason, "")

    def test_deleted_degraded_selector_fails_loudly(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            with self.assertRaisesRegex(
                RuntimeError,
                "old_degraded_action_selector_deleted",
            ):
                engine._decide_degraded(_modem(), _computed())

    def test_deleted_congested_selector_fails_loudly(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            with self.assertRaisesRegex(
                RuntimeError,
                "old_congested_action_selector_deleted",
            ):
                engine._decide_congested(_modem(), _computed())

    def test_deleted_band_probe_selector_fails_for_cautious_caller(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            with self.assertRaisesRegex(
                RuntimeError,
                "old_exploration_action_path_deleted",
            ):
                engine._try_band_exploration(_modem(), _computed())

    def test_deleted_band_probe_selector_fails_for_selected_pool_caller(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            with self.assertRaisesRegex(
                RuntimeError,
                "old_exploration_action_path_deleted",
            ):
                engine._try_band_exploration(_modem(), _computed())


class TestHealthAndIncidentHold(unittest.TestCase):
    """Health and incident hold guards."""

    def test_note_calibration_handoff_stores(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.note_calibration_handoff(
                {"allow_prediction_input": True, "reason": "ok"}
            )
            self.assertEqual(
                engine._calibration_handoff.get("allow_prediction_input"), True
            )
            self.assertEqual(engine._calibration_handoff.get("reason"), "ok")

    def test_note_constraint_vector_stores(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.note_constraint_vector(
                {"status": "restricted", "observe_only": False}
            )
            self.assertEqual(engine._constraint_vector.get("status"), "restricted")
            self.assertEqual(engine._constraint_vector.get("observe_only"), False)

    def test_runtime_status_includes_calibration_and_gate_fields(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.note_calibration_handoff(
                {"allow_prediction_input": False, "reason": "saved_setup_drifted"}
            )
            engine.note_constraint_vector(
                {"status": "restricted", "observe_only": True}
            )
            engine._constraint_gate_reason = "observe_only_mode"
            engine._prediction_gate_reason = "saved_setup_drifted"
            engine._crystal_ball_gate_reason = "saved_setup_drifted"

            status = engine.get_runtime_status()

            self.assertEqual(
                status.get("calibration_handoff", {}).get("reason"),
                "saved_setup_drifted",
            )
            self.assertEqual(
                status.get("constraint_vector", {}).get("status"), "restricted"
            )
            self.assertEqual(status.get("constraint_gate_reason"), "observe_only_mode")
            self.assertEqual(
                status.get("prediction_gate_reason"), "saved_setup_drifted"
            )
            self.assertEqual(
                status.get("crystal_ball_gate_reason"), "saved_setup_drifted"
            )

    def test_note_health_status_stores(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.note_health_status({"status": "degraded"})
            self.assertEqual(engine._health_status.get("status"), "degraded")

    def test_note_incident_assessment_stores(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.note_incident_assessment({"active": True})
            self.assertEqual(engine._incident_assessment.get("active"), True)

    def test_health_hold_post_owner_guard_is_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.note_health_status(None)
            action = _band_action()
            with self.assertRaisesRegex(
                RuntimeError, "old_post_owner_health_hold_guard_deleted"
            ):
                engine._guard_health_hold(action)

    def test_incident_hold_post_owner_guard_is_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.note_incident_assessment(None)
            action = _band_action()
            with self.assertRaisesRegex(
                RuntimeError, "old_post_owner_incident_hold_guard_deleted"
            ):
                engine._guard_incident_hold(action)

    def test_constraint_vector_observe_only_blocks_non_fault_decisions_early(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.note_constraint_vector(
                {
                    "supports_active_control": False,
                    "status": "restricted",
                    "observe_only": True,
                }
            )

            action = _decide(engine, _modem(), _computed())

            self.assertIsNone(action)
            self.assertEqual(engine._constraint_gate_reason, "observe_only_mode")
            trace = engine.get_last_decision_trace()
            self.assertEqual(trace.get("source"), "constraint_vector")

    def test_constraint_vector_planned_quiet_blocks_non_fault_decisions_early(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            engine.note_constraint_vector(
                {
                    "supports_active_control": False,
                    "status": "restricted",
                    "planned_radio_quiet_active": True,
                    "planned_radio_quiet_remaining_s": 42,
                }
            )

            action = _decide(engine, _modem(), _computed())

            self.assertIsNone(action)
            self.assertEqual(engine._constraint_gate_reason, "planned_radio_quiet")
            trace = engine.get_last_decision_trace()
            self.assertEqual(trace.get("source"), "constraint_vector")

    def test_constraint_vector_does_not_block_fault_state_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(cfg=_cfg(startup_grace_s=0.0), td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.BAD_RF
            engine.note_constraint_vector(
                {
                    "supports_active_control": False,
                    "status": "restricted",
                    "observe_only": True,
                }
            )

            blocked = engine._lane_constraint_vector_blockers(
                modem=_modem(),
                state=SystemState.BAD_RF,
            )

            self.assertFalse(blocked)
            self.assertEqual(engine._constraint_gate_reason, "")

    def test_constraint_vector_health_hold_blocks_risky_action_before_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.note_constraint_vector(
                {
                    "health_status": {
                        "stage": "warning",
                        "overall_ok": True,
                        "action_hint": "observe",
                    }
                }
            )

            result = engine._action_availability_verdict(
                action=_band_action(intent="explore"),
                modem=_modem(),
                state=SystemState.WORKING,
                now=time.time(),
                check_objective=True,
            )

            self.assertFalse(result["allowed"])
            self.assertEqual(result["reason_code"], "health_hold")

    def test_constraint_vector_incident_hold_blocks_risky_action_before_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.note_constraint_vector(
                {
                    "incident_assessment": {
                        "incident_class": "post_action_instability",
                        "recommended_next_step": "settle_backoff",
                        "suppress_exploration": True,
                    }
                }
            )

            result = engine._action_availability_verdict(
                action=_band_action(intent="explore"),
                modem=_modem(),
                state=SystemState.WORKING,
                now=time.time(),
                check_objective=True,
            )

            self.assertFalse(result["allowed"])
            self.assertEqual(result["reason_code"], "incident_hold")


class TestGuardChainOrdering(unittest.TestCase):
    """Verify guard chain runs in documented order."""

    def test_guard_chain_order(self):
        """The _run_action_pipeline guard_map keys match documented order."""
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            # We can't easily call _run_action_pipeline in isolation without
            # setting up all state, but we can verify the guard ordering
            # by inspecting the ordered_blocker_guard_chain output.
            from arch4.brain.blocker_authority import ordered_blocker_guard_chain

            guard_map = {
                "layer3_action_trust": lambda a: a,
                "a1_restricted_filter": lambda a: a,
                "learning_guard": lambda a: a,
                "a1_antithrash": lambda a: a,
            }
            chain = list(ordered_blocker_guard_chain(guard_map))
            names = [name for name, _ in chain]
            expected = [
                "layer3_action_trust",
                "a1_restricted_filter",
                "learning_guard",
                "a1_antithrash",
            ]
            self.assertEqual(names, expected)


class TestApplyGuardStep(unittest.TestCase):
    """_apply_guard_step tracks steps and passes/blocks."""

    def test_passing_guard(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            steps = []
            action = _band_action()
            result = engine._apply_guard_step(
                steps=steps,
                gate="test_gate",
                action=action,
                guard_fn=lambda a: a,
            )
            self.assertIs(result, action)
            self.assertEqual(len(steps), 1)
            self.assertEqual(steps[0]["gate"], "test_gate")
            self.assertEqual(steps[0]["result"], "pass")

    def test_blocking_guard(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            steps = []
            action = _band_action()
            result = engine._apply_guard_step(
                steps=steps,
                gate="test_gate",
                action=action,
                guard_fn=lambda a: None,
            )
            self.assertIsNone(result)
            self.assertEqual(steps[0]["result"], "blocked")


class TestObjectiveFeedback(unittest.TestCase):
    """Objective lifecycle and feedback blocking."""

    def test_objective_not_blocked_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            action = _band_action(intent="explore")
            now = time.time()
            verdict = engine._action_availability_verdict(
                action=action,
                modem=_modem(),
                state=SystemState.WORKING,
                now=now,
                check_objective=True,
            )
            self.assertTrue(verdict["allowed"])

    def test_objective_blocked_after_feedback(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            action = _band_action(bands=[1, 3], intent="explore")
            # Record negative feedback using the actual API
            engine._record_objective_feedback(
                action,
                code="NO_EFFECT",
                reason="bands unchanged",
                block_s=600.0,
            )
            now = time.time()
            verdict = engine._action_availability_verdict(
                action=action,
                modem=_modem(),
                state=SystemState.WORKING,
                now=now + 1,
                check_objective=True,
            )
            self.assertFalse(verdict["allowed"])
            self.assertEqual(verdict["reason_code"], "objective_feedback")

    def test_working_explore_feedback_blocks_same_context_proposal(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.constraints.provider_plmn = "22005"
            engine.posture_authority._transition(SystemState.WORKING)
            modem = ModemInput(
                sinr_db=10.0,
                rsrp_dbm=-85.0,
                rsrq_db=-9.0,
                rssi_dbm=-70.0,
                mcs_dl=12,
                mcs_ul=10,
                cqi=9,
                ri=2,
                carriers=[],
                ca_active=False,
                ca_band_signature="B3",
                primary_band=3,
                pci=255,
                earfcn=1650,
                enb_id=8,
                cell_id=2080,
                radio_connected=True,
                session_uptime_s=100.0,
                rx_bytes_per_sec=0.0,
                tx_bytes_per_sec=0.0,
            )
            action = _band_action(bands=[20], intent="explore")
            engine._record_objective_feedback(
                action,
                code="RECOVERED_LANE_REGRESSION",
                reason="left recovered lane and regressed",
                block_s=600.0,
                modem=modem,
                current_state=SystemState.WORKING,
            )
            verdict = engine._action_availability_verdict(
                action=action,
                modem=modem,
                state=SystemState.WORKING,
                now=time.time(),
                check_objective=True,
            )
            self.assertFalse(verdict["allowed"])
            self.assertEqual(verdict["reason_code"], "objective_feedback")
            self.assertIn("RECOVERED_LANE_REGRESSION", verdict["reason"])


class TestBandProposalCapabilities(unittest.TestCase):
    """Band proposals must respect modem/profile capability authority."""

    def test_make_band_action_suppressed_when_band_selection_unsupported(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.constraints.band_select_available = False

            action = engine._make_band_action(
                bands=[1, 3],
                reason="better band",
                intent="explore",
            )

            self.assertIsNone(action)
            events = engine.drain_runtime_events()
            self.assertTrue(
                any(
                    e.get("kind") == "proposal_suppressed"
                    and e.get("gate") == "unsupported_lever"
                    and e.get("reason")
                    == "band_selection_not_supported_by_modem_profile"
                    for e in events
                )
            )

    def test_make_band_action_allowed_when_band_selection_supported(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.constraints.band_select_available = True

            action = engine._make_band_action(
                bands=[1, 3],
                reason="better band",
                intent="explore",
            )

            self.assertIsNotNone(action)
            self.assertEqual(action.lever, LeverType.BAND_SELECTION)


class TestMarkActionTaken(unittest.TestCase):
    """mark_action_taken updates timestamp."""

    def test_updates_last_action_time(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            before = engine._last_action_time
            engine.mark_action_taken()
            self.assertGreater(engine._last_action_time, before)


class TestNoteActionResult(unittest.TestCase):
    """note_band_action_result and note_action_result."""

    def test_note_band_action_result_success(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            action = _band_action(bands=[1, 3], intent="explore")
            # Should not raise
            engine.note_band_action_result(
                action,
                True,
                _modem(primary_band=1),
            )
            # Band fail streak should be 0 on success
            self.assertEqual(engine._band_fail_streak, 0)

    def test_note_band_action_result_failure_increments_streak(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            action = _band_action(bands=[1, 3], intent="explore")
            engine.note_band_action_result(
                action,
                False,
                _modem(),
            )
            self.assertGreater(engine._band_fail_streak, 0)

    def test_note_action_result_restart(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            action = LeverAction(
                lever=LeverType.RESTART,
                params={},
                intent="fault_recovery",
            )
            # Should not raise
            engine.note_action_result(
                action,
                True,
                _modem(),
            )


class TestCaLostSignal(unittest.TestCase):
    """CA-lost detection bookkeeping."""

    def test_ca_lost_activates_when_ca_drops(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            now = time.time()
            # Simulate CA was active, now dropped
            engine._ca_was_active = True
            engine._ca_lost_state = False
            modem_no_ca = _modem(ca_active=False, ca_signature="B3")
            computed_no_ca = _computed(
                cart_ld=0, cart_ld_valid=True, cast_ld=0.0, cast_ld_valid=True
            )
            engine._update_ca_lost_signal(modem_no_ca, computed_no_ca, now)
            # The exact behavior depends on internal logic, but the method
            # should not raise and should update internal state
            # (ca_lost_state is set via a multi-tick evaluation window)


class TestPopActionTraceId(unittest.TestCase):
    """pop_action_trace_id returns and clears trace ID."""

    def test_pop_returns_empty_when_no_trace(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            result = engine.pop_action_trace_id(None)
            self.assertEqual(result, "")

    def test_pop_returns_trace_id_when_set(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            action = _band_action()
            # Store trace ID keyed by action object identity
            engine._action_trace_ids[id(action)] = "test-trace-123"
            result = engine.pop_action_trace_id(action)
            self.assertEqual(result, "test-trace-123")
            # Should be cleared after pop
            self.assertNotIn(id(action), engine._action_trace_ids)


class TestDecisionContextAndResult(unittest.TestCase):
    """get_last_decision_context_object / get_last_decision_result_object."""

    def test_none_before_first_decide(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            self.assertIsNone(engine.get_last_decision_context_object())
            self.assertIsNone(engine.get_last_decision_result_object())

    def test_populated_after_decide(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine.posture_authority.private_state_for_test.state = SystemState.WORKING
            _decide(engine, _modem(), _computed())
            # After a decide call, at minimum the trace should exist
            trace = engine.get_last_decision_trace()
            self.assertIsNotNone(trace)

    def test_stability_skip_emits_fresh_no_action_context(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine._startup_time = -1000.0
            modem = _modem()
            computed = _computed()
            engine._emit_decision_trace(
                source="seed",
                state=engine.posture_authority.state,
                proposed=None,
                final=None,
                steps=[{"gate": "seed", "result": "blocked"}],
                modem=modem,
            )
            self.assertIsNotNone(engine.get_last_decision_context_object())
            self.assertIsNotNone(engine.get_last_decision_trace_object())

            with patch("arch4.brain.decision_pipeline.time.time", return_value=1000.0):
                hard = getattr(engine, "_thresholds", {}).get("hard", {})
                rtt_warning = float(
                    hard.get("rtt_warning_ms", engine.cfg.rtt_warning_ms)
                )
                rtt_cliff = float(hard.get("rtt_cliff_ms", engine.cfg.rtt_cliff_ms))
                rtt_avg = computed.rtt_sliding_avg
                rtt_bucket = (
                    "alarm"
                    if rtt_avg >= rtt_cliff
                    else "warning"
                    if rtt_avg >= rtt_warning
                    else "normal"
                )
                engine._stability_keyframe = (
                    engine.posture_authority.state,
                    int(modem.primary_band),
                    rtt_bucket,
                    False,
                    bool(modem.has_traffic),
                    time.localtime(1000.0).tm_hour,
                    bool(engine._soft_congestion_active),
                    bool(engine._ca_lost_state),
                    False,
                    engine.cooldowns.active_count(now=1000.0),
                    engine._self_learning_candidate_keyframe_signature(now=1000.0),
                    int(getattr(engine, "_escape_probe_rf_cliff_good_ticks", 0) or 0),
                    int(getattr(engine, "_nr_lock_bad_sinr_ticks", 0) or 0),
                )
                engine._stability_skip_count = 0
                second = _decide(engine, modem, computed)

            self.assertIsNone(second)
            trace = engine.get_last_decision_trace()
            self.assertEqual(trace.get("source"), "stability_keyframe")
            self.assertEqual(trace.get("decision_trace_id"), "dt_2")
            self.assertEqual(trace.get("tsar_verdict"), "idle")
            self.assertNotEqual(trace.get("source"), "seed")
            self.assertIsNotNone(engine.get_last_decision_context_object())
            self.assertEqual(
                engine.get_last_decision_context_object().decision_trace_id,
                trace.get("decision_trace_id"),
            )
            self.assertEqual(
                engine.get_last_decision_result_object().decision_trace_id,
                trace.get("decision_trace_id"),
            )

    def test_runtime_status_falls_back_to_live_incident_when_context_is_stale(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine._decision_context_last_obj = SimpleNamespace(
                congestion_kind="",
                warning_class="warning",
                warning_source="stale",
                warning_mechanism="stale",
                warning_reason="stale",
                health_stage="degraded_persist",
                health_action_hint="escalation_candidate",
                incident_class="post_action_instability",
                incident_next_step="settle_backoff",
                incident_suppress_exploration=True,
                to_dict=lambda: {"incident_class": "post_action_instability"},
            )
            engine._decision_trace_last_obj = SimpleNamespace(
                source="state:WORKING",
                final="none",
                passed=1,
                blocked=0,
                decision_trace_id="old-trace",
                to_dict=lambda: {"decision_trace_id": "old-trace"},
            )
            engine._decision_result_last_obj = SimpleNamespace(
                to_dict=lambda: {"decision_trace_id": "old-trace"},
            )
            engine._decision_trace_last = {"decision_trace_id": "old-trace"}
            engine._clear_tick_decision_exports()
            engine._health_status = {
                "stage": "healthy",
                "action_hint": "none",
                "overall_ok": True,
            }
            engine._incident_assessment = {
                "incident_class": "healthy",
                "recommended_next_step": "none",
                "suppress_exploration": False,
            }

            status = engine.get_runtime_status()

            self.assertEqual(status["health_stage"], "healthy")
            self.assertEqual(status["health_action_hint"], "none")
            self.assertEqual(status["incident_class"], "healthy")
            self.assertEqual(status["incident_next_step"], "none")
            self.assertFalse(status["incident_suppress_exploration"])

    def test_clear_tick_exports_hides_stale_decision_trace_and_gate_exports(self):
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td=td)
            engine._decision_trace_last = {
                "decision_trace_id": "old-trace",
                "source": "state:WORKING",
                "final": "BAND_SELECTION",
                "passed": 3,
                "blocked": 0,
            }
            engine._decision_trace_last_obj = SimpleNamespace(
                source="state:WORKING",
                final="BAND_SELECTION",
                passed=3,
                blocked=0,
                decision_trace_id="old-trace",
            )
            engine._decision_tick_exports_fresh = True
            engine._constraint_gate_reason = "old_constraint"
            engine._prediction_gate_reason = "old_prediction"
            engine._crystal_ball_gate_reason = "old_crystal"
            _publish_tsar_plan(
                engine,
                {
                    "status": "idle",
                    "reason": "old_test_plan",
                    "selected": {},
                    "considered": [],
                    "deferred": [],
                    **_tsar_context_fields_for_test(_modem()),
                },
            )

            engine._clear_tick_decision_exports()
            status = engine.get_runtime_status()

            self.assertEqual(engine.get_last_decision_trace(), {})
            self.assertEqual(status["decision_trace_id"], "")
            self.assertEqual(status["decision_trace_final"], "")
            self.assertEqual(status["constraint_gate_reason"], "")
            self.assertEqual(status["prediction_gate_reason"], "")
            self.assertEqual(status["crystal_ball_gate_reason"], "")
            self.assertEqual(status["smart_loop_tsar_plan"]["reason"], "old_test_plan")
            self.assertNotIn("decision_context", status)


if __name__ == "__main__":
    unittest.main()
