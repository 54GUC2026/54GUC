"""Readable data expressions lowered to the single bounded rule evaluator.

This parser accepts literals and registered operation calls only. It never
compiles or executes Python; ast is used solely as a syntax tree reader.
"""
from __future__ import annotations
import ast
import json
import math
import io
import tokenize
from .vocabulary import EXPRESSION_OPERATIONS


class DefinitionError(ValueError):
    """An actionable profile pointer without echoing router/private input."""


_NAMES={'if':'choose','try':'attempt','and':'all_of','or':'any_of','not':'negate'}
_REVERSE={v:k for k,v in _NAMES.items()}
_KEYS={'as':'item_name','else':'otherwise'}
_REVERSE_KEYS={v:k for k,v in _KEYS.items()}
_BINARY={ast.Add:'add',ast.Sub:'subtract',ast.Mult:'multiply',ast.Div:'divide',
         ast.FloorDiv:'floor_divide',ast.Mod:'modulo',ast.BitAnd:'bit_and',ast.LShift:'shift_left'}
_SYMBOL={'add':'+','subtract':'-','multiply':'*','divide':'/','floor_divide':'//','modulo':'%','bit_and':'&','shift_left':'<<'}


def compile_rule(text,*,pointer='/read'):
    """Return ordinary rule data; file identity and schema stay with the loader."""
    def fail(reason):raise DefinitionError(f'{pointer}: {reason}')
    if not isinstance(text,str)or len(text)>250_000:fail('expected a bounded formula string')
    try:
        if '#'in text and any(token.type==tokenize.COMMENT for token in tokenize.generate_tokens(io.StringIO(text).readline)):
            fail('put descriptions in the description field, not formula comments')
    except(tokenize.TokenError,IndentationError):fail('invalid formula syntax')
    try:tree=ast.parse(text,mode='eval')
    except (SyntaxError,ValueError,RecursionError):fail('invalid formula syntax')
    if sum(1 for _ in ast.walk(tree))>40_000:fail('formula is too large')
    def convert(node,depth=0):
        if depth>80:fail('formula is too deeply nested')
        child=lambda n:convert(n,depth+1)
        if isinstance(node,ast.Constant):
            if type(node.value)not in(str,int,float,bool,type(None)):fail('unsupported literal')
            if isinstance(node.value,float)and not math.isfinite(node.value):fail('finite number required')
            return node.value
        if isinstance(node,ast.Name)and node.id in('null','true','false'):
            return {'null':None,'true':True,'false':False}[node.id]
        if isinstance(node,ast.List):return[child(v)for v in node.elts]
        if isinstance(node,ast.Dict):
            if any(k is None or not isinstance(k,ast.Constant)or not isinstance(k.value,str)for k in node.keys):fail('literal object keys must be text')
            keys=[k.value for k in node.keys]
            if len(keys)!=len(set(keys)):fail('duplicate object key')
            return{k.value:child(v)for k,v in zip(node.keys,node.values)}
        if isinstance(node,ast.UnaryOp)and isinstance(node.op,(ast.USub,ast.UAdd)):
            value=child(node.operand)
            if type(value)not in(int,float):fail('sign requires a numeric literal')
            return -value if isinstance(node.op,ast.USub)else value
        if isinstance(node,ast.BinOp)and type(node.op)in _BINARY:
            return{'op':_BINARY[type(node.op)],'args':[child(node.left),child(node.right)]}
        if isinstance(node,ast.Subscript)and not isinstance(node.slice,ast.Slice):
            return {'op':'get','value':child(node.value),'key':child(node.slice),'default':None}
        if not isinstance(node,ast.Call)or not isinstance(node.func,ast.Name):fail('only registered data operations are allowed')
        name=node.func.id
        if name=='use':
            if len(node.args)!=1 or not isinstance(node.args[0],ast.Constant)or not isinstance(node.args[0].value,str):
                fail('use requires one named local rule')
            if any(k.arg is None for k in node.keywords):fail('expanded keyword arguments are forbidden')
            keys=[k.arg for k in node.keywords]
            if len(set(keys))!=len(keys):fail('duplicate local rule argument')
            return {'op':'call','rule':node.args[0].value,'arguments':{k.arg:child(k.value)for k in node.keywords}}
        if name in('reply','context','profile'):
            if node.keywords:fail('source paths take positional keys only')
            path=[child(v)for v in node.args]
            if not all(type(v)in(str,int)for v in path):fail('source path keys must be text or integer')
            return{'op':'input','path':[{'reply':'replies','context':'context','profile':'profile'}[name],*path]}
        operation=_REVERSE.get(name,name)
        if operation not in EXPRESSION_OPERATIONS:fail('unknown data operation')
        if any(k.arg is None for k in node.keywords):fail('expanded keyword arguments are forbidden')
        keys=[_REVERSE_KEYS.get(k.arg,k.arg)for k in node.keywords]
        if len(keys)!=len(set(keys))or'op'in keys:fail('duplicate or reserved operation argument')
        result={'op':operation}
        if node.args:result['args']=[child(v)for v in node.args]
        result.update({key:child(k.value)for key,k in zip(keys,node.keywords)})
        return result
    try:return convert(tree.body)
    except DefinitionError:raise
    except (TypeError,ValueError,RecursionError,OverflowError):fail('invalid formula structure')


def render_rule(value):
    """Lossless authoring migration; saved readable files become the authority."""
    if isinstance(value,dict)and'op'in value:
        op=value['op']
        if op=='get'and set(value)=={'op','value','key','default'}and value['default']is None:
            return render_rule(value['value'])+'['+render_rule(value['key'])+']'
        if op=='call'and set(value)=={'op','rule','arguments'}:
            return 'use('+repr(value['rule'])+''.join(', '+k+'='+render_rule(v)for k,v in value['arguments'].items())+')'
        if op=='input'and set(value)=={'op','path'}and value['path'][0]in('replies','context','profile'):
            name={'replies':'reply','context':'context','profile':'profile'}[value['path'][0]]
            return name+'('+', '.join(render_rule(v)for v in value['path'][1:])+')'
        if op in _SYMBOL and set(value)=={'op','args'}and len(value['args'])==2:
            return '('+render_rule(value['args'][0])+' '+_SYMBOL[op]+' '+render_rule(value['args'][1])+')'
        name=_NAMES.get(op,op);args=[]
        for k,v in value.items():
            if k=='op':continue
            if k=='args'and v:args=[*(render_rule(x)for x in v),*args]
            else:args.append(_KEYS.get(k,k)+'='+render_rule(v))
        return name+'('+', '.join(args)+')'
    if isinstance(value,list):return'['+', '.join(render_rule(v)for v in value)+']'
    if isinstance(value,dict):return'{'+', '.join(json.dumps(k)+': '+render_rule(v)for k,v in value.items())+'}'
    # Single-quoted source keys stay readable inside the outer JSON string.
    if isinstance(value,str):return repr(value)
    return json.dumps(value,ensure_ascii=False,allow_nan=False)
