"""arch4/portable_diagnostic_dump.py

Single-command portable-state snapshot for Windows portable validation runs.

Collects every piece of portable state that support review needs to diagnose startup
failures without additional back-and-forth:
  - system / Python environment
  - writable-root choice and fallback status
  - config persistence health
  - bundle / preflight check results
  - runtime directory and lock state

Produces two artefacts:
  <out_dir>/portable_diagnostic.json  — machine-readable, full detail
  <out_dir>/portable_diagnostic.txt   — human-readable summary

Usage:
    python -m arch4.portable_diagnostic_dump [options]

    --app-root DIR    Portable bundle root (default: directory containing this file)
    --out-dir  DIR    Where to write artefacts (default: <app-root>/guc_output)
    --stdout          Also print text summary to stdout
    --json-only       Write only the JSON artefact (skip .txt)

Exit codes:
    0  Dump written successfully
    1  Fatal error — dump could not be written
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import struct
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

_DUMP_VERSION = "1"


# ── Section collectors ──────────────────────────────────────────────────────

def _collect_meta() -> Dict[str, Any]:
    """System / Python identity — no filesystem dependency."""
    bits = struct.calcsize("P") * 8
    vi = sys.version_info
    return {
        "dump_version": _DUMP_VERSION,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hostname": platform.node() or "(unknown)",
        "platform": platform.platform(),
        "python": f"{vi.major}.{vi.minor}.{vi.micro} ({bits}-bit)",
        "frozen": getattr(sys, "frozen", False),
        "pid": os.getpid(),
        "cwd": os.getcwd(),
    }


def _collect_writable_root(app_root: Path) -> Dict[str, Any]:
    """Writable-root resolution and fallback status."""
    result: Dict[str, Any] = {"app_root": str(app_root)}
    try:
        from arch4.runtime_paths import resolve_writable_root, is_fallback_root_active
        writable_root = resolve_writable_root()
        result["writable_root"] = str(writable_root)
        result["fallback_active"] = is_fallback_root_active()
        result["fallback"] = str(writable_root) != str(app_root.resolve())
        result["error"] = None
    except Exception as exc:
        result["writable_root"] = str(app_root)
        result["fallback_active"] = False
        result["fallback"] = False
        result["error"] = str(exc)
    return result


def _collect_runtime_dirs(app_root: Path) -> Dict[str, Any]:
    """Runtime and memory directory state."""
    try:
        from arch4.interfaces import ARCH_RUNTIME_OUTPUT_DIR, ARCH_RUNTIME_MEMORY_DIR
        runtime_dir = Path(
            os.environ.get("ARCH_RUNTIME_OUTPUT_DIR", str(app_root / ARCH_RUNTIME_OUTPUT_DIR))
        )
        memory_dir = Path(
            os.environ.get("ARCH_RUNTIME_MEMORY_DIR", str(app_root / ARCH_RUNTIME_MEMORY_DIR))
        )
    except Exception:
        runtime_dir = app_root / "guc_output"
        memory_dir = app_root / "guc_memory"

    def _dir_info(p: Path) -> Dict[str, Any]:
        try:
            exists = p.is_dir()
            writable = os.access(p, os.W_OK) if exists else False
            try:
                file_count = sum(1 for _ in p.iterdir()) if exists else 0
            except Exception:
                file_count = -1
            return {"path": str(p), "exists": exists, "writable": writable, "file_count": file_count}
        except Exception as exc:
            return {"path": str(p), "exists": False, "writable": False, "file_count": -1, "error": str(exc)}

    lock_path = runtime_dir / "arch4.lock"
    lock_info: Dict[str, Any] = {"path": str(lock_path), "exists": lock_path.is_file()}
    if lock_path.is_file():
        try:
            from arch4.portable_launcher_helpers import get_lock_file_pid, is_process_running
            pid = get_lock_file_pid(lock_path)
            lock_info["pid"] = pid
            lock_info["process_running"] = is_process_running(pid) if pid else False
        except Exception as exc:
            lock_info["error"] = str(exc)

    return {
        "runtime_dir": _dir_info(runtime_dir),
        "memory_dir": _dir_info(memory_dir),
        "lock": lock_info,
    }


def _collect_config_persistence(app_root: Path) -> Dict[str, Any]:
    """Persistence health for the independent owner settings stores."""
    try:
        from arch4.runtime_paths import resolve_config_dir
        from arch4.foundation.settings_health import (
            foundation_settings_persistence_status,
        )

        status = foundation_settings_persistence_status(resolve_config_dir())
        status["source"] = "runtime_paths"
        return status
    except Exception as exc:
        return {
            "settings_dir": str(app_root),
            "settings_files": {},
            "settings_writable": False,
            "degraded": True,
            "reason": f"Could not assess: {exc}",
            "source": "error",
        }


def _collect_bundle(app_root: Path) -> Dict[str, Any]:
    """Bundle resource presence check — no preflight subprocess needed."""
    files = {
        "config": app_root / "guc_config.xml",
        "start_here": app_root / "BETA_PACKAGE" / "START_HERE.txt",
        "manifest": app_root / "version_manifest.json",
    }
    result: Dict[str, Any] = {}
    for name, path in files.items():
        result[name] = {
            "path": str(path),
            "exists": path.is_file(),
            "size_bytes": path.stat().st_size if path.is_file() else None,
        }
    manifest_path = app_root / "version_manifest.json"
    version_string = "(missing)"
    build_date_string = "(missing)"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
        if isinstance(manifest, dict):
            version_string = str(manifest.get("version", "") or "").strip() or "(missing)"
            build_date_string = str(manifest.get("build_date", "") or "").strip() or "(missing)"
    result["version_string"] = version_string
    result["build_date_string"] = build_date_string
    return result


def _collect_preflight(app_root: Path) -> Dict[str, Any]:
    """Run all preflight checks and capture results."""
    try:
        from arch4.preflight_diagnostics import run_preflight
        report = run_preflight(app_root)
        return {
            "passed": report.passed,
            "failed": report.failed,
            "warnings": report.warnings,
            "checks": report.checks,
            "ok": report.failed == 0,
        }
    except Exception as exc:
        return {
            "passed": 0,
            "failed": 1,
            "warnings": 0,
            "checks": [{"name": "preflight_import", "passed": False, "detail": str(exc)}],
            "ok": False,
        }


# ── Assembly ────────────────────────────────────────────────────────────────

def build_diagnostic_dump(app_root: Path) -> Dict[str, Any]:
    """Collect all sections into one diagnostic artifact dict.

    Always returns a complete dict — individual section failures are captured
    as error fields, never as exceptions.
    """
    return {
        "meta": _collect_meta(),
        "writable_root": _collect_writable_root(app_root),
        "runtime_dirs": _collect_runtime_dirs(app_root),
        "config_persistence": _collect_config_persistence(app_root),
        "bundle": _collect_bundle(app_root),
        "preflight": _collect_preflight(app_root),
    }


# ── Text formatting ─────────────────────────────────────────────────────────

def format_diagnostic_dump(dump: Dict[str, Any]) -> str:
    """Render diagnostic dump as compact human-readable text for support review."""
    lines: List[str] = []
    a = lines.append

    meta = dump.get("meta", {})
    a("══════════════════════════════════════")
    a(f"  54GUC PORTABLE DIAGNOSTIC DUMP v{meta.get('dump_version', '?')}")
    a(f"  {meta.get('timestamp_utc', '?')}  host={meta.get('hostname', '?')}")
    a("══════════════════════════════════════")

    # Writable root
    wr = dump.get("writable_root", {})
    fallback = wr.get("fallback_active") or wr.get("fallback")
    a(f"\n[WRITABLE ROOT]")
    a(f"  app_root:      {wr.get('app_root', '?')}")
    a(f"  writable_root: {wr.get('writable_root', '?')}")
    a(f"  fallback:      {'⚠ YES — runtime redirected' if fallback else 'no'}")
    if wr.get("error"):
        a(f"  error:         {wr['error']}")

    # Runtime dirs
    rd = dump.get("runtime_dirs", {})
    a(f"\n[RUNTIME DIRS]")
    for key in ("runtime_dir", "memory_dir"):
        d = rd.get(key, {})
        status = "OK" if (d.get("exists") and d.get("writable")) else "⚠ PROBLEM"
        a(f"  {key:<15} {status}  path={d.get('path','?')}  files={d.get('file_count','?')}")
    lock = rd.get("lock", {})
    lock_status = "present" if lock.get("exists") else "absent"
    if lock.get("exists") and lock.get("process_running"):
        lock_status += " (process running — OK)"
    elif lock.get("exists") and not lock.get("process_running"):
        lock_status += " ⚠ (stale — process not running)"
    a(f"  lock:           {lock_status}")

    # Config persistence
    cp = dump.get("config_persistence", {})
    a(f"\n[CONFIG PERSISTENCE]")
    a(f"  config_dir:    {cp.get('config_dir', '?')}")
    a(f"  exists:        {cp.get('config_exists', '?')}")
    a(f"  writable:      {cp.get('config_writable', '?')}")
    degraded = cp.get("degraded", False)
    a(f"  status:        {'⚠ DEGRADED — ' + cp.get('reason','') if degraded else 'OK'}")

    # Bundle
    bun = dump.get("bundle", {})
    a(f"\n[BUNDLE]")
    a(f"  version:       {bun.get('version_string', '?')}")
    a(f"  build date:    {bun.get('build_date_string', '?')}")
    for name in ("config", "version", "manifest"):
        info = bun.get(name, {})
        mark = "✓" if info.get("exists") else "✗ MISSING"
        a(f"  {name:<10}    {mark}")

    # Preflight
    pf = dump.get("preflight", {})
    a(f"\n[PREFLIGHT]")
    ok = pf.get("ok", False)
    a(f"  result:        {'PASS' if ok else '⚠ FAIL'}")
    a(f"  passed={pf.get('passed',0)}  failed={pf.get('failed',0)}  warnings={pf.get('warnings',0)}")
    for chk in pf.get("checks", []):
        if not chk.get("passed"):
            a(f"  ✗ {chk.get('name','?')}: {chk.get('detail','')}")

    # Overall verdict
    problems = []
    if fallback:
        problems.append("fallback-root active")
    if degraded:
        problems.append("config persistence degraded")
    if not pf.get("ok"):
        problems.append(f"preflight failed ({pf.get('failed',0)} checks)")
    a(f"\n[VERDICT]")
    if problems:
        a(f"  ⚠ ISSUES: {', '.join(problems)}")
    else:
        a(f"  OK — no portable-state issues detected")
    a("══════════════════════════════════════")

    return "\n".join(lines)


# ── Writer ──────────────────────────────────────────────────────────────────

def write_diagnostic_dump(
    app_root: Path,
    out_dir: Path,
    *,
    json_only: bool = False,
) -> tuple[Path, Optional[Path]]:
    """Write JSON (and optionally text) diagnostic artifacts to out_dir.

    Returns (json_path, txt_path) where txt_path is None if json_only=True.
    Never raises — any write failure is re-raised to the caller who can decide
    whether to surface it.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dump = build_diagnostic_dump(app_root)

    json_path = out_dir / "portable_diagnostic.json"
    json_path.write_text(json.dumps(dump, indent=2, default=str), encoding="utf-8")

    txt_path: Optional[Path] = None
    if not json_only:
        txt_path = out_dir / "portable_diagnostic.txt"
        txt_path.write_text(format_diagnostic_dump(dump), encoding="utf-8")

    return json_path, txt_path


# ── CLI entry point ──────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="54GUC portable diagnostic dump for Windows portable validation",
    )
    parser.add_argument(
        "--app-root",
        default=None,
        help="Portable bundle root directory (default: parent of arch4 package)",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Output directory for artefacts (default: <app-root>/guc_output)",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Also print text summary to stdout",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Write only JSON artefact (skip .txt)",
    )
    args = parser.parse_args()

    # Resolve app_root
    if args.app_root:
        app_root = Path(args.app_root).resolve()
    else:
        app_root = Path(__file__).resolve().parent.parent

    # Resolve out_dir
    if args.out_dir:
        out_dir = Path(args.out_dir).resolve()
    else:
        out_dir = app_root / "guc_output"

    try:
        json_path, txt_path = write_diagnostic_dump(app_root, out_dir, json_only=args.json_only)
        print(f"Diagnostic dump written:")
        print(f"  JSON: {json_path}")
        if txt_path:
            print(f"  Text: {txt_path}")
        if args.stdout:
            dump = json.loads(json_path.read_text(encoding="utf-8"))
            print(format_diagnostic_dump(dump))
        return 0
    except Exception as exc:
        print(f"ERROR: could not write diagnostic dump: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
