"""Pure immutable-identity admission shared by owner-specific handoffs."""

from __future__ import annotations

import re
from collections.abc import Mapping


def handoff_identity_token(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def confirmed_handoff_identity_matches(
    source_identity: Mapping[str, object] | None,
    confirmed_identity: Mapping[str, object] | None,
) -> bool:
    """Require one matching immutable modem ID and reject known conflicts."""

    source = source_identity if isinstance(source_identity, Mapping) else {}
    current = confirmed_identity if isinstance(confirmed_identity, Mapping) else {}
    # Access/profile/firmware/hash changes do not replace the physical router.
    # Their separate settings/learning compatibility checks remain with callers.
    matched_immutable_id = False
    for key in ("serial", "imei"):
        source_value = handoff_identity_token(source.get(key))
        current_value = handoff_identity_token(current.get(key))
        if not source_value or not current_value:
            continue
        if source_value != current_value:
            return False
        matched_immutable_id = True
    return matched_immutable_id


__all__ = (
    "confirmed_handoff_identity_matches",
    "handoff_identity_token",
)
