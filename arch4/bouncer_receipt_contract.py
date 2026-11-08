"""Neutral consumed-Bouncer receipt contract.

The Bouncer owns safety evaluation and receipt consumption. Action admission
receives only this immutable value contract and must not import the executable
Bouncer owner.
"""

from __future__ import annotations

from dataclasses import dataclass

from arch4.action_transaction import ActionTransactionKey


@dataclass(frozen=True, slots=True)
class BouncerReceiptValidation:
    accepted: bool
    reason: str
    disposition: str = ""
    receipt_token: str = ""
    action_key: ActionTransactionKey | None = None

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise TypeError("bouncer_validation_accepted_bool_required")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("bouncer_validation_reason_required")
        if self.accepted and not isinstance(
            self.action_key,
            ActionTransactionKey,
        ):
            raise TypeError("bouncer_validation_action_key_required")
        if self.action_key is not None and not isinstance(
            self.action_key,
            ActionTransactionKey,
        ):
            raise TypeError("bouncer_validation_action_key_invalid")


__all__ = ["BouncerReceiptValidation"]
