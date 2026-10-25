"""Run bounded background checks of Modem Live profiles and evidence.

Scout may update its profile overlay from proven capability reads and record
profile-to-Matrix differences. It does not own modem I/O, Matrix values,
provider policy, Core commands, or NGUI state.
"""

from __future__ import annotations

from hashlib import sha256
import json
import logging
import math
from pathlib import Path
import threading
import time
from typing import Any, Mapping

from arch4.atomic_io import atomic_write_json
from arch4.canonical_json import canonical_json_text
from arch4.memory_store.event_journal import JsonlEventJournal
from arch4.modem.catalog import BAND_CAPABILITY_DECLARED
from arch4.modem_live.session_band_capability import (
    SessionBandCapability,
    normalize_capability_bands,
)
from arch4.modem_live.profile_matrix_contract import (
    PROFILE_MATRIX_SUPPORTED,
    PROFILE_MATRIX_SYNTHETIC,
    PROFILE_MATRIX_UNSUPPORTED,
    profile_matrix_dispositions,
)
from arch4.modem_live.truth_matrix import (
    CELL_AVAILABLE,
    CELL_INVALID,
    CELL_MISSING,
    CELL_UNSUPPORTED,
    MODEM_LIVE_TRUTH_FIELD_CATALOG,
    ModemLiveTruthMatrixError,
    ValidatedTruthMatrixReader,
    validate_truth_cell_value,
)


OWNER = "54guc.modem_live.scout"
OVERLAY_SCHEMA = "ModemLiveScoutOverlay.v1"
EVENT_SCHEMA = "ModemLiveScoutRepairEvent.v1"
UNKNOWN_ALIAS_TRANSLATION_PROPOSAL_SCHEMA = "ModemLiveScoutUnknownAliasTranslationProposal.v1"
PROFILE_DIFFERENCE_EVENT_SCHEMA = "ModemLiveScoutProfileDifferenceEvent.v1"
OVERLAY_FILENAME = "modem_live_scout_overlay.json"
CORRUPT_OVERLAY_FILENAME = "modem_live_scout_overlay.corrupt.json"
JOURNAL_FILENAME = "modem_live_scout_journal.jsonl"
JOURNAL_MAX_RECORDS = 256
JOURNAL_MAX_BYTES = 512 * 1024
ALLOWED_FIELDS = frozenset({"supported_lte_bands", "supported_nr_bands"})
SCOUT_WORKER_NAME = "arch4-modem-live-scout"
WORK_CAPABILITY_REPAIR = "capability_repair"
WORK_PROFILE_MATRIX_DIFFERENCE = "profile_matrix_difference"


