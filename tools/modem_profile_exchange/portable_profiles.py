"""Build and validate one independent JSON definition per current profile.

Candidate extraction is explicit. Once saved, JSON definitions are the owned
contract; a driver edit cannot update an existing file or its content revision.
"""
from __future__ import annotations
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path

from tools.modem_profile_exchange.export_profiles import (
    ROOT, InvalidExport, bind_fields, digest, driver_sources, encoded,
    field_address, load_fields, privacy_check, profile_records, PROFILE_KEYS,
)
from arch4.modem_live.profile_catalog import modem_profile_selection
from arch4.modem_live.access_profile_owner import resolve_modem_live_access_profile
from dataclasses import asdict
from tools.modem_profile_exchange.algorithm_export import library_for
from tools.modem_profile_exchange import huawei_rules, mikrotik_rules
from tools.modem_profile_exchange.rule_authoring import (
    common_rules, zte_rules, zte_fields, dev2_fields, xiaomi_fields,
    op,inp,choose,get,call,alg,either,both,text,
)
from tools.modem_profile_exchange.expressions import Evaluator
from tools.modem_profile_exchange.replay import decode
from tools.modem_profile_exchange.vocabulary import prune_algorithms,validate_vocabulary
from tools.modem_profile_exchange.acquisition import contract
from tools.modem_profile_exchange.shape import validate_shape
from tools.modem_profile_exchange.field_catalogue import catalogue_v2
import hashlib


SCHEMA='54GUC.PortableModemProfile.v1'
EMPTY={'unsupported','non_useful','unclassified'}
NR_MIKROTIK_GAPS={
    'radio.nr5g_active','radio.nr_arfcn','radio.nr_band','radio.nr_cell_id','radio.nr_mode',
    'radio.nr_pci','radio.nr_rsrp_dbm','radio.nr_rsrq_db','radio.nr_sinr_db',
    'profile.nr5g_band_set','profile.nr5g_rx0_rsrp_dbm','profile.nr5g_rx1_rsrp_dbm',
}
NR_MIKROTIK_NATIVE_PROFILES = frozenset({
    'mikrotik_routeros_v7_rg502q', 'mikrotik_routeros_v721_rg520f',
    'mikrotik_routeros_v721_rg650e', 'mikrotik_routeros_r17_v7211_regression',
})
NR_MIKROTIK_NATIVE_FIELDS = frozenset({
    'radio.nr5g_active', 'radio.nr_mode', 'radio.nr_band', 'radio.nr_pci',
    'radio.nr_arfcn', 'radio.nr_rsrp_dbm', 'radio.nr_rsrq_db', 'radio.nr_sinr_db',
})


def family_for(record):
    selection=modem_profile_selection(record['profile_id'])
    name=Path(driver_sources()[selection.backend]).name
    return {'huawei_b818.py':'huawei','mikrotik.py':'mikrotik','tplink_dev2.py':'dev2',
        'xiaomi_miwifi.py':'xiaomi','zte_goform.py':'goform','zte_zwrt.py':'zwrt'}[name]


@lru_cache(maxsize=6)
def family_definition(family):
    expressions={'huawei':huawei_rules.fields,'mikrotik':mikrotik_rules.fields,'dev2':dev2_fields,
        'xiaomi':xiaomi_fields,'goform':zte_fields,'zwrt':lambda:zte_fields(zwrt=True)}[family]()
    if family=='mikrotik':
        expressions['radio.serving_band_signature']=choose(expressions['radio.serving_band'],
            call('band_signature',bands={'op':'list','items':[expressions['radio.serving_band']]}),'')
        for address in NR_MIKROTIK_GAPS - NR_MIKROTIK_NATIVE_FIELDS:expressions[address]=None
    expressions['capability.traffic_volume']=inp('profile','capabilities','supports_traffic_volume')
    expressions['profile.traffic_presence']=inp('profile','capabilities','supports_traffic_presence')
    expressions['capability.ue_category']=inp('profile','ue_capability','ue_category')
    expressions['capability.ul_64qam']=inp('profile','ue_capability','ul_64qam')
    rules={**common_rules(),**(zte_rules() if family in {'goform','zwrt'} else {})}
    return {'expressions':expressions,'functions':rules,'algorithms':library_for(family)}


