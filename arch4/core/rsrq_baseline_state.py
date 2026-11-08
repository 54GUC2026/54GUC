"""Small standalone persistence adapter for CGF-owned RSRQ baselines."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Mapping

from arch4.atomic_io import atomic_write_json
from arch4.customer_contracts.handoff_identity import confirmed_handoff_identity_matches
from arch4.runtime_identity import parse_runtime_session_scope_key


RSRQ_BASELINE_STATE_ROOT_ENV = "ARCH_CGF_RSRQ_BASELINE_STATE_ROOT"
RSRQ_BASELINE_STATE_FILENAME = "cgf_rsrq_baselines.json"
RSRQ_BASELINE_STATE_ENVELOPE_SCHEMA = "CGFRsrqBaselineStateEnvelope.v2"
RSRQ_BASELINE_STATE_OWNER = "54guc.core.rsrq_baseline_state"
_PORTABLE_DIRNAME = "_cgf_signal_state"
_PROFILE_ID = re.compile(r"[A-Za-z0-9_.-]+")
_RETIRED_PATH_ENV = "ARCH_CGF_RSRQ_BASELINE_STATE_PATH"


def _exact_profile_id(profile_id: object) -> str:
    profile = str(profile_id or "").strip()
    if _PROFILE_ID.fullmatch(profile) is None:
        raise ValueError("cgf_rsrq_baseline_portable_profile_invalid")
    return profile


def portable_rsrq_baseline_state_root(stage_dir: str | Path) -> Path:
    """Return the release-shared root; Core appends the exact profile."""

    stage = Path(stage_dir).expanduser().resolve()
    return (stage.parent / _PORTABLE_DIRNAME).resolve()


def portable_rsrq_baseline_state_path(
    stage_dir: str | Path,
    *,
    profile_id: str,
) -> Path:
    """Return one exact-profile state path shared by sibling releases."""

    profile = _exact_profile_id(profile_id)
    return (
        portable_rsrq_baseline_state_root(stage_dir)
        / profile
        / RSRQ_BASELINE_STATE_FILENAME
    ).resolve()


def resolve_rsrq_baseline_state_path(
    state_dir: str | Path,
    *,
    profile_id: object,
) -> Path | None:
    if str(os.environ.get(_RETIRED_PATH_ENV, "") or "").strip():
        raise RuntimeError("cgf_rsrq_baseline_retired_full_path_env_illegal")
    selected = str(os.environ.get(RSRQ_BASELINE_STATE_ROOT_ENV, "") or "").strip()
    if selected:
        profile = _exact_profile_id(profile_id)
        return (
            Path(selected).expanduser().resolve()
            / profile
            / RSRQ_BASELINE_STATE_FILENAME
        ).resolve()
    profile = str(profile_id or "").strip()
    if not profile:
        return None
    return (
        Path(state_dir).expanduser().resolve()
        / _exact_profile_id(profile)
        / RSRQ_BASELINE_STATE_FILENAME
    )


class RSRQBaselineStateStore:
    """Atomic storage only; RSRQProcessor owns all baseline semantics."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema": RSRQ_BASELINE_STATE_ENVELOPE_SCHEMA,
                    "owner": RSRQ_BASELINE_STATE_OWNER, "devices": {}}
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("cgf_rsrq_baseline_state_unreadable") from exc
        if not isinstance(envelope, dict):
            raise TypeError("cgf_rsrq_baseline_state_mapping_required")
        if (
            envelope.get("schema") not in {RSRQ_BASELINE_STATE_ENVELOPE_SCHEMA, "CGFRsrqBaselineStateEnvelope.v1"}
            or envelope.get("owner") != RSRQ_BASELINE_STATE_OWNER
        ):
            raise ValueError("cgf_rsrq_baseline_state_contract_invalid")
        field = "state" if envelope["schema"] == "CGFRsrqBaselineStateEnvelope.v1" else "devices"
        if not isinstance(envelope.get(field), dict):
            raise TypeError("cgf_rsrq_baseline_processor_state_required")
        return envelope

    @staticmethod
    def _device_key(devices: Mapping[str, Any], physical_scope: str) -> str | None:
        physical = parse_runtime_session_scope_key(physical_scope)
        if not (physical.get("serial") or physical.get("imei")):
            return None  # Unconfirmed sessions may compute, but not reuse a device's floors.
        matches = [key for key in devices if confirmed_handoff_identity_matches(
            parse_runtime_session_scope_key(key), physical)]
        if len(matches) > 1:
            raise ValueError("cgf_rsrq_baseline_physical_owner_ambiguous")
        return matches[0] if matches else physical_scope

    def load(self, *, physical_scope: str) -> dict[str, Any]:
        envelope = self._read()
        if envelope["schema"] == "CGFRsrqBaselineStateEnvelope.v1":
            raise ValueError("cgf_rsrq_baseline_legacy_physical_identity_unproven")
        devices = envelope["devices"]
        key = self._device_key(devices, physical_scope)
        state = devices.get(key, {}) if key is not None else {}
        if not isinstance(state, dict):
            raise TypeError("cgf_rsrq_baseline_processor_state_required")
        return dict(state)

    def save(self, state: Mapping[str, Any], *, physical_scope: str) -> None:
        if not isinstance(state, Mapping):
            raise TypeError("cgf_rsrq_baseline_processor_state_mapping_required")
        envelope = self._read()
        devices = dict(envelope.get("devices", {}))
        key = self._device_key(devices, physical_scope)
        if key is None:
            return
        if envelope["schema"] == "CGFRsrqBaselineStateEnvelope.v1":
            backup = self.path.with_name(self.path.stem + ".unattributed_v1.json")
            if not backup.exists():
                shutil.copy2(self.path, backup)
            elif backup.read_bytes() != self.path.read_bytes():
                raise ValueError("cgf_rsrq_baseline_legacy_backup_conflict")
        devices[key] = dict(state)
        atomic_write_json(
            self.path,
            {
                "schema": RSRQ_BASELINE_STATE_ENVELOPE_SCHEMA,
                "owner": RSRQ_BASELINE_STATE_OWNER,
                "devices": devices,
            },
            sort_keys=True,
            trailing_newline=True,
            # Signal admission calls this synchronously. Retry on a later
            # sample, never sleep through incoming modem generations.
            replace_attempts=1,
        )


__all__ = (
    "RSRQ_BASELINE_STATE_ENVELOPE_SCHEMA",
    "RSRQ_BASELINE_STATE_FILENAME",
    "RSRQ_BASELINE_STATE_OWNER",
    "RSRQ_BASELINE_STATE_ROOT_ENV",
    "RSRQBaselineStateStore",
    "portable_rsrq_baseline_state_path",
    "portable_rsrq_baseline_state_root",
    "resolve_rsrq_baseline_state_path",
)
