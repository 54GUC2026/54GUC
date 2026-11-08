"""Core-owned deterministic runtime defaults persistence."""

from __future__ import annotations

import json
from pathlib import Path

from arch4.atomic_io import atomic_write_json
from arch4.customer_contracts.settings import CoreRuntimeDefaults


SCHEMA = "CoreRuntimeDefaults.v1"
OWNER = "arch4.core.runtime_defaults"
FILENAME = "core_runtime_defaults.json"


class CoreRuntimeDefaultsError(RuntimeError):
    pass


class CoreRuntimeDefaultsStore:
    def __init__(self, storage_root: str | Path) -> None:
        self.storage_root = Path(storage_root).resolve()
        self.path = self.storage_root / FILENAME

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> CoreRuntimeDefaults:
        if not self.path.is_file():
            return CoreRuntimeDefaults()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CoreRuntimeDefaultsError(
                f"Core runtime defaults cannot be read: {exc}"
            ) from exc
        expected = {
            "schema",
            "owner",
            "rsrq_baseline_default_db",
            "rtt_baseline_default_ms",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise CoreRuntimeDefaultsError("Core runtime defaults fields mismatch")
        if payload["schema"] != SCHEMA or payload["owner"] != OWNER:
            raise CoreRuntimeDefaultsError("Core runtime defaults owner mismatch")
        try:
            return CoreRuntimeDefaults(
                rsrq_baseline_default_db=payload["rsrq_baseline_default_db"],
                rtt_baseline_default_ms=payload["rtt_baseline_default_ms"],
            )
        except ValueError as exc:
            raise CoreRuntimeDefaultsError(str(exc)) from exc

    def save(self, settings: CoreRuntimeDefaults) -> None:
        if type(settings) is not CoreRuntimeDefaults:
            raise TypeError("CoreRuntimeDefaultsStore requires CoreRuntimeDefaults")
        atomic_write_json(
            self.path,
            {
                "schema": SCHEMA,
                "owner": OWNER,
                "rsrq_baseline_default_db": settings.rsrq_baseline_default_db,
                "rtt_baseline_default_ms": settings.rtt_baseline_default_ms,
            },
            sort_keys=True,
            trailing_newline=True,
        )

    def apply_to_config(self, config: object) -> CoreRuntimeDefaults:
        settings = self.load()
        config.rtt_baseline_default = settings.rtt_baseline_default_ms
        return settings
