"""Conditional stock CB0401V2 LuCI dialect; no root bridge or login fallback."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlencode
from types import MappingProxyType

MAX_REPLY = 1024 * 1024
PUBLIC_READS = frozenset({'xqsystem/init_info'})
READ_METHODS = MappingProxyType({
    'xqsystem/init_info': 'GET',
    'xqdtcustom/cpe_detect': 'GET',
    'xqdtcustom/simple_cpe_detect': 'GET',
    'xqdtcustom/newstatus': 'GET',
    'misystem/status': 'GET',
    'misystem': 'GET',
    'xqnetwork/wan_info': 'GET',
    'xqnetwork/wifi_detail_all': 'GET',
    'xqdtcustom/get_sim_info': 'GET',
    'xqdtcustom/get_mobile_net_info': 'GET',
    'xqmobile/get_mobile_net_info': 'GET',
    'xqmobile/get_mobile_net_cfg': 'GET',
    'xqmobile/get_apn_info': 'GET',
    'xqmobile/get_cellular_band_list': 'GET',
    # The stock frontend sends an empty POST for this read, not a setting.
    'xqmobile/get_search_net_mode': 'POST',
    'xqmobile/check_sim_status': 'GET',
    'xqmobile/get_data_usage_info': 'GET',
    'xqmobile/get_data_plan_info': 'GET',
})
WRITES = frozenset({'xqmobile/set_cellular_band'})
MODE_WRITE = 'xqmobile/set_mobile_net_cfg'
ASSUMPTION_ID = 'CB0401V2_3.0.88_stock_3.0.85_reference_v1'


class MiWiFiProtocolError(ValueError):
    pass


class MiWiFiRejected(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('xiaomi_api_rejected')


class MiWiFiSessionExpired(RuntimeError):
    pass


def password_digest(password, key, nonce, mode):
    if type(mode) is not int or mode not in {0, 1}:
        raise MiWiFiProtocolError('xiaomi_login_hash_mode_unknown')
    digest = hashlib.sha256 if mode == 1 else hashlib.sha1
    inner = digest((password + key).encode('utf8')).hexdigest()
    return digest((nonce + inner).encode('utf8')).hexdigest()


def decode_reply(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_REPLY:
        raise MiWiFiProtocolError('xiaomi_reply_size_or_type_invalid')
    try:
        result = json.loads(raw.decode('utf8'))
    except (ValueError, UnicodeError) as exc:
        raise MiWiFiProtocolError('xiaomi_reply_not_json') from exc
    if not isinstance(result, dict) or type(result.get('code')) is not int:
        raise MiWiFiProtocolError('xiaomi_reply_code_invalid')
    return result


class MiWiFiSession:
    """One stock authentication policy and explicit read/write vocabularies.

    Missing newEncryptMode means SHA1 in the inspected stock frontend. This
    is the declared stock dialect default, not a failed-login alternate try.
    An explicit unknown mode fails before sending any password hash.
    """
    def __init__(self, request, username, password):
        self._request = request
        self._username = username
        self._password = password
        self.token = ''
        self.authentication_observation = {}

    @property
    def authenticated(self):
        return bool(self.token)

    def clear(self):
        self.token = ''

    def login(self):
        self.clear()
        page = self._request('GET', '/cgi-bin/luci/web', None, 'boot_static', 'login_metadata')
        try:
            html = page.decode('utf8')
        except UnicodeError as exc:
            raise MiWiFiProtocolError('xiaomi_login_page_not_utf8') from exc
        keys = set(re.findall(r'\bkey\s*:\s*[\"\']([a-fA-F0-9]{32})[\"\']', html))
        devices = set(re.findall(r'\bdeviceId\s*=\s*[\"\']([a-fA-F0-9:]{17})[\"\']', html))
        if len(keys) != 1 or len(devices) != 1:
            raise MiWiFiProtocolError('xiaomi_stock_login_metadata_missing_or_ambiguous')
        info = self.read('xqsystem/init_info', public=True, lane='boot_static')
        mode = info.get('newEncryptMode', 0)
        if mode in ('0', '1'):
            mode = int(mode)
        self.authentication_observation = {
            'assumption_id': ASSUMPTION_ID,
            'hash_mode': mode if type(mode) is int and mode in {0, 1} else 'unknown',
            'hash_mode_source': 'native_init_info' if 'newEncryptMode' in info else 'stock_frontend_default',
            'login_metadata_present': True,
        }
        nonce = f'0_{next(iter(devices))}_{int(time.time())}_{secrets.randbelow(10000)}'
        payload = {'username': self._username, 'password': password_digest(self._password, next(iter(keys)), nonce, mode),
                   'logtype': '2', 'nonce': nonce}
        reply = decode_reply(self._request('POST', '/cgi-bin/luci/api/xqsystem/login',
            urlencode(payload).encode('utf8'), 'boot_static', 'login'))
        if reply['code'] != 0:
            raise MiWiFiRejected(reply['code'])
        token = reply.get('token')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', token):
            raise MiWiFiProtocolError('xiaomi_login_token_invalid')
        self.token = token

    def _api(self, method, endpoint, body, lane, public=False, *, query=None):
        if public and endpoint not in PUBLIC_READS:
            raise MiWiFiProtocolError('xiaomi_public_read_not_registered')
        if not public and not self.token:
            raise MiWiFiSessionExpired('xiaomi_session_missing')
        prefix = '/cgi-bin/luci' if public else '/cgi-bin/luci/;stok=' + self.token
        path = prefix + '/api/' + endpoint
        if query is not None:
            path += '?' + urlencode(query)
        reply = decode_reply(self._request(method, path, body, lane, endpoint))
        if reply['code'] in {401, 403}:
            self.clear()
            raise MiWiFiSessionExpired('xiaomi_session_expired')
        if reply['code'] != 0:
            raise MiWiFiRejected(reply['code'])
        return reply

    def read(self, endpoint, *, lane='fast_live', public=False):
        if endpoint not in READ_METHODS:
            raise MiWiFiProtocolError('xiaomi_read_path_not_registered')
        method = READ_METHODS[endpoint]
        return self._api(method, endpoint, b'' if method == 'POST' else None, lane, public)

    def write_form(self, endpoint, fields):
        if endpoint not in WRITES or not isinstance(fields,dict)or not fields:
            raise MiWiFiProtocolError('xiaomi_write_not_qualified')
        if any(not isinstance(k,str)or not isinstance(v,str)or len(v)>4096 for k,v in fields.items()):
            raise MiWiFiProtocolError('xiaomi_write_form_invalid')
        return self._api('POST', endpoint, urlencode(fields).encode('ascii'), 'event_write')

    def write_mode(self, fields):
        if (not isinstance(fields, dict) or set(fields) != {'networktype', 'networkdata', 'networkroam'}
                or fields['networktype'] not in {'auto', 'nsa', 'sa', '4g'}
                or fields['networkdata'] not in {'0', '1'} or fields['networkroam'] not in {'0', '1'}):
            raise MiWiFiProtocolError('xiaomi_mode_write_not_qualified')
        return self._api('GET', MODE_WRITE, None, 'event_write', query=fields)
