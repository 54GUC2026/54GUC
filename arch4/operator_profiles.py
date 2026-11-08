from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Dict, FrozenSet, Mapping
import re

from arch4.app_info import build_runtime_provenance
from arch4.runtime_sidecar_identity import build_expected_sidecar_identity


@dataclass(frozen=True)
class OperatorContext:
    region_key: str
    provider_plmn_map: Mapping[str, str]
    deployed_bands: FrozenSet[int]
    provider_deployed_bands: Mapping[str, FrozenSet[int]]
    provider_nr_deployed_bands: Mapping[str, FrozenSet[int]]

    def provider_name(self, plmn: str) -> str:
        return str(self.provider_plmn_map.get(str(plmn or "").strip(), "Unknown"))




@dataclass(frozen=True)
class ActionAliasTemplate:
    action_id: str
    policy_family: str = ""
    reason_code: str = ""
    reason_text: str = ""
    intent_alias: str = ""
    legacy_intent: str = ""
    objective_alias_template: str = ""
    legacy_objective_template: str = ""
    event_aliases: tuple[str, ...] = ()
    hold_event_aliases: tuple[str, ...] = ()

    def render(self, **context: Any) -> Dict[str, Any]:
        def _fmt(value: str) -> str:
            try:
                return str(value or "").format(**context)
            except Exception:
                return str(value or "")
        return {
            "policy_family": str(self.policy_family or ""),
            "reason_code": str(self.reason_code or ""),
            "reason_text": _fmt(self.reason_text),
            "intent_alias": str(self.intent_alias or ""),
            "legacy_intent": str(self.legacy_intent or ""),
            "objective_alias_id": _fmt(self.objective_alias_template),
            "legacy_objective_id": _fmt(self.legacy_objective_template),
            "event_aliases": list(self.event_aliases),
            "hold_event_aliases": list(self.hold_event_aliases),
        }


@dataclass(frozen=True)
class PolicyTemplateDefinition:
    policy_key: str
    policy_family: str
    name_template: str
    modem_backend: str = ""
    backend_family: str = ""
    full_decision_engine_enabled: bool = False
    family_device_monitor_enabled: bool = False
    night_ca_bonus_only: bool = False
    enable_night_band_training: bool | None = None
    daytime_auto_band_actions_enabled: bool = True
    night_probe_dwell_s: float = 900.0
    night_probe_cooldown_s: float = 600.0
    overnight_recovery_enabled: bool = False


_PROFILE_DATA_PATH = Path(__file__).resolve().parent.parent / "config" / "vendor_neutral_profiles.json"


