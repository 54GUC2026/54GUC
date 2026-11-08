"""Finite alternate stock reads; shared lifecycle, projection and I/O owner."""
from dataclasses import dataclass

import requests

from arch4.modem.read_diagnostic_projection import project_fields
from arch4.modem.read_diagnostics import DiagnosticDeviceContinuity
from arch4.modem.xiaomi_miwifi_protocol import (
    ASSUMPTION_ID, MiWiFiProtocolError, MiWiFiRejected, MiWiFiSessionExpired,
)


@dataclass(frozen=True)
class Probe:
    label: str
    endpoint: str


PROBES = (
    Probe('Firmware and login mode', 'xqsystem/init_info'),
    Probe('Combined radio interface', 'xqdtcustom/cpe_detect'),
    Probe('Alternate simple radio interface', 'xqdtcustom/simple_cpe_detect'),
    Probe('Mobile radio interface', 'xqmobile/get_mobile_net_info'),
    Probe('Alternate mobile radio interface', 'xqdtcustom/get_mobile_net_info'),
    Probe('Router status', 'xqdtcustom/newstatus'),
    Probe('System status', 'misystem/status'),
    Probe('System metadata', 'misystem'),
    Probe('WAN configuration', 'xqnetwork/wan_info'),
    Probe('Wi-Fi status', 'xqnetwork/wifi_detail_all'),
    Probe('SIM metadata', 'xqdtcustom/get_sim_info'),
    Probe('SIM presence and readiness', 'xqmobile/check_sim_status'),
    Probe('Mobile data and roaming configuration', 'xqmobile/get_mobile_net_cfg'),
    Probe('APN profiles and active profile', 'xqmobile/get_apn_info'),
    Probe('Supported and selected LTE/NR bands', 'xqmobile/get_cellular_band_list'),
    Probe('Network selection configuration', 'xqmobile/get_search_net_mode'),
    Probe('Native traffic volume', 'xqmobile/get_data_usage_info'),
    Probe('Native data-plan configuration', 'xqmobile/get_data_plan_info'),
)


class XiaomiReadDiagnosticAdapter:
    backend = 'xiaomi_cb0401v2'
    probes = PROBES

    def __init__(self, modem):
        self.modem = modem
        self.observing = True
        self._session = modem._miwifi.token
        self._sim = modem._sim_identity
        self._device = DiagnosticDeviceContinuity(modem.get_modem_info())
        self._changed = False

    def context_current(self):
        return bool(self.modem.is_connected() and self.modem._miwifi.token == self._session
                    and self.modem._sim_identity == self._sim and not self._changed
                    and self._device.matches_snapshot(self.modem.get_modem_info()))

    def read(self, probe):
        if probe not in PROBES:
            raise ValueError('xiaomi_diagnostic_probe_not_registered')
        row = {'probe': probe.label, 'interface': probe.endpoint,
               'assumption_id': ASSUMPTION_ID, 'qualification': 'conditional; native result retained'}
        try:
            result = self.modem._miwifi.read(probe.endpoint, lane='slow_live')
        except MiWiFiSessionExpired:
            return {**row, 'outcome': 'session_not_found'}
        except MiWiFiRejected as exc:
            return {**row, 'outcome': 'access_denied' if exc.code == 13 else 'request_failed', 'native_code': exc.code}
        except requests.Timeout:
            return {**row, 'outcome': 'timeout'}
        except requests.RequestException:
            return {**row, 'outcome': 'transport_error'}
        except MiWiFiProtocolError:
            return {**row, 'outcome': 'malformed_reply'}
        if probe.endpoint == 'xqdtcustom/get_sim_info':
            current = self.modem.sim_identity(result)
            if self._sim and current and current != self._sim:
                self._changed = True
                return {**row, 'outcome': 'context_changed', **project_fields(result)}
        if probe.endpoint == 'xqsystem/init_info':
            row['authentication_observation'] = dict(self.modem._miwifi.authentication_observation)
        identity_paths = ('id',) if probe.endpoint == 'xqsystem/init_info' else ()
        evidence = project_fields(result, identity_paths=identity_paths)
        if probe.endpoint in {'xqsystem/init_info', 'xqdtcustom/newstatus'}:
            from arch4.modem.xiaomi_miwifi import stock_model, unique_text, device_identity_text
            try:
                if probe.endpoint == 'xqsystem/init_info':
                    device = dict(model=stock_model(result), firmware=unique_text(result, ('romversion', 'firmware')),
                                  serial=device_identity_text(result, ('id', 'sn', 'serial')),
                                  imei=device_identity_text(result, ('imei', 'IMEI')))
                else:
                    hardware = result.get('hardware', {})
                    if not isinstance(hardware, dict):
                        raise MiWiFiProtocolError('xiaomi_native_hardware_shape_invalid')
                    device = dict(serial=device_identity_text(hardware, ('sn', 'serial')),
                                  imei=device_identity_text(hardware, ('imei', 'IMEI')))
                device = self._device.canonical_values(device)
            except (MiWiFiProtocolError, ValueError):
                return {**row, 'outcome': 'malformed_reply', **evidence}
            if not self._device.observe(device):
                self._changed = True
                return {**row, 'outcome': 'context_changed', **evidence}
        return {**row, 'outcome': 'success' if len(result) > 1 else 'empty_reply', **evidence}

    def summarize(self, rows):
        return [{'field': row['probe'], 'result': 'Returned; interpretation needs native qualification' if row['outcome'] == 'success' else 'Not returned; see the individual check below',
                 'interface': row['interface'],
                 'candidate_fields': sorted(row.get('fields', {})),
                 'note': 'Diagnostic candidates do not replace normal measurements.'} for row in rows]
