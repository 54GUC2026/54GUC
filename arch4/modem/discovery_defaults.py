"""
Central registry for vendor/model discovery host defaults.

This keeps connection bootstrap and on-demand scan ordering aligned so the app
uses one explainable set of host preferences across supported families.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoveryHostProfile:
    """Ordered discovery defaults for one vendor or model lane."""
    key: str
    label: str
    hosts: tuple[str, ...]


DISCOVERY_HOST_PROFILES: tuple[DiscoveryHostProfile, ...] = (
    DiscoveryHostProfile(
        key="huawei_hilink",
        label="Huawei HiLink",
        hosts=("192.168.8.1", "192.168.1.1"),
    ),
    DiscoveryHostProfile(
        key="mikrotik_routeros",
        label="MikroTik RouterOS",
        hosts=("192.168.88.1",),
    ),
    DiscoveryHostProfile(
        key="zte_general",
        label="ZTE retail and carrier web UI",
        hosts=("192.168.0.1", "192.168.32.1", "192.168.1.1", "192.168.100.1", "192.168.254.1"),
    ),
    DiscoveryHostProfile(
        key="zyxel_cpe",
        label="Zyxel CPE",
        hosts=("192.168.1.1",),
    ),
    DiscoveryHostProfile(
        key="tplink_archer",
        label="TP-Link Archer LTE/5G",
        hosts=("192.168.1.1", "192.168.0.1"),
    ),
    DiscoveryHostProfile(
        key="tplink_deco_5g",
        label="TP-Link Deco 5G mesh",
        hosts=("192.168.68.1",),
    ),
)


def _ordered_unique(items: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


DEFAULT_DISCOVERY_HOSTS: tuple[str, ...] = _ordered_unique([
    host
    for profile in DISCOVERY_HOST_PROFILES
    for host in profile.hosts
])


BACKEND_DEFAULT_HOSTS: dict[str, str] = {
    "b818": "192.168.8.1",
    "b535": "192.168.8.1",
    "b530": "192.168.8.1",
    "h112": "192.168.8.1",
    "h122": "192.168.8.1",
    "huawei": "192.168.8.1",
    "mikrotik": "192.168.88.1",
    "mf296r": "192.168.1.1",
    "mc888": "192.168.32.1",
    "g5b": "192.168.32.1",
    "zte_zwrt": "192.168.0.1",
    "nr5101": "192.168.1.1",
    "fwa510": "192.168.1.1",
    "deco_x50_5g": "192.168.68.1",
    "openwrt": "192.168.8.1",
}


def default_host_for_backend(backend: str) -> str:
    """Return the preferred general-purpose host for a backend."""
    return BACKEND_DEFAULT_HOSTS.get(str(backend or "").strip().lower(), "")
