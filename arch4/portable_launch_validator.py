"""arch4/portable_launch_validator.py

Scriptable post-launch validation checklist for frozen Windows bundles.

Runs immediately after a frozen LUC bundle launches. Checks everything that
can be validated without live hardware:
  - runtime directories created and writable
  - writable-root and fallback state correct
  - config persistence state (config file present + writable)
  - bootstrap mode vs live mode consistent with config
  - launcher mode matches expectations (frozen / non-frozen)
  - bundle resources present (version_manifest.json, config)
  - lock file state (no stale lock from prior crash)
  - diagnostic report shape (portable_diagnostic.json readable if present)

Documents which checks genuinely need hardware and cannot run here.

Usage:
    python -m arch4.portable_launch_validator [options]

    --app-root DIR    Portable bundle root
    --out-dir  DIR    Runtime output dir (default: <app-root>/portable_runtime/guc_output)
    --expect-fallback  Assert fallback-root is active (for non-writable-root tests)
    --expect-frozen    Assert sys.frozen == True (in frozen bundle runs)

Exit codes:
    0  All automated checks passed
    1  One or more automated checks failed
    2  Usage error
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import struct
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ── Result types ────────────────────────────────────────────────────────────

@dataclass
class ValidationCheck:
    name: str
    passed: bool
    detail: str = ""
    manual: bool = False      # True = cannot be automated; documents local checks by hand


@dataclass
class ValidationReport:
    timestamp_utc: str = ""
    app_root: str = ""
    automated_passed: int = 0
    automated_failed: int = 0
    manual_required: int = 0
    checks: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, check: ValidationCheck) -> None:
        self.checks.append(asdict(check))
        if check.manual:
            self.manual_required += 1
        elif check.passed:
            self.automated_passed += 1
        else:
            self.automated_failed += 1

    @property
    def ok(self) -> bool:
        return self.automated_failed == 0


# ── Individual checks ────────────────────────────────────────────────────────

def check_runtime_dirs(out_dir: Path, memory_dir: Optional[Path] = None) -> List[ValidationCheck]:
    """Runtime and memory dirs must exist and be writable."""
    checks = []
    for label, path in [("runtime_dir", out_dir), ("memory_dir", memory_dir)]:
        if path is None:
            continue
        exists = path.is_dir()
        writable = os.access(path, os.W_OK) if exists else False
        checks.append(ValidationCheck(
            name=f"{label}_exists_and_writable",
            passed=exists and writable,
            detail=f"{path}: exists={exists}, writable={writable}",
        ))
    return checks


def check_writable_root(app_root: Path, expect_fallback: bool = False) -> ValidationCheck:
    """Writable-root resolves correctly; fallback state matches expectation."""
    try:
        from arch4.runtime_paths import resolve_writable_root, is_fallback_root_active
        writable_root = resolve_writable_root()
        fallback_active = is_fallback_root_active()
        if expect_fallback:
            passed = fallback_active
            detail = f"fallback_active={fallback_active} (expected True), writable_root={writable_root}"
        else:
            passed = not fallback_active
            detail = f"fallback_active={fallback_active} (expected False), writable_root={writable_root}"
        return ValidationCheck(name="writable_root_state", passed=passed, detail=detail)
    except Exception as exc:
        return ValidationCheck(name="writable_root_state", passed=False,
                               detail=f"Could not resolve: {exc}")


def check_config_persistence(app_root: Path) -> ValidationCheck:
    """Independent owner settings stores must have writable storage."""
    try:
        from arch4.runtime_paths import resolve_config_dir
        from arch4.foundation.settings_health import (
            foundation_settings_persistence_status,
        )

        status = foundation_settings_persistence_status(resolve_config_dir())
    except Exception as exc:
        return ValidationCheck(
            name="config_persistence",
            passed=False,
            detail=f"Cannot assess independent settings stores: {exc}",
        )
    passed = not status.get("degraded", True)
    reason = status.get("reason", "")
    return ValidationCheck(
        name="config_persistence",
        passed=passed,
        detail=f"degraded={status.get('degraded')}, "
               f"writable={status.get('settings_writable')}"
               + (f", reason={reason}" if reason else ""),
    )


def check_bootstrap_mode(app_root: Path) -> ValidationCheck:
    """Bootstrap remains neutral while the sole XML decoder validates input."""

    try:
        from arch4.external_settings_decoder import validate_external_settings_source

        status = validate_external_settings_source(app_root)
    except Exception as exc:
        return ValidationCheck(
            name="bootstrap_mode_check",
            passed=False,
            detail=f"External settings validation failed: {exc}",
        )
    if not status["present"]:
        return ValidationCheck(
            name="bootstrap_mode_check",
            passed=True,
            detail=(
                "External settings absent — neutral first-run bootstrap is valid"
            ),
        )
    return ValidationCheck(
        name="bootstrap_mode_check",
        passed=True,
        detail=(
            f"Validated external settings input {status['source_name']}; "
            "no startup authority granted"
        ),
    )


def check_launcher_mode(expect_frozen: bool) -> ValidationCheck:
    """frozen flag must match expectation (frozen bundle vs repo run)."""
    is_frozen = bool(getattr(sys, "frozen", False))
    passed = is_frozen == expect_frozen
    return ValidationCheck(
        name="launcher_mode",
        passed=passed,
        detail=f"sys.frozen={is_frozen}, expected={expect_frozen}",
    )


def check_bundle_resources(app_root: Path) -> List[ValidationCheck]:
    """Critical bundle resources must be present."""
    checks = []
    required = {
        "version_manifest.json": app_root / "version_manifest.json",
    }
    advisory = {
        "guc_config.xml": app_root / "guc_config.xml",
    }
    for name, path in required.items():
        passed = path.is_file()
        detail = f"{path}: {'present' if path.is_file() else 'MISSING'}"
        if passed and name == "version_manifest.json":
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                version = str(manifest.get("version", "") or "").strip()
                build_date = str(manifest.get("build_date", "") or "").strip()
                if not version or not build_date:
                    passed = False
                    detail = f"{path}: missing required keys version/build_date"
                else:
                    detail = f"{path}: present, version={version}, build_date={build_date}"
            except Exception as exc:
                passed = False
                detail = f"{path}: invalid JSON ({exc})"
        checks.append(ValidationCheck(
            name=f"resource_{name.replace('.', '_').replace('-', '_')}",
            passed=passed,
            detail=detail,
        ))
    for name, path in advisory.items():
        # External settings input is optional.  Missing means typed owner
        # defaults remain active; no runtime component creates this file.
        detail = f"{path}: {'present' if path.is_file() else 'absent (advisory)'}"
        if name == "guc_config.xml" and not path.is_file():
            detail = f"{path}: absent (advisory — typed owner defaults active)"
        checks.append(ValidationCheck(
            name=f"resource_{name.replace('.', '_').replace('-', '_')}",
            passed=True,  # advisory — always passes; detail conveys presence
            detail=detail,
        ))
    return checks


def check_gui_import_available() -> ValidationCheck:
    """The frozen bundle must contain the dynamically imported GUI module."""
    try:
        module = importlib.import_module("arch4.ngui.application")
        launch = getattr(module, "launch", None)
        passed = callable(launch)
        detail = "arch4.ngui.application import OK, launch callable present" if passed else "arch4.ngui.application import OK, launch missing"
        return ValidationCheck(name="gui_import_available", passed=passed, detail=detail)
    except Exception as exc:
        return ValidationCheck(
            name="gui_import_available",
            passed=False,
            detail=f"arch4.ngui.application import failed: {type(exc).__name__}: {exc}",
        )


def check_lock_state(out_dir: Path) -> ValidationCheck:
    """No stale lock file should exist at launch (crash recovery should have cleared it)."""
    lock_path = out_dir / "arch4.lock"
    if not lock_path.is_file():
        return ValidationCheck(name="lock_state", passed=True,
                               detail="No lock file present — clean startup")
    try:
        from arch4.portable_launcher_helpers import get_lock_file_pid, is_process_running
        pid = get_lock_file_pid(lock_path)
        running = is_process_running(pid) if pid else False
        if running:
            return ValidationCheck(name="lock_state", passed=True,
                                   detail=f"Lock present, process {pid} running — OK (another instance)")
        return ValidationCheck(
            name="lock_state",
            passed=False,
            detail=f"Stale lock: {lock_path}, pid={pid} not running — crash recovery may have missed it",
        )
    except Exception as exc:
        return ValidationCheck(name="lock_state", passed=False,
                               detail=f"Could not read lock: {exc}")


def check_diagnostic_report_shape(out_dir: Path) -> ValidationCheck:
    """If portable_diagnostic.json exists, it must parse and have all required sections."""
    diag_path = out_dir / "portable_diagnostic.json"
    if not diag_path.is_file():
        return ValidationCheck(
            name="diagnostic_report_shape",
            passed=True,
            detail="portable_diagnostic.json absent — run portable_diagnostic_dump first",
        )
    try:
        data = json.loads(diag_path.read_text(encoding="utf-8"))
        required = {"meta", "writable_root", "runtime_dirs", "config_persistence", "bundle", "preflight"}
        missing = required - set(data.keys())
        passed = not missing
        detail = f"all sections present" if passed else f"missing sections: {missing}"
        return ValidationCheck(name="diagnostic_report_shape", passed=passed, detail=detail)
    except Exception as exc:
        return ValidationCheck(name="diagnostic_report_shape", passed=False,
                               detail=f"Could not parse: {exc}")


def check_portable_state_import_contract(app_root: Path) -> ValidationCheck:
    """Imported Beta state must preserve existing operator config facts."""
    try:
        from arch4.foundation.settings_handoff import (
            foundation_settings_handoff_contract_errors,
        )
        errors = foundation_settings_handoff_contract_errors(app_root)
    except Exception as exc:
        return ValidationCheck(
            name="portable_state_import_contract",
            passed=False,
            detail=f"Cannot assess import contract: {exc}",
        )
    if errors:
        return ValidationCheck(
            name="portable_state_import_contract",
            passed=False,
            detail="; ".join(errors),
        )
    return ValidationCheck(
        name="portable_state_import_contract",
        passed=True,
        detail="completed import did not drop operator license/setup facts",
    )


# ── Manual-only checks (documented, not automated) ──────────────────────────

def manual_checks() -> List[ValidationCheck]:
    """Checks that require live hardware or Windows-specific environment.

    These are always marked manual=True and always 'pass' in automated runs
    — their purpose is to document what the operator must verify by hand.
    """
    return [
        ValidationCheck(
            name="modem_connection_live",
            passed=True,
            manual=True,
            detail="Manual: verify modem connects and DEVICE page shows signal (requires local hardware)",
        ),
        ValidationCheck(
            name="band_selection_executes",
            passed=True,
            manual=True,
            detail="Manual: verify a manual band change executes and modem responds (requires local hardware)",
        ),
        ValidationCheck(
            name="gui_renders_correctly",
            passed=True,
            manual=True,
            detail="Manual: verify DEVICE/GRAPHS/ADVANCED pages render without blank panels (visual check)",
        ),
        ValidationCheck(
            name="settings_persist_across_restart",
            passed=True,
            manual=True,
            detail="Manual: change a setting, close, reopen, and verify setting retention (config persistence live check)",
        ),
    ]


# ── Assembly ─────────────────────────────────────────────────────────────────

def run_validation(
    app_root: Path,
    out_dir: Optional[Path] = None,
    memory_dir: Optional[Path] = None,
    *,
    expect_fallback: bool = False,
    expect_frozen: Optional[bool] = None,
) -> ValidationReport:
    """Run all automated checks and document manual ones.

    Returns a ValidationReport. Never raises — individual check failures are
    captured as failed ValidationCheck entries.
    """
    if out_dir is None:
        out_dir = app_root / "portable_runtime" / "guc_output"
    if memory_dir is None:
        memory_dir = app_root / "portable_runtime" / "guc_memory"
    if expect_frozen is None:
        expect_frozen = bool(getattr(sys, "frozen", False))

    report = ValidationReport(
        timestamp_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        app_root=str(app_root),
    )

    # Automated checks
    for chk in check_runtime_dirs(out_dir, memory_dir):
        report.add(chk)
    try:
        report.add(check_writable_root(app_root, expect_fallback=expect_fallback))
    except Exception as exc:
        report.add(ValidationCheck(name="writable_root_state", passed=False,
                                   detail=f"Unexpected error: {exc}"))
    try:
        report.add(check_config_persistence(app_root))
    except Exception as exc:
        report.add(ValidationCheck(name="config_persistence", passed=False,
                                   detail=f"Unexpected error: {exc}"))
    try:
        report.add(check_bootstrap_mode(app_root))
    except Exception as exc:
        report.add(ValidationCheck(name="bootstrap_mode_check", passed=False,
                                   detail=f"Unexpected error: {exc}"))
    report.add(check_launcher_mode(expect_frozen=bool(expect_frozen)))
    for chk in check_bundle_resources(app_root):
        report.add(chk)
    try:
        report.add(check_gui_import_available())
    except Exception as exc:
        report.add(ValidationCheck(name="gui_import_available", passed=False,
                                   detail=f"Unexpected error: {exc}"))
    try:
        report.add(check_lock_state(out_dir))
    except Exception as exc:
        report.add(ValidationCheck(name="lock_state", passed=False,
                                   detail=f"Unexpected error: {exc}"))
    try:
        report.add(check_diagnostic_report_shape(out_dir))
    except Exception as exc:
        report.add(ValidationCheck(name="diagnostic_report_shape", passed=False,
                                   detail=f"Unexpected error: {exc}"))
    try:
        report.add(check_portable_state_import_contract(app_root))
    except Exception as exc:
        report.add(ValidationCheck(name="portable_state_import_contract", passed=False,
                                   detail=f"Unexpected error: {exc}"))

    # Manual-only checks (documented, not automated)
    for chk in manual_checks():
        report.add(chk)

    return report


def format_report(report: ValidationReport) -> str:
    """Render validation report as human-readable text."""
    lines = [
        "══════════════════════════════════════",
        "  54GUC POST-LAUNCH VALIDATION",
        f"  {report.timestamp_utc}",
        f"  app_root: {report.app_root}",
        "══════════════════════════════════════",
        f"\nAUTOMATED: {report.automated_passed} passed, {report.automated_failed} failed",
        f"MANUAL:    {report.manual_required} checks require local hardware/manual verification",
        "",
    ]
    for chk in report.checks:
        if chk["manual"]:
            lines.append(f"  [MANUAL] {chk['name']}: {chk['detail']}")
        elif chk["passed"]:
            lines.append(f"  [PASS]   {chk['name']}: {chk['detail']}")
        else:
            lines.append(f"  [FAIL]   {chk['name']}: {chk['detail']}")
    lines.append("")
    lines.append(f"RESULT: {'PASS' if report.ok else 'FAIL — see FAIL entries above'}")
    lines.append("══════════════════════════════════════")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="54GUC portable post-launch validation checklist",
    )
    parser.add_argument("--app-root", default=None)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--expect-fallback", action="store_true")
    parser.add_argument("--expect-frozen", action="store_true")
    args = parser.parse_args()

    app_root = Path(args.app_root).resolve() if args.app_root else Path(__file__).resolve().parent.parent
    out_dir = Path(args.out_dir).resolve() if args.out_dir else app_root / "portable_runtime" / "guc_output"

    report = run_validation(app_root, out_dir, expect_fallback=args.expect_fallback,
                            expect_frozen=args.expect_frozen)
    print(format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
