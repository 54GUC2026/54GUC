"""Author the portable definition using only the documented JSON operations.

This file never imports a driver. The generated JSON carries its own functions;
evaluation needs expressions.py and supplied native replies, never this builder.
"""
from __future__ import annotations

def op(name, *args, **options): return {'op': name, 'args': list(args), **options}
def var(name): return {'op': 'variable', 'name': name}
def inp(*path): return {'op': 'input', 'path': list(path)}
def get(value, key, default=None): return {'op': 'get', 'value': value, 'key': key, 'default': default}
def call(name, **arguments): return {'op': 'call', 'rule': name, 'arguments': arguments}
def choose(condition, yes, no=None): return {'op': 'if', 'condition': condition, 'then': yes, 'else': no}
def let(bindings, result): return {'op': 'let', 'bindings': bindings, 'result': result}
def attempt(value, default=None): return {'op': 'try', 'value': value, 'default': default}
def seq(*items): return {'op': 'list', 'items': list(items)}
def obj(**members): return {'op': 'object', 'members': members}
def mapped(items, expression, name='item'): return {'op': 'map', 'items': items, 'as': name, 'expression': expression}
def filtered(items, expression, name='item'): return {'op': 'filter', 'items': items, 'as': name, 'expression': expression}
def first(*args): return {'op': 'first_present', 'args': list(args)}
def either(*args): return op('or', *args)
def both(*args): return op('and', *args)
def text(value): return op('text', either(value, ''))
def stripped(value): return op('strip', text(value))
def rule(parameters, expression): return {'parameters': parameters.split(), 'expression': expression}
def alg(name,*args,**kwargs):return {'op':'algorithm','name':name,'args':list(args),'kwargs':kwargs}


def common_rules():
    v = var('value')
    rules = {}
    rules['bounded_number'] = rule('value low high integer', attempt(let([
        ['n', choose(op('is_type', v, type='bool'), None, op('number', v))],
    ], choose(both(op('ne', var('n'), None), op('finite', var('n')),
        either(op('eq', var('low'), None), op('ge', var('n'), var('low'))),
        either(op('eq', var('high'), None), op('le', var('n'), var('high'))),
        either(op('not', var('integer')), op('integral', var('n')))),
        choose(var('integer'), op('integer', var('n')), var('n'))))))
    rules['legacy_integer'] = rule('value', let([['s', stripped(v)]], attempt(
        op('integer', var('s'), base=0), attempt(op('integer',
            op('extract', var('s'), pattern=r'(0x[0-9a-fA-F]+|-?\d+)', group=1, default=''), base=0), 0))))
    rules['metric_decimal'] = rule('value', attempt(op('number',
        op('extract', stripped(v), pattern=r'-?\d+(?:\.\d+)?', default='')), 0.0))
    rules['dbm_negative'] = rule('value', op('multiply', -1, op('abs', call('metric_decimal', value=v))))
    rules['first_nonzero'] = rule('values', let([
        ['valid', filtered(var('values'), op('gt', op('abs', var('item')), .0001))]], get(var('valid'), 0, 0.0)))
    rules['duration'] = rule('value', let([['s', stripped(v)]], choose(
        op('match', var('s'), pattern=r'\d+(?:\.\d+)?'), op('number', var('s')),
        attempt(let([['p', op('split', var('s'), separator=':')]], choose(op('eq', op('length', var('p')), 3),
            op('maximum', 0.0, op('add', op('add', op('multiply', op('integer', get(var('p'), 0)), 3600),
                op('multiply', op('integer', get(var('p'), 1)), 60)), op('number', get(var('p'), 2)))), 0.0)), 0.0))))
    rules['band_signature'] = rule('bands', op('join', mapped(op('sorted_unique', var('bands')),
        op('add', 'B', op('text', var('item')))), separator='+'))
    rules['radio_number'] = rule('value low high integer', call('bounded_number', value=v,
        low=var('low'), high=var('high'), integer=var('integer')))
    rules['labelled_band'] = rule('value prefix high', let([
        ['s', stripped(v)],
        ['n', choose(choose(op('eq',var('prefix'),'B'),
            op('match',var('s'),pattern=r'B[1-9]\d{0,2}'),op('match',var('s'),pattern=r'[Nn][1-9]\d{0,3}')),
            op('extract', var('s'), pattern=r'\d+'), v)],
    ], call('bounded_number', value=var('n'), low=1, high=var('high'), integer=True)))
    rules['unsigned64']=rule('value',let([['n',choose(op('is_type',v,type='str'),
        choose(op('match',op('strip',v),pattern=r'[0-9]{1,20}'),op('integer',op('strip',v),base=10)),v)]],
        choose(both(op('is_type',var('n'),type='int'),op('ge',var('n'),0),op('lt',var('n'),2**64)),var('n'))))
    rules['subscription_key']=rule('value',choose(both(op('is_type',v,type='str'),op('match',v,pattern=r'[0-9]{14,16}')),
        op('sha256_utf8',op('add','54GUC.subscription.v1:',v)),''))
    rules['qam_label']=rule('value',let([['s',op('upper',op('replace',stripped(v),old=' ',new=''))]],
        op('enum',var('s'),values={'2':'QPSK','4':'16QAM','6':'64QAM','8':'256QAM',
            'QPSK':'QPSK','16QAM':'16QAM','64QAM':'64QAM','256QAM':'256QAM',
            'QAM16':'16QAM','QAM64':'64QAM','QAM256':'256QAM'},default='')))
    return rules


