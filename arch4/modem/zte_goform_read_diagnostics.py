"""Finite GoForm reads through the existing paced transport; no writes/repair."""
from dataclasses import dataclass
import json

import requests

from arch4.modem.read_diagnostic_projection import project_fields
from arch4.modem.read_diagnostics import DiagnosticDeviceContinuity


@dataclass(frozen=True)
class Probe:
    label: str
    fields: tuple[str, ...]
    include_session: bool = True


PROBES = (
    Probe('Management login state', ()),
    Probe('Device and firmware identity', ('model_name', 'product_name', 'serial_number', 'imei', 'hardware_version', 'wa_inner_version', 'web_version', 'cr_version')),
    Probe('SIM identity and readiness', ('sim_imsi', 'imsi', 'iccid', 'sim_status')),
    Probe('LTE radio and serving cell', ('network_type', 'lte_band', 'lte_rsrp', 'lte_rsrq', 'lte_rssi', 'lte_snr', 'lte_pci', 'lte_earfcn', 'cell_id', 'tac')),
    Probe('Alternate LTE signal and receive chains', ('rssi', 'rscp', 'sinr', 'ecio', 'Z_PCI', 'Z_dl_earfcn', 'earfcn', 'lte_rsrp_1', 'lte_rsrp_2', 'lte_rsrp_3', 'lte_rsrp_4', 'lte_snr_1', 'lte_snr_2', 'lte_snr_3', 'lte_snr_4')),
    Probe('NR radio Z field dialect', ('Z5g_rsrp', 'Z5g_rsrq', 'Z5g_SINR', 'Z5g_snr', 'Z5g_PCI', 'network_Z5g_PCI', 'Z5g_CELL_ID', 'Z5g_CELLINFO_band', 'network_Z5g_CELLINFO_band', 'Z5g_dlEarfcn', '5g_rx0_rsrp', '5g_rx1_rsrp')),
    Probe('Alternate NR serving fields', ('nr5g_cell_id', 'nr5g_pci', 'nr5g_action_channel', 'nr5g_action_band', 'nr5g_action_nsa_band', 'nr5g_nsa_bandwidth')),
    Probe('LTE active carriers', ('wan_lte_ca', 'lte_ca_pcell_band', 'lte_ca_scell_band', 'lte_ca_pcell_bandwidth', 'lte_ca_scell_bandwidth', 'lte_ca_pcell_arfcn', 'lte_ca_scell_arfcn', 'lte_ca_scell_info', 'lte_multi_ca_scell_info', 'lte_multi_ca_scell_sig_info')),
    Probe('NR active carriers', ('nr_ca_pcell_band', 'nr_ca_pcell_freq', 'nr_multi_ca_scell_info')),
    Probe('Native temperature candidates', ('pm_sensor_ambient', 'pm_sensor_mdm', 'pm_sensor_5g', 'pm_sensor_pa1', 'pm_modem_5g', 'wifi_chip_temp')),
    Probe('Native traffic rates', ('realtime_rx_thrpt', 'realtime_tx_thrpt', 'realtime_time')),
    Probe('Native traffic counters', ('wan_rx_bytes', 'wan_tx_bytes', 'rx_bytes', 'tx_bytes', 'realtime_rx_bytes', 'realtime_tx_bytes', 'monthly_rx_bytes', 'monthly_tx_bytes', 'monthly_time')),
    Probe('Mobile link and provider', ('wan_connected', 'wan_ipaddr', 'wan_iface_name', 'ppp_status', 'mccmnc', 'rmcc', 'rmnc', 'network_provider', 'network_provider_fullname')),
    Probe('Native LTE and NR restrictions', ('lte_band_lock', 'lte_freq_lock', 'lte_pci_lock', 'lte_earfcn_lock', 'ZCELLINFO_band', 'nr5g_sa_band_lock', 'nr5g_nsa_band_lock')),
    Probe('Native APN and network configuration', ('net_select_mode', 'wan_apn', 'ppp_username', 'ppp_auth_mode', 'pdp_type', 'profile_name', 'Current_index')),
    Probe('Mobile connection configuration', ('ConnectionMode',)),
    # Permanent, explicit independent observations. Some stock GoForm builds
    # blank an entire batch when one candidate name is unknown. These queries
    # run once as part of the finite tool, never as normal-reader fallbacks.
    *(Probe(f'Individual native field: {field}', (field,), include_session=False)
      for field in ('wa_inner_version', 'model_name', 'hardware_version',
                    'serial_number', 'imei', 'sim_imsi', 'iccid', 'lte_rsrp',
                    'lte_snr', 'Z5g_rsrp', 'nr5g_nsa_bandwidth', 'pm_sensor_mdm',
                    'realtime_rx_thrpt', 'wan_ipaddr', 'wan_apn', 'ConnectionMode')),
)

ASSUMPTION_ID = 'GoForm_batch_and_single_field_diagnostic_v2'
MAX_REPLY_BYTES = 1024 * 1024


def _cookies(session):
    # Comparison only, never emitted to a report or trace.
    return tuple(sorted((cookie.domain, cookie.path, cookie.name, cookie.value)
                        for cookie in session.cookies))


