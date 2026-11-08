"""Runtime sidecar identity helpers without sidecar writer dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


def normalize_identity_value(key: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if key in {"modem_backend", "backend_family", "profile_name"}:
        return text.lower()
    if key in {"runtime_dir", "memory_dir"}:
        try:
            return str(Path(text).expanduser())
        except Exception:
            return text
    return text


BACKEND_IDENTITY_ALIASES = {
    "huawei_b818": "b818",
    "huawei_hilink_b818": "b818",
    "huawei_b535": "b535",
    "huawei_hilink_b535": "b535",
    "huawei_hilink": "huawei_hilink_like",
    "hilink": "huawei_hilink_like",
    "routeros": "mikrotik",
    "routeros_api": "mikrotik",
    "mikrotik_routeros": "mikrotik",
    "zte_goform": "zte_goform_like",
    "openwrt_modemmanager": "openwrt_modemmanager_like",
}
BACKEND_FAMILY_IDENTITY_VALUES = {
    "huawei_hilink_like",
    "routeros_at_like",
    "zte_goform_like",
    "openwrt_modemmanager_like",
}
BACKEND_FAMILY_ALIASES = {
    "b818": "huawei_hilink_like",
    "b311": "huawei_hilink_like",
    "b535": "huawei_hilink_like",
    "b530": "huawei_hilink_like",
    "h112": "huawei_hilink_like",
    "h122": "huawei_hilink_like",
    "h155": "huawei_hilink_like",
    "huawei": "huawei_hilink_like",
    "mikrotik": "routeros_at_like",
    "routeros": "routeros_at_like",
    "routeros_api": "routeros_at_like",
    "openwrt": "openwrt_modemmanager_like",
    "mc888": "zte_goform_like",
    "g5b": "zte_goform_like",
    "mf296r": "zte_goform_like",
    "zte": "zte_goform_like",
    "zte_goform": "zte_goform_like",
    "zte_zwrt": "zte_zwrt_ubus_like",
    "nr5101": "zyxel_webui_like",
    "fwa510": "zyxel_webui_like",
    "model": "modem_model_like",
}


def infer_sidecar_identity_backend_family(backend: str) -> str:
    text = normalize_identity_value("modem_backend", backend)
    if text in BACKEND_FAMILY_IDENTITY_VALUES:
        return text
    return str(BACKEND_FAMILY_ALIASES.get(text, "") or "")


def build_expected_sidecar_identity(
    *,
    modem_backend: str = "",
    backend_family: str = "",
    runtime_dir: str = "",
    memory_dir: str = "",
    instance_id: str = "",
    profile_name: str = "",
) -> dict[str, str]:
    backend = normalize_identity_value("modem_backend", modem_backend)
    backend = BACKEND_IDENTITY_ALIASES.get(backend, backend)
    family = normalize_identity_value("backend_family", backend_family)
    if backend in BACKEND_FAMILY_IDENTITY_VALUES:
        if not family:
            family = backend
        backend = ""
    if backend and not family:
        family = normalize_identity_value("backend_family", infer_sidecar_identity_backend_family(backend))
    identity = {
        "modem_backend": backend,
        "backend_family": family,
        "runtime_dir": normalize_identity_value("runtime_dir", runtime_dir),
        "memory_dir": normalize_identity_value("memory_dir", memory_dir),
        "instance_id": normalize_identity_value("instance_id", instance_id),
        "profile_name": normalize_identity_value("profile_name", profile_name),
    }
    return {k: v for k, v in identity.items() if v}


def extract_sidecar_identity(
    payload: Any,
    *,
    source_path: Path | None = None,
) -> dict[str, str]:
    data = payload if isinstance(payload, dict) else {}
    provenance = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
    backend = (
        data.get("modem_backend")
        or data.get("backend")
        or provenance.get("modem_backend")
        or ""
    )
    family = (
        data.get("backend_family")
        or data.get("family")
        or provenance.get("backend_family")
        or ""
    )
    runtime_dir = (
        data.get("runtime_dir")
        or provenance.get("runtime_dir")
        or (str(source_path.parent) if source_path is not None else "")
    )
    memory_dir = data.get("memory_dir") or provenance.get("memory_dir") or ""
    instance_id = data.get("instance_id") or ""
    profile_name = data.get("profile_name") or data.get("profile") or provenance.get("profile_name") or ""
    return build_expected_sidecar_identity(
        modem_backend=str(backend or ""),
        backend_family=str(family or ""),
        runtime_dir=str(runtime_dir or ""),
        memory_dir=str(memory_dir or ""),
        instance_id=str(instance_id or ""),
        profile_name=str(profile_name or ""),
    )


def sidecar_identity_matches(
    payload: Any,
    *,
    expected: Mapping[str, Any] | None = None,
    source_path: Path | None = None,
) -> bool:
    src = dict(expected or {})
    required = build_expected_sidecar_identity(
        modem_backend=str(src.get("modem_backend", "") or ""),
        backend_family=str(src.get("backend_family", "") or ""),
        runtime_dir=str(src.get("runtime_dir", "") or ""),
        memory_dir=str(src.get("memory_dir", "") or ""),
        instance_id=str(src.get("instance_id", "") or ""),
        profile_name=str(src.get("profile_name", "") or src.get("profile", "") or ""),
    )
    if not required:
        return True
    actual = extract_sidecar_identity(payload, source_path=source_path)
    for key, expected_value in required.items():
        actual_value = normalize_identity_value(key, actual.get(key, ""))
        if not actual_value:
            return False
        if actual_value != expected_value:
            return False
    return True
