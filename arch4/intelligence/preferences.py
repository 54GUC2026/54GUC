"""Intelligence-owned operator priors for learning and proposal ranking."""

from __future__ import annotations

import json
from pathlib import Path

from arch4.atomic_io import atomic_write_json
from arch4.customer_contracts.settings import IntelligencePreferences


SCHEMA = "IntelligencePreferences.v1"
OWNER = "arch4.intelligence.preferences"
FILENAME = "intelligence_preferences.json"


class IntelligencePreferencesError(RuntimeError):
    pass


class IntelligencePreferencesStore:
    def __init__(self, storage_root: str | Path) -> None:
        self.storage_root = Path(storage_root).resolve()
        self.path = self.storage_root / FILENAME

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> IntelligencePreferences:
        if not self.path.is_file():
            return IntelligencePreferences()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise IntelligencePreferencesError(
                f"Intelligence preferences cannot be read: {exc}"
            ) from exc
        expected = {"schema", "owner", "band_priority"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise IntelligencePreferencesError(
                "Intelligence preferences fields mismatch"
            )
        if payload["schema"] != SCHEMA or payload["owner"] != OWNER:
            raise IntelligencePreferencesError("Intelligence preferences owner mismatch")
        if not isinstance(payload["band_priority"], dict):
            raise IntelligencePreferencesError("band_priority must be an object")
        try:
            return IntelligencePreferences(
                band_priority={
                    int(band): score
                    for band, score in payload["band_priority"].items()
                }
            )
        except (TypeError, ValueError) as exc:
            raise IntelligencePreferencesError(str(exc)) from exc

    def save(self, settings: IntelligencePreferences) -> None:
        if type(settings) is not IntelligencePreferences:
            raise TypeError(
                "IntelligencePreferencesStore requires IntelligencePreferences"
            )
        atomic_write_json(
            self.path,
            {
                "schema": SCHEMA,
                "owner": OWNER,
                "band_priority": {
                    str(band): score
                    for band, score in sorted(settings.band_priority.items())
                },
            },
            sort_keys=True,
            trailing_newline=True,
        )
