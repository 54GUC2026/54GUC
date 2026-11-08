"""Typed persistence codec at the raw-to-accepted generation boundary."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
import hashlib
import json
from types import SimpleNamespace
from typing import Any, Mapping

from arch4.accepted_modem_generation import (
    AcceptedModemGeneration,
)
from arch4.interfaces import CarrierInfo, ComputedSignals


_TYPED_DATACLASSES = {
    "ComputedSignals": ComputedSignals,
    "CarrierInfo": CarrierInfo,
}


def encode_accepted_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): encode_accepted_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [encode_accepted_value(item) for item in value]
    if isinstance(value, tuple):
        return {
            "__type__": "tuple",
            "items": [encode_accepted_value(item) for item in value],
        }
    if isinstance(value, SimpleNamespace):
        return {
            "__type__": "SimpleNamespace",
            "fields": encode_accepted_value(vars(value)),
        }
    if is_dataclass(value):
        name = type(value).__name__
        if name not in _TYPED_DATACLASSES:
            raise TypeError(f"unsupported_accepted_generation_dataclass:{name}")
        return {
            "__type__": name,
            "fields": {
                field.name: encode_accepted_value(getattr(value, field.name))
                for field in fields(value)
            },
        }
    raise TypeError(f"unsupported_accepted_generation_value:{type(value).__name__}")


def decode_accepted_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [decode_accepted_value(item) for item in value]
    if not isinstance(value, Mapping):
        raise TypeError("accepted_generation_encoded_value_invalid")
    row = dict(value)
    type_name = str(row.get("__type__") or "")
    if not type_name:
        return {str(key): decode_accepted_value(item) for key, item in row.items()}
    if type_name == "tuple":
        if set(row) != {"__type__", "items"}:
            raise ValueError(
                "accepted_generation_tuple_fields_invalid"
            )
        return tuple(decode_accepted_value(item) for item in list(row.get("items") or []))
    if type_name == "enum_value":
        raise ValueError(
            "accepted_generation_untyped_enum_forbidden"
        )
    if set(row) != {"__type__", "fields"}:
        raise ValueError("accepted_generation_typed_fields_invalid")
    field_values = decode_accepted_value(row.get("fields") or {})
    if not isinstance(field_values, Mapping):
        raise TypeError("accepted_generation_encoded_fields_invalid")
    if type_name == "SimpleNamespace":
        return SimpleNamespace(**dict(field_values))
    cls = _TYPED_DATACLASSES.get(type_name)
    if cls is None:
        raise TypeError(f"accepted_generation_encoded_type_unknown:{type_name}")
    allowed = {field.name for field in fields(cls)}
    if set(field_values) != allowed:
        raise ValueError(
            f"accepted_generation_{type_name}_field_set_invalid"
        )
    kwargs = dict(field_values)
    return cls(**kwargs)


def accepted_generation_to_dict(accepted: AcceptedModemGeneration) -> dict[str, Any]:
    raw = accepted.raw
    return {
        "raw": {
            "matrix_identity": raw.matrix_identity.as_dict(),
            "sampled_at": raw.sampled_at,
            "published_at": raw.published_at,
            "source": raw.source,
            "truth_matrix": encode_accepted_value(raw.truth_matrix_copy()),
        },
        "accepted_at": accepted.accepted_at,
        "computed": encode_accepted_value(accepted.computed_copy()),
        "matrix_rtt": accepted.matrix_rtt.as_dict(),
    }


def accepted_generation_from_dict(raw: Mapping[str, Any]) -> AcceptedModemGeneration:
    del raw
    raise RuntimeError(
        "accepted_generation_restore_forbidden_fresh_modem_live_matrix_required"
    )


def accepted_generation_fingerprint(accepted: AcceptedModemGeneration) -> str:
    payload = accepted_generation_to_dict(accepted)
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = (
    "accepted_generation_fingerprint",
    "accepted_generation_from_dict",
    "accepted_generation_to_dict",
    "decode_accepted_value",
    "encode_accepted_value",
)
