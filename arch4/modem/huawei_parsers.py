"""Pure Huawei HiLink parser helpers.

No HTTP/session state belongs here. The backend owns I/O; this module owns
small deterministic conversions that can be tested without a modem.
"""

from __future__ import annotations

import re
from math import isfinite
import xml.etree.ElementTree as ET
from typing import Any, Optional

from .lte_frequency import lte_channel_from_reported_value

BAND_TO_BIT = {
    1: 0x1, 2: 0x2, 3: 0x4, 4: 0x8,
    5: 0x10, 7: 0x40, 8: 0x80, 12: 0x800,
    13: 0x1000, 17: 0x10000, 18: 0x20000, 19: 0x40000,
    20: 0x80000, 25: 0x1000000, 26: 0x2000000, 28: 0x8000000,
    32: 0x80000000, 38: 0x2000000000, 40: 0x8000000000,
    41: 0x10000000000, 42: 0x20000000000, 43: 0x40000000000,
}

ALL_BANDS_HEX = "7FFFFFFFFFFFFFFF"

BW_TO_MHZ = {
    "1400KHz": 1.4, "1.4MHz": 1.4, "3MHz": 3, "5MHz": 5,
    "10MHz": 10, "15MHz": 15, "20MHz": 20,
}

NETWORK_TYPE_MAP = {
    "0": "No Service",
    "1": "GSM (2G)",
    "2": "GPRS (2G)",
    "3": "EDGE (2G)",
    "41": "UMTS (3G)",
    "42": "HSDPA (3G)",
    "43": "HSUPA (3G)",
    "44": "HSPA (3G)",
    "45": "HSPA+ (3.5G)",
    "46": "DC-HSPA+ (3.5G)",
    "101": "LTE (4G)",
    "1011": "LTE (4G)",
    "111": "NR (5G)",
}

TRANSMODE_MAP = {
    "1": "SISO",
    "2": "TX Diversity",
    "3": "OL-MIMO",
    "4": "CL-MIMO",
    "7": "Beamforming",
}


def parse_b818_cqi(raw: str) -> Optional[int]:
    text = str(raw or "").strip()
    if not text or text == "32639":
        return None
    try:
        value = int(text)
    except Exception:
        return None
    if 0 <= value <= 15:
        return value
    return None


def dl_mcs_to_tbs(mcs: int) -> Optional[int]:
    if mcs == 0:
        return 0
    if mcs == 1:
        return 2
    if mcs == 2:
        return 4
    if mcs == 3:
        return 6
    if mcs == 4:
        return 8
    if 5 <= mcs <= 20:
        return mcs + 5
    if 21 <= mcs <= 27:
        return mcs + 6
    if mcs == 28:
        return 8
    if mcs == 29:
        return 15
    if mcs == 30:
        return 24
    if mcs == 31:
        return 27
    return None


def ul_mcs_to_tbs(mcs: int) -> Optional[int]:
    if 0 <= mcs <= 9:
        return mcs
    if 10 <= mcs <= 16:
        return mcs - 1
    if 17 <= mcs <= 31:
        return mcs - 2
    return None


def bands_to_hex(bands: list[int]) -> str:
    if not bands:
        raise ValueError("huawei_lte_band_mask_requires_explicit_bands")
    unknown = sorted({int(band) for band in bands if int(band) not in BAND_TO_BIT})
    if unknown:
        raise ValueError(
            "huawei_lte_band_mask_unsupported:" + ",".join(map(str, unknown))
        )
    mask = 0
    for band in bands:
        mask |= BAND_TO_BIT[int(band)]
    return format(mask, "x")


def hex_to_bands(hex_str: str, *, auto_is_empty: bool = True) -> list[int]:
    text = str(hex_str or "").strip()
    if not text:
        return []
    if text.lower().startswith("0x"):
        text = text[2:]
    if auto_is_empty and text.upper() == ALL_BANDS_HEX.upper():
        return []
    try:
        mask = int(text, 16)
    except ValueError:
        return []
    return [band for band, bit in sorted(BAND_TO_BIT.items()) if mask & bit]


def extract_xml_mask_values(xml_str: str, tag: str) -> list[str]:
    values: list[str] = []
    try:
        root = ET.fromstring(xml_str)
        for node in root.findall(f".//{tag}"):
            node_text = str(node.text or "").strip()
            if node_text:
                values.append(node_text)
            for value_node in node.findall(".//Value"):
                value_text = str(value_node.text or "").strip()
                if value_text:
                    values.append(value_text)
    except ET.ParseError:
        pass
    if not values:
        block_match = re.search(
            rf"<{tag}\b[^>]*>(.*?)</{tag}>",
            str(xml_str or ""),
            re.IGNORECASE | re.DOTALL,
        )
        if block_match:
            block = block_match.group(1)
            for match in re.findall(r"<Value>\s*([^<]+?)\s*</Value>", block, re.IGNORECASE):
                text = str(match or "").strip()
                if text:
                    values.append(text)
            if not values:
                plain = re.sub(r"<[^>]+>", " ", block).strip()
                if plain:
                    values.append(plain)
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return deduped


