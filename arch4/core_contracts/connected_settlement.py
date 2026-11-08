"""One source-clock contract for the existing connected settling interval."""
from __future__ import annotations

import math

CONNECTED_SETTLE_S = 10.0
CONNECTION_SETTLING_REASON = "connection.settling"


class ConnectedSettlement:
    """Observe accepted receipts; never sleep or start a processing-time delay."""

    def __init__(self, *, settle_s: float = CONNECTED_SETTLE_S) -> None:
        self.settle_s = float(settle_s)
        if not math.isfinite(self.settle_s) or self.settle_s < 0:
            raise ValueError("connected_settlement_duration_invalid")
        self._namespace = None
        self._since: float | None = None
        self._first_sequence = 0

    def evaluate(self, *, connected: bool, source_at: float,
                 source_sequence: int, namespace: object) -> dict[str, object]:
        if type(connected) is not bool or type(source_sequence) is not int or source_sequence < 0:
            raise TypeError("connected_settlement_receipt_invalid")
        stamp = float(source_at)
        if not math.isfinite(stamp) or stamp < 0:
            raise ValueError("connected_settlement_timestamp_invalid")
        if not connected or namespace != self._namespace:
            self._namespace = namespace
            self._since = None
            self._first_sequence = 0
        if connected and source_sequence > 0 and stamp > 0 and self._since is None:
            self._since = stamp
            self._first_sequence = source_sequence
        elapsed = max(0., stamp-self._since) if self._since is not None else 0.
        advanced = source_sequence > self._first_sequence > 0
        return dict(connected_elapsed_s=elapsed,
            source_advanced_after_connect=advanced,
            stable_connected=bool(connected and advanced and elapsed >= self.settle_s))
