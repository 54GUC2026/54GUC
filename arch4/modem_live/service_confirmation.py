"""Bounded process-independent Internet confirmation, owned by Modem Live.

No router command is sent here. The local route to the selected modem must
match the route to the external address. Only an actual external TCP response
is positive evidence. A blocked port or local socket error stays inconclusive.
"""

from dataclasses import dataclass
from ipaddress import ip_address
import math
import socket

from arch4.architecture_contracts.runtime_generation import RuntimeGenerationNamespace


CONFIRMATION_MIN_INTERVAL_S = 15.0
CONFIRMATION_TIMEOUT_S = 1.0


@dataclass(frozen=True, slots=True)
class ServiceConfirmationReceipt:
    request_id: str
    namespace: RuntimeGenerationNamespace
    requested_at: float
    completed_at: float
    target: str
    response_received: bool
    reason: str

    def __post_init__(self):
        if not self.request_id or not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise ValueError("service_confirmation_identity_required")
        if not all(math.isfinite(v) and v > 0 for v in (self.requested_at, self.completed_at)) or self.completed_at < self.requested_at:
            raise ValueError("service_confirmation_time_invalid")
        if type(self.response_received) is not bool or not self.reason:
            raise ValueError("service_confirmation_result_invalid")

    def as_dict(self):
        return {"request_id": self.request_id, "namespace": self.namespace.as_dict(),
                "requested_at": self.requested_at, "completed_at": self.completed_at,
                "target": self.target, "response_received": self.response_received,
                "reason": self.reason, "source": "bound_external_tcp"}


def confirm_external_response(*, router_host: str, target: str) -> tuple[bool, str]:
    """Use a literal address and one local socket; do not spawn a process."""
    try:
        router = ip_address(router_host)
        remote = ip_address(target)
        if router.version != 4 or remote.version != 4 or not remote.is_global or router.is_loopback:
            return False, "external_confirmation_route_unavailable"
        # UDP connect selects a route locally. No datagram is sent to the router
        # or external target, and router access timing is therefore unchanged.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect((str(router), 9))
            local_ip = route.getsockname()[0]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect((str(remote), 53))
            if route.getsockname()[0] != local_ip:
                return False, "external_confirmation_route_mismatch"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(CONFIRMATION_TIMEOUT_S)
            probe.bind((local_ip, 0))
            # The configured public resolver is reached over TCP instead of
            # ICMP. No DNS name lookup, credentials or application data is sent.
            probe.connect((str(remote), 53))
        return True, "external_tcp_response_received"
    except (OSError, ValueError) as exc:
        return False, "external_confirmation_inconclusive:" + type(exc).__name__
