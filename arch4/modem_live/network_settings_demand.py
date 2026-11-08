"""One Modem Live settings-read goal, scoped to the admitted router session."""
from dataclasses import dataclass, field
from uuid import uuid4

from arch4.architecture_contracts.runtime_generation import RuntimeGenerationNamespace


@dataclass(slots=True)
class NetworkSettingsDemand:
    namespace: RuntimeGenerationNamespace
    next_attempt_at: float = 0.0
    attempt_deadline: float = 0.0
    demand_id: str = field(default_factory=lambda: uuid4().hex)

    def begin_attempt(self, now: float) -> None:
        self.attempt_deadline = now + 60.0

    def retry_after(self, now: float, interval_s: float) -> None:
        if interval_s <= 0:
            raise ValueError("network_settings_retry_interval_invalid")
        self.attempt_deadline = 0.0
        self.next_attempt_at = now + interval_s
