"""Typed, transport-neutral command targets owned by the Core boundary.

Every command origin constructs one of these values.  No downstream owner is
allowed to reinterpret an arbitrary mapping into a physical target.
"""

from __future__ import annotations

from arch4.core_contracts.network_settings import CoreNetworkSettingsTarget, network_target_from_mapping

from dataclasses import dataclass
import math
import re
import hashlib
import json
from typing import Any, ClassVar, Mapping, TypeAlias


def _positive_int(value: Any, reason: str) -> int:
    if type(value) is not int:
        raise TypeError(reason)
    parsed = value
    if parsed <= 0:
        raise ValueError(reason)
    return parsed


def _bands(value: Any, reason: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(reason)
    parsed = tuple(sorted({_positive_int(item, reason) for item in value}))
    if not parsed:
        raise ValueError(reason)
    return parsed


def _optional_bands(value: Any, reason: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(reason)
    return tuple(sorted({_positive_int(item, reason) for item in value}))


def _finite_nonnegative(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(reason)
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0.0:
        raise ValueError(reason)
    return parsed


def core_lte_band_signature(bands: tuple[int, ...] | list[int]) -> str:
    """Return the one canonical configured LTE pool signature."""

    values = _bands(tuple(bands), "core_lte_band_signature_bands_required")
    return "+".join(f"B{band}" for band in values)


def core_lte_pband_signature(
    pband: int,
    ca_pool: tuple[int, ...] | list[int],
) -> str:
    """Return the one canonical PBAND plus configured-pool target signature."""

    primary = _positive_int(pband, "core_lte_pband_signature_pband_required")
    pool = _bands(tuple(ca_pool), "core_lte_pband_signature_pool_required")
    if primary not in pool:
        raise ValueError("core_lte_pband_signature_primary_not_in_pool")
    return f"PBAND=B{primary}|CA={core_lte_band_signature(pool)}"


@dataclass(frozen=True, slots=True)
class CoreBandSetTarget:
    bands: tuple[int, ...]
    command_kind: str = "BAND_SELECTION"
    auto: bool = False
    release_manual: bool = False
    schema: ClassVar[str] = "CoreBandSetTarget.v1"

    def __post_init__(self) -> None:
        if not isinstance(self.command_kind, str):
            raise TypeError("core_band_set_target_kind_invalid")
        kind = self.command_kind
        if kind not in {"BAND_SELECTION", "BAND_LOCK"}:
            raise ValueError("core_band_set_target_kind_invalid")
        if self.auto is not False:
            raise ValueError("core_band_set_auto_forbidden")
        if type(self.release_manual) is not bool:
            raise TypeError("core_band_release_manual_bool_required")
        object.__setattr__(self, "command_kind", kind)
        object.__setattr__(
            self,
            "bands",
            _bands(self.bands, "core_band_set_target_bands_required"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "lever": self.command_kind,
            "bands": list(self.bands),
            "auto": False,
            **({"release_manual": True} if self.release_manual else {}),
        }


@dataclass(frozen=True, slots=True)
class CoreMobileDataTarget:
    """Enable or disable cellular data without closing device management."""

    enabled: bool
    command_kind: ClassVar[str] = "MOBILE_DATA"
    schema: ClassVar[str] = "CoreMobileDataTarget.v1"

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("core_mobile_data_enabled_bool_required")

    def to_mapping(self) -> dict[str, Any]:
        return {"lever": self.command_kind, "enabled": self.enabled}


@dataclass(frozen=True, slots=True)
class CoreBandAutoTarget:
    domain: str
    command_kind: ClassVar[str] = "BAND_AUTO"
    schema: ClassVar[str] = "CoreBandAutoTarget.v1"

    def __post_init__(self) -> None:
        if not isinstance(self.domain, str):
            raise TypeError("core_band_auto_target_domain_invalid")
        domain = self.domain.strip().lower()
        if domain not in {"lte_allocated_auto", "nr_approved_auto"}:
            raise ValueError("core_band_auto_target_domain_invalid")
        object.__setattr__(self, "domain", domain)

    def to_mapping(self) -> dict[str, Any]:
        return {"lever": self.command_kind, "domain": self.domain}


@dataclass(frozen=True, slots=True)
class CorePBandTarget:
    pband: int
    ca_pool: tuple[int, ...] = ()
    command_kind: ClassVar[str] = "PBAND"
    schema: ClassVar[str] = "CorePBandTarget.v1"

    def __post_init__(self) -> None:
        if type(self.pband) is not int or self.pband < 0:
            raise TypeError("core_pband_target_required")
        pband = self.pband
        pool = (
            _bands(self.ca_pool, "core_pband_ca_pool_invalid")
            if self.ca_pool
            else ()
        )
        if pband == 0 and pool:
            raise ValueError("core_pband_release_with_ca_pool_invalid")
        if pool and pband not in pool:
            pool = tuple(sorted({pband, *pool}))
        object.__setattr__(self, "pband", pband)
        object.__setattr__(self, "ca_pool", pool)

    def to_mapping(self) -> dict[str, Any]:
        value: dict[str, Any] = {"lever": self.command_kind, "pband": self.pband}
        if self.ca_pool:
            value["ca_pool"] = list(self.ca_pool)
        return value

    @property
    def has_ca_companions(self) -> bool:
        """Whether the configured pool contains a band besides PBAND."""

        return bool(
            self.ca_pool
            and self.ca_pool != (self.pband,)
        )


def core_lte_pci(value: Any) -> int:
    """Passive LTE target value contract; availability is never a zero test."""
    if type(value) is not int:
        raise TypeError("core_cell_lock_pci_required")
    if not 0 <= value <= 503:
        raise ValueError("core_cell_lock_pci_out_of_range")
    return value


@dataclass(frozen=True, slots=True)
class CoreCellLockTarget:
    pci: int | None = None
    earfcn: int = 0
    band: int | None = None
    unlock: bool = False
    command_kind: ClassVar[str] = "CELL_LOCK"
    schema: ClassVar[str] = "CoreCellLockTarget.v1"

    def __post_init__(self) -> None:
        if type(self.unlock) is not bool:
            raise TypeError("core_cell_lock_unlock_bool_required")
        if self.unlock:
            object.__setattr__(self, "pci", 0)
            object.__setattr__(self, "earfcn", 0)
            object.__setattr__(self, "band", None)
            return
        object.__setattr__(
            self,
            "pci",
            core_lte_pci(self.pci),
        )
        object.__setattr__(
            self,
            "earfcn",
            _positive_int(self.earfcn, "core_cell_lock_earfcn_required"),
        )
        if self.band is not None:
            object.__setattr__(
                self,
                "band",
                _positive_int(self.band, "core_cell_lock_band_invalid"),
            )

    def to_mapping(self) -> dict[str, Any]:
        if self.unlock:
            return {"lever": self.command_kind, "unlock": True}
        value: dict[str, Any] = {
            "lever": self.command_kind,
            "pci": self.pci,
            "earfcn": self.earfcn,
            "unlock": False,
        }
        if self.band is not None:
            value["band"] = self.band
        return value


@dataclass(frozen=True, slots=True)
class CoreNrBandTarget:
    nr_bands: tuple[int, ...]
    release_manual: bool = False
    command_kind: ClassVar[str] = "NR_BAND_LOCK"
    schema: ClassVar[str] = "CoreNrBandTarget.v1"

    def __post_init__(self) -> None:
        if type(self.release_manual) is not bool:
            raise TypeError("core_band_release_manual_bool_required")
        object.__setattr__(
            self,
            "nr_bands",
            _optional_bands(
                self.nr_bands,
                "core_nr_band_target_invalid",
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {"lever": self.command_kind, "nr_bands": list(self.nr_bands),
                **({"release_manual": True} if self.release_manual else {})}


@dataclass(frozen=True, slots=True)
class CoreRestartTarget:
    emergency_fast: bool = False
    command_kind: ClassVar[str] = "RESTART"
    schema: ClassVar[str] = "CoreRestartTarget.v1"

    def __post_init__(self) -> None:
        if type(self.emergency_fast) is not bool:
            raise TypeError("core_restart_emergency_fast_bool_required")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "lever": self.command_kind,
            "restart": True,
            "emergency_fast": self.emergency_fast,
        }


@dataclass(frozen=True, slots=True)
class CoreTrafficPresenceTarget:
    keepalive_interval_s: float = 0.0
    background_mbps: float = 0.0
    command_kind: ClassVar[str] = "TRAFFIC_PRESENCE"
    schema: ClassVar[str] = "CoreTrafficPresenceTarget.v1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "keepalive_interval_s",
            _finite_nonnegative(
                self.keepalive_interval_s,
                "core_traffic_presence_interval_invalid",
            ),
        )
        object.__setattr__(
            self,
            "background_mbps",
            _finite_nonnegative(
                self.background_mbps,
                "core_traffic_presence_background_invalid",
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "lever": self.command_kind,
            "keepalive_interval_s": self.keepalive_interval_s,
            "background_mbps": self.background_mbps,
        }


@dataclass(frozen=True, slots=True)
class CoreTrafficVolumeTarget:
    background_mbps: float | None = None
    sqm_limit_mbps: float | None = None
    command_kind: ClassVar[str] = "TRAFFIC_VOLUME"
    schema: ClassVar[str] = "CoreTrafficVolumeTarget.v1"

    def __post_init__(self) -> None:
        if self.background_mbps is None and self.sqm_limit_mbps is None:
            raise ValueError("core_traffic_volume_target_required")
        if self.background_mbps is not None:
            object.__setattr__(
                self,
                "background_mbps",
                _finite_nonnegative(
                    self.background_mbps,
                    "core_traffic_volume_background_invalid",
                ),
            )
        if self.sqm_limit_mbps is not None:
            object.__setattr__(
                self,
                "sqm_limit_mbps",
                _finite_nonnegative(
                    self.sqm_limit_mbps,
                    "core_traffic_volume_sqm_invalid",
                ),
            )

    def to_mapping(self) -> dict[str, Any]:
        value: dict[str, Any] = {"lever": self.command_kind}
        if self.background_mbps is not None:
            value["background_mbps"] = self.background_mbps
        if self.sqm_limit_mbps is not None:
            value["sqm_limit_mbps"] = self.sqm_limit_mbps
        return value


@dataclass(frozen=True, slots=True)
class CoreUeCapabilityTarget:
    dl_256qam: bool | None = None
    ul_64qam: bool | None = None
    restart: bool | None = None
    command_kind: ClassVar[str] = "UE_CAPABILITY"
    schema: ClassVar[str] = "CoreUeCapabilityTarget.v1"

    def __post_init__(self) -> None:
        for value in (self.dl_256qam, self.ul_64qam, self.restart):
            if value is not None and type(value) is not bool:
                raise TypeError("core_ue_capability_bool_required")
        if (
            self.dl_256qam is None
            and self.ul_64qam is None
            and self.restart is None
        ):
            raise ValueError("core_ue_capability_target_required")

    def to_mapping(self) -> dict[str, Any]:
        value: dict[str, Any] = {"lever": self.command_kind}
        if self.dl_256qam is not None:
            value["dl_256qam"] = self.dl_256qam
        if self.ul_64qam is not None:
            value["ul_64qam"] = self.ul_64qam
        if self.restart is not None:
            value["restart"] = self.restart
        return value


@dataclass(frozen=True, slots=True)
class CoreWifiRadioTarget:
    band: str
    enabled: bool
    command_kind: ClassVar[str] = "WIFI_RADIO"
    schema: ClassVar[str] = "CoreWifiRadioTarget.v1"

    def __post_init__(self) -> None:
        if not isinstance(self.band, str):
            raise TypeError("core_wifi_radio_band_invalid")
        band = self.band.strip().lower()
        if band not in {"2.4ghz", "5ghz", "6ghz"}:
            raise ValueError("core_wifi_radio_band_invalid")
        if type(self.enabled) is not bool:
            raise TypeError("core_wifi_radio_enabled_bool_required")
        object.__setattr__(self, "band", band)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "lever": self.command_kind,
            "band": self.band,
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True)
class CoreRadioModeTarget:
    """A guarded RAT change, retaining the exact LTE and NR restrictions.

    This is deliberately separate from band selection, APN and mobile data.
    Neither an empty NR mask nor an unbounded AUTO request means LTE-only.
    """

    mode: str
    expected_mode: str
    lte_bands: tuple[int, ...]
    nr_bands: tuple[int, ...]
    band_selection_mode: str
    subscription_key: str
    profile_definition_hash: str
    retry_delay_s: float = 1800.0
    # Optional admitted profile-choice keys preserve distinct mixed-mode options.
    # Their native meanings remain inside ModemLive; Core stores no wire commands.
    mode_key: str | None = None
    expected_mode_key: str | None = None
    lte_selection_mode: str | None = None
    nr_selection_mode: str | None = None
    preserved_configuration_key: str | None = None
    command_kind: ClassVar[str] = "RADIO_MODE"
    schema: ClassVar[str] = "CoreRadioModeTarget.v1"

    def __post_init__(self) -> None:
        modes = {"lte_only", "nsa_preferred"}
        configuration = dict(mode=self.mode,
            lte_bands=self.lte_bands, nr_bands=self.nr_bands,
            band_selection_mode=self.band_selection_mode, subscription_key=self.subscription_key,
            profile_definition_hash=self.profile_definition_hash)
        preferences = (self.mode_key, self.expected_mode_key, self.lte_selection_mode,
            self.nr_selection_mode, self.preserved_configuration_key)
        if any(value is not None for value in preferences):
            if any(value is None for value in preferences):
                raise ValueError("core_radio_mode_preference_pair_required")
            _validate_mode_key(self.expected_mode_key, self.expected_mode)
            configuration.update(mode_key=self.mode_key, lte_selection_mode=self.lte_selection_mode,
                nr_selection_mode=self.nr_selection_mode, preserved_configuration_key=self.preserved_configuration_key)
        configuration = validate_radio_mode_observation(configuration)
        if self.expected_mode not in modes:
            raise ValueError("core_radio_mode_invalid")
        if self.mode == self.expected_mode:
            raise ValueError("core_radio_mode_change_required")
        if type(self.retry_delay_s) not in {int, float} or self.retry_delay_s not in {1800.0, 3600.0, 5400.0}:
            raise ValueError("core_radio_mode_retry_delay_invalid")
        # WebUI LTE-only may remove the NR mask. Explicit enable5G preserves
        # that empty selection until its following requested NR band stage.
        # An automatic fallback still needs the NR restriction it will restore.
        if (self.mode == "lte_only" and not configuration["nr_bands"]
                and configuration.get("nr_selection_mode") != "auto"):
            raise ValueError("core_radio_mode_nr_restriction_required")
        object.__setattr__(self, "lte_bands", tuple(configuration["lte_bands"]))
        object.__setattr__(self, "nr_bands", tuple(configuration["nr_bands"]))

    def to_mapping(self) -> dict[str, Any]:
        result = {"lever": self.command_kind, "mode": self.mode,
                "expected_mode": self.expected_mode, "lte_bands": list(self.lte_bands),
                "nr_bands": list(self.nr_bands), "band_selection_mode": self.band_selection_mode,
                "subscription_key": self.subscription_key,
                "profile_definition_hash": self.profile_definition_hash, "retry_delay_s": self.retry_delay_s}
        if self.mode_key is not None:
            result.update(mode_key=self.mode_key, expected_mode_key=self.expected_mode_key,
                lte_selection_mode=self.lte_selection_mode, nr_selection_mode=self.nr_selection_mode,
                preserved_configuration_key=self.preserved_configuration_key)
        return result

    def configuration(self, *, expected: bool = False) -> dict[str, Any]:
        """Return only the exact observed settings, before or after this command."""
        result = dict(mode=self.expected_mode if expected else self.mode,
            lte_bands=list(self.lte_bands), nr_bands=list(self.nr_bands),
            band_selection_mode=self.band_selection_mode, subscription_key=self.subscription_key,
            profile_definition_hash=self.profile_definition_hash)
        if self.mode_key is not None:
            result.update(mode_key=self.expected_mode_key if expected else self.mode_key,
                lte_selection_mode=self.lte_selection_mode, nr_selection_mode=self.nr_selection_mode,
                preserved_configuration_key=self.preserved_configuration_key)
        return validate_radio_mode_observation(result)

    @classmethod
    def transition(cls, observation: Mapping[str, Any], *, mode: str,
                   mode_key: str | None = None, retry_delay_s: float = 1800.0) -> CoreRadioModeTarget:
        configuration = validate_radio_mode_observation(observation)
        expected_mode = configuration["mode"]
        if "mode_key" in configuration:
            expected_key = configuration["mode_key"]
            if mode == "lte_only":
                if mode_key not in (None, "lte_only"):
                    raise ValueError("core_radio_mode_lte_choice_invalid")
                mode_key = "lte_only"
            _validate_mode_key(mode_key, mode)
            configuration.update(mode_key=mode_key, expected_mode_key=expected_key)
        elif mode_key is not None:
            raise ValueError("core_radio_mode_preference_observation_required")
        configuration["mode"] = mode
        return cls(**configuration, expected_mode=expected_mode, retry_delay_s=retry_delay_s)


def _validate_mode_key(value: object, mode: str) -> None:
    if (type(value) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None
            or (value == "lte_only") != (mode == "lte_only")):
        raise ValueError("core_radio_mode_preference_invalid")


def validate_radio_mode_observation(value: object) -> dict[str, Any]:
    required = {"mode", "lte_bands", "nr_bands", "band_selection_mode", "subscription_key", "profile_definition_hash"}
    preferences = {"mode_key", "lte_selection_mode", "nr_selection_mode", "preserved_configuration_key"}
    if not isinstance(value, Mapping) or set(value) not in (required, required | preferences):
        raise ValueError("radio_mode_observation_fields_invalid")
    if value["mode"] not in {"lte_only", "nsa_preferred"}:
        raise ValueError("core_radio_mode_invalid")
    if value["band_selection_mode"] not in {"auto", "manual"}:
        raise ValueError("core_radio_mode_band_selection_required")
    if type(value["subscription_key"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", value["subscription_key"]):
        raise ValueError("core_radio_mode_subscription_required")
    if type(value["profile_definition_hash"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", value["profile_definition_hash"]):
        raise ValueError("core_radio_mode_profile_definition_required")
    result = dict(value)
    if "mode_key" in value:
        _validate_mode_key(value["mode_key"], value["mode"])
        key = value["preserved_configuration_key"]
        if type(key) is not str or re.fullmatch(r"[0-9a-f]{64}", key) is None:
            raise ValueError("core_radio_mode_configuration_receipt_required")
        if any(value[key] not in {"auto", "manual"} for key in ("lte_selection_mode", "nr_selection_mode")):
            raise ValueError("core_radio_mode_selection_invalid")
        selection = "auto" if value["lte_selection_mode"] == value["nr_selection_mode"] == "auto" else "manual"
        if value["band_selection_mode"] != selection:
            raise ValueError("core_radio_mode_selection_conflict")
    parse_lte = _optional_bands if value.get("lte_selection_mode") == "auto" else _bands
    result["lte_bands"] = list(parse_lte(value["lte_bands"], "core_radio_mode_lte_restriction_required"))
    # Explicitly empty is real configuration; missing/invalid remains invalid.
    # The native mode, never the presence of NR bands, identifies LTE-only.
    result["nr_bands"] = list(_optional_bands(value["nr_bands"], "core_radio_mode_nr_restriction_required"))
    return result


def core_radio_mode_signature(target: CoreRadioModeTarget) -> str:
    if not isinstance(target, CoreRadioModeTarget):
        raise TypeError("core_radio_mode_target_required")
    encoded = json.dumps(target.to_mapping(), sort_keys=True, separators=(",", ":")).encode()
    return "MODE=" + target.mode + ":" + hashlib.sha256(encoded).hexdigest()[:16]


CoreCommandTarget: TypeAlias = (
    CoreBandSetTarget
    | CoreMobileDataTarget
    | CoreBandAutoTarget
    | CorePBandTarget
    | CoreCellLockTarget
    | CoreNrBandTarget
    | CoreRestartTarget
    | CoreTrafficPresenceTarget
    | CoreTrafficVolumeTarget
    | CoreUeCapabilityTarget
    | CoreWifiRadioTarget
    | CoreNetworkSettingsTarget
    | CoreRadioModeTarget
)

CORE_COMMAND_TARGET_TYPES = (
    CoreMobileDataTarget,
    CoreBandSetTarget,
    CoreBandAutoTarget,
    CorePBandTarget,
    CoreCellLockTarget,
    CoreNrBandTarget,
    CoreRestartTarget,
    CoreTrafficPresenceTarget,
    CoreTrafficVolumeTarget,
    CoreUeCapabilityTarget,
    CoreWifiRadioTarget,
    CoreNetworkSettingsTarget,
    CoreRadioModeTarget,
)


def core_command_target_from_mapping(value: Mapping[str, Any]) -> CoreCommandTarget:
    if not isinstance(value, Mapping):
        raise TypeError("core_command_target_mapping_required")
    row = dict(value)
    if "lever" not in row:
        raise ValueError("core_command_target_missing_fields:lever")
    kind = row.pop("lever")
    if not isinstance(kind, str):
        raise TypeError("core_command_target_kind_string_required")
    if kind != kind.strip().upper():
        raise ValueError("core_command_target_kind_not_canonical")

    def require_keys(
        *,
        required: frozenset[str],
        optional: frozenset[str] = frozenset(),
    ) -> None:
        unknown = set(row) - required - optional
        missing = required - set(row)
        if unknown:
            raise ValueError(
                "core_command_target_unknown_fields:"
                + ",".join(sorted(unknown))
            )
        if missing:
            raise ValueError(
                "core_command_target_missing_fields:"
                + ",".join(sorted(missing))
            )

    if kind == "NETWORK_SETTINGS":
        return network_target_from_mapping(value)
    if kind == "RADIO_MODE":
        require_keys(required=frozenset({"mode", "expected_mode", "lte_bands", "nr_bands", "band_selection_mode", "subscription_key", "profile_definition_hash"}), optional=frozenset({"retry_delay_s", "mode_key", "expected_mode_key", "lte_selection_mode", "nr_selection_mode", "preserved_configuration_key"}))
        return CoreRadioModeTarget(**row)
    if kind == "MOBILE_DATA":
        require_keys(required=frozenset({"enabled"}))
        return CoreMobileDataTarget(enabled=row["enabled"])
    if kind in {"BAND_SELECTION", "BAND_LOCK"}:
        require_keys(
            required=frozenset({"bands"}),
            optional=frozenset({"auto", "release_manual"}),
        )
        auto = row.get("auto", False)
        if type(auto) is not bool:
            raise TypeError("core_band_set_auto_bool_required")
        bands = row.get("bands")
        if not isinstance(bands, list):
            raise TypeError("core_band_set_target_bands_list_required")
        return CoreBandSetTarget(
            bands=tuple(bands),
            command_kind=kind,
            auto=auto,
            release_manual=row.get("release_manual", False),
        )
    if kind == "BAND_AUTO":
        require_keys(required=frozenset({"domain"}))
        return CoreBandAutoTarget(domain=row.get("domain"))
    if kind == "PBAND":
        require_keys(
            required=frozenset({"pband"}),
            optional=frozenset({"ca_pool"}),
        )
        ca_pool = row.get("ca_pool", [])
        if not isinstance(ca_pool, list):
            raise TypeError("core_pband_ca_pool_list_required")
        return CorePBandTarget(
            pband=row.get("pband", 0),
            ca_pool=tuple(ca_pool),
        )
    if kind == "CELL_LOCK":
        if "unlock" not in row or type(row.get("unlock")) is not bool:
            raise TypeError("core_cell_lock_unlock_bool_required")
        unlock = row.get("unlock", False)
        require_keys(
            required=(
                frozenset({"unlock"})
                if unlock
                else frozenset({"pci", "earfcn", "unlock"})
            ),
            optional=(
                frozenset()
                if unlock
                else frozenset({"band"})
            ),
        )
        return CoreCellLockTarget(
            pci=row.get("pci", 0),
            earfcn=row.get("earfcn", 0),
            band=row.get("band"),
            unlock=unlock,
        )
    if kind == "NR_BAND_LOCK":
        require_keys(required=frozenset({"nr_bands"}), optional=frozenset({"release_manual"}))
        nr_bands = row.get("nr_bands")
        if not isinstance(nr_bands, list):
            raise TypeError("core_nr_band_target_list_required")
        return CoreNrBandTarget(
            nr_bands=tuple(nr_bands),
            release_manual=row.get("release_manual", False),
        )
    if kind == "RESTART":
        require_keys(
            required=frozenset({"restart", "emergency_fast"}),
        )
        if row.get("restart") is not True:
            raise ValueError("core_restart_target_marker_invalid")
        emergency = row.get("emergency_fast", False)
        if type(emergency) is not bool:
            raise TypeError("core_restart_emergency_fast_bool_required")
        return CoreRestartTarget(
            emergency_fast=emergency
        )
    if kind == "TRAFFIC_PRESENCE":
        require_keys(
            required=frozenset(
                {"keepalive_interval_s", "background_mbps"}
            ),
        )
        return CoreTrafficPresenceTarget(
            keepalive_interval_s=row.get("keepalive_interval_s", 0.0),
            background_mbps=row.get("background_mbps", 0.0),
        )
    if kind == "TRAFFIC_VOLUME":
        require_keys(
            required=frozenset(),
            optional=frozenset({"background_mbps", "sqm_limit_mbps"}),
        )
        return CoreTrafficVolumeTarget(
            background_mbps=row.get("background_mbps"),
            sqm_limit_mbps=row.get("sqm_limit_mbps"),
        )
    if kind == "UE_CAPABILITY":
        require_keys(
            required=frozenset(),
            optional=frozenset({"dl_256qam", "ul_64qam", "restart"}),
        )
        return CoreUeCapabilityTarget(
            dl_256qam=row.get("dl_256qam"),
            ul_64qam=row.get("ul_64qam"),
            restart=row.get("restart"),
        )
    if kind == "WIFI_RADIO":
        require_keys(required=frozenset({"band", "enabled"}))
        return CoreWifiRadioTarget(
            band=row.get("band"),
            enabled=row.get("enabled"),
        )
    raise ValueError(f"core_command_target_kind_invalid:{kind or 'missing'}")


def core_command_target_payload(target: CoreCommandTarget) -> dict[str, Any]:
    if not isinstance(target, CORE_COMMAND_TARGET_TYPES):
        raise TypeError("core_command_typed_target_required")
    return {
        "schema": target.schema,
        "command_kind": target.command_kind,
        "value": target.to_mapping(),
    }


def core_command_target_from_payload(value: Any) -> CoreCommandTarget:
    if not isinstance(value, Mapping):
        raise TypeError("core_command_target_payload_required")
    row = dict(value)
    unknown = set(row) - {"schema", "command_kind", "value"}
    missing = {"schema", "command_kind", "value"} - set(row)
    if unknown:
        raise ValueError(
            "core_command_target_payload_unknown_fields:"
            + ",".join(sorted(unknown))
        )
    if missing:
        raise ValueError(
            "core_command_target_payload_missing_fields:"
            + ",".join(sorted(missing))
        )
    mapping = row.get("value")
    target = core_command_target_from_mapping(mapping)
    if not isinstance(row.get("schema"), str):
        raise TypeError("core_command_target_schema_string_required")
    if row.get("schema") != target.schema:
        raise ValueError("core_command_target_schema_invalid")
    if not isinstance(row.get("command_kind"), str):
        raise TypeError("core_command_target_kind_string_required")
    if row.get("command_kind") != target.command_kind:
        raise ValueError("core_command_target_kind_mismatch")
    return target


__all__ = (
    "CoreMobileDataTarget",
    "CORE_COMMAND_TARGET_TYPES",
    "CoreBandAutoTarget",
    "CoreBandSetTarget",
    "CoreCellLockTarget",
    "CoreCommandTarget",
    "CoreNrBandTarget",
    "CorePBandTarget",
    "CoreRestartTarget",
    "CoreTrafficPresenceTarget",
    "CoreTrafficVolumeTarget",
    "core_lte_band_signature",
    "core_lte_pband_signature",
    "core_lte_pci",
    "CoreUeCapabilityTarget",
    "CoreWifiRadioTarget",
    "CoreRadioModeTarget",
    "validate_radio_mode_observation",
    "core_radio_mode_signature",
    "core_command_target_from_mapping",
    "core_command_target_from_payload",
    "core_command_target_payload",
)
