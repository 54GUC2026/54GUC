from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import importlib
import io
import json
import logging
import multiprocessing
import os
import sys
import tempfile
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from arch4.application_lease import (
    APPLICATION_LEASE_LOCK_FILENAME,
    ApplicationLease,
    application_lifecycle_authority_dir,
)
from arch4.architecture_contracts.application_lifecycle import (
    ApplicationLeaseState,
)
from arch4.app_info import build_runtime_provenance
from arch4.atomic_io import atomic_output_path, atomic_write_json
from arch4.interfaces import (
    ARCH_RUNTIME_MEMORY_DIR,
    ARCH_RUNTIME_OUTPUT_DIR,
    ARCH_SW_LABEL,
    Arch4Config,
)
from arch4.portable_launcher_helpers import validate_config_file
from arch4.privacy_scrub import (
    scrub_record,
)
from arch4.support_bundle_context import SupportBundleContext
from arch4.config_file_contract import CONFIG_FILENAME
from arch4.foundation.support_bundle import (
    FoundationSupportExportSnapshot,
    LIGHT_RUNTIME_ARTIFACT_FILENAMES,
    SUPPORT_BUNDLE_VERIFICATION_RECEIPT_FILENAME,
    capture_light_runtime_support_snapshot,
    support_session_selection,
)

_log = logging.getLogger(__name__)
WINDOWS_PORTABLE_RUNTIME_DIR = "portable_runtime"


