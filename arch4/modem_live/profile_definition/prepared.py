"""Prepare the closed parser vocabulary once; never compile supplied source.

Closures bind only operations already owned by Machine. Every invocation keeps
its own scope, work counter and mutable results. The JSON remains the export.
"""
import operator

from .algorithm import _Return, _Break, _Continue, _Function
from .expressions import RuleError
from .limits import binary, join

_UNARY={'not':operator.not_,'negative':operator.neg,'positive':operator.pos,'invert':operator.invert}
_COMPARE={'equal':operator.eq,'not_equal':operator.ne,'less':operator.lt,'less_equal':operator.le,
    'greater':operator.gt,'greater_equal':operator.ge,'in':lambda a,b:a in b,'not_in':lambda a,b:a not in b,
    'is':lambda a,b:(a==b if isinstance(a,str) and isinstance(b,str) and a.startswith('$builtin:') and b.startswith('$builtin:') else a is b),
    'is_not':lambda a,b:(a!=b if isinstance(a,str) and isinstance(b,str) and a.startswith('$builtin:') and b.startswith('$builtin:') else a is not b)}
_BUILTINS=frozenset('str int float bool list tuple set frozenset dict len abs min max sum round sorted enumerate zip map filter any all next iter range isinstance type ord Exception ValueError TypeError OverflowError ZeroDivisionError KeyError IndexError'.split())
_MODULES=frozenset('re math ET hashlib ipaddress'.split())


def _charged(operation):
    def run(machine,scope):
        machine.tick()
        return operation(machine,scope)
    return run


