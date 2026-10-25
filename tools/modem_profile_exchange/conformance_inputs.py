"""Synthetic native replies and existing-driver oracles for offline conformance.

This module is test/authoring support only. It is not imported by JSON replay.
Transport seams supply native replies; production parsing is not mocked.
"""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

from tools.modem_profile_exchange.export_profiles import field_address, ROOT


NOW=2000000000.0


def canonical_values(modem,snapshot,status):
    result={}
    if snapshot is not None:
        for name,value in asdict(snapshot).items():
            try:result[field_address(name)]=value
            except ValueError:pass
        for name in ('ca_band_signature','carrier_count'):
            try:result[field_address(name)]=getattr(snapshot,name)
            except (ValueError,AttributeError):pass
    for name,value in status.items():
        # These presentation aliases carry text with units. The explicit
        # typed field (even None) is the canonical measurement, independent
        # of public-status dictionary iteration order.
        if name in {'dl_bandwidth','ul_bandwidth'} and (
                name+'_mhz' in status or snapshot is not None and hasattr(snapshot,name+'_mhz')):
            continue
        try:result[field_address(name)]=value
        except ValueError:pass
    # The selected NR reader publishes an explicit typed status alias. Its
    # missing value must override ModemInput's legacy numeric default.
    definition=getattr(modem,'_definition',None)
    if (definition is not None and definition.has_read('slow.nr_bandwidth_mhz')
            and 'nr5g_nsa_bandwidth' in status and 'nr_bandwidth_mhz' not in status):
        result['slow.nr_bandwidth_mhz']=status['nr5g_nsa_bandwidth']
    return result


def base_context():
    return {'sampled_at':NOW,'monotonic_at':NOW,'counter_sampled_at':NOW,
        'resource_sampled_at':NOW,'monitor_sampled_at':NOW,
        'provider_names':{'22001':'Yettel','22003':'MTS'},
        'provider_plmn_by_name':{'yettel':'22001','mts':'22003'},
        'provider_bands':[1,3,7,20,28],'supported_lte_bands':[1,3,7,20,28],
        'previous_qm':{},'traffic_previous':{},'volume_previous':{},
        'previous_wifi':[None,None,None],'qca_carriers':[],
        'wifi_observation':{},'counter_transition_status':'baseline','monitor_interval_s':5.0}


def goform_replies():
    return {'network_type':'ENDC','ppp_status':'ipv4_connected','ppp_uptime':'01:02:03',
        'wan_active_band':'B3+B1+B20','lte_band_lock':'0x80005','wan_lte_ca':'1',
        'lte_rsrp':'-86','lte_rsrq':'-9','lte_rssi':'-58','lte_snr':'12',
        'lte_pci':'303','lte_earfcn':'1300','cell_id':'123456','tac':'112',
        'lte_ca_pcell_bandwidth':'20','mccmnc':'22001','network_provider':'Yettel',
        'wan_ipaddr':'192.0.2.10','wifi_onoff_state':'1',
        'model_name':'TEST_MODEL','serial_number':'SYNTHETIC_SERIAL',
        'imei':'000000000000000','sim_imsi':'000000000000000',
        'hardware_version':'TEST_HARDWARE','wa_inner_version':'TEST_FIRMWARE',
        'web_version':'TEST_WEBUI','cr_version':'TEST_CONFIG',
        'nr_ca_pcell_band':'78','nr5g_pci':'101','nr_ca_pcell_freq':'641760',
        'nr5g_cell_id':'1234567','Z5g_rsrp':'-89','Z5g_rsrq':'-10','Z5g_SINR':'17',
        'nr5g_nsa_bandwidth':'100','5g_rx0_rsrp':'-89','5g_rx1_rsrp':'-91',
        'pm_sensor_ambient':'30','pm_sensor_mdm':'40','pm_sensor_5g':'42',
        'pm_sensor_pa1':'45','wifi_chip_temp':'44','tx_power':'12',
        'realtime_rx_thrpt':'12000','realtime_tx_thrpt':'6000',
        'monthly_rx_bytes':'1234567','monthly_tx_bytes':'765432'}


