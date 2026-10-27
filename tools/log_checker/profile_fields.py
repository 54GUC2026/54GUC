"""Saved-reply diagnosis using the same profile engine as Modem Live.

No transport, driver, commands, History mutation or guessed missing replies.
Decoded means a software conversion ran; it does not prove native semantics.
"""
from arch4.privacy_scrub import scrub_record


def _xml_text(node,depth=0):
    from xml.etree import ElementTree as ET
    if depth>32 or not isinstance(node,dict)or not isinstance(node.get('tag'),str):
        raise ValueError('saved_xml_shape')
    element=ET.Element(node['tag'],node.get('attributes',{}));element.text=node.get('value','')
    children=node.get('children',[])
    if not isinstance(children,list)or len(children)>4096:raise ValueError('saved_xml_shape')
    for child in children:element.append(ET.fromstring(_xml_text(child,depth+1)))
    return ET.tostring(element,encoding='unicode')


def _native_candidates(rule,request,response):
    """Only exact declared native destinations; input hash chooses a view."""
    import re
    protocol=rule['protocol'];wire=request.get('request',{})
    payload=None
    if protocol in{'routeros_api','routeros_at'}:
        if wire.get('protocol')!='routeros':return None
        operation=wire.get('operation','');args=wire.get('args',[])
        native=wire.get('arguments',{})
        if operation!=rule['path']+':call' or not args or args[0]!=rule['operation']:return None
        if protocol=='routeros_at':
            native=args[1]if len(args)>1 else native
            if native.get('input')!=rule['input']:return None
        if 'error_type'in response:return []
        payload=response.get('result')
    elif protocol=='tplink_dev2':
        if wire.get('protocol')!='tplink_dev2_decoded'or wire.get('operation')!=rule['operation']+':'+rule['object_id']:return None
        return []if 'error_type'in response else[response.get('result')]
    else:
        path=lambda value:re.sub(r'/;stok=[^/]+','',value)
        if wire.get('protocol')!='http'or path(wire.get('path',''))!=path(rule.get('path','')):return None
        if protocol=='zte_goform'and (str(wire.get('method','')).upper()!='GET'
                or rule.get('field_parameter')not in (wire.get('query_arguments')or{})):return None
        if protocol=='zwrt_ubus':
            sent=wire.get('body');sent=sent if isinstance(sent,list)else[sent]
            calls=rule.get('calls',[{'service':rule.get('service'),'method':rule.get('operation')}])
            if len(sent)!=len(calls)or any(not isinstance(item,dict)or item.get('params',[])[1:3]!=[call['service'],call['method']]
                                         for item,call in zip(sent,calls)):return None
        if 'error_type'in response:return []
        received=response.get('replies',[])
        if not received or not 200<=received[-1].get('status',0)<300:return []
        payload=received[-1].get('body')
        if protocol=='hilink_xml':return[_xml_text(payload)]
        if protocol=='zwrt_ubus':
            received=payload if isinstance(payload,list)else[payload]
            by_id={item['id']:item for item in received if isinstance(item,dict)and'id'in item}
            if len(by_id)!=len(received)or any(item.get('id')not in by_id for item in sent):return []
            results=[]
            for item in sent:
                reply=by_id[item['id']]
                if 'error'in reply:
                    error=reply['error']
                    if not isinstance(error,dict)or type(error.get('code'))is not int:return []
                    # Native UBUS keeps an optional denied service as one
                    # failed envelope; it does not discard successful peers.
                    results.append({'success':False,'data':None})
                    continue
                result=reply.get('result',[])
                if not isinstance(result,list)or len(result)!=2 or type(result[0])is not int:return []
                data=result[1]
                results.append({'success':result[0]==0 and (data is None or isinstance(data,dict)),
                                'data':data if isinstance(data,dict)else{}})
            return[results]if 'calls'in rule else[results[0]['data']]
        return[payload]
    # Native APIs legitimately return either a row or a list. Never pick the
    # first of several rows: use an exact singleton or the declared interface.
    candidates=[payload]
    if isinstance(payload,list):
        if len(payload)==1:
            candidates.append(payload[0])
            if protocol=='routeros_at'and isinstance(payload[0],dict):candidates.append(payload[0].get('output'))
        elif rule.get('selector')=='unique named lte1 row':
            selected=[row for row in payload if isinstance(row,dict)and row.get('name')=='lte1']
            if len(selected)==1:candidates.append(selected[0])
    return candidates