def _env_flag_enabled(name: str) -> bool:
    return str(os.environ.get(name, "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _startup_notice_suppressed_by_environment() -> bool:
    if _env_flag_enabled("ARCH_DISABLE_STARTUP_NOTICE"):
        return True
    if str(os.environ.get("PYTEST_CURRENT_TEST", "") or "").strip():
        return True
    if str(os.environ.get("QT_QPA_PLATFORM", "") or "").strip().lower() == "offscreen":
        return True
    return False


class _StartupNotice:
    """Tiny best-effort window shown before slow portable startup work."""

    def __init__(self, root: Any = None, label: Any = None):
        self._root = root
        self._label = label

    @classmethod
    def show(cls, *, profile: str = "") -> "_StartupNotice":
        if _startup_notice_suppressed_by_environment():
            return cls()
        try:
            import tkinter as tk
        except Exception:
            return cls()
        try:
            root = tk.Tk()
            root.title(ARCH_SW_LABEL)
            root.resizable(False, False)
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            try:
                root.attributes("-toolwindow", True)
            except Exception:
                pass
            frame = tk.Frame(root, padx=22, pady=16, bg="#111827")
            frame.pack(fill="both", expand=True)
            title = tk.Label(
                frame,
                text=f"{ARCH_SW_LABEL} is starting",
                font=("Segoe UI", 12, "bold"),
                fg="#f9fafb",
                bg="#111827",
            )
            title.pack(anchor="w")
            profile_text = str(profile or "").strip().upper()
            detail_text = "Loading, please wait."
            if profile_text:
                detail_text = f"Loading {profile_text}, please wait."
            label = tk.Label(
                frame,
                text=detail_text,
                font=("Segoe UI", 9),
                fg="#cbd5e1",
                bg="#111827",
            )
            label.pack(anchor="w", pady=(8, 0))
            root.update_idletasks()
            width = max(320, int(root.winfo_reqwidth()))
            height = max(104, int(root.winfo_reqheight()))
            screen_w = int(root.winfo_screenwidth() or 0)
            screen_h = int(root.winfo_screenheight() or 0)
            x = max(0, int((screen_w - width) / 2)) if screen_w else 120
            y = max(0, int((screen_h - height) / 3)) if screen_h else 120
            root.geometry(f"{width}x{height}+{x}+{y}")
            root.update()
            return cls(root, label)
        except Exception:
            try:
                root.destroy()
            except Exception:
                pass
            return cls()

    def pulse(self, message: str = "") -> None:
        root = self._root
        if root is None:
            return
        try:
            if message and self._label is not None:
                self._label.configure(text=str(message))
            root.update_idletasks()
            root.update()
        except Exception:
            pass

    def close(self) -> None:
        root = self._root
        self._root = None
        self._label = None
        if root is None:
            return
        try:
            root.destroy()
        except Exception:
            pass


# ── WSL / foreign-path guard ─────────────────────────────────────────────────


def _configure_windows_console_encoding() -> None:
    """Avoid cp1252 console crashes in Windows validation/diagnostic flows."""
    if os.name != "nt":
        return
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _ensure_console_streams() -> None:
    """Provide harmless sink streams when frozen/windowed startup has no console.

    freezer windowed launches may expose ``sys.stdout`` / ``sys.stderr`` as
    ``None``.  That breaks logging setup and any status-path writes before the
    GUI takes over.  Replace missing streams with UTF-8 devnull sinks so the
    launcher remains safe in both console and no-console modes.
    """
    for stream_name in ("stdout", "stderr"):
        if getattr(sys, stream_name, None) is not None:
            continue
        try:
            setattr(
                sys,
                stream_name,
                open(os.devnull, "w", encoding="utf-8", errors="replace"),
            )
        except Exception:
            pass


def _log_release_stamp(context: "WindowsLaunchContext") -> None:
    provenance = build_runtime_provenance(
        runtime_dir=str(context.runtime_dir),
        memory_dir=str(context.memory_dir),
        sidecar_role="launcher",
        extra={
            "profile": str(context.profile or ""),
            "config_source": str(context.config_source or ""),
            "modem_backend": str(getattr(context.config, "modem_backend", "") or ""),
            "observe_only": bool(context.observe_only),
        },
    )
    _log.info(
        "Release stamp: version=%s channel=%s build=%s commit=%s profile=%s backend=%s frozen=%s app_root=%s runtime=%s memory=%s config=%s",
        provenance.get("version", ""),
        provenance.get("release_channel", ""),
        provenance.get("build_date", ""),
        provenance.get("commit", ""),
        provenance.get("profile", ""),
        provenance.get("modem_backend", ""),
        provenance.get("frozen", False),
        provenance.get("app_root", ""),
        provenance.get("runtime_dir", ""),
        provenance.get("memory_dir", ""),
        provenance.get("config_source", ""),
    )


def _write_startup_trace(
    context: "WindowsLaunchContext",
    phase: str,
    **extra: Any,
) -> None:
    """Atomically retain only the latest pre-GUI launch state."""
    payload: Dict[str, Any] = {
        "ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "phase": str(phase),
        "profile": str(getattr(context, "profile", "") or ""),
        "app_root": str(getattr(context, "app_root", "") or ""),
        "runtime_dir": str(getattr(context, "runtime_dir", "") or ""),
        "memory_dir": str(getattr(context, "memory_dir", "") or ""),
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    payload.update(extra)
    try:
        runtime_root = (
            Path(getattr(context, "app_root", Path.cwd()))
            / WINDOWS_PORTABLE_RUNTIME_DIR
        )
        state_dir = runtime_root / "launcher_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            state_dir / "startup_state.json",
            payload,
            sort_keys=True,
            trailing_newline=True,
        )
    except Exception as exc:
        _log.debug("Startup trace write skipped: %s", exc)


def _console_safe_text(text: str) -> str:
    raw = str(text)
    if os.name != "nt":
        return raw
    stream = getattr(sys, "stdout", None)
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        return raw.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except Exception:
        return raw.encode("ascii", errors="replace").decode("ascii", errors="replace")


def _console_print(text: str = "", *, end: str = "\n") -> None:
    stream = getattr(sys, "stdout", None)
    payload = _console_safe_text(text)
    terminator = _console_safe_text(end)
    if stream is None:
        return
    try:
        stream.write(payload)
        if terminator:
            stream.write(terminator)
        stream.flush()
        return
    except Exception:
        pass
    try:
        sys.__stdout__.write(payload)
        if terminator:
            sys.__stdout__.write(terminator)
        sys.__stdout__.flush()
    except Exception:
        pass


def _is_windows_native_path(path: Path) -> bool:
    """Return True if *path* looks like a real Windows-local path.

    On a frozen Windows build, any path that starts with a POSIX mount prefix
    (``/mnt/``, ``/home/``, ``/usr/``, ``/tmp/``, ``/var/``) is a WSL or Linux
    path that was left in the environment by a prior WSL session.  Following it
    would silently redirect the portable runtime to a WSL tree the EXE cannot
    write, and the bundle exporter would export the wrong session.

    The check is intentionally conservative:
    - On Windows (``os.name == 'nt'``): reject anything whose string form starts
      with one of the known POSIX prefixes.
    - On POSIX (dev/CI): always accept — no Windows path guard needed.
    """
    if os.name != "nt":
        return True  # POSIX dev/CI — no WSL risk
    raw = str(path)
    posix_prefixes = ("/mnt/", "/home/", "/usr/", "/tmp/", "/var/", "/root/", "/run/")
    return not any(raw.startswith(p) for p in posix_prefixes)


def _looks_like_windows_drive_path(raw: Any) -> bool:
    text = str(raw or "").strip()
    if text.startswith("\\\\?\\") and not text.startswith("\\\\?\\UNC\\"):
        text = text[4:]
    if len(text) < 3:
        return False
    return bool(text[0].isalpha() and text[1] == ":" and text[2] in {"\\", "/"})


def _is_windows_runtime_storage_path(raw: Any) -> bool:
    """Return True when *raw* is safe for Windows-owned runtime state.

    Windows source-runs may load code from a UNC/WSL app root, but runtime
    files and SQLite databases must live on a local Windows drive. UNC paths
    are valid Windows paths for code, but they are not valid storage ownership
    for the live runtime.
    """
    if os.name != "nt":
        return True
    text = str(raw or "").strip()
    if not text:
        return False
    if _windows_lock_path_is_unc(text):
        return False
    posix_prefixes = ("/mnt/", "/home/", "/usr/", "/tmp/", "/var/", "/root/", "/run/")
    if any(text.startswith(prefix) for prefix in posix_prefixes):
        return False
    return _looks_like_windows_drive_path(text)


def _sanitise_env_path(raw: str, fallback: Path, label: str) -> Path:
    """Resolve *raw* env-var path, rejecting WSL/foreign paths.

    If the resolved path fails the Windows-native guard, log a warning and
    return *fallback* so the portable build stays on its intended local tree.
    """
    if not raw:
        return fallback
    if not _is_windows_runtime_storage_path(raw):
        _log.warning(
            "Ignoring %s env var pointing to non-local Windows runtime storage %s. "
            "Using local runtime root instead.",
            label,
            raw,
        )
        return fallback
    candidate = Path(raw).resolve()
    if not _is_windows_native_path(candidate):
        _log.warning(
            "Ignoring %s env var pointing to non-Windows path %s — "
            "likely a stale WSL env var. Using local runtime root instead.",
            label,
            candidate,
        )
        return fallback
    return candidate


def _portable_runtime_root_for_app(app_root: Path) -> Path:
    return (app_root / WINDOWS_PORTABLE_RUNTIME_DIR).resolve()


def _is_timestamped_runtime_output_dir_name(name: str) -> bool:
    text = str(name or "").strip()
    prefix = f"{ARCH_RUNTIME_OUTPUT_DIR}_"
    if not text.startswith(prefix):
        return False
    stamp = text[len(prefix) : len(prefix) + 15]
    suffix = text[len(prefix) + 15 :]
    if len(stamp) != 15 or stamp[8] != "_":
        return False
    if not (stamp[:8].isdigit() and stamp[9:].isdigit()):
        return False
    return not suffix or (suffix.startswith("_") and suffix[1:].isdigit())


def _is_runtime_output_dir_name(name: str) -> bool:
    text = str(name or "").strip()
    return text == ARCH_RUNTIME_OUTPUT_DIR or _is_timestamped_runtime_output_dir_name(
        text
    )


def _runtime_output_root_for_dir(runtime_dir: Path) -> Path:
    resolved = Path(runtime_dir).resolve()
    if _is_runtime_output_dir_name(resolved.name):
        return resolved.parent.resolve()
    return resolved


def _portable_runtime_root(context: "WindowsLaunchContext") -> Path:
    runtime_dir = Path(context.runtime_dir)
    if (
        _is_runtime_output_dir_name(runtime_dir.name)
        or runtime_dir.name == ARCH_RUNTIME_MEMORY_DIR
    ):
        return runtime_dir.parent.resolve()
    return runtime_dir.resolve()


def _effective_portable_runtime_root(context: "WindowsLaunchContext") -> Path:
    for env_name in ("ARCH_RUNTIME_OUTPUT_DIR", "ARCH_RUNTIME_MEMORY_DIR"):
        raw = str(os.environ.get(env_name, "") or "").strip()
        if not raw:
            continue
        env_path = Path(raw).resolve()
        if (
            _is_runtime_output_dir_name(env_path.name)
            or env_path.name == ARCH_RUNTIME_MEMORY_DIR
        ):
            return env_path.parent.resolve()
        return env_path
    return _portable_runtime_root(context)


def _allow_explicit_runtime_env_override(context: "WindowsLaunchContext") -> bool:
    """Admit explicit runtime paths only for governed Windows source runs.

    Portable app launches must not inherit stale ``ARCH_RUNTIME_*`` variables
    from a prior shell.  The exception is a Windows Python source-run whose
    app root is a UNC path such as ``\\\\wsl.localhost\\...``: code may live in WSL,
    but SQLite/runtime state must be redirected to a real Windows filesystem.
    """
    return os.name == "nt" and not getattr(sys, "frozen", False) and (
        bool(getattr(context, "source_run_precreated_session", False))
        or _windows_lock_path_is_unc(getattr(context, "app_root", ""))
    )


def _context_with_runtime_paths(
    context: "WindowsLaunchContext",
    *,
    runtime_dir: Path,
    memory_dir: Path,
) -> "WindowsLaunchContext":
    resolved_runtime = Path(runtime_dir).resolve()
    resolved_memory = Path(memory_dir).resolve()
    if (
        resolved_runtime == Path(context.runtime_dir).resolve()
        and resolved_memory == Path(context.memory_dir).resolve()
    ):
        return context
    return replace(
        context,
        runtime_dir=resolved_runtime,
        memory_dir=resolved_memory,
    )


def _context_with_explicit_runtime_env_override(
    context: "WindowsLaunchContext",
) -> "WindowsLaunchContext":
    """Apply source-run runtime env overrides without creating directories."""
    if not _allow_explicit_runtime_env_override(context):
        return context
    runtime_dir = context.runtime_dir
    memory_dir = context.memory_dir
    env_output = os.environ.get("ARCH_RUNTIME_OUTPUT_DIR", "").strip()
    env_memory = os.environ.get("ARCH_RUNTIME_MEMORY_DIR", "").strip()
    missing = [
        name
        for name, value in (
            ("ARCH_RUNTIME_OUTPUT_DIR", env_output),
            ("ARCH_RUNTIME_MEMORY_DIR", env_memory),
        )
        if not value
    ]
    if missing:
        raise StartupError(
            "Windows source-run requires explicit NTFS runtime paths.",
            detail=(
                "Set ARCH_RUNTIME_OUTPUT_DIR and ARCH_RUNTIME_MEMORY_DIR to a local Windows "
                "path such as D:\\CODEX\\54b\\hp_source_run_runtime\\b818 before launch."
            ),
            reason="runtime_env",
        )
    for label, value in (
        ("ARCH_RUNTIME_OUTPUT_DIR", env_output),
        ("ARCH_RUNTIME_MEMORY_DIR", env_memory),
    ):
        if not _is_windows_runtime_storage_path(value):
            raise StartupError(
                f"{label} is not valid Windows runtime storage for a source-run: {value}",
                detail=(
                    "Runtime output and memory, especially calibration.db, must be on a "
                    "local Windows drive, not \\\\wsl.localhost or another UNC path."
                ),
                reason="runtime_env",
            )
    if env_output:
        runtime_dir = _sanitise_env_path(
            env_output, context.runtime_dir, "ARCH_RUNTIME_OUTPUT_DIR"
        )
    if env_memory:
        memory_dir = _sanitise_env_path(
            env_memory, context.memory_dir, "ARCH_RUNTIME_MEMORY_DIR"
        )
    return _context_with_runtime_paths(
        context, runtime_dir=runtime_dir, memory_dir=memory_dir
    )


@dataclass(frozen=True)
class WindowsBundleProfile:
    name: str
    backend: str
    env_prefix: str
    expected_model_default: str
    reduced_shell: bool
    live_default: bool
    profile_id: str = ""
    profile_path: str = ""


WINDOWS_BUNDLE_PROFILES: Dict[str, WindowsBundleProfile] = {
    "bootstrap": WindowsBundleProfile(
        name="bootstrap",
        backend="",
        env_prefix="",
        expected_model_default="",
        reduced_shell=False,
        live_default=True,
        profile_id="",
        profile_path="",
    ),
    "mc888": WindowsBundleProfile(
        name="mc888",
        backend="mc888",
        env_prefix="MC888",
        expected_model_default="",
        reduced_shell=True,
        live_default=True,
        profile_id="",
        profile_path="config/modems/mc888.env",
    ),
    "b818": WindowsBundleProfile(
        name="b818",
        backend="b818",
        env_prefix="B818",
        expected_model_default="",
        reduced_shell=False,
        live_default=True,
        profile_id="",
        profile_path="config/modems/b818.env",
    ),
    "internal": WindowsBundleProfile(
        name="internal",
        backend="b818",
        env_prefix="B818",
        expected_model_default="",
        reduced_shell=False,
        live_default=False,
        profile_id="",
        profile_path="config/modems/b818.env",
    ),
}


def _env_prefix_from_host_keys(host_keys: Iterable[str]) -> str:
    for key in tuple(host_keys or ()):
        token = str(key or "").strip().upper()
        if token.endswith("_HOST"):
            return token[: -len("_HOST")]
    return ""


def _exact_windows_bundle_profiles() -> Dict[str, WindowsBundleProfile]:
    from arch4.modem_live.profile_catalog import modem_profile_selections

    rows: Dict[str, WindowsBundleProfile] = {}
    for contract in modem_profile_selections():
        prefix = _env_prefix_from_host_keys(contract.env_host_keys)
        rows[contract.profile_id] = WindowsBundleProfile(
            name=contract.profile_id,
            backend=contract.backend,
            env_prefix=prefix,
            expected_model_default=contract.target_guard_model,
            reduced_shell=contract.backend == "mc888",
            live_default=True,
            profile_id=contract.profile_id,
            profile_path=contract.profile_path,
        )
    return rows


WINDOWS_BUNDLE_PROFILES.update(_exact_windows_bundle_profiles())

WINDOWS_PROFILE_DEFAULT_VALUES: Dict[str, Dict[str, str]] = {
    "bootstrap": {
        "SAMPLE_INTERVAL": "2.0",
    },
    "mc888": {
        "MC888_HOST": "",
        "MC888_USER": "admin",
        "MC888_PASS": "",
        "MC888_EXPECTED_MODEL": "",
        "MC888_EXPECTED_SERIAL": "",
        "MC888_EXPECTED_FIRMWARE": "",
        "MC888_EXPECTED_IMEI": "",
        "PING_IFACE": "",
        "SAMPLE_INTERVAL": "2.0",
    },
    "b818": {
        "B818_HOST": "",
        "B818_USER": "admin",
        "B818_PASS": "",
        "B818_EXPECTED_MODEL": "",
        "B818_EXPECTED_SERIAL": "",
        "B818_EXPECTED_FIRMWARE": "",
        "B818_EXPECTED_IMEI": "",
        "PING_IFACE": "",
        "SAMPLE_INTERVAL": "2.0",
        "SINR_SCALE": "1.0",
        "RSRP_SCALE": "1.0",
        "RSRQ_SCALE": "1.0",
        "RSSI_SCALE": "1.0",
        "SIGNAL_CONTRACT_WARN": "1",
        "EMERGENCY_SINR_FLOOR": "0.0",
    },
    "internal": {
        "B818_HOST": "",
        "B818_USER": "admin",
        "B818_PASS": "",
        "B818_EXPECTED_MODEL": "",
        "B818_EXPECTED_SERIAL": "",
        "B818_EXPECTED_FIRMWARE": "",
        "B818_EXPECTED_IMEI": "",
        "PING_IFACE": "",
        "SAMPLE_INTERVAL": "2.0",
        "SINR_SCALE": "1.0",
        "RSRP_SCALE": "1.0",
        "RSRQ_SCALE": "1.0",
        "RSSI_SCALE": "1.0",
        "SIGNAL_CONTRACT_WARN": "1",
        "EMERGENCY_SINR_FLOOR": "0.0",
    },
}

try:
    from arch4.windows_bundle_embedded import EMBEDDED_WINDOWS_PROFILE_VALUES
except Exception:
    EMBEDDED_WINDOWS_PROFILE_VALUES: Dict[str, Dict[str, str]] = {}


class StartupError(RuntimeError):
    """Compact, user-facing startup failure.

    message  – one-line explanation shown in the terminal / message box.
    detail   – optional longer hint (e.g. "Run as Administrator" or "Move to a writable folder").
    reason   – short machine-readable tag for log grep / telemetry
               (e.g. ``"writable"``, ``"chdir"``, ``"mkdir_output"``).
    """

    def __init__(self, message: str, *, detail: str = "", reason: str = ""):
        self.detail = detail
        self.reason = reason
        super().__init__(message)


def verify_writable_root(app_root: Path) -> Path:
    """Check that *app_root* is writable; fall back if not.

    Tries app_root first. If not writable, uses the fallback chain
    from ``runtime_paths.resolve_writable_root()``. Returns the
    resolved writable root (may differ from app_root).

    Raises ``StartupError`` only when ALL candidates fail.
    """
    from arch4.runtime_paths import (
        resolve_writable_root,
        is_fallback_root_active,
        clear_writable_root_cache,
        clear_app_root_cache,
    )

    # Reject if path exists but is a file (common user mistake).
    if app_root.exists() and not app_root.is_dir():
        raise StartupError(
            f"App folder path is a file, not a directory: {app_root}",
            detail="Remove or rename the file, then restart.",
            reason="path_is_file",
        )

    # Clean up stale probe from a prior interrupted startup.
    stale_probe = app_root / ".guc_write_probe"
    if stale_probe.exists():
        try:
            stale_probe.unlink()
        except OSError:
            pass

    # Ensure runtime_paths resolves using this app_root.
    os.environ["ARCH_APP_ROOT"] = str(app_root.resolve())
    clear_app_root_cache()
    clear_writable_root_cache()

    try:
        writable_root = resolve_writable_root()
    except RuntimeError as exc:
        raise StartupError(
            f"App root not writable and no writable fallback was found (tried {app_root} and fallbacks)",
            detail=str(exc),
            reason="writable",
        )

    if is_fallback_root_active():
        _log.warning(
            "App root %s not writable — runtime data will be stored at %s",
            app_root,
            writable_root,
        )

    return writable_root


def _modem_live_clock_plan(config: Arch4Config) -> Dict[str, object]:
    try:
        from arch4.modem_live.access_profile_owner import (
            modem_live_bootstrap_clock_plan,
        )

        return modem_live_bootstrap_clock_plan(config)
    except Exception as exc:
        _log.debug("Unable to resolve Modem Live clock plan: %s", exc)
        return {}


@dataclass(frozen=True)
class WindowsLaunchContext:
    profile: str
    app_root: Path
    config_source: str
    runtime_dir: Path
    memory_dir: Path
    observe_only: bool
    windows_mc888: bool
    config: Arch4Config
    source_run_precreated_session: bool = False

    def as_dict(self) -> Dict[str, object]:
        return {
            "profile": self.profile,
            "app_root": str(self.app_root),
            "config_source": str(self.config_source),
            "runtime_dir": str(self.runtime_dir),
            "memory_dir": str(self.memory_dir),
            "observe_only": bool(self.observe_only),
            "windows_mc888": bool(self.windows_mc888),
            "modem_backend": str(self.config.modem_backend),
            "sample_interval_s": float(self.config.sample_interval_s),
            "engine_sample_interval_s": float(self.config.sample_interval_s),
            **_modem_live_clock_plan(self.config),
        }


def resolve_app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def read_shell_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _coerce_float(raw: str, default: float) -> float:
    try:
        return float(str(raw or "").strip())
    except Exception:
        return float(default)


def _coerce_bool(raw: str, default: bool) -> bool:
    token = str(raw or "").strip().lower()
    if not token:
        return bool(default)
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _pick_profile(name: str) -> WindowsBundleProfile:
    key = str(name or "").strip().lower()
    if key not in WINDOWS_BUNDLE_PROFILES:
        supported = ", ".join(sorted(WINDOWS_BUNDLE_PROFILES))
        raise StartupError(
            f"Unsupported Windows profile '{name}'. Supported: {supported}",
            reason="profile_invalid",
        )
    return WINDOWS_BUNDLE_PROFILES[key]


def _build_runtime_dir(app_root: Path, profile: WindowsBundleProfile) -> Path:
    """Windows portable runtime dir under the portable root next to exe."""
    del profile  # Unused — paths are app-level, not modem-specific
    return (
        _portable_runtime_root_for_app(app_root) / ARCH_RUNTIME_OUTPUT_DIR
    ).resolve()


def _build_timestamped_runtime_dir(base_runtime_dir: Path) -> Path:
    """Return a new per-launch runtime output directory.

    ``portable_runtime/guc_output`` is only the historical anchor. Live Windows
    sessions must write directly into ``guc_output_YYYYMMDD_HHMMSS`` so support
    bundles and soak comparisons cannot mix multiple sessions.
    """
    runtime_root = _runtime_output_root_for_dir(base_runtime_dir)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = (runtime_root / f"{ARCH_RUNTIME_OUTPUT_DIR}_{stamp}").resolve()
    suffix = 1
    while candidate.exists():
        candidate = (
            runtime_root / f"{ARCH_RUNTIME_OUTPUT_DIR}_{stamp}_{suffix:02d}"
        ).resolve()
        suffix += 1
    return candidate


def _validate_precreated_source_runtime_session(
    context: "WindowsLaunchContext",
) -> None:
    """Admit only the run directory created by the source-debug boundary."""

    if not _allow_explicit_runtime_env_override(context):
        raise StartupError(
            "Precreated runtime sessions are restricted to explicit Windows source-runs.",
            reason="source_runtime_session",
        )
    runtime_dir = Path(context.runtime_dir).resolve()
    memory_dir = Path(context.memory_dir).resolve()
    if (
        runtime_dir.name != ARCH_RUNTIME_OUTPUT_DIR
        or memory_dir.name != ARCH_RUNTIME_MEMORY_DIR
        or runtime_dir.parent != memory_dir.parent
    ):
        raise StartupError(
            "Precreated source-run paths do not form one isolated runtime session.",
            reason="source_runtime_session",
        )
    manifest_path = runtime_dir.parent / "launch_manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StartupError(
            "Precreated source-run launch manifest is unavailable.",
            detail=f"{type(exc).__name__}: {exc}",
            reason="source_runtime_session",
        ) from exc
    if not isinstance(payload, Mapping):
        raise StartupError(
            "Precreated source-run launch manifest is invalid.",
            reason="source_runtime_session",
        )
    expected_mode = "observe" if context.observe_only else "live"
    exact = {
        "schema": "54guc_cgf_source_run_launch_manifest_v1",
        "owner": "scripts.windows_ngui_debug_autoconnect",
        "profile": context.profile,
        "mode": expected_mode,
        "output_dir": str(runtime_dir),
        "memory_dir": str(memory_dir),
    }
    mismatches = tuple(
        key for key, value in exact.items() if payload.get(key) != value
    )
    if mismatches:
        raise StartupError(
            "Precreated source-run launch manifest does not match the runtime session.",
            detail=",".join(mismatches),
            reason="source_runtime_session",
        )


_RUNTIME_PAYLOAD_FILENAMES = LIGHT_RUNTIME_ARTIFACT_FILENAMES


def _runtime_payload_mtime(runtime_dir: Path) -> float:
    """Return newest payload mtime, or 0 when the directory has no runtime data."""
    if not runtime_dir.is_dir():
        return 0.0
    newest = 0.0
    for name in _RUNTIME_PAYLOAD_FILENAMES:
        path = runtime_dir / name
        if not path.is_file():
            continue
        try:
            newest = max(newest, float(path.stat().st_mtime))
        except OSError:
            continue
    return newest


def _latest_runtime_output_dir(base_runtime_dir: Path) -> Path:
    """Find the newest runtime output folder with actual payload files."""
    base = Path(base_runtime_dir).resolve()
    runtime_root = _runtime_output_root_for_dir(base)
    candidates: list[Path] = []
    plain = (runtime_root / ARCH_RUNTIME_OUTPUT_DIR).resolve()
    if plain.is_dir():
        candidates.append(plain)
    try:
        for item in runtime_root.glob(f"{ARCH_RUNTIME_OUTPUT_DIR}_*"):
            if item.is_dir() and _is_timestamped_runtime_output_dir_name(item.name):
                candidates.append(item.resolve())
    except Exception:
        pass
    scored = [
        (candidate, _runtime_payload_mtime(candidate)) for candidate in candidates
    ]
    scored = [(candidate, score) for candidate, score in scored if score > 0.0]
    if not scored:
        return base
    return max(scored, key=lambda item: item[1])[0]


def _build_memory_dir(app_root: Path, profile: WindowsBundleProfile) -> Path:
    """Windows portable memory dir under the same portable root."""
    del profile  # Unused — paths are app-level, not modem-specific
    return (
        _portable_runtime_root_for_app(app_root) / ARCH_RUNTIME_MEMORY_DIR
    ).resolve()


def _default_profile_values(profile: WindowsBundleProfile) -> Dict[str, str]:
    direct = dict(WINDOWS_PROFILE_DEFAULT_VALUES.get(profile.name, {}))
    if direct:
        return direct
    prefix = str(profile.env_prefix or "").strip().upper()
    values: Dict[str, str] = {
        "PING_IFACE": "",
        "SAMPLE_INTERVAL": "2.0",
        "SINR_SCALE": "1.0",
        "RSRP_SCALE": "1.0",
        "RSRQ_SCALE": "1.0",
        "RSSI_SCALE": "1.0",
        "SIGNAL_CONTRACT_WARN": "1",
        "EMERGENCY_SINR_FLOOR": "0.0",
    }
    if prefix:
        values.update(
            {
                f"{prefix}_HOST": "",
                f"{prefix}_USER": "admin",
                f"{prefix}_PASS": "",
                f"{prefix}_EXPECTED_MODEL": str(profile.expected_model_default or ""),
                f"{prefix}_EXPECTED_SERIAL": "",
                f"{prefix}_EXPECTED_FIRMWARE": "",
                f"{prefix}_EXPECTED_IMEI": "",
            }
        )
    return values


def _embedded_profile_values(profile: WindowsBundleProfile) -> Dict[str, str]:
    raw = EMBEDDED_WINDOWS_PROFILE_VALUES.get(profile.name, {})
    out: Dict[str, str] = {}
    for key, value in dict(raw or {}).items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        out[key_text] = str(value or "")
    return out


def _build_config_for_profile(
    profile: WindowsBundleProfile, values: Mapping[str, str]
) -> Arch4Config:
    from arch4.config_views import apply_connection_credentials

    sample_interval_s = _coerce_float(values.get("SAMPLE_INTERVAL", ""), 2.0)
    backend = str(profile.backend or "").strip().lower()
    prefix = str(profile.env_prefix or "").strip().upper()

    # Bootstrap profile: no vendor, no credentials, just tick interval
    if not backend:
        return Arch4Config(
            sample_interval_s=sample_interval_s,
            modem_backend="",
        )

    host_key = f"{prefix}_HOST"
    user_key = f"{prefix}_USER"
    pass_key = f"{prefix}_PASS"
    model_key = f"{prefix}_EXPECTED_MODEL"

    defaults = _default_profile_values(profile)
    host_default = str(defaults.get(host_key, "") or "")
    user_default = str(defaults.get(user_key, "admin") or "admin")
    pass_default = str(defaults.get(pass_key, "") or "")

    host = str(values.get(host_key, host_default) or host_default)
    user = str(values.get(user_key, user_default) or user_default)
    password = str(values.get(pass_key, pass_default) or pass_default)

    cfg = Arch4Config(
        sample_interval_s=sample_interval_s,
        modem_backend=backend,
        modem_profile_id=profile.profile_id,
        target_guard_model=str(
            values.get(model_key, profile.expected_model_default)
            or profile.expected_model_default
        ),
        target_guard_serial=str(values.get(f"{prefix}_EXPECTED_SERIAL", "") or ""),
        target_guard_firmware=str(values.get(f"{prefix}_EXPECTED_FIRMWARE", "") or ""),
        target_guard_imei=str(values.get(f"{prefix}_EXPECTED_IMEI", "") or ""),
        signal_sinr_scale=_coerce_float(values.get("SINR_SCALE", ""), 1.0),
        signal_rsrp_scale=_coerce_float(values.get("RSRP_SCALE", ""), 1.0),
        signal_rsrq_scale=_coerce_float(values.get("RSRQ_SCALE", ""), 1.0),
        signal_rssi_scale=_coerce_float(values.get("RSSI_SCALE", ""), 1.0),
        signal_contract_warn=_coerce_bool(values.get("SIGNAL_CONTRACT_WARN", ""), True),
        emergency_sinr_floor=_coerce_float(values.get("EMERGENCY_SINR_FLOOR", ""), 0.0),
    )
    apply_connection_credentials(cfg, backend, host, user, password)
    if backend == "mikrotik":
        cfg.mikrotik_at_live_polling_enabled = _coerce_bool(
            values.get("MIKROTIK_AT_LIVE_POLLING", ""),
            False,
        )
        cfg.mikrotik_runtime_capability_probe_enabled = _coerce_bool(
            values.get("MIKROTIK_RUNTIME_CAPABILITY_PROBE", ""),
            False,
        )

    # Vendor-specific ping interface — read from profile env vars.
    ping_iface = str(values.get("PING_IFACE", "") or "")
    if ping_iface:
        from arch4.modem_live.profile_catalog import modem_connection_config_fields
        from arch4.operator_profiles import infer_backend_family

        family = infer_backend_family(backend)
        ping_field = modem_connection_config_fields(backend)[3]
        if ping_field:
            setattr(cfg, ping_field, ping_iface)
        elif family == "huawei_hilink_like":
            cfg.b818_ping_interface = ping_iface
        elif family in {"zte_goform_like", "zte_zwrt_ubus_like"}:
            cfg.zte_ping_interface = ping_iface

    return cfg


def build_launch_context(
    *,
    profile_name: str,
    app_root: Path,
    config_path: Path | None = None,
    observe_only: bool | None = None,
) -> WindowsLaunchContext:
    profile = _pick_profile(profile_name)
    if config_path is not None:
        chosen_config_path = Path(config_path).resolve()
        if not chosen_config_path.is_file():
            raise StartupError(
                f"Missing Windows modem profile override: {chosen_config_path}",
                detail="Check the --config path and retry.",
                reason="config_missing",
            )
        values = _default_profile_values(profile)
        values.update(_embedded_profile_values(profile))
        values.update(read_shell_env_file(chosen_config_path))
        config_source = str(chosen_config_path)
    else:
        values = _default_profile_values(profile)
        values.update(_embedded_profile_values(profile))
        local_profile_path = (
            app_root / str(profile.profile_path or f"config/modems/{profile.name}.env")
        ).resolve()
        local_override_path = (
            app_root / "config" / "modems.local" / f"{profile.name}.env"
        ).resolve()
        if profile.name != "bootstrap" and local_profile_path.is_file():
            values.update(read_shell_env_file(local_profile_path))
            config_source = str(local_profile_path)
            if local_override_path.is_file():
                values.update(read_shell_env_file(local_override_path))
                config_source = str(local_override_path)
        else:
            config_source = f"embedded:{profile.name}"
    runtime_dir = _build_runtime_dir(app_root, profile)
    memory_dir = _build_memory_dir(app_root, profile)
    if observe_only is None:
        resolved_observe_only = not profile.live_default
    else:
        resolved_observe_only = bool(observe_only)
    return WindowsLaunchContext(
        profile=profile.name,
        app_root=app_root.resolve(),
        config_source=config_source,
        runtime_dir=runtime_dir,
        memory_dir=memory_dir,
        observe_only=resolved_observe_only,
        windows_mc888=bool(profile.reduced_shell),
        config=_build_config_for_profile(profile, values),
    )


def _configure_runtime_environment(
    context: WindowsLaunchContext,
) -> WindowsLaunchContext:
    """Create runtime dirs and set environment variables.

    When writable-root fallback has redirected paths, the env vars
    ``ARCH_RUNTIME_OUTPUT_DIR`` / ``ARCH_RUNTIME_MEMORY_DIR`` will
    already point to the fallback location. This function detects
    that case by checking whether the env var differs from what
    the context says — if so, the fallback override wins.

    Raises ``StartupError`` with a compact message if any directory
    cannot be created (e.g. disk full, permissions, path too long).
    """
    try:
        os.chdir(str(context.app_root))
    except OSError as exc:
        raise StartupError(
            f"Cannot set working directory: {context.app_root}",
            detail=str(exc),
            reason="chdir",
        )

    # Detect whether writable-root fallback has redirected paths.
    from arch4.runtime_paths import is_fallback_root_active

    effective_output = context.runtime_dir
    effective_memory = context.memory_dir
    if is_fallback_root_active() or _allow_explicit_runtime_env_override(context):
        env_output = os.environ.get("ARCH_RUNTIME_OUTPUT_DIR", "").strip()
        env_memory = os.environ.get("ARCH_RUNTIME_MEMORY_DIR", "").strip()
        if env_output:
            effective_output = _sanitise_env_path(
                env_output, context.runtime_dir, "ARCH_RUNTIME_OUTPUT_DIR"
            )
        if env_memory:
            effective_memory = _sanitise_env_path(
                env_memory, context.memory_dir, "ARCH_RUNTIME_MEMORY_DIR"
            )

    created_paths: set[Path] = set()
    for label, path in (("output", effective_output), ("memory", effective_memory)):
        if path in created_paths:
            continue
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StartupError(
                f"Cannot create {label} directory: {path}",
                detail=str(exc),
                reason=f"mkdir_{label}",
            )
        created_paths.add(path)
    os.environ["ARCH_RUNTIME_OUTPUT_DIR"] = str(effective_output)
    os.environ["ARCH_RUNTIME_MEMORY_DIR"] = str(effective_memory)
    if sys.platform.startswith("win"):
        from arch4.foundation.traffic_accounting import (
            FOUNDATION_TRAFFIC_ACCOUNTING_ROOT_ENV,
            prepare_portable_traffic_accounting_root,
        )

        os.environ[FOUNDATION_TRAFFIC_ACCOUNTING_ROOT_ENV] = str(
            prepare_portable_traffic_accounting_root(context.app_root)
        )
    if bool(getattr(sys, "frozen", False)):
        from arch4.core.rsrq_baseline_state import (
            RSRQ_BASELINE_STATE_ROOT_ENV,
            portable_rsrq_baseline_state_root,
        )
        from arch4.product_intelligence_runtime import (
            portable_intelligence_history_path,
        )

        try:
            history_env, history_path = portable_intelligence_history_path(
                context.app_root
            )
        except (ValueError, OSError) as exc:
            raise StartupError(
                "Connected portable History path is invalid or missing.",
                detail=str(exc),
                reason="connected_history_path",
            ) from exc
        os.environ[history_env] = str(history_path)
        os.environ[RSRQ_BASELINE_STATE_ROOT_ENV] = str(
            portable_rsrq_baseline_state_root(context.app_root)
        )
    os.environ["ARCH_WINDOWS_BUNDLE_PROFILE"] = str(context.profile)
    os.environ.pop("ARCH_WINDOWS_AUTOCONNECT_PROFILE", None)
    os.environ.pop("ARCH_WINDOWS_SOURCE_RUN_AUTOCONNECT", None)
    if context.profile in ("internal", "bootstrap"):
        os.environ.pop("ARCH_WINDOWS_ALLOWED_BACKENDS", None)
    else:
        os.environ["ARCH_WINDOWS_ALLOWED_BACKENDS"] = str(
            context.config.modem_backend or ""
        )
    return _context_with_runtime_paths(
        context,
        runtime_dir=effective_output,
        memory_dir=effective_memory,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"{ARCH_SW_LABEL} Windows Native Launcher"
    )
    parser.add_argument(
        "--profile",
        default="bootstrap",
        choices=sorted(WINDOWS_BUNDLE_PROFILES),
        help="Windows packaging profile (bootstrap default — DEVICE page resolves modem; explicit profiles are for direct backend starts).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--live", action="store_true", help="Start in live/manual-action mode."
    )
    mode.add_argument(
        "--observe", action="store_true", help="Start in observe-only mode."
    )
    parser.add_argument(
        "--config",
        default="",
        help="Optional modem profile override for internal testing. Public installers use embedded defaults.",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    parser.add_argument(
        "--profile-tools", nargs=argparse.REMAINDER, default=None,
        help="Export, validate or review portable profiles without starting the application; pass --help for commands.",
    )
    parser.add_argument(
        "--open-logs",
        action="store_true",
        help="Open the active runtime log folder and exit.",
    )
    parser.add_argument(
        "--create-support-bundle",
        action="store_true",
        help="Create a support bundle zip from portable_runtime and exit.",
    )
    parser.add_argument(
        "--print-launch-plan",
        action="store_true",
        help="Print the resolved launch plan as JSON and exit without starting the GUI.",
    )
    parser.add_argument(
        "--portable-validate",
        action="store_true",
        help=(
            "Run portable-state validation: write diagnostic dump and post-launch checklist "
            "to portable_runtime/, print summary to stdout, exit 0 if all automated checks pass."
        ),
    )
    parser.add_argument(
        "--skip-beta-state-import",
        action="store_true",
        help="Explicitly record that Beta state import was skipped before starting the GUI.",
    )
    parser.add_argument(
        "--source-run-precreated-session",
        action="store_true",
        help=(
            "Use the isolated runtime session created by the connected-source "
            "launcher after validating its launch manifest."
        ),
    )
    return parser


def _resolve_observe_flag(args: argparse.Namespace) -> bool | None:
    if bool(getattr(args, "observe", False)):
        return True
    if bool(getattr(args, "live", False)):
        return False
    return None


def _print_launch_plan(context: WindowsLaunchContext) -> int:
    _console_print(json.dumps(context.as_dict(), indent=2, sort_keys=True))
    return 0


def _application_lease_lock_path(context: WindowsLaunchContext) -> Path:
    del context
    return (
        application_lifecycle_authority_dir()
        / APPLICATION_LEASE_LOCK_FILENAME
    ).resolve()


def _windows_lock_path_is_unc(path: Any) -> bool:
    text = str(path or "")
    if text.startswith("\\\\?\\UNC\\"):
        return True
    if text.startswith("\\\\?\\"):
        return False
    return text.startswith("\\\\")


def _should_acquire_single_instance_lock(args: argparse.Namespace) -> bool:
    return not any(
        (
            bool(getattr(args, "print_launch_plan", False)),
            bool(getattr(args, "open_logs", False)),
        )
    )


def _build_application_lease(
    context: WindowsLaunchContext,
) -> ApplicationLease:
    return ApplicationLease(
        app_root=context.app_root,
        profile=context.profile,
        authority_dir=application_lifecycle_authority_dir(),
        executable_path=Path(sys.executable),
    )


def _runtime_root(context: WindowsLaunchContext) -> Path:
    """Root directory for launcher metadata files."""
    return context.app_root.resolve()


def _portable_config_path(context: WindowsLaunchContext) -> Path:
    return (context.app_root / CONFIG_FILENAME).resolve()


def support_bundle_context_from_windows_launch(
    context: WindowsLaunchContext,
) -> SupportBundleContext:
    """Explicitly adapt the launcher owner into the support-export contract."""

    if not isinstance(context, WindowsLaunchContext):
        raise TypeError("support_bundle_windows_launch_context_required")
    return SupportBundleContext.from_runtime_paths(
        profile=context.profile,
        app_root=context.app_root,
        runtime_dir=context.runtime_dir,
        memory_dir=context.memory_dir,
        source="windows_launch_context",
    )


def _support_bundle_root(context: SupportBundleContext) -> Path:
    return (context.app_root / "support_bundles").resolve()




def _bundle_relative_path(dst: Path, stage_root: Path | None) -> str:
    if stage_root is None:
        return dst.name
    try:
        return dst.relative_to(stage_root).as_posix()
    except ValueError:
        return dst.name


def _stage_scrubbed_bundle_file(*args, **kwargs):
    raise RuntimeError("early_support_privacy_staging_retired_use_finalization")


def _stage_scrubbed_bundle_bytes(*args, **kwargs):
    raise RuntimeError("early_support_privacy_staging_retired_use_finalization")


def _stage_product_history_export(*args, **kwargs):
    raise RuntimeError("early_support_history_staging_retired_use_finalization")


def _stage_generated_bundle_text(*args, **kwargs):
    raise RuntimeError("early_support_privacy_staging_retired_use_finalization")


def _support_bundle_privacy_integrity(*args, **kwargs):
    raise RuntimeError("staging_only_integrity_retired_use_final_archive_reader")


def _write_privacy_note() -> str:
    """Build a short privacy note for inclusion in support bundles.

    Tells the tester which data is present and which is scrubbed.
    """
    lines = [
        "54GUC SUPPORT BUNDLE — PRIVACY NOTE",
        "=" * 50,
        "",
        "This bundle was created by 54GUC to help diagnose signal quality",
        "and band selection issues.  The following rules apply.",
        "",
        "DATA INCLUDED",
        "-------------",
        "  Radio signal measurements: SINR, RSRP, RSRQ, RSSI (dB values)",
        "  Band selection events and band learning state",
        "  Session uptime and connection quality scores",
        "  Modem model and firmware version",
        "  Carrier PLMN code (e.g. 27201 for an Irish network)",
        "  Typed APN settings: name, value, username, authentication and protocol",
        "  An explicitly identified APN password, when present (provider configuration)",
        "  Approximate throughput in Mbps",
        "",
        "DATA MASKED OR EXCLUDED",
        "-----------------------",
        "  Device serial → replaced with ***SCRUBBED***",
        "  IMEI          → replaced with ***SCRUBBED***",
        "  IMSI          → replaced with ***SCRUBBED***",
        "  WAN / public IP → replaced with ***SCRUBBED***",
        "  Wi-Fi SSID    → replaced with ***SCRUBBED***",
        "  License key / nonce / email → replaced with ***SCRUBBED***",
        "  Customer, user, site, address, and contact fields → replaced with ***SCRUBBED***",
        "  Local Windows path / username → not included in the bundle manifest",
        "  Router/login passwords, session tokens, API keys → replaced or excluded",
        "  Malformed JSON/JSONL records → content removed; safe marker retained",
        "  Unsupported, undecodable, or binary files → excluded and recorded",
        "",
        "APN provider settings are public; router access and SIM identity are separate.",
        "",
        "HOW TO VERIFY",
        "-------------",
        "Open BUNDLE_FILE_LIST.txt and confirm the exact file list first.",
        "Open bundle_manifest.json and inspect privacy_scrub dispositions.",
        f"Open any .jsonl file from {WINDOWS_PORTABLE_RUNTIME_DIR}/{ARCH_RUNTIME_OUTPUT_DIR}/ in a text editor.",
        "Search for your IMEI, serial, or public IP — you should find ***SCRUBBED***.",
        "If you see an unmasked value, do not share the bundle and report it.",
        "",
    ]
    return "\n".join(lines)


def _source_run_support_version_manifest(
    context: SupportBundleContext,
) -> Dict[str, Any]:
    """Project canonical runtime provenance into a shareable source-run manifest.

    Packaged builds already carry ``version_manifest.json`` and that artifact is
    copied unchanged.  A source run has no packaged-build manifest, so support
    export must create one from the same provenance owner used by launcher and
    engine release stamps.  Only shareable release identity crosses this
    boundary; local paths and the Python executable are deliberately omitted.
    """

    provenance = build_runtime_provenance(
        runtime_dir=str(context.runtime_dir),
        memory_dir=str(context.memory_dir),
        sidecar_role="support_bundle_export",
        extra={
            "profile": str(context.profile or "source_run"),
            "source": str(
                getattr(context, "source", "runtime_context") or "runtime_context"
            ),
        },
    )
    version = str(provenance.get("version", "") or "v0.0").strip()
    commit = str(provenance.get("commit", "") or "unknown").strip()
    build_date = str(provenance.get("build_date", "") or "").strip()
    return {
        "release_tag": ARCH_SW_LABEL,
        "stamp": f"{version}_source_{commit[:12]}",
        "profile": str(context.profile or "source_run"),
        "bundle_type": "source_run",
        "bundle_layout": "54GUC_source_run_v1",
        "launcher": "python_source",
        "version": version,
        "build_date": build_date,
        "release_channel": str(provenance.get("release_channel", "") or "mainline"),
        "commit": commit,
        "source_commit": commit,
        "frozen": bool(provenance.get("frozen", False)),
        "provenance_role": "support_bundle_export",
        "source": str(
            getattr(context, "source", "runtime_context") or "runtime_context"
        ),
    }


def _support_secret_byte_variants(secret: str) -> tuple[bytes, ...]:
    raw = str(secret or "")
    if len(raw) < 4:
        return ()
    variants = {raw.encode("utf-8")}
    encoded = json.dumps(raw, ensure_ascii=False)
    if len(encoded) >= 2:
        variants.add(encoded[1:-1].encode("utf-8"))
    return tuple(variants)


def _verify_history_member(
    content: bytes,
    *,
    expected_rows: int,
    secret_variants: tuple[bytes, ...],
) -> list[str]:
    """Check the owner-scrubbed History without rebuilding every JSON row."""
    errors: list[str] = []
    lines = 0
    overlap = max((len(value) for value in secret_variants), default=1) - 1
    tail = b""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content), mode="rb") as source:
            while chunk := source.read(1024 * 1024):
                lines += chunk.count(b"\n")
                search = tail + chunk if secret_variants else b""
                if secret_variants and any(value in search for value in secret_variants):
                    errors.append("known_secret_survived:history")
                    secret_variants = ()
                tail = chunk[-overlap:] if overlap else b""
    except (OSError, EOFError):
        errors.append("history_gzip_unreadable")
    if lines != expected_rows:
        errors.append("history_row_count_mismatch")
    return errors


