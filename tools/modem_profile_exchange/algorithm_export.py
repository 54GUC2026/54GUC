"""Export only the reviewed pure parser modules to the JSON algorithm vocabulary."""
import ast
from copy import deepcopy
from pathlib import Path

from tools.modem_profile_exchange.algorithm import compile_node

ROOT=Path(__file__).resolve().parents[2]


def add_module(library,path,*,functions=None):
    tree=ast.parse((ROOT/path).read_text(encoding='utf-8-sig'))
    for node in tree.body:
        if isinstance(node,ast.FunctionDef) and (functions is None or node.name in functions):
            library['functions'][node.name]=compile_node(node)
        elif functions is None and isinstance(node,(ast.Assign,ast.AnnAssign)):
            targets=node.targets if isinstance(node,ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target,ast.Name) and node.value is not None:
                    library['constants'][target.id]=compile_node(node.value)


class UnbindPureMethod(ast.NodeTransformer):
    def visit_Attribute(self,node):
        if isinstance(node.value,ast.Name) and node.value.id=='self' and node.attr in {'_parse_nr_band','_dedupe_bands','_goform_source_observed','_normalize_network_type'}:
            return ast.Name(id={'_parse_nr_band':'parse_nr_band','_dedupe_bands':'normalize_band_list',
                '_goform_source_observed':'goform_source_observed','_normalize_network_type':'normalize_network_type'}[node.attr],ctx=ast.Load())
        return self.generic_visit(node)


def library_for(family):
    result={'vocabulary':'54GUC.BoundedParserAlgorithms.v1','functions':{},'constants':{},'initializers':[]}
    add_module(result,'arch4/interfaces.py',functions={'normalize_band_list','band_signature','parse_band_signature'})
    add_module(result,'arch4/modem/lte_frequency.py')
    add_module(result,'arch4/lte_mcs.py')
    if family=='huawei':
        add_module(result,'arch4/modem/huawei_parsers.py')
        add_module(result,'arch4/modem/identity.py',functions={'normalize_modem_identity_fields'})
        add_module(result,'arch4/modem/utils.py',functions={'xml_text'})
        tree=ast.parse((ROOT/'arch4/modem/huawei_b818.py').read_text(encoding='utf-8'))
        add_huawei_read_algorithms(result,tree)
        result['functions']['isfinite']=compile_node(ast.parse('def isfinite(value): return math.isfinite(value)').body[0])
    elif family=='mikrotik':
        add_module(result,'arch4/modem/mikrotik_parsers.py')
        add_module(result,'arch4/core_contracts/traffic_counter_transition.py')
        add_mikrotik_algorithms(result)
    elif family in {'goform','zwrt'}:
        add_module(result,'arch4/modem/zte_parsers.py')
        tree=ast.parse((ROOT/'arch4/modem/zte_goform.py').read_text(encoding='utf-8'))
        root=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_parse_nr5g_status')
        root=UnbindPureMethod().visit(deepcopy(root));root.args.args=root.args.args[1:];root.name='nr_status'
        result['functions']['nr_status']=compile_node(root)
        for source,name in [('_goform_source_observed','goform_source_observed'),('_raw_radio_connected','radio_connected'),('_normalize_network_type','normalize_network_type')]:
            node=deepcopy(next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name==source))
            if source in {'_raw_radio_connected','_normalize_network_type'}:node.args.args=node.args.args[1:]
            node.name=name
            result['functions'][name]=compile_node(UnbindPureMethod().visit(node))
        if family=='zwrt':
            add_module(result,'arch4/modem/zte_zwrt.py',functions={'_cpu_temperature'})
            add_module(result,'arch4/modem/zte_ng_network_settings.py',functions={'_wwan_flags','_mobile_data_enabled'})
            add_zwrt_preprocessing(result)
    if family in {'dev2','xiaomi'}:
        add_module(result,'arch4/core_contracts/network_settings.py',functions={'subscription_key','_text','validate_network_observation'})
        path='arch4/modem/tplink_dev2.py' if family=='dev2' else 'arch4/modem/xiaomi_miwifi.py'
        add_module(result,path,functions={'number','operator_plmn'} if family=='dev2' else {'unique_text','band_list','selected_bands'})
        if family=='dev2':
            add_module(result,path,functions={'native_radio_rat','select_native_serving_rows','native_byte_counter','native_traffic_counter_pair','native_period_values','native_router_resource','bands','native_measurement'})
            module=ast.parse((ROOT/path).read_text(encoding='utf-8'))
            for node in module.body:
                if isinstance(node,ast.Assign):
                    for target in node.targets:
                        if isinstance(target,ast.Name) and target.id in {'AUTH','PDP','NATIVE_INTEGER_FIELDS','TRAFFIC_COUNTER_FIELDS','TRAFFIC_COUNTER_CANONICAL_FIELDS','NATIVE_PERIOD_FIELDS','NATIVE_PERIOD_CANONICAL_FIELDS'}:result['constants'][target.id]=compile_node(node.value)
            rates=deepcopy(next(n for n in ast.walk(module) if isinstance(n,ast.FunctionDef) and n.name=='_wan_rates'))
            rates.name='wan_rates';rates.args.args=rates.args.args[1:]
            rates.body=[n for n in rates.body if not isinstance(n,ast.Assign) or not any(isinstance(t,ast.Attribute) for t in n.targets)]
            rates.body[:0]=ast.parse('if previous is not None:previous=tuple(previous)').body
            result['functions']['wan_rates']=compile_node(rates)
        tree=ast.parse((ROOT/path).read_text(encoding='utf-8'))
        if family=='xiaomi':
            add_module(result,path,functions={'sim_identity','network_observation','stock_model','device_text'})
        else:
            add_module(result,path,functions={'network_observation'})
        result['functions']=replace_names(result['functions'],{'Mapping':'dict','MiWiFiProtocolError':'ValueError','NetworkSettingsReadIncomplete':'ValueError','Dev2ProtocolError':'ValueError'})
    return result