def _goform_fragment_reply(rule,responses,expected_hash):
    """Reproduce the native bounded packet merge, only within this conversion.

    Exact input hash is mandatory. Never combine different operations, future
    responses, arbitrary subsets or an unbounded session's native fields.
    """
    from arch4.modem_live.startup_trace import native_reply_hash
    merged={};references=[]
    for request,response in reversed(responses):
        candidates=_native_candidates(rule,request,response)
        if not candidates or not isinstance(candidates[0],dict):continue
        merged={**candidates[0],**merged}  # Native order: newest value wins.
        references.append(request['request_id'])
        if len(merged)>512 or len(references)>512:return None
        if native_reply_hash(merged)==expected_hash:
            return merged,tuple(reversed(references))
    return None


def inspect_wire_fields(profile,rows,*,candidate=None):
    """Replay only bound conversion inputs; never infer a bug from missing proof."""
    import math
    from arch4.modem_live.startup_trace import native_reply_hash
    from arch4.privacy_scrub import SCRUB_MASK
    if len(rows)>8192:raise ValueError('profile_wire_record_limit')
    if candidate is not None and candidate.profile_id!=profile.profile_id:
        raise ValueError('Compare two versions of the same profile.')
    rules=profile.requests;operations={};conversions={};failed=set();replayed={}
    candidate_requests={rule['reply']:rule for rule in candidate.requests}if candidate is not None else{}
    for row in rows:
        if not isinstance(row,dict):continue
        op=(row.get('trace_id'),row.get('operation_id'))
        if not all(isinstance(part,str)and part for part in op):continue
        state=operations.setdefault(op,{'requests':{},'responses':[]})
        if row.get('kind')=='request':
            if type(row.get('request_id'))is not int or not isinstance(row.get('request'),dict):
                raise ValueError('profile_wire_record_shape')
            state['requests'][row['request_id']]=row
        elif row.get('kind')=='response':
            request=state['requests'].get(row.get('request_id'))
            if request is None:continue
            state['responses'].append((request,row))
            for rule in rules:
                try:candidates=_native_candidates(rule,request,row)
                except (ValueError,TypeError,KeyError,IndexError,AttributeError):
                    raise ValueError('profile_wire_record_shape')from None
                if candidates==[]:failed.add(rule['reply'])
        elif row.get('kind')=='profile_decode'and row.get('profile')==profile.provenance():
            values=row.get('values',{})
            if not isinstance(values,dict):continue
            for line,fields in values.items():
                if not isinstance(fields,dict):continue
                for field in fields:
                    # Snapshot references now; a later read in this operation
                    # must not be replayed as an earlier conversion's input.
                    conversions[line+'.'+field]=(row,tuple(state['responses']))
    selected={}
    for address,(event,_)in conversions.items():selected.setdefault(id(event),set()).add(address)
    # Literal/profile/context rules need no native request. Their availability
    # with an empty input cannot establish a failed router read.
    failed_addresses=(profile.available_addresses({group:None for group in failed})
                      - profile.available_addresses({})) if failed else set()
    result=[];mismatches=[]
    for address,field in sorted(profile.fields.items(),key=lambda pair:pair[1]['id']):
        line,key=address.split('.',1)
        output={'id':field['id'],'field':address,'unit':field['unit'],
                'rule_pointer':'/fields/'+address+'/read','rule':field['read'],
                'status':('Unsupported'if field['trust']in{'unsupported','non_useful'}else'No native rule')
                         if field['read']is None else'No saved conversion'}
        if field['read']is not None and address not in conversions:
            if address in failed_addresses:output['status']='Native read failed'
        if address in conversions:
            event,responses=conversions[address]
            identity=id(event)
            if identity not in replayed:
                context=event.get('context');native={};references={}
                hashes=event.get('reply_hashes',{})
                for request,response in reversed(responses):
                    for rule in rules:
                        group=rule['reply']
                        if group in native or group not in hashes:continue
                        try:
                            candidates=_native_candidates(rule,request,response)
                            matches=[value for value in candidates or[]if native_reply_hash(value)==hashes[group]]
                        except (ValueError,TypeError,KeyError,IndexError,AttributeError):matches=[]
                        if matches:
                            native[group]=matches[0];references[group]=(request['request_id'],)
                for rule in rules:
                    group=rule['reply']
                    if rule['protocol']!='zte_goform'or group in native or group not in hashes:continue
                    match=_goform_fragment_reply(rule,responses,hashes[group])
                    if match is not None:native[group],references[group]=match
                values={};errors=[];other={};candidate_available=set();eligible=set();withheld=set()
                if isinstance(context,dict)and not context.get('omitted'):
                    eligible=profile.available_addresses(native,context=context,addresses=selected[identity])
                    if eligible:
                        values=profile.decode(native,context,addresses=eligible,issues=errors,withheld_inputs=withheld)
                        if candidate is not None:
                            unchanged={rule['reply']:native[rule['reply']]for rule in rules
                                       if rule['reply']in native and candidate_requests.get(rule['reply'])==rule}
                            candidate_available=candidate.available_addresses(unchanged,context=context,addresses=eligible)
                            other=candidate.decode(native,context,addresses=eligible&candidate_available,issues=[])
                replayed[identity]=(native,references,context,values,{issue.address for issue in errors},other,candidate_available,eligible,withheld)
            native,references,context,decoded,errors,other,candidate_available,eligible,withheld=replayed[identity]
            if not isinstance(context,dict)or context.get('omitted'):
                output['status']='Context not retained'
            elif address not in eligible:
                output['status']='Native reply not retained'
            else:
                recorded=event['values'][line][key];value=decoded.get(address)
                public=scrub_record({line:{key:value}})[line][key]
                output['request_ids']=sorted({identifier for ids in references.values()for identifier in ids})
                if recorded==SCRUB_MASK or public==SCRUB_MASK:
                    output['status']='Private value withheld'
                elif address in withheld or isinstance(value,str)and SCRUB_MASK in value:
                    output['status']='Private input withheld'
                elif address in errors or any(issue.get('address')==address or (
                        issue.get('address')==SCRUB_MASK and
                        issue.get('pointer')=='/fields/'+address+'/read')
                        for issue in event.get('issues',[])):
                    output['status']='Conversion failed'
                else:
                    output['value']=public
                    equal=(type(value)is type(recorded)and value==recorded)
                    if type(value)in(int,float)and type(recorded)in(int,float):
                        equal=math.isclose(value,recorded,rel_tol=1e-12,abs_tol=1e-9)
                    output['status']='Replay matches'if equal else'Replay differs'
                    if not equal:mismatches.append(address)
                    elif value is None:output['status']='Unavailable'
                    elif candidate is not None:
                        if address not in candidate_available:output['candidate_status']='Native request not exercised'
                        else:
                            new_value=other.get(address)
                            output['candidate_value']=scrub_record({line:{key:new_value}})[line][key]
                            if type(new_value)is not type(value)or new_value!=value:output['status']='Candidate changes value'
        result.append(output)
    return scrub_record({'schema':'PortableProfileNativeReview.v1','profile':profile.provenance(),
        'fields':result,'mismatches':mismatches,'limits':[
            'Only retained native reads with the exact profile, operation, input view and conversion context were replayed.',
            'Matching replay proves reproducible conversion, not independent real-world field meaning or correct firmware behavior.',
            'Missing or private input is an evidence limit. No router was contacted, command sent, or History changed.']})


