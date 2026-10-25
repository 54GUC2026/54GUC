"""Deterministic external ZTE MC888 GoForm appliance simulator.

The simulator owns virtual hardware behavior only. Production CGF reaches it
through the normal ``mc888`` backend; this module never publishes Matrix truth.
"""

from __future__ import annotations

import base64
import hashlib
import heapq
import json
import math
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, parse_qsl, urlparse


SCENARIO_SCHEMA = "ZteMc888SimulatorScenario.v3"
CONTROL_HEADER = "X-CGF-Simulator-Control"
EVENT_LOG_MAX_BYTES = 2 * 1024 * 1024
MAX_CONTROL_BYTES = 64 * 1024
MAX_TRANSFER_BYTES = 32 * 1024 * 1024
MAX_QUEUED_FAULTS = 256
PUBLIC_FIELDS = frozenset(
    {
        "LD",
        "RD",
        "Language",
        "loginfo",
        "network_type",
        "network_lte_rsrp",
        "lte_rsrp",
        "Z5g_rsrp",
        "wa_inner_version",
        "cr_version",
    }
)


def _sha256_upper(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest().upper()


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _bands(values: Iterable[Any]) -> list[int]:
    result: list[int] = []
    for value in values:
        band = _as_int(value)
        if band > 0 and band not in result:
            result.append(band)
    return result


def _lte_mask(values: Iterable[int]) -> int:
    mask = 0
    for band in _bands(values):
        mask |= 1 << (band - 1)
    return mask


def _decode_lte_mask(value: Any) -> list[int]:
    text = str(value or "").strip().lower()
    if not text:
        return []
    try:
        mask = int(text, 16) if text.startswith("0x") else int(text)
    except ValueError:
        return []
    return [band for band in range(1, 89) if mask & (1 << (band - 1))]


@dataclass(frozen=True)
class HttpReply:
    status: int
    body: bytes
    content_type: str = "application/json"
    headers: tuple[tuple[str, str], ...] = ()
    close_without_reply: bool = False
    delay_s: float = 0.0


class TrafficEngine:
    """One clock for rates, interval-integrated bytes and link availability.

    Schedule/variation/action time is scaled; Mbps and byte integration use real
    elapsed seconds. Reading more often cannot change the traffic workload.
    Fractional bytes are retained (only published totals are rounded down).
    """

    def __init__(self, config: Mapping[str, Any], *, time_scale: float) -> None:
        self._config = dict(config or {})
        self._profiles = {
            str(name): dict(value or {})
            for name, value in dict(self._config.get("profiles") or {}).items()
        }
        if "idle" not in self._profiles:
            raise ValueError("mc888_simulator_traffic_idle_profile_required")
        rows = list(self._config.get("schedule") or [])
        if len(rows) > 256 or any(not isinstance(row, dict) or not math.isfinite(float(row.get("at_s", 0))) or float(row.get("at_s", 0)) < 0 for row in rows):
            raise ValueError("simulator_traffic_schedule_invalid")
        self._schedule = sorted(
            (
                max(0.0, _as_float(row.get("at_s"))),
                str(row.get("profile") or "idle"),
            )
            for row in list(self._config.get("schedule") or [])
            if isinstance(row, dict)
        ) or [(0.0, "idle")]
        for _at, name in self._schedule:
            if name not in self._profiles:
                raise ValueError(f"mc888_simulator_unknown_traffic_profile:{name}")
        self._loop_s = _as_float(self._config.get("loop_s"))
        if not math.isfinite(self._loop_s) or self._loop_s < 0:
            raise ValueError("simulator_traffic_loop_invalid")
        self._time_scale = float(time_scale)
        if not math.isfinite(self._time_scale) or self._time_scale < 0.001:
            raise ValueError("simulator_time_scale_must_be_finite_and_at_least_0.001")
        if self._loop_s and self._loop_s <= self._schedule[-1][0]:
            raise ValueError("simulator_traffic_loop_must_follow_last_schedule_entry")
        for profile in self._profiles.values():
            for key in ("down_mbps", "up_mbps", "variation", "variation_period_s", "phase", "latency_ms", "jitter_ms", "loss_percent", "congestion_percent"):
                if key in profile and not math.isfinite(float(profile[key])):
                    raise ValueError(f"simulator_traffic_nonfinite:{key}")
        self.reset()

    def reset(self) -> None:
        self._started = time.monotonic()
        self._last_update = self._started
        self._rx_total = float(max(0, _as_int(self._config.get("initial_rx_bytes"), 100_000_000)))
        self._tx_total = float(max(0, _as_int(self._config.get("initial_tx_bytes"), 20_000_000)))
        self._monthly_rx = float(max(self._rx_total, _as_int(self._config.get("monthly_rx_bytes"), 5_000_000_000)))
        self._monthly_tx = float(max(self._tx_total, _as_int(self._config.get("monthly_tx_bytes"), 700_000_000)))
        self._override = None
        self._blocked_until = self._started
        self._nr_blocked_until = self._started
        self._data_enabled = True
        self._connected_since: float | None = None
        self._last_sample = {}

    def select(self, profile: str, duration_s: float = 0.0) -> None:
        name = str(profile or "").strip()
        if name not in self._profiles:
            raise ValueError(f"mc888_simulator_unknown_traffic_profile:{name}")
        if not math.isfinite(float(duration_s)) or duration_s < 0:
            raise ValueError("simulator_traffic_duration_invalid")
        now = time.monotonic()
        self._advance(now)
        until = float("inf") if duration_s == 0.0 else now + duration_s * self._time_scale
        self._override = (name, until)
        self._update_connection(now, self._connected(name, now))

    def set_data_enabled(self, enabled: bool) -> None:
        """Pause link availability without replacing the selected workload."""
        now = time.monotonic()
        self._advance(now)
        self._data_enabled = enabled
        self._update_connection(now, self._connected(self._profile_at(now), now))

    def block_until(self, deadline: float, *, nr_deadline: float | None = None) -> None:
        """Appliance radio/reboot owner declares a real-monotonic outage end."""
        if not math.isfinite(float(deadline)) or (nr_deadline is not None and (not math.isfinite(float(nr_deadline)) or nr_deadline < deadline)):
            raise ValueError("simulator_traffic_deadline_invalid")
        now = time.monotonic()
        self._advance(now)
        self._blocked_until = max(now, self._blocked_until, float(deadline))
        self._nr_blocked_until = max(self._nr_blocked_until, float(deadline if nr_deadline is None else nr_deadline))
        self._update_connection(now, self._connected(self._profile_at(now), now))

    def add_bytes(self, *, rx_bytes: int = 0, tx_bytes: int = 0) -> None:
        rx = max(0, int(rx_bytes))
        tx = max(0, int(tx_bytes))
        self._rx_total += rx
        self._tx_total += tx
        self._monthly_rx += rx
        self._monthly_tx += tx

    def _scheduled_profile(self, now: float) -> str:
        elapsed = max(0.0, (now - self._started) / self._time_scale)
        if self._loop_s > 0.0:
            elapsed %= self._loop_s
        selected = self._schedule[0][1]
        for at_s, profile in self._schedule:
            if elapsed < at_s:
                break
            selected = profile
        return selected

    def _profile_at(self, now: float) -> str:
        if self._override is not None and now < self._override[1]:
            return self._override[0]
        return self._scheduled_profile(now)

    def _connected(self, name: str, now: float) -> bool:
        return self._data_enabled and now >= self._blocked_until and bool(self._profiles[name].get("connected", True))

    def _update_connection(self, now: float, connected: bool) -> None:
        if not connected:
            self._connected_since = None
        elif self._connected_since is None:
            self._connected_since = now

    def _next_boundary(self, start: float, end: float) -> float:
        candidates = [end]
        if start < self._blocked_until < end:
            candidates.append(self._blocked_until)
        if self._override is not None and start < self._override[1]:
            return min(*candidates, self._override[1])
        elapsed = (start - self._started) / self._time_scale
        cycle = math.floor(elapsed / self._loop_s) if self._loop_s else 0
        offset = cycle * self._loop_s
        for at_s, _name in self._schedule:
            boundary = self._started + (offset + at_s) * self._time_scale
            if self._loop_s and boundary <= start:
                boundary += self._loop_s * self._time_scale
            if boundary > start + 1e-10:
                candidates.append(boundary)
        if self._loop_s:
            boundary = self._started + (offset + self._loop_s) * self._time_scale
            if boundary <= start:
                boundary += self._loop_s * self._time_scale
            candidates.append(boundary)
        return min(value for value in candidates if value > start)

    def _wave_parameters(self, profile: Mapping[str, Any]) -> tuple[float, float, float]:
        period = max(0.25, _as_float(profile.get("variation_period_s"), 7.0)) * self._time_scale
        return math.tau / period, max(0.0, min(0.95, _as_float(profile.get("variation")))), _as_float(profile.get("phase"))

    def _advance(self, current: float) -> None:
        if not math.isfinite(current) or current < self._last_update:
            raise ValueError("simulator_traffic_clock_must_be_monotonic_and_finite")
        start = self._last_update
        rx_parts: list[float] = []
        tx_parts: list[float] = []
        while start < current:
            end = self._next_boundary(start, current)
            middle = start + (end - start) / 2
            name = self._profile_at(middle)
            connected = self._connected(name, middle)
            self._update_connection(start, connected)
            if connected:
                profile = self._profiles[name]
                omega, variation, phase = self._wave_parameters(profile)
                wave_integral = (math.cos(omega * (start - self._started) + phase) - math.cos(omega * (end - self._started) + phase)) / omega
                rx_parts.append(max(0.0, _as_float(profile.get("down_mbps"))) * 125_000 * (end - start + variation * wave_integral))
                tx_parts.append(max(0.0, _as_float(profile.get("up_mbps"))) * 125_000 * (end - start - 0.5 * variation * wave_integral))
            start = end
        rx, tx = math.fsum(rx_parts), math.fsum(tx_parts)
        self._rx_total += rx
        self._tx_total += tx
        self._monthly_rx += rx
        self._monthly_tx += tx
        self._last_update = current
        self._update_connection(current, self._connected(self._profile_at(current), current))
        if self._override is not None and current >= self._override[1]:
            self._override = None

    def sample(self, now: float | None = None) -> dict[str, Any]:
        current = time.monotonic() if now is None else float(now)
        self._advance(current)
        profile_name = self._profile_at(current)
        profile = self._profiles[profile_name]
        elapsed = max(0.0, current - self._started)
        omega, variation, phase = self._wave_parameters(profile)
        wave = math.sin(elapsed * omega + phase)
        factor = max(0.0, 1.0 + variation * wave)
        down_mbps = max(0.0, _as_float(profile.get("down_mbps")) * factor)
        up_mbps = max(0.0, _as_float(profile.get("up_mbps")) * max(0.0, 1.0 - variation * wave * 0.5))
        connected = self._connected(profile_name, current)
        if not connected:
            down_mbps = up_mbps = 0.0
        latency_ms = max(0.1, _as_float(profile.get("latency_ms"), 28.0))
        jitter_ms = max(0.0, _as_float(profile.get("jitter_ms"), 2.0))
        instantaneous_latency = max(0.1, latency_ms + jitter_ms * wave)
        self._last_sample = {
            "profile": profile_name,
            "down_mbps": round(down_mbps, 4),
            "up_mbps": round(up_mbps, 4),
            "rx_bytes_per_s": int(down_mbps * 1_000_000.0 / 8.0),
            "tx_bytes_per_s": int(up_mbps * 1_000_000.0 / 8.0),
            "rx_total": int(self._rx_total),
            "tx_total": int(self._tx_total),
            "monthly_rx": int(self._monthly_rx),
            "monthly_tx": int(self._monthly_tx),
            "latency_ms": round(instantaneous_latency, 3),
            "jitter_ms": round(jitter_ms, 3),
            "loss_percent": max(0.0, min(100.0, _as_float(profile.get("loss_percent")))),
            "connected": connected,
            "connected_seconds": max(0.0, current - self._connected_since) if self._connected_since is not None else 0.0,
            "ca_active": connected and bool(profile.get("ca_active", down_mbps >= 5.0)),
            "nr_active": connected and current >= self._nr_blocked_until and bool(profile.get("nr_active", True)),
            "congestion_percent": max(0.0, min(100.0, _as_float(profile.get("congestion_percent")))),
        }
        return dict(self._last_sample)


class ZteMc888Appliance:
    """State and protocol owner behind the virtual HTTP appliance."""

    def __init__(
        self,
        scenario: Mapping[str, Any],
        *,
        password: str | None = None,
        control_token: str = "mc888-lab",
        time_scale: float = 1.0,
        event_log: str | Path | None = None,
        lab_session: str = "",
    ) -> None:
        self.scenario = validate_scenario(scenario)
        self.time_scale = float(time_scale)
        self.password = str(password if password is not None else self.scenario["authentication"]["password"])
        self.control_token = str(control_token or "mc888-lab")
        self.event_log = Path(event_log).expanduser() if event_log else None
        self.event_log_error_count = 0
        self.lab_session = str(lab_session)
        self.scenario_sha256 = hashlib.sha256(
            json.dumps(self.scenario, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.request_lock = threading.Lock()
        self.ingress_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.started = time.monotonic()
        self.request_counter = 0
        self.nonce_counter = 0
        self.sessions: dict[str, float] = {}
        self.current_session = ""
        self.latest_ld = ""
        self.latest_rd = ""
        self.latest_ad = ""
        self.field_overrides: dict[str, Any] = {}
        self.faults: deque[dict[str, Any]] = deque()
        self.events: deque[dict[str, Any]] = deque(maxlen=4000)
        self.pending: list[tuple[float, int, str, dict[str, Any]]] = []
        self.pending_counter = 0
        self.queued_requests = 0
        self.max_queued_requests = 0
        self.physical_inflight = 0
        self.max_physical_inflight = 0
        self.ingress_inflight = 0
        self.max_ingress_inflight = 0
        self.last_ingress_completed_at: float | None = None
        self.minimum_ingress_gap_s: float | None = None
        self.traffic = TrafficEngine(self.scenario["traffic"], time_scale=self.time_scale)
        self._reset_radio()

    def _reset_radio(self) -> None:
        radio = dict(self.scenario["radio"])
        self.configured_pool = _bands(radio.get("configured_lte_pool") or [3])
        self.serving_band = _as_int(radio.get("serving_band"), self.configured_pool[0])
        self.active_bands = _bands(radio.get("active_lte_bands") or [self.serving_band])
        self.nr_bands = _bands(radio.get("nr_bands") or [78])
        self.nr_band = _as_int(radio.get("nr_band"), self.nr_bands[0])
        self.mode_choices = tuple(self.scenario.get("radio_mode", {}).get("choices", ()))
        self.configured_mode = self.scenario.get("radio_mode", {}).get("selected", "")
        self.data_enabled = True
        self.wifi_enabled = bool(radio.get("wifi_enabled", True))
        self.cell_lock = {"pci": "", "earfcn": ""}
        self.radio_available_at = self.started
        self.nr_available_at = self.started
        self.reboot_until = 0.0
        self.pending.clear()
        self.traffic.reset()

    def reset(self) -> None:
        with self.state_lock:
            self.started = time.monotonic()
            self.sessions.clear()
            self.current_session = ""
            self.latest_ld = ""
            self.latest_rd = ""
            self.latest_ad = ""
            self.field_overrides.clear()
            self.faults.clear()
            self._reset_radio()
            self.record("reset")

    def scaled(self, seconds: Any) -> float:
        return max(0.0, _as_float(seconds)) * self.time_scale

    def _timing(self, name: str, default: float = 0.0) -> float:
        return self.scaled(self.scenario["timing"].get(name, default))

    def response_delay(self, lane: str) -> float:
        key = {
            "homepage": "homepage_response_s",
            "read": "read_response_s",
            "login": "login_response_s",
            "write": "write_ack_s",
            "control": "control_response_s",
        }.get(lane, "read_response_s")
        return self._timing(key)

    def record(self, kind: str, **data: Any) -> None:
        event = {
            "kind": str(kind),
            "monotonic_s": round(time.monotonic() - self.started, 6),
            **data,
        }
        encoded = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > min(16 * 1024, EVENT_LOG_MAX_BYTES):
            event = {"kind": str(kind), "monotonic_s": event["monotonic_s"], "detail_truncated": True,
                     "detail_sha256": hashlib.sha256(encoded).hexdigest()}
            encoded = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
        self.events.append(event)
        if self.event_log is not None:
            try:
                self.event_log.parent.mkdir(parents=True, exist_ok=True)
                if self.event_log.exists() and self.event_log.stat().st_size + len(encoded) > EVENT_LOG_MAX_BYTES:
                    self.event_log.replace(self.event_log.with_suffix(self.event_log.suffix + ".1"))
                with self.event_log.open("ab") as handle:
                    handle.write(encoded)
            except OSError:
                self.event_log_error_count += 1

    def schedule(self, delay_s: float, label: str, changes: Mapping[str, Any]) -> None:
        if len(self.pending) >= 128:
            raise ValueError("mc888_simulator_pending_transition_limit")
        self.pending_counter += 1
        heapq.heappush(
            self.pending,
            (time.monotonic() + self.scaled(delay_s), self.pending_counter, str(label), dict(changes)),
        )

    def _apply_due(self) -> None:
        now = time.monotonic()
        while self.pending and self.pending[0][0] <= now:
            _at, _ordinal, label, changes = heapq.heappop(self.pending)
            for key, value in changes.items():
                if key in {"configured_pool", "active_bands", "nr_bands"}:
                    setattr(self, key, _bands(value or []))
                elif hasattr(self, key):
                    setattr(self, key, value)
            self.record("state_transition", label=label, changes=changes)

    def _new_nonce(self, name: str, length: int) -> str:
        self.nonce_counter += 1
        material = f"{name}:{self.nonce_counter}:{self.scenario['name']}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest().upper()[:length]

    def _fresh_tokens(self) -> tuple[str, str]:
        self.latest_ld = self._new_nonce("LD", 64)
        self.latest_rd = self._new_nonce("RD", 32)
        identity = self.scenario["identity"]
        seed = str(identity["wa_inner_version"]) + str(identity.get("cr_version") or "")
        self.latest_ad = self._access_id(seed, self.latest_rd)
        return self.latest_ld, self.latest_rd

    def _fresh_rd(self) -> str:
        if not self.latest_ld:
            self.latest_ld = self._new_nonce("LD", 64)
        self.latest_rd = self._new_nonce("RD", 32)
        identity = self.scenario["identity"]
        seed = str(identity["wa_inner_version"]) + str(identity.get("cr_version") or "")
        self.latest_ad = self._access_id(seed, self.latest_rd)
        return self.latest_rd

    def _access_id(self, seed: str, nonce: str) -> str:
        algorithm = self.scenario["authentication"]["access_id_algorithm"]
        if algorithm == "sha256":
            return _sha256_upper(_sha256_upper(seed) + nonce)
        if algorithm == "md5":
            inner = hashlib.md5(seed.encode("utf-8")).hexdigest()
            return hashlib.md5((inner + nonce).encode("utf-8")).hexdigest().upper()
        raise ValueError("zte_simulator_access_id_algorithm_invalid")

    def _cookie_token(self, headers: Mapping[str, str]) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(str(headers.get("Cookie") or ""))
        except Exception:
            return ""
        morsel = cookie.get("stok")
        return str(morsel.value if morsel is not None else "")

    def _expire_inactive_session(self) -> None:
        token = self.current_session
        if not token:
            return
        inactivity_s = max(1.0, _as_float(self.scenario["authentication"].get("session_inactivity_s"), 600.0))
        if time.monotonic() - self.sessions[token] > self.scaled(inactivity_s):
            self.sessions.pop(token, None)
            self.current_session = ""
            self.record("session_expired", token=token[:8])

    def authenticated(self, headers: Mapping[str, str]) -> bool:
        self._expire_inactive_session()
        token = self._cookie_token(headers)
        if not token or token != self.current_session or token not in self.sessions:
            return False
        self.sessions[token] = time.monotonic()
        return True

    def _open_session(self) -> str:
        token = self._new_nonce("STOK", 24)
        replaced = self.current_session
        self.sessions.clear()
        self.sessions[token] = time.monotonic()
        self.current_session = token
        self.record("session_opened", token=token[:8], replaced=bool(replaced))
        return token

    def expire_session(self, *, takeover: bool = False) -> None:
        old = self.current_session
        self.sessions.clear()
        self.current_session = self._new_nonce("WEBUI", 24) if takeover else ""
        if takeover:
            self.sessions[self.current_session] = time.monotonic()
        self.record("session_taken_over" if takeover else "session_forced_expired", old=old[:8])

    def _identity_fields(self) -> dict[str, Any]:
        identity = self.scenario["identity"]
        return {
            "model_name": identity["model"],
            "product_name": identity["model"],
            "hardware_version": identity["hardware_version"],
            "wa_inner_version": identity["wa_inner_version"],
            "web_version": identity["web_version"],
            "cr_version": identity.get("cr_version", ""),
            "serial_number": identity["serial"],
            "imei": identity["imei"],
            "sim_imsi": identity["imsi"],
            "imsi": identity["imsi"],
            "iccid": identity["iccid"],
            "mac_address": identity["mac_address"],
        }

    def _radio_fields(self) -> dict[str, Any]:
        self._apply_due()
        now = time.monotonic()
        radio = self.scenario["radio"]
        traffic = self.traffic.sample(now)
        rebooting = now < self.reboot_until
        radio_ready = not rebooting and now >= self.radio_available_at
        ppp_connected = radio_ready and self.data_enabled and bool(traffic["connected"])
        nr_ready = (ppp_connected and self.configured_mode != "Only_LTE"
                    and now >= self.nr_available_at and bool(traffic["nr_active"]))
        active = [self.serving_band]
        ca_enabled = ppp_connected and bool(traffic["ca_active"])
        if ca_enabled:
            for band in self.configured_pool:
                if len(active) >= max(1, _as_int(radio.get("max_lte_carriers"), 4)):
                    break
                if band not in active:
                    active.append(band)
        self.active_bands = active if ppp_connected else []
        network_type = "ENDC" if nr_ready else ("LTE_A" if ca_enabled else "LTE")
        if not radio_ready:
            network_type = ""
        ppp_status = "ipv4_ipv6_connected" if ppp_connected else "disconnected"
        if self.mode_choices and not ppp_connected:
            ppp_status = "ppp_disconnected"
        session_uptime = int(traffic["connected_seconds"]) if ppp_connected else 0
        primary = radio["lte_cells"][str(self.serving_band)]
        pci = int(primary["pci"])
        # The selected MC801A reference uses decimal PCI. MC888's retained
        # native packets use hexadecimal; do not impose that dialect on siblings.
        pci_format = "d" if self.scenario["identity"]["model"] == "MC801A" else "x"
        earfcn = int(primary["earfcn"])
        cell_id = int(primary["cell_id"])
        bandwidth = float(primary["bandwidth_mhz"])
        scell_info: list[str] = []
        scell_sig: list[str] = []
        for index, band in enumerate(self.active_bands[1:], start=1):
            cell = radio["lte_cells"][str(band)]
            sc_pci = int(cell["pci"])
            sc_earfcn = int(cell["earfcn"])
            sc_bw = float(cell["bandwidth_mhz"])
            scell_info.append(f"{index},{sc_pci},0,{band},{sc_earfcn},{sc_bw:g}")
            scell_sig.append(
                ",".join(
                    (
                        str(cell["rsrp_dbm"]),
                        str(cell["rsrq_db"]),
                        str(cell["sinr_db"]),
                        str(cell["rssi_dbm"]),
                        "0",
                        "1",
                    )
                )
            )
        rf_visible = radio_ready and bool(radio.get("rf_visible", True))
        nr_cell = radio["nr_cells"][str(self.nr_band)]
        nr_pci = int(nr_cell["pci"])
        nr_arfcn = int(nr_cell["arfcn"])
        nr_cell_id = int(nr_cell["cell_id"])
        result: dict[str, Any] = {
            "Language": "en",
            "network_type": network_type,
            "current_network_mode": network_type,
            "modem_main_state": "modem_init_complete" if radio_ready else "modem_disconnected",
            "ppp_status": ppp_status,
            "ppp_uptime": str(session_uptime),
            "wan_connected": "1" if ppp_connected else "0",
            "wan_ipaddr": str(radio.get("wan_ip", "100.64.8.8")) if ppp_connected else "",
            "wan_active_band": str(self.serving_band) if rf_visible else "",
            "wan_active_channel": str(earfcn) if rf_visible else "",
            "wan_lte_ca": "ca_activated" if len(self.active_bands) > 1 else "ca_deactivated",
            "lte_ca_pcell_band": str(self.serving_band) if rf_visible else "",
            "network_lte_ca_pcell_band": str(self.serving_band) if rf_visible else "",
            "lte_ca_pcell_bandwidth": f"{bandwidth:g}" if rf_visible else "",
            "network_lte_ca_pcell_bandwidth": f"{bandwidth:g}" if rf_visible else "",
            "lte_ca_pcell_arfcn": str(earfcn) if rf_visible else "",
            "lte_ca_pcell_freq": str(earfcn) if rf_visible else "",
            "lte_ca_scell_band": "+".join(str(v) for v in self.active_bands[1:]),
            "lte_ca_scell_bandwidth": "+".join(f"{float(radio['lte_cells'][str(b)]['bandwidth_mhz']):g}" for b in self.active_bands[1:]),
            "lte_multi_ca_scell_info": ";".join(scell_info) + (";" if scell_info else ""),
            "network_lte_ca_scell_info": ";".join(scell_info) + (";" if scell_info else ""),
            "lte_multi_ca_scell_sig_info": ";".join(scell_sig) + (";" if scell_sig else ""),
            "lte_band_lock": f"0x{_lte_mask(self.configured_pool):X}",
            "lte_pci_lock": self.cell_lock["pci"],
            "lte_earfcn_lock": self.cell_lock["earfcn"],
            "lte_pci": format(pci, pci_format) if rf_visible else "",
            "Z_PCI": format(pci, pci_format) if rf_visible else "",
            "network_Z_PCI": format(pci, pci_format) if rf_visible else "",
            "lte_earfcn": str(earfcn) if rf_visible else "",
            "Z_dl_earfcn": str(earfcn) if rf_visible else "",
            "network_Z_dl_earfcn": str(earfcn) if rf_visible else "",
            "cell_id": str(cell_id) if rf_visible else "",
            "network_cell_id": str(cell_id) if rf_visible else "",
            "tac": str(radio.get("tac", 12001)) if rf_visible else "",
            "rmcc": str(radio.get("mcc", "220")),
            "rmnc": str(radio.get("mnc", "05")),
            "mccmnc": f"{radio.get('mcc', '220')}{radio.get('mnc', '05')}",
            "network_provider": str(radio.get("provider", "A1 SRB")),
            "network_provider_fullname": str(radio.get("provider", "A1 SRB")),
            "wifiEnabled": "1" if self.wifi_enabled else "0",
            "wifi_onoff_state": "1" if self.wifi_enabled else "0",
            "wifi_chip1_ssid1_switch_onoff": "1" if self.wifi_enabled else "0",
            "wifi_chip2_ssid1_switch_onoff": "1" if self.wifi_enabled else "0",
            "realtime_rx_bytes": str(traffic["rx_total"]),
            "realtime_tx_bytes": str(traffic["tx_total"]),
            "realtime_rx_thrpt": str(traffic["rx_bytes_per_s"]),
            "realtime_tx_thrpt": str(traffic["tx_bytes_per_s"]),
            "monthly_rx_bytes": str(traffic["monthly_rx"]),
            "monthly_tx_bytes": str(traffic["monthly_tx"]),
            "realtime_time": str(session_uptime),
            "monthly_time": str(session_uptime),
            "pm_sensor_mdm": str(radio.get("temperature_lte_c", 51)),
            "pm_modem_5g": str(radio.get("temperature_nr_c", 56)),
            "upgrade_result": "success",
        }
        for key, cell_key in (
            ("lte_rsrp", "rsrp_dbm"),
            ("network_lte_rsrp", "rsrp_dbm"),
            ("lte_rsrq", "rsrq_db"),
            ("network_Z_rsrq", "rsrq_db"),
            ("lte_rssi", "rssi_dbm"),
            ("lte_snr", "sinr_db"),
            ("network_sinr", "sinr_db"),
        ):
            result[key] = str(primary[cell_key]) if rf_visible else ""
        nr_visible = rf_visible and nr_ready
        result.update(
            {
                "Z5g_rsrp": str(nr_cell["rsrp_dbm"]) if nr_visible else "",
                "Z5g_rsrq": str(nr_cell["rsrq_db"]) if nr_visible else "",
                "Z5g_SINR": str(nr_cell["sinr_db"]) if nr_visible else "",
                "Z5g_snr": str(nr_cell["sinr_db"]) if nr_visible else "",
                "nr5g_action_band": str(self.nr_band) if nr_visible else "",
                "nr5g_action_nsa_band": str(self.nr_band) if nr_visible else "",
                "network_Z5g_CELLINFO_band": f"n{self.nr_band}" if nr_visible else "",
                "Z5g_CELLINFO_band": f"n{self.nr_band}" if nr_visible else "",
                "nr5g_action_channel": str(nr_arfcn) if nr_visible else "",
                "Z5g_dlEarfcn": str(nr_arfcn) if nr_visible else "",
                "network_Z5g_PCI": format(nr_pci, pci_format) if nr_visible else "",
                "Z5g_PCI": format(nr_pci, pci_format) if nr_visible else "",
                "nr5g_pci": format(nr_pci, pci_format) if nr_visible else "",
                "nr5g_cell_id": str(nr_cell_id) if nr_visible else "",
                "Z5g_CELL_ID": str(nr_cell_id) if nr_visible else "",
                "nr5g_nsa_bandwidth": str(nr_cell["bandwidth_mhz"]) if nr_visible else "",
                "nr5g_nsa_band_lock": ",".join(str(v) for v in self.nr_bands),
                "nr5g_sa_band_lock": "",
            }
        )
        result.update(self._identity_fields())
        if self.mode_choices:
            mode = self.scenario["radio_mode"]
            result.update(net_select=self.configured_mode,
                net_select_mode=mode["operator_selection"],
                m_netselect_save=mode["saved_operator_selection"], wan_apn=mode["apn"],
                nr5g_sa_band_lock=mode["nr_sa_bands"])
        result.update(self.field_overrides)
        return result

    def snapshot(self, requested: Iterable[str], *, authenticated: bool) -> dict[str, Any]:
        requested = tuple(requested)
        settings = self.scenario.get("native_settings_read", {})
        if authenticated and requested == ("ConnectionMode",) and settings:
            return dict(settings["connection_mode_response"])
        fields = self._radio_fields()
        if authenticated:
            fields.update(settings.get("fields", {}))
        result: dict[str, Any] = {}
        for name in requested:
            key = str(name).strip()
            if not key:
                continue
            if key == "LD":
                if not self.latest_ld:
                    self._fresh_tokens()
                result[key] = self.latest_ld
            elif key == "RD":
                result[key] = self._fresh_rd()
            elif key == "loginfo":
                result[key] = "ok" if authenticated else ""
            elif authenticated or key in PUBLIC_FIELDS:
                result[key] = fields.get(key, "")
            else:
                result[key] = ""
        return result

    def login(self, form: Mapping[str, str]) -> HttpReply:
        self._expire_inactive_session()
        if not self.latest_ld or not self.latest_rd:
            self._fresh_tokens()
        goform_id = str(form.get("goformId") or "")
        supplied = str(form.get("password") or "")
        expected_hash = _sha256_upper(_sha256_upper(self.password) + self.latest_ld)
        valid_password = supplied.upper() == expected_hash
        if goform_id != "LOGIN":
            return self.json_reply({"result": "unsupported_login_method"})
        supplied_ad = str(form.get("AD") or "").upper()
        ad_required = bool(
            self.scenario["authentication"].get("login_ad_required", True)
        )
        valid_ad = (
            supplied_ad == self.latest_ad
            if ad_required
            else (not supplied_ad or supplied_ad == self.latest_ad)
        )
        if not valid_password or not valid_ad:
            self.record("login_rejected", password_ok=valid_password, ad_ok=valid_ad)
            return self.json_reply({"result": "3" if not valid_password else "1"})
        policy = str(self.scenario["authentication"].get("single_session_policy") or "replace")
        if policy == "busy" and self.current_session:
            return self.json_reply({"result": "1"})
        token = self._open_session()
        self.latest_rd = ""
        self.latest_ad = ""
        return self.json_reply(
            {"result": "0"},
            headers=(("Set-Cookie", f"stok={token}; path=/; HttpOnly"),),
        )

    def _ad_ok(self, form: Mapping[str, str]) -> bool:
        supplied = str(form.get("AD") or "").upper()
        return bool(supplied and self.latest_ad and supplied == self.latest_ad)

    def _radio_transition(self, *, label: str, changes: Mapping[str, Any], override: Mapping[str, Any] | None = None) -> None:
        changes = dict(changes)
        if override:
            for name in ("serving_band", "nr_band"):
                if name in override:
                    changes[name] = int(override[name])
        timing = self.scenario["timing"]
        self.radio_available_at = time.monotonic() + self.scaled(timing["lte_reattach_s"])
        self.nr_available_at = time.monotonic() + self.scaled(timing["nr_reattach_s"])
        self.traffic.block_until(self.radio_available_at, nr_deadline=self.nr_available_at)
        self.schedule(_as_float(timing["lte_reattach_s"]), label, changes)
        self.record(
            "radio_transition_started",
            label=label,
            lte_reattach_s=timing["lte_reattach_s"],
            nr_reattach_s=timing["nr_reattach_s"],
        )

    def write(self, form: Mapping[str, str], *, authenticated: bool) -> HttpReply:
        goform_id = str(form.get("goformId") or "").strip()
        if goform_id in {"LOGIN", "LOGIN_MULTI_USER"}:
            return self.login(form)
        if not authenticated:
            return self.json_reply({"result": "3"})
        if goform_id in {"LOGOFF", "LOGOUT"}:
            token = self.current_session
            self.sessions.clear()
            self.current_session = ""
            self.record("logout", token=token[:8])
            return self.json_reply({"result": "success"})
        if not self._ad_ok(form):
            self.record("write_rejected", goform_id=goform_id, reason="stale_or_missing_ad")
            return self.json_reply({"result": "3"})
        self._apply_due()
        if len(self.pending) >= 128:
            return self.json_reply({"result": "failure"})
        self.latest_rd = ""
        self.latest_ad = ""
        outcome_spec = self.scenario.get("write_outcomes", {}).get(goform_id, {})
        outcome = outcome_spec.get("outcome", "applied")
        if outcome in {"ignored", "rejected"}:
            self.record("write_outcome", goform_id=goform_id, outcome=outcome)
            return self.json_reply({"result": "success" if outcome == "ignored" else "failure"})
        override = outcome_spec if outcome == "overridden" else None
        if goform_id == "SET_BEARER_PREFERENCE":
            native = form.get("BearerPreference")
            if not self.mode_choices or native not in self.mode_choices or self.data_enabled:
                return self.json_reply({"result": "failure"})
            self._radio_transition(label="configured_mode", changes={"configured_mode": native})
        elif goform_id in {"BAND_SELECT", "SET_LTE_BAND_LOCK", "SET_NETWORK_BAND_LOCK"}:
            raw_mask = form.get("lte_band_mask") or form.get("lte_band_lock")
            pool = _decode_lte_mask(raw_mask)
            nr_raw = form.get("nr5g_nsa_band_lock") or form.get("nr5g_band_mask")
            nr_bands = _bands(str(nr_raw or "").replace("n", "").split(",")) if nr_raw else []
            if any(str(b) not in self.scenario["radio"]["lte_cells"] for b in pool) or any(str(b) not in self.scenario["radio"]["nr_cells"] for b in nr_bands):
                return self.json_reply({"result": "failure"})
            changes: dict[str, Any] = {}
            if pool:
                preferred = [band for band in self.scenario["radio"].get("serving_priority", []) if band in pool]
                changes["configured_pool"] = pool
                changes["serving_band"] = preferred[0] if preferred else pool[0]
                changes["cell_lock"] = {"pci": "", "earfcn": ""}
            if nr_bands:
                changes["nr_bands"] = nr_bands
                changes["nr_band"] = nr_bands[0]
            if not changes:
                return self.json_reply({"result": "failure"})
            self._radio_transition(label="band_select", changes=changes, override=override)
        elif goform_id == "WAN_PERFORM_NR5G_BAND_LOCK":
            nr_bands = _bands(str(form.get("nr5g_band_mask") or "").replace("n", "").split(","))
            if not nr_bands or any(str(b) not in self.scenario["radio"]["nr_cells"] for b in nr_bands):
                return self.json_reply({"result": "failure"})
            self._radio_transition(label="nr_band_select", changes={"nr_bands": nr_bands, "nr_band": nr_bands[0]}, override=override)
        elif goform_id == "LTE_LOCK_CELL_SET":
            pci = str(form.get("lte_pci_lock") or "").strip()
            earfcn = str(form.get("lte_earfcn_lock") or "").strip()
            cells = self.scenario["radio"]["lte_cells"]
            matches = [int(band) for band, cell in cells.items() if str(cell["pci"]) == pci and str(cell["earfcn"]) == earfcn and int(band) in self.configured_pool]
            if not pci and not earfcn:
                matches = [band for band in self.scenario["radio"]["serving_priority"] if band in self.configured_pool]
            if not matches:
                return self.json_reply({"result": "failure"})
            self._radio_transition(label="cell_lock", changes={"cell_lock": {"pci": pci, "earfcn": earfcn}, "serving_band": matches[0]}, override=override)
        elif goform_id == "SET_WIFI_INFO":
            enabled = str(form.get("wifiEnabled") or form.get("m_ssid_enable") or "0") == "1"
            self.schedule(_as_float(self.scenario["timing"]["wifi_apply_s"]), "wifi_apply", {"wifi_enabled": enabled})
        elif goform_id == "REBOOT_DEVICE":
            now = time.monotonic()
            self.reboot_until = now + self._timing("reboot_downtime_s")
            self.radio_available_at = self.reboot_until + self._timing("radio_drop_s")
            self.nr_available_at = self.reboot_until + self._timing("nr_reattach_s")
            self.traffic.block_until(self.radio_available_at, nr_deadline=self.nr_available_at)
            self.sessions.clear()
            self.current_session = ""
            self.record("reboot_started", downtime_s=self.scenario["timing"]["reboot_downtime_s"])
        elif goform_id == "DISCONNECT_NETWORK":
            self.data_enabled = False
            if self.mode_choices:
                self.traffic.set_data_enabled(False)
            else:
                self.traffic.select("outage", duration_s=0.0)
        elif goform_id == "CONNECT_NETWORK":
            self.data_enabled = True
            if self.mode_choices:
                self.traffic.set_data_enabled(True)
            else:
                self.traffic.select("idle", duration_s=0.0)
        else:
            self.record("write_rejected", goform_id=goform_id, reason="unsupported")
            return self.json_reply({"result": "failure"})
        self.record("write_accepted", goform_id=goform_id, form={k: v for k, v in form.items() if k not in {"password", "AD"}})
        if outcome == "ack_lost":
            self.record("write_outcome", goform_id=goform_id, outcome=outcome)
            return HttpReply(status=200, body=b"", close_without_reply=True)
        return self.json_reply({"result": "success"})

    def inject_fault(self, kind: str, *, count: int = 1, delay_s: float = 0.0) -> None:
        allowed = {"empty_json", "malformed_json", "http_500", "timeout", "drop_connection", "blank_rf"}
        if kind not in allowed:
            raise ValueError(f"mc888_simulator_unknown_fault:{kind}")
        if type(count) is not int or not 1 <= count <= MAX_QUEUED_FAULTS or len(self.faults) + count > MAX_QUEUED_FAULTS:
            raise ValueError("mc888_simulator_fault_queue_limit")
        if not math.isfinite(float(delay_s)) or not 0 <= delay_s <= 120:
            raise ValueError("mc888_simulator_fault_delay_invalid")
        for _ in range(count):
            self.faults.append({"kind": kind, "delay_s": float(delay_s)})
        self.record("fault_queued", fault=kind, count=count)

    def pop_fault(self) -> dict[str, Any] | None:
        return self.faults.popleft() if self.faults else None

    @staticmethod
    def json_reply(payload: Mapping[str, Any], *, status: int = 200, headers: tuple[tuple[str, str], ...] = ()) -> HttpReply:
        return HttpReply(status=status, body=json.dumps(dict(payload), separators=(",", ":")).encode("utf-8"), headers=headers)

    def control(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "").strip()
        if action == "set_traffic":
            self.traffic.select(str(payload.get("profile") or ""), _as_float(payload.get("duration_s")))
        elif action == "session_takeover":
            self.expire_session(takeover=True)
        elif action == "expire_session":
            self.expire_session(takeover=False)
        elif action == "fault":
            self.inject_fault(
                str(payload.get("kind") or ""),
                count=payload.get("count", 1),
                delay_s=float(payload.get("delay_s", 0.0)),
            )
        elif action == "set_fields":
            fields = payload.get("fields")
            if not isinstance(fields, Mapping):
                raise ValueError("mc888_simulator_control_fields_mapping_required")
            if len(set(self.field_overrides) | set(fields)) > 256 or any(not isinstance(key, str) or not key.strip() or len(key) > 128 for key in fields):
                raise ValueError("mc888_simulator_control_field_limit")
            for key, value in fields.items():
                name = str(key or "").strip()
                if not name:
                    raise ValueError("mc888_simulator_control_field_name_required")
                self.field_overrides[name] = "" if value is None else value
        elif action == "clear_fields":
            names = payload.get("fields")
            if names is None:
                self.field_overrides.clear()
            elif isinstance(names, list):
                for name in names:
                    self.field_overrides.pop(str(name or "").strip(), None)
            else:
                raise ValueError("mc888_simulator_control_field_list_required")
        elif action == "reset":
            self.reset()
        else:
            raise ValueError(f"mc888_simulator_unknown_control_action:{action or '<empty>'}")
        return self.public_state()

    def public_state(self) -> dict[str, Any]:
        self._expire_inactive_session()
        fields = self._radio_fields()
        traffic = self.traffic.sample()
        return {
            "lab_session": self.lab_session,
            "process_id": os.getpid(),
            "scenario_sha256": self.scenario_sha256,
            "scenario": self.scenario["name"],
            "evidence": self.scenario["evidence"],
            "firmware": self.scenario["identity"]["wa_inner_version"],
            "session_active": bool(self.current_session),
            "configured_lte_pool": list(self.configured_pool),
            "serving_band": self.serving_band,
            "active_lte_bands": list(self.active_bands),
            "cell_lock": dict(self.cell_lock),
            "nr_band": self.nr_band,
            "network_type": fields.get("network_type"),
            "ppp_status": fields.get("ppp_status"),
            "wifi_enabled": self.wifi_enabled,
            "traffic": traffic,
            "field_overrides": dict(self.field_overrides),
            "queued_faults": len(self.faults),
            "event_log_error_count": self.event_log_error_count,
            "queued_requests": self.queued_requests,
            "max_queued_requests": self.max_queued_requests,
            "max_physical_inflight": self.max_physical_inflight,
            "max_ingress_inflight": self.max_ingress_inflight,
            "minimum_ingress_gap_s": self.minimum_ingress_gap_s,
            "pending_transitions": len(self.pending),
        }


class _Handler(BaseHTTPRequestHandler):
    server_version = "WebServer-Webs"
    protocol_version = "HTTP/1.1"

    @property
    def appliance(self) -> ZteMc888Appliance:
        return self.server.appliance  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _read_body(self) -> bytes:
        raw = self.headers.get("Content-Length", "0")
        if not raw.isdigit() or len(raw) > 10:
            raise ValueError("invalid_content_length")
        length = int(raw)
        maximum = MAX_TRANSFER_BYTES if urlparse(self.path).path == "/_sim/upload" else MAX_CONTROL_BYTES
        if length > maximum:
            raise ValueError("body_too_large")
        self.connection.settimeout(5.0)
        return self.rfile.read(length) if length else b""

    def _control_allowed(self) -> bool:
        return secrets.compare_digest(
            str(self.headers.get(CONTROL_HEADER) or ""),
            self.appliance.control_token,
        )

    def _dispatch(self, method: str, body: bytes) -> tuple[HttpReply, str]:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        now = time.monotonic()
        authenticated = False if path.startswith("/_sim/") else self.appliance.authenticated(self.headers)
        if not path.startswith("/_sim/") and now < self.appliance.reboot_until:
            return self.appliance.json_reply({"error": "booting"}, status=503), "read"
        if path == "/" and method == "GET":
            identity = self.appliance.scenario["identity"]
            html = f"<html><title>ZTE 5G CPE</title><body>{identity['model']} {identity['wa_inner_version']}</body></html>"
            return HttpReply(200, html.encode("utf-8"), "text/html"), "homepage"
        if path == "/js/config/config.js" and method == "GET" and self.appliance.mode_choices:
            choices = ",".join("{value:" + json.dumps(value) + "}"
                               for value in self.appliance.mode_choices)
            return HttpReply(200, ("define({AUTO_MODES:[" + choices + "]});").encode(),
                             "application/javascript"), "read"
        if path.startswith("/_sim/"):
            if not self._control_allowed():
                return self.appliance.json_reply({"error": "control_token_required"}, status=403), "control"
            if path == "/_sim/state" and method == "GET":
                return self.appliance.json_reply(self.appliance.public_state()), "control"
            if path == "/_sim/events" and method == "GET":
                return self.appliance.json_reply({"events": list(self.appliance.events)}), "control"
            if path == "/_sim/network-sample" and method == "GET":
                return self.appliance.json_reply(self.appliance.traffic.sample()), "control"
            if path == "/_sim/control" and method == "POST":
                try:
                    payload = json.loads(body.decode("utf-8") or "{}", parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"nonfinite_json:{value}")))
                    if not isinstance(payload, dict):
                        raise ValueError("control_payload_mapping_required")
                    return self.appliance.json_reply(self.appliance.control(payload)), "control"
                except (ValueError, TypeError, OverflowError) as exc:
                    return self.appliance.json_reply({"error": str(exc)}, status=400), "control"
            if path == "/_sim/download" and method == "GET":
                size = min(32 * 1024 * 1024, max(1, _as_int((query.get("bytes") or [1_048_576])[0], 1_048_576)))
                self.appliance.traffic.add_bytes(rx_bytes=size)
                return HttpReply(200, b"\x5a" * size, "application/octet-stream"), "control"
            if path == "/_sim/upload" and method == "POST":
                self.appliance.traffic.add_bytes(tx_bytes=len(body))
                return self.appliance.json_reply({"accepted_bytes": len(body)}), "control"
            return self.appliance.json_reply({"error": "not_found"}, status=404), "control"
        if path == "/goform/goform_get_cmd_process" and method == "GET":
            fault = self.appliance.pop_fault()
            if fault:
                kind = fault["kind"]
                if kind == "timeout":
                    fields = [item for item in str((query.get("cmd") or [""])[0]).split(",") if item]
                    reply = self.appliance.json_reply(self.appliance.snapshot(fields, authenticated=authenticated))
                    return HttpReply(reply.status, reply.body, delay_s=self.appliance.scaled(fault.get("delay_s") or 2.0)), "read"
                elif kind == "http_500":
                    return self.appliance.json_reply({"error": "injected"}, status=500), "read"
                elif kind == "malformed_json":
                    return HttpReply(200, b'{"network_type":"ENDC",,}'), "read"
                elif kind == "empty_json":
                    return self.appliance.json_reply({}), "read"
                elif kind == "drop_connection":
                    return HttpReply(0, b"", close_without_reply=True), "read"
                elif kind == "blank_rf":
                    fields = [item for item in str((query.get("cmd") or [""])[0]).split(",") if item]
                    payload = self.appliance.snapshot(fields, authenticated=authenticated)
                    for key in tuple(payload):
                        if key not in self.appliance._identity_fields() and key not in {"loginfo", "LD", "RD"}:
                            payload[key] = ""
                    return self.appliance.json_reply(payload), "read"
            fields = [item for item in str((query.get("cmd") or [""])[0]).split(",") if item]
            return self.appliance.json_reply(self.appliance.snapshot(fields, authenticated=authenticated)), "read"
        if path == "/goform/goform_set_cmd_process" and method == "POST":
            form = {str(key): str(value) for key, value in parse_qsl(body.decode("utf-8"), keep_blank_values=True)}
            lane = "login" if form.get("goformId") in {"LOGIN", "LOGIN_MULTI_USER"} else "write"
            return self.appliance.write(form, authenticated=authenticated), lane
        return self.appliance.json_reply({"error": "not_found"}, status=404), "read"

    def _send(self, reply: HttpReply) -> None:
        self.close_connection = True
        if reply.close_without_reply:
            self.close_connection = True
            return
        self.send_response(int(reply.status))
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(reply.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for key, value in reply.headers:
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(reply.body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle(self, method: str) -> None:
        appliance = self.appliance
        physical = not urlparse(self.path).path.startswith("/_sim/")
        if physical:
            with appliance.ingress_lock:
                now = time.monotonic()
                if appliance.last_ingress_completed_at is not None:
                    gap = max(0.0, now - appliance.last_ingress_completed_at)
                    prior = appliance.minimum_ingress_gap_s
                    appliance.minimum_ingress_gap_s = gap if prior is None else min(prior, gap)
                appliance.ingress_inflight += 1
                appliance.max_ingress_inflight = max(appliance.max_ingress_inflight, appliance.ingress_inflight)
        try:
            self._serve_request(method)
        finally:
            if physical:
                with appliance.ingress_lock:
                    appliance.ingress_inflight -= 1
                    appliance.last_ingress_completed_at = time.monotonic()

    def _serve_request(self, method: str) -> None:
        appliance = self.appliance
        received = time.monotonic()
        try:
            body = self._read_body() if method == "POST" else b""
        except (ValueError, TimeoutError) as exc:
            self._send(appliance.json_reply({"error": str(exc)}, status=413 if str(exc) == "body_too_large" else 400))
            return
        path = urlparse(self.path).path
        if path.startswith("/_sim/"):
            with appliance.state_lock:
                reply, lane = self._dispatch(method, body)
            time.sleep(appliance.response_delay(lane))
            self._send(reply)
            return
        with appliance.state_lock:
            appliance.queued_requests += 1
            appliance.max_queued_requests = max(appliance.max_queued_requests, appliance.queued_requests)
        with appliance.request_lock:
            acquired = time.monotonic()
            with appliance.state_lock:
                appliance.queued_requests -= 1
                appliance.physical_inflight += 1
                appliance.max_physical_inflight = max(appliance.max_physical_inflight, appliance.physical_inflight)
                appliance.request_counter += 1
                request_id = appliance.request_counter
            status = 0
            lane = "read"
            try:
                with appliance.state_lock:
                    reply, lane = self._dispatch(method, body)
                time.sleep(appliance.response_delay(lane) + reply.delay_s)
                status = int(reply.status)
                self._send(reply)
            finally:
                finished = time.monotonic()
                with appliance.state_lock:
                    appliance.physical_inflight -= 1
                    appliance.record(
                        "http_request",
                        request_id=request_id,
                        method=method,
                        path=urlparse(self.path).path,
                        lane=lane,
                        status=status,
                        queue_wait_ms=round((acquired - received) * 1000.0, 3),
                        elapsed_ms=round((finished - received) * 1000.0, 3),
                    )


class ZteMc888SimulatorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], appliance: ZteMc888Appliance):
        self.appliance = appliance
        super().__init__(server_address, _Handler)


class RunningSimulator:
    def __init__(self, server: ZteMc888SimulatorServer) -> None:
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, name="zte-mc888-simulator", daemon=True)

    @property
    def host(self) -> str:
        return str(self.server.server_address[0])

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def appliance(self) -> ZteMc888Appliance:
        return self.server.appliance

    def start(self) -> "RunningSimulator":
        self.thread.start()
        return self

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3.0)

    def __enter__(self) -> "RunningSimulator":
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


