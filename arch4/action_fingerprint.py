"""Stable semantic fingerprint shared by action safety and admission owners."""

from __future__ import annotations

import hashlib

from arch4.canonical_json import canonical_json_bytes
from arch4.interfaces import LeverAction
from arch4.action_transaction import bound_core_command_target_from_action


def action_semantic_fingerprint(action: LeverAction) -> str:
    """Bind a safety/admission receipt to immutable command semantics."""

    try:
        payload = {
            "target": bound_core_command_target_from_action(
                action
            ).to_mapping(),
            "command_id": str(getattr(action, "command_id", "") or ""),
        }
        encoded = canonical_json_bytes(payload)
    except Exception as exc:
        raise RuntimeError(f"action_fingerprint_failed:{type(exc).__name__}") from exc
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["action_semantic_fingerprint"]
