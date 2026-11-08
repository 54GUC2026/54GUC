"""
arch4/brain/memory_namespaces.py
───────────────────────────────────────────────────────────────────────────────
Provider namespace helpers for Memory.

These helpers keep provider-scoped learning state isolated without changing
the public Memory facade.
───────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any, Dict

from arch4.brain.band_learning import BandLearningStore
from arch4.brain.delivered_load_memory import normalize_dlst
from arch4.brain.memory_baselines import BaselineStats
from arch4.brain.memory_calibration_marker_owner import (
    compact_calibration_alias_graph,
    resolve_marker_alias,
)
from arch4.brain.memory_io import band_to_dict, cell_to_dict
from arch4.interfaces import BandKnowledge, CellKnowledge

OWNER = "arch4.brain.memory_namespaces"
log = logging.getLogger("arch4.brain.memory_namespaces")


def new_provider_namespace() -> Dict[str, Any]:
    return {
        "bands": {},
        "cells": {},
        "baselines": {},
        "band_learning_store": BandLearningStore(),
        "calibration_contexts": {},
        "calibration_aliases": {},
        "current_calibration_marker": "C0",
        "current_calibration_signature": {},
        "current_context_seen_actions": 0,
        "band_anchor_stability": {},
        "delivered_load_speed_tests": {},
    }


def capture_current_namespace(mem) -> None:
    mem._provider_namespaces[mem._current_provider_plmn] = {
        "bands": mem.bands,
        "cells": mem.cells,
        "baselines": mem.baselines,
        "band_learning_store": mem._band_learning_store,
        "calibration_contexts": mem.calibration_contexts,
        "calibration_aliases": mem.calibration_aliases,
        "current_calibration_marker": mem.current_calibration_marker,
        "current_calibration_signature": mem.current_calibration_signature,
        "current_context_seen_actions": int(mem.current_context_seen_actions),
        "band_anchor_stability": mem.band_anchor_stability,
        "delivered_load_speed_tests": mem._delivered_load_speed_tests,
    }


def bind_namespace(mem, ns: Dict[str, Any]) -> None:
    mem.bands = ns["bands"]
    mem.cells = ns["cells"]
    mem.baselines = ns["baselines"]
    mem._band_learning_store = ns.get("band_learning_store") or BandLearningStore.from_dict(ns)
    mem.calibration_contexts = ns.get("calibration_contexts", {})
    mem.calibration_aliases = ns.get("calibration_aliases", {})
    compact_calibration_alias_graph(mem)
    mem.current_calibration_marker = resolve_marker_alias(mem, str(ns.get("current_calibration_marker", "C0") or "C0"))
    mem.current_calibration_signature = dict(ns.get("current_calibration_signature", {}) or {})
    mem.current_context_seen_actions = int(ns.get("current_context_seen_actions", 0) or 0)
    mem.band_anchor_stability = ns["band_anchor_stability"]
    mem._delivered_load_speed_tests = {
        str(k): normalize_dlst(v)
        for k, v in dict(ns.get("delivered_load_speed_tests", {}) or {}).items()
    }


def set_provider_context(mem, provider_plmn: str) -> None:
    plmn = (provider_plmn or "").strip()
    if plmn == getattr(mem, "_current_provider_plmn", ""):
        return
    previous_plmn = getattr(mem, "_current_provider_plmn", "")
    capture_current_namespace(mem)
    ns = mem._provider_namespaces.get(plmn)
    if ns is None:
        if plmn and previous_plmn == "":
            global_ns = mem._provider_namespaces.get("")
            if isinstance(global_ns, dict) and global_ns:
                ns = deepcopy(global_ns)
            else:
                ns = new_provider_namespace()
        else:
            ns = new_provider_namespace()
        mem._provider_namespaces[plmn] = ns
    bind_namespace(mem, ns)
    mem._current_provider_plmn = plmn
    log.info("Memory provider context: %s", plmn or "<global>")


def apply_calibration_context_state(mem, context: Dict[str, Any]) -> None:
    providers = context.get("providers", {})
    if isinstance(providers, dict):
        for plmn, ns_d in providers.items():
            plmn_key = str(plmn or "")
            ns = mem._provider_namespaces.get(plmn_key)
            if ns is None:
                ns = new_provider_namespace()
                mem._provider_namespaces[plmn_key] = ns
            ns["calibration_contexts"] = {
                str(k): dict(v)
                for k, v in dict(ns_d.get("calibration_contexts", {}) or {}).items()
            }
            ns["calibration_aliases"] = {
                str(k): str(v)
                for k, v in dict(ns_d.get("calibration_aliases", {}) or {}).items()
            }
            ns["current_calibration_marker"] = str(
                ns_d.get("current_calibration_marker", "C0") or "C0"
            )
            ns["current_calibration_signature"] = dict(
                ns_d.get("current_calibration_signature", {}) or {}
            )
            ns["current_context_seen_actions"] = int(
                ns_d.get("current_context_seen_actions", 0) or 0
            )
    provider_context = str(context.get("provider_context", getattr(mem, "_current_provider_plmn", "")) or "")
    if provider_context not in mem._provider_namespaces:
        mem._provider_namespaces[provider_context] = new_provider_namespace()
    mem._current_provider_plmn = provider_context
    bind_namespace(mem, mem._provider_namespaces[provider_context])


def serialize_provider_namespaces(mem) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    capture_current_namespace(mem)
    for plmn, ns in mem._provider_namespaces.items():
        band_learning_store = ns.get("band_learning_store") or BandLearningStore.from_dict(ns)
        out[plmn] = {
            "bands": {str(b): band_to_dict(bk) for b, bk in ns["bands"].items()},
            "cells": {str(p): cell_to_dict(ck) for p, ck in ns["cells"].items()},
            "baselines": {k: v.to_dict() for k, v in ns["baselines"].items()},
            **band_learning_store.to_dict(),
            "calibration_contexts": ns.get("calibration_contexts", {}),
            "calibration_aliases": ns.get("calibration_aliases", {}),
            "current_calibration_marker": ns.get("current_calibration_marker", "C0"),
            "current_calibration_signature": ns.get("current_calibration_signature", {}),
            "current_context_seen_actions": int(ns.get("current_context_seen_actions", 0) or 0),
            "band_anchor_stability": {
                str(b): s for b, s in ns["band_anchor_stability"].items()
            },
            "delivered_load_speed_tests": {
                str(k): normalize_dlst(v)
                for k, v in dict(ns.get("delivered_load_speed_tests", {}) or {}).items()
            },
        }
    return out


def serialize_provider_stm_namespaces(mem) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    capture_current_namespace(mem)
    for plmn, ns in mem._provider_namespaces.items():
        band_learning_store = ns.get("band_learning_store") or BandLearningStore.from_dict(ns)
        out[plmn] = {
            **band_learning_store.to_stm_dict(),
            "delivered_load_speed_tests": {
                str(k): normalize_dlst(v)
                for k, v in dict(ns.get("delivered_load_speed_tests", {}) or {}).items()
            },
        }
    return out


def serialize_provider_ltm_namespaces(mem) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    capture_current_namespace(mem)
    for plmn, ns in mem._provider_namespaces.items():
        band_learning_store = ns.get("band_learning_store") or BandLearningStore.from_dict(ns)
        out[plmn] = {
            "bands": {str(b): band_to_dict(bk) for b, bk in ns["bands"].items()},
            "cells": {str(p): cell_to_dict(ck) for p, ck in ns["cells"].items()},
            "baselines": {k: v.to_dict() for k, v in ns["baselines"].items()},
            **band_learning_store.to_ltm_dict(),
            "band_anchor_stability": {
                str(b): s for b, s in ns["band_anchor_stability"].items()
            },
            "delivered_load_speed_tests": {
                str(k): normalize_dlst(v)
                for k, v in dict(ns.get("delivered_load_speed_tests", {}) or {}).items()
            },
        }
    return out


def serialize_provider_calibration_namespaces(mem) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    capture_current_namespace(mem)
    for plmn, ns in mem._provider_namespaces.items():
        out[plmn] = {
            "calibration_contexts": {
                str(k): dict(v) for k, v in dict(ns.get("calibration_contexts", {}) or {}).items()
            },
            "calibration_aliases": {
                str(k): str(v) for k, v in dict(ns.get("calibration_aliases", {}) or {}).items()
            },
            "current_calibration_marker": str(ns.get("current_calibration_marker", "C0") or "C0"),
            "current_calibration_signature": dict(ns.get("current_calibration_signature", {}) or {}),
            "current_context_seen_actions": int(ns.get("current_context_seen_actions", 0) or 0),
        }
    return out


def deserialize_provider_namespace(mem, ns_d: dict) -> Dict[str, Any]:
    bands: Dict[int, BandKnowledge] = {}
    for b_str, bk_d in ns_d.get("bands", {}).items():
        bk = BandKnowledge(band=int(b_str))
        bk.times_tried = bk_d.get("times_tried", 0)
        bk.times_appeared_in_ca = bk_d.get("times_appeared_in_ca", 0)
        bk.last_tried = bk_d.get("last_tried", 0)
        bk.last_sinr = bk_d.get("last_sinr", 0)
        bk.last_ca_signature_with = bk_d.get("last_ca_signature_with", "")
        bk.avg_throughput_with = bk_d.get("avg_throughput_with", 0)
        bk.never_works = bk_d.get("never_works", False)
        bk.times_primary_attach = bk_d.get("times_primary_attach", 0)
        bk.times_fallback_attach = bk_d.get("times_fallback_attach", 0)
        bk.times_unusable_attach = bk_d.get("times_unusable_attach", 0)
        bk.last_outcome = bk_d.get("last_outcome", "")
        bk.DLST = normalize_dlst(bk_d.get("DLST"))
        bands[int(b_str)] = bk

    cells: Dict[int, CellKnowledge] = {}
    for p_str, ck_d in ns_d.get("cells", {}).items():
        ck = CellKnowledge(
            pci=int(p_str),
            band=ck_d.get("band", 0),
            earfcn=ck_d.get("earfcn", 0),
        )
        ck.times_served = ck_d.get("times_served", 0)
        ck.avg_sinr = ck_d.get("avg_sinr", 0)
        ck.avg_throughput = ck_d.get("avg_throughput", 0)
        ck.avg_mcs = ck_d.get("avg_mcs", 0)
        ck.avg_bq_cell = float(ck_d.get("avg_bq_cell", 0.0) or 0.0)
        ck.avg_cq_cell = float(ck_d.get("avg_cq_cell", 0.0) or 0.0)
        ck.best_ca_signature = ck_d.get("best_ca_signature", "")
        ck.last_seen = ck_d.get("last_seen", 0)
        ck.observed_bw_mhz = float(ck_d.get("observed_bw_mhz", 0.0) or 0.0)
        ck.nr_attach_count = int(ck_d.get("nr_attach_count", 0) or 0)
        ck.nr_total_count = int(ck_d.get("nr_total_count", 0) or 0)
        ck.nr_sinr_ema = float(ck_d.get("nr_sinr_ema", 0.0) or 0.0)
        ck.DLST = normalize_dlst(ck_d.get("DLST"))
        cells[int(p_str)] = ck

    baselines = {
        key: BaselineStats.from_dict(bs_d)
        for key, bs_d in ns_d.get("baselines", {}).items()
    }
    band_learning_store = BandLearningStore.from_dict(ns_d)
    calibration_contexts = {
        str(k): dict(v) for k, v in ns_d.get("calibration_contexts", {}).items()
    }
    calibration_aliases = {
        str(k): str(v) for k, v in ns_d.get("calibration_aliases", {}).items()
    }
    band_anchor_stability: Dict[int, Dict[str, int]] = {}
    for b_str, stats in ns_d.get("band_anchor_stability", {}).items():
        band_anchor_stability[int(b_str)] = {
            "attempts": stats.get("attempts", 0),
            "stuck": stats.get("stuck", 0),
        }
    return {
        "bands": bands,
        "cells": cells,
        "baselines": baselines,
        "band_learning_store": band_learning_store,
        "calibration_contexts": calibration_contexts,
        "calibration_aliases": calibration_aliases,
        "current_calibration_marker": str(ns_d.get("current_calibration_marker", "C0") or "C0"),
        "current_calibration_signature": dict(ns_d.get("current_calibration_signature", {}) or {}),
        "current_context_seen_actions": int(ns_d.get("current_context_seen_actions", 0) or 0),
        "band_anchor_stability": band_anchor_stability,
        "delivered_load_speed_tests": {
            str(k): normalize_dlst(v)
            for k, v in dict(ns_d.get("delivered_load_speed_tests", {}) or {}).items()
        },
    }
