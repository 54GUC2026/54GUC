import inspect

from arch4.canonical_scheduler_display import (
    build_scheduler_display_contract,
    build_scheduler_display_surface,
)
from arch4.controller_frames import project_controller_read_model
from arch4.interfaces import ComputedSignals, ModemInput
from arch4.status_payload import (
    _build_operator_summary as _production_build_operator_summary,
    add_legacy_status_aliases,
    build_canonical_status_payload as _production_build_canonical_status_payload,
    build_status_payload as _production_build_status_payload,
)
from arch4.tests.controller_generation_factory import (
    materialize_controller_generation,
)


def _canonical_status_from_raw(
    *,
    base_status,
    modem,
    computed,
    constraints,
    state_name,
    **kwargs,
):
    accepted = materialize_controller_generation(
        modem,
        computed,
        owner_values=base_status,
    )
    return _production_build_canonical_status_payload(
        base_status=base_status,
        accepted=accepted,
        constraints=constraints,
        state_name=state_name,
        **kwargs,
    )


def _export_status_from_raw(
    *,
    base_status,
    modem,
    computed,
    constraints,
    state_name,
    **kwargs,
):
    accepted = materialize_controller_generation(
        modem,
        computed,
        owner_values=base_status,
    )
    return _production_build_status_payload(
        base_status=base_status,
        accepted=accepted,
        constraints=constraints,
        state_name=state_name,
        **kwargs,
    )


def _operator_summary_from_raw(*, modem, computed, state_name, payload):
    accepted = materialize_controller_generation(modem, computed)
    return _production_build_operator_summary(
        modem=accepted.intelligence,
        computed=accepted.derived,
        state_name=state_name,
        payload=payload,
    )


def test_status_payload_delegates_computed_congestion_surface_to_congestion_module():
    canonical_source = inspect.getsource(
        _production_build_canonical_status_payload
    )
    projection_source = inspect.getsource(project_controller_read_model)

    assert "project_controller_read_model" in canonical_source
    assert "build_computed_status_surface" not in canonical_source
    assert "asdict(intelligence.quality)" in projection_source
    assert "asdict(intelligence.latency)" in projection_source


def test_canonical_status_payload_exports_congestion_authority_trend_verbatim():
    modem = ModemInput.safe_defaults(radio_connected=True)
    computed = ComputedSignals(
        congestion_pct=35.0,
        congestion_pct_valid=True,
        congestion_slope=4.2,
        congestion_rising=True,
        congestion_trend_display="UP",
    )

    payload = _canonical_status_from_raw(
        base_status={},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["congestion_trend_display"] == "UP"
    assert payload["congestion_slope"] == 4.2
    assert payload["congestion_rising"] is True


def test_canonical_status_payload_keeps_stepped_congestion_display_owner_fields():
    payload = _canonical_status_from_raw(
        base_status={},
        modem=ModemInput.safe_defaults(radio_connected=True),
        computed=ComputedSignals(
            congestion_pct=83.0,
            congestion_pct_valid=True,
            congestion_display_pct=45.0,
            congestion_display_valid=True,
        ),
        constraints=None,
        state_name="WORKING",
    )

    assert payload["congestion_pct"] == 83.0
    assert payload["congestion_display_pct"] == 45.0
    assert payload["congestion_display_valid"] is True


def test_status_payload_export_keeps_congestion_authority_trend_verbatim():
    modem = ModemInput.safe_defaults(radio_connected=True)
    computed = ComputedSignals(
        congestion_pct=35.0,
        congestion_pct_valid=True,
        congestion_slope=4.2,
        congestion_rising=True,
        congestion_trend_display="UP",
    )

    payload = _export_status_from_raw(
        base_status={},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["congestion_trend_display"] == "UP"
    assert payload["congestion_slope"] == 4.2
    assert payload["congestion_rising"] is True


def test_status_payload_exports_modem_owned_traffic_counters():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        traffic_day_used_bytes=2_717_073_852,
        traffic_month_download_bytes=2_656_806_029,
        traffic_month_upload_bytes=60_000_000,
        traffic_session_download_bytes=1_584_339_891,
        traffic_accounting_source="huawei_native",
        traffic_accounting_status="native",
    )

    payload = _canonical_status_from_raw(
        base_status={},
        modem=modem,
        computed=ComputedSignals(),
        constraints=None,
        state_name="WORKING",
    )

    assert payload["traffic_day_used_bytes"] == 2_717_073_852
    assert payload["traffic_month_download_bytes"] == 2_656_806_029
    assert payload["traffic_month_upload_bytes"] == 60_000_000
    assert payload["traffic_session_download_bytes"] == 1_584_339_891
    assert payload["traffic_accounting_source"] == "huawei_native"
    assert payload["traffic_accounting_status"] == "native"


def test_status_payload_exports_customer_display_derivatives_from_canonical_truth():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        primary_band=3,
        configured_band_pool=[3, 7, 20, 28],
        configured_band_pool_signature="B3+B7+B20+B28",
        configured_band_pool_source="net-mode",
        traffic_day_used_bytes=16 * 1024**3,
        traffic_accounting_source="huawei_month_statistics",
    )

    payload = _export_status_from_raw(
        base_status={
            "traffic_month_download_bytes": 274 * 1024**3,
            "traffic_quota_limit_bytes": 1000 * 1024**3,
            "traffic_quota_limit_label": "1000GB",
        },
        modem=modem,
        computed=ComputedSignals(),
        constraints=None,
        state_name="WORKING",
    )

    assert payload["configured_ca_pool_display"] == "B3+B7+B20+B28"
    assert "ca_pool_display" not in payload
    assert payload["volume_used_today_gb"] == 16.0
    assert "volume_allowance_gb" not in payload
    assert "volume_remaining_today_gb" not in payload
    assert payload["traffic_quota_left_bytes"] is None
    assert payload["traffic_quota_limit_bytes"] == 1000 * 1024**3


