"""
arch4/modem/device_probe.py
═══════════════════════════════════════════════════════════════
Read-only device probe for backend detection at bootstrap time.

Probes common router/modem IPs with lightweight HTTP requests
and classifies the device without authentication or state changes.

Returns:
    detected_backend: str   — e.g. "b818", "mikrotik", "openwrt", ""
    backend_family: str     — e.g. "huawei_hilink_like", "routeros_at_like", "unknown"
    confidence: float       — 0.0 (nothing detected) to 1.0 (certain)
    host: str               — IP address that responded
    probe_details: dict     — raw classification data
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import logging
import re
import socket
import subprocess
from dataclasses import dataclass, field
from typing import Any

from arch4.modem.discovery_defaults import DEFAULT_DISCOVERY_HOSTS

log = logging.getLogger("arch4.modem.device_probe")

# ── Known detection signatures ──────────────────────────────────────────

# Each signature: (family, backend_hint, confidence, description)
# Checked in order — first match wins within a host probe.

_HUAWEI_HILINK_MARKERS = [
    "HiLink",
    "/api/webserver/SesTokInfo",
    "HUAWEI",
    "huawei",
    "e.customData",
    "wlan/multi-basic-settings",
    "emui webui",
    "csrf_token",
    "emui-jquery",
    "AJAX_HEADER",
]

_MIKROTIK_MARKERS = [
    "RouterOS",
    "mikrotik",
    "MikroTik",
    "webfig",
    "/jsproxy",
]

_OPENWRT_MARKERS = [
    "LuCI",
    "openwrt",
    "OpenWrt",
    "luci-static",
]

_ZTE_GOFORM_MARKERS = [
    "goform",
    "ZTE",
    "zte_webui",
    "CR_EWAN_STATUS",
]

_ZTE_MF296R_MARKERS = [
    "MF296R",
    "WL1MF296R",
    "A1SRBMF296R",
]

_ZTE_MC888_MARKERS = [
    "MC888",
    "MC801A",
    "LIVEUKMC888",
    "H3GUKMC888",
]

_ZTE_ZWRT_MARKERS = [
    "ZTE",
    "ubus",
    "luci",  # ZTE ZWRT also uses LuCI
]

_ZYXEL_MARKERS = [
    "Zyxel",
    "zyxel",
    "NR5103",
    "NR7101",
]


@dataclass(frozen=True)
class DeviceProbeResult:
    """Result of a read-only device probe."""
    detected_backend: str = ""
    backend_family: str = ""
    confidence: float = 0.0
    host: str = ""
    probe_details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected_backend": self.detected_backend,
            "backend_family": self.backend_family,
            "confidence": self.confidence,
            "host": self.host,
            "probe_details": dict(self.probe_details),
        }


@dataclass(frozen=True)
class NetworkDiscoveryEntry:
    """One read-only network discovery finding."""
    host: str
    category: str = ""
    detected_backend: str = ""
    backend_family: str = ""
    confidence: float = 0.0
    open_ports: tuple[int, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class NetworkScanResult:
    """Summary of an explicit on-demand network scan."""
    mode: str = ""
    seed_host: str = ""
    scanned_host_count: int = 0
    entries: tuple[NetworkDiscoveryEntry, ...] = ()


# ── Default probe targets ───────────────────────────────────────────────

DEFAULT_PROBE_HOSTS = list(DEFAULT_DISCOVERY_HOSTS)

_GENERIC_DISCOVERY_PORTS = (443, 22)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _tcp_reachable(host: str, port: int = 80, timeout: float = 2.0) -> bool:
    """Quick TCP connect check — no HTTP, just SYN/ACK."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _is_private_ipv4(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _parse_ipv4_candidates(text: str) -> list[str]:
    hosts: list[str] = []
    seen: set[str] = set()
    for match in _IPV4_RE.findall(str(text or "")):
        try:
            ip = str(ipaddress.ip_address(match))
        except ValueError:
            continue
        if ip in seen:
            continue
        seen.add(ip)
        hosts.append(ip)
    return hosts


def _neighbor_cache_hosts() -> list[str]:
    """Best-effort read-only local neighbor discovery from OS caches."""
    commands = [
        ["ip", "neigh"],
        ["arp", "-a"],
    ]
    hosts: list[str] = []
    seen: set[str] = set()
    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except Exception:
            continue
        for ip in _parse_ipv4_candidates(proc.stdout):
            if ip in seen or not _is_private_ipv4(ip):
                continue
            seen.add(ip)
            hosts.append(ip)
    return hosts


def build_network_scan_hosts(seed_host: str = "") -> tuple[str, list[str]]:
    """Build the explicit network-scan target list.

    If the user entered a private IPv4 host, scan that `/24`.
    Otherwise, fall back to neighbor-cache IPs plus known modem defaults.
    """
    seed = str(seed_host or "").strip()
    if _is_private_ipv4(seed):
        net = ipaddress.ip_network(f"{seed}/24", strict=False)
        hosts = [str(ip) for ip in net.hosts()]
        return "seed_subnet", hosts

    ordered: list[str] = []
    seen: set[str] = set()

    def _push(host: str) -> None:
        item = str(host or "").strip()
        if not item or item in seen or not _is_private_ipv4(item):
            return
        seen.add(item)
        ordered.append(item)

    for host in _neighbor_cache_hosts():
        _push(host)
    for host in DEFAULT_PROBE_HOSTS:
        _push(host)
    return "neighbor_and_defaults", ordered


