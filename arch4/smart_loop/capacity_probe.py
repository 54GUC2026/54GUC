"""Measured DL capacity fact owner for SmartLoop.

The probe is evidence, not policy. It runs only under quiet-window gates and
publishes one owner-stamped register row for SmartLoop/Crystal Ball.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from arch4.runtime_clock import ClockedJobOutcome
from arch4.runtime_registers import (
    REGISTER_CAPACITY,
    REGISTER_DECISION,
    REGISTER_IDENTITY,
    REGISTER_LEARNING,
    REGISTER_MUTATION,
    REGISTER_RADIO,
    REGISTER_TRAFFIC,
    require_core_runtime_register_bank,
)
from arch4.runtime_decision_projection import (
    OWNER as DECISION_PROJECTION_OWNER,
    SCHEMA as DECISION_PROJECTION_SCHEMA,
)
from arch4.smart_loop.passive_dl_peak import build_passive_dl_peak_fact


SCHEMA = "capacity_evidence_register.v1"
ACTIVE_SCHEMA = "measured_capacity_fact.v1"
OWNER = "arch4.smart_loop.capacity_probe"
DEFAULT_DOWNLOAD_API_URL = "https://speed.cloudflare.com/__down"


@dataclass(frozen=True)
class CapacityProbeConfig:
    enabled: bool = True
    interval_s: float = 3600.0
    quiet_window_s: float = 30.0
    quiet_dl_mbps: float = 0.4
    quiet_ul_mbps: float = 0.2
    max_bytes: int = 250_000
    timeout_s: float = 2.5
    endpoint_url: str = DEFAULT_DOWNLOAD_API_URL


@dataclass(frozen=True)
class CapacityProbeRuntimeSnapshot:
    """Exact register values plus separately owned register metadata."""

    values: Mapping[str, Mapping[str, Any]]
    metadata: Mapping[str, Mapping[str, Any]]


def run_capacity_probe_clock_lane(engine: Any, *, now: float) -> ClockedJobOutcome:
    """Publish capacity evidence from passive traffic and gated active probes."""

    cfg = _capacity_probe_config(engine)
    runtime_snapshot = _runtime_snapshot(engine)
    values = runtime_snapshot.values
    previous_capacity = _mapping(values.get(REGISTER_CAPACITY))
    previous_active = _active_capacity_register_fact(previous_capacity)
    previous_passive = _passive_dl_peak_register_fact(previous_capacity)
    previous_evidence_present = _capacity_evidence_present(previous_capacity)
    passive_fact, passive_advanced = build_passive_dl_peak_fact(
        identity=_mapping(values.get(REGISTER_IDENTITY)),
        radio=_mapping(values.get(REGISTER_RADIO)),
        traffic=_mapping(values.get(REGISTER_TRAFFIC)),
        traffic_register_metadata=_mapping(
            runtime_snapshot.metadata.get(REGISTER_TRAFFIC)
        ),
        previous=previous_passive,
        now=now,
    )
    allowed, reason, context = capacity_probe_gate(engine, values=values, cfg=cfg, now=now)
    if not allowed:
        if passive_advanced or not previous_evidence_present:
            _publish_capacity_evidence(
                engine,
                _capacity_evidence_payload(
                    active_capacity_fact=previous_active,
                    passive_dl_peak_fact=passive_fact,
                    active_probe_status="skipped",
                    active_probe_reason=reason,
                    now=now,
                ),
                now=now,
                valid=True,
            )
        return ClockedJobOutcome(ran=bool(passive_advanced), backpressure_reason=reason)

    runner = getattr(engine, "_capacity_probe_runner", None)
    try:
        result = runner(cfg, context, now) if callable(runner) else _run_default_probe(cfg)
    except Exception as exc:
        _publish_capacity_evidence(
            engine,
            _capacity_evidence_payload(
                active_capacity_fact=previous_active,
                passive_dl_peak_fact=passive_fact,
                active_probe_status="failed",
                active_probe_reason=str(exc or "capacity_probe_failed"),
                now=now,
            ),
            now=now,
            valid=False,
            error=str(exc or ""),
        )
        return ClockedJobOutcome(ran=False, last_error=str(exc or ""), backpressure_reason="capacity_probe_failed")

    measured = _mapping(result)
    active_fact = _active_capacity_payload(
        status="measured",
        reason="quiet_window_probe_completed",
        context=context,
        cfg=cfg,
        now=now,
        measured=measured,
    )
    _record_probe_ts(engine, context_key=str(context.get("context_key") or ""), now=now)
    _publish_capacity_evidence(
        engine,
        _capacity_evidence_payload(
            active_capacity_fact=active_fact,
            passive_dl_peak_fact=passive_fact,
            active_probe_status="measured",
            active_probe_reason="quiet_window_probe_completed",
            now=now,
        ),
        now=now,
        valid=True,
    )
    return ClockedJobOutcome(ran=True)


def capacity_probe_gate(
    engine: Any,
    *,
    values: Mapping[str, Mapping[str, Any]],
    cfg: CapacityProbeConfig,
    now: float,
) -> tuple[bool, str, dict[str, Any]]:
    """Return whether a measured capacity probe may run right now."""

    radio = _mapping(values.get(REGISTER_RADIO))
    traffic = _mapping(values.get(REGISTER_TRAFFIC))
    mutation = _mapping(values.get(REGISTER_MUTATION))
    decision = _mapping(values.get(REGISTER_DECISION))
    learning = _mapping(values.get(REGISTER_LEARNING))
    context = _probe_context(radio=radio, now=now)

    if not bool(cfg.enabled):
        return False, "capacity_probe_disabled", context
    if not _bool(radio.get("radio_connected"), True):
        return False, "radio_not_connected", context
    mutation_remaining_s = max(
        _float(mutation.get("remaining_s"), 0.0),
        _float(mutation.get("cooldown_s"), 0.0),
    )
    if mutation_remaining_s > 0.0:
        return False, "mutation_cooldown_active", context
    if _bool(mutation.get("active"), False) or _bool(mutation.get("mutation_active"), False):
        return False, "mutation_active", context
    if decision and (
        str(decision.get("schema") or "") != DECISION_PROJECTION_SCHEMA
        or str(decision.get("owner") or "") != DECISION_PROJECTION_OWNER
    ):
        raise ValueError("runtime_decision_projection_required")
    decision_availability = _mapping(decision.get("action_availability"))
    if _bool(
        decision_availability.get("smart_loop_roi_observation_active"),
        False,
    ):
        return False, "roi_observation_active", context
    if _bool(learning.get("proof_pending"), False):
        return False, "proof_pending", context

    dl_mbps = _float(traffic.get("dl_mbps", traffic.get("throughput_dl_mbps")), 0.0)
    ul_mbps = _float(traffic.get("ul_mbps", traffic.get("throughput_ul_mbps")), 0.0)
    quiet_now = dl_mbps <= cfg.quiet_dl_mbps and ul_mbps <= cfg.quiet_ul_mbps
    if not quiet_now:
        setattr(engine, "_capacity_probe_quiet_since", 0.0)
        return False, "user_traffic_active", context

    quiet_since = float(getattr(engine, "_capacity_probe_quiet_since", 0.0) or 0.0)
    if quiet_since <= 0.0:
        quiet_since = float(now)
        setattr(engine, "_capacity_probe_quiet_since", quiet_since)
    quiet_for_s = max(0.0, float(now) - quiet_since)
    context["quiet_for_s"] = round(quiet_for_s, 3)
    if quiet_for_s < cfg.quiet_window_s:
        return False, "quiet_window_not_met", context

    last_by_context = getattr(engine, "_capacity_probe_last_by_context", None)
    if not isinstance(last_by_context, dict):
        last_by_context = {}
        setattr(engine, "_capacity_probe_last_by_context", last_by_context)
    last_ts = float(last_by_context.get(str(context.get("context_key") or ""), 0.0) or 0.0)
    if last_ts > 0.0 and (float(now) - last_ts) < cfg.interval_s:
        return False, "hourly_context_cadence_active", context
    return True, "allowed", context


def _run_default_probe(cfg: CapacityProbeConfig) -> dict[str, Any]:
    latency_ms = _measure_latency_ms(cfg)
    started = time.perf_counter()
    url = _with_bytes(cfg.endpoint_url, int(cfg.max_bytes))
    request = Request(url, headers={"User-Agent": "54GUC-capacity-probe/1.0"})
    byte_count = 0
    with urlopen(request, timeout=float(cfg.timeout_s)) as response:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            byte_count += len(chunk)
            if byte_count >= int(cfg.max_bytes):
                break
    duration_s = max(0.001, time.perf_counter() - started)
    dl_mbps = (float(byte_count) * 8.0) / duration_s / 1_000_000.0
    return {
        "dl_mbps": dl_mbps,
        "latency_ms": latency_ms,
        "bytes": int(byte_count),
        "duration_s": duration_s,
        "endpoint_url": cfg.endpoint_url,
    }


def _measure_latency_ms(cfg: CapacityProbeConfig) -> float:
    started = time.perf_counter()
    request = Request(_with_bytes(cfg.endpoint_url, 0), headers={"User-Agent": "54GUC-capacity-probe/1.0"})
    with urlopen(request, timeout=float(cfg.timeout_s)) as response:
        response.read(1)
    return max(0.0, (time.perf_counter() - started) * 1000.0)


def _with_bytes(url: str, byte_count: int) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode({'bytes': max(0, int(byte_count))})}"


def _capacity_probe_config(engine: Any) -> CapacityProbeConfig:
    cfg = getattr(engine, "cfg", None)
    return CapacityProbeConfig(
        enabled=_bool(getattr(cfg, "capacity_probe_enabled", True), True),
        interval_s=max(60.0, _float(getattr(cfg, "capacity_probe_interval_s", 3600.0), 3600.0)),
        quiet_window_s=max(1.0, _float(getattr(cfg, "capacity_probe_quiet_window_s", 30.0), 30.0)),
        quiet_dl_mbps=max(0.0, _float(getattr(cfg, "capacity_probe_quiet_dl_mbps", 0.4), 0.4)),
        quiet_ul_mbps=max(0.0, _float(getattr(cfg, "capacity_probe_quiet_ul_mbps", 0.2), 0.2)),
        max_bytes=max(50_000, _int(getattr(cfg, "capacity_probe_max_bytes", 250_000), 250_000)),
        timeout_s=max(0.5, _float(getattr(cfg, "capacity_probe_timeout_s", 2.5), 2.5)),
        endpoint_url=str(getattr(cfg, "capacity_probe_endpoint_url", DEFAULT_DOWNLOAD_API_URL) or DEFAULT_DOWNLOAD_API_URL),
    )


def _runtime_snapshot(engine: Any) -> CapacityProbeRuntimeSnapshot:
    bank = require_core_runtime_register_bank(engine)
    register_names = (
        REGISTER_IDENTITY,
        REGISTER_RADIO,
        REGISTER_TRAFFIC,
        REGISTER_MUTATION,
        REGISTER_DECISION,
        REGISTER_LEARNING,
        REGISTER_CAPACITY,
    )
    rows = bank.snapshot(register_names)
    values: dict[str, dict[str, Any]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for name in register_names:
        row = rows.get(name) if isinstance(rows, Mapping) else None
        values[name] = (
            dict(row.get("value") or {}) if isinstance(row, Mapping) else {}
        )
        metadata[name] = (
            {
                "updated_at": _float(row.get("updated_at"), 0.0),
                "valid": _bool(row.get("valid"), False),
                "stale": _bool(row.get("stale"), True),
                "source_kind": _text(row.get("source_kind")),
            }
            if isinstance(row, Mapping)
            else {
                "updated_at": 0.0,
                "valid": False,
                "stale": True,
                "source_kind": "",
            }
        )
    return CapacityProbeRuntimeSnapshot(values=values, metadata=metadata)


def _probe_context(*, radio: Mapping[str, Any], now: float) -> dict[str, Any]:
    provider = _text(radio.get("provider_plmn"))
    cell_id = _int(radio.get("cell_id"), 0)
    pci = _int(radio.get("pci"), 0)
    earfcn = _int(radio.get("earfcn_dl") if "earfcn_dl" in radio else radio.get("earfcn"), 0)
    serving_band = _int(radio.get("serving_band") if "serving_band" in radio else radio.get("primary_band"), 0)
    target_bands = _int_list(radio.get("configured_band_pool") or ())
    if serving_band > 0 and serving_band not in target_bands:
        target_bands = [serving_band, *target_bands]
    context_key = "|".join((provider, str(cell_id), str(pci), str(earfcn), str(serving_band)))
    return {
        "provider_plmn": provider,
        "cell_id": cell_id,
        "pci": pci,
        "earfcn": earfcn,
        "serving_band": serving_band,
        "target_bands": target_bands,
        "context_key": context_key,
        "sample_ts": float(now),
    }


def _active_capacity_payload(
    *,
    status: str,
    reason: str,
    context: Mapping[str, Any],
    cfg: CapacityProbeConfig,
    now: float,
    measured: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = _mapping(measured)
    bytes_read = _int(result.get("bytes"), 0)
    duration_s = _float(result.get("duration_s"), 0.0)
    dl_mbps = _float(result.get("dl_mbps"), 0.0)
    confidence = 0.0
    if status == "measured" and bytes_read > 0 and duration_s > 0.0 and dl_mbps > 0.0:
        confidence = max(0.1, min(1.0, bytes_read / max(1.0, float(cfg.max_bytes))))
    return {
        "schema": ACTIVE_SCHEMA,
        "owner": OWNER,
        "measured": status == "measured",
        "status": status,
        "reason": reason,
        "dl_mbps": round(dl_mbps, 3),
        "latency_ms": round(_float(result.get("latency_ms"), 0.0), 3),
        "bytes": bytes_read,
        "duration_s": round(duration_s, 3),
        "confidence": round(confidence, 3),
        "endpoint_url": str(result.get("endpoint_url") or cfg.endpoint_url),
        "quiet_window_s": float(cfg.quiet_window_s),
        "sample_ts": float(now),
        "context_key": str(context.get("context_key") or ""),
        "provider_plmn": str(context.get("provider_plmn") or ""),
        "cell_id": _int(context.get("cell_id"), 0),
        "pci": _int(context.get("pci"), 0),
        "earfcn": _int(context.get("earfcn"), 0),
        "serving_band": _int(context.get("serving_band"), 0),
        "target_bands": _int_list(context.get("target_bands")),
        "quiet_for_s": _float(context.get("quiet_for_s"), 0.0),
    }


def _capacity_evidence_payload(
    *,
    active_capacity_fact: Mapping[str, Any],
    passive_dl_peak_fact: Mapping[str, Any],
    active_probe_status: str,
    active_probe_reason: str,
    now: float,
) -> dict[str, Any]:
    active = _active_capacity_subfact(active_capacity_fact)
    passive = _passive_dl_peak_subfact(passive_dl_peak_fact)
    out = {
        "schema": SCHEMA,
        "owner": OWNER,
        "active_probe_status": str(active_probe_status or ""),
        "active_probe_reason": str(active_probe_reason or ""),
        "sample_ts": float(now),
        "active_capacity_fact": active,
        "passive_dl_peak_fact": passive,
    }
    if active:
        for key in (
            "measured",
            "status",
            "reason",
            "dl_mbps",
            "latency_ms",
            "bytes",
            "duration_s",
            "confidence",
            "endpoint_url",
            "quiet_window_s",
            "context_key",
            "provider_plmn",
            "cell_id",
            "pci",
            "earfcn",
            "serving_band",
            "target_bands",
            "quiet_for_s",
        ):
            if key in active:
                out[key] = active[key]
    return out


def _active_capacity_register_fact(value: Mapping[str, Any] | None) -> dict[str, Any]:
    row = _mapping(value)
    if row.get("schema") != SCHEMA or row.get("owner") != OWNER:
        return {}
    row = _mapping(row.get("active_capacity_fact"))
    return _active_capacity_subfact(row)


def _active_capacity_subfact(value: Mapping[str, Any] | None) -> dict[str, Any]:
    row = _mapping(value)
    if row.get("schema") != ACTIVE_SCHEMA or row.get("owner") != OWNER:
        return {}
    return dict(row)


def _capacity_evidence_present(value: Mapping[str, Any] | None) -> bool:
    row = _mapping(value)
    if row.get("schema") == SCHEMA and row.get("owner") == OWNER:
        return True
    return False


def _passive_dl_peak_register_fact(value: Mapping[str, Any] | None) -> dict[str, Any]:
    row = _mapping(value)
    if row.get("schema") != SCHEMA or row.get("owner") != OWNER:
        return {}
    row = _mapping(row.get("passive_dl_peak_fact"))
    return _passive_dl_peak_subfact(row)


def _passive_dl_peak_subfact(value: Mapping[str, Any] | None) -> dict[str, Any]:
    row = _mapping(value)
    if row.get("schema") != "passive_dl_peak_fact.v1" or row.get("owner") != OWNER:
        return {}
    return dict(row)


def _publish_capacity_evidence(
    engine: Any,
    payload: Mapping[str, Any],
    *,
    now: float,
    valid: bool,
    error: str = "",
) -> None:
    bank = require_core_runtime_register_bank(engine)
    bank.write_capacity(
        value=dict(payload),
        source="capacity_probe",
        updated_at=float(now),
        valid=bool(valid),
        stale=False,
        trust="derived",
        source_kind="derived",
        validity=("valid" if valid else "dirty"),
        error=str(error or ""),
    )


def _record_probe_ts(engine: Any, *, context_key: str, now: float) -> None:
    if not context_key:
        return
    last_by_context = getattr(engine, "_capacity_probe_last_by_context", None)
    if not isinstance(last_by_context, dict):
        last_by_context = {}
        setattr(engine, "_capacity_probe_last_by_context", last_by_context)
    last_by_context[str(context_key)] = float(now)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _int_list(value: Any) -> list[int]:
    if isinstance(value, str):
        value = value.replace("+", ",").split(",")
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[int] = []
    for item in value:
        parsed = _int(item, 0)
        if parsed > 0 and parsed not in out:
            out.append(parsed)
    return out


__all__ = [
    "ACTIVE_SCHEMA",
    "DEFAULT_DOWNLOAD_API_URL",
    "OWNER",
    "SCHEMA",
    "CapacityProbeConfig",
    "capacity_probe_gate",
    "run_capacity_probe_clock_lane",
]