def build_read_diagnostic_coverage(*, rows, probes, descriptors, profile_id,
                                   matrix_reader=None, unqualified_interfaces=None):
    """Pure on-demand report. Native descriptors belong to the selected translator.

    The supplied reader is one already committed generation, not a new read or
    admission. Volatile diagnostic and Matrix values are separate observations.
    Neither this report nor an unmapped candidate can repair a profile overlay.
    """
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError('scout_diagnostic_profile_required')
    if matrix_reader is not None and not isinstance(matrix_reader, ValidatedTruthMatrixReader):
        raise TypeError('scout_diagnostic_validated_reader_required')
    if len(rows) > 64 or len(probes) > 64 or len(descriptors) > 2048:
        raise ValueError('scout_diagnostic_report_limit')
    rejected = {} if unqualified_interfaces is None else unqualified_interfaces
    if (not isinstance(rejected, Mapping) or len(rejected) > 64 or
            any(not isinstance(reason, str) or not reason or len(reason) > 240
                for reason in rejected.values())):
        raise ValueError('scout_diagnostic_unqualified_reason_invalid')
    mapped = {}
    for descriptor in descriptors:
        path = descriptor['canonical_path']
        line, _, field = path.partition('.')
        if field not in MODEM_LIVE_TRUTH_FIELD_CATALOG.get(line, ()):
            raise ValueError('scout_diagnostic_canonical_path_invalid')
        scale = descriptor['scale']
        conversion = descriptor.get('conversion')
        if conversion is not None:
            if (scale is not None or type(conversion) is not str or not conversion.strip()
                    or len(conversion)>160 or any(ord(c)<32 for c in conversion)):
                raise ValueError('scout_diagnostic_conversion_invalid')
        elif type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0:
            raise ValueError('scout_diagnostic_scale_invalid')
        key = (descriptor['interface'], descriptor['native_path'], path)
        if key in mapped and mapped[key] != descriptor:
            raise ValueError('scout_diagnostic_descriptor_conflict')
        mapped[key] = descriptor
    indexed = {}
    for (interface, native_path, _), descriptor in mapped.items():
        indexed.setdefault((interface, native_path), []).append(descriptor)
    result = []
    def item(label, state, message, **evidence):
        return dict(field=label,
                    coverage_state=state, result=message, **evidence)
    for row in rows:
        interface = row.get('interface', '')
        outcome = row['outcome']
        if outcome != 'success':
            result.append(item(row['probe'], outcome,
                f"Check ended with {outcome.replace('_', ' ')}; this does not prove feature absence",
                interface=interface, **({'native_code':row['native_code']} if 'native_code' in row else {})))
            continue
        fields = row.get('fields', {})
        if not isinstance(fields, Mapping) or len(fields) > 256:
            raise ValueError('scout_diagnostic_fields_mapping_required')
        paths = set(fields) | {p for oid, p in indexed if oid == interface}
        for native_path in sorted(paths):
            candidate = fields.get(native_path, {})
            native_state = candidate.get('state', 'not_returned')
            for descriptor in indexed.get((interface, native_path), (None,)):
                canonical = descriptor['canonical_path'] if descriptor else ''
                evidence = dict(interface=interface, native_path=native_path,
                    canonical_path=canonical, native_state=native_state,
                    raw_unit=descriptor['raw_unit'] if descriptor else 'unknown',
                    canonical_unit=descriptor['canonical_unit'] if descriptor else 'unknown',
                    scale=descriptor['scale'] if descriptor else None,
                    rat=descriptor['rat'] if descriptor else 'unclassified',
                    comparison_state='not_compared')
                if descriptor is not None:
                    if 'conversion' in descriptor:
                        evidence['conversion'] = descriptor['conversion']
                    if 'component' in descriptor:
                        evidence['component'] = descriptor['component']
                if 'value' in candidate:
                    evidence['raw_value'] = candidate['value']
                if 'observed_at' in row:
                    evidence['native_observed_at'] = row['observed_at']
                if canonical and matrix_reader is not None:
                    line, field = canonical.split('.', 1)
                    cell = matrix_reader.cell(line, field)
                    evidence.update(comparison_state='compared', matrix_state=cell.state,
                        matrix_reason=cell.reason, matrix_sampled_at=cell.sampled_at,
                        matrix_acquired_at=cell.acquired_at, matrix_retained=cell.retention is not None)
                reason = (descriptor.get('qualification_reason', '') if descriptor
                          else rejected.get(interface, ''))
                if native_state != 'returned':
                    state = native_state
                    message = 'Empty native value' if native_state == 'empty' else 'Native value was not returned'
                elif reason:
                    state, message = 'returned_unqualified', 'Returned but unqualified: ' + reason
                elif not canonical:
                    state, message = 'returned_unmapped', 'Returned but unmapped; its unit and meaning require review'
                elif matrix_reader is None:
                    state, message = 'mapped_not_compared', 'Returned with a canonical mapping; not compared because no current Matrix reader was supplied'
                elif evidence['matrix_state'] == CELL_AVAILABLE:
                    state = 'mapped_available'
                    message = ('Returned; normal monitoring admits this field as a separate observation'
                               + (' (Matrix value is retained)' if evidence['matrix_retained'] else ''))
                else:
                    state = 'mapped_not_admitted'
                    message = 'Returned; Matrix does not currently admit this field: ' + evidence['matrix_reason']
                label = f'{interface}.{native_path}' + (f' → {canonical}' if canonical else '')
                result.append(item(label, state, message, **evidence))
        for identity in row.get('identity_presence', ()):
            result.append(item(f"{interface}.{identity['name']}", 'identity_presence',
                f"Identity {identity['state']}; value withheld", interface=interface,
                native_path=identity['name']))
        if row.get('truncated'):
            result.append(item(row['probe'], 'partial',
                'Native capture exceeded its bounded size; coverage is partial', interface=interface))
    completed = {row['probe'] for row in rows}
    for probe in probes:
        if probe.label not in completed:
            result.append(item(probe.label, 'not_tested',
                'Not tested; the run ended before this check', interface=probe.oid))
    return result

_FUNCTION_REQUIRED_MATRIX_PATHS: Mapping[str, tuple[str, ...]] = {
    "band_selection": ("radio.selected_band_set",),
    "band_lock": ("radio.selected_band_set",),
    "pband": ("radio.serving_band", "radio.selected_band_set"),
    "cell_lock": ("rf.pci", "rf.earfcn_dl"),
    "nr_band_lock": ("radio.nr_band",),
    "wifi_toggle": (
        "slow.wifi_24ghz_enabled",
        "slow.wifi_5ghz_enabled",
        "slow.wifi_6ghz_enabled",
    ),
}

log = logging.getLogger("arch4.modem_live.scout")

_SCOPE_FIELDS = (
    "identity_code",
    "identity_status",
    "profile_id",
    "access_type_id",
    "access_type",
    "backend",
    "backend_family",
    "vendor",
    "model",
    "hardware_sku",
    "hardware_version",
    "software_version",
    "firmware",
    "firmware_version",
    "webui",
    "provider_name",
    "provider_plmn",
)


def _json(value: Any) -> str:
    return canonical_json_text(value)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _matrix_path(path: str) -> tuple[str, str]:
    text = str(path or "").strip()
    if "." not in text:
        raise ValueError("modem_live_scout_translation_matrix_path_invalid")
    line, field = text.split(".", 1)
    if field not in MODEM_LIVE_TRUTH_FIELD_CATALOG.get(line, ()):
        raise ValueError("modem_live_scout_translation_matrix_path_unknown")
    return line, field


