"""Read-only integrity validator for the CGF current-run evidence store."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime_artifacts import read_cgf_evidence_events


OWNER = "arch4.foundation.evidence_validation"
SCHEMA = "CgfEvidenceValidation.v1"


@dataclass(frozen=True, slots=True)
class CgfEvidenceValidation:
    path: str
    record_count: int
    first_sequence: int
    last_sequence: int
    first_timestamp: float
    last_timestamp: float
    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "path": self.path,
            "valid": self.valid,
            "record_count": self.record_count,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "errors": list(self.errors),
        }


def validate_cgf_evidence(path: str | Path) -> CgfEvidenceValidation:
    source = Path(path).expanduser().resolve()
    records, status = read_cgf_evidence_events(source, max_events=1_000_000)
    disposition = str(status.get("disposition") or "")
    errors = () if disposition == "captured" else (
        f"{disposition}:{status.get('reason') or source}",
    )
    return CgfEvidenceValidation(
        path=str(source),
        record_count=len(records),
        first_sequence=(int(records[0]["sequence"]) if records else 0),
        last_sequence=(int(records[-1]["sequence"]) if records else 0),
        first_timestamp=(float(records[0]["ts"]) if records else 0.0),
        last_timestamp=(float(records[-1]["ts"]) if records else 0.0),
        errors=errors,
    )


__all__ = (
    "CgfEvidenceValidation",
    "OWNER",
    "SCHEMA",
    "validate_cgf_evidence",
)