def _http_probe(host: str, timeout: float = 3.0) -> tuple[int, str]:
    """Minimal HTTP GET / — returns (status_code, body_snippet).

    Uses raw socket + minimal HTTP to avoid heavy dependencies.
    Returns (0, "") on any failure.
    """
    try:
        import urllib.request
        import urllib.error

        url = f"http://{host}/"
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "54GUC-Probe/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read(4096).decode("utf-8", errors="ignore")
            return status, body
    except urllib.error.HTTPError as e:
        # Even error responses can contain useful identification data
        try:
            body = e.read(4096).decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        return e.code, body
    except Exception:
        return 0, ""


def _classify_response(host: str, status: int, body: str) -> DeviceProbeResult:
    """Classify a device from its HTTP response."""
    if status == 0 or not body:
        return DeviceProbeResult(
            host=host,
            probe_details={"status": status, "body_length": len(body)},
        )

    body_lower = body.lower()
    details: dict[str, Any] = {
        "status": status,
        "body_length": len(body),
    }

    # ── Huawei HiLink ──
    hilink_score = sum(1 for m in _HUAWEI_HILINK_MARKERS if m.lower() in body_lower)
    if hilink_score >= 2:
        details["hilink_markers"] = hilink_score
        return DeviceProbeResult(
            detected_backend="b818",
            backend_family="huawei_hilink_like",
            confidence=min(0.5 + hilink_score * 0.15, 0.95),
            host=host,
            probe_details=details,
        )

    # ── MikroTik RouterOS ──
    mikrotik_score = sum(1 for m in _MIKROTIK_MARKERS if m.lower() in body_lower)
    if mikrotik_score >= 1:
        details["mikrotik_markers"] = mikrotik_score
        return DeviceProbeResult(
            detected_backend="mikrotik",
            backend_family="mikrotik_routeros",
            confidence=min(0.5 + mikrotik_score * 0.2, 0.95),
            host=host,
            probe_details=details,
        )

    # ── ZTE goform ──
    zte_goform_score = sum(1 for m in _ZTE_GOFORM_MARKERS if m.lower() in body_lower)
    mf296r_score = sum(1 for m in _ZTE_MF296R_MARKERS if m.lower() in body_lower)
    mc888_score = sum(1 for m in _ZTE_MC888_MARKERS if m.lower() in body_lower)
    if mf296r_score >= 1:
        details["zte_goform_markers"] = zte_goform_score
        details["mf296r_markers"] = mf296r_score
        return DeviceProbeResult(
            detected_backend="mf296r",
            backend_family="zte_goform_like",
            confidence=min(0.70 + mf296r_score * 0.15 + zte_goform_score * 0.05, 0.98),
            host=host,
            probe_details=details,
        )
    if mc888_score >= 1:
        details["zte_goform_markers"] = zte_goform_score
        details["mc888_markers"] = mc888_score
        return DeviceProbeResult(
            detected_backend="mc888",
            backend_family="zte_goform_like",
            confidence=min(0.70 + mc888_score * 0.15 + zte_goform_score * 0.05, 0.98),
            host=host,
            probe_details=details,
        )
    if zte_goform_score >= 2:
        # ZTE goform confirmed by body markers but no model-specific markers found.
        # Do NOT infer model from IP address — the user must select the correct
        # profile on the SIGN IN page. Returning an empty backend with family known
        # keeps the UI honest: it found a ZTE device but cannot name the model.
        details["zte_goform_markers"] = zte_goform_score
        return DeviceProbeResult(
            detected_backend="",
            backend_family="zte_goform_like",
            confidence=min(0.45 + zte_goform_score * 0.05, 0.60),
            host=host,
            probe_details=details,
        )

    # ── OpenWrt (check before ZTE ZWRT since both use LuCI) ──
    openwrt_score = sum(1 for m in _OPENWRT_MARKERS if m.lower() in body_lower)
    zte_zwrt_score = sum(1 for m in _ZTE_ZWRT_MARKERS if m.lower() in body_lower)

    # If ZTE + LuCI, it's likely a ZTE ZWRT device
    if zte_zwrt_score >= 2 and "zte" in body_lower:
        details["zte_zwrt_markers"] = zte_zwrt_score
        return DeviceProbeResult(
            detected_backend="zte_zwrt",
            backend_family="zte_zwrt_like",
            confidence=min(0.5 + zte_zwrt_score * 0.15, 0.85),
            host=host,
            probe_details=details,
        )

    if openwrt_score >= 2:
        details["openwrt_markers"] = openwrt_score
        return DeviceProbeResult(
            detected_backend="openwrt",
            backend_family="openwrt_like",
            confidence=min(0.5 + openwrt_score * 0.15, 0.85),
            host=host,
            probe_details=details,
        )

    # ── Zyxel ──
    zyxel_score = sum(1 for m in _ZYXEL_MARKERS if m.lower() in body_lower)
    if zyxel_score >= 1:
        details["zyxel_markers"] = zyxel_score
        return DeviceProbeResult(
            detected_backend="nr5103e",
            backend_family="zyxel_like",
            confidence=min(0.4 + zyxel_score * 0.2, 0.80),
            host=host,
            probe_details=details,
        )

    # ── Unknown but reachable ──
    details["classification"] = "unknown_http_device"
    details["modem_live_profile"] = "unknown"
    details["discovery_policy"] = "read_only_probe_only"
    return DeviceProbeResult(
        detected_backend="",
        backend_family="unknown",
        confidence=0.1,
        host=host,
        probe_details=details,
    )


