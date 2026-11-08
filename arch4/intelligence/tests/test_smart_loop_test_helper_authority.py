from types import SimpleNamespace

import pytest

from arch4.action_transaction_authority import ActionTransactionAuthority
from arch4.tests.smart_loop_test_helpers import _action_authority_for_test


def test_standalone_brain_helper_uses_the_brain_transaction_port() -> None:
    brain = SimpleNamespace(action_transaction_authority=None)

    authority = _action_authority_for_test(brain, create=True)

    assert authority is brain.action_transaction_authority
    assert not hasattr(brain, "controller")


def test_whole_engine_helper_uses_the_runtime_transaction_port() -> None:
    engine = SimpleNamespace(controller=SimpleNamespace(runtime=SimpleNamespace()))

    authority = _action_authority_for_test(engine, create=True)

    assert authority is engine.controller.runtime.action_transaction_authority
    assert not hasattr(engine, "action_transaction_authority")


def test_competing_brain_and_runtime_test_owners_fail_loudly() -> None:
    brain_authority = ActionTransactionAuthority()
    runtime_authority = ActionTransactionAuthority()
    brain = SimpleNamespace(
        action_transaction_authority=brain_authority,
        controller=SimpleNamespace(
            runtime=SimpleNamespace(
                action_transaction_authority=runtime_authority,
            )
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="test_action_transaction_authority_competing_owners",
    ):
        _action_authority_for_test(brain, create=True)


def test_runtime_owner_cannot_fill_an_unbound_standalone_brain_port() -> None:
    brain = SimpleNamespace(
        action_transaction_authority=None,
        controller=SimpleNamespace(
            runtime=SimpleNamespace(
                action_transaction_authority=ActionTransactionAuthority(),
            )
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="test_action_transaction_authority_competing_owners",
    ):
        _action_authority_for_test(brain, create=True)
