"""Optional operator hints. Valid syntax is not verified network evidence."""
from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class NetworkSuggestions:
    country: str = ""
    provider: str = ""
    bands: str = ""

    def __post_init__(self):
        for name in ("country", "provider", "bands"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > 256 or any(ord(c) < 32 for c in value):
                raise ValueError(f"network_{name}_suggestion_invalid")
            object.__setattr__(self, name, value.strip())
        self.band_items()

    def band_items(self) -> tuple[tuple[str, int], ...]:
        values = set()
        for part in filter(None, re.split(r"[,+;\s]+", self.bands)):
            match = re.fullmatch(r"([bBnN]?)([0-9]{1,4})", part)
            if not match or not 0 < int(match[2]) <= 1024:
                raise ValueError("Use bands such as B1,B3,n78")
            values.add(("nr" if match[1].lower() == "n" else "lte", int(match[2])))
        return tuple(sorted(values))
