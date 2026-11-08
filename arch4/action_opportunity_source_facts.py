"""Typed, immutable source facts for the complete action-opportunity ledger.

Facts describe owner-published constraints, candidates, schedules, leases and
observations.  They are not actions and cannot select, execute, grade, acquire
a mutation lease or communicate with Modem Live.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from arch4.core.behavior_disposition import (
    behavior_disposition_catalog,
)


OWNER = "arch4.action_opportunity_source_facts"
FACT_SCHEMA = "ActionOpportunitySourceFact.v1"
FACT_SET_SCHEMA = "ActionOpportunitySourceFactSet.v1"

_FACT_KINDS = frozenset(
    {"constraint", "candidate", "schedule", "lease", "observation"}
)
_OWNER_CLASSES = frozenset(
    {"optimization", "policy", "maintenance", "recovery", "service", "operator"}
)
_SYSTEM_BEHAVIOR_IDS = frozenset(
    {
        "smart_loop_global_action_availability",
    }
)


class _FrozenDict(dict[str, Any]):
    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("action_opportunity_source_fact_is_immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


@dataclass(frozen=True, slots=True)
class ActionOpportunitySourceFact:
    behavior_id: str
    fact_kind: str
    source_owner: str
    state_id: str
    active: bool
    reason: str
    lever: str = ""
    action: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    owner_class: str = "policy"
    source_admitted: bool = False
    source_unavailability_reason: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    schedule: Mapping[str, Any] = field(default_factory=dict)
    applies_to: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("params", "evidence", "schedule", "applies_to"):
            object.__setattr__(
                self,
                name,
                _freeze_json_mapping(_jsonable_mapping(getattr(self, name))),
            )

    def as_dict(self) -> dict[str, Any]:
        row = asdict(self)
        for name in ("params", "evidence", "schedule", "applies_to"):
            row[name] = _jsonable_mapping(getattr(self, name))
        content = {
            "schema": FACT_SCHEMA,
            "owner": OWNER,
            **row,
        }
        return {**content, "fact_id": _digest(content)}


@dataclass(frozen=True, slots=True)
class ActionOpportunitySourceFactSet:
    identity_code: str
    session_scope_key: str
    source_epoch: str
    source_sequence: int
    facts: Sequence[ActionOpportunitySourceFact] = ()

    def __post_init__(self) -> None:
        values = tuple(self.facts or ())
        if any(not isinstance(value, ActionOpportunitySourceFact) for value in values):
            raise TypeError("action_opportunity_source_fact_required")
        object.__setattr__(
            self,
            "facts",
            tuple(
                sorted(
                    values,
                    key=lambda value: (
                        value.behavior_id,
                        value.fact_kind,
                        value.state_id,
                        value.as_dict()["fact_id"],
                    ),
                )
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        content = {
            "schema": FACT_SET_SCHEMA,
            "owner": OWNER,
            "identity_code": str(self.identity_code or ""),
            "session_scope_key": str(self.session_scope_key or ""),
            "source_epoch": str(self.source_epoch or ""),
            "source_sequence": int(self.source_sequence or 0),
            "facts": [value.as_dict() for value in self.facts],
        }
        return {**content, "fact_set_id": _digest(content)}


def empty_action_opportunity_source_fact_set(
    *,
    identity_code: str,
    session_scope_key: str,
    source_epoch: str,
    source_sequence: int,
) -> ActionOpportunitySourceFactSet:
    return ActionOpportunitySourceFactSet(
        identity_code=str(identity_code or ""),
        session_scope_key=str(session_scope_key or ""),
        source_epoch=str(source_epoch or ""),
        source_sequence=int(source_sequence or 0),
    )


def action_opportunity_source_fact_error(
    value: Mapping[str, Any] | None,
) -> str:
    row = dict(value or {})
    if row.get("schema") != FACT_SCHEMA or row.get("owner") != OWNER:
        return "source_fact_owner_schema_invalid"
    content = {key: item for key, item in row.items() if key != "fact_id"}
    if not str(row.get("fact_id") or "") or str(row.get("fact_id")) != _digest(
        content
    ):
        return "source_fact_checksum_invalid"
    behavior_id = str(row.get("behavior_id") or "")
    if behavior_id not in _catalog_behavior_ids():
        return "source_fact_behavior_unknown"
    if str(row.get("fact_kind") or "") not in _FACT_KINDS:
        return "source_fact_kind_invalid"
    if not str(row.get("source_owner") or ""):
        return "source_fact_owner_required"
    if not str(row.get("state_id") or ""):
        return "source_fact_state_id_required"
    owner_class = str(row.get("owner_class") or "")
    if owner_class not in _OWNER_CLASSES:
        return "source_fact_owner_class_invalid"
    if row.get("active") is not True and row.get("active") is not False:
        return "source_fact_active_invalid"
    if row.get("source_admitted") is not True and row.get("source_admitted") is not False:
        return "source_fact_admission_invalid"
    for name in ("params", "evidence", "schedule", "applies_to"):
        if not isinstance(row.get(name), Mapping):
            return f"source_fact_{name}_invalid"
        error = _json_contract_error(row.get(name))
        if error:
            return f"source_fact_{name}_invalid:{error}"
    if str(row.get("fact_kind") or "") in {"candidate", "schedule", "lease"}:
        if not str(row.get("lever") or ""):
            return "source_fact_lever_required"
        if not str(row.get("action") or ""):
            return "source_fact_action_required"
    return ""


def action_opportunity_source_fact_set_error(
    value: Mapping[str, Any] | None,
) -> str:
    row = dict(value or {})
    if row.get("schema") != FACT_SET_SCHEMA or row.get("owner") != OWNER:
        return "source_fact_set_owner_schema_invalid"
    content = {key: item for key, item in row.items() if key != "fact_set_id"}
    if not str(row.get("fact_set_id") or "") or str(
        row.get("fact_set_id")
    ) != _digest(content):
        return "source_fact_set_checksum_invalid"
    if int(row.get("source_sequence") or 0) < 0:
        return "source_fact_set_source_sequence_invalid"
    values = row.get("facts")
    if not isinstance(values, list):
        return "source_fact_set_facts_invalid"
    keys: list[tuple[str, str, str, str]] = []
    for value_row in values:
        if not isinstance(value_row, Mapping):
            return "source_fact_set_fact_invalid"
        error = action_opportunity_source_fact_error(value_row)
        if error:
            return f"source_fact_set_fact_invalid:{error}"
        keys.append(
            (
                str(value_row.get("behavior_id") or ""),
                str(value_row.get("fact_kind") or ""),
                str(value_row.get("state_id") or ""),
                str(value_row.get("fact_id") or ""),
            )
        )
    if keys != sorted(keys):
        return "source_fact_set_order_invalid"
    if len({str(value.get("fact_id") or "") for value in values}) != len(values):
        return "source_fact_set_duplicate_fact"
    return ""


def action_opportunity_source_fact_from_mapping(
    value: Mapping[str, Any],
) -> ActionOpportunitySourceFact:
    """Cross a serialized fact boundary through the one typed constructor."""

    row = dict(value or {})
    error = action_opportunity_source_fact_error(row)
    if error:
        raise ValueError(f"action_opportunity_source_fact_invalid:{error}")
    return ActionOpportunitySourceFact(
        behavior_id=str(row.get("behavior_id") or ""),
        fact_kind=str(row.get("fact_kind") or ""),
        source_owner=str(row.get("source_owner") or ""),
        state_id=str(row.get("state_id") or ""),
        active=bool(row.get("active")),
        reason=str(row.get("reason") or ""),
        lever=str(row.get("lever") or ""),
        action=str(row.get("action") or ""),
        params=dict(row.get("params") or {}),
        owner_class=str(row.get("owner_class") or ""),
        source_admitted=bool(row.get("source_admitted")),
        source_unavailability_reason=str(
            row.get("source_unavailability_reason") or ""
        ),
        evidence=dict(row.get("evidence") or {}),
        schedule=dict(row.get("schedule") or {}),
        applies_to=dict(row.get("applies_to") or {}),
    )


def action_opportunity_source_fact_set_from_mapping(
    value: Mapping[str, Any],
) -> ActionOpportunitySourceFactSet:
    """Validate and rehydrate one immutable source-fact set."""

    row = dict(value or {})
    error = action_opportunity_source_fact_set_error(row)
    if error:
        raise ValueError(f"action_opportunity_source_fact_set_invalid:{error}")
    return ActionOpportunitySourceFactSet(
        identity_code=str(row.get("identity_code") or ""),
        session_scope_key=str(row.get("session_scope_key") or ""),
        source_epoch=str(row.get("source_epoch") or ""),
        source_sequence=int(row.get("source_sequence") or 0),
        facts=tuple(
            action_opportunity_source_fact_from_mapping(item)
            for item in list(row.get("facts") or ())
        ),
    )


def _catalog_behavior_ids() -> frozenset[str]:
    catalog = behavior_disposition_catalog()
    return frozenset(
        str(row.get("behavior_id") or "")
        for row in catalog.get("rows", [])
        if isinstance(row, Mapping)
    ) | _SYSTEM_BEHAVIOR_IDS


def _jsonable_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value or {})
    error = _json_contract_error(row)
    if error:
        raise ValueError(f"action_opportunity_source_fact_non_json:{error}")
    return json.loads(
        json.dumps(
            row,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    )


def _freeze_json_mapping(value: Mapping[str, Any]) -> _FrozenDict:
    return _FrozenDict(
        {
            str(key): _freeze_json_value(item)
            for key, item in dict(value or {}).items()
        }
    )


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_json_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _json_contract_error(value: Any, *, path: str = "$") -> str:
    if value is None or isinstance(value, (str, bool, int)):
        return ""
    if isinstance(value, float):
        return "" if math.isfinite(value) else f"{path}:non_finite_float"
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                return f"{path}:non_string_key:{type(key).__name__}"
            error = _json_contract_error(item, path=f"{path}.{key}")
            if error:
                return error
        return ""
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            error = _json_contract_error(item, path=f"{path}[{index}]")
            if error:
                return error
        return ""
    return f"{path}:unsupported_type:{type(value).__name__}"


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value or {}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "ActionOpportunitySourceFact",
    "ActionOpportunitySourceFactSet",
    "FACT_SCHEMA",
    "FACT_SET_SCHEMA",
    "OWNER",
    "action_opportunity_source_fact_error",
    "action_opportunity_source_fact_from_mapping",
    "action_opportunity_source_fact_set_error",
    "action_opportunity_source_fact_set_from_mapping",
    "empty_action_opportunity_source_fact_set",
]
