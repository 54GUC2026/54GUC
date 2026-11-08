from __future__ import annotations

import pytest

from arch4.modem.mikrotik_parsers import (
    decode_routeros_row,
    normalize_routeros_antenna_mode,
    parse_routeros_lte_bands,
    parse_routeros_lte_monitor_row,
    parse_qeng_servingcell,
    rb_to_mhz,
    routeros_antenna_status,
    routeros_monitor_ca_observation_status,
    routeros_monitor_ca_bands,
    routeros_monitor_carrier_details,
    routeros_monitor_ca_entry_count,
    routeros_monitor_active_bands,
)


def test_decode_routeros_row_accepts_binary_and_text_values():
    row = decode_routeros_row({b"primary-band": b"B20@10Mhz", "status": "connected", "empty": None})

    assert row == {"primary-band": "B20@10Mhz", "status": "connected", "empty": ""}


def test_parse_routeros_lte_bands_requires_band_context_for_text_numbers():
    assert parse_routeros_lte_bands("B1+B3 LTE BAND 20") == [1, 3, 20]
    assert parse_routeros_lte_bands("earfcn 6400 pci 319") == []
    assert parse_routeros_lte_bands("B6400") == []
    assert parse_routeros_lte_bands([20, "B3", "LTE BAND 1"]) == [1, 3, 20]


def test_routeros_monitor_active_bands_collects_ca_fields():
    row = {
        "primary-band": "B20@10Mhz",
        "ca-band": "B3",
        "secondary-band-1": "LTE BAND 1",
        "earfcn": "6400",
    }

    assert routeros_monitor_active_bands(row, primary_band=20) == [1, 3, 20]


def test_routeros_monitor_ca_bands_count_same_band_ca_as_active():
    row = {
        "primary-band": "B3@10Mhz earfcn: 1651 phy-cellid: 10",
        "ca-band": "B3@20Mhz earfcn: 1795 phy-cellid: 11",
    }

    result = parse_routeros_lte_monitor_row(row)

    assert routeros_monitor_ca_bands(row) == [3]
    assert result["active_band_set"] == [3]
    assert result["ca_component_band_set"] == [3]
    assert routeros_monitor_ca_entry_count(row) == 1
    assert result["ca_component_count"] == 1
    assert result["ca_composition_signature"] == "P@B3@earfcn=1651@pci=10 + CA@B3@earfcn=1795@pci=11"
    assert result["ca_active"] is True


def test_routeros_monitor_counts_same_band_ca_entries_not_unique_bands():
    row = {
        "primary-band": "B3@10Mhz earfcn: 1651 phy-cellid: 10",
        "ca-band": "B3@20Mhz earfcn: 1795 phy-cellid: 11",
        "secondary-band-1": "B3@15Mhz earfcn: 1801 phy-cellid: 12",
    }

    result = parse_routeros_lte_monitor_row(row)

    assert routeros_monitor_ca_bands(row) == [3]
    assert routeros_monitor_ca_entry_count(row) == 2
    assert result["ca_component_band_set"] == [3]
    assert result["ca_component_count"] == 2
    assert result["ca_active"] is True
    assert "CA@B3@earfcn=1795@pci=11" in result["ca_composition_signature"]
    assert "CA@B3@earfcn=1801@pci=12" in result["ca_composition_signature"]


def test_routeros_monitor_generic_active_bands_do_not_prove_ca():
    row = {
        "primary-band": "B3@10Mhz",
        "active-bands": "B3+B20",
    }

    result = parse_routeros_lte_monitor_row(row)

    assert result["active_band_set"] == [3]
    assert result["active_band_set_signature"] == "B3"
    assert result["ca_active"] is False
    assert result["ca_component_band_set"] == []


def test_routeros_monitor_parses_multiple_scell_members_from_one_ca_value():
    row = {
        "primary-band": "B3@10Mhz earfcn: 1651 phy-cellid: 10",
        "ca-band": (
            "B3@20Mhz earfcn: 1795 phy-cellid: 11\n"
            "B1@15Mhz earfcn: 250 phy-cellid: 12"
        ),
    }

    result = parse_routeros_lte_monitor_row(row)

    assert routeros_monitor_ca_observation_status(row) == "active"
    assert result["ca_active"] is True
    assert result["active_band_set"] == [1, 3]
    assert result["ca_component_band_set"] == [1, 3]
    assert result["ca_component_count"] == 2
    assert [item["band"] for item in result["carrier_details"]] == [3, 3, 1]
    assert [item["is_pcell"] for item in result["carrier_details"]] == [True, False, False]
    assert result["ca_composition_signature"] == (
        "P@B3@earfcn=1651@pci=10 + "
        "CA@B3@earfcn=1795@pci=11 + "
        "CA@B1@earfcn=250@pci=12"
    )


def test_routeros_monitor_accepts_bare_band_only_inside_ca_field_context():
    row = {
        "primary-band": "3@10Mhz",
        "ca-band": "3",
    }

    result = parse_routeros_lte_monitor_row(row)

    assert result["ca_active"] is True
    assert result["ca_component_band_set"] == [3]
    assert result["ca_component_count"] == 1


