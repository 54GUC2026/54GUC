"""One startup snapshot of portable profile definitions. No embedded fallback.

Private transport credentials and physical device state are not profile data.
The parser plan remains private to this owner and is never reloaded mid-lease.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import zlib
from weakref import WeakValueDictionary
from threading import Lock
from functools import lru_cache
from urllib.parse import urlsplit
from .readable import compile_rule,DefinitionError
from .replay import decode
from .dependencies import reply_dependencies,native_dependencies,native_field_keys
from .vocabulary import validate_vocabulary,walk
from .fields import canonical_fields
from .expressions import Evaluator
from .shape import validate_shape,ShapeError

SCHEMA='54GUC.ModemProfile.v1'
MAX_FILE_BYTES=8*1024*1024
MAX_FILES=256

# Existing typed stages remain the only writers. A profile can translate their
# ordinary parameter fields, never choose a service, command, host or device ID.
NATIVE_FIELD_STAGES = {
    'goform:BAND_SELECT': ('zte_webui_like', {'is_gw_band','gw_band_mask','is_lte_band','lte_band_mask','nr5g_nsa_band_lock'}),
    'goform:SET_LTE_BAND_LOCK': ('zte_webui_like', {'lte_band_lock'}),
    'goform:SET_BEARER_PREFERENCE': ('zte_webui_like', {'BearerPreference'}),
    'goform:DISCONNECT_NETWORK': ('zte_webui_like', {'notCallback'}),
    'goform:CONNECT_NETWORK': ('zte_webui_like', {'notCallback'}),
    'goform:LTE_LOCK_CELL_SET': ('zte_webui_like', {'lte_pci_lock','lte_earfcn_lock'}),
    'hilink:/api/net/net-mode': ('huawei_hilink_like', {'NetworkMode','NetworkBand','LTEBand','networkOption'}),
    'hilink:/api/dialup/mobile-dataswitch': ('huawei_hilink_like', {'dataswitch'}),
    'zwrt:zte_nwinfo_api.nwinfo_set_gwl_bandlock': ('zte_zwrt_ubus_like', {'is_gw_band','gw_band_mask','is_lte_band','lte_band_mask'}),
    'zwrt:zte_nwinfo_api.nwinfo_lock_lte_cell': ('zte_zwrt_ubus_like', {'lock_lte_pci','lock_lte_earfcn'}),
    'zwrt:zte_nwinfo_api.nwinfo_set_nrbandlock': ('zte_zwrt_ubus_like', {'nr5g_type','nr5g_band'}),
    'zwrt:zte_nwinfo_api.nwinfo_set_netselect': ('zte_zwrt_ubus_like', {'net_select'}),
    'routeros:lte.band': ('routeros_at_like', {'band'}),
    'routeros:lte.network-mode': ('routeros_at_like', {'network_mode'}),
}
_NATIVE_RESERVED_KEYS = frozenset({'goformId','isTest','AD','id','.id','service','method','config','section','token','password','username'})


class ProfileError(ValueError):pass


@dataclass(frozen=True,slots=True)
class FileIssue:
    file:str
    profile_id:str
    message:str
    def __str__(self):return f'{self.file}: {self.message}'


@dataclass(frozen=True,slots=True)
class _FileSnapshot:
    path:str
    compressed:bytes
    metadata:dict
    priority:int


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ProfileError('duplicate JSON key')
        result[key]=value
    return result


def _parse(raw):
    try:return json.loads(raw.decode('utf-8-sig'),object_pairs_hook=_pairs,
                         parse_constant=lambda _:(_ for _ in()).throw(ProfileError('finite JSON values required')))
    except ProfileError:raise
    except (ValueError,UnicodeError,RecursionError):raise ProfileError('invalid UTF-8 JSON')from None


def _check_tree(value,depth=0):
    if depth>100:raise ProfileError('definition nesting limit')
    if isinstance(value,dict):
        for k,v in value.items():
            if not isinstance(k,str)or len(k)>1024:raise ProfileError('invalid object key')
            _check_tree(v,depth+1)
    elif isinstance(value,list):
        if len(value)>10_000:raise ProfileError('definition collection limit')
        for v in value:_check_tree(v,depth+1)
    elif isinstance(value,str):
        if len(value)>250_000:raise ProfileError('definition string limit')
        if re.search(r'(?:github_pat_|ghp_|sk-proj-)[A-Za-z0-9_]+',value):raise ProfileError('credential value is forbidden')
    elif isinstance(value,float)and not math.isfinite(value):raise ProfileError('finite number required')


@lru_cache(maxsize=1)
def _metadata_schema():
    return json.loads(Path(__file__).with_name('metadata-schema.json').read_text(encoding='utf-8'))


def _validate_metadata(metadata):
    schema=_metadata_schema();properties=schema['properties']
    if set(metadata)-set(properties):raise ProfileError('/profile: unknown metadata field')
    for name,spec in properties.items():
        if name not in metadata:
            if name=='driver':continue  # Earlier V1 snapshots use the legacy binding.
            raise ProfileError('/profile/'+name+': required')
        try:validate_shape(metadata[name],spec)
        except ShapeError:raise ProfileError('/profile/'+name+': invalid type or value')from None
    if metadata.get('driver'):
        from arch4.modem_live.connection_profiles import backend_profile_contract
        native=backend_profile_contract(metadata['driver'])
        family={'zte_webui_like':'zte_goform_like'}.get(metadata['family'],metadata['family'])
        if native is None or native.family!=family:
            raise ProfileError('/profile/driver: registered native driver for this family required')
        if metadata['backend'] and metadata['driver']!=metadata['backend']:
            raise ProfileError('/profile/driver: conflicts with backend matching scope')
    for name in('supported_lte_bands','supported_nr_bands','provider_band_pool'):
        if any(type(v)is not int or v<=0 for v in metadata[name]):raise ProfileError('/profile/'+name+': positive integer bands required')
    if metadata['firmware_exact']and metadata['firmware_pattern']:raise ProfileError('/profile/firmware_pattern: conflicting exact firmware selector')
    from .bounded_regex import compile_pattern
    try:compile_pattern(metadata['firmware_pattern'])
    except ValueError:raise ProfileError('/profile/firmware_pattern: invalid pattern')from None
    if metadata['device_radio_class']=='4g_only'and metadata['nr_band_capability_status']!='unsupported':
        raise ProfileError('/profile/nr_band_capability_status: 4G profile cannot declare NR')
    for rat in('lte','nr'):
        declared=metadata[rat+'_band_capability_status']=='declared'
        if bool(metadata['supported_'+rat+'_bands'])!=declared:
            raise ProfileError('/profile/supported_'+rat+'_bands: bands and declared capability must agree')
    if not metadata['band_capability_reference'].strip():raise ProfileError('/profile/band_capability_reference: required')
    if any(v not in{'confirmed','expected','synthetic','untested','unsupported','non_useful'}for v in metadata['field_trust'].values()):
        raise ProfileError('/profile/field_trust: invalid trust status')


def _compile(document):
    if not isinstance(document,dict):raise ProfileError('/: profile object required')
    allowed={'schema','revision','selection_priority','profile','connection','parameters','fields','requests','access','context','helpers','decoder','preprocessing','state','writes','provenance','privacy','qualification'}
    if set(document)-allowed:raise ProfileError('/: unknown definition member; observations do not belong in a profile')
    _check_tree(document)
    if document.get('schema')!=SCHEMA:raise ProfileError('/schema: unsupported profile format')
    if type(document.get('selection_priority'))is not int or not 0<=document['selection_priority']<=100_000:
        raise ProfileError('/selection_priority: integer from0to100000 required')
    revision=document.get('revision')
    if not isinstance(revision,str)or not revision.strip()or len(revision)>80:raise ProfileError('/revision: nonempty text required')
    metadata=document.get('profile')
    if not isinstance(metadata,dict):raise ProfileError('/profile: object required')
    pid=metadata.get('profile_id')
    if not isinstance(pid,str)or not re.fullmatch(r'[a-z0-9][a-z0-9_]{0,119}',pid):raise ProfileError('/profile/profile_id: invalid ID')
    for field in('backend','vendor','family','display_name','firmware_exact','firmware_pattern','target_guard_model','hardware_exact'):
        if not isinstance(metadata.get(field),str):raise ProfileError('/profile/'+field+': text required')
    _validate_metadata(metadata)
    if 'connection' in document:
        connection=document['connection']
        routeros=metadata['family']=='routeros_at_like'
        if not isinstance(connection,dict) or set(connection)!=({'port'} if routeros else {'scheme','port'}):
            raise ProfileError('/connection: only native scheme and port settings are allowed')
        if not routeros and (not isinstance(connection['scheme'],str) or connection['scheme'] not in {'auto','http','https'}):
            raise ProfileError('/connection/scheme: auto, http or https required')
        port=connection['port']
        if port is not None and (type(port) is not int or not 1<=port<=65535):
            raise ProfileError('/connection/port: null or an integer from 1 to 65535 required')
    access=document.get('access')
    if not isinstance(access,dict):raise ProfileError('/access: timing contract required')
    for name,value in access.items():
        if name.endswith('_s')and value is not None:
            if type(value)not in(int,float)or not math.isfinite(value)or not 0<=value<=86400:
                raise ProfileError('/access/'+name+': finite seconds from0to86400 required')
    if access.get('write_retry_limit')!=1 or access.get('readback_required')is not True or access.get('effective_match_required')is not True:
        raise ProfileError('/access: one write attempt and fresh readback are mandatory')
    requests=document.get('requests')
    if not isinstance(requests,list):raise ProfileError('/requests: native read list required')
    aliases=set()
    for index,request in enumerate(requests):
        pointer='/requests/'+str(index)
        if not isinstance(request,dict)or not isinstance(request.get('reply'),str)or not request['reply']:
            raise ProfileError(pointer+': unique reply name required')
        if request['reply']in aliases:raise ProfileError(pointer+': duplicate reply name')
        aliases.add(request['reply'])
        if request.get('protocol')=='zwrt_ubus':
            if request.get('method')!='POST' or request.get('path')!='/ubus/':
                raise ProfileError(pointer+': same-router UBUS read envelope required')
            calls=request.get('calls')
            if calls is None:
                calls=[{'service':request.get('service'),'method':request.get('operation'),
                        'params':request.get('parameters',{})}]
            expected={'status_batch':5,'metadata_batch':6}.get(request['reply'],1)
            if not isinstance(calls,list) or len(calls)!=expected:
                raise ProfileError(pointer+'/calls: preserve the ordered native read slots')
            read_methods={'nwinfo_get_netinfo','get_cpu_temp','get_device_info','router_get_status',
                'get_wwandst','get_sim_info','status','get','get_modem_msn','get_imei','get_wwaniface'}
            for call in calls:
                if (not isinstance(call,dict) or set(call)!={'service','method','params'}
                        or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]{0,119}',str(call.get('service','')))
                        or call.get('method')not in read_methods or not isinstance(call.get('params'),dict)
                        or len(call['params'])>16):
                    raise ProfileError(pointer+': registered native read method and bounded parameters required')
        if request.get('protocol')in{'routeros_api','routeros_at'}:
            if not re.fullmatch(r'(?:/[a-z][a-z0-9_-]*){1,8}',str(request.get('path',''))):
                raise ProfileError(pointer+'/path: native RouterOS resource path required')
            if request['protocol']=='routeros_api':
                expected=('monitor'if request['reply']=='/interface/lte/monitor'else
                    'monitor-traffic'if request['reply']=='/interface/monitor-traffic'else'print')
                if request.get('operation')!=expected:
                    raise ProfileError(pointer+'/operation: preserve the native read operation')
            else:
                command=request.get('input')
                if (request.get('operation')!='at-chat' or request['path']!='/interface/lte'
                        or request.get('interface')!='lte1' or not isinstance(command,str)
                        or command not in{'AT+QCAINFO','AT+QENG="servingcell"','AT+QNWCFG="mcs"'}
                        and not re.fullmatch(r'AT\+[A-Z][A-Z0-9_]{0,39}\?',command)):
                    raise ProfileError(pointer+'/input: a single supported AT read query is required')
        if request.get('protocol')=='hilink_xml':
            path=request.get('path')
            if (request.get('method')!='GET' or not isinstance(path,str) or len(path)>256
                    or not re.fullmatch(r'/api/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+',path)):
                raise ProfileError(pointer+': same-router native GET path under /api/ required')
        if request.get('protocol')=='zte_goform':
            if request.get('method')!='GET' or not re.fullmatch(r'/goform/goform_get[a-zA-Z0-9_]*',str(request.get('path',''))):
                raise ProfileError(pointer+': a native GoForm read endpoint is required')
            if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]{0,79}',str(request.get('field_parameter',''))):
                raise ProfileError(pointer+'/field_parameter: native field-list parameter required')
            for key in ('startup_fields','fast_fields'):
                value=request.get(key)
                if not isinstance(value,list)or not value or len(value)>512 or any(not isinstance(v,str)or not re.fullmatch(r'[a-zA-Z0-9_]{1,100}',v)for v in value):
                    raise ProfileError(pointer+'/'+key+': bounded native field names required')
            parameters=request.get('parameters')
            if not isinstance(parameters,dict)or len(parameters)>16 or any(not isinstance(k,str)or not isinstance(v,str)or len(k)>80 or len(v)>512 for k,v in parameters.items()):
                raise ProfileError(pointer+'/parameters: bounded native read parameters required')
        if request.get('protocol')=='tplink_dev2':
            if request.get('operation')not in{'go','gl'}:raise ProfileError(pointer+'/operation: reads cannot write')
            if not isinstance(request.get('object_id'),str)or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}',request['object_id']):
                raise ProfileError(pointer+'/object_id: native object name required')
            if not isinstance(request.get('stack'),str)or not re.fullmatch(r'\d+(?:,\d+){5}',request['stack']):
                raise ProfileError(pointer+'/stack: six native coordinates required')
    fields=document.get('fields')
    if not isinstance(fields,dict)or not fields:raise ProfileError('/fields: complete field object required')
    definitions=canonical_fields()
    expected={item['address']:item for item in definitions}
    if set(fields)!=set(expected):raise ProfileError('/fields: complete canonical field coverage required')
    slots=[]
    for address,item in fields.items():
        pointer='/fields/'+address+'/read'
        if not isinstance(item,dict)or item.get('id')!=expected[address]['id']:raise ProfileError('/fields/'+address+'/id: canonical ID mismatch')
        if item.get('value_type')!=expected[address]['value_type']or item.get('unit')!=expected[address]['unit']:raise ProfileError('/fields/'+address+': canonical type/unit mismatch')
        trust=item.get('trust')
        if trust not in {'confirmed','expected','synthetic','untested','unsupported','non_useful','unclassified'}:raise ProfileError('/fields/'+address+'/trust: unknown status')
        formula=item.get('read')
        if address=='identity.profile_definition'and formula is not None:
            raise ProfileError(pointer+': computed application provenance is not a modem read')
        required=item.get('requires',[])
        if not isinstance(required,list)or any(not isinstance(v,str)or not v for v in required):
            raise ProfileError('/fields/'+address+'/requires: native reply names required')
        if trust in {'unsupported','non_useful','unclassified'}:
            if formula is not None:raise ProfileError(pointer+': unsupported field must have an empty read')
            rule=None
        else:
            if formula is None and item.get('availability')!='declared_without_native_producer':raise ProfileError(pointer+': readable rule required')
            rule={'expression':compile_rule(formula,pointer=pointer)if formula is not None else None}
        slots.append({'definition':expected[address],'trust':trust,'read':rule,'requires':required})
    helpers=document.get('helpers',{})
    functions={}
    for name,item in helpers.items():
        functions[name]={'parameters':item['parameters'],'expression':compile_rule(item['formula'],pointer='/helpers/'+name+'/formula')}
    from .codecs import decoder_library
    plan={'metadata':{**metadata,**document.get('parameters',{})},'fields':slots,'functions':functions,'algorithms':{},
          'preprocessing':[]}
    for name,item in document.get('preprocessing',{}).items():
        if not isinstance(item,dict)or set(item)!={'read','requires'}or not isinstance(item['requires'],list)or any(not isinstance(v,str)or not v for v in item['requires']):
            raise ProfileError('/preprocessing/'+name+': read and native reply requirements required')
        plan['preprocessing'].append({'output':name,'expression':compile_rule(item['read'],pointer='/preprocessing/'+name+'/read'),'requires':item['requires']})
    plan['state']={name:compile_rule(formula,pointer='/state/'+name)for name,formula in document.get('state',{}).items()}
    plan['writes']={}
    for name,write in document.get('writes',{}).items():
        pointer='/writes/'+name
        if not isinstance(write,dict)or not isinstance(write.get('fields'),dict):
            raise ProfileError(pointer+': native template object required')
        protocol=write.get('protocol','tplink_dev2')
        if protocol=='native_fields':
            stage=NATIVE_FIELD_STAGES.get(name)
            if (stage is None or stage[0]!=metadata['family'] or write.get('operation')!=name
                    or set(write)!={'protocol','operation','fields'}
                    or set(write['fields'])-stage[1]):
                raise ProfileError(pointer+': parameters must belong to the existing typed native stage')
        elif protocol=='miwifi_json':
            if write.get('operation')!='POST'or write.get('path')!='xqmobile/set_cellular_band':
                raise ProfileError(pointer+': unqualified MiWiFi write')
        elif protocol=='miwifi_radio_mode':
            if (metadata['family']!='xiaomi_miwifi_like' or name!='set_mobile_net_cfg'
                    or write.get('operation')!='GET' or write.get('path')!='xqmobile/set_mobile_net_cfg'
                    or set(write)!={'protocol','operation','path','fields'}
                    or set(write['fields'])!={'networktype','networkdata','networkroam'}):
                raise ProfileError(pointer+': unqualified MiWiFi mode write')
        elif protocol=='tplink_dev2':
            if (write.get('operation')not in{'so','op'}or not isinstance(write.get('object_id'),str)
                    or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}',write['object_id'])):
                raise ProfileError(pointer+': invalid DEV2 native template')
            if write.get('stack')is not None and(not isinstance(write['stack'],str)
                    or not re.fullmatch(r'\d+(?:,\d+){5}',write['stack'])):
                raise ProfileError(pointer+'/stack: native coordinates or runtime stack required')
            if name != write['operation']+':'+write['object_id']:
                raise ProfileError(pointer+': native command must match the qualified stage')
        else:raise ProfileError(pointer+': unqualified native write protocol')
        parameters={}
        native_names=set()
        for parameter,field in write['fields'].items():
            if not isinstance(field,dict)or not isinstance(field.get('key'),str)or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,119}',field['key']):
                raise ProfileError(pointer+'/fields: native field name required')
            if field['key']in native_names:raise ProfileError(pointer+'/fields: duplicate native field')
            if protocol=='native_fields' and field['key'] in _NATIVE_RESERVED_KEYS:
                raise ProfileError(pointer+'/fields: command, authentication and device routing are fixed')
            native_names.add(field['key'])
            parameters[parameter]={'key':field['key'],'expression':compile_rule(field.get('value'),pointer=pointer+'/fields/'+parameter+'/value')}
        plan['writes'][name]={**write,'fields':parameters}
    # Human edits with missing/extra helper parameters are definition errors,
    # not a router failure to discover on the first live read.
    sections=[('/fields/'+slot['definition']['address']+'/read',slot['read']) for slot in slots]
    sections += [('/helpers/'+name+'/formula',value) for name,value in functions.items()]
    sections += [('/preprocessing/'+step['output'],step) for step in plan['preprocessing']]
    sections += [('/state/'+name,value) for name,value in plan['state'].items()]
    sections += [('/writes/'+name,value) for name,value in plan['writes'].items()]
    try:library=decoder_library(document.get('decoder'),family=metadata['family'],expressions=[value for _,value in sections])
    except (ValueError,TypeError):raise ProfileError('/decoder: select the registered decoder for this profile family')from None
    plan['algorithms']=library
    for pointer,value in sections:
        for node in walk(value):
            name=node.get('rule')
            if node.get('op')=='algorithm'and node.get('name')not in library['functions']:
                raise ProfileError(pointer+': decoder operation is not provided by the selected fixed decoder')
            if node.get('op')=='call'and name not in functions:
                raise ProfileError(pointer+': helper is not declared in this profile')
            if node.get('op')=='call' and name in functions:
                if set(node.get('arguments',{}))!=set(functions[name]['parameters']):
                    raise ProfileError(pointer+': parameters do not match /helpers/'+name)
    validate_vocabulary(plan)
    return plan




class PortableProfile:
    __slots__=('profile_id','revision','content_hash','interpretation_hash','path','_document','_plan','_dependencies','_requests','native_private_keys','opaque_native_privacy','__weakref__')
    def __setattr__(self,name,value):
        if hasattr(self,name):raise AttributeError('portable profile snapshot is immutable')
        object.__setattr__(self,name,value)
    def __delattr__(self,name):raise AttributeError('portable profile snapshot is immutable')
    def __init__(self,path,raw,document):
        self._document=deepcopy(document);document=self._document
        self._plan=_compile(document)
        from .private_sources import native_privacy_plan
        self.native_private_keys,self.opaque_native_privacy=native_privacy_plan(self._plan)
        if any(request.get('protocol')=='zte_goform'for request in document['requests']):
            for slot in self._plan['fields']:
                if slot['read']is None:continue
                if any(not isinstance(key,str)or not re.fullmatch(r'[A-Za-z0-9_]{1,100}',key)
                       for key in native_field_keys(slot['read']['expression'],'goform',self._plan['functions'])):
                    raise ProfileError('/fields/'+slot['definition']['address']+'/read: bounded native GoForm field selector required')
        self.path=path;self.profile_id=document['profile']['profile_id'];self.revision=document['revision']
        self.content_hash=hashlib.sha256(raw).hexdigest()
        self._requests={r['reply']:deepcopy(r)for r in document['requests']}
        prepared={step['output']:step for step in self._plan['preprocessing']}
        self._dependencies={slot['definition']['address']:native_dependencies(
            frozenset(slot['requires'])|reply_dependencies(slot['read']['expression'],self._plan['metadata'],self._plan['functions']),
            prepared,self._plan['metadata'],self._plan['functions'])for slot in self._plan['fields']if slot['read']is not None}
        # Descriptions/revision/JSON spacing do not change interpreted values.
        interpretation={k:v for k,v in self._plan.items()if k!='metadata'}
        interpretation['metadata']={k:v for k,v in document['profile'].items()if k not in('notes','display_name','band_capability_reference','provenance')}
        interpretation['requests']=document.get('requests',[])
        interpretation['writes']=document.get('writes',{})
        interpretation['parameters']=document.get('parameters',{})
        interpretation['connection']=self.connection
        self.interpretation_hash=_digest(interpretation)
    @property
    def metadata(self):return deepcopy(self._document['profile'])
    @property
    def connection(self):
        default={'port':None} if self._document['profile']['family']=='routeros_at_like' else {'scheme':'auto','port':None}
        return deepcopy(self._document.get('connection',default))
    def http_address(self,address):
        """Apply admitted defaults; native owners retain I/O and TLS policy."""
        settings=self.connection
        if 'scheme' not in settings:raise ProfileError('/connection: selected transport is not HTTP')
        scheme=settings['scheme'];port=settings['port']
        if scheme=='auto' and port is None:return address
        explicit='://' in str(address)
        try:
            parsed=urlsplit(address if explicit else 'http://'+address)
            if (parsed.scheme not in {'http','https'} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.path not in {'','/'}
                    or parsed.query or parsed.fragment or '\\' in address or any(c.isspace() for c in address)):
                raise ValueError()
            supplied_port=parsed.port
        except (TypeError,ValueError):
            raise ProfileError('/connection: valid same-router device address required') from None
        if explicit and scheme!='auto' and parsed.scheme!=scheme:
            raise ProfileError('/connection/scheme: conflicts with explicit device address')
        if port is not None and supplied_port is not None and supplied_port!=port:
            raise ProfileError('/connection/port: conflicts with explicit device address')
        host=parsed.hostname
        if ':' in host:host='['+host+']'
        selected_port=port if port is not None else supplied_port
        if selected_port is not None:host+=':'+str(selected_port)
        if scheme=='auto' and not explicit:return host
        return (parsed.scheme if scheme=='auto' else scheme)+'://'+host
    @property
    def fields(self):return deepcopy(self._document['fields'])
    @property
    def access(self):return deepcopy(self._document['access'])
    @property
    def requests(self):return deepcopy(self._document['requests'])
    def has_request(self,identifier):return identifier in self._requests
    def has_read(self,address):return address in self._dependencies
    def native_source_keys(self,addresses,*,reply):
        """Source selectors for the native owner's existing packet planner."""
        return frozenset().union(*(native_field_keys(slot['read']['expression'],reply,self._plan['functions'])
            for slot in self._plan['fields']if slot['read']is not None and slot['definition']['address']in addresses))
    def request(self,identifier,*,protocol):
        request=self._requests.get(identifier)
        if request is None or request.get('protocol')!=protocol:
            raise ProfileError(self.profile_id+': /requests: selected native read is not defined')
        return deepcopy(request)
    @property
    def writes(self):return deepcopy(self._document.get('writes',{}))
    def snapshot(self):return deepcopy(self._document)
    def available_addresses(self,replies,*,context=None,addresses=None):
        """Only fields whose native reply groups belong to this read."""
        if context is None:
            return frozenset(address for address,required in self._dependencies.items()
                if (addresses is None or address in addresses)and required.issubset(replies))
        # The native owner may know which alternative reader actually ran.
        # Select pure context/profile branches; never invent a missing reply.
        prepared={step['output']:step for step in self._plan['preprocessing']}
        result=set()
        for slot in self._plan['fields']:
            address=slot['definition']['address']
            if slot['read']is None or addresses is not None and address not in addresses:continue
            required=native_dependencies(frozenset(slot['requires'])|reply_dependencies(
                slot['read']['expression'],self._plan['metadata'],self._plan['functions'],context=context),
                prepared,self._plan['metadata'],self._plan['functions'],context=context)
            if required.issubset(replies):result.add(address)
        return frozenset(result)
    def provenance(self):
        return{'schema':'PortableProfileProvenance.v1','profile_id':self.profile_id,'revision':self.revision,'content_hash':self.content_hash,'interpretation_hash':self.interpretation_hash,'rule_engine':'54GUC.BoundedParser.v1'}
    def state_value(self,name,replies,context=None):
        """Pure proposed state; the existing adapter owns admission/lifetime."""
        if name not in self._plan['state']:raise ProfileError('/state: adapter input is not defined')
        return Evaluator(self._plan['functions'],algorithms=self._plan['algorithms']).run(
            self._plan['state'][name],{'profile':self._plan['metadata'],'replies':replies,'context':context or{}})
    def _prepare_write_fields(self,name,payload):
        write=self._plan['writes'].get(name)
        if write is None:raise ProfileError('/writes: selected typed native stage is not defined')
        if set(payload)-set(write['fields']):raise ProfileError('/writes: stage parameter is not defined')
        evaluator=Evaluator(self._plan['functions'],algorithms=self._plan['algorithms'])
        inputs={'profile':self._plan['metadata'],'replies':{},'context':{'payload':payload}}
        output={}
        for parameter in payload:
            field=write['fields'][parameter]
            pointer='/writes/'+name+'/fields/'+parameter+'/value'
            try:value=evaluator.run(field['expression'],inputs)
            except (ValueError,TypeError,KeyError,IndexError,OverflowError,ZeroDivisionError):
                raise ProfileError(pointer+': native parameter conversion unavailable')from None
            if not isinstance(value,str)or len(value)>4096:raise ProfileError(pointer+': bounded native text required')
            output[field['key']]=value
        return write,output
    def prepare_dev2_write(self,name,payload,*,stack):
        """Transform an already authorized native stage. Never performs I/O."""
        write,output=self._prepare_write_fields(name,payload)
        if write.get('protocol','tplink_dev2')!='tplink_dev2':
            raise ProfileError('/writes: protocol does not match the DEV2 adapter')
        return write['operation'],write['object_id'],output,stack if write['stack']is None else write['stack']
    def prepare_form_write(self,name,payload):
        """Encode an admitted MiWiFi stage; transport retains its closed API."""
        write,output=self._prepare_write_fields(name,payload)
        if write.get('protocol')!='miwifi_json':
            raise ProfileError('/writes: protocol does not match the MiWiFi adapter')
        return write['path'],output
    def prepare_radio_mode_write(self,payload):
        """Only the selected stock MiWiFi configured-mode GET writer."""
        write,output=self._prepare_write_fields('set_mobile_net_cfg',payload)
        if write.get('protocol')!='miwifi_radio_mode':
            raise ProfileError('/writes: protocol does not match the MiWiFi mode adapter')
        return write['path'],output
    def prepare_native_write_fields(self,name,payload):
        """Pure parameter overlay at an already-authorized native write."""
        write=self._plan['writes'].get(name)
        if write is None:return dict(payload)
        if write.get('protocol')!='native_fields':raise ProfileError('/writes/'+name+': native parameter definition required')
        evaluator=Evaluator(self._plan['functions'],algorithms=self._plan['algorithms'])
        inputs={'profile':self._plan['metadata'],'replies':{},'context':{'payload':payload}}
        output={}
        for key,original in payload.items():
            field=write['fields'].get(key)
            target=field['key']if field else key
            pointer='/writes/'+name+'/fields/'+key+'/value'
            try:value=evaluator.run(field['expression'],inputs)if field else original
            except (ValueError,TypeError,KeyError,IndexError,OverflowError,ZeroDivisionError):
                raise ProfileError(pointer+': native parameter conversion unavailable')from None
            if field and not (type(value)in(str,int,bool,float) and (not isinstance(value,str)or len(value)<=4096)
                              and (type(value)is not float or math.isfinite(value))):
                raise ProfileError(pointer+': bounded native parameter required')
            if target in output or target!=key and target in payload:
                raise ProfileError('/writes/'+name+': native parameter collision')
            output[target]=value
        return output
    def decode(self,replies,context=None,*,addresses=None,issues=None,withheld_inputs=None):
        return decode(self._plan,replies,context,addresses=addresses,issues=issues,admit_outputs=True,withheld_inputs=withheld_inputs)


