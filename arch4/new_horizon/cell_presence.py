"""Pure recorded-cell coverage, used by the Referee history snapshot.

No modem access, storage or AQ calculation. A share of recorded time is not
a promise that a future band request will reach that cell.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any, Iterable, Mapping

from arch4.new_horizon.hourly_aq import fixed_hour
from arch4.new_horizon.evidence_weighting import EVIDENCE_RETENTION_S, evidence_influence


def serving_cell_key(identity: Mapping[str, Any]) -> tuple[str, str, int] | None:
    """Keep the provider/device boundary outside; never infer a tower from PCI."""
    rat = identity.get("network_type")
    field = "cell_id" if identity.get("pband") is not None else "nr_cell_id"
    cell = identity.get(field)
    if not isinstance(rat, str) or not rat or type(cell) is not int or cell <= 0:
        return None
    return rat, field, cell


def daily_cell_coverage(intervals: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Union repeated coverage, exclude conflicting cells, then split clock hours."""
    events: dict[float, list[tuple[tuple[Any, ...], int]]] = defaultdict(list)
    seen = set()
    for row in intervals:
        cell = serving_cell_key(row["identity"])
        if cell is None:
            continue
        start, end = row["opened_at"], row["closed_at"]
        if any(type(t) not in {int, float} or not math.isfinite(t) or t <= 0
               for t in (start, end)) or end <= start:
            raise ValueError("new_horizon_cell_presence_interval_invalid")
        zone = row["timezone"]
        source = (cell, zone)
        item = (source, start, end)
        if item in seen:
            continue
        seen.add(item)
        events[start].append((source, 1))
        events[end].append((source, -1))
    active: dict[tuple[Any, ...], int] = defaultdict(int)
    days: dict[tuple[Any, ...], dict[str, Any]] = {}
    previous = None
    for stamp, changes in sorted(events.items()):
        sources = [source for source, count in active.items() if count > 0]
        cells = {cell for cell, zone in sources}
        # Contradictory identities are not two simultaneous serving cells.
        if previous is not None and len(cells) == 1:
            cell = next(iter(cells))
            cursor = previous
            while cursor < stamp:
                slots = [fixed_hour(cursor, timezone=zone) for _, zone in sources]
                start, end, label, hour = slots[0]
                stop = min(stamp, end)
                if len({(s[0], s[3]) for s in slots}) != 1:
                    # Imported clocks disagree for the same time. Keep their
                    # source records, but do not assign this overlap to an hour.
                    cursor = min(stamp, *(s[1] for s in slots))
                    continue
                key = (cell, start)
                row = days.setdefault(key, dict(cell_key=list(cell), hour_start=start,
                    hour_label=label, local_hour=hour, coverage_s=0.0,
                    first_at=cursor, last_at=stop))
                row["coverage_s"] += stop - cursor
                row["last_at"] = stop
                cursor = stop
        for source, delta in changes:
            active[source] += delta
        previous = stamp
    return tuple(days[key] for key in sorted(days, key=repr))


def hourly_cell_presence(daily: Iterable[Mapping[str, Any]], *, hour: int, now: float) -> dict[str, Any]:
    """Summarize only this clock hour; retain every dated coverage row."""
    if type(hour) is not int or not 0 <= hour < 24 or not math.isfinite(now):
        raise ValueError("new_horizon_cell_presence_clock_invalid")
    rows = [dict(row) for row in daily if row["local_hour"] == hour
            and now - EVIDENCE_RETENTION_S < row["last_at"] <= now]
    mass: dict[tuple[Any, ...], float] = defaultdict(float)
    for row in rows:
        mass[tuple(row["cell_key"])] += row["coverage_s"] * evidence_influence(row["last_at"], now=now)
    total = math.fsum(mass.values())
    return dict(hour=hour, basis="recorded_context_time", coverage_s=math.fsum(r["coverage_s"] for r in rows),
        days=len({r["hour_label"][:10] for r in rows}), daily=rows,
        cells=[dict(cell_key=list(cell), share=value / total)
               for cell, value in sorted(mass.items()) if total > 0])


def hourly_cell_outlook(pattern: Mapping[str, Any] | None) -> dict[str, Any]:
    """Rank the hour's cells before considering bands or AQ.

    Keep every observed cell. Shares describe residence, not calibrated
    attachment probability. The supplied snapshot owns history and aging.
    """
    if pattern is None:
        return dict(state="missing", hour=None, days=0, coverage_s=0., cells=[])
    hour, days, coverage = pattern["hour"], pattern["days"], pattern["coverage_s"]
    if (type(hour) is not int or not 0 <= hour < 24
            or type(days) is not int or days < 0
            or type(coverage) not in {int, float} or not math.isfinite(coverage)
            or coverage < 0):
        raise ValueError("new_horizon_cell_outlook_evidence_invalid")
    cells = []
    seen = set()
    for row in pattern["cells"]:
        key, share = tuple(row["cell_key"]), row["share"]
        if (len(key) != 3 or not isinstance(key[0], str) or not key[0]
                or key[1] not in {"cell_id", "nr_cell_id"}
                or type(key[2]) is not int or key[2] <= 0 or key in seen
                or type(share) not in {int, float} or not math.isfinite(share)
                or not 0 < share <= 1):
            raise ValueError("new_horizon_cell_outlook_cell_invalid")
        seen.add(key)
        cells.append(dict(cell_key=list(key), share=float(share)))
    if not cells:
        if days or coverage:
            raise ValueError("new_horizon_cell_outlook_missing_cells")
        return dict(state="missing", hour=hour, days=days, coverage_s=coverage, cells=[])
    if not days or not coverage or not math.isclose(sum(r["share"] for r in cells), 1., abs_tol=1e-9):
        raise ValueError("new_horizon_cell_outlook_coverage_invalid")
    cells.sort(key=lambda r: (-r["share"], tuple(r["cell_key"])))
    return dict(state="observed",
                hour=hour, days=days, coverage_s=coverage, cells=cells)
