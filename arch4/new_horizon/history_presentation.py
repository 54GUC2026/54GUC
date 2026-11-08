"""Saved radio evidence to display fields; never consult the live connection."""

from collections.abc import Mapping

from arch4.core_contracts.quality_model import NR_QUALITY_MODEL
from arch4.new_horizon.hourly_aq import nr_evidence_condition


def saved_nr_label(identity: Mapping, vector: Mapping) -> str:
    """Keep the recorded NR condition separate from the LTE target label."""
    condition = nr_evidence_condition(identity)
    if condition is None:
        return "---"
    if condition != ("no_nr",):
        return condition[1]
    return "NR off" if vector.get("quality_model") == NR_QUALITY_MODEL else "---"