def goform_case(profile_id):
    from arch4.modem.zte_goform import ZteGoformModem
    from arch4.modem_live.profile_catalog import modem_profile_record,modem_profile_selection
    raw=goform_replies()
    p=modem_profile_record(profile_id)
    raw.update(model_name=p['target_guard_model'] or next(iter(p['model_contains']),'TEST_MODEL'),
        wa_inner_version=p['firmware_exact'] or ' '.join(p['firmware_contains']) or 'TEST_FIRMWARE')
    modem=ZteGoformModem(host='127.0.0.1',username='',password='',backend_name=modem_profile_selection(profile_id).backend,profile_id=profile_id)
    modem._connected=True
    modem._last_raw=deepcopy(raw)
    snapshot=modem._build_modem_input_from_raw(deepcopy(raw),traffic_observed_at=NOW)
    result=canonical_values(modem,snapshot,modem._last_status)
    result['rf.cqi']=snapshot.cqi  # declared synthetic inference, not native CQI0
    result['radio.carrier_count']=len(modem._last_status['configured_band_pool'])
    return {'goform':raw},base_context(),result


def xml(values):
    import xml.etree.ElementTree as ET
    root=ET.Element('response')
    for key,value in values.items():ET.SubElement(root,key).text=str(value)
    return ET.tostring(root,encoding='unicode')


def huawei_replies():
    return {'/api/device/signal':xml({'sinr':'12dB','rsrp':'-86dBm','rsrq':'-9dB','rssi':'-58dBm',
        'pci':'303','earfcn':'1300','cell_id':'123456','tac':'112','band':'3',
        'dlbandwidth':'20MHz','ulbandwidth':'20MHz','dlfrequency':'1815MHz','ulfrequency':'1720MHz',
        'dl_mcs':'Code0:17 Code1:18','ul_mcs':'19','ri':'2','cqi0':'12','cqi1':'11',
        'txpower':'12dBm','rrc':'1','transmode':'4','nrsinr':'17dB','nrrsrp':'-89dBm',
        'nrrsrq':'-10dB','nrearfcn':'641760','nrdlbandwidth':'100MHz'}),
        '/api/monitoring/status':xml({'ConnectionStatus':'901','CurrentNetworkTypeEx':'1011'}),
        '/api/monitoring/traffic-statistics':xml({'CurrentDownloadRate':'12000','CurrentUploadRate':'6000'}),
        '/api/net/current-plmn':xml({'Numeric':'22001','ShortName':'Yettel','FullName':'Yettel'}),
        '/api/net/net-mode':xml({'LTEBand':'80005','NetworkBand':'3FFFFFFF','NetworkMode':'03'}),
        '/api/net/net-mode-list':'<response><LTEBand><LTEBand>80045</LTEBand></LTEBand></response>',
        '/api/device/information':xml({'DeviceName':'SYNTHETIC_ROUTER','SerialNumber':'SYNTHETIC_SERIAL',
            'HardwareVersion':'TEST_HW','SoftwareVersion':'TEST_FW','WebUIVersion':'TEST_WEBUI',
            'Imei':'000000000000000','Imsi':'000000000000000','LanMacAddress':'02:00:00:00:00:01',
            'WanIPAddress':'192.0.2.10'}),
        '/api/device/boot_time':xml({'boot_time':'00:01:02:03'}),
        '/api/dialup/mobile-dataswitch':xml({'dataswitch':'1'}),
        '/api/device/antenna_set_type':xml({'antennasettype':'1'}),
        '/api/wlan/multi-basic-settings':'<response><Ssids><Ssid><WifiEnable>1</WifiEnable><Index>0</Index></Ssid><Ssid><WifiEnable>0</WifiEnable><Index>1</Index></Ssid></Ssids></response>'}


def huawei_case(profile_id):
    from arch4.modem.huawei_b818 import HuaweiHiLinkModem
    from arch4.modem_live.profile_catalog import modem_profile_selection
    modem=HuaweiHiLinkModem(host='127.0.0.1',username='',password='',profile_id=profile_id,
        backend_name=modem_profile_selection(profile_id).backend)
    modem._connected=True
    replies=huawei_replies()
    from arch4.modem_live.profile_catalog import modem_profile_record
    p=modem_profile_record(profile_id)
    info={'DeviceName':next(iter(p['model_contains']),'B818-263'),
        'SerialNumber':'SYNTHETIC_SERIAL','HardwareVersion':p['hardware_exact'] or 'TEST_HW',
        'SoftwareVersion':next(iter(p['firmware_contains']),'TEST_FW'),
        'WebUIVersion':'TEST_WEBUI','Imei':'000000000000000','Imsi':'000000000000000',
        'LanMacAddress':'02:00:00:00:00:01','WanIPAddress':'192.0.2.10'}
    replies['/api/device/information']=xml(info)
    modem._api_get=lambda path,*a,**k:deepcopy(replies.get(path))
    # Existing selected-profile metadata controls admission outside this parser.
    modem.bind_provider_pool_policy(lambda **k:{'schema':'FoundationProviderBandProfile.v1',
        'owner':'54guc.foundation.band_auto_policy','rat':'lte','band_auto_provider_plmn':'22001','bands':[1,3,7,20,28]})
    snapshot=modem._read_modem_input_impl(actionable_only=False)
    assert snapshot is not None
    status=dict(modem._last_status)
    from arch4.modem.huawei_network_settings import read_mobile_data
    try:status.update(read_mobile_data(modem))
    except RuntimeError:pass  # unqualified dialects have no periodic mobile-data producer
    values=canonical_values(modem,snapshot,status)
    from arch4.interfaces import band_signature
    values['radio.ca_signature']=status['configured_band_pool_signature']
    values['radio.active_band_set']=values['radio.band_set']=[snapshot.primary_band] if snapshot.primary_band else []
    return replies,base_context(),values


