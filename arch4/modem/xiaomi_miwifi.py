"""Profile-bound stock Xiaomi adapter; all wire calls belong to Modem Live."""
from __future__ import annotations

import ipaddress
import math
import re
import time
from urllib.parse import urlsplit, parse_qs
from arch4.core_contracts.command_targets import CoreRadioModeTarget

import requests

from arch4.modem_live.profile_diagnostics import decode_native_profile
from arch4.interfaces import ModemInput, band_signature
from arch4.modem.base import (ModemInterface, LiveAcquisitionContract,
    AuthenticationError, SessionExpiredError, TargetGuardError, evaluate_target_guard,
    build_public_modem_status)
from arch4.modem.backend_acquisition import DECLARED_CANONICAL_SOURCE_FIELDS, matrix_cells_for_canonical_fields
from arch4.modem.live_lane_contract import modem_live_access_profile_for
from arch4.modem.network_inspection import NetworkSettingsRead
from arch4.modem.utils import measure_host_ping_rtt
from arch4.modem_live.physical_request_authority import ModemLivePhysicalRequestAuthority
from arch4.modem_live.physical_command import ModemLivePhysicalStagePlan, ModemLivePhysicalStageResult
from arch4.modem_live.startup_trace import traced_http
from arch4.core_contracts.command_targets import CoreBandSetTarget, CoreNrBandTarget
from arch4.core_contracts.network_settings import subscription_key, validate_network_observation
from arch4.modem.xiaomi_miwifi_protocol import (
    MiWiFiSession, MiWiFiProtocolError, MiWiFiRejected, MiWiFiSessionExpired,
    READ_METHODS, WRITES, MAX_REPLY,
)
from arch4.modem.xiaomi_miwifi_tls import XiaomiHTTPSAdapter

BACKEND = 'xiaomi_cb0401v2'
PROFILE_ID = 'xiaomi_cb0401v2_3_0_88'


def number(value, low, high, integer=False):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or not low <= result <= high or integer and not result.is_integer():
        return None
    return int(result) if integer else result


def reported_carriers(info, channel_ranges):
    """Native, position-matched LTE/NR composition, not an allowed band pool.

    The stock CPE and public owner replies supply parallel band, channel and
    downlink-width lists. Unknown composition cannot become an empty/inactive CA.
    """
    if not isinstance(info, dict):
        return None
    names, channels, widths = (info.get(key) for key in ('freqband', 'arfcn', 'dl_bw'))
    if any(not isinstance(value, str) or len(value) > 512 for value in (names, channels, widths)):
        return None
    names, channels, widths = names.split('+'), channels.split(','), widths.split(',')
    if not 1 <= len(names) <= 16 or len(names) != len(channels) or len(names) != len(widths):
        return None
    rows = []
    for index, name in enumerate(names):
        label = re.fullmatch(r'(B|N)?([1-9][0-9]{0,3})', name.strip().upper())
        if label is None:
            return None
        band = int(label.group(2))
        prefix = label.group(1)
        if prefix is None:
            # A bare number is LTE only when its channel independently agrees.
            # Otherwise an explicit NR serving label must identify its RAT.
            lte_channel = number(channels[index].strip(), 0, 262143, True)
            if any(row[0] == band and lte_channel is not None
                   and row[1] <= lte_channel <= row[2] for row in channel_ranges):
                prefix = 'B'
            else:
                nr_label = re.fullmatch(r'N?([1-9][0-9]{0,3})(?:\s*\((?:FDD|TDD)\s+[0-9]{2,5}(?:\s+APT)?\))?',
                    str(info.get('cell_band_5g') or '').strip().upper())
                if nr_label is None or int(nr_label.group(1)) != band:
                    return None
                prefix = 'N'
        nr = prefix == 'N'
        channel = number(channels[index].strip(), 0, 3279165 if nr else 262143, True)
        width = number(widths[index].strip(), .1, 400 if nr else 20)
        if channel is None or width is None:
            return None
        if not nr and (not any(row[0] == band and row[1] <= channel <= row[2]
                               for row in channel_ranges)
                       or width not in (1.4, 3, 5, 10, 15, 20)):
            return None
        rows.append({'rat': 'NR' if nr else 'LTE', 'band': band,
            'arfcn' if nr else 'earfcn': channel, 'dl_bandwidth_mhz': width})
    return rows


def reported_lte_carrier_details(rows, serving_band, earfcn):
    """Existing carrier display, with independently identified primary only."""
    if not isinstance(rows, list) or len(rows) > 16:
        return None
    lte = [row for row in rows if isinstance(row, dict) and row.get('rat') == 'LTE']
    primary = [row for row in lte
               if row.get('band') == serving_band and row.get('earfcn') == earfcn]
    if len(primary) != 1:
        return None
    return [{**row, 'is_pcell': row is primary[0]} for row in lte]


def unique_text(row, names):
    values = {str(row[k]).strip() for k in names if row.get(k) not in (None, '')}
    if len(values) > 1:
        raise MiWiFiProtocolError('xiaomi_native_alias_values_conflict')
    return next(iter(values), '')


def stock_model(row, forms=None):
    # Stock hardware and model are two representations, not raw aliases.
    if forms is None:
        forms = {'CB0401V2': 'CB0401V2', 'XIAOMI.ROUTER.CB0401V2': 'CB0401V2'}
    values = set()
    for name in ('hardware', 'model'):
        value = row.get(name)
        if value in (None, ''):
            continue
        if not isinstance(value, str):
            raise MiWiFiProtocolError('xiaomi_native_model_shape_invalid')
        text = value.strip().upper()
        values.add(forms.get(text, text))
    if len(values) > 1:
        raise MiWiFiProtocolError('xiaomi_native_model_values_conflict')
    return next(iter(values), '')


