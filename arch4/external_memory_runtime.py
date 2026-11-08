from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from arch4.coerce import safe_bool
from arch4.external_memory_learning_projection import learning_prior_from_external_memory_pack
from arch4.evidence_scope import EvidenceScope, validated_evidence_scope
from arch4.external_memory_evidence_scope import validated_external_projection_scope
from arch4.external_memory_promotion import DEFAULT_IMPORT_MARKER_NAME, DEFAULT_PRIOR_PACK_NAME
from arch4.historical_continuity_store import (
    AUTO_STATUS_FILENAME,
    IMPORT_MARKER_FILENAME,
    PRIOR_PACK_FILENAME,
    committed_artifact_path,
    committed_generation_status,
)

AUTO_IMPORT_STATUS_NAME = "external_memory_auto_import_status.json"
AUTO_IMPORT_SCHEMA = "external_memory_auto_import_status_v1"
EXTERNAL_MEMORY_PRIOR_REGISTER_SCHEMA = "external_memory_prior_register_v1"
EXTERNAL_MEMORY_PRIOR_REGISTER_OWNER = "external_memory_runtime"
DEFAULT_EXTERNAL_MEMORY_PRIOR_REFRESH_S = 30.0


def _safe_json_load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _workspace_pack_path(memory_dir: str | Path) -> Path:
    return Path(memory_dir).resolve() / DEFAULT_PRIOR_PACK_NAME


def _workspace_marker_path(memory_dir: str | Path) -> Path:
    return Path(memory_dir).resolve() / DEFAULT_IMPORT_MARKER_NAME


def _workspace_auto_status_path(memory_dir: str | Path) -> Path:
    return Path(memory_dir).resolve() / AUTO_IMPORT_STATUS_NAME


def _auto_status_path(path: Path) -> dict[str, Any]:
    status = _safe_json_load(path)
    if status.get("schema") != AUTO_IMPORT_SCHEMA:
        return {
            "present": False,
            "status": "not_attempted",
            "authority": "automatic_promotion_gate",
            "runtime_influence": "none",
            "live_truth_override": False,
        }
    status["present"] = True
    status.setdefault("live_truth_override", False)
    return status


def load_external_memory_prior_pack(memory_dir: str | Path) -> dict[str, Any]:
    path = committed_artifact_path(
        memory_dir,
        PRIOR_PACK_FILENAME,
        verify_sha256=True,
    )
    if path is None:
        return {}
    pack = _safe_json_load(path)
    if pack.get("schema") != "external_memory_prior_pack_v1":
        return {}
    return pack


def load_external_memory_promotion_workspace_pack(
    memory_dir: str | Path,
) -> dict[str, Any]:
    """Read one private promotion workspace; never use this in live runtime."""

    pack = _safe_json_load(_workspace_pack_path(memory_dir))
    if pack.get("schema") != "external_memory_prior_pack_v1":
        return {}
    return pack


def _identity_binding(identity: Mapping[str, Any] | None) -> dict[str, str]:
    source = identity if isinstance(identity, Mapping) else {}
    nested: list[Mapping[str, Any]] = [source]
    for key in (
        "compatibility_identity",
        "runtime_identity",
        "resolved_identity_card",
        "verified_modem_identity",
        "identity",
        "evidence_scope",
    ):
        value = source.get(key)
        if isinstance(value, Mapping):
            nested.append(value)
    merged: dict[str, Any] = {}
    for row in nested:
        for key in (
            "identity_code",
            "identity_status",
            "provider_plmn",
            "catalog_version",
            "catalog_checksum",
            "identity_catalog_version",
            "identity_catalog_checksum",
            "access_type_id",
            "profile_id",
            "session_scope_key",
            "source_epoch",
            "source_sequence",
        ):
            value = row.get(key)
            if value not in (None, ""):
                merged[key] = value
        catalog = row.get("decode_catalog_snapshot")
        if isinstance(catalog, Mapping):
            merged.setdefault("catalog_version", catalog.get("catalog_version"))
            merged.setdefault("catalog_checksum", catalog.get("catalog_checksum"))
    merged.setdefault("catalog_version", merged.get("identity_catalog_version"))
    merged.setdefault("catalog_checksum", merged.get("identity_catalog_checksum"))
    return {
        key: str(merged.get(key, "") or "").strip()
        for key in (
            "identity_code",
            "identity_status",
            "provider_plmn",
            "catalog_version",
            "catalog_checksum",
            "identity_catalog_version",
            "identity_catalog_checksum",
            "access_type_id",
            "profile_id",
            "session_scope_key",
            "source_epoch",
            "source_sequence",
        )
    }


