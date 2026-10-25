"""Profile selection drafts owned by NGUI.

This owner does not publish connected identity, modem capabilities, Foundation
health, persistence, or license state.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any, Mapping

from arch4.config_views import apply_connection_credentials
from arch4.customer_contracts.settings import CoreRuntimeDefaults, NGUISettings
from arch4.customer_contracts.network_suggestions import NetworkSuggestions
from arch4.modem_live.profile_catalog import (
    ModemProfileSelection,
    modem_profile_selection,
    modem_profile_selections,
)
from arch4.interfaces import Arch4Config

from .adapters.owner_register import adapt_owner_register
from .input_registers import OwnerInputBatch
from .login_lifecycle import login_lifecycle
from .profile_discovery import discovery_suggestion_from_result
from .registry import NGUI_INPUT_SOURCE_REGISTRY
from .settings_store import NGUISettingsStore
from .network_preset_store import NGUINetworkPresetStore
from .types import SourceOwner


log = logging.getLogger("54guc.ngui.profile_selection")


@dataclass(frozen=True, slots=True)
class ResolvedProfileLogin:
    contract: ModemProfileSelection
    config: Arch4Config
    address: str
    username: str
    entered_license: str
    observe_only: bool


CONNECTION_DEFAULTS_SCHEMA = "NGUIProfileConnectionDefaults.v1"
CONNECTION_DEFAULTS_OWNER = "54guc.ngui.profile_selection"


_LOCAL_OWNER_SOURCE_IDS = {
    owner: frozenset(
        source_id
        for source_id, spec in NGUI_INPUT_SOURCE_REGISTRY.items()
        if spec.owner is owner
    )
    for owner in (
        SourceOwner.PROFILE_SELECTION,
        SourceOwner.PROFILE_DISCOVERY,
        SourceOwner.LOGIN_LIFECYCLE,
    )
}


class ExactProfileAuthority:
    """Own profile drafts, discovery results, and the login lifecycle."""

    def __init__(
        self,
        *,
        storage_root: str | Path,
        settings_store: NGUISettingsStore,
        core_runtime_defaults: CoreRuntimeDefaults,
        remembered_settings_store: NGUISettingsStore,
        remembered_network_store: NGUINetworkPresetStore,
    ) -> None:
        if type(settings_store) is not NGUISettingsStore:
            raise TypeError("exact_profile_authority_requires_ngui_settings_store")
        if type(remembered_settings_store) is not NGUISettingsStore:
            raise TypeError("exact_profile_authority_requires_stable_ngui_settings_store")
        if type(remembered_network_store) is not NGUINetworkPresetStore:
            raise TypeError("exact_profile_authority_requires_stable_network_preset_store")
        if type(core_runtime_defaults) is not CoreRuntimeDefaults:
            raise TypeError("exact_profile_authority_requires_core_runtime_defaults")
        self.storage_root = Path(storage_root).resolve()
        if settings_store.storage_root != self.storage_root:
            raise RuntimeError("profile NGUISettingsStore root mismatch")
        if remembered_settings_store.storage_root == self.storage_root:
            raise RuntimeError("stable NGUI settings cannot be the per-run projection")
        if remembered_network_store.path.parent != remembered_settings_store.storage_root:
            raise RuntimeError("stable network preset root mismatch")
        self._settings_store = settings_store
        self._remembered_settings_store = remembered_settings_store
        self._remembered_network_store = remembered_network_store
        self._core_runtime_defaults = core_runtime_defaults
        self._contracts = modem_profile_selections()
        self._lock = RLock()
        self._by_label = {
            contract.label.casefold(): contract for contract in self._contracts
        }
        self._sequences = {
            owner: 0 for owner in _LOCAL_OWNER_SOURCE_IDS
        }
        self._current_values = {
            SourceOwner.PROFILE_SELECTION: self._neutral_selection_values(),
            SourceOwner.PROFILE_DISCOVERY: {
                "profile.discovery_suggestion": {},
            },
            SourceOwner.LOGIN_LIFECYCLE: self._login_values(
                "draft",
                "Enter router details",
            ),
        }

    @property
    def available_models(self) -> tuple[str, ...]:
        return tuple(contract.label for contract in self._contracts)

    @property
    def models_by_manufacturer(self) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, list[str]] = {}
        for contract in self._contracts:
            manufacturer = str(contract.manufacturer or "").strip()
            label = str(contract.label or "").strip()
            if not manufacturer or not label:
                continue
            grouped.setdefault(manufacturer, []).append(label)
        return {manufacturer: tuple(labels) for manufacturer, labels in grouped.items()}

    def neutral_publications(self, *, now: float) -> tuple[OwnerInputBatch, ...]:
        with self._lock:
            return tuple(
                self._owner_batch(owner, values, now=now)
                for owner, values in self._current_values.items()
            )

    def reset_runtime_state(
        self,
        *,
        now: float,
    ) -> tuple[
        Mapping[str, Any],
        Mapping[str, Any],
        tuple[OwnerInputBatch, ...],
    ]:
        """Clear profile, discovery, and login state but keep settings."""

        with self._lock:
            prior = self._runtime_reset_observation_unlocked()
            self._sequences = {
                owner: 0 for owner in _LOCAL_OWNER_SOURCE_IDS
            }
            self._current_values = {
                SourceOwner.PROFILE_SELECTION: (
                    self._neutral_selection_values()
                ),
                SourceOwner.PROFILE_DISCOVERY: {
                    "profile.discovery_suggestion": {},
                },
                SourceOwner.LOGIN_LIFECYCLE: self._login_values(
                    "draft",
                    "Enter router details",
                ),
            }
            batches = tuple(
                self._owner_batch(owner, values, now=now)
                for owner, values in self._current_values.items()
            )
            result = self._runtime_reset_observation_unlocked()
            return (
                MappingProxyType(prior),
                MappingProxyType(result),
                batches,
            )

    def _neutral_selection_values(self) -> dict[str, Any]:
        return {
            "profile.available_models": self.available_models,
            "profile.models_by_manufacturer": self.models_by_manufacturer,
            "profile.connection_defaults": self.connection_defaults(),
            "profile.selected_profile": "",
            "profile.runtime_mode": "Not connected",
        }

    def connection_defaults(self) -> Mapping[str, Any]:
        """Return non-secret defaults for a profile connection draft."""

        try:
            remembered = self._remembered_settings_store.load()
        except Exception as exc:
            log.error("NGUI remembered connection settings rejected: %s", exc)
            remembered = NGUISettings()
        try:
            network_presets = self._remembered_network_store.load_all()
        except Exception as exc:
            log.error("NGUI remembered network settings rejected: %s", exc)
            network_presets = {}
        profiles: dict[str, Mapping[str, Any]] = {}
        for contract in self._contracts:
            hint = remembered.connection_hints.get(contract.profile_id)
            address = (
                str(hint.address or "").strip()
                if hint is not None
                else "192.168.1.1"
            )
            username = (
                str(hint.username or "").strip()
                if hint is not None
                else ""
            ) or contract.default_username
            profiles[contract.label] = {
                "profile_id": contract.profile_id,
                "backend": str(contract.backend or "").strip().lower(),
                "address": address,
                "username": username,
                "router_serial": hint.router_serial if hint else "",
                "network_country": hint.network_country if hint else "",
                "network_provider": hint.network_provider if hint else "",
                "network_bands": hint.network_bands if hint else "",
                "network_preset": network_presets.get(contract.profile_id, {}),
                "network_settings_writable": contract.network_settings_writable,
                "apn_profile_creation": contract.apn_profile_creation,
            }
        return {
            "schema": CONNECTION_DEFAULTS_SCHEMA,
            "owner": CONNECTION_DEFAULTS_OWNER,
            "last_profile_id": remembered.last_profile_id,
            "profiles": profiles,
        }

    def discovery_publication(
        self,
        result: Mapping[str, Any],
        *,
        now: float,
    ) -> OwnerInputBatch:
        """Publish a typed discovery suggestion through Profile PageTruth."""

        suggestion = discovery_suggestion_from_result(
            result,
            contracts=self._contracts,
            observed_at=now,
        )
        return self._owner_transition(
            SourceOwner.PROFILE_DISCOVERY,
            {
                "profile.discovery_suggestion": (
                    dict(suggestion.as_dict())
                    if suggestion is not None
                    else {}
                )
            },
            now=now,
        )

    def resolve_login(
        self,
        *,
        selection: str,
        address: str,
        username: str,
        password: str,
        router_serial: str = "",
        entered_license: str,
        observe_only: bool = False,
        network_country: str = "",
        network_provider: str = "",
        network_bands: str = "",
    ) -> ResolvedProfileLogin:
        contract = self._resolve_contract(selection)
        host = str(address or "").strip()
        user = str(username or "").strip() or contract.default_username
        if not host:
            raise ValueError("router address is required")
        serial = str(router_serial or "").strip()
        from arch4.runtime_identity import is_placeholder_identity_text
        if serial and (
            len(serial) > 128
            or any(ord(character) < 32 for character in serial)
            or is_placeholder_identity_text(serial)
            or set(serial) == {"0"}
        ):
            raise ValueError("router serial is invalid")
        suggestions = NetworkSuggestions(network_country, network_provider, network_bands)
        config = Arch4Config(
            modem_profile_id=contract.profile_id,
            modem_backend=contract.backend,
            target_guard_model=contract.target_guard_model,
            network_country_suggestion=suggestions.country,
            network_provider_suggestion=suggestions.provider,
            network_bands_suggestion=suggestions.bands,
        )
        config.rtt_baseline_default = (
            self._core_runtime_defaults.rtt_baseline_default_ms
        )
        config.target_guard_serial = serial
        apply_connection_credentials(
            config, contract.backend, host, user, str(password or "")
        )
        return ResolvedProfileLogin(
            contract=contract,
            config=config,
            address=host,
            username=user,
            entered_license=str(entered_license or "").strip(),
            observe_only=bool(observe_only),
        )

    def begin_connection(
        self,
        login: ResolvedProfileLogin,
        *,
        now: float,
    ) -> tuple[OwnerInputBatch, OwnerInputBatch]:
        """Publish fresh selection and connecting state after reset."""

        return (
            self._owner_transition(
                SourceOwner.PROFILE_SELECTION,
                self._profile_values(
                    login.contract,
                    observe_only=login.observe_only,
                ),
                now=now,
            ),
            self._owner_transition(
                SourceOwner.LOGIN_LIFECYCLE,
                self._login_values("connecting", "Connecting"),
                now=now,
            ),
        )

    def confirm_connected(
        self,
        login: ResolvedProfileLogin,
        *,
        now: float,
    ) -> tuple[OwnerInputBatch, OwnerInputBatch]:
        """Save and republish confirmed profile and login state."""

        try:
            values = dict(
                profile_id=login.contract.profile_id,
                address=login.address,
                username=login.username,
                router_serial=login.config.target_guard_serial,
                network_country=login.config.network_country_suggestion,
                network_provider=login.config.network_provider_suggestion,
                network_bands=login.config.network_bands_suggestion,
                make_last=True,
            )
            self._remembered_settings_store.save_connection_hint(**values)
            self._settings_store.save_connection_hint(**values)
        except Exception as exc:
            # Connection state remains live. Report a settings write failure;
            # do not present it as successful storage.
            log.error("NGUI connection hint persistence failed: %s", exc)
        return (
            self._owner_transition(
                SourceOwner.PROFILE_SELECTION,
                self._profile_values(
                    login.contract,
                    observe_only=login.observe_only,
                ),
                now=now,
            ),
            self._owner_transition(
                SourceOwner.LOGIN_LIFECYCLE,
                self._login_values("connected", "Connected", connected=True),
                now=now,
            ),
        )

    def remember_network_preset(self, row: Mapping[str, object]) -> None:
        """Retain a verified SIM/network form without storing APN credentials."""
        self._remembered_network_store.save(row)

    def service_progress_publication(
        self,
        *,
        state: str,
        message: str = "",
        cycle_count: int = 0,
        now: float,
    ) -> OwnerInputBatch:
        """Publish login and runtime progress through profile readback."""

        state_key = str(state or "").strip().lower()
        message_text = str(message or "").strip()
        if state_key == "starting":
            text = "Starting runtime"
            lifecycle_state = "starting"
        elif state_key == "identity_required":
            text = (
                "Router login succeeded. Enter the router serial number "
                "in the open dialog to continue."
            )
            lifecycle_state = "identity_required"
        elif state_key == "reconnecting":
            text = "Reconnecting modem"
            lifecycle_state = "reconnecting"
        elif state_key == "running":
            if int(cycle_count or 0) <= 0:
                text = "Waiting for modem data"
                lifecycle_state = "waiting_for_data"
            else:
                text = "Confirming modem"
                lifecycle_state = "confirming_modem"
        elif state_key == "failed":
            lifecycle_state = "failed"
            if "zwrt_initial_physical_identity_unavailable" in message_text:
                text = (
                    "Router did not report a serial number or IMEI, and serial "
                    "entry was not completed."
                )
            elif message_text.startswith("CGF runtime teardown was not clean:"):
                # Full teardown evidence belongs to the retained shutdown receipt.
                text = "Runtime stopped"
            else:
                text = "Runtime failed"
                if message_text:
                    text = f"{text}: {message_text}"
        elif state_key in {"stopping", "stopped"}:
            text = "Runtime stopped"
            lifecycle_state = state_key
        elif state_key == "login_required":
            text = message_text or "Please sign in again."
            lifecycle_state = "draft"
        else:
            text = "Connecting"
            lifecycle_state = "connecting"
        return self._owner_transition(
            SourceOwner.LOGIN_LIFECYCLE,
            {
                "profile.login_status": text,
                "profile.login_lifecycle": login_lifecycle(
                    lifecycle_state,
                    message=text,
                ),
            },
            now=now,
        )

    def connection_stop_publication(
        self, *, pending: bool, disconnect: bool, error: str = "", now: float,
    ) -> OwnerInputBatch:
        """Session stop readback preserves profile selection and login drafts."""

        state = "failed" if error else "stopping" if pending else "stopped"
        text = (
            f"Connection stop failed: {error}"
            if error else
            ("Disconnecting; waiting for runtime to stop." if disconnect else "Cancelling connection; waiting for runtime to stop.")
            if pending else
            ("Disconnected. Your entries are retained for another attempt." if disconnect else "Connection cancelled. Your entries are retained for another attempt.")
        )
        return self._owner_transition(
            SourceOwner.LOGIN_LIFECYCLE,
            self._login_values(state, text),
            now=now,
        )

    def connection_failure_publication(
        self,
        *,
        message: str,
        connected_session_retained: bool,
        now: float,
    ) -> OwnerInputBatch:
        """Publish rejected or failed Connect readback through the profile owner."""

        detail = str(message or "").strip()
        if connected_session_retained:
            text = "Connected"
            if detail:
                text = f"{text} — restart not started: {detail}"
        else:
            text = "Connection failed"
            if detail:
                text = f"{text}: {detail}"
        return self._owner_transition(
            SourceOwner.LOGIN_LIFECYCLE,
            {
                "profile.login_status": text,
                "profile.login_lifecycle": login_lifecycle(
                    "connected" if connected_session_retained else "failed",
                    message=text,
                    connected=connected_session_retained,
                ),
            },
            now=now,
        )

    def _resolve_contract(self, selection: str) -> ModemProfileSelection:
        token = str(selection or "").strip()
        contract = modem_profile_selection(token) or self._by_label.get(token.casefold())
        if contract is None:
            raise ValueError(f"unknown exact modem profile: {token or '<empty>'}")
        return contract

    def _profile_values(
        self,
        contract: ModemProfileSelection,
        *,
        observe_only: bool,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {
            "profile.available_models": self.available_models,
            "profile.models_by_manufacturer": self.models_by_manufacturer,
            "profile.connection_defaults": self.connection_defaults(),
            "profile.selected_profile": contract.label,
            "profile.runtime_mode": "Observe only" if observe_only else "Live control",
        }
        return values

    @staticmethod
    def _login_values(
        state: str,
        message: str,
        *,
        connected: bool = False,
    ) -> dict[str, Any]:
        return {
            "profile.login_status": message,
            "profile.login_lifecycle": login_lifecycle(
                state,
                message=message,
                connected=connected,
            ),
        }

    def _runtime_reset_observation_unlocked(self) -> dict[str, Any]:
        selection = self._current_values[SourceOwner.PROFILE_SELECTION]
        discovery = self._current_values[SourceOwner.PROFILE_DISCOVERY]
        login = self._current_values[SourceOwner.LOGIN_LIFECYCLE]
        lifecycle = login["profile.login_lifecycle"]
        return {
            "sequences": {
                owner.value: self._sequences[owner]
                for owner in sorted(
                    _LOCAL_OWNER_SOURCE_IDS,
                    key=lambda item: item.value,
                )
            },
            "selected_profile": str(
                selection.get("profile.selected_profile") or ""
            ),
            "runtime_mode": str(
                selection.get("profile.runtime_mode") or ""
            ),
            "discovery_suggestion": dict(
                discovery.get("profile.discovery_suggestion") or {}
            ),
            "login_status": str(
                login.get("profile.login_status") or ""
            ),
            "login_lifecycle": dict(lifecycle),
        }

    def _owner_batch(
        self,
        owner: SourceOwner,
        values: Mapping[str, Any],
        *,
        now: float,
    ) -> OwnerInputBatch:
        with self._lock:
            expected = _LOCAL_OWNER_SOURCE_IDS.get(owner)
            if expected is None:
                raise RuntimeError(f"exact_profile_owner_illegal:{owner.value}")
            source_ids = frozenset(values)
            if source_ids != expected:
                missing = sorted(expected - source_ids)
                extra = sorted(source_ids - expected)
                raise RuntimeError(
                    f"{owner.value}_complete_snapshot_required:"
                    f"missing={','.join(missing)};extra={','.join(extra)}"
                )
            sequence = self._sequences[owner] + 1
            batch = adapt_owner_register(
                owner=owner,
                sequence=sequence,
                published_at=now,
                values=values,
                validity={source_id: True for source_id in values},
                sampled_at={source_id: now for source_id in values},
                sample_sequences={source_id: sequence for source_id in values},
            )
            self._sequences[owner] = sequence
            self._current_values[owner] = dict(values)
            return batch

    def _owner_transition(
        self,
        owner: SourceOwner,
        updates: Mapping[str, Any],
        *,
        now: float,
    ) -> OwnerInputBatch:
        with self._lock:
            expected = _LOCAL_OWNER_SOURCE_IDS.get(owner)
            if expected is None:
                raise RuntimeError(f"exact_profile_owner_illegal:{owner.value}")
            unknown = frozenset(updates) - expected
            if unknown:
                raise RuntimeError(
                    f"{owner.value}_transition_source_unknown:"
                    + ",".join(sorted(unknown))
                )
            current = self._current_values.get(owner)
            if current is None:
                raise RuntimeError(f"{owner.value}_snapshot_not_initialized")
            values = dict(current)
            values.update(dict(updates))
            return self._owner_batch(owner, values, now=now)


__all__ = [
    "CONNECTION_DEFAULTS_OWNER",
    "CONNECTION_DEFAULTS_SCHEMA",
    "ExactProfileAuthority",
    "ResolvedProfileLogin",
]