def _verify_final_support_archive(bundle_path: Path, *, request_id: str,
        export_snapshot: FoundationSupportExportSnapshot,
        known_secret_values: Iterable[str]) -> dict[str, Any]:
    """Read-only final ZIP verification: CRC, schema, counts, privacy. No repair."""
    from arch4.foundation.support_archive import read_support_archive
    manifest, files = read_support_archive(bundle_path)
    if (manifest.get("request_id") != request_id
            or manifest.get("export_snapshot_id") != export_snapshot.snapshot_id):
        raise ValueError("support_archive_capture_identity_mismatch")
    variants = tuple({v for secret in known_secret_values
                      for v in _support_secret_byte_variants(secret)})
    history = manifest.get("intelligence_history", {})
    history_name = str(Path(WINDOWS_PORTABLE_RUNTIME_DIR, ARCH_RUNTIME_OUTPUT_DIR,
                           str(history.get("member", "")))).replace("\\", "/")
    for name, body in files.items():
        if name == history_name and history.get("included") is True:
            errors = _verify_history_member(body, expected_rows=1+int(history["evidence_row_count"])
                +int(history["checkpoint_row_count"]), secret_variants=variants)
            if errors:
                raise RuntimeError("support_history_invalid:" + ",".join(errors))
        elif any(v in body for v in variants):
            raise RuntimeError("known_secret_survived:" + name)
    return dict(schema="FoundationSupportArchiveVerification.v2",
        owner="arch4.foundation.support_bundle", request_id=request_id,
        export_snapshot_id=export_snapshot.snapshot_id,
        verified_at=dt.datetime.now(dt.timezone.utc).isoformat(), archive_name=bundle_path.name,
        archive_size_bytes=bundle_path.stat().st_size, member_count=len(files),
        zip_crc_verified=True, privacy_reopen_verified=True, known_secret_scan_verified=True)


