"""Fixed-clock AQ arithmetic, called only by the Referee history owner.

This module does not open history, read a modem, grade measurements or select
an action. It combines already graded complete intervals. IQ is not an input.
"""

from __future__ import annotations
from arch4.core_contracts.intelligence_proposals import valid_intelligence_grade
from arch4.core_contracts.quality_model import require_quality_model, LTE_QUALITY_MODEL, NR_QUALITY_MODEL

from arch4.new_horizon.service_quality import project_service_vector
from copy import deepcopy
from datetime import datetime, timezone as datetime_timezone
import hashlib
import json
import math
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo
from arch4.new_horizon.evidence_weighting import EvidenceWeights, EVIDENCE_RETENTION_S, evidence_influence
from arch4.new_horizon.ca_evidence import is_ca_counter, observed_ca_bands, observed_ca_support


AQ_HOUR_KIND = "referee_aq_hour_v1"
AQ_HOUR_SCHEMA = "NewHorizonFixedHourAQ.v1"
AQ_BLOCK_SECONDS = 600.0
AQ_RETENTION_SECONDS = EVIDENCE_RETENTION_S
AQ_PHYSICAL_FIELDS = ("network_type", "pband", "cell_id", "pci", "earfcn",
                      "nr_band", "nr_arfcn", "nr_pci", "nr_cell_id")


def _immutable(*_args, **_kwargs):
    raise TypeError("hourly_aq_snapshot_is_immutable")


class _AQDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __deepcopy__(self, _memo):
        return _mutable_hour_value(self)


class _AQList(list):
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _immutable

    def __deepcopy__(self, _memo):
        return _mutable_hour_value(self)


class _ValidatedAQHour(_AQDict):
    """Private marker for a detached, deeply immutable, owner-validated hour."""


