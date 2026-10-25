"""Shared trajectory workflow contract values."""

from arch4.core.band_action_policy import OPERATOR_BAND_COOLDOWN_S

MIN_BAND_SEQUENCE_INTERVAL_S = int(OPERATOR_BAND_COOLDOWN_S) + 1
BAND_SEQUENCE_INTERVAL_REJECT_MESSAGE = (
    f"Band sequence step delay must be at least {MIN_BAND_SEQUENCE_INTERVAL_S} seconds."
)
