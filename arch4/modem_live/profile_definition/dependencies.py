"""Pure native-reply dependency analysis shared by admission and replay."""
from .expressions import Evaluator,RuleError
from .vocabulary import walk


def native_field_keys(expression, reply, functions):
    """Literal root fields used by a read, including helper-bound selectors.

    This describes packet layout, not values or freshness. Whole-reply fixed
    codecs retain their native adapter's existing field lists.
    """
    steps=0
    def charge():
        nonlocal steps
        steps+=1
        if steps>200000:raise RuleError('native_field_plan_work_budget_exceeded')
    def bound(node,bindings):
        charge()
        if isinstance(node,list):return [bound(v,bindings)for v in node]
        if not isinstance(node,dict):return node
        if node.get('op')=='variable'and node.get('name')in bindings:return bindings[node['name']]
        return {k:bound(v,bindings)for k,v in node.items()}
    def path(node):
        charge()
        if not isinstance(node,dict):return ()
        if node.get('op')=='input':return tuple(node['path'])
        if node.get('op')=='get'and isinstance(node.get('key'),str):
            parent=path(node.get('value'))
            if parent:return (*parent,node['key'])
        return ()
    def visit(node,active=()):
        charge()
        if isinstance(node,list):return set().union(*(visit(v,active)for v in node))
        if not isinstance(node,dict):return set()
        source=path(node)
        if len(source)>2 and source[:2]==('replies',reply):
            return {source[2]}|visit(node.get('default'),active)
        if node.get('op')=='call':
            name=node['rule']
            if name in functions and name not in active:
                return visit(bound(functions[name]['expression'],node.get('arguments',{})),(*active,name))
        return set().union(*(visit(v,active)for v in node.values()))
    return frozenset(visit(expression))


def _known_context_path(context,path):
    if context is None:return False
    value=context
    for key in path[1:]:
        if isinstance(value,dict)and key in value:value=value[key]
        elif isinstance(value,(list,tuple))and type(key)is int and 0<=key<len(value):value=value[key]
        else:return False
    return True


def reply_dependencies(expression,metadata,functions=None,seen=frozenset(),*,context=None):
    """Only the selected firmware branch can require a native reply.

    E.g. reported-rate firmware never needs the counter-delta WAN reply.
    Known native-owner context can also select a pure branch. Conditions using
    raw replies, algorithms or unknown context remain conservative.
    """
    if isinstance(expression,list):
        return frozenset().union(*(reply_dependencies(v,metadata,functions,seen,context=context)for v in expression))
    if not isinstance(expression,dict):return frozenset()
    if expression.get('op')=='if':
        condition=expression['condition']
        dynamic=any(n.get('op')in{'algorithm','call','variable'}or
            n.get('op')=='input'and (n['path'][0]not in{'profile','context'}or
                n['path'][0]=='context'and not _known_context_path(context,n['path']))for n in walk(condition))
        if not dynamic:
            try:chosen=Evaluator({}).run(condition,{'profile':metadata,'context':context or{}})
            except (ValueError,TypeError,KeyError,IndexError,OverflowError):pass
            else:return reply_dependencies(expression['then'if chosen else'else'],metadata,functions,seen,context=context)
    if expression.get('op')=='call'and expression['rule']not in seen:
        name=expression['rule']
        if functions and name in functions:
            return reply_dependencies(functions[name]['expression'],metadata,functions,seen|{name},context=context)|reply_dependencies(expression['arguments'],metadata,functions,seen,context=context)
    if expression.get('op')=='input'and expression['path'][0]=='replies'and len(expression['path'])>1:
        return frozenset({expression['path'][1]})
    return frozenset().union(*(reply_dependencies(v,metadata,functions,seen,context=context)for v in expression.values()))


def native_dependencies(names, preprocessing, metadata, functions, seen=frozenset(),*,context=None):
    result=set()
    for name in names:
        if name not in preprocessing:result.add(name);continue
        if name in seen:raise RuleError('recursive_preprocessing:'+name)
        step=preprocessing[name]
        required=frozenset(step['requires'])|reply_dependencies(step['expression'],metadata,functions,context=context)
        result.update(native_dependencies(required,preprocessing,metadata,functions,seen|{name},context=context))
    return frozenset(result)
