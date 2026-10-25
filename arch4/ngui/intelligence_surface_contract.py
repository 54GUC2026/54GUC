"""Declare the read-only New Horizon surface without a GUI toolkit."""

from __future__ import annotations

from dataclasses import dataclass

from arch4.intelligence_presentation_contract import (
    INTELLIGENCE_NGUI_EXTENSION_HOOK_ID,
    INTELLIGENCE_NGUI_EXTENSION_SCHEMA,
    INTELLIGENCE_NGUI_SOURCE_IDS,
)
from arch4.core_contracts.intelligence_boundary import (
    INTELLIGENCE_MATRIX_OWNER,
    INTELLIGENCE_MATRIX_OUTPUT_SCHEMA,
)


INTELLIGENCE_SURFACE_CONTRACT_VERSION = "NewHorizonIntelligenceSurface.v9"
INTELLIGENCE_EXTENSION_STATE = "ATTACHED_READ_ONLY"


@dataclass(frozen=True, slots=True)
class IntelligenceFieldRequirement:
    field_id: str
    source_contract: str = INTELLIGENCE_NGUI_EXTENSION_SCHEMA


@dataclass(frozen=True, slots=True)
class IntelligencePageRequirement:
    page_id: str


@dataclass(frozen=True, slots=True)
class IntelligenceControlRequirement:
    object_name: str
    destination_owner: str = "54guc.ngui.presentation"
    page_id: str = "intelligence"
    mutates_intelligence: bool = False
    command_capable: bool = False
    changes_control_policy: bool = False


@dataclass(frozen=True, slots=True)
class IntelligenceSurfaceContract:
    version: str
    field_requirements: tuple[IntelligenceFieldRequirement, ...]
    control_requirements: tuple[IntelligenceControlRequirement, ...]
    page_requirements: tuple[IntelligencePageRequirement, ...]
    matrix_input_contract: str
    terminal_evidence_contract: str


@dataclass(frozen=True, slots=True)
class ReadOnlyIntelligencePresentationBoundary:
    presentation_id: str = INTELLIGENCE_NGUI_EXTENSION_HOOK_ID
    contract_version: str = INTELLIGENCE_NGUI_EXTENSION_SCHEMA
    state: str = INTELLIGENCE_EXTENSION_STATE


INTELLIGENCE_SURFACE_CONTRACT = IntelligenceSurfaceContract(
    version=INTELLIGENCE_SURFACE_CONTRACT_VERSION,
    field_requirements=tuple(
        IntelligenceFieldRequirement(field_id)
        for field_id in sorted(INTELLIGENCE_NGUI_SOURCE_IDS)
        if field_id != "intelligence.recovery_status"  # Input to Recent Events, not another Brain widget.
    ),
    control_requirements=(
        IntelligenceControlRequirement("nguiIntelligenceCheckLiveHistory"),
        IntelligenceControlRequirement("nguiIntelligenceCheckImportStatus"),
        IntelligenceControlRequirement(
            "nguiDeveloperAutopilotOn",
            destination_owner=INTELLIGENCE_MATRIX_OWNER,
            page_id="developer_control",
            changes_control_policy=True,
        ),
        IntelligenceControlRequirement(
            "nguiDeveloperAutopilotOff",
            destination_owner=INTELLIGENCE_MATRIX_OWNER,
            page_id="developer_control",
            changes_control_policy=True,
        ),
        IntelligenceControlRequirement(
            "nguiDeveloperExploreOn",
            destination_owner=INTELLIGENCE_MATRIX_OWNER,
            page_id="developer_control",
            changes_control_policy=True,
        ),
        IntelligenceControlRequirement(
            "nguiDeveloperExploreOff",
            destination_owner=INTELLIGENCE_MATRIX_OWNER,
            page_id="developer_control",
            changes_control_policy=True,
        ),
    ),
    page_requirements=(
        IntelligencePageRequirement("referee"),
        IntelligencePageRequirement("intelligence"),
    ),
    matrix_input_contract=INTELLIGENCE_MATRIX_OUTPUT_SCHEMA,
    terminal_evidence_contract="CoreCommandTerminalReceipt.v1",
)
INTELLIGENCE_PRESENTATION_BOUNDARY = (
    ReadOnlyIntelligencePresentationBoundary()
)


def _require_exact_contract() -> None:
    fields = tuple(
        requirement.field_id
        for requirement in INTELLIGENCE_SURFACE_CONTRACT.field_requirements
    )
    if set(fields) != set(INTELLIGENCE_NGUI_SOURCE_IDS) - {"intelligence.recovery_status"}:
        raise RuntimeError("New Horizon Intelligence surface denominator mismatch")
    if tuple(
        requirement.page_id
        for requirement in INTELLIGENCE_SURFACE_CONTRACT.page_requirements
    ) != ("referee", "intelligence"):
        raise RuntimeError("New Horizon Intelligence page denominator mismatch")
    local_controls = tuple(
        requirement
        for requirement in INTELLIGENCE_SURFACE_CONTRACT.control_requirements
        if requirement.page_id == "intelligence"
    )
    product_controls = tuple(
        requirement
        for requirement in INTELLIGENCE_SURFACE_CONTRACT.control_requirements
        if requirement.page_id == "developer_control"
    )
    if any(
        requirement.mutates_intelligence or requirement.command_capable
        for requirement in local_controls
    ):
        raise RuntimeError("Phase 2 Intelligence surface must remain read-only")
    if {
        requirement.destination_owner
        for requirement in local_controls
    } != {"54guc.ngui.presentation"}:
        raise RuntimeError("Intelligence status controls must remain local")
    if (
        {requirement.object_name for requirement in product_controls}
        != {
            "nguiDeveloperAutopilotOn",
            "nguiDeveloperAutopilotOff",
            "nguiDeveloperExploreOn",
            "nguiDeveloperExploreOff",
        }
        or any(
            requirement.destination_owner != INTELLIGENCE_MATRIX_OWNER
            or not requirement.changes_control_policy
            or requirement.mutates_intelligence
            or requirement.command_capable
            for requirement in product_controls
        )
    ):
        raise RuntimeError("Intelligence controls must terminate at Product Matrix")
    if INTELLIGENCE_PRESENTATION_BOUNDARY.state != "ATTACHED_READ_ONLY":
        raise RuntimeError("New Horizon Intelligence surface state mismatch")


_require_exact_contract()


__all__ = (
    "INTELLIGENCE_PRESENTATION_BOUNDARY",
    "INTELLIGENCE_EXTENSION_STATE",
    "INTELLIGENCE_SURFACE_CONTRACT",
    "INTELLIGENCE_SURFACE_CONTRACT_VERSION",
    "IntelligenceFieldRequirement",
    "IntelligenceControlRequirement",
    "IntelligencePageRequirement",
    "IntelligenceSurfaceContract",
    "ReadOnlyIntelligencePresentationBoundary",
)
