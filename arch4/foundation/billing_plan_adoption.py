"""Approved boundary adapter: one-time R14.5 billing-plan adoption.

The receipt only records assignment. OperatorSettings remains the plan owner;
there is no legacy lookup in the live budget reader.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from arch4.atomic_io import atomic_write_json
from arch4.core_contracts.network_settings import subscription_key
from arch4.customer_contracts.handoff_identity import confirmed_handoff_identity_matches, handoff_identity_token
from arch4.customer_contracts.settings import TrafficPlan, traffic_plan_scope_id
from arch4.foundation.traffic_accounting import product_traffic_accounting_root

ADOPTION_NAME = "R14.5 billing-plan adoption"
SCHEMA = "FoundationBillingPlanAdoption.v1"


def adopt_r145_billing_plan(raw: Mapping[str, object], *, source_identity: Mapping[str, object],
                           confirmed_identity: Mapping[str, object], profile_id: str,
                           receipt_root: Path | None = None) -> TrafficPlan | None:
    if not confirmed_handoff_identity_matches(source_identity, confirmed_identity):
        return None
    subscription = subscription_key(str(confirmed_identity.get("imsi") or ""))
    network = str(confirmed_identity.get("provider_plmn") or "")
    if (not subscription or not network or network != str(raw.get("provider_plmn") or "")
            or profile_id != raw.get("profile_id")):
        return None
    backend = str(raw.get("modem_backend") or "")
    scope = traffic_plan_scope_id(subscription_key=subscription, modem_backend=backend,
        provider_name=str(raw.get("provider_name") or ""), provider_plmn=network, profile_id=profile_id)
    override = raw.get("override_enabled", True)
    if isinstance(override, str):
        if override.lower() not in {"true", "false"}:
            raise ValueError("billing_adoption_override_invalid")
        override = override.lower() == "true"
    plan = TrafficPlan(scope_id=scope, subscription_key=subscription, modem_backend=backend,
        provider_name=str(raw.get("provider_name") or ""), provider_plmn=network, profile_id=profile_id,
        monthly_allowance_gb=raw["monthly_allowance_gb"], cycle_start_day=raw["cycle_start_day"],
        override_enabled=override)
    physical = ("serial:" + handoff_identity_token(source_identity["serial"])
                if source_identity.get("serial") else "imei:" + handoff_identity_token(source_identity.get("imei")))
    key = hashlib.sha256(f"{physical}|{backend}|{profile_id}|{network}".encode()).hexdigest()
    path = (receipt_root or product_traffic_accounting_root()) / "r145_billing_plan_adoptions.json"
    receipt = json.loads(path.read_text()) if path.exists() else dict(schema=SCHEMA, name=ADOPTION_NAME, claims={})
    if receipt.get("schema") != SCHEMA or receipt.get("name") != ADOPTION_NAME or not isinstance(receipt.get("claims"), dict):
        raise ValueError("billing_adoption_receipt_invalid")
    claims = receipt["claims"]
    if key in claims:
        if claims[key] != dict(subscription_key=subscription, scope_id=scope):
            return None
    else:
        # Commit the assignment before the plan. Retrying after interruption
        # can complete that assignment; a different SIM can never claim it.
        claims[key] = dict(subscription_key=subscription, scope_id=scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, receipt, sort_keys=True, trailing_newline=True)
    return plan