def test_status_payload_does_not_erase_cached_traffic_with_default_modem_values():
    modem = ModemInput.safe_defaults(radio_connected=True)

    payload = _canonical_status_from_raw(
        base_status={
            "traffic_day_used_bytes": 116_315_826,
            "traffic_month_download_bytes": 380_254_869_361,
            "traffic_month_upload_bytes": 46_933_333_412,
            "traffic_accounting_source": "huawei_native",
            "traffic_accounting_status": "native",
        },
        modem=modem,
        computed=ComputedSignals(),
        constraints=None,
        state_name="WORKING",
    )

    assert payload["traffic_day_used_bytes"] == 116_315_826
    assert payload["traffic_month_download_bytes"] == 380_254_869_361
    assert payload["traffic_month_upload_bytes"] == 46_933_333_412
    assert payload["traffic_accounting_source"] == "huawei_native"
    assert payload["traffic_accounting_status"] == "native"


def test_status_payload_preserves_live_ca_active_when_load_qualified_cart_is_zero():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        primary_band=3,
        ca_active=True,
        ca_band_signature="B3+B20",
    )
    computed = ComputedSignals(cart=0, cast=0.0, cart_ld=0, cast_ld=0.0)

    payload = _export_status_from_raw(
        base_status={},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["ca_active"] is True
    assert payload["ca_state_text"] == "MULTI-BAND ACTIVE"
    assert payload["ca_signature"] == "B3+B20"
    assert payload["cart"] == 0


def test_export_status_alias_adapter_preserves_owner_ca_active_over_cart_alias():
    payload = add_legacy_status_aliases(
        {
            "serving_band": 3,
            "ca_active": True,
            "cart": 0,
            "cast": 0.0,
            "cart_ld": 0,
            "cart_ld_valid": False,
        }
    )

    assert payload["ca_active"] is True
    assert payload["ca_realtime_state"] == 0


def test_operator_summary_prefers_nested_decision_context_and_execution_outcome():
    modem = ModemInput(
        sinr_db=2.0,
        rsrp_dbm=-96.0,
        rsrq_db=-15.0,
        rssi_dbm=-70.0,
        mcs_dl=4,
        mcs_ul=6,
        cqi=3,
        ri=1,
        carriers=[],
        ca_active=False,
        ca_band_signature="",
        primary_band=20,
        pci=101,
        earfcn=6300,
        enb_id=9876,
        radio_connected=True,
        session_uptime_s=120.0,
        rx_bytes_per_sec=100_000,
        tx_bytes_per_sec=50_000,
        selected_band_set=[3, 20],
        timestamp=1710400400.0,
    )
    computed = ComputedSignals(
        rtt_ms=88.0,
        rtt_sliding_avg=90.0,
        rtt_pressure_level="high",
        cq_cell=0.31,
    )

    summary = _operator_summary_from_raw(
        modem=modem,
        computed=computed,
        state_name="BAD_RF",
        payload={
            "brain": {
                "warning_class": "legacy_warning",
                "warning_source": "legacy_source",
                "warning_mechanism": "legacy_mechanism",
                "decision_context": {
                    "warning_class": "rf_event",
                    "warning_source": "radio_environment",
                    "warning_mechanism": "rf_cliff",
                },
                "execution_outcome": {
                    "action_reason": "restore CA",
                },
            },
            "incident_assessment": {
                "incident_class": "healthy",
                "root_cause": {"probable_cause": "none"},
            },
            "calibration": {},
            "action_text": "",
        },
    )

    assert summary["warning_class"] == "rf_event"
    assert summary["warning_source"] == "radio_environment"
    assert summary["warning_mechanism"] == "rf_cliff"
    assert "restoring the multi-band" in summary["status"]


def test_operator_summary_downgrades_session_origin_radio_down_to_control_reset_warning():
    modem = ModemInput.safe_defaults(radio_connected=False)
    computed = ComputedSignals(rtt_ms=34.0, rtt_sliding_avg=34.0, cq_cell=0.0)

    summary = _operator_summary_from_raw(
        modem=modem,
        computed=computed,
        state_name="E_RADIO_DOWN",
        payload={
            "brain": {
                "emergency_class": "E_RADIO_DOWN",
                "emergency_origin_class": "E_SESSION_RESET",
                "decision_context": {
                    "warning_class": "session",
                    "warning_source": "modem_session",
                    "warning_mechanism": "reset",
                },
            },
            "incident_assessment": {
                "incident_class": "radio_disconnected",
                "root_cause": {"probable_cause": "none"},
            },
            "calibration": {},
            "action_text": "",
        },
    )

    assert "control session reset observed" in summary["status"]
    assert "Radio connection is down" not in summary["status"]


def test_operator_summary_prefers_brain_health_broadcast_over_state_label():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        sinr_db=1.0,
        rsrp_dbm=-77.0,
        rsrq_db=-10.0,
        ca_active=True,
        ca_band_signature="B3+B20",
    )
    computed = ComputedSignals(rtt_ms=35.0, rtt_sliding_avg=35.0, cq_cell=0.72)

    summary = _operator_summary_from_raw(
        modem=modem,
        computed=computed,
        state_name="BAD_RF",
        payload={
            "brain": {},
            "canonical_incident_state": {
                "incident_class": "short_ca_blip_observe",
                "probable_cause": "transient_ca_blip",
                "health_status": "investigating",
                "health_confidence": 0.72,
                "user_message": "Brain sees unusual symptoms and is collecting evidence before naming the cause.",
            },
            "calibration": {},
            "action_text": "",
        },
    )

    assert "collecting evidence" in summary["status"]
    assert "RF conditions are poor" not in summary["status"]


