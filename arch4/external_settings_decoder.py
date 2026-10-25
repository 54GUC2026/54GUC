"""Neutral, read-only decoder for the retired external XML settings input.

``guc_config.xml`` and ``luc_config.xml`` are accepted only as external input
artifacts. This module validates the whole document and returns immutable typed
owner slices. It writes no owner store, receipt, XML, or runtime state.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from xml.etree import ElementTree as ET

from arch4.customer_contracts.settings import (
    CoreRuntimeDefaults,
    FoundationOperatorSettings,
    IntelligencePreferences,
    NGUIConnectionHint,
    NGUISettings,
    TrafficPlan,
    is_scrubbed_connection_user,
    traffic_plan_scope_id,
)
from arch4.modem_live.profile_catalog import modem_profile_selection


EXTERNAL_FILENAMES = ("guc_config.xml", "luc_config.xml")

_OPERATOR_FIELDS = frozenset(
    {
        "operator_name",
        "customer_name",
        "user_name",
        "site_name",
        "site_address",
        "install_address",
        "address",
        "address_line1",
        "address_line2",
        "city",
        "contact_name",
        "contact_phone",
        "contact_email",
        "site_notes",
    }
)
_LICENSE_FIELDS = frozenset(
    {"license_key", "license_nonce", "license_email", "license_tier"}
)
_SCALAR_FIELDS = frozenset(
    {
        "rsrq_default",
        "rtt_default",
        "band_priority",
        "last_modem",
        "traffic_monthly_allowance_gb",
        "traffic_cycle_start_day",
        "traffic_plan_override_enabled",
        "dev_full_access",
        "modem_backend",
        *_OPERATOR_FIELDS,
        *_LICENSE_FIELDS,
    }
)
_CONTAINER_FIELDS = frozenset({"connection_hints", "traffic_plan_scopes"})


class ExternalSettingsImportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExternalSettingsSlices:
    source_name: str
    source_sha256: str
    core: CoreRuntimeDefaults
    ngui: NGUISettings
    foundation: FoundationOperatorSettings
    intelligence: IntelligencePreferences
    ignored_input_fields: tuple[str, ...]
    deferred_license_candidate: bool


@dataclass(frozen=True, slots=True)
class PreviousHandoffSettingsSlice:
    """Identity-qualified owner slices decoded from one pre-split archive."""

    source_sha256: str
    foundation: FoundationOperatorSettings
    connection_hint: NGUIConnectionHint | None
    license_fields: Mapping[str, str]
    ignored_input_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        allowed = {"license_key", "license_nonce", "license_email", "license_tier"}
        source_fields = dict(self.license_fields or {})
        if set(source_fields) - allowed:
            raise ValueError("previous handoff contains unknown license fields")
        fields = {
            str(name): _text(value)
            for name, value in source_fields.items()
            if _text(value)
        }
        object.__setattr__(self, "license_fields", MappingProxyType(fields))
        object.__setattr__(
            self,
            "ignored_input_fields",
            tuple(sorted(set(self.ignored_input_fields))),
        )


def _text(value: object) -> str:
    return str(value or "").strip()


def _bool(value: object, *, field: str) -> bool:
    token = _text(value).lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off", ""}:
        return False
    raise ExternalSettingsImportError(f"{field} must be boolean")


def _fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_path(root: Path) -> Path | None:
    existing = [root / name for name in EXTERNAL_FILENAMES if (root / name).is_file()]
    if len(existing) > 1:
        raise ExternalSettingsImportError(
            "both guc_config.xml and luc_config.xml exist; external settings source is ambiguous"
        )
    return existing[0] if existing else None


def _validate_shape(root: ET.Element) -> None:
    if root.tag != "guc_config":
        raise ExternalSettingsImportError("external settings root must be guc_config")
    for element in list(root):
        if element.tag not in _SCALAR_FIELDS | _CONTAINER_FIELDS:
            raise ExternalSettingsImportError(
                f"unknown external settings field:{element.tag}"
            )
        if element.tag in _SCALAR_FIELDS and (list(element) or element.attrib):
            raise ExternalSettingsImportError(
                f"scalar external settings field has nested data:{element.tag}"
            )
    hints = root.find("connection_hints")
    if hints is not None:
        if hints.attrib:
            raise ExternalSettingsImportError("connection_hints attributes are illegal")
        for child in list(hints):
            if child.tag != "backend" or list(child):
                raise ExternalSettingsImportError(
                    "connection_hints may contain only empty backend records"
                )
            if not set(child.attrib).issubset({"name", "profile_id", "host", "user"}):
                raise ExternalSettingsImportError(
                    "connection hint contains unknown or secret attributes"
                )
    scopes = root.find("traffic_plan_scopes")
    if scopes is not None:
        if scopes.attrib:
            raise ExternalSettingsImportError("traffic_plan_scopes attributes are illegal")
        allowed = {
            "subscription_key",
            "id",
            "modem_backend",
            "provider_name",
            "provider_plmn",
            "profile_name",
            "profile_id",
            "monthly_allowance_gb",
            "cycle_start_day",
            "override_enabled",
        }
        for child in list(scopes):
            if child.tag != "plan" or list(child) or not set(child.attrib).issubset(allowed):
                raise ExternalSettingsImportError(
                    "traffic_plan_scopes contains an invalid plan record"
                )


def _parse_band_priority(text: str) -> IntelligencePreferences:
    values: dict[int, float] = {}
    for item in _text(text).split(","):
        token = item.strip()
        if not token:
            continue
        band_text, separator, score_text = token.partition(":")
        if not separator:
            raise ExternalSettingsImportError("band_priority entry requires band:score")
        try:
            band = int(band_text.strip())
            score = float(score_text.strip())
        except (TypeError, ValueError) as exc:
            raise ExternalSettingsImportError("band_priority entry is invalid") from exc
        if band in values:
            raise ExternalSettingsImportError(f"duplicate band_priority entry:B{band}")
        values[band] = score
    try:
        return IntelligencePreferences(band_priority=values)
    except ValueError as exc:
        raise ExternalSettingsImportError(str(exc)) from exc


def _exact_profile_id(token: object) -> str:
    contract = modem_profile_selection(_text(token))
    return str(contract.profile_id) if contract is not None else ""


def _decode(root: ET.Element) -> tuple[
    CoreRuntimeDefaults,
    NGUISettings,
    FoundationOperatorSettings,
    IntelligencePreferences,
    list[str],
    bool,
]:
    authoritative_backend = _text(root.findtext("modem_backend"))
    if authoritative_backend:
        raise ExternalSettingsImportError(
            "modem_backend is retired settings authority and cannot be imported"
        )
    if _bool(root.findtext("dev_full_access"), field="dev_full_access"):
        raise ExternalSettingsImportError(
            "dev_full_access is launch policy and cannot be imported from disk"
        )

    try:
        core = CoreRuntimeDefaults(
            rsrq_baseline_default_db=_text(root.findtext("rsrq_default")) or -10.0,
            rtt_baseline_default_ms=_text(root.findtext("rtt_default")) or 55.0,
        )
    except ValueError as exc:
        raise ExternalSettingsImportError(str(exc)) from exc
    intelligence = _parse_band_priority(root.findtext("band_priority") or "")

    ignored: list[str] = []
    last_profile_id = _exact_profile_id(root.findtext("last_modem"))
    if _text(root.findtext("last_modem")) and not last_profile_id:
        ignored.append("last_modem_without_exact_profile")
    hints: dict[str, NGUIConnectionHint] = {}
    hints_root = root.find("connection_hints")
    if hints_root is not None:
        for element in hints_root.findall("backend"):
            token = _text(element.get("profile_id")) or _text(element.get("name"))
            profile_id = _exact_profile_id(token)
            host = _text(element.get("host"))
            user = _text(element.get("user"))
            if is_scrubbed_connection_user(user):
                user = ""
            if not profile_id:
                ignored.append(f"connection_hint_without_exact_profile:{token or '<empty>'}")
                continue
            if not host:
                raise ExternalSettingsImportError(
                    f"connection hint for {profile_id} has no host"
                )
            hints[profile_id] = NGUIConnectionHint(profile_id, host, user)
    ngui = NGUISettings(
        last_profile_id=last_profile_id,
        connection_hints=hints,
    )

    operator_fields = {
        tag: _text(root.findtext(tag))
        for tag in _OPERATOR_FIELDS
        if _text(root.findtext(tag))
    }
    plans: dict[str, TrafficPlan] = {}
    scopes = root.find("traffic_plan_scopes")
    if scopes is not None:
        for element in scopes.findall("plan"):
            subscription = _text(element.get("subscription_key"))
            if not subscription:
                ignored.append("traffic_plan_without_confirmed_subscription")
                continue
            backend = _text(element.get("modem_backend")).lower()
            profile_token = _text(element.get("profile_id")) or _text(
                element.get("profile_name")
            )
            profile_id = _exact_profile_id(profile_token)
            provider_name = _text(element.get("provider_name"))
            provider_plmn = _text(element.get("provider_plmn"))
            if not profile_id:
                ignored.append(
                    f"traffic_plan_without_exact_profile:{profile_token or '<empty>'}"
                )
                continue
            scope_id = traffic_plan_scope_id(
                subscription_key=subscription,
                modem_backend=backend,
                provider_name=provider_name,
                provider_plmn=provider_plmn,
                profile_id=profile_id,
            )
            if not scope_id:
                ignored.append(f"traffic_plan_without_exact_scope:{profile_id}")
                continue
            try:
                plan = TrafficPlan(
                    subscription_key=subscription,
                    scope_id=scope_id,
                    modem_backend=backend,
                    provider_name=provider_name,
                    provider_plmn=provider_plmn,
                    profile_id=profile_id,
                    monthly_allowance_gb=_text(
                        element.get("monthly_allowance_gb")
                    )
                    or 1000.0,
                    cycle_start_day=_text(element.get("cycle_start_day")) or 1,
                    override_enabled=_bool(
                        element.get("override_enabled"),
                        field="traffic_plan.override_enabled",
                    ),
                )
            except ValueError as exc:
                raise ExternalSettingsImportError(str(exc)) from exc
            plans[plan.scope_id] = plan
    if any(
        _text(root.findtext(tag))
        for tag in (
            "traffic_monthly_allowance_gb",
            "traffic_cycle_start_day",
            "traffic_plan_override_enabled",
        )
    ):
        ignored.append("unscoped_traffic_plan")
    foundation = FoundationOperatorSettings(
        operator_fields=operator_fields,
        traffic_plans=plans,
    )
    license_candidate = bool(
        _text(root.findtext("license_key"))
        or _text(root.findtext("license_nonce"))
    )
    return core, ngui, foundation, intelligence, sorted(set(ignored)), license_candidate


def _decode_source(source: Path, source_bytes: bytes | None = None) -> tuple[
    bytes,
    CoreRuntimeDefaults,
    NGUISettings,
    FoundationOperatorSettings,
    IntelligencePreferences,
    list[str],
    bool,
]:
    payload = source.read_bytes() if source_bytes is None else bytes(source_bytes)
    try:
        xml_root = ET.fromstring(payload)
    except Exception as exc:
        raise ExternalSettingsImportError(
            f"external settings XML cannot be parsed:{exc}"
        ) from exc
    _validate_shape(xml_root)
    decoded = _decode(xml_root)
    return payload, *decoded


def decode_external_settings_source(
    storage_root: str | Path,
) -> ExternalSettingsSlices | None:
    """Decode all typed slices without choosing or mutating an owner."""

    root_path = Path(storage_root).resolve()
    source = _source_path(root_path)
    if source is None:
        return None
    source_bytes, core, ngui, foundation, intelligence, ignored, license_candidate = (
        _decode_source(source)
    )
    return ExternalSettingsSlices(
        source_name=source.name,
        source_sha256=_fingerprint(source_bytes),
        core=core,
        ngui=ngui,
        foundation=foundation,
        intelligence=intelligence,
        ignored_input_fields=tuple(ignored),
        deferred_license_candidate=bool(license_candidate),
    )


def _previous_handoff_hint(
    root: ET.Element,
    *,
    exact_profile_id: str,
    backend: str,
    decoded_ngui: NGUISettings,
) -> NGUIConnectionHint | None:
    hint = decoded_ngui.connection_hints.get(exact_profile_id)
    if hint is not None:
        return hint
    matches: list[NGUIConnectionHint] = []
    hints_root = root.find("connection_hints")
    for element in hints_root.findall("backend") if hints_root is not None else ():
        token = _text(element.get("profile_id")) or _text(element.get("name"))
        if token.lower() != backend:
            continue
        host = _text(element.get("host"))
        if not host:
            raise ExternalSettingsImportError(
                "previous handoff matching connection hint has no host"
            )
        matches.append(
            NGUIConnectionHint(
                profile_id=exact_profile_id,
                address=host,
                username=_text(element.get("user")),
            )
        )
    if len(matches) > 1:
        raise ExternalSettingsImportError(
            "previous handoff has ambiguous matching connection hints"
        )
    return matches[0] if matches else None


def _previous_handoff_traffic_plans(
    root: ET.Element,
    *,
    exact_profile_id: str,
    backend: str,
    confirmed_identity: Mapping[str, object],
    decoded_foundation: FoundationOperatorSettings,
    ignored: set[str],
) -> tuple[dict[str, TrafficPlan], dict[str, Mapping[str, object]]]:
    plans = dict(decoded_foundation.traffic_plans)
    unassigned = dict(decoded_foundation.unassigned_traffic_plans)
    live_provider_name = _text(confirmed_identity.get("provider_name")).lower()
    live_provider_plmn = _text(confirmed_identity.get("provider_plmn"))
    scopes = root.find("traffic_plan_scopes")
    for element in scopes.findall("plan") if scopes is not None else ():
        subscription = _text(element.get("subscription_key"))
        profile_token = _text(element.get("profile_id")) or _text(
            element.get("profile_name")
        )
        if _exact_profile_id(profile_token) and subscription:
            continue
        if _exact_profile_id(profile_token) and _exact_profile_id(profile_token) != exact_profile_id:
            continue
        if _text(element.get("modem_backend")).lower() != backend:
            continue
        provider_name = _text(element.get("provider_name"))
        provider_plmn = _text(element.get("provider_plmn"))
        if (
            live_provider_plmn
            and provider_plmn
            and provider_plmn != live_provider_plmn
        ):
            ignored.add("previous_traffic_plan_provider_plmn_mismatch")
            continue
        if (
            not live_provider_plmn
            and live_provider_name
            and provider_name
            and provider_name.lower() != live_provider_name
        ):
            ignored.add("previous_traffic_plan_provider_name_mismatch")
            continue
        scoped_name = provider_name or _text(confirmed_identity.get("provider_name"))
        scoped_plmn = provider_plmn or live_provider_plmn
        if not subscription:
            # A serialized legacy record, never a live TrafficPlan. The named
            # adoption boundary assigns it only after physical/SIM admission.
            key = f"backend={backend}|plmn={scoped_plmn}|profile={exact_profile_id}"
            unassigned[key] = dict(scope_id=key, modem_backend=backend,
                profile_id=exact_profile_id, provider_name=scoped_name, provider_plmn=scoped_plmn,
                monthly_allowance_gb=_text(element.get("monthly_allowance_gb")) or "1000",
                cycle_start_day=_text(element.get("cycle_start_day")) or "1",
                override_enabled=_text(element.get("override_enabled")) or "true")
            continue
        scope_id = traffic_plan_scope_id(
            subscription_key=subscription,
            modem_backend=backend,
            provider_name=scoped_name,
            provider_plmn=scoped_plmn,
            profile_id=exact_profile_id,
        )
        if not scope_id:
            ignored.add("previous_traffic_plan_without_exact_scope")
            continue
        try:
            candidate = TrafficPlan(
                subscription_key=subscription,
                scope_id=scope_id,
                modem_backend=backend,
                provider_name=scoped_name,
                provider_plmn=scoped_plmn,
                profile_id=exact_profile_id,
                monthly_allowance_gb=_text(element.get("monthly_allowance_gb"))
                or 1000.0,
                cycle_start_day=_text(element.get("cycle_start_day")) or 1,
                override_enabled=_bool(
                    element.get("override_enabled"),
                    field="previous_handoff.traffic_plan.override_enabled",
                ),
            )
        except ValueError as exc:
            raise ExternalSettingsImportError(str(exc)) from exc
        previous = plans.get(candidate.scope_id)
        if previous is not None and previous != candidate:
            raise ExternalSettingsImportError(
                "previous handoff has conflicting exact traffic plans"
            )
        plans[candidate.scope_id] = candidate
    return plans, unassigned


def decode_previous_handoff_settings_xml(
    source_bytes: bytes,
    *,
    confirmed_profile_id: str,
    confirmed_identity: Mapping[str, object],
) -> PreviousHandoffSettingsSlice:
    """Decode one named pre-split XML member into exact owner slices."""

    profile = modem_profile_selection(confirmed_profile_id)
    if profile is None:
        raise ExternalSettingsImportError(
            "previous handoff requires exact confirmed profile_id"
        )
    (
        payload,
        _core,
        decoded_ngui,
        decoded_foundation,
        _intelligence,
        decoded_ignored,
        _license_candidate,
    ) = _decode_source(Path("guc_config.xml"), bytes(source_bytes))
    root = ET.fromstring(payload)
    ignored = set(decoded_ignored)
    exact_profile_id = str(profile.profile_id)
    backend = _text(profile.backend).lower()
    hint = _previous_handoff_hint(
        root,
        exact_profile_id=exact_profile_id,
        backend=backend,
        decoded_ngui=decoded_ngui,
    )
    plans, unassigned = _previous_handoff_traffic_plans(
        root,
        exact_profile_id=exact_profile_id,
        backend=backend,
        confirmed_identity=confirmed_identity,
        decoded_foundation=decoded_foundation,
        ignored=ignored,
    )

    license_fields = {
        name: _text(root.findtext(name))
        for name in ("license_key", "license_nonce", "license_email", "license_tier")
        if _text(root.findtext(name))
    }
    return PreviousHandoffSettingsSlice(
        source_sha256=_fingerprint(payload),
        foundation=FoundationOperatorSettings(
            unassigned_traffic_plans=unassigned,
            operator_fields=decoded_foundation.operator_fields,
            traffic_plans=plans,
        ),
        connection_hint=hint,
        license_fields=license_fields,
        ignored_input_fields=tuple(ignored),
    )


def validate_external_settings_source(storage_root: str | Path) -> dict[str, object]:
    """Validate external XML through the sole decoder without committing state."""

    decoded = decode_external_settings_source(storage_root)
    if decoded is None:
        return {
            "present": False,
            "source_name": "",
            "source_sha256": "",
            "ignored_input_fields": [],
            "deferred_license_candidate": False,
        }
    return {
        "present": True,
        "source_name": decoded.source_name,
        "source_sha256": decoded.source_sha256,
        "ignored_input_fields": list(decoded.ignored_input_fields),
        "deferred_license_candidate": decoded.deferred_license_candidate,
    }


__all__ = (
    "ExternalSettingsImportError",
    "ExternalSettingsSlices",
    "PreviousHandoffSettingsSlice",
    "decode_external_settings_source",
    "decode_previous_handoff_settings_xml",
    "validate_external_settings_source",
)
