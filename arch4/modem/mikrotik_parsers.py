"""Pure MikroTik RouterOS parser helpers.

Backend sessions own transport and timing. This module owns string/row
normalization so parser behavior can be tested without a live RouterOS API.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from arch4.interfaces import band_signature, normalize_band_list
from arch4.modem.lte_frequency import lte_band_from_dl_earfcn, lte_earfcn_bounds


def _valid_lte_band_number(value: int) -> bool:
    return 0 < int(value or 0) <= 256


def _valid_lte_earfcn(value: int) -> bool:
    """Return True for plausible LTE EARFCN values.

    RouterOS can briefly expose unsigned sentinel values such as 4294967295
    while the modem/driver is transitioning. Those must not become serving
    cell truth or they create fake structural changes in the rest of the app.
    """

    try:
        parsed = int(value or 0)
    except Exception:
        return False
    return 0 < parsed <= 262143


def decode_routeros_row(row: Mapping[Any, Any]) -> dict[str, str]:
    """Decode one RouterOS API row into string keys and string values."""

    decoded: dict[str, str] = {}
    for k, v in row.items():
        key = k.decode("utf-8") if isinstance(k, bytes) else str(k)
        val = v.decode("utf-8") if isinstance(v, bytes) else str(v) if v is not None else ""
        decoded[key] = val
    return decoded


def parse_routeros_lte_bands(value: Any) -> List[int]:
    """Parse RouterOS LTE band text without accepting unrelated plain numbers."""

    if value in (None, "", [], {}):
        return []
    if isinstance(value, (list, tuple, set)):
        bands: List[int] = []
        for item in value:
            for band in parse_routeros_lte_bands(item):
                if band not in bands:
                    bands.append(band)
            try:
                band = int(item or 0)
            except Exception:
                band = 0
            if band > 0 and band not in bands:
                bands.append(band)
        return normalize_band_list(bands)

    text = str(value or "")
    bands: List[int] = []
    patterns = (
        r"\bLTE\s+BAND\s*(\d{1,3})\b",
        r"\bBAND\s*(\d{1,3})\b",
        r"\bB\s*(\d{1,3})\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            try:
                band = int(match.group(1))
            except Exception:
                band = 0
            if _valid_lte_band_number(band) and band not in bands:
                bands.append(band)
    return normalize_band_list(bands)


def routeros_monitor_active_bands(row: Mapping[str, Any], primary_band: int = 0) -> List[int]:
    """Extract only physically observed PCell/SCell bands."""

    bands: List[int] = [int(primary_band)] if int(primary_band or 0) > 0 else []
    for band in routeros_monitor_ca_bands(row):
        if _valid_lte_band_number(band) and band not in bands:
            bands.append(band)
    return normalize_band_list(bands)


def routeros_monitor_ca_bands(row: Mapping[str, Any]) -> List[int]:
    """Extract explicit RouterOS CA/SCC carrier bands.

    These fields define MikroTik raw CA state: any parsed CA/SCC carrier means
    raw CA is active. Generic band-list fields are excluded because they can
    describe the allowed/current band set without proving a secondary carrier.
    """

    return normalize_band_list([
        detail["band"]
        for detail in routeros_monitor_carrier_details(row)
        if not bool(detail.get("is_pcell", False))
    ])


def routeros_monitor_ca_entry_count(row: Mapping[str, Any]) -> int:
    """Count explicit RouterOS CA/SCC carrier entries without band de-duplication."""

    return sum(
        1
        for detail in routeros_monitor_carrier_details(row)
        if not bool(detail.get("is_pcell", False))
    )


def _routeros_ca_key(value: Any) -> bool:
    key = str(value or "").strip().lower().replace("_", "-")
    return key != "primary-band" and any(
        token in key
        for token in (
            "ca-band",
            "secondary-band",
            "scc",
            "component-carrier",
            "carrier-aggregation",
        )
    )


def _routeros_value_items(value: Any) -> Iterable[str]:
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _routeros_value_items(item)
        return
    text = str(value or "").strip()
    if text:
        yield text


def _routeros_context_band(value: Any) -> int:
    bands = parse_routeros_lte_bands(value)
    if bands:
        return int(bands[0])
    text = str(value or "").strip()
    match = re.match(r"^(\d{1,3})(?:\s*(?:@|MHz|Mhz|mhz)\b|$)", text)
    if not match:
        return 0
    band = int(match.group(1))
    return band if _valid_lte_band_number(band) else 0


def _routeros_carrier_segments(value: Any) -> list[str]:
    """Split one RouterOS carrier value while preserving same-band entries."""

    segments: list[str] = []
    marker = re.compile(
        r"(?:\bLTE\s+BAND\s*\d{1,3}\b|\bBAND\s*\d{1,3}\b|\bB\s*\d{1,3}\b|\b\d{1,3}(?=\s*@))",
        flags=re.IGNORECASE,
    )
    for item in _routeros_value_items(value):
        matches = list(marker.finditer(item))
        if len(matches) <= 1:
            segments.append(item)
            continue
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(item)
            segment = item[match.start():end].strip(" ,;+\r\n\t")
            if segment:
                segments.append(segment)
    return segments


def _routeros_carrier_detail(value: Any, *, is_pcell: bool) -> dict[str, Any] | None:
    text = str(value or "").strip()
    band = _parse_routeros_primary_band(text) if is_pcell else _routeros_context_band(text)
    if band <= 0:
        return None
    detail: dict[str, Any] = {
        "band": band,
        "state": 0 if is_pcell else 1,
        "is_pcell": bool(is_pcell),
    }
    bandwidth = re.search(r"@(\d+(?:\.\d+)?)\s*Mhz", text, re.IGNORECASE)
    if bandwidth:
        detail["bandwidth_mhz"] = float(bandwidth.group(1))
    earfcn = re.search(r"earfcn[:=]\s*(\d+)", text, re.IGNORECASE)
    if earfcn and _valid_lte_earfcn(int(earfcn.group(1))):
        detail["earfcn"] = int(earfcn.group(1))
    pci = re.search(r"(?:phy-cellid|pci)[:=]\s*(\d+)", text, re.IGNORECASE)
    if pci:
        detail["pci"] = int(pci.group(1))
    return detail


def routeros_monitor_carrier_details(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return RouterOS-observed PCell/SCell rows in canonical Matrix language."""

    details: list[dict[str, Any]] = []
    for segment in _routeros_carrier_segments(row.get("primary-band", "")):
        detail = _routeros_carrier_detail(segment, is_pcell=True)
        if detail is not None:
            details.append(detail)
            break
    for key, value in row.items():
        if not _routeros_ca_key(key):
            continue
        for segment in _routeros_carrier_segments(value):
            if (
                "carrier-aggregation" in str(key or "").strip().lower().replace("_", "-")
                and segment.strip().lower()
                in {"true", "yes", "on", "active", "enabled", "1", "false", "no", "off", "inactive", "disabled", "none", "0"}
            ):
                continue
            detail = _routeros_carrier_detail(segment, is_pcell=False)
            if detail is not None:
                details.append(detail)
    return details


