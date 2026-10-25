from __future__ import annotations

from typing import Any, Dict, Mapping

from arch4.policy.family_status_policy import apply_family_status_aliases
from arch4.runtime_identity import backend_family_from_identity_surfaces


def apply_engine_runtime_surface_aliases(*, backend: str, extra: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(extra or {})
    family = backend_family_from_identity_surfaces(out, out.get("runtime_identity"), backend=backend)
    runtime_recovery = dict(out.get("runtime_recovery") or {})
    if runtime_recovery and family == "routeros_at_like":
        active = bool(runtime_recovery.get("active", False))
        runtime_recovery.setdefault("routeros_runtime_recovery_active", active)
        runtime_recovery.setdefault("family_runtime_recovery_active", active)
        runtime_recovery = apply_family_status_aliases(runtime_recovery)
        out["runtime_recovery"] = runtime_recovery
    brain = dict(out.get("brain") or {})
    planned = dict(brain.get("planned_radio_quiet") or {})
    if planned and family == "routeros_at_like":
        active = bool(planned.get("active", False))
        planned.setdefault("routeros_manual_radio_quiet_active", active)
        planned.setdefault("family_manual_radio_quiet_active", active)
        planned = apply_family_status_aliases(planned)
        brain["planned_radio_quiet"] = planned
        brain.setdefault("planned_radio_quiet_active", active)
    if brain:
        out["brain"] = brain
    return out
