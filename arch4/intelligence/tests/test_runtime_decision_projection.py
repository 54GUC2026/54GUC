from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arch4.payload_budget import retained_payload_bytes
from arch4.runtime_decision_projection import (
    BOUNCER_PROJECTION_OWNER,
    BOUNCER_PROJECTION_SCHEMA,
    BOUNCER_SAFETY_SCHEMA,
    BOUNCER_SOURCE_OWNER,
    MAX_RUNTIME_DECISION_PROJECTION_BYTES,
    OWNER,
    SCHEMA,
    RuntimeDecisionProjectionError,
    build_runtime_decision_projection,
)
from arch4.runtime_registers import REGISTER_DECISION, RuntimeRegisterBank
from arch4.smart_loop.action_availability import build_action_availability


ROOT = Path(__file__).resolve().parents[1]


def _brain_bouncer_projection(
    *,
    hard_stop_active: bool,
    state: str = "blocked",
    trip_reason: str = "unknown_intent_blocked",
) -> dict[str, object]:
    return {
        "schema": BOUNCER_PROJECTION_SCHEMA,
        "owner": BOUNCER_PROJECTION_OWNER,
        "source_owner": BOUNCER_SOURCE_OWNER,
        "state": state,
        "tripped": state == "blocked",
        "trip_reason": trip_reason,
        "trip_source": (
            "bouncer_hard_stop_latch"
            if hard_stop_active
            else "constraint_vector"
        ),
        "reentry_state": "waiting",
        "reentry_condition": "explicit recovery required",
        "hard_stop_latch": {
            "active": hard_stop_active,
            "epoch": 7,
            "source_generation": 324,
            "safety_available": True,
        },
    }


def _good_projection() -> dict[str, object]:
    return build_runtime_decision_projection(
        {
            "brain": {
                "state": "WORKING",
                "autonomous_band_embargo_active": True,
                "autonomous_band_embargo_remaining_s": 875.0,
                "normal_policy_safety": {
                    "throughput_hold": False,
                    "incident_class": "radio_ca_loss",
                    "known_outage_hold": False,
                },
                "ca_recovery_trend": {"hold_restore_ca": True},
                "threshold_authority": {"hard": {"rtt_warning_ms": 150.0}},
            },
            "brain_vectors": {
                "prediction_vector": {
                    "prediction_posture": "normal",
                    "prediction_modifier": 0.8,
                    "evidence_count": 42,
                    "slots": 14,
                    "predictive_history_prior": {
                        "status": "active",
                        "fact_count": 18,
                    },
                },
                "learning_vector": {
                    "status": "observing",
                    "evidence_count": 22,
                },
                "brain_tsar": {"mode": "prediction_available"},
            },
            "self_learning_strategy": {
                "bandit_decision": {
                    "decision_semantic": "stay_current_anchor",
                    "selected_action": "stay_current_anchor",
                    "confidence": 0.85,
                    "evidence_count": 200,
                },
                "candidate_counts": {"total": 1},
                "action_availability": {"reasons": []},
                "state": {"recent_windows": {"observation_count": 142}},
            },
            "smart_loop_authority": {
                "status": "no_action_justified",
                "candidate_count": 1,
                "reason": "current_anchor_already_best",
            },
            "beta_soak": {
                "status_label": "Soaking",
                "release_channel": "mainline",
                "history_window_days": 10,
                "history_target_days": 14,
            },
        }
    )