def test_routeros_monitor_does_not_turn_unparsed_ca_value_into_false():
    row = {
        "primary-band": "B3@10Mhz",
        "ca-band": "vendor-new-carrier-value",
    }

    result = parse_routeros_lte_monitor_row(row)

    assert routeros_monitor_ca_observation_status(row) == "unparsed"
    assert result["_routeros_ca_observation_status"] == "unparsed"
    assert "ca_active" not in result
    assert "ca_component_band_set" not in result


def test_parse_routeros_lte_monitor_row_normalizes_identity_and_state():
    row = {
        "status": "connected",
        "primary-band": "B20@10Mhz earfcn: 6200 phy-cellid: 304",
        "ca-band": "B3",
        "secondary-band": "B1",
        "sinr": "11dB",
        "rsrp": "-86dBm",
        "rsrq": "-9dB",
        "rssi": "-62dBm",
        "cqi": "12",
        "ri": "2",
        "current-operator": "A1",
        "current-cellid": "0x123456",
        "tracking-area-code": "0x0f0a",
        "session-uptime": "1m5s",
        "dl-mcs": "16",
        "ul-mcs": "8",
        "dl-modulation": "256-qam",
        "ul-modulation": "64QAM",
        "imei": "356597080872497",
        "imsi": "220031234567890",
        "manufacturer": "Quectel",
        "model": "EG120K-EA",
        "revision": "EG120KEAAAR01A10M2G",
    }

    result = parse_routeros_lte_monitor_row(row, parse_uptime=lambda raw: 65.0 if raw == "1m5s" else 0.0)

    assert result["primary_band"] == 20
    assert result["dl_bw_mhz"] == 10.0
    assert result["earfcn"] == 6200
    assert result["pci"] == 304
    assert result["active_band_set"] == [1, 3, 20]
    assert result["active_band_set_signature"] == "B1+B3+B20"
    assert result["ca_component_band_set"] == [1, 3]
    assert result["ca_component_count"] == 2
    assert result["ca_active"] is True
    assert result["cell_id"] == 0x123456
    assert result["enb_id"] == (0x123456 >> 8)
    assert result["tac"] == 0x0F0A
    assert result["radio_connected"] is True
    assert result["session_uptime_s"] == 65.0
    assert result["mcs_dl"] == 16
    assert result["mcs_ul"] == 8
    assert result["qam_dl0_reported"] == 8
    assert result["qam_ul_reported"] == 6
    assert result["imei"] == "356597080872497"
    assert result["imsi"] == "220031234567890"
    assert result["vendor"] == "Quectel"
    assert result["modem_model"] == "EG120K-EA"
    assert result["firmware"] == "EG120KEAAAR01A10M2G"


@pytest.mark.parametrize("value", [None, "", "unknown", "n/a", "vendor-new-value"])
def test_parse_routeros_lte_monitor_row_keeps_optional_modulation_missing(value):
    result = parse_routeros_lte_monitor_row(
        {
            "status": "connected",
            "dl-modulation": value,
            "ul-modulation": value,
        }
    )

    assert "qam_dl0_reported" not in result
    assert "qam_ul_reported" not in result


def test_parse_routeros_lte_monitor_row_translates_routeros_lac_to_canonical_tac():
    decimal = parse_routeros_lte_monitor_row(
        {"status": "connected", "lac": "20213"}
    )
    hexadecimal = parse_routeros_lte_monitor_row(
        {"status": "connected", "lac": "0x4ef5"}
    )

    assert decimal["tac"] == 20213
    assert hexadecimal["tac"] == 20213


def test_routeros_antenna_status_normalizes_static_fields():
    assert normalize_routeros_antenna_mode("external") == "External"
    assert normalize_routeros_antenna_mode("internal") == "Internal"
    assert normalize_routeros_antenna_mode("auto") == "Automatic"
    assert normalize_routeros_antenna_mode("both") == "External"
    assert normalize_routeros_antenna_mode("none") == "Internal"
    assert normalize_routeros_antenna_mode("main=external,div=internal") == "main=external,div=internal"

    result = routeros_antenna_status({"external-antenna-selected": "external"})

    assert result == {
        "antenna": "External",
        "antenna_mode": "External",
        "antenna_mode_source": "routeros_static",
    }


def test_routeros_antenna_status_maps_routeros_both_to_external_antennas():
    result = routeros_antenna_status({"external-antenna-selected": "both"})

    assert result == {
        "antenna": "External",
        "antenna_mode": "External",
        "antenna_mode_source": "routeros_static",
    }


def test_parse_routeros_lte_monitor_row_preserves_antenna_when_present_in_row():
    result = parse_routeros_lte_monitor_row({"status": "connected", "external-antenna-selected": "internal"})

    assert result["antenna"] == "Internal"
    assert result["antenna_mode"] == "Internal"
    assert result["antenna_mode_source"] == "routeros_static"