def parse_band_list_text(raw_text: str) -> list[int]:
    text = str(raw_text or "").strip()
    if not text:
        return []
    normalized = text[2:] if text.lower().startswith("0x") else text
    if re.fullmatch(r"[0-9a-fA-F]+", normalized):
        return []
    bands: list[int] = []
    for part in re.findall(r"\d+", text):
        try:
            band = int(part)
        except Exception:
            continue
        if 0 < band <= 256:
            bands.append(band)
    return sorted(set(bands)) if bands else []


def is_all_bands_mask(raw_value: Any) -> bool:
    text = str(raw_value or "").strip()
    if not text:
        return False
    if text.lower().startswith("0x"):
        text = text[2:]
    return text.upper() == ALL_BANDS_HEX.upper()


def parse_signal_val(raw: str) -> float:
    if not raw:
        return 0.0
    cleaned = (
        raw.replace("dBm", "")
        .replace("dB", "")
        .replace(">=", "")
        .replace("<=", "")
        .replace(">", "")
        .replace("<", "")
        .replace("Unknown", "0")
        .strip()
    )
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_bandwidth_mhz(raw: str) -> Optional[float]:
    text = str(raw or "").strip()
    if not text:
        return None
    if text in BW_TO_MHZ:
        return BW_TO_MHZ[text]
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def huawei_optional_nr_read(xml: str, tag: str):
    """One same-reply, explicitly scoped NR quantity; no RAT inference.

    The H122-373 response shape is model-level evidence; firmware was not
    supplied. Existing DL bandwidth retains its numeric-MHz contract. Other
    frequency and width reads require explicit units. MCS is a raw index,
    not modulation; the UL producer also accepts an integer in its NR tag.
    """
    allowed = {"nrdlbandwidth", "nrulbandwidth", "nrdlfreq", "nrulfreq",
               "nrdlmcs", "nrulmcs"}
    if tag not in allowed or not isinstance(xml, str) or len(xml) > 1048576 or "<!" in xml:
        raise ValueError("huawei_optional_nr_shape")
    root = ET.fromstring(xml)
    rows = root.findall(tag)
    if not rows:
        return None
    if root.tag != "response" or len(rows) != 1 or list(rows[0]):
        raise ValueError("huawei_optional_nr_scalar_conflict")
    raw = (rows[0].text or "").strip()
    if not raw:
        return None
    if len(raw) > 180:
        raise ValueError("huawei_optional_nr_size")
    # Attribute access is intentionally outside the fixed codec vocabulary.
    # Reject attributes without extending that shared execution boundary.
    opening = re.search("<" + tag + r"(?=[\s>])([^>]*)>", xml)
    if opening is not None and opening.group(1).strip():
        raise ValueError("huawei_optional_nr_scalar_attributes")
    if tag in {"nrdlmcs", "nrulmcs"}:
        # The native tag supplies NR/UL scope for its established scalar shape.
        if tag == "nrulmcs" and re.fullmatch(r"[0-9]{1,2}", raw) and int(raw) <= 31:
            return int(raw)
        # Code1 is an independent optional component. Its bad value must not
        # erase a clear Code0, but every label/token must form one full record.
        pattern = (r"(?:NR)?mcsDownCarrier1Code0:\s*([0-9]{1,2})(?:\s+(?:NR)?mcsDownCarrier1Code1:\s*([^\s]{1,40}))?"
                   if tag == "nrdlmcs" else r"(?:NR)?mcsUpCarrier1:\s*([0-9]{1,2})")
        match = re.fullmatch(pattern, raw)
        if not match or int(match.group(1)) > 31:
            raise ValueError("huawei_optional_nr_mcs_index")
        return int(match.group(1))
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(MHz|kHz|Hz)?", raw, re.IGNORECASE)
    if not match or match.group(2) is None and tag != "nrdlbandwidth":
        raise ValueError("huawei_optional_nr_unit")
    unit = (match.group(2) or "MHz").lower()
    value = float(match.group(1)) / {"mhz": 1.0, "khz": 1000.0, "hz": 1000000.0}[unit]
    maximum = 400.0 if tag in {"nrdlbandwidth", "nrulbandwidth"} else 100000.0
    if not isfinite(value) or not 0.0 < value <= maximum:
        raise ValueError("huawei_optional_nr_range")
    return value


