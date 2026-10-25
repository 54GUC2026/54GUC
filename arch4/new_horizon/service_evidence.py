"""Availability projection used only by the Referee history owner.

AQ says how good the measured service was. NW says an identified connection
did not work; NE says it was not observed. Neither is a numeric grade.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable, Mapping

from arch4.new_horizon.hourly_aq import (
    AQ_RETENTION_SECONDS, fixed_hour, hourly_observed_vector,
    AQ_PHYSICAL_FIELDS as RADIO_FIELDS, aq_pool_key,
)
from arch4.new_horizon.evidence_weighting import evidence_influence


def service_subject(identity: Mapping[str, Any]) -> tuple[Any, ...]:
    pool = tuple(identity["ca_pool"])
    # Same serving identity, same pool equivalence already used for AQ.
    return (*tuple(identity.get(k) for k in RADIO_FIELDS),
            aq_pool_key(pool))


def hourly_service_evidence(hours: Iterable[Mapping[str, Any]],
                            partials: Iterable[Mapping[str, Any]], *,
                            now: float, hour: int, timezone: str) -> tuple[dict[str, Any], ...]:
    """Read validated history; never grade, ping, or infer an absent cell."""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    source = []
    for h in hours:
        # Referee validated these records when it admitted the hourly snapshot.
        # Only this clock hour can contribute to this query; decoding every
        # retained hour on every live observation repeats the full-history work.
        if (h["local_hour"] != hour or h["last_at"] > now
                or now - h["last_at"] >= AQ_RETENTION_SECONDS):
            continue
        source.append(dict(identity=h["identity"], metrics=hourly_observed_vector(h), last_at=h["last_at"],
                           duration=h["coverage_s"], local_hour=h["local_hour"], ref="aq-hour:"+h["hour_id"]))
    for p in partials:
        slot_start, slot_end, _, local_hour = fixed_hour(p["opened_at"], timezone=timezone)
        if p["closed_at"] > slot_end:
            # A combined percentage cannot be split honestly between hours.
            # Keep the original visit in history, outside hourly advice.
            continue
        source.append(dict(identity=p["identity"], metrics=p["observed_vector"], last_at=p["closed_at"],
                           duration=p["closed_at"]-p["opened_at"],
                           local_hour=local_hour,
                           ref="partial:"+p["assessment_id"]))
    seen = set()
    for r in source:
        identity, m = r["identity"], r["metrics"]
        if (r["ref"] in seen or r["local_hour"] != hour or r["last_at"] > now
                or now-r["last_at"] >= AQ_RETENTION_SECONDS):
            continue
        seen.add(r["ref"])
        if any(identity.get(k) is None for k in RADIO_FIELDS[:5]):
            continue
        pct = m.get("internet_availability_pct")
        if type(pct) not in {int, float} or not math.isfinite(pct) or not 0 <= pct <= 100:
            continue
        if "internet_attributed_s" in m:
            duration, working = m["internet_attributed_s"], m.get("internet_working_s")
            if (type(duration) not in {int, float} or type(working) not in {int, float}
                    or not math.isfinite(duration) or not math.isfinite(working)
                    or not 0 <= working <= duration):
                raise ValueError("referee_service_evidence_duration_invalid")
            if duration <= 0:
                continue
            r["duration"] = duration
            pct = 100.0 * working / duration
        elif pct != 100 or m.get("sample_count", 0) <= 0:
            # Existing healthy measured history proves usable service. Older
            # failed/mixed windows lack attribution and cannot manufacture NW.
            continue
        if r["duration"] <= 0:
            continue
        r["availability_pct"] = float(pct)
        groups[service_subject(identity)].append(r)
    result = []
    for key, rows in sorted(groups.items(), key=lambda pair: repr(pair[0])):
        weights = [r["duration"]*evidence_influence(now=now, observed_at=r["last_at"]) for r in rows]
        total = math.fsum(weights)
        if total <= 0:
            continue
        pct = math.fsum(w*r["availability_pct"] for w, r in zip(weights, rows))/total
        coverage = math.fsum(r["duration"] for r in rows)
        state = ("NW" if pct == 0 else "WORKING" if pct == 100 else "INTERMITTENT")
        result.append(dict(identity=dict(rows[-1]["identity"]), state=state,
                           hour=hour, availability_pct=pct, coverage_s=coverage,
                           evidence_refs=[r["ref"] for r in rows], last_at=max(r["last_at"] for r in rows)))
    return tuple(result)


def target_service_evidence(rows: Iterable[Mapping[str, Any]], *, pband: int,
                            pool: tuple[int, ...], identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Keep separate cells visible; no observed alternative means NE."""
    matches = [r for r in rows if r["identity"]["pband"] == pband
               and aq_pool_key(r["identity"]["ca_pool"]) == aq_pool_key(pool)
               and (identity is None or all(r["identity"].get(k) == identity.get(k) for k in RADIO_FIELDS))]
    if not matches:
        return dict(state="NE", coverage_s=0.0, availability_pct=None, evidence_refs=[], contexts=[])
    state = ("NW" if all(r["state"] == "NW" for r in matches) else
             "WORKING" if all(r["state"] == "WORKING" for r in matches) else "INTERMITTENT")
    return dict(state=state, coverage_s=sum(r["coverage_s"] for r in matches),
                availability_pct=min(r["availability_pct"] for r in matches),
                evidence_refs=sorted({ref for r in matches for ref in r["evidence_refs"]}),
                contexts=matches)
