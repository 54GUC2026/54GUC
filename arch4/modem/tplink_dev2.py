"""Selected TP-Link encrypted DEV2 adapter. No other TP-Link dialect fallback."""
from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import math
import re
import time
from urllib.parse import urlsplit

import requests

from arch4.modem_live.profile_diagnostics import decode_native_profile
from arch4.core_contracts.command_targets import CoreRadioModeTarget, validate_radio_mode_observation
from arch4.interfaces import ModemInput, CarrierInfo, band_signature
from arch4.modem.base import (ModemInterface, LiveAcquisitionContract,
    AuthenticationError, SessionExpiredError, ModemTransportUnavailable, TargetGuardError, evaluate_target_guard,
    build_public_modem_status)
from arch4.modem.backend_acquisition import BackendAcquisitionRequest, matrix_cells_for_canonical_fields, DECLARED_CANONICAL_SOURCE_FIELDS
from arch4.modem.live_lane_contract import modem_live_access_profile_for
from arch4.modem.network_inspection import NetworkSettingsRead, NetworkSettingsReadIncomplete
from arch4.modem.utils import measure_host_ping_rtt
from arch4.modem_live.physical_request_authority import ModemLivePhysicalRequestAuthority
from arch4.modem_live.startup_trace import traced_http, traced_protocol
from arch4.modem_live.physical_command import ModemLivePhysicalStagePlan, ModemLivePhysicalStageResult
from arch4.core_contracts.command_targets import CoreBandSetTarget, CoreBandAutoTarget, CoreNrBandTarget, CoreWifiRadioTarget, CorePBandTarget, CoreRestartTarget, CoreMobileDataTarget
from arch4.core_contracts.physical_commands import PhysicalCommandDispatchEvidence
from arch4.core_contracts.network_settings import CoreNetworkSettingsTarget, network_stage_kinds, subscription_key, validate_network_observation
from arch4.protected_credentials import open_apn_credentials
from arch4.modem.tplink_dev2_transport import (Dev2Session,
    Dev2AuthenticationError, Dev2ProtocolError, Dev2Rejected, MAX_REPLY, ZERO_STACK)
from arch4.modem_live.profile_catalog import (modem_profile_record, modem_profile_selection,
    modem_profile_native_identity_qualified, modem_profile_definition)
from arch4.modem_live.profile_matrix_contract import profile_field_matrix_address
from arch4.lte_mcs import canonical_qam_label

ONE = '1,0,0,0,0,0'
AUTH = {'0':'none','1':'pap','2':'chap','3':'auto'}
PDP = {'0':'ipv4','1':'ipv6','2':'ipv4v6'}


def mobile_data_payload(enabled):
    if type(enabled) is not bool:
        raise TypeError('tplink_mobile_data_bool_required')
    return {'dataSwitchStatusBackup': '1' if enabled else '0', 'profileAction': '0'}

# Exact firmware qualification is declared in the catalog. Speeds, rxFlow and
# calendar counters cannot substitute for this same-response cumulative pair.
TRAFFIC_COUNTER_FIELDS = (
    ('bytesReceived', 'traffic_device_download_counter_bytes', 'traffic_total_download_bytes'),
    ('bytesSent', 'traffic_device_upload_counter_bytes', 'traffic_total_upload_bytes'),
)
TRAFFIC_COUNTER_CANONICAL_FIELDS = frozenset(
    field for _, raw, total in TRAFFIC_COUNTER_FIELDS for field in (raw, total)
) | {'traffic_accounting_source'}


def native_byte_counter(value):
    if type(value) is str:
        value = value.strip()
        if not re.fullmatch(r'[0-9]{1,20}', value):
            return None
        value = int(value)
    return value if type(value) is int and 0 <= value < 2**64 else None


def native_traffic_counter_pair(traffic, contract):
    declared = TRAFFIC_COUNTER_CANONICAL_FIELDS & set(contract['supported_fields'])
    if not declared:
        return None
    if declared != TRAFFIC_COUNTER_CANONICAL_FIELDS:
        raise RuntimeError('tplink_traffic_counter_contract_incomplete')
    pair = tuple(native_byte_counter(traffic.get(native)) for native, _, _ in TRAFFIC_COUNTER_FIELDS)
    return pair if all(value is not None for value in pair) else None

# Exact250902 native calendar/session facts. The user's billing ledger is
# Foundation-owned and cannot be replaced by the router's calendar counters.
NATIVE_PERIOD_FIELDS = (
    ('monthBytesReceived', 'traffic_month_download_bytes', 'bytes'),
    ('monthBytesSent', 'traffic_month_upload_bytes', 'bytes'),
    ('dailyFlow', 'traffic_day_used_bytes', 'bytes'),
    ('paymentDay', 'traffic_quota_start_day', 'day'),
    ('totalConnTime', 'traffic_session_duration_s', 'seconds'),
)
NATIVE_PERIOD_CANONICAL_FIELDS = frozenset(field for _, field, _ in NATIVE_PERIOD_FIELDS)


def native_period_values(traffic, contract):
    declared = NATIVE_PERIOD_CANONICAL_FIELDS & set(contract['supported_fields'])
    if not declared:
        return {}
    if declared != NATIVE_PERIOD_CANONICAL_FIELDS:
        raise RuntimeError('tplink_native_period_contract_incomplete')
    result = {}
    for native, field, unit in NATIVE_PERIOD_FIELDS:
        value = traffic.get(native)
        # Native fixed-point byte totals are integral; do not round fractions,
        # accept exponents, or pass large counters through binary float.
        if type(value) is str and re.fullmatch(r'[0-9]{1,20}\.0{1,4}', value):
            value = value.split('.')[0]
        value = native_byte_counter(value)
        if value is None:
            continue
        if field.startswith('traffic_month_') or field == 'traffic_quota_start_day':
            if str(traffic.get('enablePaymentDay')) != '1':
                continue
        if unit == 'day' and not 1 <= value <= 31:
            continue
        result[field] = value
    return result

# These are the sole native measurement/unit descriptors for both normal
# translation and on-demand diagnostic coverage. Diagnostics never admit values.
LTE_SERVING_FIELDS = (
    ('band','primary_band',1,1,256,'band number','band number'),
    ('PCI','pci',1,0,503,'cell identifier','cell identifier'),
    ('ARFCN','earfcn_dl',1,0,262143,'channel number','channel number'),
    ('cid','cell_id',1,1,268435455,'cell identifier','cell identifier'),
    ('SNR','sinr_db',.1,-300,500,'tenths of dB','dB'),
    ('RSRP','rsrp_dbm',1,-150,-30,'dBm','dBm'),
    ('RSRQ','rsrq_db',1,-50,0,'dB','dB'),
    ('RSSI','rssi_dbm',1,-150,-10,'dBm','dBm'),
    ('downBandWidth','dl_bandwidth_mhz',.001,1,100000,'kHz','MHz'),
    ('upBandWidth','ul_bandwidth_mhz',.001,1,100000,'kHz','MHz'),
    ('downFreq','dl_freq_mhz',1,1,8000,'MHz','MHz'),
    ('upFreq','ul_freq_mhz',1,1,8000,'MHz','MHz'),
    ('TAC','tac',1,0,65535,'area code','area code'),
    ('CQI','cqi0',1,0,15,'index','index'), ('RI','ri',1,1,8,'rank','rank'),
    ('downMCS','mcs_dl0',1,0,31,'index','index'), ('upMCS','mcs_ul',1,0,31,'index','index'),
    ('numRbs','lte_num_rbs',1,0,110,'resource blocks','resource blocks'),
    ('txPowerPUCCH','tx_power',.1,-500,500,'tenths of dBm','dBm'),
)
NR_SERVING_FIELDS = (
    ('band','nr_band',1,1,1024,'band number','band number'),
    ('PCI','nr_pci',1,0,1007,'cell identifier','cell identifier'),
    ('ARFCN','nr_arfcn',1,0,3279165,'channel number','channel number'),
    ('cid','nr_cell_id',1,0,68719476735,'cell identifier','cell identifier'),
    ('SSRSRP','nr_rsrp_dbm',1,-150,-30,'dBm','dBm'),
    ('SSRSRQ','nr_rsrq_db',1,-50,0,'dB','dB'),
    ('SSSINR','nr_sinr_db',.1,-300,500,'tenths of dB','dB'),
    ('downBandWidth','nr_bandwidth_mhz',.001,1,400000,'kHz','MHz'),
    ('upBandWidth','nr_ul_bandwidth_mhz',.001,1,400000,'kHz','MHz'),
    ('downFreq','nr_dl_freq_mhz',1,1,100000,'MHz','MHz'),
    ('upFreq','nr_ul_freq_mhz',1,1,100000,'MHz','MHz'),
    ('RI','nr_ri',1,1,8,'rank','rank'), ('CQI','nr_cqi',1,0,15,'index','index'),
    ('downMCS','nr_mcs_dl',1,0,31,'index','index'), ('upMCS','nr_mcs_ul',1,0,31,'index','index'),
    ('numRbs','nr_num_rbs',1,0,275,'resource blocks','resource blocks'),
    ('txPowerPUCCH','nr_tx_power_dbm',.1,-500,500,'tenths of dBm','dBm'),
)
LTE_MODULATION_FIELDS = (('downlinkModType','qam_dl0'), ('uplinkModType','qam_ul'))
NR_MODULATION_FIELDS = (('downlinkModType','nr_qam_dl'), ('uplinkModType','nr_qam_ul'))
NATIVE_INTEGER_FIELDS = frozenset({'band','PCI','ARFCN','cid','TAC','CQI','RI','downMCS','upMCS','numRbs'})

