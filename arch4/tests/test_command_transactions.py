"""Wave 3 behavior proofs for the typed cellular Command Authority."""

from __future__ import annotations

import json
from inspect import getsource
from dataclasses import replace
from types import SimpleNamespace

import pytest

from arch4.action_origin import ActionOriginKind
from arch4.action_transaction import (
    bind_action_transaction_key,
    build_action_transaction_key,
)
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from arch4.command_transactions import (
    COMMAND_AUTHORITY_STATE_FILENAME,
    COMMAND_AUTHORITY_STATE_SCHEMA,
    CommandAuthority,
    CommandAuthorityStateRecord,
    CommandLifecycleStage,
    CommandRootState,
    CommandTerminalReceipt,
    CommandTerminalStatus,
    TransactionFamily,
    build_cellular_transaction,
)
from arch4.runtime_validation.active_cgf_runtime_validator import (
    read_command_authority_state,
)
from arch4.core_contracts.command_targets import (
    CoreBandSetTarget,
    CoreCommandTarget,
    CorePBandTarget,
    CoreWifiRadioTarget,
)
from arch4.core_contracts.action_reachability import (
    ActionReachabilityDisposition,
    ActionReachabilityUnavailable,
    ActionReachabilityReceipt,
    ReachabilityProbeSample,
)
from arch4.core_contracts.clients import CoreClientRole
from arch4.core_contracts.commands import (
    CoreCommandExecutionReceipt,
    CoreCommandOrigin,
    CoreCommandRequest,
    core_command_request_root,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core.lever_action_adapter import (
    CoreLeverActionAdapter,
    lever_action_authority,
)
from arch4.core.action_runtime import publish_command_terminal_evidence
from arch4.core_contracts.physical_commands import (
    PhysicalCommandDispatchEvidence,
    PhysicalCommandReceipt,
)
from arch4.interfaces import LeverAction, LeverType
from arch4.radio_target_contract import (
    RadioTargetFacts,
    TargetConfirmationReceipt,
)


SESSION = "session-test"
IDENTITY = "modem-test"
SOURCE_GENERATION = 1


def _matrix(
    generation: int,
    *,
    session_scope_key: str = SESSION,
) -> MatrixGenerationIdentity:
    return MatrixGenerationIdentity(
        namespace=RuntimeGenerationNamespace(
            session_scope_key=session_scope_key,
            source_epoch="test-source-epoch",
            lifecycle_generation=1,
        ),
        matrix_sequence=generation,
        matrix_revision=(f"{generation:x}" * 64)[:64],
    )


def _default_target(lever: LeverType) -> CoreCommandTarget:
    if lever is LeverType.PBAND:
        return CorePBandTarget(pband=3, ca_pool=(1, 3, 20))
    if lever is LeverType.BAND_SELECTION:
        return CoreBandSetTarget(
            bands=(3,),
            command_kind="BAND_SELECTION",
        )
    raise ValueError(f"unsupported_test_lever:{lever.name}")


def _action(
    lever: LeverType = LeverType.BAND_SELECTION,
    *,
    target: CoreCommandTarget | None = None,
    origin_kind: ActionOriginKind = ActionOriginKind.OPERATOR_NGUI,
    source: str = "test_manual",
    generation: int = SOURCE_GENERATION,
    command_id: str = "operator-command",
    session_scope_key: str = SESSION,
) -> LeverAction:
    typed_target = target or _default_target(lever)
    if typed_target.command_kind != lever.name:
        raise ValueError("test_target_lever_mismatch")
    operator = origin_kind is ActionOriginKind.OPERATOR_NGUI
    request = CoreCommandRequest(
        request_id=command_id,
        correlation_id=f"correlation:{command_id}",
        client_id=(
            "ngui.operator" if operator else "intelligence-primary"
        ),
        client_role=(
            CoreClientRole.OPERATOR
            if operator
            else CoreClientRole.AUTONOMOUS_INTELLIGENCE
        ),
        origin=(
            CoreCommandOrigin.OPERATOR
            if operator
            else CoreCommandOrigin.AUTONOMOUS_INTELLIGENCE
        ),
        matrix_identity=_matrix(
            generation,
            session_scope_key=session_scope_key,
        ),
        identity_code="identity-code-test",
        command_kind=lever.name,
        target=typed_target,
        parameters=CoreFrozenPayload.from_mapping(
            {
                "reason": "test command",
                "intent": "manual" if operator else "autonomous",
                "objective_id": f"correlation:{command_id}",
                "action_parameters": {},
                **(
                    {}
                    if operator
                    else {"autonomous_source": "smart_loop"}
                ),
            }
        ),
        capability_reference=lever_action_authority(
            lever
        ).capability_key,
        created_at=100.0,
        expires_at=400.0,
        confirmed=operator,
        safety_context=CoreFrozenPayload.empty(),
        autonomous_lease="" if operator else "test-lease",
    )
    action = CoreLeverActionAdapter().materialize(request)
    setattr(action, "_test_core_request", request)
    return bind_action_transaction_key(
        action,
        build_action_transaction_key(
            action,
            matrix_identity=request.matrix_identity,
            identity_key=IDENTITY,
            source=source,
            origin_kind=origin_kind.value,
        ),
    )


def _dispatch(
    authority: CommandAuthority,
    action: LeverAction,
    *,
    owner: str = "core_command_gateway",
    recovery_timing=None,
):
    request = getattr(action, "_test_core_request")
    authority.receive_core_command(request, now=99.0)
    authority.advance_core_command(
        request_id=request.request_id,
        expected=CommandRootState.RECEIVED,
        target=CommandRootState.VALIDATED,
        now=99.1,
    )
    authority.advance_core_command(
        request_id=request.request_id,
        expected=CommandRootState.VALIDATED,
        target=CommandRootState.ADMITTED,
        now=99.2,
    )
    admission = authority.claim_cellular_action(
        action,
        execution_owner=owner,
        now=100.0,
        recovery_timing=recovery_timing,
    )
    assert admission.accepted is True
    assert admission.lease is not None
    _ack_projection(authority, at=100.0)
    authority.mark_dispatched(
        admission.lease,
        receipt={"owner": owner},
        now=101.0,
    )
    _ack_projection(authority, at=101.0)
    authority.advance_core_command(
        request_id=request.request_id,
        expected=CommandRootState.ADMITTED,
        target=CommandRootState.DISPATCHED,
        now=101.1,
        execution_receipt=CoreCommandExecutionReceipt(
            request_id=request.request_id,
            command_root_id=core_command_request_root(request),
            matrix_identity=request.matrix_identity,
            submitted=True,
            reason="submitted_async",
            physical_action_root_id=admission.lease.root_id,
            occurred_at=101.1,
        ),
    )
    return admission.lease


def _claim_only(
    authority: CommandAuthority,
    action: LeverAction,
    *,
    owner: str = "core_command_gateway",
):
    request = getattr(action, "_test_core_request")
    authority.receive_core_command(request, now=99.0)
    authority.advance_core_command(
        request_id=request.request_id,
        expected=CommandRootState.RECEIVED,
        target=CommandRootState.VALIDATED,
        now=99.1,
    )
    authority.advance_core_command(
        request_id=request.request_id,
        expected=CommandRootState.VALIDATED,
        target=CommandRootState.ADMITTED,
        now=99.2,
    )
    admission = authority.claim_cellular_action(
        action,
        execution_owner=owner,
        now=100.0,
    )
    assert admission.accepted is True
    assert admission.lease is not None
    _ack_projection(authority, at=100.0)
    return admission.lease


def _ack_projection(
    authority: CommandAuthority,
    *,
    at: float,
) -> None:
    obligation = authority.pending_projection_obligation()
    assert obligation is not None
    authority.acknowledge_projection(
        obligation_id=obligation.obligation_id,
        payload_fingerprint=obligation.payload_fingerprint,
        register_sequence=obligation.authority_sequence,
        register_signature=obligation.payload_fingerprint,
        acknowledged_at=max(float(at), obligation.created_at),
    )


def _stage(
    *,
    kind: str,
    ordinal: int,
    target: CoreCommandTarget,
    dispatched_at: float,
    returned_at: float,
    outcome: str = "accepted",
) -> PhysicalCommandDispatchEvidence:
    return PhysicalCommandDispatchEvidence(
        stage_kind=kind,
        ordinal=ordinal,
        request_dispatched_at=dispatched_at,
        transport_returned_at=returned_at,
        outcome=outcome,
        reason=(
            "confirmed_post_ok"
            if outcome == "accepted"
            else "physical_stage_rejected"
        ),
        detail={},
        target=target,
    )


def _single_stage_receipt(
    lease,
    *,
    accepted: bool = True,
    reason: str = "modem_command_accepted",
    completed_at: float = 102.0,
) -> PhysicalCommandReceipt:
    target = lease.transaction.action_key.target
    return PhysicalCommandReceipt(
        request_id=lease.transaction.command_id,
        transaction_root_id=lease.root_id,
        target=target,
        execution_started_at=101.0,
        transaction_completed_at=completed_at,
        accepted=accepted,
        reason=reason,
        stages=(
            _stage(
                kind=target.command_kind.lower(),
                ordinal=1,
                target=target,
                dispatched_at=101.1,
                returned_at=101.5,
                outcome="accepted" if accepted else "rejected",
            ),
        ),
    )


def _pband_receipt(
    lease,
    *,
    second_outcome: str = "accepted",
) -> PhysicalCommandReceipt:
    target = lease.transaction.action_key.target
    assert isinstance(target, CorePBandTarget)
    accepted = second_outcome == "accepted"
    return PhysicalCommandReceipt(
        request_id=lease.transaction.command_id,
        transaction_root_id=lease.root_id,
        target=target,
        execution_started_at=101.0,
        transaction_completed_at=102.0,
        accepted=accepted,
        reason=(
            "pband_and_ca_pool_accepted"
            if accepted
            else "ca_pool_write_rejected"
        ),
        stages=(
            _stage(
                kind="pband_anchor",
                ordinal=1,
                target=CorePBandTarget(pband=target.pband),
                dispatched_at=101.1,
                returned_at=101.3,
            ),
            _stage(
                kind="ca_pool",
                ordinal=2,
                target=CoreBandSetTarget(
                    bands=target.ca_pool,
                    command_kind="BAND_SELECTION",
                ),
                dispatched_at=101.4,
                returned_at=101.8,
                outcome=second_outcome,
            ),
        ),
    )


def _accepted_radio_lifecycle(
    authority: CommandAuthority,
    action: LeverAction | None = None,
):
    lease = _dispatch(authority, action or _action())
    transition = authority.acknowledge_modem_result(
        lease,
        physical_receipt=_single_stage_receipt(lease),
        now=102.0,
    )
    assert transition.active is not None
    assert transition.active.stage is CommandLifecycleStage.ACCEPTED
    _ack_projection(authority, at=102.0)
    return lease


def _target_receipt(lease, *, generation: int = 2) -> TargetConfirmationReceipt:
    return TargetConfirmationReceipt(
        request_id=lease.transaction.command_id,
        transaction_root_id=lease.root_id,
        matrix_identity=replace(
            lease.transaction.matrix_identity,
            matrix_sequence=generation,
            matrix_revision=(f"{generation:x}" * 64)[:64],
        ),
        target=lease.transaction.action_key.target,
        observed=RadioTargetFacts(
            source_sequence=generation,
            connected=True,
            serving_band_available=True,
            serving_band_unavailable_reason="",
            primary_band=3,
            selected_bands_available=True,
            selected_bands_unavailable_reason="",
            selected_bands=frozenset({3}),
            cell_locked=False,
            pci=1,
            earfcn=1650,
            nr_bands=frozenset(),
        ),
        committed_at=103.0,
        reason="band_set:[3]",
    )


def _wifi_target_receipt(
    lease,
    *,
    generation: int = 2,
) -> TargetConfirmationReceipt:
    target = lease.transaction.action_key.target
    assert isinstance(target, CoreWifiRadioTarget)
    return TargetConfirmationReceipt(
        request_id=lease.transaction.command_id,
        transaction_root_id=lease.root_id,
        matrix_identity=replace(
            lease.transaction.matrix_identity,
            matrix_sequence=generation,
            matrix_revision=(f"{generation:x}" * 64)[:64],
        ),
        target=target,
        observed=RadioTargetFacts(
            source_sequence=generation,
            connected=True,
            serving_band_available=True,
            serving_band_unavailable_reason="",
            primary_band=3,
            selected_bands_available=True,
            selected_bands_unavailable_reason="",
            selected_bands=frozenset({1, 3, 20}),
            cell_locked=False,
            pci=1,
            earfcn=1650,
            nr_bands=frozenset(),
            wifi_24ghz_enabled=target.enabled,
            wifi_5ghz_enabled=True,
        ),
        committed_at=103.0,
        reason=f"wifi_{target.band}:{'on' if target.enabled else 'off'}",
    )


def _reachability_receipt(lease) -> ActionReachabilityReceipt:
    return ActionReachabilityReceipt(
        request_id=lease.transaction.command_id,
        transaction_root_id=lease.root_id,
        matrix_identity=lease.transaction.matrix_identity,
        identity_key=lease.transaction.identity_key,
        identity_code="identity-code-test",
        observer_id="foundation_host_ping",
        measurement_started_at=101.1,
        measurement_completed_at=104.0,
        physical_completed_at=102.0,
        max_probe_gap_s=5.0,
        coverage_complete=True,
        samples=(
            ReachabilityProbeSample(
                probed_at=100.5,
                reachable=True,
                target="8.8.8.8",
                service_reachable=True,
                source="foundation_host_ping",
            ),
            ReachabilityProbeSample(
                probed_at=104.0,
                reachable=True,
                target="8.8.8.8",
                service_reachable=True,
                source="foundation_host_ping",
            ),
        ),
        outage_intervals=(),
        complete=True,
        disposition=ActionReachabilityDisposition.MEASURED,
    )


def test_typed_operator_and_autonomous_transactions_are_distinct() -> None:
    operator = build_cellular_transaction(_action(), now=100.0)
    autonomous_action = _action(
        origin_kind=ActionOriginKind.SMART_LOOP,
        source="smart_loop_controller",
        command_id="autonomous-command",
    )
    autonomous = build_cellular_transaction(autonomous_action, now=100.0)

    assert isinstance(operator.action_key.target, CoreBandSetTarget)
    assert isinstance(autonomous.action_key.target, CoreBandSetTarget)
    assert operator.family is TransactionFamily.CELLULAR_OPERATOR
    assert autonomous.family is TransactionFamily.CELLULAR_AUTONOMOUS
    assert operator.public_command_root_id == core_command_request_root(
        getattr(_action(), "_test_core_request")
    )
    assert autonomous.public_command_root_id == core_command_request_root(
        getattr(autonomous_action, "_test_core_request")
    )
    assert operator.policy.learning == "operator_proof_admission"
    assert autonomous.policy.learning == "graded_proof_only"


def test_exact_physical_receipt_round_trip_and_authority_rejections() -> None:
    authority = CommandAuthority()
    lease = _dispatch(authority, _action())
    receipt = _single_stage_receipt(lease)

    assert PhysicalCommandReceipt.from_mapping(receipt.as_dict()) == receipt
    with pytest.raises(RuntimeError, match="physical_receipt_request_mismatch"):
        authority.acknowledge_modem_result(
            lease,
            physical_receipt=replace(receipt, request_id="foreign-command"),
            now=102.0,
        )
    with pytest.raises(RuntimeError, match="physical_receipt_root_mismatch"):
        authority.acknowledge_modem_result(
            lease,
            physical_receipt=replace(
                receipt,
                transaction_root_id="action:foreign-root",
            ),
            now=102.0,
        )
    with pytest.raises(RuntimeError, match="physical_receipt_target_mismatch"):
        authority.acknowledge_modem_result(
            lease,
            physical_receipt=replace(
                receipt,
                target=CoreBandSetTarget(
                    bands=(1, 3),
                    command_kind="BAND_SELECTION",
                ),
                stages=(
                    _stage(
                        kind="band_selection",
                        ordinal=1,
                        target=CoreBandSetTarget(
                            bands=(1, 3),
                            command_kind="BAND_SELECTION",
                        ),
                        dispatched_at=101.1,
                        returned_at=101.5,
                    ),
                ),
            ),
            now=102.0,
        )
    with pytest.raises(
        RuntimeError,
        match="physical_receipt_after_acknowledgement",
    ):
        authority.acknowledge_modem_result(
            lease,
            physical_receipt=replace(receipt, transaction_completed_at=103.0),
            now=102.0,
        )

    accepted = authority.acknowledge_modem_result(
        lease,
        physical_receipt=receipt,
        now=102.0,
    )
    assert accepted.active is not None
    assert accepted.active.receipts


def test_pband_two_stage_success_is_exact_and_durable(tmp_path) -> None:
    authority = CommandAuthority(persistence_dir=tmp_path)
    lease = _dispatch(
        authority,
        _action(LeverType.PBAND, command_id="pband-success"),
    )
    receipt = _pband_receipt(lease)

    transition = authority.acknowledge_modem_result(
        lease,
        physical_receipt=receipt,
        now=102.0,
    )

    assert transition.active is not None
    stored = transition.active.as_dict()["receipts"]["modem"]
    assert stored == receipt.as_dict()
    restored = CommandAuthority(persistence_dir=tmp_path).active_lifecycle()
    assert restored is not None
    assert restored.stage is CommandLifecycleStage.ACCEPTED
    assert restored.as_dict()["receipts"]["modem"] == receipt.as_dict()


def test_pband_second_stage_rejection_is_terminal_partial_failure() -> None:
    authority = CommandAuthority()
    lease = _dispatch(
        authority,
        _action(LeverType.PBAND, command_id="pband-partial-failure"),
    )
    receipt = _pband_receipt(lease, second_outcome="rejected")

    transition = authority.acknowledge_modem_result(
        lease,
        physical_receipt=receipt,
        now=102.0,
    )

    assert transition.active is None
    assert transition.terminal is not None
    assert transition.terminal.terminal_status is CommandTerminalStatus.FAILED
    assert transition.terminal.reason == "ca_pool_write_rejected"
    stored = transition.terminal.as_dict()["receipts"]["modem"]
    assert stored["accepted"] is False
    assert [stage["outcome"] for stage in stored["stages"]] == [
        "accepted",
        "rejected",
    ]
    assert authority.active_lifecycle() is None


def test_target_and_fresh_stream_settles_without_pause_dependency() -> None:
    authority = CommandAuthority()
    lease = _accepted_radio_lifecycle(authority)

    with pytest.raises(
        RuntimeError,
        match="command_stream_settle_from_accepted",
    ):
        authority.latch_stream_continuation(
            root_id=lease.root_id,
            source_generation=2,
            now=102.5,
        )
    authority.acknowledge_target_confirmed(
        root_id=lease.root_id,
        receipt=_target_receipt(lease),
        now=103.0,
    )
    _ack_projection(authority, at=103.0)
    with pytest.raises(
        RuntimeError,
        match="command_settle_requires_later_generation",
    ):
        authority.latch_stream_continuation(
            root_id=lease.root_id,
            source_generation=2,
            now=103.5,
        )
    latched = authority.latch_stream_continuation(
        root_id=lease.root_id,
        source_generation=3,
        receipt={"owner": "accepted_generation_stream"},
        now=104.0,
    )
    _ack_projection(authority, at=104.0)
    assert latched.settle_generation == 3
    assert authority.terminal_receipts() == ()
    terminal = authority.settle_confirmed_stream(
        root_id=lease.root_id,
        now=104.0,
    )
    assert terminal.terminal_status is CommandTerminalStatus.SETTLED
    assert terminal.reason == "target_confirmed_and_stream_settled"
    assert terminal.target_generation == 2
    assert terminal.settle_generation == 3
    assert terminal.proof_ready is True
    receipts = terminal.as_dict()["receipts"]
    assert set(receipts) >= {"modem", "target", "settle"}
    assert "reachability" not in receipts
    assert "reachability_unavailable" not in receipts
    assert "presentation" not in receipts
    terminal.validate_settlement_proof()
    assert CommandTerminalReceipt.from_mapping(terminal.as_dict()) == terminal
    assert authority.active_lifecycle() is None


def test_every_cellular_transition_expires_before_accepting_late_evidence() -> None:
    def assert_timeout(authority: CommandAuthority, operation) -> None:
        active = authority.active_lifecycle()
        assert active is not None
        with pytest.raises(
            RuntimeError,
            match="cellular_command_transition_after_deadline",
        ):
            operation(active.deadline_ts)
        terminal = authority.terminal_receipts()[-1]
        assert terminal.terminal_status is CommandTerminalStatus.TIMED_OUT
        assert terminal.reason.endswith("_deadline_expired")
        assert authority.active_lifecycle() is None
        _ack_projection(authority, at=terminal.terminal_ts)

    authority = CommandAuthority()
    lease = _claim_only(authority, _action(command_id="late-dispatch"))
    assert_timeout(
        authority,
        lambda deadline: authority.mark_dispatched(lease, now=deadline),
    )

    authority = CommandAuthority()
    lease = _dispatch(authority, _action(command_id="late-modem-result"))
    receipt = _single_stage_receipt(lease)
    assert_timeout(
        authority,
        lambda deadline: authority.acknowledge_modem_result(
            lease,
            physical_receipt=receipt,
            now=deadline,
        ),
    )

    authority = CommandAuthority()
    lease = _accepted_radio_lifecycle(
        authority,
        _action(command_id="late-target"),
    )
    assert_timeout(
        authority,
        lambda deadline: authority.acknowledge_target_confirmed(
            root_id=lease.root_id,
            receipt=_target_receipt(lease),
            now=deadline,
        ),
    )

    authority = CommandAuthority()
    lease = _accepted_radio_lifecycle(
        authority,
        _action(command_id="late-continuation"),
    )
    authority.acknowledge_target_confirmed(
        root_id=lease.root_id,
        receipt=_target_receipt(lease),
        now=103.0,
    )
    _ack_projection(authority, at=103.0)
    assert_timeout(
        authority,
        lambda deadline: authority.latch_stream_continuation(
            root_id=lease.root_id,
            source_generation=3,
            now=deadline,
        ),
    )

    authority = CommandAuthority()
    lease = _accepted_radio_lifecycle(
        authority,
        _action(command_id="late-settlement"),
    )
    authority.acknowledge_target_confirmed(
        root_id=lease.root_id,
        receipt=_target_receipt(lease),
        now=103.0,
    )
    _ack_projection(authority, at=103.0)
    authority.latch_stream_continuation(
        root_id=lease.root_id,
        source_generation=3,
        now=104.0,
    )
    _ack_projection(authority, at=104.0)
    assert_timeout(
        authority,
        lambda deadline: authority.settle_confirmed_stream(
            root_id=lease.root_id,
            now=deadline,
        ),
    )

    for command_id, transition in (
        (
            "late-failure",
            lambda authority, lease, deadline: authority.fail_cellular(
                root_id=lease.root_id,
                reason="late_worker_failure",
                now=deadline,
            ),
        ),
        (
            "late-cancellation",
            lambda authority, lease, deadline: authority.cancel_cellular(
                root_id=lease.root_id,
                reason="late_cancellation",
                now=deadline,
            ),
        ),
    ):
        authority = CommandAuthority()
        lease = _claim_only(authority, _action(command_id=command_id))
        assert_timeout(
            authority,
            lambda deadline, operation=transition: operation(
                authority,
                lease,
                deadline,
            ),
        )


def test_terminal_projection_failure_keeps_terminal_and_records_fault() -> None:
    authority = CommandAuthority()
    lease = _accepted_radio_lifecycle(
        authority,
        _action(command_id="terminal-projection-fault"),
    )
    authority.acknowledge_target_confirmed(
        root_id=lease.root_id,
        receipt=_target_receipt(lease),
        now=103.0,
    )
    _ack_projection(authority, at=103.0)
    authority.latch_stream_continuation(
        root_id=lease.root_id,
        source_generation=3,
        now=104.0,
    )
    _ack_projection(authority, at=104.0)
    terminal = authority.settle_confirmed_stream(
        root_id=lease.root_id,
        now=104.1,
    )
    engine = SimpleNamespace(
        diagnostics=SimpleNamespace(
            log_event=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("injected_projection_failure")
            )
        )
    )

    publish_command_terminal_evidence(engine, terminal, now=terminal.terminal_ts)

    assert authority.terminal_receipts()[-1] == terminal
    assert authority.active_lifecycle() is None
    faults = engine._action_runtime_faults["suppressed_faults"]
    assert faults["by_category"] == {"authority": 1}
    fault = faults["last_fault_event"]
    assert fault["metadata"]["error_type"] == "RuntimeError"
    assert fault["detail"] == "injected_projection_failure"


