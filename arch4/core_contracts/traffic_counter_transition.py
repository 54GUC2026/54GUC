"""Pure counter-pair admission, shared by billing and session projections."""

from __future__ import annotations

import math


def counter_pair_increment(
    previous: tuple[int, int] | None,
    current: tuple[int, int],
    *,
    previous_at: float,
    observed_at: float,
    period_started_at: float | None = None,
    interface_started_at: float | None = None,
) -> tuple[int, int] | None:
    """Return proven increments; None means a discontinuity, not zero usage.

    A backend may supply the start of a physically evidenced counter period.
    An application epoch, mobile reconnect alone or smaller number is not proof.
    A native interface-start hint qualifies only a decrease in both directions.
    Both directions are admitted together so a partial reset cannot corrupt the
    other baseline. The caller retains the accepted pair on a discontinuity.
    """
    if any(type(value) is not int or value < 0 for value in current):
        raise ValueError("traffic_counter_pair_invalid")
    for started_at in (period_started_at, interface_started_at):
        if started_at is not None and (
            not math.isfinite(started_at) or not 0 < started_at <= observed_at
        ):
            raise ValueError("traffic_counter_period_evidence_invalid")
    if previous is None:
        return 0, 0
    if period_started_at is not None and previous_at < period_started_at:
        return current
    # An interface can restart while the router stays up. Unlike a boot,
    # reconnecting does not itself prove that counters reset: both directions
    # must also have decreased, on a new natively evidenced interface session.
    if (interface_started_at is not None and previous_at < interface_started_at
            and all(new < old for old, new in zip(previous, current))):
        return current
    if any(new < old for old, new in zip(previous, current)):
        return None
    return current[0] - previous[0], current[1] - previous[1]