class ProfileRegistry:
    def __init__(self,directory,profiles,errors):
        self.directory=directory;self._files=profiles;self._profiles=WeakValueDictionary();self.errors=tuple(errors)
    @classmethod
    def load(cls,directory):
        directory=Path(directory);profiles={};errors=[];duplicates=set()
        paths=sorted(directory.glob('*.json'))if directory.is_dir()else[]
        if len(paths)>MAX_FILES:raise ProfileError('profile folder file limit exceeded')
        for path in paths:
            identifier=path.stem
            try:
                if path.is_symlink()or not path.is_file():raise ProfileError('profile must be a regular local file')
                if path.stat().st_size>MAX_FILE_BYTES:raise ProfileError('profile file size limit exceeded')
                # Exactly one byte snapshot: later edits do not affect this registry.
                with path.open('rb')as stream:raw=stream.read(MAX_FILE_BYTES+1)
                if len(raw)>MAX_FILE_BYTES:raise ProfileError('profile file size limit exceeded')
                document=_parse(raw)
                if isinstance(document,dict)and isinstance(document.get('profile'),dict):
                    identifier=document['profile'].get('profile_id',identifier)
                profile=PortableProfile(str(path),raw,document)
                if identifier in profiles or identifier in duplicates:
                    profiles.pop(identifier,None);duplicates.add(identifier)
                    raise ProfileError('duplicate profile ID; neither file is active')
                profiles[identifier]=_FileSnapshot(str(path),zlib.compress(raw),profile.metadata,document['selection_priority'])
            except (ValueError,KeyError,TypeError,RecursionError,OSError)as error:
                message=str(error)if isinstance(error,(ProfileError,DefinitionError))else'invalid definition structure'
                errors.append(FileIssue(path.name,str(identifier),message))
        return cls(directory,profiles,errors)
    def ids(self):
        # Matching precedence is part of the definition. File names/order must
        # never let a family baseline mask a more specific firmware profile.
        return tuple(sorted(self._files,key=lambda identifier:(self._files[identifier].priority,identifier)))
    def metadata(self,identifier):
        if identifier not in self._files:self.select(identifier)
        return deepcopy(self._files[identifier].metadata)
    def definition_bytes(self,identifier):
        if identifier not in self._files:self.select(identifier)
        return zlib.decompress(self._files[identifier].compressed)
    def select(self,identifier):
        retained=self._profiles.get(identifier)
        if retained is not None:return retained
        if identifier not in self._files:
            issue=next((x for x in self.errors if x.profile_id==identifier),None)
            raise ProfileError(str(issue)if issue else f'profile {identifier!s} not available in modem_profiles')from None
        entry=self._files[identifier];raw=zlib.decompress(entry.compressed)
        profile=PortableProfile(entry.path,raw,_parse(raw));self._profiles[identifier]=profile
        return profile


_STARTUP_REGISTRY=None
_STARTUP_LOCK=Lock()


def profile_directory():
    configured=os.environ.get('CGF_MODEM_PROFILE_DIRECTORY')
    if configured:return Path(configured).resolve()
    return(Path(sys.executable).resolve().parent if getattr(sys,'frozen',False)else Path(__file__).resolve().parents[3])/'modem_profiles'


def startup_registry():
    global _STARTUP_REGISTRY
    if _STARTUP_REGISTRY is None:
        with _STARTUP_LOCK:
            if _STARTUP_REGISTRY is None:_STARTUP_REGISTRY=ProfileRegistry.load(profile_directory())
    return _STARTUP_REGISTRY