class GoformReadDiagnosticAdapter:
    probes = PROBES

    def __init__(self, modem):
        self.modem = modem
        self.backend = modem._backend_name
        self.observing = True
        self._session = modem._session
        self._cookies = _cookies(self._session)
        self._sim = self._sim_values(modem._last_raw)
        self._changed = len(self._sim) > 1
        self._device = DiagnosticDeviceContinuity(self._device_values(modem._last_raw))

    @staticmethod
    def _device_values(data):
        # GoForm firmware and configuration/WebUI versions are distinct fields.
        # Preserve its normal model-name precedence; product_name is descriptive.
        return DiagnosticDeviceContinuity.native_values(data, {
            'model': ('model_name',) if data.get('model_name') else ('product_name',),
            'hardware_version': ('hardware_version',), 'firmware': ('wa_inner_version',),
            'webui': ('web_version',), 'config_version': ('cr_version',),
            'serial': ('serial_number',), 'imei': ('imei',)})

    def _sim_values(self, data):
        return frozenset(value for key in ('imsi', 'sim_imsi')
                         if (value := self.modem._normalize_imsi(data.get(key))))

    def context_current(self):
        current = self._sim_values(self.modem._last_raw)
        try:
            device_current = self._device.matches_snapshot(self._device_values(self.modem._last_raw))
        except ValueError:
            device_current = False
        return bool(self.modem._connected and self.modem._authenticated
                    and self.modem._session is self._session
                    and _cookies(self.modem._session) == self._cookies
                    and not self._changed and device_current and len(current) <= 1
                    and not (self._sim and current and current != self._sim))

    def read(self, probe):
        if probe not in PROBES:
            raise ValueError('goform_diagnostic_probe_not_registered')
        requested = (('loginfo',) if probe.include_session else ()) + probe.fields
        row = {'probe': probe.label, 'interface': '/goform/goform_get_cmd_process',
               'requested_fields': list(requested), 'assumption_id': ASSUMPTION_ID,
               'query_mode': 'batch_with_session' if probe.include_session else 'independent_single_field',
               'qualification': 'Native field candidates; no measurement or control admission'}
        try:
            response = self.modem._zte_http_get(
                self.modem._base_url + row['interface'], operation='read_diagnostic',
                lane='slow_live', timeout=self.modem._maintenance_read_timeout,
                params={'isTest': 'false', 'cmd': ','.join(requested), 'multi_data': '1'},
                headers=self.modem._goform_headers(), allow_redirects=False,
            )
        except requests.Timeout:
            return {**row, 'outcome': 'timeout'}
        except requests.RequestException:
            return {**row, 'outcome': 'transport_error'}
        row['http_status'] = response.status_code
        if response.status_code != 200:
            if response.status_code == 401:
                self.modem._authenticated_session_explicitly_lost({'loginfo': ''})
            return {**row, 'outcome': 'access_denied' if response.status_code == 403
                    else 'session_not_found' if response.status_code == 401
                    else 'method_not_found' if response.status_code == 404 else 'request_failed'}
        if len(response.content) > MAX_REPLY_BYTES:
            return {**row, 'outcome': 'malformed_reply', 'reason': 'reply_size_limit'}
        try:
            data = json.loads(response.content)
        except (ValueError, UnicodeError):
            return {**row, 'outcome': 'malformed_reply'}
        if not isinstance(data, dict):
            return {**row, 'outcome': 'malformed_reply'}
        # Do not call is_connected(): it would perform a second HTTP read. Only
        # the same reply's explicit login evidence can invalidate the session.
        if 'loginfo' in data:
            if self.modem._authenticated_session_explicitly_lost(data):
                return {**row, 'outcome': 'session_not_found'}
            row['session_observation'] = 'authenticated'
        else:
            row['session_observation'] = 'not returned'
        current = self._sim_values(data)
        if len(current) > 1 or (self._sim and current and current != self._sim):
            self._changed = True
            return {**row, 'outcome': 'context_changed', **project_fields(data)}
        if data.get('error') not in (None, '', 0, '0'):
            return {**row, 'outcome': 'request_failed', **project_fields(data)}
        if current and not self._sim:
            self._sim = current
        try: current_device = self._device_values(data)
        except ValueError: return {**row, 'outcome': 'malformed_reply', **project_fields(data)}
        if not self._device.observe(current_device):
            self._changed = True
            return {**row, 'outcome': 'context_changed', **project_fields(data)}
        row['missing_fields'] = [key for key in requested if key not in data]
        row['empty_fields'] = [key for key in requested if key in data and data[key] in (None, '', [], {})]
        useful = probe.fields or ('loginfo',)
        state = 'success' if any(data.get(key) not in (None, '', [], {}) for key in useful) else 'empty_reply'
        return {**row, 'outcome': state, **project_fields(data)}

    def summarize(self, rows):
        return [{'field': row['probe'], 'result': 'Native candidates returned; interpretation remains separate'
                 if row['outcome'] == 'success' else 'Not returned; see the individual check below',
                 'candidate_fields': sorted(row.get('fields', {})),
                 'note': 'Missing fields do not prove a missing feature.'} for row in rows]
