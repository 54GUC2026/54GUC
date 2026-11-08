from types import SimpleNamespace

import pytest

from arch4.signal_processing.scheduler_processor import (
    PROVENANCE_CQI_REPORTED,
    PROVENANCE_DUAL_CQI,
    PROVENANCE_DUAL_MCS,
    PROVENANCE_MCS_REPORTED,
    PROVENANCE_UNAVAILABLE,
    TRUST_NONE,
    SchedulerProcessor,
)
from arch4.interfaces import CarrierInfo, ComputedSignals, ModemInput
from arch4.status_payload import build_status_payload
from arch4.synthetic_quality import derive_synthetic_scheduler_lane, export_synthetic_quality_fields
from arch4.tests.controller_generation_factory import (
    materialize_controller_generation,
)


def _modem(**overrides) -> ModemInput:
    base = dict(
        sinr_db=11.0,
        rsrp_dbm=-75.0,
        rsrq_db=-10.0,
        rssi_dbm=-55.0,
        mcs_dl=24,
        mcs_ul=0,
        cqi=14,
        ri=2,
        carriers=[
            CarrierInfo(
                band=3,
                earfcn=1651,
                pci=205,
                bandwidth_mhz=20.0,
                sinr_db=11.0,
                rsrp_dbm=-75.0,
                state=1,
                is_pcell=True,
            )
        ],
        ca_active=True,
        ca_band_signature="B3+CA",
        primary_band=3,
        pci=205,
        earfcn=1651,
        enb_id=2070,
        radio_connected=True,
        session_uptime_s=3600.0,
        rx_bytes_per_sec=1_000_000.0,
        tx_bytes_per_sec=100_000.0,
        mcs_dl0=24,
        mcs_dl1=0,
        mod0=8,
        mod1=0,
        qam_dl_primary=8,
        qam_dl_secondary=0,
        qam_ul=4,
        qam_source="reported",
        qam_confidence=1.0,
    )
    base.update(overrides)
    return ModemInput(**base)


def _derive_with_canonical_processor(
    modem: ModemInput,
    *,
    mode: str,
):
    return SchedulerProcessor().update(
        cqi=modem.cqi,
        mcs_dl=modem.mcs_dl0 or modem.mcs_dl,
        cs=SimpleNamespace(),
        mode=mode,
    )


def test_cqi_mode_uses_approved_bucket_table():
    lane = _derive_with_canonical_processor(
        _modem(cqi=2, mcs_dl0=0, mcs_dl=0),
        mode="cqi",
    )
    assert lane.qamt == 2
    assert lane.cqit == 2
    assert lane.mcst == 4
    assert lane.provenance == PROVENANCE_CQI_REPORTED

    lane = _derive_with_canonical_processor(
        _modem(cqi=5, mcs_dl0=0, mcs_dl=0),
        mode="cqi",
    )
    assert lane.qamt == 4
    assert lane.cqit == 5
    assert lane.mcst == 7

    lane = _derive_with_canonical_processor(
        _modem(cqi=9, mcs_dl0=0, mcs_dl=0),
        mode="cqi",
    )
    assert lane.qamt == 6
    assert lane.cqit == 9
    assert lane.mcst == 15

    lane = _derive_with_canonical_processor(
        _modem(cqi=14, mcs_dl0=0, mcs_dl=0),
        mode="cqi",
    )
    assert lane.qamt == 8
    assert lane.cqit == 14
    assert lane.mcst == 24


