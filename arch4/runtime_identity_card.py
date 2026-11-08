"""Resolved runtime identity-card selection helpers.

Kept below engine_runtime_status/runtime_sidecar so both status assembly and
support-bundle readers can select identity evidence without importing each
other.
"""

from __future__ import annotations

from typing import Any, Mapping


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return False


def normalize_resolved_identity_card(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict) or not candidate:
        return {}
    out = {k: v for k, v in candidate.items() if not isinstance(v, set)}
    out["exact_identity"] = coerce_bool(out.get("exact_identity"))
    out["provisional"] = coerce_bool(out.get("provisional"))
    if "exact_identity" not in candidate and str(out.get("match_quality", "")).strip().lower() == "exact_firmware":
        out["exact_identity"] = True
    if not out.get("exact_identity"):
        return out
    if coerce_bool(out.get("firmware_revalidation_required")):
        out["exact_identity"] = False
    elif coerce_bool(out.get("provisional")) and out.get("match_quality") != "exact_firmware":
        out["exact_identity"] = False
    return out


def resolved_identity_card_rank(candidate: Mapping[str, Any]) -> tuple[int, int, int, int, int]:
    exact = coerce_bool(candidate.get("exact_identity"))
    provisional = coerce_bool(candidate.get("provisional"))
    profile_id = str(candidate.get("profile_id") or "").strip()
    identity_key = str(candidate.get("identity_key") or "").strip()
    backend = str(candidate.get("backend") or "").strip()
    model = str(candidate.get("model") or "").strip()
    firmware = str(candidate.get("firmware") or "").strip()
    family = str(candidate.get("family") or "").strip()

    if exact and profile_id and not provisional:
        tier = 4
    elif exact and not provisional:
        tier = 3
    elif profile_id and not provisional:
        tier = 2
    elif profile_id:
        tier = 1
    else:
        tier = 0

    completeness = sum(1 for value in (backend, model, firmware, family, identity_key) if value)
    return (tier, len(profile_id), completeness, len(identity_key), 1 if exact else 0)


def resolve_resolved_identity_card(*candidates: Any) -> dict[str, Any]:
    best_card: dict[str, Any] = {}
    best_rank = (-1, -1, -1, -1, -1)
    for candidate in candidates:
        card = normalize_resolved_identity_card(candidate)
        if not card:
            continue
        rank = resolved_identity_card_rank(card)
        if rank > best_rank:
            best_rank = rank
            best_card = card
    return dict(best_card)