def routeros_monitor_ca_observation_status(row: Mapping[str, Any]) -> str:
    """Classify exact RouterOS CA evidence as active, inactive, or unparsed."""

    ca_values = [
        value
        for key, value in row.items()
        if _routeros_ca_key(key)
    ]
    if not ca_values:
        return "inactive"
    if any(
        not bool(detail.get("is_pcell", False))
        for detail in routeros_monitor_carrier_details(row)
    ):
        return "active"
    text = " ".join(_routeros_value_items(ca_values)).strip().lower()
    if not text or text in {"false", "no", "off", "inactive", "disabled", "none", "0"}:
        return "inactive"
    if text in {"true", "yes", "on", "active", "enabled", "1"}:
        return "active"
    return "unparsed"


def _parse_routeros_primary_band(value: Any) -> int:
    """Parse RouterOS ``primary-band`` without mistaking EARFCN for a band."""

    text = str(value or "").strip()
    if not text:
        return 0
    bands = parse_routeros_lte_bands(text)
    if bands:
        return int(bands[0])

    # Some RouterOS builds expose primary-band as a bare value like ``20`` or
    # ``20@10Mhz``. Accept that bare RouterOS form, but never accept
    # EARFCN-sized numbers.
    match = re.match(r"^\s*(\d{1,3})(?:\s*(?:@|MHz|Mhz|mhz)\b|$)", text)
    if not match:
        return 0
    try:
        band = int(match.group(1))
    except ValueError:
        return 0
    return band if _valid_lte_band_number(band) else 0


def routeros_monitor_ca_composition_signature(row: Mapping[str, Any]) -> str:
    """Build a diagnostic PCell+SCell signature without de-duplicating bands."""

    parts: List[str] = []
    for detail in routeros_monitor_carrier_details(row):
        role = "P" if bool(detail.get("is_pcell", False)) else "CA"
        part = [role, f"B{int(detail['band'])}"]
        if int(detail.get("earfcn", 0) or 0) > 0:
            part.append(f"earfcn={int(detail['earfcn'])}")
        if int(detail.get("pci", 0) or 0) > 0:
            part.append(f"pci={int(detail['pci'])}")
        parts.append("@".join(part))
    return " + ".join(parts)