def parse_int(raw: str) -> int:
    text = str(raw or "").strip()
    if not text:
        return 0
    try:
        return int(text, 0)
    except ValueError:
        pass
    match = re.search(r"(0x[0-9a-fA-F]+|-?\d+)", text)
    if not match:
        return 0
    try:
        return int(match.group(1), 0)
    except ValueError:
        return 0


def parse_earfcn_dl(raw: str) -> int:
    if not raw:
        return 0
    match = re.search(r"DL:\s*(\d+)", raw)
    if match:
        return int(match.group(1))
    try:
        return int(raw)
    except ValueError:
        return 0


def parse_earfcn_ul(raw: str) -> int:
    if not raw:
        return 0
    match = re.search(r"UL:\s*(\d+)", raw)
    if match:
        return int(match.group(1))
    return 0


def parse_serving_earfcns(raw: str, *, band: int, dl_frequency_100khz: str = "",
                          ul_frequency_100khz: str = "") -> tuple[int, int]:
    """Adapt Huawei standard channels or same-reply frequency-form tokens.

    B818 B28 can put100kHz frequencies in the field named `earfcn`.
    Require the separate native frequency field to confirm that representation;
    never reinterpret an arbitrary out-of-band token by numerical resemblance.
    Complete unresolved input is retained for ModemLive's non-blocking audit.
    """
    def channel(token: int, frequency_raw: str, *, uplink: bool) -> int:
        try:
            frequency = int(frequency_raw)
        except (TypeError, ValueError):
            frequency = 0
        return lte_channel_from_reported_value(band, token, frequency, uplink=uplink)

    return (channel(parse_earfcn_dl(raw), dl_frequency_100khz, uplink=False),
            channel(parse_earfcn_ul(raw), ul_frequency_100khz, uplink=True))


def parse_freq_mhz(raw: str) -> Optional[float]:
    if not raw:
        return None
    cleaned = str(raw).strip().replace(" ", "")
    if not cleaned or cleaned == "0":
        return None
    try:
        lowered = cleaned.lower()
        if lowered.endswith("mhz"):
            numeric = float(lowered[:-3])
        elif lowered.endswith("khz"):
            numeric = float(lowered[:-3]) / 1000
        elif lowered.endswith("hz"):
            numeric = float(lowered[:-2]) / 1_000_000
        else:
            numeric = float(lowered)
            if numeric >= 1_000_000_000:
                numeric /= 1_000_000
            elif numeric >= 100_000:
                numeric /= 1000
        return numeric if isfinite(numeric) else None
    except ValueError:
        return None


def parse_dl_mcs(raw: str) -> tuple[int, int]:
    code0 = code1 = 0
    if not raw:
        return code0, code1
    m0 = re.search(r"Code0:\s*(\d+)", raw)
    if m0:
        code0 = int(m0.group(1))
    m1 = re.search(r"Code1:\s*(\d+)", raw)
    if m1:
        code1 = int(m1.group(1))
    return code0, code1


def parse_ul_mcs(raw: str) -> int:
    if not raw:
        return 0
    match = re.search(r"Carrier1:\s*(\d+)", raw)
    if match:
        return int(match.group(1))
    cleaned = raw.replace("mcsUpCarrier1:", "").replace("NRmcsUpCarrier1:", "").strip()
    try:
        return int(cleaned)
    except ValueError:
        return 0


# Existing HiLink read conversions; I/O, retention and freshness stay in the backend.
def wifi_ssids(xml, cached_status):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return cached_status
    if root.find('code') is not None:
        return cached_status
    ssids_el = root.find('Ssids')
    if ssids_el is None:
        pass
        return cached_status
    wifi_24 = None
    wifi_5 = None
    for ssid_el in ssids_el.findall('Ssid'):
        guest_el = ssid_el.find('wifiisguestnetwork')
        if guest_el is not None and (guest_el.text or '').strip() == '1':
            continue
        idx_el = ssid_el.find('Index')
        id_el = ssid_el.find('ID')
        enable_el = ssid_el.find('WifiEnable')
        if enable_el is None:
            continue
        enabled = enable_el.text.strip() == '1' if enable_el.text else False
        ssid_id = id_el.text.strip() if id_el is not None and id_el.text else ''
        if '.Radio.1.' in ssid_id or '.Wifi.Radio.1.' in ssid_id:
            wifi_24 = bool((wifi_24 or False) or enabled)
            continue
        if '.Radio.2.' in ssid_id or '.Wifi.Radio.2.' in ssid_id:
            wifi_5 = bool((wifi_5 or False) or enabled)
            continue
        idx = idx_el.text.strip() if idx_el is not None and idx_el.text else ''
        if idx == '0':
            wifi_24 = enabled
        elif idx in {'1', '5'}:
            wifi_5 = enabled
    if wifi_24 is None and wifi_5 is None:
        return cached_status
    overall = bool(wifi_24 or wifi_5)
    return (overall, wifi_24, wifi_5)

