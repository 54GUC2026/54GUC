"""
Low-confidence external knowledge lane for calibration.

This module never overrides live evidence.
It only normalizes and scores advisory priors about:

- provider/site/band hints
- device/model/firmware hints
"""

from __future__ import annotations

import time
import json
from pathlib import Path
from typing import Any, Dict, List

from arch4.brain.calibration_profile import MAX_EXTERNAL_KNOWLEDGE_FILE_BYTES


MAX_ADVISORY_WEIGHT = 0.35


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    if out < 0.0:
        return 0.0
    return float(out)


def _provider_name_from_mapping(data: Dict[str, Any]) -> str:
    return str(data.get("provider_name", data.get("provider", "")) or "")


def _provider_plmn_from_mapping(data: Dict[str, Any]) -> str:
    return str(data.get("provider_plmn", data.get("plmn", "")) or "")


def sanitize_external_knowledge(raw: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(raw or {})
    site_hints = data.get("site_hints") if isinstance(data.get("site_hints"), list) else []
    provider_hints = data.get("provider_hints") if isinstance(data.get("provider_hints"), dict) else {}
    device_hints = data.get("device_hints") if isinstance(data.get("device_hints"), dict) else {}

    out_site: List[Dict[str, Any]] = []
    for row in site_hints:
        if not isinstance(row, dict):
            continue
        bands = row.get("bands") if isinstance(row.get("bands"), list) else []
        out_site.append(
            {
                "cell_id": str(row.get("cell_id", "") or ""),
                "bands": [int(v) for v in bands if str(v).strip().isdigit()],
                "bandwidth_mhz": _as_float(row.get("bandwidth_mhz", 0.0)),
                "confidence": min(1.0, _as_float(row.get("confidence", 0.0))),
                "freshness_hours": _as_float(row.get("freshness_hours", 0.0)),
                "source": str(row.get("source", "") or ""),
                "note": str(row.get("note", "") or ""),
            }
        )

    return {
        "provider_hints": {
            "provider_name": _provider_name_from_mapping(provider_hints),
            "provider_plmn": _provider_plmn_from_mapping(provider_hints),
            "provider": _provider_name_from_mapping(provider_hints),
            "plmn": _provider_plmn_from_mapping(provider_hints),
            "coverage_note": str(provider_hints.get("coverage_note", "") or ""),
            "confidence": min(1.0, _as_float(provider_hints.get("confidence", 0.0))),
            "freshness_hours": _as_float(provider_hints.get("freshness_hours", 0.0)),
            "source": str(provider_hints.get("source", "") or ""),
            "bands": [int(v) for v in (provider_hints.get("bands") if isinstance(provider_hints.get("bands"), list) else []) if str(v).strip().isdigit()],
        },
        "device_hints": {
            "model": str(device_hints.get("model", "") or ""),
            "firmware": str(device_hints.get("firmware", "") or ""),
            "confidence": min(1.0, _as_float(device_hints.get("confidence", 0.0))),
            "freshness_hours": _as_float(device_hints.get("freshness_hours", 0.0)),
            "source": str(device_hints.get("source", "") or ""),
            "note": str(device_hints.get("note", "") or ""),
            "known_limits": [str(v) for v in (device_hints.get("known_limits") if isinstance(device_hints.get("known_limits"), list) else []) if str(v).strip()],
        },
        "site_hints": out_site,
        "updated_ts": float(time.time()),
    }


def merge_external_knowledge_advisories(
    current: Dict[str, Any],
    incoming: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge one advisory source without letting weaker history erase stronger hints."""

    existing = sanitize_external_knowledge(current if isinstance(current, dict) else {})
    candidate = sanitize_external_knowledge(incoming if isinstance(incoming, dict) else {})

    def choose_hint(name: str) -> Dict[str, Any]:
        old = dict(existing.get(name) or {})
        new = dict(candidate.get(name) or {})
        old_confidence = _as_float(old.get("confidence", 0.0))
        new_confidence = _as_float(new.get("confidence", 0.0))
        if new_confidence <= old_confidence:
            return old
        merged = dict(new)
        for key, value in old.items():
            if merged.get(key) in (None, "", [], {}):
                merged[key] = value
        return merged

    sites: Dict[str, Dict[str, Any]] = {}
    for row in list(existing.get("site_hints") or []) + list(
        candidate.get("site_hints") or []
    ):
        if not isinstance(row, dict):
            continue
        cell_id = str(row.get("cell_id") or "").strip()
        key = cell_id or "generic|{}|{}".format(
            str(row.get("source") or "").strip(),
            ",".join(str(value) for value in row.get("bands", []) or []),
        )
        previous = sites.get(key)
        if previous is None or _as_float(row.get("confidence", 0.0)) > _as_float(
            previous.get("confidence", 0.0)
        ):
            sites[key] = dict(row)
    return sanitize_external_knowledge(
        {
            "provider_hints": choose_hint("provider_hints"),
            "device_hints": choose_hint("device_hints"),
            "site_hints": list(sites.values())[:64],
        }
    )


def load_external_knowledge_file(
    path: Path | str,
    *,
    max_bytes: int = MAX_EXTERNAL_KNOWLEDGE_FILE_BYTES,
) -> Dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        return {"ok": False, "error": "file_not_found", "path": str(file_path)}
    try:
        size_bytes = int(file_path.stat().st_size)
    except Exception:
        size_bytes = 0
    if size_bytes > int(max_bytes):
        return {
            "ok": False,
            "error": "file_too_large",
            "path": str(file_path),
            "size_bytes": int(size_bytes),
            "max_bytes": int(max_bytes),
        }
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "ok": False,
            "error": "invalid_json",
            "path": str(file_path),
            "detail": str(exc),
        }
    if not isinstance(raw, dict):
        return {"ok": False, "error": "invalid_shape", "path": str(file_path)}
    sanitized = sanitize_external_knowledge(raw)
    provider_hints = (
        sanitized.get("provider_hints")
        if isinstance(sanitized.get("provider_hints"), dict)
        else {}
    )
    device_hints = (
        sanitized.get("device_hints")
        if isinstance(sanitized.get("device_hints"), dict)
        else {}
    )
    site_hints = sanitized.get("site_hints") if isinstance(sanitized.get("site_hints"), list) else []
    return {
        "ok": True,
        "path": str(file_path),
        "size_bytes": int(size_bytes),
        "knowledge": sanitized,
        "provider_hint_present": bool(provider_hints),
        "device_hint_present": bool(device_hints),
        "site_hint_count": int(len(site_hints)),
    }


def build_external_knowledge_advisory(
    *,
    runtime_context: Dict[str, Any],
    external_knowledge: Dict[str, Any],
) -> Dict[str, Any]:
    runtime = dict(runtime_context or {})
    runtime_identity = (
        runtime.get("runtime_identity")
        if isinstance(runtime.get("runtime_identity"), dict)
        else (
            runtime.get("identity")
            if isinstance(runtime.get("identity"), dict)
            else {}
        )
    )
    knowledge = sanitize_external_knowledge(external_knowledge if isinstance(external_knowledge, dict) else {})
    provider = knowledge.get("provider_hints", {})
    device = knowledge.get("device_hints", {})
    site_hints = knowledge.get("site_hints", [])

    runtime_model = str(runtime_identity.get("model", "") or "").strip().lower()
    runtime_fw = str(runtime_identity.get("firmware", "") or "").strip().lower()
    runtime_plmn = _provider_plmn_from_mapping(runtime_identity).strip()
    runtime_provider = _provider_name_from_mapping(runtime_identity).strip().lower()

    provider_match = bool(
        (runtime_plmn and runtime_plmn == _provider_plmn_from_mapping(provider).strip())
        or (runtime_provider and runtime_provider == _provider_name_from_mapping(provider).strip().lower())
    )
    device_model_match = bool(
        runtime_model and runtime_model == str(device.get("model", "") or "").strip().lower()
    )
    device_fw_match = bool(
        runtime_fw and runtime_fw == str(device.get("firmware", "") or "").strip().lower()
    )

    provider_conf = float(provider.get("confidence", 0.0) or 0.0)
    device_conf = float(device.get("confidence", 0.0) or 0.0)
    site_conf = 0.0
    freshest_hours = 0.0
    expected_bands: List[int] = []
    best_bandwidth = 0.0
    for row in site_hints:
        if not isinstance(row, dict):
            continue
        site_conf = max(site_conf, float(row.get("confidence", 0.0) or 0.0))
        freshest_hours = max(freshest_hours, float(row.get("freshness_hours", 0.0) or 0.0))
        best_bandwidth = max(best_bandwidth, float(row.get("bandwidth_mhz", 0.0) or 0.0))
        for band in row.get("bands", []) if isinstance(row.get("bands"), list) else []:
            if int(band) not in expected_bands:
                expected_bands.append(int(band))

    raw_confidence = max(provider_conf, device_conf, site_conf)
    freshness_penalty = 1.0
    if freshest_hours > 0.0:
        freshness_penalty = min(1.0, 24.0 / max(24.0, freshest_hours))
    advisory_weight = min(MAX_ADVISORY_WEIGHT, raw_confidence * freshness_penalty * MAX_ADVISORY_WEIGHT)

    site_value_hint = "unknown"
    if best_bandwidth >= 20.0 or len(expected_bands) >= 3:
        site_value_hint = "promising"
    elif best_bandwidth > 0.0 or expected_bands:
        site_value_hint = "limited"

    summary_parts: List[str] = []
    if provider_match:
        summary_parts.append("provider hint matches runtime identity")
    elif provider_conf > 0.0:
        summary_parts.append("provider hint does not fully match runtime identity")
    if device_model_match:
        summary_parts.append("device model hint matches")
    elif device_conf > 0.0:
        summary_parts.append("device hint is unverified against runtime model")
    if expected_bands:
        summary_parts.append("site hints suggest bands " + ",".join(f"B{int(v)}" for v in expected_bands[:5]))
    if site_value_hint == "promising":
        summary_parts.append("site hints look promising but remain unproven")
    elif site_value_hint == "limited":
        summary_parts.append("site hints suggest limited opportunity")

    return {
        "available": bool(raw_confidence > 0.0 or expected_bands or provider_match or device_model_match),
        "advisory_only": True,
        "provider_match": bool(provider_match),
        "device_model_match": bool(device_model_match),
        "device_firmware_match": bool(device_fw_match),
        "provider_confidence": float(provider_conf),
        "device_confidence": float(device_conf),
        "site_confidence": float(site_conf),
        "advisory_weight": float(advisory_weight),
        "freshness_penalty": float(freshness_penalty),
        "freshness_hours": float(freshest_hours),
        "expected_bands": expected_bands,
        "best_bandwidth_mhz": float(best_bandwidth),
        "site_value_hint": str(site_value_hint),
        "summary": "; ".join(summary_parts) if summary_parts else "no external knowledge matched to runtime context",
        "source_count": int(
            (1 if provider_conf > 0.0 else 0)
            + (1 if device_conf > 0.0 else 0)
            + (1 if site_conf > 0.0 else 0)
        ),
    }
