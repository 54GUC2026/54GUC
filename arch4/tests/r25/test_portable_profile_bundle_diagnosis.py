"""Human field diagnosis replays bound, sanitized native reads, never a router."""
from copy import deepcopy
from pathlib import Path
import gzip,json
import pytest
import requests
from arch4.modem_live.profile_definition.registry import ProfileRegistry,PortableProfile
from arch4.modem_live.profile_diagnostics import decode_native_profile
from arch4.modem_live.startup_trace import StartupModemTrace,traced_http,traced_protocol

ROOT=Path(__file__).resolve().parents[3]
IDS=['mikrotik_chateau_lte12_eg120k_routeros_v7','tplink_nx520_srba1_250902_dev2',
     'huawei_b818_263_h220sp1c11','xiaomi_cb0401v2_3_0_88','zte_mc888_goform','zte_g5tc_zwrt_model']


@pytest.fixture(scope='module')
def definitions():return ProfileRegistry.load(ROOT/'modem_profiles')


def captured(profile):
    with gzip.open(Path(__file__).with_name('fixtures')/'r250_r24_native_baseline.jsonl.gz','rt')as stream:
        case=next(json.loads(line)for line in stream if json.loads(line)['profile_id']==profile.profile_id)
    if any(r['reply']=='status_batch'for r in profile.requests):
        groups=[r for r in profile.requests if r['reply']in{'status_batch','metadata_batch'}]
    else:
        groups=[next(r for r in profile.requests if r['reply']in case['replies']
                     and 'rf.sinr_db'in profile.available_addresses({r['reply']:case['replies'][r['reply']]}))]
    rows=[];replies={};trace=StartupModemTrace()
    with trace.scope(lambda row:rows.append(row)or True,'startup','acquire'):
        for group in groups:
            raw=case['replies'][group['reply']];replies[group['reply']]=raw
            if group['protocol']=='routeros_api':
                traced_protocol(lambda *a,**k:[raw],operation=group['path']+':call',args=(group['operation'],{}))
            elif group['protocol']=='tplink_dev2':
                traced_protocol(lambda *a,**k:raw,protocol='tplink_dev2_decoded',
                    operation=group['operation']+':'+group['object_id'])
            elif group['protocol']=='zte_goform':
                from arch4.modem.zte_goform import ZteGoformModem
                modem=ZteGoformModem(backend_name=profile.metadata['backend']or'mc888',profile_id=profile.profile_id)
                for chunk in modem._goform_request_chunks(tuple(raw)):
                    response=requests.Response();response.status_code=200
                    response.headers={'Content-Type':'application/json'}
                    response._content=json.dumps({key:raw[key]for key in chunk if key in raw}).encode()
                    def get(*args,**kwargs):return response
                    traced_http(get,'http://192.0.2.1'+group['path'],
                        params={group['field_parameter']:','.join(chunk)})
            else:
                kwargs={};body=raw
                if group['protocol']=='zwrt_ubus':
                    kwargs['json']=[{'method':'call','id':i,'params':['PRIVATE_SESSION',c['service'],c['method'],c.get('params',{})]}
                                    for i,c in enumerate(group['calls'])]
                    body=[{'id':i,'result':[0,item['data']]}for i,item in enumerate(raw)]
                response=requests.Response();response.status_code=200
                response.headers={'Content-Type':'application/xml'if group['protocol']=='hilink_xml'else'application/json'}
                response._content=(body if isinstance(body,str)else json.dumps(body)).encode()
                traced_http(lambda *a,**k:response,'http://192.0.2.1'+group['path'].replace(';stok={session}/',''),**kwargs)
        decode_native_profile(profile,replies,case['context'],addresses={'rf.sinr_db'},issues=[])

    return rows,case['expected']['rf.sinr_db']


@pytest.mark.parametrize('identifier',IDS)
def test_every_native_protocol_can_explain_a_bound_rf_conversion(definitions,identifier):
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile=definitions.select(identifier);rows,expected=captured(profile)
    untouched=deepcopy(rows)
    review=inspect_wire_fields(profile,rows)
    field=next(row for row in review['fields']if row['field']=='rf.sinr_db')
    assert field['status']=='Replay matches',field
    assert field['value']==pytest.approx(expected)
    assert field['unit']=='dB'
    assert 'PRIVATE_SESSION'not in json.dumps(review)
    assert rows==untouched