def device_identity_text(row, names):
    from arch4.modem.operator_identity import operator_serial_text
    values = set()
    for name in names:
        value = row.get(name)
        if value in (None, ''):
            continue
        if not isinstance(value, str):
            raise MiWiFiProtocolError('xiaomi_native_device_identity_shape_invalid')
        text = operator_serial_text(value)
        if text:
            values.add(text)
    if len(values) > 1:
        raise MiWiFiProtocolError('xiaomi_native_device_identity_values_conflict')
    return next(iter(values), '')


def band_list(value):
    if not isinstance(value, list) or any(type(v) not in (int, str) or not re.fullmatch(r'[1-9]\d{0,3}', str(v)) for v in value):
        raise MiWiFiProtocolError('xiaomi_band_array_invalid')
    result = [int(v) for v in value]
    if len(result) != len(set(result)):
        raise MiWiFiProtocolError('xiaomi_supported_band_duplicates')
    return result


def selected_bands(supported, flags):
    # Both a string and an array support the indexed '1' test in stock JS.
    if isinstance(flags, str) and re.fullmatch(r'[01]*', flags):
        flags = list(flags)
    if not isinstance(flags, list) or len(flags) != len(supported) or any(type(v) not in (int, str) or str(v) not in {'0', '1'} for v in flags):
        raise MiWiFiProtocolError('xiaomi_selection_mask_invalid')
    return [band for band, flag in zip(supported, flags) if str(flag) == '1']


def sim_identity(reply):
    info = reply.get('info', reply)
    if not isinstance(info, dict): return ''
    value = unique_text(info, ('imsi', 'sim_imsi'))
    return value if re.fullmatch(r'\d{14,16}', value) else ''


def device_text(row, names, placeholders):
    """Pure identity conversion; placeholder names belong to the profile."""
    values = set()
    for name in names:
        value = row.get(name)
        if value in (None, ''):
            continue
        if not isinstance(value, str):
            raise ValueError('device_identity_shape_invalid')
        text = value.strip()
        if (not text or len(text) > 128 or any(ord(c) < 32 for c in text)
                or text.lower() in placeholders or set(text) == {'0'}):
            continue
        values.add(text)
    if len(values) > 1:
        raise ValueError('device_identity_alias_conflict')
    return next(iter(values), '')


def network_observation(replies, profile, session_subscription=''):
    sim = replies.get('xqdtcustom/get_sim_info', {}) or {}
    pass
    cfg = replies.get('xqmobile/get_mobile_net_cfg') or {}
    pass
    profiles = replies.get('xqmobile/get_apn_info') or {}
    pass
    imsi = sim_identity(sim)
    native_subscription = subscription_key(imsi)
    if native_subscription and session_subscription and native_subscription != session_subscription:
        raise ValueError('xiaomi_network_subscription_changed')
    pass
    rows = profiles.get('apnlist')
    # Optional APN details cannot invalidate independent operator/roaming
    # facts. An absent or ambiguous active profile stays unknown for writes.
    active = [r for r in rows if isinstance(r, dict) and type(r.get('id')) in (int, str)
              and type(profiles.get('curid')) in (int, str)
              and str(r['id']) == str(profiles['curid'])] if isinstance(rows, list) else []
    apn = None
    if len(active) == 1:
        row = active[0]
        if isinstance(row.get('apn'), str):
            apn = dict(profile_id=str(row['id']),
                name=row.get('file', '') if isinstance(row.get('file', ''), str) else '',
                apn=row['apn'],
                username=row.get('user', '') if isinstance(row.get('user', ''), str) else '',
                authentication={'none': 'none', 'pap': 'pap', 'chap': 'chap', 'auto': 'auto'}.get(
                    row.get('encryption', '').strip().lower() if isinstance(row.get('encryption', ''), str) else ''),
                ip_protocol={'ip': 'ipv4', 'ipv4': 'ipv4', 'ipv6': 'ipv6', 'both': 'ipv4v6', 'ipv4v6': 'ipv4v6'}.get(
                    row.get('pdp', '').strip().lower() if isinstance(row.get('pdp', ''), str) else ''),
                editable=False)
    operator = replies.get('xqmobile/get_search_net_mode', {})
    selection = {'1': 'auto', 'auto': 'auto', 'automatic': 'auto',
                 '0': 'manual', 'manual': 'manual'}.get(str(operator.get('mode', '')).strip().lower())
    observation = validate_network_observation(dict(schema='NetworkSettingsObservation.v1', subscription_key=native_subscription or session_subscription or '', selection=selection, target_plmn=None if selection == 'manual' else '', roaming_enabled={'0': False, '1': True}.get(str(cfg.get('networkroam')).strip()), apn=apn))
    return observation


