"""ModemInput boundary validation.

This module is the crash-safety gate between Modem Live/adapters and the rest
of the system. It does not repair modem truth. It either accepts a sample as
safe to consume or returns a typed quarantine reason.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import math
from typing import Any

from arch4.interfaces import ModemInput


@dataclass(frozen=True)
class ModemInputValidation:
    valid: bool
    reason: str = ""
    field: str = ""
    value_repr: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": bool(self.valid),
            "state": "VALID" if self.valid else "INVALID_SAMPLE",
            "reason": self.reason,
            "field": self.field,
            "value_repr": self.value_repr,
        }


_NON_NEGATIVE_FIELDS: frozenset[str] = frozenset(
    {
        "timestamp",
        "session_uptime_s",
        "primary_band",
        "serving_band",
        "pci",
        "earfcn",
        "enb_id",
        "rx_bytes_per_sec",
        "tx_bytes_per_sec",
        "dl_mbps",
        "ul_mbps",
        "rtt_ms",
    }
)

_SEQUENCE_FIELDS: frozenset[str] = frozenset(
    {
        "carriers",
        "neighbors",
        "selected_band_set",
        "active_band_set",
        "band_set",
        "nr_cells",
    }
)


def _short_repr(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 120 else text[:117] + "..."


def validate_modem_input_sample(sample: Any) -> ModemInputValidation:
    """Validate one ModemInput before it crosses the engine boundary.

    This deliberately avoids source fallbacks or guessed repairs. If an adapter
    returns garbage, downstream modules receive a typed missed-read/quarantine
    state instead of malformed data.
    """

    if not isinstance(sample, ModemInput):
        return ModemInputValidation(
            valid=False,
            reason="not_modem_input",
            field="sample",
            value_repr=type(sample).__name__,
        )

    for field_info in fields(ModemInput):
        name = field_info.name
        value = getattr(sample, name, None)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            numeric = float(value)
            if not math.isfinite(numeric):
                return ModemInputValidation(
                    valid=False,
                    reason="non_finite_numeric",
                    field=name,
                    value_repr=_short_repr(value),
                )
            if name in _NON_NEGATIVE_FIELDS and numeric < 0.0:
                return ModemInputValidation(
                    valid=False,
                    reason="negative_numeric",
                    field=name,
                    value_repr=_short_repr(value),
                )
        elif name in _SEQUENCE_FIELDS and not isinstance(value, (list, tuple)):
            return ModemInputValidation(
                valid=False,
                reason="invalid_sequence",
                field=name,
                value_repr=_short_repr(value),
            )

    return ModemInputValidation(valid=True)