def _freeze_hour_value(value):
    if isinstance(value, dict):
        return _AQDict({key: _freeze_hour_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return _AQList(_freeze_hour_value(item) for item in value)
    return value


def _mutable_hour_value(value):
    if isinstance(value, dict):
        return {key: _mutable_hour_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mutable_hour_value(item) for item in value]
    return value


def immutable_aq_hour(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate a new revision once; immutable admitted hours can be shared.

    Raw dictionaries never qualify through a claimed ID/revision. Writers use
    require_aq_hour to get an ordinary detached mutable copy instead.
    """
    if isinstance(value, _ValidatedAQHour):
        return value
    return _ValidatedAQHour(_freeze_hour_value(require_aq_hour(value)))


def nr_evidence_condition(identity: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Observed service condition, not an NR actuator or device-class key.

    A missing NR band cannot lend its outcome to a known band. Device/provider
    isolation remains with the enclosing confirmed History continuity owner.
    """
    network = identity.get("network_type")
    if not isinstance(network, str) or not network:
        return None
    if network in {"NR NSA (5G)", "NR SA (5G)"}:
        band = identity.get("nr_band")
        return (network, f"n{band}") if type(band) is int and band > 0 else None
    return ("no_nr",)


def aq_pool_key(pool: Iterable[int]) -> tuple[str | int, ...]:
    """Normal pools share evidence; one/two-band restrictions remain exact."""
    bands = tuple(sorted(set(pool)))
    return ("broad",) if len(bands) >= 3 else ("restricted", *bands)


_IDENTITY_FIELDS = (
    "network_type", "pband", "ca_pool", "cell_id", "pci", "earfcn",
    "nr_band", "nr_cell_id", "nr_pci", "nr_arfcn", "scope_key",
    "target_signature",
)
_COUNTERS = frozenset({
    "sample_count", "dl_sample_count", "jitter_sample_count",
    "latency_rtt_sample_count", "internet_probe_count", "internet_excluded_probe_count",
    "internet_attributed_s", "internet_working_s",
})


def _is_counter(name: str) -> bool:
    return name in _COUNTERS or is_ca_counter(name)


def _number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def fixed_hour(stamp: float, *, timezone: str = "local") -> tuple[float, float, str, int]:
    """Return an unambiguous local clock hour, including a DST fold's offset."""
    if not _number(stamp) or stamp <= 0:
        raise ValueError("hourly_aq_timestamp_invalid")
    local = (datetime.fromtimestamp(stamp, datetime_timezone.utc).astimezone() if timezone == "local"
             else datetime.fromtimestamp(stamp, datetime_timezone.utc if timezone == "UTC" else ZoneInfo(timezone)))
    start = local.replace(minute=0, second=0, microsecond=0).timestamp()
    # Our supported clock slots are actual one-hour intervals. A repeated DST
    # hour gets another UTC start and offset, not two hours of claimed coverage.
    end = start + 3600.0
    return start, end, local.replace(minute=0, second=0, microsecond=0).isoformat(), local.hour


def require_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(identity, Mapping) or any(k not in identity for k in _IDENTITY_FIELDS):
        raise ValueError("hourly_aq_complete_identity_contract_required")
    result = {key: deepcopy(identity[key]) for key in _IDENTITY_FIELDS}
    if not result["scope_key"] or not result["target_signature"] or not result["network_type"]:
        raise ValueError("hourly_aq_exact_identity_required")
    pool = result["ca_pool"]
    if not isinstance(pool, (list, tuple)) or any(type(b) is not int or b <= 0 for b in pool):
        raise ValueError("hourly_aq_pool_invalid")
    result["ca_pool"] = sorted(set(pool))
    for key in ("pband", "cell_id", "nr_band"):
        value = result[key]
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError("hourly_aq_identity_value_invalid:" + key)
    for key in ("pci", "nr_pci", "earfcn", "nr_arfcn", "nr_cell_id"):
        value = result[key]
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("hourly_aq_identity_value_invalid:" + key)
    return result


def make_aq_block(
    *,
    assessment_id: str,
    continuity_key: str,
    identity: Mapping[str, Any],
    opened_at: float,
    ended_at: float,
    aq: float,
    observed_vector: Mapping[str, Any],
) -> dict[str, Any]:
    """Boundary adapter for one canonical, final, ten-minute absolute result."""
    if not assessment_id or not continuity_key:
        raise ValueError("hourly_aq_source_identity_required")
    if not all(_number(t) and t > 0 for t in (opened_at, ended_at)):
        raise ValueError("hourly_aq_interval_invalid")
    if abs(ended_at - opened_at - AQ_BLOCK_SECONDS) > 0.000001:
        raise ValueError("hourly_aq_complete_ten_minute_interval_required")
    if not _number(aq) or not valid_intelligence_grade(aq,dimension="service_outcome"):
        raise ValueError("hourly_aq_grade_invalid")
    if not isinstance(observed_vector, Mapping):
        raise ValueError("hourly_aq_observed_vector_required")
    vector = deepcopy(dict(observed_vector))
    observed_ca_support(vector)
    # Reject corrupt numeric values; do not turn them into silently missing data.
    _digest(vector)
    return dict(
        assessment_id=assessment_id, continuity_key=continuity_key,
        identity=require_identity(identity), opened_at=float(opened_at),
        ended_at=float(ended_at), aq=float(aq), observed_vector=vector,
    )


def merge_aq_block(
    previous: Mapping[str, Any] | None,
    block: Mapping[str, Any],
    *,
    timezone: str,
) -> dict[str, Any]:
    """Add once to the exact hour; crossing or overlapping intervals fail loudly."""
    block = make_aq_block(**dict(block))
    start, end, label, local_hour = fixed_hour(block["opened_at"], timezone=timezone)
    if block["ended_at"] > end + 0.000001:
        raise ValueError("hourly_aq_cross_hour_interval")
    key = _digest([block["continuity_key"], block["identity"], start])
    if previous is None:
        hour = dict(
            schema=AQ_HOUR_SCHEMA, hour_id=key, continuity_key=block["continuity_key"],
            identity=block["identity"], timezone=timezone, hour_start=start, hour_end=end,
            local_hour=local_hour, hour_label=label, coverage_s=0.0, aq_sum=0.0,
            time_sum=0.0, first_at=block["opened_at"], last_at=block["ended_at"],
            intervals=[], metrics={},
        )
    else:
        hour = require_aq_hour(previous)
        if hour["hour_id"] != key:
            raise ValueError("hourly_aq_merge_identity_mismatch")
    model = block["observed_vector"].get("quality_model")
    if model is not None:
        require_quality_model(model)
        if "quality_model" in hour and hour["quality_model"] != model:
            raise ValueError("hourly_aq_quality_models_mixed")
        if previous is not None and "quality_model" not in hour and model != LTE_QUALITY_MODEL:
            raise ValueError("hourly_aq_retired_population_not_importable")
    signature = _digest(block)
    for old in hour["intervals"]:
        if old["assessment_id"] == block["assessment_id"]:
            if old["signature"] != signature:
                raise ValueError("hourly_aq_source_identity_reused")
            return hour
        if max(old["start"], block["opened_at"]) < min(old["end"], block["ended_at"]) - 0.000001:
            raise ValueError("hourly_aq_overlapping_coverage")
    duration = block["ended_at"] - block["opened_at"]
    if model is not None:
        hour["quality_model"] = model
    hour["coverage_s"] += duration
    hour["aq_sum"] += block["aq"] * duration
    hour["time_sum"] += block["ended_at"] * duration
    hour["first_at"] = min(hour["first_at"], block["opened_at"])
    hour["last_at"] = max(hour["last_at"], block["ended_at"])
    # Only interval identity is retained, not individual ten-minute grades.
    hour["intervals"].append(dict(assessment_id=block["assessment_id"], signature=signature,
        start=block["opened_at"], end=block["ended_at"]))
    hour["intervals"].sort(key=lambda item: (item["start"], item["assessment_id"]))
    for name, value in sorted(block["observed_vector"].items()):
        if not _number(value) or name == "observed_at":
            continue
        metric = hour["metrics"].setdefault(name, dict(total=0.0, coverage_s=0.0, maximum=float(value)))
        metric["total"] += float(value) if _is_counter(name) else float(value) * duration
        metric["coverage_s"] += duration
        metric["maximum"] = max(metric["maximum"], float(value))
    hour["aq"] = hour["aq_sum"] / hour["coverage_s"]
    hour["mean_evidence_at"] = hour["time_sum"] / hour["coverage_s"]
    hour["revision"] = _digest({k:v for k,v in hour.items() if k != "revision"})
    return require_aq_hour(hour)


def require_aq_hour(value: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, _ValidatedAQHour):
        return _mutable_hour_value(value)
    if not isinstance(value, Mapping) or value.get("schema") != AQ_HOUR_SCHEMA:
        raise ValueError("hourly_aq_schema_invalid")
    hour = deepcopy(dict(value))
    if "quality_model" in hour:
        require_quality_model(hour["quality_model"])
    identity = require_identity(hour["identity"])
    local = datetime.fromisoformat(hour["hour_label"])
    start, end = hour["hour_start"], hour["hour_end"]
    if (local.utcoffset() is None or local.timestamp() != start or
        end != start + 3600.0 or local.hour != hour["local_hour"] or
        (local.minute, local.second, local.microsecond) != (0, 0, 0)):
        raise ValueError("hourly_aq_clock_identity_invalid")
    if hour["hour_id"] != _digest([hour["continuity_key"], identity, start]):
        raise ValueError("hourly_aq_hour_identity_invalid")
    intervals = hour["intervals"]
    if not isinstance(intervals, list) or not 1 <= len(intervals) <= 6:
        raise ValueError("hourly_aq_interval_count_invalid")
    ordered = sorted(intervals, key=lambda r: (r["start"], r["assessment_id"]))
    if len({r["assessment_id"] for r in intervals}) != len(intervals):
        raise ValueError("hourly_aq_duplicate_source")
    for row in ordered:
        if (not row["assessment_id"] or not row["signature"] or
            row["start"] < start or row["end"] > end + .000001 or
            abs(row["end"] - row["start"] - AQ_BLOCK_SECONDS) > .000001):
            raise ValueError("hourly_aq_interval_invalid")
    if any(b["start"] < a["end"] - .000001 for a,b in zip(ordered, ordered[1:])):
        raise ValueError("hourly_aq_overlapping_coverage")
    coverage = math.fsum(r["end"]-r["start"] for r in ordered)
    if abs(coverage-hour["coverage_s"]) > .000001 or not 0 < coverage <= 3600.000001:
        raise ValueError("hourly_aq_coverage_invalid")
    if (not _number(hour["aq"]) or not valid_intelligence_grade(hour["aq"],dimension="service_outcome") or
        abs(hour["aq"] - hour["aq_sum"]/coverage) > .000001):
        raise ValueError("hourly_aq_grade_invalid")
    if abs(hour["time_sum"] / coverage - hour["mean_evidence_at"]) > .000001:
        raise ValueError("hourly_aq_age_invalid")
    if not hour["first_at"] <= hour["mean_evidence_at"] <= hour["last_at"] <= end + .000001:
        raise ValueError("hourly_aq_age_invalid")
    for metric in hour["metrics"].values():
        if (not all(_number(metric.get(k)) for k in ("total", "coverage_s", "maximum"))
            or not 0 < metric["coverage_s"] <= coverage + .000001):
            raise ValueError("hourly_aq_metric_invalid")
    observed_ca_support({name: metric["total"] for name, metric in hour["metrics"].items()
                         if name.startswith("ca_observed_")})
    if hour["revision"] != _digest({k:v for k,v in hour.items() if k != "revision"}):
        raise ValueError("hourly_aq_revision_invalid")
    return hour


def rekey_aq_hour(
    value: Mapping[str, Any], *, continuity_key: str, scope_key: str,
) -> dict[str, Any]:
    """Move a validated hour to a corrected identity without regrading it."""

    hour = require_aq_hour(value)
    if not all(type(item) is str and item for item in (continuity_key, scope_key)):
        raise ValueError("hourly_aq_rekey_identity_required")
    hour["continuity_key"] = continuity_key
    hour["identity"]["scope_key"] = scope_key
    hour["hour_id"] = _digest(
        [continuity_key, hour["identity"], hour["hour_start"]]
    )
    hour["revision"] = _digest(
        {key: item for key, item in hour.items() if key != "revision"}
    )
    return require_aq_hour(hour)


def hourly_observed_vector(hour: Mapping[str, Any]) -> dict[str, Any]:
    return _hourly_observed_vector_validated(immutable_aq_hour(hour))


def _hourly_observed_vector_validated(hour: Mapping[str, Any]) -> dict[str, Any]:
    """Project one hour already validated by the caller at this boundary."""
    vector = {
        **({"quality_model": hour["quality_model"]} if "quality_model" in hour else {}),
        **{
        name: (
            metric["total"] if _is_counter(name) else
            metric["maximum"] if name == "max_dl_mbps" else
            metric["total"] / metric["coverage_s"]
        )
        for name, metric in hour["metrics"].items()
        },
    }

    # Permanent pre-model LTE hourly adapter keeps its archived raw fields.
    return project_service_vector(vector) if "quality_model" in vector else vector


def merge_aq_hours(previous: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Join two validated identity-migration summaries without invented coverage."""
    a, b = require_aq_hour(previous), require_aq_hour(incoming)
    if a["hour_id"] != b["hour_id"]:
        raise ValueError("hourly_aq_merge_identity_mismatch")
    if a.get("quality_model") != b.get("quality_model"):
        raise ValueError("hourly_aq_quality_models_mixed")
    left = {i["assessment_id"]: i for i in a["intervals"]}
    right = {i["assessment_id"]: i for i in b["intervals"]}
    common = left.keys() & right.keys()
    if any(left[k] != right[k] for k in common):
        raise ValueError("hourly_aq_source_identity_reused")
    if left.keys() == right.keys():
        if a != b:
            raise ValueError("hourly_aq_conflicting_hour_revisions")
        return a
    if left.keys() <= right.keys():
        return b
    if right.keys() <= left.keys():
        return a
    if common:
        raise ValueError("hourly_aq_partial_overlap_cannot_be_subtracted")
    result = deepcopy(a)
    for key in ("coverage_s", "aq_sum", "time_sum"):
        result[key] = math.fsum((a[key], b[key]))
    result["first_at"] = min(a["first_at"], b["first_at"])
    result["last_at"] = max(a["last_at"], b["last_at"])
    result["intervals"] = sorted(a["intervals"] + b["intervals"], key=lambda i:(i["start"],i["assessment_id"]))
    for name, metric in b["metrics"].items():
        if name not in result["metrics"]:
            result["metrics"][name] = deepcopy(metric)
        else:
            entry = result["metrics"][name]
            entry["total"] = math.fsum((entry["total"], metric["total"]))
            entry["coverage_s"] = math.fsum((entry["coverage_s"], metric["coverage_s"]))
            entry["maximum"] = max(entry["maximum"], metric["maximum"])
    result["aq"] = result["aq_sum"] / result["coverage_s"]
    result["mean_evidence_at"] = result["time_sum"] / result["coverage_s"]
    result["revision"] = _digest({k:v for k,v in result.items() if k != "revision"})
    return require_aq_hour(result)


def aggregate_aq_blocks(
    blocks: Iterable[Mapping[str, Any]], *, timezone: str,
    discard_legacy_crossings: bool = False,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    """Import boundary: only explicitly approved crossing blocks may be excluded."""
    hours: dict[tuple[str,str,float], dict[str, Any]] = {}
    excluded: set[str] = set()
    seen: dict[str,str] = {}
    for raw in sorted(blocks, key=lambda r:(r["opened_at"],r["assessment_id"])):
        block = make_aq_block(**dict(raw))
        source_key = block["continuity_key"] + ":" + block["assessment_id"]
        signature = _digest(block)
        if source_key in seen:
            if seen[source_key] != signature:
                raise ValueError("hourly_aq_source_identity_reused")
            continue
        seen[source_key] = signature
        start, end, _, _ = fixed_hour(block["opened_at"], timezone=timezone)
        if block["ended_at"] > end + .000001:
            if not discard_legacy_crossings:
                raise ValueError("hourly_aq_cross_hour_interval")
            excluded.add(source_key)
            continue
        key = (block["continuity_key"],_digest(block["identity"]),start)
        hours[key] = merge_aq_block(hours.get(key),block,timezone=timezone)
    return tuple(hours[key] for key in sorted(hours)), tuple(sorted(excluded))


def hourly_aq_advice(
    hours: Iterable[Mapping[str, Any]], *, now: float,
) -> tuple[dict[str, Any], ...]:
    """One value for each separate clock hour and exact stored identity.

    Dated hours remain separate in storage. Advice combines the same clock
    hour across retained days. Age reduces influence, not the saved AQ value.
    """
    if not _number(now) or now <= 0:
        raise ValueError("hourly_aq_clock_invalid")
    grouped: dict[tuple[str,str,int,tuple[int, ...]], list[dict[str, Any]]] = {}
    seen: dict[str, str] = {}
    for raw in hours:
        hour = immutable_aq_hour(raw)
        old_revision = seen.get(hour["hour_id"])
        if old_revision is not None:
            if old_revision != hour["revision"]:
                raise ValueError("hourly_aq_conflicting_hour_revisions")
            continue
        seen[hour["hour_id"]] = hour["revision"]
        if hour["last_at"] > now or now-hour["last_at"] >= AQ_RETENTION_SECONDS:
            continue
        age = evidence_influence(hour["mean_evidence_at"], now=now)
        if not age:
            continue
        entry = {**hour, "age_weight": age}
        # Keep different measured carrier requirements separate until the one
        # target selector admits them. AQ and physical identity do not change.
        ca_bands = observed_ca_bands(_hourly_observed_vector_validated(hour))
        key = (hour["continuity_key"], _digest(hour["identity"]), hour["local_hour"], ca_bands)
        grouped.setdefault(key,[]).append(entry)
    result=[]
    for key in sorted(grouped):
        rows=sorted(grouped[key],key=lambda r:r["hour_start"])
        total=math.fsum(r["coverage_s"]*r["age_weight"] for r in rows)
        values={}
        for name in sorted(set().union(*(r["metrics"] for r in rows))):
            pairs=[(r["metrics"][name],r["age_weight"]) for r in rows if name in r["metrics"]]
            if _is_counter(name):
                values[name]=math.fsum(m["total"] for m,_ in pairs)
            elif name == "max_dl_mbps":
                values[name]=max(m["maximum"] for m,_ in pairs)
            else:
                masses = tuple(m["coverage_s"] * w for m, w in pairs)
                values[name] = EvidenceWeights(masses, math.fsum(masses)).mean(
                    m["total"] / m["coverage_s"] for m, _ in pairs)
        metric_names = tuple(values)
        models = {require_quality_model(r["quality_model"]) for r in rows if "quality_model" in r}
        if len(models) > 1 or (NR_QUALITY_MODEL in models and any("quality_model" not in r for r in rows)):
            raise ValueError("hourly_aq_advice_quality_models_mixed")
        if models:
            values = project_service_vector({**values, "quality_model": next(iter(models))})
        result.append(dict(
            continuity_key=rows[0]["continuity_key"], identity=_mutable_hour_value(rows[0]["identity"]),
            local_hour=key[2], time_bucket=f"{key[2]:02d}",
            aq=EvidenceWeights(tuple(r["coverage_s"] * r["age_weight"] for r in rows), total).mean(r["aq"] for r in rows),
            coverage_s=math.fsum(r["coverage_s"] for r in rows),
            effective_blocks=total/AQ_BLOCK_SECONDS, dated_hours=len(rows),
            dated_observations=tuple(dict(hour_id=r["hour_id"], hour_start=r["hour_start"],
                aq=r["aq"], coverage_s=r["coverage_s"], observed_at=r["mean_evidence_at"])
                for r in rows),
            first_at=min(r["first_at"] for r in rows), last_at=max(r["last_at"] for r in rows),
            observed_vector=values, hour_refs=[r["hour_id"] for r in rows],
            metric_weights={name: math.fsum(r["metrics"][name]["coverage_s"] * r["age_weight"] for r in rows if name in r["metrics"])
                            for name in metric_names},
            evidence_id=_digest([(r["hour_id"], r["revision"]) for r in rows]),
        ))
    return tuple(result)


def aq_population_statistics(rows: Iterable[Mapping[str, Any]], *, now: float) -> dict[str, Any]:
    """Count dated physical observations, never the number of summary rows.

    Broad pools on the same physical cell/date/hour share one observation.
    Their measured time contributes weight; they do not become extra days.
    """
    dated: dict[tuple[Any, ...], list[tuple[float, float]]] = {}
    seen: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        physical = (*tuple(row["identity"].get(k) for k in AQ_PHYSICAL_FIELDS),
                    aq_pool_key(row["identity"]["ca_pool"]))
        for observation in row["dated_observations"]:
            key = observation["hour_id"]
            if key in seen:
                if seen[key] != observation:
                    raise ValueError("hourly_aq_conflicting_dated_observation")
                continue
            seen[key] = observation
            weight = observation["coverage_s"] * evidence_influence(observation["observed_at"], now=now)
            if weight > 0:
                dated.setdefault((observation["hour_start"], *physical), []).append((observation["aq"],weight))
    samples = [(math.fsum(q*w for q,w in values)/math.fsum(w for _,w in values),
                math.fsum(w for _,w in values)) for values in dated.values()]
    total = math.fsum(w for _,w in samples)
    mean = math.fsum(q*w for q,w in samples)/total if total else 0.0
    variance = math.fsum(w*(q-mean)**2 for q,w in samples)/total if total else 0.0
    return dict(pulls=len(samples), mean_grade=mean, variance=variance,
                negative_count=sum(q<0 for q,_ in samples))
