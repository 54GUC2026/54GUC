"""Existing approved operator-serial boundary; no UI, persistence or identity DB."""
from arch4.modem.base import ModemIdentityRequiredError
from arch4.runtime_identity import is_placeholder_identity_text


def operator_serial_text(value):
    serial = str(value or '').strip()
    if (not serial or len(serial) > 128 or any(ord(c) < 32 for c in serial)
            or is_placeholder_identity_text(serial) or set(serial) == {'0'}):
        return ''
    return serial


def require_operator_serial(expected, prompt):
    serial = operator_serial_text(expected)
    if not serial and prompt is not None:
        serial = operator_serial_text(prompt())
    if not serial:
        raise ModemIdentityRequiredError('initial_physical_identity_unavailable')
    return serial
