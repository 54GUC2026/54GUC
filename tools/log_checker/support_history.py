"""Standalone diagnostic export adapter, outside the application runtime graph."""
from __future__ import annotations
from pathlib import Path
from arch4.foundation.support_bundle import scrub_support_export_artifact

def build_support_history_export(path: str | Path, **kwargs):
    """Explicit standalone diagnostic adapter: private capture, final privacy."""
    from dataclasses import replace
    from arch4.new_horizon.history import capture_support_history_export
    captured = capture_support_history_export(path, **kwargs)
    secrets: set[str] = set()
    result = scrub_support_export_artifact(captured.filename, captured.content, known_secret_values=secrets)
    if not result.included:
        raise ValueError("support_history_final_privacy_failed")
    return replace(captured, content=result.content, known_secret_values=tuple(sorted(secrets)))