def test_execution_only_wifi_settles_from_exact_core_receipts() -> None:
    authority = CommandAuthority()
    lease = _accepted_radio_lifecycle(
        authority,
        _action(
            LeverType.WIFI_RADIO,
            target=CoreWifiRadioTarget(band="2.4ghz", enabled=False),
            command_id="wifi-off",
        ),
    )
    authority.acknowledge_target_confirmed(
        root_id=lease.root_id,
        receipt=_wifi_target_receipt(lease),
        now=103.0,
    )
    _ack_projection(authority, at=103.0)
    authority.latch_stream_continuation(
        root_id=lease.root_id,
        source_generation=3,
        receipt={"owner": "accepted_generation_stream"},
        now=104.0,
    )
    _ack_projection(authority, at=104.0)

    terminal = authority.settle_confirmed_stream(
        root_id=lease.root_id,
        now=104.1,
    )

    assert terminal.terminal_status is CommandTerminalStatus.SETTLED
    assert terminal.proof_ready is True
    assert set(terminal.as_dict()["receipts"]) >= {
        "modem",
        "target",
        "settle",
    }
    terminal.validate_settlement_proof()


def test_command_authority_rejects_embedded_pause_resolution() -> None:
    authority = CommandAuthority()
    lease = _accepted_radio_lifecycle(authority)
    authority.acknowledge_target_confirmed(
        root_id=lease.root_id,
        receipt=_target_receipt(lease),
        now=103.0,
    )
    _ack_projection(authority, at=103.0)
    active = authority.latch_stream_continuation(
        root_id=lease.root_id,
        source_generation=3,
        receipt={
            "owner": "accepted_generation_stream",
            "sampled_at": 104.0,
            "identity_code": "identity-code-test",
        },
        now=104.0,
    )
    _ack_projection(authority, at=104.0)
    unavailable = ActionReachabilityUnavailable(
        request_id=active.key.command_id,
        transaction_root_id=active.key.root_id,
        matrix_identity=active.key.matrix_identity,
        identity_key=active.key.identity_key,
        identity_code="identity-code-test",
        coverage_required_at=101.5,
        deadline_at=active.deadline_ts,
        resolved_at=104.0,
        reason="reachability_evidence_unavailable_at_settlement",
    )
    with pytest.raises(
        RuntimeError,
        match="command_reachability_owned_by_action_pause",
    ):
        authority.acknowledge_reachability_unavailable(unavailable)
    terminal = authority.settle_confirmed_stream(
        root_id=lease.root_id,
        now=104.1,
    )

    terminal.validate_settlement_proof()
    receipts = terminal.as_dict()["receipts"]
    assert "reachability_unavailable" not in receipts
    for missing_name in ("modem", "target", "settle"):
        incomplete = replace(
            terminal,
            receipts={
                name: value
                for name, value in receipts.items()
                if name != missing_name
            },
        )
        with pytest.raises(
            ValueError,
            match=(
                "command_terminal_receipt_settlement_proof_incomplete:"
                + missing_name
            ),
        ):
            incomplete.validate_settlement_proof()
    embedded_resolution = replace(
        terminal,
        receipts={**receipts, "reachability_unavailable": unavailable.as_dict()},
    )
    with pytest.raises(
        ValueError,
        match="command_terminal_receipt_embedded_reachability_forbidden",
    ):
        embedded_resolution.validate_settlement_proof()


