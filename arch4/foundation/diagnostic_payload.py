"""Lossless diagnostic content codec. No clocks, I/O or event decisions here."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping
import zlib


def encode_payload(record: Mapping[str, Any]) -> tuple[bytes, bytes]:
    """Time and sequence belong to each occurrence, not shared contents."""

    body = {k: v for k, v in record.items() if k not in {"sequence", "ts"}}
    raw = json.dumps(body, ensure_ascii=True, separators=(",", ":"),
                     sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).digest(), zlib.compress(raw, level=6)


def decode_payload(
    digest: bytes, compressed: bytes, *, sequence: int, ts: float, max_bytes: int,
) -> dict[str, Any]:
    """Bound expansion before parsing; corrupt data must never look valid."""

    if type(digest) is not bytes or len(digest) != 32 or type(compressed) is not bytes:
        raise ValueError("cgf_evidence_payload_type_invalid")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, max_bytes + 1)
    if (len(raw) > max_bytes or not decoder.eof or decoder.unused_data
            or decoder.unconsumed_tail):
        raise ValueError("cgf_evidence_payload_compression_invalid")
    if hashlib.sha256(raw).digest() != digest:
        raise ValueError("cgf_evidence_payload_digest_invalid")
    body = json.loads(raw)
    if not isinstance(body, dict) or "sequence" in body or "ts" in body:
        raise ValueError("cgf_evidence_payload_body_invalid")
    return {**body, "sequence": sequence, "ts": ts}
