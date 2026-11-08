"""Non-blocking ID audit at ModemLive's last native acquisition boundary.

No cache, polling, profile policy, RF changes or historical repair lives here.
Native adapters correct their formats first; this audit uses only evidence
marked acquired by this exact receipt. Raw unresolved input is retained here;
LiveRegisterStore alone decides whether it qualifies as serving identity.
"""
from __future__ import annotations

import copy
from itertools import count
import math

from arch4.modem.backend_diagnostic import ModemBackendDiagnostic
from arch4.modem.lte_frequency import lte_channel_from_reported_value, lte_earfcn_bounds


_SEQUENCE = count(1)  # Diagnostic deduplication identity, never radio truth.


def identity_diagnostic(*, backend, observed_at, source, evidence,
                        severity="WARNING", kind="radio_identity_inconsistency"):
    return ModemBackendDiagnostic(
        owner="54guc.modem_live.radio_identity", sequence=next(_SEQUENCE),
        observed_at=observed_at, kind=kind,
        backend=backend, severity=severity,
        data={"source": source, **evidence},
    ).to_payload()


def channel_disagrees(band, channel, *, uplink=False):
    # Zero is the typed missing uplink channel. Downlink zero is valid in B1.
    if uplink and channel == 0:
        return False
    bounds = lte_earfcn_bounds(band, uplink=uplink)
    return bounds is not None and type(channel) is int and channel >= 0 and not bounds[0] <= channel <= bounds[1]


def audit_backend_identity(*, request, backend, profile_id, modem, status, acquired_cells):
    """Return detached corrections/diagnostic; never certify unacquired fields."""
    diagnostic = status.get("backend_diagnostic")
    if ("slow.backend_diagnostic" in acquired_cells and isinstance(diagnostic, dict)
            and diagnostic.get("owner") == "54guc.modem_live.radio_identity"):
        status = dict(status)
        status["backend_diagnostic"] = {**diagnostic, "data": {
            **diagnostic["data"], "profile_id": profile_id,
            "attempt_id": request.attempt_id, "lane": request.lane,
            "started_at": request.started_at}}
    def fresh(path, attribute, *aliases):
        if path not in acquired_cells:
            return None
        # Radio translation prefers the sample; slow frequencies are status
        # authority. Read only already canonical values, not new vendor aliases.
        if path.startswith(("rf.", "radio.")) and modem is not None:
            value = getattr(modem, attribute, None)
            if type(value) is int and value > 0:
                return value
        for alias in aliases:
            if status.get(alias) is not None:
                return status[alias]
        return getattr(modem, attribute, None) if modem is not None else None

    band = fresh("radio.serving_band", "primary_band", "serving_band", "primary_band", "band")
    original = {
        "band": band,
        "cell_id": fresh("rf.cell_id", "cell_id", "cell_id"),
        "pci": fresh("rf.pci", "pci", "pci"),
        "earfcn_dl": fresh("rf.earfcn_dl", "earfcn_dl", "earfcn_dl", "earfcn"),
        "earfcn_ul": fresh("rf.earfcn_ul", "earfcn_ul", "earfcn_ul"),
    }
    published = dict(original)
    issues, frequencies = [], {}
    if type(band) is int and band > 0:
        for suffix in ("dl", "ul"):
            field = f"earfcn_{suffix}"
            channel = original[field]
            if type(channel) is not int or channel < 0:
                continue
            frequency = fresh(f"slow.{suffix}_freq_mhz", f"{suffix}_freq_mhz", f"{suffix}_freq_mhz")
            token = 0
            if type(frequency) in (int, float) and math.isfinite(frequency) and frequency > 0:
                scaled = frequency * 10
                if abs(scaled - round(scaled)) < 1e-6:
                    token = round(scaled)
                    frequencies[suffix] = frequency
            corrected = lte_channel_from_reported_value(band, channel, token, uplink=suffix == "ul")
            published[field] = corrected
            if corrected != channel:
                issues.append(f"{field}_frequency_token_corrected")
            elif channel_disagrees(band, channel, uplink=suffix == "ul"):
                issues.append(f"{field}_band_disagreement_unresolved")
    for field, limit in (("pci", 504), ("cell_id", 1 << 28)):
        value = original[field]
        if type(value) is int and not 0 <= value < limit:
            issues.append(f"{field}_out_of_range_unresolved")
    if not issues:
        return modem, status, acquired_cells
    status = dict(status)
    if published != original:
        modem = copy.deepcopy(modem)
        for field in ("earfcn_dl", "earfcn_ul"):
            if published[field] != original[field]:
                status[field] = published[field]
                if field == "earfcn_dl" and "earfcn" in status:
                    status["earfcn"] = published[field]
                if modem is not None:
                    setattr(modem, field, published[field])
    evidence = dict(profile_id=profile_id, attempt_id=request.attempt_id,
                    lane=request.lane, started_at=request.started_at,
                    original=original, published=published, issues=issues,
                    fresh_frequency_mhz=frequencies)
    if "slow.backend_diagnostic" in acquired_cells and status.get("backend_diagnostic"):
        evidence["native_diagnostic"] = status["backend_diagnostic"]
    status["backend_diagnostic"] = identity_diagnostic(
        backend=backend, observed_at=request.started_at, source="modem_live_last_boundary", evidence=evidence)
    return modem, status, acquired_cells | frozenset({"slow.backend_diagnostic"})