def test_candidate_edit_changes_only_offline_result(definitions):
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile=definitions.select(IDS[0]);rows,expected=captured(profile)
    document=profile.snapshot();document['revision']='candidate'
    document['fields']['rf.sinr_db']['read']='('+document['fields']['rf.sinr_db']['read']+')*2'
    candidate=PortableProfile('candidate.json',json.dumps(document).encode(),document)
    review=inspect_wire_fields(profile,rows,candidate=candidate)
    field=next(row for row in review['fields']if row['field']=='rf.sinr_db')
    assert field['status']=='Candidate changes value'
    assert field['value']==expected and field['candidate_value']==expected*2


@pytest.mark.parametrize('damage',['profile_hash','input_hash','new_operation','no_binding','missing_context'])
def test_missing_or_mismatched_proof_is_an_evidence_limit(definitions,damage):
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile=definitions.select(IDS[0]);rows,_=captured(profile)
    conversion=next(row for row in rows if row['kind']=='profile_decode')
    if damage=='profile_hash':conversion['profile']['content_hash']='0'*64
    elif damage=='input_hash':conversion['reply_hashes']={k:'0'*64 for k in conversion['reply_hashes']}
    elif damage=='new_operation':conversion['operation_id']='different-operation'
    elif damage=='no_binding':rows=[row for row in rows if row['kind']!='profile_decode']
    else:conversion['context']={'omitted':'body_size_limit'}
    review=inspect_wire_fields(profile,rows)
    field=next(row for row in review['fields']if row['field']=='rf.sinr_db')
    assert field['status']in{'No saved conversion','Native reply not retained','Context not retained'}
    assert field.get('value')is None
    assert not review['mismatches']


def test_failed_native_request_is_not_a_conversion_error(definitions):
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile=definitions.select(IDS[0]);rows=[]
    def fail(*a,**k):raise TimeoutError('PRIVATE_TRANSPORT_DETAIL')
    with StartupModemTrace().scope(lambda row:rows.append(row)or True,'startup','acquire'):
        with pytest.raises(TimeoutError):traced_protocol(fail,operation='/interface/lte:call',args=('monitor',{}))
    review=inspect_wire_fields(profile,rows)
    field=next(row for row in review['fields']if row['field']=='rf.sinr_db')
    assert field['status']=='Native read failed'
    assert 'PRIVATE_TRANSPORT_DETAIL'not in json.dumps(review)


def test_fields_from_one_read_are_replayed_together(definitions):
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile=definitions.select(IDS[0]);rows,_=captured(profile)
    conversion=next(row for row in rows if row['kind']=='profile_decode')
    conversion['values']['rf']['rsrp_dbm']=-86.
    class Counted:
        calls=0
        def __getattr__(self,name):return getattr(profile,name)
        def decode(self,*args,**kwargs):
            self.calls+=1
            return profile.decode(*args,**kwargs)
    counted=Counted();inspect_wire_fields(counted,rows)
    assert counted.calls==1,'Do not rebuild native preprocessing once per field.'


def test_http_refusal_is_a_native_read_failure(definitions):
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile=definitions.select(IDS[2]);rows,_=captured(profile)
    rows=[row for row in rows if row['kind']!='profile_decode']
    next(row for row in rows if row['kind']=='response')['replies'][-1]['status']=403
    field=next(row for row in inspect_wire_fields(profile,rows)['fields']if row['field']=='rf.sinr_db')
    assert field['status']=='Native read failed'