# Object, canonical cell, native inputs, conversion. The only normal/diagnostic
# resource definition; raw memory units cancel in the ratio.
ROUTER_RESOURCE_FIELDS = (
    ('DEV2_PROC_STATUS','router_cpu_load_pct',('CPUUsage',),'integer_percent'),
    ('DEV2_MEM_STATUS','router_memory_used_pct',('total','free'),'used_percent'),
    ('DEV2_DEV_INFO','router_system_uptime_s',('upTime',),'seconds'),
)


def native_router_resource(row, conversion):
    if conversion=='integer_percent':
        value=number(row.get('CPUUsage'),low=0,high=100)
        return int(value) if value is not None else None
    if conversion=='used_percent':
        total=number(row.get('total'),low=0);free=number(row.get('free'),low=0)
        return int((total-free)/total*100) if total is not None and total>0 and free is not None and free<=total else None
    if conversion=='seconds':
        value=number(row.get('upTime'),low=0)
        return int(value) if value is not None and value.is_integer() else None
    raise ValueError('tplink_router_resource_conversion_undeclared')


def native_radio_rat(value):
    return {'3':'LTE', '7':'LTE', '8':'NR'}.get(str(value))


def select_native_serving_rows(rows):
    active = [r for r in rows if str(r.get('cellConnectionStatus')) in {'1','2'}
              and native_radio_rat(r.get('networkType'))]
    lte = [r for r in active if native_radio_rat(r['networkType'])=='LTE']
    nr = [r for r in active if native_radio_rat(r['networkType'])=='NR']
    primary = [r for r in lte if str(r.get('cellConnectionStatus'))=='1']
    if len(primary)>1:
        raise Dev2ProtocolError('tplink_multiple_primary_cells')
    classified = all(str(r.get('cellConnectionStatus')) in {'0','1','2'} and
        (str(r.get('cellConnectionStatus'))=='0' or native_radio_rat(r.get('networkType')))
        for r in rows)
    return lte, nr, primary, classified


def number(value, *, low=None, high=None):
    if isinstance(value, bool): return None
    try: n = float(value)
    except (TypeError, ValueError): return None
    if not math.isfinite(n) or low is not None and n < low or high is not None and n > high: return None
    return n


def native_measurement(native, value, scale, low, high):
    parsed = number(value, low=low, high=high)
    if parsed is None:
        return None
    if native in NATIVE_INTEGER_FIELDS:
        return int(parsed) if parsed.is_integer() else None
    return parsed * scale


def bands(value, rat):
    return sorted({int(m[1]) for term in str(value or '').split(';')
        if (m := re.fullmatch(rat+r'(\d+)', term)) and 0 < int(m[1]) < 1025})


def operator_plmn(entry):
    # Native scan entry is structured comma-separated text, never fuzzy digits.
    found = [s for s in str(entry or '').split(',') if re.fullmatch(r'\d{5,6}',s)]
    return found[0] if len(found)==1 else ''


def network_observation(replies, profile):
    sim = replies.get('DEV2_CELL_INTF_USIM')
    wan = replies.get('DEV2_LTE_WAN_CFG')
    network = replies.get('DEV2_LTE_NETWORK_SEARCH')
    profiles = replies.get('DEV2_LTE_PROFILE')
    index = number(wan.get('activeProfile'), low=0)
    active = profiles[int(index)] if index is not None and index.is_integer() and (index < len(profiles)) else None
    if not active or not {'profileID', 'profileName', 'apn', 'authType', 'pdpType'} <= set(active):
        raise ValueError('tplink_active_apn_profile_unavailable')
    apn = None
    if active:
        apn = {'profile_id': str(active.get('profileID', '')), 'name': str(active.get('profileName', '')), 'apn': str(active.get('apn', '')), 'username': str(active.get('username', '')), 'authentication': AUTH.get(str(active.get('authType'))), 'ip_protocol': PDP.get(str(active.get('pdpType'))), 'editable': bool(profile['capabilities']['supports_network_settings'] and number(wan.get('defaultProfile'), low=0) is not None and (index >= number(wan.get('defaultProfile'), low=0)))}
    imsi = str(sim.get('IMSI', ''))
    key = subscription_key(imsi)
    observation = validate_network_observation({'schema': 'NetworkSettingsObservation.v1', 'subscription_key': key, 'selection': {'1': 'auto', '2': 'manual'}.get(str(network.get('selectedMode'))), 'target_plmn': operator_plmn(network.get('selectedOperator')), 'roaming_enabled': {'1': True, '0': False}.get(str(wan.get('roamingEnabled'))), 'apn': apn})
    return observation


