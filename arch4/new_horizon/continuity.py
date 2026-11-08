"""Resolve learning continuity, without changing live modem facts.

No Service is a condition of the current radio, not a new radio technology.
Only this boundary may retain the last confirmed provider/RAT for history.
"""

from __future__ import annotations

import hashlib
from arch4.core_contracts.quality_model import NR_QUALITY_MODEL, LTE_QUALITY_MODEL, require_quality_model
from typing import Any, Mapping
from arch4.customer_contracts.handoff_identity import (
    confirmed_handoff_identity_matches, handoff_identity_token,
)


CONTINUITY_PATHS = ("identity.profile_id", "identity.provider_name", "radio.network_type")
LEARNING_SCOPE_FIELDS = ("learning_subscription_key", "learning_network_plmn")
INITIAL_SIM_CHECKPOINT = "initial_sim_network_v1"
ABSENT = frozenset({"", "---", "N/A", "NA", "UNKNOWN", "NO SERVICE", "NO_SERVICE",
                    "DISCONNECTED", "NOT REGISTERED", "SEARCHING"})


def read_confirmed_device_associations(path) -> tuple[dict, ...]:
    """Project existing confirmed bindings without activating/migrating History."""
    from pathlib import Path
    from arch4.new_horizon.history import export_history_checkpoints
    source=Path(path)
    if not source.exists():
        return ()
    result=[]; current_scopes=set()
    for row in export_history_checkpoints(source,name='confirmed_network_v2'):
        payload=row['payload']
        if payload.get('schema')!='NewHorizonDeviceContinuity.v2':
            raise ValueError('confirmed_device_association_schema_invalid')
        physical=dict(payload['physical_identity']); profile=str(payload['binding']['identity.profile_id'])
        if not profile or not any(handoff_identity_token(physical.get(k)) for k in ('serial','imei')):
            raise ValueError('confirmed_device_association_identity_invalid')
        result.append({'physical_identity':physical,'profile_id':profile})
        current_scopes.add(payload['physical_scope'])
    # Existing named v1 historical adapter; never a second live writer/store.
    for row in export_history_checkpoints(source,name='confirmed_network_v1'):
        payload=row['payload']
        if payload.get('schema')!='NewHorizonDeviceContinuity.v1':
            raise ValueError('confirmed_device_association_v1_schema_invalid')
        scope=str(payload['physical_scope'])
        if scope in current_scopes:
            continue
        physical=dict(part.split('=', 1) for part in scope.split('|') if '=' in part)
        profile=str(payload['binding']['identity.profile_id'])
        if not profile or not any(handoff_identity_token(physical.get(k)) for k in ('serial','imei')):
            raise ValueError('confirmed_device_association_identity_invalid')
        result.append({'physical_identity':physical,'profile_id':profile})
    return tuple(result)