def test_projection_is_exact_bounded_and_excludes_large_owner_payloads() -> None:
    source = {
        "brain": {
            "state": "WORKING",
            "normal_policy_safety": {"throughput_hold": False},
            "unbounded_private_history": [
                {"sample": index, "blob": "x" * 8_192} for index in range(400)
            ],
        },
        "brain_vectors": {
            "prediction_vector": {"evidence_count": 42},
            "private_candidate_matrix": [list(range(500)) for _ in range(500)],
        },
        "self_learning_strategy": {
            "bandit_decision": {"selected_action": "stay_current_anchor"},
            "private_history": ["y" * 8_192 for _ in range(400)],
        },
        "smart_loop_authority": {"status": "active", "candidate_count": 1},
    }

    projection = build_runtime_decision_projection(source)

    assert set(projection) == {
        "schema",
        "owner",
        "action_availability",
        "prediction",
        "smart_loop",
        "learning",
        "history",
        "dashboard",
    }
    assert projection["schema"] == SCHEMA
    assert projection["owner"] == OWNER
    assert projection["smart_loop"]["selected_action"] == "stay_current_anchor"
    assert "brain" not in projection
    assert "brain_vectors" not in projection
    assert "self_learning_strategy" not in projection
    assert (
        retained_payload_bytes(
            projection,
            max_bytes=MAX_RUNTIME_DECISION_PROJECTION_BYTES,
            label="test Decision projection",
        )
        < MAX_RUNTIME_DECISION_PROJECTION_BYTES
    )


@pytest.mark.parametrize(
    "illegal",
    (
        {"brain": {"state": "WORKING"}},
        {
            **_good_projection(),
            "brain": {"state": "WORKING"},
        },
        {
            **_good_projection(),
            "smart_loop": {
                **_good_projection()["smart_loop"],
                "private_candidate_matrix": [],
            },
        },
    ),
)
def test_register_hard_fails_old_or_undeclared_decision_shapes_and_retains_last_good(
    illegal: dict[str, object],
) -> None:
    bank = RuntimeRegisterBank()
    good = _good_projection()
    accepted = bank.write_decision(
        value=good,
        source="decision_engine",
        updated_at=100.0,
    )

    with pytest.raises(RuntimeDecisionProjectionError):
        bank.write_decision(
            value=illegal,
            source="decision_engine",
            updated_at=101.0,
        )

    retained = bank.read(REGISTER_DECISION)
    assert retained.sequence == accepted.sequence
    assert retained.signature == accepted.signature
    assert retained.value == good


def test_smart_loop_consumer_rejects_old_broad_decision_shape() -> None:
    with pytest.raises(ValueError, match="runtime_decision_projection_required"):
        build_action_availability(
            decision={"brain": {"autonomous_band_embargo_active": True}},
            mutation={},
            health={},
            now=100.0,
        )


def test_durable_bouncer_latch_reaches_policy_before_another_action_is_built() -> None:
    projection = build_runtime_decision_projection(
        {
            "brain_bouncer": _brain_bouncer_projection(hard_stop_active=True),
            "brain_vectors": {
                "brain_tsar": {"bouncer_state": "idle"},
            },
        }
    )

    safety = projection["action_availability"]["bouncer_safety"]
    assert safety == {
        "schema": BOUNCER_SAFETY_SCHEMA,
        "owner": OWNER,
        "source_owner": BOUNCER_SOURCE_OWNER,
        "state": "blocked",
        "tripped": True,
        "trip_reason": "unknown_intent_blocked",
        "trip_source": "bouncer_hard_stop_latch",
        "reentry_state": "waiting",
        "reentry_condition": "explicit recovery required",
        "hard_stop_active": True,
        "hard_stop_epoch": 7,
        "hard_stop_source_generation": 324,
        "safety_available": True,
    }

    availability = build_action_availability(
        decision=projection,
        mutation={},
        health={},
        now=100.0,
    )
    assert availability["decision_allowed"] is False
    assert availability["safety_block"] == (
        "bouncer_hard_stop:unknown_intent_blocked"
    )
    assert availability["reasons"] == [
        "bouncer_hard_stop:unknown_intent_blocked"
    ]
    assert projection["smart_loop"]["bouncer"] == "blocked"


