"""Bounded data-only parser machine, shared by runtime and offline authoring.

No source compiler, transport, filesystem, process, plugin or driver imports.
"""
from __future__ import annotations
import hashlib
import ipaddress
import math
import operator
import re
import xml.etree.ElementTree as ET
from .expressions import RuleError

_SAFE_BUILTINS={'str':str,'int':int,'float':float,'bool':bool,'list':list,'tuple':tuple,'set':set,'frozenset':frozenset,'dict':dict,'iter':list,
    'len':len,'abs':abs,'min':min,'max':max,'sum':sum,'round':round,'sorted':sorted,
    'any':any,'all':all,'ord':ord,'ValueError':ValueError,'TypeError':TypeError,'Exception':ValueError}
_SAFE_TYPES={'$builtin:str':str,'$builtin:int':int,'$builtin:float':float,'$builtin:bool':bool,
    '$builtin:list':list,'$builtin:tuple':tuple,'$builtin:set':set,'$builtin:frozenset':frozenset,'$builtin:dict':dict}
_SAFE_METHODS={str:frozenset({'strip','lower','upper','replace','split','join','startswith','endswith','isdigit','isdecimal','rsplit','lstrip','rstrip','splitlines','encode'}),
    dict:frozenset({'get','items','keys','values','update','pop','setdefault'}),list:frozenset({'append','extend','sort','pop'}),set:frozenset({'add','update','intersection','difference'})}
_HASH_TYPE=type(hashlib.sha256())

class _Return(Exception):
    def __init__(self,value):self.value=value
class _Break(Exception):pass
class _Continue(Exception):pass
class _Function:
    def __init__(self,definition,scope):self.definition=definition;self.scope=scope
class _Member:
    def __init__(self,owner,name):self.owner=owner;self.name=name