def zte_rules():
    raw=var('raw'); value=var('value')
    r=lambda name:get(raw,name)
    rules={}
    rules['zte_band_list']=rule('value',op('unique',filtered(mapped(
        op('find_all',op('upper',stripped(value)),pattern=r'\d+'),op('integer',var('item'))),
        op('gt',var('item'),0))))
    rules['zte_active_bands']=rule('raw',let([
        ['p',either(*[call('zte_band_list',value=r(n)) for n in
            ('lte_ca_pcell_band','wan_active_band','ZCELLINFO_band','lte_band')],[])],
    ],choose(op('ne',op('length',var('p')),0),op('unique',op('concat',var('p'),
        call('zte_band_list',value=r('lte_ca_scell_band')))),[])))
    upper=op('upper',stripped(value))
    limited=op('match',stripped(value),pattern=r'limited[\s_-]+service(?:[\s_-]+[a-z0-9]+)*',ignore_case=True)
    nsa=either(op('contains',['ENDC','EN-DC','LTE-NSA'],upper),
        both(op('contains',upper,'NSA'),either(op('contains',upper,'5G'),op('contains',upper,'NR'))))
    sa=either(op('eq',upper,'SA'),both(op('contains',upper,'5G'),op('contains',upper,'SA')))
    rules['zte_rat']=rule('value',choose(limited,'',choose(nsa,'NR NSA (5G)',choose(sa,'NR SA (5G)',
        choose(either(op('contains',upper,'LTE'),op('eq',upper,'4G')),'LTE (4G)','')))))
    rules['zte_optional_int']=rule('value maximum',choose(either(op('is_type',value,type='bool'),
        op('eq',value,None),op('eq',value,'')),None,attempt(let([
            ['s',op('strip',op('text',value))],
            ['n',choose(op('match',var('s'),pattern=r'0[xX].*'),op('integer',var('s'),base=16),op('integer',var('s'),base=10))]],
        choose(both(op('ge',var('n'),0),either(op('eq',var('maximum'),None),op('le',var('n'),var('maximum')))),var('n'))))))
    cleaned=op('extract',stripped(value),pattern=r'^(.*?)\s*(?:MHz|dBm|dB)?$',group=1,ignore_case=True,default='')
    rules['zte_optional_metric']=rule('value low high',choose(either(op('is_type',value,type='bool'),
        op('eq',value,None),op('eq',value,'')),None,
        call('bounded_number',value=cleaned,low=var('low'),high=var('high'),integer=False)))
    rules['zte_nr_band']=rule('value',attempt(op('integer',op('extract',
        op('replace',op('replace',op('replace',op('lower',stripped(value)),old='band',new=''),old='nr',new=''),old='lte',new=''),
        pattern=r'n?(\d+)',group=1,default='')),0))
    rules['zte_wifi']=rule('value',choose(either(op('eq',value,None),op('eq',stripped(value),'')),None,
        op('contains',['1','true','on','enabled'],op('lower',stripped(value)))))
    # Distinct qualified masks; both tables are carried in each profile JSON.
    for name,masktable in [('goform_lte_mask',{1:1,2:2,3:4,5:16,7:64,8:128,20:0x80000,
            28:0x8000000,32:0x80000000,38:0x800000000,40:0x1000000000,41:0x2000000000}),
            ('zwrt_lte_mask',{b:1<<(b-1) for b in [1,2,3,5,7,8,20,28,32,38,40,41]})]:
        rules[name]=rule('value',attempt(let([['mask',op('integer',stripped(value),base=0)]],
            mapped(filtered([list(p) for p in sorted(masktable.items())],
                op('ne',op('bit_and',var('mask'),get(var('item'),1)),0)),get(var('item'),0))),[]))
    return rules


