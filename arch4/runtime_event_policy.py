"""Runtime event emission policy helpers."""

from __future__ import annotations

import os


def emit_interpretive_runtime_events_enabled() -> bool:
    text = str(os.environ.get("ARCH_EMIT_INTERPRETIVE_RUNTIME_EVENTS", "") or "").strip().lower()
    return text in {"1", "true", "yes", "on"}
