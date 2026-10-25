import pytest

from arch4.interfaces import CarrierInfo, ComputedSignals, ModemInput
from arch4.status_payload import build_status_payload
from arch4.synthetic_quality import export_synthetic_quality_fields
from arch4.tests.controller_generation_factory import (
    materialize_controller_generation,
)


def _modem(**overrides) -> ModemInput:
    base = dict(
        sinr_db=11.0,
        rsrp_dbm=-71.0,
        rsrq_db=-10.0,
        rssi_dbm=-51.0,
        mcs_dl=25,
        mcs_ul=20,
        cqi=11,
        ri=2,
        carriers=[
            CarrierInfo(
                band=3,
                earfcn=1651,
                pci=205,
                bandwidth_mhz=20.0,
                sinr_db=11.0,
                rsrp_dbm=-71.0,
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
        rx_bytes_per_sec=12_500_000.0,
        tx_bytes_per_sec=62_500.0,
        mcs_dl0=25,
        mcs_dl1=0,
        mod0=8,
        mod1=0,
        qam_dl_primary=8,
        qam_dl_secondary=0,
        qam_ul=6,
        qam_source="inferred",
        qam_confidence=0.75,
    )
    base.update(overrides)
    return ModemInput(**base)

def test_export_synthetic_quality_fields_prefers_supervised_qam_under_real_traffic():
    modem = _modem(sinr_db=12.0, mcs_dl=25, mcs_dl0=25, mod0=8, qam_dl_primary=8)
    with pytest.raises(
        RuntimeError,
        match="retired_synthetic_scheduler_export",
    ):
        export_synthetic_quality_fields(modem, backend="b818")


def test_build_status_payload_exports_synthetic_display_fields():
    modem = _modem()
    computed = ComputedSignals()
    base_status = {"backend": "b818", "model": "B818-263"}
    payload = build_status_payload(
        base_status=base_status,
        accepted=materialize_controller_generation(
            modem,
            computed,
            owner_values=base_status,
        ),
        constraints=None,
        state_name="WORKING",
    )
    assert "qam_t" not in payload
    assert "cqi_t" not in payload
    assert "mcs_t" not in payload
    assert "sinr_est" not in payload


def test_build_status_payload_falls_back_when_runtime_scheduler_lane_is_unavailable():
    modem = _modem(cqi=0, mcs_dl=24, mcs_dl0=24, mod0=0, qam_dl_primary=0)
    computed = ComputedSignals()
    base_status = {"backend": "mc888", "model": "MC888"}
    payload = build_status_payload(
        base_status=base_status,
        accepted=materialize_controller_generation(
            modem,
            computed,
            owner_values=base_status,
        ),
        constraints=None,
        state_name="WORKING",
    )
    assert "qamt" not in payload
    assert "cqit" not in payload
    assert "mcst" not in payload
