"""5G-device structural quality from qualified, same-generation carrier inputs.

This scorer does not discover capabilities, infer CA-pool membership or grade
learning. Core publishes its input receipt and Referee consumes the result.
"""
from __future__ import annotations
import math
from typing import Mapping, Any
from arch4.signal_processing.band_quality import (
    BASE_BQ_WEIGHTS, normalize_sinr_db, normalize_rsrp_db,
)

CAPACITY_REFERENCE_MHZ = 120.0
FORMULA_REVISION = "5g-bw50-rf40-ca10-v1"
_RADIO_WEIGHT = sum(BASE_BQ_WEIGHTS[k] for k in ("sinr", "rsrp", "ri"))


def _number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def _clip(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_combined_quality(inputs: Mapping[str, Any], *, ca_quality_value: float | None) -> dict[str, Any]:
    """Unknown input is unavailable, never an invented zero or LTE substitution."""
    result = dict(valid=False, reason="radio_inputs_unavailable", bq=None, rf=None,
                  capacity=None, total_bw_mhz=None)
    connected = inputs.get("connected")
    if connected is False:
        return dict(valid=True, reason="radio_disconnected", bq=0.0, rf=0.0,
                    capacity=0.0, total_bw_mhz=0.0)
    if connected is not True or type(inputs.get("nr_active")) is not bool:
        return result
    if not _number(ca_quality_value) or not 0 <= ca_quality_value <= 1:
        result["reason"] = "ca_quality_unavailable"
        return result
    carriers = []
    if inputs.get("network_type") != "NR SA (5G)":
        carriers.append(inputs.get("lte"))
        # Only measured primary bandwidths count; never infer CA secondaries.
    if inputs["nr_active"]:
        carriers.append(inputs.get("nr"))
    if not carriers:
        return result
    radio_terms = []
    for carrier in carriers:
        if not isinstance(carrier, Mapping) or any(
                not _number(carrier.get(k)) for k in ("bw", "sinr", "rsrp")):
            return result
        if not (carrier["bw"] > 0
                and -150 <= carrier["rsrp"] <= -20 and -30 <= carrier["sinr"] <= 60):
            result["reason"] = "carrier_measurement_out_of_range"
            return result
        rank = carrier.get("ri")
        omit_rank = rank is None and carrier.get("ri_optional") is True
        if not omit_rank:
            if not _number(rank):
                return result
            if not 1 <= rank <= 8:
                result["reason"] = "carrier_measurement_out_of_range"
                return result
        weighted_rf = (
            BASE_BQ_WEIGHTS["sinr"] * normalize_sinr_db(carrier["sinr"])
            + BASE_BQ_WEIGHTS["rsrp"] * normalize_rsrp_db(carrier["rsrp"])
        )
        weight = _RADIO_WEIGHT
        if omit_rank:
            # Redistribute the absent RI contribution proportionally within
            # this carrier's RF term; never substitute a made-up rank.
            weight -= BASE_BQ_WEIGHTS["ri"]
        else:
            weighted_rf += BASE_BQ_WEIGHTS["ri"] * _clip(rank / 2)
        radio_terms.append(carrier["bw"] * weighted_rf / weight)
    total = sum(c["bw"] for c in carriers)
    radio = sum(radio_terms) / total
    capacity = _clip(total / CAPACITY_REFERENCE_MHZ)
    # CQB excludes capacity, retaining the same relative radio/CA influence.
    communication = (.4 * radio + .1 * ca_quality_value) / .5
    return dict(valid=True, reason="qualified_carriers",
                bq=.5*capacity+.4*radio+.1*ca_quality_value,
                rf=communication, capacity=capacity, total_bw_mhz=total)
