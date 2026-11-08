"""Calibration marker alias owner shared by Memory context stores."""

from __future__ import annotations

from typing import Any, Dict

OWNER = "arch4.brain.memory_calibration_marker_owner"


def resolve_marker_alias(memory: Any, marker: Any) -> str:
    cur = str(marker or "C0")
    aliases = getattr(memory, "calibration_aliases", {}) or {}
    seen: list[str] = []
    while cur in aliases and cur not in seen:
        seen.append(cur)
        cur = str(aliases.get(cur) or cur)
    resolved = cur or "C0"
    for alias in seen:
        if alias != resolved:
            aliases[alias] = resolved
        else:
            aliases.pop(alias, None)
    memory.calibration_aliases = aliases
    return resolved


def compact_calibration_alias_graph(memory: Any) -> None:
    aliases = getattr(memory, "calibration_aliases", {}) or {}
    if not aliases:
        return
    compacted: Dict[str, str] = {}
    for alias in list(aliases.keys()):
        resolved = resolve_marker_alias(memory, alias)
        if alias and resolved and alias != resolved:
            compacted[str(alias)] = str(resolved)
    memory.calibration_aliases = compacted


__all__ = ["OWNER", "compact_calibration_alias_graph", "resolve_marker_alias"]