def _validate_proposed_canonical_value(line: str, field: str, value: Any) -> None:
    try:
        validate_truth_cell_value(line, field, value)
    except ModemLiveTruthMatrixError as exc:
        raise ValueError("modem_live_scout_translation_value_not_canonical") from exc


def build_unknown_alias_translation_proposal(
    *,
    evidence: Mapping[str, Any],
    raw_key: str,
    raw_value: Any,
    canonical_path: str,
    proposed_canonical_value: Any,
    evidence_sources: list[Mapping[str, Any]],
    confidence: float,
    notes: str = "",
) -> dict[str, Any]:
    """Build a Scout proposal for an unknown vendor phrase.

    This function only creates a proposal. It does not update aliases, Matrix,
    profile overlay, or live register. Runtime changes require review, a Modem
    Live translator entry, and tests.
    """

    line, field = _matrix_path(canonical_path)
    _validate_proposed_canonical_value(line, field, proposed_canonical_value)
    sources = [dict(item) for item in (evidence_sources or []) if isinstance(item, Mapping)]
    if not sources:
        raise ValueError("modem_live_scout_translation_sources_required")
    score = float(confidence)
    if not (0.0 <= score <= 1.0):
        raise ValueError("modem_live_scout_translation_confidence_invalid")
    evidence_payload = _mapping(evidence)
    return {
        "schema": UNKNOWN_ALIAS_TRANSLATION_PROPOSAL_SCHEMA,
        "owner": OWNER,
        "status": "proposed",
        "runtime_effect": "none",
        "raw": {
            "key": str(raw_key or ""),
            "value_type": type(raw_value).__name__,
            "value_repr": repr(raw_value)[:160],
        },
        "target": {
            "line": line,
            "field": field,
            "path": f"{line}.{field}",
            "proposed_canonical_value": proposed_canonical_value,
        },
        "evidence": {
            "unknown_alias_evidence": json.loads(_json(evidence_payload)),
            "sources": json.loads(_json(sources)),
            "confidence": score,
            "notes": str(notes or ""),
        },
        "required_before_runtime_truth": [
            "review_proposal",
            "add_dictionary_alias",
            "add_generated_alias_test",
            "add_matrix_rejection_test",
        ],
    }


def _empty_overlay(now: float) -> dict[str, Any]:
    return {
        "schema": OVERLAY_SCHEMA,
        "owner": OWNER,
        "sequence": 0,
        "updated_at": float(now),
        "profiles": {},
    }


def _validated_overlay(value: Any, *, now: float) -> dict[str, Any]:
    data = _mapping(value)
    if not data:
        return _empty_overlay(now)
    if data.get("schema") != OVERLAY_SCHEMA or data.get("owner") != OWNER:
        raise ValueError("modem_live_scout_overlay_owner_schema_invalid")
    profiles = _mapping(data.get("profiles"))
    clean_profiles: dict[str, Any] = {}
    for profile_id, raw_record in profiles.items():
        key = str(profile_id or "").strip()
        record = _mapping(raw_record)
        if not key or str(record.get("profile_id") or "") != key:
            raise ValueError("modem_live_scout_overlay_profile_id_invalid")
        fields = _mapping(record.get("fields"))
        illegal = sorted(set(fields) - ALLOWED_FIELDS)
        if illegal:
            raise ValueError("modem_live_scout_overlay_illegal_field:" + ",".join(illegal))
        clean_profiles[key] = {
            **record,
            "fields": {name: list(normalize_capability_bands(fields[name])) for name in fields},
        }
    return {
        "schema": OVERLAY_SCHEMA,
        "owner": OWNER,
        "sequence": int(data.get("sequence", 0) or 0),
        "updated_at": float(data.get("updated_at", now) or now),
        "profiles": clean_profiles,
    }


def _capability(value: Any) -> SessionBandCapability:
    if isinstance(value, SessionBandCapability):
        return value
    return SessionBandCapability.from_mapping(_mapping(value))


def _scope(identity: Mapping[str, Any], capability: SessionBandCapability) -> dict[str, Any]:
    source = _mapping(identity)
    out = {key: source[key] for key in _SCOPE_FIELDS if source.get(key) not in (None, "", [], {})}
    out["profile_id"] = str(out.get("profile_id") or capability.profile_id or "")
    session = str(capability.session_scope_key or "")
    if session:
        out["session_scope_hash"] = sha256(session.encode("utf-8")).hexdigest()[:24]
    return json.loads(_json(out))


