from __future__ import annotations

from typing import Any

from arch4.tests.physical_request_test_support import (
    install_physical_request_test_authority,
)


def install_routeros_test_api(modem: Any, raw_api: Any) -> Any:
    """Install a deterministic zero-wall-time paced RouterOS test boundary."""

    install_physical_request_test_authority(
        modem,
        backend="mikrotik",
        backend_family="routeros_at_like",
    )
    modem._install_routeros_api(raw_api)
    return raw_api
