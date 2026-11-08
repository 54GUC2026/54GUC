"""Read-only DEV2 object plan; the existing encrypted transport owns the wire."""
from dataclasses import dataclass

import requests

from arch4.modem.base import SessionExpiredError, ModemTransportUnavailable
from arch4.modem.tplink_dev2_transport import Dev2ProtocolError, Dev2Rejected, ZERO_STACK
from arch4.modem.read_diagnostic_projection import project_fields
from arch4.modem.read_diagnostics import DiagnosticDeviceContinuity
from arch4.core_contracts.network_settings import subscription_key


@dataclass(frozen=True)
class Probe:
    label: str
    oid: str
    stack: str = '1,0,0,0,0,0'
    listing: bool = False


PROBES = (
    Probe('Device and firmware identity', 'DEV2_DEV_INFO', ZERO_STACK),
    Probe('Cellular device identity', 'DEV2_CELL_INTF'),
    Probe('Active SIM metadata', 'DEV2_CELL_INTF_USIM'),
    Probe('Mobile connection state', 'DEV2_LTE_LINK_CFG'),
    Probe('Native LTE and NR serving carriers', 'DEV2_LTE_SERVING_CELL_INFO', ZERO_STACK, True),
    Probe('Traffic rates and counters', 'DEV2_XTP_LTE_INTF_CFG'),
    Probe('Supported and selected bands', 'DEV2_LTE_BAND_SEARCH'),
    Probe('Current network provider', 'DEV2_LTE_PROF_STAT'),
    Probe('Mobile data, roaming and active APN', 'DEV2_LTE_WAN_CFG'),
    Probe('Stored APN profiles', 'DEV2_LTE_PROFILE', ZERO_STACK, True),
    Probe('Network selection state', 'DEV2_LTE_NETWORK_SEARCH'),
    Probe('Final active SIM continuity check', 'DEV2_CELL_INTF_USIM'),
)

EXTRA_PROBES={
    'DEV2_ADT_WAN':Probe('Cellular interface traffic counters','DEV2_ADT_WAN',ZERO_STACK,True),
    'DEV2_XTP_LTE':Probe('Cellular module firmware and product','DEV2_XTP_LTE',ZERO_STACK),
    'DEV2_LTE_SIMLOCK':Probe('SIM unlock attempt counters','DEV2_LTE_SIMLOCK'),
    'DEV2_ADT_WIFI_COMMON':Probe('Main Wi-Fi network state','DEV2_ADT_WIFI_COMMON',ZERO_STACK,True),
    'DEV2_PROC_STATUS':Probe('Router CPU load','DEV2_PROC_STATUS',ZERO_STACK),
    'DEV2_MEM_STATUS':Probe('Router memory total and free','DEV2_MEM_STATUS',ZERO_STACK),
    'DEV2_ADT_LAN':Probe('LAN interface identity','DEV2_ADT_LAN',ZERO_STACK,True),
}


class Dev2ReadDiagnosticAdapter:
    probes = PROBES

    def __init__(self, modem):
        self.modem = modem
        self.backend = modem._backend_name
        self.observing = True
        self._cipher = modem._dev2._cipher
        self._token = modem._dev2._token
        self._changed = False
        self._sim = ''
        self._matrix_comparison = None
        self._device = DiagnosticDeviceContinuity(modem._identity)
        self.probes=PROBES+tuple(EXTRA_PROBES[oid] for oid in modem._device_contract['read_diagnostic_extra_oids'])

    def context_current(self):
        return bool(self.modem.is_connected() and self.modem._dev2._cipher is self._cipher
                    and self.modem._dev2._token == self._token and not self._changed
                    and self._device.matches_snapshot(self.modem._identity))

    def read(self, probe):
        if probe not in self.probes: raise ValueError('dev2_diagnostic_probe_not_registered')
        row = {'probe': probe.label, 'interface': probe.oid,
               'operation': 'gl' if probe.listing else 'go',
               'assumption_id': f'DEV2_{self.modem._profile_id}_read_plan_v1',
               'qualification': 'Selected exact device contract; software assumptions require native verification'}
        try:
            data = self.modem._read(probe.oid, stack=probe.stack,
                                    listing=probe.listing, lane='slow_live')
        except SessionExpiredError: return {**row, 'outcome': 'session_not_found'}
        except Dev2Rejected as exc: return {**row, 'outcome': 'request_failed', 'native_code': exc.code}
        except Dev2ProtocolError: return {**row, 'outcome': 'malformed_reply'}
        except ModemTransportUnavailable as exc:
            return {**row, 'outcome': 'timeout' if exc.reason=='modem_transport_timeout' else 'transport_error'}
        except requests.Timeout: return {**row, 'outcome': 'timeout'}
        except requests.RequestException: return {**row, 'outcome': 'transport_error'}
        if probe.oid in {'DEV2_DEV_INFO', 'DEV2_CELL_INTF'}:
            aliases = {'model': ('modelName',), 'hardware_version': ('hardwareVersion',),
                       'firmware': ('softwareVersion',), 'serial': ('serialNumber',)} if probe.oid == 'DEV2_DEV_INFO' else {'imei': ('IMEI',)}
            try: current_device = self._device.native_values(data, aliases)
            except ValueError: return {**row, 'outcome': 'malformed_reply', **project_fields(data)}
            if not self._device.observe(current_device):
                self._changed = True
                return {**row, 'outcome': 'context_changed', **project_fields(data)}
        if probe.oid == 'DEV2_CELL_INTF_USIM':
            current = subscription_key(str(data.get('IMSI') or ''))
            if self._sim and current and current != self._sim:
                self._changed = True
                return {**row, 'outcome': 'context_changed', **project_fields(data)}
            if current: self._sim = current
        return {**row, 'outcome': 'success' if data else 'empty_reply',
                **project_fields({'records': data} if probe.listing else data)}

    def bind_matrix_comparison(self, matrix_reader, *, profile_id):
        from arch4.modem_live.truth_matrix import ValidatedTruthMatrixReader
        if profile_id != self.modem._profile_id:
            raise ValueError('dev2_diagnostic_comparison_profile_mismatch')
        if self._matrix_comparison is not None:
            raise RuntimeError('dev2_diagnostic_comparison_already_bound')
        if not isinstance(matrix_reader, ValidatedTruthMatrixReader):
            raise TypeError('dev2_diagnostic_validated_reader_required')
        self._matrix_comparison = matrix_reader

    def summarize(self, rows):
        from arch4.modem_live.scout import build_read_diagnostic_coverage
        unqualified = None
        try:
            descriptors = self.modem.diagnostic_native_field_descriptors(rows)
        except Dev2ProtocolError:
            # Explicit classification refusal, not an alternate parser. Native
            # candidates stay evidence; no canonical scalar can be claimed.
            descriptors = ()
            unqualified = {'DEV2_LTE_SERVING_CELL_INFO':
                'Serving carrier classification is ambiguous or invalid; no canonical scalar comparison was made.'}
        return build_read_diagnostic_coverage(rows=rows, probes=self.probes,
            descriptors=descriptors, unqualified_interfaces=unqualified,
            profile_id=self.modem._profile_id, matrix_reader=self._matrix_comparison)

    def diagnostic_coverage_context(self):
        from arch4.modem_live.scout import OWNER
        return {'owner': OWNER, 'profile_id': self.modem._profile_id,
                'comparison_state': 'compared' if self._matrix_comparison is not None else 'not_compared',
                'matrix_identity': self._matrix_comparison.identity.as_dict()
                    if self._matrix_comparison is not None else None}