def test_deleted_settle_api_fails_loudly() -> None:
    authority = CommandAuthority()
    with pytest.raises(
        RuntimeError,
        match="command_authority_old_settle_path_illegal",
    ):
        authority.acknowledge_settled(
            root_id="old-root",
            source_generation=2,
        )


def test_request_freshness_cannot_shorten_core_physical_lifecycle() -> None:
    action = _action(command_id="short-proposal-freshness")
    action.expires_after_s = 10.0
    action.expires_at_ts = 110.0
    transaction = build_cellular_transaction(action, now=100.0)
    assert transaction.created_ts == 100.0
    assert transaction.expires_at_ts == 400.0

    authority = CommandAuthority()
    lease = _dispatch(authority, action)
    active = authority.active_lifecycle()
    assert active is not None
    assert active.lease == lease
    assert active.lease.expires_ts == 400.0
    assert active.deadline_ts == 161.0

    source = getsource(build_cellular_transaction)
    assert "_action_expires_at" not in source
    assert 'getattr(action, "expires_at_ts"' not in source


def test_active_lifecycle_restores_then_expires_durably(tmp_path) -> None:
    authority = CommandAuthority(persistence_dir=tmp_path)
    action = _action(command_id="durable-expiry-command")
    lease = _dispatch(authority, action)

    restored = CommandAuthority(persistence_dir=tmp_path)
    active = restored.active_lifecycle()
    assert active is not None
    assert active.stage is CommandLifecycleStage.DISPATCHED
    assert active.lease == lease
    if restored.pending_projection_obligation() is not None:
        _ack_projection(restored, at=101.0)
    assert restored.expire_overdue(now=160.9) == ()

    [expired] = restored.expire_overdue(now=161.0)
    assert expired.terminal_status is CommandTerminalStatus.TIMED_OUT
    assert expired.reason == "dispatched_deadline_expired"
    assert expired.receipts
    assert restored.active_lifecycle() is None

    terminal_restore = CommandAuthority(persistence_dir=tmp_path)
    [durable] = terminal_restore.terminal_receipts()
    assert durable == expired
    if terminal_restore.pending_projection_obligation() is not None:
        _ack_projection(terminal_restore, at=161.0)
    duplicate = terminal_restore.claim_cellular_action(
        _action(command_id="durable-expiry-command"),
        execution_owner="core_command_gateway",
        now=200.0,
    )
    assert duplicate.accepted is False
    assert duplicate.reason == "command_root_admission_identity_mismatch"


