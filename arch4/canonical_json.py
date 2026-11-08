"""One strict JSON representation boundary for CGF contract identities."""

from __future__ import annotations

import json
import math
from typing import Any, Mapping


def canonical_json_value(value: Any, *, path: str = "$") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(
                f"not JSON serializable: {path} contains a non-finite number"
            )
        return value
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    "not JSON serializable: "
                    f"{path} contains a non-string mapping key"
                )
            if key in output:
                raise TypeError(
                    "not JSON serializable: "
                    f"{path} contains a duplicate mapping key: {key}"
                )
            output[key] = canonical_json_value(
                item,
                path=f"{path}.{key}",
            )
        return output
    if isinstance(value, (list, tuple)):
        return [
            canonical_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(
        "not JSON serializable: "
        f"{path} contains unsupported value type {type(value).__name__}"
    )


def canonical_json_bytes(value: Any) -> bytes:
    canonical = canonical_json_value(value)
    return json.dumps(
        canonical,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


__all__ = (
    "canonical_json_bytes",
    "canonical_json_text",
    "canonical_json_value",
)
