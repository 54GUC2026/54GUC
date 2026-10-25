"""Publish supported bands for the current Modem Live session.

The selected profile provides the initial value. A protected, non-empty backend
read replaces it for the session. Consumers see one supported-band value. The
profile-to-live comparison is diagnostic only. This module does no modem I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from typing import Any, Mapping


SCHEMA = "ModemLiveSessionBandCapability.v2"
OWNER = "54guc.modem_live.session_band_capability"
REVISION = 2
_VALID_STATUS = frozenset({"proven", "seeded", "unavailable", "unsupported"})
_VALID_PROFILE_DECLARATION = frozenset(
    {"declared", "unsupported", "live_required"}
)


class SessionBandCapabilityError(ValueError):
    """Raised when a session-capability contract is invalid."""


def normalize_capability_bands(value: Any) -> tuple[int, ...]:
    """Validate capability IDs once, before publication or durable overlay use."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise SessionBandCapabilityError("session_band_capability_list_required")
    out: set[int] = set()
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise SessionBandCapabilityError("session_band_capability_band_invalid")
        try:
            band = int(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SessionBandCapabilityError(
                "session_band_capability_band_invalid"
            ) from exc
        if band <= 0 or (isinstance(raw, float) and raw != band):
            raise SessionBandCapabilityError("session_band_capability_band_invalid")
        out.add(band)
    return tuple(sorted(out))


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _capability_id(payload: Mapping[str, Any]) -> str:
    return "session-band:" + sha256(_canonical(payload).encode("utf-8")).hexdigest()[:32]


def _first_observed_bands(
    status: Mapping[str, Any],
    keys: tuple[str, ...],
) -> tuple[tuple[int, ...], str, str]:
    for key in keys:
        if key not in status:
            continue
        try:
            values = normalize_capability_bands(status.get(key))
        except SessionBandCapabilityError as exc:
            return (), key, str(exc)
        if values:
            return values, key, ""
    return (), "", ""


def _expected_profile(
    profile_record: Mapping[str, Any],
    profile_capability_evidence: Mapping[str, Any] | None = None,
) -> tuple[
    str,
    str,
    tuple[int, ...],
    str,
    tuple[int, ...],
]:
    record = dict(profile_record or {})
    capability = dict(profile_capability_evidence or {})
    if capability:
        row = capability
        profile_id = str(
            row.get("profile_id")
            or record.get("profile_id")
            or ""
        )
    else:
        if not record:
            return "", "live_required", (), "live_required", ()
        row = record
        profile_id = str(row.get("profile_id") or "")
    lte_status = str(row.get("lte_band_capability_status") or "").strip().lower()
    nr_status = str(row.get("nr_band_capability_status") or "").strip().lower()
    if profile_id and (
        lte_status not in _VALID_PROFILE_DECLARATION
        or nr_status not in _VALID_PROFILE_DECLARATION
    ):
        raise SessionBandCapabilityError(
            "session_band_capability_profile_declaration_missing"
        )
    expected_lte = normalize_capability_bands(row.get("supported_lte_bands") or ())
    expected_nr = normalize_capability_bands(row.get("supported_nr_bands") or ())
    for rat, declaration, bands in (
        ("lte", lte_status, expected_lte),
        ("nr", nr_status, expected_nr),
    ):
        if declaration == "declared" and not bands:
            raise SessionBandCapabilityError(
                f"session_band_capability_profile_{rat}_bands_required"
            )
        if declaration != "declared" and bands:
            raise SessionBandCapabilityError(
                f"session_band_capability_profile_{rat}_bands_conflict"
            )
    return (
        profile_id,
        lte_status or "live_required",
        expected_lte,
        nr_status or "live_required",
        expected_nr,
    )


@dataclass(frozen=True, slots=True)
class SessionBandCapability:
    session_scope_key: str
    profile_id: str
    observed_at: float
    lte_status: str
    supported_lte_bands: tuple[int, ...]
    lte_source: str
    nr_status: str
    supported_nr_bands: tuple[int, ...]
    nr_source: str
    profile_seed_lte_bands: tuple[int, ...]
    profile_seed_nr_bands: tuple[int, ...]
    live_observed_lte_bands: tuple[int, ...]
    live_observed_nr_bands: tuple[int, ...]
    comparison_status: str
    discrepancies: tuple[dict[str, Any], ...]
    capability_id: str
    schema: str = SCHEMA
    owner: str = OWNER
    revision: int = REVISION

    def __post_init__(self) -> None:
        if self.schema != SCHEMA or self.owner != OWNER or int(self.revision) != REVISION:
            raise SessionBandCapabilityError("session_band_capability_owner_schema_invalid")
        if not str(self.session_scope_key or "").strip():
            raise SessionBandCapabilityError("session_band_capability_session_required")
        if not math.isfinite(float(self.observed_at)) or float(self.observed_at) <= 0.0:
            raise SessionBandCapabilityError("session_band_capability_timestamp_invalid")
        for field_name in ("lte_status", "nr_status"):
            if str(getattr(self, field_name) or "") not in _VALID_STATUS:
                raise SessionBandCapabilityError(
                    f"session_band_capability_{field_name}_invalid"
                )
        for status_name, bands_name, source_name in (
            ("lte_status", "supported_lte_bands", "lte_source"),
            ("nr_status", "supported_nr_bands", "nr_source"),
        ):
            status = str(getattr(self, status_name))
            bands = tuple(getattr(self, bands_name))
            source = str(getattr(self, source_name) or "")
            if status in {"proven", "seeded"} and (not bands or not source):
                raise SessionBandCapabilityError(
                    f"session_band_capability_{bands_name}_proof_required"
                )
            if status not in {"proven", "seeded"} and bands:
                raise SessionBandCapabilityError(
                    f"session_band_capability_{bands_name}_status_conflict"
                )
        expected_id = _capability_id(self._content())
        if str(self.capability_id or "") != expected_id:
            raise SessionBandCapabilityError("session_band_capability_id_mismatch")

    def _content(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "revision": int(self.revision),
            "session_scope_key": self.session_scope_key,
            "profile_id": self.profile_id,
            "observed_at": float(self.observed_at),
            "lte_status": self.lte_status,
            "supported_lte_bands": list(self.supported_lte_bands),
            "lte_source": self.lte_source,
            "nr_status": self.nr_status,
            "supported_nr_bands": list(self.supported_nr_bands),
            "nr_source": self.nr_source,
            "profile_seed_lte_bands": list(self.profile_seed_lte_bands),
            "profile_seed_nr_bands": list(self.profile_seed_nr_bands),
            "live_observed_lte_bands": list(self.live_observed_lte_bands),
            "live_observed_nr_bands": list(self.live_observed_nr_bands),
            "comparison_status": self.comparison_status,
            "discrepancies": [dict(row) for row in self.discrepancies],
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._content(), "capability_id": self.capability_id}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SessionBandCapability":
        row = dict(value or {})
        discrepancies = row.get("discrepancies")
        if not isinstance(discrepancies, list) or not all(
            isinstance(item, Mapping) for item in discrepancies
        ):
            raise SessionBandCapabilityError(
                "session_band_capability_discrepancies_invalid"
            )
        return cls(
            session_scope_key=str(row.get("session_scope_key") or ""),
            profile_id=str(row.get("profile_id") or ""),
            observed_at=float(row.get("observed_at", 0.0) or 0.0),
            lte_status=str(row.get("lte_status") or ""),
            supported_lte_bands=normalize_capability_bands(row.get("supported_lte_bands")),
            lte_source=str(row.get("lte_source") or ""),
            nr_status=str(row.get("nr_status") or ""),
            supported_nr_bands=normalize_capability_bands(row.get("supported_nr_bands")),
            nr_source=str(row.get("nr_source") or ""),
            profile_seed_lte_bands=normalize_capability_bands(row.get("profile_seed_lte_bands")),
            profile_seed_nr_bands=normalize_capability_bands(row.get("profile_seed_nr_bands")),
            live_observed_lte_bands=normalize_capability_bands(row.get("live_observed_lte_bands")),
            live_observed_nr_bands=normalize_capability_bands(row.get("live_observed_nr_bands")),
            comparison_status=str(row.get("comparison_status") or ""),
            discrepancies=tuple(dict(item) for item in discrepancies),
            capability_id=str(row.get("capability_id") or ""),
            schema=str(row.get("schema") or ""),
            owner=str(row.get("owner") or ""),
            revision=int(row.get("revision", 0) or 0),
        )


def build_session_band_capability(
    *,
    session_scope_key: str,
    status: Mapping[str, Any],
    profile_record: Mapping[str, Any],
    observed_at: float,
    profile_capability_evidence: Mapping[str, Any] | None = None,
) -> SessionBandCapability:
    """Compare expected profile data with data read by the backend."""

    status_row = dict(status or {})
    (
        profile_id,
        profile_lte_declaration,
        expected_lte,
        profile_nr_declaration,
        expected_nr,
    ) = _expected_profile(
        profile_record,
        profile_capability_evidence,
    )
    actual_lte, lte_key, lte_error = _first_observed_bands(
        status_row,
        (
            "supported_lte_bands",
            "netmode_list_supported_bands",
        ),
    )
    actual_nr, nr_key, nr_error = _first_observed_bands(
        status_row,
        (
            "supported_nr_bands",
            "netmode_list_supported_nr_bands",
        ),
    )
    effective_lte = actual_lte or expected_lte
    effective_nr = actual_nr or expected_nr
    lte_status = (
        "proven"
        if actual_lte
        else "seeded"
        if expected_lte
        else "unsupported"
        if profile_lte_declaration == "unsupported"
        else "unavailable"
    )
    nr_status = (
        "proven"
        if actual_nr
        else "seeded"
        if expected_nr
        else "unsupported"
        if profile_nr_declaration == "unsupported"
        else "unavailable"
    )
    lte_source = lte_key if actual_lte else f"profile:{profile_id}" if expected_lte else ""
    nr_source = nr_key if actual_nr else f"profile:{profile_id}" if expected_nr else ""
    discrepancies: list[dict[str, Any]] = []
    for rat, source, error in (
        ("lte", lte_key, lte_error),
        ("nr", nr_key, nr_error),
    ):
        if error:
            discrepancies.append(
                {
                    "rat": rat,
                    "source": source,
                    "evidence_error": error,
                }
            )
    for rat, expected, actual in (
        ("lte", expected_lte, actual_lte),
        ("nr", expected_nr, actual_nr),
    ):
        if not expected or not actual:
            continue
        missing_live = sorted(set(expected) - set(actual))
        extra_live = sorted(set(actual) - set(expected))
        if missing_live or extra_live:
            discrepancies.append(
                {
                    "rat": rat,
                    "expected_not_reported_live": missing_live,
                    "reported_live_not_expected": extra_live,
                }
            )
    if lte_error or nr_error:
        comparison = "actual_invalid"
    elif (
        not actual_lte
        and not actual_nr
        and (effective_lte or effective_nr)
        and (lte_status == "seeded" or nr_status == "seeded")
    ):
        comparison = "profile_seeded_no_live_probe"
    elif not actual_lte and not actual_nr:
        comparison = "actual_unavailable"
    elif not expected_lte and not expected_nr:
        comparison = "profile_unavailable"
    elif discrepancies:
        comparison = "mismatch"
    else:
        comparison = "match"
    content = {
        "schema": SCHEMA,
        "owner": OWNER,
        "revision": REVISION,
        "session_scope_key": str(session_scope_key or "").strip(),
        "profile_id": profile_id,
        "observed_at": float(observed_at),
        "lte_status": lte_status,
        "supported_lte_bands": list(effective_lte),
        "lte_source": lte_source,
        "nr_status": nr_status,
        "supported_nr_bands": list(effective_nr),
        "nr_source": nr_source,
        "profile_seed_lte_bands": list(expected_lte),
        "profile_seed_nr_bands": list(expected_nr),
        "live_observed_lte_bands": list(actual_lte),
        "live_observed_nr_bands": list(actual_nr),
        "comparison_status": comparison,
        "discrepancies": discrepancies,
    }
    return SessionBandCapability.from_mapping(
        {**content, "capability_id": _capability_id(content)}
    )


__all__ = (
    "OWNER",
    "REVISION",
    "SCHEMA",
    "SessionBandCapability",
    "SessionBandCapabilityError",
    "build_session_band_capability",
    "normalize_capability_bands",
)
