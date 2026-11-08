"""Bound retained Python object graphs before copy, queue, or serialization."""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Mapping
from typing import Any


class PayloadBudgetExceeded(MemoryError):
    """Raised before an object graph can exceed a declared memory boundary."""


def retained_payload_bytes(
    value: Any,
    *,
    max_bytes: int,
    label: str,
    max_depth: int = 256,
    max_items: int = 100_000,
) -> int:
    """Measure retained object bytes incrementally and fail at the boundary."""

    limit = max(1, int(max_bytes))
    seen: set[int] = set()
    stack: list[tuple[Any, int]] = [(value, 0)]
    total = 0
    items = 0
    while stack:
        current, depth = stack.pop()
        if current is None or isinstance(current, (bool, int, float)):
            total += sys.getsizeof(current)
        elif isinstance(current, (str, bytes, bytearray, memoryview)):
            total += sys.getsizeof(current)
        else:
            object_id = id(current)
            if object_id in seen:
                continue
            seen.add(object_id)
            total += sys.getsizeof(current)
            if depth >= max_depth:
                raise PayloadBudgetExceeded(
                    f"{label} exceeds traversal depth {max_depth}"
                )
            if isinstance(current, Mapping):
                for key, item in current.items():
                    stack.append((key, depth + 1))
                    stack.append((item, depth + 1))
            elif isinstance(current, (list, tuple, set, frozenset)):
                for item in current:
                    stack.append((item, depth + 1))
            elif dataclasses.is_dataclass(current):
                for field in dataclasses.fields(current):
                    stack.append((getattr(current, field.name), depth + 1))
            elif hasattr(current, "__dict__"):
                stack.append((vars(current), depth + 1))
        items += 1
        if items > max_items:
            raise PayloadBudgetExceeded(
                f"{label} exceeds traversal item limit {max_items}"
            )
        if total > limit:
            raise PayloadBudgetExceeded(
                f"{label} retained bytes {total} exceed limit {limit}"
            )
    return int(total)


__all__ = ["PayloadBudgetExceeded", "retained_payload_bytes"]
