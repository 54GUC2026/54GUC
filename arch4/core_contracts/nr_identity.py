"""Passive NR identity value contract shared by independent runtime owners."""
from __future__ import annotations

from typing import Any


def nr_identity_values_valid(*, band: Any, arfcn: Any, pci: Any, nci: Any = None) -> bool:
    """Validate projected values, not attachment. Optional NCI and zero are data.

    Matrix availability still determines missingness; callers project unavailable
    facts as None. This contract performs no I/O, inference or scope generation.
    """
    return bool(
        type(band) is int and band > 0
        and type(arfcn) is int and arfcn >= 0
        and type(pci) is int and pci >= 0
        and (nci is None or (type(nci) is int and nci >= 0))
    )
