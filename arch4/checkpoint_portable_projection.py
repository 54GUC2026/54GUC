"""Fixed-name operator/tool projection of one Intelligence checkpoint.

The selected ``IntelligenceCheckpointSet.v1`` generation is learning truth. This
module publishes a bounded fixed-name projection for portable handoff,
external-memory inspection, and operator tooling.  Runtime startup never mixes
these files into a manifest-selected generation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arch4.intelligence.checkpoint_store import IntelligenceCheckpointSelection
from arch4.memory_store.snapshot_store import JsonSnapshotStore


PORTABLE_CHECKPOINT_PROJECTION_SCHEMA = "PortableCheckpointProjectionReceipt.v1"
PORTABLE_CHECKPOINT_PROJECTION_OWNER = "arch4.checkpoint_portable_projection"
PORTABLE_CHECKPOINT_ARTIFACTS = (
    "memory.json",
    "stm_snapshot.json",
    "ltm_snapshot.json",
    "memory_meta.json",
    "historical.json",
)


def publish_portable_checkpoint_projection(
    root_dir: Path | str,
    selection: IntelligenceCheckpointSelection,
) -> dict[str, Any]:
    """Publish one selected generation to fixed operator/tool filenames."""

    if not isinstance(selection, IntelligenceCheckpointSelection):
        raise TypeError(
            "portable_projection_requires_intelligence_checkpoint_selection"
        )
    receipt: dict[str, Any] = {
        "schema": PORTABLE_CHECKPOINT_PROJECTION_SCHEMA,
        "owner": PORTABLE_CHECKPOINT_PROJECTION_OWNER,
        "generation": str(selection.generation or ""),
        "status": "publishing",
        "published": [],
        "errors": [],
    }
    root = Path(root_dir)
    for logical_name in PORTABLE_CHECKPOINT_ARTIFACTS:
        try:
            payload = selection.artifact(logical_name)
            JsonSnapshotStore(root, logical_name).write(
                payload,
                indent=2,
                sort_keys=logical_name in {"memory_meta.json", "historical.json"},
            )
            receipt["published"].append(logical_name)
        except Exception as exc:
            receipt["errors"].append(
                f"{logical_name}:{type(exc).__name__}: {exc}"
            )
    receipt["status"] = "published" if not receipt["errors"] else "degraded"
    receipt["complete"] = not receipt["errors"]
    return receipt


__all__ = [
    "PORTABLE_CHECKPOINT_ARTIFACTS",
    "PORTABLE_CHECKPOINT_PROJECTION_OWNER",
    "PORTABLE_CHECKPOINT_PROJECTION_SCHEMA",
    "publish_portable_checkpoint_projection",
]
