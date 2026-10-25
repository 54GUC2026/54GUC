"""Native conversion observer; pure definitions and consumer APIs stay unchanged."""
from copy import deepcopy
from itertools import count
import time

from arch4.modem.backend_diagnostic import ModemBackendDiagnostic
from .startup_trace import trace_profile_decode

_SEQUENCE = count(1)


def field_diagnostic(definition, issues, *, native=None, previous=None):
    """Add profile evidence without replacing the existing diagnostic contract.

    Reuse the backend's previous status for deduplication and one recovery
    fact. No additional history, cache, transport or consumer owner is needed.
    """
    previous = previous if isinstance(previous, dict) else {}
    data = previous.get('data')
    earlier = data.get('profile_fields') if isinstance(data, dict) else None
    if not isinstance(earlier, dict):
        earlier = previous if previous.get('schema') == 'PortableProfileFieldDiagnostic.v1' else None
    if earlier and (earlier.get('profile_id') != definition.profile_id or
            earlier.get('content_hash', earlier.get('definition_hash')) != definition.content_hash):
        earlier = None
    if not issues and not (earlier and earlier.get('issues')):
        return native
    packet = {'schema':'PortableProfileFieldDiagnostic.v1',
        'profile_id':definition.profile_id, 'content_hash':definition.content_hash,
        'message':('Some profile fields could not be converted; other readings continue.'
                   if issues else 'Profile field conversions recovered.'),
        # "address" is a private network endpoint key at the shared scrubber.
        # These are static canonical field names, so label them as fields.
        'issues':[{'field':value.address, 'pointer':value.pointer, 'reason':value.reason}
                  for _, value in sorted(issues.items())]}
    if isinstance(native, dict) and native.get('schema') == 'ModemBackendDiagnostic.v1':
        result = deepcopy(native)
        result['data']['profile_fields'] = packet
        return result
    if not native and previous.get('owner') == '54guc.modem_live.profile_definition' and earlier == packet:
        return deepcopy(previous)
    data = {'profile_fields':packet}
    if native:
        data['native_diagnostic'] = deepcopy(native)
    return ModemBackendDiagnostic(owner='54guc.modem_live.profile_definition',
        sequence=next(_SEQUENCE), observed_at=time.time(),
        kind='profile_field_conversion' if issues else 'profile_field_recovery',
        backend=definition.metadata['backend'] or definition.metadata['family'], severity='WARNING' if issues else 'INFO',
        data=data).to_payload()


def decode_native_profile(definition, replies, context=None, *, addresses=None, issues=None):
    # Preserve the decoder's exact error contract: issues=None still raises.
    first_issue = len(issues) if issues is not None else 0
    values = definition.decode(replies, context, addresses=addresses, issues=issues)
    trace_profile_decode(definition, replies, context, values,
                         issues[first_issue:] if issues is not None else ())
    return values
