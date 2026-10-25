"""Closed operation registry and structural validation for portable definitions."""
from arch4.modem_live.profile_definition.expressions import RuleError

EXPRESSION_OPERATIONS=frozenset('''literal require xml_text algorithm input variable get let if and or
first_present try call map filter every some object list eq ne lt le gt ge add subtract multiply divide
floor_divide modulo bit_and shift_left not contains is_type text strip lower upper replace split join length
slice entries keys concat flatten unique integer number finite integral abs round sha256_utf8 indexed log10
minimum maximum sorted_unique intersection enum match extract find_all'''.split())
INSTRUCTION_KINDS=frozenset('''constant name list tuple set dictionary member index slice invoke spread binary
unary boolean compare conditional comprehension named_value format_join format_value function return assign
expression emit if for while try raise break continue pass'''.split())
BINARY=frozenset('add subtract multiply divide floor_divide modulo power bit_or bit_and shift_left shift_right'.split())
UNARY=frozenset('not negative positive invert'.split())
COMPARE=frozenset('equal not_equal less less_equal greater greater_equal in not_in is is_not'.split())
BUILTINS=frozenset('''str int float bool list tuple set frozenset dict len abs min max sum round sorted
enumerate zip map filter any all next iter range isinstance type ord Exception ValueError TypeError
OverflowError ZeroDivisionError KeyError IndexError re math ET hashlib ipaddress'''.split())


def local_names(value):
    result=set()
    for n in walk(value):
        if n.get('kind')=='function':
            if 'name' in n:result.add(n['name'])
            p=n['parameters'];result.update(p['positional']);result.update(p['keywords'])
            result.update(v for v in (p['varargs'],p['kwargs']) if v)
        if n.get('kind') in {'assign','named_value','for'}:
            targets=n.get('targets',[n.get('target')])
            for target in targets:
                result|=references(target)
        if n.get('kind')=='comprehension':
            for loop in n['loops']:result|=references(loop['target'])
        if n.get('kind')=='try':
            result.update(h['name'] for h in n['handlers'] if h['name'])
    return result


def walk(value):
    if isinstance(value,list):
        for item in value:yield from walk(item)
    elif isinstance(value,dict):
        yield value
        for item in value.values():yield from walk(item)


def references(value):
    return {n['name'] for n in walk(value) if n.get('kind')=='name'}


def prune_algorithms(library,expressions):
    """Keep only each file's transitive operation/constant closure."""
    wanted={n['name'] for n in walk(expressions) if n.get('op')=='algorithm'}
    wanted|=references(library.get('initializers',[]))
    seen=set()
    while wanted-seen:
        for name in list(wanted-seen):
            seen.add(name)
            wanted|=references(library['functions'].get(name,library['constants'].get(name)))
    return {**library,'functions':{k:v for k,v in library['functions'].items() if k in wanted},
        'constants':{k:v for k,v in library['constants'].items() if k in wanted}}


def validate_vocabulary(data):
    functions=data['functions'];algorithms=data['algorithms']
    if algorithms.get('vocabulary')!='54GUC.BoundedParserAlgorithms.v1':raise RuleError('unknown_algorithm_vocabulary')
    for node in walk([data['fields'],functions,data.get('preprocessing',[]),data.get('state',{}),data.get('writes',{})]):
        if 'op' not in node:continue
        operation=node['op']
        if operation not in EXPRESSION_OPERATIONS:raise RuleError('unknown_operation:'+str(operation))
        if operation=='call' and node.get('rule') not in functions:raise RuleError('unresolved_rule')
        if operation=='algorithm' and node.get('name') not in algorithms['functions']:raise RuleError('unresolved_algorithm')
        if operation=='input':
            if not node.get('path') or node['path'][0] not in {'replies','context','profile'}:raise RuleError('unknown_input_namespace')
        if operation=='require' and not isinstance(node.get('reason'),str):raise RuleError('rejection_reason_required')
    for node in walk(algorithms):
        if 'kind' not in node:continue
        kind=node['kind']
        if kind not in INSTRUCTION_KINDS:raise RuleError('unknown_instruction:'+str(kind))
        if kind=='member' and node['name'].startswith('__'):raise RuleError('private_algorithm_member')
        if kind in {'binary','unary'} and node['operation'] not in (BINARY if kind=='binary' else UNARY):raise RuleError('unknown_instruction_operation')
        if kind=='compare' and set(node['operations'])-COMPARE:raise RuleError('unknown_comparison')
    globals_=set(algorithms['functions'])|set(algorithms['constants'])|BUILTINS|local_names(algorithms['initializers'])
    for name,program in algorithms['functions'].items():
        missing=references(program)-globals_-local_names(program)
        if missing:raise RuleError('unbound_algorithm_names:'+name+':'+','.join(sorted(missing)))
    # A profile cannot hide source code or an external executable fallback.
    forbidden={'python_source','python_call','eval','exec','imports','driver_callback','external_profile'}
    for node in walk(data):
        if forbidden & set(node):raise RuleError('executable_extension_forbidden')
