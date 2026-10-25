from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from arch4.coerce import safe_bool
from arch4.operator_profiles import infer_backend_family
from arch4.runtime_sidecar_identity import build_expected_sidecar_identity


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


_PROVIDER_PLACEHOLDER_VALUES: frozenset[str] = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "unknown",
        "none",
        "null",
        "n/a",
        "na",
        "miss",
        "missing",
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
    }
)


def coerce_provider_plmn_text(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    text = _text(value)
    if not text:
        return ""
    if text.lower() in _PROVIDER_PLACEHOLDER_VALUES:
        return ""
    return text if text.isdigit() else ""


def is_placeholder_identity_text(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    text = _text(value)
    return not text or text.lower() in _PROVIDER_PLACEHOLDER_VALUES


def coerce_provider_name_text(value: Any) -> str:
    if is_placeholder_identity_text(value):
        return ""
    text = _text(value)
    if text.isdigit():
        return ""
    return text


def _first_text(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _first_plmn_text(*values: Any) -> str:
    for value in values:
        text = coerce_provider_plmn_text(value)
        if text:
            return text
    return ""


def _first_provider_name_text(*values: Any) -> str:
    for value in values:
        text = coerce_provider_name_text(value)
        if text:
            return text
    return ""


def _owned_provider_name_text(owner: Mapping[str, Any], *fallback_owners: Mapping[str, Any]) -> str:
    if "provider_name" in owner:
        return coerce_provider_name_text(owner.get("provider_name"))
    return _first_provider_name_text(*(fallback.get("provider_name") for fallback in fallback_owners))


def _owned_provider_plmn_text(owner: Mapping[str, Any], *fallback_owners: Mapping[str, Any]) -> str:
    if "provider_plmn" in owner:
        return coerce_provider_plmn_text(owner.get("provider_plmn"))
    return _first_plmn_text(*(fallback.get("provider_plmn") for fallback in fallback_owners))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_float(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def _clamp_confidence(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


_RUNTIME_SESSION_SCOPE_KEYS: tuple[str, ...] = (
    "target",
    "backend",
    "backend_family",
    "model",
    "firmware",
    "provider_plmn",
    "provider_name",
    "serial",
    "imei",
    "imsi",
    "sim_id",
)
_RUNTIME_SESSION_STRONG_KEYS: tuple[str, ...] = (
    "provider_plmn",
    "serial",
    "imei",
    "imsi",
    "sim_id",
)
_RUNTIME_SESSION_FALLBACK_KEYS: tuple[str, ...] = (
    "target",
    "backend",
    "model",
)
_RUNTIME_SESSION_UNKNOWN_VALUES: frozenset[str] = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "unknown",
        "none",
        "null",
        "n/a",
        "na",
        "miss",
        "missing",
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
    }
)
_RUNTIME_SESSION_GENERIC_MODEL_VALUES: frozenset[str] = frozenset(
    {
        "generic",
        "mikrotik",
        "routeros",
        "routeros_at_like",
        "huawei",
        "zte",
        "openwrt",
    }
)


def _coerce_runtime_scope_value(key: str, value: Any) -> str:
    if key == "provider_plmn":
        return coerce_provider_plmn_text(value)
    if key == "provider_name":
        return coerce_provider_name_text(value).lower()
    text = _text(value)
    if not text or text.lower() in _RUNTIME_SESSION_UNKNOWN_VALUES:
        return ""
    if key in {"target", "backend", "backend_family", "model", "firmware"}:
        return text.lower()
    return text


def _first_runtime_scope_value(key: str, *values: Any) -> str:
    for value in values:
        text = _coerce_runtime_scope_value(key, value)
        if text:
            return text
    return ""


def _derived_confidence(*, backend: str, backend_family: str, vendor: str, model: str, firmware: str) -> float:
    score = 0.0
    if backend:
        score += 0.25
    if backend_family and backend_family != "unknown":
        score += 0.15
    if vendor:
        score += 0.15
    if model:
        score += 0.30
    if firmware:
        score += 0.15
    return _clamp_confidence(score)


def _identity_state(*, backend_family: str, vendor: str, model: str) -> str:
    if model:
        return "identified"
    if backend_family and backend_family != "unknown":
        return "family_known_model_unknown"
    if vendor:
        return "vendor_known_model_unknown"
    return "unknown"


def _firmware_signature(*, model: str, firmware: str) -> str:
    if model and firmware:
        return f"{model}::{firmware}"
    return firmware or ""


def build_runtime_owner(
    *,
    runtime_dir: str = "",
    memory_dir: str = "",
    instance_id: str = "",
    profile_name: str = "",
) -> dict[str, str]:
    owner = build_expected_sidecar_identity(
        runtime_dir=str(runtime_dir or ""),
        memory_dir=str(memory_dir or ""),
        instance_id=str(instance_id or ""),
        profile_name=str(profile_name or ""),
    )
    return {
        key: str(owner.get(key, "") or "")
        for key in ("runtime_dir", "memory_dir", "instance_id", "profile_name")
        if str(owner.get(key, "") or "")
    }


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    backend: str = ""
    backend_family: str = ""
    vendor: str = ""
    model: str = ""
    firmware: str = ""
    firmware_signature: str = ""
    provider_name: str = ""
    provider_plmn: str = ""
    identity_state: str = "unknown"
    identity_confidence: float = 0.0
    runtime_owner: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "backend_family": self.backend_family,
            "vendor": self.vendor,
            "model": self.model,
            "firmware": self.firmware,
            "firmware_signature": self.firmware_signature,
            "provider_name": self.provider_name,
            "provider_plmn": self.provider_plmn,
            "identity_state": self.identity_state,
            "identity_confidence": self.identity_confidence,
            "runtime_owner": dict(self.runtime_owner),
        }


def build_runtime_identity(
    *,
    backend: str = "",
    backend_family: str = "",
    vendor: str = "",
    model: str = "",
    firmware: str = "",
    provider_name: str = "",
    provider_plmn: str = "",
    confidence: float | None = None,
    runtime_dir: str = "",
    memory_dir: str = "",
    instance_id: str = "",
    profile_name: str = "",
) -> RuntimeIdentity:
    norm_backend = _lower(backend)
    norm_family = _lower(backend_family)
    if norm_backend and not norm_family:
        try:
            norm_family = _lower(infer_backend_family(norm_backend))
        except Exception:
            norm_family = ""
    norm_vendor = _text(vendor)
    norm_model = _text(model)
    norm_firmware = _text(firmware)
    resolved_confidence = _first_float(confidence)
    if resolved_confidence is None:
        resolved_confidence = _derived_confidence(
            backend=norm_backend,
            backend_family=norm_family,
            vendor=norm_vendor,
            model=norm_model,
            firmware=norm_firmware,
        )
    owner = build_runtime_owner(
        runtime_dir=runtime_dir,
        memory_dir=memory_dir,
        instance_id=instance_id,
        profile_name=profile_name,
    )
    return RuntimeIdentity(
        backend=norm_backend,
        backend_family=norm_family,
        vendor=norm_vendor,
        model=norm_model,
        firmware=norm_firmware,
        firmware_signature=_firmware_signature(model=norm_model, firmware=norm_firmware),
        provider_name=_text(provider_name),
        provider_plmn=coerce_provider_plmn_text(provider_plmn),
        identity_state=_identity_state(
            backend_family=norm_family,
            vendor=norm_vendor,
            model=norm_model,
        ),
        identity_confidence=_clamp_confidence(float(resolved_confidence)),
        runtime_owner=owner,
    )


def _identity_surface_candidates(source: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(source, Mapping):
        return ()
    out: list[Mapping[str, Any]] = []
    for key in (
        "resolved_identity_card",
        "runtime_identity",
        "identity",
        "capability_profile",
        "support",
        "capability_contract",
    ):
        value = source.get(key)
        if isinstance(value, Mapping):
            out.append(value)
    out.append(source)
    return tuple(out)


def backend_family_from_identity_surfaces(
    *sources: Any,
    backend: Any = "",
) -> str:
    """Resolve behavior family from exact identity surfaces.

    Production behavior must not infer modem family locally from raw backend
    strings. This helper is the identity-owner boundary: resolved identity card
    and runtime identity win; backend inference is contained here as the last
    resort for early startup/partial-profile moments.
    """

    backend_hint = _lower(backend)
    for source in sources:
        for candidate in _identity_surface_candidates(source):
            family = _lower(_first_text(candidate.get("backend_family"), candidate.get("family")))
            if family:
                return family
            if not backend_hint:
                backend_hint = _lower(candidate.get("backend"))
    if backend_hint:
        try:
            return _lower(infer_backend_family(backend_hint))
        except Exception:
            return ""
    return ""


def runtime_backend_family_for_engine(engine: Any, *sources: Any, backend: Any = "") -> str:
    """Resolve engine behavior family from promoted runtime identity first."""

    cfg = getattr(engine, "cfg", None)
    profile = getattr(engine, "policy_profile", None)
    profile_source = {
        "backend": getattr(profile, "modem_backend", ""),
        "backend_family": getattr(profile, "backend_family", ""),
    }
    cfg_source = {
        "backend": getattr(cfg, "modem_backend", ""),
        "backend_family": getattr(cfg, "backend_family", ""),
    }
    return backend_family_from_identity_surfaces(
        *sources,
        getattr(engine, "_last_resolved_identity_card", {}),
        getattr(engine, "_last_runtime_identity", {}),
        profile_source,
        cfg_source,
        backend=backend or getattr(cfg, "modem_backend", ""),
    )


def _capability_connection_identity(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    identity = _mapping(payload.get("identity"))
    allowed = ("target", "iface", "iface_id", "sim_id")
    return {key: identity.get(key) for key in allowed if _text(identity.get(key))}


def runtime_identity_from_capabilities_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    data = payload if isinstance(payload, Mapping) else {}
    canonical_runtime_identity = (
        data.get("runtime_identity")
        if isinstance(data.get("runtime_identity"), Mapping)
        else {}
    )
    resolved_card = (
        data.get("resolved_identity_card")
        if isinstance(data.get("resolved_identity_card"), Mapping)
        else {}
    )
    provenance = data.get("provenance") if isinstance(data.get("provenance"), Mapping) else {}
    profile = _first_text(
        resolved_card.get("profile_id"),
        data.get("profile_name"),
        data.get("profile"),
        provenance.get("profile_name"),
    )
    out = build_runtime_identity(
        backend=_first_text(
            canonical_runtime_identity.get("backend"),
            data.get("backend"),
            provenance.get("modem_backend"),
        ),
        backend_family=_first_text(
            canonical_runtime_identity.get("backend_family"),
            data.get("backend_family"),
            provenance.get("backend_family"),
        ),
        vendor=_first_text(
            canonical_runtime_identity.get("vendor"),
        ),
        model=_first_text(
            canonical_runtime_identity.get("model"),
        ),
        firmware=_first_text(
            canonical_runtime_identity.get("firmware"),
        ),
        provider_name=_first_provider_name_text(
            canonical_runtime_identity.get("provider_name"),
            canonical_runtime_identity.get("provider"),
        ),
        provider_plmn=_first_plmn_text(
            canonical_runtime_identity.get("provider_plmn"),
            canonical_runtime_identity.get("plmn"),
        ),
        confidence=_first_float(
            canonical_runtime_identity.get("identity_confidence"),
            data.get("identity_confidence"),
        ),
        runtime_dir=_first_text(data.get("runtime_dir"), provenance.get("runtime_dir")),
        memory_dir=_first_text(data.get("memory_dir"), provenance.get("memory_dir")),
        instance_id=_first_text(data.get("instance_id"), provenance.get("instance_id")),
        profile_name=profile,
    )
    result = out.to_dict()
    for key in (
        "target",
        "iface",
        "iface_id",
        "sim_id",
        "serial",
        "imei",
        "imsi",
        "hardware_version",
        "software_version",
        "webui",
        "config_version",
        "lan_mac",
        "wan_ip",
        "antenna",
        "antenna_mode",
        "antenna_mode_source",
        "os_name",
        "os_version",
    ):
        value = _first_text(
            canonical_runtime_identity.get(key),
        )
        if value:
            result[key] = value
    connection_identity = _capability_connection_identity(data)
    for key in ("target", "iface", "iface_id", "sim_id"):
        value = _first_text(connection_identity.get(key))
        if value:
            result[key] = value
    if resolved_card:
        result["resolved_profile_id"] = _first_text(resolved_card.get("profile_id"))
        result["resolved_profile_provenance"] = _first_text(resolved_card.get("profile_provenance"))
        result["resolved_identity_exact"] = safe_bool(resolved_card.get("exact_identity", False), False)
        result["resolved_identity_provisional"] = safe_bool(resolved_card.get("provisional", False), False)
        provisional_reason = _first_text(resolved_card.get("provisional_reason"))
        if provisional_reason:
            result["resolved_identity_provisional_reason"] = provisional_reason
    return result


def runtime_identity_from_sample_sources(
    *,
    base_status: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    runtime_health: Mapping[str, Any] | None = None,
    capabilities: Mapping[str, Any] | None = None,
    backend_hint: str = "",
    backend_family_hint: str = "",
    runtime_dir: str = "",
    memory_dir: str = "",
    instance_id: str = "",
    profile_name: str = "",
) -> dict[str, Any]:
    base = base_status if isinstance(base_status, Mapping) else {}
    extra_map = extra if isinstance(extra, Mapping) else {}
    health = runtime_health if isinstance(runtime_health, Mapping) else {}
    caps = capabilities if isinstance(capabilities, Mapping) else {}
    canonical_runtime_identity = _mapping(extra_map.get("runtime_identity"))
    capability_runtime_identity = _mapping(caps.get("runtime_identity"))
    capability_identity = _mapping(caps.get("identity"))
    resolved_card = _mapping(caps.get("resolved_identity_card"))
    if not resolved_card:
        resolved_card = _mapping(extra_map.get("resolved_identity_card"))
    canonical_owner = (
        canonical_runtime_identity.get("runtime_owner")
        if isinstance(canonical_runtime_identity.get("runtime_owner"), Mapping)
        else {}
    )
    out = build_runtime_identity(
        backend=_first_text(
            canonical_runtime_identity.get("backend"),
            capability_runtime_identity.get("backend"),
            backend_hint,
        ),
        backend_family=_first_text(
            canonical_runtime_identity.get("backend_family"),
            capability_runtime_identity.get("backend_family"),
            backend_family_hint,
        ),
        vendor=_first_text(
            canonical_runtime_identity.get("vendor"),
            capability_runtime_identity.get("vendor"),
        ),
        model=_first_text(
            canonical_runtime_identity.get("model"),
            capability_runtime_identity.get("model"),
        ),
        firmware=_first_text(
            canonical_runtime_identity.get("firmware"),
            capability_runtime_identity.get("firmware"),
        ),
        provider_name=_owned_provider_name_text(canonical_runtime_identity, capability_runtime_identity),
        provider_plmn=_owned_provider_plmn_text(canonical_runtime_identity, capability_runtime_identity),
        confidence=_first_float(
            canonical_runtime_identity.get("identity_confidence"),
            capability_runtime_identity.get("identity_confidence"),
        ),
        runtime_dir=_first_text(
            runtime_dir,
            health.get("runtime_dir"),
            canonical_owner.get("runtime_dir"),
        ),
        memory_dir=_first_text(
            memory_dir,
            health.get("memory_dir"),
            canonical_owner.get("memory_dir"),
        ),
        instance_id=_first_text(
            instance_id,
            health.get("instance_id"),
            canonical_owner.get("instance_id"),
        ),
        profile_name=_first_text(
            profile_name,
            health.get("profile_name"),
            canonical_owner.get("profile_name"),
        ),
    )
    result = out.to_dict()
    capability_connection_identity_keys = frozenset({"target", "iface", "iface_id", "sim_id"})
    for key in (
        "target",
        "iface",
        "iface_id",
        "sim_id",
        "serial",
        "imei",
        "imsi",
        "hardware_version",
        "software_version",
        "webui",
        "config_version",
        "lan_mac",
        "wan_ip",
        "antenna",
        "antenna_mode",
        "antenna_mode_source",
    ):
        capability_identity_value = (
            capability_identity.get(key)
            if key in capability_connection_identity_keys
            else None
        )
        value = _first_text(
            canonical_runtime_identity.get(key),
            capability_runtime_identity.get(key),
            capability_identity_value,
        )
        if value:
            result[key] = value
    access_contract = _mapping(canonical_runtime_identity.get("access_type_contract"))
    if not access_contract:
        access_contract = _mapping(capability_runtime_identity.get("access_type_contract"))
    if not access_contract:
        access_contract = _mapping(caps.get("access_type_contract"))
    access_type = _first_text(
        access_contract.get("display_name"),
        canonical_runtime_identity.get("access_type"),
        capability_runtime_identity.get("access_type"),
        capability_identity.get("access_type"),
    )
    if access_type:
        result["access_type"] = access_type
    if resolved_card:
        result["resolved_profile_id"] = _first_text(resolved_card.get("profile_id"))
        result["resolved_profile_provenance"] = _first_text(resolved_card.get("profile_provenance"))
        result["resolved_identity_exact"] = safe_bool(resolved_card.get("exact_identity", False), False)
        result["resolved_identity_provisional"] = safe_bool(resolved_card.get("provisional", False), False)
        result["resolved_identity_provisional_reason"] = _first_text(
            resolved_card.get("provisional_reason")
        )
    return result


def build_runtime_session_scope(mapping: Mapping[str, Any] | None) -> dict[str, str]:
    data = mapping if isinstance(mapping, Mapping) else {}
    direct_runtime_scope = data.get("runtime_session_scope") if isinstance(data.get("runtime_session_scope"), Mapping) else {}
    if direct_runtime_scope:
        return {
            key: coerced
            for key, value in direct_runtime_scope.items()
            for coerced in (_coerce_runtime_scope_value(key, value),)
            if coerced
        }
    stored_scope = data.get("scope") if isinstance(data.get("scope"), Mapping) else {}
    runtime_context = data.get("runtime_context") if isinstance(data.get("runtime_context"), Mapping) else {}
    identity_source = runtime_context if runtime_context else data
    runtime_identity = (
        identity_source.get("runtime_identity")
        if isinstance(identity_source.get("runtime_identity"), Mapping)
        else (
            data.get("runtime_identity")
            if isinstance(data.get("runtime_identity"), Mapping)
            else {}
        )
    )
    scope = {
        "target": _first_runtime_scope_value("target", stored_scope.get("target"), runtime_identity.get("target"), identity_source.get("target")),
        "backend": _first_runtime_scope_value("backend", stored_scope.get("backend"), runtime_identity.get("backend"), identity_source.get("backend")),
        "backend_family": _first_runtime_scope_value("backend_family", stored_scope.get("backend_family"), runtime_identity.get("backend_family"), identity_source.get("backend_family")),
        "model": _first_runtime_scope_value("model", stored_scope.get("model"), runtime_identity.get("model"), identity_source.get("model")),
        "firmware": _first_runtime_scope_value("firmware", stored_scope.get("firmware"), runtime_identity.get("firmware"), identity_source.get("firmware")),
        "provider_plmn": coerce_provider_plmn_text(
            _first_plmn_text(
                stored_scope.get("provider_plmn"),
                runtime_identity.get("provider_plmn"),
                identity_source.get("provider_plmn"),
            )
        ),
        "provider_name": _lower(
            _first_provider_name_text(
                stored_scope.get("provider_name"),
                runtime_identity.get("provider_name"),
                identity_source.get("provider_name"),
            )
        ),
        "serial": _first_runtime_scope_value("serial", stored_scope.get("serial"), runtime_identity.get("serial"), identity_source.get("serial")),
        "imei": _first_runtime_scope_value("imei", stored_scope.get("imei"), runtime_identity.get("imei"), identity_source.get("imei")),
        "imsi": _first_runtime_scope_value("imsi", stored_scope.get("imsi"), runtime_identity.get("imsi"), identity_source.get("imsi")),
        "sim_id": _first_runtime_scope_value("sim_id", stored_scope.get("sim_id"), runtime_identity.get("sim_id"), identity_source.get("sim_id")),
    }
    return {key: value for key, value in scope.items() if value}


def runtime_session_scope_key(mapping: Mapping[str, Any] | None) -> str:
    scope = build_runtime_session_scope(mapping)
    parts = [f"{key}={scope[key]}" for key in _RUNTIME_SESSION_SCOPE_KEYS if scope.get(key)]
    return "|".join(parts)


def runtime_device_namespace_key(mapping: Mapping[str, Any] | None) -> str:
    """Return the CGF namespace for one physical modem.

    Provider, SIM, firmware and other mutable session facts deliberately do
    not participate.  Once a permanent hardware identifier is available it
    is the namespace; before then a target/backend/model key is explicitly
    provisional.
    """

    scope = build_runtime_session_scope(mapping)
    backend = scope.get("backend", "")
    serial = scope.get("serial", "")
    imei = scope.get("imei", "")
    if serial:
        parts = (("backend", backend), ("serial", serial))
    elif imei:
        parts = (("backend", backend), ("imei", imei))
    else:
        parts = (
            ("target", scope.get("target", "")),
            ("backend", backend),
            ("model", scope.get("model", "")),
        )
    return "|".join(f"{key}={value}" for key, value in parts if value)


def runtime_physical_device_changed(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> bool:
    """Return true only for contradictory permanent hardware identity."""

    previous_scope = build_runtime_session_scope(previous)
    current_scope = build_runtime_session_scope(current)
    if not previous_scope or not current_scope:
        return False
    for key in ("serial", "imei"):
        previous_value = previous_scope.get(key, "")
        current_value = current_scope.get(key, "")
        if previous_value and current_value and previous_value != current_value:
            return True
    return False


def parse_runtime_session_scope_key(value: Any) -> dict[str, str]:
    """Decode only canonical scope keys produced by runtime_session_scope_key()."""

    text = str(value or "").strip()
    if not text:
        return {}
    scope: dict[str, str] = {}
    for component in text.split("|"):
        key, separator, raw_value = component.partition("=")
        if (
            separator != "="
            or key not in _RUNTIME_SESSION_SCOPE_KEYS
            or key in scope
            or not raw_value
        ):
            raise ValueError("runtime_session_scope_key_invalid")
        coerced = _coerce_runtime_scope_value(key, raw_value)
        if not coerced:
            raise ValueError("runtime_session_scope_key_invalid")
        scope[key] = coerced
    if runtime_session_scope_key(scope) != text:
        raise ValueError("runtime_session_scope_key_not_canonical")
    return scope


def runtime_session_scope_is_strict_enrichment(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> bool:
    """Return true only when current adds facts without changing or dropping one."""

    previous_scope = build_runtime_session_scope(previous)
    current_scope = build_runtime_session_scope(current)
    if not previous_scope or not current_scope or previous_scope == current_scope:
        return False
    if runtime_session_scope_changed(previous_scope, current_scope):
        return False
    if any(current_scope.get(key) != value for key, value in previous_scope.items()):
        return False
    return runtime_session_scope_key(
        merge_runtime_session_scope(previous_scope, current_scope)
    ) == runtime_session_scope_key(current_scope)


def runtime_session_scope_matches(
    expected: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> bool:
    expected_scope = build_runtime_session_scope(expected)
    current_scope = build_runtime_session_scope(current)
    if not expected_scope and not current_scope:
        return True
    if not expected_scope or not current_scope:
        return False
    shared_strong = [
        key
        for key in _RUNTIME_SESSION_STRONG_KEYS
        if expected_scope.get(key) and current_scope.get(key)
    ]
    for key in _RUNTIME_SESSION_SCOPE_KEYS:
        expected_value = expected_scope.get(key, "")
        current_value = current_scope.get(key, "")
        if expected_value and current_value and expected_value != current_value:
            return False
    if shared_strong:
        return True
    shared_fallback = [
        key
        for key in _RUNTIME_SESSION_FALLBACK_KEYS
        if expected_scope.get(key) and current_scope.get(key)
    ]
    return len(shared_fallback) >= 2


def _specific_scope_model(scope: Mapping[str, str]) -> str:
    model = str(scope.get("model", "") or "").strip().lower()
    if model in _RUNTIME_SESSION_UNKNOWN_VALUES:
        return ""
    generic = {
        str(scope.get("backend", "") or "").strip().lower(),
        str(scope.get("backend_family", "") or "").strip().lower(),
    }
    generic.update(_RUNTIME_SESSION_GENERIC_MODEL_VALUES)
    generic.update(_RUNTIME_SESSION_UNKNOWN_VALUES)
    return "" if model in generic else model


def _runtime_scope_is_routeros(scope: Mapping[str, str]) -> bool:
    backend = str(scope.get("backend", "") or "").strip().lower()
    family = str(scope.get("backend_family", "") or "").strip().lower()
    return backend == "mikrotik" or family == "routeros_at_like"


def runtime_session_scope_changed(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> bool:
    previous_scope = build_runtime_session_scope(previous)
    current_scope = build_runtime_session_scope(current)
    if not previous_scope or not current_scope:
        return False

    # A runtime session change is a safety boundary, not an enrichment event.
    # RouterOS can enrich modem/SIM facts late through MBIM/AT reads while the
    # router target is unchanged; only router serial/target/backend identify a
    # different RouterOS control session.
    routeros_scope = _runtime_scope_is_routeros(previous_scope) or _runtime_scope_is_routeros(current_scope)
    strong_keys = ("serial",) if routeros_scope else _RUNTIME_SESSION_STRONG_KEYS
    for key in strong_keys:
        previous_value = previous_scope.get(key, "")
        current_value = current_scope.get(key, "")
        if previous_value and current_value and previous_value != current_value:
            return True

    previous_target = previous_scope.get("target", "")
    current_target = current_scope.get("target", "")
    if previous_target and current_target and previous_target != current_target:
        return True

    previous_backend = previous_scope.get("backend", "")
    current_backend = current_scope.get("backend", "")
    if previous_backend and current_backend and previous_backend != current_backend:
        return True

    previous_family = previous_scope.get("backend_family", "")
    current_family = current_scope.get("backend_family", "")
    if previous_family and current_family and previous_family != current_family:
        return True

    if not routeros_scope:
        previous_model = _specific_scope_model(previous_scope)
        current_model = _specific_scope_model(current_scope)
        if previous_model and current_model and previous_model != current_model:
            return True
    return False


def merge_runtime_session_scope(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Retain sparse facts while accepting fresh subscription/network changes.

    Strong context evidence is not necessarily immutable: PLMN, IMSI and SIM
    can change on the same router. Device replacement remains with Modem Live.
    """
    previous_scope = build_runtime_session_scope(previous)
    current_scope = build_runtime_session_scope(current)
    if not previous_scope:
        return dict(current_scope)
    if not current_scope:
        return dict(previous_scope)
    merged = dict(previous_scope)
    for key in _RUNTIME_SESSION_SCOPE_KEYS:
        current_value = current_scope.get(key, "")
        if not current_value or current_value.lower() in _RUNTIME_SESSION_UNKNOWN_VALUES:
            continue
        previous_value = merged.get(key, "")
        if key == "model" and previous_value and not _specific_scope_model(current_scope):
            continue
        if previous_value and previous_value.lower() in _RUNTIME_SESSION_UNKNOWN_VALUES:
            previous_value = ""
        if previous_value and previous_value != current_value:
            if key in {"serial", "imei", "target", "backend", "backend_family"}:
                continue
            if key == "model" and runtime_session_scope_changed(previous_scope, current_scope):
                continue
        merged[key] = current_value
    return {key: value for key, value in merged.items() if value}