def zte_fields(*, zwrt=False):
    raw=inp('replies','nwinfo' if zwrt else 'goform')
    r=lambda name:get(raw,name)
    metric=lambda *names:call('first_nonzero',values=seq(*[call('metric_decimal',value=r(n)) for n in names]))
    dbm=lambda *names:call('first_nonzero',values=seq(*[call('dbm_negative',value=r(n)) for n in names]))
    fields={}
    active=call('zte_active_bands',raw=raw)
    pool=either(inp('profile','provider_band_pool'),call('zwrt_lte_mask' if zwrt else 'goform_lte_mask',value=r('lte_band_lock')))
    signature=call('band_signature',bands=pool)
    fields['rf.sinr_db']=metric('lte_snr','sinr','ecio')
    fields['rf.rsrp_dbm']=dbm('lte_rsrp','lte_rsrp_1')
    fields['rf.rssi_dbm']=dbm('lte_rssi','rssi')
    reported=call('dbm_negative',value=r('lte_rsrq'))
    bw=metric('lte_ca_pcell_bandwidth','bandwidth')
    estimate=op('round',op('maximum',-19.5,op('minimum',-3.0,op('subtract',op('add',
        op('multiply',10,op('log10',op('enum',choose(op('gt',bw,0),op('integer',op('round',bw)),20),
            values={'1':6,'3':15,'5':25,'10':50,'15':75,'20':100},default=100))),
            fields['rf.rsrp_dbm']),fields['rf.rssi_dbm']))),digits=1)
    fields['rf.rsrq_db']=choose(op('ge',op('abs',reported),.001),reported,
        choose(both(op('ge',op('abs',fields['rf.rsrp_dbm']),.001),op('ge',op('abs',fields['rf.rssi_dbm']),.001)),estimate,0.0))
    fields['radio.network_type']=alg('normalize_network_type',either(r('network_type'),r('current_network_mode')))
    for native,address in [('lte_pci','rf.pci'),('cell_id','rf.cell_id'),('tac','slow.tac'),('lte_earfcn','rf.earfcn_dl')]:
        aliases={'lte_pci':['Z_PCI'],'tac':['lac_code'],'lte_earfcn':['earfcn','Z_dl_earfcn','lte_ca_pcell_arfcn','lte_ca_pcell_freq','wan_active_channel']}
        fields[address]=either(call('legacy_integer',value=either(r(native),*[r(k) for k in aliases.get(native,[])])),None)
    fields['rf.enb_id']=choose(op('gt',either(fields['rf.cell_id'],0),255),op('floor_divide',fields['rf.cell_id'],256))
    fields['radio.serving_band']=either(get(active,0,0),None)
    for address in ['radio.active_band_set','radio.band_set']:fields[address]=op('sorted_unique',active)
    for address in ['radio.active_band_set_signature','radio.band_set_signature']:
        fields[address]=call('band_signature',bands=op('sorted_unique',active))
    for address in ['radio.selected_band_set','radio.configured_band_pool']:fields[address]=either(pool,None)
    for address in ['radio.selected_band_set_signature','radio.configured_band_pool_signature','radio.ca_signature','radio.ca_band_signature','radio.configured_ca_pool_display']:
        fields[address]=either(signature,None)
    fields['radio.carrier_count']=op('length',pool)
    fields['radio.configured_band_pool_source']=choose(inp('profile','provider_band_pool'),'profile_provider_band_pool',choose(pool,'zte_goform_lte_band_lock'))
    fields['radio.configured_band_pool_status']=choose(pool,'available','unavailable')
    fields['radio.configured_band_pool_age_s']=choose(pool,0.0)
    fields['radio.configured_band_pool_last_sample_ts']=choose(pool,inp('context','sampled_at'))
    fields['radio.ca_active']=either(op('contains',['1','true','on','yes','enable','enabled','active','activated','ca','ca_activated'],op('lower',stripped(r('wan_lte_ca')))),
        op('gt',op('length',active),1),op('contains',op('upper',text(either(r('network_type'),r('current_network_mode')))),'CA'))
    for field,natives in {'model':['model_name','product_name'],'router_model':['model_name','product_name'],
            'hardware_version':['hardware_version'],'software':['web_version','wa_inner_version'],
            'software_version':['web_version','wa_inner_version'],'firmware':['wa_inner_version','cr_version'],
            'webui':['web_version'],'serial':['serial_number'],'imei':['imei'],'lan_mac':['mac_address']}.items():
        fields['identity.'+field]=either(stripped(either(*[r(n) for n in natives])),None)
    fields['profile.config_version']=either(stripped(r('cr_version')),None)
    fields['identity.imsi']=let([['s',stripped(either(r('sim_imsi'),r('imsi')))]],
        choose(op('match',var('s'),pattern=r'\d{14,16}'),var('s'),''))
    fields['slow.wan_ip']=either(stripped(r('wan_ipaddr')),None)
    fields['radio.tx_power']=call('metric_decimal',value=r('tx_power'))
    for field,natives in [('temperature_ambient_c',['pm_sensor_ambient']),('temperature_modem_4g_c',['pm_sensor_mdm']),
            ('temperature_modem_5g_c',['pm_sensor_5g','pm_modem_5g']),('temperature_pa1_c',['pm_sensor_pa1']),('temperature_wifi_c',['wifi_chip_temp'])]:
        fields[('slow.' if field=='temperature_modem_5g_c' else 'profile.')+field]=call('metric_decimal',value=either(*[r(n) for n in natives]))
    wifi=either(r('wifiEnabled'),r('wifi_onoff_state'))
    for address,native in [('slow.wifi_enabled',None),('slow.wifi_24ghz_enabled','wifi_chip1_ssid1_switch_onoff'),('slow.wifi_5ghz_enabled','wifi_chip2_ssid1_switch_onoff')]:
        fields[address]=call('zte_wifi',value=first(choose(op('ne',stripped(r(native)),''),r(native)) if native else None,wifi))
    fields['rf.ri']=0
    for field in ['rf.mcs_dl0','rf.mcs_dl1','rf.mcs_ul']:fields[field]=0
    sinr=fields['rf.sinr_db']
    fields['rf.cqi']=choose(op('lt',op('abs',sinr),.001),0,choose(op('lt',sinr,1),2,
        choose(op('lt',sinr,8.5),6,choose(op('lt',sinr,16),10,14))))
    for address,native in [('traffic.traffic_month_download_bytes','monthly_rx_bytes'),('traffic.traffic_month_upload_bytes','monthly_tx_bytes')]:
        fields[address]=call('legacy_integer',value=r(native))
    # PLMN and operator-name fallback use the host's neutral public operator
    # catalogue, an explicit input rather than another vendor parser.
    digits=lambda value:op('join',op('find_all',stripped(value),pattern=r'\d'),separator='')
    direct=digits(r('mccmnc'));mcc=digits(r('rmcc'));mnc=digits(r('rmnc'))
    fields['identity.provider_plmn']=choose(op('ge',op('length',direct),6),op('slice',direct,start=0,stop=6),
        choose(op('eq',op('length',direct),5),direct,choose(both(mcc,mnc),op('add',mcc,
            choose(op('eq',op('length',mnc),1),op('add','0',mnc),mnc)),'')))
    plmn=fields['identity.provider_plmn']
    fullname=stripped(r('network_provider_fullname'));name=stripped(r('network_provider'))
    known=get(inp('context','provider_names'),plmn,'')
    good=lambda n:both(n,op('not',op('contains',['unknown','n/a'],op('lower',n))))
    fields['identity.provider_name']=either(choose(both(good(fullname),op('ne',fullname,plmn)),fullname),
        choose(good(name),name),known)
    # This result is radio/native attachment evidence only, never Internet truth.
    ppp=op('lower',stripped(either(r('ppp_status'),r('modem_main_state'))))
    fields['health.radio_connected']=alg('radio_connected',raw)
    fields['health.radio_state_text']=choose(fields['health.radio_connected'],'Connected','Disconnected')
    fields['traffic.session_uptime_s']=choose(op('ne',stripped(r('ppp_uptime')),''),call('duration',value=r('ppp_uptime')),
        choose(fields['health.radio_connected'],op('maximum',0,op('subtract',inp('context','sampled_at'),
            first(inp('context','session_started_at'),inp('context','sampled_at')))),0.0))
    for direction,address in [('rx','traffic.rx_bytes_per_sec'),('tx','traffic.tx_bytes_per_sec')]:
        numeric=lambda value:call('bounded_number',value=value,low=0,high=None,integer=False)
        rate=first(*[numeric(r(k)) for k in [f'realtime_{direction}_thrpt',f'{direction}_thrpt',f'{direction}_rate']])
        total_names=[f'wan_{direction}_bytes',f'{direction}_bytes',f'realtime_{direction}_bytes',f'traffic_{direction}',f'total_{direction}_bytes']
        total_key=first(*[choose(op('ne',numeric(r(k)),None),k) for k in total_names])
        previous=inp('context','traffic_previous',direction)
        counter_rate=let([['key',total_key],['total',numeric(get(raw,total_key))]],choose(both(
            op('ne',var('key'),None),op('ne',previous,None),
            op('gt',inp('context','monotonic_at'),get(previous,1)),op('eq',get(previous,2),var('key')),
            op('ge',var('total'),get(previous,0))),op('divide',op('subtract',op('integer',var('total')),get(previous,0)),
                op('subtract',inp('context','monotonic_at'),get(previous,1))),None))
        fields[address]=first(rate,counter_rate)
    raw_type=op('upper',stripped(either(r('network_type'),r('current_network_mode'))))
    nsa=op('contains',['ENDC','EN-DC','LTE-NSA'],raw_type)
    sa=either(op('eq',raw_type,'SA'),both(op('contains',raw_type,'5G'),op('contains',raw_type,'SA')))
    fields['radio.nr_mode']=choose(nsa,'NSA',choose(sa,'SA',''))
    fields['radio.nr5g_active']=either(sa,both(nsa,op('ne',raw_type,'LTE-NSA')))
    nr_first=lambda *names:first(*[choose(both(op('ne',r(k),None),op('ne',r(k),'')),r(k)) for k in names])
    for address,names,maximum in [('radio.nr_cell_id',['nr5g_cell_id','Z5g_CELL_ID'],None),
            ('radio.nr_pci',['nr5g_pci','network_Z5g_PCI','Z5g_PCI'],1007),
            ('radio.nr_arfcn',['nr_ca_pcell_freq','nr5g_action_channel','Z5g_dlEarfcn'],3279165)]:
        fields[address]=call('zte_optional_int',value=nr_first(*names),maximum=maximum)
    band=nr_first('nr_ca_pcell_band')
    band=first(band,choose(nsa,nr_first('nr5g_action_nsa_band'),nr_first('nr5g_action_band')),
        nr_first('nr5g_action_band','network_Z5g_CELLINFO_band','Z5g_CELLINFO_band'))
    fields['radio.nr_band']=let([['band',call('zte_nr_band',value=band)]],choose(op('gt',var('band'),0),var('band')))
    for address,names,low,high in [('radio.nr_rsrp_dbm',['Z5g_rsrp','5g_rx0_rsrp','5g_rx1_rsrp'],-160,-20),
            ('radio.nr_rsrq_db',['Z5g_rsrq'],-50,10),('radio.nr_sinr_db',['Z5g_SINR','Z5g_snr'],-50,100),
            ('profile.nr5g_rx0_rsrp_dbm',['5g_rx0_rsrp','Z5g_rsrp'],-160,-20),
            ('profile.nr5g_rx1_rsrp_dbm',['5g_rx1_rsrp'],-160,-20),
            ('slow.nr_bandwidth_mhz',['nr5g_nsa_bandwidth'],.01,400)]:
        fields[address]=call('zte_optional_metric',value=nr_first(*names),low=low,high=high)
    if zwrt:
        fields['slow.temperature_cpu_c']=get(alg('_cpu_temperature',inp('replies','cpu_thermal')),0)
        fields['slow.mobile_data_enabled']=alg('_mobile_data_enabled',inp('replies','zwrt_data.get_wwaniface'))
    nr_data=alg('nr_status',raw,fields['radio.network_type'])
    for address,key in [('radio.nr_band','band'),('radio.nr_pci','pci'),('radio.nr_arfcn','arfcn'),
            ('radio.nr_cell_id','cell_id'),('radio.nr_mode','mode'),('radio.nr5g_active','active'),
            ('radio.nr_rsrp_dbm','rsrp_dbm'),('radio.nr_rsrq_db','rsrq_db'),('radio.nr_sinr_db','sinr_db'),
            ('profile.nr5g_rx0_rsrp_dbm','rx0_rsrp_dbm'),('profile.nr5g_rx1_rsrp_dbm','rx1_rsrp_dbm'),
            ('slow.nr_bandwidth_mhz','bandwidth_mhz'),('profile.nr5g_band_set','band_set')]:
        fields[address]=get(nr_data,key)
    return fields


