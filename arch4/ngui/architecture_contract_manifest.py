"""Executable inventory of NGUI architecture ownership.

This module does not use a GUI toolkit. It records operator fields, controls, pages, owners,
and required replacements. Later changes must resolve ``replacement_required``
entries. Do not remove an inventory entry only to make an audit pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .control_contracts import CGF_GUI_CONTROL_MANIFEST
from .intelligence_surface_contract import (
    INTELLIGENCE_PRESENTATION_BOUNDARY,
    INTELLIGENCE_SURFACE_CONTRACT,
)
from .registry import NGUI_FIELD_REGISTRY
from .types import (
    NGUI_VISIBLE_PAGES,
    PageId,
    SourceOwner,
)


ACTIVE_OPERATOR_PAGES: tuple[PageId, ...] = NGUI_VISIBLE_PAGES

ACTIVE_SURFACE = "active_operator"
DETACHED_SURFACE = "detached_future_requirement"
DIALOG_SURFACE = "active_dialog"
CHROME_SURFACE = "operator_chrome"
LOGICAL_SURFACE = "logical_register"


DETACHED_INTELLIGENCE_FIELD_IDS: frozenset[str] = frozenset()
DETACHED_INTELLIGENCE_CONTROL_IDS: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class NGUIFieldArchitectureContract:
    field_id: str
    current_owner: str
    legal_owner: str
    source_path: str
    consumer_pages: tuple[str, ...]
    event_source: str
    stale_invalid_policy: str
    evidence_artifact: str
    disposition: str
    surface: str


@dataclass(frozen=True, slots=True)
class NGUIControlArchitectureContract:
    object_name: str
    kind: str
    draft_owner: str
    submit_intents: tuple[str, ...]
    destinations: tuple[str, ...]
    surface: str
    event_source: str
    evidence_artifact: str


@dataclass(frozen=True, slots=True)
class NGUIPageArchitectureContract:
    page: PageId
    surface: str
    current_construction: str
    target_construction: str
    navigation: str
    replacement_batch: str


@dataclass(frozen=True, slots=True)
class NGUIReadOnlyValueContract:
    object_name: str
    value_owner: str
    surface: str
    evidence_artifact: str


@dataclass(frozen=True, slots=True)
class NGUIReplacementFinding:
    finding_id: str
    competing_path: str
    legal_owner: str
    required_action: str
    deletion_wave: int
    static_ban_token: str
    status: str = "pending"


def _legal_field_owner(field_id: str, current_owner: SourceOwner) -> str:
    del field_id
    return current_owner.value


def _field_event_source(legal_owner: str) -> str:
    if legal_owner == "54guc.modem_live.truth_matrix":
        return "accepted immutable Matrix generation and change notice"
    if legal_owner.startswith("54guc.foundation."):
        return "typed Foundation owner receipt"
    if legal_owner.startswith("54guc.ngui."):
        return "typed NGUI draft or lifecycle event"
    if legal_owner.startswith("command."):
        return "typed Command Authority lifecycle receipt"
    if legal_owner.startswith("runtime."):
        return "canonical runtime-clock publication"
    return "accepted owner-register generation"


def _field_evidence(legal_owner: str) -> str:
    if legal_owner == "54guc.modem_live.truth_matrix":
        return "current_state.json; ngui_page_truth.json"
    if legal_owner.startswith("54guc.foundation."):
        return "current_state.json; bounded Foundation evidence journal"
    if legal_owner.startswith("command."):
        return "Core command journal; ngui_page_truth.json"
    return "ngui_page_truth.json"


def _build_field_contracts() -> Mapping[str, NGUIFieldArchitectureContract]:
    result: dict[str, NGUIFieldArchitectureContract] = {}
    for field_id, spec in NGUI_FIELD_REGISTRY.items():
        legal_owner = _legal_field_owner(field_id, spec.owner)
        disposition = (
            "current"
            if legal_owner == spec.owner.value
            else "replacement_required"
        )
        result[field_id] = NGUIFieldArchitectureContract(
            field_id=field_id,
            current_owner=spec.owner.value,
            legal_owner=legal_owner,
            source_path=spec.source_path,
            consumer_pages=tuple(page.value for page in spec.pages),
            event_source=_field_event_source(legal_owner),
            stale_invalid_policy=(
                f"{spec.policy.value}; missing={spec.missing_text!r}; "
                f"unsupported={spec.unsupported_text!r}; "
                f"freshness_ms={spec.freshness_ms}; hold_ms={spec.hold_ms}"
            ),
            evidence_artifact=_field_evidence(legal_owner),
            disposition=disposition,
            surface=(
                DETACHED_SURFACE
                if field_id in DETACHED_INTELLIGENCE_FIELD_IDS
                else ACTIVE_SURFACE
            ),
        )
    return MappingProxyType(result)


NGUI_FIELD_ARCHITECTURE_CONTRACTS = _build_field_contracts()


_DIALOG_CONTROL_CONTRACTS: tuple[NGUIControlArchitectureContract, ...] = (
    NGUIControlArchitectureContract(
        "nguiLicenseKeyEditor",
        "text_editor",
        "54guc.ngui.license_dialog_draft",
        ("update_license",),
        ("54guc.foundation.license",),
        DIALOG_SURFACE,
        "QPlainTextEdit textChanged; dialog submit",
        "utility.lifecycle license terminal",
    ),
    NGUIControlArchitectureContract(
        "nguiLicenseKeyValidateActivate",
        "button",
        "",
        ("update_license",),
        ("54guc.foundation.license",),
        DIALOG_SURFACE,
        "QDialogButtonBox accepted",
        "utility.lifecycle license terminal",
    ),
    NGUIControlArchitectureContract(
        "nguiLicenseKeyCancel",
        "button",
        "",
        (),
        ("54guc.ngui.dialog_lifecycle",),
        DIALOG_SURFACE,
        "QDialogButtonBox rejected",
        "dialog rejected",
    ),
    NGUIControlArchitectureContract(
        "nguiBuyLicenseEmail",
        "text_editor",
        "54guc.ngui.buy_license_dialog_draft",
        (),
        ("54guc.ngui.dialog_lifecycle",),
        DIALOG_SURFACE,
        "QLineEdit textChanged; local validation",
        "typed unsupported-payment message",
    ),
    NGUIControlArchitectureContract(
        "nguiBuyLicenseProceed",
        "button",
        "54guc.ngui.buy_license_dialog_draft",
        (),
        ("unsupported.payment",),
        DIALOG_SURFACE,
        "QDialogButtonBox accepted",
        "typed unsupported-payment message",
    ),
    NGUIControlArchitectureContract(
        "nguiBuyLicenseCancel",
        "button",
        "",
        (),
        ("54guc.ngui.dialog_lifecycle",),
        DIALOG_SURFACE,
        "QDialogButtonBox rejected",
        "dialog rejected",
    ),
    NGUIControlArchitectureContract(
        "nguiPublicVersionClose",
        "button",
        "",
        (),
        ("54guc.ngui.dialog_lifecycle",),
        DIALOG_SURFACE,
        "QDialogButtonBox rejected",
        "dialog rejected",
    ),
    NGUIControlArchitectureContract(
        "nguiRouterSerialInput",
        "text_editor",
        "54guc.ngui.startup_router_serial_draft",
        (),
        ("54guc.ngui.runtime_service",),
        DIALOG_SURFACE,
        "QLineEdit textChanged; dialog submit",
        "typed startup router serial submission",
    ),
    NGUIControlArchitectureContract(
        "nguiRouterSerialSubmit",
        "button",
        "54guc.ngui.startup_router_serial_draft",
        (),
        ("54guc.ngui.runtime_service",),
        DIALOG_SURFACE,
        "QDialogButtonBox accepted",
        "typed startup router serial submission",
    ),
    NGUIControlArchitectureContract(
        "nguiRouterSerialCancel",
        "button",
        "",
        (),
        ("54guc.ngui.dialog_lifecycle",),
        DIALOG_SURFACE,
        "QDialogButtonBox rejected",
        "pending connection cancellation",
    ),
    NGUIControlArchitectureContract(
        "nguiTerminalMessageOk",
        "button",
        "",
        (),
        ("54guc.ngui.dialog_lifecycle",),
        DIALOG_SURFACE,
        "QMessageBox accepted",
        "dialog accepted",
    ),
)


def _build_control_contracts() -> Mapping[str, NGUIControlArchitectureContract]:
    result: dict[str, NGUIControlArchitectureContract] = {}
    for object_name, contract in CGF_GUI_CONTROL_MANIFEST.items():
        detached = object_name in DETACHED_INTELLIGENCE_CONTROL_IDS
        result[object_name] = NGUIControlArchitectureContract(
            object_name=object_name,
            kind=contract.kind.value,
            draft_owner=(
                contract.draft_owner.value if contract.draft_owner is not None else ""
            ),
            submit_intents=tuple(intent.value for intent in contract.submit_intents),
            destinations=contract.destinations,
            surface=DETACHED_SURFACE if detached else ACTIVE_SURFACE,
            event_source="NGUIInteractionRegister typed intent",
            evidence_artifact=(
                "typed terminal owner receipt; ngui_page_truth.json"
                if not detached
                else "detached Intelligence surface requirement"
            ),
        )
    for contract in _DIALOG_CONTROL_CONTRACTS:
        if contract.object_name in result:
            raise RuntimeError(
                f"duplicate NGUI surface control identity: {contract.object_name}"
            )
        result[contract.object_name] = contract
    return MappingProxyType(result)


NGUI_CONTROL_ARCHITECTURE_CONTRACTS = _build_control_contracts()

NGUI_DIALOG_CONTROL_CONTRACTS: Mapping[str, NGUIControlArchitectureContract] = (
    MappingProxyType(
        {contract.object_name: contract for contract in _DIALOG_CONTROL_CONTRACTS}
    )
)

NGUI_READ_ONLY_VALUE_CONTRACTS: Mapping[str, NGUIReadOnlyValueContract] = (
    MappingProxyType(
        {
            "nguiBuyLicenseToken": NGUIReadOnlyValueContract(
                object_name="nguiBuyLicenseToken",
                value_owner="54guc.foundation.license_purchase",
                surface=DIALOG_SURFACE,
                evidence_artifact="typed license purchase terminal token",
            )
        }
    )
)

NGUI_PAGE_ARCHITECTURE_CONTRACTS: Mapping[PageId, NGUIPageArchitectureContract] = (
    MappingProxyType(
        {
            PageId.ENTRY: NGUIPageArchitectureContract(
                PageId.ENTRY,
                LOGICAL_SURFACE,
                "logical_page_truth",
                "logical_page_truth",
                "none",
                "G00",
            ),
            PageId.HEADER: NGUIPageArchitectureContract(
                PageId.HEADER,
                CHROME_SURFACE,
                "constructed_chrome",
                "constructed_chrome",
                "persistent",
                "G00",
            ),
            PageId.FOOTER: NGUIPageArchitectureContract(
                PageId.FOOTER,
                CHROME_SURFACE,
                "constructed_chrome",
                "constructed_chrome",
                "persistent",
                "G00",
            ),
            PageId.DEVICE: NGUIPageArchitectureContract(
                PageId.DEVICE,
                ACTIVE_SURFACE,
                "constructed",
                "constructed",
                "active",
                "G00",
            ),
            PageId.GRAPHS: NGUIPageArchitectureContract(
                PageId.GRAPHS,
                ACTIVE_SURFACE,
                "constructed",
                "constructed",
                "active",
                "G00",
            ),
            PageId.DASHBOARD: NGUIPageArchitectureContract(
                PageId.DASHBOARD,
                ACTIVE_SURFACE,
                "constructed",
                "constructed",
                "active",
                "G00",
            ),
            PageId.DEVELOPER_CONTROL: NGUIPageArchitectureContract(
                PageId.DEVELOPER_CONTROL,
                ACTIVE_SURFACE,
                "constructed",
                "constructed",
                "active",
                "G00",
            ),
            PageId.REFEREE: NGUIPageArchitectureContract(
                PageId.REFEREE,
                ACTIVE_SURFACE,
                "constructed",
                "constructed",
                "active_read_only",
                "NEW_HORIZON_REFEREE",
            ),
            PageId.INTELLIGENCE: NGUIPageArchitectureContract(
                PageId.INTELLIGENCE,
                ACTIVE_SURFACE,
                "constructed",
                "constructed",
                "active_read_only",
                "NEW_HORIZON_P2",
            ),
        }
    )
)

def _require_g00_surface_denominator() -> None:
    active_fields = {
        field_id
        for field_id, contract in NGUI_FIELD_ARCHITECTURE_CONTRACTS.items()
        if contract.surface == ACTIVE_SURFACE
    }
    detached_fields = {
        field_id
        for field_id, contract in NGUI_FIELD_ARCHITECTURE_CONTRACTS.items()
        if contract.surface == DETACHED_SURFACE
    }
    active_controls = {
        object_name
        for object_name, contract in NGUI_CONTROL_ARCHITECTURE_CONTRACTS.items()
        if contract.surface in {ACTIVE_SURFACE, DIALOG_SURFACE}
    }
    detached_controls = {
        object_name
        for object_name, contract in NGUI_CONTROL_ARCHITECTURE_CONTRACTS.items()
        if contract.surface == DETACHED_SURFACE
    }
    active_pages = {
        page
        for page, contract in NGUI_PAGE_ARCHITECTURE_CONTRACTS.items()
        if contract.surface == ACTIVE_SURFACE
    }
    expected_counts = {
        # One bounded forecast and decision-history contract replaced eight
        # separate grade values. An ambiguous capability field became two
        # owner fields. Two congestion fields show the combiner inputs. The
        # NSA boundary publishes secondary-active and transition state; NGUI
        # does not infer them. The typed utility lifecycle is the only utility
        # status owner and feeds the nearby rows.
        # Core Internet reachability now supplies current state plus one typed
        # completed-outage event. NGUI plots and labels those facts but must
        # not reconstruct them from RTT, RF, or modem-specific status.
        # Antenna Guide has one start and one stop control. Formula values are
        # Core-owned readbacks, not operator controls or editable drafts.
        # Network suggestions are three drafts and one Foundation verdict.
        # One Matrix-owned missing-network diagnostic projection.
        # CPU temperature has its own component identity, separate from 5G.
        # Two last-transition diagnostic display rows were retired by the user.
        # Eleven independently observed NR measurements plus the existing
        # permanent NR capability scalar used for Graphs layout.
        # Independent LTE primary resource-block count; not NR RB or RI.
        # Router CPU/memory/system uptime are distinct from host/session values.
        # Retired duplicate NR channel alias; frequency uses its existing MHz field.
        # Independent Foundation-owned 5G Antenna Guide score and role.
        "active_fields": (len(active_fields), 308),
        "detached_fields": (len(detached_fields), 0),
        "active_controls": (len(active_controls), 117),
        "detached_controls": (len(detached_controls), 0),
        "active_pages": (len(active_pages), 6),
        "detached_pages": (0, 0),
        "dialog_controls": (len(NGUI_DIALOG_CONTROL_CONTRACTS), 11),
        "read_only_values": (len(NGUI_READ_ONLY_VALUE_CONTRACTS), 1),
    }
    mismatches = {
        name: {"actual": actual, "expected": expected}
        for name, (actual, expected) in expected_counts.items()
        if actual != expected
    }
    if mismatches:
        raise RuntimeError(f"NGUI G00 surface denominator mismatch: {mismatches}")
    if detached_fields != DETACHED_INTELLIGENCE_FIELD_IDS:
        raise RuntimeError("NGUI detached field denominator identity mismatch")
    if detached_controls != DETACHED_INTELLIGENCE_CONTROL_IDS:
        raise RuntimeError("NGUI detached control denominator identity mismatch")
    if active_pages != set(ACTIVE_OPERATOR_PAGES):
        raise RuntimeError("NGUI active page denominator identity mismatch")
    if set(NGUI_FIELD_REGISTRY) != active_fields:
        raise RuntimeError("NGUI active field registry identity mismatch")
    if set(CGF_GUI_CONTROL_MANIFEST) | set(NGUI_DIALOG_CONTROL_CONTRACTS) != active_controls:
        raise RuntimeError("NGUI active control registry identity mismatch")
    if INTELLIGENCE_PRESENTATION_BOUNDARY.state != "ATTACHED_READ_ONLY":
        raise RuntimeError("NGUI Intelligence extension must be read-only")


_require_g00_surface_denominator()


NGUI_REPLACEMENT_FINDINGS: tuple[NGUIReplacementFinding, ...] = (
    NGUIReplacementFinding("N-01", "complete-frame cursor advance on rejected owner input", "NGUI accepted complete-frame cursor", "commit or reject each complete frame atomically", 5, "projection_cursor_advanced_without_commit"),
    NGUIReplacementFinding("N-02", "partial owner batches replace complete NGUI owner state", "complete declared owner snapshot", "publish explicit state for every declared owner member", 5, "partial_owner_batch"),
    NGUIReplacementFinding("N-03", "runtime namespace envelope can mix with retained Matrix/register state", "atomic namespace cutover frame", "retire old namespace and install complete baseline atomically", 5, "mixed_namespace_baseline"),
    NGUIReplacementFinding("N-04", "NGUI presentation reinterprets normalized Modem Live values", "54guc.modem_live.truth_matrix", "consume canonical Matrix values without vendor synonym translation", 5, "ngui_modem_value_translation"),
    NGUIReplacementFinding("N-05", "radio.ca_state raw unavailable text bypass", "NGUI Presentation Authority", "use canonical PresentationState only", 5, "raw_ca_unavailable_text", "completed"),
    NGUIReplacementFinding("N-06", "integer presentation accepts fractional source values", "NGUI input contract validation", "reject fractional integer inputs", 5, "fractional_integer_acceptance", "completed"),
    NGUIReplacementFinding("N-07", "NGUI duplicated action availability policy", "Core/Foundation action and session owners", "render a stateless control view and submit every request to its owner", 5, "duplicate_ngui_action_availability", "completed"),
    NGUIReplacementFinding("N-08", "manual lifecycle fabricates zero Internet pause", "action reachability transaction", "publish numeric outage only from measured reachability evidence", 3, "fabricated_zero_internet_pause"),
    NGUIReplacementFinding("N-09", "manual lifecycle retains only one connectivity interval", "action reachability transaction", "retain ordered outage intervals and total", 3, "single_outage_interval"),
    NGUIReplacementFinding("N-10", "runtime_service updates command context before frame acceptance", "accepted frame delivery receipt", "commit command context only after accepted delivery", 5, "command_context_before_frame_accept", "completed"),
    NGUIReplacementFinding("N-11", "parent delivery has no Core acknowledgment", "Core delivery ledger", "return typed frame/front/chart acknowledgment", 2, "missing_ngui_delivery_ack", "completed"),
    NGUIReplacementFinding("N-12", "projection wake removed before successful projection", "NGUI runtime projection", "retain uncommitted wake until commit or terminal rejection", 5, "lost_projection_wake", "completed"),
    NGUIReplacementFinding("N-13", "one bounded queue mixes status, lifecycle outcome and shutdown", "typed durable lifecycle queues", "separate coalescible status, replayable outcomes and shutdown", 5, "mixed_control_event_queue", "completed"),
    NGUIReplacementFinding("N-14", "runtime worker invokes ad hoc recover_client", "Core customer-port resync protocol", "migrate recovery to typed resync", 2, "recover_client", "completed"),
    NGUIReplacementFinding("N-15", "NGUI classifies arbitrary startup exceptions", "typed startup owner result", "consume pending/retryable/terminal startup receipts", 5, "ngui_startup_exception_classifier", "completed"),
    NGUIReplacementFinding("N-16", "Matrix readiness is all-or-nothing", "Modem Live required-startup manifest", "quarantine optional cells without blocking required truth", 1, "all_or_nothing_matrix_readiness"),
    NGUIReplacementFinding("N-17", "_DeviceWifiPending and _settle_wifi_pending", "Core Command Authority Wi-Fi lifecycle", "delete renderer lifecycle/interlock/timeout state", 5, "_DeviceWifiPending", "completed"),
    NGUIReplacementFinding("N-18", "Device renderer maps missing or unsupported Wi-Fi to OFF", "NGUI Presentation Authority", "render the canonical presentation state unchanged", 5, "missing_wifi_as_off", "completed"),
    NGUIReplacementFinding("N-19", "Device connect state parses profile.login_status English prefixes", "typed NGUI login lifecycle", "delete renderer prefix parsing and local pending state", 5, "_connect_pending_after_sequence", "completed"),
    NGUIReplacementFinding("N-20", "Dashboard _SUPPRESSED_STATES blanks PageTruth event text", "NGUI Presentation Authority", "renderer displays canonical event text unchanged", 5, "_SUPPRESSED_STATES", "completed"),
    NGUIReplacementFinding("N-21", "page-local NGUIConfirmationCoordinator instances", "window-scoped confirmation coordinator", "route every dangerous control through one coordinator", 5, "page_local_confirmation_coordinator", "completed"),
    NGUIReplacementFinding("N-22", "Graphs _normalized_bands and Developer raw band targets", "canonical Core command-target parser", "submit one typed target from every page", 3, "_normalized_bands"),
    NGUIReplacementFinding("N-23", "renderer.chrome parse_license_entry", "54guc.foundation.license", "send raw draft and render typed Foundation receipt", 5, "parse_license_entry", "completed"),
    NGUIReplacementFinding("N-24", "application scan bridge mutates window._pages and infers profile", "typed Profile/Discovery suggestion", "publish suggestion through page truth", 5, "window._pages", "completed"),
    NGUIReplacementFinding("N-25", "Developer visible.manual_* pseudo-fields", "NGUI Presentation Authority field registry", "make visible timing values first-class fields", 5, "visible.manual_", "completed"),
    NGUIReplacementFinding("N-26", "page-specific partial PageTruth guards", "one exact page-shell contract guard", "enforce identity and complete exact fields for every page", 5, "partial_page_truth_guard", "completed"),
    NGUIReplacementFinding("N-27", "blank traffic-plan editor retains old draft", "NGUI traffic-plan draft register", "commit explicit invalid empty draft and block submit", 5, "blank_retains_traffic_plan", "completed"),
    NGUIReplacementFinding("N-28", "NGUIEditRegister silently clears after 90 seconds", "typed edit owner receipt", "publish visible terminal timeout failure", 5, "EDIT_PENDING_TIMEOUT_S", "completed"),
    NGUIReplacementFinding("N-29", "per-kind external/heavy executors queue hidden work", "Foundation active-job admission owner", "declare active/queued budgets and retained terminal identities", 4, "unbounded_kind_executor_admission", "completed"),
    NGUIReplacementFinding("N-30", "NGUI fault append-then-compact journal", "Foundation atomic bounded journal adapter", "migrate parent evidence and delete duplicate journal", 4, "ngui_append_compact_fault_journal", "completed"),
    NGUIReplacementFinding("N-31", "duplicate profile runtime capability publication", "54guc.modem_live.truth_matrix", "Matrix direct-cell migration and old caller removal", 5, "profile.capability", "completed"),
    NGUIReplacementFinding("N-32", "NGUIEditRegister defaults appear as accepted truth", "typed Foundation owner readback", "separate draft defaults from accepted truth", 5, "default_as_accepted_truth", "completed"),
    NGUIReplacementFinding("N-33", "synthetic intent tests claim production execution", "real boundary end-to-end proof", "rename declaration claims and add real IPC/Core/Modem Live proofs", 6, "fake_manual_nmi_execution_claim"),
)


NGUI_REPLACEMENT_FINDINGS_BY_ID: Mapping[str, NGUIReplacementFinding] = (
    MappingProxyType({finding.finding_id: finding for finding in NGUI_REPLACEMENT_FINDINGS})
)


__all__ = (
    "ACTIVE_SURFACE",
    "ACTIVE_OPERATOR_PAGES",
    "CHROME_SURFACE",
    "DETACHED_INTELLIGENCE_CONTROL_IDS",
    "DETACHED_INTELLIGENCE_FIELD_IDS",
    "DETACHED_SURFACE",
    "LOGICAL_SURFACE",
    "NGUI_CONTROL_ARCHITECTURE_CONTRACTS",
    "NGUI_DIALOG_CONTROL_CONTRACTS",
    "NGUI_FIELD_ARCHITECTURE_CONTRACTS",
    "NGUI_PAGE_ARCHITECTURE_CONTRACTS",
    "NGUI_READ_ONLY_VALUE_CONTRACTS",
    "NGUI_REPLACEMENT_FINDINGS",
    "NGUI_REPLACEMENT_FINDINGS_BY_ID",
    "NGUIControlArchitectureContract",
    "NGUIFieldArchitectureContract",
    "NGUIPageArchitectureContract",
    "NGUIReadOnlyValueContract",
    "NGUIReplacementFinding",
)
