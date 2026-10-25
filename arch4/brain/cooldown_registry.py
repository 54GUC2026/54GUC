"""
arch4/brain/cooldown_registry.py
════════════════════════════════════════════════════════════════
Centralized cooldown/rate-limit registry for the decision engine.

Replaces ~17 scattered self._xxx_cooldown_until / self._xxx_retry_until
variables across decision_*.py mixins with one dict-backed registry.

Two kinds of entries:
  - Scalar:  "band_cooldown" → expires at timestamp T
  - Keyed:   "restore_ca_retry" / "B1+B3" → expires at timestamp T

Usage:
    cooldowns = CooldownRegistry()
    cooldowns.set("band_cooldown", hold_s=3600.0)
    if cooldowns.is_blocked("band_cooldown"):
        return None  # still cooling down

    cooldowns.set_keyed("restore_ca_retry", "B1+B3", hold_s=1800.0)
    if cooldowns.is_blocked_keyed("restore_ca_retry", "B1+B3"):
        return None
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from arch4.operator_profiles import cooldown_alias_keys


class CooldownRegistry:
    """Single-dict cooldown tracker replacing scattered timer variables."""

    __slots__ = ("_scalars", "_keyed")

    def __init__(self) -> None:
        # key → expiry timestamp (float)
        self._scalars: Dict[str, float] = {}
        # namespace → {sub_key → expiry timestamp}
        self._keyed: Dict[str, Dict[str, float]] = {}

    # ── Scalar cooldowns ─────────────────────────────────────────

    def set(self, key: str, hold_s: float, *, now: Optional[float] = None) -> None:
        """Block `key` for `hold_s` seconds from now."""
        t = now if now is not None else time.time()
        self._scalars[key] = t + max(0.0, float(hold_s))

    def set_until(self, key: str, until: float) -> None:
        """Block `key` until absolute timestamp `until`."""
        self._scalars[key] = float(until)

    def is_blocked(self, key: str, *, now: Optional[float] = None) -> bool:
        """True if `key` cooldown is still active."""
        until = self._scalars.get(key, 0.0)
        if until <= 0.0:
            return False
        t = now if now is not None else time.time()
        return t < until

    def remaining_s(self, key: str, *, now: Optional[float] = None) -> float:
        """Seconds remaining on `key` cooldown (0.0 if expired/absent)."""
        until = self._scalars.get(key, 0.0)
        if until <= 0.0:
            return 0.0
        t = now if now is not None else time.time()
        return max(0.0, until - t)

    def clear(self, key: str) -> None:
        """Remove a scalar cooldown."""
        self._scalars.pop(key, None)

    def get_until(self, key: str) -> float:
        """Raw expiry timestamp for `key` (0.0 if absent)."""
        return self._scalars.get(key, 0.0)

    # ── Keyed cooldowns (per-objective / per-context) ────────────

    def set_keyed(
        self, namespace: str, sub_key: str, hold_s: float,
        *, now: Optional[float] = None,
    ) -> None:
        """Block `namespace`/`sub_key` for `hold_s` seconds."""
        t = now if now is not None else time.time()
        if namespace not in self._keyed:
            self._keyed[namespace] = {}
        self._keyed[namespace][sub_key] = t + max(0.0, float(hold_s))

    def set_keyed_until(
        self, namespace: str, sub_key: str, until: float,
    ) -> None:
        """Block `namespace`/`sub_key` until absolute timestamp."""
        if namespace not in self._keyed:
            self._keyed[namespace] = {}
        self._keyed[namespace][sub_key] = float(until)

    def is_blocked_keyed(
        self, namespace: str, sub_key: str,
        *, now: Optional[float] = None,
    ) -> bool:
        """True if `namespace`/`sub_key` cooldown is active."""
        ns = self._keyed.get(namespace)
        if not ns:
            return False
        until = ns.get(sub_key, 0.0)
        if until <= 0.0:
            return False
        t = now if now is not None else time.time()
        return t < until

    def remaining_s_keyed(
        self, namespace: str, sub_key: str,
        *, now: Optional[float] = None,
    ) -> float:
        """Seconds remaining on keyed cooldown."""
        ns = self._keyed.get(namespace)
        if not ns:
            return 0.0
        until = ns.get(sub_key, 0.0)
        if until <= 0.0:
            return 0.0
        t = now if now is not None else time.time()
        return max(0.0, until - t)

    def clear_keyed(self, namespace: str, sub_key: str) -> None:
        """Remove one keyed cooldown entry."""
        ns = self._keyed.get(namespace)
        if ns:
            ns.pop(sub_key, None)
            if not ns:
                self._keyed.pop(namespace, None)

    def clear_keyed_all(self, namespace: str) -> None:
        """Remove all entries under a namespace."""
        self._keyed.pop(namespace, None)

    def get_keyed_until(self, namespace: str, sub_key: str) -> float:
        """Raw expiry timestamp for keyed entry (0.0 if absent)."""
        ns = self._keyed.get(namespace)
        if not ns:
            return 0.0
        return ns.get(sub_key, 0.0)

    def keyed_count(self, namespace: str) -> int:
        """Number of entries in a namespace (including expired)."""
        return len(self._keyed.get(namespace, {}))

    # ── Serialization (for checkpoint persistence) ───────────────

    def save_state(self) -> Dict[str, Any]:
        """Serialize active cooldowns for checkpoint.

        Only includes entries that haven't expired yet.
        """
        now = time.time()
        scalars = {
            k: v for k, v in self._scalars.items() if v > now
        }
        keyed = {}
        for ns, entries in self._keyed.items():
            active = {k: v for k, v in entries.items() if v > now}
            if active:
                keyed[ns] = active
        return {"scalars": scalars, "keyed": keyed}

    def restore_state(self, state: Dict[str, Any]) -> None:
        """Restore cooldowns from checkpoint.

        Only restores entries that are still in the future.
        """
        if not isinstance(state, dict):
            return
        now = time.time()
        scalars = state.get("scalars")
        if isinstance(scalars, dict):
            for k, v in scalars.items():
                ts = float(v or 0.0)
                if ts > now:
                    self._scalars[str(k)] = ts
        keyed = state.get("keyed")
        if isinstance(keyed, dict):
            for ns, entries in keyed.items():
                if not isinstance(entries, dict):
                    continue
                for k, v in entries.items():
                    ts = float(v or 0.0)
                    if ts > now:
                        if ns not in self._keyed:
                            self._keyed[ns] = {}
                        self._keyed[ns][str(k)] = ts

    # ── Legacy bridge (backward-compatible save/restore) ─────────

    def save_state_legacy(self) -> Dict[str, Any]:
        """Serialize in the old flat format for backward compatibility.

        Maps registry keys back to the old variable names used by
        get_decision_cooldown_state() / restore_decision_cooldown_state().
        """
        now = time.time()

        def _keep(key: str) -> float:
            v = self._scalars.get(key, 0.0)
            return float(v) if v > now else 0.0

        def _keep_dict(namespace: str) -> Dict[str, float]:
            ns = self._keyed.get(namespace, {})
            return {k: float(v) for k, v in ns.items() if float(v) > now}

        out = {
            "band_cooldown_until": _keep("band_cooldown"),
            "global_band_churn_cooldown_until": _keep("global_band_churn"),
            "emergency_cooldown_until": _keep("emergency"),
            "hilink_restart_cooldown_until": _keep("hilink_restart"),
            "congestion_mitigation_cooldown_until": _keep("congestion_mitigation"),
            "ca_lost_eval_block_until": _keep("ca_lost_eval_block"),
            "smart_loop_roi_observation_until": _keep("smart_loop_radio_action_observation"),
            "restore_ca_retry_until": _keep_dict("restore_ca_retry"),
            "restore_ca_repeat_until": _keep_dict("restore_ca_repeat"),
            "restart_retry_until": _keep_dict("restart_retry"),
            "objective_feedback_until": _keep_dict("objective_feedback"),
        }
        for alias_key in cooldown_alias_keys("hilink_rotate"):
            out[alias_key] = _keep("hilink_rotate")
        for alias_key in cooldown_alias_keys("hilink_rotate_success_quiet"):
            out[alias_key] = _keep("hilink_rotate_success_quiet")
        for alias_key in cooldown_alias_keys("mikrotik_rotate"):
            out[alias_key] = _keep("mikrotik_rotate")
        for alias_key in cooldown_alias_keys("mikrotik_rotate_success_quiet"):
            out[alias_key] = _keep("mikrotik_rotate_success_quiet")
        for alias_key in cooldown_alias_keys("mikrotik_explore_retry", keyed=True):
            out[alias_key] = _keep_dict("mikrotik_explore_retry")
        for alias_key in cooldown_alias_keys("cell_lock_retry", keyed=True):
            out[alias_key] = _keep_dict("cell_lock_retry")
        return out

    def restore_state_legacy(self, state: Dict[str, Any]) -> None:
        """Restore from the old flat format.

        Maps old variable names back to registry keys.
        """
        if not isinstance(state, dict):
            return
        now = time.time()

        _SCALAR_MAP = {
            "band_cooldown_until": "band_cooldown",
            "global_band_churn_cooldown_until": "global_band_churn",
            "emergency_cooldown_until": "emergency",
            "hilink_restart_cooldown_until": "hilink_restart",
            # Migration-only input: older persisted state used B818 names for
            # shared HiLink cooldowns. Do not emit these names from save.
            "b818_restart_cooldown_until": "hilink_restart",
            "congestion_mitigation_cooldown_until": "congestion_mitigation",
            "ca_lost_eval_block_until": "ca_lost_eval_block",
            "smart_loop_roi_observation_until": "smart_loop_radio_action_observation",
        }
        for alias_key in cooldown_alias_keys("hilink_rotate"):
            _SCALAR_MAP[alias_key] = "hilink_rotate"
        for alias_key in cooldown_alias_keys("hilink_rotate_success_quiet"):
            _SCALAR_MAP[alias_key] = "hilink_rotate_success_quiet"
        _SCALAR_MAP.update(
            {
                "b818_rotate_cooldown_until": "hilink_rotate",
                "b818_rotate_success_quiet_until": "hilink_rotate_success_quiet",
            }
        )
        for alias_key in cooldown_alias_keys("mikrotik_rotate"):
            _SCALAR_MAP[alias_key] = "mikrotik_rotate"
        for alias_key in cooldown_alias_keys("mikrotik_rotate_success_quiet"):
            _SCALAR_MAP[alias_key] = "mikrotik_rotate_success_quiet"
        _KEYED_MAP = {
            "restore_ca_retry_until": "restore_ca_retry",
            "restore_ca_repeat_until": "restore_ca_repeat",
            "restart_retry_until": "restart_retry",
            "objective_feedback_until": "objective_feedback",
        }
        for alias_key in cooldown_alias_keys("mikrotik_explore_retry", keyed=True):
            _KEYED_MAP[alias_key] = "mikrotik_explore_retry"
        for alias_key in cooldown_alias_keys("cell_lock_retry", keyed=True):
            _KEYED_MAP[alias_key] = "cell_lock_retry"

        for old_key, new_key in _SCALAR_MAP.items():
            ts = float(state.get(old_key) or 0.0)
            if ts > now:
                self._scalars[new_key] = ts

        for old_key, namespace in _KEYED_MAP.items():
            d = state.get(old_key)
            if isinstance(d, dict):
                for k, v in d.items():
                    ts = float(v or 0.0)
                    if ts > now:
                        if namespace not in self._keyed:
                            self._keyed[namespace] = {}
                        self._keyed[namespace][str(k)] = ts

    # ── Diagnostics ──────────────────────────────────────────────

    def active_summary(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """Return a diagnostic summary of all active cooldowns."""
        t = now if now is not None else time.time()
        scalars = {
            k: {"until": v, "remaining_s": max(0, int(round(v - t)))}
            for k, v in self._scalars.items()
            if v > t
        }
        keyed = {}
        for ns, entries in self._keyed.items():
            active = {
                k: {"until": v, "remaining_s": max(0, int(round(v - t)))}
                for k, v in entries.items()
                if v > t
            }
            if active:
                keyed[ns] = active
        return {"scalars": scalars, "keyed": keyed}

    def active_count(self, *, now: Optional[float] = None) -> int:
        """Count of currently active cooldowns (scalars + keyed)."""
        t = now if now is not None else time.time()
        n = sum(1 for v in self._scalars.values() if v > t)
        for entries in self._keyed.values():
            n += sum(1 for v in entries.values() if v > t)
        return n

    def __repr__(self) -> str:
        n_scalars = sum(1 for v in self._scalars.values() if v > time.time())
        n_keyed = sum(
            sum(1 for v in entries.values() if v > time.time())
            for entries in self._keyed.values()
        )
        return f"CooldownRegistry(active_scalars={n_scalars}, active_keyed={n_keyed})"
