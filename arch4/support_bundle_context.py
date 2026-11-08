"""Typed support-export context shared by packaged and source-run launches."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SupportBundleContext:
    profile: str
    app_root: Path
    runtime_dir: Path
    memory_dir: Path
    source: str
    request_id: str = ""

    @classmethod
    def from_runtime_paths(
        cls,
        *,
        app_root: Path,
        runtime_dir: Path,
        memory_dir: Path,
        profile: str = "source_run",
        source: str = "ngui_source_runtime",
        request_id: str = "",
    ) -> "SupportBundleContext":
        return cls(
            profile=str(profile or "source_run").strip(),
            app_root=Path(app_root).resolve(),
            runtime_dir=Path(runtime_dir).resolve(),
            memory_dir=Path(memory_dir).resolve(),
            source=str(source or "ngui_source_runtime").strip(),
            request_id=str(request_id or "").strip(),
        )


__all__ = ["SupportBundleContext"]
