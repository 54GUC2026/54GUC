"""The only Core entry for accepted configuration generations."""

from __future__ import annotations

from pathlib import Path
from time import time
from typing import Any, Callable, Mapping

from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.clients import CoreClientRole
from arch4.core_contracts.configuration import (
    CoreConfigDisposition,
    CoreConfigProposal,
    CoreConfigReceipt,
    CoreConfigSnapshot,
)
from arch4.core.config_generation import (
    CONFIG_GENERATION_SOURCE_OPERATOR,
    CONFIG_GENERATION_SOURCE_POLICY_OVERLAY,
    CONFIG_GENERATION_SOURCE_POLICY_TEMPLATE,
    CONFIG_GENERATION_SOURCE_PROFILE,
    CONFIG_GENERATION_SOURCE_RUNTIME,
    CONFIG_GENERATION_OWNER,
    CONFIG_GENERATION_RECEIPT_SCHEMA,
    _commit_config_generation,
    _restore_config_generation,
    _build_config_generation,
    _build_profile_config_generation,
    require_active_config_generation_authority,
)
from arch4.core.configuration_journal import CoreConfigurationJournal


OWNER = "54guc.core.active_configuration"


class CoreActiveConfigurationService:
    def __init__(
        self,
        engine: Any,
        *,
        persistence_dir: str | Path,
        evidence_dir: str | Path | None = None,
        refresh_core_consumers: Callable[..., Mapping[str, Any]],
    ) -> None:
        if engine is None:
            raise TypeError("core_active_configuration_engine_required")
        self._engine = engine
        if not callable(refresh_core_consumers):
            raise TypeError("core_configuration_refresh_port_required")
        self._refresh_core_consumers = refresh_core_consumers
        self._proposal_receipts: dict[str, tuple[str, CoreConfigReceipt]] = {}
        self._journal = CoreConfigurationJournal(
            persistence_dir,
            evidence_dir=evidence_dir,
        )

    def active_snapshot(self) -> CoreConfigSnapshot:
        active = require_active_config_generation_authority(self._engine).current
        receipt = dict(active.receipt or {})
        return CoreConfigSnapshot(
            generation_id=active.generation_id,
            config_fingerprint=active.config_fingerprint,
            values=CoreFrozenPayload.from_mapping(active.config.to_plain_dict()),
            metadata=CoreFrozenPayload.from_mapping(
                {
                    "profile_id": str(receipt.get("profile_id") or ""),
                    "profile_version": int(
                        receipt.get("profile_version", 0) or 0
                    ),
                    "active_config_overrides": dict(
                        receipt.get("active_config_overrides") or {}
                    ),
                    "profile_tuning_proposals": list(
                        receipt.get("profile_tuning_proposals") or []
                    ),
                    "runtime_policy_state": dict(
                        receipt.get("runtime_policy_state") or {}
                    ),
                }
            ),
            occurred_at=time(),
        )

    def submit_proposal(
        self,
        proposal: CoreConfigProposal,
    ) -> CoreConfigReceipt:
        """Validate and commit a customer config proposal."""

        if not isinstance(proposal, CoreConfigProposal):
            raise TypeError("core_configuration_typed_proposal_required")
        payload_digest = CoreFrozenPayload.from_mapping(
            proposal.to_payload()
        ).sha256
        prior = self._proposal_receipts.get(proposal.proposal_id)
        if prior is not None:
            prior_digest, prior_receipt = prior
            if prior_digest != payload_digest:
                return self._proposal_receipt(
                    proposal,
                    CoreConfigDisposition.REJECTED,
                    "core_configuration_proposal_id_conflict",
                )
            return prior_receipt
        rejection = self._proposal_rejection(proposal)
        if rejection:
            receipt = self._proposal_receipt(
                proposal,
                CoreConfigDisposition.REJECTED,
                rejection,
            )
            self._proposal_receipts[proposal.proposal_id] = (
                payload_digest,
                receipt,
            )
            return receipt
        try:
            generation = self._generation_from_proposal(proposal)
            committed = _commit_config_generation(
                self._engine,
                generation=generation,
                refresh_core_consumers=self._refresh_core_consumers,
                commit_journal=self._journal.commit,
            )
            receipt = CoreConfigReceipt(
                proposal_id=proposal.proposal_id,
                disposition=CoreConfigDisposition.ACCEPTED,
                active_generation_id=str(committed.get("generation_id") or ""),
                reason="core_configuration_committed",
                occurred_at=time(),
            )
        except Exception as exc:
            receipt = self._proposal_receipt(
                proposal,
                CoreConfigDisposition.REJECTED,
                f"core_configuration_commit_failed:{type(exc).__name__}:{exc}",
            )
        self._proposal_receipts[proposal.proposal_id] = (
            payload_digest,
            receipt,
        )
        return receipt

    def restore_committed(self) -> bool:
        return _restore_config_generation(
            self._engine,
            receipt=self._journal.load(),
            refresh_core_consumers=self._refresh_core_consumers,
            commit_journal=self._journal.commit,
        )

    def committed_generation_receipt(self) -> dict[str, Any]:
        """Return durable evidence for the active config generation."""

        return self._journal.load()

    def committed_generation_for_operational_request(
        self,
        request_id: str,
    ) -> dict[str, Any]:
        """Return durable config evidence for one operational request."""

        return self._journal.find_by_operational_request_id(request_id)

    def publish_current_run_evidence(self) -> bool:
        if self._journal.export_current_run_evidence():
            return True
        active = require_active_config_generation_authority(self._engine).current
        receipt = dict(active.receipt or {})
        if not receipt:
            receipt = {
                "schema": CONFIG_GENERATION_RECEIPT_SCHEMA,
                "owner": CONFIG_GENERATION_OWNER,
                "generation_id": active.generation_id,
                "parent_generation_id": "",
                "source_kind": "bootstrap",
                "proposal_id": "",
                "changed_keys": [],
                "effective_config_values": dict(
                    active.config.to_plain_dict()
                ),
                "active_config_overrides": {},
                "profile_tuning_proposals": [],
                "runtime_policy_state": {},
                "config_fingerprint": active.config_fingerprint,
                "committed": False,
                "transaction_state": "active_generation_evidence",
                "observed_ts": time(),
            }
        return self._journal.publish_evidence_receipt(receipt)

    def _proposal_rejection(self, proposal: CoreConfigProposal) -> str:
        active_id = self.active_snapshot().generation_id
        if proposal.parent_generation_id != active_id:
            return (
                "core_configuration_stale_parent:"
                f"declared={proposal.parent_generation_id}:active={active_id}"
            )
        changes = proposal.changes.to_mapping()
        source_kind = str(changes.get("source_kind") or "")
        allowed_roles = {
            CONFIG_GENERATION_SOURCE_PROFILE: {
                CoreClientRole.AUTONOMOUS_INTELLIGENCE,
            },
            CONFIG_GENERATION_SOURCE_RUNTIME: {
                CoreClientRole.AUTONOMOUS_INTELLIGENCE,
            },
            CONFIG_GENERATION_SOURCE_OPERATOR: {
                CoreClientRole.OPERATOR,
                CoreClientRole.OPERATOR_AUTOMATION,
            },
            CONFIG_GENERATION_SOURCE_POLICY_OVERLAY: {
                CoreClientRole.OPERATOR_AUTOMATION,
            },
            CONFIG_GENERATION_SOURCE_POLICY_TEMPLATE: {
                CoreClientRole.OPERATOR_AUTOMATION,
            },
        }
        if source_kind not in allowed_roles:
            return "core_configuration_source_kind_invalid"
        if proposal.client_role not in allowed_roles[source_kind]:
            return (
                "core_configuration_client_role_forbidden:"
                f"{proposal.client_role.value}:{source_kind}"
            )
        return ""

    def _generation_from_proposal(
        self,
        proposal: CoreConfigProposal,
    ) -> dict[str, Any]:
        changes = proposal.changes.to_mapping()
        evidence = proposal.evidence.to_mapping()
        parent = require_active_config_generation_authority(
            self._engine
        ).receipt()
        source_kind = str(changes.get("source_kind") or "")
        if source_kind == CONFIG_GENERATION_SOURCE_PROFILE:
            return _build_profile_config_generation(
                application_plan=dict(changes.get("application_plan") or {}),
                active_profile=dict(evidence.get("active_profile") or {}),
                parent_receipt=parent,
            )
        return _build_config_generation(
            source_kind=source_kind,
            set_config_values=dict(changes.get("set_config_values") or {}),
            restore_config_values=dict(
                changes.get("restore_config_values") or {}
            ),
            proposal=dict(evidence.get("proposal") or {}),
            evidence_scope=dict(evidence.get("evidence_scope") or {}),
            formula_vector=dict(evidence.get("formula_vector") or {}),
            rollback=dict(evidence.get("rollback") or {}),
            provenance=dict(evidence.get("provenance") or {}),
            profile_id=str(changes.get("profile_id") or ""),
            profile_version=int(changes.get("profile_version", 0) or 0),
            active_config_overrides=dict(
                changes.get("active_config_overrides") or {}
            ),
            profile_tuning_proposals=list(
                changes.get("profile_tuning_proposals") or []
            ),
            runtime_policy_state=dict(
                changes.get("runtime_policy_state") or {}
            ),
            parent_receipt=parent,
        )

    def _proposal_receipt(
        self,
        proposal: CoreConfigProposal,
        disposition: CoreConfigDisposition,
        reason: str,
    ) -> CoreConfigReceipt:
        return CoreConfigReceipt(
            proposal_id=proposal.proposal_id,
            disposition=disposition,
            active_generation_id=self.active_snapshot().generation_id,
            reason=reason,
            occurred_at=time(),
        )


__all__ = ("CoreActiveConfigurationService",)
