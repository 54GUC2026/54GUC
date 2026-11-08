"""Compare every declared field with the current driver's real decoding path."""
from copy import deepcopy
import json
import math

from tools.modem_profile_exchange import conformance_inputs as fixtures
from tools.modem_profile_exchange.expressions import Evaluator
from tools.modem_profile_exchange.rule_authoring import alg,inp


def equivalent(left,right):
    if type(left) is bool or type(right) is bool:return type(left) is type(right) and left==right
    if isinstance(left,(int,float)) and isinstance(right,(int,float)):
        return math.isfinite(left) and math.isfinite(right) and math.isclose(left,right,rel_tol=1e-12,abs_tol=1e-9)
    return json.loads(json.dumps(left,allow_nan=False))==json.loads(json.dumps(right,allow_nan=False))


def inspect_admitted_profile(profile, record):
    """Check the current shelf, not the retired 155-slot migration snapshot.

    Native fixtures still execute the real driver. Missing acquisition groups
    and declared producer gaps remain explicit; they are never invented reads.
    The immutable R24 oracle and actual HTTP/RPC tests are separate controls.
    """
    from tools.modem_profile_exchange.portable_profiles import family_for
    from arch4.modem_live.profile_definition.registry import PortableProfile
    from unittest.mock import patch
    from tools.modem_profile_exchange.export_profiles import field_address, EMPTY
    metadata=profile.metadata
    family=family_for(record)
    native_inputs={}
    decode=PortableProfile.decode
    def observe(definition,replies,context=None,**kwargs):
        values=decode(definition,replies,context,**kwargs)
        if definition.profile_id==profile.profile_id:
            # DEV2 also decodes secondary carriers later in the same read.
            # The fixture's canonical primary is the first native projection.
            for address in values:native_inputs.setdefault(address,(deepcopy(replies),deepcopy(context or{})))
        return values
    with patch.object(PortableProfile,'decode',observe):
        options={'complete_owner_reads':True}if family in {'xiaomi','zwrt'}else{}
        replies,context,expected=getattr(fixtures,family+'_case')(profile.profile_id,**options)
    required={field_address(name)for name,trust in record['field_trust'].items()if trust not in EMPTY}
    available=profile.available_addresses(replies,context=context)
    actual=profile.decode(replies,context,addresses=available)
    fields=[]
    for address,slot in profile.fields.items():
        if slot['read']is None:
            outcome=('explicit_driver_gap'if slot.get('availability')=='declared_without_native_producer'
                     else'not_declared_readable')
            fields.append(dict(address=address,outcome=outcome));continue
        try:
            current=actual
            if address in native_inputs:
                native_replies,native_context=native_inputs[address]
                current=profile.decode(native_replies,native_context,addresses={address})
            elif address not in available:
                if address not in required:
                    fields.append(dict(address=address,outcome='optional_definition_not_exercised'))
                    continue
                raise AssertionError('native_reply_group_not_exercised')
            if address.startswith('capability.'):
                if address in {'capability.supported_lte_bands','capability.supported_nr_bands'}and family=='dev2':
                    import re
                    prefix='B'if address.endswith('lte_bands')else'N'
                    value=list(dict.fromkeys(int(x)for x in re.findall(prefix+r'(\d+)',replies['DEV2_LTE_BAND_SEARCH']['supportBandList'])))
                elif address in {'capability.supported_lte_bands','capability.supported_nr_bands'}and family=='xiaomi':
                    value=replies['xqmobile/get_cellular_band_list']['list']['list_4G'if address.endswith('lte_bands')else'list_5G']
                else:value=(record['capabilities']['supports_traffic_volume']if address=='capability.traffic_volume'
                       else metadata[address.split('.')[1]]if address in {'capability.supported_lte_bands','capability.supported_nr_bands'}
                       else record['ue_capability'].get(address.split('.')[1]))
            elif address=='profile.traffic_presence':value=record['capabilities']['supports_traffic_presence']
            elif address=='identity.profile_definition':value=metadata['profile_id']
            else:
                if address=='identity.router_model'and address not in expected:
                    # Existing fixture publishes the same model under its
                    # historical canonical alias, rather than a second read.
                    expected[address]=expected['identity.model']
                if address not in expected:raise AssertionError('native_publication_plane_missing')
                value=expected[address]
            if address=='traffic.session_uptime_s'and family=='huawei':
                import xml.etree.ElementTree as ET
                # Native public ModemInput retains its historical numeric0
                # compatibility default. It is NOT a measured packet timer.
                if ET.fromstring(replies['/api/monitoring/traffic-statistics']).find('CurrentConnectTime')is None:
                    assert value==0.0
                    value=None
            if slot.get('normalization','').startswith('Explicit modulation label'):
                from arch4.lte_mcs import canonical_qam_label
                value=canonical_qam_label(value)
            if address=='slow.wan_ip'and value==''and slot.get('normalization','').startswith('Absent native IP'):
                value=None
            if not equivalent(current[address],value):raise AssertionError(repr(current[address])+' != '+repr(value))
            fields.append(dict(address=address,outcome='driver_match'))
        except Exception as error:
            fields.append(dict(address=address,outcome='failure',error=type(error).__name__+':'+str(error)))
    return dict(profile_id=profile.profile_id,family=family,fields=fields,
        failures=sum(v['outcome']=='failure'for v in fields),
        driver_matches=sum(v['outcome']=='driver_match'for v in fields),
        driver_gaps=sum(v['outcome']=='explicit_driver_gap'for v in fields))