def test_profile_restart_deadline_survives_restore_expires_and_never_replays(tmp_path):
    from arch4.core_contracts.command_targets import CoreRestartTarget
    from arch4.modem.live_lane_contract import BETA_MODEM_LIVE_ACCESS_PROFILES
    authority = CommandAuthority(persistence_dir=tmp_path)
    action = _action(LeverType.RESTART, target=CoreRestartTarget(), command_id="slow-restart")
    with pytest.raises(TypeError, match="restart_selected_recovery_timing_required"):
        build_cellular_transaction(action, now=100.)
    timing = BETA_MODEM_LIVE_ACCESS_PROFILES["nx520-250902"].recovery_timing
    lease = _dispatch(authority, action, recovery_timing=timing)
    authority.acknowledge_modem_result(lease, physical_receipt=_single_stage_receipt(lease), now=102.)
    _ack_projection(authority, at=102.)
    restored = CommandAuthority(persistence_dir=tmp_path)
    assert restored.active_lifecycle().deadline_ts == 402.
    assert restored.active_lifecycle().lease.expires_ts == 505.
    assert restored.active_lifecycle().lease.transaction.authority_dict()["recovery_timing"] == timing.as_dict()
    if restored.pending_projection_obligation() is not None:
        _ack_projection(restored, at=102.)
    assert restored.expire_overdue(now=339.) == ()  # Actual NX520 recovery time fits.
    expired, = restored.expire_overdue(now=402.)
    assert expired.terminal_status is CommandTerminalStatus.TIMED_OUT
    terminal_restore = CommandAuthority(persistence_dir=tmp_path)
    assert terminal_restore.active_lifecycle() is None
    assert terminal_restore.terminal_receipts()[-1] == expired
    if terminal_restore.pending_projection_obligation() is not None:
        _ack_projection(terminal_restore, at=402.)
    assert not terminal_restore.claim_cellular_action(action, execution_owner="core_command_gateway",
        now=403., recovery_timing=timing).accepted