def _parse_int_text(value: Any) -> int | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    try:
        if raw.lower().startswith("0x") or re.search(r"[a-fA-F]", raw):
            return int(raw, 16)
        return int(raw)
    except ValueError:
        return None


def _parse_routeros_modulation_qm(value: Any) -> int | None:
    """Translate one optional RouterOS modulation label to LTE Qm."""

    text = re.sub(r"[^A-Z0-9]", "", str(value or "").strip().upper())
    return {
        "QPSK": 2,
        "16QAM": 4,
        "64QAM": 6,
        "256QAM": 8,
    }.get(text)


def parse_routeros_duration_seconds(raw: str) -> float | None:
    """Decode a complete native duration; never accept a partial reset proof."""
    text = str(raw).strip()
    # Native durations are short. Reject corrupt input before integer/float
    # conversion so a malformed router reply cannot overflow the reader.
    if len(text) > 64:
        return None
    if text and re.fullmatch(r"(?:\d+w)?(?:\d+d)?(?:\d+h)?(?:\d+m)?(?:\d+s)?", text):
        units = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
        return float(sum(int(value) * units[unit]
                         for value, unit in re.findall(r"(\d+)([wdhms])", text)))
    clock = re.fullmatch(r"(?:(\d+)w)?(?:(\d+)d)?(\d+):([0-5]\d):([0-5]\d)", text)
    if clock:
        weeks, days, hours, minutes, seconds = (int(value or 0) for value in clock.groups())
        return float(weeks * 604800 + days * 86400 + hours * 3600 + minutes * 60 + seconds)
    return None


