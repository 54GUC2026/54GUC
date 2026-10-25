from __future__ import annotations

import pytest

from arch4.action_opportunity_set import (
    ActionOpportunitySourceSnapshot,
    build_action_opportunity_set,
)
from arch4.action_opportunity_source_facts import (
    ActionOpportunitySourceFact,
    ActionOpportunitySourceFactSet,
    action_opportunity_source_fact_set_error,
)
from arch4.evidence_scope import EvidenceScope, FormulaVector


IDENTITY = "0x0000000000000101"
SESSION = "session-1"
EPOCH = "boot-1"


def _scope(sequence: int = 10) -> dict:
    return EvidenceScope(
        identity_code=IDENTITY,
        identity_status="exact",
        identity_catalog_version="identity-catalog-v1",
        identity_catalog_checksum="a" * 64,
        session_scope_key=SESSION,
        source_epoch=EPOCH,
        source_sequence=sequence,
        observed_at=1000.0 + sequence,
        evidence_age_s=0.1,
        confidence=1.0,
        evidence_count=1,
        formula_vector=FormulaVector.build({"source_facts": "v1"}),
    ).as_dict()


def test_retired_provider_action_entrypoints_fail_loudly() -> None:
    from arch4.policy.a1_b818_antithrash import (
        apply_a1_restricted_band_filter,
        guard_a1_antithrash,
    )
    from arch4.policy.a1_b818_policy import (
        decide_a1_b818_big_dip,
        maybe_ca_restore_sequence,
        suppress_a1_provider_daytime_band_action,
    )
    from arch4.policy.a1_b818_sequences import (
        a1_morning_routine_guard_status,
        decide_a1_b818_morning_sequence,
        decide_a1_overnight_recovery,
        note_a1_overnight_action,
    )

    retired = (
        (decide_a1_overnight_recovery, (None, None, 1.0), "old_provider_overnight_action_owner_deleted"),
        (decide_a1_b818_morning_sequence, (None, None, 1.0), "old_provider_morning_action_owner_deleted"),
        (a1_morning_routine_guard_status, (None, 1.0), "old_provider_morning_guard_owner_deleted"),
        (note_a1_overnight_action, (None, None, 1.0), "old_provider_result_writer_deleted"),
        (maybe_ca_restore_sequence, (None, None, None, 1.0), "old_provider_ca_restore_action_owner_deleted"),
        (decide_a1_b818_big_dip, (None, None, 1.0), "old_provider_radio_drop_action_owner_deleted"),
        (apply_a1_restricted_band_filter, (None, None, None, 1.0), "old_post_owner_provider_restricted_filter_deleted"),
        (guard_a1_antithrash, (None, None, None, 1.0), "old_provider_antithrash_guard_owner_deleted"),
    )
    for function, args, reason in retired:
        with pytest.raises(RuntimeError, match=reason):
            function(*args)
    with pytest.raises(
        RuntimeError,
        match="old_provider_daytime_filter_owner_deleted",
    ):
        suppress_a1_provider_daytime_band_action(None, None, now=1.0)


def test_fact_set_is_checksummed_sorted_and_deeply_immutable() -> None:
    fact = ActionOpportunitySourceFact(
        behavior_id="radio_stability_hold_policy",
        fact_kind="constraint",
        source_owner="test.owner",
        state_id="manual:1",
        active=True,
        reason="test",
        source_admitted=True,
        evidence={"nested": {"value": 1}},
        applies_to={"levers": ["PBAND"]},
    )
    fact_set = ActionOpportunitySourceFactSet(
        identity_code=IDENTITY,
        session_scope_key=SESSION,
        source_epoch=EPOCH,
        source_sequence=10,
        facts=(fact,),
    )
    row = fact_set.as_dict()
    assert action_opportunity_source_fact_set_error(row) == ""
    row["facts"][0]["reason"] = "corrupt"
    assert (
        action_opportunity_source_fact_set_error(row)
        == "source_fact_set_checksum_invalid"
    )
    with pytest.raises(
        TypeError,
        match="action_opportunity_source_fact_is_immutable",
    ):
        fact.evidence["nested"]["value"] = 2


def test_fact_scope_mismatch_fails_loudly() -> None:
    facts = ActionOpportunitySourceFactSet(
        identity_code=IDENTITY,
        session_scope_key=SESSION,
        source_epoch=EPOCH,
        source_sequence=9,
    )
    with pytest.raises(
        ValueError,
        match="action_opportunity_source_fact_scope_invalid:source_sequence_mismatch",
    ):
        build_action_opportunity_set(
            learning_context_key="ctx-1",
            action_context_key="action:ctx-1",
            evidence_scope=_scope(sequence=10),
            source_snapshot=ActionOpportunitySourceSnapshot(
                capabilities={},
                current={},
                source_facts=facts.as_dict(),
            ),
        )