def test_changed_native_request_is_not_proved_by_the_old_reply(definitions):
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile=definitions.select(IDS[0]);rows,_=captured(profile)
    document=profile.snapshot();document['revision']='changed-request'
    next(r for r in document['requests']if r['reply']=='/interface/lte/monitor')['path']='/interface/other'
    candidate=PortableProfile('candidate.json',json.dumps(document).encode(),document)
    field=next(row for row in inspect_wire_fields(profile,rows,candidate=candidate)['fields']if row['field']=='rf.sinr_db')
    assert field['status']=='Replay matches'
    assert field['candidate_status']=='Native request not exercised'
    assert 'candidate_value'not in field


def test_malformed_native_record_is_not_silently_replayed(definitions):
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile=definitions.select(IDS[0]);rows,_=captured(profile)
    next(row for row in rows if row['kind']=='request')['request']=None
    with pytest.raises(ValueError,match='wire_record'):inspect_wire_fields(profile,rows)


@pytest.mark.parametrize('second,expected',[
    ({'error':{'code':-32002,'message':'Access denied'}},{'success':False,'data':None}),
    ({'error':{'code':-32601,'message':'Method not found'}},{'success':False,'data':None}),
    ({'result':[6,{}]},{'success':False,'data':{}}),
    ({'result':[0,None]},{'success':True,'data':{}}),
    ({'result':[True,{}]},None),({'error':'untyped refusal'},None)])
def test_optional_ubus_failure_does_not_discard_successful_peer(second,expected):
    from tools.log_checker.profile_fields import _native_candidates
    rule={'protocol':'zwrt_ubus','path':'/ubus/','reply':'batch','calls':[
        {'service':'zte_nwinfo_api','method':'nwinfo_get_netinfo'},
        {'service':'network.interface.zte_wan','method':'status'}]}
    request={'request':{'protocol':'http','path':'/ubus/','body':[
        {'id':0,'params':['masked','zte_nwinfo_api','nwinfo_get_netinfo',{}]},
        {'id':1,'params':['masked','network.interface.zte_wan','status',{}]}]}}
    response={'replies':[{'status':200,'body':[
        {'id':0,'result':[0,{'lte_snr':'12.5'}]},{'id':1,**second}]}]}
    candidates=_native_candidates(rule,request,response)
    if expected is None:assert candidates==[]
    else:assert candidates==[[{'success':True,'data':{'lte_snr':'12.5'}},expected]]



def captured_dev2_metadata(profile, *, firmware='1.2.3.4(TEST)',
                           hardware='TEST_HARDWARE', uptime='120'):
    """Real trace projection, explicit synthetic values and no transport."""
    rows = []
    native = {'softwareVersion': firmware, 'hardwareVersion': hardware,
              'upTime': uptime, 'serialNumber': 'SYNTHETIC_PRIVATE_UNIT'}
    addresses = {'identity.firmware', 'identity.software_version',
                 'identity.hardware_version', 'slow.router_system_uptime_s'}
    with StartupModemTrace().scope(lambda row: rows.append(row) or True,
                                  'startup', 'metadata'):
        traced_protocol(lambda: native, protocol='tplink_dev2_decoded',
                        operation='go:DEV2_DEV_INFO')
        decode_native_profile(profile, {'DEV2_DEV_INFO': native}, {},
                              addresses=addresses, issues=[])
    return rows


@pytest.mark.parametrize('address', ('identity.firmware', 'identity.software_version'))
def test_partial_mask_in_selected_dev2_firmware_is_private_input(definitions, address):
    from arch4.privacy_scrub import SCRUB_MASK
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile = definitions.select('tplink_nx520v_eu1_260408')
    rows = captured_dev2_metadata(profile)
    untouched = deepcopy(rows)
    saved = next(row for row in rows if row['kind'] == 'response')['result']['softwareVersion']
    assert SCRUB_MASK in saved and saved != SCRUB_MASK
    report = inspect_wire_fields(profile, rows)
    field = next(row for row in report['fields'] if row['field'] == address)
    assert field['status'] == 'Private input withheld'
    assert address not in report['mismatches']
    assert 'value' not in field and 'candidate_value' not in field
    assert rows == untouched


