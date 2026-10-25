"""Single-owner publishers for independently timed runtime status facts."""

from __future__ import annotations

import time
from typing import Any, Mapping, Protocol

from arch4.core_contracts.current_health import (
    ModemLiveCurrentHealthPublication,
)


class RuntimeStatusRegisterBankPort(Protocol):
    """Structural writer boundary; the concrete bank remains sole owner."""

    def write_downstream_health(self, **kwargs: Any) -> Any: ...

    def write_modem_live_current_health(
        self,
        publication: ModemLiveCurrentHealthPublication,
    ) -> Any: ...


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def publish_downstream_health_status(
    bank: RuntimeStatusRegisterBankPort,
    status: Mapping[str, Any],
    *,
    now: float | None = None,
) -> Any | None:
    value = _mapping(status.get("downstream_health"))
    if not value:
        return None
    now_ts = float(time.time() if now is None else now)
    state = str(value.get("status") or value.get("stage") or "").strip().lower()
    stale = state in {"error", "unavailable"}
    return bank.write_downstream_health(
        value=dict(value),
        source="downstream_health",
        updated_at=float(value.get("ts") or now_ts),
        valid=True,
        stale=stale,
        dirty=False,
        trust="stale" if stale else "live",
        source_kind="live",
        validity="stale" if stale else "valid",
        transition=False,
    )


def publish_modem_live_current_health_status(
    bank: RuntimeStatusRegisterBankPort,
    *,
    publication: ModemLiveCurrentHealthPublication,
) -> Any:
    """Commit one exact Modem Live health push to its owned register."""

    if not isinstance(publication, ModemLiveCurrentHealthPublication):
        raise TypeError("modem_live_current_health_publication_required")
    return bank.write_modem_live_current_health(publication)


def publish_core_health_status(*args: Any, **kwargs: Any) -> Any:
    del args, kwargs
    raise RuntimeError(
        "core_pulled_health_publication_illegal:"
        "use_modem_live_typed_health_push"
    )


__all__ = (
    "publish_core_health_status",
    "publish_downstream_health_status",
    "publish_modem_live_current_health_status",
)
