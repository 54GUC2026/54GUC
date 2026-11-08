from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .snapshot_store import JsonSnapshotStore
from .event_journal import JsonlEventJournal


class CalibrationStore:
    """
    Calibration persistence boundary.

    Phase 1 keeps the authority snapshot on the existing
    `calibration_authority.json` path and defines the future CM file layout.
    """

    def __init__(self, root_dir: Path | str):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.authority_snapshot = JsonSnapshotStore(self.root_dir, "calibration_authority.json")
        self.cm_samples_journal = JsonlEventJournal(self.root_dir, "cm_samples.jsonl")
        self.cm_profiles_snapshot = JsonSnapshotStore(self.root_dir, "cm_profiles.json")
        self.cm_meta_snapshot = JsonSnapshotStore(self.root_dir, "cm_meta.json")

    @property
    def authority_path(self) -> Path:
        return self.authority_snapshot.path

    def read_authority_state(self) -> Optional[Dict[str, Any]]:
        raise RuntimeError("fixed_calibration_authority_restart_reader_deleted")

    def write_authority_state(self, payload: Dict[str, Any]) -> None:
        del payload
        raise RuntimeError("fixed_calibration_authority_writer_deleted")

    def read_meta_snapshot(self) -> Optional[Dict[str, Any]]:
        raw = self.cm_meta_snapshot.read()
        return raw if isinstance(raw, dict) else None

    def write_profiles_snapshot(self, profiles: Dict[str, Any]) -> None:
        del profiles
        raise RuntimeError("independent_calibration_profile_snapshot_writer_deleted")

    def write_meta_snapshot(self, meta: Dict[str, Any]) -> None:
        self.cm_meta_snapshot.write(dict(meta or {}), indent=2, sort_keys=True)

    def write_authority_meta(self, meta: Dict[str, Any]) -> None:
        del meta
        raise RuntimeError("independent_calibration_authority_meta_writer_deleted")

    def read_context_state(self) -> Optional[Dict[str, Any]]:
        raw = self.read_meta_snapshot() or {}
        context = raw.get("memory_context")
        return dict(context) if isinstance(context, dict) else None

    def write_context_state(self, context: Dict[str, Any]) -> None:
        merged = dict(self.read_meta_snapshot() or {})
        merged["memory_context"] = dict(context or {})
        self.write_meta_snapshot(merged)

    def append_sample(self, event: Dict[str, Any]) -> None:
        self.cm_samples_journal.append(event)

    def compact_samples(self, *, keep_last: int = 1000, gap_threshold: float = 0.4) -> Dict[str, int]:
        events = self.cm_samples_journal.read_all()
        total_before = len(events)
        if total_before <= 0:
            return {"before": 0, "after": 0, "dropped": 0}
        keep_recent = max(0, int(keep_last or 0))
        if keep_recent > 0:
            older = events[:-keep_recent]
            recent = events[-keep_recent:]
        else:
            older = events
            recent = []
        retained = [ev for ev in older if self._should_keep_compacted_sample(ev, gap_threshold=gap_threshold)]
        retained.extend(recent)
        self.cm_samples_journal.rewrite(retained)
        return {
            "before": total_before,
            "after": len(retained),
            "dropped": max(0, total_before - len(retained)),
        }

    @staticmethod
    def _should_keep_compacted_sample(event: Dict[str, Any], *, gap_threshold: float) -> bool:
        if not isinstance(event, dict):
            return True
        sample = event.get("sample")
        if isinstance(sample, dict):
            gap_score = float(sample.get("gap_score", 0.0) or 0.0)
            needs_calibration = bool(sample.get("needs_calibration", False))
            baseline_ready = bool(sample.get("baseline_ready", False))
            return needs_calibration or (not baseline_ready) or gap_score >= float(gap_threshold)
        status = event.get("calibration_status")
        if isinstance(status, dict):
            gap_score = float(status.get("gap_score", 0.0) or 0.0)
            needs_calibration = bool(status.get("needs_calibration", False))
            baseline_ready = bool(status.get("baseline_ready", False))
            return needs_calibration or (not baseline_ready) or gap_score >= float(gap_threshold)
        return True

    def get_runtime_stats(self) -> Dict[str, Any]:
        return {
            "authority_snapshot": self.authority_snapshot.stats(),
            "cm_profiles_snapshot": self.cm_profiles_snapshot.stats(),
            "cm_meta_snapshot": self.cm_meta_snapshot.stats(),
            "cm_samples_journal": self.cm_samples_journal.stats(),
        }
