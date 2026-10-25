"""Context-owned support bundle job with a durable failure receipt."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import hashlib
import json
import time
from typing import Any, Callable, Mapping

from arch4.atomic_io import atomic_write_text
from arch4.privacy_scrub import scrub_text
from arch4.support_bundle_context import SupportBundleContext


def run_support_bundle_job(
    context: SupportBundleContext,
    *,
    exporter: Callable[..., Any],
    probe_mode: bool = False,
    runtime_mode: str = "",
    modem_identity: Mapping[str, Any] | None = None,
    progress: Callable[[float, str], None] | None = None,
    prepare_history: Callable[[float, Path], None] | None = None,
):
    """Run the sole support exporter and report failures without crashing Engine."""

    if type(context) is not SupportBundleContext:
        raise TypeError("support_bundle_context_required")
    request_id = str(context.request_id or "").strip()
    if not request_id:
        request_id = "support-request:" + hashlib.sha256(
            (
                f"{context.profile}|{context.source}|{time.time_ns()}"
            ).encode("utf-8")
        ).hexdigest()[:24]
    effective_context = replace(context, request_id=request_id)
    try:
        if not callable(exporter):
            raise TypeError("support_bundle_exporter_required")

        kwargs = {
            "probe_mode": bool(probe_mode),
            "runtime_mode": str(runtime_mode or ""),
            "modem_identity": dict(modem_identity or {}),
        }
        if progress is not None:
            kwargs["progress"] = progress
        if prepare_history is not None:
            kwargs["prepare_history"] = prepare_history
        return exporter(effective_context, **kwargs)
    except Exception as exc:
        receipt = {
            "schema": "SupportBundleFailureReceipt.v1",
            "owner": "arch4.support_bundle_job",
            "failed_at": float(time.time()),
            "request_id": request_id,
            "error": scrub_text(f"{type(exc).__name__}: {exc}"),
            "launch_profile": str(effective_context.profile or ""),
            "launch_source": str(effective_context.source or ""),
        }
        atomic_write_text(
            context.runtime_dir / "support_bundle_failure_receipt.json",
            json.dumps(receipt, indent=2, sort_keys=True),
        )
        raise


__all__ = ["run_support_bundle_job"]
