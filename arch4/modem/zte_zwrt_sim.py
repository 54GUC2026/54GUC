"""One boundary interpretation of fresh ZWRT active-SIM replies.

Accept spelling/case/separator and lossless scalar variations, never an
arbitrary '*imsi*' match that could identify an inactive slot. No I/O or cache.
"""
from collections.abc import Mapping
import re


def _key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _unique(sources, aliases, pattern):
    values = set()
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key, value in source.items():
            if _key(key) not in aliases or type(value) not in (str, int):
                continue
            text = str(value).strip()
            if re.fullmatch(pattern, text) and set(text) != {"0"}:
                values.add(text)
    return next(iter(values)) if len(values) == 1 else ""


def active_sim(*sources):
    """Sources must be fresh active-SIM/metadata replies from one acquisition.

    Conflicting active identities are unavailable, not first-match wins. Slot
    lists and SIM1/SIM2 fields require an explicit slot-selection adapter and
    are deliberately not recursively searched here.
    """
    states = set()
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key, value in source.items():
            if _key(key) not in {"simstatus", "simstates", "cardstate"}:
                continue
            state = _key(value)
            normalized = {
                "ready": "ready", "simready": "ready",
                "pin": "pin", "simpin": "pin", "pinrequired": "pin",
                "puk": "puk", "simpuk": "puk", "pukrequired": "puk",
                "absent": "absent", "nosim": "absent", "simabsent": "absent",
            }.get(state)
            if normalized:
                states.add(normalized)
    status = next(iter(states)) if len(states) == 1 else ""
    imsi = _unique(sources, {"imsi", "simimsi"}, r"[0-9]{14,16}")
    iccid = _unique(sources, {"iccid", "simiccid", "simid"}, r"[0-9]{18,22}")
    if status in {"pin", "puk", "absent"} or len(states) > 1:
        imsi = ""
    return {"imsi": imsi, "iccid": iccid, "status": status}
