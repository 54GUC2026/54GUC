"""Validate the mutable Modem Live profile catalog offline.

This module checks the owner data in place. Runtime does not import it. It does
not save data or own current modem values.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any

from arch4.modem.catalog import (
    _ALL_KNOWN_ACTIONS,
    BAND_CAPABILITY_DECLARED,
    BAND_CAPABILITY_STATUSES,
    CONSERVATIVE_CAPABILITY_FLAGS,
    FIRMWARE_PROFILE_OVERLAYS,
    FirmwareProfileOverlay,
)
from arch4.modem.live_lane_contract import GLOBAL_LIVE_LANE_DEFINITIONS

from .access_profile_owner import resolve_modem_live_access_profile
from .profile_catalog import modem_profile_selection
from .profile_matrix_contract import profile_matrix_dispositions


PROFILE_PROVENANCE = frozenset({"observed", "documented", "inferred", "community"})
FIELD_TRUST_LEVELS = frozenset(
    {"confirmed", "expected", "synthetic", "untested", "unsupported", "non_useful"}
)
ACTION_TRUST_LEVELS = frozenset({"supported", "degraded", "unsupported", "untested"})


class ModemProfileValidationError(ValueError):
    """Raised when one or more profile records are invalid."""


@dataclass(frozen=True, slots=True)
class ModemProfileValidationReport:
    profile_count: int
    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.errors

    def require_valid(self) -> None:
        if self.errors:
            raise ModemProfileValidationError("\n".join(self.errors))


def _error(errors: list[str], profile_id: object, reason: str) -> None:
    errors.append(f"{str(profile_id or '<unknown>')}:{reason}")


def _validate_bands(
    errors: list[str],
    profile: FirmwareProfileOverlay,
    *,
    rat: str,
    status: str,
    bands: tuple[int, ...],
) -> None:
    prefix = profile.profile_id
    if status not in BAND_CAPABILITY_STATUSES:
        _error(errors, prefix, f"{rat}_band_status_invalid")
        return
    if tuple(bands) != tuple(sorted(set(bands))) or any(
        not isinstance(band, int) or isinstance(band, bool) or band <= 0
        for band in bands
    ):
        _error(errors, prefix, f"{rat}_bands_invalid")
    if status == BAND_CAPABILITY_DECLARED and not bands:
        _error(errors, prefix, f"{rat}_declared_bands_missing")
    if status != BAND_CAPABILITY_DECLARED and bands:
        _error(errors, prefix, f"{rat}_undeclared_bands_present")


def _validate_access(errors: list[str], profile: FirmwareProfileOverlay) -> None:
    prefix = profile.profile_id
    selection = modem_profile_selection(prefix)
    if selection is None:
        _error(errors, prefix, "launch_selection_missing")
        return
    try:
        access = resolve_modem_live_access_profile(
            backend=selection.backend,
            backend_family=selection.family,
            profile_id=selection.profile_id,
            model=selection.model_hint,
            firmware=selection.software_hint,
            vendor=selection.manufacturer,
        )
    except Exception as exc:
        _error(errors, prefix, f"access_profile_unresolved:{type(exc).__name__}")
        return
    if str(access.profile_id or "").strip() in {"", "unknown_probe_only_access_v1"}:
        _error(errors, prefix, "access_profile_missing")
    invalid_timing = False
    for name, value in asdict(access).items():
        if not name.endswith("_s") or value is None:
            continue
        try:
            finite = math.isfinite(float(value))
        except (TypeError, ValueError, OverflowError):
            finite = False
        if not finite:
            _error(errors, prefix, f"access_timing_not_finite:{name}")
            invalid_timing = True
    if invalid_timing:
        return
    expected_lanes = {item.lane for item in GLOBAL_LIVE_LANE_DEFINITIONS}
    if set(access.lane_groups) != expected_lanes:
        _error(errors, prefix, "access_lane_set_invalid")
    for definition in GLOBAL_LIVE_LANE_DEFINITIONS:
        groups = tuple(access.lane_groups.get(definition.lane, ()))
        if len(groups) != len(set(groups)) or not set(groups).issubset(
            definition.allowed_groups
        ):
            _error(errors, prefix, f"access_lane_groups_invalid:{definition.lane}")
    positive = (
        "fast_live_interval_s",
        "fast_live_stale_s",
        "slow_live_interval_s",
        "fast_read_slo_s",
        "fast_signal_connect_timeout_s",
        "fast_signal_read_timeout_s",
        "maintenance_connect_timeout_s",
        "maintenance_read_timeout_s",
        "fast_traffic_connect_timeout_s",
        "fast_traffic_min_timeout_s",
        "fast_traffic_max_timeout_s",
    )
    for name in positive:
        if float(getattr(access, name, 0.0) or 0.0) <= 0.0:
            _error(errors, prefix, f"access_timing_not_positive:{name}")
    nonnegative = (
        "event_write_min_quiet_s",
        "post_write_readback_delay_s",
        "min_inter_request_gap_s",
        "fast_traffic_cache_s",
    )
    for name in nonnegative:
        if float(getattr(access, name, 0.0) or 0.0) < 0.0:
            _error(errors, prefix, f"access_timing_negative:{name}")
    if float(access.fast_live_stale_s) < float(access.fast_live_interval_s):
        _error(errors, prefix, "access_stale_shorter_than_interval")
    if float(access.fast_traffic_min_timeout_s) > float(
        access.fast_traffic_max_timeout_s
    ):
        _error(errors, prefix, "access_traffic_timeout_range_invalid")
    if int(access.read_retry_limit) < 0 or int(access.write_retry_limit) < 0:
        _error(errors, prefix, "access_retry_limit_negative")
    if access.direct_modem_writes_allowed and access.readback_required:
        if float(access.post_write_readback_deadline_s) <= float(
            access.post_write_readback_delay_s
        ):
            _error(errors, prefix, "access_readback_deadline_invalid")


def _validate_profile(
    profile: FirmwareProfileOverlay,
    errors: list[str],
) -> None:
    prefix = str(getattr(profile, "profile_id", "") or "")
    required_text = (
        "profile_id",
        "vendor",
        "family",
        "display_name",
        "notes",
        "band_capability_reference",
    )
    for name in required_text:
        if not str(getattr(profile, name, "") or "").strip():
            _error(errors, prefix, f"required_value_missing:{name}")
    if profile.provenance not in PROFILE_PROVENANCE:
        _error(errors, prefix, "provenance_invalid")
    if not (
        profile.model_contains
        or profile.firmware_contains
        or profile.os_name
        or profile.profile_id.endswith("baseline")
    ):
        _error(errors, prefix, "identity_selector_missing")

    capabilities = dict(profile.capabilities or {})
    if not set(CONSERVATIVE_CAPABILITY_FLAGS).issubset(capabilities):
        _error(errors, prefix, "capability_vocabulary_incomplete")
    if any(
        not isinstance(capabilities.get(name), bool)
        for name in CONSERVATIVE_CAPABILITY_FLAGS
    ):
        _error(errors, prefix, "capability_value_not_boolean")

    supported = set(profile.supported_fields or ())
    unsupported = set(profile.unsupported_fields or ())
    synthetic = set((profile.synthetic_fields or {}).keys())
    if supported.intersection(unsupported) or synthetic.intersection(unsupported):
        _error(errors, prefix, "field_classification_conflict")
    declared = supported | unsupported | synthetic
    field_trust = dict(profile.field_trust or {})
    if not declared.issubset(field_trust):
        _error(errors, prefix, "field_trust_incomplete")
    if not set(field_trust.values()).issubset(FIELD_TRUST_LEVELS):
        _error(errors, prefix, "field_trust_value_invalid")
    try:
        profile_matrix_dispositions(asdict(profile))
    except Exception as exc:
        _error(errors, prefix, f"matrix_field_mapping_invalid:{exc}")

    actions = set(_ALL_KNOWN_ACTIONS)
    action_trust = dict(profile.action_trust or {})
    if set(action_trust) != actions or not set(action_trust.values()).issubset(
        ACTION_TRUST_LEVELS
    ):
        _error(errors, prefix, "action_trust_invalid")

    _validate_bands(
        errors,
        profile,
        rat="lte",
        status=profile.lte_band_capability_status,
        bands=tuple(profile.supported_lte_bands),
    )
    _validate_bands(
        errors,
        profile,
        rat="nr",
        status=profile.nr_band_capability_status,
        bands=tuple(profile.supported_nr_bands),
    )
    _validate_access(errors, profile)


def validate_modem_profiles() -> ModemProfileValidationReport:
    """Validate the catalog in place without making another profile copy."""

    profiles = tuple(FIRMWARE_PROFILE_OVERLAYS)
    errors: list[str] = []
    profile_ids = tuple(str(profile.profile_id or "") for profile in profiles)
    if len(set(profile_ids)) != len(profile_ids):
        errors.append("catalog:duplicate_profile_id")
    for profile in profiles:
        _validate_profile(profile, errors)
    return ModemProfileValidationReport(
        profile_count=len(profiles),
        errors=tuple(sorted(set(errors))),
    )


def require_valid_modem_profiles() -> int:
    report = validate_modem_profiles()
    report.require_valid()
    return report.profile_count


def main() -> int:
    report = validate_modem_profiles()
    if report.errors:
        for error in report.errors:
            print(error)
        return 1
    print(f"{report.profile_count} modem profiles valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "ModemProfileValidationError",
    "ModemProfileValidationReport",
    "require_valid_modem_profiles",
    "validate_modem_profiles",
)
