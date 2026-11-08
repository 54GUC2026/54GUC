"""Explicit policy-to-ConfigGeneration boundary.

Policy templates and file overlays produce values only.  This adapter is their
sole route into the immutable upper-controller config authority.
"""

from __future__ import annotations

from time import time
from typing import Any, Mapping

from arch4.core.config_generation import (
    CONFIG_GENERATION_SOURCE_POLICY_OVERLAY,
    CONFIG_GENERATION_SOURCE_POLICY_TEMPLATE,
    require_active_config_generation_authority,
    require_active_config_snapshot,
)
from arch4.core_contracts.clients import CoreClientRole
from arch4.core_contracts.configuration import (
    CoreConfigDisposition,
    CoreConfigProposal,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.policy.templates import build_policy_template_overrides


def _commit(
    engine: Any,
    *,
    source_kind: str,
    set_config_values: Mapping[str, Any],
    restore_config_values: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any],
    runtime_policy_state: Mapping[str, Any],
) -> dict[str, Any]:
    service = engine.controller.core.active_configuration
    parent_id = service.active_snapshot().generation_id
    changes = {
        "source_kind": source_kind,
        "set_config_values": dict(set_config_values),
        "restore_config_values": dict(restore_config_values or {}),
        "runtime_policy_state": dict(runtime_policy_state),
    }
    digest = CoreFrozenPayload.from_mapping(changes).sha256
    proposal = CoreConfigProposal(
        proposal_id=f"policy:{source_kind}:{parent_id}:{digest}",
        client_id="54guc.policy.configuration",
        client_role=CoreClientRole.OPERATOR_AUTOMATION,
        parent_generation_id=parent_id,
        changes=CoreFrozenPayload.from_mapping(changes),
        evidence=CoreFrozenPayload.from_mapping(
            {"provenance": dict(provenance)}
        ),
        created_at=time(),
    )
    receipt = service.submit_proposal(proposal)
    if receipt.disposition is not CoreConfigDisposition.ACCEPTED:
        raise RuntimeError(receipt.reason)
    return require_active_config_generation_authority(engine).receipt()


def publish_policy_template(engine: Any, profile: Any) -> dict[str, Any]:
    active_config = require_active_config_snapshot(engine)
    values, source = build_policy_template_overrides(active_config, profile)
    if not values:
        return {
            "applied": False,
            "values": {},
            "source": str(source or ""),
            "receipt": {},
        }
    receipt = _commit(
        engine,
        source_kind=CONFIG_GENERATION_SOURCE_POLICY_TEMPLATE,
        set_config_values=values,
        provenance={
            "application_owner": "arch4.policy.config_publication",
            "application_reason": "selected_policy_profile",
            "policy_profile": str(getattr(profile, "name", "") or ""),
            "source_path": str(source or ""),
        },
        runtime_policy_state={
            "policy_template": {
                "profile": str(getattr(profile, "name", "") or ""),
                "source": str(source or ""),
                "changed_keys": sorted(values),
            }
        },
    )
    return {
        "applied": True,
        "values": dict(values),
        "source": str(source or ""),
        "receipt": dict(receipt),
    }


def publish_policy_overlay_proposal(
    engine: Any,
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    require_active_config_snapshot(engine)
    row = dict(proposal or {})
    kind = str(row.get("kind", "") or "")
    if kind not in {"policy_overlay_applied", "policy_overlay_cleared"}:
        raise ValueError("policy_overlay_config_proposal_invalid")
    set_values = dict(row.get("set_config_values") or {})
    restore_values = dict(row.get("restore_config_values") or {})
    if not set_values and not restore_values:
        return {"committed": False, "receipt": {}}
    status = dict(row.get("status") or {})
    receipt = _commit(
        engine,
        source_kind=CONFIG_GENERATION_SOURCE_POLICY_OVERLAY,
        set_config_values=set_values,
        restore_config_values=restore_values,
        provenance={
            "application_owner": "arch4.policy.config_publication",
            "application_reason": kind,
            "overlay_id": str(status.get("overlay_id", "") or ""),
            "overlay_version": str(status.get("version", "") or ""),
            "overlay_digest": str(status.get("digest", "") or ""),
            "source_path": str(status.get("path", "") or ""),
        },
        runtime_policy_state={
            "policy_overlay": {
                "active": bool(status.get("active", False)),
                "overlay_id": str(status.get("overlay_id", "") or ""),
                "version": str(status.get("version", "") or ""),
                "digest": str(status.get("digest", "") or ""),
                "changed_keys": list(status.get("changed_keys", []) or []),
            }
        },
    )
    return {"committed": True, "receipt": receipt}


__all__ = ["publish_policy_overlay_proposal", "publish_policy_template"]
