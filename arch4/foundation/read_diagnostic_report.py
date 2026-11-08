"""Foundation's lossless bounded report boundary for shared router diagnostics.

Lifecycle controls stay small. Detailed evidence uses Foundation's existing
content codec and is expanded only by the public reader below. This is a report
adapter, never a modem translator, acquisition path or measurement store.
"""
from __future__ import annotations

import base64
import json
from collections.abc import Mapping

from arch4.canonical_json import canonical_json_value
from arch4.foundation.diagnostic_payload import encode_payload, decode_payload
from arch4.payload_budget import PayloadBudgetExceeded, retained_payload_bytes

SCHEMA = 'RouterReadDiagnostic.v2'
DETAIL_SCHEMA = 'RouterReadDiagnosticDetails.v1'
BODY_SCHEMA = 'RouterReadDiagnosticBody.zlib_sha256.v1'
MAX_DECODED_BYTES = 16 * 1024 * 1024
MAX_PROJECTION_BYTES = 384 * 1024
MAX_DETAIL_RETAINED_BYTES = 96 * 1024 * 1024
MAX_REPORT_ITEMS = 1_500_000


class ReadDiagnosticReportLimit(ValueError):
    """Capture must declare partial evidence before exceeding a report limit."""


def _encoded(controls, details):
    raw_bytes = len(json.dumps(details, ensure_ascii=True, separators=(',', ':'),
                               sort_keys=True).encode('utf-8'))
    if raw_bytes > MAX_DECODED_BYTES:
        raise ReadDiagnosticReportLimit('decoded detail exceeds 16 MiB')
    digest, compressed = encode_payload(details)
    data = dict(controls, schema=SCHEMA, capture=details['capture'], detailed_body={
        'schema': BODY_SCHEMA, 'digest': digest.hex(), 'decoded_bytes': raw_bytes,
        'compressed_base64': base64.b64encode(compressed).decode('ascii')})
    try:
        retained_payload_bytes(data, max_bytes=MAX_PROJECTION_BYTES,
                               label='router diagnostic encoded report')
    except PayloadBudgetExceeded as exc:
        raise ReadDiagnosticReportLimit('encoded report exceeds 384 KiB') from exc
    return data


def encode_read_diagnostic_data(*, controls, results, field_coverage, coverage_context, capture=None):
    """Encode once; explicit partial capture is the only over-limit disposition.

    Ordinary native reports are lossless. If unusually large detail exceeds the
    declared limits, coverage is reduced first, then whole raw detail sections.
    Every excluded section is recorded; probe outcome/timing remain available.
    The caller stops further probes after such a receipt.
    """
    details = canonical_json_value(dict(schema=DETAIL_SCHEMA, run_id=controls['run_id'],
        namespace=controls['namespace'], results=results, field_coverage=field_coverage,
        coverage_context=coverage_context, capture=capture if capture is not None else {'complete': True,
            'decoded_limit_bytes': MAX_DECODED_BYTES, 'projection_limit_bytes': MAX_PROJECTION_BYTES,
            'omitted_coverage': 0, 'omitted_sections': []}))
    if len(details['results']) > 64 or len(details['field_coverage']) > 18_496:
        raise ValueError('read_diagnostic_report_count_limit')
    try:
        retained_payload_bytes(details, max_bytes=MAX_DETAIL_RETAINED_BYTES,
            max_items=MAX_REPORT_ITEMS, label='router diagnostic capture')
        return _encoded(controls, details), details
    except (ReadDiagnosticReportLimit, PayloadBudgetExceeded):
        details['capture']['complete'] = False
    coverage = details['field_coverage']
    # Retain the largest prefix that fits, using logarithmic budget checks.
    low, high = 0, len(coverage)
    while low < high:
        middle = (low + high + 1) // 2
        details['field_coverage'] = coverage[:middle]
        details['capture']['omitted_coverage'] = len(coverage) - middle
        try:
            _encoded(controls, details)
        except ReadDiagnosticReportLimit:
            high = middle - 1
        else:
            low = middle
    details['field_coverage'] = coverage[:low]
    details['capture']['omitted_coverage'] = len(coverage) - low
    try:
        return _encoded(controls, details), details
    except ReadDiagnosticReportLimit:
        pass
    # Raw fields retain priority over derived coverage. The detailed outcome of
    # an over-limit check is explicitly partial, never converted to a zero/empty
    # modem reading or a successful complete capture.
    for index in range(len(details['results']) - 1, -1, -1):
        row = details['results'][index]
        for section in ('fields', 'evidence', 'advertised_methods', 'identity_presence'):
            if not row.get(section):
                continue
            value = row.pop(section)
            row['truncated'] = True
            details['capture']['omitted_sections'].append(dict(result_index=index,
                section=section, items=len(value) if isinstance(value, (list, dict)) else 1))
            try:
                return _encoded(controls, details), details
            except ReadDiagnosticReportLimit:
                continue
    # Lifecycle-only rows are intrinsically small for the admitted finite plan.
    # Invalid callers must fail rather than manufacture a completed report.
    raise ValueError('read_diagnostic_lifecycle_controls_exceed_report_limit')


