"""
arch4/modem/registry.py
═══════════════════════════════════════════════════════════════
Modem backend registration system.

Replaces the hard-coded if/elif chain in factory.py with a
dictionary-based registry. Adding a new modem backend requires
only:

  1. Create your modem class in arch4/modem/your_modem.py
     (implement arch4.modem.base.ModemInterface)
  2. Add a connection config view in arch4/config_views.py
     (follow the pattern of build_huawei_connection_config)
  3. Register it here:

        @register_backend("your_backend_name")
        def _build_your_modem(config: Arch4Config):
            cfg = build_your_connection_config(config)
            return YourModem(host=cfg.host, ...)

  4. Add an entry to arch4/modem/catalog.py so it appears in
     --help output and capability queries.

See ADDING_NEW_BACKEND.md for the full step-by-step guide.
═══════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
from typing import Callable, Dict

from arch4.interfaces import Arch4Config

log = logging.getLogger("arch4.modem.registry")

# Registry: backend_name → factory function(config) → ModemInterface
_REGISTRY: Dict[str, Callable[[Arch4Config], object]] = {}
# Aliases: allow multiple names to map to the same builder
_ALIASES: Dict[str, str] = {}


def register_backend(
    *names: str,
    alias_for: str | None = None,
) -> Callable:
    """Decorator to register a modem backend factory function.

    Usage:
        @register_backend("your_backend", "your_alias")
        def _build(config: Arch4Config):
            return YourModem(config)

    The first name is canonical; subsequent names are aliases.
    If alias_for is set, the decorated function is ignored and
    all names are registered as aliases for an existing backend.
    """
    def decorator(fn: Callable) -> Callable:
        canonical = names[0]
        if alias_for:
            for name in names:
                _ALIASES[name] = alias_for
        else:
            _REGISTRY[canonical] = fn
            for name in names[1:]:
                _ALIASES[name] = canonical
        return fn
    return decorator


def resolve_backend_name(backend: str) -> str:
    """Resolve an alias to its canonical backend name."""
    return _ALIASES.get(backend, backend)


def create_from_registry(config: Arch4Config) -> object | None:
    """Create a modem backend from the registry.

    Returns the modem instance if the backend is registered,
    or None if the backend is unknown (caller should raise).
    """
    backend = str(config.modem_backend or "").strip().lower()
    canonical = resolve_backend_name(backend)
    factory = _REGISTRY.get(canonical)
    if factory is None:
        return None
    log.debug("Creating modem backend %r via registry", canonical)
    return factory(config)


def registered_backends() -> list[str]:
    """Return all registered backend names (canonical + aliases)."""
    return sorted(list(_REGISTRY.keys()) + list(_ALIASES.keys()))


def is_registered(backend: str) -> bool:
    """True if the backend (or its alias) is registered."""
    canonical = resolve_backend_name(backend)
    return canonical in _REGISTRY
