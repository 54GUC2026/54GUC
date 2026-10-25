"""Mandatory disposition rules for every active CGF production module.

The exact source-set digest is checked by the Wave-0 audit.  Adding, deleting or
moving a production module therefore requires an explicit manifest update; a
new file cannot silently enter the runtime as an unclassified owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


PRODUCTION_SOURCE_ROOTS: tuple[str, ...] = (
    "arch4/core",
    "arch4/foundation",
    "arch4/modem_live",
    "arch4/new_horizon",
    "arch4/ngui",
)
PRODUCTION_SOURCE_COUNT = 250
PRODUCTION_SOURCE_SET_SHA256 = (
    '33be7f5eae64cb2a81d89c5f9d2f152015b8fcb4f2e70aa5ebe91ef683e3bc35'
)


@dataclass(frozen=True, slots=True)
class ProductionFileDisposition:
    path: str
    subsystem: str
    disposition: str
    canonical_owner: str
    boundary: str


_BOUNDARY_FILES = frozenset(
    {
        "arch4/core/composition.py",
        "arch4/core/command_safety_adapter.py",
        "arch4/core/lever_action_adapter.py",
        "arch4/core/observation_adapter.py",
        "arch4/foundation/artifact_io.py",
        "arch4/foundation/billing_plan_adoption.py",
        "arch4/foundation/composition.py",
        "arch4/foundation/diagnostic_payload.py",
        "arch4/foundation/support_archive.py",
        "arch4/foundation/read_diagnostic_report.py",
        "arch4/foundation/live_artifact_reader.py",
        "arch4/foundation/ngui_extension.py",
        "arch4/foundation/operator_data_budget_reader.py",
        "arch4/foundation/service_job_fabric.py",
        "arch4/modem_live/access_contract.py",
        "arch4/modem_live/access_profile_owner.py",
        "arch4/modem_live/bootstrap.py",
        "arch4/modem_live/compatibility_identity.py",
        "arch4/modem_live/io_gate.py",
        "arch4/modem_live/nsa_snapshot.py",
        "arch4/modem_live/radio_mode_configuration.py",
        "arch4/modem_live/reviewed_compatibility_registry.py",
        "arch4/modem_live/rtt_register.py",
        "arch4/modem_live/service_confirmation.py",
        "arch4/modem_live/snapshots.py",
        "arch4/new_horizon/history_identity_migration.py",
        "arch4/new_horizon/history_presentation.py",
        "arch4/new_horizon/support_targets.py",
        "arch4/new_horizon/site_report_projection.py",
        "arch4/new_horizon/service_evidence.py",
        "arch4/ngui/application.py",
        "arch4/ngui/millisecond_text.py",
        "arch4/ngui/display_text.py",
        "arch4/ngui/runtime_frame_transport.py",
        "arch4/ngui/runtime_ipc.py",
        "arch4/ngui/runtime_lifecycle_transport.py",
        "arch4/ngui/runtime_projection.py",
        "arch4/ngui/runtime_service.py",
        "arch4/ngui/runtime_worker.py",
    }
)

_CUSTOMER_FILES = frozenset(
    {
        "arch4/modem_live/capability_report.py",
        "arch4/ngui/demo.py",
        "arch4/ngui/modem_live_matrix_view.py",
    }
)

_DORMANT_HOOK_FILES = frozenset(
    {
        "arch4/core/extension_hooks.py",
    }
)


def classify_production_file(path: str) -> ProductionFileDisposition:
    """Return the single reviewed disposition for one exact CGF source path."""

    normalized = str(PurePosixPath(path))
    matching_roots = tuple(
        root for root in PRODUCTION_SOURCE_ROOTS if normalized.startswith(f"{root}/")
    )
    if len(matching_roots) != 1 or not normalized.endswith(".py"):
        raise ValueError(f"not one active CGF production Python source: {path}")
    subsystem = matching_roots[0].rsplit("/", 1)[-1]
    canonical_owner = f"54guc.{subsystem}"
    name = PurePosixPath(normalized).name

    if normalized in _DORMANT_HOOK_FILES:
        disposition = "dormant_hook"
        boundary = "typed detached customer hook; cannot write active CGF truth"
    elif (
        normalized in _BOUNDARY_FILES
        or name in {"__init__.py", "__main__.py"}
        or "/adapters/" in normalized
        or "/renderer/" in normalized
    ):
        disposition = "boundary_adapter"
        boundary = "typed subsystem boundary; no independent truth ownership"
    elif normalized in _CUSTOMER_FILES:
        disposition = "customer"
        boundary = "read-only customer of the declared canonical owner"
    else:
        disposition = "owner"
        boundary = "subsystem-owned state, service, policy, or pure contract"

    return ProductionFileDisposition(
        path=normalized,
        subsystem=subsystem,
        disposition=disposition,
        canonical_owner=canonical_owner,
        boundary=boundary,
    )


__all__ = (
    "PRODUCTION_SOURCE_COUNT",
    "PRODUCTION_SOURCE_ROOTS",
    "PRODUCTION_SOURCE_SET_SHA256",
    "ProductionFileDisposition",
    "classify_production_file",
)
