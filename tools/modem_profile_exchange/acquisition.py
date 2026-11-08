"""Native read coordinates and caller-owned context, exported as profile data."""
import ast
from tools.modem_profile_exchange.algorithm_export import ROOT
from tools.modem_profile_exchange.vocabulary import walk


def static_value(path,name):
    tree=ast.parse((ROOT/path).read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node,(ast.Assign,ast.AnnAssign)):
            targets=node.targets if isinstance(node,ast.Assign) else [node.target]
            if any(isinstance(t,ast.Name) and t.id==name for t in targets):return ast.literal_eval(node.value)
    raise ValueError('native_read_coordinate_missing:'+name)


def contract(family,fields):
    paths=sorted({tuple(n['path']) for n in walk(fields) if n.get('op')=='input' and n['path'][0]=='replies'})
    aliases={p[1] for p in paths if len(p)>1}
    if any(s['definition']['address']=='slow.network_settings' and s['read'] is not None for s in fields):
        if family=='dev2':aliases|={'DEV2_CELL_INTF_USIM','DEV2_LTE_WAN_CFG','DEV2_LTE_NETWORK_SEARCH','DEV2_LTE_PROFILE'}
        if family=='xiaomi':aliases|={'xqdtcustom/get_sim_info','xqmobile/get_mobile_net_cfg','xqmobile/get_apn_info'}
    reads=[]
    for alias in sorted(aliases):
        item={'reply':alias,'boundary':'transport-validated native reply; no caller-decoded measurement'}
        if family=='huawei':item.update(protocol='hilink_xml',method='GET',path=alias)
        elif family=='xiaomi':item.update(protocol='miwifi_json',method='GET',path='/cgi-bin/luci/;stok={session}/api/'+alias)
        elif family=='dev2':
            listing=alias in {'DEV2_LTE_SERVING_CELL_INFO','DEV2_ADT_WAN','DEV2_ADT_WIFI_COMMON','DEV2_ADT_LAN','DEV2_LTE_PROFILE'}
            item.update(protocol='tplink_dev2',operation='gl' if listing else 'go',object_id=alias,
                stack='0,0,0,0,0,0' if listing or alias=='DEV2_DEV_INFO' else '1,0,0,0,0,0',
                reply_shape='array_of_rows_with_stack' if listing else 'object')
        elif family=='mikrotik':
            if alias.startswith('AT+'):
                item.update(protocol='routeros_at',path='/interface/lte',operation='at-chat',input=alias,interface='lte1')
            else:
                request={'/interface/lte/monitor':('/interface/lte','monitor'),'/interface/lte/print':('/interface/lte','print'),
                    '/interface/monitor-traffic':('/interface','monitor-traffic'),'/interface/print_stats':('/interface','print')}
                path,operation=request.get(alias,(alias,'print'))
                item.update(protocol='routeros_api',path=path,operation=operation,
                    selector='unique named lte1 row' if alias.startswith('/interface/lte') or alias.startswith('/interface/print') else 'reported native rows')
        elif family=='goform':
            item.update(protocol='zte_goform',method='GET',path='/goform/goform_get_cmd_process',
                parameters={'isTest':'false','multi_data':'1'},field_parameter='cmd',
                startup_fields=list(static_value('arch4/modem/zte_goform.py','GOFORM_READ_FIELDS')),
                fast_fields=list(static_value('arch4/modem/zte_goform.py','GOFORM_FAST_READ_FIELDS')))
        elif family=='zwrt':
            if alias=='nwinfo':continue  # produced from the exact two native batches below
            item.update(protocol='zwrt_ubus',method='POST',path='/ubus')
            if alias=='cpu_thermal':item.update(service='zwrt_bsp.thermal',operation='get_cpu_temp',parameters={})
            elif alias=='zwrt_data.get_wwaniface':item.update(service='zwrt_data',operation='get_wwaniface',parameters={'cid':1})
        reads.append(item)
    if family=='zwrt':
        for alias,name in [('status_batch','ZWRT_STATUS_CALLS'),('metadata_batch','ZWRT_METADATA_CALLS')]:
            reads.append({'reply':alias,'protocol':'zwrt_ubus','method':'POST','path':'/ubus',
                'calls':static_value('arch4/modem/zte_zwrt.py',name),
                'boundary':'ordered per-call success/data envelopes from the existing UBUS transport'})
    context=sorted({'.'.join(n['path'][1:]) for n in walk(fields) if n.get('op')=='input' and n['path'][0]=='context'})
    return {'reads':reads,'context_paths':context,
        'context_owner':'existing ModemLive acquisition state and Foundation provider policy; never an alternate controller',
        'clock_contract':'sampled_at is epoch seconds; monotonic_at is monotonic seconds; previous clocks retain the same domain',
        'previous_state_contract':'only the previous accepted observation from this same physical identity/session; preserve native source key and time',
        'freshness_contract':'native parser defaults and retained values are not fresh facts; existing acquisition receipts and Matrix admission retain authority',
        'write_contract':'capability/action metadata retained; native typed write stages and authorization are not implemented by this offline reader'}