def dev2_case(profile_id):
    from arch4.modem.tplink_dev2 import TplinkDev2Modem
    modem=TplinkDev2Modem(profile_id=profile_id,host='127.0.0.1',username='',password='')
    fixture='nx520_srba1_250902_native.json' if '250902' in profile_id else 'nx520v_260408.json' if 'nx520v' in profile_id else 'nx520_srba1_20250915_assumption.json'
    data=json.loads((ROOT/'arch4/tests/fixtures'/fixture).read_text())['objects']
    multi={'DEV2_LTE_SERVING_CELL_INFO','DEV2_ADT_WAN','DEV2_ADT_WIFI_COMMON','DEV2_ADT_LAN','DEV2_LTE_PROFILE'}
    replies={oid:([{**r,'stack':stack} for stack,r in rows.items()] if oid in multi else next(iter(rows.values()),{})) for oid,rows in data.items()}
    # These sibling fixtures include no active LTE or omit scheduler slots.
    # Add a labelled synthetic LTE row using the real decoder's native names;
    # no statement about that sibling's physical firmware is made.
    native=json.loads((ROOT/'arch4/tests/fixtures/nx520_srba1_250902_native.json').read_text())['objects']['DEV2_LTE_SERVING_CELL_INFO']
    lte=deepcopy(next(r for r in native.values() if r.get('networkType') in {'3','7'} and r.get('cellConnectionStatus')=='1'))
    lte.update(RI='2',CQI='12',downMCS='17',upMCS='19',RSSI='-58',downBandWidth='20000',upBandWidth='20000',stack='1,1,0,0,0,0')
    rows=replies['DEV2_LTE_SERVING_CELL_INFO']
    primary=next((r for r in rows if r.get('networkType') in {'3','7'} and r.get('cellConnectionStatus')=='1'),None)
    if primary is None:rows.insert(0,lte)
    else:
        for key in ('RI','CQI','downMCS','upMCS','RSSI','downBandWidth','upBandWidth'):
            primary[key]=lte[key]
    modem._read=lambda oid,*a,**k:deepcopy(replies.get(oid,[] if k.get('listing') else {}))
    modem.bind_provider_pool_policy(lambda **k:{'schema':'FoundationProviderBandProfile.v1',
        'owner':'54guc.foundation.band_auto_policy','rat':'lte','band_auto_provider_plmn':k['provider_plmn'],'bands':[1,3,7,20,28]})
    modem.read_fresh_device_identity()
    snapshot=modem.read_modem_input()
    status=dict(modem._status)
    if 'network_settings' in modem._device_contract['supported_fields']:
        status['network_settings']=generator_result(modem._iter_network_settings_observation()).observation
    values=canonical_values(modem,snapshot,status)
    from arch4.interfaces import band_signature
    for address,source in [('radio.ca_signature','configured_band_pool'),('radio.selected_band_set_signature','selected_band_set'),('radio.active_band_set_signature','active_band_set')]:
        values[address]=band_signature(status.get(source,[]))
    for name in ('rx_bytes_per_sec','tx_bytes_per_sec'):
        values['traffic.'+name]=status.get(name)
    return replies,base_context(),values