def test_operator_summary_prefers_canonical_operator_projection_over_mixed_incident_surface():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        sinr_db=10.0,
        rsrp_dbm=-70.0,
        rsrq_db=-8.0,
        ca_active=True,
        ca_band_signature="B3+B20",
    )
    computed = ComputedSignals(rtt_ms=31.0, rtt_sliding_avg=31.0, cq_cell=0.81)

    summary = _operator_summary_from_raw(
        modem=modem,
        computed=computed,
        state_name="WORKING",
        payload={
            "brain": {},
            "incident_assessment": {
                "incident_class": "post_action_instability",
                "root_cause": {"probable_cause": "self_induced_settle"},
            },
            "canonical_action_outcome": {
                "action_scope": "action",
                "semantic": "beneficial",
            },
            "canonical_incident_state": {
                "incident_class": "post_action_instability",
            },
            "calibration": {},
            "action_text": "",
        },
    )

    assert summary["status"].startswith("Recent action succeeded.")
    assert "holding and waiting" not in summary["status"]
    assert summary["canonical_projection_class"] == "successful_action_settling"


def test_operator_summary_uses_stamped_canonical_operator_projection():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        sinr_db=10.0,
        rsrp_dbm=-70.0,
        rsrq_db=-8.0,
        ca_active=True,
        ca_band_signature="B3+B20",
    )
    computed = ComputedSignals(rtt_ms=31.0, rtt_sliding_avg=31.0, cq_cell=0.81)

    summary = _operator_summary_from_raw(
        modem=modem,
        computed=computed,
        state_name="WORKING",
        payload={
            "brain": {},
            "canonical_action_outcome": {
                "action_scope": "action",
                "semantic": "harmful",
            },
            "canonical_incident_state": {
                "incident_class": "post_action_instability",
            },
            "canonical_operator_projection": {
                "projection_class": "operator_owner_test",
                "status_text": "Stamped operator projection wins.",
                "action_scope": "action",
                "action_semantic": "beneficial",
                "incident_class": "post_action_instability",
            },
            "calibration": {},
            "action_text": "",
        },
    )

    assert summary["status"] == "Stamped operator projection wins."
    assert summary["canonical_projection_class"] == "operator_owner_test"


def test_operator_summary_no_longer_interprets_post_action_instability_without_canonical_projection():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        sinr_db=9.0,
        rsrp_dbm=-71.0,
        rsrq_db=-9.0,
        ca_active=True,
        ca_band_signature="B3+B20",
    )
    computed = ComputedSignals(rtt_ms=34.0, rtt_sliding_avg=34.0, cq_cell=0.78)

    summary = _operator_summary_from_raw(
        modem=modem,
        computed=computed,
        state_name="WORKING",
        payload={
            "brain": {},
            "incident_assessment": {
                "incident_class": "post_action_instability",
                "root_cause": {"probable_cause": "self_induced_settle"},
            },
            "calibration": {},
            "action_text": "",
        },
    )

    assert "Signal settling after a recent change" not in summary["status"]
    assert summary["status"] == "Signal is good. Autopilot is running normally."


