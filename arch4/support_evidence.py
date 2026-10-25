"""Shared read-only diagnostic projections. Never accepted by live Core.

Stateless contracts used by History, Foundation and the offline checker.
Foundation remains the export owner; these functions import no active owner.
"""
from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

from arch4.architecture_contracts.runtime_generation import MatrixGenerationIdentity
from arch4.command_transactions import CommandTerminalReceipt, COMMAND_TERMINAL_RECEIPT_SCHEMA, CommandTerminalStatus
from arch4.core_contracts.accepted_current_facts import CoreAcceptedCurrentFacts, CoreAcceptedCurrentFact, ACCEPTED_CURRENT_FACTS_SCHEMA

OWNER = "arch4.foundation.support_bundle"
SNAPSHOT_SCHEMA = "SupportEvidenceSnapshot.v1"
FACTS_SCHEMA = "SupportAcceptedFacts.v1"
COMMAND_SCHEMA = "SupportCommandEvidence.v1"


def project_support_records(value: Any) -> Any:
    """Validate originals BEFORE redaction; remove live-contract promises once."""
    if isinstance(value, list):
        return [project_support_records(v) for v in value]
    if not isinstance(value, dict):
        return value
    facts = value.get("accepted_current_facts")
    if (isinstance(facts, dict) and facts.get("schema") == ACCEPTED_CURRENT_FACTS_SCHEMA
            and "matrix_identity" in value
            and MatrixGenerationIdentity.from_mapping(value["matrix_identity"])
                != MatrixGenerationIdentity.from_mapping(facts["matrix_identity"])):
        raise ValueError("support_source_facts_generation_mismatch")
    schema = value.get("schema")
    if schema == ACCEPTED_CURRENT_FACTS_SCHEMA:
        CoreAcceptedCurrentFacts.from_payload(value)
        value = {k: v for k, v in value.items() if k not in {"facts_digest", "denominator_digest"}}
        value = {**value, "schema": FACTS_SCHEMA, "owner": OWNER}
    elif schema == COMMAND_TERMINAL_RECEIPT_SCHEMA:
        CommandTerminalReceipt.from_mapping(value)
        value = {**{k: v for k, v in value.items() if k != "checksum"},
                 "schema": COMMAND_SCHEMA, "owner": OWNER}
    elif isinstance(schema, str) and schema in {FACTS_SCHEMA, COMMAND_SCHEMA, SNAPSHOT_SCHEMA}:
        raise ValueError("support_projection_cannot_be_reexported_as_runtime_source")
    elif isinstance(schema, str) and schema.startswith(("CoreAcceptedCurrentFacts.", "command_terminal_receipt.")):
        raise ValueError("support_source_contract_version_unsupported")
    return {k: project_support_records(v) for k, v in value.items()}


def read_support_facts(core: Mapping[str, Any]) -> tuple[MatrixGenerationIdentity, dict[str, CoreAcceptedCurrentFact]]:
    """Validate diagnostic structure, not a pre-redaction runtime digest."""
    payload = core["accepted_current_facts"]
    if (not isinstance(payload, dict) or payload.get("schema") != FACTS_SCHEMA
            or payload.get("owner") != OWNER
            or set(payload) != {"schema", "owner", "matrix_identity", "declared_paths", "facts"}):
        raise ValueError("support_facts_contract_invalid")
    identity = MatrixGenerationIdentity.from_mapping(payload["matrix_identity"])
    if identity != MatrixGenerationIdentity.from_mapping(core["matrix_identity"]):
        raise ValueError("support_facts_generation_mismatch")
    rows = payload["facts"]
    if not isinstance(rows, list) or len(rows) > 4096:
        raise ValueError("support_facts_rows_invalid")
    facts = [CoreAcceptedCurrentFact.from_payload(row) for row in rows]
    paths = [f.path for f in facts]
    if paths != payload["declared_paths"] or paths != sorted(set(paths)):
        raise ValueError("support_facts_denominator_invalid")
    if any(f.source_sequence > identity.matrix_sequence
           or (f.acquisition_namespace is not None and f.acquisition_namespace != identity.namespace)
           for f in facts):
        raise ValueError("support_fact_generation_mismatch")
    return identity, {f.path: f for f in facts}


def read_support_command(value: Mapping[str, Any]) -> dict[str, Any]:
    """Recorded outcome only: no transaction construction or command admission."""
    if (value.get("schema") != COMMAND_SCHEMA or value.get("owner") != OWNER
            or "checksum" in value):
        raise ValueError("support_command_contract_invalid")
    identity = MatrixGenerationIdentity.from_mapping(value["matrix_identity"])
    stamp = value["terminal_ts"]
    if (type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0
            or not isinstance(value["terminal_status"], str)
            or value["terminal_status"] not in {v.value for v in CommandTerminalStatus}
            or not isinstance(value["reason"], str)
            or not isinstance(value["command_id"], str)
            or type(value.get("receipt_sequence")) is not int or value["receipt_sequence"] <= 0
            or not isinstance(value.get("transaction_root_id"), str) or not value["transaction_root_id"]
            or not isinstance(value.get("command"), str)
            or not isinstance(value["receipts"], dict)):
        raise ValueError("support_command_outcome_invalid")
    return dict(value, matrix_identity=identity.as_dict())


def validate_support_records(value: Any) -> None:
    """Read-only exported-record validation, including copies inside PageTruth."""
    if isinstance(value, list):
        for item in value:
            validate_support_records(item)
    elif isinstance(value, dict):
        facts = value.get("accepted_current_facts")
        if isinstance(facts, dict) and facts.get("schema") == FACTS_SCHEMA:
            read_support_facts(value)
            for key, item in value.items():
                if key != "accepted_current_facts":
                    validate_support_records(item)
            return
        if value.get("schema") == FACTS_SCHEMA:
            read_support_facts({"accepted_current_facts": value, "matrix_identity": value["matrix_identity"]})
        elif value.get("schema") == COMMAND_SCHEMA:
            read_support_command(value)
        elif isinstance(value.get("schema"), str) and value["schema"] in {ACCEPTED_CURRENT_FACTS_SCHEMA, COMMAND_TERMINAL_RECEIPT_SCHEMA}:
            raise ValueError("runtime_contract_forbidden_in_support_export")
        elif isinstance(value.get("schema"), str) and value["schema"].startswith(
                ("SupportAcceptedFacts.", "SupportCommandEvidence.")):
            raise ValueError("support_record_version_unsupported")
        for item in value.values():
            validate_support_records(item)
