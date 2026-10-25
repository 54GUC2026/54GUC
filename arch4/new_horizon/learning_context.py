"""Build the New Horizon learning-context identity.

Runtime accepts only the current compact contract. Older data can be converted
only by the named History-boundary migration before readers start.
"""

from __future__ import annotations

import hashlib
import json
import math
from time import localtime
from typing import Any, Mapping


LEARNING_CONTEXT_SCHEMA = "NewHorizonLearningContext.v2"
LEARNING_TIME_BUCKETS = tuple(f"{hour:02d}" for hour in range(24))
_CURRENT_FIELDS = frozenset({"schema", "exact_scope_key", "time_bucket"})
_LEGACY_FIELDS = frozenset(
    {"exact_scope_key", "day_class", "hour", "load_class"}
)


def learning_time_bucket_for_hour(hour: int) -> str:
    if type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError("new_horizon_learning_hour_invalid")
    return LEARNING_TIME_BUCKETS[hour]


def learning_time_bucket_for_timestamp(observed_at: float) -> str:
    if (
        type(observed_at) not in {int, float}
        or not math.isfinite(float(observed_at))
        or float(observed_at) <= 0.0
    ):
        raise ValueError("new_horizon_learning_timestamp_invalid")
    return learning_time_bucket_for_hour(int(localtime(float(observed_at)).tm_hour))


def build_learning_context(
    *,
    exact_scope_key: str,
    observed_at: float,
) -> dict[str, Any]:
    scope = str(exact_scope_key or "").strip()
    if not scope:
        raise ValueError("new_horizon_learning_exact_scope_required")
    return {
        "schema": LEARNING_CONTEXT_SCHEMA,
        "exact_scope_key": scope,
        "time_bucket": learning_time_bucket_for_timestamp(observed_at),
    }


def require_learning_context(components: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(components, Mapping):
        raise TypeError("new_horizon_learning_context_components_required")
    row = dict(components)
    if set(row) != set(_CURRENT_FIELDS):
        raise ValueError("new_horizon_learning_context_fields_invalid")
    if row.get("schema") != LEARNING_CONTEXT_SCHEMA:
        raise ValueError("new_horizon_learning_context_schema_invalid")
    scope = row.get("exact_scope_key")
    bucket = row.get("time_bucket")
    if type(scope) is not str or not scope or scope != scope.strip():
        raise ValueError("new_horizon_learning_exact_scope_required")
    if bucket not in LEARNING_TIME_BUCKETS:
        raise ValueError("new_horizon_learning_time_bucket_invalid")
    return {
        "schema": LEARNING_CONTEXT_SCHEMA,
        "exact_scope_key": scope,
        "time_bucket": bucket,
    }


def learning_context_key(components: Mapping[str, Any]) -> str:
    current = require_learning_context(components)
    encoded = json.dumps(
        current,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def migrate_legacy_learning_context(
    components: Mapping[str, Any],
    *, observed_at: float | None = None,
) -> dict[str, Any]:
    """Convert a retained context at the explicit History boundary."""

    if not isinstance(components, Mapping):
        raise TypeError("new_horizon_learning_context_components_required")
    row = dict(components)
    if set(row) == set(_CURRENT_FIELDS):
        if row.get("schema") == "NewHorizonLearningContext.v1":
            if row.get("time_bucket") not in ("00-05", "06-11", "12-17", "18-23"):
                raise ValueError("new_horizon_legacy_learning_time_bucket_invalid")
            # A six-hour key cannot tell us the original hour. Only the
            # retained event time is legal at this migration boundary.
            return build_learning_context(exact_scope_key=row["exact_scope_key"],
                                          observed_at=observed_at)
        return require_learning_context(row)
    if set(row) != set(_LEGACY_FIELDS):
        raise ValueError("new_horizon_legacy_learning_context_fields_invalid")
    scope = row.get("exact_scope_key")
    hour = row.get("hour")
    if type(scope) is not str or not scope or scope != scope.strip():
        raise ValueError("new_horizon_learning_exact_scope_required")
    if row.get("day_class") not in {"weekday", "weekend"}:
        raise ValueError("new_horizon_legacy_learning_day_class_invalid")
    if row.get("load_class") not in {"loaded", "quiet", "unknown"}:
        raise ValueError("new_horizon_legacy_learning_load_class_invalid")
    return {
        "schema": LEARNING_CONTEXT_SCHEMA,
        "exact_scope_key": scope,
        "time_bucket": learning_time_bucket_for_hour(hour),
    }


__all__ = (
    "LEARNING_CONTEXT_SCHEMA",
    "LEARNING_TIME_BUCKETS",
    "build_learning_context",
    "learning_context_key",
    "learning_time_bucket_for_hour",
    "learning_time_bucket_for_timestamp",
    "migrate_legacy_learning_context",
    "require_learning_context",
)
