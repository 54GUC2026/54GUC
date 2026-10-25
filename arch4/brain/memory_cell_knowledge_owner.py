"""Serving-cell knowledge owner for Memory."""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from arch4.signal_processing.band_quality import score_band_quality
from arch4.brain.delivered_load_memory import normalize_dlst
from arch4.brain.memory_delivered_load_owner import (
    apply_delivered_load_speed_test_record,
    dlst_from_modem_computed,
)
from arch4.brain.memory_learning_gate_journal_owner import (
    store_learning_gate_allowed as memory_store_learning_gate_allowed,
)
from arch4.interfaces import CellKnowledge

OWNER = "arch4.brain.memory_cell_knowledge_owner"


@dataclass(frozen=True)
class CellKnowledgeSnapshot:
    pci: int
    cell_id: int
    band: int
    earfcn: int
    times_served: int
    last_seen: float
    avg_sinr: float
    avg_throughput: float
    best_ca_signature: str
    observed_bw_mhz: float
    avg_bq_cell: float
    avg_cq_cell: float
    nr_total_count: int
    nr_attach_count: int
    nr_sinr_ema: float


def _cell_snapshot(cell: Any) -> CellKnowledgeSnapshot:
    return CellKnowledgeSnapshot(
        pci=int(getattr(cell, "pci", 0) or 0),
        cell_id=int(getattr(cell, "cell_id", 0) or 0),
        band=int(getattr(cell, "band", 0) or 0),
        earfcn=int(getattr(cell, "earfcn", 0) or 0),
        times_served=int(getattr(cell, "times_served", 0) or 0),
        last_seen=float(getattr(cell, "last_seen", 0.0) or 0.0),
        avg_sinr=float(getattr(cell, "avg_sinr", 0.0) or 0.0),
        avg_throughput=float(getattr(cell, "avg_throughput", 0.0) or 0.0),
        best_ca_signature=str(getattr(cell, "best_ca_signature", "") or ""),
        observed_bw_mhz=float(getattr(cell, "observed_bw_mhz", 0.0) or 0.0),
        avg_bq_cell=float(getattr(cell, "avg_bq_cell", 0.0) or 0.0),
        avg_cq_cell=float(getattr(cell, "avg_cq_cell", 0.0) or 0.0),
        nr_total_count=int(getattr(cell, "nr_total_count", 0) or 0),
        nr_attach_count=int(getattr(cell, "nr_attach_count", 0) or 0),
        nr_sinr_ema=float(getattr(cell, "nr_sinr_ema", 0.0) or 0.0),
    )


def get_cell_snapshot(memory: Any, pci: int) -> Optional[CellKnowledgeSnapshot]:
    cell = getattr(memory, "cells", {}).get(int(pci or 0))
    if cell is None:
        return None
    return _cell_snapshot(cell)


def iter_cell_snapshots(memory: Any) -> list[tuple[int, CellKnowledgeSnapshot]]:
    return [
        (int(pci), _cell_snapshot(cell))
        for pci, cell in list(getattr(memory, "cells", {}).items())
    ]


def update_cell(
    memory: Any,
    modem: CellularIntelligenceGeneration,
    computed: Optional[DerivedSignalFrames] = None,
    *,
    learning_gate: Dict[str, Any],
) -> None:
    if not memory_store_learning_gate_allowed(
        memory,
        learning_gate,
        surface="cell_memory",
        context={
            "cell_id": int(getattr(modem.cellular, 'cell_id', 0) or 0),
            "band": int(getattr(modem.cellular, 'primary_band', 0) or 0),
        },
    ):
        return
    cell_id = int(getattr(modem.cellular, 'cell_id', 0) or 0)
    if cell_id not in memory.cells:
        memory.cells[cell_id] = CellKnowledge(
            pci=getattr(modem.cellular, 'pci', 0),
            cell_id=cell_id,
            band=getattr(modem.cellular, 'primary_band', 0),
            earfcn=getattr(modem.cellular, 'earfcn_dl', 0),
        )
    cell = memory.cells[cell_id]
    _update_lte_cell_fields(memory, cell, modem)
    if computed is not None:
        _update_computed_cell_fields(memory, cell, modem, computed)
    memory._mark_dirty()
    _update_dwell_tracking(memory, modem)


def _update_lte_cell_fields(memory: Any, cell: CellKnowledge, modem: CellularIntelligenceGeneration) -> None:
    cell.times_served += 1
    cell.last_seen = time.time()
    alpha = 0.05
    cell.avg_sinr = (1 - alpha) * cell.avg_sinr + alpha * float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0)
    cell.avg_throughput = (1 - alpha) * cell.avg_throughput + alpha * float(
        getattr(modem.traffic, 'dl_mbps', 0.0) or 0.0
    )
    if bool(getattr(modem.cellular, 'ca_active', False)):
        cell.best_ca_signature = str(getattr(modem.cellular, 'ca_band_signature', "") or "")
    bw = float(getattr(modem.cellular, 'primary_bw_mhz', 0.0) or 0.0)
    if bw > 0.0 and bw > cell.observed_bw_mhz:
        cell.observed_bw_mhz = bw


