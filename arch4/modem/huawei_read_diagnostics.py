"""Finite HiLink reads; native candidates do not expand measurement authority."""
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import requests

from arch4.modem.read_diagnostic_projection import project_fields
from arch4.modem.read_diagnostics import DiagnosticDeviceContinuity
from arch4.core_contracts.network_settings import subscription_key


@dataclass(frozen=True)
class Probe:
    label: str
    path: str


PROBES = (
    Probe('Device and firmware identity', '/api/device/information'),
    Probe('Alternate device metadata', '/api/device/basic_information'),
    Probe('Alternate basic information spelling', '/api/device/basicinformation'),
    Probe('Native LTE and NR signal candidates', '/api/device/signal'),
    Probe('Mobile connection status', '/api/monitoring/status'),
    Probe('Traffic rates and cumulative counters', '/api/monitoring/traffic-statistics'),
    Probe('Native monthly traffic', '/api/monitoring/month_statistics'),
    Probe('Native billing cycle', '/api/monitoring/start_date'),
    Probe('Device uptime', '/api/device/boot_time'),
    Probe('Current network provider', '/api/net/current-plmn'),
    Probe('Configured network mode and bands', '/api/net/net-mode'),
    Probe('Supported network modes and bands', '/api/net/net-mode-list'),
    Probe('Network registration configuration', '/api/net/register'),
    Probe('Mobile data and roaming configuration', '/api/dialup/connection'),
    Probe('Active and stored APN profiles', '/api/dialup/profiles'),
    Probe('Native antenna status', '/api/device/antenna_status'),
    Probe('Alternate native antenna type', '/api/device/antenna_type'),
    Probe('Native LTE and NR secondary carriers', '/api/device/seccellinfo'),
    Probe('Native LTE and NR neighbor cells', '/api/device/nbrcellinfo'),
    Probe('Configured LTE and NR frequency restrictions', '/api/net/lock-freq'),
    Probe('Final device and SIM continuity check', '/api/device/information'),
)


def _structure(root):
    budget, truncated = [512], [False]
    def walk(node, depth=0):
        if budget[0] <= 0 or depth > 6:
            truncated[0] = True
            return {}
        budget[0] -= 1
        if not len(node):
            return node.text or ''
        result = {}
        for child in node:
            if budget[0] <= 0:
                truncated[0] = True
                break
            value = walk(child, depth+1)
            if child.tag in result:
                if not isinstance(result[child.tag], list): result[child.tag] = [result[child.tag]]
                result[child.tag].append(value)
            else: result[child.tag] = value
        return result
    return walk(root), truncated[0]


class HiLinkReadDiagnosticAdapter:
    probes = PROBES

    def __init__(self, modem):
        self.modem = modem
        self.backend = modem._backend_name
        self.observing = True
        self._session = modem._session_id
        self._sim = subscription_key(str(modem._device_info_cache.get('imsi') or ''))
        self._iccid = modem._device_info_cache.get('iccid')
        self._changed = False
        self._device = DiagnosticDeviceContinuity(modem._device_info_cache)

    def context_current(self):
        current_sim = subscription_key(str(self.modem._device_info_cache.get('imsi') or ''))
        return bool(self.modem._connected and self.modem._session_id == self._session
                    and self._device.matches_snapshot(self.modem._device_info_cache)
                    and not (self._sim and current_sim and current_sim != self._sim)
                    and self.modem._device_info_cache.get('iccid') == self._iccid
                    and not self._changed)

    def _expired(self, path):
        self.modem._note_api_session_expired(path=path, lane_name='slow', deferred=True)
        self.modem._connected = False
        self.modem._logged_in = False

    def read(self, probe):
        if probe not in PROBES: raise ValueError('hilink_diagnostic_probe_not_registered')
        row = {'probe': probe.label, 'interface': probe.path,
               'assumption_id': 'HiLink_finite_read_candidates_v1',
               'qualification': 'Native candidate reply; selected firmware still owns admission'}
        try:
            response = self.modem._hilink_http_get(self.modem._base_url + probe.path,
                timeout=self.modem._timeout, lane='slow', allow_redirects=False)
        except requests.Timeout: return {**row, 'outcome': 'timeout'}
        except requests.RequestException: return {**row, 'outcome': 'transport_error'}
        row['http_status'] = response.status_code
        if response.status_code != 200:
            if response.status_code == 401: self._expired(probe.path)
            return {**row, 'outcome': 'session_not_found' if response.status_code == 401
                else 'access_denied' if response.status_code == 403
                else 'method_not_found' if response.status_code == 404 else 'request_failed'}
        raw = response.content
        if len(raw) > 1048576 or b'<!' in raw:
            return {**row, 'outcome': 'malformed_reply'}
        try: root = ET.fromstring(raw)
        except ET.ParseError: return {**row, 'outcome': 'malformed_reply'}
        if root.tag == 'error':
            code = root.findtext('code', '')
            if code in {'125002', '125003'}:
                self._expired(probe.path)
                return {**row, 'outcome': 'session_not_found', 'native_code': code}
            return {**row, 'outcome': 'method_not_found' if code == '100002'
                    else 'access_denied' if code == '100003' else 'request_failed',
                    'native_code': code if code.isdigit() and len(code) <= 8 else 'unrecognized'}
        if root.tag != 'response': return {**row, 'outcome': 'malformed_reply'}
        sim_keys = {subscription_key(node.text or '') for name in ('Imsi', 'IMSI')
                    for node in root.findall(name)} - {''}
        if len(sim_keys) > 1 or (self._sim and sim_keys and sim_keys != {self._sim}):
            self._changed = True
            return {**row, 'outcome': 'context_changed'}
        if sim_keys and not self._sim: self._sim = next(iter(sim_keys))
        data, truncated = _structure(root)
        evidence = project_fields(data)
        evidence['truncated'] = evidence['truncated'] or truncated
        if probe.path in {'/api/device/information', '/api/device/basic_information', '/api/device/basicinformation'}:
            try:
                current_device = self._device.native_values(data, {
                    'model': ('DeviceName',), 'hardware_version': ('HardwareVersion',),
                    'firmware': ('SoftwareVersion',), 'webui': ('WebUIVersion',),
                    'config_version': ('ConfigVersion',), 'serial': ('SerialNumber',),
                    'imei': ('Imei', 'IMEI')})
                normalized = self.modem._normalize_device_identity(current_device)
                current_device = self._device.canonical_values(normalized)
            except ValueError: return {**row, 'outcome': 'malformed_reply', **evidence}
            if not self._device.observe(current_device):
                self._changed = True
                return {**row, 'outcome': 'context_changed', **evidence}
        return {**row, 'outcome': 'success' if len(root) else 'empty_reply', **evidence}

    def summarize(self, rows):
        return [{'field': row['probe'], 'result': 'Returned; interpretation remains separate'
                 if row['outcome'] == 'success' else 'Not returned; see the individual check below',
                 'candidate_fields': sorted(row.get('fields', {}))} for row in rows]
