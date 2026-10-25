"""Leaf names for the exact active Core current-state register contract.

This module defines names only. It must not import the register bank or any
publisher, so the register owner graph remains one-way.
"""

from __future__ import annotations


REGISTER_MODEM_LIVE_MATRIX = "modem_live_matrix"
# These names identify displaced pre-matrix registers.  They are retained
# only so static audits and detached code can name the illegal paths.  They
# are not members of the active Core register catalog.
REGISTER_IDENTITY = "identity"
REGISTER_CAPABILITY = "capability"
REGISTER_RADIO = "radio"
REGISTER_MODEM_SLOW = "modem_slow"
REGISTER_MODEM_TIMING = "modem_timing"
REGISTER_TRAFFIC = "traffic"
REGISTER_LATENCY = "latency"
REGISTER_QUALITY = "quality"
REGISTER_CA = "ca"
REGISTER_HEALTH = "health"
REGISTER_RUNTIME_CLOCK = "runtime_clock"
REGISTER_MUTATION = "mutation"
REGISTER_ACTION = "action"
REGISTER_ACTION_AUXILIARY = "action_auxiliary"
REGISTER_OPERATIONAL_STATUS = "operational_status"
REGISTER_DOWNSTREAM_HEALTH = "downstream_health"
REGISTER_INTERNET_REACHABILITY = "internet_reachability"

# Signal Processing Matrix outputs have one shared source Matrix identity but
# independent ownership from Modem Live's raw Matrix.  Health, clock, action
# and operational-service registers also keep their independent owners.
SIGNAL_PROCESSING_REGISTER_NAMES: tuple[str, ...] = (
    REGISTER_LATENCY,
    REGISTER_QUALITY,
    REGISTER_CA,
)

# Exact read-only customer denominator.  Modem Live owns the raw Matrix; SPM
# owns the three derived lanes; the remaining rows keep their independent
# owners and sequences.  A customer snapshot is a view of these owners, never
# another register authority.
CORE_CUSTOMER_REGISTER_NAMES: tuple[str, ...] = (
    REGISTER_MODEM_LIVE_MATRIX,
    *SIGNAL_PROCESSING_REGISTER_NAMES,
    REGISTER_HEALTH,
    REGISTER_RUNTIME_CLOCK,
    REGISTER_MUTATION,
    REGISTER_ACTION,
    REGISTER_ACTION_AUXILIARY,
    REGISTER_OPERATIONAL_STATUS,
    REGISTER_DOWNSTREAM_HEALTH,
    REGISTER_INTERNET_REACHABILITY,
)


__all__ = [
    name
    for name in globals()
    if name.startswith("REGISTER_")
]
