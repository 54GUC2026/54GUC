"""Offline authoring compiler; replay uses the one runtime-owned machine."""
import ast
from arch4.modem_live.profile_definition.expressions import RuleError
from arch4.modem_live.profile_definition.algorithm import Machine

def compile_node(node):
    """Canonical JSON syntax uses explicit operations, never source snippets."""
    if node is None: return None
    if isinstance(node, ast.Constant): return {'kind':'constant','value':node.value}
    if isinstance(node, ast.Name): return {'kind':'name','name':node.id}
    if isinstance(node, (ast.List,ast.Tuple,ast.Set)):
        return {'kind':{ast.List:'list',ast.Tuple:'tuple',ast.Set:'set'}[type(node)],'items':[compile_node(v) for v in node.elts]}
    if isinstance(node, ast.Dict):
        return {'kind':'dictionary','items':[[compile_node(k),compile_node(v)] for k,v in zip(node.keys,node.values)]}
    if isinstance(node, ast.Attribute):
        if node.attr.startswith('__'): raise RuleError('private_attribute')
        return {'kind':'member','object':compile_node(node.value),'name':node.attr}
    if isinstance(node, ast.Subscript): return {'kind':'index','object':compile_node(node.value),'key':compile_node(node.slice)}
    if isinstance(node, ast.Slice): return {'kind':'slice','start':compile_node(node.lower),'stop':compile_node(node.upper),'step':compile_node(node.step)}
    if isinstance(node, ast.Call):
        return {'kind':'invoke','function':compile_node(node.func),'arguments':[compile_node(a) for a in node.args],
            'keywords':[[k.arg,compile_node(k.value)] for k in node.keywords]}
    if isinstance(node, ast.Starred):return {'kind':'spread','value':compile_node(node.value)}
    if isinstance(node, ast.BinOp):
        names={ast.Add:'add',ast.Sub:'subtract',ast.Mult:'multiply',ast.Div:'divide',ast.FloorDiv:'floor_divide',
            ast.Mod:'modulo',ast.Pow:'power',ast.BitOr:'bit_or',ast.BitAnd:'bit_and',ast.LShift:'shift_left',ast.RShift:'shift_right'}
        if type(node.op) not in names:raise RuleError('unsupported_binary')
        return {'kind':'binary','operation':names[type(node.op)],'left':compile_node(node.left),'right':compile_node(node.right)}
    if isinstance(node, ast.UnaryOp):
        names={ast.Not:'not',ast.USub:'negative',ast.UAdd:'positive',ast.Invert:'invert'}
        return {'kind':'unary','operation':names[type(node.op)],'value':compile_node(node.operand)}
    if isinstance(node, ast.BoolOp):return {'kind':'boolean','operation':'and' if isinstance(node.op,ast.And) else 'or','values':[compile_node(v) for v in node.values]}
    if isinstance(node, ast.Compare):
        names={ast.Eq:'equal',ast.NotEq:'not_equal',ast.Lt:'less',ast.LtE:'less_equal',ast.Gt:'greater',ast.GtE:'greater_equal',
            ast.In:'in',ast.NotIn:'not_in',ast.Is:'is',ast.IsNot:'is_not'}
        return {'kind':'compare','operations':[names[type(v)] for v in node.ops],
            'values':[compile_node(node.left)]+[compile_node(v) for v in node.comparators]}
    if isinstance(node, ast.IfExp):return {'kind':'conditional','condition':compile_node(node.test),'yes':compile_node(node.body),'no':compile_node(node.orelse)}
    if isinstance(node, (ast.ListComp,ast.SetComp,ast.DictComp,ast.GeneratorExp)):
        if any(g.is_async for g in node.generators):raise RuleError('async_parser_forbidden')
        return {'kind':'comprehension','collection':'dictionary' if isinstance(node,ast.DictComp) else 'set' if isinstance(node,ast.SetComp) else 'list',
            'loops':[{'target':compile_node(g.target),'items':compile_node(g.iter),'conditions':[compile_node(c) for c in g.ifs]} for g in node.generators],
            'value':compile_node(node.value if isinstance(node,ast.DictComp) else node.elt),
            'key':compile_node(node.key) if isinstance(node,ast.DictComp) else None}
    if isinstance(node, ast.NamedExpr):return {'kind':'named_value','target':compile_node(node.target),'value':compile_node(node.value)}
    if isinstance(node, ast.JoinedStr):
        return {'kind':'format_join','parts':[compile_node(v) for v in node.values]}
    if isinstance(node, ast.FormattedValue):return {'kind':'format_value','value':compile_node(node.value),'format':compile_node(node.format_spec),'conversion':node.conversion}
    if isinstance(node, ast.Lambda):return {'kind':'function','parameters':parameters(node.args),'body':[{'kind':'return','value':compile_node(node.body)}]}
    if isinstance(node, ast.FunctionDef):
        return {'kind':'function','name':node.name,'parameters':parameters(node.args),'body':compile_body(node.body),
            'emits':any(isinstance(n,(ast.Yield,ast.YieldFrom)) for n in ast.walk(node))}
    if isinstance(node, ast.Return):return {'kind':'return','value':compile_node(node.value)}
    if isinstance(node, ast.Assign):return {'kind':'assign','targets':[compile_node(v) for v in node.targets],'value':compile_node(node.value)}
    if isinstance(node, ast.AnnAssign):return {'kind':'assign','targets':[compile_node(node.target)],'value':compile_node(node.value)}
    if isinstance(node, ast.AugAssign):
        return {'kind':'assign','targets':[compile_node(node.target)],'value':compile_node(ast.BinOp(node.target,node.op,node.value))}
    if isinstance(node, ast.Expr) and isinstance(node.value,(ast.Yield,ast.YieldFrom)):
        return {'kind':'emit','many':isinstance(node.value,ast.YieldFrom),'value':compile_node(node.value.value)}
    if isinstance(node, ast.Expr):return {'kind':'expression','value':compile_node(node.value)}
    if isinstance(node, ast.If):return {'kind':'if','condition':compile_node(node.test),'yes':compile_body(node.body),'no':compile_body(node.orelse)}
    if isinstance(node, ast.For):return {'kind':'for','target':compile_node(node.target),'items':compile_node(node.iter),'body':compile_body(node.body),'else':compile_body(node.orelse)}
    if isinstance(node, ast.While):return {'kind':'while','condition':compile_node(node.test),'body':compile_body(node.body),'else':compile_body(node.orelse)}
    if isinstance(node, (ast.Break,ast.Continue,ast.Pass)):
        return {'kind':{ast.Break:'break',ast.Continue:'continue',ast.Pass:'pass'}[type(node)]}
    if isinstance(node, ast.Try):
        return {'kind':'try','body':compile_body(node.body),'handlers':[{'errors':exception_names(h.type),'name':h.name,'body':compile_body(h.body)} for h in node.handlers],
            'else':compile_body(node.orelse),'finally':compile_body(node.finalbody)}
    if isinstance(node, ast.Raise):return {'kind':'raise','value':compile_node(node.exc)}
    # Pure helpers can declare local stdlib imports. Resolution is fixed by the
    # replay vocabulary, never by an import instruction or filesystem lookup.
    if isinstance(node, ast.Import) and all(a.name in {'math','re'} and not a.asname for a in node.names):return {'kind':'pass'}
    raise RuleError('unsupported_parser_syntax:'+type(node).__name__)


def exception_names(node):
    if node is None:return ['Exception']
    if isinstance(node,ast.Tuple):return [name for n in node.elts for name in exception_names(n)]
    if isinstance(node,ast.Name):return [node.id]
    if isinstance(node,ast.Attribute) and isinstance(node.value,ast.Name) and node.value.id=='ET':return ['ParseError']
    raise RuleError('unsupported_exception_type')


def parameters(args):
    names=[a.arg for a in args.posonlyargs+args.args]
    return {'positional':names,'defaults':{n:compile_node(v) for n,v in zip(names[-len(args.defaults):],args.defaults)} if args.defaults else {},
        'keywords':{a.arg:compile_node(v) for a,v in zip(args.kwonlyargs,args.kw_defaults)},
        'varargs':args.vararg.arg if args.vararg else None,'kwargs':args.kwarg.arg if args.kwarg else None}


def compile_body(body):
    return [compile_node(n) for n in body if not (isinstance(n,ast.Expr) and isinstance(n.value,ast.Constant) and isinstance(n.value.value,str))]
