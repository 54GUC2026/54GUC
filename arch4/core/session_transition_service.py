"""Blocked in-process session adoption. Product replaces the worker when the Modem Live
namespace changes; a running Core worker must not adopt another modem session.
"""

from __future__ import annotations

from typing import Any


OWNER = "54guc.product.worker_replacement"
_ILLEGAL_REASON = (
    "core_session_transition_service_retired_use_product_worker_replacement"
)


class CoreSessionTransitionService:
    """Removed mixed-worker session service. Every call fails clearly."""

    def __init__(self, core: Any) -> None:
        del core
        raise RuntimeError(_ILLEGAL_REASON)


__all__ = ("CoreSessionTransitionService", "OWNER")