def continuity_value(path: str, raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    upper = value.upper()
    if upper in ABSENT:
        return None
    if path == "radio.network_type":
        return ("NR_SA" if "NR SA" in upper or upper == "NR_SA" else
                "NR_NSA" if "NR NSA" in upper or upper == "NR_NSA" else
                "LTE" if "LTE" in upper or "4G" in upper else value)
    return value


def resolve_continuity(*, identity_code: str, values: Mapping[str, Any],
                       confirmed: Mapping[str, Any]) -> dict[str, Any]:
    """Use current confirmation, retaining absence only for the same identity."""
    if type(identity_code) is not str or not identity_code:
        raise ValueError("new_horizon_continuity_identity_code_required")
    previous = confirmed if confirmed.get("identity_code") == identity_code else {}
    result: dict[str, Any] = {"identity_code": identity_code}
    result["quality_model"] = require_quality_model(values["quality_model"])
    for path in CONTINUITY_PATHS:
        current = continuity_value(path, values.get(path))
        result[path] = current if current is not None else previous.get(path)
    for key in ("history_device_key", "history_aliases", "initial_sim_network"):
        if previous.get(key):
            result[key] = previous[key]
    # Current intake owns these facts. A durable last-device checkpoint must
    # never supply a SIM or visited network missing from this connection.
    if any(key in values for key in LEARNING_SCOPE_FIELDS):
        result.update({key: values.get(key) for key in LEARNING_SCOPE_FIELDS})
    return result


def learning_device_key(binding: Mapping[str, Any]) -> str:
    """Separate learning inside the physical router's existing History store.

    The first confirmed SIM/network owns the existing physical learning key.
    Later subscriptions use distinct keys; old measurements stay in place.
    """
    physical = binding.get("history_device_key") or binding["identity_code"]
    model = require_quality_model(binding["quality_model"])
    if model != LTE_QUALITY_MODEL:
        physical = "|".join((physical, model))
    if "learning_subscription_key" not in binding or owns_initial_sim_history(binding):
        return physical
    return "|".join((physical, "sim-network-v1", *(binding.get(key) or "unverified" for key in LEARNING_SCOPE_FIELDS)))


def owns_initial_sim_history(binding: Mapping[str, Any]) -> bool:
    assigned = binding.get("initial_sim_network")
    return isinstance(assigned, Mapping) and all(
        binding.get(key) and binding.get(key) == assigned.get(key)
        for key in LEARNING_SCOPE_FIELDS)


def continuity_digest(binding: Mapping[str, Any]) -> str:
    values = [learning_device_key(binding)]
    values.extend(binding.get(path) or f"unavailable:{path}" for path in CONTINUITY_PATHS)
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()


def bind_device_history(history: Any, *, physical_scope: str, current: Mapping[str, Any],
                        observed_at: float, identity_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Use Modem Live's physical namespace; adopt only proven old profile keys."""
    if type(physical_scope) is not str or not physical_scope:
        raise ValueError("new_horizon_physical_history_scope_required")
    scope = "device-learning:" + physical_scope
    permanent_scope = any(part.startswith(("serial=", "imei=")) and part.split("=", 1)[1]
                          for part in physical_scope.split("|"))
    evidence = dict(identity_evidence or {})
    physical = dict(evidence.get("current") or dict(
        part.split("=", 1) for part in physical_scope.split("|") if "=" in part))
    scope_identity = dict(part.split("=", 1) for part in physical_scope.split("|") if "=" in part)
    if permanent_scope and not confirmed_handoff_identity_matches(physical, scope_identity):
        raise ValueError("new_horizon_confirmed_physical_identity_mismatch")
    saved = history.load_checkpoint(scope_key=scope, name="confirmed_network_v2")
    if saved is None and permanent_scope:
        checkpoints = history.checkpoints_named("confirmed_network_v2")
        for row in checkpoints:
            retained = row["payload"]["physical_identity"]
            overlaps = any(handoff_identity_token(physical.get(key))
                           and handoff_identity_token(physical.get(key)) == handoff_identity_token(retained.get(key))
                           for key in ("serial", "imei"))
            if overlaps and not confirmed_handoff_identity_matches(retained, physical):
                raise ValueError("new_horizon_saved_physical_identity_conflict")
        matches = [row for row in checkpoints
                   if confirmed_handoff_identity_matches(row["payload"]["physical_identity"], physical)]
        if len(matches) > 1:
            raise ValueError("new_horizon_physical_history_binding_ambiguous")
        if matches:
            saved = matches[0]["payload"]
            scope = matches[0]["scope_key"]
    if saved is not None and saved.get("schema") != "NewHorizonDeviceContinuity.v2":
        raise ValueError("new_horizon_device_continuity_checkpoint_invalid")
    if saved is not None and permanent_scope and not confirmed_handoff_identity_matches(saved["physical_identity"], physical):
        raise ValueError("new_horizon_saved_physical_identity_conflict")
    previous = dict(saved["binding"]) if saved is not None else {}
    # Existing serial ownership can be enriched by IMEI, or vice versa. It
    # does not change the stable namespace already supplied by Modem Live.
    device_key = saved["physical_scope"] if saved else physical_scope
    if not permanent_scope:
        device_key = history.provisional_device_key(physical_scope)
    aliases = {(a["identity_code"], a["profile_id"]) for a in (saved or {}).get("aliases", ())}
    proven_aliases = {(a["identity_code"], a["profile_id"]) for a in (saved or {}).get("legacy_sources", ())}
    old = history.load_checkpoint(scope_key="device-learning:" + physical_scope, name="confirmed_network_v1")
    if old is not None:
        if old.get("schema") != "NewHorizonDeviceContinuity.v1" or old.get("physical_scope") != physical_scope:
            raise ValueError("new_horizon_device_continuity_checkpoint_invalid")
        if permanent_scope:
            binding = old["binding"]
            proven_aliases.add((binding.get("history_identity_code") or binding["identity_code"],
                                binding.get("history_profile_id") or binding["identity.profile_id"]))
            proven_aliases.add((binding["identity_code"], binding["identity.profile_id"]))
            if not previous:
                previous = dict(binding)
    for link in evidence.get("links", ()):
        source = link["identity"]
        if not permanent_scope or not confirmed_handoff_identity_matches(source, physical):
            raise ValueError("new_horizon_history_alias_physical_proof_required")
        if not source.get("identity_code") or not source.get("profile_id"):
            continue
        proven_aliases.add((source["identity_code"], source["profile_id"]))
    aliases.update(proven_aliases)
    if current.get("identity.profile_id"):
        aliases.add((current["identity_code"], current["identity.profile_id"]))
    previous["identity_code"] = current["identity_code"]
    result = resolve_continuity(identity_code=current["identity_code"],
        values=current, confirmed=previous)
    result["history_device_key"] = device_key
    result["history_aliases"] = [dict(identity_code=code, profile_id=profile) for code, profile in sorted(proven_aliases)]
    if result.get("quality_model") == NR_QUALITY_MODEL:
        # New 5G coordinates start empty. Physical associations remain recorded;
        # no older grades, forecasts or baseline checkpoints are adopted.
        result["history_aliases"] = []
    initial_sim = history.load_checkpoint(scope_key=scope, name=INITIAL_SIM_CHECKPOINT)
    if initial_sim is not None and (
            initial_sim.get("schema") != "NewHorizonInitialSimNetwork.v1"
            or not all(initial_sim.get(key) for key in LEARNING_SCOPE_FIELDS)):
        raise ValueError("new_horizon_initial_sim_network_checkpoint_invalid")
    if (initial_sim is None and permanent_scope
            and all(result.get(key) for key in (*CONTINUITY_PATHS, *LEARNING_SCOPE_FIELDS))):
        initial_sim = dict(schema="NewHorizonInitialSimNetwork.v1",
                           **{key: result[key] for key in LEARNING_SCOPE_FIELDS})
        # Persist ownership before any migration. An interrupted first login
        # must not let another SIM claim the same retained learning.
        history.save_checkpoint(scope_key=scope, name=INITIAL_SIM_CHECKPOINT,
                                updated_at=observed_at, payload=initial_sim)
        history.flush()
    if initial_sim is not None:
        result["initial_sim_network"] = initial_sim
    if (result["quality_model"] == LTE_QUALITY_MODEL and permanent_scope and ("learning_subscription_key" not in result or owns_initial_sim_history(result))
            and all(result.get(path) for path in CONTINUITY_PATHS)):
        for code, profile in sorted(proven_aliases):
            # Different profiles are not silently asserted equivalent. They
            # keep their own context under the same physical router.
            legacy = {key: value for key, value in result.items() if key not in LEARNING_SCOPE_FIELDS}
            source = resolve_continuity(identity_code=code, values={**legacy, "identity.profile_id": profile}, confirmed={})
            target = {**source, "history_device_key": device_key}
            history.migrate_confirmed_device_scope(source=source, target=target, observed_at=observed_at)
        if owns_initial_sim_history(result):
            # Preserve evidence already collected by the unreleased R14.2
            # source before first-run adoption was added.
            scoped = {key: value for key, value in result.items() if key != "initial_sim_network"}
            history.migrate_confirmed_device_scope(source=scoped, target=result, observed_at=observed_at)
    retained_physical = dict((saved or {}).get("physical_identity", {}))
    retained_physical.update({key: value for key, value in physical.items()
                             if key not in {"serial", "imei"} or handoff_identity_token(value)})
    payload = dict(schema="NewHorizonDeviceContinuity.v2", physical_scope=device_key,
                   physical_identity=retained_physical,
                   aliases=[dict(identity_code=code, profile_id=profile) for code, profile in sorted(aliases)],
                   legacy_sources=result["history_aliases"], binding=result)
    if saved != payload:
        history.save_checkpoint(scope_key=scope, name="confirmed_network_v2", updated_at=observed_at, payload=payload)
    return result