class PreparedAlgorithms:
    """Private, load-time dispatch plan for the existing data-only machine."""
    def __init__(self,library):
        self.expressions={};self.bodies={}
        for value in library['constants'].values():self.expression(value)
        for fn in library['functions'].values():self.function(fn)
        self.body(library.get('initializers',[]))

    def function(self,node):
        parameters=node['parameters']
        for value in parameters['defaults'].values():self.expression(value)
        for value in parameters['keywords'].values():
            if value is not None:self.expression(value)
        self.body(node['body'])

    def expression(self,node):
        key=id(node)
        if key not in self.expressions:self.expressions[key]=_charged(self._expression(node))
        return self.expressions[key]

    def _expression(self,node):
        if node is None:return lambda m,s:None
        kind=node['kind'];compile_=self.expression
        if kind=='constant':
            value=node['value'];return lambda m,s:value
        if kind=='name':
            name=node['name']
            def name_value(m,s):
                if name in s:return s[name]
                if name in _BUILTINS:return '$builtin:'+name
                if name in _MODULES:return '$module:'+name
                raise RuleError('unbound_algorithm_name:'+name)
            return name_value
        if kind in {'list','tuple','set'}:
            values=tuple(map(compile_,node['items']))
            convert={'list':list,'tuple':tuple,'set':set}[kind]
            return lambda m,s:convert(v(m,s) for v in values)
        if kind=='dictionary':
            pairs=tuple((compile_(k) if k is not None else None,compile_(v))for k,v in node['items'])
            def dictionary(m,s):
                result={}
                for key,value in pairs:
                    if key is None:result.update(value(m,s))
                    else:result[key(m,s)]=value(m,s)
                return result
            return dictionary
        if kind=='member':
            owner=compile_(node['object']);name=node['name']
            # Reuse the same closed member/attribute owner, including private
            # name rejection and non-callable XML/IP/regex flag properties.
            return lambda m,s:m.member(owner(m,s),name)
        if kind=='slice':
            start,stop,step=(compile_(node[k]) for k in ('start','stop','step'))
            return lambda m,s:slice(start(m,s),stop(m,s),step(m,s))
        if kind=='index':
            owner,key=compile_(node['object']),compile_(node['key'])
            return lambda m,s:owner(m,s)[key(m,s)]
        if kind=='binary':
            left,right=compile_(node['left']),compile_(node['right']);op=node['operation']
            return lambda m,s:binary(op,left(m,s),right(m,s))
        if kind=='unary':
            value=compile_(node['value']);operation=_UNARY[node['operation']]
            return lambda m,s:operation(value(m,s))
        if kind=='boolean':
            values=tuple(map(compile_,node['values']));is_and=node['operation']=='and'
            def boolean(m,s):
                result=None
                for value in values:
                    result=value(m,s)
                    if (is_and and not result)or(not is_and and result):break
                return result
            return boolean
        if kind=='compare':
            first=compile_(node['values'][0]);rest=tuple(zip((_COMPARE[k]for k in node['operations']),map(compile_,node['values'][1:])))
            def compare(m,s):
                left=first(m,s)
                for operation,value in rest:
                    right=value(m,s)
                    if not operation(left,right):return False
                    left=right
                return True
            return compare
        if kind=='conditional':
            condition,yes,no=(compile_(node[k])for k in ('condition','yes','no'))
            return lambda m,s:yes(m,s)if condition(m,s)else no(m,s)
        if kind=='invoke':
            fn=node['function'];args=node['arguments']
            if (fn.get('kind')=='member'and fn.get('object')=={'kind':'name','name':'re'}
                    and fn['name']!='escape'and args and args[0].get('kind')=='constant'):
                from .bounded_regex import compile_pattern
                compile_pattern(args[0]['value'])
            function=compile_(node['function'])
            arguments=tuple((v['kind']=='spread',compile_(v['value']if v['kind']=='spread'else v))for v in node['arguments'])
            keywords=tuple((k,compile_(v))for k,v in node['keywords'])
            def invoke(m,s):
                args=[];kwargs={}
                for spread,value in arguments:
                    if spread:args.extend(value(m,s))
                    else:args.append(value(m,s))
                for name,value in keywords:
                    if name is None:kwargs.update(value(m,s))
                    else:kwargs[name]=value(m,s)
                return m.call(function(m,s),args,kwargs)
            return invoke
        if kind=='named_value':
            value=compile_(node['value']);target=self.target(node['target'])
            def named(m,s):
                result=value(m,s);target(m,s,result);return result
            return named
        if kind=='function':
            self.function(node);return lambda m,s:_Function(node,dict(s))
        if kind=='format_join':
            parts=tuple(map(compile_,node['parts']))
            return lambda m,s:join('',[v(m,s)for v in parts])
        if kind=='format_value':
            value,specification=compile_(node['value']),compile_(node['format']);conversion=node['conversion']
            return lambda m,s:m.formatted(value(m,s),specification(m,s),conversion)
        if kind=='comprehension':
            loops=tuple((self.target(v['target']),compile_(v['items']),tuple(map(compile_,v['conditions'])))for v in node['loops'])
            collection=node['collection'];value=compile_(node['value'])
            key=compile_(node['key'])if collection=='dictionary'else None
            def comprehension(m,s):
                result=[];local=dict(s)
                def loop(index):
                    if index==len(loops):
                        if len(result)>=10000:raise RuleError('collection_limit')
                        result.append((key(m,local),value(m,local))if key else value(m,local));return
                    target,items,conditions=loops[index]
                    for item in m.bounded(items(m,local)):
                        target(m,local,item)
                        if all(condition(m,local)for condition in conditions):loop(index+1)
                loop(0)
                return dict(result)if collection=='dictionary'else set(result)if collection=='set'else result
            return comprehension
        raise RuleError('unknown_algorithm_expression:'+kind)

    def target(self,node):
        kind=node['kind']
        if kind=='name':
            name=node['name']
            def assign(m,s,value):s[name]=value
            return assign
        if kind in {'tuple','list'}:
            children=tuple(map(self.target,node['items']))
            def unpack(m,s,value):
                if len(children)!=len(value):raise ValueError('unpack_length')
                for child,item in zip(children,value):child(m,s,item)
            return unpack
        if kind=='index':
            owner,key=self.expression(node['object']),self.expression(node['key'])
            def index(m,s,value):
                target=owner(m,s);at=key(m,s)
                if isinstance(target,dict)and at not in target and len(target)>=10000:raise RuleError('collection_limit')
                target[at]=value
            return index
        raise RuleError('forbidden_assignment_target')

    def body(self,nodes):
        key=id(nodes)
        if key not in self.bodies:
            statements=tuple(_charged(self.statement(n))for n in nodes)
            def run(m,s):
                for statement in statements:statement(m,s)
            self.bodies[key]=run
        return self.bodies[key]

    def statement(self,node):
        kind=node['kind'];compile_=self.expression
        if kind=='return':
            value=compile_(node['value'])
            def returned(m,s):raise _Return(value(m,s))
            return returned
        if kind=='assign':
            value=compile_(node['value']);targets=tuple(map(self.target,node['targets']))
            def assign(m,s):
                result=value(m,s)
                for target in targets:target(m,s,result)
            return assign
        if kind=='function':
            self.function(node);name=node['name']
            def function(m,s):s[name]=_Function(node,s)
            return function
        if kind=='expression':return compile_(node['value'])
        if kind=='emit':
            value=compile_(node['value']);many=node['many']
            def emit(m,s):
                result=value(m,s);s['$emitted'].extend(result if many else[result])
            return emit
        if kind=='if':
            condition=compile_(node['condition']);yes,no=self.body(node['yes']),self.body(node['no'])
            return lambda m,s:yes(m,s)if condition(m,s)else no(m,s)
        if kind in {'for','while'}:
            body,otherwise=self.body(node['body']),self.body(node['else'])
            items=compile_(node['items']if kind=='for'else node['condition'])
            target=self.target(node['target'])if kind=='for'else None
            def repeated(m,s):
                def iterations():
                    if target:yield from m.bounded(items(m,s))
                    else:
                        while items(m,s):m.tick();yield None
                for item in iterations():
                    if target:target(m,s,item)
                    try:body(m,s)
                    except _Continue:continue
                    except _Break:break
                else:otherwise(m,s)
            return repeated
        if kind=='try':
            body,otherwise,finally_=(self.body(node[k])for k in ('body','else','finally'))
            handlers=tuple((v['errors'],v['name'],self.body(v['body']))for v in node['handlers'])
            def tried(m,s):
                try:
                    try:body(m,s)
                    except (_Return,_Break,_Continue,RuleError):raise
                    except Exception as error:
                        for errors,name,handler in handlers:
                            if type(error).__name__ in errors or'Exception'in errors:
                                if name:s[name]=str(error)
                                handler(m,s);break
                        else:raise
                    else:otherwise(m,s)
                finally:finally_(m,s)
            return tried
        if kind=='raise':
            value=compile_(node['value'])
            def raised(m,s):
                error=value(m,s)
                if isinstance(error,Exception):raise error
                raise ValueError(str(error))
            return raised
        if kind=='break':
            def break_(m,s):raise _Break()
            return break_
        if kind=='continue':
            def continue_(m,s):raise _Continue()
            return continue_
        if kind=='pass':return lambda m,s:None
        raise RuleError('unknown_algorithm_statement:'+kind)
