"""Durable Foundation record of Modem Live diagnostics."""

from __future__ import annotations

from typing import Any, Mapping

from arch4.core_contracts.accepted_generation_root import (
    CoreAcceptedGenerationRoot,
)
from arch4.foundation.runtime_artifacts import CgfEvidenceStore
from arch4.modem.backend_diagnostic import parse_modem_backend_diagnostic


FOUNDATION_MODEM_DIAGNOSTIC_CLIENT_ID = "foundation.modem_diagnostic"


class FoundationModemDiagnosticProjector:
    """Record each Modem Live-owned diagnostic fact once in bounded evidence."""

    def __init__(self, diagnostics: CgfEvidenceStore) -> None:
        if not isinstance(diagnostics, CgfEvidenceStore):
            raise TypeError("foundation_modem_diagnostic_evidence_store_required")
        self._diagnostics = diagnostics
        self._last_sequence_by_owner: dict[str, int] = {}

    def observe(
        self,
        accepted_root: CoreAcceptedGenerationRoot,
        *,
        observed_at: float,
    ) -> dict[str, Any] | None:
        if not isinstance(accepted_root, CoreAcceptedGenerationRoot):
            raise TypeError("foundation_modem_diagnostic_accepted_root_required")
        result = None
        for address in ("slow.backend_diagnostic", "slow.read_diagnostic"):
            fact = accepted_root.current_facts.exact(address)
            if fact.disposition == "available":
                recorded = self._record(fact, observed_at=observed_at)
                if recorded is not None:
                    result = recorded
        return result

    def _record(self, fact, *, observed_at):
        envelope = fact.value.to_mapping()
        payload = envelope["value"]
        if not isinstance(payload, Mapping) or not payload:
            return None
        diagnostic = parse_modem_backend_diagnostic(payload)
        previous_sequence = self._last_sequence_by_owner.get(diagnostic.owner, 0)
        if diagnostic.sequence <= previous_sequence:
            return None
        accepted = self._diagnostics.log_event(
            diagnostic.kind,
            ts=float(observed_at),
            event_owner=diagnostic.owner,
            severity=diagnostic.severity,
            operation="modem_backend_diagnostic",
            source_observed_at=diagnostic.observed_at,
            source_sequence=diagnostic.sequence,
            diagnostic_id=diagnostic.diagnostic_id,
            backend=diagnostic.backend,
            evidence=dict(diagnostic.data),
        )
        if not accepted:
            raise RuntimeError("foundation_modem_diagnostic_evidence_rejected")
        self._last_sequence_by_owner[diagnostic.owner] = diagnostic.sequence
        return diagnostic.to_payload()


__all__ = (
    "FOUNDATION_MODEM_DIAGNOSTIC_CLIENT_ID",
    "FoundationModemDiagnosticProjector",
)
