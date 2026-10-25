"""Declared construction boundary for the New Horizon product runtime."""

from __future__ import annotations

from importlib import import_module
import os
from pathlib import Path
from typing import Any, Callable, Mapping


_RUNTIME_MODULE = "arch4.new_horizon.runtime"
_HISTORY_MODULE = "arch4.new_horizon.history"
_SITE_REPORT_PROJECTION_MODULE = "arch4.new_horizon.site_report_projection"
NEW_HORIZON_DIAGNOSTIC_SNAPSHOT_FILENAME = (
    "new_horizon_diagnostic_snapshot.json"
)
PORTABLE_HISTORY_MIGRATION_RELEASE = "CGF_v6.8"


def _runtime_type() -> type:
    """Return the one packaged New Horizon runtime type."""

    try:
        module = import_module(_RUNTIME_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name in {"arch4.new_horizon", _RUNTIME_MODULE}:
            raise RuntimeError(
                "product_intelligence_package_required"
            ) from exc
        raise
    runtime_type = getattr(module, "AdvisoryRuntime", None)
    if not isinstance(runtime_type, type):
        raise TypeError("product_intelligence_runtime_type_required")
    return runtime_type


def _history_module() -> Any:
    try:
        return import_module(_HISTORY_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name in {"arch4.new_horizon", _HISTORY_MODULE}:
            raise RuntimeError("product_intelligence_package_required") from exc
        raise


def _site_report_projection_module() -> Any:
    try:
        return import_module(_SITE_REPORT_PROJECTION_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name in {
            "arch4.new_horizon",
            _SITE_REPORT_PROJECTION_MODULE,
        }:
            raise RuntimeError("product_intelligence_package_required") from exc
        raise


def resolve_intelligence_history_path(state_dir: str | Path) -> Path:
    """Resolve History through the sole Product Intelligence boundary."""

    resolver = getattr(_history_module(), "resolve_history_path", None)
    if not callable(resolver):
        raise TypeError("product_intelligence_history_resolver_required")
    return Path(resolver(state_dir)).resolve()


def read_confirmed_device_associations(state_dir: str | Path) -> tuple[dict, ...]:
    """Read-only Product boundary; does not construct Intelligence or History."""
    reader = getattr(_history_module(), "read_confirmed_device_associations", None)
    if not callable(reader):
        raise TypeError("product_intelligence_device_association_reader_required")
    return reader(resolve_intelligence_history_path(state_dir))


def build_intelligence_site_report_history(
    *,
    state_dir: str | Path,
    hours: float,
    now: float | None = None,
) -> Any:
    """Freeze optional Intelligence history through its sole product adapter."""

    builder = getattr(
        _site_report_projection_module(),
        "build_site_report_history_projection",
        None,
    )
    if not callable(builder):
        raise TypeError("product_intelligence_site_report_projector_required")
    return builder(
        resolve_intelligence_history_path(state_dir),
        hours=float(hours),
        now=now,
    )


def build_intelligence_support_history_export(
    *,
    state_dir: str | Path,
    session: Mapping[str, Any] | None = None,
    progress: Callable[[int, int], None] | None = None,
    selected_history_path: str | Path | None = None,
) -> Any | None:
    """Export only the selected session; never fall back to the whole store."""

    module = _history_module()
    # A support request resolves its physical store before the writer barrier.
    # Keep that selection when a device handover occurs during the export.
    path = (Path(selected_history_path).resolve() if selected_history_path is not None
            else resolve_intelligence_history_path(state_dir))
    if not path.is_file():
        return None
    if not isinstance(session, Mapping) or session.get("scope") != "session":
        raise ValueError("support_history_session_identity_unavailable")
    since, until = session["from_at"], session["to_at"]
    if float(until) - float(since) > 86400.0:
        raise ValueError("support_history_session_exceeds_24_hours")
    builder = getattr(module, "capture_support_history_export", None)
    if not callable(builder):
        raise TypeError("product_intelligence_support_history_exporter_required")
    return builder(path, since=since, until=until, progress=progress, public=True)


def portable_intelligence_history_path(stage_dir: str | Path) -> tuple[str, Path]:
    """Return the explicit connected path or ordinary portable path.

    The portable process receives one path channel.  A governed connected
    launch sets it for that child process.  Without it, sibling portable
    releases use their normal version-neutral store.
    """

    module = _history_module()
    resolver = getattr(module, "portable_history_path", None)
    env_name = str(getattr(module, "NEW_HORIZON_HISTORY_PATH_ENV", "") or "")
    if not callable(resolver) or not env_name:
        raise TypeError("product_intelligence_portable_history_boundary_required")
    selected = str(os.environ.get(env_name, "") or "").strip()
    if not selected:
        return env_name, Path(resolver(stage_dir)).resolve()
    path = Path(selected).expanduser()
    if (
        not path.is_absolute()
        or os.path.normcase(path.name) != os.path.normcase(module.NEW_HORIZON_HISTORY_FILENAME)
        or os.path.normcase(path.parent.name) != os.path.normcase(module.NEW_HORIZON_HISTORY_DIRNAME)
    ):
        raise ValueError("product_intelligence_connected_history_path_invalid")
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(
            "product_intelligence_connected_history_store_missing:" + str(path)
        )
    return env_name, path


def migrate_v68_portable_intelligence_histories(
    stage_dir: str | Path,
    *,
    version: str,
) -> dict[str, Any]:
    """Run the named V6.8-only history rollout for this portable family."""

    if str(version or "").strip() != PORTABLE_HISTORY_MIGRATION_RELEASE:
        return {
            "schema": "NewHorizonLearningContextBatchMigration.v1",
            "applied": False,
            "reason": "release_not_applicable",
            "discovered_stores": 0,
            "stores": [],
        }
    module = _history_module()
    runner = getattr(module, "migrate_learning_context_histories", None)
    if not callable(runner):
        raise TypeError("product_intelligence_history_batch_migrator_required")
    root = Path(stage_dir).expanduser().resolve().parent
    return dict(runner((root,), apply=True))


def build_intelligence_runtime(
    product: Any,
    *,
    history_path: str | Path,
    diagnostic_path: str | Path | None = None,
) -> Any:
    """Construct the required customer or fail with explicit composition truth."""

    from arch4.foundation.settings_handoff import (
        confirmed_history_identity_links, settings_handoff_identity_from_observation,
    )

    cached_link_identity = None
    cached_links = ()

    def identity_evidence(frame):
        nonlocal cached_link_identity, cached_links
        current = settings_handoff_identity_from_observation(frame)
        # Cache only archive work, not the current accepted modem identity.
        # A new serial/IMEI must revalidate links even if firmware is unchanged.
        if current != cached_link_identity:
            cached_links = confirmed_history_identity_links(
                Path(history_path).resolve().parent.parent / "_portable_next_start_handoff",
                confirmed_identity=current,
            )
            cached_link_identity = dict(current)
        return {"current": current, "links": cached_links}

    return _runtime_type()(
        product.intelligence_matrix().customer_port(),
        history_path=history_path,
        diagnostic_path=diagnostic_path,
        identity_evidence=identity_evidence,
    )


class _PackageValidationPort:
    """Construction-only Matrix-port shape used by portable validation."""

    def attach(self, _root: Any) -> None:
        raise RuntimeError("package_validation_runtime_must_not_start")

    def detach(self) -> None:
        return None

    def read_inputs(self, **_kwargs: Any) -> Any:
        raise RuntimeError("package_validation_runtime_must_not_read")

    def publish_presentation(self, _frame: Any) -> None:
        return None

    def publish_proposal(self, _proposal: Any) -> None:
        return None

    def publish_health(self, _health: Any) -> None:
        return None

    def publish_signal_baselines(self, _snapshot: Any) -> None:
        return None


def validate_intelligence_runtime_package(
    *,
    history_path: str | Path,
) -> dict[str, Any]:
    """Prove the packaged runtime imports, constructs and stops cleanly."""

    runtime_type = _runtime_type()
    runtime = runtime_type(
        _PackageValidationPort(),
        history_path=history_path,
    )
    status = runtime.status()
    if status.get("running") is not False:
        raise RuntimeError("product_intelligence_validation_started_worker")
    if runtime.stop(timeout_s=0.0) is not True:
        raise RuntimeError("product_intelligence_validation_stop_failed")
    return {
        "module": _RUNTIME_MODULE,
        "runtime_type": runtime_type.__name__,
        "constructed": True,
        "zero_survivors": True,
    }


__all__ = (
    "build_intelligence_site_report_history",
    "build_intelligence_runtime",
    "NEW_HORIZON_DIAGNOSTIC_SNAPSHOT_FILENAME",
    "migrate_v68_portable_intelligence_histories",
    "PORTABLE_HISTORY_MIGRATION_RELEASE",
    "portable_intelligence_history_path",
    "resolve_intelligence_history_path",
    "read_confirmed_device_associations",
    "validate_intelligence_runtime_package",
)
