"""Foundation-owned operator/site and exact-scope traffic-plan persistence."""

from __future__ import annotations

import json
from pathlib import Path

from arch4.atomic_io import atomic_write_json
from arch4.customer_contracts.settings import (
    FoundationOperatorSettings,
    TRAFFIC_PLAN_DEFAULT_ALLOWANCE_GB,
    TRAFFIC_PLAN_DEFAULT_CYCLE_START_DAY,
    TrafficPlan,
)


SCHEMA = "FoundationOperatorSettings.v3"
PREVIOUS_SCHEMA = "FoundationOperatorSettings.v1"
OWNER = "arch4.foundation.operator_settings"
FILENAME = "foundation_operator_settings.json"


class FoundationOperatorSettingsError(RuntimeError):
    pass


class FoundationOperatorSettingsStore:
    def __init__(self, storage_root: str | Path) -> None:
        self.storage_root = Path(storage_root).resolve()
        self.path = self.storage_root / FILENAME

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> FoundationOperatorSettings:
        if not self.path.is_file():
            return FoundationOperatorSettings()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise FoundationOperatorSettingsError(
                f"Foundation operator settings cannot be read: {exc}"
            ) from exc
        expected = {"schema", "owner", "operator_fields", "traffic_plans"}
        if isinstance(payload, dict) and payload.get("schema") in {SCHEMA, "FoundationOperatorSettings.v2"}:
            expected.add("apn_profiles")
        if isinstance(payload, dict) and payload.get("schema") == SCHEMA:
            expected.add("unassigned_traffic_plans")
        if not isinstance(payload, dict) or set(payload) != expected:
            raise FoundationOperatorSettingsError(
                "Foundation operator settings fields mismatch"
            )
        if payload["schema"] not in {SCHEMA, PREVIOUS_SCHEMA, "FoundationOperatorSettings.v2"} or payload["owner"] != OWNER:
            raise FoundationOperatorSettingsError(
                "Foundation operator settings owner mismatch"
            )
        if not isinstance(payload["operator_fields"], dict) or not isinstance(
            payload["traffic_plans"], dict
        ):
            raise FoundationOperatorSettingsError(
                "Foundation operator settings collections are invalid"
            )
        if payload["schema"] == SCHEMA and not isinstance(payload["apn_profiles"], dict):
            raise FoundationOperatorSettingsError("Foundation APN profiles must be an object")
        try:
            unassigned = dict(payload.get("unassigned_traffic_plans", {}))
            if payload["schema"] != SCHEMA:
                unassigned.update(payload["traffic_plans"])
            plans = {
                str(scope_id): TrafficPlan(**raw)
                for scope_id, raw in payload["traffic_plans"].items()
                if isinstance(raw, dict) and payload["schema"] == SCHEMA
            }
            if payload["schema"] == SCHEMA and len(plans) != len(payload["traffic_plans"]):
                raise ValueError("traffic plan record must be an object")
            return FoundationOperatorSettings(
                unassigned_traffic_plans=unassigned,
                operator_fields=payload["operator_fields"],
                traffic_plans=plans,
                apn_profiles=payload.get("apn_profiles", {}),
            )
        except (TypeError, ValueError) as exc:
            raise FoundationOperatorSettingsError(str(exc)) from exc

    def save(self, settings: FoundationOperatorSettings) -> None:
        if type(settings) is not FoundationOperatorSettings:
            raise TypeError(
                "FoundationOperatorSettingsStore requires FoundationOperatorSettings"
            )
        atomic_write_json(
            self.path,
            {
                "schema": SCHEMA,
                "owner": OWNER,
                "operator_fields": dict(settings.operator_fields),
                "unassigned_traffic_plans": {key: dict(row) for key, row in settings.unassigned_traffic_plans.items()},
                "apn_profiles": {key: profile.to_mapping() for key, profile in sorted(settings.apn_profiles.items())},
                "traffic_plans": {
                    scope_id: {
                        "scope_id": plan.scope_id,
                        "modem_backend": plan.modem_backend,
                        "provider_name": plan.provider_name,
                        "provider_plmn": plan.provider_plmn,
                        "profile_id": plan.profile_id,
                        "subscription_key": plan.subscription_key,
                        "monthly_allowance_gb": plan.monthly_allowance_gb,
                        "cycle_start_day": plan.cycle_start_day,
                        "override_enabled": plan.override_enabled,
                    }
                    for scope_id, plan in sorted(settings.traffic_plans.items())
                },
            },
            sort_keys=True,
            trailing_newline=True,
        )
    def load_traffic_plan(self, scope_id: str) -> dict[str, object]:
        scope = str(scope_id or "").strip()
        if not scope:
            raise ValueError("exact traffic-plan scope_id is required")
        plan = self.load().traffic_plans.get(scope)
        if plan is None:
            return {
                "scope_id": scope,
                "scoped": False,
                "override_enabled": False,
                "monthly_allowance_gb": TRAFFIC_PLAN_DEFAULT_ALLOWANCE_GB,
                "cycle_start_day": TRAFFIC_PLAN_DEFAULT_CYCLE_START_DAY,
            }
        return {
            "scope_id": plan.scope_id,
            "scoped": True,
            "override_enabled": plan.override_enabled,
            "monthly_allowance_gb": plan.monthly_allowance_gb,
            "cycle_start_day": plan.cycle_start_day,
        }

    def save_traffic_plan(self, plan: TrafficPlan) -> None:
        if type(plan) is not TrafficPlan:
            raise TypeError("save_traffic_plan requires TrafficPlan")
        current = self.load()
        plans = dict(current.traffic_plans)
        plans[plan.scope_id] = plan
        self.save(
            FoundationOperatorSettings(
                unassigned_traffic_plans=current.unassigned_traffic_plans,
                operator_fields=current.operator_fields,
                traffic_plans=plans,
                apn_profiles=current.apn_profiles,
            )
        )
