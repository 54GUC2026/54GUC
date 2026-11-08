"""Static broad-handler classification policy.

This module is an architecture audit catalog, not a runtime fault owner.  It
must never catch, log, resolve, retry, or suppress a fault.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from arch4.fault_supervisor import FaultBoundaryClass


class FaultContainmentMode(str, Enum):
    PROTECTED_HARDWARE = "protected_hardware_containment"
    PROCESS_TERMINAL = "process_terminal_evidence"
    STORAGE_DEGRADED = "storage_degraded_evidence"
    CODEC_REJECTION = "codec_rejection_or_explicit_default"
    GUI_UNAVAILABLE = "gui_explicit_unavailable"
    WORKER_TYPED = "typed_worker_containment"


@dataclass(frozen=True, slots=True)
class FaultBoundaryClassification:
    boundary: FaultBoundaryClass
    mode: FaultContainmentMode
    rationale: str


EXPECTED_BROAD_HANDLER_COUNT = 1233
# The normalized AST baseline is generated and enforced with the project test
# interpreter, CPython 3.13.  ``ast.dump`` follows the interpreter AST schema,
# so recalculating this value with a different Python minor is not a review.
EXPECTED_BROAD_HANDLER_FINGERPRINT = (
    "33073bae0da4836ab03d7fce8ca38e3254cfa53d71861b863c3ad4bef3ff3805"
)


_HARDWARE_PREFIXES = (
    "arch4/modem/",
    "arch4/modem_live/",
)
_PROCESS_FILES = {
    "arch4/runner.py",
    "arch4/windows_native_launcher.py",
    "arch4/portable_launcher_helpers.py",
    "arch4/portable_launch_validator.py",
    "arch4/portable_diagnostic_dump.py",
    "arch4/windows_bundle_validation.py",
    "arch4/preflight_diagnostics.py",
    "arch4/foundation/site_report_job.py",
    "arch4/historical_continuity_process.py",
}
_STORAGE_TOKENS = (
    "atomic_io",
    "checkpoint",
    "persistence",
    "portable_state",
    "historical_artifact",
    "historical_continuity",
    "external_memory",
    "user_config",
    "license_credential_store",
    "runtime_sidecar",
    "event_journal",
    "snapshot_store",
    "state_store",
    "transaction_register",
    "selected_action_register",
    "memory_calibration_store",
    "memory_store/",
    "memory.py",
    "proof_memory_store",
    "reward_ledger",
    "calibration_authority",
    "calibration_external_knowledge",
    "calibration_context_owner",
    "intelligence/evidence_recorder.py",
    "telemetry/rollup.py",
)
_CODEC_TOKENS = (
    "parser",
    "codec",
    "coerce",
    "contract",
    "interfaces.py",
    "status_payload.py",
    "display.py",
    "normalization.py",
    "runtime_identity.py",
    "runtime_band_scope.py",
    "app_info.py",
    "lte_mcs.py",
    "privacy_scrub.py",
)


def classify_broad_handler(
    relative_path: str,
    function_name: str,
) -> FaultBoundaryClassification:
    """Return the reviewed static class for one broad-handler site."""

    path = str(relative_path).replace("\\", "/")
    function = str(function_name or "<module>")
    if path.startswith(_HARDWARE_PREFIXES) or path == "arch4/startup/checklist.py":
        return FaultBoundaryClassification(
            FaultBoundaryClass.HARDWARE,
            FaultContainmentMode.PROTECTED_HARDWARE,
            "protected modem protocol, acquisition, cadence, or hardware read boundary",
        )
    if path == "arch4/ngui/runtime_service.py":
        return FaultBoundaryClassification(
            FaultBoundaryClass.PROCESS,
            FaultContainmentMode.PROCESS_TERMINAL,
            "parent Engine-process lifecycle boundary",
        )
    if path == "arch4/ngui/runtime_worker.py":
        return FaultBoundaryClassification(
            FaultBoundaryClass.WORKER,
            FaultContainmentMode.WORKER_TYPED,
            "child Engine worker and command-delivery boundary",
        )
    if path.startswith("arch4/ngui/"):
        return FaultBoundaryClassification(
            FaultBoundaryClass.GUI,
            FaultContainmentMode.GUI_UNAVAILABLE,
            "NGUI presentation, interaction, or external-work boundary",
        )
    if path in _PROCESS_FILES:
        return FaultBoundaryClassification(
            FaultBoundaryClass.PROCESS,
            FaultContainmentMode.PROCESS_TERMINAL,
            "launcher, runner, validation, or subprocess terminal boundary",
        )
    if any(token in path for token in _CODEC_TOKENS):
        return FaultBoundaryClassification(
            FaultBoundaryClass.CODEC,
            FaultContainmentMode.CODEC_REJECTION,
            "parser, coercion, normalization, immutable contract, or codec boundary",
        )
    if any(token in path for token in _STORAGE_TOKENS):
        return FaultBoundaryClassification(
            FaultBoundaryClass.STORAGE,
            FaultContainmentMode.STORAGE_DEGRADED,
            "atomic persistence, retained-state, journal, or artifact boundary",
        )
    return FaultBoundaryClassification(
        FaultBoundaryClass.WORKER,
        FaultContainmentMode.WORKER_TYPED,
        f"Controller/Brain/runtime worker boundary in {function}",
    )


__all__ = [
    "EXPECTED_BROAD_HANDLER_COUNT",
    "EXPECTED_BROAD_HANDLER_FINGERPRINT",
    "FaultBoundaryClassification",
    "FaultContainmentMode",
    "classify_broad_handler",
]
