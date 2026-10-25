"""Run Site Report generation outside the GUI process. JSON and HTML generation can be
heavy, so a child process keeps the GUI responsive.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import multiprocessing
from pathlib import Path
from queue import Empty
import time
from typing import Any

from arch4.atomic_io import atomic_write_text
from arch4.privacy_scrub import scrub_text
from arch4.support_bundle_context import SupportBundleContext
from arch4.core_contracts.site_report_history import (
    SiteReportHistoryProjection,
)


@dataclass(frozen=True, slots=True)
class SiteReportContext:
    """Report boundary for the startup context."""

    launch: SupportBundleContext

    @classmethod
    def from_support_context(
        cls,
        context: SupportBundleContext,
    ) -> "SiteReportContext":
        if type(context) is not SupportBundleContext:
            raise TypeError("site_report_support_context_required")
        return cls(launch=context)

    @property
    def runtime_dir(self) -> Path:
        return self.launch.runtime_dir


def _site_report_process_main(
    queue: Any,
    *,
    output_dir: str,
    hours: float,
    history: SiteReportHistoryProjection,
    open_browser: bool,
) -> None:
    try:
        from arch4.foundation.site_report import generate_foundation_site_report

        path = generate_foundation_site_report(
            runtime_dir=output_dir,
            hours=hours,
            history=history,
            open_browser=bool(open_browser),
        )
        queue.put({"ok": True, "path": str(path) if path is not None else ""})
    except BaseException as exc:
        queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def run_site_report_job(
    *,
    context: SiteReportContext,
    hours: float,
    history: SiteReportHistoryProjection,
    open_browser: bool = True,
    timeout_s: float = 300.0,
) -> Path:
    """Generate a Site Report in a child process and return the output path."""

    if type(context) is not SiteReportContext:
        raise TypeError("site_report_context_required")
    if type(history) is not SiteReportHistoryProjection:
        raise TypeError("site_report_history_projection_required")

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=_site_report_process_main,
        kwargs={
            "queue": queue,
            "output_dir": str(context.runtime_dir),
            "hours": float(hours),
            "history": history,
            "open_browser": bool(open_browser),
        },
        daemon=True,
        name="arch4-site-report",
    )
    try:
        process.start()
        process.join(max(1.0, float(timeout_s or 300.0)))
        if process.is_alive():
            process.terminate()
            process.join(5.0)
            raise TimeoutError(
                f"Site report generation exceeded {float(timeout_s or 300.0):.0f}s"
            )
        try:
            # Queue delivery can finish just after the child exits. Allow a
            # short window for that final receipt before reporting a crash.
            result = queue.get(timeout=1.0)
        except Empty as exc:
            raise RuntimeError(
                "Site report process exited without result:"
                f" exitcode={process.exitcode}"
            ) from exc
        if not isinstance(result, dict) or not bool(result.get("ok")):
            raise RuntimeError(
                str(
                    (result or {}).get("error")
                    if isinstance(result, dict)
                    else result
                )
            )
        path = str(result.get("path") or "").strip()
        if not path:
            raise RuntimeError("site_report_process_returned_no_artifact")
        return Path(path)
    except Exception as exc:
        receipt = {
            "schema": "SiteReportFailureReceipt.v1",
            "owner": "arch4.foundation.site_report_job",
            "failed_at": float(time.time()),
            "hours": float(hours),
            "history_status": history.status,
            "error": scrub_text(f"{type(exc).__name__}: {exc}"),
            "launch_profile": str(context.launch.profile or ""),
            "launch_source": str(context.launch.source or ""),
        }
        atomic_write_text(
            context.runtime_dir / "site_report_failure_receipt.json",
            json.dumps(receipt, indent=2, sort_keys=True),
        )
        raise


__all__ = ["SiteReportContext", "run_site_report_job"]
