"""Offline export of the existing profile catalogue; never a runtime importer.

Run with the repository's native Python. Only reviewed catalogue accessors and
static Python source are read. Drivers, configuration, History and replies are
never loaded or executed. Parser snapshots are evidence, not approved rules.
"""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arch4.modem_live.profile_catalog import (
    modem_profile_ids, modem_profile_record, modem_profile_selection,
)
from arch4.modem_live.profile_matrix_contract import profile_field_matrix_address
from arch4.modem_live.access_profile_owner import resolve_modem_live_access_profile
from tools.modem_profile_exchange.shape import ShapeError, validate_shape

SCHEMA = '54GUC.ProfileMigration.v1'
TRUST = {'confirmed', 'expected', 'synthetic', 'untested', 'unsupported', 'non_useful', 'unclassified'}
EMPTY = {'unsupported', 'non_useful', 'unclassified'}
TYPES = {'boolean', 'integer', 'number', 'string', 'array_integer', 'array_object', 'object', 'integer_or_string', 'number_or_string'}
# Profile selectors and behaviour metadata only. No connection configuration,
# notes containing captured identities, local paths, credentials or observations.
PROFILE_KEYS = (
    'profile_id', 'backend', 'vendor', 'family', 'model_contains', 'firmware_contains',
    'device_radio_class', 'provenance', 'capabilities', 'action_trust',
    'lte_band_capability_status', 'nr_band_capability_status',
    'supported_lte_bands', 'supported_nr_bands', 'provider_band_pool',
    'provider_name_contains', 'provider_plmn_contains', 'vendor_aliases',
    'target_guard_model', 'firmware_exact', 'hardware_exact', 'firmware_pattern',
    'os_name', 'os_version_contains', 'scheduler_signal_mode', 'network_settings',
    'network_settings_writable', 'apn_profile_creation', 'network_settings_firmware',
    'apn_write_requires_explicit_password', 'authentication_contract',
    'login_wire_username',
    'nr_cell_id_in_nsa', 'traffic_rate_mode', 'read_diagnostic_extra_oids',
    'static_config', 'ue_capability',
)


class InvalidExport(ValueError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def field_address(name):
    return profile_field_matrix_address(name) or 'profile.' + name


def load_fields():
    fields = []
    stable_ids = json.loads((HERE / 'field_ids.v1.json').read_text(encoding='utf-8'))
    for line in (HERE / 'fields.tsv').read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        address, kind, unit, meaning = line.split('|')
        if kind not in TYPES or not meaning:
            raise InvalidExport('invalid_field_definition:' + address)
        if address not in stable_ids:
            raise InvalidExport('new_field_requires_append_only_id:' + address)
        fields.append(dict(id=stable_ids[address], address=address, value_type=kind,
            unit=unit or None, meaning=meaning,
            missing='No value. Preserve missing/invalid/unsupported separately; never invent zero, false, an identity or an older fresh timestamp.',
            privacy='definition_only_no_observed_values', aliases=[], normalization_evidence=None))
    if len({f['address'] for f in fields}) != len(fields):
        raise InvalidExport('duplicate_field_definition')
    if set(stable_ids) != {f['address'] for f in fields}:
        raise InvalidExport('stable_field_definition_missing')
    return sorted(fields, key=lambda f: f['id'])


def profile_records():
    return [modem_profile_record(name) for name in sorted(modem_profile_ids())]


def bind_fields(fields, records):
    by_address = {f['address']: f for f in fields}
    aliases = defaultdict(set)
    for record in records:
        for name in record['field_trust']:
            aliases[field_address(name)].add(name)
    if set(aliases) != set(by_address):
        raise InvalidExport('catalogue_definition_coverage:' + repr({
            'undefined': sorted(set(aliases) - set(by_address)),
            'retired_or_unused': sorted(set(by_address) - set(aliases))}))
    for address, names in aliases.items():
        by_address[address]['aliases'] = sorted(names)


def driver_sources():
    """Reuse the existing audit owner's map without importing its live drivers."""
    tree = ast.parse((ROOT / 'scripts/audit_backend_fields.py').read_text(encoding='utf-8-sig'))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == 'BACKEND_SOURCE_FILES':
            result = {}
            for key, value in zip(node.value.keys, node.value.values):
                parts = []
                while isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div):
                    parts.insert(0, ast.literal_eval(value.right))
                    value = value.left
                if not isinstance(value, ast.Name) or value.id != 'REPO_ROOT':
                    raise InvalidExport('driver_map_expression_changed')
                result[ast.literal_eval(key)] = '/'.join(parts)
            return result
    raise InvalidExport('driver_map_missing')


