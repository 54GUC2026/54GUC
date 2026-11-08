"""Permanent ZWRT read-diagnostic adapter; no dynamic RPC execution or writes."""
from dataclasses import dataclass
import re
import time

from arch4.modem.backend_diagnostic import ModemBackendDiagnostic
from arch4.modem import read_diagnostic_projection as projection
from arch4.modem.read_diagnostics import DiagnosticDeviceContinuity
from arch4.modem.zte_goform_read_diagnostics import GoformReadDiagnosticAdapter


@dataclass(frozen=True)
class Probe:
    label: str
    service: str
    method: str = ""
    params: tuple = ()


# These are existing product read dialects, not guessed get* commands. Listing
# advertises candidates but never expands this execution plan automatically.
PROBES = (
    Probe("Available router APIs", "*"),
    Probe("Radio and carrier status", "zte_nwinfo_api", "nwinfo_get_netinfo"),
    Probe("CPU temperature", "zwrt_bsp.thermal", "get_cpu_temp"),
    Probe("Device metadata", "zwrt_mc.device.manager", "get_device_info"),
    Probe("Router status", "zwrt_router.api", "router_get_status"),
    Probe("Traffic rates and counters", "zwrt_data", "get_wwandst", (("source_module", "web"), ("cid", 1), ("type", 4))),
    Probe("SIM information", "zwrt_zte_mdm.api", "get_sim_info"),
    Probe("WAN interface status", "network.interface.zte_wan", "status"),
    Probe("Firmware metadata", "uci", "get", (("config", "zwrt_common_info"), ("section", "common_config"))),
    Probe("Serial read", "zwrt_zte_mdm.api", "get_modem_msn"),
    Probe("IMEI read", "zwrt_zte_mdm.api", "get_imei"),
    Probe("Alternate device metadata", "uci", "get", (("config", "zwrt_zte_mdm"), ("section", "device_info"))),
    Probe("Mobile data and roaming", "zwrt_data", "get_wwaniface", (("cid", 1),)),
    Probe("Radio operating mode", "uci", "get", (("config", "zte_nwinfo"), ("section", "sys_info"))),
    Probe("APN selection mode", "zwrt_apn_object", "get_apn_mode"),
    Probe("Manual APN profiles", "zwrt_apn_object", "get_manu_apn_list"),
    Probe("Automatic APN profiles", "zwrt_apn_object", "get_auto_apn_list"),
    Probe("Network registration result", "zte_nwinfo_api", "nwinfo_m_netselect_result"),
    Probe("Normal read decoding", "@normal_read"),
)


def outcome(reply):
    if not isinstance(reply, dict):
        return "no_reply"
    if reply.get("accessDenied"):
        return "access_denied"
    reason = str(reply.get("reason", ""))
    if reason.endswith((":-32601", ":4")) or "method_not_found" in reason:
        return "method_not_found"
    if reason.endswith(":-32000"):
        return "service_not_found"
    if reason.endswith(":-32001"):
        return "session_not_found"
    if "Timeout" in reason or reason.endswith(":-32003"):
        return "timeout"
    if "transport_error" in reason:
        return "transport_error"
    if not reply.get("success"):
        return "protocol_error" if "protocol_error" in reason or reason.endswith(":-1") else "request_failed"
    if not isinstance(reply.get("data"), dict):
        return "malformed_reply"
    return "success" if reply["data"] else "empty_reply"