def test_restart_reconciliation_removes_foreign_modem_command_truth(
    tmp_path,
) -> None:
    authority = CommandAuthority(persistence_dir=tmp_path)
    action = _action(
        command_id="foreign-modem-command",
        session_scope_key="backend=b535|serial=foreign",
    )
    _dispatch(authority, action)

    restored = CommandAuthority(persistence_dir=tmp_path)
    assert restored.active_lifecycle() is not None
    assert restored.pending_projection_obligation() is not None

    assert restored.reconcile_runtime_identity(
        "backend=b818|serial=current"
    ) is True
    assert restored.active_lifecycle() is None
    assert restored.terminal_receipts() == ()
    assert restored.pending_projection_obligation() is None
    assert restored.core_command_root(action.command_id) is None

    durable = CommandAuthority(persistence_dir=tmp_path)
    assert durable.active_lifecycle() is None
    assert durable.terminal_receipts() == ()
    assert durable.pending_projection_obligation() is None


def test_restart_reconciliation_retains_same_modem_command_truth(tmp_path) -> None:
    scope = "backend=b818|serial=current"
    authority = CommandAuthority(persistence_dir=tmp_path)
    action = _action(
        command_id="same-modem-command",
        session_scope_key=scope,
    )
    lease = _dispatch(authority, action)

    restored = CommandAuthority(persistence_dir=tmp_path)
    assert restored.reconcile_runtime_identity(scope) is False
    active = restored.active_lifecycle()
    assert active is not None
    assert active.key.root_id == lease.root_id
    assert restored.core_command_root(action.command_id) is not None
    assert restored.pending_projection_obligation() is not None


