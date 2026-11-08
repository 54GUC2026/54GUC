import pytest

from arch4.modem.huawei_parsers import (
    ALL_BANDS_HEX,
    bands_to_hex,
    extract_xml_mask_values,
    hex_to_bands,
    parse_band_list_text,
    parse_b818_cqi,
    parse_dl_mcs,
    parse_earfcn_dl,
    parse_earfcn_ul,
    parse_freq_mhz,
    parse_signal_val,
    parse_ul_mcs,
)


def test_huawei_band_mask_round_trips_and_auto_is_empty():
    assert bands_to_hex([1, 3, 20]) == "80005"
    assert hex_to_bands("80005") == [1, 3, 20]
    assert hex_to_bands(ALL_BANDS_HEX) == []
    assert hex_to_bands(ALL_BANDS_HEX, auto_is_empty=False)


def test_huawei_xml_mask_values_parse_nested_values_and_dedupe():
    xml = "<response><LTEBand><Value>80005</Value><Value>80005</Value><Value>4</Value></LTEBand></response>"
    assert extract_xml_mask_values(xml, "LTEBand") == ["80005", "4"]


def test_huawei_text_and_signal_parsers_cover_live_formats():
    assert parse_band_list_text("B1,B3,B20") == [1, 3, 20]
    assert parse_band_list_text("80005") == []
    assert parse_signal_val(">=12dB") == 12.0
    assert parse_earfcn_dl("DL:525 UL:18525") == 525
    assert parse_earfcn_ul("DL:525 UL:18525") == 18525
    assert parse_freq_mhz("2160000kHz") == 2160
    assert parse_freq_mhz("2160000") == 2160


def test_huawei_scheduler_metric_parsers_cover_cqi_and_mcs():
    assert parse_b818_cqi("12") == 12
    assert parse_b818_cqi("32639") is None
    assert parse_b818_cqi("127") is None
    assert parse_dl_mcs("mcsDownCarrier1Code0:16 mcsDownCarrier1Code1:11") == (16, 11)
    assert parse_ul_mcs("mcsUpCarrier1:9") == 9


@pytest.mark.parametrize("raw", ["1864.5MHz", "1864500kHz", "1864500000Hz",
    "1864.5", "1864500", "1864500000", " 1864.5 MHz "])
def test_frequency_reading_keeps_sub_mhz_precision(raw):
    assert parse_freq_mhz(raw) == 1864.5


@pytest.mark.parametrize("raw", ["NaN", "inf", "-infMHz", "1e999kHz", "wrong", ""])
def test_frequency_reading_rejects_nonfinite_or_unreadable_input(raw):
    assert parse_freq_mhz(raw) is None