def decode_read_diagnostic_data(data, *, expected_run_id=None, expected_namespace=None):
    """Public GUI/support/research reader. Raw v1 and corrupt data fail loudly."""
    if not isinstance(data, Mapping) or data.get('schema') != SCHEMA:
        raise ValueError('read_diagnostic_encoded_schema_required')
    if 'results' in data or 'field_coverage' in data or 'coverage_context' in data:
        raise ValueError('read_diagnostic_duplicate_raw_detail_forbidden')
    retained_payload_bytes(data, max_bytes=MAX_PROJECTION_BYTES,
                           label='router diagnostic encoded report')
    # PageTruth deeply freezes lists into tuples. Compare canonical JSON
    # shapes, not incidental Python container types at the GUI boundary.
    data = canonical_json_value(data)
    body = data.get('detailed_body')
    if (not isinstance(body, Mapping) or set(body) !=
            {'schema', 'digest', 'decoded_bytes', 'compressed_base64'} or
            body.get('schema') != BODY_SCHEMA):
        raise ValueError('read_diagnostic_encoded_body_required')
    digest, encoded, size = body['digest'], body['compressed_base64'], body['decoded_bytes']
    if (type(digest) is not str or len(digest) != 64 or type(encoded) is not str
            or type(size) is not int or not 0 < size <= MAX_DECODED_BYTES):
        raise ValueError('read_diagnostic_body_metadata_invalid')
    try:
        compressed = base64.b64decode(encoded, validate=True)
        digest_bytes = bytes.fromhex(digest)
    except ValueError as exc:
        raise ValueError('read_diagnostic_body_encoding_invalid') from exc
    decoded = decode_payload(digest_bytes, compressed, sequence=0, ts=0., max_bytes=MAX_DECODED_BYTES)
    decoded.pop('sequence'); decoded.pop('ts')
    actual_size = len(json.dumps(decoded, ensure_ascii=True, separators=(',', ':'),
                                sort_keys=True).encode('utf-8'))
    if actual_size != size:
        raise ValueError('read_diagnostic_decoded_length_mismatch')
    if (set(decoded) != {'schema', 'run_id', 'namespace', 'results', 'field_coverage',
                        'coverage_context', 'capture'} or decoded['schema'] != DETAIL_SCHEMA):
        raise ValueError('read_diagnostic_decoded_shape_invalid')
    if (decoded['run_id'] != data.get('run_id') or decoded['namespace'] != data.get('namespace')
            or decoded['capture'] != data.get('capture')):
        raise ValueError('read_diagnostic_context_mismatch')
    if ((expected_run_id is not None and decoded['run_id'] != expected_run_id) or
            (expected_namespace is not None and decoded['namespace'] != expected_namespace)):
        raise ValueError('read_diagnostic_expected_context_mismatch')
    if (not isinstance(decoded['results'], list) or len(decoded['results']) > 64
            or not isinstance(decoded['field_coverage'], list)
            or len(decoded['field_coverage']) > 18_496):
        raise ValueError('read_diagnostic_decoded_count_limit')
    retained_payload_bytes(decoded, max_bytes=MAX_DETAIL_RETAINED_BYTES,
        max_items=MAX_REPORT_ITEMS, label='router diagnostic decoded capture')
    return {key: canonical_json_value(value) for key, value in data.items() if key != 'detailed_body'} | {
        'results': decoded['results'], 'field_coverage': decoded['field_coverage'],
        'coverage_context': decoded['coverage_context']}