def test_restart_reconciliation_removes_foreign_terminal_receipt(tmp_path) -> None:
    authority = CommandAuthority(persistence_dir=tmp_path)
    _dispatch(
        authority,
        _action(
            command_id="foreign-terminal-command",
            session_scope_key="backend=b535|serial=foreign",
        ),
    )
    [terminal] = authority.expire_overdue(now=161.0)
    assert terminal.terminal_status is CommandTerminalStatus.TIMED_OUT

    restored = CommandAuthority(persistence_dir=tmp_path)
    assert restored.terminal_receipts() == (terminal,)
    assert restored.pending_projection_obligation() is not None
    assert restored.reconcile_runtime_identity(
        "backend=b818|serial=current"
    ) is True
    assert restored.terminal_receipts() == ()
    assert restored.pending_projection_obligation() is None


def test_internal_lifecycle_preparation_does_not_write_a_second_checkpoint(
    tmp_path,
    monkeypatch,
) -> None:
    authority = CommandAuthority(persistence_dir=tmp_path)
    store = authority._state_store
    assert store is not None
    write = store.write
    writes: list[str] = []

    def record(state):
        writes.append(str(state.get("event") or ""))
        write(state)

    monkeypatch.setattr(store, "write", record)
    _dispatch(
        authority,
        _action(command_id="single-durable-checkpoint"),
    )

    assert writes == ["core_command_dispatched"]


