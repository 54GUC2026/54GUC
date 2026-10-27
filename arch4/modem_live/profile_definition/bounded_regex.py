"""Prepared regex owner for editable rules; never used by transport code.

Version0 preserves Python re semantics. Pattern structure and allocation bounds
are independent of CPU speed. Valid matching has no elapsed-time deadline.
"""
import re as std_re
from re import _parser as pattern_parser
from functools import lru_cache
import regex
from .expressions import RuleError
from .limits import value_limit,MAX_TEXT,MAX_ITEMS

Pattern=regex.Pattern
Match=regex.Match


def compile_pattern(pattern,flags=0):
    if not isinstance(pattern,str)or len(pattern)>512:raise RuleError('regex_pattern_limit')
    return _compile_pattern(pattern,int(flags))


@lru_cache(maxsize=512)
def _compile_pattern(pattern,flags):
    # A short source can still request an enormous compiled repeat program.
    for repeat in std_re.findall(r'\{(\d+)(?:,(\d*))?\}',pattern):
        if any(value and int(value)>MAX_TEXT for value in repeat):raise RuleError('regex_pattern_limit')
    # Reject ambiguous repeated programs before matching. Editable patterns
    # cannot introduce nested variable repetition, repeated alternatives or
    # backreferences. This structural admission never measures elapsed time.
    try:syntax=pattern_parser.parse(pattern,flags)
    except std_re.error:raise RuleError('invalid_regex_pattern')from None
    def choices(nodes):
        """Conservative bound on finite backtracking choices, not time."""
        total=1
        for operation,value in nodes:
            name=str(operation);count=1
            if name in {'MAX_REPEAT','MIN_REPEAT','POSSESSIVE_REPEAT'}:
                minimum,maximum,body=value
                if maximum>64:return MAX_ITEMS+1
                inner=choices(body)
                count=1
                for _ in range(maximum):
                    count*=inner
                    if count>MAX_ITEMS:return count
                count*=maximum-minimum+1
            elif name=='SUBPATTERN':count=choices(value[-1])
            elif name=='BRANCH':count=sum(choices(branch)for branch in value[1])
            elif name in {'ASSERT','ASSERT_NOT'}:count=choices(value[1])
            total*=count
            if total>MAX_ITEMS:return total
        return total
    def alphabet(operation,value):
        name=str(operation)
        if name=='LITERAL':
            if value>127:return None
            return {ord(c)for c in chr(value).lower()+chr(value).upper()}
        if name=='CATEGORY':
            category=str(value)
            if category=='CATEGORY_DIGIT':return set(range(48,58))|{'unicode_digit'}
            if category=='CATEGORY_SPACE':return {9,10,11,12,13,32,'unicode_space'}
            return None
        if name=='IN':
            result=set()
            for op,item in value:
                if str(op)=='RANGE':
                    if item[1]-item[0]>512:return None
                    for code in range(item[0],item[1]+1):
                        chars=alphabet(pattern_parser.LITERAL,code)
                        if chars is None:return None
                        result.update(chars)
                else:
                    chars=alphabet(op,item)
                    if chars is None:return None
                    result.update(chars)
            return result
        return None
    def delimited(nodes):
        # E.g. (digits+,)+ and (separator+word+)* have unique boundaries.
        # A single (a+)+ has no boundary and is never admitted here.
        segments=[]
        def visit(items):
            for op,value in items:
                name=str(op)
                if name=='SUBPATTERN':
                    if not visit(value[-1]):return False
                    continue
                if name in {'MAX_REPEAT','MIN_REPEAT'}:
                    minimum,_,body=value
                    if minimum<1 or len(body)!=1:return False
                    chars=alphabet(*body[0])
                else:chars=alphabet(op,value)
                if chars is None:return False
                segments.append(chars)
            return True
        return visit(nodes)and len(segments)>1 and all(not a&b for a,b in zip(segments,segments[1:]+segments[:1]))
    def check(nodes,repeated=False):
        for operation,value in nodes:
            name=str(operation)
            if name in {'GROUPREF','GROUPREF_EXISTS'}:raise RuleError('regex_pattern_complexity')
            if name in {'MAX_REPEAT','MIN_REPEAT','POSSESSIVE_REPEAT'}:
                minimum,maximum,body=value
                variable=minimum!=maximum and maximum>1
                if repeated and variable:raise RuleError('regex_pattern_complexity')
                # Finite small formats and disjoint separators do not have
                # the ambiguous nesting of (a+)+ or (a|aa)+.
                safe=name=='POSSESSIVE_REPEAT'or choices([(operation,value)])<=MAX_ITEMS or delimited(body)
                check(body,False if safe else repeated or maximum>1)
            elif name=='SUBPATTERN':check(value[-1],repeated)
            elif name=='BRANCH':
                if repeated:raise RuleError('regex_pattern_complexity')
                for branch in value[1]:check(branch,repeated)
            elif name in {'ASSERT','ASSERT_NOT'}:check(value[1],repeated)
            elif name=='ATOMIC_GROUP':check(value,False)
    check(syntax)
    translated=regex.VERSION0
    for old,new in[(std_re.I,regex.I),(std_re.M,regex.M),(std_re.S,regex.S),(std_re.X,regex.X),(std_re.A,regex.A),(std_re.U,regex.U)]:
        if flags&old:translated|=new
    try:return regex.compile(pattern,translated)
    except regex.error:raise RuleError('invalid_regex_pattern')from None


class RegexBudget:
    """The existing owner retains structural and resource bounds."""
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
        options={**kwargs,'concurrent':False}
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
