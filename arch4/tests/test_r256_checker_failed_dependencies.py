"""A saved setting or calculated timestamp is not a failed modem request."""
from pathlib import Path
import pytest

from arch4.modem_live.profile_definition.registry import ProfileRegistry
from arch4.modem_live.startup_trace import StartupModemTrace, traced_protocol
from tools.log_checker.profile_fields import inspect_wire_fields

PROFILE = 'mikrotik_chateau_lte12_eg120k_routeros_v7'
STATIC = ('capability.traffic_volume', 'profile.traffic_presence',
          'traffic.traffic_accounting_source', 'traffic.traffic_rate_source',
          'traffic.traffic_volume_age_s', 'traffic.traffic_volume_last_sample_ts',
          'traffic.traffic_volume_next_sample_ts', 'traffic.traffic_volume_poll_interval_s')


@pytest.fixture(scope='module')
def profile():
    return ProfileRegistry.load(Path(__file__).resolve().parents[2] / 'modem_profiles').select(PROFILE)


@pytest.mark.parametrize('address', STATIC)
def test_without_a_failed_request_a_static_rule_cannot_be_a_native_failure(profile, address):
    rows = {row['field']: row for row in inspect_wire_fields(profile, [])['fields']}
    assert rows[address]['status'] == 'No saved conversion'


def test_actual_failed_request_only_marks_its_dependent_fields(profile):
    events = []
    def fail(*args, **kwargs):
        raise TimeoutError('SYNTHETIC_PRIVATE_DETAIL')
    with StartupModemTrace().scope(lambda row: events.append(row) or True, 'startup', 'failed-monitor'):
        with pytest.raises(TimeoutError):
            traced_protocol(fail, operation='/interface/lte:call', args=('monitor', {}))
    rows = {row['field']: row for row in inspect_wire_fields(profile, events)['fields']}
    assert rows['rf.sinr_db']['status'] == 'Native read failed'
    assert rows['traffic.session_uptime_s']['status'] == 'Native read failed'
    for address in STATIC:
        assert rows[address]['status'] == 'No saved conversion'


def test_field_warnings_and_their_clearing_are_separate_from_connection_recovery():
    from tools.log_checker.report import blank_report, inspect_events, SECTIONS
    report = blank_report(100.)
    events = [{'kind': 'profile_field_conversion', 'severity': 'WARNING', 'ts': 90.},
              {'kind': 'profile_field_recovery', 'severity': 'INFO', 'ts': 92.},
              {'kind': 'nr_service_recovery_state', 'severity': 'INFO', 'ts': 94.}]
    inspect_events(report, 'synthetic', events)
    counts = report['diagnostics']['event_counts']['synthetic']
    assert counts['field_read_warnings'] == 1
    assert counts['field_read_recoveries'] == 1
    assert counts['recovery'] == 1
    assert any('reading warning' in text for text in report['sections'][SECTIONS[1]])
    assert not any('2 recovery records' in text for text in report['sections'][SECTIONS[2]])
