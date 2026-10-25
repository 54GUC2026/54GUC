"""Blocked legacy staging owner. New work is prepared as a CoreAcceptedGenerationRoot and
retained by CoreAcceptedRootOutbox. This module remains importable so old callers fail
with a clear architecture error.
"""

from __future__ import annotations


class AcceptedGenerationCapacityBlocked(RuntimeError):
    """Legacy error kept so callers of the removed queue fail clearly."""


class AcceptedGenerationCommitStore:
    """Removed owner. Creating it is always an error."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError(
            "accepted_generation_commit_store_retired:"
            "use_atomic_accepted_root_preparation"
        )


class AcceptedGenerationStage:
    """Removed record type. Creating it is always an error."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError(
            "accepted_generation_stage_retired:"
            "use_core_accepted_generation_root"
        )


__all__ = (
    "AcceptedGenerationCapacityBlocked",
    "AcceptedGenerationCommitStore",
    "AcceptedGenerationStage",
)
