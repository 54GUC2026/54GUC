"""Read-only projection facade for Modem Live compatibility identity.

The former upper-layer hash allocator was removed.  Exact identity can only be
published by ``arch4.modem_live.compatibility_identity`` and this module may
only validate/copy that publication for legacy read callers.
"""

from __future__ import annotations

from typing import Any, Mapping

from arch4.modem_live.compatibility_identity import (
    DecodeCatalogReplica,
    EXACT_STATUS as EXACT_IDENTITY_STATUS,
    IDENTITY_SCHEMA as SCHEMA,
    OWNER,
    PROVISIONAL_STATUS as PROVISIONAL_IDENTITY_STATUS,
    UNKNOWN_IDENTITY_CODE,
    UNKNOWN_STATUS as UNKNOWN_IDENTITY_STATUS,
    validated_identity_publication_copy,
)


def identity_publication_from_status(
    status: Mapping[str, Any] | None,
    *,
    replica: DecodeCatalogReplica | None = None,
) -> dict[str, Any]:
    """Validate and copy an atomic Modem Live identity publication."""

    return validated_identity_publication_copy(
        dict(status or {}),
        replica=replica or DecodeCatalogReplica(),
    )


__all__ = [
    "EXACT_IDENTITY_STATUS",
    "OWNER",
    "PROVISIONAL_IDENTITY_STATUS",
    "SCHEMA",
    "UNKNOWN_IDENTITY_CODE",
    "UNKNOWN_IDENTITY_STATUS",
    "identity_publication_from_status",
]
