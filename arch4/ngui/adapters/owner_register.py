"""Convert an owner register update into an NGUI input batch."""

from __future__ import annotations

import math
from typing import Any, Mapping

from arch4.core_contracts.current_state import (
    CoreCurrentFieldState,
    core_current_field_state,
)

from ..input_registers import (
    InputValue,
    OwnerInputBatch,
    normalize_ngui_input_value,
)
from ..registry import input_source_spec
from ..types import FreshnessState, SourceOwner


def adapt_owner_register(
    *,
    owner: SourceOwner,
    sequence: int,
    published_at: float,
    values: Mapping[str, Any],
    validity: Mapping[str, bool] | None = None,
    sampled_at: Mapping[str, float] | None = None,
    sample_sequences: Mapping[str, int] | None = None,
    freshness: Mapping[str, FreshnessState] | None = None,
    reasons: Mapping[str, str] | None = None,
    fact_states: Mapping[str, CoreCurrentFieldState | str] | None = None,
) -> OwnerInputBatch:
    """Reject undeclared fields and fields from another owner."""

    validity = validity or {}
    freshness = freshness or {}
    reasons = reasons or {}
    fact_states = fact_states or {}
    source_ids = frozenset(current_values := dict(values or {}))
    sampled_at_values = _required_source_metadata(
        sampled_at,
        source_ids=source_ids,
        label="sampled_at",
    )
    sample_sequence_values = _required_source_metadata(
        sample_sequences,
        source_ids=source_ids,
        label="sample_sequences",
    )
    items: list[InputValue] = []
    for source_id, value in current_values.items():
        spec = input_source_spec(source_id)
        if spec.owner is not owner:
            raise ValueError(
                f"{source_id} belongs to {spec.owner.value}, not adapter owner {owner.value}"
            )
        try:
            source_sampled_at = float(sampled_at_values[source_id])
        except (TypeError, ValueError):
            raise ValueError("owner_register_sampled_at_invalid") from None
        source_valid = bool(validity.get(source_id, True))
        if (
            not math.isfinite(source_sampled_at)
            or source_sampled_at < 0.0
            or (source_valid and source_sampled_at <= 0.0)
        ):
            raise ValueError("owner_register_sampled_at_invalid")
        try:
            source_sample_sequence = int(sample_sequence_values[source_id])
        except (TypeError, ValueError):
            raise ValueError("owner_register_sample_sequence_invalid") from None
        if source_sample_sequence < 0 or (
            source_valid and source_sample_sequence <= 0
        ):
            raise ValueError("owner_register_sample_sequence_invalid")
        items.append(
            InputValue(
                source_id=source_id,
                value=normalize_ngui_input_value(
                    value,
                    path=f"$.{source_id}",
                ),
                valid=source_valid,
                sampled_at=source_sampled_at,
                sample_sequence=source_sample_sequence,
                freshness=freshness.get(source_id, FreshnessState.FRESH),
                reason=str(reasons.get(source_id, "") or ""),
                fact_state=core_current_field_state(
                    fact_states.get(
                        source_id,
                        CoreCurrentFieldState.AVAILABLE,
                    )
                ),
            )
        )
    return OwnerInputBatch(
        owner=owner,
        sequence=sequence,
        published_at=published_at,
        values=tuple(items),
    )


def _required_source_metadata(
    supplied: Mapping[str, Any] | None,
    *,
    source_ids: frozenset[str],
    label: str,
) -> dict[str, Any]:
    current = dict(supplied or {})
    extra = sorted(set(current) - source_ids)
    missing = sorted(source_ids - set(current))
    if extra or missing:
        raise ValueError(
            f"owner_register_{label}_catalog_mismatch:"
            f"missing={missing};extra={extra}"
        )
    return {source_id: current[source_id] for source_id in sorted(source_ids)}
