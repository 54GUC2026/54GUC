"""Independent stock-LuCI appliance. All target-native behavior is synthetic.

No production decoder, protocol, profile, or hashing implementation imports.
"""
from contextlib import contextmanager
import argparse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

SCHEMA = 'XiaomiStockSimulator.v1'
KEY = 'a2ffa5c9be07488bbb04a3a47d3c5f6a'
MAC = '00:11:22:33:44:55'


class XiaomiStockAppliance:
    def __init__(self, *, mode=1, password='synthetic-router-password', firmware='3.0.88', reselection_pause_s=None):
        if type(mode) is not int or mode not in {0, 1}:
            raise ValueError('xiaomi_appliance_hash_mode_required')
        if reselection_pause_s is not None and (type(reselection_pause_s) not in {int,float}
                or not math.isfinite(reselection_pause_s) or not 0<=reselection_pause_s<=60):
            raise ValueError('finite_reselection_pause_required')
        self.reselection_pause_s=reselection_pause_s
        self.reselection_until=0.;self.service_up=True;self.service_changed=False
        self.schema = SCHEMA
        self.assumptions = '3.0.88 inheriting stock 3.0.85 frontend; no physical qualification'
        self.mode, self.password = mode, password
        self.token = 'synthetic_session_1234'
        self.logged_in = False
        self.calls = []
        self.used_nonce = set()
        self.refused = {}
        self.malformed = set()
        self.write_code = 0
        self.apply_writes = True
        self.identity = {'code': 0, 'hardware': 'CB0401V2', 'model': 'xiaomi.router.cb0401v2',
            'romversion': firmware, 'hwversion': 'HW1.0', 'id': 'SYNTHETIC_XIAOMI_1', 'newEncryptMode': mode}
        self.newstatus = {'code': 0, 'hardware': {'temperature': 0}, 'router': {'uptime': 120}}
        self.radio = {'code': 0, 'net': {'code': 0, 'info': {'linktype': '5G NSA', 'operator': 'MTS', 'plmn': '22003',
            'cell_band': 'B3', 'cell_band_5g': 'n78', 'rsrp': -87, 'rsrq': -11, 'snr': 15,
            'rsrp_5g': -83, 'rsrq_5g': -9, 'snr_5g': 21, 'ci': 101634, 'pci': 175, 'datausage': 2.1}}}
        self.mobile_info = dict(self.radio['net']['info'])
        # Stock selected arrays are positional flags, not band numbers.
        self.bands = {'code': 0, 'list': {'list_4G': [20, 3, 1, 7, 28], 'band_4G': ['1', '1', '1', '0', '0'],
            'list_5G': [78, 28], 'band_5G': ['1', '0']}}
        self.sim = {'code': 0, 'info': {'imsi': '220031234567890', 'simstatus': 'ready'}}
        self.cfg = {'code': 0, 'networkdata': 1, 'networkroam': 0, 'networkauto': 1, 'networktype': 'auto'}
        self.apn = {'code': 0, 'curid': '1', 'apnlist': [{'id': '1', 'file': 'MTS', 'pdp': 'ip', 'apn': 'gprswap',
            'user': 'mts', 'passwd': 'synthetic-apn-password', 'encryption': 'pap'}]}
        self.radio_template=deepcopy(self.radio['net']['info'])
        self.wan_template=None

    def reselect(self):
        if self.reselection_pause_s is None:return
        self.wan_template=deepcopy(self.radio['net'].get('ipv4info',self.wan_template))
        self.service_changed=True
        self.reselection_until=time.monotonic()+self.reselection_pause_s
        self.refresh_service()

    def refresh_service(self):
        if self.reselection_pause_s is None or not self.service_changed:return
        mode=self.cfg['networktype'];bands=self.bands['list']
        lte=any(str(flag)=='1' and 'B'+str(band)==self.radio_template['cell_band'] for band,flag in zip(bands['list_4G'],bands['band_4G']))
        nr=any(str(flag)=='1' and 'n'+str(band)==self.radio_template['cell_band_5g'] for band,flag in zip(bands['list_5G'],bands['band_5G']))
        if mode=='4g':nr=False
        elif mode=='sa':lte=False
        elif not lte:nr=False  # The declared NSA template needs its LTE anchor.
        up=self.service_up and self.cfg['networkdata']==1 and time.monotonic()>=self.reselection_until
        lte=lte and up;nr=nr and up
        info=deepcopy(self.radio_template)
        info['linktype']='5G NSA' if lte and nr else '5G SA' if nr else '4G' if lte else 'No Service'
        if not lte:
            for field in ('cell_band','rsrp','rsrq','snr','ci','pci'):info[field]=''
        if not nr:
            for field in ('cell_band_5g','rsrp_5g','rsrq_5g','snr_5g'):info[field]=''
        self.radio['net']['info']=info;self.mobile_info=dict(info)
        if not lte and not nr:self.radio['net'].pop('ipv4info',None)
        elif self.wan_template is not None:self.radio['net']['ipv4info']=deepcopy(self.wan_template)

    def handle(self, method, path, body):
        self.refresh_service()
        self.calls.append((method, path, body))
        parsed = urlsplit(path)
        if parsed.path == '/__lab/control':
            if method != 'POST' or len(body) > 4096:
                return self.reply({'code': 405})
            try:
                command = json.loads(body)
                if not isinstance(command, dict) or set(command) != {'action', 'value'}:
                    raise ValueError('invalid command')
                action, value = command['action'], command['value']
                if action == 'expire_session' and value is True:
                    self.logged_in = False
                elif action == 'mobile_service' and type(value) is bool:
                    self.service_up=value
                    self.radio['net']['info']['linktype'] = '5G NSA' if value else 'No Service'
                    if not value: self.radio['net'].pop('ipv4info', None)
                    else: self.radio['net']['ipv4info'] = {'ipv4': [{'ip': '10.123.45.6'}]}
                    if self.reselection_pause_s is not None:
                        self.service_changed=True
                        if value:self.wan_template=deepcopy(self.radio['net']['ipv4info'])
                        self.refresh_service()
                elif action == 'identity_available' and type(value) is bool:
                    self.identity['id'] = 'SYNTHETIC_XIAOMI_1' if value else ''
                elif action == 'apply_writes' and type(value) is bool:
                    self.apply_writes = value
                else:
                    raise ValueError('unknown command')
                return self.reply({'code': 0, 'synthetic_control': action})
            except (ValueError, TypeError):
                return self.reply({'code': 22})
        if parsed.path == '/cgi-bin/luci/web' and method == 'GET':
            return 200, ('var Encrypt={key: "' + KEY + '"};var deviceId="' + MAC + '";').encode(), 'text/html'
        if parsed.path == '/cgi-bin/luci/api/xqsystem/init_info' and method == 'GET':
            return self.reply(self.identity)
        if parsed.path == '/cgi-bin/luci/api/xqsystem/login' and method == 'POST':
            values = parse_qs(body.decode())
            nonce = values.get('nonce', [''])[0]
            digest = hashlib.sha256 if self.mode else hashlib.sha1
            expected = digest((nonce + digest((self.password + KEY).encode()).hexdigest()).encode()).hexdigest()
            shape = re.fullmatch(r'0_' + MAC + r'_\d+_\d{1,4}', nonce)
            if not shape or nonce in self.used_nonce or values.get('username') != ['admin'] or values.get('logtype') != ['2'] or values.get('password') != [expected]:
                return self.reply({'code': 401})
            self.used_nonce.add(nonce)
            self.logged_in = True
            return self.reply({'code': 0, 'token': self.token})
        match = re.fullmatch(r'/cgi-bin/luci/;stok=([^/]+)/api/(.+)', parsed.path)
        if not match or match[1] != self.token or not self.logged_in:
            return self.reply({'code': 403})
        endpoint = match[2]
        if endpoint in self.malformed:
            return 200, b'<html>unrecognized shape</html>', 'text/html'
        if endpoint in self.refused:
            return self.reply({'code': self.refused[endpoint]})
        if endpoint == 'xqmobile/set_mobile_net_cfg':
            fields = parse_qs(parsed.query)
            if (method != 'GET' or body or set(fields) != {'networktype', 'networkdata', 'networkroam'}
                    or fields['networktype'][0] not in {'auto', 'sa', 'nsa', '4g'}
                    or any(fields[name] not in [['0'], ['1']] for name in ('networkdata', 'networkroam'))):
                return self.reply({'code': 22})
            if not self.write_code and self.apply_writes:
                changed=any(str(self.cfg[key])!=fields[key][0] for key in ('networktype','networkdata','networkroam'))
                self.cfg.update(networktype=fields['networktype'][0], networkdata=int(fields['networkdata'][0]),
                    networkroam=int(fields['networkroam'][0]))
                if changed:self.reselect()
            return self.reply({'code': self.write_code})
        if endpoint == 'xqmobile/set_cellular_band' and method == 'POST':
            fields = parse_qs(body.decode())
            if set(fields) != {'band_4G', 'band_5G'}:
                return self.reply({'code': 22})
            try:
                lte, nr = [set(map(int, fields[k][0].split(','))) for k in ('band_4G', 'band_5G')]
            except ValueError:
                return self.reply({'code': 22})
            data = self.bands['list']
            if not lte or not nr or not lte <= set(data['list_4G']) or not nr <= set(data['list_5G']):
                return self.reply({'code': 22})
            if not self.write_code and self.apply_writes:
                before=(list(data['band_4G']),list(data['band_5G']))
                data['band_4G'] = ['1' if b in lte else '0' for b in data['list_4G']]
                data['band_5G'] = ['1' if b in nr else '0' for b in data['list_5G']]
                if before!=(data['band_4G'],data['band_5G']):self.reselect()
            return self.reply({'code': self.write_code})
        # Independently transcribed from the stock mobileNetwork.js.htm read:
        # no parameters, mode=1 means automatic. This does not set the mode.
        if endpoint == 'xqmobile/get_search_net_mode':
            if method != 'POST' or body:
                return self.reply({'code': 405})
            return self.reply({'code': 0, 'mode': 1, 'select_name': ''})
        if method != 'GET':
            return self.reply({'code': 405})
        replies = {'xqsystem/init_info': self.identity, 'xqdtcustom/cpe_detect': self.radio,
            'xqdtcustom/simple_cpe_detect': self.radio, 'xqmobile/get_mobile_net_info': {'code': 0, 'info': self.mobile_info},
            'xqdtcustom/get_mobile_net_info': {'code': 0, 'info': self.mobile_info},
            'xqmobile/get_cellular_band_list': self.bands, 'xqdtcustom/get_sim_info': self.sim,
            'xqmobile/get_mobile_net_cfg': self.cfg, 'xqmobile/get_apn_info': self.apn,
            'xqdtcustom/newstatus': self.newstatus,
            'xqmobile/get_data_usage_info': {'code': 0, 'datausage': 2.1}}
        return self.reply(replies.get(endpoint, {'code': 404}))

    @staticmethod
    def reply(value):
        return 200, json.dumps(value).encode(), 'application/json'