def inspect_bundle_fields(manifest,files,*,candidate=None):
    """Use only the exact admitted definition saved in this archive."""
    import json
    from arch4.modem_live.profile_definition.registry import PortableProfile
    from arch4.modem_live.startup_trace import TRACE_MAX_BYTES,TRACE_SCHEMA,TRACE_OWNER
    unavailable={'schema':'PortableProfileNativeReview.v1','status':'Not retained','fields':[],
                 'mismatches':[],'limits':['This bundle has no qualified editable-profile and conversion evidence.']}
    metadata=manifest.get('portable_profile',{})
    if not isinstance(metadata,dict):return unavailable
    if metadata.get('included')is not True:return unavailable
    if candidate is not None and candidate.profile_id!=metadata.get('profile_id'):
        raise ValueError('Choose a candidate for the same router profile saved in the bundle.')
    try:
        name=metadata['member'];raw=files[name]
        if len(raw)>2*1024*1024:raise ValueError('profile_bytes_limit')
        profile=PortableProfile(name,raw,json.loads(raw))
        if any(metadata.get(key)!=value for key,value in profile.provenance().items()):
            raise ValueError('profile_provenance_mismatch')
        wire=files.get('portable_runtime/guc_output/modem_startup_trace.jsonl',b'')
        if len(wire)>TRACE_MAX_BYTES+4096:raise ValueError('trace_bytes_limit')
        rows=[json.loads(line)for line in wire.splitlines()if line.strip()]
        if any(not isinstance(row,dict)or row.get('schema')!=TRACE_SCHEMA or row.get('owner')!=TRACE_OWNER for row in rows):
            raise ValueError('trace_record_contract')
        return inspect_wire_fields(profile,rows,candidate=candidate)
    except (ValueError,TypeError,KeyError,UnicodeError,RecursionError):
        return {**unavailable,'status':'Evidence incomplete',
                'limits':['The saved profile or native trace could not be validated. No current profile was substituted.']}


