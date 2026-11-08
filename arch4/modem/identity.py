from __future__ import annotations

from typing import Any, Dict


def normalize_modem_identity_fields(status: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize model identity from hard modem evidence."""
    out = dict(status or {})
    backend = str(out.get("backend") or out.get("modem_backend") or "").strip().lower()
    model = str(out.get("model") or out.get("router_model") or "").strip()
    hardware = str(out.get("hardware_version") or "").strip()
    firmware = str(out.get("firmware") or out.get("software_version") or out.get("software") or "").strip()
    config_version = str(out.get("config_version") or out.get("configuration_file_version") or "").strip()

    if backend == "b818" and (not model or model in {"Huawei B818", "B818"}):
        hard_identity = " ".join((hardware, firmware, config_version)).upper()
        if "B818" in hard_identity or "H220SP1C11" in hard_identity:
            model = "B818-263"
            out.setdefault("model_source", "hard_identity")

    if model:
        out["model"] = model
        out.setdefault("router_model", model)
    return out


__all__ = ["normalize_modem_identity_fields"]
