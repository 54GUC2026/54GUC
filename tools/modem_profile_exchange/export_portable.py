"""Explicit candidate authoring or byte-preserving export of owned definitions."""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from tools.modem_profile_exchange.portable_profiles import build_profile,validate_profile
from tools.modem_profile_exchange.export_profiles import profile_records,encoded,digest,InvalidExport
from tools.modem_profile_exchange.field_catalogue import catalogue_v2

HERE=Path(__file__).resolve().parent


def profile_id(data):return data['profile']['profile_id']if data.get('schema')=='54GUC.ModemProfile.v1'else data['profile_id']


def validate_document(data):
    if not isinstance(data,dict):raise InvalidExport('profile_object_required')
    if data.get('schema')=='54GUC.ModemProfile.v1':
        from arch4.modem_live.profile_definition.registry import PortableProfile
        return PortableProfile('<candidate>',json.dumps(data,ensure_ascii=False).encode('utf-8'),data)
    validate_profile(data)


def read_snapshot(path):
    if path.is_symlink() or path.stat().st_size>8_388_608:raise InvalidExport('profile_file_boundary')
    def object_pairs(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise InvalidExport('duplicate_json_key')
            result[key]=value
        return result
    def nonfinite(value):raise InvalidExport('nonfinite_json_number')
    with path.open('rb')as stream:raw=stream.read(8_388_609)
    if len(raw)>8_388_608:raise InvalidExport('profile_file_boundary')
    return raw,json.loads(raw.decode('utf-8'),object_pairs_hook=object_pairs,parse_constant=nonfinite)


def read_document(path):return read_snapshot(path)[1]


def current_profile(path):
    result=validate_document(read_document(path))
    if result is None:raise InvalidExport('Use a current 54GUC.ModemProfile.v1 file for validation or replay; this is a legacy migration export.')
    return result


def verify_folder(folder,*,expected_ids=None):
    folder=folder.resolve(strict=True)
    paths=sorted(folder.glob('*.json'));ids=[];hashes={}
    if not paths:raise InvalidExport('empty_profile_folder')
    for path in paths:
        raw,data=read_snapshot(path);validate_document(data);identifier=profile_id(data)
        if path.name!=identifier+'.json':raise InvalidExport('profile_filename_identity_mismatch')
        ids.append(identifier);hashes[path.name]=hashlib.sha256(raw).hexdigest()
    if len(ids)!=len(set(i.casefold() for i in ids)):raise InvalidExport('duplicate_profile_id')
    if expected_ids is not None and set(ids)!=set(expected_ids):raise InvalidExport('profile_folder_inventory_mismatch')
    return {'profile_count':len(ids),'files_sha256':hashes,'runtime_changed':False}


def publish(folder,documents,*,payloads=None):
    folder=folder.resolve()
    parent=folder.parent.resolve(strict=True)
    if folder.exists():raise FileExistsError('existing_profile_folder_is_immutable:'+str(folder))
    serialized={profile_id(d)+'.json':encoded(d)+b'\n' for d in documents} if payloads is None else payloads
    if len(serialized)!=len(documents):raise InvalidExport('duplicate_profile_id')
    for data in documents:validate_document(data)
    temporary=Path(tempfile.mkdtemp(prefix='.'+folder.name+'-validated-',dir=parent)).resolve()
    if temporary.parent!=parent or folder.parent!=parent:raise InvalidExport('publication_parent_mismatch')
    # On failure the prepared directory is retained for diagnosis, not removed
    # recursively. Existing destinations are never updated in place.
    for name,payload in serialized.items():
        with (temporary/name).open('xb') as stream:stream.write(payload)
    verify_folder(temporary,expected_ids=[profile_id(d) for d in documents])
    os.rename(temporary,folder)
    return verify_folder(folder,expected_ids=[profile_id(d) for d in documents])


def export_internal_snapshot(registry,folder):
    """Export ModemLive's admitted definitions, not a second driver extraction.

    The registry keeps exact startup bytes even if someone edits a file later.
    Native observations, credentials and live consumer state are never inputs.
    """
    from arch4.modem_live.profile_definition.privacy import checked_definition
    documents=[];payloads={}
    for identifier in registry.ids():
        raw=registry.definition_bytes(identifier)
        admitted=checked_definition(raw)
        if admitted.profile_id!=identifier:raise InvalidExport('internal_profile_identity_mismatch')
        documents.append(admitted.snapshot())
        payloads[identifier+'.json']=raw
    if not documents:raise InvalidExport('no_valid_internal_profiles')
    return publish(folder,documents,payloads=payloads)


def author_candidates():
    import socket
    from tools.modem_profile_exchange.conformance import inspect_profile
    from tools.modem_profile_exchange.conformance_inputs import NOW
    def denied(*a,**k):raise AssertionError('offline candidate authoring attempted network')
    documents=[];results=[]
    with patch('socket.socket.connect',denied),patch('socket.create_connection',denied),patch('time.time',lambda:NOW),patch('time.monotonic',lambda:NOW):
        for record in profile_records():
            data=build_profile(record);result=inspect_profile(data,record)
            if result['failures']:raise InvalidExport('driver_conformance_failure:'+record['profile_id'])
            data['conformance'].update(status='offline_differential_checked',matched_fields=result['driver_matches'],
                explicit_driver_gap_fields=result['driver_gaps'],
                evidence='Synthetic native replies through the existing driver; no physical firmware claim.')
            data.pop('content_sha256');data['content_sha256']=digest(data)
            validate_profile(data);documents.append(data);results.append(result)
    return documents,results


def _main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--candidate-from-current-code',action='store_true',help='Legacy extraction/conformance audit; use --export-internal for current editable profiles')
    mode.add_argument('--copy-owned',action='store_true')
    mode.add_argument('--export-internal',action='store_true',help='Export the admitted ModemLive profiles as human-editable JSON; no router contact')
    mode.add_argument('--verify-folder',type=Path)
    mode.add_argument('--validate-profile',type=Path)
    mode.add_argument('--replay-profile',type=Path)
    mode.add_argument('--review-bundle',type=Path,help='Review a support ZIP directly; optionally preview a corrected profile without contacting a router')
    parser.add_argument('--replies',type=Path)
    parser.add_argument('--context',type=Path)
    parser.add_argument('--compare-profile',type=Path)
    from arch4.modem_live.profile_definition.registry import profile_directory
    parser.add_argument('--source-folder',type=Path,default=profile_directory())
    parser.add_argument('--output-folder',type=Path)
    parser.add_argument('--receipt',type=Path)
    parser.add_argument('--report-folder',type=Path,help='New folder for a readable report when reviewing a support ZIP')
    args=parser.parse_args(argv)
    if args.report_folder and not args.review_bundle:parser.error('--report-folder requires --review-bundle')
    if args.receipt and args.receipt.exists():raise FileExistsError('existing_receipt_is_immutable')
    if args.review_bundle:
        from arch4.foundation.support_archive import read_support_archive
        from tools.log_checker.profile_fields import inspect_bundle_fields
        candidate=current_profile(args.compare_profile)if args.compare_profile else None
        if args.report_folder:
            from tools.log_checker.report import analyse_bundle,write_report
            report=analyse_bundle(args.review_bundle,candidate_profile=candidate)
            receipt=report['profile_fields']
            write_report(report,args.report_folder)
        else:
            manifest,files=read_support_archive(args.review_bundle)
            receipt=inspect_bundle_fields(manifest,files,candidate=candidate)
    elif args.validate_profile:
        profile=current_profile(args.validate_profile)
        receipt={'valid':True,'profile':profile.provenance(),'runtime_changed':False}
    elif args.replay_profile:
        if args.replies is None:parser.error('--replies is required')
        from tools.log_checker.profile_fields import inspect_fields,compare_fields
        profile=current_profile(args.replay_profile)
        replies=read_document(args.replies);context=read_document(args.context)if args.context else{}
        if not isinstance(replies,dict)or not isinstance(context,dict):raise InvalidExport('Saved replies and context must be JSON objects.')
        receipt=(compare_fields(profile,current_profile(args.compare_profile),replies,context)
            if args.compare_profile else inspect_fields(profile,replies,context))
    elif args.verify_folder:
        receipt=verify_folder(args.verify_folder)
    else:
        if args.output_folder is None:parser.error('--output-folder is required')
        if args.export_internal:
            from arch4.modem_live.profile_definition.registry import ProfileRegistry
            registry=ProfileRegistry.load(args.source_folder)
            if registry.errors:raise InvalidExport(str(registry.errors[0]))
            receipt=export_internal_snapshot(registry,args.output_folder)
            if args.receipt:
                with args.receipt.open('x',encoding='utf-8')as stream:json.dump(receipt,stream,indent=2)
            print(json.dumps({k:v for k,v in receipt.items()if k!='files_sha256'},sort_keys=True))
            return
        results=None;payloads=None
        if args.candidate_from_current_code:documents,results=author_candidates()
        else:
            snapshots={p.name:read_snapshot(p)for p in sorted(args.source_folder.glob('*.json'))}
            if not snapshots:raise InvalidExport('empty_profile_folder')
            documents=[data for raw,data in snapshots.values()]
            payloads={name:raw for name,(raw,data)in snapshots.items()}
        receipt=publish(args.output_folder,documents,payloads=payloads)
        if results is not None:
            receipt.update(matched_fields=sum(r['driver_matches'] for r in results),
                explicit_driver_gap_fields=sum(r['driver_gaps'] for r in results),profiles=results)
    if args.receipt:
        with args.receipt.open('x',encoding='utf-8') as stream:json.dump(receipt,stream,indent=2)
    print(json.dumps({k:v for k,v in receipt.items() if k not in {'files_sha256','profiles'}},sort_keys=True))


def main(argv=None):
    import sys
    try:_main(argv);return 0
    except(OSError,ValueError,KeyError,TypeError,RecursionError)as error:
        print('Profile check stopped: '+str(error),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