class ModemLiveScout:
    """Bounded background worker for Modem Live evidence."""

    owner = OWNER

    def __init__(self, storage_dir: str | Path | None) -> None:
        self.storage_dir = (
            Path(storage_dir).expanduser().resolve()
            if storage_dir is not None
            else None
        )
        self.overlay_path = (
            self.storage_dir / OVERLAY_FILENAME
            if self.storage_dir is not None
            else None
        )
        self._journal = (
            JsonlEventJournal(
                self.storage_dir,
                JOURNAL_FILENAME,
                max_records=JOURNAL_MAX_RECORDS,
                max_bytes=JOURNAL_MAX_BYTES,
            )
            if self.storage_dir is not None
            else None
        )
        self.journal_path = (
            self._journal.path if self._journal is not None else None
        )
        self._lock = threading.RLock()
        self.load_error = ""
        self._overlay = self._load()
        self._last_difference_digest_by_profile: dict[str, str] = {}
        self._last_difference_decision: dict[str, Any] = {
            "owner": OWNER,
            "status": "not_observed",
            "runtime_effect": "none",
        }
        self._latest_observed_profile_evidence: dict[str, Any] = {
            "owner": OWNER,
            "status": "not_observed",
            "runtime_effect": "none",
        }
        self._work_changed = threading.Condition(self._lock)
        self._pending_work: dict[str, tuple[Any, ...]] = {}
        self._worker: threading.Thread | None = None
        self._stop_requested = False
        self._active_work = ""
        self._submitted_count = 0
        self._completed_count = 0
        self._coalesced_count = 0
        self._failure_count = 0
        self._last_worker_error = ""
        self._last_decision: dict[str, Any] = {
            "owner": OWNER,
            "status": "idle",
            "runtime_effect": "none",
        }

    def effective_profile(self, profile_id: str, seed_profile: Mapping[str, Any] | None) -> dict[str, Any]:
        profile = json.loads(_json(dict(seed_profile or {})))
        key = str(profile_id or profile.get("profile_id") or "").strip()
        if not key:
            return profile
        with self._lock:
            record = _mapping(_mapping(self._overlay.get("profiles")).get(key))
            fields = _mapping(record.get("fields"))
        for field_name in ALLOWED_FIELDS:
            if field_name in fields:
                profile[field_name] = list(normalize_capability_bands(fields[field_name]))
                rat = "lte" if field_name == "supported_lte_bands" else "nr"
                profile[f"{rat}_band_capability_status"] = BAND_CAPABILITY_DECLARED
        evidence_id = str(record.get("evidence_id") or "").strip()
        if evidence_id and any(field_name in fields for field_name in ALLOWED_FIELDS):
            profile["band_capability_reference"] = f"modem_live_scout:{evidence_id}"
        return profile

    def submit_session_capability(
        self,
        capability: SessionBandCapability | Mapping[str, Any],
        *,
        identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            cap = _capability(capability)
            source_identity = _mapping(identity)
            detached_identity = {
                key: source_identity[key]
                for key in _SCOPE_FIELDS
                if source_identity.get(key) not in (None, "", [], {})
            }
        except Exception as exc:
            return self._submission_failure(WORK_CAPABILITY_REPAIR, exc)
        return self._submit(
            WORK_CAPABILITY_REPAIR,
            (cap, detached_identity),
            profile_id=cap.profile_id,
            evidence_id=cap.capability_id,
        )

    def submit_profile_matrix_difference(
        self,
        *,
        profile_record: Mapping[str, Any],
        truth_matrix: Mapping[str, Any],
        identity: Mapping[str, Any] | None = None,
        observed_at: float | None = None,
    ) -> dict[str, Any]:
        try:
            detached_profile = dict(profile_record)
            if not isinstance(truth_matrix, Mapping):
                raise TypeError("modem_live_scout_truth_matrix_required")
            source_identity = _mapping(identity)
            detached_identity = {
                key: source_identity[key]
                for key in _SCOPE_FIELDS
                if source_identity.get(key) not in (None, "", [], {})
            }
            profile_id = str(detached_profile.get("profile_id") or "").strip()
            if not profile_id:
                raise ValueError("modem_live_scout_accepted_profile_record_required")
        except Exception as exc:
            return self._submission_failure(WORK_PROFILE_MATRIX_DIFFERENCE, exc)
        return self._submit(
            WORK_PROFILE_MATRIX_DIFFERENCE,
            (
                detached_profile,
                truth_matrix,
                detached_identity,
                float(observed_at or time.time()),
            ),
            profile_id=profile_id,
        )

    def _repair_from_session_capability(
        self,
        capability: SessionBandCapability | Mapping[str, Any],
        *,
        identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        cap = _capability(capability)
        profile_id = str(cap.profile_id or "").strip()
        if not profile_id:
            return self._decision("not_repaired", "profile_id_missing", cap)

        updates: dict[str, list[int]] = {}
        previous: dict[str, list[int]] = {}
        if cap.lte_status == "proven" and list(cap.supported_lte_bands) != list(
            cap.profile_seed_lte_bands
        ):
            updates["supported_lte_bands"] = list(cap.supported_lte_bands)
            previous["supported_lte_bands"] = list(cap.profile_seed_lte_bands)
        if cap.nr_status == "proven" and list(cap.supported_nr_bands) != list(
            cap.profile_seed_nr_bands
        ):
            updates["supported_nr_bands"] = list(cap.supported_nr_bands)
            previous["supported_nr_bands"] = list(cap.profile_seed_nr_bands)
        if not updates:
            return self._decision("not_repaired", "no_proven_capability_delta", cap)

        now = time.time()
        with self._lock:
            overlay = _validated_overlay(self._overlay, now=now)
            sequence = int(overlay["sequence"]) + 1
            profiles = _mapping(overlay.get("profiles"))
            record = _mapping(profiles.get(profile_id))
            fields = _mapping(record.get("fields"))
            fields.update({name: list(normalize_capability_bands(value)) for name, value in updates.items()})
            profiles[profile_id] = {
                "profile_id": profile_id,
                "updated_at": now,
                "evidence_id": cap.capability_id,
                "identity_scope": _scope(identity or {}, cap),
                "fields": {name: list(normalize_capability_bands(value)) for name, value in fields.items()},
            }
            overlay.update({"sequence": sequence, "updated_at": now, "profiles": profiles})
            event = {
                "schema": EVENT_SCHEMA,
                "owner": OWNER,
                "sequence": sequence,
                "ts": now,
                "profile_id": profile_id,
                "evidence_id": cap.capability_id,
                "previous_fields": previous,
                "new_fields": updates,
                "identity_scope": profiles[profile_id]["identity_scope"],
            }
        # The sole worker serializes persistence; live readers need only state.
        self._write(overlay)
        self._append(event)
        with self._lock:
            self._overlay = overlay
        return self._decision("repaired", "proven_modem_live_capability_delta", cap, sequence, updates)

    def status(self) -> dict[str, Any]:
        with self._lock:
            decision = dict(self._last_decision)
            return {
                **decision,
                "owner": OWNER,
                "overlay_sequence": int(self._overlay.get("sequence", 0) or 0),
                "profile_count": len(_mapping(self._overlay.get("profiles"))),
                "load_error": self.load_error,
                "evidence_mode": (
                    "durable" if self.storage_dir is not None else "in_memory"
                ),
                "profile_difference": dict(self._last_difference_decision),
                "latest_observed_profile_evidence": dict(
                    self._latest_observed_profile_evidence
                ),
                "worker_alive": bool(
                    self._worker is not None and self._worker.is_alive()
                ),
                "active_work": self._active_work,
                "pending_work": sorted(self._pending_work),
                "submitted_count": self._submitted_count,
                "completed_count": self._completed_count,
                "coalesced_count": self._coalesced_count,
                "failure_count": self._failure_count,
                "last_worker_error": self._last_worker_error,
            }

    def wait_idle(self, timeout_s: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._work_changed:
            while self._pending_work or self._active_work:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._work_changed.wait(remaining)
            return True

    def shutdown(self, timeout_s: float = 1.0) -> tuple[str, ...]:
        with self._work_changed:
            self._stop_requested = True
            self._pending_work.clear()
            worker = self._worker
            self._work_changed.notify_all()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, float(timeout_s)))
        return (
            (SCOUT_WORKER_NAME,)
            if worker is not None and worker.is_alive()
            else ()
        )

    def _observe_profile_matrix_difference(
        self,
        *,
        profile_record: Mapping[str, Any],
        truth_matrix: Mapping[str, Any],
        identity: Mapping[str, Any] | None = None,
        observed_at: float | None = None,
    ) -> dict[str, Any]:
        """Compare a proven Matrix generation with its selected static profile.

        This check may create evidence or a proposal. It does not change Matrix
        or the static profile, and it does not block live publication.
        """

        record = _mapping(profile_record)
        profile_id = str(record.get("profile_id") or "").strip()
        if not profile_id:
            raise ValueError("modem_live_scout_accepted_profile_record_required")
        reader = (
            truth_matrix
            if isinstance(truth_matrix, ValidatedTruthMatrixReader)
            else ValidatedTruthMatrixReader(truth_matrix)
        )
        differences: list[dict[str, Any]] = []
        pending_evidence: list[dict[str, Any]] = []
        required_field_warnings: list[dict[str, Any]] = []
        nr_mode = reader.cell("radio", "nr_mode")
        profile_dispositions = profile_matrix_dispositions(
            record, nr_mode=nr_mode.value if nr_mode.state == CELL_AVAILABLE else None,
        )
        action_support = _mapping(record.get("action_trust"))
        required_paths: dict[str, set[str]] = {}
        for action, paths in _FUNCTION_REQUIRED_MATRIX_PATHS.items():
            trust = str(action_support.get(action) or "untested")
            function_enabled = trust in {"supported", "degraded"}
            if not function_enabled:
                continue
            for path in paths:
                if profile_dispositions.get(path) in {
                    PROFILE_MATRIX_SUPPORTED,
                    PROFILE_MATRIX_SYNTHETIC,
                }:
                    required_paths.setdefault(path, set()).add(action)
        for address, profile_state in sorted(profile_dispositions.items()):
            if profile_state not in {
                PROFILE_MATRIX_SUPPORTED,
                PROFILE_MATRIX_SYNTHETIC,
                PROFILE_MATRIX_UNSUPPORTED,
            }:
                continue
            line, name = address.split(".", 1)
            cell = reader.cell(line, name)
            if (
                profile_state in {
                    PROFILE_MATRIX_SUPPORTED,
                    PROFILE_MATRIX_SYNTHETIC,
                }
                and cell.state == CELL_MISSING
            ):
                pending_evidence.append(
                    {
                        "path": address,
                        "profile_state": profile_state,
                        "live_state": cell.state,
                        "live_reason": cell.reason,
                    }
                )
            elif profile_state in {
                PROFILE_MATRIX_SUPPORTED,
                PROFILE_MATRIX_SYNTHETIC,
            } and cell.state in {CELL_INVALID, CELL_UNSUPPORTED}:
                differences.append(
                    {
                        "path": address,
                        "profile_state": profile_state,
                        "live_state": cell.state,
                        "live_reason": cell.reason,
                    }
                )
            elif (
                profile_state == PROFILE_MATRIX_UNSUPPORTED
                and cell.state == CELL_AVAILABLE
            ):
                differences.append(
                    {
                        "path": address,
                        "profile_state": "unsupported",
                        "live_state": cell.state,
                        "live_reason": cell.reason,
                    }
                )
            if len(differences) >= 64:
                break

        for path, actions in sorted(required_paths.items()):
            line, name = path.split(".", 1)
            cell = reader.cell(line, name)
            if cell.state != CELL_AVAILABLE:
                required_field_warnings.append(
                    {
                        "path": path,
                        "required_by": sorted(actions),
                        "live_state": cell.state,
                        "live_reason": cell.reason,
                        "acquisition_attempt_id": cell.acquisition_attempt_id,
                        "source_lane": cell.source_lane,
                    }
                )

        observed_identity: dict[str, Any] = {}
        for field in MODEM_LIVE_TRUTH_FIELD_CATALOG["identity"]:
            cell = reader.cell("identity", field)
            if cell.state != CELL_AVAILABLE:
                continue
            value = cell.value
            if type(value) in {bool, int, float}:
                observed_identity[field] = value
            elif type(value) is str and len(value) <= 512:
                observed_identity[field] = value
        diagnostic = reader.cell("slow", "backend_diagnostic")
        unknown_alias_evidence: Mapping[str, Any] = {}
        if diagnostic.state == CELL_AVAILABLE and isinstance(
            diagnostic.value, Mapping
        ):
            diagnostic_data = _mapping(diagnostic.value.get("data"))
            unknown_alias_evidence = _mapping(
                diagnostic_data.get("unknown_alias_evidence")
            )
        observed_profile_evidence = {
            "owner": OWNER,
            "status": "observed",
            "runtime_effect": "none",
            "profile_id": profile_id,
            "matrix_source_sequence": int(reader.metadata("sequence")),
            "matrix_sampled_at": float(reader.metadata("sampled_at")),
            "identity": json.loads(_json(observed_identity)),
            "required_paths": {
                path: sorted(actions)
                for path, actions in sorted(required_paths.items())
            },
            "required_field_warnings": json.loads(
                _json(required_field_warnings)
            ),
            "unknown_alias_evidence": json.loads(
                _json(unknown_alias_evidence)
            ),
        }

        def _normalized_text(value: Any) -> str:
            return " ".join(str(value or "").strip().casefold().split())

        def _compare_identity_selectors(
            *,
            address: tuple[str, str],
            selectors: Any,
            reason: str,
        ) -> None:
            if len(differences) >= 64:
                return
            expected = tuple(
                _normalized_text(item)
                for item in (selectors or ())
                if _normalized_text(item)
            )
            if not expected:
                return
            cell = reader.cell(*address)
            if cell.state != CELL_AVAILABLE:
                return
            actual = _normalized_text(cell.value)
            if actual and not any(item in actual for item in expected):
                differences.append(
                    {
                        "path": ".".join(address),
                        "profile_state": list(expected),
                        "live_state": actual,
                        "live_reason": reason,
                    }
                )

        _compare_identity_selectors(
            address=("identity", "model"),
            selectors=record.get("model_contains"),
            reason="model_selector_delta",
        )
        _compare_identity_selectors(
            address=("identity", "firmware"),
            selectors=record.get("firmware_contains"),
            reason="firmware_selector_delta",
        )
        _compare_identity_selectors(
            address=("identity", "provider_name"),
            selectors=record.get("provider_name_contains"),
            reason="provider_name_selector_delta",
        )
        _compare_identity_selectors(
            address=("identity", "provider_plmn"),
            selectors=record.get("provider_plmn_contains"),
            reason="provider_plmn_selector_delta",
        )
        expected_backend = _normalized_text(record.get("backend"))
        backend_cell = reader.cell("identity", "backend")
        if (
            len(differences) < 64
            and expected_backend
            and backend_cell.state == CELL_AVAILABLE
            and _normalized_text(backend_cell.value) != expected_backend
        ):
            differences.append(
                {
                    "path": "identity.backend",
                    "profile_state": expected_backend,
                    "live_state": _normalized_text(backend_cell.value),
                    "live_reason": "backend_delta",
                }
            )

        capability = reader.cell("capability", "session_band_capability")
        if capability.state == CELL_AVAILABLE and isinstance(capability.value, Mapping):
            live = _mapping(capability.value)
            for rat, expected in (
                ("lte", tuple(record.get("supported_lte_bands") or ())),
                ("nr", tuple(record.get("supported_nr_bands") or ())),
            ):
                status = str(live.get(f"{rat}_status") or "")
                actual = tuple(int(item) for item in live.get(f"supported_{rat}_bands", ()) or ())
                if status == "proven" and actual != tuple(expected):
                    differences.append(
                        {
                            "path": f"capability.supported_{rat}_bands",
                            "profile_state": list(expected),
                            "live_state": list(actual),
                            "live_reason": "proven_capability_delta",
                        }
                    )

        now = float(observed_at or time.time())
        scope = {
            key: value
            for key, value in _mapping(identity).items()
            if key in _SCOPE_FIELDS and value not in (None, "", [], {})
        }
        profile_record_checksum = sha256(
            _json(record).encode("utf-8")
        ).hexdigest()
        semantic_required_field_warnings = [
            {
                key: warning[key]
                for key in (
                    "path",
                    "required_by",
                    "live_state",
                    "live_reason",
                )
            }
            for warning in required_field_warnings
        ]
        digest = sha256(
            _json(
                {
                    "profile_id": profile_id,
                    "profile_record_checksum": profile_record_checksum,
                    "differences": differences,
                    "pending_evidence": pending_evidence,
                    "required_field_warnings": semantic_required_field_warnings,
                    "observed_identity": observed_profile_evidence["identity"],
                    "required_paths": observed_profile_evidence["required_paths"],
                    "unknown_alias_evidence": observed_profile_evidence[
                        "unknown_alias_evidence"
                    ],
                    "scope": scope,
                }
            ).encode("utf-8")
        ).hexdigest()
        decision = {
            "schema": PROFILE_DIFFERENCE_EVENT_SCHEMA,
            "owner": OWNER,
            "status": (
                "required_field_warning"
                if required_field_warnings
                else "difference_detected"
                if differences
                else "pending_evidence"
                if pending_evidence
                else "matched"
            ),
            "runtime_effect": "none",
            "profile_id": profile_id,
            "profile_record_checksum": profile_record_checksum,
            "observed_at": now,
            "matrix_source_sequence": int(reader.metadata("sequence")),
            "difference_digest": digest,
            "differences": differences,
            "pending_evidence": pending_evidence,
            "required_field_warnings": required_field_warnings,
            "observed_profile_evidence": observed_profile_evidence,
            "identity_scope": json.loads(_json(scope)),
            "required_action": (
                "modem_live_restore_required_matrix_fact"
                if required_field_warnings
                else "review_profile_evidence"
                if differences
                else "await_physical_evidence"
                if pending_evidence
                else "none"
            ),
        }
        with self._lock:
            duplicate = (
                self._last_difference_digest_by_profile.get(
                    profile_id
                )
                == digest
            )
        if (differences or required_field_warnings) and not duplicate:
            self._append(decision)
            log.warning(
                "Modem Live Scout profile evidence warning: "
                "profile=%s required=%s differences=%s",
                profile_id,
                len(required_field_warnings),
                len(differences),
            )
        with self._lock:
            self._last_difference_digest_by_profile[
                profile_id
            ] = digest
            self._last_difference_decision = {
                **decision,
                "deduplicated": duplicate,
            }
            self._latest_observed_profile_evidence = dict(
                observed_profile_evidence
            )
        return {**decision, "deduplicated": duplicate}

    def _submit(
        self,
        kind: str,
        payload: tuple[Any, ...],
        *,
        profile_id: str = "",
        evidence_id: str = "",
    ) -> dict[str, Any]:
        with self._work_changed:
            if self._stop_requested:
                return {
                    "owner": OWNER,
                    "status": "not_queued",
                    "runtime_effect": "none",
                    "reason": "scout_stopped",
                    "work_kind": kind,
                    "profile_id": str(profile_id or ""),
                    "evidence_id": str(evidence_id or ""),
                }
            if kind in self._pending_work:
                self._coalesced_count += 1
            self._pending_work[kind] = payload
            self._submitted_count += 1
            decision = {
                "owner": OWNER,
                "status": "queued",
                "runtime_effect": "none",
                "reason": "background_scout_work",
                "work_kind": kind,
                "profile_id": str(profile_id or ""),
                "evidence_id": str(evidence_id or ""),
            }
            self._last_decision = dict(decision)
            if not self._start_worker_locked():
                return dict(self._last_decision)
            self._work_changed.notify()
            return decision

    def _start_worker_locked(self) -> bool:
        if self._worker is not None and self._worker.is_alive():
            return True
        worker = threading.Thread(
            target=self._worker_loop,
            name=SCOUT_WORKER_NAME,
            daemon=True,
        )
        self._worker = worker
        try:
            worker.start()
            return True
        except Exception as exc:
            self._worker = None
            self._pending_work.clear()
            self._failure_count += 1
            self._last_worker_error = f"{type(exc).__name__}:{exc}"
            self._last_decision = {
                "owner": OWNER,
                "status": "worker_start_failed",
                "runtime_effect": "none",
                "reason": self._last_worker_error,
            }
            log.warning("Modem Live Scout worker start failed: %s", exc)
            return False

    def _worker_loop(self) -> None:
        current = threading.current_thread()
        while True:
            with self._work_changed:
                while not self._pending_work and not self._stop_requested:
                    self._work_changed.wait()
                if self._stop_requested:
                    self._active_work = ""
                    if self._worker is current:
                        self._worker = None
                    self._work_changed.notify_all()
                    return
                kind = (
                    WORK_CAPABILITY_REPAIR
                    if WORK_CAPABILITY_REPAIR in self._pending_work
                    else next(iter(self._pending_work))
                )
                payload = self._pending_work.pop(kind)
                self._active_work = kind
            try:
                if kind == WORK_CAPABILITY_REPAIR:
                    capability, identity = payload
                    decision = self._repair_from_session_capability(
                        capability,
                        identity=identity,
                    )
                elif kind == WORK_PROFILE_MATRIX_DIFFERENCE:
                    profile, matrix, identity, observed_at = payload
                    decision = self._observe_profile_matrix_difference(
                        profile_record=profile,
                        truth_matrix=matrix,
                        identity=identity,
                        observed_at=observed_at,
                    )
                else:
                    raise RuntimeError("modem_live_scout_work_kind_unknown")
            except Exception as exc:
                decision = {
                    "owner": OWNER,
                    "status": "work_failed",
                    "runtime_effect": "none",
                    "work_kind": kind,
                    "reason": f"{type(exc).__name__}:{exc}",
                }
                with self._lock:
                    self._failure_count += 1
                    self._last_worker_error = str(decision["reason"])
                log.warning("Modem Live Scout background work failed: %s", exc)
            with self._work_changed:
                self._completed_count += 1
                self._last_decision = dict(decision)
                self._active_work = ""
                self._work_changed.notify_all()

    def _submission_failure(self, kind: str, exc: BaseException) -> dict[str, Any]:
        decision = {
            "owner": OWNER,
            "status": "submission_failed",
            "runtime_effect": "none",
            "work_kind": kind,
            "reason": f"{type(exc).__name__}:{exc}",
        }
        with self._lock:
            self._failure_count += 1
            self._last_worker_error = str(decision["reason"])
            self._last_decision = dict(decision)
        log.warning("Modem Live Scout work submission failed: %s", exc)
        return decision

    def _decision(
        self,
        status: str,
        reason: str,
        capability: SessionBandCapability,
        sequence: int | None = None,
        updates: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "owner": OWNER,
            "status": status,
            "reason": reason,
            "profile_id": capability.profile_id,
            "evidence_id": capability.capability_id,
            "overlay_sequence": int(sequence if sequence is not None else self._overlay.get("sequence", 0) or 0),
            "repaired_fields": sorted((updates or {}).keys()),
            "load_error": self.load_error,
        }

    def _load(self) -> dict[str, Any]:
        now = time.time()
        if self.overlay_path is None or not self.overlay_path.exists():
            return _empty_overlay(now)
        try:
            return _validated_overlay(json.loads(self.overlay_path.read_text(encoding="utf-8")), now=now)
        except Exception as exc:
            self.load_error = f"{type(exc).__name__}:{exc}"
            try:
                quarantine = self.overlay_path.with_name(CORRUPT_OVERLAY_FILENAME)
                quarantine.unlink(missing_ok=True)
                self.overlay_path.replace(quarantine)
            except OSError as quarantine_exc:
                self.load_error += (
                    "|quarantine_failed:"
                    f"{type(quarantine_exc).__name__}:{quarantine_exc}"
                )
            return _empty_overlay(now)

    def _write(self, overlay: Mapping[str, Any]) -> None:
        if self.overlay_path is None:
            return
        atomic_write_json(
            self.overlay_path,
            _validated_overlay(overlay, now=time.time()),
            indent=2,
            sort_keys=True,
            trailing_newline=True,
        )

    def _append(self, event: Mapping[str, Any]) -> None:
        if self._journal is not None:
            self._journal.append(dict(event))


__all__ = (
    "ALLOWED_FIELDS",
    "CORRUPT_OVERLAY_FILENAME",
    "EVENT_SCHEMA",
    "JOURNAL_FILENAME",
    "JOURNAL_MAX_BYTES",
    "JOURNAL_MAX_RECORDS",
    "OVERLAY_FILENAME",
    "OVERLAY_SCHEMA",
    "OWNER",
    "PROFILE_DIFFERENCE_EVENT_SCHEMA",
    "SCOUT_WORKER_NAME",
    "UNKNOWN_ALIAS_TRANSLATION_PROPOSAL_SCHEMA",
    "ModemLiveScout",
    "build_unknown_alias_translation_proposal",
    "build_read_diagnostic_coverage",
)