def wifi_switches(xml) -> Optional[tuple[bool, Optional[bool], Optional[bool]]]:
    if not xml:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    if root.find('code') is not None:
        return None
    radios = root.find('radios')
    if radios is None:
        return None
    wifi_24: Optional[bool] = None
    wifi_5: Optional[bool] = None
    for row in radios.findall('radio'):
        radio_id = (row.findtext('ID') or '').strip()
        index = (row.findtext('index') or '').strip()
        enabled_text = (row.findtext('wifienable') or '').strip()
        if enabled_text not in {'0', '1'}:
            continue
        enabled = enabled_text == '1'
        if '.Radio.1.' in radio_id or '.Wifi.Radio.1.' in radio_id or (not radio_id and index == '0'):
            wifi_24 = enabled
        elif '.Radio.2.' in radio_id or '.Wifi.Radio.2.' in radio_id or (not radio_id and index == '1'):
            wifi_5 = enabled
    if wifi_24 is None and wifi_5 is None:
        return None
    return (bool(wifi_24 or wifi_5), wifi_24, wifi_5)


# Existing antenna interpretation, including the final two-radio label.
def _label(val: str) -> str:
    if val is None:
        return ''
    v = str(val).strip()
    if not v:
        return ''
    if v.isdigit():
        return {'0': 'Automatic', '1': 'External', '2': 'Internal', '3': 'Mixed'}.get(v, v)
    v_low = v.lower()
    if 'auto' in v_low:
        return 'Automatic'
    if 'extern' in v_low:
        return 'External'
    if 'intern' in v_low:
        return 'Internal'
    if 'mixed' in v_low:
        return 'Mixed'
    return v

def _pair_label(v1: str, v2: str) -> str:
    left = str(v1 or '').strip()
    right = str(v2 or '').strip()
    if not left or not right:
        return ''
    if left == '0' and right == '0':
        return 'Internal'
    if left == '1' and right == '1':
        return 'External'
    if (left, right) in {('0', '1'), ('1', '0')}:
        return 'Mixed'
    return ''

def _set_type_label(val: str) -> str:
    token = str(val or '').strip()
    if token == '0':
        return 'Automatic'
    if token == '1':
        return 'External'
    if token == '2':
        return 'Internal'
    if token == '3':
        return 'Mixed'
    return _label(token)

def _from_xml(xml: str) -> str:
    set_type = xml_text(xml, 'antennasettype') or xml_text(xml, 'AntennaSetType') or xml_text(xml, 'AntennaTypes')
    if set_type:
        label = _set_type_label(set_type)
        if label:
            return label
    pair = _pair_label(xml_text(xml, 'antenna1type') or xml_text(xml, 'Antenna1Type'), xml_text(xml, 'antenna2type') or xml_text(xml, 'Antenna2Type'))
    if pair:
        return pair
    pair = _pair_label(xml_text(xml, 'antenna1_status') or xml_text(xml, 'Antenna1Status'), xml_text(xml, 'antenna2_status') or xml_text(xml, 'Antenna2Status'))
    if pair:
        return pair
    for tag in ('AntennaStatus', 'AntennaType', 'AntennaMode', 'antenna_status', 'antenna_type', 'antenna_mode', 'Antenna', 'antenna'):
        val = xml_text(xml, tag)
        if val:
            label = _label(val)
            if label:
                return label
    try:
        root = ET.fromstring(xml)
        candidates = []
        for el in root.iter():
            if not el.text:
                continue
            tag = el.tag.lower()
            if 'antenna' in tag:
                candidates.append((tag, el.text.strip()))
        for tag, val in candidates:
            if any((k in tag for k in ('status', 'type', 'mode'))):
                label = _label(val)
                if label:
                    return label
        if candidates:
            label = _label(candidates[0][1])
            if label:
                return label
    except Exception as exc:
        pass
        pass
    return ''

def huawei_antenna(xml):
    if not xml:
        return ''
    status = _from_xml(xml)
    a1 = xml_text(xml, 'Antenna1')
    a2 = xml_text(xml, 'Antenna2')
    if not a1:
        a1 = xml_text(xml, 'antenna1')
    if not a2:
        a2 = xml_text(xml, 'antenna2')
    label = _label(status)
    l1 = _label(a1)
    l2 = _label(a2)
    if l1 or l2:
        if l1 and l2 and (l1 == l2):
            label = l1
        elif l1 and l2:
            label = f'{l1}/{l2}'
        else:
            label = l1 or l2
    return label