class StripLogging(ast.NodeTransformer):
    def visit_Expr(self,node):
        if isinstance(node.value,ast.Call) and isinstance(node.value.func,ast.Attribute) and isinstance(node.value.func.value,ast.Name) and node.value.func.value.id=='log':
            return ast.Pass()
        return self.generic_visit(node)


def replace_names(value,names):
    if isinstance(value,list):return [replace_names(v,names) for v in value]
    if not isinstance(value,dict):return value
    result={k:replace_names(v,names) for k,v in value.items()}
    if result.get('kind')=='name':result['name']=names.get(result['name'],result['name'])
    return result




def add_huawei_read_algorithms(library,tree):
    def method(name):return deepcopy(next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name==name))
    # Wi-Fi conversions live in huawei_parsers with the other pure native
    # conversions. Do not extract them from the backend's I/O/cache owner.
    for source,name in [('_read_uptime','huawei_uptime')]:
        node=method(source);node.name=name
        node.args=ast.parse('def f(xml):pass').body[0].args
        start=next(i for i,n in enumerate(node.body) if isinstance(n,ast.If) and isinstance(n.test,ast.UnaryOp) and isinstance(n.test.operand,ast.Name) and n.test.operand.id=='xml')
        node.body=node.body[start:]
        library['functions'][name]=compile_node(StripLogging().visit(node))


class PrefixNames(ast.NodeTransformer):
    def __init__(self,names):self.names=names
    def visit_Name(self,node):
        node.id=self.names.get(node.id,node.id);return node
    def visit_FunctionDef(self,node):
        node.name=self.names.get(node.name,node.name);return self.generic_visit(node)


def add_prefixed_module(library,path,prefix):
    tree=ast.parse((ROOT/path).read_text(encoding='utf-8'))
    names={}
    for n in tree.body:
        if isinstance(n,ast.FunctionDef):names[n.name]=prefix+n.name
        if isinstance(n,(ast.Assign,ast.AnnAssign)):
            for t in n.targets if isinstance(n,ast.Assign) else [n.target]:
                if isinstance(t,ast.Name):names[t.id]=prefix+t.id
    tree=PrefixNames(names).visit(tree)
    for node in tree.body:
        if isinstance(node,ast.FunctionDef):library['functions'][node.name]=compile_node(node)
        elif isinstance(node,(ast.Assign,ast.AnnAssign,ast.For)):
            library['initializers'].append(compile_node(node))
    return names