@contextmanager
def serve(appliance, *, port=0, event_log=None):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('xiaomi_simulator_port_invalid')
    journal = None
    if event_log is not None:
        path = Path(event_log)
        path.parent.mkdir(parents=True, exist_ok=True)
        journal = path.open('x', encoding='utf-8')
    event_lock = threading.Lock()
    sequence = 0
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self): self.respond()
        def do_POST(self): self.respond()
        def respond(self):
            nonlocal sequence
            started = time.time()
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            status, data, content_type = appliance.handle(self.command, self.path, body)
            if journal is not None:
                with event_lock:
                    sequence += 1
                    journal.write(json.dumps(dict(schema=SCHEMA, sequence=sequence, started_at=started,
                        returned_at=time.time(), method=self.command,
                        path=re.sub(r';stok=[^/]+', ';stok=WITHHELD', self.path),
                        http_status=status, reply_bytes=len(data), qualification='synthetic')) + '\n')
                    journal.flush()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args): pass
    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except Exception:
        if journal is not None: journal.close()
        raise
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        if journal is not None: journal.close()


def main():
    parser = argparse.ArgumentParser(description='Independent Xiaomi stock hypothesis appliance; loopback only.')
    parser.add_argument('--port', type=int, default=8313)
    parser.add_argument('--event-log', required=True)
    parser.add_argument('--reselection-pause-s',type=float,default=None)
    args = parser.parse_args()
    appliance = XiaomiStockAppliance(reselection_pause_s=args.reselection_pause_s)
    appliance.radio['net']['ipv4info'] = {'ipv4': [{'ip': '10.123.45.6'}]}
    with serve(appliance, port=args.port, event_log=args.event_log) as url:
        print(json.dumps({'schema': SCHEMA, 'url': url, 'qualification': appliance.assumptions}), flush=True)
        try:
            stop = threading.Event()
            while not stop.wait(0.25):
                pass
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
