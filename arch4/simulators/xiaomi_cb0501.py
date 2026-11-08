"""Evidence-labelled CB0501 MGDB boundary; never relabel the LuCI simulator.

Operator APK supplies login/identity/Wi-Fi. Optional RF/APN/traffic schemas are
from independently saved related-stock clients, not a physical CB0501 capture.
No production decoder, hashing or profile implementation is imported.
"""
from copy import deepcopy
import hashlib
import hmac
import json
import re
import time

from arch4.simulators.xiaomi_cb0401v2 import serve


class XiaomiMgdbAppliance:
    def __init__(self, *, firmware='3.0.10', password='synthetic-router-password'):
        self.password = password
        self.username = 'admin'
        self.logged_in = False
        self.calls = []
        self.refused = {}
        self.malformed = set()
        self.delays = {}
        self.response_headers = ()
        self.cookie = 'synthetic_mgdb_session'
        self.login_info = None
        self.identity = dict(device_product_name='CB0501', device_software_version=firmware,
            device_hardware_version='HW1.0', device_sn='SYNTHETIC_CB0501_1', device_imei='123456789012345')
        self.traffic = dict(statistics_rx_bytes_rate=1000000, statistics_tx_bytes_rate=125000)
        self.wifi = dict(wifi_state_0='ap_disable', wifi_state_1='ap_enable')
        self.wan = dict(device_uptime=120, mnet_sim_status='ready')
        self.signal = dict(rssi=-60, rsrp=-89, rsrq=-12, sinr=150, sinr_ex='15.0', pci=95)
        self.network = dict(network_type='LTE', lte_pci=95, cell_id='37',
            operator_name='MTS', plmn='22003', pcc_band='b3', scc_band_1=0, ca_state=0)
        self.sim = dict(imsi='220031234567890', iccid='8900000000000000000')
        self.apn = dict(curid='2', profileList=[dict(index='1', name='inactive', apn='wrong'),
            dict(index=2, name='MTS', apn='gprswap', username='mts', password='encrypted-opaque', authmode=1, iptype=1)])
        self.connection = dict(dataswitch=1, roamingswitch=0)
        # Optional related manufacturer WebUI route. None models an exact
        # firmware without this route; never claim physical CB0501 proof.
        self.carrier_rows = None
        # R28.9: independent manufacturer-WebUI/PINSU MGDB formats. These
        # are related-firmware protocol evidence, not physical CB0501 proof.
        self.band_settings = dict(mnet_band_lock_list='lte:B1,B3,B7,B8,B20,B28,B32,B38',
            mnet_band_lock_type='unlock,normal', mnet_band_lock_info='')
        self.cell_settings = 'LTE,0,0,0,0'
        self.hold_control_readback = False
        self.control_reply = None
        self.native_overrides = {}
        self.native_missing = set()
        self.native_uppercase = False

    @staticmethod
    def reply(value):
        return 200, json.dumps(value).encode(), 'application/json'

    def handle_http(self, method, path, body, headers):
        self.response_headers = ()
        if path not in {'/goform/login'} and headers.get('Cookie', '') != '-webs-session-=' + self.cookie:
            self.calls.append((method, path, body))
            return self.reply({'retcode': 401})
        return self.handle(method, path, body)

    def handle(self, method, path, body):
        self.calls.append((method, path, body))
        if path in self.delays:
            time.sleep(self.delays[path])
        if path in self.refused:
            return self.reply({'retcode': self.refused[path]})
        if path in self.malformed:
            return 200, b'<html>unrecognized reply</html>', 'text/html'
        payload = json.loads(body) if body else None
        if (method, path) == ('POST', '/goform/login'):
            digest = lambda value: hmac.new(b'0123456789', value.encode('ascii'), hashlib.md5).hexdigest()
            if payload != dict(username=digest(self.username), password=digest(self.password)):
                return self.reply({'retcode': 1})
            self.logged_in = True
            self.response_headers = (('Set-Cookie', '-webs-session-=' + self.cookie + '; Path=/; HttpOnly'),)
            return self.reply({'retcode': 0})
        if not self.logged_in:
            return self.reply({'retcode': 401})
        if (method, path) == ('GET', '/goform/get_login_info'):
            # Actual CB0501 3.0 owner capture,6Nov2026: loginUser is the same
            # HMAC representation sent at login; loginAlias is display text.
            encoded_user = hmac.new(b'0123456789', self.username.encode('ascii'), hashlib.md5).hexdigest()
            return self.reply(deepcopy(self.login_info) if self.login_info is not None else
                dict(retcode=0, loginStatus=1, loginUser=encoded_user, loginRole=1, loginAlias=self.username))
        if (method, path) == ('POST', '/action/get_mgdb_params'):
            selected=self.apn['curid']
            apns={f'dialup_profile_{row["index"]}':','.join(str(row.get(k,'')) for k in ('name','apn','username','password'))+',pap,ipv4,preset' for row in self.apn['profileList']}
            values = {**self.identity, **self.traffic, **self.wifi, **self.wan, **self.band_settings,
                'mnet_cell_lock_info':self.cell_settings,
                'mnet_sysmode':self.network.get('network_type'), 'mnet_operator_name':self.network.get('operator_name'),
                'mnet_reg_plmn':self.network.get('plmn'), 'mnet_wnw_pci':self.signal.get('pci'),
                'mnet_wnw_cellid':self.network.get('cell_id'), 'mnet_wnw_band':self.network.get('pcc_band'),
                'mnet_wnw_dlch':self.network.get('earfcn',1795),'mnet_wnw_ulch':19795,
                'mnet_rsrp':self.signal.get('rsrp')+141 if self.signal.get('rsrp') is not None else None,
                'mnet_rsrq':self.signal.get('rsrq')*2+40 if self.signal.get('rsrq') is not None else None,
                'mnet_rssi':self.signal.get('rssi')+111 if self.signal.get('rssi') is not None else None,
                'mnet_snr':float(self.signal['sinr_ex'])*5+101 if self.signal.get('sinr_ex') is not None else None,
                # Separate native fields: firmware rfparamters.js displays
                # mnet_sinr directly; mnet_snr is a different encoded measure.
                'mnet_sinr':self.signal.get('sinr_ex'),
                'mnet_sim_imsi':self.sim.get('imsi'), 'mnet_ca_act_status':'yes' if self.network.get('ca_state') else 'no',
                'rt_wwan_conn_info':'connected,37,198.51.100.2,255.255.255.0,198.51.100.1,bmwan0,modem,1,0',
                'dialup_profile_default':selected,'dialup_roamswitch':str(self.connection['roamingswitch']),**apns}
            values.update(self.native_overrides)
            data={k: deepcopy(values[k]) for k in payload['keys'] if k in values and k not in self.native_missing}
            if self.native_uppercase:
                return self.reply(dict(RETCODE=' 0 ', DATA={k.upper():v for k,v in data.items()}))
            return self.reply(dict(retcode=0, data=data))
        if (method,path)==('POST','/action/mnet_set_band_lock_switch'):
            if set(payload)!={'mnet_band_lock_type','mnet_band_lock_info'}:
                return self.reply({'retcode':1})
            mode=payload['mnet_band_lock_type'];info=payload['mnet_band_lock_info']
            if not (mode=='unlock,normal' and info=='' or mode=='lock,normal' and re.fullmatch(r'lte:[1-9][0-9]*(?:,[1-9][0-9]*)*',info)):
                return self.reply({'retcode':1})
            if not self.hold_control_readback:
                self.band_settings.update(payload)
                if mode=='lock,normal':self.network['pcc_band']='B'+info[4:].split(',')[0]
            return self.reply(self.control_reply if self.control_reply is not None else {'retcode':0})
        if (method,path)==('POST','/action/mnet_set_celllock_switch'):
            if payload=={'mnet_cell_lock_rat':'13','mnet_cell_lock_type':'1'}:
                if not self.hold_control_readback:self.cell_settings='LTE,0,0,0,0'
            elif (set(payload)=={'mnet_cell_lock_rat','mnet_cell_lock_type','mnet_cell_lock_arfcn','mnet_cell_lock_pci'}
                    and payload['mnet_cell_lock_rat']=='3' and payload['mnet_cell_lock_type']=='1'
                    and payload['mnet_cell_lock_arfcn'].isdecimal() and 0<=int(payload['mnet_cell_lock_arfcn'])<=262143
                    and payload['mnet_cell_lock_pci'].isdecimal() and 0<=int(payload['mnet_cell_lock_pci'])<=503):
                if not self.hold_control_readback:
                    self.cell_settings='LTE,1,1,'+payload['mnet_cell_lock_arfcn']+','+payload['mnet_cell_lock_pci']
                    self.network['earfcn']=int(payload['mnet_cell_lock_arfcn']);self.signal['pci']=int(payload['mnet_cell_lock_pci'])
            else:return self.reply({'retcode':1})
            return self.reply(self.control_reply if self.control_reply is not None else {'retcode':0})
        if (method,path)==('POST','/action/mnet_get_ca_list'):
            return self.reply({'retcode':404} if self.carrier_rows is None else
                {'retcode':0,'data':{'mnet_ca_list':deepcopy(self.carrier_rows)}})
        rows = {'/action/get_signal_info': self.signal, '/action/get_mobile_network_info': self.network,
            '/goform/get_sim_info': self.sim, '/action/get_dialup_profile': self.apn,
            '/action/get_mobile_connection_cfg': self.connection}
        if method == 'POST' and path in rows:
            return self.reply(dict(retcode=0, **deepcopy(rows[path])))
        return self.reply({'retcode': 404})
