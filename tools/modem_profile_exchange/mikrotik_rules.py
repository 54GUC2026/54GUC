"""RouterOS field bindings; all parser algorithms are embedded JSON operations."""
from tools.modem_profile_exchange.rule_authoring import (
    op,inp,get,choose,let,var,seq,obj,call,alg,either,both,text,stripped,first,mapped,filtered)


def fields():
    row=inp('replies','/interface/lte/monitor')
    monitor=alg('parse_routeros_lte_monitor_row',row)
    m=lambda name:get(monitor,name)
    result={}
    for source,address,default in [('sinr','rf.sinr_db',0),('rsrp','rf.rsrp_dbm',-140),
            ('rsrq','rf.rsrq_db',-20),('rssi','rf.rssi_dbm',-100)]:
        raw=either(m(source),default)
        result[address]=choose(both(op('ne',source,'sinr'),op('le',raw,-900)),raw,
            op('multiply',raw,either(inp('context','signal_scales',source),1)))
    result['radio.serving_band']=either(m('primary_band'),0)
    result['rf.pci']=either(m('pci'),0)
    result['rf.cell_id']=either(m('cell_id'),0)
    result['rf.enb_id']=either(m('enb_id'),0)
    qeng=alg('parse_qeng_servingcell',inp('replies','AT+QENG="servingcell"'))
    extra=alg('serving_extra',qeng,either(inp('context','previous_serving_extra'),{}))
    qeng_mcs=alg('qeng_mcs',inp('replies','AT+QENG="servingcell"'),inp('context','previous_qeng_mcs'))
    qca=alg('qcainfo',either(inp('replies','AT+QCAINFO'),''))
    pcell=get(filtered(either(qca,[]),get(var('item'),'is_pcell')),0,{})
    result['rf.earfcn_dl']=either(m('earfcn'),get(pcell,'earfcn'),get(qeng,'earfcn'),0)
    result['slow.tac']=either(m('tac'),get(extra,'tac'),0)
    result['rf.ri']=op('maximum',either(m('ri'),0),0)
    result['rf.cqi0']=either(m('cqi'),0)
    result['health.radio_connected']=m('radio_connected')
    result['health.radio_state_text']=choose(m('radio_connected'),'Connected','Disconnected')
    result['radio.network_type']=choose(m('radio_connected'),'LTE (4G)','')
    for native,address in [('mcs_dl','rf.mcs_dl0'),('mcs_ul','rf.mcs_ul')]:
        qnw=alg('qnwcfg_mcs',inp('replies','AT+QNWCFG="mcs"'),inp('context','previous_qnwcfg'))
        key='mcs_dl0' if native=='mcs_dl' else native
        reported=first(get(qnw,key),m(native),0)
        result[address]=either(reported,get(qeng_mcs,native),0)
    result['rf.mcs_dl1']=either(get(qnw,'mcs_dl1'),0)
    for address,key,reported in [('rf.qam_dl0','rf.mcs_dl0','qam_dl0_reported'),
            ('rf.qam_ul','rf.mcs_ul','qam_ul_reported'),('rf.qam_dl1','rf.mcs_dl1',None)]:
        qm=either(m(reported) if reported else None,get(alg('resolve_lte_qm_with_retx_hold',result[key],
            either(inp('context','previous_qm',address),0)),0))
        if key=='rf.mcs_dl1':qm=choose(op('gt',result[key],0),qm,0)
        if key=='rf.mcs_ul':qm=choose(either(op('gt',result[key],0),op('contains',[28,29,30,31],result[key]),m(reported)),qm,0)
        result[address]=qm
    result['slow.dl_bandwidth_mhz']=either(m('dl_bw_mhz'),get(extra,'dl_bw_mhz_qeng'))
    result['slow.ul_bandwidth_mhz']=get(extra,'ul_bw_mhz')
    result['radio.tx_power']=get(extra,'tx_power')
    frequency=alg('lte_frequency_mhz_from_dl_earfcn',result['rf.earfcn_dl'])
    result['slow.dl_freq_mhz']=either(get(frequency,0),None)
    result['slow.ul_freq_mhz']=either(get(frequency,1),None)
    for address,key in [('radio.ca_component_band_set','ca_component_band_set'),('radio.ca_component_band_signature','ca_component_band_signature'),
            ('radio.ca_component_count','ca_component_count'),('radio.ca_composition_signature','ca_composition_signature'),
            ('radio.ca_source','ca_source'),('radio.carrier_rows','carrier_rows')]:result[address]=m(key)
    from_qca=op('sorted_unique',mapped(filtered(either(qca,[]),either(
        op('ge',get(var('item'),'state',0),1),get(var('item'),'is_pcell'))),get(var('item'),'band')))
    active=either(from_qca,m('active_band_set'),[])
    for address in ['radio.active_band_set','radio.band_set']:result[address]=active
    for address in ['radio.active_band_set_signature','radio.band_set_signature']:result[address]=alg('band_signature',active)
    result['radio.ca_active']=either({'op':'some','items':either(qca,[]),'expression':op('ge',get(var('item'),'state',0),1)},m('ca_active'),False)
    result['radio.ca_state_text']=choose(result['radio.ca_active'],'ACTIVE','INACTIVE')
    interface=inp('replies','/interface/lte/print')
    # Native RouterOS configured mask is deliberately distinct from CA rows.
    pool=alg('normalize_band_list',mapped(filtered(op('split',text(get(interface,'band')),separator=','),
        op('match',var('item'),pattern=r'\d+')),op('integer',var('item'))))
    for address in ['radio.configured_band_pool','radio.selected_band_set']:result[address]=pool
    for address in ['radio.ca_signature','radio.configured_band_pool_signature','radio.selected_band_set_signature']:
        result[address]=alg('band_signature',pool)
    result['radio.carrier_count']=op('length',pool)
    result['traffic.packet_session_connected']=m('radio_connected')
    result['traffic.connection_status_code']=choose(m('radio_connected'),'connected','disconnected')
    result['traffic.session_uptime_s']=either(alg('parse_routeros_duration_seconds',get(row,'session-uptime','')),0.0)
    result['traffic.traffic_valid']=m('radio_connected')
    result['traffic.traffic_state_detail']=choose(m('radio_connected'),'routeros_lte_connected','routeros_lte_disconnected')
    result['traffic.traffic_invalid_reason']=choose(m('radio_connected'),'','routeros_lte_disconnected')
    rates=inp('replies','/interface/monitor-traffic')
    for native,address in [('rx-bits-per-second','traffic.rx_bytes_per_sec'),('tx-bits-per-second','traffic.tx_bytes_per_sec')]:
        result[address]=op('divide',op('number',either(get(rates,native),0)),8)
    stats=inp('replies','/interface/print_stats')
    for names,address in [(['rx-byte','rx-bytes','rx-byte-total','rx-total-byte'],'traffic.traffic_device_download_counter_bytes'),
            (['tx-byte','tx-bytes','tx-byte-total','tx-total-byte'],'traffic.traffic_device_upload_counter_bytes')]:
        result[address]=first(*[{'op':'try','value':op('integer',op('extract',op('replace',stripped(get(stats,name)),old=' ',new=''),pattern=r'^(\d+)',group=1))} for name in names])
    result['traffic.traffic_total_download_bytes']=either(result['traffic.traffic_device_download_counter_bytes'],0)
    result['traffic.traffic_total_upload_bytes']=either(result['traffic.traffic_device_upload_counter_bytes'],0)
    sampled=inp('context','counter_sampled_at')
    result['traffic.traffic_accounting_source']='routeros_interface_stats'
    result['traffic.traffic_rate_source']='routeros_monitor_traffic'
    result['traffic.traffic_volume_poll_interval_s']=60.0
    result['traffic.traffic_volume_last_sample_ts']=sampled
    result['traffic.traffic_volume_next_sample_ts']=op('add',sampled,60)
    result['traffic.traffic_volume_age_s']=op('maximum',0,op('subtract',inp('context','sampled_at'),sampled))
    uptime=alg('parse_routeros_duration_seconds',inp('replies','/system/resource','uptime'))
    result['traffic.traffic_counter_period_started_at']=choose(op('ne',uptime,None),
        either(op('maximum',0,op('subtract',op('subtract',inp('context','resource_sampled_at'),uptime),1)),None))
    session=alg('parse_routeros_duration_seconds',get(row,'session-uptime',''))
    monitor_at=inp('context','monitor_sampled_at')
    fresh=both(m('radio_connected'),op('gt',monitor_at,0),
        op('ge',op('subtract',sampled,monitor_at),0),
        op('le',op('subtract',sampled,monitor_at),op('maximum',2,op('multiply',inp('context','monitor_interval_s'),2))),
        op('le',either(inp('context','monitor_failure_at'),0),monitor_at))
    result['traffic.traffic_counter_interface_started_at']=choose(both(fresh,op('ne',session,None)),
        either(op('maximum',0,op('subtract',op('subtract',inp('context','monitor_sampled_at'),session),1)),None))
    pair=seq(result['traffic.traffic_device_download_counter_bytes'],result['traffic.traffic_device_upload_counter_bytes'])
    transition=alg('routeros_counter_transition',pair,either(inp('context','counter_previous'),{}),sampled,
        result['traffic.traffic_counter_period_started_at'],result['traffic.traffic_counter_interface_started_at'])
    result['traffic.traffic_accounting_status']=get(transition,'status')
    for index,address in [(0,'traffic.traffic_session_download_bytes'),(1,'traffic.traffic_session_upload_bytes')]:
        result[address]=get(get(transition,'session_totals'),index)
    identity=inp('replies','/system/routerboard')
    resource=inp('replies','/system/resource')
    for address,value in [('identity.serial',get(identity,'serial-number')),('identity.hardware_version',either(get(resource,'architecture-name'),get(identity,'model'))),
            ('identity.router_model',get(identity,'model')),('identity.software_version',get(resource,'version')),
            ('identity.software',get(resource,'version')),('identity.model',op('strip',op('join',seq(either(get(identity,'model'),get(resource,'board-name'),''),either(m('modem_model'),'')),separator=' '))),
            ('identity.firmware',m('firmware')),('identity.imei',m('imei'))]:result[address]=value
    result['identity.provider_name']=get(row,'current-operator','')
    operator=get(row,'current-operator','')
    result['identity.provider_plmn']=either(op('extract',operator,pattern=r'\b(\d{5,6})\b',group=1),
        get(inp('context','provider_plmn_by_name'),op('lower',operator)),'')
    # Optional network identity is a separate native read, not part of RF.
    result['slow.wan_ip']=alg('_clean_ip_address',inp('replies','/ip/address','address'))
    wifi=alg('routeros_wifi',choose(op('eq',inp('context','native_wifi_path'),'/interface/wireless'),
        inp('replies','/interface/wireless'),inp('replies','/interface/wifi')),
        inp('context','previous_wifi'))
    for address,radio in [('slow.wifi_24ghz_enabled','2.4'),('slow.wifi_5ghz_enabled','5')]:
        result[address]=get(wifi,1 if radio=='2.4' else 2)
    result['slow.wifi_enabled']=get(wifi,0)
    # Additive explicit NR monitor grammar. Profile trust/selection determines
    # which exact profiles expose it; no LTE input supplies an NR measurement.
    nr = choose(op('eq', inp('context', 'routeros_nr_read_source'), 'qeng'),
        alg('parse_routeros_nr_qeng', inp('replies', 'AT+QENG="servingcell"')),
        alg('parse_routeros_nr_monitor_row', row))
    result['radio.network_type'] = choose(both(
        op('eq', inp('context', 'routeros_nr_current_interface'), True),
        op('eq', get(nr, 'nr5g_active'), True)),
        op('join', seq('NR ', get(nr, 'nr_mode'), ' (5G)'), separator=''), result['radio.network_type'])
    for address, name in (
        ('radio.nr5g_active', 'nr5g_active'), ('radio.nr_mode', 'nr_mode'),
        ('radio.nr_band', 'nr_band'), ('radio.nr_pci', 'nr_pci'),
        ('radio.nr_arfcn', 'nr_arfcn'), ('radio.nr_rsrp_dbm', 'nr_rsrp_dbm'),
        ('radio.nr_rsrq_db', 'nr_rsrq_db'), ('radio.nr_sinr_db', 'nr_sinr_db'),
        ('slow.nr_bandwidth_mhz', 'nr_bandwidth_mhz'), ('slow.nr_cells', 'nr_cells'),
    ):
        result[address] = get(nr, name)
    result['slow.nr_ul_bandwidth_mhz'] = alg('parse_routeros_nr_qcainfo_ul', inp('replies', 'AT+QCAINFO'))
    return result
