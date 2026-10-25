"""Confirmed-device History boundary. No modem access or independent store.

This planner changes routing keys, not measurements, dates or command IDs.
History applies the plan in one transaction before its consumers bind.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from arch4.new_horizon.hourly_aq import rekey_aq_hour, require_aq_hour, merge_aq_hours
from arch4.new_horizon.learning_context import learning_context_key


MIGRATION_RECEIPT = "confirmed_device_history_migration_v1"
MIGRATION_SCHEMA = "NewHorizonConfirmedDeviceHistoryMigration.v1"
_ROUTING_KEYS = frozenset({"scope_key", "continuity_key", "exact_scope_key",
                          "baseline_scope_key", "result_scope_key"})


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _identities(value: Any):
    if isinstance(value, Mapping):
        if {"pband", "ca_pool", "cell_id", "network_type", "scope_key"} <= value.keys():
            yield value
        for child in value.values():
            yield from _identities(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            yield from _identities(child)


def radio_key(identity: Mapping[str, Any], binding: Mapping[str, Any]) -> str:
    from arch4.new_horizon.continuity import learning_device_key
    from arch4.new_horizon.proof import radio_scope_key_from_values
    return radio_scope_key_from_values(
        identity_code=learning_device_key(binding),
        profile_id=binding["identity.profile_id"], provider_name=binding["identity.provider_name"],
        network_type=identity["network_type"], serving_band=identity["pband"],
        configured_band_pool=identity["ca_pool"], cell_id=identity["cell_id"],
        earfcn_dl=identity.get("earfcn"), pci=identity.get("pci"),
        nr_band=identity.get("nr_band"), nr_arfcn=identity.get("nr_arfcn"),
        nr_pci=identity.get("nr_pci"), nr_cell_id=identity.get("nr_cell_id"))


def _route(value: Any, scopes: Mapping[str, str]) -> Any:
    if isinstance(value, Mapping):
        result = {key: (scopes.get(item, item) if key in _ROUTING_KEYS and isinstance(item, str)
                        else _route(item, scopes)) for key, item in value.items()}
        # Older unqualified proof records deliberately have no learning
        # context. Preserve their diagnostic key; do not invent eligibility.
        if "learning_context_key" in result and isinstance(result.get("learning_context"), Mapping) and result["learning_context"]:
            result["learning_context_key"] = learning_context_key(result["learning_context"])
        return result
    if isinstance(value, (tuple, list)):
        return [_route(item, scopes) for item in value]
    return value


def plan_device_scope_migration(*, source: Mapping[str, Any], target: Mapping[str, Any],
                                records: tuple[dict[str, Any], ...], source_hours: Mapping[str, Any] | None,
                                target_hours: Mapping[str, Any] | None,
                                factual_checkpoints: tuple[Mapping[str, Any], ...] = ()) -> dict[str, Any]:
    from arch4.new_horizon.continuity import continuity_digest
    from arch4.new_horizon.proof import (AQ_HOURLY_CHECKPOINT_SCHEMA, READABLE_REFEREE_PROOF_KINDS,
        RefereeAssessmentHistoryOwner)
    from arch4.new_horizon.hourly_aq import make_aq_block
    old, new = continuity_digest(source), continuity_digest(target)
    if old == new or not target.get("history_device_key"):
        raise ValueError("history_device_migration_distinct_scopes_required")
    scopes = {old: new}
    for value in (*[r["payload"] for r in records], source_hours or {}, *factual_checkpoints):
        for identity in _identities(value):
            previous = radio_key(identity, source)
            if identity["scope_key"] == previous:
                scopes[previous] = radio_key(identity, target)
    moved = tuple({**row, "scope_key": new, "payload": _route(row["payload"], scopes)} for row in records)
    # The small raw AQ display ring overlaps the saved hourly summaries.
    # Recompute only the integrity signature whose routing fields changed.
    signatures: dict[str, tuple[str, str]] = {}
    snapshot = RefereeAssessmentHistoryOwner.snapshot_from_records(
        old, tuple(r for r in records if r["kind"] in READABLE_REFEREE_PROOF_KINDS))
    for assessment in snapshot.assessments:
        p = assessment.payloads[0]
        grade = assessment.proofs[0].grade
        aq = None if grade is None else grade / 3.0
        if p["subject_kind"] != "active_context_window" or aq is None:
            continue
        block = make_aq_block(assessment_id=assessment.assessment_id, continuity_key=old,
            identity=p["result_identity"], opened_at=p["opened_at"], ended_at=p["due_at"],
            aq=aq * 3.0, observed_vector=p["observed_vector"])
        signatures[assessment.assessment_id] = (_digest(block), _digest(_route(block, scopes)))
    hours: dict[str, dict[str, Any]] = {}
    excluded = {}
    for checkpoint, changing in ((target_hours, False), (source_hours, True)):
        if checkpoint is None:
            continue
        if checkpoint.get("schema") != AQ_HOURLY_CHECKPOINT_SCHEMA:
            raise ValueError("history_device_migration_hour_schema_invalid")
        for raw in checkpoint.get("hours", ()):
            hour = require_aq_hour(raw)
            if hour["continuity_key"] != (old if changing else new):
                raise ValueError("history_device_migration_hour_scope_invalid")
            if changing:
                if hour["identity"]["scope_key"] not in scopes:
                    raise ValueError("history_device_migration_hour_identity_unproven")
                hour = rekey_aq_hour(hour, continuity_key=new, scope_key=scopes[hour["identity"]["scope_key"]])
                for interval in hour["intervals"]:
                    pair = signatures.get(interval["assessment_id"])
                    if pair is not None:
                        if interval["signature"] != pair[0]:
                            raise ValueError("history_device_migration_source_signature_mismatch")
                        interval["signature"] = pair[1]
                hour["revision"] = _digest({k:v for k,v in hour.items() if k != "revision"})
                hour = require_aq_hour(hour)
            previous = hours.get(hour["hour_id"])
            hours[hour["hour_id"]] = merge_aq_hours(previous, hour) if previous else hour
        excluded.update(checkpoint.get("excluded", {}))
    return dict(records=moved, hours=dict(schema=AQ_HOURLY_CHECKPOINT_SCHEMA,
        hours=sorted(hours.values(), key=lambda h:(h["hour_start"],h["hour_id"])), excluded=excluded),
        source_scope=old, target_scope=new, scope_map=scopes)


def route_factual_checkpoint(value: Mapping[str, Any], scopes: Mapping[str, str]) -> dict[str, Any]:
    """Permanent adapter for approved retired factual summaries, not policies."""
    result = _route(value, scopes)
    targets = result.get("targets")
    if isinstance(targets, dict):
        result["targets"] = {
            key.rsplit("|", 1)[0] + "|" + scopes[key.rsplit("|", 1)[-1]]
            if "|" in key and key.rsplit("|", 1)[-1] in scopes else key: row
            for key, row in targets.items()}
    return result