def test_status_payload_exports_canonical_and_legacy_nr_aliases_from_modem_contract():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        nsa_active=True,
        nr_band=78,
        nr_pci=42,
        nr_arfcn=641760,
        nr_rsrp_dbm=-81.0,
        nr_rsrq_db=-10.0,
        nr_sinr_db=17.5,
    )
    computed = ComputedSignals()

    payload = _export_status_from_raw(
        base_status={},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["nsa_active"] is True
    assert payload["nr_band"] == 78
    assert payload["nr_pci"] == 42
    assert payload["nr_arfcn"] == 641760
    assert payload["nr_rsrp_dbm"] == -81.0
    assert payload["nr5g_active"] is True
    assert payload["nr5g_band"] == 78
    assert payload["nr5g_pci"] == 42
    assert payload["nr5g_arfcn"] == 641760


def test_canonical_status_payload_prefers_modem_reported_cell_id_over_derived_identity():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        pci=153,
        enb_id=8,
        cell_id=2070,
    )
    computed = ComputedSignals()

    payload = _canonical_status_from_raw(
        base_status={"cell_id": 99095},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["cell_id"] == 2070


def test_canonical_status_payload_uses_promoted_modem_cell_identity_not_base_status():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        pci=255,
        earfcn=1795,
        enb_id=8,
        cell_id=2050,
        tac=112,
    )
    computed = ComputedSignals()

    payload = _canonical_status_from_raw(
        base_status={
            "pci": 255,
            "earfcn_dl": 1795,
            "enb_id": 8,
            "cell_id": 2050,
            "tac": 112,
        },
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["pci"] == 255
    assert payload["earfcn_dl"] == 1795
    assert payload["enb_id"] == 8
    assert payload["cell_id"] == 2050
    assert payload["tac"] == 112


def test_canonical_status_payload_translates_band_alias_without_reexporting_legacy_keys():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        nsa_active=True,
        nr_band=78,
        nr_pci=42,
        nr_arfcn=641760,
    )
    computed = ComputedSignals(cq_cell=0.44, cq_vector_a_valid=True, cq_cell_source="live")

    payload = _canonical_status_from_raw(
        base_status={
            "provider": "LegacyVendor",
            "plmn": "12345",
            "provider_name": "",
            "provider_plmn": "",
            "band": 20,
            "nr5g_band": 1,
        },
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["provider_name"] == ""
    assert payload["provider_plmn"] == ""
    assert payload["serving_band"] == 20
    assert payload["nr_band"] == 78
    assert abs(float(payload["cq_cell"]) - 0.44) <= 0.001
    assert payload["cq_vector_a_valid"] is True
    assert payload["cq_cell_source"] == "live"
    for key in {
        "provider", "plmn", "band", "primary_band",
        "traffic_active", "nr5g_active", "nr5g_band", "nr5g_pci", "nr5g_arfcn",
    }:
        assert key not in payload


def test_canonical_status_payload_ignores_boolean_plmn_alias_and_uses_constraint_plmn():
    modem = ModemInput.safe_defaults(radio_connected=True)
    setattr(modem, "provider_name", "A1")
    setattr(modem, "provider_plmn", True)
    computed = ComputedSignals()

    payload = _canonical_status_from_raw(
        base_status={"provider_name": "A1", "plmn": True},
        modem=modem,
        computed=computed,
        constraints=type("C", (), {"provider_name": "A1", "provider_plmn": "22005"})(),
        state_name="WORKING",
    )

    assert payload["provider_plmn"] == "22005"


def test_status_payload_exports_congestion_authority_fields():
    modem = ModemInput.safe_defaults(radio_connected=True)
    computed = ComputedSignals(
        congestion_pct=61.0,
        congestion_pct_valid=True,
        congestion_internal_bucket="mild",
        congestion_bucket="mild",
        congestion_state_avg_pct=58.0,
        congestion_state_bucket="normal",
        congestion_source="rsrq",
        congestion_flags_valid=True,
        congestion_bad_now=True,
        congestion_pressure_high=False,
        congestion_pressure_moderate=True,
        congestion_kind_hint="attached_cell",
    )

    payload = _canonical_status_from_raw(
        base_status={},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["congestion_pct"] == 61.0
    assert payload["congestion_internal_bucket"] == "mild"
    assert payload["congestion_bucket"] == "mild"
    assert payload["congestion_state_avg_pct"] == 58.0
    assert payload["congestion_state_bucket"] == "normal"
    assert payload["congestion_source"] == "rsrq"
    assert payload["congestion_flags_valid"] is True
    assert payload["congestion_bad_now"] is True
    assert payload["congestion_pressure_high"] is False
    assert payload["congestion_pressure_moderate"] is True
    assert payload["congestion_kind_hint"] == "attached_cell"


def test_operator_summary_reports_fast_congestion_before_state_confirms():
    modem = ModemInput.safe_defaults(radio_connected=True, sinr_db=8.0, rsrp_dbm=-70.0, rsrq_db=-11.0)
    computed = ComputedSignals(
        rtt_ms=350.0,
        rtt_sliding_avg=190.0,
        congestion_pct=72.0,
        congestion_pct_valid=True,
        congestion_bucket="high",
        congestion_state_avg_pct=30.0,
        congestion_state_bucket="normal",
    )

    summary = _operator_summary_from_raw(
        modem=modem,
        computed=computed,
        state_name="WORKING",
        payload={
            "brain": {},
            "incident_assessment": {
                "incident_class": "healthy",
                "root_cause": {"probable_cause": "none"},
            },
            "calibration": {},
            "action_text": "",
        },
    )

    assert "Network delay is high" in summary["status"]
    assert "Autopilot is running normally" not in summary["status"]


def test_operator_summary_prioritizes_local_traffic_warning_in_working():
    modem = ModemInput.safe_defaults(radio_connected=True, sinr_db=12.0, rsrp_dbm=-75.0, rsrq_db=-9.0)
    computed = ComputedSignals(rtt_ms=80.0, rtt_sliding_avg=80.0)

    summary = _operator_summary_from_raw(
        modem=modem,
        computed=computed,
        state_name="WORKING",
        payload={
            "brain": {
                "decision_context": {
                    "warning_class": "congestion",
                    "warning_source": "local_traffic",
                    "warning_mechanism": "pipe_saturation",
                },
            },
            "incident_assessment": {
                "incident_class": "healthy",
                "root_cause": {"probable_cause": "none"},
            },
            "calibration": {},
            "action_text": "",
        },
    )

    assert "Heavy local traffic" in summary["status"]
    assert "Autopilot is running normally" not in summary["status"]


def test_status_payload_exports_configured_pool_and_antenna_mode_lanes():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        ca_band_signature="",
        mcs_ul=14,
        mcs_ul_supported=True,
        selected_band_set=[3],
        configured_band_pool=[1, 3, 20],
        configured_band_pool_signature="B1+B3+B20",
        configured_band_pool_source="routeros_lte_band_mask",
        configured_band_pool_status="stale",
        configured_band_pool_age_s=17.5,
        configured_band_pool_last_sample_ts=1234.0,
        antenna="External",
        antenna_mode="External",
        antenna_mode_source="routeros",
        antenna_switch_api_supported=False,
        external_antenna_ports_present=True,
    )
    computed = ComputedSignals()

    payload = _export_status_from_raw(
        base_status={},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["ca_signature"] == ""
    assert payload["mcs_ul_supported"] is True
    assert payload["configured_band_pool"] == [1, 3, 20]
    assert payload["configured_band_pool_signature"] == "B1+B3+B20"
    assert payload["configured_band_pool_source"] == "routeros_lte_band_mask"
    assert payload["configured_band_pool_status"] == "stale"
    assert payload["configured_band_pool_age_s"] == 17.5
    assert payload["configured_band_pool_last_sample_ts"] == 1234.0
    assert payload["antenna"] == "External"
    assert payload["antenna_mode"] == "External"
    assert payload["antenna_mode_source"] == "routeros"
    assert payload["antenna_switch_api_supported"] is False
    assert payload["external_antenna_ports_present"] is True


def test_status_payload_uses_modem_owner_configured_pool():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        selected_band_set=[],
        configured_band_pool=[1, 3, 20, 28],
        configured_band_pool_signature="B1+B3+B20+B28",
        configured_band_pool_source="net-mode-list",
    )
    computed = ComputedSignals()

    payload = _export_status_from_raw(
        base_status={
            "configured_band_pool": [1, 3, 20, 28],
            "configured_band_pool_signature": "B1+B3+B20+B28",
            "configured_band_pool_source": "net-mode-list",
        },
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["configured_band_pool"] == [1, 3, 20, 28]
    assert payload["configured_band_pool_signature"] == "B1+B3+B20+B28"
    assert payload["configured_band_pool_source"] == "net-mode-list"


def test_status_payload_does_not_filter_owner_configured_pool_by_provider_profile():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        provider_plmn="22005",
        provider_name="A1",
        configured_band_pool=[1, 3, 5, 7, 8, 20, 28, 32, 38, 40, 41, 42, 43],
        configured_band_pool_signature="B1+B3+B5+B7+B8+B20+B28+B32+B38+B40+B41+B42+B43",
        configured_band_pool_source="net-mode-list",
    )
    computed = ComputedSignals()

    payload = _export_status_from_raw(
        base_status={},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["configured_band_pool"] == [1, 3, 5, 7, 8, 20, 28, 32, 38, 40, 41, 42, 43]
    assert payload["configured_band_pool_signature"] == "B1+B3+B5+B7+B8+B20+B28+B32+B38+B40+B41+B42+B43"
    assert payload["configured_band_pool_source"] == "net-mode-list"


def test_status_payload_does_not_intersect_owner_configured_pool_with_selected_band_subset():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        provider_plmn="22005",
        provider_name="A1",
        selected_band_set=[1, 3, 20],
        configured_band_pool=[1, 3, 5, 7, 8, 20, 28, 32, 38, 40, 41, 42, 43],
        configured_band_pool_signature="B1+B3+B5+B7+B8+B20+B28+B32+B38+B40+B41+B42+B43",
        configured_band_pool_source="net-mode-list",
    )
    computed = ComputedSignals()

    payload = _export_status_from_raw(
        base_status={},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["configured_band_pool"] == [1, 3, 5, 7, 8, 20, 28, 32, 38, 40, 41, 42, 43]
    assert payload["configured_band_pool_signature"] == "B1+B3+B5+B7+B8+B20+B28+B32+B38+B40+B41+B42+B43"
    assert payload["configured_band_pool_source"] == "net-mode-list"


def test_status_payload_exports_separate_dl_jitter_vector_fields():
    modem = ModemInput.safe_defaults(radio_connected=True)
    computed = ComputedSignals(
        rtt_dl_jitter_bucket="20_30",
        rtt_dl_jitter_trimmed_ms=186.0,
        rtt_dl_jitter_baseline_ms=74.0,
        rtt_dl_jitter_delta_ms=112.0,
        rtt_dl_jitter_pct=11.2,
        rtt_dl_jitter_valid=True,
    )

    payload = _canonical_status_from_raw(
        base_status={},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["rtt_dl_jitter_bucket"] == "20_30"
    assert payload["rtt_dl_jitter_trimmed_ms"] == 186.0
    assert payload["rtt_dl_jitter_baseline_ms"] == 74.0
    assert payload["rtt_dl_jitter_delta_ms"] == 112.0
    assert payload["rtt_dl_jitter_pct"] == 11.2
    assert payload["rtt_dl_jitter_valid"] is True


def test_status_payload_exports_canonical_delta_display_surface():
    modem = ModemInput.safe_defaults(radio_connected=True)
    modem.rsrq_db = -8.4
    modem.rsrq_source = "reported"
    computed = ComputedSignals(
        rtt_delta_valid=True,
        rtt_quiet_delta=18.6,
        rtt_quiet_held=False,
        rtt_delta_display_ms=18.6,
        rtt_delta_display_valid=True,
        rtt_delta_display_held=False,
        rsrq_baseline=-8.8,
        rsrq_baseline_valid=True,
        rsrq_gap=4.2,
        rsrq_gap_valid=True,
        rsrq_delta_display_db=4.2,
        rsrq_delta_display_valid=True,
        rsrq_delta_display_held=False,
        rsrq_baseline_display_db=-8.8,
        rsrq_baseline_display_valid=True,
        rsrq_baseline_display_held=False,
    )

    payload = _canonical_status_from_raw(
        base_status={},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["rtt_delta_display_ms"] == 18.6
    assert payload["rtt_delta_display_valid"] is True
    assert payload["rtt_delta_display_held"] is False
    assert payload["rsrq_delta_display_db"] == 4.2
    assert payload["rsrq_delta_display_valid"] is True
    assert payload["rsrq_delta_display_held"] is False
    assert payload["rsrq_baseline_display_db"] == -8.8
    assert payload["rsrq_baseline_display_valid"] is True
    assert payload["rsrq_baseline_display_held"] is False


def test_status_payload_preserves_previous_delta_display_surface_when_current_sample_invalid():
    modem = ModemInput.safe_defaults(radio_connected=True, timestamp=110.0)
    modem.rsrq_db = -8.4
    modem.rsrq_source = "reported"
    computed = ComputedSignals(
        rtt_delta_valid=False,
        rtt_quiet_delta=0.0,
        rtt_quiet_held=False,
        rsrq_baseline=0.0,
        rsrq_baseline_valid=False,
        rsrq_gap=0.0,
        rsrq_gap_valid=False,
    )

    payload = _canonical_status_from_raw(
        base_status={
            "rtt_delta_display_ms": 18.6,
            "rtt_delta_display_valid": True,
            "rtt_delta_display_held": False,
            "rtt_delta_display_live_ts": 100.0,
            "rsrq_delta_display_db": 4.2,
            "rsrq_delta_display_valid": True,
            "rsrq_delta_display_held": False,
            "rsrq_delta_display_live_ts": 100.0,
            "rsrq_baseline_display_db": -8.8,
            "rsrq_baseline_display_valid": True,
            "rsrq_baseline_display_held": False,
            "rsrq_baseline_display_live_ts": 100.0,
        },
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["rtt_delta_display_ms"] == 18.6
    assert payload["rtt_delta_display_valid"] is True
    assert payload["rtt_delta_display_held"] is True
    assert payload["rtt_delta_display_live_ts"] == 100.0
    assert payload["rsrq_delta_display_db"] == 4.2
    assert payload["rsrq_delta_display_valid"] is True
    assert payload["rsrq_delta_display_held"] is True
    assert payload["rsrq_delta_display_live_ts"] == 100.0
    assert payload["rsrq_baseline_display_db"] == -8.8
    assert payload["rsrq_baseline_display_valid"] is True
    assert payload["rsrq_baseline_display_held"] is True


def test_status_payload_uses_current_held_rsrq_operator_display_without_previous_surface():
    modem = ModemInput.safe_defaults(radio_connected=True, timestamp=110.0)
    modem.rsrq_db = -12.2
    modem.rsrq_source = "reported"
    computed = ComputedSignals(
        rsrq_gap=0.0,
        rsrq_gap_valid=False,
        rsrq_delta_display_db=4.2,
        rsrq_delta_display_valid=True,
        rsrq_delta_display_held=True,
        rsrq_delta_display_live_ts=110.0,
        rsrq_baseline=-8.8,
        rsrq_baseline_valid=True,
        rsrq_baseline_display_db=-8.8,
        rsrq_baseline_display_valid=True,
        rsrq_baseline_display_held=True,
        rsrq_baseline_display_live_ts=110.0,
    )

    payload = _canonical_status_from_raw(
        base_status={},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["rsrq_delta_display_db"] == 4.2
    assert payload["rsrq_delta_display_valid"] is True
    assert payload["rsrq_delta_display_held"] is True
    assert payload["rsrq_delta_display_live_ts"] == 110.0


def test_status_payload_does_not_derive_rsrq_delta_from_raw_gap():
    modem = ModemInput.safe_defaults(radio_connected=True, timestamp=110.0)
    modem.rsrq_db = -11.0
    modem.rsrq_source = "reported"
    computed = ComputedSignals(
        rsrq_gap=2.0,
        rsrq_gap_valid=True,
        rsrq_baseline=-9.0,
        rsrq_baseline_valid=True,
        rsrq_baseline_display_db=-9.0,
        rsrq_baseline_display_valid=True,
        rsrq_baseline_display_held=False,
        rsrq_baseline_display_live_ts=100.0,
        rsrq_delta_display_db=0.0,
        rsrq_delta_display_valid=False,
        rsrq_delta_display_held=False,
        rsrq_delta_display_live_ts=0.0,
    )

    payload = _canonical_status_from_raw(
        base_status={},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["rsrq_delta_display_db"] is None
    assert payload["rsrq_delta_display_valid"] is False
    assert payload["rsrq_delta_display_held"] is False
    assert payload["rsrq_baseline_display_db"] == -9.0
    assert payload["rsrq_baseline_display_valid"] is True


def test_status_payload_exports_radio_detail_contract_fields():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        mcs_dl=17,
        mcs_ul=6,
        cqi=11,
        ri=2,
    )
    modem.tx_power = "PPusch:21dBm"
    modem.cqi0 = 11
    modem.cqi1 = 9
    modem.mcs_dl0 = 17
    modem.mcs_dl1 = 12
    modem.qam_dl0 = "64QAM"
    modem.qam_dl1 = "16QAM"
    modem.qam_ul = 4
    modem.dl_bandwidth = "20 MHz"
    modem.ul_bandwidth = "15 MHz"
    modem.dl_bandwidth_mhz = 20.0
    modem.ul_bandwidth_mhz = 15.0
    modem.dl_freq_mhz = 1850.0
    modem.ul_freq_mhz = 1745.0
    modem.antenna = "External"
    computed = ComputedSignals()

    payload = _canonical_status_from_raw(
        base_status={},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["cqi0"] == 11
    assert payload["cqi1"] == 9
    assert payload["mcs_dl0"] == 17
    assert payload["mcs_dl1"] == 12
    assert payload["qam_dl0"] == "64QAM"
    assert payload["qam_dl1"] == "16QAM"
    assert payload["qam_ul"] == "16QAM"
    assert payload["tx_power"] == "PPusch:21dBm"
    assert payload["dl_bandwidth"] == "20 MHz"
    assert payload["ul_bandwidth"] == "15 MHz"
    assert payload["dl_bandwidth_mhz"] == 20.0
    assert payload["ul_bandwidth_mhz"] == 15.0
    assert payload["dl_freq_mhz"] == 1850.0
    assert payload["ul_freq_mhz"] == 1745.0
    assert payload["antenna"] == "External"


def test_status_payload_exports_canonical_scheduler_display_surface():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        mcs_dl=0,
        mcs_ul=0,
        cqi=0,
        ri=1,
        ri_valid=True,
        ri_source="native_signal",
    )
    modem.mcs_dl0 = 0
    modem.mcs_dl1 = 0
    modem.cqi0 = 0
    modem.cqi1 = 0
    modem.qam_dl0 = 2
    modem.qam_dl1 = ""
    modem.qam_ul = 2
    computed = ComputedSignals()

    payload = _canonical_status_from_raw(
        base_status={},
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["ri"] == 1
    assert payload["mcs_dl0"] == 0
    assert payload["mcs_ul"] == 0
    assert payload["cqi0"] == 0
    assert payload["cqi1"] is None
    assert payload["qam_dl0"] == "QPSK"
    assert payload["qam_dl1"] == ""
    assert payload["qam_ul"] == "QPSK"
    assert payload["me0"] == 2
    assert payload["me1"] is None
    assert payload["me_ul"] == 2
    for key in {
        "scheduler_zero_authoritative",
        "scheduler_stream1_available",
        "scheduler_ri_display",
        "scheduler_mcs_dl0_display",
        "scheduler_mcs_ul_display",
        "scheduler_cqi0_display",
        "scheduler_cqi1_display",
        "scheduler_qam0_display",
        "scheduler_qam1_display",
        "scheduler_qam_ul_display",
    }:
        assert key not in payload


def test_canonical_scheduler_me_follows_resolved_qam_efficiency():
    for qam, expected_me in (
        ("QPSK", 2),
        ("16QAM", 4),
        ("64QAM", 6),
        ("256QAM", 8),
    ):
        surface = build_scheduler_display_surface(
            build_scheduler_display_contract(
                {
                    "radio_connected": True,
                    "scheduler_source": "native",
                    "qam_dl0": qam,
                }
            )
        )
        assert surface["scheduler_qam0_display"] == qam
        assert surface["scheduler_me0_display"] == expected_me

    unavailable = build_scheduler_display_surface(
        build_scheduler_display_contract({"radio_connected": True})
    )
    assert unavailable["scheduler_qam0_display"] == ""
    assert unavailable["scheduler_me0_display"] is None


def test_status_payload_scheduler_trust_respects_authoritative_modem_profile():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        vendor="Huawei",
        model="B535-333",
        firmware="11.0.5.51(H470SP6C9831)",
        mcs_dl=12,
        mcs_ul=9,
        cqi=7,
        ri=1,
    )
    modem.mcs_dl0 = 12
    modem.mcs_dl1 = 0
    modem.mcs_ul = 9
    modem.cqi0 = 7
    modem.cqi1 = 0
    modem.qam_dl0 = 6
    modem.qam_dl1 = ""
    modem.qam_ul = 4
    computed = ComputedSignals()

    payload = _canonical_status_from_raw(
        base_status={
            "backend": "b535",
            "modem_profile": {
                "profile_id": "huawei_b535_232_model",
                "field_trust": {
                    "cqi": "confirmed",
                    "cqi0": "confirmed",
                    "cqi1": "confirmed",
                    "mcs_dl0": "confirmed",
                    "mcs_dl1": "confirmed",
                    "mcs_ul": "confirmed",
                    "qam_dl0": "synthetic",
                    "qam_dl1": "synthetic",
                    "qam_ul": "synthetic",
                },
            },
            "resolved_identity_card": {
                "backend": "b535",
                "model": "B535-232",
                "firmware": "10.0.1.1(H191SP64C983)",
                "provider_name": "A1",
                "provider_plmn": "22005",
            },
        },
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["mcs_dl0"] == 12
    assert payload["mcs_ul"] == 9
    assert payload["cqi0"] == 7
    assert payload["qam_dl0"] == "64QAM"
    assert payload["qam_ul"] == "16QAM"
    assert "scheduler_zero_authoritative" not in payload
    assert "scheduler_mcs_dl0_display" not in payload


def test_status_payload_mikrotik_maps_native_dl_cqi_to_stream0_only():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        vendor="MikroTik",
        model="D53G-5HacD2HnD&EG120K-EA",
        firmware="7.21 (stable)",
        mcs_dl=7,
        mcs_ul=0,
        cqi=9,
        ri=2,
    )
    modem.mcs_dl0 = 7
    modem.mcs_dl1 = 0
    modem.cqi0 = 9
    modem.cqi1 = 0
    modem.qam_dl0 = 4
    modem.qam_dl1 = ""
    modem.qam_ul = ""
    modem.mcs_ul_supported = False

    payload = _canonical_status_from_raw(
        base_status={"backend": "mikrotik"},
        modem=modem,
        computed=ComputedSignals(),
        constraints=None,
        state_name="WORKING",
    )

    assert payload["cqi"] == 9
    assert payload["cqi0"] == 9
    assert payload["cqi1"] == 0
    assert payload["mcs_ul"] == 0
    assert payload["qam_dl0"] == "16QAM"
    assert payload["qam_dl1"] == ""
    assert payload["qam_ul"] == ""
    assert "scheduler_cqi0_display" not in payload
    assert "scheduler_stream1_available" not in payload


def test_status_payload_scheduler_trust_uses_capability_owner_identity():
    modem = ModemInput.safe_defaults(
        radio_connected=True,
        vendor="Huawei",
        model="B535-232",
        firmware="10.0.1.1(H191SP64C983)",
        mcs_dl=12,
        mcs_ul=9,
        cqi=7,
        ri=1,
    )
    modem.mcs_dl0 = 12
    modem.mcs_dl1 = 0
    modem.mcs_ul = 9
    modem.cqi0 = 7
    modem.cqi1 = 0
    modem.qam_dl0 = 6
    modem.qam_dl1 = ""
    modem.qam_ul = 4
    computed = ComputedSignals()

    payload = _canonical_status_from_raw(
        base_status={
            "backend": "b535",
            "runtime_identity": {
                "backend": "b535",
                "vendor": "Huawei",
                "model": "B535-333",
                "firmware": "11.0.5.51(H470SP6C9831)",
                "provider_name": "A1",
                "provider_plmn": "22005",
            },
            "resolved_identity_card": {
                "backend": "b535",
                "model": "B535-333",
                "firmware": "11.0.5.51(H470SP6C9831)",
                "provider_name": "A1",
                "provider_plmn": "22005",
            },
        },
        modem=modem,
        computed=computed,
        constraints=None,
        state_name="WORKING",
    )

    assert payload["mcs_dl0"] == 0
    assert payload["mcs_ul"] == 0
    assert payload["cqi0"] == 0
    assert payload["qam_dl0"] == ""
    assert payload["qam_ul"] == ""
    assert "scheduler_zero_authoritative" not in payload
    assert "scheduler_mcs_dl0_display" not in payload


def test_modem_live_translation_maps_native_live_aliases_for_unknown_modem():
    modem = ModemInput.safe_defaults(radio_connected=True)

    payload = _canonical_status_from_raw(
        base_status={
            "backend": "unknown_http",
            "native_fields": {
                "pband": 3,
                "ca_pool": "B1+B3+B20",
                "dl_cqi": 11,
                "dl_mcs": 13,
                "dl_qam": "64QAM",
                "dl_bw_mhz": 20,
                "dl_freq": 1835,
                "tracking_area_code": 33001,
            },
        },
        modem=modem,
        computed=ComputedSignals(),
        constraints=None,
        state_name="WORKING",
    )

    assert payload["serving_band"] == 3
    assert payload["serving_band_signature"] == "B3"
    assert payload["configured_band_pool"] == [1, 3, 20]
    assert payload["configured_band_pool_signature"] == "B1+B3+B20"
    assert payload["cqi0"] == 11
    assert payload["mcs_dl0"] == 13
    assert payload["qam_dl0"] == "64QAM"
    assert payload["dl_bandwidth_mhz"] == 20
    assert payload["dl_freq_mhz"] == 1835
    assert payload["tac"] == 33001
    assert "scheduler_cqi0_display" not in payload
    assert "scheduler_mcs_dl0_display" not in payload


def test_modem_live_translation_does_not_backfill_live_bands_from_status_mirrors():
    modem = ModemInput.safe_defaults(radio_connected=True)

    payload = _canonical_status_from_raw(
        base_status={
            "backend": "unknown_http",
            "current_state": {
                "pband": 7,
                "ca_pool": "B7+B20",
            },
            "operator": {
                "pband": 1,
                "ca_pool": "B1+B3",
            },
        },
        modem=modem,
        computed=ComputedSignals(),
        constraints=None,
        state_name="WORKING",
    )

    assert payload["serving_band"] == 0
    assert payload["serving_band_signature"] == ""
    assert payload["selected_band_set"] == []
    assert payload["selected_band_set_signature"] == ""
    assert payload["configured_band_pool"] == []
    assert payload["configured_band_pool_signature"] == ""


def test_modem_live_translation_still_respects_discovered_capability_support():
    modem = ModemInput.safe_defaults(radio_connected=True)

    payload = _canonical_status_from_raw(
        base_status={
            "backend": "b535",
            "runtime_identity": {
                "backend": "b535",
                "vendor": "Huawei",
                "model": "B535-333",
                "firmware": "11.0.5.51(H470SP6C9831)",
                "provider_name": "A1",
                "provider_plmn": "22005",
            },
            "native_fields": {
                "dl_cqi": 11,
                "dl_mcs": 13,
                "dl_qam": "64QAM",
            },
        },
        modem=modem,
        computed=ComputedSignals(),
        constraints=None,
        state_name="WORKING",
    )

    assert payload["cqi0"] == 0
    assert payload["mcs_dl0"] == 0
    assert payload["qam_dl0"] == ""
    assert "scheduler_cqi0_display" not in payload
    assert "scheduler_mcs_dl0_display" not in payload
    assert "scheduler_qam0_display" not in payload


def test_export_status_alias_adapter_rehydrates_forensic_fields():
    payload = add_legacy_status_aliases(
        {
            "provider_name": "A1",
            "provider_plmn": "22005",
            "serving_band": 3,
            "has_traffic": True,
            "cq_cell": 0.8,
            "cq_cell_valid": True,
            "cq_cell_source": "live",
            "nsa_active": True,
            "nr_band": 78,
            "nr_pci": 42,
            "nr_arfcn": 641760,
            "nr_rsrp_dbm": -81.0,
            "nr_rsrq_db": -10.0,
            "nr_sinr_db": 17.5,
        }
    )

    assert payload["provider"] == "A1"
    assert payload["plmn"] == "22005"
    assert payload["band"] == 3
    assert payload["primary_band"] == 3
    assert payload["traffic_active"] is True
    assert payload["cq_cell"] == 0.8
    assert payload["cq_cell_valid"] is True
    assert payload["cq_cell_source"] == "live"
    assert payload["cq_cell"] == 0.8
    assert payload["nr5g_active"] is True
    assert payload["nr5g_band"] == 78
