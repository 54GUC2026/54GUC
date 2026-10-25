"""Apply the required rules to a Modem Live access contract.

This module has no ``arch4.*`` imports. ``ModemLiveModule`` supplies the backend
name and family. These rules are applied before the modem is accessed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any


LIVE_ACQUISITION_BOOL_OVERRIDES = {
    "backend_io_parallel_safe": "modem_live_backend_io_parallel_safe",
    "io_serial_required": "modem_live_io_serial_required",
    "startup_parallel_allowed": "modem_live_startup_parallel_allowed",
    "maintenance_holds_io_lock": "modem_live_maintenance_holds_io_lock",
}

HUAWEI_HILINK_FAMILY = "huawei_hilink_like"
HUAWEI_HTTP_SINGLE_LANE_BACKENDS = frozenset({"b818", "b535", "b535-232"})
HUAWEI_HTTP_SINGLE_LANE_REQUIRED = {
    "backend_io_parallel_safe": False,
    "io_serial_required": True,
    "startup_parallel_allowed": False,
    "runtime_action_requires_io_lock": True,
    "maintenance_holds_io_lock": True,
}


HUAWEI_HTTP_SINGLE_LANE_ACCESS_TYPE = "huawei_http_single_lane"


def _backend_key(backend: Any) -> str:
    return str(backend or "").strip().lower()


def _family_key(backend_family: Any) -> str:
    return str(backend_family or "").strip().lower()


def _access_type_key(access_type: Any) -> str:
    return str(access_type or "").strip().lower()


def _is_huawei_hilink_like(*, backend: Any, backend_family: Any) -> bool:
    family = _family_key(backend_family)
    if family:
        return family == HUAWEI_HILINK_FAMILY
    return _backend_key(backend) in {"b818", "huawei", "huawei_hilink"}


def enforce_live_access_hard_rules(
    contract: Any,
    *,
    backend: Any,
    backend_family: Any = "",
    access_type: Any = "",
) -> tuple[Any, dict[str, Any], dict[str, dict[str, bool]]]:
    """Apply the required Modem Live I/O rules to ``contract``."""

    backend_key = _backend_key(backend)
    access_key = _access_type_key(access_type)
    huawei_http_single_lane = (
        access_key == HUAWEI_HTTP_SINGLE_LANE_ACCESS_TYPE
        or backend_key in HUAWEI_HTTP_SINGLE_LANE_BACKENDS
    )
    if not _is_huawei_hilink_like(backend=backend, backend_family=backend_family) and not huawei_http_single_lane:
        return contract, {}, {}

    if not huawei_http_single_lane:
        return (
            replace(contract, runtime_action_requires_io_lock=True),
            {
                "huawei_hilink_write_actions_stay_protected": {
                    "runtime_action_requires_io_lock": True,
                },
            },
            {},
        )

    required = dict(HUAWEI_HTTP_SINGLE_LANE_REQUIRED)
    clamps: dict[str, dict[str, bool]] = {}
    for field_name, enforced_value in required.items():
        current_value = bool(getattr(contract, field_name, False))
        if current_value != enforced_value:
            clamps[field_name] = {
                "requested": current_value,
                "effective": bool(enforced_value),
            }

    hard_rules = {"huawei_http_single_lane_required": required}
    if not clamps:
        return contract, hard_rules, clamps
    return replace(contract, **required), hard_rules, clamps
