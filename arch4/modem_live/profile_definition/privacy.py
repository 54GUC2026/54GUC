"""A complete declarative definition is shareable, or it is omitted entirely.

Native field names are syntax here, never observed identity/credential values.
This adapter does not relax privacy for any ordinary runtime record.
"""
import json
import re
from .registry import PortableProfile,_parse,MAX_FILE_BYTES


def checked_definition(raw,known_secrets=()):
    if len(raw)>MAX_FILE_BYTES:raise ValueError('profile_definition_size_limit')
    document=_parse(raw)
    profile=PortableProfile('<support-definition>',raw,document)
    secrets=tuple(str(v)for v in known_secrets if len(str(v))>=6)
    def walk(value,path=(),syntax=False):
        if isinstance(value,dict):
            for name,item in value.items():walk(item,(*path,name),syntax)
        elif isinstance(value,(list,tuple)):
            for index,item in enumerate(value):walk(item,(*path,index),syntax)
        elif isinstance(value,str):
            # Formula text contains escaped regular expressions, not UNC paths.
            # Its parsed literal values are checked below without that escaping.
            formula=(len(path)==3 and(path[0]=='fields'and path[2]=='read'or path[0]=='helpers'and path[2]=='formula')
                or len(path)==2 and path[0]in{'state','preprocessing'}
                or len(path)==5 and path[0]=='writes'and path[2]=='fields'and path[4]=='value')
            if syntax and formula:return
            if (any(secret in value for secret in secrets)
                    or re.search(r'(?:github_pat_|ghp_|sk-proj-)[A-Za-z0-9_]+',value)
                    or re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',value)
                    or re.search(r'''(?:^|[\s"'])(?:[A-Za-z]:[\\/]|\\\\[A-Za-z][^\s]*[\\/])|/home/|/Users/''',value)
                    or re.fullmatch(r'[0-9]{14,16}',value)and set(value)!={'0'}
                    or re.fullmatch(r'(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}',value)and re.sub('[:-]','',value)!='000000000000'):
                raise ValueError('profile_definition_private_literal')
        elif type(value)in(int,float)and str(value)in secrets:
            raise ValueError('profile_definition_private_literal')
    walk(document,syntax=True)
    # Also inspect parsed literal strings inside formulas. A quoted identifier
    # must not escape the same rule by hiding inside expression text.
    walk(profile._plan)
    return profile