def test_tick_local_bouncer_block_does_not_fabricate_a_durable_latch() -> None:
    projection = build_runtime_decision_projection(
        {
            "brain_bouncer": _brain_bouncer_projection(
                hard_stop_active=False,
                trip_reason="planned_radio_quiet",
            )
        }
    )

    availability = build_action_availability(
        decision=projection,
        mutation={},
        health={},
        now=100.0,
    )
    assert availability["decision_allowed"] is True
    assert availability["safety_block"] == ""
    assert availability["reasons"] == []
    assert availability["bouncer_safety"]["state"] == "blocked"
    assert availability["bouncer_safety"]["hard_stop_active"] is False


def test_decision_projection_rejects_old_tick_only_bouncer_shape() -> None:
    with pytest.raises(
        RuntimeDecisionProjectionError,
        match="canonical_brain_bouncer_projection_required",
    ):
        build_runtime_decision_projection(
            {"brain_bouncer": {"state": "idle", "tripped": False}}
        )


def test_static_audit_prevents_broad_decision_paths_from_returning() -> None:
    runtime_registers = (ROOT / "runtime_registers.py").read_text(encoding="utf-8")
    ngui_projection = (ROOT / "ngui" / "runtime_projection.py").read_text(
        encoding="utf-8"
    )
    smartness = (ROOT / "smartness_fact_register.py").read_text(encoding="utf-8")
    consumers = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "smart_loop/action_availability.py",
            "smart_loop/capacity_probe.py",
            "smart_loop/normal_behavior_opportunities.py",
        )
    )

    assert "return normalize_runtime_decision_projection(value)" in runtime_registers
    intelligence_projection = (
        ROOT / "intelligence" / "ngui_extension.py"
    ).read_text(encoding="utf-8")
    assert "decision = build_runtime_decision_projection(status)" in intelligence_projection
    for forbidden in (
        'REGISTER_DECISION, "brain.',
        'REGISTER_DECISION, "brain_vectors.',
        'REGISTER_DECISION, "self_learning_strategy.',
        'REGISTER_DECISION, "smart_loop_authority.',
    ):
        assert forbidden not in ngui_projection
    for forbidden in (
        'decision.get("brain")',
        'decision.get("brain_vectors")',
        'decision.get("self_learning_strategy")',
        'decision.get("smart_loop_authority")',
    ):
        assert forbidden not in consumers
    assert "normalize_runtime_decision_projection(value)" in smartness
    assert 'source["brain"]' not in smartness
    assert 'source["brain_vectors"]' not in smartness


def test_static_audit_requires_canonical_bouncer_status_at_every_projection_boundary() -> None:
    decision_projection = (ROOT / "runtime_decision_projection.py").read_text(
        encoding="utf-8"
    )
    availability = (
        ROOT / "smart_loop" / "action_availability.py"
    ).read_text(encoding="utf-8")

    assert not (ROOT / "engine_finalize_brain.py").exists()
    assert not (ROOT / "engine_runtime_status.py").exists()
    assert "canonical_brain_bouncer_projection_required" in decision_projection
    assert 'bouncer_safety.get("hard_stop_active")' in availability
    assert 'decision_row.get("brain_bouncer")' not in availability

    production_callers: list[tuple[str, set[str]]] = []
    for path in sorted(ROOT.rglob("*.py")):
        if "tests" in path.relative_to(ROOT).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (
                isinstance(node.func, ast.Name)
                and node.func.id == "build_brain_bouncer_section"
            ):
                continue
            production_callers.append(
                (
                    str(path.relative_to(ROOT)),
                    {
                        str(keyword.arg)
                        for keyword in node.keywords
                        if keyword.arg is not None
                    },
                )
            )
    assert production_callers == []
    opportunity_sources = (ROOT / "action_opportunity_sources.py").read_text(
        encoding="utf-8"
    )
    assert "_global_optimization_constraint_facts" not in opportunity_sources
    assert "_global_autonomous_constraint_facts" in opportunity_sources
    assert "constraint_bouncer" not in decision_projection + opportunity_sources
    assert 'brain_tsar.get("bouncer_state")' not in decision_projection
