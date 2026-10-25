"""Immutable expected-effect mask for one admitted cellular mutation.

The action lifecycle owns which short-lived effects are expected from an
already-admitted mutation.  Modem Live may carry this contract beside its
bounded connectivity token, but it must not invent, broaden, or reinterpret
the mask.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from math import isfinite
from typing import Any, Mapping

from arch4.canonical_json import canonical_json_bytes
from arch4.action_transaction import ActionTransactionKey
from arch4.interfaces import LeverAction, LeverType


SCHEMA = "ActionExpectedEffectMask.v1"
OWNER = "action_command_lifecycle"

EFFECT_MODEM_READ_MISS = "modem_read_miss"
EFFECT_RADIO_DISCONNECTED = "radio_disconnected"
EFFECT_SESSION_UPTIME_RESET = "session_uptime_reset"
EFFECT_BACKEND_SESSION_UNAVAILABLE = "backend_session_unavailable"

FAULT_RADIO_DOWN = "E_RADIO_DOWN"
FAULT_SESSION_RESET = "E_SESSION_RESET"

_RADIO_MUTATION_LEVERS = frozenset(
    {
        LeverType.BAND_SELECTION.name,
        LeverType.BAND_LOCK.name,
        LeverType.CELL_LOCK.name,
        LeverType.PBAND.name,
        LeverType.UE_CAPABILITY.name,
        LeverType.RESTART.name,
    }
)


def _checksum_material(
    *,
    action_key: Mapping[str, Any],
    expected_effects: tuple[str, ...],
    tolerated_fault_classes: tuple[str, ...],
    created_ts: float,
    expires_ts: float,
) -> str:
    material = {
        "action_key": dict(action_key),
        "expected_effects": list(expected_effects),
        "tolerated_fault_classes": list(tolerated_fault_classes),
        "created_ts": float(created_ts),
        "expires_ts": float(expires_ts),
    }
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


@dataclass(frozen=True, slots=True)
class ActionExpectedEffectMask:
    action_key: ActionTransactionKey
    expected_effects: tuple[str, ...]
    tolerated_fault_classes: tuple[str, ...]
    created_ts: float
    expires_ts: float
    checksum: str

    def __post_init__(self) -> None:
        if self.action_key.lever not in _RADIO_MUTATION_LEVERS:
            raise ValueError("expected_effect_mask_non_radio_action")
        if not isfinite(float(self.created_ts)) or not isfinite(
            float(self.expires_ts)
        ):
            raise ValueError("expected_effect_mask_time_not_finite")
        if float(self.expires_ts) <= float(self.created_ts):
            raise ValueError("expected_effect_mask_expiry_invalid")
        expected = tuple(sorted(set(self.expected_effects)))
        tolerated = tuple(sorted(set(self.tolerated_fault_classes)))
        if expected != self.expected_effects:
            raise ValueError("expected_effect_mask_effects_not_canonical")
        if tolerated != self.tolerated_fault_classes:
            raise ValueError("expected_effect_mask_faults_not_canonical")
        if not expected:
            raise ValueError("expected_effect_mask_effects_missing")
        if not tolerated:
            raise ValueError("expected_effect_mask_faults_missing")
        legal_effects = {
            EFFECT_MODEM_READ_MISS,
            EFFECT_RADIO_DISCONNECTED,
            EFFECT_SESSION_UPTIME_RESET,
            EFFECT_BACKEND_SESSION_UNAVAILABLE,
        }
        if not set(expected).issubset(legal_effects):
            raise ValueError("expected_effect_mask_effect_unknown")
        if not set(tolerated).issubset(
            {FAULT_RADIO_DOWN, FAULT_SESSION_RESET}
        ):
            raise ValueError("expected_effect_mask_fault_unknown")
        required_faults: set[str] = set()
        if EFFECT_RADIO_DISCONNECTED in expected:
            required_faults.add(FAULT_RADIO_DOWN)
        if EFFECT_SESSION_UPTIME_RESET in expected:
            required_faults.add(FAULT_SESSION_RESET)
        if set(tolerated) != required_faults:
            raise ValueError("expected_effect_mask_fault_effect_mismatch")
        retained = _checksum_material(
            action_key=self.action_key.as_dict(),
            expected_effects=expected,
            tolerated_fault_classes=tolerated,
            created_ts=float(self.created_ts),
            expires_ts=float(self.expires_ts),
        )
        if retained != str(self.checksum or ""):
            raise ValueError("expected_effect_mask_checksum_invalid")

    @property
    def root_id(self) -> str:
        return self.action_key.root_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "mask_id": f"effect-mask:{self.checksum[:32]}",
            "action_root_id": self.action_key.root_id,
            "action_key": self.action_key.as_dict(),
            "expected_effects": list(self.expected_effects),
            "tolerated_fault_classes": list(self.tolerated_fault_classes),
            "created_ts": float(self.created_ts),
            "expires_ts": float(self.expires_ts),
            "checksum": self.checksum,
        }

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "ActionExpectedEffectMask":
        row = dict(raw or {})
        if row.get("schema") != SCHEMA or row.get("owner") != OWNER:
            raise ValueError("expected_effect_mask_contract_invalid")
        action_key_raw = row.get("action_key")
        if not isinstance(action_key_raw, Mapping):
            raise ValueError("expected_effect_mask_action_key_missing")
        action_key = ActionTransactionKey.from_mapping(action_key_raw)
        if str(row.get("action_root_id") or "") != action_key.root_id:
            raise ValueError("expected_effect_mask_action_root_mismatch")
        mask = cls(
            action_key=action_key,
            expected_effects=tuple(
                sorted(
                    {
                        str(value or "").strip()
                        for value in list(row.get("expected_effects") or [])
                        if str(value or "").strip()
                    }
                )
            ),
            tolerated_fault_classes=tuple(
                sorted(
                    {
                        str(value or "").strip()
                        for value in list(
                            row.get("tolerated_fault_classes") or []
                        )
                        if str(value or "").strip()
                    }
                )
            ),
            created_ts=float(row.get("created_ts", 0.0) or 0.0),
            expires_ts=float(row.get("expires_ts", 0.0) or 0.0),
            checksum=str(row.get("checksum") or ""),
        )
        if str(row.get("mask_id") or "") != f"effect-mask:{mask.checksum[:32]}":
            raise ValueError("expected_effect_mask_identity_mismatch")
        return mask

    def covers_fault(
        self,
        fault_class: str,
        *,
        now: float,
        session_scope_key: str,
        identity_key: str,
    ) -> bool:
        return bool(
            float(self.created_ts) <= float(now) < float(self.expires_ts)
            and str(session_scope_key or "")
            == self.action_key.matrix_identity.session_scope_key
            and str(identity_key or "") == self.action_key.identity_key
            and str(fault_class or "") in self.tolerated_fault_classes
        )


def build_action_expected_effect_mask(
    action: LeverAction,
    *,
    created_ts: float,
    expires_ts: float,
) -> ActionExpectedEffectMask:
    from arch4.action_transaction import action_transaction_key_from_action

    action_key = action_transaction_key_from_action(action)
    if action_key is None:
        raise ValueError("expected_effect_mask_action_key_missing")
    if action_key.lever not in _RADIO_MUTATION_LEVERS:
        raise ValueError("expected_effect_mask_non_radio_action")
    effects = {
        EFFECT_MODEM_READ_MISS,
        EFFECT_RADIO_DISCONNECTED,
        EFFECT_SESSION_UPTIME_RESET,
    }
    if action_key.lever == LeverType.RESTART.name or (
        action_key.lever == LeverType.UE_CAPABILITY.name
        and bool(dict(getattr(action, "params", {}) or {}).get("restart", False))
    ):
        effects.add(EFFECT_BACKEND_SESSION_UNAVAILABLE)
    expected = tuple(sorted(effects))
    tolerated = tuple(
        sorted({FAULT_RADIO_DOWN, FAULT_SESSION_RESET})
    )
    checksum = _checksum_material(
        action_key=action_key.as_dict(),
        expected_effects=expected,
        tolerated_fault_classes=tolerated,
        created_ts=float(created_ts),
        expires_ts=float(expires_ts),
    )
    return ActionExpectedEffectMask(
        action_key=action_key,
        expected_effects=expected,
        tolerated_fault_classes=tolerated,
        created_ts=float(created_ts),
        expires_ts=float(expires_ts),
        checksum=checksum,
    )


__all__ = [
    "ActionExpectedEffectMask",
    "EFFECT_BACKEND_SESSION_UNAVAILABLE",
    "EFFECT_MODEM_READ_MISS",
    "EFFECT_RADIO_DISCONNECTED",
    "EFFECT_SESSION_UPTIME_RESET",
    "FAULT_RADIO_DOWN",
    "FAULT_SESSION_RESET",
    "OWNER",
    "SCHEMA",
    "build_action_expected_effect_mask",
]