def project_read_diagnostic_data(data, project):
    """Permanent export adapter using the same decoder and lossless encoder.

    Privacy policy belongs to the supplied canonical traversal. Capture health
    is preserved; an export cannot turn an incomplete report into full success.
    Removing the live schema during traversal prevents a second encoded pass.
    """
    decoded = decode_read_diagnostic_data(data)
    clean = project({key: value for key, value in decoded.items() if key != 'schema'})
    details = {'schema': DETAIL_SCHEMA}
    for key in ('run_id', 'namespace', 'results', 'field_coverage', 'coverage_context', 'capture'):
        details[key] = clean[key]
    controls = {key: value for key, value in clean.items()
                if key not in {'results', 'field_coverage', 'coverage_context', 'capture'}}
    return _encoded(controls, details)


ROUTER_CHECK_FILENAME = 'router_check.txt'
TERMINAL_READ_STATES = frozenset({'completed', 'cancelled', 'timed_out', 'interrupted', 'limited'})


def _monitoring_report_lines(overview):
    """Explain collected evidence without claiming a control was exercised."""
    from datetime import datetime
    lines = ['', 'CURRENT MONITORING INFORMATION',
             ('Profile: ' + overview['profile_id'] if overview['profile_id'] else
              'Backend-only connection; no selectable portable profile.'),
             'These are recorded monitoring states, not additional router reads.']
    for row in overview['fields']:
        if row['field'].startswith('capability.action_') and row['field'].endswith('_allowed'):
            continue  # shown with the corresponding control's evidence state
        proof = row.get('proof_state')
        if proof is not None:
            status = {
                'supported': 'Declared available', 'estimated': 'Estimated capability',
                'unsupported': 'Not provided by this profile',
                'non_useful': 'Not useful for this profile',
                'unknown': 'Unknown or not currently available',
            }[proof]
        elif row['retained']:
            status = 'Earlier reading retained'
        else:
            status = {'available': 'Information available',
                      'unsupported': 'Not provided by this profile',
                      'missing': 'Unknown or not currently available',
                      'invalid': 'Reading could not be validated'}[row['state']]
        when = ('; acquired ' + datetime.fromtimestamp(row['acquired_at']).astimezone().isoformat(timespec='seconds')
                if row['acquired_at'] > 0 else '')
        if 'control_allowed' in row:
            status += '; ' + {True: 'Control currently available in 54GUC',
                              False: 'Control currently unavailable in 54GUC',
                              None: 'Current control availability unknown'}[row['control_allowed']]
        lines.append(f"- {row['field']}: {status}{when}.")
    lines.append('No control command was tested. Declared availability is not proof that a command succeeded.')
    return lines