def test_pre_dispatch_authority_state_is_not_written_to_disk(tmp_path) -> None:
    authority = CommandAuthority(persistence_dir=tmp_path)
    state_path = tmp_path / COMMAND_AUTHORITY_STATE_FILENAME
    initial_state = state_path.read_bytes()
    action = _action(command_id="memory-only-preparation")
    request = getattr(action, "_test_core_request")

    authority.receive_core_command(request, now=100.0)
    authority.advance_core_command(
        request_id=request.request_id,
        expected=CommandRootState.RECEIVED,
        target=CommandRootState.VALIDATED,
        now=101.0,
    )
    authority.advance_core_command(
        request_id=request.request_id,
        expected=CommandRootState.VALIDATED,
        target=CommandRootState.ADMITTED,
        now=102.0,
    )

    assert state_path.read_bytes() == initial_state


def test_command_authority_publishes_idle_current_state_at_startup(tmp_path) -> None:
    CommandAuthority(persistence_dir=tmp_path)

    state_path = tmp_path / COMMAND_AUTHORITY_STATE_FILENAME
    record = CommandAuthorityStateRecord.from_mapping(
        json.loads(state_path.read_text(encoding="utf-8"))
    )
    assert record.event == "command_authority_initialized"
    assert record.active_cellular is None
    assert record.command_roots == ()
    assert record.terminal == ()
    assert record.admission_frozen is False


