"""One selected-device admission; permanent hardware and profile stay separate."""
from collections.abc import Mapping, Iterable

from arch4.customer_contracts.handoff_identity import (
    confirmed_handoff_identity_matches, handoff_identity_token,
)
from arch4.modem.base import TargetGuardError, evaluate_target_guard
from arch4.modem.identity import normalize_modem_identity_fields
from arch4.modem_live.profile_catalog import (
    modem_profile_hardware_qualified, modem_profile_record, modem_profile_selection,
)
from arch4.modem_live.runtime_identity_projection import build_resolved_identity_card_section
from arch4.runtime_identity import is_placeholder_identity_text


def require_selected_device_admission(*, backend: str, selected_profile_id: str,
                                     observed: Mapping, associations: Iterable[Mapping],
                                     expected_serial: str = '', expected_imei: str = '',
                                     discovery: bool = False) -> None:
    """Validate discovery/full native facts before any connected publication.

    Stored associations only constrain hardware ownership. They never supply
    current firmware, manufacture a native profile or authorize a new command.
    The non-wire model backend is the existing explicit synthetic boundary.
    """
    if backend == 'model':
        return
    selected = modem_profile_selection(selected_profile_id)
    if selected is None or selected.backend != backend:
        raise TargetGuardError('Select the matching router model before connecting.',
                               reason='selected_device_profile_required')
    native = normalize_modem_identity_fields({**dict(observed), 'backend': backend})
    for key in ('serial', 'imei'):
        value = native.get(key)
        if is_placeholder_identity_text(value) or set(str(value).strip()) == {'0'}:
            native[key] = ''
    if not any(handoff_identity_token(native.get(k)) for k in ('serial', 'imei')):
        raise TargetGuardError('The router serial number or IMEI could not be read. '
                               'The connection cannot be confirmed yet.',
                               reason='selected_device_permanent_identity_required')
    # Management discovery cannot read a disabled LTE module's IMEI. Check
    # present identifiers now; the complete receipt must satisfy both guards.
    guard_ok, reason = evaluate_target_guard(observed=native,
        expected_serial=expected_serial if not discovery or native.get('serial') else '',
        expected_imei=expected_imei if not discovery or native.get('imei') else '')
    if not guard_ok:
        axis = 'serial number' if 'serial' in reason else 'IMEI'
        detail = 'could not be read' if 'missing' in reason else 'does not match the saved login'
        raise TargetGuardError(f'The router {axis} {detail}. Check the selected device; '
                               'its History has been preserved.', reason=reason)
    matches=[]
    for association in associations:
        saved=association['physical_identity']
        overlaps=[k for k in ('serial','imei') if handoff_identity_token(saved.get(k))
                  and handoff_identity_token(native.get(k))]
        matching=[k for k in overlaps if handoff_identity_token(saved[k]) == handoff_identity_token(native.get(k))]
        if not matching:
            continue
        if not confirmed_handoff_identity_matches(saved,native):
            raise TargetGuardError('The router serial number and IMEI conflict with its '
                                   'saved device record. Check the selected router; '
                                   'its History has been preserved.', reason='known_device_identifiers_conflict')
        matches.append(association)
    for association in matches:
        saved=association['physical_identity']; model=str(saved.get('model') or '')
        known_id=str(association['profile_id']); known=modem_profile_selection(known_id)
        known_name=known.label if known is not None else (model or known_id)
        if model and not modem_profile_hardware_qualified(selected_profile_id,model=model):
            raise TargetGuardError(f'This serial number or IMEI belongs to {known_name}. '
                                   f'You selected {selected.label}. Select the matching model and connect again.',
                                   reason='known_device_selected_profile_mismatch')
        if model and not modem_profile_record(selected_profile_id).get('model_contains'):
            if handoff_identity_token(model)!=handoff_identity_token(native.get('model')):
                raise TargetGuardError(f'The router reports a different model from its saved '
                                       f'{known_name} device record. History has been preserved.',
                                       reason='known_device_native_model_conflict')
    card=build_resolved_identity_card_section(native)
    if card['profile_id'] != selected_profile_id:
        actual_id=str(card['profile_id'] or '')
        actual=modem_profile_selection(actual_id) if actual_id else None
        actual_name=actual.label if actual else str(native.get('model') or 'an unrecognised router')
        raise TargetGuardError(f'The router reports {actual_name}; you selected {selected.label}. '
                               'Check the model and firmware selection, then connect again.',
                               reason='selected_device_native_profile_mismatch')
    record=modem_profile_record(selected_profile_id)
    hardware=str(record.get('hardware_exact') or '')
    if hardware and str(native.get('hardware_version') or '')!=hardware:
        raise TargetGuardError(f'The router hardware version does not match {selected.label}. '
                               'Check your model selection.', reason='selected_device_hardware_mismatch')

__all__=('require_selected_device_admission',)