class ZwrtReads(ast.NodeTransformer):
    METHODS={'_set_first','_coalesce','_extract_zwrt_wan_ip','_normalize_service_measurements','_translate_native_lte_carriers'}
    def visit_ImportFrom(self,node):return ast.Pass()
    def visit_Assign(self,node):
        # Native evidence plumbing is not part of the pure reply conversion.
        if any(isinstance(t,ast.Attribute) and isinstance(t.value,ast.Name)
               and t.value.id=='self' and t.attr=='_portable_reply_groups' for t in node.targets):
            return ast.Pass()
        return self.generic_visit(node)
    def visit_Call(self,node):
        if isinstance(node.func,ast.Attribute) and isinstance(node.func.value,ast.Name) and node.func.value.id=='self':
            if node.func.attr=='_call_ubus_current_session':
                group='status_batch' if 'ZWRT_STATUS_CALLS' in ast.unparse(node.args[0]) else 'metadata_batch'
                return ast.Call(func=ast.Attribute(value=ast.Name(id='replies',ctx=ast.Load()),attr='get',ctx=ast.Load()),args=[ast.Constant(group),ast.List(elts=[],ctx=ast.Load())],keywords=[])
            if node.func.attr in self.METHODS:
                node.func=ast.Name(id=node.func.attr,ctx=ast.Load())
        return self.generic_visit(node)
    def visit_Attribute(self,node):
        if isinstance(node.value,ast.Name) and node.value.id=='self' and node.attr=='_expected_serial':
            return ast.Call(func=ast.Attribute(value=ast.Name(id='context',ctx=ast.Load()),attr='get',ctx=ast.Load()),args=[ast.Constant('operator_serial'),ast.Constant('')],keywords=[])
        return self.generic_visit(node)


def add_zwrt_preprocessing(library):
    sim=add_prefixed_module(library,'arch4/modem/zte_zwrt_sim.py','sim__')
    traffic=add_prefixed_module(library,'arch4/modem/zte_zwrt_traffic.py','traffic__')
    add_module(library,'arch4/runtime_identity.py',functions={'_text','_lower','is_placeholder_identity_text'})
    tree=ast.parse((ROOT/'arch4/runtime_identity.py').read_text())
    constant=next(n for n in tree.body if isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name) and n.target.id=='_PROVIDER_PLACEHOLDER_VALUES')
    library['constants']['_PROVIDER_PLACEHOLDER_VALUES']=compile_node(constant.value)
    add_module(library,'arch4/modem/operator_identity.py',functions={'operator_serial_text'})
    tree=ast.parse((ROOT/'arch4/modem/zte_zwrt.py').read_text(encoding='utf-8'))
    names=ZwrtReads.METHODS|{'_fetch_flat_status'}
    for node in ast.walk(tree):
        if not isinstance(node,ast.FunctionDef) or node.name not in names:continue
        node=deepcopy(node)
        if node.name in {'_set_first','_fetch_flat_status'}:node.args.args=node.args.args[1:]
        if node.name=='_fetch_flat_status':
            node.name='zwrt_flatten';node.args=ast.parse('def f(replies,context):pass').body[0].args
            # This is the native CPU parser's result. The later portable-field
            # overlay is caller state and must never recursively enter its codec.
            start=next(i for i,n in enumerate(node.body) if isinstance(n,ast.Assign)
                       and any(isinstance(t,ast.Name) and t.id=='values' for t in n.targets)
                       and '_decode_profile_values' in ast.unparse(n.value))
            end=next(i for i,n in enumerate(node.body[start:],start)
                     if isinstance(n,ast.Expr) and ast.unparse(n.value).startswith("raw.pop('temperature_cpu_c'"))
            del node.body[start:end]
        node=ZwrtReads().visit(node)
        definition=compile_node(node)
        definition=replace_names(definition,{**{k:v for k,v in sim.items() if k=='active_sim'},**{k:v for k,v in traffic.items() if k in {'decode_zwrt_traffic','ZWRT_TRAFFIC_PARENT_FIELDS'}}})
        library['functions'][node.name]=definition
    for node in tree.body:
        if isinstance(node,(ast.Assign,ast.AnnAssign)):
            targets=node.targets if isinstance(node,ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target,ast.Name) and target.id=='ZWRT_NR_MEASUREMENT_FIELDS':library['constants'][target.id]=compile_node(node.value)
    library['functions']=replace_names(library['functions'],{'Mapping':'dict'})


