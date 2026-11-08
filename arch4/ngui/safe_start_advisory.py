"""Read-only startup check of NGUI PageTruth after connection."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from arch4.core_contracts.connected_settlement import ConnectedSettlement, CONNECTED_SETTLE_S

from .front_register import (
    FrontRegisterSnapshot,
    PresentationState,
    PresentationValue,
    accepted_modem_source_generation,
)
from .registry import fields_for_page
from .types import FreshnessState, NGUI_VISIBLE_PAGES, PageId


SAFE_START_SCHEMA = "NGUISafeStartAdvisory.v1"
SAFE_START_OWNER = "arch4.ngui.safe_start_advisory"
SAFE_START_FILENAME = "ngui_safe_start_advisory.json"
OPERATOR_PAGES = NGUI_VISIBLE_PAGES
SHARED_SURFACES = (PageId.HEADER, PageId.FOOTER)
ISSUE_CLASSES = frozenset({"missing", "invalid", "stale", "contradictory"})
_SAFE_START_IDLE_MANUAL_FIELDS = frozenset(
    {
        "manual.action_sequence",
        "manual.core_received_ms",
        "manual.modem_work_started_ms",
        "manual.pband_command_sent_ms",
        "manual.ca_pool_command_sent_ms",
        "manual.target_first_verified_ms",
        "manual.next_chart_sample_ms",
        "manual.target_sample_sequence",
        "manual.chart_sample_sequence",
    }
)
_SAFE_START_EMPTY_REASON_FIELDS = frozenset(
    {
        "quality.rtt_failure_reason",
        "traffic.invalid_reason",
    }
)
_SAFE_START_OPTIONAL_IDLE_FIELDS = frozenset(
    {
        "command.manual_reason",
        "event.connectivity_transition",
        "event.connectivity_conclusion",
    }
)
_SAFE_START_INACTIVE_COMMAND_FIELDS = frozenset(
    {
        "command.status",
        "command.band_sequence_status",
        "command.band_schedule_status",
        "command.command_protection_active",
        "command.command_protection_reason",
        "command.manual_transaction",
    }
)
_SAFE_START_UNSUPPORTED_COMMAND_FIELDS = {
    "command.cell_locked": "capability.cell_lock",
}


class NGUISafeStartAdvisor:
    """Classify PageTruth after connection without changing or republishing it."""

    def __init__(self, *, connected_settle_s: float = CONNECTED_SETTLE_S) -> None:
        self._settlement = ConnectedSettlement(settle_s=connected_settle_s)

    def evaluate(self, snapshot: FrontRegisterSnapshot) -> dict[str, Any]:
        if not isinstance(snapshot, FrontRegisterSnapshot):
            raise TypeError("safe-start advisory requires FrontRegisterSnapshot")
        connected = _connected(snapshot.fields.get("connection.user_link"))
        current_source_sequence = accepted_modem_source_generation(snapshot)
        settlement = self._settlement.evaluate(connected=connected,
            source_at=snapshot.accepted_modem_sampled_at,
            source_sequence=current_source_sequence,
            namespace=(snapshot.runtime_session_scope_key, snapshot.runtime_source_epoch))
        connected_elapsed_s = settlement["connected_elapsed_s"]
        source_advanced = settlement["source_advanced_after_connect"]
        stable = settlement["stable_connected"]

        pages: dict[str, Any] = {}
        issue_total = 0
        for page in OPERATOR_PAGES:
            page_report = _audit_page(snapshot, page)
            pages[page.value] = page_report
            issue_total += int(page_report["issue_count"])
        shared_surfaces: dict[str, Any] = {}
        for page in SHARED_SURFACES:
            page_report = _audit_page(snapshot, page)
            shared_surfaces[page.value] = page_report
            issue_total += int(page_report["issue_count"])
        if not connected:
            status = "awaiting_connected"
        elif not stable:
            status = "connected_settling"
        elif issue_total:
            status = "advisories_present"
        else:
            status = "ready"
        return {
            "schema": SAFE_START_SCHEMA,
            "owner": SAFE_START_OWNER,
            "advisory_only": True,
            "runtime_influence": "none",
            "status": status,
            "connected": connected,
            "stable_connected": stable,
            "connected_elapsed_s": round(connected_elapsed_s, 3),
            "connected_settle_s": self._settlement.settle_s,
            "source_advanced_after_connect": source_advanced,
            "front_sequence": int(snapshot.sequence),
            "input_generation": int(snapshot.input_generation),
            "runtime_source_sequence": current_source_sequence,
            "published_at": float(snapshot.published_at),
            "operator_page_count": len(OPERATOR_PAGES),
            "shared_surface_count": len(SHARED_SURFACES),
            "issue_count": issue_total,
            "pages": pages,
            "shared_surfaces": shared_surfaces,
        }


def _connected(field: PresentationValue | None) -> bool:
    if field is None or field.state not in {
        PresentationState.VALID,
        PresentationState.HELD,
    }:
        return False
    return str(field.value or field.text or "").strip().upper() == "CONNECTED"


def _audit_page(snapshot: FrontRegisterSnapshot, page: PageId) -> dict[str, Any]:
    truth = snapshot.pages.get(page)
    specs = tuple(fields_for_page(page))
    expected = tuple(spec.field_id for spec in specs)
    spec_by_id = {spec.field_id: spec for spec in specs}
    actual = dict(truth.fields) if truth is not None else {}
    rows: dict[str, Any] = {}
    counts: Counter[str] = Counter()
    issues: list[dict[str, str]] = []
    for field_id in expected:
        field = actual.get(field_id)
        canonical = snapshot.fields.get(field_id)
        classification = _classify_field(snapshot, field_id, field)
        reason = str(getattr(field, "reason", "") or "")
        spec = spec_by_id[field_id]
        if (
            spec.readiness_required
            and field is not None
            and (
                not isinstance(field.value, Mapping)
                or field.value.get("ready") is not True
            )
        ):
            classification = "invalid"
            reason = "required_readiness_contract_not_ready"
        if field is not None and canonical is not None and field != canonical:
            classification = "contradictory"
            reason = "page_value_disagrees_with_front_register"
        counts[classification] += 1
        if classification in ISSUE_CLASSES and (
            spec.display_in_grid or spec.readiness_required
        ):
            issues.append(
                {
                    "field_id": field_id,
                    "classification": classification,
                    "reason": reason or classification,
                }
            )
        rows[field_id] = _field_advisory(field, classification, reason)
    undeclared = sorted(set(actual) - set(expected))
    for field_id in undeclared:
        counts["contradictory"] += 1
        issues.append(
            {
                "field_id": field_id,
                "classification": "contradictory",
                "reason": "undeclared_page_field",
            }
        )
    return {
        "page": page.value,
        "field_count": len(expected),
        "counts": dict(sorted(counts.items())),
        "issue_count": len(issues),
        "issues": issues,
        "undeclared_fields": undeclared,
        "fields": rows,
    }


def _classify(field: PresentationValue | None) -> str:
    if field is None:
        return "missing"
    if field.state is PresentationState.UNSUPPORTED:
        return "unsupported"
    if field.state is PresentationState.NOT_APPLICABLE:
        return "not_applicable"
    if field.state is PresentationState.PENDING:
        return "pending"
    if field.state is PresentationState.INVALID:
        return "invalid"
    if field.state is PresentationState.UNATTACHED:
        return "intelligence_unattached"
    if field.state is PresentationState.MISSING:
        return "missing"
    if field.freshness is FreshnessState.STALE:
        return "stale"
    if field.state is PresentationState.HELD:
        return "held"
    if field.state is PresentationState.TRANSITION:
        return "transition"
    return "valid"


def _classify_field(
    snapshot: FrontRegisterSnapshot,
    field_id: str,
    field: PresentationValue | None,
) -> str:
    classification = _classify(field)
    if classification != "missing" or field is None:
        return classification
    if field_id in _SAFE_START_EMPTY_REASON_FIELDS:
        return "empty"
    if field_id in _SAFE_START_OPTIONAL_IDLE_FIELDS and (
        not field_id.startswith("event.connectivity_")
        or _connectivity_transition_is_idle(snapshot)
    ):
        return "inactive"
    if field_id in _SAFE_START_IDLE_MANUAL_FIELDS and _manual_action_is_idle(
        snapshot,
    ):
        return "inactive"
    if field_id == "radio.neighbors" and _neighbors_are_not_current(snapshot, field):
        return "empty"
    if field_id in _SAFE_START_INACTIVE_COMMAND_FIELDS and _command_is_idle(
        snapshot,
        field,
    ):
        return "inactive"
    if _command_field_is_unsupported(snapshot, field_id):
        return "unsupported"
    if field_id == "command.sms_message" and _sms_runtime_is_inactive(snapshot):
        return "inactive"
    if field_id == "antenna_guide.active_action" and _antenna_guide_is_stopped(snapshot):
        return "inactive"
    if field_id == "command.ping_host" and _downstream_ping_is_inactive(
        snapshot,
    ):
        return "inactive"
    return classification


def _connectivity_transition_is_idle(snapshot: FrontRegisterSnapshot) -> bool:
    field = snapshot.fields.get("event.connectivity_transition")
    return field is None or field.state in {
        PresentationState.MISSING,
        PresentationState.NOT_APPLICABLE,
    }


def _manual_action_is_idle(snapshot: FrontRegisterSnapshot) -> bool:
    pending = snapshot.fields.get("manual.action_pending")
    status = snapshot.fields.get("manual.status")
    if pending is None or pending.state not in {
        PresentationState.VALID,
        PresentationState.HELD,
    }:
        return False
    if bool(pending.value):
        return False
    text = str(
        (status.value if status is not None else "")
        or (status.text if status is not None else "")
        or ""
    ).strip().lower()
    return text in {
        "",
        "ready",
        "idle",
        "stream_settled",
        "completed",
        "settled",
    }


def _command_is_idle(
    snapshot: FrontRegisterSnapshot,
    field: PresentationValue,
) -> bool:
    text = str(field.text or field.value or "").strip().lower()
    if "idle" in text:
        return True
    return _manual_action_is_idle(snapshot)


def _command_field_is_unsupported(snapshot: FrontRegisterSnapshot, field_id: str) -> bool:
    capability_id = _SAFE_START_UNSUPPORTED_COMMAND_FIELDS.get(field_id)
    if not capability_id:
        return False
    capability = snapshot.fields.get(capability_id)
    if capability is None:
        return False
    if capability.state is PresentationState.UNSUPPORTED:
        return True
    text = str(capability.text or capability.value or "").strip().upper()
    return text in {"NO", "OFF", "UNSUPPORTED", "N/A", "NA", "---"}


def _downstream_ping_is_inactive(snapshot: FrontRegisterSnapshot) -> bool:
    enabled = snapshot.fields.get("command.ping_enabled")
    if enabled is None or enabled.state not in {
        PresentationState.VALID,
        PresentationState.HELD,
    }:
        return True
    return bool(enabled.value) is not True


def _sms_runtime_is_inactive(snapshot: FrontRegisterSnapshot) -> bool:
    enabled = snapshot.fields.get("command.sms_enabled")
    if enabled is None or enabled.state not in {
        PresentationState.VALID,
        PresentationState.HELD,
    }:
        return True
    return bool(enabled.value) is not True


def _antenna_guide_is_stopped(snapshot: FrontRegisterSnapshot) -> bool:
    status = snapshot.fields.get("antenna_guide.status")
    if status is None or status.state not in {
        PresentationState.VALID,
        PresentationState.HELD,
    }:
        return False
    text = str(status.value or status.text or "").strip().lower()
    return text in {"", "stopped", "idle", "ready"}


def _neighbors_are_not_current(
    snapshot: FrontRegisterSnapshot,
    field: PresentationValue,
) -> bool:
    support = snapshot.fields.get("capability.neighbors")
    if support is not None:
        if support.state is PresentationState.UNSUPPORTED:
            return True
        support_text = str(support.text or support.value or "").strip().upper()
        if support_text in {"NO", "OFF", "UNSUPPORTED", "N/A", "NA", "---"}:
            return True
    text = str(field.text or field.value or "").strip()
    reason = str(field.reason or "").strip()
    return text in {"", "---"} or reason in {
        "source_missing",
        "neighbor_sliding_hold_expired",
        "ngui_neighbor_hold_expired",
    }


def _field_advisory(
    field: PresentationValue | None,
    classification: str,
    reason: str,
) -> dict[str, Any]:
    if field is None:
        return {
            "classification": classification,
            "state": "missing",
            "freshness": "unknown",
            "owner": "",
            "source_path": "",
            "source_sequence": 0,
            "reason": reason or "page_field_missing",
        }
    return {
        "classification": classification,
        "state": field.state.value,
        "freshness": field.freshness.value,
        "owner": field.owner.value,
        "source_path": field.source_path,
        "source_sequence": int(field.source_sequence),
        "reason": reason,
    }


__all__ = [
    "NGUISafeStartAdvisor",
    "OPERATOR_PAGES",
    "SHARED_SURFACES",
    "SAFE_START_FILENAME",
    "SAFE_START_OWNER",
    "SAFE_START_SCHEMA",
]
