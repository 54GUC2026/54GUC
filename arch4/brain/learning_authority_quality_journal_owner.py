"""Quarantine boundary for the retired immediate Referee quality journal.

The old journal is still readable by explicit offline forensic/report tools,
but production LearningAuthority must neither load it nor append to it.  Its
binary pre-proof outcomes are not compatible with the terminal layered reward
contract and therefore cannot seed current strategic learning.
"""

from __future__ import annotations

from typing import Any

OWNER = "arch4.brain.learning_authority_quality_journal_owner"

def quality_profile_from_learning_authority(authority: Any) -> None:
    """Never admit the legacy binary journal into runtime learning quality."""

    del authority
    return None


__all__ = [
    "OWNER",
    "quality_profile_from_learning_authority",
]