class XiaomiMiWiFiModem(ModemInterface):
    physical_request_authority_required = True
    requires_provider_pool_policy = True

    def __init__(self, *, host, username='admin', password='', ping_interface=None,
                 expected_model='', expected_serial='', expected_firmware='', expected_imei='',
                 profile_id=None):
        from arch4.modem_live.profile_catalog import modem_profile_definition, modem_profile_record
        self._definition = modem_profile_definition(profile_id or PROFILE_ID)
        self._device_contract = modem_profile_record(self._definition.profile_id)
        if (self._device_contract.get('backend') != BACKEND
                or self._device_contract.get('family') != 'xiaomi_miwifi_like'
                or not (self._device_contract.get('firmware_exact') or self._device_contract.get('firmware_pattern'))
                or not self._device_contract.get('target_guard_model')):
            raise ValueError('xiaomi_explicit_exact_device_contract_required')
        host = self._definition.http_address(host)
        parsed = urlsplit(host if '://' in host else 'http://' + host)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
            raise ValueError('xiaomi_device_address_invalid')
        self._base_url = f'{parsed.scheme}://{parsed.netloc}'
        self._profile_field_issues = {}
        self._backend_name = BACKEND
        self._profile = modem_live_access_profile_for(backend=BACKEND, profile_id=self._definition.profile_id)
        self._physical_request_authority = ModemLivePhysicalRequestAuthority(self._profile)
        self._http = requests.Session()
        self._http.trust_env = False
        self._http.mount('https://', XiaomiHTTPSAdapter())
        self._http.headers.update({'Connection': self._profile.http_connection_mode,
                                   'Content-Type': 'application/x-www-form-urlencoded'})
        self._miwifi = MiWiFiSession(self._wire, username, password)
        self._expected = dict(model=expected_model, serial=expected_serial, firmware=expected_firmware, imei=expected_imei)
        self._identity = {}
        self._status = {}
        self._fresh_status = {}
        self._sim_identity = ''
        self._qualified = False
        self._provider_pool_policy = None
        self._ping_iface = ping_interface
        self._last_write_started = self._last_write_returned = 0.0
        self._diagnostic_sequence = 0
        self._startup_identity_prompt = None

    def physical_request_lane_scope(self, lane):
        return self._physical_request_authority.lane_scope(lane)

    def physical_request_authority_status(self):
        return self._physical_request_authority.snapshot()

    def bind_provider_pool_policy(self, resolver):
        if not callable(resolver):
            raise TypeError('xiaomi_provider_pool_policy_required')
        if self._provider_pool_policy is not None or self._qualified:
            raise RuntimeError('xiaomi_provider_pool_policy_already_bound_or_connected')
        self._provider_pool_policy = resolver

    def _wire(self, method, path, body, lane, operation):
        public = path in {'/cgi-bin/luci/web', '/cgi-bin/luci/api/xqsystem/init_info'}
        login = path == '/cgi-bin/luci/api/xqsystem/login'
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError('xiaomi_wire_path_not_registered')
        matched = re.fullmatch(r'/cgi-bin/luci/;stok=[A-Za-z0-9_-]{8,128}/api/([a-z0-9_]+(?:/[a-z0-9_]+)?)', parsed.path)
        endpoint = matched[1] if matched else ''
        read = not parsed.query and endpoint in READ_METHODS and method == READ_METHODS[endpoint] and (method == 'GET' or body == b'')
        mode_write = False
        if endpoint == 'xqmobile/set_mobile_net_cfg' and method == 'GET' and body is None:
            from arch4.modem.xiaomi_radio_mode import qualified
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
            mode_write = (qualified(self) and set(query) == {'networktype', 'networkdata', 'networkroam'}
                and query['networktype'] in [['auto'], ['nsa'], ['sa'], ['4g']]
                and query['networkdata'] in [['0'], ['1']] and query['networkroam'] in [['0'], ['1']])
        if not (public and method == 'GET' or login and method == 'POST' or read
                or not parsed.query and endpoint in WRITES and method == 'POST' or mode_write):
            raise ValueError('xiaomi_wire_path_not_registered')
        writing = endpoint in WRITES and method == 'POST' or endpoint == 'xqmobile/set_mobile_net_cfg' and method == 'GET'
        if writing:
            capability = 'supports_radio_mode' if endpoint == 'xqmobile/set_mobile_net_cfg' else 'supports_band_selection'
            if not self._device_contract['capabilities'].get(capability, False):
                raise RuntimeError('xiaomi_control_target_not_qualified')
        actual_lane = self._physical_request_authority.current_lane(default=lane)
        for attempt in range(2):
            with self._physical_request_authority.request(lane=actual_lane, operation='xiaomi:' + operation):
                writing = endpoint in WRITES and method == 'POST' or endpoint == 'xqmobile/set_mobile_net_cfg' and method == 'GET'
                if writing:
                    self._last_write_started = time.time()
                timeout = (
                    (self._profile.maintenance_connect_timeout_s, self._profile.maintenance_read_timeout_s)
                    if actual_lane == 'slow_live' else
                    (self._profile.fast_signal_connect_timeout_s, self._profile.fast_signal_read_timeout_s)
                )
                opts = dict(data=body, allow_redirects=False, stream=True, timeout=self.background_read_timeout(timeout))
                try:
                    response = traced_http(self._http.get if method == 'GET' else self._http.post, self._base_url + path, **opts)
                except requests.Timeout as exc:
                    if self._physical_request_authority.remaining_read_s() is not None:
                        from arch4.modem_live.physical_request_authority import BackgroundReadSlotExpired
                        raise BackgroundReadSlotExpired() from exc
                    raise
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if method == 'GET' and path == '/cgi-bin/luci/web' and not self._miwifi.authenticated:
                            self._accept_login_https_upgrade(response, attempt)
                            continue
                        self._miwifi.clear()
                        raise MiWiFiSessionExpired('xiaomi_session_expired')
                    if response.status_code in {401, 403}:
                        self._miwifi.clear()
                        raise MiWiFiSessionExpired('xiaomi_session_expired')
                    response.raise_for_status()
                    chunks = []
                    size = 0
                    for chunk in response.iter_content(16384):
                        size += len(chunk)
                        if size > MAX_REPLY:
                            raise MiWiFiProtocolError('xiaomi_reply_too_large')
                        chunks.append(chunk)
                    return b''.join(chunks)
                finally:
                    response.close()
                    if writing:
                        self._last_write_returned = time.time()
        raise MiWiFiProtocolError('xiaomi_login_redirect_limit')

    def _accept_login_https_upgrade(self, response, attempt):
        location = response.headers.get('Location', '')
        try:
            original, target = urlsplit(self._base_url), urlsplit(location)
            valid = (attempt == 0 and response.status_code == 301
                and original.scheme == 'http' and original.port in (None, 80)
                and target.scheme == 'https' and target.hostname == original.hostname
                and target.port in (None, 443) and target.username is None and target.password is None
                and target.path == '/cgi-bin/luci/web' and not target.query and not target.fragment
                and not any(ord(c) <= 32 for c in location))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise MiWiFiProtocolError('xiaomi_login_redirect_not_qualified')
        # Only this initial, credential-free GET negotiates the stock transport.
        # Every following metadata, login, read and write stays on this origin.
        self._base_url = f'https://{target.netloc}'

    def _read(self, endpoint, lane='fast_live'):
        request=self._definition.request(endpoint,protocol='miwifi_json')
        path=request.get('path','')
        match=re.fullmatch(r'/cgi-bin/luci/;stok=\{session\}/api/([a-z0-9_]+(?:/[a-z0-9_]+)?)',path)
        selected=match[1] if match else ''
        if selected not in READ_METHODS or request.get('method')!=READ_METHODS[selected]:
            raise MiWiFiProtocolError('xiaomi_profile_read_not_qualified')
        try:
            return self._miwifi.read(selected, lane=lane)
        except MiWiFiSessionExpired as exc:
            raise SessionExpiredError('Xiaomi router session expired. Reconnect to the device.') from exc

    def connect(self):
        self._qualified = False
        self._identity = {}
        self._sim_identity = ''
        self._refused_measurement_endpoints = set()
        self._measurement_endpoint = 'xqdtcustom/get_mobile_net_info'
        try:
            self._miwifi.login()
            self.read_fresh_device_identity()
            self._qualified = True
            return True
        except MiWiFiRejected as exc:
            self._miwifi.clear()
            raise AuthenticationError('Xiaomi login was refused. Check the router password.') from exc
        except Exception:
            self._miwifi.clear()
            raise

    def disconnect(self):
        self._qualified = False
        self._identity = {}
        self._sim_identity = ''
        self._miwifi.clear()
        self._http.close()

    def is_connected(self):
        return self._qualified and self._miwifi.authenticated

    def bind_startup_identity_prompt(self, prompt):
        if prompt is not None and not callable(prompt):
            raise TypeError('xiaomi_startup_identity_prompt_callable_required')
        self._startup_identity_prompt = prompt

    def read_fresh_device_identity(self):
        row = self._read('xqsystem/init_info', 'boot_static')
        addresses={'identity.model','identity.firmware','identity.software_version',
                   'identity.hardware_version','identity.serial','identity.imei'}
        values,diagnostic=self._decode_fields({'xqsystem/init_info':row},
            context={'hardware_identity':False},addresses=addresses)
        required={'identity.model','identity.firmware','identity.serial','identity.imei'}
        if required.intersection(self._profile_field_issues):
            self._status['backend_diagnostic']=diagnostic
            raise MiWiFiProtocolError('xiaomi_profile_identity_conversion_failed')
        model=values['identity.model'];firmware=values['identity.firmware']
        identity=dict(vendor='Xiaomi',model=model,firmware=firmware,
            software_version=values['identity.software_version'],hardware_version=values['identity.hardware_version'],
            serial=values['identity.serial']or'',imei=values['identity.imei']or'',
            physical_identity_source='xqsystem/init_info')
        exact = self._device_contract['firmware_exact']
        if exact:
            firmware_matches = firmware == exact
        else:
            from arch4.modem_live.profile_definition.bounded_regex import RegexBudget
            firmware_matches = (isinstance(firmware, str) and len(firmware) <= 256
                and RegexBudget().call('fullmatch', self._device_contract['firmware_pattern'], firmware) is not None)
        if model != self._device_contract['target_guard_model'] or not firmware_matches:
            from arch4.observation import allow_related_model
            if not allow_related_model(identity, 'xiaomi_exact_firmware_identity_mismatch'):
                raise TargetGuardError('This Xiaomi model or firmware does not match the conditional profile.', reason='xiaomi_exact_firmware_identity_mismatch')
        from arch4.modem.operator_identity import operator_serial_text, require_operator_serial
        if not operator_serial_text(identity['serial']) and not operator_serial_text(identity['imei']):
            # Only explicit device hardware metadata can supply a router SN.
            # SIM metadata must never become the permanent device identity.
            try:
                candidate = self._read('xqdtcustom/newstatus', 'boot_static')
            except (MiWiFiRejected, MiWiFiProtocolError):
                candidate = {}
            values,diagnostic=self._decode_fields({'xqsystem/init_info':row,
                'xqdtcustom/newstatus':candidate},context={'hardware_identity':True},
                addresses={'identity.serial','identity.imei'})
            if {'identity.serial','identity.imei'}.intersection(self._profile_field_issues):
                self._status['backend_diagnostic']=diagnostic
                raise MiWiFiProtocolError('xiaomi_profile_identity_conversion_failed')
            if isinstance(candidate.get('hardware'),dict):
                identity.update(serial=values['identity.serial']or'',imei=values['identity.imei']or'',
                    physical_identity_source='xqdtcustom/newstatus.hardware')
            if not operator_serial_text(identity['serial']) and not operator_serial_text(identity['imei']):
                serial = require_operator_serial(self._expected['serial'], self._startup_identity_prompt)
                self._expected['serial'] = serial
                identity.update(serial=serial, physical_identity_source='operator_supplied_serial')
        for expected in (self._expected, self._identity):
            ok, reason = evaluate_target_guard(expected_model=expected.get('model', ''), expected_serial=expected.get('serial', ''),
                expected_firmware=expected.get('firmware', ''), expected_imei=expected.get('imei', ''), observed=identity)
            if not ok:
                raise TargetGuardError('The connected router does not match the selected device.', reason=reason)
        self._identity = identity
        return dict(identity)

    def get_modem_info(self): return dict(self._identity)
    def get_last_status(self): return build_public_modem_status(self._status)
    def get_capabilities(self):
        from arch4.interfaces import get_capabilities, ModemCapabilities
        if self._device_contract['device_radio_class'] == '4g_only':
            return ModemCapabilities(**self._device_contract['capabilities'])
        return get_capabilities(BACKEND)
    def verify_static_config(self): return {}
    def verify_ue_capability(self): return {}
    def read_neighbors(self): return []
    def scan_neighbors(self): return []
    def read_band_priority(self): return []
    def read_serving_cell(self): return dict(self._status)
    def get_current_bands(self): return list(self._status.get('selected_band_set', []))
    def measure_rtt(self, target='8.8.8.8', count=3, *, timeout_s):
        return measure_host_ping_rtt(target, count=count, ping_interface=self._ping_iface, timeout_s=timeout_s)

    def _assumption_diagnostic(self, endpoint, error):
        from arch4.modem.backend_diagnostic import ModemBackendDiagnostic
        from arch4.modem.xiaomi_miwifi_protocol import ASSUMPTION_ID
        self._diagnostic_sequence += 1
        return ModemBackendDiagnostic(owner='modem_live.xiaomi.assumption_check', sequence=self._diagnostic_sequence,
            observed_at=time.time(), kind='router_assumption_check', backend=BACKEND, severity='WARNING',
            data={'assumption_id': ASSUMPTION_ID if self._definition.profile_id == PROFILE_ID else self._definition.profile_id + '_conditional_shared_family', 'interface': endpoint, 'error_type': type(error).__name__,
                  'native_code': error.code if isinstance(error, MiWiFiRejected) else None,
                  'result': ('No measurement admitted; management session retained. Create a support bundle.'
                    if self._definition.snapshot().get('parameters', {}).get('native_protocol') == 'xiaomi_mgdb'
                    else 'No measurement admitted; management session retained. Run Read diagnostics.')}).to_payload()

    def _radio(self):
        try:
            return self._radio_read()
        except (MiWiFiRejected, MiWiFiProtocolError) as error:
            self._fresh_status = {'backend_diagnostic': self._assumption_diagnostic('xqdtcustom/cpe_detect', error)}
            self._status.update(self._fresh_status)
            return ModemInput.safe_defaults(traffic_valid=False, session_uptime_source='unsupported')

    def _decode_fields(self, replies, *, context=None, addresses=None):
        if addresses is None: addresses = self._definition.available_addresses(replies, context=context)
        issues = []
        values = decode_native_profile(self._definition, replies, context, addresses=addresses, issues=issues)
        for address in addresses: self._profile_field_issues.pop(address, None)
        self._profile_field_issues.update((issue.address, issue) for issue in issues)
        from arch4.modem_live.profile_diagnostics import field_diagnostic
        diagnostic = field_diagnostic(self._definition, self._profile_field_issues,
            previous=getattr(self, '_status', {}).get('backend_diagnostic'))
        return values, diagnostic

    def _radio_read(self):
        # Stock firmware uses this endpoint to request a measurement. The
        # sibling cpe_detect endpoint may serve only the previous measurement.
        replies = {}
        endpoints = ('xqdtcustom/get_mobile_net_info', 'xqmobile/get_mobile_net_info')
        preferred = getattr(self, '_measurement_endpoint', endpoints[0])
        refused = getattr(self, '_refused_measurement_endpoints', set())
        self._refused_measurement_endpoints = refused
        spent, measured, last_attempted = 0, None, None
        for endpoint in sorted(endpoints, key=lambda item: item != preferred):
            if endpoint in refused:
                continue
            spent += 1
            last_attempted = endpoint
            try:
                refreshed = self._read(endpoint)
                if not isinstance(refreshed.get('info'), dict):
                    raise MiWiFiProtocolError('xiaomi_measurement_reply_shape_unknown')
                replies[endpoint] = refreshed
                measured = endpoint
                self._measurement_endpoint = endpoint
                break
            except MiWiFiRejected as error:
                # Only an explicit missing API is a session capability result.
                # Unknown device/server errors remain retryable in later slots.
                if error.code == 404:
                    refused.add(endpoint)
            except MiWiFiProtocolError:
                pass  # Malformed is retryable; a working alias can replace it.
        cached_only = measured is None
        net = None
        if cached_only and spent < 2:
            reply = self._read('xqdtcustom/cpe_detect')
            spent += 1
            replies['xqdtcustom/cpe_detect'] = reply
            net = reply.get('net')
        elif not cached_only:
            net = {'code': refreshed.get('code'), 'info': refreshed.get('info')}
        if net is not None:
            if not isinstance(net, dict) or type(net.get('code')) is not int or net['code'] != 0 or not isinstance(net.get('info'), dict):
                raise MiWiFiProtocolError('xiaomi_radio_reply_shape_unknown')
            mode = net['info'].get('linktype')
            if mode is not None and not isinstance(mode, str):
                raise MiWiFiProtocolError('xiaomi_radio_mode_shape_invalid')
        modem = ModemInput.safe_defaults(traffic_valid=False, session_uptime_source='unsupported')
        # Both stock measurement aliases are permitted. Their first discovery
        # may use two reads; the next slot uses the working one plus WAN.
        if spent < 2:
            try:
                replies['misystem/status'] = self._read('misystem/status')
            except (MiWiFiRejected, MiWiFiProtocolError, requests.RequestException):
                pass  # Optional traffic cannot discard the usable radio reply.
        values, diagnostic = self._decode_fields(replies, context={
            'radio_reader': ('cache' if cached_only else 'measurement'
                if measured == endpoints[0] else 'measurement_alt')})
        if cached_only:
            # cpe_detect can keep the previous measurement indefinitely.
            # Its fresh HTTP response cannot renew RF/RAT/provider truth.
            # Matrix keeps the last confirmed value with its original age;
            # independently sampled WAN rates remain usable in this slot.
            values = {address: value for address, value in values.items()
                      if address.startswith('traffic.') or address in {
                          'slow.router_cpu_load_pct', 'slow.router_memory_used_pct',
                          'slow.router_system_uptime_s'}}
            diagnostic = self._assumption_diagnostic(
                'xqdtcustom/cpe_detect' if 'xqdtcustom/cpe_detect' in replies else last_attempted,
                MiWiFiProtocolError('xiaomi_cached_measurement_age_unknown'))
        status = {}
        for address, value in values.items():
            if value is None: continue
            field = address.split('.', 1)[1]
            if field == 'provider_plmn' and not value:
                # A missing native ID is not a fresh replacement for the
                # separately sampled SIM fallback or last valid native ID.
                continue
            status[field] = value
            if field in modem.__dataclass_fields__: setattr(modem, field, value)
        if 'serving_band' in status:
            status['primary_band'] = status['serving_band']
            modem.primary_band = status['serving_band']
        details = reported_lte_carrier_details(status.get('carrier_rows'),
            status.get('serving_band'), status.get('earfcn_dl'))
        if details is not None:
            status['carrier_details'] = details
        if status.get('provider_plmn'):
            status['provider_plmn_source'] = 'native'
        if diagnostic is not None: status['backend_diagnostic'] = diagnostic
        # Native datausage still supplies neither a rate nor a DL/UL split.
        self._fresh_status = status
        self._status.update(status)
        return modem

    def _bands(self, lane):
        reply = self._read('xqmobile/get_cellular_band_list', lane)
        native = reply.get('list')
        if not isinstance(native, dict):
            raise MiWiFiProtocolError('xiaomi_band_list_shape_unknown')
        values, _ = self._decode_fields({'xqmobile/get_cellular_band_list': reply},
            addresses={'capability.supported_lte_bands', 'capability.supported_nr_bands',
                       'radio.selected_band_set', 'profile.nr5g_band_set'})
        supported = values['capability.supported_lte_bands']
        nr_supported = values.get('capability.supported_nr_bands')
        selected = values['radio.selected_band_set']
        nr_selected = values.get('profile.nr5g_band_set')
        if self._device_contract['device_radio_class'] == '4g_only':
            nr_supported, nr_selected = [], []  # Qualified LTE-only hardware, not a missing NR reply.
        if any(value is None for value in (supported, nr_supported, selected, nr_selected)):
            raise MiWiFiProtocolError('xiaomi_band_definition_unavailable')
        if not set(selected) <= set(supported) or not set(nr_selected) <= set(nr_supported):
            raise MiWiFiProtocolError('xiaomi_selected_bands_not_supported')
        if self._provider_pool_policy is None:
            raise RuntimeError('xiaomi_provider_pool_policy_unbound')
        plmn = self._status.get('provider_plmn', '')
        policy = self._provider_pool_policy(domain='lte', provider_plmn=plmn) if plmn else None
        if policy is not None and (policy.get('schema') != 'FoundationProviderBandProfile.v1' or policy.get('owner') != '54guc.foundation.band_auto_policy' or policy.get('rat') != 'lte' or policy.get('band_auto_provider_plmn') != plmn):
            raise RuntimeError('xiaomi_provider_pool_policy_invalid')
        pool_values, _ = self._decode_fields({'xqmobile/get_cellular_band_list': reply},
            context={'provider_plmn': plmn, 'provider_bands': list(policy['bands']) if policy else []},
            addresses={'radio.configured_band_pool', 'radio.configured_band_pool_signature',
                       'radio.ca_signature', 'radio.ca_band_signature'})
        pool = pool_values['radio.configured_band_pool']
        if pool is None: raise MiWiFiProtocolError('xiaomi_pool_definition_unavailable')
        return {'supported_lte_bands': supported, 'supported_nr_bands': nr_supported,
            'selected_band_set': selected, 'selected_band_set_signature': band_signature(sorted(selected)),
            'nr5g_band_set': nr_selected,
            'configured_band_pool': pool, 'configured_band_pool_signature': pool_values['radio.configured_band_pool_signature'],
            'ca_signature': pool_values['radio.ca_signature'],
            'ca_band_signature': pool_values['radio.ca_band_signature'],
            'configured_band_pool_source': 'foundation_provider_policy_intersection',
            'configured_band_pool_status': 'derived' if pool else 'unavailable_provider_pool',
            'configured_band_pool_last_sample_ts': time.time()}

    def _sim_status(self, lane):
        """Passive SIM read; the existing ModemLive owner resolves its home ID."""
        reply = self._read('xqdtcustom/get_sim_info', lane)
        values, diagnostic = self._decode_fields({'xqdtcustom/get_sim_info': reply},
            addresses={'identity.imsi'})
        imsi = values.get('identity.imsi')
        if imsi is None:
            raise MiWiFiProtocolError('xiaomi_sim_definition_unavailable')
        self._sim_identity = imsi
        from arch4.modem_live.sim_identity import sim_network_identity
        home = sim_network_identity(imsi)['home_plmn']
        native = (self._fresh_status.get('provider_plmn', '')
                  if self._fresh_status.get('provider_plmn_source') == 'native' else '')
        status = {'imsi': imsi, 'provider_plmn': native or home,
                  'provider_plmn_source': 'native' if native else 'sim_imsi' if home else 'unavailable'}
        if diagnostic is not None: status['backend_diagnostic'] = diagnostic
        return status

    def _inspect_sim(self, lane):
        try:
            return self._sim_status(lane)
        except (MiWiFiRejected, MiWiFiProtocolError) as error:
            return {'backend_diagnostic': self._assumption_diagnostic('xqdtcustom/get_sim_info', error)}

    def _inspect_bands(self, lane):
        try:
            return self._bands(lane)
        except (MiWiFiRejected, MiWiFiProtocolError, requests.RequestException) as error:
            return {'configured_band_pool': [], 'configured_band_pool_signature': '',
                    'configured_band_pool_source': 'foundation_provider_policy_intersection',
                    'configured_band_pool_status': 'unavailable_band_inspection',
                    'configured_band_pool_last_sample_ts': time.time(),
                    'backend_diagnostic': self._assumption_diagnostic('xqmobile/get_cellular_band_list', error)}

    def _receipt(self, request, modem, status):
        if modem is not None:
            for field in ('selected_band_set', 'configured_band_pool', 'configured_band_pool_source',
                          'configured_band_pool_status', 'configured_band_pool_last_sample_ts'):
                if field in status:
                    setattr(modem, field, status[field])
            if 'configured_band_pool' in status:
                modem.ca_band_signature = band_signature(status['configured_band_pool'])
        fields = frozenset(set(status) & DECLARED_CANONICAL_SOURCE_FIELDS)
        return self._capture_backend_acquisition_receipt(request=request, modem=modem,
            acquired_cells=matrix_cells_for_canonical_fields(fields), status_override=status)

    def read_actionable_modem_input(self): return self._radio()
    radio_mode_preferred_key = 'mixed_auto'

    def _radio_mode_status(self, lane, *, required=False):
        from arch4.modem.xiaomi_radio_mode import status
        return status(self, lane, required=required)

    def read_modem_input(self):
        # Startup receipts must contain identity read inside this attempt.
        self.read_fresh_device_identity()
        modem = self._radio()
        self._fresh_status.update(self._identity)
        sim = self._inspect_sim('startup')
        self._fresh_status.update(sim)
        self._status.update(sim)
        self._fresh_status.update(self._inspect_bands('startup'))
        from arch4.modem.xiaomi_radio_mode import qualified
        if qualified(self):
            self._fresh_status.update(self._radio_mode_status('startup'))
        self._status.update(self._fresh_status)
        return modem
    def acquire_modem_input(self, request):
        if request.lane != 'startup_full': raise ValueError('xiaomi_startup_lane_required')
        return self._receipt(request, self.read_modem_input(), self._fresh_status)
    def acquire_actionable_modem_input(self, request):
        if request.lane != 'fast_live': raise ValueError('xiaomi_fast_lane_required')
        return self._receipt(request, self._radio(), self._fresh_status)
    def acquire_post_write_modem_input(self, request, stage):
        if request.lane != 'post_write' or not stage.permits_target_verification:
            raise ValueError('xiaomi_accepted_post_write_stage_required')
        if isinstance(stage.target, CoreRadioModeTarget):
            status = self._radio_mode_status('readback')
            self._status.update(status)
            return self._receipt(request, None, status)
        modem = self._radio()
        self._fresh_status.update(self._bands('readback'))
        self._status.update(self._fresh_status)
        return self._receipt(request, modem, self._fresh_status)
    def iter_acquire_live_maintenance(self, request):
        if request.lane != 'slow_live': raise ValueError('xiaomi_slow_lane_required')
        if any(not self._identity.get(field)
               for field in ('software_version','hardware_version','serial','imei')
               if field in self._device_contract['supported_fields']):
            status = self.read_fresh_device_identity()
            self._status.update(status)
            yield self._receipt(request, None, status)
        if not self._sim_identity:
            status = self._inspect_sim('slow_live')
            self._status.update(status)
            yield self._receipt(request, None, status)
        status = self._inspect_bands('slow_live')
        self._status.update(status)
        yield self._receipt(request, None, status)
        try:
            reply = self._read('xqnetwork/wifi_detail_all', 'slow_live')
            values, diagnostic = self._decode_fields({'xqnetwork/wifi_detail_all': reply},
                addresses={'slow.wifi_24ghz_enabled', 'slow.wifi_5ghz_enabled', 'slow.wifi_enabled'})
            status = {address.split('.', 1)[1]: value for address, value in values.items()
                      if value is not None}
            if diagnostic is not None:
                status['backend_diagnostic'] = diagnostic
        except (MiWiFiRejected, MiWiFiProtocolError, requests.RequestException) as error:
            # An optional Wi-Fi read cannot discard radio/settings progress or
            # turn an unknown radio into OFF. Retained values keep their age.
            status = {'backend_diagnostic': self._assumption_diagnostic('xqnetwork/wifi_detail_all', error)}
        self._status.update(status)
        yield self._receipt(request, None, status)
        from arch4.modem.xiaomi_radio_mode import qualified, iter_status
        if qualified(self):
            try:
                for status in iter_status(self, 'slow_live', use_session_imsi=True):
                    if status is None:
                        yield None
                    else:
                        self._status.update(status)
                        yield self._receipt(request, None, status)
            except (RuntimeError, KeyError, TypeError, ValueError):
                self._status['radio_mode'] = None
                yield self._receipt(request, None, {'radio_mode': None})
        else:
            # Read-only/LTE profiles still publish APN and operator settings.
            # Their reads must not depend on permission to change radio mode.
            try:
                settings = yield from self._iter_network_settings_observation()
                status = {'network_settings': settings.observation}
            except (MiWiFiRejected, MiWiFiProtocolError, requests.RequestException) as error:
                status = {'backend_diagnostic': self._assumption_diagnostic('network_settings', error)}
            self._status.update(status)
            yield self._receipt(request, None, status)
    def acquire_live_maintenance(self, request): raise RuntimeError('xiaomi_stepwise_maintenance_required')
    def refresh_live_maintenance(self): raise RuntimeError('xiaomi_stepwise_maintenance_required')

    @staticmethod
    def sim_identity(reply):
        return sim_identity(reply)

    def _read_optional_network_setting(self, endpoint, lane):
        try:
            return self._read(endpoint, lane)
        except (MiWiFiRejected, MiWiFiProtocolError, requests.RequestException):
            # Authentication, SIM identity and slot expiry still propagate.
            return {}

    def _iter_network_settings_observation(self):
        from arch4.core_contracts.network_settings import subscription_key
        replies={}
        imsi=''
        if not subscription_key(self._sim_identity):
            sim=self._read('xqdtcustom/get_sim_info','slow_live');yield None
            replies['xqdtcustom/get_sim_info']=sim
            imsi=self._definition.state_value('subscription_imsi',replies)
            if not subscription_key(imsi):
                raise MiWiFiProtocolError('xiaomi_apn_subscription_unavailable')
            self._sim_identity=imsi
        cfg = self._read_optional_network_setting('xqmobile/get_mobile_net_cfg', 'slow_live'); yield None
        profiles = self._read_optional_network_setting('xqmobile/get_apn_info', 'slow_live'); yield None
        replies.update({'xqmobile/get_mobile_net_cfg':cfg,'xqmobile/get_apn_info':profiles})
        replies['xqmobile/get_search_net_mode'] = self._read_optional_network_setting('xqmobile/get_search_net_mode', 'slow_live')
        yield None
        values, _ = self._decode_fields(replies,
            context={'session_subscription':subscription_key(self._sim_identity)},
            addresses={'slow.network_settings'})
        observation = values['slow.network_settings']
        if observation is None:
            raise MiWiFiProtocolError('xiaomi_apn_definition_unavailable')
        return NetworkSettingsRead(validate_network_observation(observation), imsi)

    def prepare_read_diagnostics(self):
        from arch4.modem.read_diagnostics import ReadDiagnosticUnavailable
        from arch4.modem.xiaomi_read_diagnostics import XiaomiReadDiagnosticAdapter
        if not self.is_connected():
            raise ReadDiagnosticUnavailable('Connect to the router before running read diagnostics.')
        return XiaomiReadDiagnosticAdapter(self)

    def execute_physical_stage(self, plan):
        if not isinstance(plan, ModemLivePhysicalStagePlan): raise TypeError('xiaomi_typed_stage_required')
        capability = ('supports_radio_mode' if isinstance(plan.target, CoreRadioModeTarget) else
                      'supports_nr_band_lock' if isinstance(plan.target, CoreNrBandTarget) else 'supports_band_selection')
        if not self._device_contract['capabilities'].get(capability, False):
            raise RuntimeError('xiaomi_control_target_not_qualified')
        if isinstance(plan.target, CoreRadioModeTarget):
            self.read_fresh_device_identity()
            from arch4.modem.xiaomi_radio_mode import mode_fields
            fields = mode_fields(self, plan)
            started = time.time()
            self._last_write_started = self._last_write_returned = 0.
            outcome, reason = 'accepted', 'xiaomi_mode_accepted_for_fresh_readback'
            try:
                self._miwifi.write_mode(fields)
            except MiWiFiRejected as exc:
                outcome, reason = 'rejected', f'xiaomi_mode_rejected:{exc.code}'
            except (requests.RequestException, MiWiFiProtocolError, MiWiFiSessionExpired):
                outcome, reason = 'response_unavailable', 'xiaomi_mode_reply_unavailable_no_replay'
            return ModemLivePhysicalStageResult(plan=plan,
                request_dispatched_at=self._last_write_started or started,
                transport_returned_at=max(time.time(), self._last_write_returned), outcome=outcome, reason=reason)
        if not isinstance(plan.target, (CoreBandSetTarget, CoreNrBandTarget)):
            raise RuntimeError('xiaomi_control_target_not_qualified')
        self.read_fresh_device_identity()
        if isinstance(plan.target, CoreNrBandTarget) and plan.preceding_mode is not None:
            if self._radio_mode_status('event_write', required=True)['radio_mode'] != plan.preceding_mode.target.configuration():
                raise RuntimeError('xiaomi_nr_enabled_mode_configuration_changed')
        current = self._bands('event_write')
        nr = isinstance(plan.target, CoreNrBandTarget)
        requested = list(plan.target.nr_bands if nr else plan.target.bands)
        supported = current['supported_nr_bands' if nr else 'supported_lte_bands']
        if not requested or not set(requested) <= set(supported):
            raise ValueError('xiaomi_requested_bands_empty_or_unsupported')
        lte_bands = current['selected_band_set'] if nr else requested
        nr_bands = requested if nr else current['nr5g_band_set']
        if not lte_bands or not nr_bands:
            raise ValueError('xiaomi_other_rat_selection_missing')
        started = time.time()
        self._last_write_started = self._last_write_returned = 0.0
        outcome, reason = 'accepted', 'accepted_for_fresh_readback'
        try:
            endpoint,fields=self._definition.prepare_form_write('set_cellular_band',
                {'lte':lte_bands,'nr':nr_bands})
            self._miwifi.write_form(endpoint,fields)
        except MiWiFiRejected as exc:
            outcome, reason = 'rejected', f'xiaomi_band_command_rejected:{exc.code}'
        except (requests.RequestException, MiWiFiProtocolError, MiWiFiSessionExpired):
            outcome, reason = 'response_unavailable', 'xiaomi_write_response_unavailable_no_replay'
        return ModemLivePhysicalStageResult(plan=plan, request_dispatched_at=self._last_write_started or started,
            transport_returned_at=max(time.time(), self._last_write_returned), outcome=outcome, reason=reason)

    def get_live_acquisition_contract(self):
        return LiveAcquisitionContract(acquisition_class='aligned_split', clocked_register_read=True,
            live_register_interval_s=self._profile.fast_live_interval_s, live_register_stale_s=self._profile.fast_live_stale_s,
            profile_name=self._definition.profile_id, has_explicit_actionable_snapshot=True, has_explicit_maintenance_refresh=True,
            io_serial_required=True, startup_parallel_allowed=False, runtime_action_requires_io_lock=True,
            generic_neighbor_scan_enabled=False, maintenance_stepwise=True, maintenance_completion_ordered=True,
            maintenance_holds_io_lock=False, rtt_holds_io_lock=False,
            fast_read_groups=('radio_status', 'rf_quality', 'nr_status'), slow_read_groups=('sim_identity', 'network_mode'),
            static_capability_groups=('device_identity', 'firmware', 'supported_bands'),
            control_write_groups=('band_selection', 'nr_band_lock', 'login_token'), export_groups=('sidecars', 'support_bundle', 'history'))