def xiaomi_fields():
    net=inp('replies','xqdtcustom/cpe_detect','net')
    raw=inp('replies','xqdtcustom/cpe_detect','net','info')
    row={'op':'require','condition':both(op('is_type',net,type='dict'),
        op('is_type',get(net,'code'),type='int'),op('eq',get(net,'code'),0),op('is_type',raw,type='dict'),
        either(op('eq',get(raw,'linktype'),None),op('is_type',get(raw,'linktype'),type='str'))),
        'value':raw,'reason':'xiaomi_radio_reply_shape_unknown'}
    mode = get(row, 'linktype')
    lte = op('contains', ['4G', 'LTE', '5G NSA'], mode)
    nr = op('contains', ['5G NSA', '5G SA'], mode)
    fields = {}
    for native, field, lo, hi, integer in (
        ('rsrp','rf.rsrp_dbm',-150,-30,False), ('rsrq','rf.rsrq_db',-50,0,False),
        ('snr','rf.sinr_db',-30,50,False), ('rssi','rf.rssi_dbm',-150,0,False),
        ('ci','rf.cell_id',1,268435455,True), ('pci','rf.pci',0,503,True),
        ('earfcn','rf.earfcn_dl',0,262143,True)):
        fields[field] = choose(lte, call('bounded_number', value=get(row,native),low=lo,high=hi,integer=integer))
    fields['radio.serving_band'] = choose(lte, call('labelled_band',value=get(row,'cell_band'),prefix='B',high=256))
    for native, field, lo, hi in [('rsrp_5g','radio.nr_rsrp_dbm',-150,-30),
            ('rsrq_5g','radio.nr_rsrq_db',-50,0),('snr_5g','radio.nr_sinr_db',-30,50)]:
        fields[field] = choose(nr,call('bounded_number',value=get(row,native),low=lo,high=hi,integer=False))
    fields['radio.nr_band'] = choose(nr,call('labelled_band',value=get(row,'cell_band_5g'),prefix='N',high=1024))
    fields['radio.network_type'] = op('enum',mode,values={'4G':'LTE (4G)','LTE':'LTE (4G)',
        '5G NSA':'NR NSA (5G)','5G SA':'NR SA (5G)','No Service':'No Service'})
    fields['radio.nr_mode'] = choose(op('contains',['4G','LTE','5G NSA','5G SA','No Service'],mode),
        choose(op('eq',mode,'5G NSA'),'NSA',choose(op('eq',mode,'5G SA'),'SA','')))
    fields['identity.provider_name'] = choose(both(op('is_type',get(row,'operator'),type='str'),
        op('ne',stripped(get(row,'operator')),'')),stripped(get(row,'operator')))
    plmn=alg('unique_text',row,['plmn','mccmnc'])
    fields['identity.provider_plmn']=choose(op('match',plmn,pattern=r'\d{5,6}'),plmn,'')
    bandlist=inp('replies','xqmobile/get_cellular_band_list','list')
    supported=alg('band_list',get(bandlist,'list_4G'))
    flags=get(bandlist,'band_4G')
    # Stock flag strings and lists are indexed against the reported band list.
    selected=alg('selected_bands',supported,flags)
    fields['radio.selected_band_set']=selected
    fields['radio.selected_band_set_signature']=call('band_signature',bands=selected)
    pool=choose(fields['identity.provider_plmn'],op('intersection',inp('context','provider_bands'),supported,selected),[])
    fields['radio.configured_band_pool']=pool
    fields['radio.configured_band_pool_signature']=call('band_signature',bands=pool)
    fields['slow.network_settings']=alg('network_observation',inp('replies'),inp('profile'))
    return fields