def test_terminal_receipt_round_trip_rejects_tampering() -> None:
    authority = CommandAuthority()
    lease = _dispatch(authority, _action())
    failed = authority.acknowledge_modem_result(
        lease,
        physical_receipt=_single_stage_receipt(
            lease,
            accepted=False,
            reason="modem_rejected",
        ),
        now=102.0,
    ).terminal
    assert failed is not None
    payload = failed.as_dict()
    assert CommandTerminalReceipt.from_mapping(payload) == failed

    tampered = dict(payload)
    tampered["reason"] = "changed"
    with pytest.raises(ValueError, match="checksum"):
        CommandTerminalReceipt.from_mapping(tampered)


def test_canonical_command_authority_state_has_one_typed_reader(
    tmp_path,
) -> None:
    state_dir = tmp_path / "guc_memory" / "cgf_state"
    runtime_dir = tmp_path / "guc_output"
    runtime_dir.mkdir()
    authority = CommandAuthority(persistence_dir=state_dir)
    action = _action(command_id="state-reader-command")
    lease = _dispatch(authority, action)

    state_path = state_dir / COMMAND_AUTHORITY_STATE_FILENAME
    row = json.loads(state_path.read_text(encoding="utf-8"))
    direct = CommandAuthorityStateRecord.from_mapping(row)
    record = read_command_authority_state(runtime_dir)
    assert record == direct
    assert record is not None and record.active_cellular is not None
    assert record.active_cellular.lease == lease
    assert record.sequence > 0

    wrong_schema = dict(row)
    wrong_schema["schema"] = "OldCommandJournal.v0"
    with pytest.raises(
        ValueError,
        match="command_authority_state_schema_invalid",
    ):
        CommandAuthorityStateRecord.from_mapping(wrong_schema)

    wrong_owner = dict(row)
    wrong_owner["owner"] = "competing_command_owner"
    with pytest.raises(
        ValueError,
        match="command_authority_state_owner_invalid",
    ):
        CommandAuthorityStateRecord.from_mapping(wrong_owner)


def test_command_authority_persistence_is_one_bounded_atomic_state(tmp_path) -> None:
    authority = CommandAuthority(persistence_dir=tmp_path)
    _dispatch(
        authority,
        _action(command_id="one-bounded-command-state"),
    )

    state_path = tmp_path / COMMAND_AUTHORITY_STATE_FILENAME
    text = state_path.read_text(encoding="utf-8")
    assert len(text.splitlines()) == 1
    assert json.loads(text)["schema"] == COMMAND_AUTHORITY_STATE_SCHEMA
    assert tuple(
        path.name
        for path in tmp_path.iterdir()
        if path.name.startswith("command_authority")
    ) == (COMMAND_AUTHORITY_STATE_FILENAME,)


def test_active_validator_rejects_missing_or_malformed_authority_state(
    tmp_path,
) -> None:
    runtime_dir = tmp_path / "guc_output"
    runtime_dir.mkdir()
    assert read_command_authority_state(runtime_dir, required=False) is None
    with pytest.raises(RuntimeError, match="state is missing"):
        read_command_authority_state(runtime_dir)

    state_dir = tmp_path / "guc_memory" / "cgf_state"
    authority = CommandAuthority(persistence_dir=state_dir)
    _dispatch(
        authority,
        _action(command_id="state-sequence-command"),
    )
    state_path = state_dir / COMMAND_AUTHORITY_STATE_FILENAME
    state_path.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="state has malformed JSON"):
        read_command_authority_state(runtime_dir)
