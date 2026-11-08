"""Atomic config publication owned by Core. Profiles and runtime policy may propose values,
but this module is the only publisher for upper-controller config. Readers, checkpoints,
and publication share one lock, so they always see a complete generation. Modem Live
timing and protocol settings stay outside this owner.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import hashlib
import json
import math
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping

from arch4.core_contracts.configuration_keys import (
    CALIBRATION_PROFILE_BLOCKED_KEYS,
    CALIBRATION_PROFILE_KEYS,
    OVERRIDABLE_POLICY_FIELDS,
    PROFILE_TUNING_PROPOSAL_KEYS,
)


CONFIG_GENERATION_SCHEMA = "ConfigGeneration.v3"
CONFIG_GENERATION_RECEIPT_SCHEMA = "ConfigGenerationReceipt.v3"
CONFIG_GENERATION_OWNER = "config_generation_authority"
CONFIG_GENERATION_SOURCE_PROFILE = "calibration_profile"
CONFIG_GENERATION_SOURCE_RUNTIME = "runtime_tuning"
CONFIG_GENERATION_SOURCE_OPERATOR = "operator_runtime"
CONFIG_GENERATION_SOURCE_POLICY_OVERLAY = "policy_overlay"
CONFIG_GENERATION_SOURCE_POLICY_TEMPLATE = "policy_template"
CONFIG_GENERATION_SOURCE_KINDS = frozenset(
    {
        CONFIG_GENERATION_SOURCE_PROFILE,
        CONFIG_GENERATION_SOURCE_RUNTIME,
        CONFIG_GENERATION_SOURCE_OPERATOR,
        CONFIG_GENERATION_SOURCE_POLICY_OVERLAY,
        CONFIG_GENERATION_SOURCE_POLICY_TEMPLATE,
    }
)
OPERATOR_RUNTIME_CONFIG_KEYS = frozenset(
    {
        "sms_command_enabled",
        "sms_command_approved_senders",
        "sms_command_code_word",
        "sms_command_delete_processed",
        "sms_inbox_poll_interval_s",
    }
)
STARTUP_SAFETY_RESET_CONFIG_KEYS = frozenset(
    {
        # SMS control is enabled for one run only. Sender, keyword, and interval
        # may persist, but a new process must start with modem control disarmed.
        "sms_command_enabled",
    }
)
POLICY_OVERLAY_CONFIG_KEYS = frozenset(OVERRIDABLE_POLICY_FIELDS)
# Validate policy templates against the frozen config. A static audit keeps
# modem and profile fields out of this upper-level policy.
POLICY_TEMPLATE_FORBIDDEN_PREFIXES = ("modem_live_", "hilink_netmode_")
POLICY_TEMPLATE_FORBIDDEN_KEYS = frozenset(
    {
        "modem_backend",
        "router_host",
        "router_user",
        "router_password",
        "model_profile_name",
        "model_profile_path",
        "health_checks_enabled",
        "calibration_enabled",
        *PROFILE_TUNING_PROPOSAL_KEYS,
    }
)
RUNTIME_TUNING_CONFIG_KEYS = frozenset(
    {
        "congestion_rtt_weight",
        "congestion_rsrq_weight",
        "jitter_formula_pct_scale_ms",
        "jitter_formula_visible_scale",
        "jitter_formula_trim_frac",
        "jitter_formula_low_frac",
    }
)
PROFILE_CONFIG_KEYS = frozenset(
    (
        set(CALIBRATION_PROFILE_KEYS)
        | set(CALIBRATION_PROFILE_BLOCKED_KEYS)
    )
    - set(PROFILE_TUNING_PROPOSAL_KEYS)
)
PROFILE_SET_CONFIG_KEYS = frozenset(
    set(PROFILE_CONFIG_KEYS) - set(CALIBRATION_PROFILE_BLOCKED_KEYS)
)
FIXED_DURABLE_CONFIG_KEYS = frozenset(
    set(PROFILE_SET_CONFIG_KEYS)
    | set(RUNTIME_TUNING_CONFIG_KEYS)
    | set(OPERATOR_RUNTIME_CONFIG_KEYS)
    | set(POLICY_OVERLAY_CONFIG_KEYS)
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=repr)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return _json_value(value.value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(
        "config_generation_value_not_json:" + type(value).__name__
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            _json_value(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class FrozenConfigSnapshot:
    """Copy-isolated, read-only view of every validated config field."""

    __slots__ = ("__values", "generation_id", "fingerprint")

    def __init__(
        self,
        values: Mapping[str, Any],
        *,
        generation_id: str,
    ) -> None:
        plain = {
            str(key): copy.deepcopy(value)
            for key, value in dict(values or {}).items()
        }
        fingerprint = _digest(plain)
        object.__setattr__(self, "_FrozenConfigSnapshot__values", MappingProxyType(plain))
        object.__setattr__(self, "generation_id", str(generation_id or "bootstrap"))
        object.__setattr__(self, "fingerprint", fingerprint)

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        generation_id: str,
    ) -> "FrozenConfigSnapshot":
        if isinstance(config, cls):
            values = config.to_plain_dict()
        elif is_dataclass(config) and not isinstance(config, type):
            values = {
                field.name: copy.deepcopy(getattr(config, field.name))
                for field in fields(config)
            }
        else:
            raise TypeError("config_generation_dataclass_config_required")
        return cls(values, generation_id=generation_id)

    def with_updates(
        self,
        updates: Mapping[str, Any],
        *,
        generation_id: str,
    ) -> "FrozenConfigSnapshot":
        values = self.to_plain_dict()
        missing = sorted(str(key) for key in updates if str(key) not in values)
        if missing:
            raise RuntimeError(
                "config_generation_unknown_config_keys:" + ",".join(missing)
            )
        values.update(
            {str(key): copy.deepcopy(value) for key, value in updates.items()}
        )
        return FrozenConfigSnapshot(values, generation_id=generation_id)

    def to_plain_dict(self) -> dict[str, Any]:
        values = object.__getattribute__(self, "_FrozenConfigSnapshot__values")
        return copy.deepcopy(dict(values))

    def __getattr__(self, name: str) -> Any:
        values = object.__getattribute__(self, "_FrozenConfigSnapshot__values")
        if name not in values:
            raise AttributeError(name)
        return copy.deepcopy(values[name])

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise RuntimeError("active_config_generation_is_immutable")

    def __deepcopy__(self, memo: dict[int, Any]) -> "FrozenConfigSnapshot":
        del memo
        return self


@dataclass(frozen=True, slots=True)
class PreparedConfigGeneration:
    generation_id: str
    parent_generation_id: str
    config: FrozenConfigSnapshot
    config_fingerprint: str


@dataclass(frozen=True, slots=True)
class ActiveConfigGeneration:
    generation_id: str
    config: FrozenConfigSnapshot
    config_fingerprint: str
    receipt: Mapping[str, Any]


class ActiveConfigGenerationAuthority:
    """The only live config owner and shared reader/checkpoint transaction lock."""

    owner = CONFIG_GENERATION_OWNER

    def __init__(self, bootstrap_config: Any) -> None:
        self.__lock = threading.RLock()
        config = FrozenConfigSnapshot.from_config(
            bootstrap_config,
            generation_id="bootstrap",
        )
        self.__current = ActiveConfigGeneration(
            generation_id="bootstrap",
            config=config,
            config_fingerprint=config.fingerprint,
            receipt=MappingProxyType({}),
        )
        self.__transition = MappingProxyType(
            {
                "generation_id": "bootstrap",
                "state": "active",
                "published": True,
            }
        )

    @property
    def current(self) -> ActiveConfigGeneration:
        with self.__lock:
            return self.__current

    @contextmanager
    def transaction(self):
        with self.__lock:
            yield

    @contextmanager
    def runtime_read(self):
        with self.__lock:
            yield self.__current

    def prepare(
        self,
        *,
        generation_id: str,
        parent_generation_id: str,
        updates: Mapping[str, Any],
    ) -> PreparedConfigGeneration:
        with self.__lock:
            active = self.__current
            if str(parent_generation_id or "") != (
                "" if active.generation_id == "bootstrap" else active.generation_id
            ):
                raise RuntimeError(
                    "config_generation_stale_parent:"
                    f"declared={parent_generation_id or 'bootstrap'}:"
                    f"active={active.generation_id}"
                )
            config = active.config.with_updates(
                updates,
                generation_id=generation_id,
            )
            return PreparedConfigGeneration(
                generation_id=str(generation_id),
                parent_generation_id=str(parent_generation_id or ""),
                config=config,
                config_fingerprint=config.fingerprint,
            )

    def set_transition(self, payload: Mapping[str, Any]) -> None:
        with self.__lock:
            self.__transition = MappingProxyType(
                copy.deepcopy(dict(payload or {}))
            )

    def activate(
        self,
        prepared: PreparedConfigGeneration,
        receipt: Mapping[str, Any],
    ) -> ActiveConfigGeneration:
        with self.__lock:
            if prepared.config_fingerprint != str(
                receipt.get("config_fingerprint", "") or ""
            ):
                raise RuntimeError("config_generation_activation_fingerprint_mismatch")
            current = ActiveConfigGeneration(
                generation_id=prepared.generation_id,
                config=prepared.config,
                config_fingerprint=prepared.config_fingerprint,
                receipt=MappingProxyType(copy.deepcopy(dict(receipt or {}))),
            )
            self.__current = current
            self.__transition = MappingProxyType(
                {
                    "generation_id": prepared.generation_id,
                    "parent_generation_id": prepared.parent_generation_id,
                    "state": "active",
                    "published": True,
                }
            )
            return current

    def restore(self, active: ActiveConfigGeneration, *, reason: str) -> None:
        with self.__lock:
            self.__current = active
            self.__transition = MappingProxyType(
                {
                    "generation_id": active.generation_id,
                    "state": "restored_after_failed_commit",
                    "reason": str(reason or "config_generation_failed"),
                    "published": True,
                }
            )

    def receipt(self) -> dict[str, Any]:
        with self.__lock:
            return copy.deepcopy(dict(self.__current.receipt or {}))

    def status(self) -> dict[str, Any]:
        with self.__lock:
            return {
                "owner": self.owner,
                "generation_id": self.__current.generation_id,
                "config_fingerprint": self.__current.config_fingerprint,
                "transition": copy.deepcopy(dict(self.__transition)),
            }


def require_active_config_generation_authority(
    core: Any,
) -> ActiveConfigGenerationAuthority:
    authority = getattr(core, "config_generation_authority", None)
    if not isinstance(authority, ActiveConfigGenerationAuthority):
        raise RuntimeError("active_config_generation_authority_required")
    return authority


def require_active_config_snapshot(core: Any) -> FrozenConfigSnapshot:
    """Return the only active config and reject a stale Core projection."""

    active = require_active_config_generation_authority(core).current.config
    if getattr(core, "cfg", None) is not active:
        raise RuntimeError("core_active_config_projection_stale")
    return active


def _generation_identity(data: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(data or {})
    return {
        "source_kind": str(row.get("source_kind", "") or ""),
        "profile_id": str(row.get("profile_id", "") or ""),
        "profile_version": int(row.get("profile_version", 0) or 0),
        "proposal": copy.deepcopy(dict(row.get("proposal") or {})),
        "evidence_scope": copy.deepcopy(dict(row.get("evidence_scope") or {})),
        "formula_vector": copy.deepcopy(dict(row.get("formula_vector") or {})),
        "rollback": copy.deepcopy(dict(row.get("rollback") or {})),
        "provenance": copy.deepcopy(dict(row.get("provenance") or {})),
        "restore_config_values": copy.deepcopy(
            dict(row.get("restore_config_values") or {})
        ),
        "set_config_values": copy.deepcopy(dict(row.get("set_config_values") or {})),
        "effective_config_values": copy.deepcopy(
            dict(row.get("effective_config_values") or {})
        ),
        "active_config_overrides": copy.deepcopy(
            dict(row.get("active_config_overrides") or {})
        ),
        "profile_tuning_proposals": copy.deepcopy(
            list(row.get("profile_tuning_proposals") or [])
        ),
        "runtime_policy_state": copy.deepcopy(
            dict(row.get("runtime_policy_state") or {})
        ),
        "parent_generation_id": str(row.get("parent_generation_id", "") or ""),
    }


def _config_fingerprint(effective_config_values: Mapping[str, Any]) -> str:
    return _digest(dict(effective_config_values or {}))


def _committed_parent(parent_receipt: Mapping[str, Any] | None) -> dict[str, Any]:
    parent = copy.deepcopy(dict(parent_receipt or {}))
    if not parent:
        return {}
    if str(parent.get("schema", "") or "") != CONFIG_GENERATION_RECEIPT_SCHEMA:
        raise ValueError("config_generation_parent_schema_mismatch")
    if str(parent.get("owner", "") or "") != CONFIG_GENERATION_OWNER:
        raise ValueError("config_generation_parent_owner_mismatch")
    if not bool(parent.get("committed", False)):
        raise ValueError("config_generation_parent_not_committed")
    return parent


def _validate_update_keys(
    source_kind: str,
    *,
    set_config_values: Mapping[str, Any],
    restore_config_values: Mapping[str, Any],
) -> None:
    if source_kind == CONFIG_GENERATION_SOURCE_PROFILE:
        set_allowed = PROFILE_SET_CONFIG_KEYS
        restore_allowed = PROFILE_CONFIG_KEYS
    elif source_kind == CONFIG_GENERATION_SOURCE_RUNTIME:
        set_allowed = restore_allowed = RUNTIME_TUNING_CONFIG_KEYS
    elif source_kind == CONFIG_GENERATION_SOURCE_OPERATOR:
        set_allowed = restore_allowed = OPERATOR_RUNTIME_CONFIG_KEYS
    elif source_kind == CONFIG_GENERATION_SOURCE_POLICY_OVERLAY:
        set_allowed = restore_allowed = POLICY_OVERLAY_CONFIG_KEYS
    elif source_kind == CONFIG_GENERATION_SOURCE_POLICY_TEMPLATE:
        keys = set(set_config_values) | set(restore_config_values)
        illegal_prefix = sorted(
            key
            for key in keys
            if str(key).startswith(POLICY_TEMPLATE_FORBIDDEN_PREFIXES)
            or str(key) in POLICY_TEMPLATE_FORBIDDEN_KEYS
        )
        if illegal_prefix:
            raise ValueError(
                "policy_template_modem_live_field_rejected:"
                + ",".join(illegal_prefix)
            )
        set_allowed = restore_allowed = frozenset(keys)
    else:
        raise ValueError("config_generation_source_kind_invalid")
    illegal = sorted(
        [
            *(f"set:{key}" for key in set_config_values if key not in set_allowed),
            *(
                f"restore:{key}"
                for key in restore_config_values
                if key not in restore_allowed
            ),
        ]
    )
    if illegal:
        raise ValueError(
            "config_generation_source_keys_invalid:"
            f"{source_kind}:"
            + ",".join(illegal)
        )


def _durable_updates(
    source_kind: str,
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    if source_kind != CONFIG_GENERATION_SOURCE_PROFILE:
        return copy.deepcopy(dict(updates or {}))
    blocked = set(CALIBRATION_PROFILE_BLOCKED_KEYS)
    return {
        str(key): copy.deepcopy(value)
        for key, value in dict(updates or {}).items()
        if key not in blocked
    }


def _normalize_restored_config_value(
    key: str,
    value: Any,
    *,
    current_value: Any,
) -> Any:
    """Validate one durable value against the current typed bootstrap field."""

    _json_value(value)
    if isinstance(current_value, bool):
        if not isinstance(value, bool):
            raise ValueError(f"config_generation_restored_value_type_invalid:{key}")
        return value
    if isinstance(current_value, int) and not isinstance(current_value, bool):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"config_generation_restored_value_type_invalid:{key}")
        return value
    if isinstance(current_value, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"config_generation_restored_value_type_invalid:{key}")
        restored = float(value)
        if not math.isfinite(restored):
            raise ValueError(f"config_generation_restored_value_non_finite:{key}")
        return restored
    if isinstance(current_value, str):
        if not isinstance(value, str):
            raise ValueError(f"config_generation_restored_value_type_invalid:{key}")
        return value
    if isinstance(current_value, tuple):
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"config_generation_restored_value_type_invalid:{key}")
        return tuple(copy.deepcopy(value))
    if isinstance(current_value, list):
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"config_generation_restored_value_type_invalid:{key}")
        return list(copy.deepcopy(value))
    if isinstance(current_value, frozenset):
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError(f"config_generation_restored_value_type_invalid:{key}")
        return frozenset(copy.deepcopy(value))
    if isinstance(current_value, set):
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError(f"config_generation_restored_value_type_invalid:{key}")
        return set(copy.deepcopy(value))
    if isinstance(current_value, Mapping):
        if not isinstance(value, Mapping):
            raise ValueError(f"config_generation_restored_value_type_invalid:{key}")
        return copy.deepcopy(dict(value))
    if current_value is None:
        return copy.deepcopy(value)
    if not isinstance(value, type(current_value)):
        raise ValueError(f"config_generation_restored_value_type_invalid:{key}")
    return copy.deepcopy(value)


def _validated_restored_effective_values(
    active: FrozenConfigSnapshot,
    values: Mapping[str, Any],
) -> dict[str, Any]:
    """Admit only configuration-owner values into restart activation."""

    current = active.to_plain_dict()
    restored: dict[str, Any] = {}
    for raw_key, raw_value in dict(values or {}).items():
        key = str(raw_key)
        if key not in current:
            raise ValueError(f"config_generation_restored_key_unknown:{key}")
        if key in CALIBRATION_PROFILE_BLOCKED_KEYS:
            raise ValueError(
                f"config_generation_restored_blocked_key_rejected:{key}"
            )
        if key not in FIXED_DURABLE_CONFIG_KEYS and (
            key.startswith(POLICY_TEMPLATE_FORBIDDEN_PREFIXES)
            or key in POLICY_TEMPLATE_FORBIDDEN_KEYS
        ):
            raise ValueError(
                f"config_generation_restored_non_owner_key_rejected:{key}"
            )
        if key in STARTUP_SAFETY_RESET_CONFIG_KEYS:
            raw_value = current[key]
        restored[key] = _normalize_restored_config_value(
            key,
            raw_value,
            current_value=current[key],
        )
    return restored


def _runtime_policy_state_for_restored_config(
    policy_state: Mapping[str, Any],
    effective_values: Mapping[str, Any],
) -> dict[str, Any]:
    out = copy.deepcopy(dict(policy_state or {}))
    if "sms_command_enabled" in effective_values:
        sms = copy.deepcopy(dict(out.get("sms_command_control") or {}))
        sms["enabled"] = bool(effective_values.get("sms_command_enabled"))
        out["sms_command_control"] = sms
    return out


def _build_config_generation(
    *,
    source_kind: str,
    set_config_values: Mapping[str, Any],
    restore_config_values: Mapping[str, Any] | None = None,
    proposal: Mapping[str, Any] | None = None,
    evidence_scope: Mapping[str, Any] | None = None,
    formula_vector: Mapping[str, Any] | None = None,
    rollback: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
    profile_id: str = "",
    profile_version: int = 0,
    active_config_overrides: Mapping[str, Any] | None = None,
    profile_tuning_proposals: list[Mapping[str, Any]] | None = None,
    runtime_policy_state: Mapping[str, Any] | None = None,
    parent_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one complete generation from a policy/profile application plan."""

    kind = str(source_kind or "")
    if kind not in CONFIG_GENERATION_SOURCE_KINDS:
        raise ValueError("config_generation_source_kind_invalid")
    parent = _committed_parent(parent_receipt)
    parent_generation_id = str(parent.get("generation_id", "") or "")
    restore_values = copy.deepcopy(dict(restore_config_values or {}))
    set_values = copy.deepcopy(dict(set_config_values or {}))
    updates = copy.deepcopy(restore_values)
    updates.update(copy.deepcopy(set_values))
    if not updates and kind != CONFIG_GENERATION_SOURCE_PROFILE:
        raise ValueError("config_generation_updates_required")
    _validate_update_keys(
        kind,
        set_config_values=set_values,
        restore_config_values=restore_values,
    )

    proposal_payload = copy.deepcopy(dict(proposal or {}))
    scope_payload = copy.deepcopy(
        dict(evidence_scope or proposal_payload.get("evidence_scope") or {})
    )
    formula_payload = copy.deepcopy(
        dict(
            formula_vector
            or proposal_payload.get("formula_vector")
            or scope_payload.get("formula_vector")
            or {}
        )
    )
    rollback_payload = copy.deepcopy(
        dict(rollback or proposal_payload.get("rollback") or {})
    )
    if not rollback_payload:
        rollback_payload = {
            "strategy": "restore_previous_committed_generation",
            "values": copy.deepcopy(
                {
                    key: dict(parent.get("effective_config_values") or {}).get(key)
                    for key in updates
                    if key in dict(parent.get("effective_config_values") or {})
                }
            ),
            "required_on": [
                "core_consumer_refresh_failure",
                "core_journal_failure",
                "publication_failure",
            ],
        }

    effective_values = copy.deepcopy(
        dict(parent.get("effective_config_values") or {})
    )
    if kind == CONFIG_GENERATION_SOURCE_PROFILE:
        for blocked_key in CALIBRATION_PROFILE_BLOCKED_KEYS:
            effective_values.pop(blocked_key, None)
    effective_values.update(_durable_updates(kind, updates))
    inherited_policy_state = copy.deepcopy(
        dict(parent.get("runtime_policy_state") or {})
    )
    inherited_policy_state.update(copy.deepcopy(dict(runtime_policy_state or {})))
    inherited_overrides = copy.deepcopy(
        dict(
            active_config_overrides
            if active_config_overrides is not None
            else parent.get("active_config_overrides") or {}
        )
    )
    inherited_profile_proposals = copy.deepcopy(
        list(
            profile_tuning_proposals
            if profile_tuning_proposals is not None
            else parent.get("profile_tuning_proposals") or []
        )
    )
    effective_profile_id = str(
        profile_id
        or parent.get("profile_id")
        or ""
    )
    effective_profile_version = int(
        profile_version
        if int(profile_version or 0) > 0
        else parent.get("profile_version", 0)
        or 0
    )
    provenance_payload = copy.deepcopy(dict(provenance or {}))
    seed = {
        "source_kind": kind,
        "profile_id": effective_profile_id,
        "profile_version": effective_profile_version,
        "proposal": proposal_payload,
        "evidence_scope": scope_payload,
        "formula_vector": formula_payload,
        "rollback": rollback_payload,
        "provenance": provenance_payload,
        "restore_config_values": restore_values,
        "set_config_values": set_values,
        "effective_config_values": effective_values,
        "active_config_overrides": inherited_overrides,
        "profile_tuning_proposals": inherited_profile_proposals,
        "runtime_policy_state": inherited_policy_state,
        "parent_generation_id": parent_generation_id,
    }
    generation_id = _digest(_generation_identity(seed))
    return {
        "schema": CONFIG_GENERATION_SCHEMA,
        "owner": CONFIG_GENERATION_OWNER,
        "generation_id": generation_id,
        "source_kind": seed["source_kind"],
        "profile_id": seed["profile_id"],
        "profile_version": seed["profile_version"],
        "proposal_id": str(proposal_payload.get("proposal_id", "") or ""),
        "proposal": seed["proposal"],
        "evidence_scope": seed["evidence_scope"],
        "formula_vector": seed["formula_vector"],
        "rollback": seed["rollback"],
        "provenance": seed["provenance"],
        "restore_config_values": seed["restore_config_values"],
        "set_config_values": seed["set_config_values"],
        "effective_config_values": seed["effective_config_values"],
        "active_config_overrides": seed["active_config_overrides"],
        "profile_tuning_proposals": seed["profile_tuning_proposals"],
        "runtime_policy_state": seed["runtime_policy_state"],
        "parent_generation_id": seed["parent_generation_id"],
        "created_ts": float(time.time()),
    }


