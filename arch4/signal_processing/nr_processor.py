"""One transition-aware processor for Matrix-owned 5G NR NSA truth.

The processor consumes only ``NsaPhysicalSnapshot.v1``. It owns transition
state and short NR RSRP stability; it does not infer activity from residue,
estimate NR throughput, accept SA as supported, or create actions.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from arch4.interfaces import ComputedSignals
from arch4.modem_live.nsa_snapshot import (
    IDENTITY_EXACT,
    RAT_MODE_SA_UNSUPPORTED,
    validate_nsa_physical_snapshot,
)


NR_TRANSITION_ABSENT = "absent"
NR_TRANSITION_ATTACHING = "attaching"
NR_TRANSITION_STABLE = "stable"
NR_TRANSITION_DETACHING = "detaching"
NR_TRANSITION_SA_UNSUPPORTED = "sa_unsupported"


class NRProcessor:
    def __init__(
        self,
        *,
        ema_alpha: float = 0.133,
        ema_min_samples: int = 8,
    ) -> None:
        self._ema_alpha = float(ema_alpha)
        self._ema_min_samples = max(1, int(ema_min_samples))
        self._rsrp_ema = 0.0
        self._rsrp_ema_valid = False
        self._rsrp_ema_samples = 0
        self._stable_active = False
        self._stable_identity = ""
        self._pending_kind = ""
        self._pending_identity = ""
        self._pending_count = 0
        self._previous_requested_active = False

    def update(
        self,
        *,
        nsa_snapshot: Mapping[str, Any],
        cs: ComputedSignals,
    ) -> None:
        row = validate_nsa_physical_snapshot(nsa_snapshot)
        requested_active = bool(row.get("secondary_active", False))
        identity = str(row.get("identity_key") or "")
        exact = row.get("identity_completeness") == IDENTITY_EXACT
        coherent = bool(row.get("coherent", False) and exact and identity)
        required = max(1, int(row.get("coherent_samples_required", 2) or 2))
        sa_unsupported = row.get("rat_mode") == RAT_MODE_SA_UNSUPPORTED
        just_dropped = bool(
            self._previous_requested_active and not requested_active
        )

        if sa_unsupported:
            state = NR_TRANSITION_SA_UNSUPPORTED
            dirty = True
            self._stable_active = False
            self._stable_identity = ""
            self._clear_pending()
        elif requested_active:
            if self._stable_active and coherent and identity == self._stable_identity:
                state = NR_TRANSITION_STABLE
                dirty = False
                self._clear_pending()
            elif coherent:
                self._advance_pending("attach", identity)
                if self._pending_count >= required:
                    self._stable_active = True
                    self._stable_identity = identity
                    state = NR_TRANSITION_STABLE
                    dirty = False
                    self._clear_pending()
                else:
                    state = NR_TRANSITION_ATTACHING
                    dirty = True
            else:
                state = NR_TRANSITION_ATTACHING
                dirty = True
                self._clear_pending()
        elif self._stable_active or self._previous_requested_active:
            self._advance_pending("detach", "")
            if self._pending_count >= required:
                self._stable_active = False
                self._stable_identity = ""
                state = NR_TRANSITION_ABSENT
                dirty = False
                self._clear_pending()
            else:
                state = NR_TRANSITION_DETACHING
                dirty = True
        else:
            self._stable_active = False
            self._stable_identity = ""
            state = NR_TRANSITION_ABSENT
            dirty = False
            self._clear_pending()

        fields = row["fields"]
        rsrp = _available_float(fields, "nr.ss_rsrp_dbm")
        if rsrp is not None and -200.0 <= rsrp <= -20.0:
            if self._rsrp_ema_samples == 0:
                self._rsrp_ema = rsrp
            else:
                self._rsrp_ema += self._ema_alpha * (rsrp - self._rsrp_ema)
            self._rsrp_ema_samples += 1
            self._rsrp_ema_valid = self._rsrp_ema_samples >= self._ema_min_samples
        if state in {NR_TRANSITION_ABSENT, NR_TRANSITION_SA_UNSUPPORTED}:
            self._rsrp_ema = 0.0
            self._rsrp_ema_samples = 0
            self._rsrp_ema_valid = False

        cs.nsa_active = requested_active
        cs.sa_active = sa_unsupported
        cs.nr_secondary_active = bool(
            state == NR_TRANSITION_STABLE and self._stable_active
        )
        cs.nr_available = cs.nr_secondary_active
        cs.nr_transition_state = state
        cs.nr_transition_dirty = dirty
        cs.nr_identity_completeness = str(
            row.get("identity_completeness") or "absent"
        )
        cs.nr_band = int(_available_number(fields, "nr.band", 0))
        cs.nr_rsrp_dbm = float(rsrp or 0.0)
        cs.nr_rsrq_db = float(
            _available_number(fields, "nr.ss_rsrq_db", 0.0)
        )
        cs.nr_sinr_db = float(
            _available_number(fields, "nr.ss_sinr_db", 0.0)
        )
        cs.nr_rsrp_ema = self._rsrp_ema
        cs.nr_rsrp_ema_valid = self._rsrp_ema_valid
        cs.nr_recovering = state == NR_TRANSITION_ATTACHING
        cs.nr_just_dropped = just_dropped
        cs.nsa_snapshot = copy.deepcopy(row)
        cs.nr_capacity_evidence = {
            "schema": "NrCapacityEvidence.v1",
            "owner": "signal_processing_matrix.nr_processor",
            "secondary_active": cs.nr_secondary_active,
            "transition_dirty": dirty,
            "identity_completeness": cs.nr_identity_completeness,
            "band": _available_value(fields, "nr.band"),
            "bandwidth_mhz": _available_value(fields, "nr.bandwidth_mhz"),
            "scs_khz": _available_value(fields, "nr.scs_khz"),
            "total_nsa_capacity": None,
            "total_nsa_capacity_reason": "unqualified_without_physical_model",
        }
        self._previous_requested_active = requested_active

    def _advance_pending(self, kind: str, identity: str) -> None:
        if self._pending_kind == kind and self._pending_identity == identity:
            self._pending_count += 1
            return
        self._pending_kind = kind
        self._pending_identity = identity
        self._pending_count = 1

    def _clear_pending(self) -> None:
        self._pending_kind = ""
        self._pending_identity = ""
        self._pending_count = 0

    def export_state(self) -> dict[str, Any]:
        return {
            "rsrp_ema": self._rsrp_ema,
            "rsrp_ema_valid": self._rsrp_ema_valid,
            "rsrp_ema_samples": self._rsrp_ema_samples,
            "stable_active": self._stable_active,
            "stable_identity": self._stable_identity,
            "pending_kind": self._pending_kind,
            "pending_identity": self._pending_identity,
            "pending_count": self._pending_count,
            "previous_requested_active": self._previous_requested_active,
        }

    def restore_state(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            self._rsrp_ema = float(payload.get("rsrp_ema", 0.0) or 0.0)
            self._rsrp_ema_valid = bool(payload.get("rsrp_ema_valid", False))
            self._rsrp_ema_samples = int(payload.get("rsrp_ema_samples", 0) or 0)
            self._stable_active = bool(payload.get("stable_active", False))
            self._stable_identity = str(payload.get("stable_identity") or "")
            self._pending_kind = str(payload.get("pending_kind") or "")
            self._pending_identity = str(payload.get("pending_identity") or "")
            self._pending_count = int(payload.get("pending_count", 0) or 0)
            self._previous_requested_active = bool(
                payload.get("previous_requested_active", False)
            )
            return True
        except (TypeError, ValueError):
            return False


def _available_value(fields: Mapping[str, Any], name: str) -> Any:
    field = fields.get(name)
    if not isinstance(field, Mapping) or field.get("available") is not True:
        return None
    return field.get("value")


def _available_number(
    fields: Mapping[str, Any], name: str, default: int | float
) -> int | float:
    value = _available_value(fields, name)
    if type(value) not in {int, float} or type(value) is bool:
        return default
    return value


def _available_float(fields: Mapping[str, Any], name: str) -> float | None:
    field = fields.get(name)
    if not isinstance(field, Mapping) or field.get("available") is not True:
        return None
    value = field.get("value")
    if type(value) not in {int, float} or type(value) is bool:
        return None
    return float(value)


__all__ = (
    "NRProcessor",
    "NR_TRANSITION_ABSENT",
    "NR_TRANSITION_ATTACHING",
    "NR_TRANSITION_DETACHING",
    "NR_TRANSITION_SA_UNSUPPORTED",
    "NR_TRANSITION_STABLE",
)
