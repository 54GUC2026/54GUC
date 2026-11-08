"""Result of one explicit settings inspection, never a periodic cache."""
from dataclasses import dataclass
from arch4.modem.backend_diagnostic import ModemBackendDiagnostic


class NetworkSettingsReadIncomplete(RuntimeError):
    """A bounded read has not yet obtained its usable native observation."""


@dataclass(frozen=True)
class NetworkSettingsRead:
    observation: dict
    # Only an IMSI physically returned by this inspection belongs here.
    imsi: str = ""
    diagnostic: ModemBackendDiagnostic | None = None
