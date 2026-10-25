"""
Shared blocker arbitration metadata.

This module is analysis-only: it does not alter control path execution.
It provides one deterministic priority map to label primary blocker reasons.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple, Any


BLOCKER_PRIORITY: Dict[str, int] = {
    "objective_lifecycle": 10,
    "proposal_feedback": 15,
    "emergency_guard": 20,
    "layer3_action_trust": 25,
    "a1_restricted_filter": 30,
    "a1_antithrash": 40,
    "learning_guard": 60,
    "min_action_interval": 80,
}

BLOCKER_GUARD_ORDER: List[str] = [
    "layer3_action_trust",
    "a1_restricted_filter",
    "learning_guard",
    "a1_antithrash",
]


def classify_blockers(steps: List[dict]) -> Tuple[str, str, List[str]]:
    """Return primary blocker gate/reason + other blocker gates."""
    blocked_steps = [s for s in (steps or []) if str(s.get("result", "")) == "blocked"]
    if not blocked_steps:
        return "", "", []

    ranked = sorted(
        blocked_steps,
        key=lambda s: (BLOCKER_PRIORITY.get(str(s.get("gate", "")), 999), str(s.get("gate", ""))),
    )
    primary = ranked[0]
    primary_gate = str(primary.get("gate", ""))
    primary_reason = _step_block_reason(primary)
    also = [str(s.get("gate", "")) for s in ranked[1:]]
    return primary_gate, primary_reason, also


def _step_block_reason(step: dict) -> str:
    """Normalize blocker reasons so decision traces never lose the useful text."""
    for key in (
        "reason",
        "message",
        "note",
        "block_reason",
        "hold_reason",
        "escape_probe_reason",
    ):
        value = str(step.get(key, "") or "").strip()
        if value:
            return value

    gate = str(step.get("gate", "") or "").strip()
    remaining_s = step.get("remaining_s")
    if isinstance(remaining_s, (int, float)):
        if gate == "startup_grace":
            return f"startup_grace active ({int(round(float(remaining_s)))}s remaining)"
        if gate == "min_action_interval":
            return f"min_action_interval active ({int(round(float(remaining_s)))}s remaining)"
        return f"{gate} active ({int(round(float(remaining_s)))}s remaining)".strip()

    fallback = {
        "startup_grace": "startup_grace active",
        "min_action_interval": "min_action_interval active",
        "incident_hold": "incident hold active",
        "health_hold": "health hold active",
        "objective_lifecycle": "objective lifecycle blocked",
        "proposal_feedback": "proposal feedback hold active",
        "learning_guard": "learning guard blocked action",
        "band_cooldown_guard": "band cooldown active",
        "a1_antithrash": "anti-thrash hold active",
    }
    return fallback.get(gate, gate)


def ordered_blocker_guard_chain(
    guard_map: Dict[str, Callable[[Any], Any]],
) -> List[Tuple[str, Callable[[Any], Any]]]:
    """
    Return deterministic guard execution chain from centralized authority order.

    Known guards use BLOCKER_GUARD_ORDER; any extra guards are appended in
    lexical order to keep behavior deterministic.
    """
    chain: List[Tuple[str, Callable[[Any], Any]]] = []
    used: set[str] = set()
    for gate in BLOCKER_GUARD_ORDER:
        fn = guard_map.get(gate)
        if fn is None:
            continue
        chain.append((gate, fn))
        used.add(gate)
    for gate in sorted(k for k in guard_map.keys() if k not in used):
        chain.append((gate, guard_map[gate]))
    return chain