def test_parse_routeros_lte_monitor_row_prefers_explicit_earfcn_without_inventing_cell_id():
    row = {
        "status": "running",
        "primary-band": "B1@15Mhz earfcn: 250 phy-cellid: 303",
        "earfcn": "275",
        "enb-id": "90001",
        "phy-cellid": "303",
    }

    result = parse_routeros_lte_monitor_row(row)

    assert result["primary_band"] == 1
    assert result["earfcn"] == 275
    assert result["pci"] == 303
    assert result["enb_id"] == 90001
    assert "cell_id" not in result
    assert result["radio_connected"] is True


def test_parse_routeros_lte_monitor_row_never_treats_earfcn_as_primary_band():
    row = {
        "status": "connected",
        "primary-band": "earfcn: 6400 phy-cellid: 319 LTE BAND 20 @10Mhz",
        "earfcn": "6400",
        "phy-cellid": "319",
    }

    result = parse_routeros_lte_monitor_row(row)

    assert result["primary_band"] == 20
    assert result["earfcn"] == 6400
    assert result["pci"] == 319
    assert result["active_band_set"] == [20]


def test_parse_routeros_lte_monitor_row_rejects_earfcn_only_primary_band_text():
    row = {
        "status": "connected",
        "primary-band": "earfcn: 6400 phy-cellid: 319",
        "earfcn": "6400",
        "phy-cellid": "319",
    }

    result = parse_routeros_lte_monitor_row(row)

    assert "primary_band" not in result
    assert result["earfcn"] == 6400
    assert result["pci"] == 319


def test_parse_routeros_lte_monitor_row_rejects_unsigned_earfcn_sentinel():
    row = {
        "status": "connected",
        "primary-band": "B20@10Mhz",
        "earfcn": "4294967295",
        "phy-cellid": "319",
    }

    result = parse_routeros_lte_monitor_row(row)

    assert result["primary_band"] == 20
    assert "earfcn" not in result
    assert result["pci"] == 319
    assert result["_routeros_invalid_earfcn"] is True


def test_parse_qeng_servingcell_rejects_unsigned_earfcn_sentinel() -> None:
    raw = '+QENG: "servingcell","CONNECT","LTE","FDD",220,05,19601,303,4294967295,20,5,5,4EF5,-84,-14,-48,12,120,-'

    result = parse_qeng_servingcell(raw)

    assert result["earfcn"] == 0
    assert result["band"] == 20


def test_parse_qeng_servingcell_handles_live_quectel_lte_row() -> None:
    raw = '+QENG: "servingcell","CONNECT","LTE","FDD",220,05,19601,303,1500,3,5,5,4EF5,-84,-14,-48,12,120,-'

    result = parse_qeng_servingcell(raw)

    assert result["state"] == "CONNECT"
    assert result["rat"] == "LTE"
    assert result["mcc"] == 220
    assert result["mnc"] == 5
    assert result["cellid"] == 19601
    assert result["pci"] == 303
    assert result["earfcn"] == 1500
    assert result["band"] == 3
    assert result["dl_bw_rb"] == 5
    assert result["ul_bw_rb"] == 5
    assert result["tac"] == "4EF5"
    assert result["rsrp"] == -84.0
    assert result["rsrq"] == -14.0
    assert result["rssi"] == -48.0
    assert result["sinr"] == 12.0
    assert result["cqi"] == 0
    assert result["tx_power"] == "-"


def test_parse_qeng_servingcell_rejects_error_and_out_of_range_cqi() -> None:
    assert parse_qeng_servingcell("ERROR") == {}
    raw = '+QENG: "servingcell","NOCONN","LTE","FDD",220,05,19601,303,1500,3,5,5,4EF5,-84,-14,-48,12,99,7'
    assert parse_qeng_servingcell(raw)["cqi"] == 0
    assert parse_qeng_servingcell(raw)["tx_power"] == "7"
    assert "trans_mode" not in parse_qeng_servingcell(raw)


def test_parse_qeng_servingcell_extracts_explicit_transmission_mode_only() -> None:
    raw = (
        '+QENG: "servingcell","CONNECT","LTE","FDD",220,05,19601,303,1500,3,5,5,'
        '4EF5,-84,-14,-48,12,10,-,TM[4]\r\nOK'
    )
    assert parse_qeng_servingcell(raw)["trans_mode"] == "TM4"

    named = (
        '+QENG: "servingcell","CONNECT","LTE","FDD",220,05,19601,303,1500,3,5,5,'
        '4EF5,-84,-14,-48,12,10,-,transmode=3\r\nOK'
    )
    assert parse_qeng_servingcell(named)["trans_mode"] == "TM3"


def test_rb_to_mhz_accepts_encoded_index_and_actual_rb_count() -> None:
    assert rb_to_mhz(5) == 20.0
    assert rb_to_mhz(100) == 20.0
    assert rb_to_mhz(50) == 10.0
    assert rb_to_mhz(999) == 0.0