def _current_evidence_scope(
    identity: Mapping[str, Any] | None,
) -> EvidenceScope | None:
    source = identity if isinstance(identity, Mapping) else {}
    candidates: list[Mapping[str, Any]] = []
    if source.get("schema") == "EvidenceScope.v1":
        candidates.append(source)
    for row in (
        source,
        source.get("runtime_identity")
        if isinstance(source.get("runtime_identity"), Mapping)
        else {},
        source.get("accepted_generation")
        if isinstance(source.get("accepted_generation"), Mapping)
        else {},
    ):
        scope = row.get("evidence_scope") if isinstance(row, Mapping) else None
        if isinstance(scope, Mapping):
            candidates.append(scope)
    for candidate in candidates:
        try:
            return validated_evidence_scope(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _prior_activation(
    pack: Mapping[str, Any],
    current_identity: Mapping[str, Any] | None,
    learning_prior: Mapping[str, Any],
) -> dict[str, Any]:
    current = _identity_binding(current_identity)
    target = _identity_binding(
        pack.get("target_identity")
        if isinstance(pack.get("target_identity"), Mapping)
        else {}
    )
    reason = ""
    current_scope = _current_evidence_scope(current_identity)
    try:
        target_scope = validated_external_projection_scope(
            pack.get("target_evidence_scope")
            if isinstance(pack.get("target_evidence_scope"), Mapping)
            else {}
        )
    except (TypeError, ValueError):
        target_scope = None
    if current_scope is None:
        reason = "current_exact_evidence_scope_unavailable"
    elif target_scope is None:
        reason = "prior_target_evidence_scope_invalid"
    elif not target_scope.same_physical_namespace(current_scope):
        if target_scope.identity_code != current_scope.identity_code:
            reason = "prior_target_identity_mismatch"
        elif (
            target_scope.identity_catalog_version
            != current_scope.identity_catalog_version
            or target_scope.identity_catalog_checksum
            != current_scope.identity_catalog_checksum
        ):
            reason = "prior_identity_catalog_mismatch"
        elif target_scope.session_scope_key != current_scope.session_scope_key:
            reason = "prior_target_session_mismatch"
        else:
            reason = "prior_target_source_epoch_mismatch"
    elif (
        target_scope.provider_plmn
        and current_scope.provider_plmn
        and target_scope.provider_plmn != current_scope.provider_plmn
    ):
        reason = "prior_target_provider_mismatch"
    elif not safe_bool(learning_prior.get("present", False), False):
        reason = str(learning_prior.get("reason") or "no_operational_learning_prior")
    elif int(learning_prior.get("sample_count", 0) or 0) <= 0:
        reason = "operational_learning_prior_empty"
    active = not reason
    return {
        "schema": "ExternalMemoryPriorActivation.v1",
        "owner": EXTERNAL_MEMORY_PRIOR_REGISTER_OWNER,
        "active": active,
        "reason": reason or "identity_matched_operational_projection",
        "current_identity": current,
        "target_identity": target,
        "current_evidence_scope": (
            current_scope.as_dict() if current_scope is not None else {}
        ),
        "target_evidence_scope": (
            target_scope.as_dict() if target_scope is not None else {}
        ),
    }


def _prior_status_from_paths(
    *,
    pack_path: Path,
    marker_path: Path,
    auto_status_path: Path,
    current_identity: Mapping[str, Any] | None,
    missing_reason: str,
    commit_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    pack = _safe_json_load(pack_path)
    marker = _safe_json_load(marker_path)
    auto_status = _auto_status_path(auto_status_path)
    if not pack:
        return {
            "present": False,
            "runtime_influence": "none",
            "authority": "none",
            "prior_pack_path": str(pack_path),
            "import_marker_path": str(marker_path),
            "fact_count": 0,
            "observation_count": 0,
            "scope_tiers": [],
            "allowed_consumers": [],
            "disallowed_consumers": [],
            "target_identity": {},
            "target_evidence_scope": {},
            "activation": {
                "schema": "ExternalMemoryPriorActivation.v1",
                "owner": EXTERNAL_MEMORY_PRIOR_REGISTER_OWNER,
                "active": False,
                "reason": str(missing_reason or "prior_pack_missing"),
            },
            "auto_import": auto_status,
            "historical_continuity_commit": dict(commit_status or {}),
        }

    summary = pack.get("summary") if isinstance(pack.get("summary"), dict) else {}
    fact_count = int(summary.get("fact_count") or 0)
    observation_count = int(summary.get("observation_count") or 0)
    allowed_consumers = list(pack.get("allowed_consumers") or [])
    disallowed_consumers = list(pack.get("disallowed_consumers") or [])
    projected_learning_prior = learning_prior_from_external_memory_pack(pack)
    activation = _prior_activation(pack, current_identity, projected_learning_prior)
    historical_seed_active = bool(activation.get("active", False))
    learning_prior = dict(projected_learning_prior)
    if not historical_seed_active:
        learning_prior.update(
            {
                "present": False,
                "runtime_influence": "none",
                "reason": str(activation.get("reason") or "prior_inactive"),
            }
        )
    return {
        "present": True,
        "runtime_influence": "advisory_prior" if historical_seed_active else "none",
        "runtime_influence_kind": "weighted_historical_seed",
        "authority": str(pack.get("authority") or "imported_prior_advisory"),
        "prior_pack_path": str(pack_path),
        "import_marker_path": str(marker_path),
        "imported_at_ts": marker.get("imported_at_ts"),
        "promotion_plan": str(marker.get("promotion_plan") or ""),
        "fact_count": fact_count,
        "observation_count": observation_count,
        "scope_tiers": list(summary.get("scope_tiers") or []),
        "allowed_consumers": allowed_consumers,
        "disallowed_consumers": disallowed_consumers,
        "target_identity": dict(pack.get("target_identity") or {}),
        "target_evidence_scope": dict(pack.get("target_evidence_scope") or {}),
        "activation": activation,
        "historical_ltm_seed_active": historical_seed_active,
        "brain_prior_active": historical_seed_active,
        "learning_prior": learning_prior,
        "operational_learning_prior": dict(projected_learning_prior),
        "live_sample_weight": "highest_recency_authority",
        "historical_sample_weight": "decayed_by_age_and_scope",
        "semantic_boundary": "history_seeds_ltm_and_prediction; live_samples_remain_recency_authority",
        "live_truth_override": safe_bool(marker.get("live_truth_override", False), False),
        "auto_import": auto_status,
        "historical_continuity_commit": dict(commit_status or {}),
    }


def external_memory_promotion_workspace_status(
    memory_dir: str | Path,
    *,
    current_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect one private promotion workspace before atomic publication."""

    memory = Path(memory_dir).resolve()
    return _prior_status_from_paths(
        pack_path=_workspace_pack_path(memory),
        marker_path=_workspace_marker_path(memory),
        auto_status_path=_workspace_auto_status_path(memory),
        current_identity=current_identity,
        missing_reason="promotion_workspace_prior_pack_missing",
        commit_status={
            "present": False,
            "reason": "private_promotion_workspace",
        },
    )


def external_memory_prior_status(
    memory_dir: str | Path,
    *,
    current_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read only the atomically committed historical continuity generation."""

    memory = Path(memory_dir).resolve()
    commit_status = committed_generation_status(memory)
    if not commit_status.get("present"):
        rejected_root_artifacts = [
            path.name
            for path in (
                _workspace_pack_path(memory),
                _workspace_marker_path(memory),
                _workspace_auto_status_path(memory),
            )
            if path.is_file()
        ]
        missing_reason = str(
            commit_status.get("reason") or "historical_continuity_commit_missing"
        )
        if rejected_root_artifacts:
            missing_reason = "uncommitted_root_prior_artifacts_rejected"
        status = _prior_status_from_paths(
            pack_path=memory / ".committed_history_absent",
            marker_path=memory / ".committed_marker_absent",
            auto_status_path=memory / ".committed_status_absent",
            current_identity=current_identity,
            missing_reason=missing_reason,
            commit_status=commit_status,
        )
        status["rejected_uncommitted_root_artifacts"] = rejected_root_artifacts
        status["source_contract"] = "atomic_committed_generation_only"
        return status

    pack_path = committed_artifact_path(
        memory,
        PRIOR_PACK_FILENAME,
        verify_sha256=True,
    )
    marker_path = committed_artifact_path(
        memory,
        IMPORT_MARKER_FILENAME,
        verify_sha256=True,
    )
    auto_status_path = committed_artifact_path(
        memory,
        AUTO_STATUS_FILENAME,
        verify_sha256=True,
    )
    if pack_path is None or marker_path is None or auto_status_path is None:
        raise RuntimeError("committed historical continuity artifacts unavailable")
    status = _prior_status_from_paths(
        pack_path=pack_path,
        marker_path=marker_path,
        auto_status_path=auto_status_path,
        current_identity=current_identity,
        missing_reason="committed_prior_pack_invalid",
        commit_status=commit_status,
    )
    status["source_contract"] = "atomic_committed_generation_only"
    return status


def _engine_prior_register(engine: Any) -> dict[str, Any]:
    if engine is None:
        return {}
    register = getattr(engine, "_external_memory_prior_register", None)
    if isinstance(register, dict):
        return register
    register = {}
    try:
        setattr(engine, "_external_memory_prior_register", register)
    except Exception:
        return {}
    return register


def read_external_memory_prior_register(engine: Any, *, allow_stale: bool = True) -> dict[str, Any]:
    register = _engine_prior_register(engine)
    value = register.get("value") if isinstance(register.get("value"), Mapping) else {}
    if not value:
        return {}
    if not allow_stale and not safe_bool(register.get("valid", False), False):
        return {}
    return dict(value)


def invalidate_external_memory_prior_register(
    engine: Any,
    *,
    reason: str,
) -> dict[str, Any]:
    """Fail closed when the continuity owner's exact target generation changes."""

    register = _engine_prior_register(engine)
    register.clear()
    register.update(
        {
            "schema": EXTERNAL_MEMORY_PRIOR_REGISTER_SCHEMA,
            "owner": EXTERNAL_MEMORY_PRIOR_REGISTER_OWNER,
            "valid": False,
            "published_at_ts": time.time(),
            "identity_binding": {},
            "invalidation_reason": str(reason or "target_generation_changed"),
            "value": {},
        }
    )
    return dict(register)


def publish_external_memory_prior_register(
    engine: Any,
    memory_dir: str | Path,
    *,
    now: float | None = None,
    force: bool = False,
    min_refresh_s: float = DEFAULT_EXTERNAL_MEMORY_PRIOR_REFRESH_S,
    current_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Canonical live-runtime owner for imported historical prior status.

    The runtime adapter resolves only the atomic generation pointer. Explicit
    promotion workspaces use a separate non-runtime inspection function.
    """
    current_ts = time.time() if now is None else float(now)
    register = _engine_prior_register(engine)
    current_binding = _identity_binding(current_identity)
    if register and not force:
        value = register.get("value") if isinstance(register.get("value"), Mapping) else {}
        try:
            published_at = float(register.get("published_at_ts", 0.0) or 0.0)
        except Exception:
            published_at = 0.0
        if (
            value
            and register.get("identity_binding") == current_binding
            and published_at > 0.0
            and (current_ts - published_at) < max(0.0, float(min_refresh_s))
        ):
            return dict(value)

    value = external_memory_prior_status(
        memory_dir,
        current_identity=current_identity,
    )
    surface = dict(value)
    surface.setdefault("owner", EXTERNAL_MEMORY_PRIOR_REGISTER_OWNER)
    surface.setdefault("source", "external_memory_prior_register")
    surface.setdefault("published_at_ts", current_ts)
    register.clear()
    register.update(
        {
            "schema": EXTERNAL_MEMORY_PRIOR_REGISTER_SCHEMA,
            "owner": EXTERNAL_MEMORY_PRIOR_REGISTER_OWNER,
            "valid": True,
            "memory_dir": str(Path(memory_dir).resolve()),
            "published_at_ts": current_ts,
            "identity_binding": current_binding,
            "value": dict(surface),
        }
    )
    return surface
