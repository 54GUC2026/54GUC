"""Timed regex owner for editable rules; never used by transport code.

Version0 preserves Python re semantics. One evaluator shares one total budget
with its data-only algorithms. Profiles cannot raise any limit.
"""
import re as std_re
from time import perf_counter
import regex
from .expressions import RuleError
from .limits import value_limit,MAX_TEXT,MAX_ITEMS

Pattern=regex.Pattern
Match=regex.Match


def compile_pattern(pattern,flags=0):
    if not isinstance(pattern,str)or len(pattern)>512:raise RuleError('regex_pattern_limit')
    # A short source can still request an enormous compiled repeat program.
    for repeat in std_re.findall(r'\{(\d+)(?:,(\d*))?\}',pattern):
        if any(value and int(value)>MAX_TEXT for value in repeat):raise RuleError('regex_pattern_limit')
    translated=regex.VERSION0
    for old,new in[(std_re.I,regex.I),(std_re.M,regex.M),(std_re.S,regex.S),(std_re.X,regex.X),(std_re.A,regex.A),(std_re.U,regex.U)]:
        if flags&old:translated|=new
    try:return regex.compile(pattern,translated)
    except regex.error:raise RuleError('invalid_regex_pattern')from None


class RegexBudget:
    def __init__(self):self.remaining=0.25
    def call(self,name,*args,**kwargs):
        if name=='escape':return value_limit(regex.escape(*args,**kwargs))
        if name=='compile':return compile_pattern(*args,**kwargs)
        flags=kwargs.pop('flags',0)
        # re module positional flags follow count for sub, string otherwise.
        base=3 if name=='sub'else 2
        if len(args)>base:
            values=list(args)
            index=4 if name=='sub'else 2
            if len(values)>index:flags=values.pop(index)
            args=tuple(values)
        pattern=compile_pattern(args[0],flags)
        return self.member(pattern,name,*args[1:],**kwargs)
    def member(self,pattern,name,*args,**kwargs):
        if self.remaining<=0:raise RuleError('regex_time_limit')
        # Reject profile-controlled escape hatches to native timeout/options.
        if set(kwargs)-{'pos','endpos','count'}:raise RuleError('regex_option_forbidden')
        text=args[1]if name=='sub'else args[0]
        if not isinstance(text,str)or len(text)>MAX_TEXT:raise RuleError('text_limit')
        if name=='sub':
            replacement=args[0]
            if not isinstance(replacement,str):raise RuleError('regex_replacement_text_required')
            count=args[2]if len(args)>2 else kwargs.get('count',0)
            bound=len(text)+1 if count==0 else min(count,len(text)+1)
            if len(text)+bound*len(replacement)>MAX_TEXT:raise RuleError('expansion_limit')
        started=perf_counter()
        try:
            options={**kwargs,'timeout':min(0.05,self.remaining),'concurrent':True}
            # Consume iterators inside the timed operation; bound retained results.
            if name in{'finditer','findall'}:
                matches=[]
                for match in pattern.finditer(*args,**options):
                    if len(matches)>=MAX_ITEMS:raise RuleError('collection_limit')
                    if name=='finditer':matches.append(match)
                    elif pattern.groups==0:matches.append(match.group())
                    elif pattern.groups==1:matches.append(match.group(1)or'')
                    else:matches.append(tuple(v or''for v in match.groups()))
                return matches
            return value_limit(getattr(pattern,name)(*args,**options))
        except TimeoutError:raise RuleError('regex_time_limit')from None
        finally:self.remaining-=perf_counter()-started
