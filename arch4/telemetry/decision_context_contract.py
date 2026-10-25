"""Declared compact contract for decision-context capture rows.

Decision-context retention is a sparse forensic window, not a second copy of
the full telemetry history row.  This module owns the boundary: numbered
production facts provide the compact tick base and only the explicitly named
Brain forensic sections may be added from runtime reporting.
"""

from __future__ import annotations

from itertools import islice
from typing import Any, Mapping

from arch4.telemetry.production_contract import (
    build_production_telemetry_row,
    flatten_production_telemetry_row,
)


DECISION_CONTEXT_BRAIN_SECTIONS: tuple[str, ...] = (
    "brain_vectors",
    "brain_bouncer",
    "brain_referee",
)

_FORENSIC_MAX_NODES = 384
_FORENSIC_MAX_TEXT_CHARS = 24 * 1024
_FORENSIC_MAX_MAPPING_ITEMS = 32
_FORENSIC_MAX_SEQUENCE_ITEMS = 16
_FORENSIC_MAX_STRING_CHARS = 512
_FORENSIC_MAX_KEY_CHARS = 128


def _compact_forensic_key(value: Any, *, budget: dict[str, Any]) -> str:
    text = str(value)
    remaining = max(0, int(budget["text_chars"]))
    limit = min(_FORENSIC_MAX_KEY_CHARS, remaining)
    compacted = text[:limit]
    budget["text_chars"] = remaining - len(compacted)
    if len(compacted) < len(text):
        budget["truncated"] = True
    return compacted


def _compact_forensic_value(
    value: Any,
    *,
    budget: dict[str, Any],
    depth: int = 0,
) -> Any:
    if int(budget["nodes"]) <= 0:
        budget["truncated"] = True
        return {"truncated": "node_budget"}
    budget["nodes"] = int(budget["nodes"]) - 1
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        remaining = max(0, int(budget["text_chars"]))
        limit = min(_FORENSIC_MAX_STRING_CHARS, remaining)
        text = value[:limit]
        budget["text_chars"] = remaining - len(text)
        if len(text) < len(value):
            budget["truncated"] = True
        return text
    if depth >= 5:
        budget["truncated"] = True
        return {"truncated": "depth", "type": type(value).__name__}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        rows = list(islice(value.items(), _FORENSIC_MAX_MAPPING_ITEMS + 1))
        visible_rows = rows[:_FORENSIC_MAX_MAPPING_ITEMS]
        for key, item in visible_rows:
            if int(budget["nodes"]) <= 0 or int(budget["text_chars"]) <= 0:
                break
            out[_compact_forensic_key(key, budget=budget)] = _compact_forensic_value(
                item,
                budget=budget,
                depth=depth + 1,
            )
        try:
            total_items = len(value)
        except Exception:
            total_items = len(rows)
        if len(out) < total_items:
            budget["truncated"] = True
            out["truncated_items"] = max(1, total_items - len(out))
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        rows = list(islice(value, _FORENSIC_MAX_SEQUENCE_ITEMS + 1))
        out = [
            _compact_forensic_value(item, budget=budget, depth=depth + 1)
            for item in rows[:_FORENSIC_MAX_SEQUENCE_ITEMS]
            if int(budget["nodes"]) > 0
        ]
        try:
            total_items = len(value)
        except Exception:
            total_items = len(rows)
        if len(out) < total_items:
            budget["truncated"] = True
            out.append({"truncated_items": max(1, total_items - len(out))})
        return out
    budget["truncated"] = True
    return {"unsupported_type": type(value).__name__}


def build_decision_context_capture_snapshot(
    telemetry_row: Mapping[str, Any],
    *,
    context_fields: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one bounded-shape forensic snapshot from canonical owners.

    The production projection intentionally excludes broad owner payloads such
    as ``self_learning_strategy``, ``brain_learning_effect``, and
    ``decision_quality``.  Their compact, production-justified conclusions are
    already present in S042; retaining the complete nested payloads here would
    make this sparse ring another full-history owner.
    """

    snapshot = flatten_production_telemetry_row(
        build_production_telemetry_row(telemetry_row)
    )
    budget: dict[str, Any] = {
        "nodes": _FORENSIC_MAX_NODES,
        "text_chars": _FORENSIC_MAX_TEXT_CHARS,
        "truncated": False,
    }
    snapshot.update(
        {
            _compact_forensic_key(key, budget=budget): _compact_forensic_value(
                value,
                budget=budget,
            )
            for key, value in context_fields.items()
        }
    )
    for section in DECISION_CONTEXT_BRAIN_SECTIONS:
        value = runtime.get(section)
        if isinstance(value, Mapping):
            snapshot[section] = _compact_forensic_value(value, budget=budget)
    snapshot["decision_context_compaction"] = {
        "schema": "decision_context_compaction.v1",
        "owner": "arch4.telemetry.decision_context_contract",
        "node_limit": _FORENSIC_MAX_NODES,
        "text_char_limit": _FORENSIC_MAX_TEXT_CHARS,
        "truncated": bool(budget["truncated"]),
    }
    return snapshot


__all__ = (
    "DECISION_CONTEXT_BRAIN_SECTIONS",
    "build_decision_context_capture_snapshot",
)
