"""Modem Live package. Public imports are loaded lazily to avoid cycles."""

from __future__ import annotations

from importlib import import_module
from typing import Any


__all__ = [
    "ModemLiveModule",
    "ModemLivePublicationNotice",
    "create_model_live_pair",
    "create_modem_live_backend",
    "create_modem_live_module",
]


def __getattr__(name: str) -> Any:
    if name == "ModemLiveModule":
        return import_module("arch4.modem_live.acquisition").ModemLiveModule
    if name == "ModemLivePublicationNotice":
        return import_module("arch4.modem_live.publication").ModemLivePublicationNotice
    if name in {
        "create_model_live_pair",
        "create_modem_live_backend",
        "create_modem_live_module",
    }:
        return getattr(import_module("arch4.modem_live.bootstrap"), name)
    if name == "acquisition":
        return import_module("arch4.modem_live.acquisition")
    raise AttributeError(name)
