"""Command and readback register boundary."""

from __future__ import annotations

from typing import Any, Mapping

from .owner_register import adapt_owner_register
from ..input_registers import OwnerInputBatch
from ..types import SourceOwner


def adapt_command_readback_register(
    *, sequence: int, published_at: float, values: Mapping[str, Any],
) -> OwnerInputBatch:
    source_ids = tuple(values)
    return adapt_owner_register(
        owner=SourceOwner.COMMAND,
        sequence=sequence,
        published_at=published_at,
        values=values,
        sampled_at={source_id: published_at for source_id in source_ids},
        sample_sequences={source_id: sequence for source_id in source_ids},
    )
