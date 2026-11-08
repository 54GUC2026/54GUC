"""Blocked legacy accepted-root owner."""

from __future__ import annotations


class CoreAcceptedGenerationRootAuthority:
    """Removed duplicate. CoreAcceptedRootOutbox is the only root owner."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError(
            "core_accepted_generation_root_authority_retired:"
            "use_core_accepted_root_outbox"
        )


__all__ = ("CoreAcceptedGenerationRootAuthority",)
