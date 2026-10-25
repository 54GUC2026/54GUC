"""Reusable read-tool lifecycle; physical requests belong to explicit adapters.

Advanced only by Modem Live's existing maintenance worker under its IO gate.
Reports are diagnostic facts, never an alternative measurement store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from threading import Event
import time
from typing import Protocol, runtime_checkable

from arch4.modem.backend_diagnostic import ModemBackendDiagnostic
from arch4.runtime_identity import is_placeholder_identity_text


class DiagnosticDeviceContinuity:
    """One private sweep snapshot. Never updates normal identity or performs I/O."""
    FIELDS = frozenset({'model', 'hardware_version', 'firmware', 'webui',
                        'config_version', 'serial', 'imei'})

    @staticmethod
    def _text(value):
        if value is None: return ''
        if not isinstance(value, str) or len(value) > 256 or any(ord(c) < 32 for c in value):
            raise ValueError('diagnostic_identity_shape_invalid')
        text = value.strip()
        return '' if is_placeholder_identity_text(text) or text and set(text) == {'0'} else text

    @classmethod
    def native_values(cls, data, aliases):
        if not isinstance(data, Mapping):
            raise ValueError('diagnostic_identity_mapping_required')
        if set(aliases) - cls.FIELDS:
            raise ValueError('diagnostic_identity_unknown_canonical_field')
        result = {}
        for field, names in aliases.items():
            values = set()
            for name in names:
                if name not in data: continue
                items = data[name] if isinstance(data[name], list) else [data[name]]
                if len(items) > 8: raise ValueError('diagnostic_identity_alias_limit')
                values.update(cls._text(value) for value in items)
            values.discard('')
            if len(values) > 1: raise ValueError('diagnostic_identity_alias_conflict')
            if values: result[field] = next(iter(values))
        return result

    @classmethod
    def canonical_values(cls, data):
        return cls.native_values(data, {field: (field,) for field in cls.FIELDS})

    def __init__(self, identity):
        self._identity = self.canonical_values(identity)

    def _matches(self, current):
        return all(key not in self._identity or self._identity[key] == value
                   for key, value in current.items())

    def matches_snapshot(self, identity):
        """Compare the existing normal cache without anchoring or reading it."""
        try:
            return self._matches(self.canonical_values(identity))
        except ValueError:
            return False

    def observe(self, identity):
        if not isinstance(identity, Mapping):
            raise ValueError('diagnostic_identity_mapping_required')
        if set(identity) - self.FIELDS:
            raise ValueError('diagnostic_identity_unknown_canonical_field')
        current = self.canonical_values(identity)
        if not self._matches(current):
            return False
        self._identity.update(current)
        return True


class ReadDiagnosticUnavailable(RuntimeError):
    """Expected refusal before diagnostic admission or physical reads."""


class ReadDiagnosticAdapter(Protocol):
    backend: str
    observing: bool
    probes: tuple

    def read(self, probe: object) -> dict: ...
    def context_current(self) -> bool: ...
    def summarize(self, rows: list) -> list: ...


@runtime_checkable
class DiagnosticMatrixComparisonConsumer(Protocol):
    """Permanent report boundary for adapters with canonical field descriptors."""
    def bind_matrix_comparison(self, matrix_reader, *, profile_id: str) -> None: ...
    def diagnostic_coverage_context(self) -> dict: ...


@dataclass
class ReadDiagnosticRun:
    run_id: str
    namespace: object
    deadline: float
    adapter: ReadDiagnosticAdapter
    cancelled: Event = field(default_factory=Event)
    rows: list = field(default_factory=list)
    state: str = "queued"
    message: str = "Queued; normal monitoring has priority."
    sequence: int = 0
    _matrix_comparison_bound: bool = field(default=False, init=False, repr=False)
    _capture_health: dict | None = field(default=None, init=False, repr=False)
    _limited_coverage: list | None = field(default=None, init=False, repr=False)

    @property
    def terminal(self):
        return self.state not in {"queued", "running"}

    def finish(self, state, message):
        self.state, self.message = state, message

    def bind_matrix_comparison(self, matrix_reader, *, profile_id, namespace_matches=True):
        """Compare terminal evidence to one committed reader, without modem I/O."""
        from arch4.modem_live.truth_matrix import ValidatedTruthMatrixReader
        if not self.terminal:
            raise RuntimeError('diagnostic_matrix_binding_requires_terminal_run')
        if self._matrix_comparison_bound:
            raise RuntimeError('diagnostic_matrix_comparison_already_bound')
        if type(namespace_matches) is not bool:
            raise TypeError('diagnostic_namespace_match_boolean_required')
        if not namespace_matches or not isinstance(self.adapter, DiagnosticMatrixComparisonConsumer):
            return False
        if not isinstance(matrix_reader, ValidatedTruthMatrixReader):
            raise TypeError('diagnostic_validated_matrix_reader_required')
        if matrix_reader.identity.namespace != self.namespace:
            raise ValueError('diagnostic_matrix_comparison_namespace_mismatch')
        self.adapter.bind_matrix_comparison(matrix_reader, profile_id=profile_id)
        self._matrix_comparison_bound = True
        return True

    def advance(self):
        if self.terminal:
            return
        if self.cancelled.is_set():
            self.finish("cancelled", "Cancelled. Completed checks are retained.")
            return
        if time.time() >= self.deadline:
            self.finish("timed_out", "The diagnostic time limit was reached. Completed checks are retained.")
            return
        if not self.adapter.context_current():
            self.finish("interrupted", "The router session or active SIM changed. Run diagnostics again for the current connection.")
            return
        index = len(self.rows)
        probe = self.adapter.probes[index]
        started = time.monotonic()
        # An optional adapter failure must not terminate monitoring. Record its
        # type only: exception text may contain credentials or a response body.
        try:
            row = self.adapter.read(probe)
            if not isinstance(row, dict) or not isinstance(row.get("probe"), str) or not isinstance(row.get("outcome"), str):
                raise TypeError("read_diagnostic_result_mapping_required")
        except Exception as exc:
            row = {"probe": str(probe.label), "outcome": "adapter_error",
                   "error_type": type(exc).__name__}
        row["observed_at"] = time.time()
        row["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        self.rows.append(row)
        self.state = "running"
        self.message = f"Checked {index + 1} of {len(self.adapter.probes)}: {probe.label}."
        if row["outcome"] == "context_changed":
            self.finish("interrupted", "The router identity or active SIM changed; further diagnostic reads stopped.")
        elif self.cancelled.is_set():
            self.finish("cancelled", "Cancelled. Completed checks are retained.")
        elif time.time() >= self.deadline:
            self.finish("timed_out", "The diagnostic time limit was reached. Completed checks are retained.")
        elif len(self.rows) == len(self.adapter.probes):
            self.finish("completed", "All checks finished. Results are included in the support bundle.")

    def envelope(self):
        from arch4.architecture_contracts.runtime_generation import RuntimeGenerationNamespace
        from arch4.foundation.read_diagnostic_report import encode_read_diagnostic_data
        self.sequence += 1
        counts = {}
        for row in self.rows:
            outcome = row["outcome"]
            counts[outcome] = counts.get(outcome, 0) + 1
        namespace = self.namespace.as_dict() if isinstance(self.namespace, RuntimeGenerationNamespace) else None
        if self.state == 'limited' and (self._capture_health is None or self._limited_coverage is None):
            raise RuntimeError('diagnostic_limited_capture_receipt_required')
        coverage = (self._limited_coverage if self.state == 'limited' else
                    self.adapter.summarize(self.rows) if self.terminal else [])
        context = (self.adapter.diagnostic_coverage_context()
                   if isinstance(self.adapter, DiagnosticMatrixComparisonConsumer)
                   else {'owner': 'modem_live.read_diagnostics', 'backend': self.adapter.backend})
        data, retained = encode_read_diagnostic_data(controls={
            'run_id': self.run_id, 'namespace': namespace, 'state': self.state, 'message': self.message,
            'completed': len(self.rows), 'total': len(self.adapter.probes),
            'progress_pct': round(100 * len(self.rows) / len(self.adapter.probes)), 'outcomes': counts},
            results=self.rows, field_coverage=coverage,
            coverage_context=context, capture=self._capture_health)
        if not data['capture']['complete']:
            # This report is the run owner's retained evidence, not a hidden
            # second full report. Stop additional optional reads at this bound.
            self.rows = retained['results']
            self._capture_health = retained['capture']
            self._limited_coverage = retained['field_coverage']
            self.finish('limited', 'The diagnostic capture limit was reached (16 MiB decoded detail or 384 KiB encoded report). '
                        'Completed checks are retained. The report states exactly which detail was excluded.')
            data.update(state=self.state, message=self.message)
        return ModemBackendDiagnostic(
            owner="modem_live.read_diagnostics." + self.run_id,
            sequence=self.sequence, observed_at=time.time(),
            kind="router_read_diagnostic", backend=self.adapter.backend,
            severity="INFO" if data['capture']['complete'] else "WARNING", data=data,
        ).to_payload()
