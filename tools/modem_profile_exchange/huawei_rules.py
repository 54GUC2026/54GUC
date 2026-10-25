"""HiLink field bindings. Request paths/selectors and ordered fallbacks are data."""
from tools.modem_profile_exchange.rule_authoring import (
    op,inp,get,choose,let,var,seq,obj,call,alg,either,both,text,stripped,first,mapped,filtered)


def xml(path,tag,default=''):
    return {'op':'xml_text','value':inp('replies',path),'path':tag,'default':default}


def fields():
    sig=lambda tag:xml('/api/device/signal',tag)
    info=lambda tag:xml('/api/device/information',tag)
    result={}
    for tag,address,metric in [('sinr','rf.sinr_db','sinr'),('rsrp','rf.rsrp_dbm','rsrp'),
            ('rsrq','rf.rsrq_db','rsrq'),('rssi','rf.rssi_dbm','rssi')]:
        raw=alg('parse_signal_val',sig(tag))
        result[address]=choose(both(op('contains',['rsrp','rsrq','rssi'],metric),op('le',raw,-900)),raw,
            op('multiply',raw,either(inp('context','signal_scales',metric),1.0)))
    for tag,address in [('band','radio.serving_band'),('cell_id','rf.cell_id')]:
        result[address]=either(op('integer',either(sig(tag),'0')),None)
    result['rf.pci']=either(op('integer',either(sig('pci'),sig('sc'),'0')),None)
    result['rf.enb_id']=choose(result['rf.cell_id'],op('floor_divide',result['rf.cell_id'],256))
    result['slow.tac']=let([['s',either(sig('tac'),sig('lac'))]],choose(op('match',var('s'),pattern=r'\d+'),either(op('integer',var('s')),None)))
    channels=alg('parse_serving_earfcns',sig('earfcn'),band=either(result['radio.serving_band'],0),
        dl_frequency_100khz=sig('ltedlfreq'),ul_frequency_100khz=sig('lteulfreq'))
    result['rf.earfcn_dl']=either(get(channels,0),None)
    result['rf.earfcn_ul']=either(get(channels,1),None)
    for tag,address in [('dlfrequency','slow.dl_freq_mhz'),('ulfrequency','slow.ul_freq_mhz')]:
        result[address]=alg('parse_freq_mhz',sig(tag))
    for tag,address in [('dlbandwidth','slow.dl_bandwidth_mhz'),('ulbandwidth','slow.ul_bandwidth_mhz')]:
        result[address]=op('enum',sig(tag),values={'1400KHz':1.4,'1.4MHz':1.4,'3MHz':3,'5MHz':5,'10MHz':10,'15MHz':15,'20MHz':20})
    mcs=alg('parse_dl_mcs',sig('dl_mcs'))
    result['rf.mcs_dl0']=get(mcs,0);result['rf.mcs_dl1']=get(mcs,1)
    result['rf.mcs_ul']=alg('parse_ul_mcs',sig('ul_mcs'))
    for tag,address in [('cqi0','rf.cqi0'),('cqi1','rf.cqi1')]:result[address]=alg('parse_b818_cqi',sig(tag))
    result['rf.cqi']=either(result['rf.cqi0'],0)
    direct=alg('parse_int',either(*[sig(t) for t in ['ri','rankindicator','rank_indicator','rank','lte_ri']]))
    code0=op('ne',op('extract',sig('dl_mcs'),pattern=r'Code0:\s*\d+'),None)
    code1=op('ne',op('extract',sig('dl_mcs'),pattern=r'Code1:\s*\d+'),None)
    result['rf.ri']=choose(op('gt',direct,0),direct,choose(both(code0,code1),2,
        choose(either(code0,code1),1,choose(inp('context','actionable_only'),either(inp('context','previous_ri'),0),0))))
    for address,key in [('rf.qam_dl0','rf.mcs_dl0'),('rf.qam_dl1','rf.mcs_dl1'),('rf.qam_ul','rf.mcs_ul')]:
        qm=get(alg('resolve_lte_qm_with_retx_hold',result[key],either(inp('context','previous_qm',address),0)),0)
        result[address]=alg('qm_to_qam',qm) if address!='rf.qam_ul' else qm
    result['radio.tx_power']=either(sig('txpower'),None)
    code=xml('/api/monitoring/status','ConnectionStatus')
    net=xml('/api/monitoring/status','CurrentNetworkTypeEx')
    result['health.radio_connected']=either(op('eq',code,'901'),
        op('contains',['1','connected','connect','rrcconnected','registered'],op('lower',either(sig('rrc'),sig('rrc_status')))),
        op('not',op('not',either(result['radio.serving_band'],result['rf.earfcn_dl'],result['rf.cell_id'],result['rf.pci'],result['rf.rsrp_dbm'],result['rf.sinr_db']))))
    result['radio.network_type']=op('enum',net,values={'0':'No Service','1':'GSM (2G)','2':'GPRS (2G)','3':'EDGE (2G)',
        '41':'UMTS (3G)','42':'HSDPA (3G)','43':'HSUPA (3G)','44':'HSPA (3G)','45':'HSPA+ (3.5G)','46':'DC-HSPA+ (3.5G)',
        '101':'LTE (4G)','1011':'LTE (4G)','111':'NR (5G)'},default='')
    result['radio.ca_active']=op('eq',net,'1011')
    rrc=op('lower',either(sig('rrc'),sig('rrc_status')))
    result['slow.rrc']=either(op('enum',rrc,values={
        **{key:'Connected'for key in ('1','connected','connect','rrcconnected','registered')},
        **{key:'Idle'for key in ('0','idle','noconn','noconnection','rrcidle')}},default=None),
        inp('context','native_rrc_fallback'))
    tm=either(*[sig(tag) for tag in ['transmode','trans_mode','transmissionmode','transmission_mode','lte_transmode']])
    digit=op('extract',tm,pattern=r'(?:TM\s*)?\[?\s*([1-9])\s*\]?',group=1,ignore_case=True,default='')
    label=op('enum',digit,values={'1':'SISO','2':'TX Diversity','3':'OL-MIMO','4':'CL-MIMO','7':'Beamforming'},default='')
    result['slow.trans_mode']=choose(digit,op('add',op('add','TM[',digit),op('add',']',choose(label,op('add',op('add',' (',label),')'),''))),tm)
    for tag,address in [('nrsinr','radio.nr_sinr_db'),('nrrsrp','radio.nr_rsrp_dbm'),('nrrsrq','radio.nr_rsrq_db')]:
        result[address]=either(alg('parse_signal_val',sig(tag)),None)
    result['radio.nr_arfcn']=either(alg('parse_int',sig('nrearfcn')),None)
    for tag,address in [('nrdlbandwidth','slow.nr_bandwidth_mhz'),
            ('nrulbandwidth','slow.nr_ul_bandwidth_mhz'),
            ('nrdlfreq','slow.nr_dl_freq_mhz'),('nrulfreq','slow.nr_ul_freq_mhz'),
            ('nrdlmcs','radio.nr_mcs_dl'),('nrulmcs','radio.nr_mcs_ul')]:
        result[address]=alg('huawei_optional_nr_read',inp('replies','/api/device/signal'),tag=tag)
    result['radio.nr5g_active']=op('not',op('not',either(*[sig(tag) for tag in
        ['nrsinr','nrrsrp','nrrsrq','nrearfcn','nrdlbandwidth','nrulbandwidth','nrdlmcs','nrulmcs','nrtxpower']])))
    # The existing Huawei decoder publishes no native NR PCI or band. Keep the
    # declaration mismatch visible instead of inferring either from ARFCN.
    result['radio.nr_band']=None;result['radio.nr_pci']=None
    result['radio.active_band_set']=choose(result['radio.serving_band'],seq(result['radio.serving_band']),[])
    result['radio.band_set']=result['radio.active_band_set']
    mask=xml('/api/net/net-mode','LTEBand')
    restriction=alg('hex_to_bands',mask)
    hardware=inp('context','supported_lte_bands')
    pool=op('intersection',inp('context','provider_bands'),hardware,
        choose(alg('is_all_bands_mask',mask),hardware,restriction))
    for address in ['radio.configured_band_pool','radio.selected_band_set']:result[address]=pool
    for address in ['radio.configured_band_pool_signature','radio.selected_band_set_signature','radio.ca_signature']:
        result[address]=alg('band_signature',pool)
    result['radio.carrier_count']=op('length',pool)
    result['identity.provider_plmn']=xml('/api/net/current-plmn','Numeric')
    result['identity.provider_name']=either(get(inp('context','provider_names'),result['identity.provider_plmn']),
        xml('/api/net/current-plmn','ShortName'),xml('/api/net/current-plmn','FullName'),'')
    for tag,address in [('DeviceName','identity.model'),('SerialNumber','identity.serial'),('HardwareVersion','identity.hardware_version'),
            ('SoftwareVersion','identity.firmware'),('SoftwareVersion','identity.software_version'),('WebUIVersion','identity.webui')]:result[address]=info(tag)
    result['identity.router_model']=result['identity.model']
    result['identity.model']=get(alg('normalize_modem_identity_fields',obj(
        backend=inp('profile','backend'),model=result['identity.model'],
        firmware=result['identity.firmware'],hardware_version=result['identity.hardware_version'])), 'model')
    result['identity.imei']=either(info('Imei'),info('IMEI'),info('SerialNumber'),'')
    result['identity.imsi']=either(info('Imsi'),info('IMSI'),'')
    result['identity.lan_mac']=either(info('LanMacAddress'),info('MacAddress1'),info('macAddress1'),'')
    result['slow.wan_ip']=info('WanIPAddress')
    result['slow.mobile_data_enabled']=op('enum',xml('/api/dialup/mobile-dataswitch','dataswitch'),values={'0':False,'1':True})
    for tag,address in [('CurrentDownloadRate','traffic.rx_bytes_per_sec'),('CurrentUploadRate','traffic.tx_bytes_per_sec')]:
        result[address]=attempt_number(xml('/api/monitoring/traffic-statistics',tag))
    timer=xml('/api/monitoring/traffic-statistics','CurrentConnectTime')
    result['traffic.session_uptime_s']=choose(op('match',timer,pattern=r'[0-9]+'),op('integer',timer))
    antenna_paths = [
        '/api/device/antenna_set_type','/api/device/antenna_status','/api/device/antenna_type',
        '/api/device/antenna_settings','/api/device/antenna','/api/device/antennastatus','/api/device/antenna-mode']
    antenna = either(*[alg('huawei_antenna',inp('replies',path)) for path in antenna_paths])
    for path in reversed(antenna_paths):
        antenna = choose(op('eq',inp('context','native_antenna_path'),path),
            alg('huawei_antenna',inp('replies',path)),antenna)
    result['slow.antenna']=antenna
    physical=alg('wifi_switches',inp('replies','/api/wlan/status-switch-settings'))
    wifi=alg('wifi_ssids',either(inp('replies','/api/wlan/multi-basic-settings'),''),
        first(inp('context','previous_wifi'),[None,None,None]))
    wifi=choose(both(op('contains',op('lower',info('DeviceName')),'b535-333'),op('ne',physical,None)),physical,wifi)
    for address,index in [('slow.wifi_enabled',0),('slow.wifi_24ghz_enabled',1),('slow.wifi_5ghz_enabled',2)]:result[address]=get(wifi,index)
    return result


def attempt_number(value):
    return {'op':'try','value':op('number',value),'default':0.0}