def _inspect_fields(profile,replies,context=None):
    available=profile.available_addresses(replies,context=context)
    issues=[];values=profile.decode(replies,context,addresses=available,issues=issues)
    failures={issue.address:issue for issue in issues}
    rows=[];planes={}
    for address,field in sorted(profile.fields.items(),key=lambda pair:pair[1]['id']):
        value=values.get(address)
        if field['read']is None:
            status=('Unsupported'if field['trust']in{'unsupported','non_useful'}else'No native rule')
            explanation=('The profile does not use this field.'if status=='Unsupported'else'This file has no native read rule for this field.')
        elif address not in available:
            status='Missing reply';explanation='A required native reply is absent from this saved input.'
        elif address in failures:
            status='Conversion failed';explanation='The saved value could not be converted to the required field type.'
        elif value is None:
            status='Unavailable';explanation='The rule found no usable value in this saved input.'
        else:
            status='Decoded';explanation='The rule decoded a value. Its real-world meaning still requires native evidence.'
        rows.append({'id':field['id'],'field':address,'status':status,'explanation':explanation,
            'rule_pointer':'/fields/'+address+'/read','rule':field['read'],'unit':field['unit']})
        if address in values:
            line,key=address.split('.',1);planes.setdefault(line,{})[key]=value
    return {'schema':'PortableProfileFieldReview.v1','profile':profile.provenance(),
        'fields':rows,'values':planes,'limits':['Only the supplied saved replies were replayed. No router was contacted and no command was sent.',
        'Request delivery and write results cannot be checked from this value replay.']}


def inspect_fields(profile,replies,context=None):
    return scrub_record(_inspect_fields(profile,replies,context))


def compare_fields(before,after,replies,context=None):
    if before.profile_id!=after.profile_id:raise ValueError('Compare two versions of the same profile.')
    first=_inspect_fields(before,replies,context);second=_inspect_fields(after,replies,context)
    changes=[]
    for address,field in before.fields.items():
        line,key=address.split('.',1)
        a=first['values'].get(line,{}).get(key);b=second['values'].get(line,{}).get(key)
        if type(a)is not type(b)or a!=b:changes.append({'id':field['id'],'field':address,'rule_pointer':'/fields/'+address+'/read'})
    edited=[{'id':field['id'],'field':address,'rule_pointer':'/fields/'+address+'/read'}
        for address,field in before.fields.items()if field['read']!=after.fields[address]['read']]
    return scrub_record({'schema':'PortableProfileEditReplay.v1','before':first,'after':second,'changed_fields':changes,
        'edited_rules':edited,'write_definitions_changed':before.writes!=after.writes})