def create_support_bundle(context: SupportBundleContext, *, probe_mode: bool = False,
        runtime_mode: str = "", modem_identity: Optional[Dict[str, Any]] = None,
        progress: Callable[[float, str], None] | None = None,
        prepare_history: Callable[[float, Path], None] | None = None) -> Path:
    """Capture originals first; finalize privacy once immediately before publication."""
    from arch4.foundation.support_archive import MANIFEST_SCHEMA
    from arch4.foundation.support_bundle import finalize_support_artifacts
    from arch4.product_intelligence_runtime import (
        NEW_HORIZON_DIAGNOSTIC_SNAPSHOT_FILENAME, build_intelligence_support_history_export)
    if type(context) is not SupportBundleContext:
        raise TypeError("support_bundle_context_required")
    def report(percent, message):
        if progress is not None:
            progress(percent, message)
    root = _support_bundle_root(context)
    root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle_path = root / f"54GUC_support_{context.profile}_{stamp}.zip"
    suffix = 1
    while bundle_path.exists():
        bundle_path = root / f"54GUC_support_{context.profile}_{stamp}_{suffix:02d}.zip"
        suffix += 1
    from arch4.product_intelligence_runtime import resolve_intelligence_history_path
    history_path = resolve_intelligence_history_path(context.memory_dir / "cgf_state")
    snapshot = capture_light_runtime_support_snapshot(context.runtime_dir, memory_dir=context.memory_dir)
    if resolve_intelligence_history_path(context.memory_dir / "cgf_state") != history_path:
        raise RuntimeError("support_history_selection_changed_during_snapshot")
    report(18., "Support snapshot collected")
    prefix = Path(WINDOWS_PORTABLE_RUNTIME_DIR, ARCH_RUNTIME_OUTPUT_DIR).as_posix()
    originals = {prefix + "/" + a.name: a.content for a in snapshot.artifacts}
    diagnostic = context.runtime_dir / NEW_HORIZON_DIAGNOSTIC_SNAPSHOT_FILENAME
    if diagnostic.is_file():
        originals[prefix + "/" + diagnostic.name] = diagnostic.read_bytes()
    session = support_session_selection(snapshot)
    if session is not None and session["live_owner_required"] and history_path.is_file():
        if not callable(prepare_history):
            raise RuntimeError("support_live_history_preparation_required")
        prepare_history(session["to_at"], history_path)
    history = build_intelligence_support_history_export(state_dir=context.memory_dir / "cgf_state",
        session=session, selected_history_path=history_path,
        progress=lambda done,total: report(20.+60.*done/max(1,total), "Collecting session History"))
    history_meta = {"included": False}
    prepared_artifacts = {}
    secrets = set(snapshot.known_secret_values)
    if history is not None:
        history_member = prefix + "/" + history.filename
        originals[history_member] = history.content
        if getattr(history, "prepared_artifact", None) is not None:
            prepared_artifacts[history_member] = history.prepared_artifact
        secrets.update(history.known_secret_values)
        history_meta = dict(session, included=True, member=history.filename,
            format="deduplicated_jsonl_gzip", complete=True,
            evidence_row_count=history.evidence_row_count,
            evidence_logical_count=history.evidence_logical_count,
            checkpoint_row_count=history.checkpoint_row_count)
        if getattr(history, "preparation", None) is not None:
            history_meta["preparation"] = dict(history.preparation)
    for name in ("version_manifest.json", "README_WINDOWS_NATIVE.txt", "open_source_manifest.json"):
        source = context.app_root / name
        if source.is_file():
            originals[name] = source.read_bytes()
    if "version_manifest.json" not in originals:
        originals["version_manifest.json"] = json.dumps(_source_run_support_version_manifest(context)).encode()
    # History bulk was cleaned by its existing writer. Mutable diagnostic tail
    # still crosses final privacy here; no intermediate private ZIP exists.
    request_id = context.request_id or "support-request:" + __import__("uuid").uuid4().hex
    from arch4.foundation.support_bundle import profile_definition_support_artifact
    from arch4.modem_live.profile_definition.registry import startup_registry
    profile_meta,profile_body=profile_definition_support_artifact(snapshot,startup_registry())
    manifest = dict(schema=MANIFEST_SCHEMA, owner="arch4.foundation.support_bundle",
        created_at=dt.datetime.now(dt.timezone.utc).isoformat(), profile=context.profile,
        source=context.source, request_id=request_id, export_snapshot_id=snapshot.snapshot_id,
        runtime_mode=runtime_mode, probe_mode=probe_mode, runtime_artifacts=dict(snapshot.manifest),
        intelligence_history=history_meta, modem_identity=dict(modem_identity or {}),portable_profile=profile_meta)
    originals["SUPPORT_BUNDLE_NOTE.json"] = json.dumps(dict(manifest, schema="FoundationLightSupportBundleNote.v2")).encode()
    originals["PRIVACY_NOTE.txt"] = _write_privacy_note().encode("utf-8")
    # Process last: the ordinary runtime artifacts first contribute their known
    # private values to the existing final privacy owner. A definition is copied
    # intact after validation, or omitted entirely; never partly corrupted.
    if profile_body is not None:originals[profile_meta['member']]=profile_body
    report(82., "Checking privacy and current diagnostics")
    files = finalize_support_artifacts(originals, manifest, known_secret_values=secrets,
        prepared_artifacts=prepared_artifacts)
    report(90., "Packing final support bundle")
    with atomic_output_path(bundle_path) as target:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, body in sorted(files.items()):
                info = zipfile.ZipInfo(name, dt.datetime.now().timetuple()[:6])
                # History is already compressed by its sole recording owner.
                # Copy it in bounded writes; deflating it again wastes CPU and
                # can enlarge small members. The final reader still checks CRC.
                info.compress_type = zipfile.ZIP_STORED if name.endswith(".gz") else zipfile.ZIP_DEFLATED
                info.file_size = len(body)
                with archive.open(info, "w") as member:
                    view = memoryview(body)
                    for offset in range(0, len(view), 64 * 1024):
                        member.write(view[offset:offset + 64 * 1024])
        verification = _verify_final_support_archive(target, request_id=request_id,
            export_snapshot=snapshot, known_secret_values=secrets)
    verification["archive_name"] = bundle_path.name
    atomic_write_json(context.runtime_dir / SUPPORT_BUNDLE_VERIFICATION_RECEIPT_FILENAME,
        verification, sort_keys=True, trailing_newline=True)
    report(100., "Support bundle verified")
    return bundle_path


