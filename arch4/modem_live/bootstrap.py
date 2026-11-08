"""Create backend drivers and wrap them in ``ModemLiveModule``.

Runtime code should use these helpers instead of importing backend factories.
This keeps backend creation inside Modem Live.
"""

from __future__ import annotations

from typing import Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from arch4.modem_live.compatibility_identity import DecodeCatalogSnapshot

from arch4.interfaces import Arch4Config
from arch4.modem.base import ModemInterface


def create_modem_live_backend(config: Arch4Config) -> ModemInterface:
    """Create a backend driver for Modem Live."""
    from arch4.modem.factory import create_modem_backend

    return create_modem_backend(config)


def create_modem_live_module(
    config: Arch4Config,
    *,
    modem: Optional[ModemInterface] = None,
    health_checks: Optional[object] = None,
    ops_root_dir: str | None = None,
    restored_identity_catalog: DecodeCatalogSnapshot | None = None,
    device_association_reader=None,
):
    """Wrap a backend driver in the main Modem Live module."""
    from arch4.modem_live.acquisition import ModemLiveModule
    if device_association_reader is None and ops_root_dir is not None:
        from arch4.product_intelligence_runtime import read_confirmed_device_associations
        device_association_reader=lambda: read_confirmed_device_associations(ops_root_dir)

    backend = modem if modem is not None else create_modem_live_backend(config)
    return ModemLiveModule(
        config=config,
        modem=backend,
        health_checks=health_checks,
        ops_root_dir=ops_root_dir,
        restored_identity_catalog=restored_identity_catalog,
        device_association_reader=device_association_reader,
    )


def create_model_live_pair(
    profile_name: str = "",
    *,
    profile_path: str = "",
    config: Optional[Arch4Config] = None,
) -> Tuple[ModemInterface, object]:
    """Create a model backend and Modem Live wrapper for offline tests."""
    from arch4.modem.model_backend import ModemModelBackend

    cfg = config or Arch4Config(modem_backend="model")
    cfg.modem_backend = "model"
    if profile_name:
        cfg.model_profile_name = str(profile_name)
    if profile_path:
        cfg.model_profile_path = str(profile_path)
    modem = ModemModelBackend(
        profile_name=str(profile_name or getattr(cfg, "model_profile_name", "") or ""),
        profile_path=str(profile_path or getattr(cfg, "model_profile_path", "") or ""),
    )
    return modem, create_modem_live_module(cfg, modem=modem)


__all__ = [
    "create_modem_live_backend",
    "create_modem_live_module",
    "create_model_live_pair",
]