@pytest.mark.parametrize('address,expected', (
    ('identity.hardware_version', 'TEST_HARDWARE'),
    ('slow.router_system_uptime_s', 120),
))
def test_partial_firmware_mask_does_not_taint_other_dev2_fields(definitions, address, expected):
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile = definitions.select('tplink_nx520v_eu1_260408')
    rows = captured_dev2_metadata(profile)
    field = next(row for row in inspect_wire_fields(profile, rows)['fields']
                 if row['field'] == address)
    assert field['status'] == 'Replay matches'
    assert field['value'] == expected


@pytest.mark.parametrize('address,changed', (
    ('identity.hardware_version', 'ALTERED_CONVERSION'),
    ('slow.router_system_uptime_s', 121),
))
def test_partial_firmware_mask_does_not_hide_real_dev2_conflicts(definitions, address, changed):
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile = definitions.select('tplink_nx520v_eu1_260408')
    rows = captured_dev2_metadata(profile)
    conversion = next(row for row in rows if row['kind'] == 'profile_decode')
    line, key = address.split('.', 1)
    conversion['values'][line][key] = changed
    report = inspect_wire_fields(profile, rows)
    field = next(row for row in report['fields'] if row['field'] == address)
    assert field['status'] == 'Replay differs'
    assert address in report['mismatches']


@pytest.mark.parametrize('address', ('identity.firmware', 'identity.software_version'))
def test_unmasked_dev2_firmware_conflict_still_reports_difference(definitions, address):
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile = definitions.select('tplink_nx520v_eu1_260408')
    rows = captured_dev2_metadata(profile, firmware='TEST_FIRMWARE')
    conversion = next(row for row in rows if row['kind'] == 'profile_decode')
    _, key = address.split('.', 1)
    conversion['values']['identity'][key] = 'ALTERED_CONVERSION'
    report = inspect_wire_fields(profile, rows)
    field = next(row for row in report['fields'] if row['field'] == address)
    assert field['status'] == 'Replay differs'
    assert address in report['mismatches']


def test_fully_masked_dev2_firmware_keeps_existing_private_value_status(definitions):
    from arch4.privacy_scrub import SCRUB_MASK
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile = definitions.select('tplink_nx520v_eu1_260408')
    rows = captured_dev2_metadata(profile, firmware=SCRUB_MASK)
    report = inspect_wire_fields(profile, rows)
    for address in ('identity.firmware', 'identity.software_version'):
        field = next(row for row in report['fields'] if row['field'] == address)
        assert field['status'] == 'Private value withheld'
        assert address not in report['mismatches']


def test_partial_firmware_sibling_does_not_relabel_invalid_uptime(definitions):
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile = definitions.select('tplink_nx520v_eu1_260408')
    rows = captured_dev2_metadata(profile, uptime='not-a-number')
    address = 'slow.router_system_uptime_s'
    report = inspect_wire_fields(profile, rows)
    field = next(row for row in report['fields'] if row['field'] == address)
    assert field['status'] == 'Unavailable'
    assert address not in report['mismatches']


@pytest.mark.parametrize('firmware,expected', (
    ('TEST_FIRMWARE', 'Candidate changes value'),
    ('1.2.3.4(TEST)', 'Private input withheld'),
))
def test_candidate_comparison_keeps_selected_privacy_boundary(definitions, firmware, expected):
    from tools.log_checker.profile_fields import inspect_wire_fields
    profile = definitions.select('tplink_nx520v_eu1_260408')
    rows = captured_dev2_metadata(profile, firmware=firmware)
    document = profile.snapshot()
    document['revision'] = 'partial-mask-candidate'
    document['fields']['identity.firmware']['read'] = "'CANDIDATE_FIRMWARE'"
    candidate = PortableProfile('candidate.json', json.dumps(document).encode(), document)
    report = inspect_wire_fields(profile, rows, candidate=candidate)
    field = next(row for row in report['fields'] if row['field'] == 'identity.firmware')
    assert field['status'] == expected
    if expected == 'Private input withheld':
        assert 'candidate_value' not in field
        assert 'identity.firmware' not in report['mismatches']
    else:
        assert field['value'] == 'TEST_FIRMWARE'
        assert field['candidate_value'] == 'CANDIDATE_FIRMWARE'
