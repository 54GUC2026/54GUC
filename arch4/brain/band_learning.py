"""
arch4/brain/band_learning.py

Shared containers for band-learning state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping

from arch4.brain.delivered_load_memory import normalize_dlst


def _normalize_bucket_map(raw: Mapping[str, Any] | None) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(raw, Mapping):
        return out
    for key, value in raw.items():
        if isinstance(value, Mapping):
            bucket = dict(value)
            bucket["DLST"] = normalize_dlst(bucket.get("DLST"))
            out[str(key)] = bucket
    return out


def _normalize_blacklist(raw: Any) -> set[int]:
    out: set[int] = set()
    if not isinstance(raw, (list, tuple, set)):
        return out
    for value in raw:
        try:
            band = int(value)
        except (TypeError, ValueError):
            continue
        if band > 0:
            out.add(band)
    return out


def _band_from_record_key(key: Any) -> int:
    text = str(key or "").strip()
    if not text:
        return 0
    first = text.split(":", 1)[0].strip()
    if first.upper().startswith("B"):
        first = first[1:]
    try:
        band = int(first)
    except (TypeError, ValueError):
        return 0
    return band if band > 0 else 0


def _normalize_blacklist_record(key: Any, value: Any) -> tuple[str, Dict[str, Any]] | None:
    raw = dict(value or {}) if isinstance(value, Mapping) else {}
    band = int(raw.get("band", 0) or 0) if str(raw.get("band", "") or "").strip().isdigit() else 0
    if band <= 0:
        band = _band_from_record_key(key)
    if band <= 0:
        return None

    context_key = str(raw.get("context_key", raw.get("context", "*")) or "*").strip() or "*"
    record_key = f"B{band}:{context_key}"
    created_ts = _safe_float(raw.get("created_ts", raw.get("ts", 0.0)))
    expires_ts = _safe_float(raw.get("expires_ts", 0.0))
    return record_key, {
        "band": int(band),
        "context_key": context_key,
        "created_ts": float(created_ts),
        "expires_ts": float(expires_ts),
        "reason": str(raw.get("reason", "imported_band_blacklist") or "imported_band_blacklist"),
        "provider_plmn": str(raw.get("provider_plmn", "") or ""),
    }


def _normalize_blacklist_records(raw: Any) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(raw, Mapping):
        return out
    for key, value in raw.items():
        normalized = _normalize_blacklist_record(key, value)
        if normalized is None:
            continue
        rec_key, rec = normalized
        out[rec_key] = rec
    return out


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


@dataclass
class BandLearningStore:
    stm: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    ltm: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    blacklist: set[int] = field(default_factory=set)
    blacklist_records: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "BandLearningStore":
        imported_blacklist = _normalize_blacklist((raw or {}).get("band_blacklist"))
        blacklist_records = _normalize_blacklist_records((raw or {}).get("band_blacklist_records"))
        now = time.time()
        for band in imported_blacklist:
            key = f"B{band}:*"
            blacklist_records.setdefault(
                key,
                {
                    "band": int(band),
                    "context_key": "*",
                    "created_ts": 0.0,
                    "expires_ts": 0.0,
                    "reason": "imported_band_blacklist",
                    "provider_plmn": "",
                },
            )
        store = cls(
            stm=_normalize_bucket_map((raw or {}).get("band_learning_stm")),
            ltm=_normalize_bucket_map((raw or {}).get("band_learning_ltm")),
            blacklist=set(),
            blacklist_records=blacklist_records,
        )
        store.prune_blacklist(now=now)
        return store

    def to_dict(self) -> Dict[str, Any]:
        self.prune_blacklist()
        return {
            "band_learning_stm": {str(k): dict(v) for k, v in self.stm.items()},
            "band_learning_ltm": {str(k): dict(v) for k, v in self.ltm.items()},
            "band_blacklist": sorted(self.blacklist),
            "band_blacklist_records": {
                str(k): dict(v) for k, v in sorted(self.blacklist_records.items())
            },
        }

    def to_stm_dict(self) -> Dict[str, Any]:
        return {
            "band_learning_stm": {str(k): dict(v) for k, v in self.stm.items()},
        }

    def to_ltm_dict(self) -> Dict[str, Any]:
        self.prune_blacklist()
        return {
            "band_learning_ltm": {str(k): dict(v) for k, v in self.ltm.items()},
            "band_blacklist": sorted(self.blacklist),
            "band_blacklist_records": {
                str(k): dict(v) for k, v in sorted(self.blacklist_records.items())
            },
        }

    def replace_imported_blacklist(self, values: set[int] | list[int] | tuple[int, ...]) -> None:
        records: Dict[str, Dict[str, Any]] = {}
        for band in _normalize_blacklist(values):
            records[f"B{band}:*"] = {
                "band": int(band),
                "context_key": "*",
                "created_ts": 0.0,
                "expires_ts": 0.0,
                "reason": "imported_band_blacklist",
                "provider_plmn": "",
            }
        self.blacklist_records = records
        self.blacklist = {
            int(rec["band"])
            for rec in records.values()
            if int(rec.get("band", 0) or 0) > 0
        }

    def add_blacklist_record(
        self,
        *,
        band: int,
        context_key: str = "*",
        created_ts: float = 0.0,
        expires_ts: float = 0.0,
        reason: str = "band_suppression",
        provider_plmn: str = "",
        now: float | None = None,
    ) -> bool:
        band = int(band or 0)
        if band <= 0:
            return False
        context = str(context_key or "*").strip() or "*"
        key = f"B{band}:{context}"
        existing = self.blacklist_records.get(key)
        if isinstance(existing, Mapping):
            old_expires = _safe_float(existing.get("expires_ts", 0.0))
            if old_expires <= 0.0 or old_expires > float(time.time() if now is None else now):
                return False
        self.blacklist_records[key] = {
            "band": int(band),
            "context_key": context,
            "created_ts": float(created_ts or 0.0),
            "expires_ts": float(expires_ts or 0.0),
            "reason": str(reason or "band_suppression"),
            "provider_plmn": str(provider_plmn or ""),
        }
        self.prune_blacklist(now=now)
        return True

    def prune_blacklist(self, *, now: float | None = None) -> int:
        ts = float(time.time() if now is None else now)
        before = len(self.blacklist_records)
        self.blacklist_records = {
            str(key): dict(rec)
            for key, rec in self.blacklist_records.items()
            if _safe_float(rec.get("expires_ts", 0.0)) <= 0.0
            or _safe_float(rec.get("expires_ts", 0.0)) > ts
        }
        self.blacklist = {
            int(rec.get("band", 0) or 0)
            for rec in self.blacklist_records.values()
            if int(rec.get("band", 0) or 0) > 0
        }
        return max(0, before - len(self.blacklist_records))

    def is_blacklisted(
        self,
        band: int,
        *,
        context_key: str | None = None,
        now: float | None = None,
    ) -> bool:
        band = int(band or 0)
        if band <= 0:
            return False
        self.prune_blacklist(now=now)
        context = str(context_key or "").strip()
        for rec in self.blacklist_records.values():
            if int(rec.get("band", 0) or 0) != band:
                continue
            rec_context = str(rec.get("context_key", "*") or "*")
            if rec_context == "*" or not context or rec_context == context:
                return True
        return False


@dataclass
class BandConfidenceStore:
    confidence_ltm: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "BandConfidenceStore":
        return cls(
            confidence_ltm=_normalize_bucket_map((raw or {}).get("band_confidence_ltm")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "band_confidence_ltm": {
                str(k): dict(v) for k, v in self.confidence_ltm.items()
            }
        }