def test_mcs_mode_uses_approved_bucket_table():
    lane = _derive_with_canonical_processor(
        _modem(cqi=0, mcs_dl0=4, mcs_dl=4),
        mode="mcs",
    )
    assert lane.qamt == 2
    assert lane.cqit == 2
    assert lane.mcst == 4
    assert lane.provenance == PROVENANCE_MCS_REPORTED

    lane = _derive_with_canonical_processor(
        _modem(cqi=0, mcs_dl0=7, mcs_dl=7),
        mode="mcs",
    )
    assert lane.qamt == 4
    assert lane.cqit == 5
    assert lane.mcst == 7

    lane = _derive_with_canonical_processor(
        _modem(cqi=0, mcs_dl0=15, mcs_dl=15),
        mode="mcs",
    )
    assert lane.qamt == 6
    assert lane.cqit == 9
    assert lane.mcst == 15

    lane = _derive_with_canonical_processor(
        _modem(cqi=0, mcs_dl0=24, mcs_dl=24),
        mode="mcs",
    )
    assert lane.qamt == 8
    assert lane.cqit == 14
    assert lane.mcst == 24


def test_cqi_mode_marks_missing_values_unavailable_without_hold_or_decay():
    proc = SchedulerProcessor()
    cs = SimpleNamespace()

    lane = proc.update(
        cqi=14,
        mcs_dl=0,
        cs=cs,
        mode="cqi",
    )
    assert lane.qamt == 8

    lane = proc.update(
        cqi=0,
        mcs_dl=0,
        cs=cs,
        mode="cqi",
    )
    assert lane.qamt == 0
    assert lane.cqit == 0
    assert lane.mcst == 0
    assert lane.provenance == PROVENANCE_UNAVAILABLE
    assert lane.trust == TRUST_NONE
    assert cs.qamt == 0
    assert cs.sched_provenance == PROVENANCE_UNAVAILABLE


def test_mcs_mode_marks_values_below_four_unavailable_without_hold():
    proc = SchedulerProcessor()
    cs = SimpleNamespace()

    lane = proc.update(
        cqi=0,
        mcs_dl=24,
        cs=cs,
        mode="mcs",
    )
    assert lane.qamt == 8

    lane = proc.update(
        cqi=0,
        mcs_dl=1,
        cs=cs,
        mode="mcs",
    )
    assert lane.qamt == 0
    assert lane.cqit == 0
    assert lane.mcst == 0
    assert lane.provenance == PROVENANCE_UNAVAILABLE
    assert lane.trust == TRUST_NONE
    assert cs.qamt == 0

    lane = proc.update(
        cqi=0,
        mcs_dl=12,
        cs=cs,
        mode="mcs",
    )
    assert lane.qamt == 6
    assert lane.provenance == PROVENANCE_MCS_REPORTED


def test_dual_mode_prefers_valid_mcs_and_falls_back_to_cqi_when_mcs_is_invalid():
    lane = _derive_with_canonical_processor(_modem(cqi=14, mcs_dl0=12, mcs_dl=12), mode="dual")
    assert lane.qamt == 6
    assert lane.provenance == PROVENANCE_DUAL_MCS

    lane = _derive_with_canonical_processor(_modem(cqi=14, mcs_dl0=1, mcs_dl=1), mode="dual")
    assert lane.qamt == 8
    assert lane.provenance == PROVENANCE_DUAL_CQI


def test_retired_export_helpers_fail_and_status_uses_accepted_scheduler_facts():
    modem = _modem(cqi=14, mcs_dl0=24, mcs_dl=24)
    with pytest.raises(
        RuntimeError,
        match="retired_synthetic_scheduler_export",
    ):
        export_synthetic_quality_fields(modem, backend="b818")
    with pytest.raises(
        RuntimeError,
        match="retired_synthetic_scheduler_lane",
    ):
        derive_synthetic_scheduler_lane(modem, backend="b818")

    computed = ComputedSignals()
    accepted = materialize_controller_generation(
        modem,
        computed,
        owner_values={"backend": "b818", "model": "B818-263"},
    )
    payload = build_status_payload(
        base_status={"backend": "b818", "model": "B818-263"},
        accepted=accepted,
        constraints=None,
        state_name="WORKING",
    )
    assert payload["mcs_dl0"] == 24
    assert payload["cqi"] == 14
    for key in ("qamt", "cqit", "mcst", "sched_provenance", "sched_trust"):
        assert key not in payload
