"""Brain feed-register publication for the canonical learning gate."""

from __future__ import annotations

import time
from typing import Any, Mapping

from arch4.brain.feed_registers import (
    FEED_LEARNING_GATE,
    OWNER_LEARNING_GATE,
    require_brain_feed_register_bank,
)
from arch4.learning_gate_contract import (
    LEARNING_GATE_OWNER,
    LEARNING_GATE_SCHEMA,
    LearningGateDecision,
    build_learning_gate_publication,
)


def publish_learning_gate_register(
    engine: Any,
    *,
    gate: LearningGateDecision | Mapping[str, Any],
    source: str,
    consumer: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """Publish one canonical learning-gate decision to the Brain feed bank."""

    publication = build_learning_gate_publication(
        gate,
        source=source,
        consumer=consumer,
    )
    if publication.get("schema") != LEARNING_GATE_SCHEMA or publication.get("owner") != LEARNING_GATE_OWNER:
        raise ValueError("learning gate publication owner/schema mismatch")
    bank = require_brain_feed_register_bank(engine)
    now_ts = time.time() if now is None else float(now)
    bank.write_feeder(
        FEED_LEARNING_GATE,
        owner=OWNER_LEARNING_GATE,
        payload=publication,
        source="learning_gate_registers",
        updated_at=now_ts,
        valid=True,
        stale_after_s=6.0,
    )
    return {
        "published": True,
        "feed": FEED_LEARNING_GATE,
        "owner": OWNER_LEARNING_GATE,
        "schema": LEARNING_GATE_SCHEMA,
        "publication": publication,
    }


__all__ = ["publish_learning_gate_register"]