def validate_scenario(value: Mapping[str, Any]) -> dict[str, Any]:
    data = json.loads(json.dumps(dict(value or {})))
    if data.get("schema") != SCENARIO_SCHEMA:
        raise ValueError("mc888_simulator_scenario_schema_invalid")
    for key in ("name", "evidence", "identity", "authentication", "timing", "radio", "traffic"):
        if not data.get(key):
            raise ValueError(f"mc888_simulator_scenario_{key}_required")
    identity = data["identity"]
    mode = data.get("radio_mode")
    if mode is not None:
        choices = mode.get("choices") if isinstance(mode, dict) else None
        known = {"Only_LTE", "NETWORK_auto", "LTE_AND_5G", "WL_AND_5G", "4G_AND_5G"}
        if (not isinstance(choices, list) or not 2 <= len(choices) <= 5
                or any(type(value) is not str or value not in known for value in choices)
                or len(set(choices)) != len(choices) or "Only_LTE" not in choices
                or mode.get("selected") not in choices
                or any(type(mode.get(key)) is not str for key in (
                    "nr_sa_bands", "operator_selection", "saved_operator_selection", "apn"))):
            raise ValueError("mc888_simulator_radio_mode_invalid")
    algorithm = data["authentication"].get("access_id_algorithm")
    if type(algorithm) is not str or algorithm not in {"sha256", "md5"}:
        raise ValueError("zte_simulator_access_id_algorithm_required_or_invalid")
    settings = data.get("native_settings_read")
    if settings is not None:
        allowed = {"imsi", "sim_imsi", "net_select_mode", "wan_apn", "ppp_username",
                   "ppp_auth_mode", "pdp_type", "profile_name", "Current_index"}
        if (not isinstance(settings, dict)
                or set(settings) != {"connection_mode_response", "fields"}
                or not isinstance(settings["connection_mode_response"], dict)
                or set(settings["connection_mode_response"]) != {"connectionMode", "autoConnectWhenRoaming"}
                or not isinstance(settings["fields"], dict)
                or not set(settings["fields"]) <= allowed
                or any(type(v) is not str or len(v) > 256 for row in settings.values() for v in row.values())):
            raise ValueError("mc888_simulator_native_settings_read_invalid")
    for key in ("model", "hardware_version", "wa_inner_version", "web_version", "serial", "imei", "imsi", "iccid", "mac_address"):
        if key not in identity:
            raise ValueError(f"mc888_simulator_identity_{key}_required")
    timing = data["timing"]
    for key in (
        "homepage_response_s",
        "read_response_s",
        "login_response_s",
        "write_ack_s",
        "control_response_s",
        "radio_drop_s",
        "lte_reattach_s",
        "nr_reattach_s",
        "wifi_apply_s",
        "reboot_downtime_s",
    ):
        if not math.isfinite(_as_float(timing.get(key), -1.0)) or _as_float(timing.get(key), -1.0) < 0.0:
            raise ValueError(f"mc888_simulator_timing_{key}_invalid")
    if timing["nr_reattach_s"] < timing["lte_reattach_s"]:
        raise ValueError("mc888_simulator_nr_reattach_precedes_lte")
    radio = data["radio"]
    for family, frequency, max_pci in (("lte", "earfcn", 503), ("nr", "arfcn", 1007)):
        cells = radio.get(f"{family}_cells")
        if not isinstance(cells, dict) or not cells:
            raise ValueError(f"mc888_simulator_{family}_topology_required")
        for band, cell in cells.items():
            if not str(band).isdigit() or int(band) <= 0 or not isinstance(cell, dict):
                raise ValueError("mc888_simulator_topology_cell_invalid")
            for name in ("pci", frequency, "cell_id", "bandwidth_mhz", "rsrp_dbm", "rsrq_db", "sinr_db") + (("rssi_dbm",) if family == "lte" else ()):
                if name not in cell or not math.isfinite(float(cell[name])):
                    raise ValueError(f"mc888_simulator_topology_{family}_{band}_{name}_invalid")
            if any(type(cell[name]) is not int for name in ("pci", frequency, "cell_id")):
                raise ValueError("mc888_simulator_topology_identity_integer_required")
            minimum = 0 if family == "nr" else 1
            if not 0 <= cell["pci"] <= max_pci or cell[frequency] < minimum or cell["cell_id"] < minimum or float(cell["bandwidth_mhz"]) <= 0:
                raise ValueError("mc888_simulator_topology_identity_invalid")
        selected = radio["configured_lte_pool"] if family == "lte" else radio["nr_bands"]
        serving = radio["serving_band"] if family == "lte" else radio["nr_band"]
        if not selected or serving not in selected or any(str(b) not in cells for b in selected):
            raise ValueError("mc888_simulator_topology_selected_band_missing")
    for spec in data.get("write_outcomes", {}).values():
        if not isinstance(spec, dict) or spec.get("outcome") not in {"applied", "ignored", "rejected", "ack_lost", "overridden"}:
            raise ValueError("mc888_simulator_write_outcome_invalid")
        if spec["outcome"] == "overridden":
            if not any(key in spec for key in ("serving_band", "nr_band")):
                raise ValueError("mc888_simulator_write_override_target_required")
            for key, family in (("serving_band", "lte"), ("nr_band", "nr")):
                if key in spec and str(spec[key]) not in radio[f"{family}_cells"]:
                    raise ValueError("mc888_simulator_write_override_topology_missing")
    return data


def load_scenario(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    with source.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("mc888_simulator_scenario_mapping_required")
    return validate_scenario(value)


def create_simulator(
    scenario: Mapping[str, Any],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    password: str | None = None,
    control_token: str = "mc888-lab",
    time_scale: float = 1.0,
    event_log: str | Path | None = None,
    lab_session: str = "",
) -> RunningSimulator:
    bind_host = str(host or "127.0.0.1").strip()
    if bind_host not in {"127.0.0.1", "localhost"}:
        raise ValueError("mc888_simulator_loopback_bind_required")
    appliance = ZteMc888Appliance(
        scenario,
        password=password,
        control_token=control_token,
        time_scale=time_scale,
        event_log=event_log,
        lab_session=lab_session,
    )
    server = ZteMc888SimulatorServer((bind_host, int(port)), appliance)
    return RunningSimulator(server)
