"""Small data containers for Modem Live snapshots.

This module has no ``arch4.*`` imports. The containers do not depend on Brain,
Engine, GUI, or backend modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class InputSnapshot:
    modem: Optional[Any]
    computed: Optional[Any]
    base_status: Dict[str, Any]
    extra: Dict[str, Any]
    neighbors: list
    connection_lost: bool
    modem_read_missed: bool
    rtt_observation: Any

    @property
    def rtt_ms(self) -> Optional[float]:
        """Return a read-only scalar view. The observation remains the source."""

        value = getattr(self.rtt_observation, "value_ms", None)
        return None if value is None else float(value)


@dataclass
class LiveModemRegisterSnapshot:
    modem: Optional[Any] = None
    base_status: Optional[Dict[str, Any]] = None
    updated_at: float = 0.0
    sequence: int = 0
    valid: bool = False
    last_error: str = ""
    source: str = ""
    history: Optional[List[Dict[str, Any]]] = None
