"""Store NGUI profile selection and non-secret connection hints."""

from __future__ import annotations

import json
from pathlib import Path
import re

from arch4.atomic_io import atomic_write_json
from arch4.customer_contracts.settings import (
    NGUIConnectionHint,
    NGUISettings,
)


SCHEMA = "NGUISettings.v1"
OWNER = "arch4.ngui.settings_store"
FILENAME = "ngui_settings.json"


class NGUISettingsError(RuntimeError):
    pass


class NGUISettingsStore:
    def __init__(self, storage_root: str | Path) -> None:
        self.storage_root = Path(storage_root).resolve()
        self.path = self.storage_root / FILENAME

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> NGUISettings:
        if not self.path.is_file():
            return NGUISettings()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise NGUISettingsError(f"NGUI settings cannot be read: {exc}") from exc
        expected = {
            "schema",
            "owner",
            "last_profile_id",
            "connection_hints",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise NGUISettingsError("NGUI settings fields mismatch")
        if payload["schema"] != SCHEMA or payload["owner"] != OWNER:
            raise NGUISettingsError("NGUI settings owner mismatch")
        raw_hints = payload["connection_hints"]
        if not isinstance(raw_hints, dict):
            raise NGUISettingsError("NGUI connection_hints must be an object")
        try:
            hints = {
                str(profile_id): NGUIConnectionHint(
                    profile_id=str(profile_id),
                    address=value["address"],
                    username=value.get("username", ""),
                    router_serial=value.get("router_serial", ""),
                    network_country=value.get("network_country", ""),
                    network_provider=value.get("network_provider", ""),
                    network_bands=value.get("network_bands", ""),
                )
                for profile_id, value in raw_hints.items()
                if isinstance(value, dict)
                and set(value).issubset({"address", "username", "router_serial", "network_country", "network_provider", "network_bands"})
                and "address" in value
            }
            if len(hints) != len(raw_hints):
                raise ValueError("NGUI connection hint fields mismatch")
            return NGUISettings(
                last_profile_id=payload["last_profile_id"],
                connection_hints=hints,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NGUISettingsError(str(exc)) from exc

    def save(self, settings: NGUISettings) -> None:
        if type(settings) is not NGUISettings:
            raise TypeError("NGUISettingsStore requires NGUISettings")
        atomic_write_json(
            self.path,
            {
                "schema": SCHEMA,
                "owner": OWNER,
                "last_profile_id": settings.last_profile_id,
                "connection_hints": {
                    profile_id: {
                        "address": hint.address,
                        "username": hint.username,
                        "router_serial": hint.router_serial,
                        "network_country": hint.network_country,
                        "network_provider": hint.network_provider,
                        "network_bands": hint.network_bands,
                    }
                    for profile_id, hint in sorted(settings.connection_hints.items())
                },
            },
            sort_keys=True,
            trailing_newline=True,
            mode=0o600,
        )

    def save_connection_hint(
        self,
        *,
        profile_id: str,
        address: str,
        username: str = "",
        router_serial: str = "",
        network_country: str = "",
        network_provider: str = "",
        network_bands: str = "",
        make_last: bool = True,
    ) -> NGUISettings:
        current = self.load()
        hint = NGUIConnectionHint(
            profile_id=profile_id,
            address=address,
            username=username,
            router_serial=router_serial,
            network_country=network_country,
            network_provider=network_provider,
            network_bands=network_bands,
        )
        hints = dict(current.connection_hints)
        hints[hint.profile_id] = hint
        updated = NGUISettings(
            last_profile_id=hint.profile_id if make_last else current.last_profile_id,
            connection_hints=hints,
        )
        self.save(updated)
        return updated

    def connection_hint(self, profile_id: str) -> NGUIConnectionHint | None:
        return self.load().connection_hints.get(str(profile_id or "").strip())


def import_r144_run_hints_once(
    run_store: NGUISettingsStore,
    stable_store: NGUISettingsStore,
) -> bool:
    """Move an existing R14.4 run's nonsecret hints to the stable owner once."""
    if type(run_store) is not NGUISettingsStore or type(stable_store) is not NGUISettingsStore:
        raise TypeError("NGUI settings import requires typed stores")
    if run_store.path == stable_store.path:
        raise ValueError("NGUI settings import source and target must differ")
    if stable_store.exists or not run_store.exists:
        return False
    previous = run_store.load()
    if not previous.last_profile_id or not previous.connection_hints:
        return False
    stable_store.save(previous)
    return True


def r144_run_hint_store(app_root: str | Path) -> NGUISettingsStore | None:
    """Locate the newest saved R14.4 run beside this portable for one-time import."""
    app = Path(app_root).resolve()
    old_app = app if app.name == "54GUC_R14.4" else app.parent / "54GUC_R14.4"
    runtime_root = old_app / "portable_runtime"
    if not runtime_root.is_dir():
        return None
    runs = sorted(
        (path for path in runtime_root.iterdir()
         if path.is_dir()
         and re.fullmatch(r"guc_output_\d{8}_\d{6}(?:_\d{2})?", path.name)
         and (path / FILENAME).is_file()),
        key=lambda path: path.name,
    )
    return NGUISettingsStore(runs[-1]) if runs else None
