"""
arch4/policy/overlay.py

Validated live policy overlay support for high-ROI tuning without rebuilds.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from arch4.interfaces import Arch4Config
from arch4.core_contracts.configuration_keys import OVERRIDABLE_POLICY_FIELDS

MAX_POLICY_OVERLAY_FILE_BYTES = 128 * 1024

POLICY_OVERLAY_CATEGORIES = (
    "thresholds",
    "weights",
    "feature_switches",
)


class PolicyOverlayValidationError(ValueError):
    pass


@dataclass
class PolicyOverlayStatus:
    active: bool = False
    overlay_id: str = ""
    version: str = ""
    path: str = ""
    digest: str = ""
    applied_ts: float = 0.0
    changed_keys: list[str] = field(default_factory=list)
    category_counts: Dict[str, int] = field(default_factory=dict)
    operator_messages: Dict[str, str] = field(default_factory=dict)
    precedence: Dict[str, Any] = field(default_factory=dict)
    change_history: list[Dict[str, Any]] = field(default_factory=list)
    last_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active": bool(self.active),
            "overlay_id": str(self.overlay_id or ""),
            "version": str(self.version or ""),
            "path": str(self.path or ""),
            "digest": str(self.digest or ""),
            "applied_ts": float(self.applied_ts or 0.0),
            "changed_keys": list(self.changed_keys or []),
            "category_counts": dict(self.category_counts or {}),
            "operator_messages": dict(self.operator_messages or {}),
            "precedence": dict(self.precedence or {}),
            "change_history": list(self.change_history or []),
            "last_error": str(self.last_error or ""),
        }


def _coerce_cfg_value(*, key: str, raw: Any, baseline: Any) -> Any:
    if isinstance(baseline, bool):
        if not isinstance(raw, bool):
            raise PolicyOverlayValidationError(f"{key}: expected boolean")
        return bool(raw)
    if isinstance(baseline, int) and not isinstance(baseline, bool):
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise PolicyOverlayValidationError(f"{key}: expected integer")
        return int(raw)
    if isinstance(baseline, float):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise PolicyOverlayValidationError(f"{key}: expected number")
        return float(raw)
    raise PolicyOverlayValidationError(f"{key}: unsupported overlay field type")


def _validate_operator_messages(raw: Any) -> Dict[str, str]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, Mapping):
        raise PolicyOverlayValidationError("operator_messages: expected object")
    out: Dict[str, str] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if not name:
            raise PolicyOverlayValidationError("operator_messages: empty key")
        if not isinstance(value, str):
            raise PolicyOverlayValidationError(f"operator_messages.{name}: expected string")
        out[name] = value
    return out


class PolicyOverlayManager:
    def __init__(self, cfg: Arch4Config):
        self.cfg = cfg
        self.path = Path(str(getattr(cfg, "policy_overlay_path", "config/policy_overlay.json") or "config/policy_overlay.json"))
        self.enabled = bool(getattr(cfg, "policy_overlay_enabled", True))
        self._baseline: Dict[str, Any] = {}
        self._active_fingerprint: tuple[int, int] | None = None
        self._active_digest: str = ""
        self._status = PolicyOverlayStatus(path=str(self.path))
        self._history: list[Dict[str, Any]] = []

    def _append_history(self, entry: Mapping[str, Any]) -> None:
        row = dict(entry or {})
        row.setdefault("ts", time.time())
        self._history.append(row)
        self._history = self._history[-10:]

    def _overlay_precedence(self, *, candidate: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        changed = set()
        messages = {}
        if candidate is not None:
            changed = set(str(k) for k in dict(candidate.get("changed", {}) or {}).keys())
            messages = dict(candidate.get("operator_messages", {}) or {})
        else:
            changed = set(str(k) for k in self._status.changed_keys or [])
            messages = dict(self._status.operator_messages or {})
        field_sources = {
            key: {
                "source": "policy_overlay" if key in changed else "validated_config",
                "layer_order": ["validated_config", "policy_overlay"],
            }
            for key in sorted(self._baseline.keys())
        }
        return {
            "model": "validated_config_then_policy_overlay",
            "runtime_influence": "active_config_values" if changed else "metadata_only",
            "field_sources": field_sources,
            "overlay_changed_keys": sorted(changed),
            "operator_messages": {
                "source": "policy_overlay_metadata" if messages else "none",
                "keys": sorted(str(k) for k in messages.keys()),
                "runtime_influence": "display_metadata_only",
            },
        }

    def capture_baseline(self) -> None:
        self._baseline = {
            key: getattr(self.cfg, key)
            for key in sorted(OVERRIDABLE_POLICY_FIELDS)
            if hasattr(self.cfg, key)
        }

    def status(self) -> Dict[str, Any]:
        return self._status.to_dict()

    def _restore_baseline(self) -> None:
        raise RuntimeError("mutating_policy_overlay_restore_path_deleted")

    def _fingerprint(self) -> tuple[int, int] | None:
        if not self.path.exists():
            return None
        stat = self.path.stat()
        return (int(stat.st_mtime_ns), int(stat.st_size))

    def _load_candidate(self) -> tuple[Dict[str, Any], str]:
        if not self.path.exists():
            raise FileNotFoundError(str(self.path))
        if self.path.stat().st_size > MAX_POLICY_OVERLAY_FILE_BYTES:
            raise PolicyOverlayValidationError("overlay file too large")
        raw_text = self.path.read_text(encoding="utf-8")
        digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:12]
        try:
            raw = json.loads(raw_text)
        except Exception as exc:
            raise PolicyOverlayValidationError(f"invalid JSON: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise PolicyOverlayValidationError("overlay root must be an object")

        changed: Dict[str, Any] = {}
        counts: Dict[str, int] = {}
        for category in POLICY_OVERLAY_CATEGORIES:
            block = raw.get(category, {})
            if block in (None, {}):
                counts[category] = 0
                continue
            if not isinstance(block, Mapping):
                raise PolicyOverlayValidationError(f"{category}: expected object")
            applied = 0
            for key, value in block.items():
                name = str(key or "").strip()
                if name not in OVERRIDABLE_POLICY_FIELDS or name not in self._baseline:
                    raise PolicyOverlayValidationError(f"{category}.{name}: unknown or unsupported field")
                changed[name] = _coerce_cfg_value(key=name, raw=value, baseline=self._baseline[name])
                applied += 1
            counts[category] = applied

        messages = _validate_operator_messages(raw.get("operator_messages", {}))
        counts["operator_messages"] = len(messages)
        return {
            "overlay_id": str(raw.get("overlay_id", "") or ""),
            "version": str(raw.get("version", "") or ""),
            "changed": changed,
            "category_counts": counts,
            "operator_messages": messages,
        }, digest

    def propose_refresh(self, *, force: bool = False) -> Optional[Dict[str, Any]]:
        """Return one config-generation proposal without changing live state."""

        if not self.enabled:
            return None
        if not self._baseline:
            self.capture_baseline()

        fingerprint = self._fingerprint()
        if not force and fingerprint == self._active_fingerprint:
            return None

        if fingerprint is None:
            if not self._status.active:
                return None
            old = self._status.to_dict()
            next_status = PolicyOverlayStatus(
                path=str(self.path),
                precedence=self._overlay_precedence(candidate={"changed": {}, "operator_messages": {}}),
            )
            restore = {
                key: self._baseline[key]
                for key in list(old.get("changed_keys", []) or [])
                if key in self._baseline
            }
            return {
                "kind": "policy_overlay_cleared",
                "previous": old,
                "status": next_status.to_dict(),
                "set_config_values": {},
                "restore_config_values": restore,
                "candidate_fingerprint": None,
                "candidate_digest": "",
            }

        try:
            candidate, digest = self._load_candidate()
        except Exception as exc:
            self._status.last_error = str(exc)
            self._append_history({
                "kind": "policy_overlay_rejected",
                "error": str(exc),
                "path": str(self.path),
            })
            self._status.change_history = list(self._history)
            return {
                "kind": "policy_overlay_rejected",
                "error": str(exc),
                "status": self._status.to_dict(),
            }

        if not force and digest == self._active_digest:
            self._active_fingerprint = fingerprint
            return None

        now = time.time()
        next_status = PolicyOverlayStatus(
            active=bool(candidate["changed"] or candidate["operator_messages"]),
            overlay_id=str(candidate["overlay_id"] or ""),
            version=str(candidate["version"] or ""),
            path=str(self.path),
            digest=str(digest),
            applied_ts=now,
            changed_keys=sorted(candidate["changed"].keys()),
            category_counts=dict(candidate["category_counts"] or {}),
            operator_messages=dict(candidate["operator_messages"] or {}),
            precedence=self._overlay_precedence(candidate=candidate),
            last_error="",
        )
        old_keys = set(self._status.changed_keys or [])
        new_keys = set(candidate["changed"])
        restore = {
            key: self._baseline[key]
            for key in sorted(old_keys - new_keys)
            if key in self._baseline
        }
        return {
            "kind": "policy_overlay_applied",
            "status": next_status.to_dict(),
            "set_config_values": dict(candidate["changed"]),
            "restore_config_values": restore,
            "candidate_fingerprint": fingerprint,
            "candidate_digest": digest,
        }

    def commit_proposal(self, proposal: Mapping[str, Any]) -> Dict[str, Any]:
        """Commit metadata only after ConfigGeneration durably commits."""

        row = dict(proposal or {})
        kind = str(row.get("kind", "") or "")
        status = row.get("status")
        if kind not in {"policy_overlay_applied", "policy_overlay_cleared"}:
            raise ValueError("policy_overlay_commit_proposal_invalid")
        if not isinstance(status, Mapping):
            raise ValueError("policy_overlay_commit_status_required")
        if kind == "policy_overlay_applied":
            event = {
                "kind": kind,
                "overlay_id": str(status.get("overlay_id", "") or ""),
                "version": str(status.get("version", "") or ""),
                "digest": str(status.get("digest", "") or ""),
                "changed_keys": list(status.get("changed_keys", []) or []),
                "operator_message_keys": sorted(
                    str(key)
                    for key in dict(status.get("operator_messages") or {})
                ),
            }
        else:
            previous = dict(row.get("previous") or {})
            event = {
                "kind": kind,
                "overlay_id": str(previous.get("overlay_id", "") or ""),
                "version": str(previous.get("version", "") or ""),
                "digest": str(previous.get("digest", "") or ""),
                "changed_keys": list(previous.get("changed_keys", []) or []),
            }
        self._append_history(event)
        payload = dict(status)
        payload["change_history"] = list(self._history)
        self._status = PolicyOverlayStatus(
            **{
                field_name: payload.get(field_name, default)
                for field_name, default in {
                    "active": False,
                    "overlay_id": "",
                    "version": "",
                    "path": str(self.path),
                    "digest": "",
                    "applied_ts": 0.0,
                    "changed_keys": [],
                    "category_counts": {},
                    "operator_messages": {},
                    "precedence": {},
                    "change_history": [],
                    "last_error": "",
                }.items()
            }
        )
        self._active_fingerprint = row.get("candidate_fingerprint")
        self._active_digest = str(row.get("candidate_digest", "") or "")
        return self._status.to_dict()

    def reject_proposal(self, proposal: Mapping[str, Any], *, error: str) -> None:
        row = dict(proposal or {})
        self._status.last_error = str(error or "policy_overlay_commit_failed")
        self._append_history(
            {
                "kind": "policy_overlay_commit_rejected",
                "candidate_digest": str(row.get("candidate_digest", "") or ""),
                "error": self._status.last_error,
            }
        )
        self._status.change_history = list(self._history)

    def refresh(self, *, force: bool = False) -> Optional[Dict[str, Any]]:
        del force
        raise RuntimeError("mutating_policy_overlay_refresh_path_deleted")