def router_check_text(envelope):
    """Readable projection of Scout results; never performs or admits router reads."""
    import json
    from datetime import datetime
    from arch4.privacy_scrub import scrub_record
    data = decode_read_diagnostic_data(scrub_record(envelope['data']))
    if data['state'] not in TERMINAL_READ_STATES:
        raise ValueError('router_check_terminal_report_required')
    state = data['state']
    meaning = {
        'completed': 'The planned checks finished.',
        'cancelled': 'You cancelled the check. Only completed checks are listed.',
        'timed_out': 'The check ran out of time. Some checks were not performed.',
        'interrupted': 'The connection changed before all checks finished.',
        'limited': 'The report reached its size limit. Some details were not saved.',
    }[state]
    overview = data['coverage_context'].get('monitoring_overview')
    monitoring_only = data['total'] == 0 and overview is not None
    lines = ['54GUC - Router check',
             'Checked: ' + datetime.fromtimestamp(envelope['observed_at']).astimezone().isoformat(timespec='seconds'),
             '', 'WHAT WAS CHECKED',
             ('Current monitoring information was reviewed. No additional native probes are implemented for this profile.'
              if monitoring_only else f"{data['completed']} of {data['total']} planned checks completed."),
             'No router settings were changed.',
             '', 'WHAT WAS FOUND', meaning]
    outcomes = {
        'success': 'Information received', 'access_denied': 'Router did not allow this check',
        'timeout': 'Router did not reply in time', 'method_not_found': 'This request is not available',
        'service_not_found': 'This service is not available', 'session_not_found': 'Router login was no longer valid',
        'adapter_error': 'The response could not be processed', 'protocol_error': 'The reply was not understood',
        'request_failed': 'The request failed',
    }
    # Feature-level wording belongs only to this report boundary. Scout keeps
    # every firmware-specific candidate, raw refusal and timing below.
    names = {
        '/api/device/information': 'Device and firmware information',
        '/api/device/basic_information': 'Basic device information',
        '/api/device/basicinformation': 'Basic device information',
        '/api/device/antenna_status': 'Antenna information',
        '/api/device/antenna_type': 'Antenna information',
        '/api/device/signal': 'Radio signal information',
        '/api/monitoring/status': 'Mobile connection',
        '/api/monitoring/traffic-statistics': 'Data rates and usage',
        '/api/monitoring/month_statistics': 'Monthly data usage',
        '/api/monitoring/start_date': 'Billing cycle',
        '/api/device/boot_time': 'Time since the router started',
        '/api/net/current-plmn': 'Mobile provider',
        '/api/net/net-mode': 'Current network mode and allowed bands',
        '/api/net/net-mode-list': 'Supported network modes and bands',
        '/api/net/register': 'Network selection settings',
        '/api/dialup/connection': 'Mobile data and roaming settings',
        '/api/dialup/profiles': 'APN profiles',
        '/api/device/seccellinfo': 'Additional carriers',
        '/api/device/nbrcellinfo': 'Extra nearby-cell details',
        '/api/net/lock-freq': 'Frequency restrictions',
    }
    grouped = {}
    for row in data['results']:
        name = ('Device and SIM still match after checking'
                if row['probe'] == 'Final device and SIM continuity check'
                else names.get(row.get('interface'), row['probe']))
        grouped.setdefault(name, []).append(row)
    for name, rows in grouped.items():
        successful = any(row['outcome'] == 'success' for row in rows)
        outcome = 'success' if successful else rows[0]['outcome']
        lines.append(f"- {name}: {outcomes.get(outcome, 'Information could not be read')}.")
        if name == 'Radio signal information' and any(
            row['outcome'] == 'success'
            and row.get('fields', {}).get('nei_cellid', {}).get('state') == 'returned'
            and row.get('fields', {}).get('nei_cellid', {}).get('value')
            for row in rows
        ):
            lines.append('- Nearby cell identifiers: Information received.')
    if any(len(rows)>1 and any(row['outcome']!='success' for row in rows)
           for rows in grouped.values()):
        lines.append('Some alternative requests could not be read. Their replies are retained in the technical details.')
    if overview is not None:
        lines.extend(_monitoring_report_lines(overview))
    completeness_checked = bool(data['field_coverage']) and all(
        'coverage_state' in row for row in data['field_coverage'])
    unavailable = sum(1 for r in data['field_coverage'] if r.get('coverage_state') in {'empty','not_returned','not_tested'})
    review = sum(1 for r in data['field_coverage'] if r.get('coverage_state') in {'returned_unqualified','returned_unmapped','mapped_not_admitted'})
    lines += ['', 'WHAT COULD NOT BE READ',
              f"{data['completed'] - data['outcomes'].get('success', 0)} completed checks did not return the requested information.",
              f"{data['total'] - data['completed']} checks were not completed.",
              (f'{unavailable} checked fields were empty, not returned, or not tested.'
               if completeness_checked else 'Missing fields were not counted; this check had no field completeness checklist.'),
              '', 'WHAT THIS MEANS',
              'A successful read confirms that the router answered that request. It does not prove every feature works.',
              'Missing information does not necessarily mean the router lacks that feature.',
              (f'{review} returned fields need further review before normal monitoring can use them.'
               if completeness_checked else 'The returned information was not compared with normal monitoring requirements.')]
    if not data['capture']['complete']:
        lines.append('Some details could not be saved. The omissions are recorded below.')
    lines += ['', 'TECHNICAL DETAILS FOR SUPPORT', json.dumps(data, ensure_ascii=False, indent=2), '']
    return '\n'.join(lines)


def save_router_check_report(runtime_dir, envelope):
    """Foundation owns the single latest readable Scout artifact."""
    from pathlib import Path
    from arch4.atomic_io import atomic_write_text
    path = Path(runtime_dir) / ROUTER_CHECK_FILENAME
    atomic_write_text(path, router_check_text(envelope))
    return path