def inspect_profile(data,record):
    replies,context,expected=getattr(fixtures,data['family']+'_case')(data['profile_id'])
    inputs={'profile':data['metadata'],'replies':deepcopy(replies),'context':context}
    evaluator=Evaluator(data['functions'],algorithms=data['algorithms'],max_steps=4000000)
    if data['family']=='zwrt':
        inputs['replies']['nwinfo']=evaluator.run(alg('zwrt_flatten',inp('replies'),inp('context')),inputs)
    fields=[]
    for slot in data['fields']:
        address=slot['definition']['address']
        if slot['read'] is None:
            fields.append({'address':address,'outcome':'not_declared_readable'})
            continue
        if slot['read']['availability']=='declared_without_native_producer':
            assert slot['read']['expression'] is None
            fields.append({'address':address,'outcome':'explicit_driver_gap'})
            continue
        try:
            if address.startswith('capability.'):
                key={'capability.traffic_volume':'supports_traffic_volume'}.get(address)
                value=record['capabilities'][key] if key else record['ue_capability'].get(address.split('.')[1])
            elif address=='profile.traffic_presence':value=record['capabilities']['supports_traffic_presence']
            else:
                if address not in expected:raise AssertionError('native_publication_plane_missing')
                value=expected[address]
            native=slot['read'].get('native_expression',slot['read']['expression'])
            result=evaluator.run(native,inputs)
            if not equivalent(result,value):raise AssertionError(repr(result)+' != '+repr(value))
            if 'native_expression' in slot['read']:
                from arch4.lte_mcs import canonical_qam_label
                actual=evaluator.run(slot['read']['expression'],inputs)
                if not equivalent(actual,canonical_qam_label(value)):raise AssertionError('canonical_modulation_mismatch')
            fields.append({'address':address,'outcome':'driver_match'})
        except Exception as error:
            fields.append({'address':address,'outcome':'failure','error':type(error).__name__+':'+str(error)})
    return {'profile_id':data['profile_id'],'family':data['family'],'fields':fields,
        'failures':sum(v['outcome']=='failure' for v in fields),
        'driver_matches':sum(v['outcome']=='driver_match' for v in fields),
        'driver_gaps':sum(v['outcome']=='explicit_driver_gap' for v in fields)}
