from arch4.modem.zte_parsers import (
    ZTE_BAND_MASKS,
    decode_band_mask,
    dedupe_bands,
    first_non_zero,
    is_5g_only_mode,
    normalize_imsi,
    parse_antenna_mode_text,
    parse_band_values,
    parse_dbm_metric,
    parse_duration,
    parse_int,
    parse_metric_float,
    parse_nr_band,
    parse_wifi_enabled,
)


def test_zte_band_and_metric_parsers_cover_goform_formats():
    mask = ZTE_BAND_MASKS[1] | ZTE_BAND_MASKS[3] | ZTE_BAND_MASKS[20]
    assert decode_band_mask(str(mask)) == [1, 3, 20]
    assert parse_band_values("LTE B1+B3+B20") == [1, 3, 20]
    assert dedupe_bands([3, "3", 20, 0, 1]) == [3, 20, 1]
    assert parse_metric_float("-88dBm") == -88.0
    assert parse_dbm_metric("88") == -88.0
    assert parse_nr_band("n78") == 78


def test_zte_scalar_parsers_cover_status_fields():
    assert parse_int("0x10") == 16
    assert parse_int("abc-7xyz") == -7
    assert parse_duration("01:02:03") == 3723.0
    assert parse_duration("12.5") == 12.5
    assert first_non_zero(0.0, "0", -91.5) == -91.5
    assert normalize_imsi("220051234567890") == "220051234567890"
    assert normalize_imsi("imsi 220051234567890") == ""


def test_zte_boolean_text_parsers_are_simple_and_stable():
    assert parse_wifi_enabled({"wifiEnabled": "1"}) is True
    assert parse_wifi_enabled({"wifiEnabled": "0"}) is False
    assert parse_wifi_enabled({}) is None
    assert parse_antenna_mode_text({"antenna_mode": "external"}) == "External"
    assert parse_antenna_mode_text({"antenna_type": "int"}) == "Internal"
    assert is_5g_only_mode({"network_type": "NR only"}) is True
    assert is_5g_only_mode({"network_type": "LTE"}) is False


def test_zwrt_native_mask_has_separate_codec_and_no_goform_dependency():
    from pathlib import Path
    from arch4.modem.zte_parsers import encode_zwrt_lte_mask, decode_zwrt_lte_mask, ZWRT_LTE_BANDS
    from arch4.modem.zte_zwrt import ZteZwrtUbusModem
    native = [1, 3, 7, 8, 20, 28, 38]
    assert encode_zwrt_lte_mask(native) == 0x20080800c5
    assert ZteZwrtUbusModem._decode_band_mask('0x20080800c5') == native
    for band in ZWRT_LTE_BANDS:
        assert decode_zwrt_lte_mask(hex(encode_zwrt_lte_mask([band]))) == [band]
    assert decode_band_mask('0x20080800c5') == [1, 3, 7, 8, 20, 28, 41]
    assert 'ZTE_BAND_MASKS' not in (Path(__file__).parents[1]/'modem/zte_zwrt.py').read_text(encoding='utf-8')