def _load_profile_data() -> Dict[str, Any]:
    try:
        raw = json.loads(_PROFILE_DATA_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {}


def _make_operator_context(region_key: str, data: Mapping[str, Any]) -> OperatorContext:
    provider_map = data.get("provider_plmn_map") or {}
    deployed = frozenset(int(v) for v in (data.get("deployed_bands") or []))
    provider_deployed = {
        str(k).strip(): frozenset(int(v) for v in (values or []))
        for k, values in dict(data.get("provider_deployed_bands") or {}).items()
    }
    provider_nr_deployed = {
        str(k).strip(): frozenset(int(v) for v in (values or []))
        for k, values in dict(data.get("provider_nr_deployed_bands") or {}).items()
    }
    return OperatorContext(
        region_key=str(region_key or "default"),
        provider_plmn_map={str(k): str(v) for k, v in dict(provider_map).items()},
        deployed_bands=deployed,
        provider_deployed_bands=provider_deployed,
        provider_nr_deployed_bands=provider_nr_deployed,
    )


def _make_policy_template(data: Mapping[str, Any]) -> PolicyTemplateDefinition:
    return PolicyTemplateDefinition(**dict(data))


def _make_action_alias_template(action_id: str, data: Mapping[str, Any]) -> ActionAliasTemplate:
    payload = dict(data)
    payload.setdefault("action_id", str(action_id))
    payload["event_aliases"] = tuple(str(v) for v in payload.get("event_aliases") or ())
    payload["hold_event_aliases"] = tuple(str(v) for v in payload.get("hold_event_aliases") or ())
    return ActionAliasTemplate(**payload)


_PROFILE_DATA = _load_profile_data()

_OPERATOR_CONTEXTS: Dict[str, OperatorContext] = {
    str(key): _make_operator_context(str(key), value)
    for key, value in dict(_PROFILE_DATA.get("operator_contexts") or {}).items()
    if isinstance(value, dict)
}
if not _OPERATOR_CONTEXTS:
    _OPERATOR_CONTEXTS = {
        "rs_default": OperatorContext(
            region_key="rs_default",
            provider_plmn_map={
                "22001": "Yettel",
                "22003": "MTS",
                "22005": "A1",
            },
            deployed_bands=frozenset({1, 3, 20}),
            provider_deployed_bands={
                "22005": frozenset({1, 3, 7, 8, 20, 28}),
            },
            provider_nr_deployed_bands={
                "22005": frozenset({7, 28, 78}),
            },
        )
    }

PRIMARY_OPERATOR_CONTEXT = _OPERATOR_CONTEXTS.get("rs_default") or next(iter(_OPERATOR_CONTEXTS.values()))
DEFAULT_OPERATOR_CONTEXT = PRIMARY_OPERATOR_CONTEXT

_BACKEND_FAMILY_ALIASES: Dict[str, str] = {
    str(k).strip().lower(): str(v).strip().lower()
    for k, v in dict(_PROFILE_DATA.get("backend_family_aliases") or {}).items()
}
if not _BACKEND_FAMILY_ALIASES:
    _BACKEND_FAMILY_ALIASES = {
        "b818": "huawei_hilink_like",
        "b311": "huawei_hilink_like",
        "b535": "huawei_hilink_like",
        "b530": "huawei_hilink_like",
        "h112": "huawei_hilink_like",
        "h122": "huawei_hilink_like",
        "h155": "huawei_hilink_like",
        "huawei": "huawei_hilink_like",
        "mikrotik": "routeros_at_like",
        "routeros": "routeros_at_like",
        "routeros_api": "routeros_at_like",
        "openwrt": "openwrt_modemmanager_like",
        "mc888": "zte_goform_like",
        "g5b": "zte_goform_like",
        "mf296r": "zte_goform_like",
        "zte": "zte_goform_like",
        "zte_goform": "zte_goform_like",
        "zte_zwrt": "zte_zwrt_ubus_like",
        "nr5101": "zyxel_webui_like",
        "fwa510": "zyxel_webui_like",
        "model": "modem_model_like",
    }

FAMILY_ROUTEROS_AT = "routeros_at_like"
FAMILY_HUAWEI_HILINK = "huawei_hilink_like"
FAMILY_ZTE_GOFORM = "zte_goform_like"
FAMILY_ZTE_WEBUI = "zte_webui_like"
FAMILY_ZTE_ZWRT = "zte_zwrt_ubus_like"
FAMILY_OPENWRT_MM = "openwrt_modemmanager_like"
FAMILY_ZYXEL_WEBUI = "zyxel_webui_like"
FAMILY_MODEM_MODEL = "modem_model_like"
FAMILY_UNKNOWN = "unknown"


FAMILY_LABELS: Dict[str, str] = {
    str(k).strip().lower(): str(v)
    for k, v in dict(_PROFILE_DATA.get("family_labels") or {}).items()
}
if not FAMILY_LABELS:
    FAMILY_LABELS = {
        FAMILY_ROUTEROS_AT: "RouterOS / AT-like",
        FAMILY_HUAWEI_HILINK: "Huawei HiLink-like",
        FAMILY_ZTE_GOFORM: "ZTE GoForm family",
        FAMILY_ZTE_WEBUI: "ZTE WebUI family",
        FAMILY_ZTE_ZWRT: "ZTE OpenWrt-like family",
        FAMILY_OPENWRT_MM: "OpenWrt / ModemManager",
        FAMILY_ZYXEL_WEBUI: "Zyxel WebUI family",
        FAMILY_MODEM_MODEL: "Model / emulator family",
        FAMILY_UNKNOWN: "Unknown family",
    }

FAMILY_SUPPORT_TEXT: Dict[str, str] = {
    str(k).strip().lower(): str(v)
    for k, v in dict(_PROFILE_DATA.get("family_support_text") or {}).items()
}


def family_label(family: str) -> str:
    key = str(family or "unknown").strip().lower() or "unknown"
    return str(FAMILY_LABELS.get(key, key))


def family_support_text(family: str) -> str:
    key = str(family or "unknown").strip().lower() or "unknown"
    return str(FAMILY_SUPPORT_TEXT.get(key, ""))


def _load_action_policy_registry() -> Dict[tuple[str, str], Dict[str, str]]:
    raw_section = dict(_PROFILE_DATA.get("provider_action_policy_registry") or {})
    out: Dict[tuple[str, str], Dict[str, str]] = {}
    for key, value in raw_section.items():
        if not isinstance(value, dict):
            continue
        plmn, sep, name = str(key).partition("|")
        if not sep:
            continue
        out[(plmn.strip(), name.strip().lower())] = {str(k): str(v) for k, v in value.items()}
    return out


PROVIDER_ACTION_POLICY_REGISTRY: Dict[tuple[str, str], Dict[str, str]] = _load_action_policy_registry()


def _load_action_alias_templates() -> Dict[str, ActionAliasTemplate]:
    raw_section = dict(_PROFILE_DATA.get("action_alias_templates") or {})
    out: Dict[str, ActionAliasTemplate] = {}
    for key, value in raw_section.items():
        if isinstance(value, dict):
            out[str(key)] = _make_action_alias_template(str(key), value)
    return out


ACTION_ALIAS_TEMPLATES: Dict[str, ActionAliasTemplate] = _load_action_alias_templates()


COOLDOWN_ALIASES: Dict[str, Dict[str, tuple[str, ...]]] = {
    "scalar": {
        str(k): tuple(str(v) for v in values)
        for k, values in dict((_PROFILE_DATA.get("cooldown_aliases") or {}).get("scalar") or {}).items()
    },
    "keyed": {
        str(k): tuple(str(v) for v in values)
        for k, values in dict((_PROFILE_DATA.get("cooldown_aliases") or {}).get("keyed") or {}).items()
    },
}
if not COOLDOWN_ALIASES["scalar"]:
    COOLDOWN_ALIASES["scalar"] = {
        "hilink_rotate": ("hilink_rotate_cooldown_until", "huawei_rotate_cooldown_until"),
        "hilink_rotate_success_quiet": ("hilink_rotate_success_quiet_until", "huawei_rotate_success_quiet_until"),
        "mikrotik_rotate": ("mikrotik_rotate_cooldown_until", "routeros_rotate_cooldown_until"),
        "mikrotik_rotate_success_quiet": ("mikrotik_rotate_success_quiet_until", "routeros_rotate_success_quiet_until"),
    }
if not COOLDOWN_ALIASES["keyed"]:
    COOLDOWN_ALIASES["keyed"] = {
        "mikrotik_explore_retry": ("mikrotik_explore_retry_until", "routeros_explore_retry_until"),
        "cell_lock_retry": ("cell_lock_retry_until", "routeros_cell_lock_retry_until"),
    }

RUNTIME_STATUS_ALIASES: Dict[str, tuple[str, ...]] = {
    str(k): tuple(str(v) for v in values)
    for k, values in dict(_PROFILE_DATA.get("status_aliases") or {}).items()
}
if not RUNTIME_STATUS_ALIASES:
    RUNTIME_STATUS_ALIASES = {
        "hilink_rotate_cooldown_active": ("huawei_rotate_cooldown_active",),
        "hilink_rotate_quiet_active": ("huawei_rotate_quiet_active",),
        "mikrotik_rotate_cooldown_active": ("routeros_rotate_cooldown_active",),
        "mikrotik_rotate_quiet_active": ("routeros_rotate_quiet_active",),
        "mikrotik_rotate_budget_used": ("routeros_rotate_budget_used",),
        "mikrotik_explore_retry_objectives": ("routeros_explore_retry_objectives",),
        "a1_antithrash_hold_active": ("hilink_antithrash_hold_active", "huawei_antithrash_hold_active", "family_antithrash_hold_active"),
        "a1_antithrash_freeze_bands": ("hilink_antithrash_freeze_bands", "huawei_antithrash_freeze_bands", "family_antithrash_freeze_bands"),
        "a1_morning_active": ("hilink_morning_active", "huawei_morning_active", "family_morning_active"),
        "a1_hard_night_freeze_active": ("hilink_hard_night_freeze_active", "huawei_hard_night_freeze_active", "family_hard_night_freeze_active"),
    }

EVENT_ALIASES = {
    str(k): tuple(str(a) for a in v)
    for k, v in (_PROFILE_DATA.get("event_aliases") or {}).items()
    if isinstance(v, (list, tuple))
}

STATUS_TEXT_TEMPLATES = {
    str(k): str(v)
    for k, v in (_PROFILE_DATA.get("status_text_templates") or {}).items()
    if isinstance(v, str)
}

EVENT_TEXT_TEMPLATES = {
    str(k).strip(): str(v)
    for k, v in (_PROFILE_DATA.get("event_text_templates") or {}).items()
    if isinstance(v, str)
}



def _load_policy_templates(section_name: str) -> Dict[str, PolicyTemplateDefinition]:
    raw_section = dict(_PROFILE_DATA.get(section_name) or {})
    out: Dict[str, PolicyTemplateDefinition] = {}
    for key, value in raw_section.items():
        if isinstance(value, dict):
            out[str(key)] = _make_policy_template(value)
    return out


FAMILY_POLICY_DEFAULTS: Dict[str, PolicyTemplateDefinition] = _load_policy_templates("family_policy_defaults")
if not FAMILY_POLICY_DEFAULTS:
    FAMILY_POLICY_DEFAULTS = {
        "routeros_at_like": PolicyTemplateDefinition(
            policy_key="GENERIC",
            policy_family="GENERIC",
            name_template="{provider}-{backend}",
            backend_family="routeros_at_like",
            full_decision_engine_enabled=False,
            family_device_monitor_enabled=False,
            night_ca_bonus_only=False,
            daytime_auto_band_actions_enabled=True,
        ),
        "openwrt_modemmanager_like": PolicyTemplateDefinition(
            policy_key="GENERIC",
            policy_family="GENERIC",
            name_template="{provider}-{backend}",
            backend_family="openwrt_modemmanager_like",
            full_decision_engine_enabled=False,
            family_device_monitor_enabled=False,
            night_ca_bonus_only=False,
            daytime_auto_band_actions_enabled=True,
        ),
        "zte_goform_like": PolicyTemplateDefinition(
            policy_key="GENERIC",
            policy_family="GENERIC",
            name_template="{provider}-{backend}",
            backend_family="zte_goform_like",
            full_decision_engine_enabled=False,
            family_device_monitor_enabled=False,
            night_ca_bonus_only=False,
            daytime_auto_band_actions_enabled=True,
        ),
        "zte_zwrt_ubus_like": PolicyTemplateDefinition(
            policy_key="GENERIC",
            policy_family="GENERIC",
            name_template="{provider}-{backend}",
            backend_family="zte_zwrt_ubus_like",
            full_decision_engine_enabled=False,
            family_device_monitor_enabled=False,
            night_ca_bonus_only=False,
            daytime_auto_band_actions_enabled=True,
        ),
        "zyxel_webui_like": PolicyTemplateDefinition(
            policy_key="GENERIC",
            policy_family="GENERIC",
            name_template="{provider}-{backend}",
            backend_family="zyxel_webui_like",
            full_decision_engine_enabled=False,
            family_device_monitor_enabled=False,
            night_ca_bonus_only=False,
            daytime_auto_band_actions_enabled=True,
        ),
        "modem_model_like": PolicyTemplateDefinition(
            policy_key="GENERIC",
            policy_family="GENERIC",
            name_template="{provider}-{backend}",
            backend_family="modem_model_like",
            full_decision_engine_enabled=False,
            family_device_monitor_enabled=False,
            night_ca_bonus_only=False,
            daytime_auto_band_actions_enabled=True,
        ),
        "huawei_hilink_like": PolicyTemplateDefinition(
            policy_key="GENERIC_HUAWEI",
            policy_family="GENERIC",
            name_template="{provider}-Huawei-conservative",
            backend_family="huawei_hilink_like",
            full_decision_engine_enabled=True,
            family_device_monitor_enabled=False,
            night_ca_bonus_only=False,
            daytime_auto_band_actions_enabled=False,
        ),
        "unknown": PolicyTemplateDefinition(
            policy_key="GENERIC",
            policy_family="GENERIC",
            name_template="{provider}-{backend}",
            backend_family="unknown",
            full_decision_engine_enabled=False,
            family_device_monitor_enabled=False,
            night_ca_bonus_only=False,
            daytime_auto_band_actions_enabled=True,
        ),
    }

BACKEND_POLICY_DEFAULTS: Dict[str, PolicyTemplateDefinition] = _load_policy_templates("backend_policy_defaults")
if not BACKEND_POLICY_DEFAULTS:
    BACKEND_POLICY_DEFAULTS = {
        "b818": PolicyTemplateDefinition(
            policy_key="GENERIC_B818",
            policy_family="GENERIC",
            name_template="{provider}-b818-conservative",
            modem_backend="b818",
            backend_family="huawei_hilink_like",
            full_decision_engine_enabled=True,
            family_device_monitor_enabled=True,
            night_ca_bonus_only=False,
            daytime_auto_band_actions_enabled=False,
        ),
    }


def _load_pair_policy_templates(section_name: str) -> Dict[tuple[str, str], PolicyTemplateDefinition]:
    raw_section = dict(_PROFILE_DATA.get(section_name) or {})
    out: Dict[tuple[str, str], PolicyTemplateDefinition] = {}
    for key, value in raw_section.items():
        if not isinstance(value, dict):
            continue
        plmn, sep, name = str(key).partition("|")
        if not sep:
            continue
        out[(plmn.strip(), name.strip().lower())] = _make_policy_template(value)
    return out


PROVIDER_BACKEND_POLICY_OVERRIDES: Dict[tuple[str, str], PolicyTemplateDefinition] = _load_pair_policy_templates("provider_backend_policy_overrides")
if not PROVIDER_BACKEND_POLICY_OVERRIDES:
    PROVIDER_BACKEND_POLICY_OVERRIDES = {
        ("22005", "b818"): PolicyTemplateDefinition(
            policy_key="A1_B818_FORCED",
            policy_family="PROVIDER_SPECIFIC",
            name_template="A1-b818-conservative",
            modem_backend="b818",
            backend_family="huawei_hilink_like",
            full_decision_engine_enabled=True,
            family_device_monitor_enabled=True,
            night_ca_bonus_only=True,
            enable_night_band_training=True,
            daytime_auto_band_actions_enabled=True,
            overnight_recovery_enabled=True,
        ),
    }

PROVIDER_FAMILY_POLICY_OVERRIDES: Dict[tuple[str, str], PolicyTemplateDefinition] = _load_pair_policy_templates("provider_family_policy_overrides")
if not PROVIDER_FAMILY_POLICY_OVERRIDES:
    PROVIDER_FAMILY_POLICY_OVERRIDES = {
        ("22005", "huawei_hilink_like"): PolicyTemplateDefinition(
            policy_key="GENERIC_HUAWEI_A1",
            policy_family="PROVIDER_SPECIFIC",
            name_template="A1-Huawei-conservative",
            backend_family="huawei_hilink_like",
            full_decision_engine_enabled=True,
            family_device_monitor_enabled=False,
            night_ca_bonus_only=False,
            daytime_auto_band_actions_enabled=False,
            overnight_recovery_enabled=True,
        ),
    }


def available_operator_contexts() -> Dict[str, OperatorContext]:
    return dict(_OPERATOR_CONTEXTS)


def policy_template_sources() -> Dict[str, Any]:
    return {
        "path": str(_PROFILE_DATA_PATH),
        "version": str(_PROFILE_DATA.get("version") or "builtin"),
        "contexts": sorted(_OPERATOR_CONTEXTS.keys()),
        "families": sorted(FAMILY_POLICY_DEFAULTS.keys()),
        "backend_defaults": sorted(BACKEND_POLICY_DEFAULTS.keys()),
        "provider_backend_overrides": sorted([f"{k[0]}|{k[1]}" for k in PROVIDER_BACKEND_POLICY_OVERRIDES.keys()]),
        "provider_family_overrides": sorted([f"{k[0]}|{k[1]}" for k in PROVIDER_FAMILY_POLICY_OVERRIDES.keys()]),
    }


def lookup_operator_context(region_key: str | None = None) -> OperatorContext:
    key = str(region_key or "").strip().lower()
    if key in {"", "default", "rs", "rs_default"}:
        return DEFAULT_OPERATOR_CONTEXT
    return _OPERATOR_CONTEXTS.get(key, DEFAULT_OPERATOR_CONTEXT)


def resolve_provider_name(plmn: str, *, region_key: str | None = None) -> str:
    return lookup_operator_context(region_key).provider_name(plmn)


def resolve_provider_plmn_from_name(name: str, *, region_key: str | None = None) -> str:
    """Resolve known regional operator display names to PLMN.

    Match exact aliases from the configured operator context and supported
    firmware suffix forms. Unknown names remain unresolved; this is not a
    generic provider guesser.
    """

    text = str(name or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    if not normalized:
        return ""
    context = lookup_operator_context(region_key)
    for plmn, provider_name in dict(context.provider_plmn_map or {}).items():
        provider_norm = re.sub(r"[^a-z0-9]+", " ", str(provider_name or "").lower()).strip()
        if not provider_norm:
            continue
        aliases = {
            provider_norm,
            f"{provider_norm} srb",
            f"{provider_norm} serbia",
            f"{provider_norm} srbija",
        }
        if provider_norm == "a1":
            aliases.update({"a1 srb", "a1 sr", "a1 serbia", "a1 srbija"})
        if normalized in aliases:
            return str(plmn)
    return ""


def infer_profile_family(profile_name: str) -> str:
    txt = str(profile_name or "").strip().lower()
    if not txt:
        return "unknown"
    if "b818" in txt or "huawei" in txt or "hilink" in txt or "b535" in txt:
        return "huawei_hilink_like"
    if "mikrotik" in txt or "routeros" in txt:
        return "routeros_at_like"
    if "zte" in txt or "mc888" in txt or "mf296" in txt or "goform" in txt:
        return "zte_goform_like"
    if "openwrt" in txt or "modemmanager" in txt:
        return "openwrt_modemmanager_like"
    return "unknown"


def derive_runtime_event_metadata(event: Mapping[str, Any]) -> Dict[str, Any]:
    src = dict(event or {})
    out: Dict[str, Any] = {}
    backend = str(src.get("modem_backend") or src.get("backend") or "").strip().lower()
    family = str(src.get("backend_family") or src.get("family") or "").strip().lower()
    if not family:
        if backend:
            family = infer_backend_family(backend)
        else:
            family = infer_profile_family(src.get("profile") or "")
    if family and family != "unknown":
        out["family"] = family
        label = family_label(family)
        if label:
            out["family_label"] = label
        support = family_support_text(family)
        if support:
            out["family_support_text"] = support
    profile_name = str(src.get("profile") or "").strip()
    if profile_name:
        out.setdefault("profile_name", profile_name)
    policy_family = str(src.get("policy_family") or "").strip()
    if policy_family:
        out["policy_family"] = policy_family
    provider_plmn = str(src.get("provider_plmn") or "").strip()
    if provider_plmn:
        out["provider_plmn"] = provider_plmn
        out.setdefault("provider_name", resolve_provider_name(provider_plmn))
    provider_name = str(src.get("provider_name") or "").strip()
    if provider_name:
        out["provider_name"] = provider_name
    if backend:
        out["modem_backend"] = backend
    return out



def derive_runtime_sidecar_metadata(payload: Mapping[str, Any], defaults: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    if isinstance(defaults, Mapping):
        merged.update({str(k): v for k, v in defaults.items() if v not in (None, "")})
    if isinstance(payload, Mapping):
        merged.update({str(k): v for k, v in payload.items() if v not in (None, "")})
    meta = derive_runtime_event_metadata(merged)
    out: Dict[str, Any] = dict(meta)
    family = str(meta.get("family") or "").strip().lower()
    if family and family != "unknown":
        out.setdefault("backend_family", family)
    profile_name = str(merged.get("profile_name") or merged.get("profile") or "").strip()
    if profile_name:
        out.setdefault("profile_name", profile_name)
    role = str(
        merged.get("sidecar_role")
        or merged.get("runtime_sidecar_role")
        or merged.get("writer_role")
        or ""
    ).strip()
    runtime_dir = str(
        merged.get("runtime_dir")
        or merged.get("output_dir")
        or ""
    ).strip()
    memory_dir = str(
        merged.get("memory_dir")
        or ""
    ).strip()
    extra = {
        "modem_backend": str(merged.get("modem_backend") or ""),
        "backend_family": str(out.get("backend_family") or ""),
        "profile_name": profile_name,
    }
    identity = build_expected_sidecar_identity(
        modem_backend=str(merged.get("modem_backend") or ""),
        backend_family=str(out.get("backend_family") or ""),
        runtime_dir=runtime_dir,
        memory_dir=memory_dir,
        instance_id=str(merged.get("instance_id") or ""),
        profile_name=profile_name,
    )
    if identity:
        out["sidecar_identity"] = identity
    out["provenance"] = build_runtime_provenance(
        runtime_dir=runtime_dir,
        memory_dir=memory_dir,
        sidecar_role=role,
        extra=extra,
    )
    return out


def enrich_runtime_sidecar_payload(payload: Mapping[str, Any] | None, defaults: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(payload or {}) if isinstance(payload, Mapping) else {}
    out.update(derive_runtime_sidecar_metadata(out, defaults))
    return out


def default_deployed_bands(*, region_key: str | None = None) -> FrozenSet[int]:
    return lookup_operator_context(region_key).deployed_bands


def deployed_bands_for_provider(provider_plmn: str, *, region_key: str | None = None) -> FrozenSet[int]:
    context = lookup_operator_context(region_key)
    plmn = str(provider_plmn or "").strip()
    if not plmn:
        return frozenset()
    scoped = context.provider_deployed_bands.get(plmn)
    if scoped:
        return scoped
    return frozenset()


def nr_deployed_bands_for_provider(provider_plmn: str, *, region_key: str | None = None) -> FrozenSet[int]:
    context = lookup_operator_context(region_key)
    plmn = str(provider_plmn or "").strip()
    if not plmn:
        return frozenset()
    scoped = context.provider_nr_deployed_bands.get(plmn)
    if scoped:
        return scoped
    return frozenset()


def _normalize_backend(backend: str) -> str:
    return str(backend or "").strip().lower()


def infer_backend_family(backend: str) -> str:
    txt = _normalize_backend(backend)
    if txt in {
        "huawei_hilink_like",
        "routeros_at_like",
        "zte_goform_like",
        "openwrt_modemmanager_like",
    }:
        return txt
    alias = _BACKEND_FAMILY_ALIASES.get(txt)
    if alias:
        return alias
    try:
        from arch4.modem_live.profile_catalog import modem_backend_family
        fam = str(modem_backend_family(txt) or "").strip().lower()
        if fam and fam != "unknown":
            return fam
    except Exception:
        pass
    profile_family = infer_profile_family(txt)
    if profile_family != "unknown":
        return profile_family
    return "unknown"

def resolve_policy_template(provider_plmn: str, modem_backend: str, *, supports_band_selection: bool = False) -> Dict[str, Any]:
    """
    Reporting/audit projection adapter for the live runtime selector.

    Runtime authority is ``interfaces.select_policy_profile()`` plus
    ``policy.templates.build_policy_template_overrides()`` followed by the
    ConfigGeneration publication boundary. This helper is a permanent
    read-only reporting adapter, so reporting cannot become a second
    policy-resolution owner.
    """
    from arch4.interfaces import select_policy_profile

    plmn = str(provider_plmn or "").strip()
    backend = _normalize_backend(modem_backend)
    family = infer_backend_family(backend)
    profile = select_policy_profile(plmn, backend)

    enable_night_band_training = bool(profile.enable_night_band_training)
    if not enable_night_band_training and supports_band_selection:
        enable_night_band_training = True

    return {
        "name": profile.name,
        "policy_key": profile.policy_key,
        "policy_family": profile.policy_family,
        "provider_plmn": profile.provider_plmn,
        "provider_name": profile.provider_name,
        "modem_backend": profile.modem_backend,
        "b818_use_full_brain": bool(profile.b818_use_full_brain),
        "b818_keep_device_monitor": bool(profile.b818_keep_device_monitor),
        "night_ca_bonus_only": bool(profile.night_ca_bonus_only),
        "enable_night_band_training": bool(enable_night_band_training),
        "b818_daytime_auto_band_actions": bool(profile.b818_daytime_auto_band_actions),
        "night_probe_dwell_s": float(profile.night_probe_dwell_s),
        "night_probe_cooldown_s": float(profile.night_probe_cooldown_s),
        "a1_overnight_recovery": bool(profile.a1_overnight_recovery),
        "backend_family": family,
    }


def resolve_action_policy_registry(provider_plmn: str, backend: str) -> Dict[str, str]:
    provider_key = str(provider_plmn or "").strip()
    backend_key = str(backend or "").strip().lower()
    family_key = infer_backend_family(backend_key)
    return dict(
        PROVIDER_ACTION_POLICY_REGISTRY.get((provider_key, backend_key))
        or PROVIDER_ACTION_POLICY_REGISTRY.get((provider_key, family_key))
        or {}
    )


def family_profile_sources() -> Dict[str, Any]:
    return {
        "version": str(_PROFILE_DATA.get("version") or "builtin"),
        "family_labels": sorted(FAMILY_LABELS.keys()),
        "family_support_text": sorted(FAMILY_SUPPORT_TEXT.keys()),
        "provider_action_policy_registry": sorted(
            f"{plmn}|{name}" for plmn, name in PROVIDER_ACTION_POLICY_REGISTRY.keys()
        ),
    }


def render_action_alias_template(action_id: str, **context: Any) -> Dict[str, Any]:
    template = ACTION_ALIAS_TEMPLATES.get(str(action_id or "").strip())
    if template is None:
        return {}
    return template.render(**context)




def cooldown_alias_definitions() -> Dict[str, Any]:
    return {
        "path": str(_PROFILE_DATA_PATH),
        "version": str(_PROFILE_DATA.get("version") or "builtin"),
        "scalar": {k: list(v) for k, v in COOLDOWN_ALIASES["scalar"].items()},
        "keyed": {k: list(v) for k, v in COOLDOWN_ALIASES["keyed"].items()},
    }


def runtime_status_alias_sources() -> Dict[str, Any]:
    return {
        "path": str(_PROFILE_DATA_PATH),
        "version": str(_PROFILE_DATA.get("version") or "builtin"),
        "aliases": {k: list(v) for k, v in RUNTIME_STATUS_ALIASES.items()},
    }


def apply_runtime_status_aliases(status: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(status or {})
    for source_key, aliases in RUNTIME_STATUS_ALIASES.items():
        if source_key not in out:
            continue
        value = out[source_key]
        for alias in aliases:
            out.setdefault(str(alias), value)
    return out


def cooldown_alias_keys(namespace: str, *, keyed: bool = False) -> tuple[str, ...]:
    section = COOLDOWN_ALIASES["keyed" if keyed else "scalar"]
    return tuple(section.get(str(namespace), ()))

def action_alias_template_sources() -> Dict[str, Any]:
    return {
        "version": str(_PROFILE_DATA.get("version") or "builtin"),
        "templates": sorted(ACTION_ALIAS_TEMPLATES.keys()),
    }


def runtime_event_alias_sources() -> Dict[str, Any]:
    return {
        "path": str(_PROFILE_DATA_PATH),
        "version": str(_PROFILE_DATA.get("version") or "builtin"),
        "aliases": {k: list(v) for k, v in EVENT_ALIASES.items()},
    }


def runtime_event_text_sources() -> Dict[str, Any]:
    return {
        "path": str(_PROFILE_DATA_PATH),
        "version": str(_PROFILE_DATA.get("version") or "builtin"),
        "templates": dict(EVENT_TEXT_TEMPLATES),
    }


def runtime_status_text_sources() -> Dict[str, Any]:
    return {
        "path": str(_PROFILE_DATA_PATH),
        "version": str(_PROFILE_DATA.get("version") or "builtin"),
        "templates": dict(STATUS_TEXT_TEMPLATES),
    }


def runtime_event_aliases(kind: str) -> tuple[str, ...]:
    return tuple(EVENT_ALIASES.get(str(kind or ""), ()))


def apply_runtime_event_texts(event: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(event or {})
    kind = str(out.get("kind") or "")
    text = EVENT_TEXT_TEMPLATES.get(kind)
    if text:
        out.setdefault("kind_label", str(text))
    legacy_kind = str(out.get("legacy_kind") or "")
    legacy_text = EVENT_TEXT_TEMPLATES.get(legacy_kind)
    if legacy_text:
        out.setdefault("legacy_kind_label", str(legacy_text))
    return out


def apply_runtime_status_texts(status: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(status or {})
    for key, text in STATUS_TEXT_TEMPLATES.items():
        if bool(out.get(key, False)):
            label_key = f"{key}_label"
            out.setdefault(label_key, text)
    return out


def build_runtime_event_records(base_event: Mapping[str, Any]) -> list[Dict[str, Any]]:
    base = dict(base_event or {})
    for key, value in derive_runtime_event_metadata(base).items():
        base.setdefault(key, value)
    base = apply_runtime_event_texts(base)
    kind = str(base.get("kind") or "")
    out = [base]
    for alias in runtime_event_aliases(kind):
        alias_event = apply_runtime_event_texts({**base, "kind": alias, "event": alias, "legacy_kind": kind})
        out.append(alias_event)
    return out
