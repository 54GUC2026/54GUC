"""Read-only native lookup tolerance; canonical output and raw evidence stay exact."""
from .expressions import RuleError
from contextlib import contextmanager
from contextvars import ContextVar
import re

_native_issues=ContextVar('native_input_issues',default=None)
_native_aliases=ContextVar('native_declared_key_aliases',default={})


def native_status_code(value):
    """Only a bounded integer status, never bool, a unit or arbitrary text."""
    if type(value) is str and re.fullmatch(r'[-+]?[0-9]{1,10}', value.strip()):
        value = int(value.strip())
    if type(value) is int and -(2**31) <= value <= 2**32-1:
        return value
    raise RuleError('native_status_code_invalid')


@contextmanager
def native_issue_scope(aliases=None):
    reasons=[];token=_native_issues.set(reasons)
    alias_token=_native_aliases.set(aliases or {})
    try:yield reasons
    finally:
        _native_aliases.reset(alias_token)
        _native_issues.reset(token)


def _conflict(reason):
    reasons=_native_issues.get()
    if reasons is not None:reasons.append(reason)
    raise RuleError(reason)


class NativeMapping(dict):
    def __deepcopy__(self,memo):
        from copy import deepcopy
        # Preserve contradictory raw aliases until a selected field rejects
        # them. Reconstructing through __setitem__ would silently merge them.
        result=NativeMapping((deepcopy(k,memo),deepcopy(v,memo))for k,v in dict.items(self))
        memo[id(self)]=result
        return result
    def copy(self):return NativeMapping(self)
    def _key(self,key):
        if not isinstance(key,str):return key
        matches=[actual for actual in self if isinstance(actual,str) and actual.casefold()==key.casefold()]
        if not matches:return key
        # Some firmware deliberately exposes two independently validated names
        # differing only by case. Do not collapse their units/zero/conflict
        # checks. Unknown case variants still use unambiguous lookup below.
        declared=_native_aliases.get().get(key.casefold(),())
        if key in declared and all(actual in declared for actual in matches):return key
        if len(matches)>1 and any(dict.__getitem__(self,k)!=dict.__getitem__(self,matches[0]) for k in matches[1:]):
            _conflict('conflicting_native_key_case')
        return key if dict.__contains__(self,key) else matches[0]

    def __getitem__(self,key):return dict.__getitem__(self,self._key(key))
    def __setitem__(self,key,value):dict.__setitem__(self,self._key(key),value)
    def get(self,key,default=None):return dict.get(self,self._key(key),default)
    def __contains__(self,key):return dict.__contains__(self,self._key(key))
    def setdefault(self,key,default=None):return dict.setdefault(self,self._key(key),default)
    def pop(self,key,*default):return dict.pop(self,self._key(key),*default)
    def update(self,*args,**kwargs):
        for key,value in dict(*args,**kwargs).items():self[key]=value


def native_view(value):
    if isinstance(value,dict):return NativeMapping((k,native_view(v)) for k,v in value.items())
    if isinstance(value,list):return [native_view(v) for v in value]
    if isinstance(value,tuple):return tuple(native_view(v) for v in value)
    return value


def native_contains(key,collection):
    return key in NativeMapping(collection) if isinstance(collection,dict) else key in collection


def xml_find(element,path,*,many=False):
    """Case-tolerant simple tag paths; leave predicates/namespaces to ET."""
    if not isinstance(path,str) or any(c in path for c in "[@{:'") or '//' in path.removeprefix('.//'):
        return element.findall(path) if many else element.find(path)
    descendants=path.startswith('.//')
    nodes=[element]
    for index,name in enumerate(path.removeprefix('.//').removeprefix('./').split('/')):
        if name=='.':continue
        matches=[]
        for node in nodes:
            children=(child for child in node.iter()if child is not node)if descendants and index==0 else node
            found=[child for child in children if child.tag.casefold()==name.casefold()]
            if not many and len(found)>1 and any(
                    (child.text,child.attrib)!= (found[0].text,found[0].attrib) for child in found[1:]):
                _conflict('conflicting_native_xml_case')
            matches.extend(found)
        nodes=matches
    return nodes if many else (nodes[0] if nodes else None)
