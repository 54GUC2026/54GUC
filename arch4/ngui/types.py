"""Types shared by NGUI processors and renderers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PageId(str, Enum):
    ENTRY = "entry"
    HEADER = "header"
    FOOTER = "footer"
    DEVICE = "device"
    GRAPHS = "graphs"
    DASHBOARD = "dashboard"
    DEVELOPER_CONTROL = "developer_control"
    REFEREE = "referee"
    INTELLIGENCE = "intelligence"


NGUI_VISIBLE_PAGES = (
    PageId.DEVICE,
    PageId.GRAPHS,
    PageId.DASHBOARD,
    PageId.DEVELOPER_CONTROL,
    PageId.REFEREE,
    PageId.INTELLIGENCE,
)


class FieldKind(str, Enum):
    BOOL = "bool"
    FLOAT = "float"
    INTEGER = "integer"
    TEXT = "text"
    LIST = "list"
    MAPPING = "mapping"


class ChartPane(str, Enum):
    UPPER = "upper"
    LOWER = "lower"


class FreshnessState(str, Enum):
    FRESH = "fresh"
    HELD = "held"
    STALE = "stale"
    TRANSITION = "transition"
    UNKNOWN = "unknown"


class SourceOwner(str, Enum):
    MODEM_MATRIX = "modem_live.truth_matrix"
    LATENCY = "signal_compute.latency"
    QUALITY = "signal_compute.quality"
    CA = "signal_compute.ca"
    HEALTH = "health.authority"
    RUNTIME_CLOCK = "runtime.clock"
    CORE_REACHABILITY = "54guc.core.action_reachability"
    FOUNDATION_SERVICE = "foundation_service.presentation"
    FOUNDATION_LICENSE = "54guc.foundation.license"
    FOUNDATION_RUNTIME_HEALTH = "54guc.foundation.runtime_health"
    FOUNDATION_PERSISTENCE = "54guc.foundation.persistence"
    COMMAND = "command.readback"
    PROFILE_SELECTION = "54guc.ngui.profile_selection"
    PROFILE_DISCOVERY = "54guc.ngui.profile_discovery"
    LOGIN_LIFECYCLE = "54guc.ngui.login_lifecycle"
    GUI_SETTINGS = "ngui.settings"
    GUI_UTILITY = "54guc.ngui.utility_surface"
    GUI_EDIT = "54guc.ngui.edit_surface"
    GUI_ACTION = "54guc.ngui.action_surface"
    GUI_VOICE = "54guc.ngui.voice_surface"
    GUI_EVENT = "54guc.ngui.event_surface"
    INTELLIGENCE = "54guc.product.intelligence_matrix"


class PresentationPolicy(str, Enum):
    DIRECT = "direct"
    SHORT_MISS_HOLD = "short_miss_hold"
    RADIO_IDENTITY_PACKAGE = "radio_identity_package"
    DEVICE_IDENTITY_PACKAGE = "device_identity_package"
    CA_LD_MODEM_SAMPLES = "ca_ld_modem_samples"
    JITTER_STABLE = "jitter_stable"
    USER_INPUT = "user_input"
    COMMAND_READBACK = "command_readback"
    CONNECTION_LED = "connection_led"
    CONNECTION_STATUS = "connection_status"
    NETWORK_BADGE = "network_badge"
    NEIGHBOR_SLIDING_HOLD = "neighbor_sliding_hold"
    TX_POWER_COMPONENT = "tx_power_component"
    ANTENNA_GUIDE = "antenna_guide"
    CONNECTIVITY_TRANSITION_SUMMARY = "connectivity_transition_summary"
    DURATION_HMS = "duration_hms"
    RTT_OBSERVATION = "rtt_observation"
    QAM_MODULATION_EFFICIENCY = "qam_modulation_efficiency"

@dataclass(frozen=True, slots=True)
class InputSpec:
    source_id: str
    owner: SourceOwner
    kind: FieldKind


@dataclass(frozen=True, slots=True)
class FieldSpec:
    field_id: str
    label: str
    owner: SourceOwner
    source_path: str
    kind: FieldKind
    source_kind: FieldKind
    pages: tuple[PageId, ...]
    policy: PresentationPolicy = PresentationPolicy.DIRECT
    unit: str = ""
    publish_ms: int = 500
    freshness_ms: int = 10_000
    hold_ms: int = 90_000
    allow_zero: bool = True
    allow_empty_text: bool = False
    missing_text: str = "---"
    unsupported_text: str = "UNSUP."
    chart_pane: ChartPane | None = None
    chart_color: str = ""
    chart_marker: bool = False
    display_in_grid: bool = True
    readiness_required: bool = False

    def __post_init__(self) -> None:
        if not self.field_id or self.field_id.strip() != self.field_id:
            raise ValueError("field_id must be a non-empty canonical identifier")
        if not self.source_path or self.source_path.strip() != self.source_path:
            raise ValueError(f"{self.field_id}: source_path is required")
        if not self.pages:
            raise ValueError(f"{self.field_id}: at least one page consumer is required")
        if len(set(self.pages)) != len(self.pages):
            raise ValueError(f"{self.field_id}: duplicate page consumer")
        if self.publish_ms < 10:
            raise ValueError(f"{self.field_id}: publish_ms must be at least 10 ms")
        if self.freshness_ms < 0:
            raise ValueError(f"{self.field_id}: freshness_ms cannot be negative")
        if self.hold_ms < 0:
            raise ValueError(f"{self.field_id}: hold_ms cannot be negative")
        if not self.missing_text:
            raise ValueError(f"{self.field_id}: missing_text cannot be empty")
        if not self.unsupported_text:
            raise ValueError(f"{self.field_id}: unsupported_text cannot be empty")
        if (self.chart_pane is None) != (not self.chart_color):
            raise ValueError(f"{self.field_id}: chart pane and color must be declared together")
        if self.chart_marker and self.kind not in {FieldKind.INTEGER, FieldKind.TEXT}:
            raise ValueError(f"{self.field_id}: structural marker must be text or integer")