def xiaomi_case(profile_id, *, complete_owner_reads=False):
    from arch4.modem.xiaomi_miwifi import XiaomiMiWiFiModem
    from arch4.simulators.xiaomi_cb0401v2 import XiaomiStockAppliance
    sim=XiaomiStockAppliance()
    sim.radio['net']['info'].update(rssi='-63',pci='175',earfcn='1300')
    sim.sim={'code':0,'imsi':'220031234567890'}
    replies={'xqdtcustom/cpe_detect':sim.radio,'xqmobile/get_cellular_band_list':sim.bands,
        'xqdtcustom/get_sim_info':sim.sim,'xqmobile/get_apn_info':sim.apn,'xqmobile/get_mobile_net_cfg':sim.cfg}
    if complete_owner_reads:replies['xqsystem/init_info']=deepcopy(sim.identity)
    modem=XiaomiMiWiFiModem(host='127.0.0.1',username='',password='')
    modem._read=lambda endpoint,*a,**k:deepcopy(replies[endpoint])
    identity=modem.read_fresh_device_identity()if complete_owner_reads else{}
    sim_status=modem._sim_status('startup')if complete_owner_reads else{}
    snapshot=modem._radio_read()
    modem.bind_provider_pool_policy(lambda **k:{'schema':'FoundationProviderBandProfile.v1',
        'owner':'54guc.foundation.band_auto_policy','rat':'lte','band_auto_provider_plmn':k['provider_plmn'],'bands':[1,3,7,20,28]})
    status={**modem._fresh_status,**modem._bands('startup')}
    status['network_settings']=generator_result(modem._iter_network_settings_observation()).observation
    values=canonical_values(modem,snapshot,{**identity,**status,**sim_status})
    if complete_owner_reads:
        for name in ('dl_bandwidth_mhz','ul_bandwidth_mhz'):
            values['slow.'+name]=status.get(name)
    return replies,base_context(),values


def generator_result(generator):
    while True:
        try:next(generator)
        except StopIteration as done:return done.value


def zwrt_case(profile_id, *, complete_owner_reads=False):
    from arch4.modem.zte_zwrt import ZteZwrtUbusModem,ZWRT_STATUS_CALLS
    from arch4.modem_live.profile_catalog import modem_profile_selection
    modem=ZteZwrtUbusModem(host='127.0.0.1',username='',password='',
        profile_id=profile_id,backend_name=modem_profile_selection(profile_id).backend)
    raw=goform_replies()
    thermal={'cpu_temp':'48','modem_temp':'40'}
    device={'model_name':'G5TC','serial_number':'SYNTHETIC_ZWRT_SERIAL','imei':'000000000000000',
        'software_version':'TEST_B17','hardware_version':'TEST_HW'}
    payloads=[raw,thermal,device,{'wan_ipaddr':'192.0.2.10'},
        {'realtime_rx_thrpt':'12000','realtime_tx_thrpt':'6000'}]
    metadata=[{'imsi':'220011234567890'}, {'up':True,'ipv4-address':[{'address':'192.0.2.10'}]},
        {'values':{}},{'modem_msn':'SYNTHETIC_ZWRT_SERIAL'},{'imei':'000000000000000'},{'values':{}}]
    wrap=lambda items:[{'success':True,'data':v} for v in items]
    replies={'status_batch':wrap(payloads),'metadata_batch':wrap(metadata),
        'cpu_thermal':thermal,'zwrt_data.get_wwaniface':{'connect_status':'ipv4_connected','enable':'0'}}
    if complete_owner_reads:
        # Labelled synthetic UCI native replies exercise the real Wi-Fi owner;
        # the frozen R24 oracle's original minimal input is left unchanged.
        replies['uci_wireless']={'values':{
            'radio0':{'.type':'wifi-device','hwmode':'11g','disabled':'0'},
            'radio1':{'.type':'wifi-device','hwmode':'11a','disabled':'1'}}}
    modem._call_ubus_current_session=lambda calls,**kwargs:deepcopy(
        replies['status_batch'if len(calls)==len(ZWRT_STATUS_CALLS)else'metadata_batch'])
    if complete_owner_reads:
        def radio_read(command,**kwargs):
            assert command['service']=='uci' and command['method']=='get'
            assert command['params']['config']=='wireless'
            values=replies['uci_wireless']['values'].get(command['params']['section'])
            return {'success':values is not None,'data':{'values':deepcopy(values or{})}}
        modem._ubus_call=radio_read
    flattened=modem._fetch_flat_status()
    assert flattened
    modem._connected=True
    modem._last_raw=deepcopy(flattened)
    snapshot=modem._build_modem_input_from_raw(flattened,traffic_observed_at=NOW)
    expected=canonical_values(modem,snapshot,modem._last_status)
    from arch4.modem.zte_ng_network_settings import _mobile_data_enabled
    expected['slow.mobile_data_enabled']=_mobile_data_enabled(replies['zwrt_data.get_wwaniface'])
    expected['slow.temperature_cpu_c']=flattened.get('temperature_cpu_c')
    expected['rf.cqi']=snapshot.cqi
    expected['radio.carrier_count']=len(modem._last_status['configured_band_pool'])
    if complete_owner_reads:
        wifi=modem._read_wifi_status()
        expected.update(dict(zip(('slow.wifi_enabled','slow.wifi_24ghz_enabled','slow.wifi_5ghz_enabled'),wifi)))
    return replies,base_context(),expected