def add_mikrotik_algorithms(library):
    tree=ast.parse((ROOT/'arch4/modem/mikrotik.py').read_text(encoding='utf-8'))
    for name in ('_rb_to_mhz','_clean_ip_address'):
        node=deepcopy(next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name==name))
        library['functions'][name]=compile_node(StripLogging().visit(node))
    qca=deepcopy(next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_parse_qcainfo'))
    class CarrierRows(ast.NodeTransformer):
        def visit_Call(self,node):
            if isinstance(node.func,ast.Name) and node.func.id=='CarrierInfo':
                return ast.Dict(keys=[ast.Constant(k.arg) for k in node.keywords],values=[self.visit(k.value) for k in node.keywords])
            return self.generic_visit(node)
    library['functions']['qcainfo']=compile_node(CarrierRows().visit(StripLogging().visit(qca)))
    # The stateless parser body is identical to the read method, with its
    # explicit prior successful observation supplied as data.
    node=deepcopy(next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_parse_qnwcfg_mcs'))
    start=next(i for i,n in enumerate(node.body) if isinstance(n,ast.If) and 'not raw' in ast.unparse(n.test))
    node.body=node.body[start:];node.name='qnwcfg_mcs';node.args=ast.parse('def f(raw,previous):pass').body[0].args
    class CacheData(ast.NodeTransformer):
        def visit_Assign(self,n):
            if any(isinstance(t,ast.Attribute) for t in n.targets):return ast.Pass()
            return self.generic_visit(n)
        def visit_Attribute(self,n):
            if isinstance(n.value,ast.Name) and n.value.id=='self':return ast.Name(id='previous',ctx=ast.Load())
            return self.generic_visit(n)
    library['functions']['qnwcfg_mcs']=compile_node(CacheData().visit(node))
    qeng=deepcopy(next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_parse_qeng_mcs'))
    start=next(i for i,n in enumerate(qeng.body) if isinstance(n,ast.If) and 'not raw' in ast.unparse(n.test))
    qeng.body=qeng.body[start:];qeng.name='qeng_mcs';qeng.args=ast.parse('def f(raw,previous):pass').body[0].args
    library['functions']['qeng_mcs']=compile_node(CacheData().visit(qeng))
    extra=deepcopy(next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_read_serving_cell_extra'))
    start=next(i for i,n in enumerate(extra.body) if isinstance(n,ast.If) and ast.unparse(n.test)=='not sc')
    end=next(i for i,n in enumerate(extra.body) if isinstance(n,ast.If) and '_trans_mode_cache' in ast.unparse(n.test))
    extra.body=extra.body[start:end]+ast.parse('return dict(result)').body
    extra.name='serving_extra';extra.args=ast.parse('def f(sc,previous):pass').body[0].args
    library['functions']['serving_extra']=compile_node(CacheData().visit(extra))
    transition=ast.parse('''def routeros_counter_transition(current,previous,observed_at,period_started_at,interface_started_at):
 pair=previous.get('pair')
 previous_at=previous.get('accepted_at',0)
 totals=previous.get('session_totals',[0,0])
 increment=counter_pair_increment(tuple(pair) if pair is not None else None,tuple(current),previous_at=previous_at,observed_at=observed_at,period_started_at=period_started_at,interface_started_at=interface_started_at)
 restarted=bool(pair is not None and increment is not None and ((period_started_at is not None and previous_at<period_started_at) or (interface_started_at is not None and previous_at<interface_started_at and current[0]<pair[0])))
 status='baseline' if pair is None else 'counter_discontinuity' if increment is None else 'counter_reset' if restarted else 'updated'
 return {'status':status,'session_totals':[totals[i]+(increment[i] if increment is not None else 0) for i in range(2)],'pair':current if increment is not None else pair,'accepted_at':observed_at if increment is not None else previous_at}
''').body[0]
    library['functions']['routeros_counter_transition']=compile_node(transition)
