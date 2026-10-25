"""Canonical learning-quality feed for self-learning candidate authority.

LearningAuthority may compute referee quality. Memory may hold action ROI.
This module owns the single runtime publication that turns those inputs into
the quality surface consumed by self-learning candidate generation.
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping

from arch4.brain.action_proof_register import action_proof_surface_from_engine
from arch4.brain.action_roi_memory import pband_action_roi_from_rewards
from arch4.brain.feed_registers import (
    FEED_LEARNING_QUALITY,
    OWNER_LEARNING_QUALITY,
    require_brain_feed_register_bank,
)
from arch4.brain.learning_memory_projection import (
    learned_bandset_candidates_from_memory,
    ranked_bandsets_from_memory,
)
from arch4.brain.learning_authority_quality_journal_owner import (
    quality_profile_from_learning_authority,
)
from arch4.external_memory_runtime import (
    read_external_memory_prior_register,
)
from arch4.runtime_band_scope import normalize_runtime_band_values, resolve_runtime_available_bands

SCHEMA = "brain_learning_quality_publication.v1"
OWNER = OWNER_LEARNING_QUALITY
FEED = FEED_LEARNING_QUALITY
DEFAULT_STALE_AFTER_S = 2.0
UNCHANGED_PUBLICATION_REFRESH_FRACTION = 0.5
_LEARNING_QUALITY_PUBLICATION_CACHE_ATTR = "_learning_quality_publication_cache"


class LearningQualityContractError(ValueError):
    """Raised when a learning-quality publication violates its owner contract."""


class LearningQualityRegisterUnavailable(RuntimeError):
    """Raised when the learning-quality feed is missing, stale, or invalid."""


def publish_learning_quality_register(
    engine: Any,
    *,
    publication: Mapping[str, Any],
    now: float | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> dict[str, Any]:
    payload = require_learning_quality_publication(publication)
    bank = require_brain_feed_register_bank(engine)
    now_ts = time.time() if now is None else float(now)
    bank.write_feeder(
        FEED,
        owner=OWNER,
        payload=payload,
        source="brain_learning_quality",
        updated_at=now_ts,
        valid=True,
        stale_after_s=max(0.0, float(stale_after_s or 0.0)),
    )
    return {"published": True, "feed": FEED, "owner": OWNER, "schema": SCHEMA}


def read_learning_quality_register(
    engine: Any,
    *,
    allow_stale: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    bank = require_brain_feed_register_bank(engine)
    now_ts = time.time() if now is None else float(now)
    row = bank.read_feeder(FEED).as_dict(now=now_ts)
    if str(row.get("error") or ""):
        raise LearningQualityRegisterUnavailable(str(row.get("error") or "missing"))
    if not bool(row.get("valid", False)):
        raise LearningQualityRegisterUnavailable("learning_quality_register_invalid")
    if bool(row.get("stale", False)) and not bool(allow_stale):
        raise LearningQualityRegisterUnavailable("learning_quality_register_stale")
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    return require_learning_quality_publication(payload)


def build_learning_quality_publication(
    engine: Any,
    *,
    quality_profile_source: Any = None,
    current_hour: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    if current_hour is None:
        current_hour = int(time.localtime(float(now if now is not None else time.time())).tm_hour)
    quality_profile = _quality_profile_to_dict(quality_profile_source)
    memory_quality = _memory_band_learning_quality(engine, current_hour=int(current_hour))
    if memory_quality:
        _merge_quality_profile(quality_profile, memory_quality)
        quality_profile["sample_count"] = max(
            _int(quality_profile.get("sample_count"), 0),
            _int(memory_quality.get("sample_count"), 0),
        )
        quality_profile["profile_confidence"] = max(
            _float(quality_profile.get("profile_confidence"), 0.0),
            _float(memory_quality.get("profile_confidence"), 0.0),
        )
        if not _text(quality_profile.get("quality_trend")):
            quality_profile["quality_trend"] = memory_quality.get("quality_trend", "")
        if not _int(quality_profile.get("trend_basis"), 0):
            quality_profile["trend_basis"] = memory_quality.get("trend_basis", 0)
    external_prior_quality = _external_memory_learning_quality(engine)
    if external_prior_quality:
        _merge_quality_profile(quality_profile, external_prior_quality)
        quality_profile["sample_count"] = max(
            _int(quality_profile.get("sample_count"), 0),
            _int(external_prior_quality.get("sample_count"), 0),
        )
        quality_profile["profile_confidence"] = max(
            _float(quality_profile.get("profile_confidence"), 0.0),
            _float(external_prior_quality.get("profile_confidence"), 0.0),
        )
        quality_profile["external_memory_learning_prior"] = {
            "owner": "arch4.external_memory_learning_projection",
            "present": True,
            "runtime_influence": "weighted_historical_seed",
            "source_count": len(external_prior_quality.get("sources", []) or []),
        }
        if not _text(quality_profile.get("quality_trend")):
            quality_profile["quality_trend"] = "external_memory_history_prior"
    ca_flapping = _mapping(getattr(engine, "_ca_flapping_status", {}))
    if ca_flapping.get("active"):
        quality_profile["ca_flapping"] = ca_flapping
    anchor_flip = _mapping(getattr(engine, "_anchor_flip_status", {}))
    if anchor_flip.get("active"):
        quality_profile["anchor_flip_instability"] = anchor_flip
    provider_plmn, provider_name = _modem_identity_from_engine(engine)
    action_roi = pband_action_roi_from_rewards(
        getattr(getattr(getattr(engine, "controller", None), "brain", None), "memory", None),
        provider_plmn=provider_plmn,
        provider_name=provider_name,
    )
    if action_roi:
        quality_profile["pband_action_roi"] = action_roi
    action_proof = action_proof_surface_from_engine(engine)
    if _int(action_proof.get("recent_count"), 0) > 0:
        quality_profile["action_proof_closures"] = action_proof
    authority_bands = _control_authority_bands(engine, quality_profile)
    if authority_bands:
        quality_profile["control_authority_bands"] = authority_bands
        quality_profile["control_authority_owner"] = OWNER
    return require_learning_quality_publication(
        {
            "schema": SCHEMA,
            "owner": OWNER,
            "current_hour": int(current_hour),
            "provider_plmn": provider_plmn,
            "provider_name": provider_name,
            "learning_quality": quality_profile,
            "published_at": float(now if now is not None else time.time()),
        }
    )


def _control_authority_bands(engine: Any, quality_profile: Mapping[str, Any]) -> list[int]:
    """Return smart-loop legal band authority, not current CA-pool readback."""

    candidates: list[int] = []

    def add(values: Any) -> None:
        for band in normalize_runtime_band_values(values):
            if band > 0 and band not in candidates:
                candidates.append(band)

    cfg = getattr(engine, "cfg", None)
    add(getattr(cfg, "deployed_bands", []))
    for mapping_key in ("band_hour_quality", "band_hour_counts", "band_quality"):
        for key in _mapping(quality_profile.get(mapping_key)).keys():
            add(str(key).split(":", 1)[0])
    add(quality_profile.get("control_authority_bands"))
    add(quality_profile.get("authority_bands"))
    return sorted(candidates)


def publish_learning_quality_from_authority(engine: Any, *, now: float) -> dict[str, Any]:
    authority = getattr(engine, "learning_authority", None)
    quality_profile_source = quality_profile_from_learning_authority(authority)
    publication = build_learning_quality_publication(
        engine,
        quality_profile_source=quality_profile_source,
        now=now,
    )
    payload = require_learning_quality_publication(publication)
    fingerprint = _learning_quality_publication_fingerprint(payload)
    cache = _mapping(getattr(engine, _LEARNING_QUALITY_PUBLICATION_CACHE_ATTR, None))
    refresh_after_s = max(0.1, DEFAULT_STALE_AFTER_S * UNCHANGED_PUBLICATION_REFRESH_FRACTION)
    last_published_ts = _float(cache.get("last_published_ts"), 0.0)
    if (
        cache.get("fingerprint") == fingerprint
        and last_published_ts > 0.0
        and float(now) - last_published_ts < refresh_after_s
    ):
        return {
            "published": False,
            "unchanged": True,
            "reason": "learning_quality_inputs_unchanged",
            "feed": FEED,
            "owner": OWNER,
            "schema": SCHEMA,
            "last_published_ts": round(last_published_ts, 3),
            "next_refresh_ts": round(last_published_ts + refresh_after_s, 3),
        }
    result = publish_learning_quality_register(
        engine,
        publication=payload,
        now=now,
        stale_after_s=DEFAULT_STALE_AFTER_S,
    )
    if bool(result.get("published", False)):
        setattr(
            engine,
            _LEARNING_QUALITY_PUBLICATION_CACHE_ATTR,
            {
                "fingerprint": fingerprint,
                "last_published_ts": float(now),
            },
        )
    return result


def require_learning_quality_publication(publication: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(publication or {}) if isinstance(publication, Mapping) else {}
    if _text(row.get("schema")) != SCHEMA:
        raise LearningQualityContractError("learning_quality_schema_mismatch")
    if _text(row.get("owner")) != OWNER:
        raise LearningQualityContractError("learning_quality_owner_mismatch")
    quality = row.get("learning_quality")
    if not isinstance(quality, Mapping):
        raise LearningQualityContractError("learning_quality_payload_missing")
    out = dict(row)
    out["learning_quality"] = dict(quality)
    out["current_hour"] = _int(out.get("current_hour"), int(time.localtime().tm_hour))
    out["provider_plmn"] = _text(out.get("provider_plmn"))
    out["provider_name"] = _text(out.get("provider_name"))
    return out


def _learning_quality_publication_fingerprint(publication: Mapping[str, Any]) -> str:
    row = require_learning_quality_publication(publication)
    stable = {
        key: value
        for key, value in row.items()
        if key not in {"published_at"}
    }
    return json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str)


def _memory_band_learning_quality(engine: Any, *, current_hour: int) -> dict[str, Any]:
    memory = getattr(engine, "memory", None)
    if memory is None:
        return {}
    modem = getattr(engine, "_last_modem", None) or getattr(engine, "last_modem", None)
    cfg = getattr(engine, "cfg", None)
    authority_bands = normalize_runtime_band_values(getattr(engine, "deployed_bands", []))
    if not authority_bands:
        authority_bands = normalize_runtime_band_values(getattr(cfg, "deployed_bands", []))
    current_pool = resolve_runtime_available_bands(modem=modem, memory=None)
    current_set = set(normalize_runtime_band_values(current_pool))
    candidate_sets: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()

    def add_candidate(values: Any) -> None:
        bands = normalize_runtime_band_values(values)
        if not bands:
            return
        key = tuple(bands)
        if key in seen:
            return
        seen.add(key)
        candidate_sets.append(bands)

    if current_set:
        add_candidate(sorted(current_set))
    legal_bands = set(authority_bands)
    for learned_bands in learned_bandset_candidates_from_memory(
        memory,
        hour=int(current_hour),
        min_attempts=2.0,
    ):
        if legal_bands and not set(learned_bands).issubset(legal_bands):
            continue
        add_candidate(learned_bands)
    for band in authority_bands:
        if current_set:
            add_candidate(sorted(current_set | {int(band)}))
        add_candidate([int(band)])
    if not candidate_sets:
        return {}
    ranked = ranked_bandsets_from_memory(
        memory,
        hour=int(current_hour),
        candidate_sets=candidate_sets,
    )
    if not ranked:
        return {}

    band_hour_quality: dict[str, float] = {}
    band_hour_counts: dict[str, int] = {}
    sample_count = 0
    for row in ranked:
        try:
            bands, score, attempts, _source = row[:4]
        except Exception:
            continue
        norm = normalize_runtime_band_values(bands)
        if not norm:
            continue
        count = _int(attempts, 0)
        value = _float(score, 0.5)
        if count <= 0:
            continue
        target_bands = [band for band in norm if band not in current_set] if current_set else list(norm)
        if not target_bands:
            continue
        sample_count += count
        for band in target_bands:
            key = f"{int(band)}:{int(current_hour)}"
            previous_count = int(band_hour_counts.get(key, 0) or 0)
            previous_score = float(band_hour_quality.get(key, 0.5))
            if key not in band_hour_quality or count > previous_count or abs(value - 0.5) > abs(previous_score - 0.5):
                band_hour_quality[key] = round(value, 4)
                band_hour_counts[key] = count

    if not band_hour_quality:
        return {}
    return {
        "band_hour_quality": band_hour_quality,
        "band_hour_counts": band_hour_counts,
        "sample_count": sample_count,
        "profile_confidence": min(1.0, max(0.0, sample_count / 10.0)),
        "quality_trend": "memory_band_learning",
        "trend_basis": sample_count,
    }


def _external_memory_learning_quality(engine: Any) -> dict[str, Any]:
    prior = read_external_memory_prior_register(engine, allow_stale=True)
    learning_prior = _mapping(prior.get("learning_prior")) if isinstance(prior, Mapping) else {}
    if (
        learning_prior.get("schema") != "external_memory_learning_prior.v1"
        or learning_prior.get("owner") != "arch4.external_memory_learning_projection"
        or not bool(learning_prior.get("present", False))
    ):
        return {}
    return {
        "band_hour_quality": _mapping(learning_prior.get("band_hour_quality")),
        "band_hour_counts": _mapping(learning_prior.get("band_hour_counts")),
        "sample_count": _int(learning_prior.get("sample_count"), 0),
        "profile_confidence": _float(learning_prior.get("profile_confidence"), 0.0),
        "quality_trend": _text(learning_prior.get("quality_trend")) or "external_memory_history_prior",
        "sources": list(learning_prior.get("sources") or []) if isinstance(learning_prior.get("sources"), list) else [],
    }


def _merge_quality_profile(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key in ("band_hour_quality", "band_hour_counts"):
        merged = _mapping(source.get(key))
        merged.update(_mapping(target.get(key)))
        target[key] = merged


def _quality_profile_to_dict(profile: Any) -> dict[str, Any]:
    if profile is None:
        return {}
    if hasattr(profile, "to_dict"):
        return dict(profile.to_dict() or {})
    return _mapping(profile)


def _modem_identity_from_engine(engine: Any) -> tuple[str, str]:
    modem = getattr(engine, "_last_modem", None) or getattr(engine, "last_modem", None)
    identity = getattr(modem, "identity", None)
    constraints = getattr(engine, "constraints", None)
    provider_plmn = _text(
        getattr(identity, "provider_plmn", "")
        or getattr(constraints, "provider_plmn", "")
    )
    provider_name = _text(
        getattr(identity, "provider_name", "")
        or getattr(constraints, "provider_name", "")
    )
    return provider_plmn, provider_name


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


__all__ = [
    "OWNER",
    "SCHEMA",
    "FEED",
    "LearningQualityContractError",
    "LearningQualityRegisterUnavailable",
    "build_learning_quality_publication",
    "publish_learning_quality_from_authority",
    "publish_learning_quality_register",
    "read_learning_quality_register",
    "require_learning_quality_publication",
]