def _update_computed_cell_fields(
    memory: Any,
    cell: CellKnowledge,
    modem: CellularIntelligenceGeneration,
    computed: DerivedSignalFrames,
) -> None:
    alpha = 0.05
    dlst = dlst_from_modem_computed(memory, modem, computed)
    cell.DLST = normalize_dlst(dlst)
    if bool(dlst.get("measured", False)):
        apply_delivered_load_speed_test_record(memory, dlst)

    bq = score_band_quality(
        sinr_db=float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0),
        rsrp_db=float(getattr(modem.cellular, 'rsrp_dbm', 0.0) or 0.0),
        ri=int(getattr(modem.cellular, 'ri', 0) or 0),
        ca_value=1.0 if bool(getattr(modem.cellular, 'ca_active', False)) else 0.0,
        bw_mhz=float(getattr(modem.cellular, 'primary_bw_mhz', 0.0) or 0.0),
    )
    bq_score = float(bq.get("quality_score", 0.0) or 0.0)
    if int(bq.get("metric_count", 0) or 0) >= 2 and bq_score > 0.0:
        if cell.avg_bq_cell <= 0.0:
            cell.avg_bq_cell = bq_score
        else:
            cell.avg_bq_cell = (1 - alpha) * cell.avg_bq_cell + alpha * bq_score

    cq = float(getattr(computed.quality, 'cq_cell', 0.0) or 0.0)
    cq_valid = bool(getattr(computed.quality, 'cq_vector_a_valid', False))
    if cq_valid and cq > 0.0:
        if cell.avg_cq_cell <= 0.0:
            cell.avg_cq_cell = cq
        else:
            cell.avg_cq_cell = (1 - alpha) * cell.avg_cq_cell + alpha * cq

    cell.nr_total_count += 1
    nsa_active = bool(
        getattr(computed.quality, "nr_secondary_active", False)
    )
    if nsa_active:
        cell.nr_attach_count += 1
        nr_sinr = float(modem.cellular.nr_sinr_db or 0.0)
        if nr_sinr > 0.0:
            if cell.nr_sinr_ema <= 0.0:
                cell.nr_sinr_ema = nr_sinr
            else:
                cell.nr_sinr_ema = 0.9 * cell.nr_sinr_ema + 0.1 * nr_sinr


def _update_dwell_tracking(memory: Any, modem: CellularIntelligenceGeneration) -> None:
    current_band = int(getattr(modem.cellular, 'primary_band', 0) or 0)
    now_ts = time.time()
    dwell_band = getattr(memory, "_dwell_current_band", 0)
    transition = modem.radio_transition
    episode_id = str(transition.episode_id or "")
    selection_source = str(transition.source or "attribution_unavailable")
    dwell_episode_id = str(getattr(memory, "_dwell_current_episode_id", "") or "")
    if current_band <= 0:
        return
    if current_band == dwell_band and episode_id and episode_id == dwell_episode_id:
        return
    dwell_since = getattr(memory, "_dwell_current_band_since", 0.0)
    if dwell_band > 0 and dwell_since > 0.0:
        dwell_s = now_ts - dwell_since
        if not hasattr(memory, "_dwell_tracking"):
            memory._dwell_tracking = {}
        if dwell_band not in memory._dwell_tracking:
            memory._dwell_tracking[dwell_band] = {
                "total_dwell_s": 0.0,
                "switch_count": 0,
                "short_dwell_count": 0,
                "lower_controller_episode_count": 0,
                "guc_command_episode_count": 0,
                "attribution_unavailable_episode_count": 0,
            }
        entry = memory._dwell_tracking[dwell_band]
        entry["total_dwell_s"] = entry.get("total_dwell_s", 0.0) + dwell_s
        entry["switch_count"] = entry.get("switch_count", 0) + 1
        threshold = getattr(memory, "_dwell_short_threshold_s", 600.0)
        if dwell_s < threshold:
            entry["short_dwell_count"] = entry.get("short_dwell_count", 0) + 1
        prior_source = str(
            getattr(
                memory,
                "_dwell_current_selection_source",
                "attribution_unavailable",
            )
            or "attribution_unavailable"
        )
        if prior_source.startswith("lower_controller"):
            count_key = "lower_controller_episode_count"
        elif prior_source == "54guc_command":
            count_key = "guc_command_episode_count"
        else:
            count_key = "attribution_unavailable_episode_count"
        entry[count_key] = int(entry.get(count_key, 0) or 0) + 1
    memory._dwell_current_band = current_band
    memory._dwell_current_band_since = now_ts
    memory._dwell_current_episode_id = episode_id
    memory._dwell_current_selection_source = selection_source


def get_band_dwell_stats(memory: Any, band: int) -> Optional[Dict[str, float]]:
    entry = getattr(memory, "_dwell_tracking", {}).get(int(band))
    if not entry or entry.get("switch_count", 0) < 2:
        return None
    count = int(entry["switch_count"])
    return {
        "avg_dwell_s": entry["total_dwell_s"] / count,
        "short_dwell_rate": entry.get("short_dwell_count", 0) / count,
        "switch_count": count,
        "lower_controller_episode_count": int(
            entry.get("lower_controller_episode_count", 0) or 0
        ),
        "guc_command_episode_count": int(
            entry.get("guc_command_episode_count", 0) or 0
        ),
        "attribution_unavailable_episode_count": int(
            entry.get("attribution_unavailable_episode_count", 0) or 0
        ),
    }


__all__ = [
    "CellKnowledgeSnapshot",
    "OWNER",
    "get_cell_snapshot",
    "get_band_dwell_stats",
    "iter_cell_snapshots",
    "update_cell",
]