def _routeros_nr_metric(value: Any, unit: str, minimum: float, maximum: float) -> float | None:
    """Exact native numeric token; magnitude never chooses units or scaling."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if len(text) > 48:
        return None
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*(?:" + re.escape(unit) + r")?", text, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    return number if minimum <= number <= maximum else None


def parse_routeros_nr_monitor_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Read explicit NR fields from one selected-interface monitor response.

    The witnessed RouterOS7.21.2 RG650E grammar is independent of LTE parsing.
    A partial field remains partial; no LTE identity, configured band or older
    request fills its gaps. Other profile selection and permissions stay with
    their existing owners. Missing/unknown is not an inactive observation.
    """
    result = dict.fromkeys(("nr5g_active", "nr_mode", "nr_band", "nr_pci", "nr_arfcn",
        "nr_rsrp_dbm", "nr_rsrq_db", "nr_sinr_db", "nr_bandwidth_mhz", "nr_cells"))
    kind = str(row.get("data-class") or "").strip().upper()
    if kind in {"LTE", "LTE-A", "LTE CA", "3G", "GSM", "WCDMA"}:
        result.update(nr5g_active=False, nr_mode="", nr_cells=[])
        return result
    if kind not in {"5G NSA", "5G SA"}:
        return result
    result["nr_mode"] = "NSA" if kind == "5G NSA" else "SA"
    for native, target, unit, low, high in (
        ("nr-rsrp", "nr_rsrp_dbm", "dBm", -160.0, -20.0),
        ("nr-rsrq", "nr_rsrq_db", "dB", -50.0, 10.0),
        ("nr-sinr", "nr_sinr_db", "dB", -50.0, 100.0),
    ):
        result[target] = _routeros_nr_metric(row.get(native), unit, low, high)
    # The retained public capture establishes NSA secondary-carrier syntax.
    # A native SA label can identify dedicated RF fields, but cannot turn an
    # NSA ca-band description into an SA primary cell or active attachment.
    if kind != "5G NSA":
        return result
    # No list-repr, nested mapping or primary-carrier guess. Each explicit NR
    # description is separate from the existing LTE CA composition and Pool.
    value = row.get("ca-band")
    if isinstance(value, list):
        value = ("\n".join(value) if len(value) <= 16
                 and all(isinstance(part, str) and "\n" not in part and "\r" not in part
                         for part in value) else None)
    cells = []
    if isinstance(value, str) and len(value) <= 4096:
        for match in re.finditer(
            r"(?<![A-Za-z0-9])n(\d{1,3})@([^\s,;]+)\s+"
            r"earfcn:\s*([^\s,;]+)\s+phy-cellid:\s*([^\s,;]+)", value, re.IGNORECASE):
            band, width, channel, pci = match.groups()
            band = int(band)
            band = band if 0 < band <= 1024 else None
            # Independent full tokens: a malformed optional PCI/width must
            # not erase a separately understood band, channel or RF metric.
            channel = int(channel) if re.fullmatch(r"[0-9]{1,7}", channel) else None
            pci = int(pci) if re.fullmatch(r"[0-9]{1,4}", pci) else None
            channel = channel if channel is not None and channel <= 3279165 else None
            pci = pci if pci is not None and pci <= 1007 else None
            width = (_routeros_nr_metric(width, "MHz", 0.01, 400.0)
                     if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?MHz", width, re.IGNORECASE) else None)
            cells.append({"role": "SCell", "band": band, "arfcn": channel,
                          "pci": pci, "bandwidth_mhz": width})
            if len(cells) >= 16:
                break
    if len(cells) == 1:
        cell = cells[0]
        result.update(nr_band=cell["band"], nr_arfcn=cell["arfcn"],
                      nr_pci=cell["pci"], nr_bandwidth_mhz=cell["bandwidth_mhz"])
        complete = all(cell[key] is not None for key in ("band", "arfcn", "pci", "bandwidth_mhz"))
        if not complete:
            return result
        result["nr_cells"] = cells
        for target in ("nr_rsrp_dbm", "nr_rsrq_db", "nr_sinr_db"):
            cell[target[3:]] = result[target]
        # Explicit current NR topology, measured RF and native registration
        # must agree. A 5G label, capability, or a retained slot alone cannot
        # create active NR or a negative recovery observation.
        if (str(row.get("status") or "").strip().lower() in {"connected", "running", "attached"}
                and all(result[key] is not None for key in
                        ("nr_rsrp_dbm", "nr_rsrq_db", "nr_sinr_db"))):
            result["nr5g_active"] = True
    elif cells and all(all(cell[key] is not None for key in
                         ("band", "arfcn", "pci", "bandwidth_mhz")) for cell in cells):
        # Multiple explicit rows remain separate; no serving identity or
        # aggregate signal association is chosen from their channel ranges.
        result["nr_cells"] = cells
    return result


def parse_routeros_nr_qeng(raw: str) -> Dict[str, Any]:
    """Known Quectel NSA7/9/11 and SA17 columns from one QENG reply.

    Width is the documented NR enum only in CONNECT. NCI/TAC and the
    firmware-dependent tail deliberately have no interpretation here.
    """
    result = dict.fromkeys(("nr5g_active", "nr_mode", "nr_band", "nr_pci", "nr_arfcn",
        "nr_rsrp_dbm", "nr_rsrq_db", "nr_sinr_db", "nr_bandwidth_mhz", "nr_cells"))
    if not isinstance(raw, str) or len(raw) > 16384 or "ERROR" in raw:
        return result
    rows = []
    for line in raw.splitlines():
        if line.strip().startswith("+QENG:"):
            rows.append([part.strip().strip('"') for part in line.split(":", 1)[1].split(",")])
    headers = [row for row in rows if row and row[0] == "servingcell"]
    if len(headers) != 1:
        return result
    header = headers[0]
    state = header[1] if len(header) > 1 else ""
    if state not in {"CONNECT", "NOCONN"}:
        return result
    coherent = False
    if len(header) == 17 and header[2] == "NR5G-SA" and header[3] in {"FDD", "TDD"}:
        if len(rows) != 1:
            return result
        result["nr_mode"] = "SA"
        pci, channel, band, width = header[7], header[9], header[10], header[11]
        rsrp, rsrq, sinr = header[12], header[13], header[14]
        coherent = bool(re.fullmatch(r"\d{3}", header[4])
                        and re.fullmatch(r"\d{2,3}", header[5]))
    elif len(header) == 2:
        nr = [row for row in rows if row and row[0] == "NR5G-NSA"]
        if len(nr) != 1 or len(nr[0]) not in {7, 9, 11}:
            return result
        row = nr[0]
        result["nr_mode"] = "NSA"
        pci, rsrp, sinr, rsrq = row[3:7]
        channel, band = (row[7], row[8]) if len(row) >= 9 else (None, None)
        width = row[9] if len(row) == 11 else None
        anchors = [part for part in rows if part and part[0] == "LTE"]
        coherent = (len(anchors) == 1 and len(anchors[0]) == 18
                    and anchors[0][1] in {"FDD", "TDD"}
                    and re.fullmatch(r"\d{3}", row[1]) is not None
                    and re.fullmatch(r"\d{2,3}", row[2]) is not None
                    and anchors[0][2:4] == row[1:3])
    else:
        return result
    for value, name, low, high in ((pci, "nr_pci", 0, 1007),
            (channel, "nr_arfcn", 0, 3279165), (band, "nr_band", 1, 1024)):
        if isinstance(value, str) and re.fullmatch(r"\d{1,7}", value):
            number = int(value)
            if low <= number <= high:
                result[name] = number
    for value, name, unit, low, high in (
            (rsrp, "nr_rsrp_dbm", "dBm", -160., -20.),
            (rsrq, "nr_rsrq_db", "dB", -50., 10.),
            (sinr, "nr_sinr_db", "dB", -50., 100.)):
        result[name] = _routeros_nr_metric(value, unit, low, high)
    widths = {0: 5., 1: 10., 2: 15., 3: 20., 4: 25., 5: 30., 6: 40., 7: 50.,
              8: 60., 9: 70., 10: 80., 11: 90., 12: 100., 13: 200., 14: 400.}
    if state == "CONNECT" and isinstance(width, str) and re.fullmatch(r"\d{1,2}", width):
        result["nr_bandwidth_mhz"] = widths.get(int(width))
    complete = coherent and all(result[key] is not None for key in (
        "nr_pci", "nr_arfcn", "nr_band", "nr_bandwidth_mhz",
        "nr_rsrp_dbm", "nr_rsrq_db", "nr_sinr_db"))
    if complete:
        result["nr5g_active"] = True
        result["nr_cells"] = [{"role": "PCell" if result["nr_mode"] == "SA" else "SCell",
            "band": result["nr_band"], "arfcn": result["nr_arfcn"], "pci": result["nr_pci"],
            "bandwidth_mhz": result["nr_bandwidth_mhz"], "rsrp_dbm": result["nr_rsrp_dbm"],
            "rsrq_db": result["nr_rsrq_db"], "sinr_db": result["nr_sinr_db"]}]
    return result


def parse_routeros_nr_qcainfo_ul(raw: str) -> float | None:
    """One activated NR QCAINFO component's configured UL width, in MHz.

    The documented stateful9/11/12 schema has distinct DL and UL enums.
    Short NR rows cannot supply UL, and multiple NR rows have no scalar
    association. Neither secondary activation nor UL configuration proves
    current uplink traffic. No identity or RF tail is interpreted here.
    """
    if not isinstance(raw, str) or len(raw) > 16384 or "ERROR" in raw:
        return None
    rows = []
    for line in raw.splitlines():
        if not line.strip().startswith("+QCAINFO:"):
            continue
        parts = [part.strip() for part in line.split(":", 1)[1].split(",")]
        if len(parts) > 3 and "NR5G" in parts[3]:
            rows.append(parts)
    if len(rows) != 1:
        return None
    row = rows[0]
    if (len(row) not in {9, 11, 12} or row[0] != '"SCC"'
            or re.fullmatch(r'"NR5G BAND [0-9]{1,3}"', row[3]) is None
            or row[4] != "2" or row[6] != "1"
            or re.fullmatch(r"[0-9]{1,2}", row[7]) is None):
        return None
    widths = {0: 5., 1: 10., 2: 15., 3: 20., 4: 25., 5: 30., 6: 40., 7: 50.,
              8: 60., 9: 70., 10: 80., 11: 90., 12: 100., 13: 200., 14: 400.,
              15: 35., 16: 45.}
    return widths.get(int(row[7]))


def parse_routeros_lte_monitor_row(
    row: Mapping[str, Any],
    *,
    parse_uptime: Callable[[str], float] | None = None,
) -> Dict[str, Any]:
    """Normalize one decoded ``/interface/lte monitor`` row."""

    result: Dict[str, Any] = {}

    # RouterOS exposes these modem-identity facts on several LTE modem/RouterOS
    # combinations as part of the normal monitor response.  They belong to the
    # same accepted read as RF truth; retaining them costs no extra modem I/O.
    for source_key, target_key in (
        ("imei", "imei"),
        ("imsi", "imsi"),
        ("manufacturer", "vendor"),
        ("model", "modem_model"),
        ("revision", "firmware"),
    ):
        text = str(row.get(source_key) or "").strip()
        if text and text.lower() not in {"unknown", "n/a", "none"}:
            result[target_key] = text

    for key, target, converter in [
        ("sinr", "sinr", lambda v: float(v.replace("dB", "").strip())),
        ("rsrp", "rsrp", lambda v: float(v.replace("dBm", "").strip())),
        ("rsrq", "rsrq", lambda v: float(v.replace("dB", "").strip())),
        ("rssi", "rssi", lambda v: float(v.replace("dBm", "").strip())),
        ("cqi", "cqi", int),
        ("ri", "ri", int),
        ("current-operator", "operator", str),
        ("phy-cellid", "pci", int),
        ("session-uptime", "session_uptime_raw", str),
    ]:
        val = row.get(key)
        if val is not None:
            try:
                result[target] = converter(str(val))
            except (ValueError, TypeError):
                pass

    band_str = row.get("primary-band", "")
    if band_str:
        band_text = str(band_str)
        primary_band = _parse_routeros_primary_band(band_text)
        if primary_band > 0:
            result["primary_band"] = primary_band
        bw_match = re.search(r"@(\d+(?:\.\d+)?)Mhz", band_text, re.IGNORECASE)
        if bw_match:
            try:
                result["dl_bw_mhz"] = float(bw_match.group(1))
            except ValueError:
                pass
        if "earfcn" not in result:
            earfcn_match = re.search(r"earfcn[:=]\s*(\d+)", band_text, re.IGNORECASE)
            if earfcn_match:
                try:
                    parsed_earfcn = int(earfcn_match.group(1))
                    if _valid_lte_earfcn(parsed_earfcn):
                        result["earfcn"] = parsed_earfcn
                    else:
                        result["_routeros_invalid_earfcn"] = True
                except ValueError:
                    pass
        if "pci" not in result:
            pci_match = re.search(r"(?:phy-cellid|pci)[:=]\s*(\d+)", band_text, re.IGNORECASE)
            if pci_match:
                try:
                    result["pci"] = int(pci_match.group(1))
                except ValueError:
                    pass

    earfcn = row.get("earfcn")
    if earfcn:
        try:
            parsed_earfcn = int(earfcn)
            if _valid_lte_earfcn(parsed_earfcn):
                result["earfcn"] = parsed_earfcn
            else:
                result["_routeros_invalid_earfcn"] = True
        except ValueError:
            pass

    # Preserve explicit-alias precedence when both channels are legal. Only a
    # demonstrably out-of-band explicit channel can be replaced by a complete,
    # standard-compliant primary-carrier description from this same reply.
    primary = _routeros_carrier_detail(str(band_str or ""), is_pcell=True) or {}
    conflicts = {}
    if (all(key in primary for key in ("band", "earfcn", "pci"))
            and lte_band_from_dl_earfcn(primary["earfcn"]) == primary["band"]
            and 0 <= primary["pci"] < 504):
        for key in ("earfcn", "pci"):
            if key in result and result[key] != primary[key]:
                conflicts[key] = result[key]
        bounds = lte_earfcn_bounds(primary["band"])
        if bounds is not None and "earfcn" in result and not bounds[0] <= result["earfcn"] <= bounds[1]:
            result.update(earfcn=primary["earfcn"], pci=primary["pci"])
    channel = result.get("earfcn")
    channel_band = lte_band_from_dl_earfcn(channel) if type(channel) is int else 0
    if conflicts or (channel_band and result.get("primary_band", 0) > 0
                     and result["primary_band"] != channel_band):
        result["_radio_identity_audit"] = {
            "native": {key: row[key] for key in ("primary-band", "earfcn", "phy-cellid", "current-cellid", "cell-id") if key in row},
            "conflicting_aliases": conflicts,
            "published": {key: result[key] for key in ("primary_band", "earfcn", "pci") if key in result},
        }

    carrier_details = routeros_monitor_carrier_details(row)
    ca_observation_status = routeros_monitor_ca_observation_status(row)
    ca_bands = routeros_monitor_ca_bands(row)
    ca_entry_count = routeros_monitor_ca_entry_count(row)
    active_bands = routeros_monitor_active_bands(row, int(result.get("primary_band", 0) or 0))
    composition_sig = routeros_monitor_ca_composition_signature(row)
    if active_bands:
        result["active_band_set"] = list(active_bands)
        result["active_band_set_signature"] = band_signature(active_bands)
    result["_routeros_ca_observation_status"] = ca_observation_status
    if ca_observation_status in {"active", "inactive"}:
        result["ca_active"] = ca_observation_status == "active"
        result["ca_component_band_set"] = list(ca_bands)
        result["ca_component_band_signature"] = band_signature(ca_bands)
        result["ca_component_count"] = ca_entry_count
        result["ca_source"] = (
            "routeros_monitor_ca_band"
            if ca_observation_status == "active"
            else "routeros_monitor_no_ca_band"
        )
        result["carrier_details"] = list(carrier_details)
        result["carrier_rows"] = [
            {
                "band": int(detail["band"]),
                "dl_mhz": detail.get("bandwidth_mhz"),
                "pcell": bool(detail.get("is_pcell", False)),
            }
            for detail in carrier_details
        ]
    if composition_sig:
        result["ca_composition_signature"] = composition_sig

    cell_id = row.get("current-cellid") or row.get("cell-id")
    if cell_id:
        cid = _parse_int_text(cell_id)
        if cid is not None:
            result["cell_id"] = cid
            result["enb_id"] = cid >> 8

    enb_id = row.get("enb-id")
    if enb_id:
        parsed_enb = _parse_int_text(enb_id)
        if parsed_enb is not None:
            result["enb_id"] = parsed_enb

    # RouterOS calls the LTE tracking-area value ``lac`` on multiple LTE
    # monitor surfaces.  Modem Live owns the native-name translation, so the
    # raw RouterOS alias must terminate here as canonical TAC before the raw
    # row is discarded.
    tac = (
        row.get("tracking-area-code")
        or row.get("tac")
        or row.get("lac")
    )
    if tac:
        parsed_tac = _parse_int_text(tac)
        if parsed_tac is not None:
            result["tac"] = parsed_tac

    status = row.get("status", "")
    has_signal = "sinr" in result and "rsrp" in result
    has_session = bool(row.get("session-uptime", ""))
    status_ok = any(s in str(status).lower() for s in ["connected", "running", "attached"])
    result["radio_connected"] = has_signal or has_session or status_ok

    uptime_raw = result.get("session_uptime_raw", "")
    result["session_uptime_s"] = parse_uptime(str(uptime_raw)) if parse_uptime is not None else 0.0

    for mk in ("dl-mcs", "mcs"):
        v = row.get(mk)
        if v is not None:
            try:
                result["mcs_dl"] = int(v)
            except ValueError:
                pass

    ul_mcs = row.get("ul-mcs")
    if ul_mcs is not None:
        try:
            result["mcs_ul"] = int(ul_mcs)
        except ValueError:
            pass

    # These values, when exposed, come from the same one-shot RouterOS LTE
    # monitor row.  They are optional hardware facts: an absent or unfamiliar
    # value remains missing and never turns the whole read into a failure.
    for source_key, target_key in (
        ("dl-modulation", "qam_dl0_reported"),
        ("ul-modulation", "qam_ul_reported"),
    ):
        qm = _parse_routeros_modulation_qm(row.get(source_key))
        if qm is not None:
            result[target_key] = qm

    antenna = routeros_antenna_status(row)
    if antenna:
        result.update(antenna)

    return result


def routeros_antenna_status(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Extract static antenna status from RouterOS rows when exposed."""

    candidates = (
        "external-antenna-selected",
        "external-antenna",
        "antenna",
        "antenna-mode",
        "antenna-mode-current",
        "current-antenna",
        "selected-antenna",
        "antenna-selection",
    )
    for key in candidates:
        value = row.get(key)
        label = normalize_routeros_antenna_mode(value)
        if label:
            return {
                "antenna": label,
                "antenna_mode": label,
                "antenna_mode_source": "routeros_static",
            }
    return {}


def normalize_routeros_antenna_mode(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    low = text.lower()
    if low in {"both", "all", "external+external"}:
        return "External"
    if low in {"none", "internal+internal"}:
        return "Internal"
    if low in {"auto", "automatic"}:
        return "Automatic"
    if low in {"external", "extern", "ext", "yes", "true", "1", "enabled", "on"}:
        return "External"
    if low in {"internal", "intern", "int", "no", "false", "0", "disabled", "off"}:
        return "Internal"
    if "auto" in low:
        return "Automatic"
    return text


def rb_to_mhz(rb: int) -> float:
    """Convert LTE resource-block count or Quectel encoded bandwidth index."""

    rb_count = int(rb or 0)
    index_to_rb = {0: 6, 1: 15, 2: 25, 3: 50, 4: 75, 5: 100}
    rb_count = index_to_rb.get(rb_count, rb_count)
    return {6: 1.4, 15: 3.0, 25: 5.0, 50: 10.0, 75: 15.0, 100: 20.0}.get(rb_count, 0.0)


def parse_qeng_servingcell(raw: Any) -> Dict[str, Any]:
    """Parse Quectel ``AT+QENG="servingcell"`` output."""

    text = str(raw or "")
    if not text or "ERROR" in text:
        return {}
    lines = [line.strip() for line in text.splitlines() if "servingcell" in line]
    if not lines:
        return {}
    parts = [part.strip() for part in lines[0].split(",")]
    if len(parts) < 12:
        return {}

    result: Dict[str, Any] = {
        "state": _strip_quotes(_part(parts, 1)),
        "rat": _strip_quotes(_part(parts, 2)),
        "duplex": _strip_quotes(_part(parts, 3)),
        "mcc": _qeng_int(_part(parts, 4)),
        "mnc": _qeng_int(_part(parts, 5)),
        "cellid": _qeng_int(_part(parts, 6)),
        "pci": _qeng_int(_part(parts, 7)),
        "earfcn": _qeng_int(_part(parts, 8)),
        "band": _qeng_int(_part(parts, 9)),
        "dl_bw_rb": _qeng_int(_part(parts, 10)),
        "ul_bw_rb": _qeng_int(_part(parts, 11)),
        "tac": _part(parts, 12) if len(parts) > 12 else "",
        "rsrp": _qeng_float(_part(parts, 13)),
        "rsrq": _qeng_float(_part(parts, 14)),
        "rssi": _qeng_float(_part(parts, 15)),
        "sinr": _qeng_float(_part(parts, 16)),
        "cqi": _qeng_cqi(_part(parts, 17)),
        "tx_power": _part(parts, 18) if len(parts) > 18 else "-",
    }
    trans_mode = _qeng_trans_mode(text, parts)
    if trans_mode:
        result["trans_mode"] = trans_mode
    if not _valid_lte_earfcn(int(result.get("earfcn", 0) or 0)):
        result["earfcn"] = 0
    return result


def _qeng_trans_mode(text: str, parts: List[str]) -> str:
    for pattern in (
        r"\bTM\s*\[?\s*([1-9])\s*\]?",
        r"\btrans(?:mission)?[-_\s]?mode\b[^0-9A-Za-z]+(?:TM\s*)?\[?\s*([1-9])\s*\]?",
        r"\btransmode\b[^0-9A-Za-z]+(?:TM\s*)?\[?\s*([1-9])\s*\]?",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return f"TM{match.group(1)}"
    for part in parts[19:]:
        token = _strip_quotes(part).strip()
        if not token:
            continue
        match = re.search(r"\bTM\s*\[?\s*([1-9])\s*\]?", token, flags=re.IGNORECASE)
        if match:
            return f"TM{match.group(1)}"
        match = re.search(
            r"\btrans(?:mission)?[-_\s]?mode\b[^0-9A-Za-z]+(?:TM\s*)?\[?\s*([1-9])\s*\]?",
            token,
            flags=re.IGNORECASE,
        )
        if match:
            return f"TM{match.group(1)}"
    return ""


def _part(parts: List[str], index: int) -> str:
    return parts[index].strip() if index < len(parts) else "-"


def _strip_quotes(value: str) -> str:
    return str(value or "").strip().strip('"')


def _qeng_int(value: str) -> int:
    text = _strip_quotes(value)
    if text in {"", "-"}:
        return 0
    try:
        return int(text)
    except Exception:
        return 0


def _qeng_float(value: str) -> float | None:
    text = _strip_quotes(value)
    if text in {"", "-"}:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _qeng_cqi(value: str) -> int:
    parsed = _qeng_int(value)
    return parsed if 0 <= parsed <= 15 else 0


# Existing RouterOS Wi-Fi conversions, shared with the fixed profile codec.
def _ros_bool(value: Any) -> bool:
    """Normalize RouterOS truthy flags returned as bool/str/int."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {'true', 'yes', 'on', '1'}

def _classify_wifi_band(row: dict) -> Optional[str]:
    """Map a RouterOS Wi-Fi interface row to '2.4' or '5'.

        Heuristics (checked in order):
          1. Explicit 'band' field containing '2ghz' or '5ghz'
          2. Interface name containing '2' or '5' (e.g. wifi1 vs wifi2,
             wlan1 vs wlan2 — convention varies across firmware)
          3. 'default-name' containing '1' → 2.4, '2' → 5 (factory naming)
          4. Frequency field if present (2400-2500 → 2.4, 5000-5900 → 5)
        Returns None if band cannot be determined.
        """
    band = str(row.get('band', '')).lower()
    if '2ghz' in band or '2.4' in band:
        return '2.4'
    if '5ghz' in band or '5g' in band:
        return '5'
    freq_str = str(row.get('frequency', '') or row.get('channel.frequency', ''))
    if freq_str:
        try:
            freq = int(freq_str)
            if 2400 <= freq <= 2500:
                return '2.4'
            if 4900 <= freq <= 5900:
                return '5'
        except (ValueError, TypeError):
            pass
    name = str(row.get('name', '')).lower()
    default_name = str(row.get('default-name', '')).lower()
    for label in (name, default_name):
        if label.endswith('1') or '2ghz' in label or '2.4' in label:
            return '2.4'
        if label.endswith('2') or '5ghz' in label:
            return '5'
    return None

def routeros_wifi(rows, previous):
    if not rows:
        return previous
    wifi_24 = None
    wifi_5 = None
    for row in rows:
        band = _classify_wifi_band(row)
        enabled = not _ros_bool(row.get('disabled', 'false'))
        if band == '2.4':
            wifi_24 = bool((wifi_24 or False) or enabled)
        elif band == '5':
            wifi_5 = bool((wifi_5 or False) or enabled)
    if wifi_24 is None and wifi_5 is None:
        return previous
    return (bool(wifi_24 or wifi_5), wifi_24, wifi_5)
