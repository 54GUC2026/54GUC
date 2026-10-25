"""Pure child-process preparation of bounded NGUI chart continuity."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from arch4.atomic_io import atomic_output_path, atomic_write_text
from arch4.evidence_scope import EvidenceScope
from arch4.external_memory_evidence_scope import (
    validated_external_projection_scope,
)
from arch4.historical_artifact_reader import (
    FAMILY_PRODUCTION,
    HistoricalArtifactReader,
    HistoricalReadCooperation,
)
from arch4.historical_continuity_store import (
    CATALOG_FILENAME,
    CHART_HISTORY_FILENAME,
)


SCHEMA = "HistoricalContinuityCatalog.v1"
OWNER = "54guc.intelligence.historical_evidence"
MAX_MATERIALIZED_ROWS = 1_000_000

ProgressCallback = Callable[[float, str, str], None]


def _same_exact_modem(source: EvidenceScope, target: EvidenceScope) -> bool:
    return bool(
        source.identity_code == target.identity_code
        and source.identity_status == target.identity_status
        and source.identity_catalog_version == target.identity_catalog_version
        and source.identity_catalog_checksum == target.identity_catalog_checksum
    )


def _reader_for_directory_source(
    source_path: Path,
    *,
    cooperation: HistoricalReadCooperation | None,
) -> HistoricalArtifactReader:
    source = source_path.resolve()
    if source.name == "guc_output" or source.name.startswith("guc_output_"):
        return HistoricalArtifactReader(source, cooperation=cooperation)
    if source.name == "portable_runtime":
        runtime = source
    elif (source / "portable_runtime").is_dir():
        runtime = source / "portable_runtime"
    else:
        return HistoricalArtifactReader(source, cooperation=cooperation)
    current = runtime / "guc_output"
    outputs = tuple(
        path for path in sorted(runtime.glob("guc_output_*")) if path.is_dir()
    )
    return HistoricalArtifactReader(
        current,
        extra_roots=outputs,
        cooperation=cooperation,
    )


def materialize_historical_chart_artifact(
    artifact_dir: str | Path,
    pack: Mapping[str, Any],
    *,
    max_age_days: int,
    transaction_id: str,
    now_ts: float | None = None,
    cooperation: HistoricalReadCooperation | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Prepare chart history without publishing a consumer-visible path."""

    target_scope = validated_external_projection_scope(
        dict(pack.get("target_evidence_scope") or {})
    )
    source_paths: list[Path] = []
    seen_paths: set[str] = set()
    for fact in pack.get("facts", []):
        if not isinstance(fact, Mapping) or fact.get("promotion_status") != "approved":
            continue
        source = str(
            (fact.get("provenance") or {}).get("source_path")
            or fact.get("source_path")
            or ""
        ).strip()
        if not source:
            continue
        resolved = Path(source).expanduser().resolve()
        if str(resolved) not in seen_paths:
            seen_paths.add(str(resolved))
            source_paths.append(resolved)

    root = Path(artifact_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    chart_path = root / CHART_HISTORY_FILENAME
    catalog_path = root / CATALOG_FILENAME
    cutoff = float(time.time() if now_ts is None else now_ts) - float(
        max(1, int(max_age_days)) * 86400
    )
    admitted = 0
    scanned = 0
    dedupe: set[str] = set()
    source_scope_ids: set[str] = set()
    with atomic_output_path(chart_path) as temporary_path:
        with gzip.open(temporary_path, "wt", encoding="utf-8") as handle:
            for source_index, source_path in enumerate(source_paths):
                if source_path.is_file() and source_path.suffix.lower() == ".zip":
                    reader = HistoricalArtifactReader(
                        source_path.parent,
                        cooperation=cooperation,
                    )
                    records = reader.iter_zip_records(
                        source_path,
                        families=(FAMILY_PRODUCTION,),
                        since_ts=cutoff,
                    )
                elif source_path.is_file():
                    reader = HistoricalArtifactReader(
                        source_path.parent,
                        cooperation=cooperation,
                    )
                    records = reader.iter_paths(
                        (source_path,),
                        families=(FAMILY_PRODUCTION,),
                        since_ts=cutoff,
                    )
                else:
                    reader = _reader_for_directory_source(
                        source_path,
                        cooperation=cooperation,
                    )
                    records = reader.iter_records(
                        families=(FAMILY_PRODUCTION,),
                        since_ts=cutoff,
                    )
                for record in records:
                    scanned += 1
                    if record.dedupe_key in dedupe:
                        continue
                    try:
                        source_scope = EvidenceScope.from_mapping(
                            dict(record.payload.get("evidence_scope") or {})
                        )
                    except (TypeError, ValueError):
                        continue
                    if not _same_exact_modem(source_scope, target_scope):
                        continue
                    if (
                        source_scope.provider_plmn
                        and target_scope.provider_plmn
                        and source_scope.provider_plmn != target_scope.provider_plmn
                    ):
                        continue
                    dedupe.add(record.dedupe_key)
                    source_scope_ids.add(source_scope.scope_id)
                    handle.write(
                        json.dumps(
                            dict(record.payload),
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    admitted += 1
                    if admitted >= MAX_MATERIALIZED_ROWS:
                        break
                if progress_callback is not None:
                    progress_callback(
                        82.0
                        + (
                            15.0
                            * float(source_index + 1)
                            / float(max(1, len(source_paths)))
                        ),
                        "chart_history_materialization",
                        f"completed source {source_index + 1}/{len(source_paths)}; "
                        f"admitted {admitted} rows",
                    )
                if admitted >= MAX_MATERIALIZED_ROWS:
                    break

    catalog = {
        "schema": SCHEMA,
        "owner": OWNER,
        "transaction_id": transaction_id,
        "generated_at_ts": time.time(),
        "max_age_days": max(1, int(max_age_days)),
        "cutoff_ts": cutoff,
        "chart_history_artifact": CHART_HISTORY_FILENAME,
        "scanned_rows": scanned,
        "admitted_rows": admitted,
        "source_count": len(source_paths),
        "source_evidence_scope_ids": sorted(source_scope_ids),
        "target_evidence_scope": target_scope.as_dict(),
        "historical_only": True,
        "live_truth_override": False,
        "history_available": admitted > 0,
    }
    atomic_write_text(
        catalog_path,
        json.dumps(catalog, indent=2, sort_keys=True),
    )
    return {
        **catalog,
        "catalog_published": True,
        "reason": (
            "bounded production-history artifact prepared"
            if admitted > 0
            else "admitted sources contain no retained production telemetry"
        ),
    }


__all__ = [
    "MAX_MATERIALIZED_ROWS",
    "OWNER",
    "SCHEMA",
    "materialize_historical_chart_artifact",
]