def open_logs_path(path: Path) -> Path:
    if hasattr(os, "startfile"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    return path


def startup_diagnostic_report(context: WindowsLaunchContext) -> Dict[str, object]:
    """Build a compact startup diagnostic snapshot for portable validation.

    Surfaces exactly what support review needs to diagnose portable launch failures:
    writable root, redirection status, launcher mode, bootstrap/live mode,
    and bundle resource detection.

    Called after _prepare_portable_runtime so all paths are resolved.
    """
    from arch4.runtime_paths import is_fallback_root_active, resolve_writable_root
    from arch4.product_intelligence_runtime import (
        resolve_intelligence_history_path,
    )

    effective_output = Path(
        os.environ.get("ARCH_RUNTIME_OUTPUT_DIR", str(context.runtime_dir))
    )
    effective_memory = Path(
        os.environ.get("ARCH_RUNTIME_MEMORY_DIR", str(context.memory_dir))
    )

    # Writable root
    try:
        writable_root = resolve_writable_root()
    except Exception:
        writable_root = context.app_root

    # Bundle resource detection
    config_path = context.app_root / "guc_config.xml"
    manifest_path = context.app_root / "version_manifest.json"
    version_path = context.app_root / "VERSION"
    manifest_data: Dict[str, object] = {}
    if manifest_path.is_file():
        try:
            parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                manifest_data = parsed
        except Exception:
            manifest_data = {}
    version_value = ""
    if version_path.is_file():
        try:
            version_value = str(version_path.read_text(encoding="utf-8")).strip()
        except Exception:
            version_value = ""
    if not version_value:
        version_value = str(manifest_data.get("version", "") or "").strip()
    version_value = version_value or "(missing)"

    report: Dict[str, object] = {
        "profile": context.profile,
        "mode": "OBSERVE" if context.observe_only else "LIVE",
        "bootstrap": context.config.modem_backend == "",
        "modem_backend": context.config.modem_backend or "(none — bootstrap)",
        "app_root": str(context.app_root),
        "writable_root": str(writable_root),
        "fallback_active": is_fallback_root_active(),
        "runtime_dir": str(effective_output),
        "memory_dir": str(effective_memory),
        "history_path": str(
            resolve_intelligence_history_path(effective_memory / "cgf_state")
        ),
        "runtime_dir_exists": effective_output.is_dir(),
        "memory_dir_exists": effective_memory.is_dir(),
        "config_exists": config_path.is_file(),
        "config_writable": os.access(config_path.parent, os.W_OK),
        "version_file": version_value,
        "manifest_exists": manifest_path.is_file(),
        "frozen": getattr(sys, "frozen", False),
        "pid": os.getpid(),
        "cwd": os.getcwd(),
    }
    return report


def format_startup_diagnostic(report: Dict[str, object]) -> str:
    """Format startup diagnostic report as compact human-readable text."""
    lines = [
        "── Startup Diagnostics ──",
        f"  Profile:       {report.get('profile', '?')} [{report.get('mode', '?')}]",
        f"  Bootstrap:     {report.get('bootstrap', '?')}",
        f"  Backend:       {report.get('modem_backend', '?')}",
        f"  App root:      {report.get('app_root', '?')}",
        f"  Writable root: {report.get('writable_root', '?')}",
        f"  History:       {report.get('history_path', '?')}",
    ]
    if report.get("fallback_active"):
        lines.append("  ⚠ FALLBACK:    Runtime redirected away from app root")
    lines.extend(
        [
            f"  Runtime dir:   {report.get('runtime_dir', '?')} (exists={report.get('runtime_dir_exists', '?')})",
            f"  Memory dir:    {report.get('memory_dir', '?')} (exists={report.get('memory_dir_exists', '?')})",
            f"  Config:        exists={report.get('config_exists', '?')}, writable={report.get('config_writable', '?')}",
            f"  Version:       {report.get('version_file', '?')}",
            f"  Manifest:      exists={report.get('manifest_exists', '?')}",
            f"  Frozen:        {report.get('frozen', '?')}",
        ]
    )
    lines.append("────────────────────────")
    return "\n".join(lines)


def _prepare_portable_runtime(
    context: WindowsLaunchContext,
    *,
    create_runtime_session: bool = False,
) -> WindowsLaunchContext:
    writable_root = verify_writable_root(context.app_root)

    # If fallback root differs from app_root, redirect runtime/memory dirs.
    if writable_root != context.app_root.resolve():
        redirected_root = _portable_runtime_root_for_app(writable_root)
        redirected_runtime = (redirected_root / ARCH_RUNTIME_OUTPUT_DIR).resolve()
        redirected_memory = (redirected_root / ARCH_RUNTIME_MEMORY_DIR).resolve()
        _log.warning(
            "Writable root fallback active — redirecting runtime to %s, memory to %s",
            redirected_runtime,
            redirected_memory,
        )
        # Patch environment so the runtime resolver uses the redirected root.
        os.environ["ARCH_RUNTIME_OUTPUT_DIR"] = str(redirected_runtime)
        os.environ["ARCH_RUNTIME_MEMORY_DIR"] = str(redirected_memory)
        context = _context_with_runtime_paths(
            context,
            runtime_dir=redirected_runtime,
            memory_dir=redirected_memory,
        )
    context = _configure_runtime_environment(context)
    if create_runtime_session:
        session_runtime = _build_timestamped_runtime_dir(context.runtime_dir)
        try:
            session_runtime.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            session_runtime = _build_timestamped_runtime_dir(context.runtime_dir)
            session_runtime.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise StartupError(
                f"Cannot create runtime session directory: {session_runtime}",
                detail=str(exc),
                reason="mkdir_runtime_session",
            )
        context = _context_with_runtime_paths(
            context,
            runtime_dir=session_runtime,
            memory_dir=context.memory_dir,
        )
        os.environ["ARCH_RUNTIME_OUTPUT_DIR"] = str(session_runtime)
    config_path = _portable_config_path(context)
    valid, error = validate_config_file(config_path)
    if not valid:
        raise StartupError(
            f"External settings validation failed: {config_path.name}",
            detail=str(error or ""),
            reason="external_settings_invalid",
        )

    return context


def _create_validation_pack(context: "WindowsLaunchContext") -> tuple[Path, bool]:
    """Build a timestamped validation folder and return (folder_path, ok).

    Collects:
      - portable_diagnostic.json / .txt
      - portable_validation.txt
      - validation_manifest.json  (includes fallback status and reason)

    On dead/read-only primary output root, falls back to a temp directory.
    The manifest and text output always record whether fallback happened,
    where artifacts were actually written, and why fallback was needed.

    Never raises — failures are captured in the manifest.
    """
    import datetime as _dt

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    app_root = Path(getattr(context, "app_root", Path.cwd()))
    runtime_dir_raw = getattr(context, "runtime_dir", None)
    if runtime_dir_raw:
        runtime_dir = Path(runtime_dir_raw).resolve()
        if runtime_dir.name == ARCH_RUNTIME_OUTPUT_DIR:
            primary_root = runtime_dir.parent
        else:
            primary_root = runtime_dir
    else:
        portable_root = _portable_runtime_root_for_app(app_root)
        runtime_dir = portable_root / ARCH_RUNTIME_OUTPUT_DIR
        primary_root = portable_root
    memory_dir_raw = getattr(context, "memory_dir", None)
    if memory_dir_raw:
        memory_dir = Path(memory_dir_raw).resolve()
    else:
        memory_dir = runtime_dir.parent / ARCH_RUNTIME_MEMORY_DIR
    errors: list[str] = []
    ok = True

    # Track fallback state so it can be surfaced in manifest and stdout.
    fallback_active = False
    fallback_reason = ""

    try:
        pack_dir = primary_root / f"validation_{stamp}"
        pack_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        fallback_reason = str(exc)
        fallback_active = True
        errors.append(f"pack_dir_primary_failed: {exc}")
        pack_dir = Path(tempfile.mkdtemp(prefix=f"guc_validation_{stamp}_"))

    try:
        from arch4.portable_diagnostic_dump import write_diagnostic_dump

        write_diagnostic_dump(app_root, pack_dir)
    except Exception as exc:
        errors.append(f"diagnostic_dump: {exc}")

    validation_ok = True
    try:
        from arch4.portable_launch_validator import run_validation, format_report

        report = run_validation(
            app_root,
            runtime_dir,
            memory_dir=memory_dir,
            expect_frozen=bool(getattr(sys, "frozen", False)),
        )
        txt = format_report(report)
        (pack_dir / "portable_validation.txt").write_text(txt, encoding="utf-8")
        validation_ok = report.ok
        ok = ok and validation_ok
    except Exception as exc:
        errors.append(f"validation: {exc}")
        validation_ok = False
        ok = False

    intelligence_package_ok = True
    intelligence_package: dict[str, object] = {}
    try:
        from arch4.product_intelligence_runtime import (
            validate_intelligence_runtime_package,
        )

        intelligence_package = validate_intelligence_runtime_package(
            history_path=pack_dir / "new_horizon_validation.sqlite3",
        )
    except Exception as exc:
        errors.append(f"new_horizon_package: {exc}")
        intelligence_package_ok = False
        ok = False

    try:
        import json as _json

        manifest = {
            "created_at": stamp,
            "app_root": str(app_root),
            "profile": getattr(context, "profile", ""),
            "validation_passed": validation_ok,
            "new_horizon_package_passed": intelligence_package_ok,
            "new_horizon_package": intelligence_package,
            # Fallback visibility — support review can detect fallback from machine-readable output.
            "pack_dir": str(pack_dir),
            "primary_root": str(primary_root),
            "fallback_active": fallback_active,
            "fallback_reason": fallback_reason,
            "errors": errors,
            "files": [p.name for p in sorted(pack_dir.iterdir()) if p.is_file()],
        }
        (pack_dir / "validation_manifest.json").write_text(
            _json.dumps(manifest, indent=2, default=str), encoding="utf-8"
        )
    except Exception as exc:
        errors.append(f"manifest: {exc}")

    return pack_dir, ok


def _run_portable_validate(context: "WindowsLaunchContext") -> int:
    """Run portable-state validation and write a timestamped artifact pack.

    Produces validation_<timestamp>/ folder. On dead/read-only primary root,
    falls back to a temp directory and reports this clearly in stdout and
    in the manifest.

    Exit 0 = all automated checks passed. Exit 1 = failures.
    """
    _console_print("Running portable validation...")
    pack_dir, ok = _create_validation_pack(context)

    # Surface fallback clearly so support review sees it immediately in terminal output.
    try:
        import json as _json

        manifest = _json.loads((pack_dir / "validation_manifest.json").read_text())
        if manifest.get("fallback_active"):
            _console_print("  *** FALLBACK ACTIVE — primary root was not writable ***")
            _console_print(f"      Primary root: {manifest.get('primary_root')}")
            _console_print(f"      Reason:       {manifest.get('fallback_reason')}")
            _console_print(f"      Artifacts at: {pack_dir}")
        else:
            _console_print(f"  Artifact pack: {pack_dir}")
    except Exception:
        _console_print(f"  Artifact pack: {pack_dir}")

    for f in sorted(pack_dir.iterdir()):
        _console_print(f"    {f.name}")
    val_txt = pack_dir / "portable_validation.txt"
    if val_txt.exists():
        _console_print(val_txt.read_text(encoding="utf-8"), end="")
    return 0 if ok else 1


def _publish_next_start_settings_after_gui_exit(
    context: "WindowsLaunchContext",
) -> None:
    """Ask Foundation to publish the small same-modem next-start handoff."""

    current_state_path = Path(context.runtime_dir) / "current_state.json"
    if not current_state_path.is_file():
        return
    try:
        payload = json.loads(current_state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log.debug("Settings handoff skipped: current state unreadable: %s", exc)
        return
    if not isinstance(payload, Mapping):
        return
    from arch4.foundation.settings_handoff import (
        publish_next_start_settings_handoff,
        settings_handoff_identity_from_current_state,
    )

    identity = settings_handoff_identity_from_current_state(payload)
    if not str(identity.get("profile_id") or "").strip():
        return
    if not (
        str(identity.get("serial") or "").strip()
        or str(identity.get("imei") or "").strip()
    ):
        return
    result = publish_next_start_settings_handoff(
        context.runtime_dir,
        confirmed_identity=identity,
        runtime_memory_dir=context.memory_dir,
    )
    if result is not None:
        _log.info("Settings handoff published: %s", result.handoff_path)


def _run_windows_action(
    args: argparse.Namespace,
    context: WindowsLaunchContext,
    *,
    application_lease: ApplicationLease | None,
) -> int:
    if bool(args.print_launch_plan):
        return _print_launch_plan(context)
    if bool(args.open_logs):
        try:
            latest_runtime_dir = _latest_runtime_output_dir(
                context.runtime_dir
            )
            target = open_logs_path(
                latest_runtime_dir
                if latest_runtime_dir.exists()
                else _runtime_root(context)
            )
            _console_print(f"Opened logs: {target}")
            return 0
        except Exception as exc:
            _console_print(
                f"STARTUP FAILED [open_logs]: Cannot open logs folder: {exc}"
            )
            return 1
    _log_release_stamp(context)
    _write_startup_trace(context, "context_built")
    if application_lease is not None:
        _write_startup_trace(
            context,
            "application_lease_acquired",
            lease_id=application_lease.identity.lease_id,
            lease_generation=application_lease.identity.generation,
        )
    startup_notice = (
        _StartupNotice.show(profile=context.profile)
        if application_lease is not None
        else _StartupNotice()
    )
    try:
        _write_startup_trace(context, "prepare_runtime_begin")
        precreated_source_session = bool(
            getattr(args, "source_run_precreated_session", False)
        )
        if precreated_source_session:
            _validate_precreated_source_runtime_session(context)
        create_runtime_session = not any(
            (
                bool(args.open_logs),
                bool(args.create_support_bundle),
                bool(getattr(args, "portable_validate", False)),
                precreated_source_session,
            )
        )
        context = _prepare_portable_runtime(
            context,
            create_runtime_session=create_runtime_session,
        )
        _write_startup_trace(context, "prepare_runtime_done")
        startup_notice.pulse("Opening dashboard, please wait.")
    except StartupError as exc:
        _write_startup_trace(
            context,
            "prepare_runtime_failed",
            error=str(exc),
            reason=str(exc.reason or ""),
        )
        startup_notice.close()
        reason_tag = f" [{exc.reason}]" if exc.reason else ""
        _console_print(f"STARTUP FAILED{reason_tag}: {exc}")
        if exc.detail:
            _console_print(f"  -> {exc.detail}")
        return 1
    except KeyboardInterrupt:
        startup_notice.close()
        _console_print(f"\n{ARCH_SW_LABEL} startup interrupted.")
        return 130
    if bool(args.create_support_bundle):
        startup_notice.close()
        try:
            latest_runtime_dir = _latest_runtime_output_dir(context.runtime_dir)
            if latest_runtime_dir != context.runtime_dir:
                context = _context_with_runtime_paths(
                    context,
                    runtime_dir=latest_runtime_dir,
                    memory_dir=context.memory_dir,
                )
            support_context = support_bundle_context_from_windows_launch(context)
            from arch4.foundation.operational_service_contracts import SupportRequest
            from arch4.foundation.operational_root import FoundationOperationalRoot
            from arch4.interfaces import LUC_VERSION

            operational_root = FoundationOperationalRoot(
                app_root=support_context.app_root,
                storage_root=support_context.runtime_dir,
                support_context=support_context,
                current_version=LUC_VERSION,
            )
            bundle = operational_root.ports.support.create(
                SupportRequest(
                    profile=support_context.profile,
                    runtime_mode=("observe" if context.observe_only else "live"),
                )
            )
            target = open_logs_path(_support_bundle_root(support_context))
            _console_print(f"Support bundle created: {bundle.path}")
            _console_print(f"Opened folder: {target}")
            return 0
        except Exception as exc:
            _console_print(
                f"STARTUP FAILED [support_bundle]: Cannot create support bundle: {exc}"
            )
            return 1
    if bool(getattr(args, "portable_validate", False)):
        startup_notice.close()
        return _run_portable_validate(context)
    if bool(getattr(sys, "frozen", False)):
        try:
            from arch4.interfaces import LUC_VERSION
            from arch4.product_intelligence_runtime import (
                migrate_v68_portable_intelligence_histories,
            )

            migration = migrate_v68_portable_intelligence_histories(
                context.app_root,
                version=LUC_VERSION,
            )
            if migration.get("reason") != "release_not_applicable":
                _log.info(
                    "V6.8 portable history migration complete: stores=%s applied=%s",
                    migration.get("discovered_stores", 0),
                    migration.get("applied", False),
                )
        except Exception as exc:
            startup_notice.close()
            _write_startup_trace(
                context,
                "portable_history_migration_failed",
                error=repr(exc),
            )
            _log.exception("V6.8 portable history migration failed")
            _console_print(
                "STARTUP FAILED [portable_history_migration]: " + str(exc)
            )
            return 1
    mode_label = "OBSERVE" if context.observe_only else "LIVE"
    _console_print(
        f"{ARCH_SW_LABEL} Windows native launcher - {context.profile.upper()} [{mode_label}]"
    )
    _console_print(f"Using config: {context.config_source}")

    if (
        bool(getattr(args, "live", False))
        and not bool(getattr(sys, "frozen", False))
        and not bool(context.observe_only)
        and context.profile not in {"bootstrap", "internal"}
    ):
        os.environ["ARCH_WINDOWS_SOURCE_RUN_AUTOCONNECT"] = "1"
        os.environ["ARCH_WINDOWS_AUTOCONNECT_PROFILE"] = str(
            context.config.modem_backend or ""
        )

    # Compact startup diagnostics for portable validation.
    try:
        _write_startup_trace(context, "startup_diagnostic_begin")
        diag = startup_diagnostic_report(context)
        _console_print(format_startup_diagnostic(diag))
        _write_startup_trace(
            context, "startup_diagnostic_done", history_path=diag["history_path"]
        )
    except Exception as exc:
        _write_startup_trace(context, "startup_diagnostic_failed", error=repr(exc))
        _log.debug("Startup diagnostics failed: %s", exc)

    try:
        _write_startup_trace(context, "gui_import_begin")
        launch = importlib.import_module("arch4.ngui.application").launch
        _write_startup_trace(context, "gui_import_done")
        startup_notice.close()
        _write_startup_trace(context, "gui_launch_begin")
        if application_lease is None:
            raise RuntimeError("normal_gui_launch_requires_application_lease")
        application_lease.transition(
            ApplicationLeaseState.PROCESS_RUNNING,
            phase="gui_launch_begin",
        )
        exit_code = int(
            launch(
                launch_context=context,
                config=context.config,
                observe_only=context.observe_only,
                windows_mc888=context.windows_mc888,
            )
        )
        application_lease.transition(
            ApplicationLeaseState.STOPPING,
            phase="gui_returned",
            exit_code=exit_code,
        )
        if exit_code == 0:
            try:
                _publish_next_start_settings_after_gui_exit(context)
            except Exception as exc:
                _write_startup_trace(
                    context,
                    "settings_handoff_publish_failed",
                    error=repr(exc),
                )
                _log.exception("Settings handoff publication failed")
        else:
            _write_startup_trace(
                context,
                "settings_handoff_skipped_unclean_shutdown",
                exit_code=exit_code,
            )
        _write_startup_trace(context, "gui_launch_done", exit_code=exit_code)
        return exit_code
    except KeyboardInterrupt:
        startup_notice.close()
        _console_print(f"\n{ARCH_SW_LABEL} interrupted.")
        return 130
    except Exception as exc:
        startup_notice.close()
        _write_startup_trace(context, "gui_launch_failed", error=repr(exc))
        _log.exception("Windows native launch failed")
        _console_print(f"STARTUP FAILED [launch]: {exc}")
        return 1


def main(argv: Iterable[str] | None = None) -> int:
    _ensure_console_streams()
    multiprocessing.freeze_support()
    _configure_windows_console_encoding()
    parser = _build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.profile_tools is not None:
        # Reuse the qualified offline tool before any runtime/settings/lease work.
        from tools.modem_profile_exchange.export_portable import main as profile_tools_main
        return profile_tools_main(args.profile_tools)
    level = logging.DEBUG if bool(args.debug) else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    app_root = resolve_app_root()
    try:
        context = build_launch_context(
            profile_name=str(args.profile),
            app_root=app_root,
            config_path=Path(str(args.config)).resolve()
            if str(args.config or "").strip()
            else None,
            observe_only=_resolve_observe_flag(args),
        )
    except StartupError as exc:
        reason_tag = f" [{exc.reason}]" if exc.reason else ""
        _console_print(f"STARTUP FAILED{reason_tag}: {exc}")
        if exc.detail:
            _console_print(f"  -> {exc.detail}")
        return 1
    try:
        if bool(args.source_run_precreated_session):
            if os.name != "nt" or getattr(sys, "frozen", False):
                raise StartupError(
                    "Precreated sessions require an unfrozen Windows source launch.",
                    reason="source_runtime_session",
                )
            context = replace(context, source_run_precreated_session=True)
        context = _context_with_explicit_runtime_env_override(context)
    except StartupError as exc:
        reason_tag = f" [{exc.reason}]" if exc.reason else ""
        _console_print(f"STARTUP FAILED{reason_tag}: {exc}")
        if exc.detail:
            _console_print(f"  -> {exc.detail}")
        return 1
    application_lease_required = _should_acquire_single_instance_lock(args)
    if not application_lease_required:
        return _run_windows_action(
            args,
            context,
            application_lease=None,
        )
    lease = _build_application_lease(context)
    if not lease.acquire():
        _console_print(
            f"{ARCH_SW_LABEL} is already running for this Windows user."
        )
        _console_print(f"  Lease lock: {lease.lock_path}")
        return 0
    exit_code = 1
    try:
        identity = lease.identity
        os.environ["ARCH_APPLICATION_LEASE_ID"] = identity.lease_id
        os.environ["ARCH_APPLICATION_LEASE_GENERATION"] = str(
            identity.generation
        )
        os.environ["ARCH_APPLICATION_LEASE_PROCESS_ID"] = str(
            identity.process_id
        )
        os.environ["ARCH_APPLICATION_LEASE_PROCESS_BIRTH_MS"] = str(
            identity.process_started_at_unix_ms
        )
        os.environ["ARCH_APPLICATION_LEASE_STAGE_DIR"] = identity.stage_dir
        os.environ["ARCH_APPLICATION_LEASE_PROFILE"] = identity.profile
        os.environ["ARCH_APPLICATION_LEASE_EXECUTABLE_PATH"] = (
            identity.executable_path
        )
        lease.transition(
            ApplicationLeaseState.STARTING,
            phase="launcher_claimed",
        )
        exit_code = _run_windows_action(
            args,
            context,
            application_lease=lease,
        )
        return exit_code
    finally:
        try:
            lease.release(
                failed=exit_code != 0,
                detail={"exit_code": int(exit_code)},
            )
        finally:
            for key in (
                "ARCH_APPLICATION_LEASE_ID",
                "ARCH_APPLICATION_LEASE_GENERATION",
                "ARCH_APPLICATION_LEASE_PROCESS_ID",
                "ARCH_APPLICATION_LEASE_PROCESS_BIRTH_MS",
                "ARCH_APPLICATION_LEASE_STAGE_DIR",
                "ARCH_APPLICATION_LEASE_PROFILE",
                "ARCH_APPLICATION_LEASE_EXECUTABLE_PATH",
            ):
                os.environ.pop(key, None)


if __name__ == "__main__":
    raise SystemExit(main())
