"""Canonical output admission; a malformed optional field stays unavailable."""
from dataclasses import dataclass
import math
import re
from decimal import Decimal
from .expressions import RuleError


@dataclass(frozen=True,slots=True)
class FieldIssue:
    address:str
    pointer:str
    reason:str


def native_scalar(value,kind,*,unit=None,address=''):
    """Accept equivalent native forms before strict canonical admission.

    No unit conversion, inferred measurement, free-text rewriting or truthiness.
    A bad suffix/boolean/band family still fails ordinary type admission.
    """
    if kind in {'number','integer'} and isinstance(value,str):
        text=value.strip()
        if len(text)>128:return value
        if kind=='integer' and unit in {'LTE band number','NR band number'}:
            prefix='n' if unit=='NR band number' else 'b'
            if re.fullmatch(prefix+r'\d+',text,re.IGNORECASE):text=text[1:]
        match=re.fullmatch(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z%/]+)?',text)
        if match and (not match[2] or unit and match[2].casefold()==unit.casefold()):
            if kind=='integer':
                parsed=Decimal(match[1])
                if parsed.is_finite() and abs(parsed.adjusted())<=308 and parsed==parsed.to_integral_value():value=int(parsed)
            else:value=float(match[1])
    elif kind=='boolean':
        if type(value) is int and value in (0,1):return bool(value)
        if isinstance(value,str):
            forms={'true':True,'false':False,'yes':True,'no':False,'on':True,'off':False,'1':True,'0':False}
            return forms.get(value.strip().casefold(),value)
    elif kind=='array_integer' and isinstance(value,(list,tuple)):
        prefix='n' if address.startswith(('nr.','profile.nr')) else 'b'
        normalized=[]
        for item in value:
            if isinstance(item,str):
                text=item.strip()
                if re.fullmatch(r'\d+',text):item=int(text)
                elif re.fullmatch(prefix+r'\d+',text,re.IGNORECASE):item=int(text[1:])
            normalized.append(item)
        return normalized
    if kind in {'object','array_object','number_or_string_or_object'}:
        def plain(item):
            if isinstance(item,dict):return {key:plain(child)for key,child in item.items()}
            if isinstance(item,(list,tuple)):return [plain(child)for child in item]
            return item
        return plain(value)
    return value


def admit(value,kind):
    if value is None:return None
    numeric=lambda v:type(v)in(int,float)and math.isfinite(v)
    integral=lambda v:numeric(v)and int(v)==v
    if kind=='number'and numeric(value):return value
    if kind=='integer'and integral(value):return int(value)
    if kind=='boolean'and type(value)is bool:return value
    if kind=='string'and isinstance(value,str):return value
    if kind=='integer_or_string'and(isinstance(value,str)or integral(value)):return value
    if kind=='array_integer'and isinstance(value,(list,tuple))and all(integral(v)for v in value):return[int(v)for v in value]
    if kind=='array_object'and isinstance(value,(list,tuple))and all(isinstance(v,dict)for v in value):return list(value)
    if kind=='object'and isinstance(value,dict):return value
    if kind=='number_or_string_or_object'and(numeric(value)or isinstance(value,(str,dict))):return value
    raise RuleError('canonical_output_type')