def mikrotik_case(profile_id, *, reply_edits=None, backend_only=False):
    from arch4.modem.mikrotik import MikroTikModem
    from arch4.interfaces import Arch4Config
    from arch4.tests.routeros_test_support import install_routeros_test_api
    replies={'/interface/lte/monitor':{'status':'connected','sinr':'12dB','rsrp':'-86dBm','rsrq':'-9dB','rssi':'-58dBm',
        'primary-band':'B3@20Mhz earfcn:1300 phy-cellid:303','ca-band':'B20@10Mhz earfcn:6300 phy-cellid:304',
        'current-cellid':'123456','tracking-area-code':'112','cqi':'12','ri':'2',
        'dl-mcs':'17','ul-mcs':'19','session-uptime':'1h2m3s','current-operator':'Yettel',
        'model':'EG120K','revision':'TEST_MODEM_FW','imei':'000000000000000'},
        '/interface/lte/print':{'.id':'*1','name':'lte1','band':'1,3,20','disabled':'false'},
        '/system/resource':{'version':'7.21','board-name':'Chateau','architecture-name':'arm64','uptime':'1d2h'},
        '/system/routerboard':{'model':'Chateau','serial-number':'SYNTHETIC_ROUTEROS_SERIAL','revision':'r1'},
        '/interface/monitor-traffic':{'rx-bits-per-second':'96000','tx-bits-per-second':'48000'},
        '/interface/print_stats':{'name':'lte1','rx-byte':'12000000','tx-byte':'6000000'},
        '/ip/address':{'address':'192.0.2.10/24','interface':'lte1'},
        '/interface/wifi':[{'name':'wifi1','band':'2ghz-ax','disabled':'false'},
            {'name':'wifi2','band':'5ghz-ax','disabled':'true'}],
        'AT+QENG="servingcell"':'', 'AT+QCAINFO':'', 'AT+QNWCFG="mcs"':''}
    for group, values in (reply_edits or {}).items():
        replies[group].update(values)
    class Resource:
        def __init__(self,path):self.path=path
        def get(self,**kwargs):
            key='/interface/lte/print' if self.path=='/interface/lte' else self.path
            value=deepcopy(replies.get(key,[]));return value if isinstance(value,list) else [value]
        def call(self,command,params,*a,**k):
            if command=='monitor':return [deepcopy(replies['/interface/lte/monitor'])]
            if command=='monitor-traffic':return [deepcopy(replies['/interface/monitor-traffic'])]
            if command=='print':return [deepcopy(replies['/interface/print_stats'])]
            if command=='at-chat':return [{'output':replies.get(params.get('input'),'ERROR')}]
            return []
        def set(self,**kwargs):raise AssertionError('read conformance attempted a write')
    api=SimpleNamespace(get_resource=lambda p:Resource(p),get_binary_resource=lambda p:Resource(p))
    from arch4.modem_live.profile_catalog import modem_profile_selection
    modem=MikroTikModem(Arch4Config(modem_backend=modem_profile_selection(profile_id).backend,
        modem_profile_id=profile_id))
    install_routeros_test_api(modem,api);modem._lte_id='*1'
    modem._at_live_polling_enabled=False
    modem._at_bus_guard_s=modem._at_read_delay_s=0
    if backend_only:
        return modem,replies
    snapshot=modem._read_modem_input_impl(refresh_auxiliary=True)
    assert snapshot is not None
    modem._refresh_traffic_volume_cache(force=True)
    modem._last_status.update(modem._read_routeros_network_identity())
    values=canonical_values(modem,snapshot,modem._last_status)
    values.update({field_address(k):v for k,v in modem._traffic_volume_cache.items() if k.startswith('traffic_')})
    context=base_context()
    context['routeros_nr_current_interface'] = (
        (modem._last_lte_monitor or {}).get('_routeros_nr_current_interface') is True)
    context['routeros_nr_read_source'] = 'monitor'
    if modem._wifi_api_path:
        context['native_wifi_path']=modem._wifi_api_path
    return replies,context,values