def _build_profile_config_generation(
    *,
    application_plan: Mapping[str, Any],
    active_profile: Mapping[str, Any],
    parent_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project the calibration-profile planner into the generic transaction."""

    plan = dict(application_plan or {})
    profile = dict(active_profile or {})
    proposal = (
        dict(profile.get("calibration_proposal") or {})
        if isinstance(profile.get("calibration_proposal"), Mapping)
        else {}
    )
    return _build_config_generation(
        source_kind=CONFIG_GENERATION_SOURCE_PROFILE,
        set_config_values=dict(plan.get("set_config_values") or {}),
        restore_config_values=dict(plan.get("restore_config_values") or {}),
        proposal=proposal,
        provenance={
            "application_owner": str(plan.get("owner", "") or ""),
            "application_reason": str(plan.get("reason", "") or ""),
            "approved_by": str(profile.get("approved_by", "") or ""),
            "approved_ts": float(profile.get("approved_ts", 0.0) or 0.0),
            "lineage": copy.deepcopy(dict(profile.get("lineage") or {})),
            "proposal_provenance": copy.deepcopy(
                dict(proposal.get("provenance") or {})
            ),
        },
        profile_id=str(plan.get("active_profile_id", "") or ""),
        profile_version=int(plan.get("active_profile_version", 0) or 0),
        active_config_overrides=dict(plan.get("active_config_overrides") or {}),
        profile_tuning_proposals=list(
            plan.get("profile_tuning_proposals") or []
        ),
        parent_receipt=parent_receipt,
    )


def _validate_generation(generation: Mapping[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(dict(generation or {}))
    schema = str(data.get("schema", "") or "")
    if schema != CONFIG_GENERATION_SCHEMA:
        if schema in {"ConfigGeneration.v1", "ConfigGeneration.v2"}:
            raise ValueError("mutable_config_generation_schema_rejected")
        raise ValueError("config_generation_schema_required")
    if str(data.get("owner", "") or "") != CONFIG_GENERATION_OWNER:
        raise ValueError("config_generation_owner_mismatch")
    if str(data.get("source_kind", "") or "") not in CONFIG_GENERATION_SOURCE_KINDS:
        raise ValueError("config_generation_source_kind_invalid")
    generation_id = str(data.get("generation_id", "") or "")
    if not generation_id:
        raise ValueError("config_generation_id_required")
    if generation_id != _digest(_generation_identity(data)):
        raise ValueError("config_generation_id_mismatch")
    proposal = dict(data.get("proposal") or {})
    if str(data.get("proposal_id", "") or "") != str(
        proposal.get("proposal_id", "") or ""
    ):
        raise ValueError("config_generation_proposal_projection_mismatch")
    for key in ("evidence_scope", "formula_vector", "rollback"):
        if key in proposal and dict(data.get(key) or {}) != dict(proposal.get(key) or {}):
            raise ValueError(f"config_generation_{key}_projection_mismatch")
    updates = dict(data.get("restore_config_values") or {})
    updates.update(dict(data.get("set_config_values") or {}))
    effective = dict(data.get("effective_config_values") or {})
    if (
        not updates
        and str(data.get("source_kind", "") or "")
        != CONFIG_GENERATION_SOURCE_PROFILE
    ):
        raise ValueError("config_generation_updates_required")
    _validate_update_keys(
        str(data.get("source_kind", "") or ""),
        set_config_values=dict(data.get("set_config_values") or {}),
        restore_config_values=dict(data.get("restore_config_values") or {}),
    )
    durable_updates = _durable_updates(
        str(data.get("source_kind", "") or ""),
        updates,
    )
    if any(effective.get(key) != value for key, value in durable_updates.items()):
        raise ValueError("config_generation_effective_values_mismatch")
    if (
        str(data.get("source_kind", "") or "")
        == CONFIG_GENERATION_SOURCE_PROFILE
        and any(
            key in effective
            for key in CALIBRATION_PROFILE_BLOCKED_KEYS
        )
    ):
        raise ValueError("config_generation_blocked_key_cannot_be_durable")
    return data


def _validate_consumer_acks(
    acknowledgements: Mapping[str, Any],
    *,
    generation_id: str,
    config_fingerprint: str,
) -> None:
    failures = [
        name
        for name, row in dict(acknowledgements or {}).items()
        if (
            not isinstance(row, Mapping)
            or not bool(row.get("acknowledged", False))
            or str(row.get("generation_id", "") or "") != generation_id
            or str(row.get("config_fingerprint", "") or "")
            != config_fingerprint
        )
    ]
    if not acknowledgements:
        failures.append("consumer_acknowledgements_empty")
    if failures:
        raise RuntimeError(
            "config_generation_consumer_ack_failed:" + ",".join(sorted(failures))
        )


def _engine_projection(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "active_config_overrides": copy.deepcopy(
            dict(data.get("active_config_overrides") or {})
        ),
        "active_profile_version": int(data.get("profile_version", 0) or 0),
        "active_profile_id": str(data.get("profile_id", "") or ""),
    }


def _publish_engine_projection(engine: Any, data: Mapping[str, Any]) -> None:
    # Core.cfg is a read-only view of the active config owner. Publish only
    # metadata here, not a second config copy.
    projection = _engine_projection(data)
    engine._active_config_overrides = projection["active_config_overrides"]
    engine._active_calibration_profile_version = projection[
        "active_profile_version"
    ]
    engine._active_calibration_profile_id = projection["active_profile_id"]


def _commit_config_generation(
    engine: Any,
    *,
    generation: Mapping[str, Any],
    refresh_core_consumers: Callable[..., Mapping[str, Any]],
    commit_journal: Callable[[Mapping[str, Any]], bool],
) -> dict[str, Any]:
    """Atomically publish and journal one Core-owned configuration generation."""

    if not callable(commit_journal):
        raise RuntimeError("config_generation_core_journal_commit_required")
    data = _validate_generation(generation)
    generation_id = str(data["generation_id"])
    updates = dict(data.get("restore_config_values") or {})
    updates.update(dict(data.get("set_config_values") or {}))
    active_authority = require_active_config_generation_authority(engine)
    with active_authority.transaction():
        prior_active = active_authority.current
        prior_receipt = active_authority.receipt()
        if (
            str(prior_receipt.get("generation_id", "") or "") == generation_id
            and bool(prior_receipt.get("committed", False))
        ):
            out = copy.deepcopy(prior_receipt)
            out["idempotent"] = True
            return out
        prepared = active_authority.prepare(
            generation_id=generation_id,
            parent_generation_id=str(data.get("parent_generation_id", "") or ""),
            updates=updates,
        )
        prior_engine_state = {
            "active_config_overrides": copy.deepcopy(
                getattr(engine, "_active_config_overrides", {})
            ),
            "active_profile_version": int(
                getattr(engine, "_active_calibration_profile_version", 0) or 0
            ),
            "active_profile_id": str(
                getattr(engine, "_active_calibration_profile_id", "") or ""
            ),
        }
        active_authority.set_transition(
            {
                "generation_id": generation_id,
                "parent_generation_id": prepared.parent_generation_id,
                "state": "validating_core_consumers",
                "published": False,
            }
        )
        try:
            acknowledgements = dict(
                refresh_core_consumers(
                    engine,
                    config=prepared.config,
                    generation_id=generation_id,
                    config_fingerprint=prepared.config_fingerprint,
                )
                or {}
            )
            _validate_consumer_acks(
                acknowledgements,
                generation_id=generation_id,
                config_fingerprint=prepared.config_fingerprint,
            )
            receipt = {
                "schema": CONFIG_GENERATION_RECEIPT_SCHEMA,
                "owner": CONFIG_GENERATION_OWNER,
                "generation_id": generation_id,
                "parent_generation_id": prepared.parent_generation_id,
                "source_kind": str(data.get("source_kind", "") or ""),
                "profile_id": str(data.get("profile_id", "") or ""),
                "profile_version": int(data.get("profile_version", 0) or 0),
                "proposal_id": str(data.get("proposal_id", "") or ""),
                "proposal": copy.deepcopy(dict(data.get("proposal") or {})),
                "evidence_scope": copy.deepcopy(dict(data.get("evidence_scope") or {})),
                "formula_vector": copy.deepcopy(dict(data.get("formula_vector") or {})),
                "rollback": copy.deepcopy(dict(data.get("rollback") or {})),
                "provenance": copy.deepcopy(dict(data.get("provenance") or {})),
                "changed_keys": sorted(updates),
                "effective_config_values": copy.deepcopy(
                    dict(data.get("effective_config_values") or {})
                ),
                "active_config_overrides": copy.deepcopy(
                    dict(data.get("active_config_overrides") or {})
                ),
                "profile_tuning_proposals": copy.deepcopy(
                    list(data.get("profile_tuning_proposals") or [])
                ),
                "runtime_policy_state": copy.deepcopy(
                    dict(data.get("runtime_policy_state") or {})
                ),
                "prior_config_fingerprint": prior_active.config_fingerprint,
                "config_fingerprint": prepared.config_fingerprint,
                "consumer_acknowledgements": acknowledgements,
                "authority_acknowledgement": {
                    "acknowledged": True,
                    "owner": CONFIG_GENERATION_OWNER,
                    "generation_id": generation_id,
                    "config_fingerprint": prepared.config_fingerprint,
                },
                "transaction_state": "committed",
                "committed": True,
                "committed_ts": float(time.time()),
                "idempotent": False,
            }
            active_authority.activate(prepared, receipt)
            _publish_engine_projection(engine, receipt)
            active_authority.set_transition(
                {
                    "generation_id": generation_id,
                    "parent_generation_id": prepared.parent_generation_id,
                    "state": "journaling",
                    "published": True,
                }
            )
            if not bool(commit_journal(receipt)):
                raise RuntimeError(
                    "config_generation_core_journal_not_committed"
                )
            active_authority.set_transition(
                {
                    "generation_id": generation_id,
                    "parent_generation_id": prepared.parent_generation_id,
                    "state": "committed",
                    "published": True,
                }
            )
            return receipt
        except Exception as exc:
            rollback_errors: list[str] = []
            try:
                active_authority.restore(prior_active, reason=str(exc))
                _publish_engine_projection(engine, prior_receipt)
                refresh_core_consumers(
                    engine,
                    config=prior_active.config,
                    generation_id=prior_active.generation_id,
                    config_fingerprint=prior_active.config_fingerprint,
                )
            except Exception as rollback_exc:
                rollback_errors.append(
                    "consumer_restore:"
                    f"{type(rollback_exc).__name__}:{rollback_exc}"
                )
            engine._active_config_overrides = prior_engine_state[
                "active_config_overrides"
            ]
            engine._active_calibration_profile_version = prior_engine_state[
                "active_profile_version"
            ]
            engine._active_calibration_profile_id = prior_engine_state[
                "active_profile_id"
            ]
            suffix = (
                ":rollback_failed:" + ";".join(rollback_errors)
                if rollback_errors
                else ""
            )
            raise RuntimeError(
                f"config_generation_failed:{exc}{suffix}"
            ) from exc


def _restore_config_generation(
    engine: Any,
    *,
    receipt: Mapping[str, Any],
    refresh_core_consumers: Callable[..., Mapping[str, Any]],
    commit_journal: Callable[[Mapping[str, Any]], bool],
) -> bool:
    """Restore the journal-validated Core generation during startup."""

    restored_receipt = copy.deepcopy(dict(receipt or {}))
    if not restored_receipt:
        return False
    schema = str(restored_receipt.get("schema", "") or "")
    if schema in {"ConfigGenerationReceipt.v1", "ConfigGenerationReceipt.v2"}:
        raise ValueError("mutable_config_generation_receipt_rejected")
    if schema != CONFIG_GENERATION_RECEIPT_SCHEMA:
        raise ValueError("config_generation_receipt_schema_required")
    if str(restored_receipt.get("owner", "") or "") != CONFIG_GENERATION_OWNER:
        raise ValueError("config_generation_receipt_owner_mismatch")
    if not bool(restored_receipt.get("committed", False)):
        raise ValueError("config_generation_receipt_not_committed")
    generation_id = str(restored_receipt.get("generation_id", "") or "")
    effective_values = copy.deepcopy(
        dict(restored_receipt.get("effective_config_values") or {})
    )
    if not generation_id:
        raise ValueError("config_generation_receipt_incomplete")
    if not callable(commit_journal):
        raise RuntimeError("config_generation_core_journal_commit_required")
    active_authority = require_active_config_generation_authority(engine)
    with active_authority.transaction():
        prior_active = active_authority.current
        prior_receipt = active_authority.receipt()
        effective_values = _validated_restored_effective_values(
            prior_active.config,
            effective_values,
        )
        prepared = active_authority.prepare(
            generation_id=generation_id,
            parent_generation_id="",
            updates=effective_values,
        )
        expected_fingerprint = prepared.config_fingerprint
        previous_activation_fingerprint = str(
            restored_receipt.get("config_fingerprint", "") or ""
        )
        try:
            acknowledgements = dict(
                refresh_core_consumers(
                    engine,
                    config=prepared.config,
                    generation_id=generation_id,
                    config_fingerprint=expected_fingerprint,
                )
                or {}
            )
            _validate_consumer_acks(
                acknowledgements,
                generation_id=generation_id,
                config_fingerprint=expected_fingerprint,
            )
            restored = copy.deepcopy(restored_receipt)
            restored["effective_config_values"] = copy.deepcopy(effective_values)
            restored["runtime_policy_state"] = _runtime_policy_state_for_restored_config(
                restored.get("runtime_policy_state") or {},
                effective_values,
            )
            restored["previous_activation_config_fingerprint"] = (
                previous_activation_fingerprint
            )
            restored["config_fingerprint"] = expected_fingerprint
            restored["consumer_acknowledgements"] = acknowledgements
            restored["authority_acknowledgement"] = {
                "acknowledged": True,
                "owner": CONFIG_GENERATION_OWNER,
                "generation_id": generation_id,
                "config_fingerprint": expected_fingerprint,
            }
            restored["restored_at_startup"] = True
            restored["activation_fingerprint_refreshed"] = (
                previous_activation_fingerprint != expected_fingerprint
            )
            restored["activated_ts"] = float(time.time())
            restored["idempotent"] = True
            active_authority.activate(prepared, restored)
            _publish_engine_projection(engine, restored)
            if previous_activation_fingerprint != expected_fingerprint:
                active_authority.set_transition(
                    {
                        "generation_id": generation_id,
                        "parent_generation_id": str(
                            restored.get("parent_generation_id", "") or ""
                        ),
                        "state": "refreshing_activation_receipt",
                        "published": True,
                    }
                )
                if not bool(commit_journal(restored)):
                    raise RuntimeError(
                        "config_generation_activation_journal_not_committed"
                    )
            active_authority.set_transition(
                {
                    "generation_id": generation_id,
                    "parent_generation_id": str(
                        restored.get("parent_generation_id", "") or ""
                    ),
                    "state": "restored",
                    "published": True,
                }
            )
            return True
        except Exception as exc:
            active_authority.restore(
                prior_active,
                reason="startup_generation_restore_failed",
            )
            _publish_engine_projection(engine, prior_receipt)
            try:
                refresh_core_consumers(
                    engine,
                    config=prior_active.config,
                    generation_id=prior_active.generation_id,
                    config_fingerprint=prior_active.config_fingerprint,
                )
            except Exception as rollback_exc:
                raise RuntimeError(
                    "config_generation_startup_restore_failed:"
                    f"{type(exc).__name__}:{exc}:rollback_failed:"
                    f"{type(rollback_exc).__name__}:{rollback_exc}"
                ) from exc
            raise


__all__ = [
    "ActiveConfigGenerationAuthority",
    "CONFIG_GENERATION_OWNER",
    "CONFIG_GENERATION_RECEIPT_SCHEMA",
    "CONFIG_GENERATION_SCHEMA",
    "CONFIG_GENERATION_SOURCE_OPERATOR",
    "CONFIG_GENERATION_SOURCE_POLICY_OVERLAY",
    "CONFIG_GENERATION_SOURCE_POLICY_TEMPLATE",
    "CONFIG_GENERATION_SOURCE_PROFILE",
    "CONFIG_GENERATION_SOURCE_RUNTIME",
    "FrozenConfigSnapshot",
    "PreparedConfigGeneration",
    "PROFILE_CONFIG_KEYS",
    "PROFILE_SET_CONFIG_KEYS",
    "RUNTIME_TUNING_CONFIG_KEYS",
    "STARTUP_SAFETY_RESET_CONFIG_KEYS",
    "require_active_config_generation_authority",
    "require_active_config_snapshot",
]
