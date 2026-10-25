from __future__ import annotations

import re
from typing import Tuple


def parse_version_key(text: str | None) -> Tuple[int, ...]:
    """Parse dotted and compact 54GUC version tags into a sortable tuple.

    Supported examples:
    - ``V4.45`` -> ``(4, 45)``
    - ``V445`` -> ``(4, 45)``
    - ``v7.10-beta`` -> ``(7, 10)``
    - ``LUC_v6.93`` -> ``(6, 93)``
    - ``54GUC_R14.2`` -> ``(14, 2)``
    """
    raw = str(text or "").strip()
    if not raw:
        return (0,)

    raw = re.sub(r"(?i)^(?:54GUC|CGF)_R(?=\d+\.)", "", raw)

    exact_dotted = re.fullmatch(r"(?i)V?(\d+)\.(\d+)(?:\.(\d+))?(?:[-+].*)?", raw)
    if exact_dotted:
        parts = [int(exact_dotted.group(1)), int(exact_dotted.group(2))]
        if exact_dotted.group(3) is not None:
            parts.append(int(exact_dotted.group(3)))
        return tuple(parts)

    exact_compact = re.fullmatch(r"(?i)V?(\d{3,})", raw)
    if exact_compact:
        compact = exact_compact.group(1)
        return int(compact[0]), int(compact[1:])

    embedded_dotted = re.search(r"(?i)V(\d+)\.(\d+)(?:\.(\d+))?", raw)
    if embedded_dotted:
        parts = [int(embedded_dotted.group(1)), int(embedded_dotted.group(2))]
        if embedded_dotted.group(3) is not None:
            parts.append(int(embedded_dotted.group(3)))
        return tuple(parts)

    embedded_compact = re.search(r"(?i)V(\d{3,})", raw)
    if embedded_compact:
        compact = embedded_compact.group(1)
        return int(compact[0]), int(compact[1:])

    try:
        return tuple(int(part) for part in raw.split("."))
    except (ValueError, TypeError):
        parsed_numbers = tuple(int(part) for part in re.findall(r"\d+", raw))
        return parsed_numbers or (0,)