def project_signatures(data):
    services, remaining, truncated = {}, 300, len(data) > 80
    for service, methods in list(data.items())[:80]:
        if remaining <= 0:
            truncated = True
            break
        if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", service) or not isinstance(methods, dict):
            continue
        limit = min(60, remaining)
        truncated = truncated or len(methods) > limit
        services[service] = {name: [arg for arg in signature if isinstance(arg, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", arg)][:16]
            for name, signature in list(methods.items())[:limit]
            if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", name) and isinstance(signature, dict)}
        remaining -= len(services[service])
    return {"advertised_methods": services, "qualification": "Names only; unknown methods were not executed.",
            "truncated": truncated}


class ZwrtReadDiagnosticAdapter:
    probes = PROBES

    def __init__(self, modem):
        self.modem = modem
        self.backend = modem._backend_name
        self.started_at = time.time()
        self.normal_read = None
        self._profile_id = modem._definition.profile_id
        self._matrix_comparison = None
        self._matrix_comparison_attempted = False
        self._comparison_reason = "not_supplied"
        self.observing = True
        self._session = modem._ubus_session_id
        self._sim = str(modem._last_raw.get("sim_imsi") or modem._last_raw.get("imsi") or "")
        self._sim_changed = False
        self._device = DiagnosticDeviceContinuity(GoformReadDiagnosticAdapter._device_values(modem._last_raw))

    def context_current(self):
        try:
            device_current = self._device.matches_snapshot(GoformReadDiagnosticAdapter._device_values(self.modem._last_raw))
        except ValueError:
            device_current = False
        return bool(self.modem._connected and self.modem._ubus_session_id == self._session
                    and self.modem._definition.profile_id == self._profile_id
                    and not self._sim_changed and device_current)

    def bind_matrix_comparison(self, matrix_reader, *, profile_id):
        from arch4.modem_live.truth_matrix import ValidatedTruthMatrixReader
        if profile_id != self._profile_id:
            raise ValueError("zwrt_diagnostic_comparison_profile_mismatch")
        if self._matrix_comparison_attempted:
            raise RuntimeError("zwrt_diagnostic_comparison_already_bound")
        if not isinstance(matrix_reader, ValidatedTruthMatrixReader):
            raise TypeError("zwrt_diagnostic_validated_matrix_reader_required")
        self._matrix_comparison_attempted = True
        if not self.context_current():
            self._comparison_reason = "context_changed"
            return
        self._matrix_comparison = matrix_reader
        self._comparison_reason = ""

    def diagnostic_coverage_context(self):
        return {"owner": "modem_live.read_diagnostics", "backend": self.backend,
                "profile_id": self._profile_id,
                "comparison_state": "compared" if self._matrix_comparison is not None else "not_compared",
                "comparison_reason": self._comparison_reason,
                "matrix_identity": self._matrix_comparison.identity.as_dict()
                    if self._matrix_comparison is not None else None}

    def summarize(self, rows):
        return field_coverage(rows, self.normal_read, self._matrix_comparison)

    def read(self, probe):
        if probe not in PROBES:
            raise ValueError("read_diagnostic_probe_not_registered")
        if probe.service == "@normal_read":
            return {"probe": probe.label, "outcome": "success" if self.normal_read else "no_current_normal_read",
                    "evidence": self.normal_read or {},
                    "note": "Same-read raw and decoded values; no diagnostic value replaces a measurement."}
        call = {"service": probe.service}
        if probe.method:
            call.update(method=probe.method, params=dict(probe.params))
        # Use the authenticated session directly; optional ACL refusal must not
        # invalidate it. No login/repair or alternate protocol is attempted here.
        reply = self.modem._ubus_call(call, describe_only=not probe.method, diagnostic_read=True)
        state = outcome(reply)
        data = reply.get("data") if isinstance(reply, dict) else None
        if state == 'success' and probe.method in {'get_device_info', 'get_modem_msn', 'get_imei', 'get'}:
            identity_data = data.get('values', {}) if probe.service == 'uci' else data
            try:
                current_device = self._device.native_values(identity_data, {
                    'model': ('model_name', 'product_name', 'device_name', 'model', 'product'),
                    'hardware_version': ('hardware_version', 'hw_version'),
                    'firmware': ('wa_inner_version', 'software_version', 'firmware_version', 'modem_version', 'inner_version'),
                    'webui': ('web_version',), 'config_version': ('cr_version', 'config_version', 'configuration_file_version'),
                    'serial': ('modem_msn', 'msn', 'serial_number', 'sn', 'sn_num', 'serial'),
                    'imei': ('imei', 'modem_imei', 'IMEI')})
            except ValueError:
                return {'probe': probe.label, 'outcome': 'malformed_reply'}
            if not self._device.observe(current_device):
                self._sim_changed = True
                return {'probe': probe.label, 'outcome': 'context_changed'}
        if probe.method == "get_sim_info" and isinstance(data, dict) and state == "success":
            from arch4.modem.zte_zwrt_sim import active_sim
            current = active_sim(data)["imsi"]
            if self._sim and current and current != self._sim:
                self._sim_changed = True
                state = "context_changed"
        evidence = (project_signatures(data) if not probe.method else projection.project_fields(data)) if isinstance(data, dict) else {}
        return {"probe": probe.label, "service": probe.service, "method": probe.method or "list",
                "parameters": dict(probe.params), "outcome": state, **evidence}

    def observe_normal_read(self, raw, modem, proven):
        if modem is None:
            return
        current = str(raw.get("sim_imsi") or raw.get("imsi") or "")
        if self._sim and current and current != self._sim:
            self._sim_changed = True
        self.normal_read = {
            "observed_at": time.time(), "raw": projection.project_fields(raw),
            "decoded_rf": {k: getattr(modem, k) for k in ("rsrp_dbm", "rsrq_db", "rssi_dbm", "sinr_db")},
            "admitted_fields": sorted(proven),
        }


def signal_evidence(raw, modem, backend, sequence):
    """Automatic anomalous RF evidence shares the tool's sanitizer, not a decoder."""
    fields = ("lte_rsrp", "lte_rsrp_1", "lte_rsrq", "lte_rssi", "rssi", "lte_snr",
              "sinr", "network_type", "lte_pci", "lte_earfcn", "lteca", "ltecasig")
    return ModemBackendDiagnostic(
        owner="modem_live.read_diagnostics.signal", sequence=sequence,
        observed_at=time.time(), kind="router_signal_read_evidence", backend=backend,
        severity="INFO", data={
            "reason": "rsrp_above_capture_threshold_not_a_fault_verdict",
            "raw": projection.project_fields({k: raw[k] for k in fields if k in raw}),
            "parsed": {k: getattr(modem, k) for k in ("rsrp_dbm", "rsrq_db", "rssi_dbm", "sinr_db")},
        }).to_payload()


# Diagnostic coverage, not decoder aliases. The normal receipt's proof is kept
# separate from these independently timed probe results.
COVERAGE = (
    ("CPU temperature", ("cputemp", "cpusstemp", "cputemperaturec", "temperaturecpu"), "temperature_cpu_c"),
    ("5G temperature", ("pmsensor5g", "pmmodem5g", "nrtemp", "5gtemperature"), "temperature_modem_5g_c"),
    ("LTE RSRP", ("ltersrp", "ltersrp1"), "rsrp_dbm"),
    ("NR signal", ("nr5grsrp", "5grx0rsrp", "z5grsrp"), "nr_rsrp_dbm"),
    ("LTE carriers", ("lteca", "ltemulticascellinfo", "ltecapcellband",
                      "ltecapcellbandwidth", "ltecascellband"), "carrier_details"),
    ("NR carriers", ("nrca", "nrmulticascellinfo", "nr5gcellid",
                     "nr5gband", "nr5gnsabandwidth"), "nr_cells"),
    ("LTE neighbours", ("lteneighborcell", "lteneighbourcell"), None),
    ("NR neighbours", ("nrneighborcell", "nrneighbourcell"), None),
    ("RRC state", ("rrc", "rrcstate", "rrcstatus"), None),
    ("Rank", ("ri", "rank", "rankindicator", "lteri"), None),
    ("Transmission mode", ("transmode", "transmissionmode", "ltetm"), None),
    ("Transmit power", ("txpower", "ltepuschpower", "puschpower", "txppusch"), "tx_power"),
    ("Downlink rate", ("realrxspeed", "rxbytespersec"), "rx_bytes_per_sec"),
    ("Uplink rate", ("realtxspeed", "txbytespersec"), "tx_bytes_per_sec"),
    ("Lifetime traffic", ("totalrxbytes", "totaltxbytes"), "traffic_device_download_counter_bytes"),
)


def field_coverage(rows, normal_read, matrix_reader=None):
    from arch4.modem_live.truth_matrix import MODEM_LIVE_TRUTH_FIELD_CATALOG
    admitted = set((normal_read or {}).get("admitted_fields", ()))
    results = []
    for label, aliases, canonical in COVERAGE:
        candidates = [{"probe": row["probe"], "name": name, "state": field["state"]}
            for row in rows if row.get("outcome") == "success"
            for name, field in row.get("fields", {}).items()
            if isinstance(field, dict) and projection.diagnostic_field_name(name.split(".")[-1]) in aliases]
        returned = any(item["state"] == "returned" for item in candidates)
        # These are existing canonical field names, not native decoder aliases.
        address = next((f"{line}.{canonical}" for line, fields in MODEM_LIVE_TRUTH_FIELD_CATALOG.items()
                        if canonical in fields), "") if canonical else ""
        comparison = {"canonical_path": address, "comparison_state": "not_compared",
                      "normal_read_admitted": canonical in admitted if normal_read is not None and canonical else None,
                      "normal_read_observed_at": (normal_read or {}).get("observed_at")}
        if address and matrix_reader is not None:
            cell = matrix_reader.cell(*address.split(".", 1))
            comparison.update(comparison_state="compared", matrix_state=cell.state,
                matrix_reason=cell.reason, matrix_sampled_at=cell.sampled_at,
                matrix_acquired_at=cell.acquired_at, matrix_source_lane=cell.source_lane,
                matrix_retained=cell.retention is not None)
        if not returned:
            state = "empty" if candidates else "not_returned"
            result = ("Returned empty by the tested interfaces" if candidates else
                      "Not reported by the tested interfaces; see individual read outcomes")
        elif not address:
            state, result = "returned_unmapped", "Returned; no canonical comparison is defined for this diagnostic field"
        elif matrix_reader is not None:
            if comparison["matrix_state"] == "available":
                state = "mapped_available"
                result = "Returned; also admitted by normal monitoring as a separate Matrix observation"
                if comparison["matrix_retained"]:
                    result += " (Matrix value is retained from its recorded acquisition time)"
            else:
                state = "mapped_not_admitted"
                result = ("Returned; the compared Matrix generation marks this field "
                          + comparison["matrix_state"] + ": " + comparison["matrix_reason"])
        else:
            state = "mapped_not_compared"
            result = ("Returned; no captured normal read was available for comparison" if normal_read is None else
                      "Returned; also admitted in the captured normal read" if canonical in admitted else
                      "Returned; not admitted in the captured normal read. Other monitoring lanes were not compared")
        results.append({"field": label, "result": result, "sources": candidates,
                        "coverage_state": state, **comparison})
    return results