def probe_device(
    host: str,
    *,
    tcp_timeout: float = 2.0,
    http_timeout: float = 3.0,
) -> DeviceProbeResult:
    """Probe a single host and classify the device.

    Read-only: only TCP connect + HTTP GET /.
    No authentication, no writes, no state changes.
    """
    if not _tcp_reachable(host, timeout=tcp_timeout):
        return DeviceProbeResult(
            host=host,
            probe_details={"status": "unreachable", "tcp_timeout": tcp_timeout},
        )

    status, body = _http_probe(host, timeout=http_timeout)
    return _classify_response(host, status, body)


def probe_all_defaults(
    *,
    hosts: list[str] | None = None,
    tcp_timeout: float = 2.0,
    http_timeout: float = 3.0,
) -> DeviceProbeResult:
    """Probe all default router IPs and return the best match.

    Returns the result with highest confidence. If nothing is found,
    returns an empty DeviceProbeResult.
    """
    targets = hosts or DEFAULT_PROBE_HOSTS
    best = DeviceProbeResult()

    for host in targets:
        result = probe_device(host, tcp_timeout=tcp_timeout, http_timeout=http_timeout)
        if result.confidence > best.confidence:
            best = result
            log.info(
                "Probe %s: detected %s (%s) confidence=%.2f",
                host, result.detected_backend or "unknown",
                result.backend_family or "unknown", result.confidence,
            )
        else:
            log.debug("Probe %s: no match or lower confidence", host)

    return best


def _host_sort_key(host: str) -> tuple[int, ...] | tuple[str]:
    try:
        return tuple(int(part) for part in host.split("."))
    except Exception:
        return (host,)


def _scan_single_host(
    host: str,
    *,
    tcp_timeout: float,
    http_timeout: float,
) -> NetworkDiscoveryEntry | None:
    result = probe_device(host, tcp_timeout=tcp_timeout, http_timeout=http_timeout)

    if result.detected_backend:
        category = "supported_modem" if result.confidence >= 0.7 else "possible_modem"
        note = "classified from read-only HTTP probe"
        return NetworkDiscoveryEntry(
            host=host,
            category=category,
            detected_backend=result.detected_backend,
            backend_family=result.backend_family,
            confidence=result.confidence,
            open_ports=(80,),
            note=note,
        )

    if result.confidence > 0.0:
        return NetworkDiscoveryEntry(
            host=host,
            category="reachable_device",
            detected_backend=result.detected_backend,
            backend_family=result.backend_family,
            confidence=result.confidence,
            open_ports=(80,),
            note="reachable HTTP device; unknown Modem Live profile is probe-only",
        )

    ports = tuple(
        port for port in _GENERIC_DISCOVERY_PORTS
        if _tcp_reachable(host, port=port, timeout=tcp_timeout)
    )
    if not ports:
        return None
    return NetworkDiscoveryEntry(
        host=host,
        category="reachable_device",
        confidence=0.0,
        open_ports=ports,
        note="reachable device",
    )


def scan_local_network(
    *,
    seed_host: str = "",
    hosts: list[str] | None = None,
    tcp_timeout: float = 0.35,
    http_timeout: float = 0.75,
    max_workers: int = 48,
) -> NetworkScanResult:
    """Explicit read-only network scan for likely modems and other reachable devices."""
    mode, targets = ("custom_hosts", list(hosts or [])) if hosts is not None else build_network_scan_hosts(seed_host)
    if not targets:
        return NetworkScanResult(mode=mode, seed_host=seed_host, scanned_host_count=0, entries=())

    entries: list[NetworkDiscoveryEntry] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(
                _scan_single_host,
                host,
                tcp_timeout=tcp_timeout,
                http_timeout=http_timeout,
            )
            for host in targets
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                entry = future.result()
            except Exception:
                continue
            if entry is not None:
                entries.append(entry)

    order = {
        "supported_modem": 0,
        "possible_modem": 1,
        "reachable_device": 2,
    }
    entries.sort(
        key=lambda item: (
            order.get(item.category, 9),
            -float(item.confidence or 0.0),
            _host_sort_key(item.host),
        )
    )
    return NetworkScanResult(
        mode=mode,
        seed_host=str(seed_host or ""),
        scanned_host_count=len(targets),
        entries=tuple(entries),
    )