class Machine:
    """One finite data-only replay. JSON never supplies a Python callable."""
    def __init__(self,definitions,constants=None,max_steps=500000,initializers=None,regex_budget=None,program=None):
        from .bounded_regex import RegexBudget
        self.program=program
        self.regex=regex_budget or RegexBudget()
        self.steps=0;self.max_steps=max_steps;self.depth=0
        self.scope={}
        for name,expression in (constants or {}).items():self.scope[name]=self.expression(expression,self.scope)
        self.scope.update({name:_Function(fn,self.scope) for name,fn in definitions.items()})
        self.body(initializers or [],self.scope)

    def tick(self):
        self.steps+=1
        if self.steps>self.max_steps:raise RuleError('algorithm_step_limit')

    def invoke(self,name,*args,**kwargs):return self.call(self.scope[name],list(args),kwargs)

    def call(self,function,args,kwargs):
        self.tick()
        if self.depth>=80:raise RuleError('algorithm_call_depth')
        if not isinstance(function,_Function):
            if isinstance(function,_Member):return self.member_call(function.owner,function.name,args,kwargs)
            if isinstance(function,str) and function.startswith('$builtin:'):return self.builtin(function[9:],args,kwargs)
            raise RuleError('unregistered_algorithm_function')
        p=function.definition['parameters'];scope=dict(function.scope)
        if len(args)>len(p['positional']) and not p['varargs']:raise TypeError('too_many_arguments')
        values=dict(zip(p['positional'],args))
        for name,value in kwargs.items():
            if name in values:raise TypeError('duplicate_argument')
            values[name]=value
        for name in p['positional']:
            if name not in values:
                if name not in p['defaults']:raise TypeError('missing_argument:'+name)
                values[name]=self.expression(p['defaults'][name],scope)
        for name,default in p['keywords'].items():
            if name not in values:
                if default is None:raise TypeError('missing_keyword:'+name)
                values[name]=self.expression(default,scope)
        if p['varargs']:values[p['varargs']]=tuple(args[len(p['positional']):])
        known=set(p['positional'])|set(p['keywords'])
        extra={k:v for k,v in values.items() if k not in known and k!=p['varargs']}
        if extra and not p['kwargs']:raise TypeError('unknown_keywords')
        if p['kwargs']:values[p['kwargs']]=extra
        scope.update(values);self.depth+=1
        if function.definition.get('emits'):scope['$emitted']=[]
        try:self.body(function.definition['body'],scope)
        except _Return as returned:return scope.get('$emitted') if function.definition.get('emits') else returned.value
        finally:self.depth-=1
        return scope.get('$emitted') if function.definition.get('emits') else None

    def expression(self,node,scope):
        if self.program is not None:return self.program.expressions[id(node)](self,scope)
        self.tick()
        if node is None:return None
        kind=node['kind'];ev=lambda n:self.expression(n,scope)
        if kind=='constant':return node['value']
        if kind=='name':
            name=node['name']
            if name in scope:return scope[name]
            if name in {'str','int','float','bool','list','tuple','set','frozenset','dict','len','abs','min','max','sum','round','sorted','enumerate','zip','map','filter','any','all','next','iter','range','isinstance','type','ord','Exception','ValueError','TypeError','OverflowError','ZeroDivisionError','KeyError','IndexError'}:
                return '$builtin:'+name
            if name in {'re','math','ET','hashlib','ipaddress'}:return '$module:'+name
            raise RuleError('unbound_algorithm_name:'+name)
        if kind in {'list','tuple','set'}:
            values=[ev(n) for n in node['items']]
            return tuple(values) if kind=='tuple' else set(values) if kind=='set' else values
        if kind=='dictionary':
            result={}
            for key,value in node['items']:
                if key is None:result.update(ev(value))
                else:result[ev(key)]=ev(value)
            return result
        if kind=='member':
            return self.member(ev(node['object']),node['name'])
        if kind=='slice':return slice(ev(node['start']),ev(node['stop']),ev(node['step']))
        if kind=='index':return ev(node['object'])[ev(node['key'])]
        if kind=='binary':
            from .limits import binary
            left,right=ev(node['left']),ev(node['right'])
            return binary(node['operation'],left,right)
        if kind=='unary':return {'not':operator.not_,'negative':operator.neg,'positive':operator.pos,'invert':operator.invert}[node['operation']](ev(node['value']))
        if kind=='boolean':
            result=None
            for value in node['values']:
                result=ev(value)
                if (node['operation']=='and' and not result) or (node['operation']=='or' and result):break
            return result
        if kind=='compare':
            left=ev(node['values'][0]);ops={'equal':operator.eq,'not_equal':operator.ne,'less':operator.lt,'less_equal':operator.le,
                'greater':operator.gt,'greater_equal':operator.ge,'in':lambda a,b:a in b,'not_in':lambda a,b:a not in b,
                'is':lambda a,b:(a==b if isinstance(a,str) and isinstance(b,str) and a.startswith('$builtin:') and b.startswith('$builtin:') else a is b),
                'is_not':lambda a,b:(a!=b if isinstance(a,str) and isinstance(b,str) and a.startswith('$builtin:') and b.startswith('$builtin:') else a is not b)}
            for name,right_node in zip(node['operations'],node['values'][1:]):
                right=ev(right_node)
                if not ops[name](left,right):return False
                left=right
            return True
        if kind=='conditional':return ev(node['yes'] if ev(node['condition']) else node['no'])
        if kind=='invoke':
            args=[];kwargs={}
            for arg in node['arguments']:
                if arg['kind']=='spread':args.extend(ev(arg['value']))
                else:args.append(ev(arg))
            for name,value in node['keywords']:
                if name is None:kwargs.update(ev(value))
                else:kwargs[name]=ev(value)
            return self.call(ev(node['function']),args,kwargs)
        if kind=='named_value':
            value=ev(node['value']);self.assign(node['target'],value,scope);return value
        if kind=='function':return _Function(node,dict(scope))
        if kind=='format_join':
            from .limits import join
            return join('',[ev(n)for n in node['parts']])
        if kind=='format_value':
            return self.formatted(ev(node['value']),ev(node['format']),node['conversion'])
        if kind=='comprehension':
            results=[];local=dict(scope)
            def loop(index):
                if index==len(node['loops']):
                    if len(results)>=10000:raise RuleError('collection_limit')
                    results.append((ev_local(node['key']),ev_local(node['value'])) if node['collection']=='dictionary' else ev_local(node['value']));return
                clause=node['loops'][index]
                for item in self.bounded(ev_local(clause['items'])):
                    self.assign(clause['target'],item,local)
                    if all(ev_local(v) for v in clause['conditions']):loop(index+1)
            ev_local=lambda n:self.expression(n,local)
            loop(0)
            return dict(results) if node['collection']=='dictionary' else set(results) if node['collection']=='set' else results
        raise RuleError('unknown_algorithm_expression:'+kind)

    def assign(self,target,value,scope):
        if target['kind']=='name':scope[target['name']]=value;return
        if target['kind'] in {'tuple','list'}:
            if len(target['items'])!=len(value):raise ValueError('unpack_length')
            for child,v in zip(target['items'],value):self.assign(child,v,scope)
            return
        if target['kind']=='index':
            owner=self.expression(target['object'],scope);key=self.expression(target['key'],scope)
            if isinstance(owner,dict)and key not in owner and len(owner)>=10000:raise RuleError('collection_limit')
            owner[key]=value;return
        raise RuleError('forbidden_assignment_target')

    def bounded(self,items):
        if len(items)>10000:raise RuleError('algorithm_collection_limit')
        return items

    def body(self,nodes,scope):
        if not nodes:return
        if self.program is not None:return self.program.bodies[id(nodes)](self,scope)
        for node in nodes:
            self.tick();kind=node['kind'];ev=lambda n:self.expression(n,scope)
            if kind=='return':raise _Return(ev(node['value']))
            if kind=='assign':
                value=ev(node['value'])
                for target in node['targets']:self.assign(target,value,scope)
            elif kind=='function':scope[node['name']]=_Function(node,scope)
            elif kind=='expression':ev(node['value'])
            elif kind=='emit':
                value=ev(node['value'])
                scope['$emitted'].extend(value if node['many'] else [value])
            elif kind=='if':self.body(node['yes'] if ev(node['condition']) else node['no'],scope)
            elif kind in {'for','while'}:
                broke=False
                def iterations():
                    if kind=='for':yield from self.bounded(ev(node['items']))
                    else:
                        while ev(node['condition']):self.tick();yield None
                for item in iterations():
                    if kind=='for':self.assign(node['target'],item,scope)
                    try:self.body(node['body'],scope)
                    except _Continue:continue
                    except _Break:broke=True;break
                if not broke:self.body(node['else'],scope)
            elif kind=='try':
                try:
                    try:self.body(node['body'],scope)
                    except (_Return,_Break,_Continue,RuleError):raise
                    except Exception as error:
                        for handler in node['handlers']:
                            if type(error).__name__ in handler['errors'] or 'Exception' in handler['errors']:
                                if handler['name']:scope[handler['name']]=str(error)
                                self.body(handler['body'],scope);break
                        else:raise
                    else:self.body(node['else'],scope)
                finally:self.body(node['finally'],scope)
            elif kind=='raise':
                error=ev(node['value'])
                if isinstance(error,Exception):raise error
                raise ValueError(str(error))
            elif kind=='break':raise _Break()
            elif kind=='continue':raise _Continue()
            elif kind!='pass':raise RuleError('unknown_algorithm_statement:'+kind)

    def member(self,owner,name):
        if name.startswith('__'):raise RuleError('private_algorithm_member')
        if owner=='$module:re' and name in {'IGNORECASE','I','DOTALL','S','MULTILINE','M'}:return {'IGNORECASE':2,'I':2,'DOTALL':16,'S':16,'MULTILINE':8,'M':8}[name]
        if isinstance(owner,ET.Element) and name in {'text','tag'}:return getattr(owner,name)
        if isinstance(owner,(ipaddress.IPv4Address,ipaddress.IPv6Address)) and name in {'is_unspecified','is_loopback'}:return getattr(owner,name)
        return _Member(owner,name)

    def formatted(self,value,specification,conversion):
        if conversion==114:value=repr(value)
        if conversion==115:value=str(value)
        specification=specification or''
        if not isinstance(specification,str)or len(specification)>128 or any(int(v)>100000 for v in re.findall(r'\d+',specification)):
            raise RuleError('format_limit')
        from .limits import value_limit
        return value_limit(format(value,specification))

    def builtin(self,name,args,kwargs):
        if name=='sorted' and isinstance(kwargs.get('key'),_Function):
            return sorted(args[0],key=lambda value:self.call(kwargs['key'],[value],{}),reverse=kwargs.get('reverse',False))
        if name in _SAFE_BUILTINS:return _SAFE_BUILTINS[name](*args,**kwargs)
        if name=='type':return '$builtin:'+type(args[0]).__name__
        if name=='isinstance':
            target=args[1];return isinstance(args[0],tuple(_SAFE_TYPES[v] for v in target) if isinstance(target,tuple) else _SAFE_TYPES[target])
        if name=='range':return list(self.bounded(range(*args,**kwargs)))
        if name in {'zip','enumerate'}:
            for value in (args if name=='zip'else args[:1]):self.bounded(value)
            result=list({'zip':zip,'enumerate':enumerate}[name](*args,**kwargs));return self.bounded(result)
        if name=='next':return next(iter(args[0]),args[1]) if len(args)>1 else next(iter(args[0]))
        if name in {'map','filter'}:
            return [self.call(args[0],[v],{}) for v in self.bounded(args[1])] if name=='map' else [v for v in self.bounded(args[1]) if self.call(args[0],[v],{})]
        raise RuleError('unknown_algorithm_builtin:'+name)

    def member_call(self,owner,name,args,kwargs):
        if owner=='$builtin:dict' and name=='fromkeys':
            if kwargs or len(args)not in{1,2}:raise TypeError('fromkeys_arguments')
            return dict.fromkeys(self.bounded(args[0]),args[1]if len(args)==2 else None)
        if isinstance(owner,str)and name in{'join','replace'}:
            from .limits import join,replace
            return join(owner,*args,**kwargs)if name=='join'else replace(owner,*args,**kwargs)
        if owner=='$module:ipaddress' and name in {'ip_address','IPv4Address','IPv6Address'}:
            return {'ip_address':ipaddress.ip_address,'IPv4Address':ipaddress.IPv4Address,'IPv6Address':ipaddress.IPv6Address}[name](*args,**kwargs)
        if owner=='$module:hashlib' and name=='sha256':return hashlib.sha256(*args,**kwargs)
        if isinstance(owner,_HASH_TYPE) and name=='hexdigest':return owner.hexdigest()
        if owner=='$module:math' and name in {'isfinite','log10','floor','ceil'}:return {'isfinite':math.isfinite,'log10':math.log10,'floor':math.floor,'ceil':math.ceil}[name](*args,**kwargs)
        if owner=='$module:re' and name in {'match','fullmatch','search','findall','finditer','sub','compile','escape'}:
            return self.regex.call(name,*args,**kwargs)
        if owner=='$module:ET' and name=='fromstring':
            if len(args[0])>100000 or '<!DOCTYPE' in args[0].upper():raise ValueError('unsafe_xml')
            return ET.fromstring(*args)
        if name in{'append','extend','add','update','setdefault'}and isinstance(owner,(list,set,dict)):
            extra=1 if name in{'append','add','setdefault'}else len(args[0])if args else len(kwargs)
            if len(owner)+extra>10000:raise RuleError('collection_limit')
        for cls,allowed in _SAFE_METHODS.items():
            if isinstance(owner,cls) and name in allowed:
                result=getattr(owner,name)(*args,**kwargs)
                return list(result) if name in {'items','keys','values'} else result
        if isinstance(owner,float) and name=='is_integer' and not args and not kwargs:return owner.is_integer()
        from .bounded_regex import Pattern,Match
        if isinstance(owner,Pattern) and name in {'match','fullmatch','search','findall','finditer','sub'}:
            return self.regex.member(owner,name,*args,**kwargs)
        if isinstance(owner,Match) and name in {'group','groups','start','end'}:return getattr(owner,name)(*args,**kwargs)
        if isinstance(owner,ET.Element) and name in {'find','findall','findtext','iter'}:
            result=getattr(owner,name)(*args,**kwargs);return list(result) if name=='iter' else result
        raise RuleError('forbidden_algorithm_member:'+name)
