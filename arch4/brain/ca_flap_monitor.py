"""
Sliding-window CA flapping detector.

The detector owns only live observation of CA signature instability. It does
not choose actions and does not classify a band as bad globally; it reports
that the current PBAND is prone to CA flapping for this provider/cell/hour.
"""

from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Tuple

from arch4.interfaces import SystemState


@dataclass
class CaFlapMonitor:
    window_s: float = 180.0
    settle_s: float = 60.0
    candidate_flips: int = 4
    active_flips: int = 6
    consecutive_candidates_required: int = 2
    clear_s: float = 180.0
    _last_identity: Tuple[int, int, int, int, str] = ()
    _last_signature: str = ""
    _quiet_until: float = 0.0
    _flips: Deque[float] = field(default_factory=deque)
    _observed_flips: Deque[float] = field(default_factory=deque)
    _candidate_streak: int = 0
    _active: bool = False
    _active_since: float = 0.0
    _clear_since: float = 0.0
    _provider_plmn: str = ""
    _provider_name: str = ""
    _last_status: Dict[str, Any] = field(default_factory=dict)

    def set_provider_context(
        self,
        provider_plmn: str = "",
        provider_name: str = "",
        *,
        force: bool = False,
    ) -> None:
        plmn = str(provider_plmn or "").strip()
        name = str(provider_name or "").strip()
        if not plmn and not name:
            return
        if force or not self._provider_plmn:
            self._provider_plmn = plmn or self._provider_plmn
        if force or not self._provider_name:
            self._provider_name = name or self._provider_name

    def observe(
        self,
        *,
        modem: CellularIntelligenceGeneration,
        state: SystemState,
        now: float | None = None,
        planned_quiet_active: bool = False,
        action_settle_active: bool = False,
        traffic_active: bool = True,
        active_load_mbps: float = 0.0,
    ) -> Dict[str, Any]:
        ts = time.time() if now is None else float(now)
        observed_provider = str(getattr(modem.identity, 'provider_plmn', "") or "").strip()
        observed_name = str(getattr(modem.identity, 'provider_name', "") or "").strip()
        if (observed_provider or observed_name) and (
            not self._provider_plmn or not self._provider_name
        ):
            self.set_provider_context(observed_provider, observed_name)
        provider = self._provider_plmn or observed_provider
        provider_name = self._provider_name or observed_name
        pband = int(getattr(modem.cellular, 'primary_band', 0) or 0)
        cell_id = int(getattr(modem.cellular, 'cell_id', 0) or 0)
        pci = int(getattr(modem.cellular, 'pci', 0) or 0)
        earfcn = int(getattr(modem.cellular, 'earfcn_dl', 0) or 0)
        hour = int(time.localtime(ts).tm_hour)
        signature = str(getattr(modem.cellular, 'ca_band_signature', "") or "").strip()
        if not signature:
            signature = f"B{pband}" if pband > 0 else ""
        identity = (pband, cell_id, pci, earfcn, provider)

        excluded = (
            not bool(getattr(modem.cellular, 'radio_connected', False))
            or bool(planned_quiet_active)
            or bool(action_settle_active)
        )
        actionable = bool(traffic_active)
        if identity != self._last_identity:
            self._quiet_until = max(self._quiet_until, ts + float(self.settle_s))
            self._flips.clear()
            self._observed_flips.clear()
            self._candidate_streak = 0
            self._active = False
            self._active_since = 0.0
            self._clear_since = 0.0
            self._last_identity = identity
            self._last_signature = signature

        if excluded or ts < self._quiet_until or pband <= 0 or not signature:
            return self._status(
                ts=ts,
                provider=provider,
                provider_name=provider_name,
                pband=pband,
                cell_id=cell_id,
                pci=pci,
                hour=hour,
                signature=signature,
                excluded=True,
                exclude_reason=(
                    "settle"
                    if ts < self._quiet_until
                    else "planned_quiet"
                    if planned_quiet_active
                    else "action_settle"
                    if action_settle_active
                    else "disconnected"
                    if excluded
                    else ""
                ),
                traffic_active=actionable,
                active_load_mbps=float(active_load_mbps or 0.0),
            )

        if self._last_signature and signature != self._last_signature:
            self._observed_flips.append(ts)
            if actionable:
                self._flips.append(ts)
        self._last_signature = signature

        cutoff = ts - float(self.window_s)
        while self._flips and self._flips[0] < cutoff:
            self._flips.popleft()
        while self._observed_flips and self._observed_flips[0] < cutoff:
            self._observed_flips.popleft()

        flip_count = len(self._flips)
        candidate = flip_count >= int(self.candidate_flips)
        if candidate:
            self._candidate_streak += 1
        else:
            self._candidate_streak = 0

        active_now = (
            flip_count >= int(self.active_flips)
            or self._candidate_streak >= int(self.consecutive_candidates_required)
        )
        if active_now and not self._active:
            self._active_since = ts
        self._active = bool(active_now or self._active)

        if flip_count < 2:
            if self._clear_since <= 0.0:
                self._clear_since = ts
            elif ts - self._clear_since >= float(self.clear_s):
                self._active = False
                self._active_since = 0.0
                self._candidate_streak = 0
        else:
            self._clear_since = 0.0

        return self._status(
            ts=ts,
            provider=provider,
            provider_name=provider_name,
            pband=pband,
            cell_id=cell_id,
            pci=pci,
            hour=hour,
            signature=signature,
            excluded=False,
            exclude_reason="",
            traffic_active=actionable,
            active_load_mbps=float(active_load_mbps or 0.0),
        )

    def status(self) -> Dict[str, Any]:
        return dict(self._last_status)

    def _status(
        self,
        *,
        ts: float,
        provider: str,
        provider_name: str,
        pband: int,
        cell_id: int,
        pci: int,
        hour: int,
        signature: str,
        excluded: bool,
        exclude_reason: str,
        traffic_active: bool = True,
        active_load_mbps: float = 0.0,
    ) -> Dict[str, Any]:
        flip_count = len(self._flips)
        observed_flip_count = len(self._observed_flips)
        candidate = flip_count >= int(self.candidate_flips)
        context = {
            "provider_plmn": provider,
            "provider_name": provider_name,
            "cell_id": int(cell_id),
            "pci": int(pci),
            "hour": int(hour),
            "pband": int(pband),
        }
        reason = (
            f"provider={provider_name or provider or '-'} cell={cell_id or pci or 0} "
            f"hour={hour:02d} PBAND=B{pband} prone_to_ca_flapping"
            if self._active and pband > 0
            else ""
        )
        self._last_status = {
            "schema": "ca_flapping_v1",
            "ts": float(ts),
            "window_s": float(self.window_s),
            "settle_s": float(self.settle_s),
            "flip_count": int(flip_count),
            "observed_flip_count": int(observed_flip_count),
            "actionable_flip_count": int(flip_count),
            "candidate": bool(candidate),
            "active": bool(self._active),
            "active_since": float(self._active_since or 0.0),
            "traffic_active": bool(traffic_active),
            "active_load_mbps": float(active_load_mbps or 0.0),
            "signature": str(signature or ""),
            "excluded": bool(excluded),
            "exclude_reason": str(exclude_reason or ""),
            "context": context,
            "reason": reason,
        }
        return dict(self._last_status)
