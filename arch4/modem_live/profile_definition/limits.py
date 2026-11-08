"""Allocation guards shared by the two data-expression forms.

Check expansion before invoking Python operators; a step counter alone cannot
bound a single multiplication, join, shift or range allocation.
"""
import math
import operator
from .expressions import RuleError

MAX_TEXT=100_000
MAX_ITEMS=10_000
MAX_BITS=4096


def value_limit(value):
    if isinstance(value,(str,bytes))and len(value)>MAX_TEXT:raise RuleError('text_limit')
    if isinstance(value,(list,tuple,set,frozenset,dict,range))and len(value)>MAX_ITEMS:raise RuleError('collection_limit')
    if type(value)is int and value.bit_length()>MAX_BITS:raise RuleError('integer_operation_limit')
    if type(value)is float and not math.isfinite(value):raise RuleError('finite_result_required')
    return value


def binary(name,left,right):
    sequence=(str,bytes,list,tuple)
    if name=='add'and isinstance(left,sequence)and isinstance(right,type(left)):
        maximum=MAX_TEXT if isinstance(left,(str,bytes))else MAX_ITEMS
        if len(left)+len(right)>maximum:raise RuleError('expansion_limit')
    if name=='multiply':
        seq,count=(left,right)if isinstance(left,sequence)else(right,left)
        if isinstance(seq,sequence)and isinstance(count,int):
            maximum=MAX_TEXT if isinstance(seq,(str,bytes))else MAX_ITEMS
            if len(seq)*max(0,count)>maximum:raise RuleError('expansion_limit')
    if name in{'shift_left','shift_right'}:
        if type(right)is not int or not 0<=right<=MAX_BITS:raise RuleError('integer_operation_limit')
        if name=='shift_left'and type(left)is int and left.bit_length()+right>MAX_BITS:raise RuleError('integer_operation_limit')
    if name=='power':
        if abs(right)>MAX_BITS:raise RuleError('integer_operation_limit')
        if type(left)is int and type(right)is int and right>=0 and max(1,abs(left).bit_length()-1)*right>MAX_BITS:
            raise RuleError('integer_operation_limit')
    if name=='modulo'and isinstance(left,str):
        # Native programs do not use arbitrary printf widths. Keep that
        # allocation surface closed rather than formatting before checking.
        raise RuleError('text_printf_forbidden')
    operations={'add':operator.add,'subtract':operator.sub,'multiply':operator.mul,'divide':operator.truediv,
        'floor_divide':operator.floordiv,'modulo':operator.mod,'power':operator.pow,'bit_or':operator.or_,
        'bit_and':operator.and_,'shift_left':operator.lshift,'shift_right':operator.rshift}
    return value_limit(operations[name](left,right))


def join(separator,items):
    value_limit(items)
    strings=[str(value_limit(v))for v in items]
    if sum(map(len,strings))+len(separator)*max(0,len(strings)-1)>MAX_TEXT:raise RuleError('expansion_limit')
    return separator.join(strings)


def replace(text,old,new,count=-1):
    occurrences=text.count(old)if count<0 else min(count,text.count(old))
    if len(text)+occurrences*(len(new)-len(old))>MAX_TEXT:raise RuleError('expansion_limit')
    return text.replace(old,new,count)
