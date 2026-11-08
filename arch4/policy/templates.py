"""
arch4/policy/templates.py

ARCH6v1.2 policy-as-data loader.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from arch4.interfaces import Arch4Config, PolicyProfile

log = logging.getLogger("arch4.policy.templates")

_POLICY_TEMPLATE_CACHE: dict[Path, tuple[int, int, Dict[str, Any]]] = {}


def load_policy_templates(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        stat = p.stat()
        mtime_ns = int(stat.st_mtime_ns)
        size = int(stat.st_size)
        cached = _POLICY_TEMPLATE_CACHE.get(p)
        if cached and cached[0] == mtime_ns and cached[1] == size:
            return dict(cached[2])
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        _POLICY_TEMPLATE_CACHE[p] = (mtime_ns, size, dict(data))
        return dict(data)
    except Exception as e:
        log.warning("Policy template parse failed (%s): %s", p, e)
        return {}


def _pick_profile_blocks(data: Dict[str, Any], profile: PolicyProfile) -> List[Dict[str, Any]]:
    profiles = data.get("profiles", {}) if isinstance(data, dict) else {}
    if not isinstance(profiles, dict):
        return []

    picks = [
        "default",
        f"default:{profile.modem_backend}",
        f"{profile.provider_plmn}:{profile.modem_backend}",
        f"{profile.policy_family}:{profile.modem_backend}",
        profile.name,
        profile.policy_key,
    ]
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for key in picks:
        if not key or key in seen:
            continue
        seen.add(key)
        block = profiles.get(key)
        if isinstance(block, dict):
            merged.append(block)
    return merged


def build_policy_template_overrides(
    cfg: Arch4Config,
    profile: PolicyProfile,
) -> Tuple[Dict[str, Any], str]:
    """Validate and return matching overrides without mutating config."""
    if not cfg.policy_templates_enabled:
        return {}, cfg.policy_templates_path

    data = load_policy_templates(cfg.policy_templates_path)
    blocks = _pick_profile_blocks(data, profile)
    if not blocks:
        return {}, cfg.policy_templates_path

    applied: Dict[str, Any] = {}
    for block in blocks:
        for k, v in block.items():
            if hasattr(cfg, k):
                applied[k] = v
            else:
                log.warning("Policy template key ignored (unknown config field): %s", k)
    return applied, cfg.policy_templates_path


def apply_policy_template(*args: Any, **kwargs: Any) -> Tuple[Dict[str, Any], str]:
    del args, kwargs
    raise RuntimeError("mutating_policy_template_path_deleted")
