"""Canonical output admission; a malformed optional field stays unavailable."""
from dataclasses import dataclass
import math
from .expressions import RuleError


@dataclass(frozen=True,slots=True)
class FieldIssue:
    address:str
    pointer:str
    reason:str


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
