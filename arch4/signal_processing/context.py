"""Signal context identity, shared by calculation and display-hold consumers.

The first three components describe the radio cell. A subscription and serving
network qualify every production key; measurement bandwidth is an extra RSRQ
dimension. Unconfirmed contexts never match a confirmed subscription.
"""
from __future__ import annotations

import re


def signal_context_key(*, band: int, cell_id: int, earfcn: int,
                       subscription_key: str, network_plmn: str) -> str:
    if subscription_key and not re.fullmatch(r"[a-f0-9]{64}", subscription_key):
        raise ValueError("signal_context_subscription_invalid")
    if network_plmn and not re.fullmatch(r"[0-9]{5,6}", network_plmn):
        raise ValueError("signal_context_network_invalid")
    return (f"{band}|{cell_id}|{earfcn}|"
            f"sim={subscription_key or 'unconfirmed'}|net={network_plmn or 'unregistered'}")


def rsrq_hold_context_base(ctx_key: str) -> str:
    """Ignore bandwidth only, retaining the subscription/network boundary."""
    parts = str(ctx_key or "").split("|")
    if len(parts) == 6 and parts[3].startswith("sim=") and parts[4].startswith("net="):
        return "|".join(parts[:5])
    # Local processor contexts contain only radio dimensions. They are not
    # production lookup keys; retained old durable entries cannot match new keys.
    if len(parts) in (3, 4):
        return "|".join(parts[:3])
    return str(ctx_key or "")


def signal_subscription_scope(ctx_key: str) -> str:
    parts = str(ctx_key or "").split("|")
    return "|".join(parts[3:5]) if len(parts) >= 5 else ""
