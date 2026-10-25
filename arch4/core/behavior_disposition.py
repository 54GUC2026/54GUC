"""Audit list for kept and removed behavior. Each kept behavior names its current producer
and proof. Each removed path has a static ban, so old code cannot silently return.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any


OWNER = "arch4.core.behavior_disposition"
SCHEMA = "BehaviorDisposition.v1"
CATALOG_SCHEMA = "BehaviorDispositionCatalog.v1"
CATALOG_VERSION = "vB5.3-to-vC1-core-command-cutover-2026-07-19"
ATTESTATION_SCHEMA = "BehaviorAttestation.v1"

_CLASSIFICATIONS = frozenset(
    {
        "safety_invariant",
        "provider_policy",
        "optimization",
        "service_maintenance",
        "operator_only",
        "retired",
    }
)
_DISPOSITIONS = frozenset(
    {
        "retained_constraint",
        "s4b_source_fact",
        "smart_loop_forecast",
        "explicit_request",
        "fault_lane_retained",
        "retired_no_replacement",
    }
)


@dataclass(frozen=True, slots=True)
class BehaviorDisposition:
    behavior_id: str
    classification: str
    source_owner: str
    disposition: str
    action_families: tuple[str, ...] = ()
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["schema"] = SCHEMA
        row["owner"] = OWNER
        row["action_families"] = list(self.action_families)
        return row


_ROWS: tuple[BehaviorDisposition, ...] = (
    BehaviorDisposition(
        "hard_fault_radio_down_recovery",
        "safety_invariant",
        "arch4.brain.decision_faults",
        "fault_lane_retained",
        ("RESTART", "BAND_SELECTION", "PBAND"),
        "Emergency containment remains outside normal optimization.",
    ),
    BehaviorDisposition(
        "hard_fault_session_reset_recovery",
        "safety_invariant",
        "arch4.brain.decision_faults",
        "fault_lane_retained",
        ("RESTART",),
        "Backend/session recovery remains explicit emergency work.",
    ),
    BehaviorDisposition(
        "hard_fault_congestion_storm_recovery",
        "safety_invariant",
        "arch4.brain.decision_faults",
        "fault_lane_retained",
        ("BAND_SELECTION", "PBAND", "CELL_LOCK"),
        "Emergency congestion escape remains evidence gated.",
    ),
    BehaviorDisposition(
        "bouncer_non_overridable_hard_stop",
        "safety_invariant",
        "arch4.core.bouncer",
        "retained_constraint",
        (),
        "Safety rejection is not an opportunity producer.",
    ),
    BehaviorDisposition(
        "bouncer_contextual_autonomy_policy",
        "optimization",
        "arch4.core.extension_hooks",
        "retained_constraint",
        ("BAND_SELECTION", "BAND_LOCK", "PBAND", "CELL_LOCK", "RESTART"),
        "Contextual policy is an Intelligence hook used only for autonomous requests.",
    ),
    BehaviorDisposition(
        "bouncer_manual_override",
        "retired",
        "arch4.bouncer_manual_override",
        "retired_no_replacement",
        (),
        "No manual path may override the Core Bouncer safety receipt.",
    ),
    BehaviorDisposition(
        "retired_side_action_guard_chain",
        "retired",
        "arch4.core.action_guards",
        "retired_no_replacement",
        (),
        "Core Bouncer, CommandAuthority and action-source policy replace duplicate runtime-cycle guards.",
    ),
    BehaviorDisposition(
        "action_settle_and_expected_effect_hold",
        "safety_invariant",
        "arch4.action_expected_effect_mask",
        "retained_constraint",
        (),
        "Expected transient effects constrain selection and posture.",
    ),
    BehaviorDisposition(
        "radio_stability_hold_policy",
        "safety_invariant",
        "arch4.core.action_source_state_authority",
        "s4b_source_fact",
        (),
        "Human cooldown and accepted-transition stability hold constrain radio actions by origin and emergency intent.",
    ),
    BehaviorDisposition(
        "a1_restricted_night_band_pool",
        "provider_policy",
        "arch4.policy.a1_b818_windows",
        "s4b_source_fact",
        ("BAND_SELECTION", "PBAND"),
        "Provider window constrains legal source admission.",
    ),
    BehaviorDisposition(
        "a1_overnight_exploration_budget",
        "provider_policy",
        "arch4.policy.a1_b818_sequences",
        "s4b_source_fact",
        ("BAND_SELECTION", "PBAND"),
        "Exploration budget becomes typed provider policy evidence.",
    ),
    BehaviorDisposition(
        "a1_night_hold_best_band",
        "provider_policy",
        "arch4.policy.a1_b818_sequences",
        "s4b_source_fact",
        ("BAND_SELECTION", "PBAND"),
        "Night hold becomes a policy row plus blocked alternatives.",
    ),
    BehaviorDisposition(
        "a1_bad_rf_b20_rescue",
        "provider_policy",
        "arch4.policy.a1_b818_sequences",
        "s4b_source_fact",
        ("BAND_SELECTION", "PBAND"),
        "One-shot B20 rescue remains provider-scoped recovery evidence.",
    ),
    BehaviorDisposition(
        "a1_morning_b3_ca_sequence",
        "provider_policy",
        "arch4.policy.a1_b818_sequences",
        "s4b_source_fact",
        ("BAND_SELECTION", "PBAND"),
        "Timed B3 to CA restoration sequence becomes maintenance rows.",
    ),
    BehaviorDisposition(
        "a1_radio_drop_big_dip_sequence",
        "provider_policy",
        "arch4.policy.a1_b818_policy",
        "s4b_source_fact",
        ("BAND_SELECTION", "PBAND"),
        "Provider-specific drop recovery remains explicit recovery evidence.",
    ),
    BehaviorDisposition(
        "a1_antithrash_freeze_and_retest",
        "provider_policy",
        "arch4.policy.a1_b818_antithrash",
        "s4b_source_fact",
        ("BAND_SELECTION", "PBAND"),
        "Freeze, strict steps and one retest become typed policy constraints.",
    ),
    BehaviorDisposition(
        "working_restore_known_good_ca",
        "optimization",
        "arch4.brain.decision_working",
        "s4b_source_fact",
        ("BAND_SELECTION", "PBAND"),
        "Known-good CA restoration is recovery evidence, not direct action.",
    ),
    BehaviorDisposition(
        "working_preemptive_congestion_avoidance",
        "optimization",
        "arch4.smart_loop.closed_loop",
        "smart_loop_forecast",
        ("BAND_SELECTION", "PBAND"),
        "Crystal Ball and history remain forecast evidence.",
    ),
    BehaviorDisposition(
        "anchor_enforcement",
        "optimization",
        "arch4.brain.decision_exploring",
        "s4b_source_fact",
        ("PBAND", "BAND_SELECTION"),
        "Anchor correction becomes an explicit policy/optimization row.",
    ),
    BehaviorDisposition(
        "degraded_best_live_neighbor",
        "optimization",
        "arch4.brain.decision_degraded",
        "s4b_source_fact",
        ("CELL_LOCK",),
        "Neighbor evidence remains visible before selection.",
    ),
    BehaviorDisposition(
        "degraded_open_all_deployed_bands",
        "optimization",
        "arch4.brain.decision_degraded",
        "s4b_source_fact",
        ("BAND_SELECTION", "PBAND"),
        "Recovery broadening becomes an explicit recovery row.",
    ),
    BehaviorDisposition(
        "congested_neighbor_escape",
        "optimization",
        "arch4.brain.decision_congestion",
        "s4b_source_fact",
        ("CELL_LOCK",),
        "Congestion cell escape retains stronger evidence and cooldown gates.",
    ),
    BehaviorDisposition(
        "congested_restore_known_good_ca",
        "optimization",
        "arch4.brain.decision_congestion",
        "s4b_source_fact",
        ("BAND_SELECTION", "PBAND"),
        "CA restoration remains a recovery opportunity.",
    ),
    BehaviorDisposition(
        "systematic_bad_rf_band_probe",
        "optimization",
        "arch4.brain.decision_night_training",
        "s4b_source_fact",
        ("BAND_SELECTION", "PBAND"),
        "Probe, dwell, best and hold state become typed source facts.",
    ),
    BehaviorDisposition(
        "ranked_band_exploration",
        "optimization",
        "arch4.smart_loop.closed_loop",
        "smart_loop_forecast",
        ("BAND_SELECTION", "PBAND"),
        "Learned band and ROI ranking stays in SmartLoop.",
    ),
    BehaviorDisposition(
        "cell_exploration_and_optimization",
        "optimization",
        "arch4.brain.decision_exploring",
        "s4b_source_fact",
        ("CELL_LOCK", "BAND_SELECTION", "PBAND"),
        "Live and durable cell evidence becomes a deterministic lock or mined best-effort steering row.",
    ),
    BehaviorDisposition(
        "band_priority_rotation",
        "optimization",
        "arch4.brain.decision_exploring",
        "s4b_source_fact",
        ("BAND_SELECTION", "PBAND"),
        "Priority rotation becomes an explicit bounded radio shape.",
    ),
    BehaviorDisposition(
        "nr_capacity_coverage_preference",
        "optimization",
        "arch4.brain.decision_support",
        "s4b_source_fact",
        ("NR_BAND_LOCK",),
        "NR preference and rollback require typed observation evidence.",
    ),
    BehaviorDisposition(
        "cell_lock_renew_release",
        "service_maintenance",
        "arch4.brain.decision_exploring",
        "s4b_source_fact",
        ("CELL_LOCK",),
        "Lease health, renewal and release become maintenance rows.",
    ),
    BehaviorDisposition(
        "idle_rrc_keepalive",
        "service_maintenance",
        "arch4.brain.decision_exploring",
        "s4b_source_fact",
        ("TRAFFIC_PRESENCE",),
        "Idle-window evidence governs keepalive enablement.",
    ),
    BehaviorDisposition(
        "scheduled_band_plan",
        "service_maintenance",
        "arch4.foundation.schedule_service",
        "explicit_request",
        ("BAND_SELECTION", "PBAND"),
        "Foundation owns schedule state and submits due typed Core commands.",
    ),
    BehaviorDisposition(
        "watchdog_restart",
        "service_maintenance",
        "54guc.foundation.modem_watchdog",
        "explicit_request",
        ("RESTART",),
        "Foundation submits one typed restart through the sole Core command gateway.",
    ),
    BehaviorDisposition(
        "operator_manual_cellular_actions",
        "operator_only",
        "54guc.core.command_gateway",
        "explicit_request",
        (
            "BAND_SELECTION",
            "BAND_LOCK",
            "PBAND",
            "CELL_LOCK",
            "NR_BAND_LOCK",
            "RESTART",
            "TRAFFIC_PRESENCE",
            "TRAFFIC_VOLUME",
            "UE_CAPABILITY",
        ),
        "Confirmed operator intent enters Core without Intelligence dependency.",
    ),
    BehaviorDisposition(
        "sms_restart_command",
        "operator_only",
        "54guc.core.command_gateway",
        "explicit_request",
        ("RESTART",),
        "Confirmed SMS intent enters the sole Core command gateway.",
    ),
    BehaviorDisposition(
        "direct_prediction_action",
        "retired",
        "arch4.brain.decision_working",
        "retired_no_replacement",
        (),
        "Prediction is evidence for SmartLoop and cannot actuate directly.",
    ),
    BehaviorDisposition(
        "normal_state_first_match_action_selection",
        "retired",
        "arch4.policy.state_dispatch_policy",
        "retired_no_replacement",
        (),
        "Complete ledger, Bandit and Brain replace first-match selection.",
    ),
    BehaviorDisposition(
        "automatic_traffic_volume_shaping",
        "retired",
        "arch4.brain.decision_exploring",
        "retired_no_replacement",
        ("TRAFFIC_VOLUME",),
        "Traffic-volume mutation remains operator or explicit schedule only.",
    ),
)


_NORMAL_PRODUCER = "arch4.smart_loop.normal_behavior_opportunities.build_normal_behavior_source_facts"
_NORMAL_PROOF = "arch4/tests/test_normal_behavior_opportunities.py"
_STATIC_PROOF = "arch4/tests/test_workspace_hardening_rules.py"

_ATTESTATIONS: dict[str, dict[str, Any]] = {
    "hard_fault_radio_down_recovery": {
        "canonical_owner": "arch4.brain.decision_faults",
        "producer_entrypoint": "DecisionFaultsMixin._decide_fault_state:E_RADIO_DOWN",
        "proof_tests": ["arch4/intelligence/tests/test_decision_pipeline.py"],
    },
    "hard_fault_session_reset_recovery": {
        "canonical_owner": "arch4.brain.decision_faults",
        "producer_entrypoint": "DecisionFaultsMixin._decide_fault_state:E_SESSION_RESET",
        "proof_tests": ["arch4/intelligence/tests/test_decision_pipeline.py"],
    },
    "hard_fault_congestion_storm_recovery": {
        "canonical_owner": "arch4.brain.decision_faults",
        "producer_entrypoint": "DecisionFaultsMixin._decide_fault_state:E_CONGESTION_STORM",
        "proof_tests": ["arch4/intelligence/tests/test_decision_pipeline.py"],
    },
    "bouncer_non_overridable_hard_stop": {
        "canonical_owner": "arch4.core.bouncer",
        "producer_entrypoint": "CoreBouncer.evaluate_intent",
        "proof_tests": ["arch4/tests/test_bouncer.py"],
    },
    "bouncer_contextual_autonomy_policy": {
        "canonical_owner": "arch4.new_horizon.reasoning",
        "producer_entrypoint": "AdvisoryReasoning.build_proposal",
        "proof_tests": ["arch4/tests/test_new_horizon_phase3.py"],
    },
    "bouncer_manual_override": {
        "canonical_owner": "retired",
        "producer_entrypoint": "",
        "proof_tests": ["arch4/tests/test_core_command_static_audit.py"],
        "static_bans": [
            "arch4/bouncer_manual_override.py",
            "build_bouncer_manual_override",
        ],
    },
    "retired_side_action_guard_chain": {
        "canonical_owner": "retired",
        "producer_entrypoint": "",
        "proof_tests": ["arch4/tests/test_core_command_static_audit.py"],
        "static_bans": [
            "suppress_unsupported_action",
            "suppress_action_during_unstable_lockout",
            "suppress_mutating_inflight",
            "suppress_action_modem_unhealthy",
            "suppress_routeros_auto_radio_write",
            "suppress_action_by_layer3_trust",
        ],
    },
    "action_settle_and_expected_effect_hold": {
        "canonical_owner": "arch4.smart_loop.action_availability",
        "producer_entrypoint": "build_action_availability",
        "proof_tests": ["arch4/intelligence/tests/test_smart_loop_closed_loop.py"],
    },
    "radio_stability_hold_policy": {
        "canonical_owner": "arch4.core.action_source_state_authority",
        "producer_entrypoint": "ActionSourceStateAuthority.band_action_policy_status",
        "proof_tests": ["arch4/tests/test_band_action_policy.py"],
    },
    "a1_restricted_night_band_pool": {
        "canonical_owner": "arch4.action_opportunity_sources",
        "producer_entrypoint": "_provider_constraint_facts",
        "proof_tests": ["arch4/tests/test_action_opportunity_sources.py"],
    },
    "a1_overnight_exploration_budget": {
        "canonical_owner": "arch4.core.action_source_state_authority",
        "producer_entrypoint": "ActionSourceStateAuthority._provider_facts",
        "proof_tests": ["arch4/intelligence/tests/test_action_opportunity_source_facts.py"],
    },
    "a1_night_hold_best_band": {
        "canonical_owner": "arch4.core.action_source_state_authority",
        "producer_entrypoint": "ActionSourceStateAuthority._provider_facts",
        "proof_tests": ["arch4/intelligence/tests/test_action_opportunity_source_facts.py"],
    },
    "a1_bad_rf_b20_rescue": {
        "canonical_owner": "arch4.core.action_source_state_authority",
        "producer_entrypoint": "ActionSourceStateAuthority._provider_facts",
        "proof_tests": ["arch4/intelligence/tests/test_action_opportunity_source_facts.py"],
    },
    "a1_morning_b3_ca_sequence": {
        "canonical_owner": "arch4.core.action_source_state_authority",
        "producer_entrypoint": "ActionSourceStateAuthority._provider_facts",
        "proof_tests": ["arch4/intelligence/tests/test_action_opportunity_source_facts.py"],
    },
    "a1_radio_drop_big_dip_sequence": {
        "canonical_owner": "arch4.core.action_source_state_authority",
        "producer_entrypoint": "ActionSourceStateAuthority._provider_facts",
        "proof_tests": ["arch4/intelligence/tests/test_action_opportunity_source_facts.py"],
    },
    "a1_antithrash_freeze_and_retest": {
        "canonical_owner": "arch4.core.action_source_state_authority",
        "producer_entrypoint": "ActionSourceStateAuthority._provider_facts",
        "proof_tests": ["arch4/intelligence/tests/test_action_opportunity_source_facts.py"],
    },
    **{
        behavior_id: {
            "canonical_owner": "arch4.smart_loop.normal_behavior_opportunities",
            "producer_entrypoint": _NORMAL_PRODUCER,
            "proof_tests": [_NORMAL_PROOF],
        }
        for behavior_id in (
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
        )
    },
    "working_preemptive_congestion_avoidance": {
        "canonical_owner": "arch4.smart_loop.opportunity_matrix",
        "producer_entrypoint": "build_opportunity_matrix:crystal_ball_future",
        "proof_tests": ["arch4/intelligence/tests/test_smart_loop_closed_loop.py"],
    },
    "ranked_band_exploration": {
        "canonical_owner": "arch4.smart_loop.opportunity_matrix",
        "producer_entrypoint": "build_opportunity_matrix:learned_band_score",
        "proof_tests": ["arch4/intelligence/tests/test_smart_loop_closed_loop.py"],
    },
    "cell_lock_renew_release": {
        "canonical_owner": "arch4.core.action_source_state_authority",
        "producer_entrypoint": "ActionSourceStateAuthority._cell_lock_lease_fact",
        "proof_tests": ["arch4/intelligence/tests/test_action_opportunity_source_facts.py"],
    },
    "idle_rrc_keepalive": {
        "canonical_owner": "arch4.core.action_source_state_authority",
        "producer_entrypoint": "ActionSourceStateAuthority._keepalive_fact",
        "proof_tests": ["arch4/intelligence/tests/test_action_opportunity_source_facts.py"],
    },
    "scheduled_band_plan": {
        "canonical_owner": "arch4.foundation.schedule_service",
        "producer_entrypoint": "FoundationScheduleService.service_once",
        "proof_tests": ["arch4/tests/test_core_schedule_command_route.py"],
    },
    "watchdog_restart": {
        "canonical_owner": "54guc.foundation.modem_watchdog",
        "producer_entrypoint": "FoundationModemWatchdog._submit_restart",
        "proof_tests": ["arch4/tests/test_foundation_modem_watchdog.py"],
    },
    "operator_manual_cellular_actions": {
        "canonical_owner": "54guc.core.command_gateway",
        "producer_entrypoint": "CoreCommandGateway.submit",
        "proof_tests": ["arch4/tests/test_core_command_gateway.py"],
    },
    "sms_restart_command": {
        "canonical_owner": "54guc.core.command_gateway",
        "producer_entrypoint": "_submit_sms_core_commands",
        "proof_tests": ["arch4/tests/test_core_command_gateway.py"],
    },
    "direct_prediction_action": {
        "canonical_owner": "retired",
        "producer_entrypoint": "",
        "proof_tests": [_STATIC_PROOF],
        "static_bans": ["prediction_direct_modem_actuation", "_try_preemptive_congestion_avoidance:return_action"],
    },
    "normal_state_first_match_action_selection": {
        "canonical_owner": "retired",
        "producer_entrypoint": "",
        "proof_tests": [_STATIC_PROOF],
        "static_bans": ["run_state_dispatch:non_fault", "old_non_fault_autonomous_action_owner"],
    },
    "automatic_traffic_volume_shaping": {
        "canonical_owner": "retired",
        "producer_entrypoint": "",
        "proof_tests": [_STATIC_PROOF],
        "static_bans": ["automatic_traffic_volume_shaping", "autonomous:TRAFFIC_VOLUME"],
    },
}


def behavior_disposition_catalog() -> dict[str, Any]:
    rows = []
    for disposition in _ROWS:
        row = disposition.as_dict()
        attestation = dict(_ATTESTATIONS.get(disposition.behavior_id) or {})
        row["attestation"] = {
            "schema": ATTESTATION_SCHEMA,
            "status": (
                "retired"
                if disposition.disposition == "retired_no_replacement"
                else "active"
            ),
            "canonical_owner": str(attestation.get("canonical_owner") or ""),
            "producer_entrypoint": str(
                attestation.get("producer_entrypoint") or ""
            ),
            "proof_tests": list(attestation.get("proof_tests") or []),
            "static_bans": list(attestation.get("static_bans") or []),
        }
        rows.append(row)
    content = {
        "schema": CATALOG_SCHEMA,
        "owner": OWNER,
        "catalog_version": CATALOG_VERSION,
        "attestation_schema": ATTESTATION_SCHEMA,
        "rows": rows,
        "behavior_count": len(rows),
        "classifications": sorted(_CLASSIFICATIONS),
    }
    return {**content, "catalog_checksum": _digest(content)}


def behavior_disposition_catalog_error(
    value: dict[str, Any] | None,
) -> str:
    row = dict(value or {})
    if row.get("schema") != CATALOG_SCHEMA or row.get("owner") != OWNER:
        return "behavior_catalog_owner_schema_invalid"
    content = {key: item for key, item in row.items() if key != "catalog_checksum"}
    if str(row.get("catalog_checksum") or "") != _digest(content):
        return "behavior_catalog_checksum_invalid"
    rows = row.get("rows")
    if not isinstance(rows, list) or len(rows) != int(
        row.get("behavior_count") or -1
    ):
        return "behavior_catalog_rows_invalid"
    identifiers: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            return "behavior_catalog_row_invalid"
        behavior_id = str(item.get("behavior_id") or "")
        if not behavior_id:
            return "behavior_catalog_id_missing"
        if item.get("schema") != SCHEMA or item.get("owner") != OWNER:
            return "behavior_catalog_row_owner_invalid"
        if str(item.get("classification") or "") not in _CLASSIFICATIONS:
            return "behavior_catalog_classification_invalid"
        if str(item.get("disposition") or "") not in _DISPOSITIONS:
            return "behavior_catalog_disposition_invalid"
        attestation = item.get("attestation")
        if not isinstance(attestation, dict):
            return "behavior_catalog_attestation_missing"
        if attestation.get("schema") != ATTESTATION_SCHEMA:
            return "behavior_catalog_attestation_schema_invalid"
        status = str(attestation.get("status") or "")
        expected_status = (
            "retired"
            if str(item.get("disposition") or "") == "retired_no_replacement"
            else "active"
        )
        if status != expected_status:
            return "behavior_catalog_attestation_status_invalid"
        if not str(attestation.get("canonical_owner") or ""):
            return "behavior_catalog_attestation_owner_missing"
        proof_tests = attestation.get("proof_tests")
        if not isinstance(proof_tests, list) or not proof_tests or any(
            not str(value or "") for value in proof_tests
        ):
            return "behavior_catalog_attestation_tests_missing"
        if status == "active" and not str(
            attestation.get("producer_entrypoint") or ""
        ):
            return "behavior_catalog_attestation_producer_missing"
        static_bans = attestation.get("static_bans")
        if status == "retired" and (
            not isinstance(static_bans, list)
            or not static_bans
            or any(not str(value or "") for value in static_bans)
        ):
            return "behavior_catalog_attestation_ban_missing"
        identifiers.append(behavior_id)
    if len(set(identifiers)) != len(identifiers):
        return "behavior_catalog_id_duplicate"
    return ""


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "CATALOG_SCHEMA",
    "CATALOG_VERSION",
    "ATTESTATION_SCHEMA",
    "BehaviorDisposition",
    "OWNER",
    "SCHEMA",
    "behavior_disposition_catalog",
    "behavior_disposition_catalog_error",
]
