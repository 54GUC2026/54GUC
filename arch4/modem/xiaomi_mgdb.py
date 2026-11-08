"""CB0501 stock MGDB dialect; shared owners, separate native wire contract.

Login/identity/Wi-Fi keys come from the operator's exact CB0501 client. Owner
replies now confirm the basic RF/cell, APN/SIM and traffic keys. The optional
carrier route and typed setting writes remain related-stock mappings; writes
require matching stored readback, not just a successful acknowledgement.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time

import requests

from arch4.interfaces import ModemInput
from arch4.modem.base import AuthenticationError, SessionExpiredError, TargetGuardError, evaluate_target_guard
from arch4.modem.network_inspection import NetworkSettingsRead
from arch4.modem.xiaomi_miwifi import XiaomiMiWiFiModem, number
from arch4.modem.xiaomi_miwifi_protocol import MAX_REPLY, MiWiFiProtocolError, MiWiFiRejected
from arch4.modem_live.profile_definition.native_inputs import native_view, native_status_code
from arch4.modem_live.profile_definition.expressions import RuleError
from arch4.modem_live.startup_trace import traced_http
from arch4.core_contracts.network_settings import subscription_key, validate_network_observation
from arch4.core_contracts.command_targets import CoreBandSetTarget, CoreBandAutoTarget, CoreCellLockTarget
from arch4.modem_live.physical_command import ModemLivePhysicalStagePlan, ModemLivePhysicalStageResult

READ_PATHS = {
    '/action/get_mgdb_params': 'POST', '/action/get_signal_info': 'POST',
    '/action/get_mobile_network_info': 'POST', '/goform/get_sim_info': 'POST',
    '/action/get_dialup_profile': 'POST', '/action/get_mobile_connection_cfg': 'POST',
    '/action/mnet_get_ca_list': 'POST',
}
WRITE_PATHS = {
    '/action/mnet_set_band_lock_switch': 'mgdb:band_lock',
    '/action/mnet_set_celllock_switch': 'mgdb:cell_lock',
}


def mgdb_band(value):
    if type(value) not in (str, int):
        return None
    match = re.fullmatch(r'(?:LTE[ \t]*)?B?[ \t]*([1-9][0-9]{0,2})', str(value).strip().upper())
    return int(match[1]) if match and int(match[1]) <= 256 else None


def mgdb_boolean(value):
    """Equivalent explicit switch values; unknown is never guessed false."""
    if type(value) is bool:
        return value
    if type(value) in (int, float):
        return bool(value) if value in (0, 1) else None
    if not isinstance(value, str):
        return None
    token = value.strip().lower().replace('-', '_').replace(' ', '_')
    return {'0': False, 'false': False, 'no': False, 'off': False,
        'disable': False, 'disabled': False, 'ap_disable': False,
        '1': True, 'true': True, 'yes': True, 'on': True,
        'enable': True, 'enabled': True, 'ap_enable': True}.get(token)


def mgdb_carrier_rows(reply):
    """Related stock WebUI: LTE band and bandwidth in resource blocks.

    Optional child failures do not erase another carrier's valid readings.
    Unknown channel/PCI/state is not manufactured from list order.
    """
    rows = reply.get('mnet_ca_list') if isinstance(reply, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        return None
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        band = mgdb_band(row.get('mnet_ca_band'))
        if band is None:
            continue
        blocks = number(row.get('mnet_ca_bandwidth'), 1, 100, True)
        width = {6: 1.4, 15: 3., 25: 5., 50: 10., 75: 15., 100: 20.}.get(blocks)
        result.append(dict(rat='LTE', band=band, dl_bandwidth_mhz=width))
    return result or None


def mgdb_network_observation(replies, session_subscription=''):
    profiles = replies.get('apn') or {}
    rows = profiles.get('profileList')
    active = [row for row in rows if isinstance(row, dict)
              and type(row.get('index')) in (int, str)
              and type(profiles.get('curid')) in (int, str)
              and str(row['index']) == str(profiles['curid'])] if isinstance(rows, list) else []
    apn = None
    if len(active) == 1:
        row = active[0]
        if all(isinstance(row.get(key, ''), str) for key in ('name', 'apn', 'username')):
            apn = dict(profile_id=str(row['index']), name=row.get('name', ''),
                apn=row.get('apn', ''), username=row.get('username', ''),
                authentication={'0': 'none', '1': 'pap', '2': 'chap', '3': 'auto',
                    'none': 'none', 'pap': 'pap', 'chap': 'chap', 'auto': 'auto'}.get(str(row.get('authmode', '')).strip().lower()),
                ip_protocol={'1': 'ipv4', '2': 'ipv6', '3': 'ipv4v6',
                    'ipv4': 'ipv4', 'ipv6': 'ipv6', 'ipv4v6': 'ipv4v6',
                    'ipv4/ipv6': 'ipv4v6', 'ipv4+ipv6': 'ipv4v6'}.get(str(row.get('iptype', '')).strip().lower()),
                editable=False)
    cfg = replies.get('connection') or {}
    return validate_network_observation(dict(schema='NetworkSettingsObservation.v1',
        subscription_key=session_subscription, selection=None, target_plmn=None,
        roaming_enabled=mgdb_boolean(cfg.get('roamingswitch')), apn=apn))


class MgdbSession:
    def __init__(self, request, username, password):
        self._request, self._username, self._password = request, username, password
        self.authenticated = False

    def clear(self):
        self.authenticated = False

    def call(self, method, path, payload=None, lane='fast_live'):
        raw = self._request(method, path, payload, lane)
        try:
            row = native_view(json.loads(raw.decode('utf-8')))
            code = native_status_code(row.get('retcode') if isinstance(row, dict) else None)
            if code in (401, 403):
                self.clear()
                raise SessionExpiredError('Xiaomi session expired. Reconnect to the router.')
            if code != 0:
                raise MiWiFiRejected(code)
            data = row.get('data', row)
            if not isinstance(data, dict):
                raise MiWiFiProtocolError('cb0501_reply_data_invalid')
            return data
        except (UnicodeError, json.JSONDecodeError, RuleError) as exc:
            raise MiWiFiProtocolError('cb0501_reply_not_json') from exc

    def login(self):
        self.clear()
        # Exact operator CB0501 client. No pre-login status probe or dialect retry.
        def digest(value):
            return hmac.new(b'0123456789', value.encode('ascii'), hashlib.md5).hexdigest()
        self.call('POST', '/goform/login', dict(username=digest(self._username), password=digest(self._password)), 'boot_static')
        confirmed = self.call('GET', '/goform/get_login_info', lane='boot_static')
        try:
            user = confirmed.get('loginUser')
            # Owner capture6Nov: this field is HMAC-MD5, with the friendly
            # name in loginAlias. Never use that alias alone as account proof.
            matching_user = isinstance(user, str) and (user == self._username or
                re.fullmatch(r'[0-9a-fA-F]{32}', user) is not None and
                hmac.compare_digest(user.lower(), digest(self._username)))
            logged_in = native_status_code(confirmed.get('loginStatus')) == 1
        except RuleError:
            matching_user = logged_in = False
        if not matching_user or not logged_in:
            raise AuthenticationError('Xiaomi did not confirm the signed-in user.')
        self.authenticated = True


class XiaomiMgdbModem(XiaomiMiWiFiModem):
    """Reuse the existing Xiaomi receipt/identity/authority owners, not LuCI."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self._definition.snapshot().get('parameters', {}).get('native_protocol') != 'xiaomi_mgdb':
            raise ValueError('cb0501_explicit_profile_required')
        self._http.headers['Content-Type'] = 'application/json'
        self._miwifi = MgdbSession(self._wire, kwargs.get('username', 'admin'), kwargs.get('password', ''))
        self._unavailable_reads = set()
        self._pending_read_diagnostic = None

    def _wire(self, method, path, payload, lane):
        login = path == '/goform/login' and method == 'POST'
        confirm = path == '/goform/get_login_info' and method == 'GET'
        reads = [r for r in self._definition.requests
                 if r['path'] == path and r['method'] == method]
        writing = method == 'POST' and path in WRITE_PATHS
        if writing:
            capability = 'supports_cell_lock' if WRITE_PATHS[path] == 'mgdb:cell_lock' else 'supports_band_selection'
            if (not self._device_contract['capabilities'].get(capability, False)
                    or self._physical_request_authority.current_lane(default=lane) != 'event_write'
                    or WRITE_PATHS[path] not in self._definition.writes):
                raise MiWiFiProtocolError('cb0501_write_not_authorized')
        if not (login or confirm or writing or reads and READ_PATHS.get(path) == method):
            raise MiWiFiProtocolError('cb0501_wire_read_not_registered')
        if not (login or confirm or self._miwifi.authenticated):
            raise SessionExpiredError('Xiaomi session is not authenticated.')
        if path == '/action/get_mgdb_params' and not any(payload == {'keys': r.get('keys')} for r in reads):
            raise MiWiFiProtocolError('cb0501_mgdb_keys_not_registered')
        actual = self._physical_request_authority.current_lane(default=lane)
        timeout = ((self._profile.maintenance_connect_timeout_s, self._profile.maintenance_read_timeout_s)
            if actual == 'slow_live' else
            (self._profile.fast_signal_connect_timeout_s, self._profile.fast_signal_read_timeout_s))
        with self._physical_request_authority.request(lane=actual, operation='xiaomi_mgdb:' + path.rsplit('/', 1)[-1]):
            if writing:
                self._last_write_started = time.time()
            try:
                response = traced_http(self._http.get if method == 'GET' else self._http.post, self._base_url + path,
                    json=payload, allow_redirects=False, stream=True,
                    timeout=self.background_read_timeout(timeout))
            except requests.Timeout as exc:
                if self._physical_request_authority.remaining_read_s() is not None:
                    from arch4.modem_live.physical_request_authority import BackgroundReadSlotExpired
                    raise BackgroundReadSlotExpired() from exc
                raise
            try:
                if response.status_code in (301, 302, 303, 307, 308, 401, 403):
                    self._miwifi.clear()
                    raise SessionExpiredError('Xiaomi session expired. Reconnect to the router.')
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(16384):
                    size += len(chunk)
                    if size > MAX_REPLY:
                        raise MiWiFiProtocolError('cb0501_reply_too_large')
                    chunks.append(chunk)
                return b''.join(chunks)
            finally:
                response.close()
                if writing:
                    self._last_write_returned = time.time()

    def _read(self, endpoint, lane='fast_live'):
        request = self._definition.request(endpoint, protocol='xiaomi_mgdb_json')
        return self._miwifi.call(request['method'], request['path'],
            {'keys': request['keys']} if 'keys' in request else None, lane)

    def _optional(self, endpoint, lane):
        if endpoint in self._unavailable_reads:
            return {}
        try:
            return self._read(endpoint, lane)
        except SessionExpiredError:
            raise
        except (MiWiFiRejected, MiWiFiProtocolError, requests.RequestException) as exc:
            if isinstance(exc, MiWiFiRejected) and exc.code == 404 or (
                    isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 404):
                self._unavailable_reads.add(endpoint)
            self._pending_read_diagnostic = self._assumption_diagnostic(endpoint, exc)
            self._status['backend_diagnostic'] = self._pending_read_diagnostic
            return {}

    def connect(self):
        self._qualified = False
        self._identity, self._status, self._fresh_status = {}, {}, {}
        self._sim_identity = ''
        self._unavailable_reads.clear()
        self._pending_read_diagnostic = None
        self._http.cookies.clear()
        try:
            self._miwifi.login()
            self.read_fresh_device_identity()
            self._qualified = True
            return True
        except MiWiFiRejected as exc:
            self._miwifi.clear()
            raise AuthenticationError('Xiaomi refused the login. Check the router credentials.') from exc
        except Exception:
            self._miwifi.clear()
            raise

    def read_fresh_device_identity(self):
        row = self._read('device', 'boot_static')
        addresses = {'identity.model', 'identity.firmware', 'identity.software_version',
                     'identity.hardware_version', 'identity.serial', 'identity.imei'}
        values, _ = self._decode_fields({'device': row}, addresses=addresses)
        identity = {k.split('.', 1)[1]: v for k, v in values.items() if v is not None}
        identity.update(vendor='Xiaomi', physical_identity_source='action/get_mgdb_params')
        identity.setdefault('serial', '')
        identity.setdefault('imei', '')
        if (identity.get('model') != 'CB0501' or not re.fullmatch(
                self._device_contract['firmware_pattern'], identity.get('firmware') or '')):
            from arch4.observation import allow_related_model
            if not allow_related_model(identity, 'xiaomi_exact_firmware_identity_mismatch'):
                raise TargetGuardError('The router does not match the CB0501 stock profile.', reason='xiaomi_exact_firmware_identity_mismatch')
        from arch4.modem.operator_identity import operator_serial_text, require_operator_serial
        if not operator_serial_text(identity['serial']) and not operator_serial_text(identity['imei']):
            identity['serial'] = require_operator_serial(self._expected['serial'], self._startup_identity_prompt)
        for expected in (self._expected, self._identity):
            ok, reason = evaluate_target_guard(expected_model=expected.get('model', ''),
                expected_serial=expected.get('serial', ''), expected_imei=expected.get('imei', ''),
                expected_firmware=expected.get('firmware', ''), observed=identity)
            if not ok:
                raise TargetGuardError('The router identity changed.', reason=reason)
        self._identity = identity
        return dict(identity)

    def _status_values(self, replies, *, context=None):
        if 'carriers' in replies:
            context = {**(context or {}), 'serving_band': self._status.get('serving_band')}
        values, diagnostic = self._decode_fields(replies, context=context)
        status = {address.split('.', 1)[1]: value for address, value in values.items() if value is not None}
        if diagnostic is not None:
            status['backend_diagnostic'] = diagnostic
        if self._pending_read_diagnostic is not None:
            status['backend_diagnostic'] = self._pending_read_diagnostic
            self._pending_read_diagnostic = None
        network = replies.get('network')
        if (isinstance(network,dict) and isinstance(network.get('mnet_operator_name'),str)
                and network['mnet_operator_name'].strip() and not status.get('provider_plmn')):
            # A freshly named but unresolved operator cannot borrow the prior
            # operator's policy. A missing reply is not a new positive binding.
            status.update(provider_plmn=None,provider_plmn_source='unavailable')
        if status.get('provider_plmn'):
            status['provider_plmn_source'] = (self._definition.state_value('provider_plmn_source',replies)
                if self._definition.snapshot().get('state',{}).get('provider_plmn_source') else 'native')
        if status.get('carrier_rows'):
            candidates = [r for r in status['carrier_rows'] if r['band']==self._status.get('serving_band')]
            if len(candidates)==1:
                status['carrier_details'] = [{**r,'is_pcell':r is candidates[0]} for r in status['carrier_rows']]
            else:
                status['carrier_details'] = None
        elif 'carriers' in replies:
            status['carrier_details'] = None
        if 'network' in replies and self._definition.has_read('radio.selected_band_set'):
            plmn = (status.get('provider_plmn') if 'provider_plmn' in status
                else self._status.get('provider_plmn',''))
            if not plmn:
                for key in ('configured_band_pool','configured_band_pool_signature',
                        'ca_signature','ca_band_signature','carrier_count'):
                    self._status.pop(key,None)
            policy = self._provider_pool_policy(domain='lte', provider_plmn=plmn) if plmn and self._provider_pool_policy else None
            if policy is not None and (policy.get('schema') != 'FoundationProviderBandProfile.v1'
                    or policy.get('owner') != '54guc.foundation.band_auto_policy'
                    or policy.get('rat') != 'lte' or policy.get('band_auto_provider_plmn') != plmn):
                raise RuntimeError('xiaomi_provider_pool_policy_invalid')
            pool, _ = self._decode_fields(replies,
                context={'provider_plmn': plmn, 'provider_bands': list(policy['bands']) if policy else []},
                addresses={'radio.configured_band_pool', 'radio.configured_band_pool_signature',
                    'radio.ca_signature', 'radio.ca_band_signature','radio.carrier_count'})
            status.update((address.split('.', 1)[1], value) for address, value in pool.items() if value is not None)
            status['configured_band_pool_source'] = 'foundation_provider_policy_intersection'
            status['configured_band_pool_status'] = 'derived' if status.get('configured_band_pool') else 'unavailable_provider_pool'
            status['configured_band_pool_last_sample_ts'] = time.time()
            cell = self._definition.state_value('cell_lock_configuration', replies)
            status['cell_locked'] = cell.get('enabled') if isinstance(cell, dict) else None
        self._status.update(status)
        return status

    def _radio_read(self):
        replies = {name: self._optional(name, 'fast_live') for name in ('traffic', 'signal')}
        self._fresh_status = self._status_values(replies)
        modem = ModemInput.safe_defaults(traffic_valid=False, session_uptime_source='unsupported')
        for name, value in self._fresh_status.items():
            if name in modem.__dataclass_fields__:
                setattr(modem, name, value)
        return modem

    def _sim_status(self, lane):
        if self._sim_identity:
            return {}  # Static for this authenticated session; no renewed timestamp.
        row = self._optional('sim', lane)
        status = self._status_values({'sim': row})
        self._sim_identity = status.get('imsi', '')
        return status

    def read_modem_input(self):
        self.read_fresh_device_identity()
        modem = self._radio_read()
        status = self._status_values({'network': self._optional('network', 'startup')})
        self._fresh_status.update(status)
        self._fresh_status.update(self._sim_status('startup'))
        self._fresh_status.update(self._identity)
        return modem

    def iter_acquire_live_maintenance(self, request):
        if request.lane != 'slow_live':
            raise ValueError('xiaomi_slow_lane_required')
        for endpoint in ('network', 'wifi', 'wan', 'carriers'):
            if endpoint == 'carriers' and not any(r['reply']=='carriers' for r in self._definition.requests):
                continue  # Older external profiles keep their original reads.
            row = self._optional(endpoint, 'slow_live')
            # Confirmed absence invalidates the session SIM cache.
            if endpoint == 'wan' and str(row.get('mnet_sim_status', '')).strip().lower() in {'absent', 'no sim'}:
                self._sim_identity = ''
            yield self._receipt(request, None, self._status_values({endpoint: row}))
        if not self._sim_identity:
            yield self._receipt(request, None, self._sim_status('slow_live'))
        settings = yield from self._iter_network_settings_observation()
        status = {'network_settings': settings.observation}
        self._status.update(status)
        yield self._receipt(request, None, status)

    def _iter_network_settings_observation(self):
        replies = {}
        for endpoint in ('apn', 'connection'):
            replies[endpoint] = self._optional(endpoint, 'slow_live')
            yield None
        if not any(replies.values()):
            return NetworkSettingsRead(None)
        values, _ = self._decode_fields(replies, context={'session_subscription': subscription_key(self._sim_identity)},
            addresses={'slow.network_settings'})
        return NetworkSettingsRead(values.get('slow.network_settings'))

    def execute_physical_stage(self, plan):
        if not isinstance(plan, ModemLivePhysicalStagePlan):
            raise TypeError('xiaomi_typed_stage_required')
        target = plan.target
        capability = ('supports_cell_lock' if isinstance(target, CoreCellLockTarget)
            else 'supports_band_lock' if isinstance(target, CoreBandSetTarget) and target.command_kind == 'BAND_LOCK'
            else 'supports_band_selection')
        if (not isinstance(target, (CoreBandSetTarget, CoreBandAutoTarget, CoreCellLockTarget))
                or not self._device_contract['capabilities'].get(capability, False)
                or isinstance(target, CoreBandAutoTarget) and target.domain != 'lte_allocated_auto'):
            raise RuntimeError('xiaomi_control_target_not_qualified')
        if self._physical_request_authority.current_lane(default='') != 'event_write':
            raise MiWiFiProtocolError('cb0501_write_not_authorized')
        self.read_fresh_device_identity()
        if isinstance(target, CoreCellLockTarget):
            name = 'mgdb:cell_lock'
            fields = dict(mnet_cell_lock_rat='13' if target.unlock else '3', mnet_cell_lock_type='1')
            if not target.unlock:
                fields.update(mnet_cell_lock_arfcn=str(target.earfcn), mnet_cell_lock_pci=str(target.pci))
        else:
            name = 'mgdb:band_lock'
            # The typed operator target is already validated. Let the router
            # decide supported bands; an empty optional inventory is no veto.
            fields = dict(mnet_band_lock_type='unlock,normal' if isinstance(target, CoreBandAutoTarget) else 'lock,normal',
                mnet_band_lock_info='' if isinstance(target, CoreBandAutoTarget) else 'lte:' + ','.join(str(n) for n in target.bands))
        fields = self._definition.prepare_native_write_fields(name, fields)
        path = next(path for path, operation in WRITE_PATHS.items() if operation == name)
        self._last_write_started = self._last_write_returned = 0.0
        started = time.time()
        outcome, reason = 'accepted', 'accepted_for_fresh_readback'
        try:
            self._miwifi.call('POST', path, fields, 'event_write')
        except MiWiFiRejected as exc:
            outcome, reason = 'rejected', f'cb0501_control_rejected:{exc.code}'
        except (requests.RequestException, MiWiFiProtocolError, SessionExpiredError):
            outcome, reason = 'response_unavailable', 'cb0501_control_reply_unavailable_no_replay'
        detail = {'native_write_outcome': outcome}
        if outcome != 'rejected':
            # MGDB has explicit configuration readback. A serving-cell match
            # alone is not proof that a lock was saved (it may already be the
            # serving cell). Confirm here before the common Core can accept it.
            time.sleep(self._profile.post_write_readback_delay_s)
            try:
                confirmed = self._configuration_matches(target, {'network': self._read('network', 'event_write')})
            except (requests.RequestException, MiWiFiProtocolError, MiWiFiRejected, SessionExpiredError):
                confirmed = False
            detail.update(configured_readback_confirmed=confirmed, configured_readback_at=time.time())
            if not confirmed:
                outcome, reason = 'response_unavailable', 'cb0501_configuration_not_confirmed_no_replay'
        return ModemLivePhysicalStageResult(plan=plan,
            request_dispatched_at=self._last_write_started or started,
            transport_returned_at=max(time.time(), self._last_write_returned), outcome=outcome, reason=reason, detail=detail)

    def _configuration_matches(self, target, replies):
        if isinstance(target, CoreCellLockTarget):
            cell = self._definition.state_value('cell_lock_configuration', replies)
            return (isinstance(cell, dict) and cell.get('enabled') is (not target.unlock)
                and (target.unlock or cell.get('pci') == target.pci and cell.get('earfcn') == target.earfcn))
        if isinstance(target, CoreBandSetTarget):
            selected = self._definition.state_value('selected_lte_bands', replies)
            return selected is not None and set(selected) == set(target.bands)
        if isinstance(target, CoreBandAutoTarget):
            return self._definition.state_value('mgdb_auto_released', replies) is True
        return False

    def acquire_post_write_modem_input(self, request, stage):
        if request.lane != 'post_write' or not stage.permits_target_verification:
            raise ValueError('xiaomi_accepted_post_write_stage_required')
        network = self._read('network', 'readback')
        replies = {'network': network}
        target = stage.target
        if not self._configuration_matches(target, replies):
            raise MiWiFiProtocolError('cb0501_configuration_not_confirmed')
        modem = self._radio()
        self._fresh_status.update(self._status_values(replies))
        self._status.update(self._fresh_status)
        return self._receipt(request, modem, self._fresh_status)

    def prepare_read_diagnostics(self):
        from arch4.modem.read_diagnostics import ReadDiagnosticUnavailable
        raise ReadDiagnosticUnavailable('CB0501 uses its stock MGDB reads; LuCI diagnostics do not apply.')
