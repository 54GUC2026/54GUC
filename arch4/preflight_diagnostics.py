"""arch4/preflight_diagnostics.py

Windows preflight tooling for portable validation sessions.

This module provides code-only checks that run WITHOUT live hardware.
It verifies bundle structure, portable write permissions, launcher
config validity, and captures system info for clean log handoff.

Usage from repo root or portable bundle:
    python -m arch4.preflight_diagnostics [--app-root DIR] [--profile PROFILE]

Exit codes:
    0  All checks passed
    1  One or more checks failed (details in output)
    2  Usage error
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)


# ── Result types ────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    warn: str = ""


@dataclass
class PreflightReport:
    timestamp: str = ""
    hostname: str = ""
    platform: str = ""
    python_version: str = ""
    python_bits: int = 0
    app_root: str = ""
    profile: str = ""
    checks: List[Dict[str, Any]] = field(default_factory=list)
    passed: int = 0
    failed: int = 0
    warnings: int = 0

    def add(self, result: CheckResult) -> None:
        self.checks.append(asdict(result))
        if result.passed:
            self.passed += 1
        else:
            self.failed += 1
        if result.warn:
            self.warnings += 1


# ── Individual checks ───────────────────────────────────────────

def check_python_environment() -> CheckResult:
    """Verify Python version and bitness."""
    ver = sys.version_info
    bits = struct.calcsize("P") * 8
    if ver < (3, 10):
        return CheckResult(
            name="python_version",
            passed=False,
            detail=f"Python {ver.major}.{ver.minor}.{ver.micro} ({bits}-bit) — requires 3.10+",
        )
    warn = ""
    if bits != 64:
        warn = f"32-bit Python detected — 64-bit recommended for production"
    return CheckResult(
        name="python_version",
        passed=True,
        detail=f"Python {ver.major}.{ver.minor}.{ver.micro} ({bits}-bit)",
        warn=warn,
    )


def check_required_packages() -> CheckResult:
    """Check that core runtime packages are importable."""
    missing = []
    for pkg in ["requests", "routeros_api"]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    # Check for importable GUI bindings without creating a window.
    gui_ok = False
    for gui_pkg in ["PyQt6", "PySide6"]:
        try:
            __import__(gui_pkg)
            gui_ok = True
            break
        except ImportError:
            pass
    if not gui_ok:
        missing.append("PyQt6/PySide6")
    if missing:
        return CheckResult(
            name="required_packages",
            passed=False,
            detail=f"Missing: {', '.join(missing)}",
        )
    return CheckResult(
        name="required_packages",
        passed=True,
        detail="All core packages importable",
    )


def check_bundle_structure(app_root: Path) -> CheckResult:
    """Verify either source-tree or frozen portable-bundle structure."""
    source_expected_files = ["arch4/__init__.py"]
    source_expected_dirs = ["arch4", "arch4/modem", "arch4/ngui"]
    source_missing = []
    for d in source_expected_dirs:
        if not (app_root / d).is_dir():
            source_missing.append(f"dir:{d}")
    for f in source_expected_files:
        if not (app_root / f).is_file():
            source_missing.append(f"file:{f}")
    if not source_missing:
        return CheckResult(
            name="bundle_structure",
            passed=True,
            detail=f"Source-tree structure OK at {app_root}",
        )

    frozen_expected = ["54GUC.exe", "_internal"]
    frozen_missing = []
    for item in frozen_expected:
        path = app_root / item
        if item.startswith("_internal"):
            if not path.is_dir():
                frozen_missing.append(f"dir:{item}")
        elif not path.exists():
            frozen_missing.append(f"file:{item}")
    manifest_or_version = (app_root / "version_manifest.json").exists() or (app_root / "VERSION").exists()
    if not manifest_or_version:
        frozen_missing.append("file:version_manifest.json|VERSION")
    if not frozen_missing:
        return CheckResult(
            name="bundle_structure",
            passed=True,
            detail=f"Frozen bundle structure OK at {app_root}",
        )

    return CheckResult(
        name="bundle_structure",
        passed=False,
        detail=(
            "Missing source layout: "
            + ", ".join(source_missing)
            + " | Missing frozen layout: "
            + ", ".join(frozen_missing)
        ),
    )


def check_write_permissions(app_root: Path) -> CheckResult:
    """Verify that runtime directories are writable."""
    dirs_to_check = [
        ("portable_runtime", app_root / "portable_runtime"),
        ("guc_output", app_root / "portable_runtime" / "guc_output"),
        ("guc_memory", app_root / "portable_runtime" / "guc_memory"),
    ]
    failures = []
    for label, dir_path in dirs_to_check:
        dir_path.mkdir(parents=True, exist_ok=True)
        probe = dir_path / ".write_probe"
        try:
            probe.write_text("probe", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            failures.append(f"{label}: {exc}")
    if failures:
        return CheckResult(
            name="write_permissions",
            passed=False,
            detail="; ".join(failures),
        )
    return CheckResult(
        name="write_permissions",
        passed=True,
        detail="portable_runtime tree writable (guc_output, guc_memory)",
    )


def check_config_file(app_root: Path) -> CheckResult:
    """Validate optional external settings through the canonical decoder."""

    try:
        from arch4.external_settings_decoder import validate_external_settings_source

        status = validate_external_settings_source(app_root)
    except Exception as exc:
        return CheckResult(
            name="config_file",
            passed=False,
            detail=f"External settings validation error: {exc}",
        )
    if not status["present"]:
        return CheckResult(
            name="config_file",
            passed=True,
            detail="No guc_config.xml or luc_config.xml — owner defaults apply",
            warn="No external settings input found",
        )
    return CheckResult(
        name="config_file",
        passed=True,
        detail=f"External settings input valid: {status['source_name']}",
    )


def check_launcher_import() -> CheckResult:
    """Verify that the launcher module imports cleanly."""
    try:
        from arch4 import windows_native_launcher  # noqa: F401
        return CheckResult(
            name="launcher_import",
            passed=True,
            detail="arch4.windows_native_launcher imports OK",
        )
    except Exception as exc:
        return CheckResult(
            name="launcher_import",
            passed=False,
            detail=f"Import failed: {exc}",
        )


def check_supervisor_script(app_root: Path) -> CheckResult:
    """Verify supervisor script exists and is readable."""
    script = app_root / "scripts" / "arch5_supervisor.sh"
    if not script.exists():
        return CheckResult(
            name="supervisor_script",
            passed=True,
            detail="No supervisor script (Windows native mode)",
            warn="Supervisor script absent — expected for portable Windows bundles",
        )
    if not os.access(script, os.R_OK):
        return CheckResult(
            name="supervisor_script",
            passed=False,
            detail=f"Cannot read {script}",
        )
    return CheckResult(
        name="supervisor_script",
        passed=True,
        detail=f"Supervisor script readable at {script}",
    )


def check_writable_root_fallback(app_root: Path) -> CheckResult:
    """Check whether writable-root fallback would activate.

    If the app root is not writable, the runtime will redirect output
    to a fallback location. This check probes writability WITHOUT
    activating the fallback — it only reports what would happen.
    """
    probe = app_root / ".guc_preflight_probe"
    try:
        app_root.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return CheckResult(
            name="writable_root",
            passed=True,
            detail=f"App root writable: {app_root}",
        )
    except Exception as exc:
        return CheckResult(
            name="writable_root",
            passed=True,  # Not a failure — fallback will handle it
            detail=f"App root NOT writable: {app_root}",
            warn=(
                f"Runtime will use fallback writable root. "
                f"Config persistence may be degraded. Reason: {exc}"
            ),
        )


# ── Runner ──────────────────────────────────────────────────────

def run_preflight(app_root: Path, profile: str = "") -> PreflightReport:
    """Run all preflight checks and return a structured report."""
    report = PreflightReport(
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        hostname=platform.node(),
        platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
        python_version=sys.version,
        python_bits=struct.calcsize("P") * 8,
        app_root=str(app_root),
        profile=profile,
    )

    report.add(check_python_environment())
    report.add(check_required_packages())
    report.add(check_bundle_structure(app_root))
    report.add(check_write_permissions(app_root))
    report.add(check_config_file(app_root))
    report.add(check_launcher_import())
    report.add(check_supervisor_script(app_root))
    report.add(check_writable_root_fallback(app_root))

    return report


def format_report(report: PreflightReport) -> str:
    """Format report as human-readable text."""
    lines = [
        f"54GUC Preflight Diagnostics",
        f"{'=' * 40}",
        f"Time:     {report.timestamp}",
        f"Host:     {report.hostname}",
        f"Platform: {report.platform}",
        f"Python:   {report.python_version}",
        f"App root: {report.app_root}",
        f"Profile:  {report.profile or '(default)'}",
        "",
    ]
    for check in report.checks:
        status = "PASS" if check["passed"] else "FAIL"
        lines.append(f"  [{status}] {check['name']}: {check['detail']}")
        if check.get("warn"):
            lines.append(f"         WARN: {check['warn']}")
    lines.append("")
    lines.append(f"Result: {report.passed} passed, {report.failed} failed, {report.warnings} warnings")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="54GUC preflight diagnostics — verify bundle before live run",
    )
    parser.add_argument(
        "--app-root",
        default=".",
        help="Root directory of the app bundle (default: current directory)",
    )
    parser.add_argument(
        "--profile",
        default="",
        help="Bundle profile name (e.g., b818, mc888)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of human-readable text",
    )
    parser.add_argument(
        "--save",
        metavar="PATH",
        help="Save report to file",
    )
    args = parser.parse_args()

    app_root = Path(args.app_root).resolve()
    report = run_preflight(app_root, profile=args.profile)

    if args.json:
        output = json.dumps(asdict(report), indent=2)
    else:
        output = format_report(report)

    print(output)

    if args.save:
        save_path = Path(args.save)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(output, encoding="utf-8")
        print(f"\nReport saved to: {save_path}")

    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