def build_profile(record):
    family=family_for(record)
    shared=deepcopy(family_definition(family))
    if family == 'mikrotik' and record['profile_id'] not in NR_MIKROTIK_NATIVE_PROFILES:
        # Other models retain their exact legacy LTE expression. A parser
        # addition cannot silently promote a 4G profile to a 5G declaration.
        shared['expressions']['radio.network_type'] = choose(
            get(alg('parse_routeros_lte_monitor_row', inp('replies', '/interface/lte/monitor')),
                'radio_connected'), 'LTE (4G)', '')
    fields=deepcopy(catalogue_v2())
    by_address={field_address(n):t for n,t in record['field_trust'].items()}
    definitions=[];gaps=[]
    for field in fields:
        field=deepcopy(field)
        trust=by_address.get(field['address'],'unclassified')
        read=None
        if trust not in EMPTY:
            address=field['address']
            if address not in shared['expressions']:raise InvalidExport('native_rule_missing:'+record['profile_id']+':'+address)
            known_gap=(family=='mikrotik' and address in NR_MIKROTIK_GAPS
                and not (record['profile_id'] in NR_MIKROTIK_NATIVE_PROFILES
                         and address in NR_MIKROTIK_NATIVE_FIELDS)) or (
                family=='huawei' and address in {'radio.nr_band','radio.nr_pci'})
            if known_gap:gaps.append(address)
            read={'expression':None if known_gap else shared['expressions'][address],
                'availability':'declared_without_native_producer' if known_gap else 'defined',
                'gap_reason':'Current driver has no native producer; do not infer from LTE or channel frequency.' if known_gap else None,
                'result_plane':'decoded_native_or_qualified_metadata_before_Matrix_admission'}
            if address in {'radio.selected_band_set','radio.selected_band_set_signature'}:
                read['selection_scope']=('provider_limited_effective_selection' if family in {'dev2','huawei'}
                    else 'profile_provider_pool' if record['provider_band_pool'] else 'native_selection')
            if address in {'rf.qam_dl0','rf.qam_dl1','rf.qam_ul','radio.nr_qam_dl','radio.nr_qam_ul'}:
                read['native_expression']=read['expression']
                read['expression']=alg('canonical_qam_label',read['expression'])
                read['normalization']='Explicit modulation label; unknown/native zero becomes empty, not QPSK.'
        definitions.append({'definition':field,'trust':trust,'read':read})
    selection=modem_profile_selection(record['profile_id'])
    profile={key:record[key] for key in PROFILE_KEYS}
    profile['backend']=selection.backend
    profile['native_period_fields']='traffic_session_duration_s' in record['field_trust'] and record['field_trust']['traffic_session_duration_s'] not in EMPTY
    data={'schema':SCHEMA,'definition_version':2,'profile_id':record['profile_id'],'family':family,
        'metadata':profile,'access':asdict(resolve_modem_live_access_profile(backend=selection.backend,
            backend_family=selection.family,profile_id=selection.profile_id,model=selection.model_hint,
            firmware=selection.software_hint,vendor=selection.manufacturer)),
        'fields':definitions,'functions':shared['functions'],'algorithms':shared['algorithms'],
        'standalone':True,'external_profile_dependencies':[],
        'runtime_integration':False,'declared_driver_gaps':gaps,
        'conformance':{'status':'pending','readable_fields':sum(s['read'] is not None for s in definitions)},
        'privacy':{'scope':'owned_static_definitions_only','observations_included':False,'credentials_included':False}}
    data['preprocessing']=[{'output':'nwinfo','expression':alg('zwrt_flatten',inp('replies'),inp('context'))}] if family=='zwrt' else []
    data['acquisition']=contract(family,data['fields'])
    source=driver_sources()[selection.backend]
    data['source_baseline']={'driver_file':source,'driver_sha256':hashlib.sha256((ROOT/source).read_bytes()).hexdigest(),
        'meaning':'conformance baseline provenance only; replay never opens or imports this driver'}
    data['algorithms']=prune_algorithms(data['algorithms'],[data['fields'],data['preprocessing']])
    data['content_sha256']=digest(data)
    validate_profile(data)
    return json.loads(encoded(data))


def validate_profile(data):
    schema=json.loads(Path(__file__).with_name('portable.schema.json').read_text(encoding='utf-8'))
    validate_shape(data,schema)
    if data.get('schema')!=SCHEMA or data.get('definition_version')!=2:raise InvalidExport('portable_schema_version')
    if data.get('standalone') is not True or data.get('external_profile_dependencies')!=[]:raise InvalidExport('profile_must_be_standalone')
    if data.get('runtime_integration') is not False:raise InvalidExport('runtime_activation_not_implemented')
    expected={f['id']:f for f in catalogue_v2()}
    actual={s['definition']['id']:s['definition'] for s in data['fields']}
    if expected!=actual or len(actual)!=len(data['fields']):raise InvalidExport('complete_field_matrix_required')
    for slot in data['fields']:
        if (slot['trust'] in EMPTY)!=(slot['read'] is None):raise InvalidExport('unsupported_null_contract')
        if slot['read'] and (slot['read']['availability']=='declared_without_native_producer')!=(slot['read']['expression'] is None):raise InvalidExport('driver_gap_must_not_fabricate_a_read')
    if set(data['metadata']) != set(PROFILE_KEYS)|{'native_period_fields'}:raise InvalidExport('unknown_profile_metadata')
    document=deepcopy(data);checksum=document.pop('content_sha256',None)
    if checksum!=digest(document):raise InvalidExport('portable_integrity_mismatch')
    privacy_check(data)
    validate_vocabulary(data)
