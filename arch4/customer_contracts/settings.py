"""Typed settings contracts at permanent product-customer boundaries.

These values deliberately do not share a persistence object.  Each product
owner persists only its own contract; the types here are neutral transfer
objects used by the external-settings decoder and composition roots.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from types import MappingProxyType
from typing import Mapping


TRAFFIC_PLAN_DEFAULT_ALLOWANCE_GB = 1000.0
TRAFFIC_PLAN_DEFAULT_CYCLE_START_DAY = 1


def _finite(value: object, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _text(value: object) -> str:
    return str(value or "").strip()


def is_scrubbed_connection_user(value: object) -> bool:
    """Return true for privacy placeholders rather than a real login name."""

    token = _text(value).lower()
    if not token:
        return False
    compact = "".join(character for character in token if character.isalnum())
    return "scrubbed" in compact or compact in {
        "redacted",
        "masked",
        "hidden",
    }


def traffic_plan_scope_id(
    *,
    subscription_key: str,
    modem_backend: str = "",
    provider_name: str = "",
    provider_plmn: str = "",
    profile_id: str = "",
) -> str:
    """Build the exact subscription/network/profile traffic-plan identity."""
    if not re.fullmatch(r"[a-f0-9]{64}", subscription_key):
        raise ValueError("traffic_plan_confirmed_subscription_required")

    def part(value: object) -> str:
        return _text(value).lower().replace("|", " ").replace("=", "-")

    backend = part(modem_backend)
    profile = part(profile_id)
    plmn = part(provider_plmn)
    provider = part(provider_name)
    if not backend or not profile or not (plmn or provider):
        return ""
    pieces = [f"backend={backend}"]
    pieces.append(f"plmn={plmn}" if plmn else f"provider={provider}")
    pieces.append(f"profile={profile}")
    pieces.append(f"sim={subscription_key}")
    return "|".join(pieces)


@dataclass(frozen=True, slots=True)
class CoreRuntimeDefaults:
    rsrq_baseline_default_db: float = -10.0
    rtt_baseline_default_ms: float = 55.0

    def __post_init__(self) -> None:
        rsrq = _finite(
            self.rsrq_baseline_default_db,
            field="rsrq_baseline_default_db",
        )
        rtt = _finite(
            self.rtt_baseline_default_ms,
            field="rtt_baseline_default_ms",
        )
        if not -40.0 <= rsrq <= 0.0:
            raise ValueError("rsrq_baseline_default_db is outside -40..0 dB")
        if not 1.0 <= rtt <= 10_000.0:
            raise ValueError("rtt_baseline_default_ms is outside 1..10000 ms")
        object.__setattr__(self, "rsrq_baseline_default_db", rsrq)
        object.__setattr__(self, "rtt_baseline_default_ms", rtt)


@dataclass(frozen=True, slots=True)
class NGUIConnectionHint:
    profile_id: str
    address: str
    username: str = ""
    router_serial: str = ""
    network_country: str = ""
    network_provider: str = ""
    network_bands: str = ""

    def __post_init__(self) -> None:
        profile_id = _text(self.profile_id)
        address = _text(self.address)
        username = _text(self.username)
        router_serial = _text(self.router_serial)
        if not profile_id:
            raise ValueError("connection hint requires exact profile_id")
        if not address:
            raise ValueError("connection hint requires address")
        if is_scrubbed_connection_user(username):
            username = ""
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "address", address)
        object.__setattr__(self, "username", username)
        object.__setattr__(self, "router_serial", router_serial)
        from arch4.customer_contracts.network_suggestions import NetworkSuggestions
        suggestions = NetworkSuggestions(self.network_country, self.network_provider, self.network_bands)
        for name in ("country", "provider", "bands"):
            object.__setattr__(self, "network_" + name, getattr(suggestions, name))


@dataclass(frozen=True, slots=True)
class NGUISettings:
    last_profile_id: str = ""
    connection_hints: Mapping[str, NGUIConnectionHint] = MappingProxyType({})

    def __post_init__(self) -> None:
        hints: dict[str, NGUIConnectionHint] = {}
        for key, raw in dict(self.connection_hints or {}).items():
            hint = raw if isinstance(raw, NGUIConnectionHint) else NGUIConnectionHint(**raw)
            if _text(key) != hint.profile_id:
                raise ValueError("connection hint key must equal exact profile_id")
            hints[hint.profile_id] = hint
        last_profile_id = _text(self.last_profile_id)
        if last_profile_id and last_profile_id not in hints:
            # The last exact profile can be retained without saved credentials.
            # It is a selection hint only and has no connect authority.
            pass
        object.__setattr__(self, "last_profile_id", last_profile_id)
        object.__setattr__(self, "connection_hints", MappingProxyType(hints))


@dataclass(frozen=True, slots=True)
class TrafficPlan:
    scope_id: str
    modem_backend: str
    provider_name: str
    provider_plmn: str
    profile_id: str
    subscription_key: str
    monthly_allowance_gb: float
    cycle_start_day: int
    override_enabled: bool = True

    def __post_init__(self) -> None:
        expected = traffic_plan_scope_id(
            subscription_key=self.subscription_key,
            modem_backend=self.modem_backend,
            provider_name=self.provider_name,
            provider_plmn=self.provider_plmn,
            profile_id=self.profile_id,
        )
        if not expected or _text(self.scope_id) != expected:
            raise ValueError("traffic plan scope_id does not match its exact scope")
        allowance = _finite(
            self.monthly_allowance_gb,
            field="monthly_allowance_gb",
        )
        if allowance < 0.0:
            raise ValueError("monthly_allowance_gb cannot be negative")
        try:
            cycle_day = int(self.cycle_start_day)
        except (TypeError, ValueError) as exc:
            raise ValueError("cycle_start_day must be an integer") from exc
        if not 1 <= cycle_day <= 31:
            raise ValueError("cycle_start_day is outside 1..31")
        object.__setattr__(self, "scope_id", expected)
        object.__setattr__(self, "modem_backend", _text(self.modem_backend).lower())
        object.__setattr__(self, "provider_name", _text(self.provider_name))
        object.__setattr__(self, "provider_plmn", _text(self.provider_plmn))
        object.__setattr__(self, "profile_id", _text(self.profile_id))
        object.__setattr__(self, "monthly_allowance_gb", allowance)
        object.__setattr__(self, "cycle_start_day", cycle_day)
        object.__setattr__(self, "override_enabled", bool(self.override_enabled))


@dataclass(frozen=True, slots=True)
class FoundationOperatorSettings:
    operator_fields: Mapping[str, str] = MappingProxyType({})
    traffic_plans: Mapping[str, TrafficPlan] = MappingProxyType({})
    apn_profiles: Mapping[str, "SavedApnProfile"] = MappingProxyType({})
    unassigned_traffic_plans: Mapping[str, Mapping[str, object]] = MappingProxyType({})

    def __post_init__(self) -> None:
        fields = {
            _text(key): _text(value)
            for key, value in dict(self.operator_fields or {}).items()
            if _text(key) and _text(value)
        }
        plans: dict[str, TrafficPlan] = {}
        for key, raw in dict(self.traffic_plans or {}).items():
            plan = raw if isinstance(raw, TrafficPlan) else TrafficPlan(**raw)
            if _text(key) != plan.scope_id:
                raise ValueError("traffic plan key must equal scope_id")
            plans[plan.scope_id] = plan
        object.__setattr__(self, "operator_fields", MappingProxyType(fields))
        object.__setattr__(self, "traffic_plans", MappingProxyType(plans))
        # Preserved old serialized records have no live lookup authority. They
        # can only be assigned by a separately approved import operation.
        unassigned = {}
        for key, raw in self.unassigned_traffic_plans.items():
            if not isinstance(raw, Mapping) or raw.get("scope_id") != key or "subscription_key" in raw:
                raise ValueError("unassigned_traffic_plan_record_invalid")
            unassigned[key] = MappingProxyType(dict(raw))
        object.__setattr__(self, "unassigned_traffic_plans", MappingProxyType(unassigned))
        from .apn_profiles import MAX_APN_PROFILES, SavedApnProfile
        profiles = {}
        for key, raw in dict(self.apn_profiles or {}).items():
            profile = raw if type(raw) is SavedApnProfile else SavedApnProfile.from_mapping(raw)
            if key != profile.profile_id:
                raise ValueError("APN profile key must equal profile_id")
            profiles[key] = profile
        if len(profiles) > MAX_APN_PROFILES:
            raise ValueError("apn_profile_library_full")
        object.__setattr__(self, "apn_profiles", MappingProxyType(profiles))


@dataclass(frozen=True, slots=True)
class IntelligencePreferences:
    band_priority: Mapping[int, float] = MappingProxyType({})

    def __post_init__(self) -> None:
        raw: dict[int, float] = {}
        for key, value in dict(self.band_priority or {}).items():
            try:
                band = int(key)
            except (TypeError, ValueError) as exc:
                raise ValueError("band priority key must be an integer") from exc
            score = _finite(value, field=f"band_priority[{band}]")
            if band <= 0 or score < 0.0:
                raise ValueError("band priority values must be non-negative")
            raw[band] = score
        if raw:
            maximum = max(raw.values())
            raw = (
                {band: round(score / maximum, 4) for band, score in raw.items()}
                if maximum > 0.0
                else {}
            )
        object.__setattr__(self, "band_priority", MappingProxyType(raw))
