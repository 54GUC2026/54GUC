"""Canonical early privacy projection and byte-bound finalization receipts.

This is a stateless adapter used by the existing History writer/exporter. It
owns no queue, database, thread, modem read or retention policy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import gzip
import hashlib
import hmac
import json
import re
import secrets
import types
from typing import Any, Mapping

from arch4 import support_evidence
from arch4 import privacy_scrub

_PROCESS_SEAL_KEY = secrets.token_bytes(32)
_FORMAT = 'PreparedSupportRecord.v2'


def _policy_constant(value: Any) -> Any:
    if value is None or type(value) in (str,int,float,bool):
        return value
    if isinstance(value,re.Pattern):
        return ['regex',value.pattern,value.flags]
    if isinstance(value,types.CodeType):
        # marshal encodes object-reference state and installation filenames.
        # Fingerprint executable content, including nested comprehensions, not
        # interpreter heap state, source locations or adaptive execution caches.
        return ['code',value.co_argcount,value.co_posonlyargcount,value.co_kwonlyargcount,
            value.co_nlocals,value.co_stacksize,value.co_flags,value.co_code.hex(),
            value.co_exceptiontable.hex(),_policy_constant(value.co_consts),
            value.co_names,value.co_varnames,value.co_freevars,value.co_cellvars]
    if isinstance(value,bytes):
        return ['bytes',value.hex()]
    if isinstance(value,complex):
        return ['complex',value.real,value.imag]
    if value is Ellipsis:
        return ['ellipsis']
    if isinstance(value,(tuple,list,set,frozenset)):
        items=[_policy_constant(item) for item in value]
        if isinstance(value,(set,frozenset)):
            items=sorted(items,key=lambda item:json.dumps(item,sort_keys=True))
        return [type(value).__name__,items]
    if isinstance(value,dict):
        return {str(k):_policy_constant(v) for k,v in sorted(value.items(),key=lambda item:str(item[0]))}
    return type(value).__qualname__


@lru_cache(maxsize=1)
def support_preparation_policy() -> str:
    """Bind caches to actual running privacy/projection code and declarations."""
    from arch4.interfaces import LUC_VERSION
    digest=hashlib.sha256(('SupportPreparationPolicy.v3|'+_FORMAT+'|'+LUC_VERSION).encode())
    for module in (privacy_scrub,support_evidence):
        for name,value in sorted(vars(module).items()):
            value=getattr(value,'__wrapped__',value)
            if isinstance(value,types.FunctionType) and value.__module__==module.__name__:
                definition=[name,_policy_constant(value.__code__),
                    _policy_constant(value.__defaults__),_policy_constant(value.__kwdefaults__)]
                digest.update(json.dumps(definition,sort_keys=True).encode())
            elif name.lstrip('_').isupper():
                digest.update(json.dumps([name,_policy_constant(value)],sort_keys=True).encode())
    return digest.hexdigest()


def prepare_support_record(record: Mapping[str,Any], *, secret_fingerprints: set[str] | None = None,
        known_secret_values: set[str] | None = None) -> tuple[bytes,int]:
    """Validate live contracts before cleaning; never reseal public data."""
    projected=support_evidence.project_support_records(dict(record))
    clean,found=privacy_scrub.scrub_record_with_secrets(projected)
    if secret_fingerprints is not None:
        secret_fingerprints.update(hashlib.sha256(value.encode()).hexdigest() for value in found)
    if known_secret_values is not None:
        known_secret_values.update(found)
    # Secrets exist only during this call, never in the prepared cache/receipt.
    raw=(json.dumps(clean,ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8')
    return gzip.compress(raw,compresslevel=6,mtime=0),int(bool(found))


@dataclass(frozen=True,slots=True)
class PreparedSupportArtifact:
    filename: str
    policy: str
    content_sha256: str
    redaction_count: int
    _seal: bytes=field(repr=False,compare=False)

    def verify(self,content: bytes) -> bool:
        digest=hashlib.sha256(content).hexdigest()
        message=(self.filename+'|'+self.policy+'|'+digest+'|'+str(self.redaction_count)).encode()
        return (self.policy==support_preparation_policy() and digest==self.content_sha256
            and hmac.compare_digest(self._seal,hmac.digest(_PROCESS_SEAL_KEY,message,'sha256')))


def seal_prepared_artifact(filename: str,content: bytes,*,redaction_count: int) -> PreparedSupportArtifact:
    """Receipt for canonical exporter-assembled, individually verified members."""
    policy=support_preparation_policy();digest=hashlib.sha256(content).hexdigest()
    message=(filename+'|'+policy+'|'+digest+'|'+str(redaction_count)).encode()
    return PreparedSupportArtifact(filename,policy,digest,redaction_count,
        hmac.digest(_PROCESS_SEAL_KEY,message,'sha256'))
