"""Hard-failed retired accepted-generation delivery owner.

CoreAcceptedGenerationRoot plus CoreAcceptedRootOutbox are the only legal
post-acceptance publication boundary.  This module remains importable only so
an accidental old caller receives an explicit architecture error.
"""

from __future__ import annotations


class AcceptedGenerationDeliveryStore:
    """Retired owner; construction is always illegal."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError(
            "accepted_generation_delivery_store_retired:"
            "use_core_accepted_generation_root_outbox"
        )


__all__ = ("AcceptedGenerationDeliveryStore",)