class TplinkDev2Modem(ModemInterface):
    physical_request_authority_required = True
    requires_provider_pool_policy = True

    def __init__(self, *, profile_id, host, username='admin', password='', ping_interface=None,
                 expected_model='', expected_serial='', expected_firmware='', expected_imei=''):
        selection = modem_profile_selection(profile_id)
        contract = modem_profile_record(profile_id)
        if (selection is None or selection.family != 'tplink_dev2_like'
                or not (contract.get('firmware_exact') or contract.get('firmware_pattern'))
                or not selection.target_guard_model):
            raise ValueError('tplink_explicit_exact_device_contract_required')
        self._device_contract = contract
        self._profile_id = selection.profile_id
        self._definition = modem_profile_definition(self._profile_id)
        self._radio_mode_readable = 'radio_mode' in contract['supported_fields']
        self._rich_radio_mode = (self._definition.snapshot().get('parameters', {}).get('radio_mode_contract')
            == 'tplink_dev2_configured_mode_v1')
        self._profile_field_issues={}
        self._backend_name = selection.backend
        self._native_identity_pin = None
        host=self._definition.http_address(host)
        parsed=urlsplit(host if '://' in host else 'http://'+host)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {'','/'} or parsed.query or parsed.fragment:
            raise ValueError('tplink_device_address_invalid')
        self._base_url=f'{parsed.scheme}://{parsed.netloc}'
        self._ping_iface=ping_interface
        self._expected={'model':expected_model,'serial':expected_serial,'firmware':expected_firmware,'imei':expected_imei}
        self._profile=modem_live_access_profile_for(backend=self._backend_name,profile_id=self._profile_id)
        self._physical_request_authority=ModemLivePhysicalRequestAuthority(self._profile)
        self._http=requests.Session();self._http.trust_env=False
        self._http.headers.update({'Connection':self._profile.http_connection_mode,
                                   'Referer':self._base_url+'/'})
        # Stock password-only roles are catalog data, not an authentication
        # guess or fallback after refusal. Other profiles use entered username.
        wire_username=contract['login_wire_username'] or username
        self._dev2=Dev2Session(self._tplink_http_request,wire_username,password)
        self._identity={};self._status={};self._fresh_status={};self._qualified=False
        self._provider_pool_policy = None
        self._last_write_started=0.0;self._last_write_returned=0.0
        self._wan_rate_baseline=None

    def physical_request_lane_scope(self,lane):
        return self._physical_request_authority.lane_scope(lane)

    def bind_provider_pool_policy(self, resolver):
        if not callable(resolver):raise TypeError('tplink_provider_pool_policy_required')
        if self._provider_pool_policy is not None:raise RuntimeError('tplink_provider_pool_policy_already_bound')
        if self._qualified:raise RuntimeError('tplink_provider_pool_policy_binding_after_login')
        self._provider_pool_policy = resolver

    def physical_request_authority_status(self):
        return self._physical_request_authority.snapshot()

    def _tplink_http_request(self,method,path,body,headers,lane,operation):
        if path not in {'/','/cgi/getGDPRParm','/cgi_gdpr?9'} or method not in {'GET','POST'}:
            raise ValueError('tplink_wire_path_not_qualified')
        physical_lane=self._physical_request_authority.current_lane(default=lane)
        request_kind = ('dev2_state_read' if operation.startswith(('go:','gl:'))
                        and self._profile.dev2_state_read_gap_s is not None else 'protected')
        with self._physical_request_authority.request(lane=physical_lane,operation='tplink:'+operation,
                                                      request_kind=request_kind):
            self._last_write_started=time.time() if operation.startswith(('so:', 'op:')) else self._last_write_started
            options=dict(data=body,headers=headers,
                timeout=(self._profile.fast_signal_connect_timeout_s,self._profile.fast_signal_read_timeout_s),
                allow_redirects=False,stream=True)
            response=None
            try:
                if method=='GET':response=traced_http(self._http.get,self._base_url+path,**options)
                else:response=traced_http(self._http.post,self._base_url+path,**options)
                if response.status_code in {301,302,303,307,308,401,403}:
                    self._dev2.clear();raise SessionExpiredError('TP-Link session expired; reconnect to the device.')
                response.raise_for_status()
                chunks=[];size=0
                for chunk in response.iter_content(16384):
                    size+=len(chunk)
                    if size>MAX_REPLY:raise Dev2ProtocolError('tplink_response_too_large')
                    chunks.append(chunk)
                return b''.join(chunks).decode('utf8')
            except requests.Timeout as exc:
                raise ModemTransportUnavailable('modem_transport_timeout') from exc
            except requests.ConnectionError as exc:
                logging.getLogger(__name__).warning(
                    'DEV2 connection failed at %s: %s', operation, exc)
                raise ModemTransportUnavailable('modem_transport_connection_lost') from exc
            except requests.exceptions.ChunkedEncodingError as exc:
                raise ModemTransportUnavailable('modem_transport_truncated_response') from exc
            finally:
                if response is not None:response.close()
                if operation.startswith(('so:', 'op:')):self._last_write_returned=time.time()

    def _read(self,oid,*,stack=ONE,listing=False,lane='fast_live'):
        definition=self._definition.request(oid,protocol='tplink_dev2')
        operation=definition['operation'];native_oid=definition['object_id'];stack=definition['stack']
        listing=operation=='gl'
        # Streamed encrypted HTTP cannot provide a decoded field audit. Reuse
        # the same bounded/sanitized trace around this existing read, never a
        # second physical request or a second trace owner.
        result=traced_protocol(self._dev2.request, protocol='tplink_dev2_decoded',
            operation=operation+':'+native_oid,args=(operation,native_oid),kwargs={'stack':stack,'lane':lane})
        if listing:
            if not isinstance(result,list) or any(not isinstance(r,dict) for r in result):raise Dev2ProtocolError('tplink_list_invalid')
        elif not isinstance(result,dict):raise Dev2ProtocolError('tplink_object_invalid')
        return result

    def _decoded_status(self,replies,addresses,context=None):
        values=self._decode_profile(replies,context,addresses=set(addresses))
        status={address.split('.',1)[1]:value for address,value in values.items()}
        diagnostic=self._profile_diagnostic()
        if diagnostic is not None:status['backend_diagnostic']=diagnostic
        return status

    def _decode_profile(self,replies,context=None,*,addresses):
        issues=[]
        values=decode_native_profile(self._definition, replies,context,addresses=addresses,issues=issues)
        for address in addresses:self._profile_field_issues.pop(address,None)
        for issue in issues:self._profile_field_issues[issue.address]=issue
        return values

    def _profile_diagnostic(self):
        from arch4.modem_live.profile_diagnostics import field_diagnostic
        return field_diagnostic(self._definition, self._profile_field_issues,
            previous=getattr(self,'_status',{}).get('backend_diagnostic'))

    def connect(self):
        self._qualified=False
        self._wan_rate_baseline=None
        self._profile_field_issues.clear()
        try:self._dev2.login()
        except Dev2AuthenticationError as exc:raise AuthenticationError('TP-Link login refused; check the router password.') from exc
        try:
            self.read_fresh_device_identity()
            self._qualified=True
            return True  # Mobile service is not a login prerequisite.
        except Exception:
            self._dev2.clear();raise

    def disconnect(self):
        try:self._dev2.logout()
        finally:self._qualified=False;self._wan_rate_baseline=None;self._http.close()

    def is_connected(self):
        return self._qualified and self._dev2.authenticated

    def read_sms_messages(self, *, limit=20, unread_only=False):
        # Qualified root list only; never write a pager or message read flag.
        if not self.is_connected() or self._device_contract['capabilities'].get('supports_sms') is not True:
            raise RuntimeError('tplink_selected_profile_sms_not_qualified')
        if type(limit) is not int or not 1 <= limit <= 50 or type(unread_only) is not bool:
            raise ValueError('tplink_sms_read_arguments_invalid')
        box=self._read('DEV2_LTE_SMS_RECVMSGBOX',stack=ZERO_STACK,lane='sms')
        rows=self._read('DEV2_LTE_SMS_RECVMSGENTRY',stack=ZERO_STACK,listing=True,lane='sms')
        count=box.get('totalNumber')
        if not isinstance(count,str) or not count.isascii() or not count.isdigit() or int(count)!=len(rows):
            raise Dev2ProtocolError('tplink_sms_inbox_count_invalid_or_incomplete')
        if not rows:return []
        clock=self._read('DEV2_TIME',stack=ZERO_STACK,lane='sms')
        offset=None
        try:
            local_now=dt.datetime.fromisoformat(clock.get('currentLocalTime',''))
            if (clock.get('status')=='Synchronized' and local_now.tzinfo is not None
                    and abs(local_now.timestamp()-time.time())<=30):
                offset=local_now.tzinfo
        except (ValueError,TypeError,OverflowError):
            pass  # Unknown clock is represented explicitly, never laptop time.
        messages=[];seen=set()
        for row in rows:
            identity=row.get('index');sender=row.get('from');text=row.get('content')
            received=row.get('receivedTime');unread=row.get('unread')
            if (not isinstance(identity,str) or not identity.isascii() or not identity.isdigit()
                    or int(identity)<=0 or identity in seen or not isinstance(sender,str)
                    or not isinstance(text,str) or not isinstance(received,str) or unread not in ('0','1')):
                raise Dev2ProtocolError('tplink_sms_native_record_invalid')
            try:native_time=dt.datetime.strptime(received,'%Y-%m-%d %H:%M:%S')
            except ValueError as exc:raise Dev2ProtocolError('tplink_sms_native_time_invalid') from exc
            seen.add(identity)
            if unread_only and unread=='0':continue
            messages.append((native_time,{
                'message_id':identity,'sender':sender,
                'text':text.replace('\x12','\n').replace('\x11','\r'),
                'timestamp':native_time.replace(tzinfo=offset).isoformat() if offset is not None else '',
                'read':unread=='0','protocol':'tplink_dev2_sms',
                'native_received_time':received,
                'timestamp_reason':'' if offset is not None else 'router_clock_unqualified',
            }))
        messages.sort(key=lambda item:item[0],reverse=True)
        return [message for _,message in messages[:limit]]

    def send_sms_message(self, number, text):
        raise RuntimeError('tplink_sms_receive_only_send_not_implemented')

    def delete_sms_message(self, message_id):
        raise RuntimeError('tplink_sms_receive_only_delete_not_implemented')

    def recover_live_register_session(self, base_status=None):
        # Connectivity admits and bounds repairs. Never retry a read/write or
        # trust the cached cipher as evidence that the remote session survives.
        return self.connect()

    def prepare_read_diagnostics(self):
        from arch4.modem.read_diagnostics import ReadDiagnosticUnavailable
        from arch4.modem.tplink_read_diagnostics import Dev2ReadDiagnosticAdapter
        if not self.is_connected():
            raise ReadDiagnosticUnavailable('Connect to the router before running read diagnostics.')
        return Dev2ReadDiagnosticAdapter(self)

    def diagnostic_native_field_descriptors(self, rows):
        """Describe bounded, projected evidence using the runtime translator's definitions."""
        result = []
        for row in rows:
            for oid,field,native_inputs,conversion in ROUTER_RESOURCE_FIELDS:
                if row.get('interface')!=oid or field not in self._device_contract['supported_fields']:
                    continue
                fields=row.get('fields',{})
                qualified=native_router_resource({k:fields.get(k,{}).get('value') for k in native_inputs},conversion)
                for native in native_inputs:
                    result.append({'interface':oid,'native_path':native,
                        'canonical_path':'slow.'+field,'raw_unit':'native memory units' if conversion=='used_percent' else '%' if conversion=='integer_percent' else 'seconds',
                        'canonical_unit':'seconds' if conversion=='seconds' else '%',
                        'scale':1 if conversion=='seconds' else None,
                        **({'conversion':'floor((total-free)/total*100)' if conversion=='used_percent' else 'integer(CPUUsage)'} if conversion!='seconds' else {}),
                        'rat':'router','qualification_reason':'' if qualified is not None else 'Required router resource inputs are missing or invalid.'})
            if row.get('interface') == 'DEV2_XTP_LTE_INTF_CFG':
                fields = row.get('fields', {})
                pair = native_traffic_counter_pair(
                    {name: fields.get(name, {}).get('value') for name, _, _ in TRAFFIC_COUNTER_FIELDS},
                    self._device_contract)
                if TRAFFIC_COUNTER_CANONICAL_FIELDS & set(self._device_contract['supported_fields']):
                    for native, raw, total in TRAFFIC_COUNTER_FIELDS:
                        for path in sorted(matrix_cells_for_canonical_fields(frozenset({raw, total}))):
                            result.append({'interface':row['interface'], 'native_path':native,
                                'canonical_path':path, 'raw_unit':'bytes', 'canonical_unit':'bytes',
                                'scale':1, 'rat':'', 'qualification_reason':
                                '' if pair is not None else 'A complete nonnegative integer download/upload counter pair is required.'})
                native_period = native_period_values(
                    {name: fields.get(name, {}).get('value') for name, _, _ in NATIVE_PERIOD_FIELDS}
                    | {'enablePaymentDay': fields.get('enablePaymentDay', {}).get('value')},
                    self._device_contract)
                for native, field, unit in NATIVE_PERIOD_FIELDS:
                    if field not in self._device_contract['supported_fields']:continue
                    for path in sorted(matrix_cells_for_canonical_fields(frozenset({field}))):
                        result.append({'interface':row['interface'], 'native_path':native,
                            'canonical_path':path, 'raw_unit':unit, 'canonical_unit':unit,
                            'scale':1, 'rat':'', 'qualification_reason':'' if field in native_period
                            else 'Native value or enabled calendar period is unavailable.'})
                continue
            if row.get('interface') != 'DEV2_LTE_SERVING_CELL_INFO':
                continue
            fields = row.get('fields', {})
            prefixes = sorted({path.rsplit('.', 1)[0] for path in fields
                               if re.fullmatch(r'records\.\d+\.networkType', path)})
            native_rows = [dict(networkType=fields[p+'.networkType'].get('value'),
                                cellConnectionStatus=fields.get(p+'.cellConnectionStatus', {}).get('value'),
                                prefix=p) for p in prefixes]
            lte, nr, primary, _classified = select_native_serving_rows(native_rows)
            selected = primary + ([next((r for r in nr if str(r['cellConnectionStatus'])=='1'), nr[0])]
                                  if nr else [])
            for prefix in prefixes:
                if not any(r['prefix']==prefix for r in selected):
                    continue
                rat = native_radio_rat(fields[prefix+'.networkType'].get('value'))
                if rat is None:
                    continue
                specs = LTE_SERVING_FIELDS if rat=='LTE' else NR_SERVING_FIELDS
                for native, field, scale, lo, hi, raw_unit, unit in specs:
                    # Primary band and serving band are one native observation.
                    target = 'serving_band' if field=='primary_band' else field
                    for path in sorted(matrix_cells_for_canonical_fields(frozenset({target}))):
                        reason = ''
                        if field=='nr_cell_id' and lte and not self._device_contract['nr_cell_id_in_nsa']:
                            reason = 'This firmware does not qualify the reported NR cell identity in NSA mode.'
                        candidate = fields.get(prefix+'.'+native, {})
                        if candidate.get('state')=='returned' and 'value' in candidate and native_measurement(native, candidate['value'], scale, lo, hi) is None:
                            reason = 'The returned value does not satisfy this measurement contract.'
                        result.append({'interface':row['interface'], 'native_path':prefix+'.'+native,
                                       'canonical_path':path, 'raw_unit':raw_unit, 'canonical_unit':unit,
                                       'scale':scale, 'rat':rat, 'qualification_reason':reason,
                                       **({'component':'ppucch'} if field=='tx_power' else {})})
                modulation = LTE_MODULATION_FIELDS if rat=='LTE' else NR_MODULATION_FIELDS
                for native, field in modulation:
                    candidate = fields.get(prefix+'.'+native, {})
                    reason = ('The returned modulation is not qualified by this measurement contract.'
                              if candidate.get('state')=='returned' and 'value' in candidate
                              and canonical_qam_label(candidate['value']) not in {'QPSK','16QAM','64QAM','256QAM'} else '')
                    for path in sorted(matrix_cells_for_canonical_fields(frozenset({field}))):
                        result.append({'interface':row['interface'], 'native_path':prefix+'.'+native,
                                       'canonical_path':path, 'raw_unit':'modulation',
                                       'canonical_unit':'modulation', 'scale':1, 'rat':rat,
                                       'qualification_reason':reason})
        return tuple(result)

    def read_fresh_device_identity(self):
        row=self._read('DEV2_DEV_INFO',stack=ZERO_STACK,lane='boot_static')
        cell=self._read('DEV2_CELL_INTF',lane='boot_static')
        identity=self._decoded_status({'DEV2_DEV_INFO':row,'DEV2_CELL_INTF':cell},
            {'identity.'+field for field in('vendor','model','hardware_version','software_version','firmware','serial','imei')})
        if not modem_profile_native_identity_qualified(self._profile_id,
                model=identity['model'], firmware=identity['firmware'], hardware=identity['hardware_version']):
            raise TargetGuardError('The router identity does not match the selected TP-Link firmware contract.',
                                   reason='tplink_exact_firmware_identity_mismatch')
        ok,reason=evaluate_target_guard(expected_model=self._expected['model'],expected_serial=self._expected['serial'],
            expected_firmware=self._expected['firmware'],expected_imei=self._expected['imei'],observed=identity)
        if not ok:raise TargetGuardError('The connected router does not match the selected device.',reason=reason)
        pin = (identity['model'], identity['firmware'], identity['hardware_version'],
               identity['serial'], identity['imei'])
        if self._native_identity_pin is not None and pin != self._native_identity_pin:
            raise TargetGuardError('The router identity changed since this connection was admitted.',
                                   reason='tplink_native_identity_changed')
        self._native_identity_pin = pin
        self._identity=identity
        return dict(identity)

    def get_modem_info(self):return dict(self._identity)
    def get_last_status(self):return build_public_modem_status(self._status)
    def get_capabilities(self):
        from arch4.interfaces import get_capabilities
        return get_capabilities(self._backend_name)
    def verify_static_config(self):return {}
    def verify_ue_capability(self):return {}
    def read_neighbors(self):return []
    def scan_neighbors(self):return []
    def read_band_priority(self):return []
    def read_serving_cell(self):return dict(self._status)
    def get_current_bands(self):return list(self._status.get('selected_band_set',[]))
    def measure_rtt(self,target='8.8.8.8',count=3,*,timeout_s):
        return measure_host_ping_rtt(target,count=count,ping_interface=self._ping_iface,timeout_s=timeout_s)

    def _wan_rates(self, link, rows, sampled_at, previous):
        interface=str(link.get('ifName','')).strip()
        candidates=[row for row in rows if row.get('accessMode')=='MBB'
            and row.get('ifName')==interface and str(row.get('enable'))=='1']
        if not interface or str(link.get('connectStatus'))!='4' or len(candidates)!=1:
            return None
        row=candidates[0]
        stack=str(row.get('stack',''))
        pair=tuple(native_byte_counter(row.get(key)) for key in ('X_TP_BytesReceived','X_TP_BytesSent'))
        if not stack or any(value is None for value in pair):return None
        current=(interface,stack,sampled_at,pair)
        self._wan_rate_baseline=current
        if previous is None or previous[:2]!=current[:2]:return None
        elapsed=sampled_at-previous[2]
        if elapsed<=0 or any(value<old for value,old in zip(pair,previous[3])):return None
        return tuple((value-old)/elapsed for value,old in zip(pair,previous[3]))

    def _radio(self):
        previous=self._wan_rate_baseline
        # A failed block cannot leave a baseline spanning an unknown interval.
        self._wan_rate_baseline=None
        link=self._read('DEV2_LTE_LINK_CFG')
        rows=self._read('DEV2_LTE_SERVING_CELL_INFO',stack=ZERO_STACK,listing=True)
        if self._device_contract['traffic_rate_mode']=='cellular_interface_counter_delta':
            wan=self._read('DEV2_ADT_WAN',stack=ZERO_STACK,listing=True)
            wan_sampled_at=time.monotonic()
        period_sampled_at=None
        if 'traffic_counter_period_started_at' in self._device_contract['supported_fields']:
            # Same conservative boot bound as RouterOS: read uptime BEFORE
            # counters. A reconnect or a decreasing counter alone is no proof.
            started=time.time()
            device=self._read('DEV2_DEV_INFO',stack=ZERO_STACK)
            period_sampled_at=started
        traffic=self._read('DEV2_XTP_LTE_INTF_CFG')
        replies={'DEV2_LTE_LINK_CFG':link,'DEV2_LTE_SERVING_CELL_INFO':rows,
                 'DEV2_XTP_LTE_INTF_CFG':traffic}
        if self._device_contract['traffic_rate_mode']=='cellular_interface_counter_delta':
            replies['DEV2_ADT_WAN']=wan
        if period_sampled_at is not None:replies['DEV2_DEV_INFO']=device
        addresses=self._definition.available_addresses(replies)
        # Structural scheduling/state stays here. The selected portable file
        # owns the actual readable field selection, conversion and units.
        context={'wan_previous':previous,'sampled_at':period_sampled_at or time.time(),
            'monotonic_at':wan_sampled_at if 'DEV2_ADT_WAN'in replies else time.monotonic()}
        decoded=self._decode_profile(replies,context,addresses=addresses)
        if self._device_contract['traffic_rate_mode']=='cellular_interface_counter_delta':
            state_address='state.wan_counter_baseline'
            try:
                baseline=self._definition.state_value('wan_counter_baseline',replies,context)
                if baseline is not None:
                    if (not isinstance(baseline,(tuple,list))or len(baseline)!=4
                            or not isinstance(baseline[0],str)or not baseline[0]
                            or not isinstance(baseline[1],str)or not re.fullmatch(r'\d+(?:,\d+){5}',baseline[1])
                            or baseline[2]!=wan_sampled_at or not isinstance(baseline[3],(list,tuple))or len(baseline[3])!=2
                            or any(type(n)is not int or not 0<=n<2**64 for n in baseline[3])):
                        raise ValueError('tplink_portable_counter_state_invalid')
                    self._wan_rate_baseline=(*baseline[:3],tuple(baseline[3]))
                self._profile_field_issues.pop(state_address,None)
            except (ValueError,TypeError,KeyError):
                # Counter state belongs to optional traffic acquisition. Never
                # turn its conversion failure into loss of valid RF facts.
                from arch4.modem_live.profile_definition.results import FieldIssue
                self._profile_field_issues[state_address]=FieldIssue(
                    state_address,'/state/wan_counter_baseline','counter_state_invalid')
                for address in ('traffic.rx_bytes_per_sec','traffic.tx_bytes_per_sec'):
                    decoded[address]=None
        status={};modem=ModemInput.safe_defaults(traffic_valid=False,radio_connected=False,session_uptime_source='unsupported')
        def put(field,value):
            if value is not None:
                status[field]=value
                if field in modem.__dataclass_fields__:setattr(modem,field,value)
        connected=decoded.get('traffic.packet_session_connected')
        put('packet_session_connected',connected);put('radio_connected',connected)
        if connected is not None:put('radio_state_text',decoded.get('health.radio_state_text'))
        if 'connectStatus' in link:put('connection_status_code',str(link['connectStatus']))
        if decoded.get('slow.wan_ip')is not None:
            try:put('wan_ip',str(ipaddress.ip_address(decoded['slow.wan_ip'])))
            except ValueError:put('wan_ip','')
        lte,nr,primary,classified=select_native_serving_rows(rows)
        # Missing native classification is unknown, not evidence of no service
        # or absence of an LTE anchor. Do not stamp a guessed RAT mode fresh.
        if classified:
            put('network_type',decoded.get('radio.network_type'))
            put('ca_active',len(lte)>1);put('nsa_active',bool(lte and nr));put('sa_active',bool(nr and not lte))
            put('nr5g_active',bool(nr))
            put('nr_mode','NSA' if lte and nr else 'SA' if nr else '')
        carriers=[]
        for r in lte:
            # Reuse the same canonical cell rules for each LTE component.
            # The row-local primary marker selects the row for decoding only;
            # the published role always remains the actual native role below.
            cell=decoded if primary and r is primary[0] else self._decode_profile(
                {'DEV2_LTE_SERVING_CELL_INFO':[{**r,'cellConnectionStatus':'1'}]},
                addresses={'radio.serving_band','rf.pci','rf.earfcn_dl',
                    'slow.dl_bandwidth_mhz','rf.rsrp_dbm','rf.sinr_db'})
            b=cell.get('radio.serving_band')
            if b is None:continue
            rsrp=cell.get('rf.rsrp_dbm')
            # Native secondary rows report zeros while measurements are absent.
            # A valid measured RSRP qualifies SINR zero; placeholder RSRP does not.
            sinr=cell.get('rf.sinr_db') if rsrp is not None else None
            carriers.append(CarrierInfo(int(b),cell.get('rf.earfcn_dl'),cell.get('rf.pci'),
                cell.get('slow.dl_bandwidth_mhz'),sinr,
                rsrp,1,str(r.get('cellConnectionStatus'))=='1'))
        modem.carriers=carriers
        put('active_band_set',sorted({c.band for c in carriers}))
        put('carrier_details',[vars(c) for c in carriers])
        if primary:
            r=primary[0]
            for native,field,scale,lo,hi,_raw_unit,_unit in LTE_SERVING_FIELDS:
                address=profile_field_matrix_address('serving_band' if field=='primary_band' else field)
                value=decoded.get(address)
                put(field,value)
            if 'primary_band' in status:put('serving_band',int(status['primary_band']))
            if 'cell_id' in status:put('enb_id',decoded.get('rf.enb_id'))
            for native,field in LTE_MODULATION_FIELDS:
                label=decoded.get(profile_field_matrix_address(field))
                qm={'QPSK':2,'16QAM':4,'64QAM':6,'256QAM':8}.get(label)
                if qm is not None:
                    status[field]=label
                    if field=='qam_ul':modem.qam_ul=qm
                    else:
                        modem.qam_dl0=label;modem.qam_dl_primary=qm;modem.mod0=qm
                    put('qam_source','reported');put('qam_confidence',1.0)
            put('scheduler_zero_authoritative',True)
            put('scheduler_stream1_available',False)
            put('scheduler_source','reported')
            if 'ri' in status:
                put('ri_valid',True);put('ri_source','native_serving_cell')
        if nr:
            # Prefer explicit NR primary; deterministic fallback is a reported secondary.
            r=next((r for r in nr if str(r['cellConnectionStatus'])=='1'),nr[0])
            nci_reported=not lte or self._device_contract['nr_cell_id_in_nsa']
            for native,field,scale,lo,hi,_raw_unit,_unit in NR_SERVING_FIELDS:
                put(field,None if native=='cid' and not nci_reported
                    else decoded.get(profile_field_matrix_address(field)))
            for native,field in NR_MODULATION_FIELDS:
                label=decoded.get(profile_field_matrix_address(field))
                if label in {'QPSK','16QAM','64QAM','256QAM'}:put(field,label)
            if not nci_reported:
                status['nr_cell_id']=None
            put('nr_cells',[{'band':number(r.get('band')),'pci':number(r.get('PCI')),'arfcn':number(r.get('ARFCN')),
                'cell_id':number(r.get('cid')) if nci_reported else None} for r in nr])
        if self._device_contract['traffic_rate_mode']=='cellular_interface_counter_delta':
            rx=decoded.get('traffic.rx_bytes_per_sec');tx=decoded.get('traffic.tx_bytes_per_sec')
            rate_source='counter_delta'
        elif self._device_contract['traffic_rate_mode']=='reported':
            rx=decoded.get('traffic.rx_bytes_per_sec');tx=decoded.get('traffic.tx_bytes_per_sec')
            rate_source='reported'
        else:raise RuntimeError('tplink_traffic_rate_contract_invalid')
        put('traffic_valid',rx is not None and tx is not None)
        if rx is not None and tx is not None:
            put('rx_bytes_per_sec',rx);put('tx_bytes_per_sec',tx);put('traffic_valid',True)
            put('dl_mbps',rx*8/1e6);put('ul_mbps',tx*8/1e6);put('has_traffic',rx>0 or tx>0);put('traffic_rate_source',rate_source)
        if NATIVE_PERIOD_CANONICAL_FIELDS & set(self._device_contract['supported_fields']):
            native_period={field:decoded.get('traffic.'+field)for field in NATIVE_PERIOD_CANONICAL_FIELDS}
            for field, value in native_period.items():put(field, value)
            # Native calendar facts are readable, but their day boundary is the
            # router's clock. Foundation alone qualifies the user's ledger.
            put('traffic_accounting_status','native_router_calendar_reference')
            duration = native_period.get('traffic_session_duration_s')
            if duration is not None:
                put('session_uptime_s',duration);put('session_uptime_source','reported')
        else:
            # Sibling firmware's explicit existing timer contract.
            val=decoded.get('traffic.session_uptime_s')
            if val is not None:put('session_uptime_s',val);put('session_uptime_source','reported')
        pair=tuple(decoded.get('traffic.'+raw)for _,raw,_ in TRAFFIC_COUNTER_FIELDS)
        if all(value is not None for value in pair):
            for (_, raw, total), value in zip(TRAFFIC_COUNTER_FIELDS, pair):
                put(raw, value);put(total, value)
            put('traffic_accounting_source',decoded.get('traffic.traffic_accounting_source'))
            put('traffic_counter_period_started_at',decoded.get('traffic.traffic_counter_period_started_at'))
        diagnostic=self._profile_diagnostic()
        if diagnostic is not None:put('backend_diagnostic',diagnostic)
        self._fresh_status=status;self._status.update(status)
        return modem

    def _bands_status(self,lane,provider):
        row=self._read('DEV2_LTE_BAND_SEARCH',lane=lane)
        if str(row.get('selectedMode'))not in{'1','2'}:raise Dev2ProtocolError('tplink_band_mode_unknown')
        if self._provider_pool_policy is None:raise RuntimeError('tplink_provider_pool_policy_unbound')
        plmn=provider['provider_plmn']
        policy=self._provider_pool_policy(domain='lte',provider_plmn=plmn) if plmn else None
        if policy is not None and (policy.get('schema')!='FoundationProviderBandProfile.v1' or
                policy.get('owner')!='54guc.foundation.band_auto_policy' or
                policy.get('rat')!='lte' or policy.get('band_auto_provider_plmn')!=plmn):
            raise RuntimeError('tplink_provider_pool_policy_contract_invalid')
        decoded=decode_native_profile(self._definition, {'DEV2_LTE_BAND_SEARCH':row},
            {'provider_bands':list(policy['bands'])if policy else[]},addresses={
                'capability.supported_lte_bands','capability.supported_nr_bands',
                'profile.nr5g_band_set','radio.configured_band_pool'})
        supported=decoded['capability.supported_lte_bands'];nr=decoded['profile.nr5g_band_set']
        lte=decoded['radio.configured_band_pool']
        return {'selected_band_set':lte,'configured_band_pool':lte,'configured_band_pool_signature':band_signature(lte),
            'configured_band_pool_source':'foundation_provider_policy_intersection',
            'configured_band_pool_status':'derived' if lte else 'unavailable_provider_pool',
            'configured_band_pool_last_sample_ts':time.time(),
            'nr5g_band_set':nr,'supported_lte_bands':supported,'supported_nr_bands':decoded['capability.supported_nr_bands']}

    def read_actionable_modem_input(self):return self._radio()
    def _provider_status(self,lane):
        row=self._read('DEV2_LTE_PROF_STAT',lane=lane)
        return self._decoded_status({'DEV2_LTE_PROF_STAT':row},
            {'identity.provider_name','identity.provider_plmn'})

    def _wifi_networks(self,lane):
        """Stock main-network checkboxes, distinct from physical radio enable."""
        if not self._device_contract['capabilities']['supports_wifi_control']:
            raise RuntimeError('tplink_selected_profile_wifi_not_qualified')
        rows=self._read('DEV2_ADT_WIFI_COMMON',stack=ZERO_STACK,listing=True,lane=lane)
        result={}
        for band in ('2.4ghz','5ghz'):
            matches=[r for r in rows if str(r.get('band','')).lower()==band]
            if len(matches)!=1:continue
            row=matches[0]
            stack=str(row.get('stack',''))
            if (not re.fullmatch(r'[1-9]\d*,(?:\d+,){4}\d+',stack)
                    or str(row.get('primaryEnable')) not in {'0','1'}):continue
            result[band]=(stack,str(row['primaryEnable'])=='1')
        return result

    def _wifi_status(self,lane):
        if not self._device_contract['capabilities']['supports_wifi_control']:
            raise RuntimeError('tplink_selected_profile_wifi_not_qualified')
        rows=self._read('DEV2_ADT_WIFI_COMMON',stack=ZERO_STACK,listing=True,lane=lane)
        return self._decoded_status({'DEV2_ADT_WIFI_COMMON':rows},
            {'slow.wifi_24ghz_enabled','slow.wifi_5ghz_enabled'})

    def _lan_status(self,lane):
        rows=self._read('DEV2_ADT_LAN',stack=ZERO_STACK,listing=True,lane=lane)
        return self._decoded_status({'DEV2_ADT_LAN':rows},{'identity.lan_mac'})

    def _wan_configuration_status(self,lane):
        # This enum is qualified only by the selected firmware's stock page.
        row=self._read('DEV2_LTE_WAN_CFG',lane=lane)
        return self._decoded_status({'DEV2_LTE_WAN_CFG':row},
            {'slow.antenna','slow.mobile_data_enabled'})

    def _radio_mode_configuration(self, lane):
        """Return fresh canonical settings and their private local WAN evidence."""
        if not self._radio_mode_readable:
            raise RuntimeError('tplink_selected_profile_radio_mode_not_qualified')
        try:
            wan = self._read('DEV2_LTE_WAN_CFG', lane=lane)
            selection = self._read('DEV2_LTE_BAND_SEARCH', lane=lane)
            sim = self._read('DEV2_CELL_INTF_USIM', lane=lane)
            if self._rich_radio_mode:
                from arch4.modem.tplink_radio_mode import configured_status
                return configured_status(self, wan, selection, sim, lane), wan
            mode = self._definition.state_value('configured_radio_mode', {'DEV2_LTE_WAN_CFG': wan})
            kind = {'1': 'auto', '2': 'manual'}.get(str(selection.get('selectedMode')))
            native_bands = selection.get('supportBandList') if kind == 'auto' else selection.get('selectedBand')
            observed = validate_radio_mode_observation(dict(mode=mode, band_selection_mode=kind,
                lte_bands=bands(native_bands, 'B'), nr_bands=bands(native_bands, 'N'),
                subscription_key=subscription_key(str(sim.get('IMSI', ''))),
                profile_definition_hash=self._definition.content_hash))
            return observed, wan
        except (TypeError, ValueError, RuntimeError, Dev2Rejected, Dev2ProtocolError,
                SessionExpiredError, ModemTransportUnavailable) as exc:
            raise RuntimeError('tplink_radio_mode_observation_unavailable') from exc

    def _radio_mode_status(self, lane, *, required=False):
        if not self._radio_mode_readable:
            raise RuntimeError('tplink_selected_profile_radio_mode_not_qualified')
        try:
            observed, _wan = self._radio_mode_configuration(lane)
        except RuntimeError:
            if required:
                raise
            return {'radio_mode': None}
        return {'radio_mode': observed}

    def manual_nr_mode_preferred_key(self, configuration):
        """Qualify the optional Manual NR prefix under ModemLive's I/O owner."""
        if not self._rich_radio_mode:
            return None
        observed, wan = self._radio_mode_configuration('event_write')
        if observed != validate_radio_mode_observation(configuration):
            raise RuntimeError('tplink_radio_mode_original_configuration_changed')
        from arch4.modem_live.radio_mode_configuration import native_mode_for_key
        native = native_mode_for_key(self._definition, 'nsa_preferred')
        options = [value.strip() for value in wan['networkPreferredModeOptionList'].split(',')]
        return 'nsa_preferred' if native in options else None

    def _iter_router_resources(self,lane):
        for oid,field,_inputs,conversion in ROUTER_RESOURCE_FIELDS:
            if field in self._device_contract['supported_fields']:
                try:
                    row=self._read(oid,stack=ZERO_STACK,lane=lane)
                except Dev2Rejected:
                    # An optional native refusal clears only this reading.
                    row={}
                yield self._decoded_status({oid:row},{profile_field_matrix_address(field)})

    def read_modem_input(self):
        modem=self._radio();provider=self._provider_status('startup');status=self._bands_status('startup',provider)
        if self._device_contract['capabilities']['supports_wifi_control']:
            status.update(self._wifi_status('startup'))
        if 'lan_mac' in self._device_contract['supported_fields']:
            status.update(self._lan_status('startup'))
        if 'antenna' in self._device_contract['supported_fields']:
            status.update(self._wan_configuration_status('startup'))
        for resources in self._iter_router_resources('startup'):
            status.update(resources)
        self._fresh_status.update(status);self._fresh_status.update(self._identity);self._fresh_status.update(provider)
        self._status.update(self._fresh_status);self._project_pool(modem,status)
        return modem

    @staticmethod
    def _project_pool(modem,status):
        for field in ('selected_band_set','configured_band_pool','configured_band_pool_source',
                'configured_band_pool_status','configured_band_pool_last_sample_ts'):
            if field in status:setattr(modem,field,status[field])
        if 'configured_band_pool' in status:modem.ca_band_signature=band_signature(status['configured_band_pool'])

    def _receipt(self,request,modem,status):
        if modem is not None:
            self._project_pool(modem,status)
        fields=frozenset(set(status)&DECLARED_CANONICAL_SOURCE_FIELDS)
        return self._capture_backend_acquisition_receipt(request=request,modem=modem,acquired_cells=matrix_cells_for_canonical_fields(fields),status_override=status)
    def _failed_receipt(self,request,error):
        return self._capture_backend_acquisition_receipt(request=request,modem=None,
            acquired_cells=frozenset(),status_override={},failure_reason=error.reason)

    def _acquire_receipt(self,request,read):
        try:
            modem=read()
            return self._receipt(request,modem,self._fresh_status)
        except ModemTransportUnavailable as exc:
            return self._failed_receipt(request,exc)

    def acquire_modem_input(self,request):
        if request.lane!='startup_full':raise ValueError('tplink_startup_lane_required')
        return self._acquire_receipt(request,self.read_modem_input)
    def acquire_actionable_modem_input(self,request):
        if request.lane!='fast_live':raise ValueError('tplink_fast_lane_required')
        return self._acquire_receipt(request,self.read_actionable_modem_input)
    def acquire_post_write_modem_input(self,request,stage):
        if not isinstance(stage, PhysicalCommandDispatchEvidence):
            raise TypeError('tplink_post_write_dispatch_evidence_required')
        if request.lane!='post_write' or not stage.permits_target_verification:raise ValueError('tplink_post_write_evidence_required')
        if isinstance(stage.target, CoreMobileDataTarget):
            try:
                return self._receipt(request,None,self._wan_configuration_status('readback'))
            except ModemTransportUnavailable as exc:
                return self._failed_receipt(request,exc)
        return self._acquire_receipt(request,lambda:self._read_command_target(stage,request))

    def _read_command_target(self,stage,request):
        settings={}
        if isinstance(stage.target, CoreNetworkSettingsTarget):
            settings=self._acquire_network_settings_readback(request).status_copy()
        modem=self._radio();provider=self._provider_status('readback')
        self._fresh_status.update(provider);self._fresh_status.update(self._bands_status('readback',provider));self._fresh_status.update(settings);self._status.update(self._fresh_status)
        if isinstance(stage.target,CoreWifiRadioTarget):
            self._fresh_status.update(self._wifi_status('readback'));self._status.update(self._fresh_status)
        if isinstance(stage.target, CoreRadioModeTarget):
            self._fresh_status.update(self._radio_mode_status('readback'));self._status.update(self._fresh_status)
        return modem
    def iter_acquire_live_maintenance(self,request):
        if request.lane!='slow_live':raise ValueError('tplink_slow_lane_required')
        try:yield from self._iter_live_maintenance(request)
        except ModemTransportUnavailable as exc:yield self._failed_receipt(request,exc)

    def _iter_live_maintenance(self,request):
        provider=self._provider_status('slow_live')
        self._status.update(provider);yield self._receipt(request,None,provider)
        status=self._bands_status('slow_live',provider)
        self._status.update(status);yield self._receipt(request,None,status)
        if self._device_contract['capabilities']['supports_wifi_control']:
            status=self._wifi_status('slow_live')
            self._status.update(status);yield self._receipt(request,None,status)
        if 'lan_mac' in self._device_contract['supported_fields']:
            status=self._lan_status('slow_live')
            self._status.update(status);yield self._receipt(request,None,status)
        if 'antenna' in self._device_contract['supported_fields']:
            status=self._wan_configuration_status('slow_live')
            self._status.update(status);yield self._receipt(request,None,status)
        for status in self._iter_router_resources('slow_live'):
            self._status.update(status);yield self._receipt(request,None,status)
        if self._radio_mode_readable:
            status = self._radio_mode_status('slow_live')
            self._status.update(status);yield self._receipt(request,None,status)
    def acquire_live_maintenance(self,request):raise RuntimeError('tplink_stepwise_maintenance_required')
    def refresh_live_maintenance(self):raise RuntimeError('tplink_stepwise_maintenance_required')

    def iter_acquire_network_settings(self,request):
        try:yield from super().iter_acquire_network_settings(request)
        except ModemTransportUnavailable as exc:yield self._failed_receipt(request,exc)
        except Dev2ProtocolError as exc:
            raise NetworkSettingsReadIncomplete('tplink_settings_reply_malformed') from exc

    def _iter_network_settings_observation(self):
        replies={}
        replies['DEV2_CELL_INTF_USIM']=self._read('DEV2_CELL_INTF_USIM',lane='slow_live');yield None
        replies['DEV2_LTE_WAN_CFG']=self._read('DEV2_LTE_WAN_CFG',lane='slow_live');yield None
        replies['DEV2_LTE_NETWORK_SEARCH']=self._read('DEV2_LTE_NETWORK_SEARCH',lane='slow_live');yield None
        replies['DEV2_LTE_PROFILE']=self._read('DEV2_LTE_PROFILE',stack=ZERO_STACK,listing=True,lane='slow_live');yield None
        values=self._decode_profile(replies,addresses={'slow.network_settings'})
        observation=values.get('slow.network_settings')
        if observation is None:
            raise NetworkSettingsReadIncomplete('tplink_network_definition_unavailable')
        try:observation=validate_network_observation(observation)
        except (TypeError,ValueError)as exc:
            raise NetworkSettingsReadIncomplete('tplink_network_definition_invalid')from exc
        # The typed subscription guard keeps its original same-read private
        # USIM source. It is not the unclassified public identity.imsi slot.
        imsi=str(replies['DEV2_CELL_INTF_USIM'].get('IMSI',''))
        if observation['subscription_key']!=subscription_key(imsi):
            raise NetworkSettingsReadIncomplete('tplink_network_subscription_definition_mismatch')
        return NetworkSettingsRead(observation,imsi if observation['subscription_key']else'')

    def execute_physical_stage(self,plan):
        if not isinstance(plan,ModemLivePhysicalStagePlan):raise TypeError('tplink_typed_stage_required')
        target=plan.target
        capabilities = self._device_contract['capabilities']
        if isinstance(target, CoreRadioModeTarget) and not capabilities.get('supports_radio_mode', False):
            raise RuntimeError('tplink_selected_profile_radio_mode_not_qualified')
        if isinstance(target,CoreMobileDataTarget) and not capabilities.get('supports_mobile_data',False):
            raise RuntimeError('tplink_selected_profile_mobile_data_not_qualified')
        if isinstance(target,CoreRestartTarget):
            if not capabilities['supports_restart']:
                raise RuntimeError('tplink_selected_profile_restart_not_qualified')
            if plan.stage_kind!='restart' or plan.ordinal!=1:
                raise ValueError('tplink_restart_stage_invalid')
        if isinstance(target,CorePBandTarget):
            if not capabilities['supports_pband']:
                raise RuntimeError('tplink_selected_profile_pband_not_qualified')
            if plan.stage_kind!='pband_anchor' or plan.ordinal!=1 or target.pband<=0:
                raise ValueError('tplink_pband_stage_invalid')
        if isinstance(target,CoreNrBandTarget) and not capabilities['supports_nr_band_lock']:
            raise RuntimeError('tplink_selected_profile_nr_control_not_qualified')
        if isinstance(target,CoreNetworkSettingsTarget) and not capabilities['supports_network_settings']:
            raise RuntimeError('tplink_selected_profile_network_control_not_qualified')
        if isinstance(target,CoreWifiRadioTarget) and (not capabilities['supports_wifi_control'] or target.band=='6ghz'):
            raise RuntimeError('tplink_selected_profile_wifi_control_not_qualified')
        self.read_fresh_device_identity()
        oid='';data={};stack=ONE;operation='so'
        detail={}
        if isinstance(target, CoreRadioModeTarget):
            if plan.stage_kind != 'radio_mode' or plan.ordinal != 1:
                raise ValueError('tplink_radio_mode_stage_invalid')
            observed, original_wan = self._radio_mode_configuration('event_write')
            expected = target.configuration(expected=True)
            if observed != expected:
                raise RuntimeError('tplink_radio_mode_original_configuration_changed')
            oid = 'DEV2_LTE_WAN_CFG'
            if self._rich_radio_mode:
                from arch4.modem_live.radio_mode_configuration import native_mode_for_key
                native = native_mode_for_key(self._definition, target.mode_key)
                latest_wan = self._read('DEV2_LTE_WAN_CFG', lane='event_write')
                if latest_wan != original_wan:
                    raise RuntimeError('tplink_radio_mode_original_configuration_changed')
                choices = latest_wan.get('networkPreferredModeOptionList', '').split(',')
                if native not in [value.strip() for value in choices]:
                    raise RuntimeError('tplink_radio_mode_requested_option_unavailable')
            data = {'networkPreferredModeSelected': target.mode_key or target.mode}
        elif isinstance(target,CoreBandAutoTarget):
            required='supports_nr_band_lock' if target.domain=='nr_approved_auto' else 'supports_band_selection'
            if not capabilities.get(required,False):raise RuntimeError('tplink_auto_control_not_qualified')
            row=self._read('DEV2_LTE_BAND_SEARCH',lane='event_write')
            mode=str(row.get('selectedMode'))
            if mode not in {'1','2'}:raise RuntimeError('tplink_auto_original_mode_unknown')
            current=row.get('selectedBand') if mode=='2' else row.get('supportBandList')
            other_rat='B' if target.domain=='nr_approved_auto' else 'N'
            preserved=bands(current,other_rat)
            if not preserved or not set(preserved).issubset(bands(row.get('supportBandList'),other_rat)):
                raise RuntimeError('tplink_auto_other_rat_selection_unavailable')
            umts=bands(row.get('selectedBand'),'W') if mode=='2' else []
            if not set(umts).issubset(bands(row.get('supportBandList'),'W')):
                raise RuntimeError('tplink_auto_umts_selection_unavailable')
            detail={'preserved_rat':other_rat,'preserved_bands':preserved,'preserved_umts_bands':umts}
            oid='DEV2_LTE_BAND_SEARCH';data={'selectedMode':'1'}
        elif isinstance(target,(CoreBandSetTarget,CoreNrBandTarget,CorePBandTarget)):
            if plan.preceding_mode is not None:
                observed = self._radio_mode_status('event_write', required=True)['radio_mode']
                expected = plan.preceding_mode.target.configuration()
                if observed != expected:
                    raise RuntimeError('tplink_nr_enabled_mode_not_confirmed')
            row=self._read('DEV2_LTE_BAND_SEARCH',lane='event_write')
            current=row.get('selectedBand') if row.get('selectedMode')=='2' else row.get('supportBandList')
            rat='N' if isinstance(target,CoreNrBandTarget) else 'B'
            requested=target.nr_bands if rat=='N' else (target.pband,) if isinstance(target,CorePBandTarget) else target.bands
            allowed=bands(row.get('supportBandList'),rat)
            if any(b not in allowed for b in requested):raise ValueError('tplink_band_not_supported_by_device')
            other=[t for t in str(current or '').split(';') if re.fullmatch(('B' if rat=='N' else 'N')+r'\d+',t)]
            umts=bands(row.get('selectedBand'),'W') if row.get('selectedMode')=='2' else []
            if isinstance(target,(CoreBandSetTarget,CoreNrBandTarget)) and target.release_manual:
                previous=plan.preceding_auto
                if previous is None:raise RuntimeError('tplink_auto_preceding_evidence_required')
                saved=previous.detail;other_rat='B' if rat=='N' else 'N'
                values=saved.get('preserved_bands')
                if (saved.get('preserved_rat')!=other_rat or not isinstance(values,(tuple,list)) or not values
                        or any(type(b) is not int or b<=0 for b in values)
                        or not set(values).issubset(bands(row.get('supportBandList'),other_rat))):
                    raise RuntimeError('tplink_auto_preserved_selection_invalid')
                other=[other_rat+str(b) for b in values]
                umts=saved.get('preserved_umts_bands')
                if (not isinstance(umts,(tuple,list))
                        or any(type(b) is not int or b<=0 for b in umts)):
                    raise RuntimeError('tplink_auto_preserved_umts_selection_invalid')
            if not set(umts).issubset(bands(row.get('supportBandList'),'W')):
                raise RuntimeError('tplink_umts_selection_unavailable')
            other=['W'+str(b) for b in umts]+other
            if not requested:raise ValueError('tplink_empty_band_selection_not_qualified')
            oid='DEV2_LTE_BAND_SEARCH';data={'selectedMode':'2','selectedBand':';'.join(other+[rat+str(b) for b in requested])}
        elif isinstance(target,CoreMobileDataTarget):
            oid='DEV2_LTE_WAN_CFG';data=mobile_data_payload(target.enabled)
        elif isinstance(target,CoreRestartTarget):
            operation='op';oid='ACT_REBOOT';stack=ZERO_STACK
        elif isinstance(target,CoreWifiRadioTarget):
            if plan.stage_kind!='wifi_radio' or plan.ordinal!=1:
                raise RuntimeError('tplink_wifi_stage_invalid')
            networks=self._wifi_networks('event_write')
            if target.band not in networks:raise RuntimeError('tplink_wifi_target_unavailable_or_ambiguous')
            stack,_enabled=networks[target.band]
            oid='DEV2_ADT_WIFI_COMMON';data={'primaryEnable':'1' if target.enabled else '0'}
        elif isinstance(target,CoreNetworkSettingsTarget):
            stages=network_stage_kinds(target,coordination_profile='tplink_dev2')
            if plan.ordinal>len(stages) or stages[plan.ordinal-1]!=plan.stage_kind:
                raise RuntimeError('network_stage_order_invalid')
            sim=self._read('DEV2_CELL_INTF_USIM',lane='event_write')
            if subscription_key(str(sim.get('IMSI','')))!=target.subscription_key:raise RuntimeError('network_subscription_changed_or_unavailable')
            wan=self._read('DEV2_LTE_WAN_CFG',lane='event_write')
            if target.apn and target.apn.creates_profile and not self._device_contract['apn_profile_creation']:
                raise RuntimeError('tplink_apn_creation_not_qualified')
            if plan.stage_kind=='network_data_hold' and target.apn:
                profiles=self._read('DEV2_LTE_PROFILE',stack=ZERO_STACK,listing=True,lane='event_write')
                if target.apn.creates_profile:
                    if any(p.get('profileName')==target.apn.name for p in profiles):
                        raise RuntimeError('tplink_apn_name_already_exists')
                elif sum(str(p.get('profileID'))==target.apn.profile_id for p in profiles)!=1:
                    raise RuntimeError('tplink_apn_native_profile_required')
                else:
                    index=next(i for i,p in enumerate(profiles) if str(p.get('profileID'))==target.apn.profile_id)
                    boundary=number(wan.get('defaultProfile'),low=0)
                    if boundary is None or index<boundary:raise RuntimeError('tplink_apn_factory_profile_read_only')
                username,password=open_apn_credentials(target.apn.protected_credentials)
                if username!=target.apn.username:raise ValueError('network_apn_credential_username_mismatch')
                if password is None:
                    if target.apn.creates_profile:raise RuntimeError('apn_explicit_password_required')
                    active=next(p for p in profiles if str(p.get('profileID'))==target.apn.profile_id)
                    if active.get('password') is None:raise RuntimeError('tplink_apn_password_unavailable')
            if plan.stage_kind!='network_data_hold' and str(wan.get('dataSwitchStatusBackup'))!='0':
                raise RuntimeError('network_data_hold_not_confirmed')
            if plan.stage_kind=='network_data_start':
                observed=self._network_observation().observation
                if target.roaming_enabled is not None and observed['roaming_enabled'] is not target.roaming_enabled:
                    raise RuntimeError('network_roaming_not_confirmed')
                if target.selection!='keep' and (observed['selection']!=target.selection or target.selection=='manual' and observed['target_plmn']!=target.target_plmn):
                    raise RuntimeError('network_selection_not_confirmed')
                if target.apn and not target.apn.matches_observation(observed['apn']):
                    raise RuntimeError('network_apn_not_confirmed')
            oid='DEV2_LTE_WAN_CFG'
            if plan.stage_kind=='network_roaming':data={'roamingEnabled':'1' if target.roaming_enabled else '0','profileAction':'0'}
            elif plan.stage_kind in {'network_data_hold','network_data_start'}:data=mobile_data_payload(plan.stage_kind=='network_data_start')
            elif plan.stage_kind=='network_operator':
                oid='DEV2_LTE_NETWORK_SEARCH';data={'selectedMode':'1' if target.selection=='auto' else '2'}
                if target.selection=='manual':
                    data['selectedOperator']=self._scan_operator(target.target_plmn)
            elif plan.stage_kind in {'network_apn_create','network_apn'} and target.apn:
                profiles=self._read('DEV2_LTE_PROFILE',stack=ZERO_STACK,listing=True,lane='event_write')
                creating=plan.stage_kind=='network_apn_create'
                if creating:
                    if not target.apn.creates_profile or any(p.get('profileName')==target.apn.name for p in profiles):
                        raise RuntimeError('tplink_apn_name_already_exists')
                    active={}
                else:
                    matches=[(i,p) for i,p in enumerate(profiles) if
                        (p.get('profileName')==target.apn.name if target.apn.creates_profile
                         else str(p.get('profileID'))==target.apn.profile_id)]
                    if len(matches)!=1:raise RuntimeError('tplink_apn_native_profile_required')
                    index,active=matches[0]
                    boundary=number(wan.get('defaultProfile'),low=0)
                    if boundary is None or index<boundary:raise RuntimeError('tplink_apn_factory_profile_read_only')
                username,password=open_apn_credentials(target.apn.protected_credentials)
                if username!=target.apn.username:raise ValueError('network_apn_credential_username_mismatch')
                if password is None:password=active.get('password')
                if password is None:raise RuntimeError('tplink_apn_password_unavailable')
                data={'profileAction':'2' if creating else '1','profileName':target.apn.name,
                    'apn':target.apn.apn,'apnType':'0','username':username,'password':password,
                    'authType':next(k for k,v in AUTH.items() if v==target.apn.authentication),'pdpType':next(k for k,v in PDP.items() if v==target.apn.ip_protocol)}
                if not creating:data.update(profileID=str(active['profileID']),activeProfile=str(index))
            else:raise RuntimeError('tplink_network_stage_not_qualified')
        else:raise RuntimeError('tplink_typed_target_not_qualified')
        operation,oid,data,stack=self._definition.prepare_dev2_write(
            operation+':'+oid,data,stack=stack)
        if isinstance(target, CoreRadioModeTarget):
            from arch4.modem_live.radio_mode_configuration import native_mode_for_key
            expected_native = native_mode_for_key(self._definition, target.mode_key or target.mode)
            if (operation != 'so' or oid != 'DEV2_LTE_WAN_CFG' or stack != ONE
                    or data != {'networkPreferredModeSelected': expected_native}):
                raise RuntimeError('tplink_radio_mode_native_template_changed_target')
        started=time.time();self._last_write_started=0.0;self._last_write_returned=0.0
        outcome='rejected';reason='tplink_command_rejected'
        try:
            self._dev2.request(operation,oid,data,stack=stack,lane='event_write')
            outcome='accepted';reason='accepted_for_fresh_readback'
        except (requests.RequestException,SessionExpiredError,Dev2ProtocolError,ModemTransportUnavailable):
            outcome='response_unavailable';reason='tplink_write_response_unavailable_no_replay'
        except Dev2Rejected as exc:reason=f'tplink_command_rejected:{exc.code}'
        return ModemLivePhysicalStageResult(plan=plan,request_dispatched_at=self._last_write_started or started,
            transport_returned_at=max(time.time(),self._last_write_returned),outcome=outcome,reason=reason,detail=detail)

    def _network_observation(self):
        steps=self._iter_network_settings_observation()
        try:
            while True:next(steps)
        except StopIteration as done:return done.value
        finally:steps.close()

    def _scan_operator(self,plmn):
        """Explicit native preparation for the authorized manual-selection stage.

        One scan, bounded reads, no retry or substitute operator. The stage
        guard has already checked SIM and stopped mobile data.
        """
        operation,oid,payload,stack=self._definition.prepare_dev2_write(
            'so:DEV2_LTE_NETWORK_SEARCH',{'action':'1'},stack=ONE)
        try:self._dev2.request(operation,oid,payload,stack=stack,lane='event_write')
        except Dev2Rejected as exc:raise RuntimeError('network_operator_scan_rejected') from exc
        except (requests.RequestException,SessionExpiredError,Dev2ProtocolError) as exc:
            raise RuntimeError('network_operator_scan_reply_unavailable') from exc
        deadline=time.monotonic()+30.0
        while time.monotonic()<deadline:
            row=self._read('DEV2_LTE_NETWORK_SEARCH',lane='event_write')
            if str(row.get('status'))=='2':break
            if str(row.get('status'))=='3':raise RuntimeError('network_operator_scan_rejected')
            time.sleep(.5)
        else:raise RuntimeError('network_operator_scan_timed_out')
        candidates={','.join(v.split(',')[:2]) for v in str(row.get('operatorList','')).split(';')
            if operator_plmn(v)==plmn and len(v.split(','))==3 and v.split(',')[2] in {'1','2'}}
        if len(candidates)!=1:raise RuntimeError('tplink_selected_network_not_in_unique_scan_result')
        return candidates.pop()

    def get_live_acquisition_contract(self):
        return LiveAcquisitionContract(acquisition_class='aligned_split',clocked_register_read=True,
            live_register_interval_s=self._profile.fast_live_interval_s,live_register_stale_s=self._profile.fast_live_stale_s,
            profile_name=self._profile_id,has_explicit_actionable_snapshot=True,has_explicit_maintenance_refresh=True,
            io_serial_required=True,startup_parallel_allowed=False,runtime_action_requires_io_lock=True,
            generic_neighbor_scan_enabled=False,maintenance_stepwise=True,maintenance_holds_io_lock=False,rtt_holds_io_lock=False,
            fast_read_groups=('radio_status','rf_quality','nr_status','traffic_presence','traffic_volume'),
            slow_read_groups=('network_mode',)+(('wifi_status',) if self._device_contract['capabilities']['supports_wifi_control'] else ()),static_capability_groups=('device_identity','firmware','supported_bands'),
            control_write_groups=(('band_selection','nr_band_lock','net_mode_write','login_token')
                if self._device_contract['capabilities']['supports_network_settings'] else ('band_selection','login_token'))
                +(('wifi_toggle',) if self._device_contract['capabilities']['supports_wifi_control'] else ())
                +(('pband',) if self._device_contract['capabilities']['supports_pband'] else ())
                +(('restart',) if self._device_contract['capabilities']['supports_restart'] else ()),
            export_groups=('sidecars','support_bundle','history'))
