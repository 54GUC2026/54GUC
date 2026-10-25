from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Mapping

from arch4.runtime_support_tier import (
    has_any_support_authority_field,
    has_complete_support_authority,
    support_tier_from_payload,
    support_tier_label,
)


FULL = "full"
PARTIAL = "partial"
UNKNOWN = "unknown"
OBSERVE = "observe"

_LABELS = {
    FULL: "",
    PARTIAL: "",
    UNKNOWN: "UNKNOWN DEVICE",
    OBSERVE: "OBSERVE ONLY",
}

_COLORS_CSS = {
    FULL: None,
    PARTIAL: None,
    UNKNOWN: "#D95F5F",
    OBSERVE: "#8A8A8A",
}

_BLOCK_REASONS = {
    UNKNOWN: (
        ""
    ),
    PARTIAL: (
        ""
    ),
    OBSERVE: (
        "This session is running in observe-only mode. "
        "No write actions are allowed."
    ),
}


@dataclass(frozen=True, slots=True)
class ModemLiveSupportAuthority:
    """Runtime diagnostic support projection.

    This is not a write gate. Product permission belongs to licensing; modem
    action permission belongs to the capability contract.
    """

    support_state: str
    support_tier: int
    support_tier_label: str
    write_authority: str
    write_block_reason: str
    source: str

    @property
    def blocks_writes(self) -> bool:
        return support_state_blocks_writes(self.support_state)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _state_from_tier(tier: int) -> str:
    if int(tier or 0) <= 0:
        return UNKNOWN
    return FULL


def derive_modem_live_support_authority(
    *,
    backend_name: str,
    backend_family: str,
    unsupported_fields: FrozenSet[str],
    capabilities: Any,
    observe_only: bool,
) -> ModemLiveSupportAuthority:
    """Derive support/write authority from modem-live/runtime truth.

    GUI and other output surfaces may present this result, but must not
    independently decide whether a modem is full/partial/unknown.
    """
    if observe_only:
        return ModemLiveSupportAuthority(
            support_state=OBSERVE,
            support_tier=0,
            support_tier_label=support_tier_label(0),
            write_authority="none_observe_only",
            write_block_reason=support_state_block_reason(OBSERVE),
            source="session_mode",
        )

    caps = _mapping(capabilities)
    tier = support_tier_from_payload(caps)
    has_runtime_surface = has_any_support_authority_field(caps)

    if has_runtime_surface:
        if not has_complete_support_authority(caps):
            state_text = _text(caps.get("support_state")).lower()
            has_diagnostic_surface = bool(
                state_text
                and caps.get("support_tier") not in (None, "")
                and _text(caps.get("support_tier_label"))
                and "write_block_reason" in caps
            )
            if has_diagnostic_surface and state_text == FULL:
                write_authority = _text(caps.get("write_authority")) or "full"
                if write_authority.startswith("none_"):
                    write_authority = "full"
                return ModemLiveSupportAuthority(
                    support_state=FULL,
                    support_tier=int(tier),
                    support_tier_label=_text(caps.get("support_tier_label")) or support_tier_label(tier),
                    write_authority=write_authority,
                    write_block_reason="",
                    source="modem_live_diagnostic_support_surface",
                )
            return ModemLiveSupportAuthority(
                support_state=UNKNOWN,
                support_tier=0,
                support_tier_label=support_tier_label(0),
                write_authority="",
                write_block_reason=support_state_block_reason(UNKNOWN),
                source="incomplete_modem_live_runtime_surface",
            )
        state = _state_from_tier(tier)
        resolved_identity = _mapping(caps.get("resolved_identity_card"))
        runtime_identity = _mapping(caps.get("runtime_identity"))
        exact_identity_published = bool(resolved_identity.get("exact_identity", False)) or (
            _text(resolved_identity.get("profile_id"))
            and _text(resolved_identity.get("match_quality")) == "exact_firmware"
        )
        runtime_identity_published = _text(runtime_identity.get("identity_state")).lower() == "identified"
        if not _text(backend_name) and state == FULL and not (exact_identity_published or runtime_identity_published):
            state = UNKNOWN
        if state == FULL:
            write_authority = _text(caps.get("write_authority")) or "full"
            if write_authority.startswith("none_"):
                write_authority = "full"
            return ModemLiveSupportAuthority(
                support_state=FULL,
                support_tier=int(tier),
                support_tier_label=_text(caps.get("support_tier_label")) or support_tier_label(tier),
                write_authority=write_authority,
                write_block_reason="",
                source="modem_live_runtime_surface",
            )

        reason = _text(caps.get("write_block_reason")) or support_state_block_reason(state)
        write_authority = _text(caps.get("write_authority"))
        return ModemLiveSupportAuthority(
            support_state=state,
            support_tier=int(tier),
            support_tier_label=_text(caps.get("support_tier_label")) or support_tier_label(tier),
            write_authority=write_authority,
            write_block_reason=reason,
            source="modem_live_runtime_surface",
        )

    return ModemLiveSupportAuthority(
        support_state=UNKNOWN,
        support_tier=0,
        support_tier_label=support_tier_label(0),
        write_authority="",
        write_block_reason=support_state_block_reason(UNKNOWN),
        source="missing_modem_live_runtime_surface",
    )


def derive_session_support_state(
    *,
    backend_name: str,
    backend_family: str,
    unsupported_fields: FrozenSet[str],
    capabilities: Any,
    observe_only: bool,
) -> str:
    return derive_modem_live_support_authority(
        backend_name=backend_name,
        backend_family=backend_family,
        unsupported_fields=unsupported_fields,
        capabilities=capabilities,
        observe_only=observe_only,
    ).support_state


def support_state_label(state: str) -> str:
    return _LABELS.get(state, "")


def support_state_color(state: str) -> str | None:
    return _COLORS_CSS.get(state)


def support_state_blocks_writes(state: str) -> bool:
    return state == OBSERVE


def support_state_block_reason(state: str) -> str:
    return _BLOCK_REASONS.get(state, "")