class StripDocumentation(ast.NodeTransformer):
    """Comments/docstrings are not parser semantics and can contain private prose."""
    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            node.body = node.body[1:] or [ast.Pass()]
        return node
    visit_AsyncFunctionDef = visit_FunctionDef


class SourceIndex:
    """Static symbol closure. Dynamic calls are recorded, never guessed/resolved by execution."""
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.modules = {}
        self.symbols = {}
        self.library = {}
        self.field_occurrences = {}

    def module(self, name):
        if name in self.modules:
            return self.modules[name]
        path = self.root.joinpath(*name.split('.')).with_suffix('.py')
        if (not name.startswith(('arch4.modem.', 'arch4.modem_live.', 'arch4.core_contracts.'))
                or name in {'arch4.modem.catalog', 'arch4.modem_live.profile_catalog',
                            'arch4.modem_live.connection_profiles'} or not path.is_file()):
            return None
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        info = {'path': path.relative_to(self.root).as_posix(), 'imports': {}, 'classes': {}, 'symbols': {}}
        self.modules[name] = info
        # Include function-local imports as well: they remain static declarations.
        for node in ast.walk(tree):
            # Function-local imports are resolved separately in freeze().
            if node not in tree.body:
                continue
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    info['imports'][alias.asname or alias.name] = node.module + '.' + alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    info['imports'][alias.asname or alias.name.split('.')[0]] = alias.name if alias.asname else alias.name.split('.')[0]
        def add(local, node, cls=None):
            symbol = name + ':' + local
            self.symbols[symbol] = (name, cls, node)
            info['symbols'][local] = symbol
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add(node.name, node)
            elif isinstance(node, ast.ClassDef):
                info['classes'][node.name] = node.bases
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        add(node.name + '.' + child.name, child, node.name)
                    elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                        targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                        for target in targets:
                            if isinstance(target, ast.Name): add(node.name + '.' + target.id, child, node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name): add(target.id, node)
        return info

    def imported(self, dotted):
        parts = dotted.split('.')
        for stop in range(len(parts) - 1, 1, -1):
            info = self.module('.'.join(parts[:stop]))
            if info:
                return info['symbols'].get('.'.join(parts[stop:]))
        return None

    def method(self, module, cls, member, seen=None):
        seen = set() if seen is None else seen
        if (module, cls) in seen: return None
        seen.add((module, cls))
        info = self.module(module)
        found = info['symbols'].get(cls + '.' + member)
        if found: return found
        for base in info['classes'].get(cls, ()):
            text = ast.unparse(base)
            target = info['imports'].get(text, module + '.' + text)
            parent_module, _, parent_cls = target.rpartition('.')
            parent = self.module(parent_module)
            if parent and parent_cls in parent['classes']:
                found = self.method(parent_module, parent_cls, member, seen)
                if found: return found
        return None

    def resolve(self, module, cls, expression):
        info = self.module(module)
        parts = expression.split('.')
        if parts[0] in {'self', 'cls'} and cls and len(parts) == 2:
            return self.method(module, cls, parts[1])
        if expression in info['symbols']: return info['symbols'][expression]
        if parts[0] in info['imports']:
            return self.imported(info['imports'][parts[0]] + ('.' + '.'.join(parts[1:]) if len(parts) > 1 else ''))
        return None

    def freeze(self, symbol):
        if symbol in self.library: return
        module, cls, node = self.symbols[symbol]
        info = self.module(module)
        clean = StripDocumentation().visit(deepcopy(node))
        source = ast.unparse(ast.fix_missing_locations(clean))
        item = dict(symbol=symbol, file=info['path'], line=node.lineno,
                    source=source, source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                    dependencies=[], external_or_dynamic_calls=[],
                    status='frozen_source_evidence_not_portable_definition')
        self.library[symbol] = item
        dependencies, external = set(), set()
        local_imports = defaultdict(set)
        for candidate in ast.walk(node):
            if isinstance(candidate, ast.ImportFrom) and candidate.module and not candidate.level:
                for alias in candidate.names:
                    local_imports[alias.asname or alias.name].add(candidate.module + '.' + alias.name)
        local_names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        local_names.update(n.arg for n in ast.walk(node) if isinstance(n, ast.arg))
        def resolve(expression):
            first, dot, rest = expression.partition('.')
            if first in local_imports:
                targets = local_imports[first]
                if len(targets) != 1: return None
                return self.imported(next(iter(targets)) + (dot + rest if dot else ''))
            if first in local_names and first not in {'self', 'cls'}:
                return None
            return self.resolve(module, cls, expression)
        for candidate in ast.walk(node):
            if isinstance(candidate, ast.Call):
                expression = ast.unparse(candidate.func)
                resolved = resolve(expression)
                if resolved and resolved != symbol: dependencies.add(resolved)
                elif not resolved: external.add(expression)
            elif isinstance(candidate, ast.Name) and isinstance(candidate.ctx, ast.Load):
                resolved = resolve(candidate.id)
                if resolved and resolved != symbol: dependencies.add(resolved)
        item['dependencies'] = sorted(dependencies)
        item['external_or_dynamic_calls'] = sorted(external)
        for dependency in sorted(dependencies): self.freeze(dependency)

    def candidates(self, driver, aliases):
        module = driver.removesuffix('.py').replace('/', '.')
        info = self.module(module)
        modules = {module}
        # ZWRT inherits GoForm decoding. Walk the actual bases, not a second map.
        pending = [info]
        while pending:
            current = pending.pop()
            for bases in current['classes'].values():
                for base in bases:
                    target = current['imports'].get(ast.unparse(base), '')
                    parent_name = target.rpartition('.')[0]
                    if parent_name.startswith('arch4.modem.') and parent_name not in modules:
                        parent = self.module(parent_name)
                        if parent:
                            modules.add(parent_name)
                            pending.append(parent)
        result = []
        names = set(aliases)
        for name in sorted(modules):
            for symbol in list(self.modules[name]['symbols'].values()):
                node = self.symbols[symbol][2]
                if symbol not in self.field_occurrences:
                    self.field_occurrences[symbol] = ({n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
                        | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
                        | {n.arg for n in ast.walk(node) if isinstance(n, ast.keyword) and n.arg is not None})
                occurrences = sorted(self.field_occurrences[symbol] & names)
                if occurrences:
                    self.freeze(symbol)
                    result.append({'symbol': symbol, 'field_names': occurrences,
                                   'binding': 'static_occurrence_requires_review'})
        return result


def privacy_check(value):
    """Reject rather than redact semantics or write a partially sanitized file."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {'observed_value', 'raw_reply', 'credentials', 'connection_config',
                              'password', 'token', 'access_token', 'authorization', 'cookie',
                              'imei', 'imsi', 'serial', 'wan_ip', 'lan_mac'} and item is not None:
                raise InvalidExport('observed_or_private_data_forbidden:' + key)
            if key == 'source' and isinstance(item, str):
                # Inspect actual literal values, not escaped source notation:
                # a regex such as \\s is not a Windows UNC path.
                tree = ast.parse(item)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        privacy_check(node.value)
            else:
                privacy_check(item)
    elif isinstance(value, (list, tuple)):
        for item in value: privacy_check(item)
    elif isinstance(value, str):
        if re.search(r'''(?:^|[\s"'])(?:[A-Za-z]:[\\/]|\\\\[A-Za-z][^\s]*[\\/])|/home/|/Users/''', value):
            raise InvalidExport('absolute_private_path_forbidden')
        if re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', value):
            raise InvalidExport('email_forbidden')
        if re.search(r'\b(?:github_pat_|ghp_|sk-proj-)[A-Za-z0-9_]+', value):
            raise InvalidExport('credential_token_forbidden')
        if re.fullmatch(r'[0-9]{14,16}', value) and set(value) != {'0'}:
            raise InvalidExport('equipment_or_subscription_identifier_forbidden')
        if re.fullmatch(r'(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}', value) and re.sub('[:-]', '', value) != '000000000000':
            raise InvalidExport('actual_mac_address_forbidden')


def build_export():
    records = profile_records()
    fields = load_fields()
    bind_fields(fields, records)
    drivers = driver_sources()
    index = SourceIndex()
    # Include the last ModemLive normalization boundary as well as native drivers.
    # This is the existing owner's map, not a new runtime alias registry.
    normalizer_module = 'arch4.modem_live.canonical_aliases'
    info = index.module(normalizer_module)
    spec_node = index.symbols[info['symbols']['CANONICAL_ALIAS_SPECS']][2]
    by_address = {f['address']: f for f in fields}
    for node in ast.walk(spec_node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'CanonicalAliasSpec':
            address = ast.literal_eval(node.args[0]) + '.' + ast.literal_eval(node.args[1])
            if address not in by_address: continue
            symbol = index.resolve(normalizer_module, None, ast.unparse(node.args[3]))
            if symbol is None: raise InvalidExport('normalizer_unresolved:' + address)
            index.freeze(symbol)
            by_address[address]['normalization_evidence'] = symbol
    bindings = {}
    profiles = []
    for record in records:
        selection = modem_profile_selection(record['profile_id'])
        driver = drivers.get(selection.backend) or drivers.get(record['backend'])
        if driver is None: raise InvalidExport('driver_source_missing:' + record['profile_id'])
        trust_by_address = {}
        for name, trust in record['field_trust'].items():
            address = field_address(name)
            if address in trust_by_address and trust_by_address[address] != trust:
                raise InvalidExport('alias_trust_conflict:' + record['profile_id'] + ':' + address)
            trust_by_address[address] = trust
        slots = []
        for field in fields:
            trust = trust_by_address.get(field['address'], 'unclassified')
            rule = None
            if trust not in EMPTY:
                key = (driver, field['id'])
                if key not in bindings:
                    bindings[key] = index.candidates(driver, field['aliases'] + [field['address'].partition('.')[2]])
                rule = {'status': 'needs_semantic_review', 'definition': None,
                        'source_candidates': bindings[key],
                        'declared_derivations': [{'field_name': name, 'rule_text': text}
                            for name, text in record['synthetic_fields'].items()
                            if field_address(name) == field['address']],
                        'reason': 'Source occurrence and helper closure do not prove a field read or a portable rule. Review native input, branches, units and missing handling before approving.'}
            slots.append({'field_id': field['id'], 'trust': trust, 'parsing': rule,
                          'observed_value': None})
        access = resolve_modem_live_access_profile(backend=selection.backend,
            backend_family=selection.family, profile_id=selection.profile_id,
            model=selection.model_hint, firmware=selection.software_hint,
            vendor=selection.manufacturer)
        profiles.append({'metadata': {key: record[key] for key in PROFILE_KEYS},
                         'access': asdict(access), 'fields': slots,
                         'runtime_ready': False})
    result = {'schema': SCHEMA, 'definition_version': 1,
        'field_definitions': fields, 'profiles': profiles,
        'parser_evidence': dict(sorted(index.library.items())),
        'coverage': {'profile_count': len(profiles), 'field_count': len(fields),
            'profile_alias_count': len({a for f in fields for a in f['aliases']}),
            'rule_evidence_count': len(index.library),
            'approved_portable_rules': 0},
        'privacy': {'input_scope': 'static_catalogue_and_source_only',
                    'observations_included': False, 'source_comments_included': False,
                    'credentials_included': False},
        'runtime_ready': False}
    # Normalize tuples to JSON arrays before validation/checksumming.
    result = json.loads(encoded(result))
    result['content_sha256'] = digest(result)
    validate_export(result)
    return result


def validate_export(data):
    try:
        validate_shape(data, json.loads((HERE / 'schema.json').read_text(encoding='utf-8')))
    except ShapeError as exc:
        raise InvalidExport('schema_shape:' + str(exc)) from exc
    if type(data) is not dict or data.get('schema') != SCHEMA:
        raise InvalidExport('wrong_schema')
    if set(data) != {'schema', 'definition_version', 'field_definitions', 'profiles', 'parser_evidence', 'coverage', 'privacy', 'runtime_ready', 'content_sha256'}:
        raise InvalidExport('unknown_or_missing_top_level_key')
    content = dict(data)
    claimed = content.pop('content_sha256')
    if claimed != digest(content): raise InvalidExport('content_hash_mismatch')
    if data['runtime_ready'] is not False or data['definition_version'] != 1:
        raise InvalidExport('unreviewed_runtime_activation_forbidden')
    fields = data['field_definitions']
    owned_fields = load_fields()
    contract_keys = ('id', 'address', 'value_type', 'unit', 'meaning', 'missing', 'privacy')
    if ([{k: f[k] for k in contract_keys} for f in fields]
            != [{k: f[k] for k in contract_keys} for f in owned_fields]):
        raise InvalidExport('definition_version_contract_mismatch')
    ids = [f['id'] for f in fields]
    if ids != list(range(1, len(fields) + 1)) or any(type(i) is not int for i in ids):
        raise InvalidExport('field_ids_invalid')
    if len({f['address'] for f in fields}) != len(fields): raise InvalidExport('duplicate_field_address')
    all_aliases = [alias for field in fields for alias in field['aliases']]
    if len(all_aliases) != len(set(all_aliases)): raise InvalidExport('duplicate_alias')
    for field in fields:
        if set(field) != {'id', 'address', 'value_type', 'unit', 'meaning', 'missing', 'privacy', 'aliases', 'normalization_evidence'}:
            raise InvalidExport('unknown_field_definition_key')
        if field['value_type'] not in TYPES or not field['meaning'] or not field['missing']:
            raise InvalidExport('field_semantics_missing')
    library = data['parser_evidence']
    for symbol, evidence in library.items():
        if set(evidence) != {'symbol', 'file', 'line', 'source', 'source_sha256', 'dependencies', 'external_or_dynamic_calls', 'status'}:
            raise InvalidExport('unknown_parser_evidence_key')
        if symbol != evidence['symbol'] or evidence['source_sha256'] != hashlib.sha256(evidence['source'].encode()).hexdigest():
            raise InvalidExport('parser_evidence_hash_mismatch')
        if any(dependency not in library for dependency in evidence['dependencies']):
            raise InvalidExport('parser_dependency_missing')
        if evidence['status'] != 'frozen_source_evidence_not_portable_definition':
            raise InvalidExport('source_is_not_a_portable_definition')
    profile_ids = []
    for profile in data['profiles']:
        if set(profile) != {'metadata', 'access', 'fields', 'runtime_ready'}:
            raise InvalidExport('unknown_profile_key')
        profile_ids.append(profile['metadata']['profile_id'])
        if profile['runtime_ready'] is not False: raise InvalidExport('unreviewed_profile_activation_forbidden')
        if [s['field_id'] for s in profile['fields']] != ids: raise InvalidExport('profile_field_coverage_missing')
        for slot in profile['fields']:
            if set(slot) != {'field_id', 'trust', 'parsing', 'observed_value'}:
                raise InvalidExport('unknown_slot_key')
            if slot['trust'] not in TRUST or slot['observed_value'] is not None:
                raise InvalidExport('slot_invalid')
            if slot['trust'] in EMPTY:
                if slot['parsing'] is not None: raise InvalidExport('unsupported_slot_must_be_null')
            else:
                rule = slot['parsing']
                if not isinstance(rule, dict) or set(rule) != {'status', 'definition', 'source_candidates', 'declared_derivations', 'reason'}:
                    raise InvalidExport('unknown_parsing_key')
                if not isinstance(rule, dict) or rule['status'] != 'needs_semantic_review' or rule['definition'] is not None:
                    raise InvalidExport('unreviewed_rule_status_invalid')
                for candidate in rule['source_candidates']:
                    if set(candidate) != {'symbol', 'field_names', 'binding'} or candidate['binding'] != 'static_occurrence_requires_review':
                        raise InvalidExport('invalid_source_binding')
                    if candidate['symbol'] not in library: raise InvalidExport('parser_candidate_missing')
        if set(profile['metadata']) != set(PROFILE_KEYS): raise InvalidExport('profile_metadata_allowlist_violation')
        if set(profile['metadata']['static_config']) - {'ttl_ipv4', 'ttl_ipv6', 'mss_ipv4', 'mss_ipv6', 'keepalive'}:
            raise InvalidExport('static_config_allowlist_violation')
        if set(profile['metadata']['ue_capability']) - {'dl_256qam', 'ul_64qam', 'ue_category'}:
            raise InvalidExport('ue_capability_allowlist_violation')
    for field in fields:
        if field['normalization_evidence'] is not None and field['normalization_evidence'] not in library:
            raise InvalidExport('normalizer_evidence_missing')
    if len(set(profile_ids)) != len(profile_ids): raise InvalidExport('duplicate_profile_id')
    counts = data['coverage']
    if counts != dict(profile_count=len(profile_ids), field_count=len(fields), profile_alias_count=len(all_aliases), rule_evidence_count=len(library), approved_portable_rules=0):
        raise InvalidExport('coverage_count_mismatch')
    privacy_check(data)


def write_export(data, destination):
    validate_export(data)
    destination = Path(destination)
    # Refuse overwrite. Serialization and validation finish before opening output.
    payload = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.profile-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic publication without overwriting any existing export. If the
        # filesystem cannot provide this guarantee, fail without a fallback.
        os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output', type=Path)
    group.add_argument('--validate', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.validate:
            data = json.loads(args.validate.read_text(encoding='utf-8'))
            validate_export(data)
        else:
            data = build_export()
            write_export(data, args.output)
        print(json.dumps({'valid': True, **data['coverage'], 'runtime_ready': False}))
    except (InvalidExport, OSError, ValueError, KeyError, TypeError) as exc:
        # Never echo rejected private values or file contents.
        print(json.dumps({'valid': False, 'error_type': type(exc).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