def dev2_fields():
    """Explicit descriptors are the actual DEV2 native units, not UI labels."""
    rows=inp('replies','DEV2_LTE_SERVING_CELL_INFO')
    selection=alg('select_native_serving_rows',rows)
    lte=get(selection,0)
    nr=get(selection,1)
    primary=get(selection,2)
    nr_primary=filtered(nr,op('eq',text(get(var('item'),'cellConnectionStatus')),'1'))
    lterow=get(primary,0,{})
    nrrow=first(get(nr_primary,0),get(nr,0),{})
    fields={}
    for native,address,scale,low,high,integer in [
        ('band','radio.serving_band',1,1,256,True),('PCI','rf.pci',1,0,503,True),
        ('ARFCN','rf.earfcn_dl',1,0,262143,True),('cid','rf.cell_id',1,1,268435455,True),
        ('SNR','rf.sinr_db',.1,-300,500,False),('RSRP','rf.rsrp_dbm',1,-150,-30,False),
        ('RSRQ','rf.rsrq_db',1,-50,0,False),('RSSI','rf.rssi_dbm',1,-150,-10,False),
        ('downBandWidth','slow.dl_bandwidth_mhz',.001,1,100000,False),
        ('upBandWidth','slow.ul_bandwidth_mhz',.001,1,100000,False),
        ('downFreq','slow.dl_freq_mhz',1,1,8000,False),('upFreq','slow.ul_freq_mhz',1,1,8000,False),
        ('TAC','slow.tac',1,0,65535,True),('CQI','rf.cqi0',1,0,15,True),('RI','rf.ri',1,1,8,True),
        ('downMCS','rf.mcs_dl0',1,0,31,True),('upMCS','rf.mcs_ul',1,0,31,True),
        ('numRbs','radio.lte_num_rbs',1,0,110,True),
        ('txPowerPUCCH','radio.tx_power',.1,-500,500,False)]:
        number=alg('native_measurement',native,get(lterow,native),1,low,high)
        fields[address]=let([['n',number]],choose(op('ne',var('n'),None),
            obj(ppucch=op('multiply',var('n'),scale)) if address=='radio.tx_power'
            else op('multiply',var('n'),scale)))
    for native,address,scale,low,high,integer in [
        ('band','radio.nr_band',1,1,1024,True),('PCI','radio.nr_pci',1,0,1007,True),
        ('ARFCN','radio.nr_arfcn',1,0,3279165,True),('cid','radio.nr_cell_id',1,0,68719476735,True),
        ('SSRSRP','radio.nr_rsrp_dbm',1,-150,-30,False),('SSRSRQ','radio.nr_rsrq_db',1,-50,0,False),
        ('SSSINR','radio.nr_sinr_db',.1,-300,500,False),
        ('downBandWidth','slow.nr_bandwidth_mhz',.001,1,400000,False),
        ('upBandWidth','slow.nr_ul_bandwidth_mhz',.001,1,400000,False),
        ('downFreq','slow.nr_dl_freq_mhz',1,1,100000,False),('upFreq','slow.nr_ul_freq_mhz',1,1,100000,False),
        ('RI','radio.nr_ri',1,1,8,True),('CQI','radio.nr_cqi',1,0,15,True),
        ('downMCS','radio.nr_mcs_dl',1,0,31,True),('upMCS','radio.nr_mcs_ul',1,0,31,True),
        ('numRbs','radio.nr_num_rbs',1,0,275,True),('txPowerPUCCH','radio.nr_tx_power_dbm',.1,-500,500,False)]:
        number=call('bounded_number',value=get(nrrow,native),low=low,high=high,integer=integer)
        if native=='cid': number=choose(either(op('eq',op('length',lte),0),inp('profile','nr_cell_id_in_nsa')),number)
        fields[address]=let([['n',number]],choose(op('ne',var('n'),None),op('multiply',var('n'),scale)))
    provider=inp('replies','DEV2_LTE_PROF_STAT')
    fields['identity.provider_name']=op('text',get(provider,'ispName',''))
    fields['identity.provider_plmn']=let([['mcc',op('text',get(provider,'ispMcc',''))],
        ['mnc',op('text',get(provider,'ispMnc',''))]],choose(both(
            op('match',var('mcc'),pattern=r'\d{3}'),op('match',var('mnc'),pattern=r'\d{1,3}')),
            op('add',var('mcc'),choose(op('eq',op('length',var('mnc')),1),op('add','0',var('mnc')),var('mnc'))),''))
    fields['rf.enb_id']=choose(op('ne',fields['rf.cell_id'],None),op('floor_divide',fields['rf.cell_id'],256))
    active_bands=op('sorted_unique',filtered(mapped(lte,
        call('bounded_number',value=get(var('item'),'band'),low=1,high=256,integer=True)),op('ne',var('item'),None)))
    fields['radio.active_band_set']=active_bands
    fields['radio.active_band_set_signature']=call('band_signature',bands=active_bands)
    classified={'op':'every','items':rows,'expression':both(
        op('contains',['0','1','2'],text(get(var('item'),'cellConnectionStatus'))),
        either(op('eq',text(get(var('item'),'cellConnectionStatus')),'0'),
            op('contains',['3','7','8'],text(get(var('item'),'networkType')))))}
    fields['radio.network_type']=choose(classified,choose(op('gt',op('length',lte),0),
        choose(op('gt',op('length',nr),0),'NR NSA (5G)','LTE (4G)'),
        choose(op('gt',op('length',nr),0),'NR SA (5G)','No Service')))
    fields['health.radio_state_text']=op('enum',text(inp('replies','DEV2_LTE_LINK_CFG','connectStatus')),
        values={'4':'Connected','0':'Disconnected','1':'Disconnected','2':'Disconnected','3':'Disconnected'})
    fields['slow.wan_ip']=inp('replies','DEV2_LTE_LINK_CFG','ipv4')
    for native,address in [('downlinkModType','rf.qam_dl0'),('uplinkModType','rf.qam_ul')]:
        fields[address]=either(call('qam_label',value=get(lterow,native)),None)
    for native,address in [('downlinkModType','radio.nr_qam_dl'),('uplinkModType','radio.nr_qam_ul')]:
        fields[address]=either(call('qam_label',value=get(nrrow,native)),None)
    bandraw=inp('replies','DEV2_LTE_BAND_SEARCH')
    bandrow={'op':'require','condition':op('contains',['1','2'],text(get(bandraw,'selectedMode'))),
        'value':bandraw,'reason':'tplink_band_mode_unknown'}
    def band_values(value):
        return op('sorted_unique',filtered(mapped(op('split',text(value),separator=';'),
            choose(op('match',var('item'),pattern=r'B\d+'),
                op('integer',op('extract',var('item'),pattern=r'\d+')))),
            both(op('ne',var('item'),None),op('gt',var('item'),0),op('lt',var('item'),1025))))
    selected=choose(op('eq',text(get(bandrow,'selectedMode')),'1'),band_values(get(bandrow,'supportBandList')),
        band_values(get(bandrow,'selectedBand')))
    pool=op('intersection',inp('context','provider_bands'),band_values(get(bandrow,'supportBandList')),selected)
    for address in ['radio.configured_band_pool','radio.selected_band_set']:fields[address]=pool
    for address in ['radio.ca_signature','radio.selected_band_set_signature','radio.configured_band_pool_signature']:
        fields[address]=call('band_signature',bands=pool)
    traffic=inp('replies','DEV2_XTP_LTE_INTF_CFG')
    link=inp('replies','DEV2_LTE_LINK_CFG')
    wan=filtered(inp('replies','DEV2_ADT_WAN'),both(op('eq',get(var('item'),'accessMode'),'MBB'),
        op('eq',get(var('item'),'ifName'),get(link,'ifName')),op('eq',text(get(var('item'),'enable')),'1')))
    previous=inp('context','wan_previous')
    wan_pair=alg('wan_rates',link,inp('replies','DEV2_ADT_WAN'),inp('context','monotonic_at'),previous)
    for native,address,index in [('X_TP_BytesReceived','traffic.rx_bytes_per_sec',0),('X_TP_BytesSent','traffic.tx_bytes_per_sec',1)]:
        delta=get(wan_pair,index)
        fields[address]=choose(op('eq',inp('profile','traffic_rate_mode'),'reported'),
            call('bounded_number',value=get(traffic,'curRxSpeed' if index==0 else 'curTxSpeed'),low=0,high=None,integer=False),delta)
    for native,address in [('bytesReceived','traffic.traffic_device_download_counter_bytes'),('bytesSent','traffic.traffic_device_upload_counter_bytes')]:
        pair_ok=both(*[op('ne',call('unsigned64',value=get(traffic,key)),None) for key in ['bytesReceived','bytesSent']])
        fields[address]=choose(pair_ok,call('unsigned64',value=get(traffic,native)))
    fields['traffic.traffic_total_download_bytes']=fields['traffic.traffic_device_download_counter_bytes']
    fields['traffic.traffic_total_upload_bytes']=fields['traffic.traffic_device_upload_counter_bytes']
    for native,address in [('monthBytesReceived','traffic.traffic_month_download_bytes'),('monthBytesSent','traffic.traffic_month_upload_bytes'),
            ('dailyFlow','traffic.traffic_day_used_bytes'),('paymentDay','traffic.traffic_quota_start_day'),('totalConnTime','traffic.traffic_session_duration_s')]:
        raw=get(traffic,native)
        n=call('unsigned64',value=choose(both(op('is_type',raw,type='str'),op('match',raw,pattern=r'[0-9]{1,20}\.0{1,4}')),
            get(op('split',raw,separator='.'),0),raw))
        condition=op('ne',n,None)
        if native in ['monthBytesReceived','monthBytesSent','paymentDay']:
            condition=both(condition,op('eq',text(get(traffic,'enablePaymentDay')),'1'))
        if native=='paymentDay':condition=both(condition,op('ge',n,1),op('le',n,31))
        fields[address]=choose(condition,n)
    fields['traffic.session_uptime_s']=choose(inp('profile','native_period_fields'),fields['traffic.traffic_session_duration_s'],
        call('bounded_number',value=get(traffic,'curConnTime'),low=0,high=None,integer=False))
    fields['traffic.session_uptime_source']=choose(op('ne',fields['traffic.session_uptime_s'],None),'reported')
    fields['traffic.traffic_accounting_source']=choose(pair_ok,'tplink_dev2_bytes_received_sent')
    fields['traffic.traffic_accounting_status']='native_router_calendar_reference'
    uptime=call('bounded_number',value=inp('replies','DEV2_DEV_INFO','upTime'),low=0,high=None,integer=True)
    fields['traffic.traffic_counter_period_started_at']=choose(both(pair_ok,op('ne',uptime,None)),
        either(op('maximum',0,op('subtract',op('subtract',inp('context','sampled_at'),uptime),1)),None))
    fields['slow.router_system_uptime_s']=uptime
    fields['slow.router_cpu_load_pct']=let([['n',call('bounded_number',value=inp('replies','DEV2_PROC_STATUS','CPUUsage'),low=0,high=100,integer=False)]],
        choose(op('ne',var('n'),None),op('integer',var('n'))))
    total=call('bounded_number',value=inp('replies','DEV2_MEM_STATUS','total'),low=0,high=None,integer=False)
    free=call('bounded_number',value=inp('replies','DEV2_MEM_STATUS','free'),low=0,high=None,integer=False)
    fields['slow.router_memory_used_pct']=choose(both(op('ne',total,None),op('gt',total,0),op('ne',free,None),op('le',free,total)),
        op('integer',op('multiply',op('divide',op('subtract',total,free),total),100)))
    for band,address in [('2.4ghz','slow.wifi_24ghz_enabled'),('5ghz','slow.wifi_5ghz_enabled')]:
        matches=filtered(inp('replies','DEV2_ADT_WIFI_COMMON'),op('eq',op('lower',text(get(var('item'),'band'))),band))
        entry=get(matches,0,{})
        fields[address]=choose(both(op('eq',op('length',matches),1),
            op('match',text(get(entry,'stack')),pattern=r'[1-9]\d*,(?:\d+,){4}\d+'),
            op('contains',['0','1'],text(get(entry,'primaryEnable')))),op('eq',text(get(entry,'primaryEnable')),'1'))
    fields['slow.antenna']=op('enum',text(inp('replies','DEV2_LTE_WAN_CFG','antennaType')),values={'0':'Internal','1':'External'})
    mobile=inp('replies','DEV2_LTE_WAN_CFG','dataSwitchStatusBackup')
    fields['slow.mobile_data_enabled']=choose(op('is_type',mobile,type='str'),op('enum',mobile,values={'0':False,'1':True}))
    macs=op('sorted_unique',filtered(mapped(inp('replies','DEV2_ADT_LAN'),
        op('replace',op('upper',text(get(var('item'),'MACAddress'))),old='-',new=':')),
        op('match',var('item'),pattern=r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}')))
    fields['identity.lan_mac']=choose(op('eq',op('length',macs),1),get(macs,0))
    fields['slow.network_settings']=alg('network_observation',inp('replies'),inp('profile'))
    return fields
