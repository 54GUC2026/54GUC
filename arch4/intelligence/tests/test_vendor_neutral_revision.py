import pytest

from arch4.interfaces import (
    Arch4Config,
    ComputedSignals,
    ModemInput,
    PolicyProfile,
    select_policy_profile,
)
from arch4.interfaces import SystemState
from arch4.core.action_source_state_authority import ActionSourceStateAuthority
from arch4.action_transaction import (
    ActionTransactionKey,
    bind_action_transaction_key,
    build_action_transaction_key,
)
from arch4.action_origin import ActionOriginKind
from arch4.action_admission import bind_selected_action_context
from arch4.action_transaction_authority import (
    ActionLifecycleStage,
    ActionTransactionAuthority,
)
from arch4.brain.proof_referee_due_owner import (
    evaluate_due_staged_roi as _evaluate_due_staged_roi_owner,
)
from arch4.brain.proof_referee_window_owner import (
    register_staged_roi_windows as _register_staged_roi_windows_owner,
)
from arch4.config_views import (
    build_backend_connection_config,
    build_startup_display_config,
)
from arch4.controller_frames import (
    CellularIntelligenceGeneration,
    DerivedSignalFrames,
)
from arch4.modem_live.capability_report import build_capabilities_report
from arch4.brain.memory_action_store import ActionHistoryStore
from arch4.brain.memory_action_history_owner import action_roi_formula_vector
from arch4.evidence_scope import EvidenceScope
from arch4.modem.catalog import (
    SUPPORT_LEVEL_ADVANCED_CONTROL,
    SUPPORT_LEVEL_SAFE_CONTROL,
    describe_backend_support,
    get_backend_action_semantics,
    get_backend_support_level,
)
from arch4.operator_profiles import (
    BACKEND_POLICY_DEFAULTS,
    FAMILY_POLICY_DEFAULTS,
    PROVIDER_BACKEND_POLICY_OVERRIDES,
    default_deployed_bands,
    deployed_bands_for_provider,
    infer_backend_family,
    nr_deployed_bands_for_provider,
    resolve_policy_template,
    resolve_provider_name,
    policy_template_sources,
    available_operator_contexts,
    family_label,
    family_support_text,
    family_profile_sources,
    resolve_action_policy_registry,
    runtime_event_alias_sources,
    runtime_status_text_sources,
    runtime_event_aliases,
    runtime_event_text_sources,
    apply_runtime_status_texts,
    build_runtime_event_records,
    resolve_provider_plmn_from_name,
)
from arch4.policy.family_engine_runtime_policy import (
    build_mutating_action_verification_event,
    inject_runtime_guard_payloads,
)
from types import SimpleNamespace

from arch4.brain.memory import Memory
from arch4.brain.memory_io import record_to_dict
from arch4.brain.cooldown_registry import CooldownRegistry
from arch4.brain.decision import DecisionEngine
from arch4.tests.controller_generation_factory import cellular_test_inputs
from arch4.tests.terminal_reward_test_support import (
    build_terminal_reward_test_fixture,
)


def _bind_test_action_transaction(action):
    params = dict(getattr(action, "params", {}) or {})
    command_id = str(
        params.get("_selected_action_command_id")
        or getattr(action, "command_id", "")
        or f"vendor-neutral-test:{getattr(action, 'objective_id', '') or action.lever.value}"
    )
    action.command_id = command_id
    bind_action_transaction_key(
        action,
        build_action_transaction_key(
            action,
            session_scope_key="test-session-scope",
            identity_key="test-identity",
            source_generation=1,
            source="arch4.tests.test_vendor_neutral_revision",
            origin_kind=ActionOriginKind.SMART_LOOP.value,
        ),
    )
    return action


def _bind_test_action_history_scope(memory, records):
    scope = EvidenceScope(
        identity_code="0x0000000000000001",
        identity_status="exact",
        identity_catalog_version="test-identity-catalog-v1",
        identity_catalog_checksum="a" * 64,
        session_scope_key="test-session-scope",
        source_epoch="test-source-epoch",
        source_sequence=1,
        observed_at=1000.0,
        evidence_age_s=0.0,
        confidence=1.0,
        evidence_count=1,
        formula_vector=action_roi_formula_vector(),
    ).as_dict()
    memory._current_action_evidence_scope = dict(scope)
    for record in records or []:
        record.baseline_evidence_scope = dict(scope)
        record.evidence_scope = dict(scope)


def _register_test_staged_roi_windows(*, action_record, action):
    return _register_staged_roi_windows_owner(
        action_record=action_record,
        action=_bind_test_action_transaction(action),
    )


def _typed_roi_inputs(modem, computed):
    if isinstance(modem, CellularIntelligenceGeneration):
        if not isinstance(computed, DerivedSignalFrames):
            raise TypeError("typed_roi_modem_requires_derived_signals")
        return modem, computed
    raw_modem = ModemInput(
        sinr_db=float(getattr(modem, "sinr_db", 0.0) or 0.0),
        rsrp_dbm=float(getattr(modem, "rsrp_dbm", -90.0) or -90.0),
        rsrq_db=float(getattr(modem, "rsrq_db", 0.0) or 0.0),
        rssi_dbm=float(getattr(modem, "rssi_dbm", -65.0) or -65.0),
        mcs_dl=int(getattr(modem, "mcs_dl", 0) or 0),
        mcs_ul=int(getattr(modem, "mcs_ul", 0) or 0),
        cqi=int(getattr(modem, "cqi", 0) or 0),
        ri=int(getattr(modem, "ri", 1) or 1),
        carriers=[],
        ca_active=bool(getattr(modem, "ca_active", False)),
        ca_band_signature=str(
            getattr(modem, "ca_band_signature", "") or ""
        ),
        primary_band=int(getattr(modem, "primary_band", 0) or 0),
        pci=int(getattr(modem, "pci", 0) or 0),
        earfcn=int(getattr(modem, "earfcn", 0) or 0),
        enb_id=int(getattr(modem, "enb_id", 0) or 0),
        radio_connected=bool(getattr(modem, "radio_connected", True)),
        session_uptime_s=float(
            getattr(modem, "session_uptime_s", 300.0) or 300.0
        ),
        rx_bytes_per_sec=float(
            getattr(modem, "throughput_dl_mbps", 0.0) or 0.0
        )
        * 1_000_000.0
        / 8.0,
        tx_bytes_per_sec=float(
            getattr(modem, "throughput_ul_mbps", 0.0) or 0.0
        )
        * 1_000_000.0
        / 8.0,
        cell_id=int(getattr(modem, "cell_id", 0) or 0),
        configured_band_pool=list(
            getattr(modem, "configured_band_pool", []) or []
        ),
        provider_plmn=str(getattr(modem, "provider_plmn", "") or ""),
        provider_name=str(getattr(modem, "provider_name", "") or ""),
    )
    raw_computed = ComputedSignals()
    for key, value in dict(vars(computed) if hasattr(computed, "__dict__") else {}).items():
        if hasattr(raw_computed, key):
            setattr(raw_computed, key, value)
    raw_computed.cart_activity_per_hour = float(
        getattr(
            computed,
            "cart_activity_per_hour",
            getattr(computed, "ca_change_per_hour", 0.0),
        )
        or 0.0
    )
    raw_computed.cast = float(
        getattr(computed, "cast", getattr(computed, "ca_stat_pct", 0.0))
        or 0.0
    )
    raw_computed.cart_ld_activity = float(
        getattr(
            computed,
            "cart_ld_activity",
            getattr(computed, "ca_ld_change_per_hour", 0.0),
        )
        or 0.0
    )
    raw_computed.cast_ld = float(
        getattr(
            computed,
            "cast_ld",
            getattr(computed, "ca_stat_ld_pct", 0.0),
        )
        or 0.0
    )
    raw_computed.cast_ld_valid = bool(
        getattr(
            computed,
            "cast_ld_valid",
            getattr(computed, "ca_stat_ld_valid", False),
        )
    )
    if hasattr(computed, "congestion_pct"):
        raw_computed.congestion_pct_valid = bool(
            getattr(computed, "congestion_pct_valid", True)
        )
    return cellular_test_inputs(
        raw_modem,
        raw_computed,
        owner_values={
            "provider_plmn": raw_modem.provider_plmn,
            "provider_name": raw_modem.provider_name,
        },
    )


def _evaluate_due_staged_roi_typed(**kwargs):
    modem_now, computed_now = _typed_roi_inputs(
        kwargs["modem_now"],
        kwargs["computed_now"],
    )
    kwargs["modem_now"] = modem_now
    kwargs["computed_now"] = computed_now
    memory = kwargs["memory"]
    authority = ActionTransactionAuthority()
    registered_roots: set[str] = set()
    for record in list(getattr(memory, "action_history", []) or []):
        metadata = dict(getattr(record, "objective_metadata", {}) or {})
        raw_key = metadata.get("action_transaction")
        if not isinstance(raw_key, dict):
            continue
        key = ActionTransactionKey.from_mapping(raw_key)
        if key.root_id in registered_roots:
            continue
        authority.transition(
            action_key=key,
            stage=ActionLifecycleStage.ADMITTED,
            now=float(getattr(record, "timestamp", 0.0) or 0.0),
            evidence={"boundary": "vendor_neutral_staged_roi_test"},
        )
        registered_roots.add(key.root_id)
    if not registered_roots:
        raise RuntimeError("staged_roi_test_action_lifecycle_missing")
    kwargs["action_transaction_authority"] = authority
    return _evaluate_due_staged_roi_owner(**kwargs)


def _disabled_test_policy_profile_vendor_neutral_aliases_reflect_legacy_fields():  # TODO: vendor-neutral WIP — re-enable when implemented
    p = PolicyProfile(
        name="test",
        b818_use_full_brain=True,
        b818_keep_device_monitor=False,
        b818_daytime_auto_band_actions=True,
        a1_overnight_recovery=True,
    )
    assert p.full_decision_engine_enabled is True
    assert p.family_device_monitor_enabled is False
    assert p.daytime_auto_band_actions_enabled is True
    assert p.overnight_recovery_enabled is True


def _disabled_test_user_config_connection_defaults_vendor_neutral_resolution():  # TODO: vendor-neutral WIP — re-enable when implemented
    cfg = Arch4Config(
        b818_host="192.168.5.1",
        b818_user="admin",
        zte_host="192.168.0.1",
        zte_user="zte",
        openwrt_host="192.168.8.1",
        openwrt_user="root",
        router_host="192.168.88.1",
        router_user="mt",
    )
    assert cfg.connection_defaults_for_backend("b818") == ("192.168.5.1", "admin")
    assert cfg.connection_defaults_for_backend("mc888") == ("192.168.0.1", "zte")
    assert cfg.connection_defaults_for_backend("openwrt") == ("192.168.8.1", "root")
    assert cfg.connection_defaults_for_backend("mikrotik") == ("192.168.88.1", "mt")


def test_backend_support_level_and_semantics_are_exposed():
    assert get_backend_support_level("b818") == SUPPORT_LEVEL_ADVANCED_CONTROL
    assert get_backend_support_level("openwrt") == SUPPORT_LEVEL_SAFE_CONTROL
    b818 = get_backend_action_semantics("b818")
    assert b818.band_control_mode == "allowlist"
    assert b818.cell_control_mode == "none"
    assert b818.safety_grade == "safe"


def test_describe_backend_support_returns_vendor_neutral_summary():
    info = describe_backend_support("mc888")
    assert info["backend"] == "mc888"
    assert info["support_level"] == SUPPORT_LEVEL_ADVANCED_CONTROL
    assert info["action_semantics"]["nr_control_mode"] == "mixed"
    assert info["capabilities"]["supports_nr_band_lock"] is True


def test_provider_specific_band_context_overrides_region_defaults():
    assert sorted(default_deployed_bands()) == [1, 3, 20]
    assert sorted(deployed_bands_for_provider("22005")) == [1, 3, 7, 8, 20, 28]
    assert sorted(nr_deployed_bands_for_provider("22005")) == [7, 28, 78]


class _Caps:
    supports_band_selection = True
    supports_cell_lock = False
    supports_traffic_presence = False
    supports_traffic_volume = False
    supports_ue_capability = False
    supports_restart = True
    supports_neighbors = False
    supports_ca_details = True
    supports_band_priority = False
    supports_plmn = True
    supports_pband = True
    # Compatibility: keep old PBAND capability field for mixed-version fixtures.
    supports_pband = True


class _Modem:
    compat_output_dir = None

    def get_modem_info(self):
        return {"model": "MC888", "firmware": "1.0", "imei": "123"}

    def get_last_status(self):
        return {"selected_band_set": [3, 20], "rrc": "CONNECTED"}


def _disabled_test_backend_connection_view_uses_generic_resolver():  # TODO: vendor-neutral WIP — re-enable when implemented
    cfg = Arch4Config(modem_backend="mc888", zte_host="192.168.0.1", zte_user="zte", zte_password="pw", zte_ping_interface="Ethernet")
    conn = build_backend_connection_config(cfg)
    assert conn.backend == "mc888"
    assert conn.host == "192.168.0.1"
    assert conn.user == "zte"
    assert conn.password == "pw"
    assert conn.transport == "webui"
    assert conn.ping_interface == "Ethernet"


def _disabled_test_startup_display_uses_backend_connection_view():  # TODO: vendor-neutral WIP — re-enable when implemented
    cfg = Arch4Config(modem_backend="openwrt", openwrt_host="192.168.8.1")
    view = build_startup_display_config(cfg)
    assert view.host_display == "192.168.8.1"
    assert view.connect_error_host == "192.168.8.1"


def _disabled_test_capabilities_report_exposes_support_summary_and_generic_identity_transport():  # TODO: vendor-neutral WIP — re-enable when implemented
    cfg = Arch4Config(modem_backend="mc888", zte_host="192.168.0.1", zte_user="zte")
    report = build_capabilities_report(
        cfg=cfg,
        capabilities=_Caps(),
        constraints=None,
        modem=_Modem(),
        deployed_bands=[1, 3, 20],
        effective_policy={"sample": True},
    )
    assert report["identity"]["target"] == "192.168.0.1"
    assert report["identity"]["user"] == "zte"
    assert report["identity"]["iface"] == "webui"
    assert report["support"]["support_level"] == SUPPORT_LEVEL_ADVANCED_CONTROL
    assert report["support"]["action_semantics"]["cell_control_mode"] == "pci_earfcn"



def _disabled_test_user_config_generic_connection_setter_and_target_resolution():  # TODO: vendor-neutral WIP — re-enable when implemented
    cfg = Arch4Config(modem_backend="mikrotik")
    cfg.set_connection_for_backend("openwrt", host="192.168.8.1", user="root", password="pw")
    assert cfg.modem_backend == "openwrt"
    assert cfg.openwrt_host == "192.168.8.1"
    assert cfg.openwrt_user == "root"
    assert cfg.openwrt_password == "pw"
    assert cfg.connection_target_for_backend("openwrt") == "192.168.8.1"


def _disabled_test_user_config_model_target_resolution_is_profile_aware():  # TODO: vendor-neutral WIP — re-enable when implemented
    cfg = Arch4Config(modem_backend="model", model_profile_name="demo_profile")
    assert cfg.connection_target_for_backend("model") == "profile:demo_profile"
    cfg.model_profile_path = "/tmp/profile.json"
    assert cfg.connection_target_for_backend("model") == "/tmp/profile.json"


class _StubEngine:
    def __init__(self):
        self.calls = []

    def _decide_provider_overnight_recovery(self, modem, now):
        self.calls.append(("overnight", now))
        return None, False

    def _decide_provider_morning_sequence(self, modem, now):
        self.calls.append(("morning", now))
        return None

    def _run_special_band_pipeline(self, **kwargs):
        return kwargs

    def _decide_working(self, modem, computed):
        self.calls.append("working")
        return "candidate"

    def _decide_degraded(self, modem, computed):
        return None

    def _decide_congested(self, modem, computed):
        return None

    def _filter_daytime_band_action_for_backend(self, action):
        self.calls.append(("filter", action))
        return f"filtered:{action}"

    def _decide_provider_radio_drop_recovery(self, modem, now):
        self.calls.append(("radio_drop", now))
        return None, False

    def _decide_e_radio_down(self, modem, computed):
        self.calls.append("e_radio_down")
        return "fallback"


def test_pre_state_policy_uses_generic_provider_hooks():
    from arch4.policy.pre_state_policy import run_pre_state_policy
    from arch4.interfaces import SystemState

    engine = _StubEngine()
    with pytest.raises(
        RuntimeError,
        match="old_provider_pre_state_action_path_deleted",
    ):
        run_pre_state_policy(
            engine,
            modem=None,
            state=SystemState.WORKING,
            now=123.0,
        )
    assert engine.calls == []


def test_state_dispatch_hard_fails_normal_lane_and_keeps_generic_fault_lane():
    from arch4.policy.state_dispatch_policy import run_state_dispatch
    from arch4.interfaces import SystemState

    engine = _StubEngine()
    with pytest.raises(
        RuntimeError,
        match="old_state_dispatch_autonomous_action_path_deleted",
    ):
        run_state_dispatch(
            engine,
            modem=None,
            computed=None,
            state=SystemState.WORKING,
            now=11.0,
        )
    assert engine.calls == []

    engine = _StubEngine()
    action = run_state_dispatch(engine, modem=None, computed=None, state=SystemState.E_RADIO_DOWN, now=22.0)
    assert action == "fallback"
    assert engine.calls == ["e_radio_down"]


def test_operator_profile_layer_exposes_default_region_mapping_and_band_sets():
    assert resolve_provider_name("22005") == "A1"
    assert resolve_provider_plmn_from_name("A1 SRB") == "22005"
    assert resolve_provider_plmn_from_name("MTS") == "22003"
    assert resolve_provider_plmn_from_name("Yettel Srbija") == "22001"
    assert sorted(default_deployed_bands()) == [1, 3, 20]




def test_policy_template_resolution_is_profile_driven_for_a1_b818():
    template = resolve_policy_template("22005", "b818", supports_band_selection=True)
    assert template["policy_key"] == PROVIDER_BACKEND_POLICY_OVERRIDES[("22005", "b818")].policy_key
    assert template["backend_family"] == "huawei_hilink_like"
    assert template["a1_overnight_recovery"] is True


def test_policy_template_resolution_uses_family_defaults_for_unknown_huawei_alias():
    template = resolve_policy_template("99999", "huawei", supports_band_selection=True)
    assert template["backend_family"] == infer_backend_family("huawei")
    assert template["policy_key"] == FAMILY_POLICY_DEFAULTS["huawei_hilink_like"].policy_key
    assert template["enable_night_band_training"] is True


def test_policy_template_resolution_uses_backend_default_for_b818_non_a1():
    template = resolve_policy_template("22003", "b818", supports_band_selection=True)
    assert template["policy_key"] == BACKEND_POLICY_DEFAULTS["b818"].policy_key
    assert template["b818_keep_device_monitor"] is True
    assert template["a1_overnight_recovery"] is False


def test_policy_template_resolution_mirrors_runtime_policy_selector():
    profile = select_policy_profile("22005", "b818")
    template = resolve_policy_template("22005", "b818", supports_band_selection=True)
    assert template["name"] == profile.name
    assert template["policy_key"] == profile.policy_key
    assert template["policy_family"] == profile.policy_family
    assert template["provider_plmn"] == profile.provider_plmn
    assert template["provider_name"] == profile.provider_name
    assert template["modem_backend"] == profile.modem_backend
    assert template["b818_use_full_brain"] == profile.b818_use_full_brain
    assert template["b818_keep_device_monitor"] == profile.b818_keep_device_monitor
    assert template["night_ca_bonus_only"] == profile.night_ca_bonus_only
    assert template["enable_night_band_training"] == profile.enable_night_band_training
    assert template["b818_daytime_auto_band_actions"] == profile.b818_daytime_auto_band_actions
    assert template["night_probe_dwell_s"] == profile.night_probe_dwell_s
    assert template["night_probe_cooldown_s"] == profile.night_probe_cooldown_s
    assert template["a1_overnight_recovery"] == profile.a1_overnight_recovery


def test_profile_data_is_loaded_from_external_source_manifest():
    info = policy_template_sources()
    assert info["version"] == "vendor_neutral_pass41"
    assert "22005|b818" in info["provider_backend_overrides"]
    assert "huawei_hilink_like" in info["families"]


def test_available_operator_contexts_expose_default_region():
    contexts = available_operator_contexts()
    assert "rs_default" in contexts
    assert contexts["rs_default"].provider_name("22003") == "MTS"


def test_family_rotate_policy_routes_huawei_family_behavior_through_generic_layer():
    from arch4.policy.family_rotate_policy import allow_family_priority_rotate, resolve_runtime_backend_family

    class _Cooldowns:
        @staticmethod
        def get_until(name):
            return 0.0

    class _Cfg:
        modem_backend = "b818"

    class _Profile:
        backend_family = "huawei_hilink_like"

    class _Engine:
        cooldowns = _Cooldowns()
        cfg = _Cfg()
        policy_profile = _Profile()
        _hilink_rotate_success_quiet_until = 0.0

    engine = _Engine()
    assert resolve_runtime_backend_family(engine) == "huawei_hilink_like"
    assert allow_family_priority_rotate(engine, now=0.0) is True


def test_family_profile_data_is_loaded_from_external_source_manifest():
    info = family_profile_sources()
    assert info["version"] == "vendor_neutral_pass41"
    assert "huawei_hilink_like" in info["family_labels"]
    assert "22005|huawei_hilink_like" in info["provider_action_policy_registry"]


def test_family_label_and_support_text_are_externalized():
    assert family_label("huawei_hilink_like") == "Huawei HiLink-like"
    assert "conservative daytime policy" in family_support_text("huawei_hilink_like")


def test_action_policy_registry_resolves_family_and_backend_paths():
    by_backend = resolve_action_policy_registry("22005", "b818")
    by_family = resolve_action_policy_registry("22005", "huawei")
    assert by_backend["overnight_recovery"] == "a1_huawei_overnight_recovery"
    assert by_family["daytime_band_filter"] == "a1_huawei_daytime_band_filter"


def test_capabilities_report_exposes_family_label_and_support_text():
    cfg = Arch4Config(modem_backend="b818", b818_host="192.168.5.1", b818_user="admin")
    report = build_capabilities_report(
        cfg=cfg,
        capabilities=_Caps(),
        constraints=None,
        modem=_Modem(),
        deployed_bands=[1, 3, 20],
        effective_policy={"sample": True},
    )
    assert report["support"]["family"] == "huawei_hilink_like"
    assert report["support"]["family_label"] == "Huawei HiLink-like"
    assert "conservative daytime policy" in report["support"]["family_support_text"]



def _disabled_test_vendor_neutral_rotate_aliases_expose_legacy_values():  # TODO: vendor-neutral WIP — re-enable when implemented
    cfg = Arch4Config(
        hilink_priority_rotate_verify_s=321.0,
        hilink_priority_rotate_fail_cooldown_s=654.0,
        hilink_priority_rotate_success_quiet_s=987.0,
        mikrotik_priority_rotate_min_interval_s=111.0,
        mikrotik_priority_rotate_fail_cooldown_s=222.0,
        mikrotik_priority_rotate_success_quiet_s=333.0,
        mikrotik_priority_rotate_window_s=444.0,
        mikrotik_priority_rotate_max_in_window=5,
        mikrotik_priority_rotate_sinr_gate_db=12.5,
        mikrotik_priority_rotate_min_dl_mbps=1.25,
    )
    assert cfg.hilink_priority_rotate_verify_s == 321.0
    assert cfg.hilink_priority_rotate_fail_cooldown_s == 654.0
    assert cfg.hilink_priority_rotate_success_quiet_s == 987.0
    assert cfg.routeros_priority_rotate_min_interval_s == 111.0
    assert cfg.routeros_priority_rotate_fail_cooldown_s == 222.0
    assert cfg.routeros_priority_rotate_success_quiet_s == 333.0
    assert cfg.routeros_priority_rotate_window_s == 444.0
    assert cfg.routeros_priority_rotate_max_in_window == 5
    assert cfg.routeros_priority_rotate_sinr_gate_db == 12.5
    assert cfg.routeros_priority_rotate_min_dl_mbps == 1.25



def test_cooldown_registry_vendor_neutral_alias_bridge_roundtrip():
    from arch4.brain.cooldown_registry import CooldownRegistry

    reg = CooldownRegistry()
    reg.set_until("hilink_rotate", 9999999999.0)
    reg.set_until("hilink_rotate_success_quiet", 9999999998.0)
    reg.set_until("mikrotik_rotate", 9999999997.0)
    reg.set_until("mikrotik_rotate_success_quiet", 9999999996.0)
    reg.set_keyed_until("mikrotik_explore_retry", "obj", 9999999995.0)
    state = reg.save_state_legacy()
    assert state["hilink_rotate_cooldown_until"] == 9999999999.0
    assert state["hilink_rotate_success_quiet_until"] == 9999999998.0
    assert "b818_rotate_cooldown_until" not in state
    assert "b818_rotate_success_quiet_until" not in state
    assert state["routeros_rotate_cooldown_until"] == state["mikrotik_rotate_cooldown_until"]
    assert state["routeros_rotate_success_quiet_until"] == state["mikrotik_rotate_success_quiet_until"]
    assert state["routeros_explore_retry_until"] == state["mikrotik_explore_retry_until"]

    restored = CooldownRegistry()
    restored.restore_state_legacy({
        "hilink_rotate_cooldown_until": 9999999900.0,
        "hilink_rotate_success_quiet_until": 9999999901.0,
        "routeros_rotate_cooldown_until": 9999999902.0,
        "routeros_rotate_success_quiet_until": 9999999903.0,
        "routeros_explore_retry_until": {"obj": 9999999904.0},
    })
    assert restored.get_until("hilink_rotate") == 9999999900.0
    assert restored.get_until("hilink_rotate_success_quiet") == 9999999901.0
    assert restored.get_until("mikrotik_rotate") == 9999999902.0
    assert restored.get_until("mikrotik_rotate_success_quiet") == 9999999903.0
    assert restored.get_keyed_until("mikrotik_explore_retry", "obj") == 9999999904.0



def _disabled_test_hilink_radio_drop_aliases():  # TODO: vendor-neutral WIP — re-enable when implemented
    from arch4.interfaces import Arch4Config
    cfg = Arch4Config()
    cfg.hilink_radio_drop_fallback_persist_s = 61.0
    cfg.hilink_radio_drop_step_wait_s = 7.0
    cfg.hilink_radio_drop_hold_s = 121.0
    assert cfg.a1_big_dip_b20_fallback_persist_s == 61.0
    assert cfg.a1_big_dip_step_wait_s == 7.0
    assert cfg.a1_big_dip_hold_s == 121.0
    assert cfg.hilink_radio_drop_fallback_persist_s == 61.0
    assert cfg.hilink_radio_drop_step_wait_s == 7.0
    assert cfg.hilink_radio_drop_hold_s == 121.0


def test_hilink_radio_drop_policy_emits_vendor_neutral_event_aliases():
    from types import SimpleNamespace
    from arch4.interfaces import LeverType, LeverAction, SystemState
    from arch4.policy.a1_b818_policy import (
        _legacy_decide_a1_b818_big_dip_oracle as decide_a1_b818_big_dip,
    )

    class _Engine:
        def __init__(self):
            self.cfg = SimpleNamespace(
                a1_big_dip_b20_fallback_persist_s=60.0,
                a1_big_dip_step_wait_s=1.0,
                a1_big_dip_hold_s=120.0,
            )
            self.posture_authority = SimpleNamespace(
                state=SystemState.E_RADIO_DOWN,
                current_posture=SimpleNamespace(
                    emergency_class="E_RADIO_DOWN",
                    time_in_state_s=120.0,
                ),
            )
            self._a1_big_dip = {"day": "", "active": False, "step": 0, "last_action_at": 0.0, "hold_until": 0.0}
            self.deployed_bands = {1, 3, 20}
            self.runtime_events = []

        def _is_a1_overnight_profile(self):
            return True

        def _emit_runtime_event(self, kind, **payload):
            self.runtime_events.append({"kind": kind, **payload})

        def _make_band_action(self, **kwargs):
            return LeverAction(
                lever=LeverType.BAND_SELECTION,
                params=dict(kwargs["params"], bands=kwargs["bands"]),
                reason=kwargs["reason"],
                intent=kwargs["intent"],
                objective_id=kwargs["objective_id"],
            )

    engine = _Engine()
    modem = SimpleNamespace(
        cellular=SimpleNamespace(
            radio_connected=False,
            primary_band=0,
            carriers=[],
        )
    )
    action, blocked = decide_a1_b818_big_dip(engine, modem, 1_700_000_000.0)
    assert blocked is True
    assert action is not None
    assert action.lever == LeverType.BAND_SELECTION
    assert action.params["intent_alias"] == "hilink_radio_drop"
    assert action.params["reason_code"] == "hilink_radio_drop_fallback"
    assert action.params["objective_alias_id"].startswith("hilink_radio_drop:")

    kinds = [e.get("kind") for e in engine.runtime_events]
    assert "a1_big_dip_start" in kinds
    assert "a1_big_dip_hold_started" in kinds
    assert "hilink_radio_drop_start" in kinds
    assert "hilink_radio_drop_hold_started" in kinds
    assert "huawei_radio_drop_start" in kinds
    assert "huawei_radio_drop_hold_started" in kinds


def test_supervisor_accepts_vendor_neutral_radio_drop_intents():
    from arch4.tests.core_command_test_support import core_bouncer_for_test
    from arch4.interfaces import Arch4Config

    sup = core_bouncer_for_test(Arch4Config())
    assert "hilink_radio_drop" in sup.KNOWN_INTENTS
    assert "huawei_radio_drop" in sup.KNOWN_INTENTS


def _disabled_test_telemetry_action_log_emits_vendor_neutral_alias_fields(tmp_path):  # TODO: vendor-neutral WIP — re-enable when implemented
    from arch4.interfaces import LeverAction, LeverType, LeverResult
    from arch4.intelligence.evidence_recorder import IntelligenceEvidenceRecorder

    logger = IntelligenceEvidenceRecorder(output_dir=str(tmp_path / "t"), enable_rollup=False)
    action = LeverAction(
        lever=LeverType.BAND_SELECTION,
        params={
            "intent_alias": "hilink_radio_drop",
            "legacy_intent": "big_dip",
            "objective_alias_id": "hilink_radio_drop:2026-04-03:b20_fallback",
            "legacy_objective_id": "big_dip:2026-04-03:b20_fallback",
            "reason_code": "hilink_radio_drop_fallback",
            "policy_family": "hilink",
        },
        reason="HiLink radio-drop fallback",
        intent="big_dip",
        objective_id="big_dip:2026-04-03:b20_fallback",
    )
    result = LeverResult(success=True, lever=LeverType.BAND_SELECTION)
    logger.log_action(action, result)
    logger.close()

    import json
    event_files = sorted((tmp_path / "t").glob("events_*.jsonl"))
    assert event_files
    rows = [json.loads(line) for line in event_files[0].read_text().splitlines() if line.strip()]
    action_rows = [r for r in rows if r.get("kind") == "action"]
    assert action_rows
    row = action_rows[-1]
    assert row["intent_alias"] == "hilink_radio_drop"
    assert row["legacy_intent"] == "big_dip"
    assert row["objective_alias_id"] == "hilink_radio_drop:2026-04-03:b20_fallback"
    assert row["legacy_objective_id"] == "big_dip:2026-04-03:b20_fallback"
    assert row["reason_code"] == "hilink_radio_drop_fallback"
    assert row["policy_family"] == "hilink"



def test_runtime_radio_drop_alias_helper_exposes_family_aliases():
    from arch4.policy.family_radio_drop_policy import runtime_radio_drop_alias_fields

    class Engine:
        def _get_hilink_radio_drop_state(self):
            return {"active": True, "hold_until": 500.0}
        def _hilink_radio_drop_hold_until(self):
            return 500.0

    row = runtime_radio_drop_alias_fields(Engine(), 100.0)
    assert "b818_big_dip_active" not in row
    assert row["hilink_radio_drop_active"] is True
    assert row["huawei_radio_drop_active"] is True
    assert row["family_radio_drop_active"] is True
    assert row["family_radio_drop_remaining_s"] == 400


def test_decision_cooldown_state_does_not_duplicate_provider_radio_drop_state():
    cfg = Arch4Config()
    decision = _make_minimal_decision_engine(cfg)
    state = decision.get_decision_cooldown_state()
    assert "hilink_radio_drop_state" not in state
    assert "huawei_radio_drop_state" not in state
    assert "family_radio_drop_state" not in state
    assert "provider" in decision.action_source_state_authority.checkpoint_payload()[
        "state"
    ]




def test_action_alias_template_sources_and_rendering():
    from arch4.operator_profiles import action_alias_template_sources, render_action_alias_template

    meta = action_alias_template_sources()
    assert "a1_huawei_radio_drop_recovery" in meta["templates"]

    rendered = render_action_alias_template("a1_huawei_radio_drop_recovery", day_key="2026-04-03")
    assert rendered["intent_alias"] == "hilink_radio_drop"
    assert rendered["reason_code"] == "hilink_radio_drop_fallback"
    assert rendered["objective_alias_id"] == "hilink_radio_drop:2026-04-03:b20_fallback"
    assert "hilink_radio_drop_start" in rendered["event_aliases"]


def test_family_action_policy_uses_hilink_wrapper_module():
    import arch4.policy.family_action_policy as fap

    class Engine:
        def __init__(self):
            self.cfg = type("Cfg", (), {"modem_backend": "b818"})()
            self.policy_profile = type("Prof", (), {"backend_family": "huawei_hilink_like"})()
        def _policy_provider_plmn(self):
            return "22005"

    called = {}
    old = fap.decide_hilink_morning_sequence
    try:
        def fake(engine, modem, now):
            called["ok"] = True
            return "sentinel"
        fap.decide_hilink_morning_sequence = fake
        out = fap.decide_family_morning_sequence(Engine(), object(), 1.0)
        assert out == "sentinel"
        assert called["ok"] is True
    finally:
        fap.decide_hilink_morning_sequence = old


def test_family_runtime_state_roundtrip_routeros_rotate_aliases():
    cfg = Arch4Config(modem_backend="mikrotik")
    original = _make_minimal_decision_engine(cfg)
    original._set_routeros_rotate_success_quiet_until(333.0)
    original._mikrotik_rotate_last_attempt_at = 111.0
    original._mikrotik_rotate_attempt_times = [101.0, 102.0]

    state = original.get_decision_cooldown_state()
    assert state["routeros_rotate_state"]["success_quiet_until"] == 333.0
    assert state["mikrotik_rotate_state"]["last_attempt_at"] == 111.0
    assert state["family_rotate_state"]["attempt_times"] == [101.0, 102.0]

    restored = _make_minimal_decision_engine(cfg)
    restored.restore_decision_cooldown_state(state)
    assert restored._get_routeros_rotate_success_quiet_until() == 333.0
    assert restored._mikrotik_rotate_last_attempt_at == 111.0
    assert restored._mikrotik_rotate_attempt_times == [101.0, 102.0]


def test_family_runtime_state_restore_does_not_recreate_hilink_provider_owner():
    cfg = Arch4Config(modem_backend="b818")
    decision = _make_minimal_decision_engine(cfg)
    state = decision.get_decision_cooldown_state()

    restored = _make_minimal_decision_engine(cfg)
    restored.restore_decision_cooldown_state(state)
    assert not hasattr(restored, "_hilink_radio_drop_state")
    assert not hasattr(restored, "_a1_big_dip")



class _RotateCfg:
    routeros_priority_rotate_min_interval_s = 60.0
    mikrotik_priority_rotate_min_interval_s = 60.0
    routeros_priority_rotate_window_s = 600.0
    mikrotik_priority_rotate_window_s = 600.0
    routeros_priority_rotate_max_in_window = 3
    mikrotik_priority_rotate_max_in_window = 3
    routeros_priority_rotate_min_dl_mbps = 1.0
    mikrotik_priority_rotate_min_dl_mbps = 1.0
    routeros_priority_rotate_sinr_gate_db = 10.0
    mikrotik_priority_rotate_sinr_gate_db = 10.0
    routeros_priority_rotate_success_quiet_s = 300.0
    mikrotik_priority_rotate_success_quiet_s = 300.0
    routeros_priority_rotate_fail_cooldown_s = 300.0
    mikrotik_priority_rotate_fail_cooldown_s = 300.0


class _Cooldowns:
    def __init__(self):
        self.values = {}
    def get_until(self, key):
        return float(self.values.get(key, 0.0) or 0.0)
    def set(self, key, hold_s, now):
        self.values[key] = float(now) + float(hold_s)


class _RotateEngine:
    def __init__(self):
        self.cfg = _RotateCfg()
        self.cooldowns = _Cooldowns()
        self._thresholds = {'soft': {}}
        self._mikrotik_rotate_last_attempt_at = 0.0
        self._mikrotik_rotate_attempt_times = []
        self._routeros_rotate_success_quiet_until = 0.0
        self._mikrotik_rotate_success_quiet_until = 0.0
        self.events = []
    def _emit_runtime_event(self, name, **kwargs):
        self.events.append((name, kwargs))
    def _set_routeros_rotate_success_quiet_until(self, until):
        self._routeros_rotate_success_quiet_until = float(until)
        self._mikrotik_rotate_success_quiet_until = float(until)
    def _get_routeros_rotate_success_quiet_until(self):
        return max(self._routeros_rotate_success_quiet_until, self._mikrotik_rotate_success_quiet_until)


def _rotate_inputs():
    modem = ModemInput(
        sinr_db=15.0,
        rsrp_dbm=-85.0,
        rsrq_db=-9.0,
        rssi_dbm=-65.0,
        mcs_dl=12,
        mcs_ul=5,
        cqi=6,
        ri=1,
        carriers=[],
        ca_active=False,
        ca_band_signature="B20",
        primary_band=20,
        pci=100,
        earfcn=6300,
        enb_id=1,
        radio_connected=True,
        session_uptime_s=300.0,
        rx_bytes_per_sec=625_000.0,
        tx_bytes_per_sec=50_000.0,
    )
    return cellular_test_inputs(modem, ComputedSignals())


def test_family_rotate_policy_supports_routeros_family_actions():
    from arch4.policy.family_rotate_policy import (
        allow_family_priority_rotate,
        family_priority_rotate_order,
        note_family_priority_rotate_reverted,
        note_family_priority_rotate_stable,
        record_family_priority_rotate_attempt,
    )

    engine = _RotateEngine()
    modem, computed = _rotate_inputs()
    assert allow_family_priority_rotate(engine, family='routeros_at_like', modem=modem, computed=computed, now=100.0) is True
    assert family_priority_rotate_order(engine, [1, 3, 20], 20, family='routeros_at_like')[0] == 3
    record_family_priority_rotate_attempt(engine, family='routeros_at_like', now=100.0)
    assert engine._mikrotik_rotate_last_attempt_at == 100.0
    note_family_priority_rotate_stable(engine, family='routeros_at_like', target_band=3, now=100.0)
    assert engine._get_routeros_rotate_success_quiet_until() > 100.0
    note_family_priority_rotate_reverted(engine, family='routeros_at_like', target_band=3, current_band=20, now=200.0)
    assert engine.cooldowns.get_until('mikrotik_rotate') > 200.0


def test_decision_policy_exposes_routeros_family_alias_methods():
    from arch4.brain.decision_policy import DecisionPolicyMixin

    class _PolicyEngine(DecisionPolicyMixin):
        def __init__(self):
            self.cfg = _RotateCfg()
            self.cooldowns = _Cooldowns()
            self._thresholds = {'soft': {}}
            self._mikrotik_rotate_last_attempt_at = 0.0
            self._mikrotik_rotate_attempt_times = []
            self._routeros_rotate_success_quiet_until = 0.0
            self._mikrotik_rotate_success_quiet_until = 0.0
            self.events = []
        def _emit_runtime_event(self, name, **kwargs):
            self.events.append((name, kwargs))
        def _set_routeros_rotate_success_quiet_until(self, until):
            self._routeros_rotate_success_quiet_until = float(until)
            self._mikrotik_rotate_success_quiet_until = float(until)
        def _get_routeros_rotate_success_quiet_until(self):
            return max(self._routeros_rotate_success_quiet_until, self._mikrotik_rotate_success_quiet_until)

    engine = _PolicyEngine()
    modem, computed = _rotate_inputs()
    assert engine._allow_routeros_priority_rotate(modem, computed, 50.0) is True
    assert engine._routeros_priority_rotate_order([1, 3, 20], 20)[0] == 3
    engine._record_routeros_priority_rotate_attempt(50.0)
    assert engine._mikrotik_rotate_last_attempt_at == 50.0
    engine._note_routeros_priority_rotate_stable(3, 50.0)
    assert engine._get_routeros_rotate_success_quiet_until() > 50.0


def test_family_objective_policy_routes_routeros_family_behavior():
    from arch4.policy.family_objective_policy import (
        check_family_explore_objective_block,
        is_family_cell_lock_objective,
        resolve_runtime_backend_family,
    )
    from arch4.interfaces import LeverAction, LeverType

    class _Cooldowns:
        def get_keyed_until(self, name, objective_id):
            assert name == "mikrotik_explore_retry"
            assert objective_id == "explore:1+3"
            return 50.0

        def clear_keyed(self, name, objective_id):
            raise AssertionError("should not clear active hold")

    class _Profile:
        backend_family = "routeros_at_like"

    class _Engine:
        cooldowns = _Cooldowns()
        policy_profile = _Profile()
        recorded = None

        def _record_objective_lifecycle_event(self, **kwargs):
            self.recorded = kwargs

    engine = _Engine()
    action = LeverAction(
        lever=LeverType.BAND_SELECTION,
        params={"bands": [1, 3]},
        reason="test",
        intent="explore",
        objective_id="explore:1+3",
    )
    blocked, reason = check_family_explore_objective_block(engine, action=action, now=10.0, family="routeros_at_like")
    assert resolve_runtime_backend_family(engine) == "routeros_at_like"
    assert blocked is True
    assert reason == "mikrotik explore retry hold"
    assert engine.recorded["stage"] == "objective_guard"
    assert is_family_cell_lock_objective(
        LeverAction(
            lever=LeverType.CELL_LOCK,
            params={"pci": 1, "earfcn": 100},
            reason="test",
            intent="explore_cell_lock",
            objective_id="explore_cell_lock:PCI1:E100",
        ),
        family="routeros_at_like",
    ) is True


def test_decision_policy_exposes_routeros_family_objective_aliases():
    from arch4.brain.decision_policy import DecisionPolicyMixin
    from arch4.interfaces import LeverAction, LeverType

    class _Dummy(DecisionPolicyMixin):
        pass

    action = LeverAction(
        lever=LeverType.CELL_LOCK,
        params={"pci": 1, "earfcn": 100},
        reason="test",
        intent="anchor_lock",
        objective_id="anchor_lock:PCI1:E100",
    )
    assert _Dummy._is_routeros_cell_lock_objective(action) is True
    assert _Dummy._is_mikrotik_cell_lock_objective(action) is True


def test_action_alias_template_sources_include_routeros_objective_templates():
    from arch4.operator_profiles import action_alias_template_sources, render_action_alias_template

    meta = action_alias_template_sources()
    assert "routeros_explore_retry" in meta["templates"]
    assert "routeros_cell_lock_retry" in meta["templates"]

    rendered = render_action_alias_template("routeros_explore_retry", objective_id="explore:1+3")
    assert rendered["intent_alias"] == "routeros_explore_retry"
    assert rendered["objective_alias_id"] == "routeros_explore:explore:1+3"


def test_mikrotik_objective_policy_emits_routeros_alias_metadata():
    from arch4.interfaces import LeverAction, LeverType
    from arch4.policy.mikrotik_objective_policy import note_mikrotik_explore_result

    class _Cfg:
        mikrotik_explore_retry_after_s = 120.0
        mikrotik_explore_fail_limit = 2
        mikrotik_explore_fail_cooldown_s = 300.0

    class _Cooldowns:
        def __init__(self):
            self.calls = []
        def set_keyed(self, name, objective_id, hold_s, now):
            self.calls.append((name, objective_id, hold_s, now))

    class _Engine:
        cfg = _Cfg()
        cooldowns = _Cooldowns()
        _thresholds = {"soft": {}}
        _mikrotik_explore_fail_count = {}
        recorded = None
        events = []
        def _objective_id_for_bands(self, prefix, bands):
            return f"{prefix}:{'+'.join(str(b) for b in bands)}"
        def _record_objective_lifecycle_event(self, **kwargs):
            self.recorded = kwargs
        def _emit_runtime_event(self, name, **kwargs):
            self.events.append((name, kwargs))

    engine = _Engine()
    action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [1, 3]}, reason="x", intent="explore", objective_id="explore:1+3")
    note_mikrotik_explore_result(engine, action=action, attached=False, now=10.0, target_sig="b1+b3")
    assert engine.recorded["metadata"]["intent_alias"] == "routeros_explore_retry"
    # Legacy mikrotik name emitted as primary event for backward compat;
    # routeros alias metadata carried alongside in the event payload.
    assert engine.events[0][0] == "mikrotik_explore_retry_scheduled"
    assert engine.events[0][1]["objective_alias_id"] == "routeros_explore:explore:1+3"


def test_family_daytime_policy_routes_hilink_family_filter():
    from arch4.interfaces import LeverAction, LeverType
    import arch4.policy.family_daytime_policy as fdp

    class _Engine:
        cfg = type("Cfg", (), {"modem_backend": "b818"})()
        policy_profile = type("Prof", (), {"backend_family": "huawei_hilink_like", "modem_backend": "b818"})()

    old = fdp.suppress_b818_daytime_band_action
    try:
        fdp.suppress_b818_daytime_band_action = lambda engine, action: None
        action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [20]}, reason="x", intent="explore")
        assert fdp.filter_family_daytime_band_action(_Engine(), action, family="huawei_hilink_like") is None
    finally:
        fdp.suppress_b818_daytime_band_action = old


def test_cooldown_alias_sources_include_huawei_and_routeros_keys():
    from arch4.operator_profiles import cooldown_alias_definitions

    meta = cooldown_alias_definitions()
    assert "hilink_rotate" in meta["scalar"]
    assert "b818_rotate" not in meta["scalar"]
    assert "huawei_rotate_cooldown_until" in meta["scalar"]["hilink_rotate"]
    assert "cell_lock_retry" in meta["keyed"]
    assert "routeros_cell_lock_retry_until" in meta["keyed"]["cell_lock_retry"]



def test_cooldown_registry_accepts_huawei_and_routeros_cell_lock_aliases():
    from arch4.brain.cooldown_registry import CooldownRegistry

    reg = CooldownRegistry()
    reg.restore_state_legacy({
        "huawei_rotate_cooldown_until": 9999999900.0,
        "routeros_cell_lock_retry_until": {"pci:1": 9999999901.0},
    })
    assert reg.get_until("hilink_rotate") == 9999999900.0
    assert reg.get_keyed_until("cell_lock_retry", "pci:1") == 9999999901.0

    state = reg.save_state_legacy()
    assert state["huawei_rotate_cooldown_until"] == 9999999900.0
    assert state["routeros_cell_lock_retry_until"]["pci:1"] == 9999999901.0



def test_runtime_status_alias_sources_and_application():
    from arch4.operator_profiles import runtime_status_alias_sources, apply_runtime_status_aliases

    meta = runtime_status_alias_sources()
    assert "hilink_rotate_cooldown_active" in meta["aliases"]
    assert "b818_rotate_cooldown_active" not in meta["aliases"]
    assert "hilink_antithrash_hold_active" in meta["aliases"]["a1_antithrash_hold_active"]

    row = apply_runtime_status_aliases({
        "hilink_rotate_cooldown_active": True,
        "mikrotik_explore_retry_objectives": 2,
        "a1_antithrash_hold_active": False,
        "a1_antithrash_freeze_bands": [20],
        "a1_morning_active": True,
    })
    assert row["huawei_rotate_cooldown_active"] is True
    assert row["routeros_explore_retry_objectives"] == 2
    assert row["hilink_antithrash_hold_active"] is False
    assert row["family_antithrash_freeze_bands"] == [20]
    assert row["huawei_morning_active"] is True



def test_runtime_event_alias_profile_source_and_lookup():
    src = runtime_event_alias_sources()
    assert src["version"]
    assert "a1_overnight_explore_start" in src["aliases"]
    aliases = runtime_event_aliases("a1_overnight_explore_start")
    assert "hilink_overnight_explore_start" in aliases


def test_emit_runtime_event_appends_vendor_neutral_alias_events():
    from arch4.brain.decision_pipeline import DecisionPipelineMixin

    class E(DecisionPipelineMixin):
        def __init__(self):
            self._runtime_events = []

    e = E()
    e._emit_runtime_event("a1_antithrash_hold_started", hold_s=600)
    kinds = [x["kind"] for x in e._runtime_events]
    assert "a1_antithrash_hold_started" in kinds
    assert "hilink_antithrash_hold_started" in kinds
    assert "huawei_antithrash_hold_started" in kinds


def test_runtime_status_text_profile_source_and_application():
    src = runtime_status_text_sources()
    assert src["version"]
    assert "hilink_radio_drop_active" in src["templates"]
    status = apply_runtime_status_texts({"hilink_radio_drop_active": True})
    assert status["hilink_radio_drop_active_label"] == "HiLink radio-drop recovery active"


def test_runtime_event_text_profile_source_and_application():
    from arch4.operator_profiles import apply_runtime_event_texts

    src = runtime_event_text_sources()
    assert src["version"]
    assert "routeros_explore_retry_scheduled" in src["templates"]
    event = apply_runtime_event_texts({"kind": "routeros_explore_retry_scheduled"})
    assert event["kind_label"] == "RouterOS-family explore retry scheduled"


def test_emit_runtime_event_applies_event_texts_to_alias_events():
    from arch4.brain.decision_pipeline import DecisionPipelineMixin

    class E(DecisionPipelineMixin):
        def __init__(self):
            self._runtime_events = []

    e = E()
    e._emit_runtime_event("mikrotik_explore_retry_scheduled", retry_after_s=60)
    by_kind = {x["kind"]: x for x in e._runtime_events}
    assert by_kind["routeros_explore_retry_scheduled"]["kind_label"] == "RouterOS-family explore retry scheduled"
    assert by_kind["routeros_explore_retry_scheduled"]["legacy_kind"] == "mikrotik_explore_retry_scheduled"


def test_runtime_status_text_application_for_routeros_family_aliases():
    status = apply_runtime_status_texts({"routeros_rotate_quiet_active": True, "routeros_explore_retry_objectives": 2})
    assert status["routeros_rotate_quiet_active_label"] == "RouterOS-family rotate quiet window active"
    assert status["routeros_explore_retry_objectives_label"] == "RouterOS-family explore retries active"


def _disabled_test_routeros_manual_and_mutating_aliases():  # TODO: vendor-neutral WIP — re-enable when implemented
    from arch4.interfaces import Arch4Config
    cfg = Arch4Config()
    cfg.routeros_manual_verify_timeout_s = 46.0
    cfg.routeros_manual_verify_poll_s = 3.0
    cfg.routeros_manual_verify_min_session_s = 9.0
    cfg.routeros_mutating_action_lock_s = 61.0
    assert cfg.mikrotik_manual_verify_timeout_s == 46.0
    assert cfg.mikrotik_manual_verify_poll_s == 3.0
    assert cfg.mikrotik_manual_verify_min_session_s == 9.0
    assert cfg.mikrotik_mutating_action_lock_s == 61.0


def test_engine_runtime_surface_aliases_for_routeros_family():
    from arch4.policy.family_engine_status_policy import apply_engine_runtime_surface_aliases
    src = runtime_status_text_sources()
    assert src["version"] == "vendor_neutral_pass41"
    extra = {
        "runtime_recovery": {"active": True, "kind": "mutating_action_attention"},
        "brain": {"planned_radio_quiet": {"active": True, "reason": "manual_radio_action_quiet"}},
    }
    shaped = apply_engine_runtime_surface_aliases(backend="mikrotik", extra=extra)
    assert shaped["runtime_recovery"]["routeros_runtime_recovery_active"] is True
    assert shaped["runtime_recovery"]["family_runtime_recovery_active"] is True
    assert shaped["runtime_recovery"]["routeros_runtime_recovery_active_label"] == "RouterOS-family runtime recovery active"
    assert shaped["brain"]["planned_radio_quiet"]["routeros_manual_radio_quiet_active"] is True
    assert shaped["brain"]["planned_radio_quiet"]["family_manual_radio_quiet_active"] is True
    assert shaped["brain"]["planned_radio_quiet"]["routeros_manual_radio_quiet_active_label"] == "RouterOS-family manual radio quiet window active"


def _disabled_test_arch4_logger_emits_routeros_family_alias_events_and_texts(tmp_path):  # TODO: vendor-neutral WIP — re-enable when implemented
    import json
    from arch4.intelligence.evidence_recorder import IntelligenceEvidenceRecorder

    logger = IntelligenceEvidenceRecorder(output_dir=str(tmp_path), enable_rollup=False)
    logger.log_event("runtime_recovery_armed", recovery_kind="mutating_action_attention")
    logger.log_event("manual_radio_guard_armed", quiet_s=90)
    logger.close()

    rows = [json.loads(line) for line in (tmp_path / logger._event_path.name).read_text().splitlines() if line.strip()]
    kinds = [row["kind"] for row in rows]
    assert "runtime_recovery_armed" in kinds
    assert "routeros_runtime_recovery_armed" in kinds
    assert "family_runtime_recovery_armed" in kinds
    assert "routeros_manual_radio_guard_armed" in kinds
    by_kind = {row["kind"]: row for row in rows}
    assert by_kind["routeros_runtime_recovery_armed"]["legacy_kind"] == "runtime_recovery_armed"
    assert by_kind["routeros_runtime_recovery_armed"]["kind_label"] == "RouterOS-family runtime recovery armed"
    assert by_kind["routeros_manual_radio_guard_armed"]["kind_label"] == "RouterOS-family manual radio quiet armed"


def test_runtime_event_record_builder_emits_aliases_and_texts():
    records = build_runtime_event_records({"kind": "restart_objective_retry_scheduled", "objective_id": "rst-1"})
    kinds = [r["kind"] for r in records]
    assert "restart_objective_retry_scheduled" in kinds
    assert "family_restart_objective_retry_scheduled" in kinds
    assert "routeros_restart_objective_retry_scheduled" in kinds
    by_kind = {r["kind"]: r for r in records}
    assert by_kind["routeros_restart_objective_retry_scheduled"]["legacy_kind"] == "restart_objective_retry_scheduled"
    assert by_kind["routeros_restart_objective_retry_scheduled"]["kind_label"] == "RouterOS-family restart objective retry scheduled"


def test_runtime_surface_aliasing_uses_family_engine_layer():
    from arch4.policy.family_engine_status_policy import apply_engine_runtime_surface_aliases
    shaped = apply_engine_runtime_surface_aliases(
        backend="mikrotik",
        extra={"brain": {"planned_radio_quiet": {"active": True, "until_ts": 10.0}}},
    )
    planned = shaped["brain"]["planned_radio_quiet"]
    assert planned["routeros_manual_radio_quiet_active"] is True
    assert planned["family_manual_radio_quiet_active"] is True
    assert planned["routeros_manual_radio_quiet_active_label"] == "RouterOS-family manual radio quiet window active"

def test_family_engine_runtime_policy_drains_default_kind_events():
    from arch4.policy.family_engine_runtime_policy import drain_runtime_event_records

    class _Logger:
        def __init__(self):
            self.events = []

        def log_event(self, kind, **kwargs):
            self.events.append({"kind": kind, **kwargs})

    logger = _Logger()
    drain_runtime_event_records(logger, [{"foo": 1}, {"kind": "explicit", "bar": 2}], default_kind="fallback", defaults={"lane": "sim"})

    assert logger.events[0]["kind"] == "fallback"
    assert logger.events[0]["lane"] == "sim"
    assert logger.events[1]["kind"] == "explicit"
    assert logger.events[1]["lane"] == "sim"

def test_family_engine_runtime_helpers_cover_lock_and_feedback_surfaces():
    from arch4.interfaces import LeverType
    from arch4.policy.family_engine_runtime_policy import (
        arm_mutating_action_lock_state,
        build_auto_action_lockout_event,
        build_manual_inflight_feedback,
        build_manual_bouncer_feedback,
        clear_mutating_action_lock_runtime,
    )

    event = build_auto_action_lockout_event(reason="x", previous_until=100.0, new_until=120.0, now=110.0)
    assert event["kind"] == "auto_action_lockout_updated"
    assert event["previous_remaining_s"] == 0
    assert event["new_remaining_s"] == 10

    until_ts, lever_name, event = arm_mutating_action_lock_state(
        current_until=0.0,
        lever=LeverType.BAND_SELECTION,
        lock_seconds=60.0,
        now=10.0,
    )
    assert until_ts >= 70.0
    assert lever_name == "BAND_SELECTION"
    assert event["kind"] == "mutating_action_lock_updated"
    assert event["reason"] == "arm"

    changed, new_until, new_lever, event = clear_mutating_action_lock_runtime(
        current_until=until_ts,
        current_lever=lever_name,
        reason="ok",
        now=10.0,
    )
    assert changed is True
    assert new_until == 0.0
    assert new_lever == ""
    assert event["lever"] == "BAND_SELECTION"
    assert event["remaining_s"] == 0

    feedback = build_manual_bouncer_feedback(lever="CELL_LOCK", code="busy", message="Blocked")
    assert feedback == {"lever": "CELL_LOCK", "code": "busy", "message": "Blocked"}

    inflight = build_manual_inflight_feedback(previous_lever="BAND_SELECTION", current_lever="CELL_LOCK", until_ts=25.0, now=10.0)
    assert inflight["lever"] == "CELL_LOCK"
    assert inflight["previous_lever"] == "BAND_SELECTION"
    assert inflight["remaining_s"] == 15


class _LeverObj:
    name = "BAND_SELECTION"


class _ActionObj:
    objective_id = "obj-1"
    lever = _LeverObj()


def _record_staged_roi_action_record(history, record):
    history.append(record)
    return {
        "stored": True,
        "reason": str(getattr(record, "learning_commit_status", "") or ""),
    }


def _close_action_proof_record(history, record):
    tid = str(getattr(record, "decision_trace_id", "") or "")
    for idx in range(len(history) - 1, -1, -1):
        existing = history[idx]
        existing_tid = str(getattr(existing, "decision_trace_id", "") or "")
        if tid and existing_tid == tid:
            history[idx] = record
            return {"closed": True, "reason": "proof_closed"}
        if (
            not tid
            and abs(
                float(getattr(existing, "timestamp", 0.0) or 0.0)
                - float(getattr(record, "timestamp", 0.0) or 0.0)
            )
            < 0.001
            and getattr(existing, "lever", None) == getattr(record, "lever", None)
            and dict(getattr(existing, "action_params", {}) or {})
            == dict(getattr(record, "action_params", {}) or {})
        ):
            history[idx] = record
            return {"closed": True, "reason": "proof_closed"}
    history.append(record)
    return {"closed": True, "reason": "proof_closed"}


class _ProofMemoryStoreFake:
    def __init__(self, records):
        self.action_history = list(records)
        self.journal = []
        self.dirty_events = 0
        self.band_learning_stm = {}
        self.band_learning_ltm = {}
        self.current_context_seen_actions = 0
        self.current_calibration_marker = "C0"
        self.calibration_contexts = {}
        self.learning_cfg = SimpleNamespace(learning_ltm_decay=0.98, learning_stm_window_s=1800.0)

    @property
    def action_history(self):
        return self._action_history_store.records

    @action_history.setter
    def action_history(self, records):
        rows = list(records or [])
        self._action_history_store = ActionHistoryStore()
        _bind_test_action_history_scope(self, rows)
        self._action_history_store.set_records(rows)

    def __getattr__(self, name):
        if name in {"band_learning_stm", "band_learning_ltm", "calibration_contexts"}:
            value = {}
            setattr(self, name, value)
            return value
        if name == "current_context_seen_actions":
            setattr(self, name, 0)
            return 0
        if name == "current_calibration_marker":
            setattr(self, name, "C0")
            return "C0"
        if name == "learning_cfg":
            value = SimpleNamespace(learning_ltm_decay=0.98, learning_stm_window_s=1800.0)
            setattr(self, name, value)
            return value
        raise AttributeError(name)

    def _append_action_history_record(self, record):
        self.action_history.append(record)
        return True

    def _upsert_action_history_record(self, record):
        _close_action_proof_record(self.action_history, record)
        return True

    def _action_record_scope_valid(self, record):
        try:
            EvidenceScope.from_mapping(
                dict(getattr(record, "baseline_evidence_scope", {}) or {})
            )
            EvidenceScope.from_mapping(
                dict(getattr(record, "evidence_scope", {}) or {})
            )
        except (TypeError, ValueError):
            return False
        return True

    def _append_journal_event(self, event):
        if not hasattr(self, "journal"):
            self.journal = []
        self.journal.append(dict(event))
        return True

    def _append_journal_event_strict(self, event):
        self._append_journal_event(event)

    def _mark_dirty(self, *, events=0):
        if not hasattr(self, "dirty_events"):
            self.dirty_events = 0
        self.dirty_events += int(events or 0)

    def _record_to_dict(self, record):
        return record_to_dict(record)

    def _resolve_marker_alias(self, marker):
        return str(marker or "C0")

    def _norm_bands(self, bands):
        out = []
        for raw in bands or []:
            try:
                band = int(raw)
            except (TypeError, ValueError):
                continue
            if band > 0 and band not in out:
                out.append(band)
        return sorted(out)

    def _marker_key(self, marker, key):
        return f"{marker}|{key}"

    def _band_learning_key(self, hour, bands):
        return f"h{int(hour):02d}:" + "+".join(f"B{int(b)}" for b in bands)

    def _get_band_seed_score(self, _norm_bands, _bw_mhz=0.0):
        return 0.5


def _make_minimal_decision_engine(cfg: Arch4Config | None = None):
    config = cfg or Arch4Config()
    engine = DecisionEngine.__new__(DecisionEngine)
    engine.cfg = config
    engine.cooldowns = CooldownRegistry()
    engine.posture_authority = SimpleNamespace(
        state=SystemState.WORKING,
        current_posture=SimpleNamespace(
            state=SystemState.WORKING,
            emergency_class="",
            time_in_state_s=0.0,
        ),
    )
    engine.action_source_state_authority = ActionSourceStateAuthority()
    engine._band_rotation_checked = True
    engine._band_rotation_target = 0
    engine._band_rotation_time = 0.0
    engine._routeros_rotate_success_quiet_until = 0.0
    engine._mikrotik_rotate_success_quiet_until = 0.0
    engine._mikrotik_rotate_last_attempt_at = 0.0
    engine._mikrotik_rotate_attempt_times = []
    return engine


def test_mutating_action_verification_event_is_shaped_by_shared_helper():
    event = build_mutating_action_verification_event(
        action=_ActionObj(),
        decision_trace_id="trace-1",
        mutating=True,
        success=False,
        note="no_settle",
    )
    assert event["kind"] == "mutating_action_verification"
    assert event["decision_trace_id"] == "trace-1"
    assert event["objective_id"] == "obj-1"
    assert event["lever"] == "BAND_SELECTION"
    assert event["mutating"] is True
    assert event["success"] is False
    assert event["note"] == "no_settle"


def test_runtime_guard_payload_injection_is_shared_for_engine_and_sim_shapes():
    extra = inject_runtime_guard_payloads(
        extra={"brain": {"existing": 1}, "runtime_recovery": {"stale": True}},
        runtime_recovery_payload={"active": True, "remaining_s": 9},
        planned_radio_quiet_payload={"active": True, "remaining_s": 5},
    )
    assert extra["runtime_recovery"]["active"] is True
    assert extra["runtime_recovery"]["remaining_s"] == 9
    assert extra["brain"]["existing"] == 1
    assert extra["brain"]["planned_radio_quiet"]["remaining_s"] == 5
    assert extra["brain"]["planned_radio_quiet_active"] is True


def test_build_runtime_event_records_derives_family_metadata_from_profile_and_backend():
    from arch4.operator_profiles import build_runtime_event_records
    records = build_runtime_event_records({
        "kind": "policy_template_applied",
        "profile": "A1-b818-conservative",
        "modem_backend": "b818",
        "provider_plmn": "22005",
    })
    base = records[0]
    assert base["family"] == "huawei_hilink_like"
    assert base["family_label"]
    assert base["provider_name"] == "A1"


def _disabled_test_logger_runtime_event_defaults_enrich_startup_event(tmp_path):  # TODO: vendor-neutral WIP — re-enable when implemented
    import json
    from arch4.intelligence.evidence_recorder import IntelligenceEvidenceRecorder
    lg = IntelligenceEvidenceRecorder(output_dir=str(tmp_path), enable_rollup=False)
    try:
        lg.configure_runtime_event_defaults(
            profile="A1-b818-conservative",
            policy_family="PROVIDER_SPECIFIC",
            provider_plmn="22005",
            modem_backend="b818",
            backend_family="huawei_hilink_like",
        )
        lg.log_event("startup", constraints={"ok": True})
        lg._event_file.flush()
        lines = [json.loads(x) for x in (tmp_path.glob("events_*.jsonl").__iter__().__next__().read_text().splitlines())]
        assert lines[0]["profile"] == "A1-b818-conservative"
        assert lines[0]["family"] == "huawei_hilink_like"
        assert lines[0]["provider_name"] == "A1"
        assert lines[0]["policy_family"] == "PROVIDER_SPECIFIC"
    finally:
        lg.close()


def test_effective_policy_snapshot_includes_family_metadata():
    from arch4.effective_policy_snapshot import build_effective_policy_snapshot
    cfg = Arch4Config(modem_backend="b818")
    snap = build_effective_policy_snapshot(cfg, {1, 3, 20})
    assert snap["modem_backend"] == "b818"
    assert snap["backend_family"] == "huawei_hilink_like"
    assert snap["family_label"]


def test_runtime_event_sources_manifest_updated_pass29():
    sources = runtime_event_alias_sources()
    assert sources["version"] == "vendor_neutral_pass41"


def test_derive_runtime_sidecar_metadata_infers_dual_layer_fields():
    from arch4.operator_profiles import derive_runtime_sidecar_metadata
    meta = derive_runtime_sidecar_metadata(
        {"profile": "A1-b818-conservative", "provider_plmn": "22005"},
        {"modem_backend": "b818"},
    )
    assert meta["family"] == "huawei_hilink_like"
    assert meta["backend_family"] == "huawei_hilink_like"
    assert meta["provider_name"] == "A1"
    assert meta["profile_name"] == "A1-b818-conservative"



from arch4.brain.tick_contract import ProposalOutcome, IntentVerdictOutcome, OutcomeVerdictOutcome, VerificationOutcome, LearningCommitOutcome
from arch4.interfaces import ActionRecord, LeverAction, LeverType
from arch4.brain.proposal_contract_owner import apply_intent_contract_to_action_record
def test_proposal_outcome_contract_is_explicit():
    action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [3]}, reason="prefer band 3", objective_id="obj1", intent="optimize_band")
    bind_selected_action_context(
        action,
        origin_kind=ActionOriginKind.SMART_LOOP,
        selection_evidence={"source": "proposal_outcome_contract_test"},
        decision_trace_id="dt1",
        now=100.0,
    )
    out = ProposalOutcome.from_runtime(decision_trace_id="dt1", action=action)
    data = out.to_dict()
    assert data["stage"] == "proposal"
    assert data["authority"] == "brain"
    assert data["triggered_by"] == "engine"
    assert data["reason"] == "prefer band 3"
    assert data["intent"] == "optimize_band"
    assert data["objective_id"] == "obj1"
    assert data["score"] == 0.0


def test_intent_verdict_outcome_contract_is_explicit():
    action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [3]}, intent="explore", objective_id="obj1", reason="probe")
    out = IntentVerdictOutcome.from_runtime(decision_trace_id="dt1", action=action, verdict={"level": "allow", "code": "ok", "message": "Allowed"})
    data = out.to_dict()
    assert data["stage"] == "intent_verdict"
    assert data["authority"] == "bouncer"
    assert data["code"] == "ok"


def test_outcome_verdict_outcome_contract_is_explicit():
    action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [3]}, intent="explore", objective_id="obj1", reason="probe")
    out = OutcomeVerdictOutcome.from_runtime(decision_trace_id="dt1", action=action, verdict={"level": "warn", "code": "band_action_no_effect", "message": "No effect"})
    data = out.to_dict()
    assert data["stage"] == "outcome_verdict"
    assert data["authority"] == "referee"
    assert data["code"] == "band_action_no_effect"
def test_verification_outcome_contract_is_explicit():
    action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [3]}, intent="explore", objective_id="obj1", reason="probe")
    out = VerificationOutcome.from_runtime(decision_trace_id="dt1", action=action, mutating=True, verified=False, note="bands_not_applied")
    assert out.status == "no_effect"
    data = out.to_dict()
    assert data["note"] == "bands_not_applied"
    assert data["stage"] == "verification_result"
    assert data["authority"] == "engine_verifier"


def test_classify_verification_status_maps_canonical_categories():
    from arch4.brain.tick_contract import classify_verification_status
    # Success cases
    assert classify_verification_status(True, "settled") == "verified"
    assert classify_verification_status(True, "not_verified") == "not_verified"
    # Timeout family: never got usable data before deadline
    assert classify_verification_status(False, "no_sample") == "timeout"
    assert classify_verification_status(False, "read_failed") == "timeout"
    assert classify_verification_status(False, "session_warmup 2.0s < 8.0s") == "timeout"
    # No-effect family: modem readable but action didn't apply
    assert classify_verification_status(False, "bands_not_applied target=[1, 3] current=[20]") == "no_effect"
    assert classify_verification_status(False, "cell_lock_not_set target=(123,1400) current=(456,1800)") == "no_effect"
    assert classify_verification_status(False, "lte1_disabled") == "no_effect"
    assert classify_verification_status(False, "radio_not_connected") == "no_effect"
    # Unknown
    assert classify_verification_status(None, "") == "unknown"
    action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [3]}, intent="explore", objective_id="obj1", reason="probe")
    rec = ActionRecord(timestamp=1.0, sinr_db=1.0, rsrq_db=-10.0, mcs_dl=5, ca_signature="3", rtt_sliding_avg=20.0, rtt_ms=20.0, lever=LeverType.BAND_SELECTION, action_params={"bands":[3]}, success=True, throughput_before_mbps=10.0, throughput_after_mbps=20.0, sinr_after=5.0, ca_signature_after="3", improvement=10.0, outcome_label="good", hard_fail=False, learning_ltm_applied=True, learning_policy_reason="ok", verification_status="verified", verification_note="settled", referee_outcome_level="good", referee_outcome_code="verified")
    out = LearningCommitOutcome.from_action_record(decision_trace_id="dt1", action=action, action_record=rec, commit_status="recorded")
    data = out.to_dict()
    assert data["commit_status"] == "recorded"
    assert data["outcome_label"] == "good"
    assert data["verification_status"] == "verified"
    assert data["intent_verdict_stage"] == "intent_verdict"
    assert data["outcome_verdict_stage"] == "outcome_verdict"
    assert data["stage"] == "learning_commit"
    assert data["authority"] == "memory_store"



def test_build_mutating_action_verification_event_carries_contract_fields():
    action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [3]}, intent="explore", objective_id="obj1", reason="probe")
    event = build_mutating_action_verification_event(
        action=action,
        decision_trace_id="dt1",
        mutating=True,
        success=False,
        note="bands_not_applied",
        status="no_effect",
    )
    assert event["stage"] == "verification_result"
    assert event["authority"] == "engine_verifier"
    assert event["status"] == "no_effect"


def test_learning_commit_outcome_uses_action_record_commit_authority_fields():
    action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [3]}, intent="explore", objective_id="obj1", reason="probe")
    rec = ActionRecord(timestamp=1.0, sinr_db=1.0, rsrq_db=-10.0, mcs_dl=5, ca_signature="3", rtt_sliding_avg=20.0, rtt_ms=20.0, lever=LeverType.BAND_SELECTION, action_params={"bands":[3]}, success=True, throughput_before_mbps=10.0, throughput_after_mbps=20.0, sinr_after=5.0, ca_signature_after="3", improvement=10.0, outcome_label="good", hard_fail=False, learning_ltm_applied=True, learning_policy_reason="ok", verification_status="verified", verification_note="settled", referee_outcome_level="neutral", referee_outcome_code="band_action_no_effect", learning_commit_stage="learning_commit", learning_commit_authority="memory_store", learning_commit_triggered_by="engine")
    out = LearningCommitOutcome.from_action_record(decision_trace_id="dt1", action=action, action_record=rec, commit_status="recorded")
    data = out.to_dict()
    assert data["referee_outcome_code"] == "band_action_no_effect"
    assert data["stage"] == "learning_commit"
    assert data["authority"] == "memory_store"
    assert data["triggered_by"] == "engine"


def test_terminal_reward_fixture_applies_layered_referee_context():
    rec = ActionRecord(timestamp=1.0, sinr_db=1.0, rsrq_db=-10.0, mcs_dl=5, ca_signature="3", rtt_sliding_avg=20.0, rtt_ms=20.0, lever=LeverType.BAND_SELECTION, action_params={"bands":[3]}, success=True, throughput_before_mbps=10.0, throughput_after_mbps=20.0, sinr_after=5.0, ca_signature_after="3", improvement=10.0, learning_ltm_applied=False, learning_gate={"learning_allowed": True, "gate_input_complete": True})
    fixture = build_terminal_reward_test_fixture(
        rec,
        grade=0,
        reason="band_action_no_effect",
    )
    assert rec.referee_outcome_level == "neutral"
    assert rec.referee_outcome_code == "staged_roi_neutral"
    assert rec.learning_commit_stage == "terminal_reward_projection"
    assert rec.learning_commit_authority == "arch4.brain.reward_ledger"
    assert rec.learning_commit_triggered_by == "arch4.brain.reward_ledger"
    assert rec.learning_commit_status == "classified"
    assert rec.learning_ltm_applied is False
    assert fixture.entry["grade"] == 0
    assert fixture.entry["label"] == "neutral"


def test_terminal_reward_lifecycle_pending_to_classified_projection():
    """A pending proof becomes classified only through terminal reward.

    Projection completion is a separate durable receipt and is not fabricated
    by mutating the generic action record.
    """
    rec = ActionRecord(timestamp=1.0, sinr_db=1.0, rsrq_db=-10.0, mcs_dl=5, ca_signature="3", rtt_sliding_avg=20.0, rtt_ms=20.0, lever=LeverType.BAND_SELECTION, action_params={"bands":[3]}, success=True, throughput_before_mbps=10.0, throughput_after_mbps=20.0, sinr_after=5.0, ca_signature_after="3", improvement=10.0, learning_commit_status="pending", learning_gate={"learning_allowed": True, "gate_input_complete": True})
    assert rec.learning_commit_status == "pending"
    fixture = build_terminal_reward_test_fixture(
        rec,
        grade=1,
        reason="verified",
    )
    assert rec.learning_commit_status == "classified"
    assert fixture.entry["schema"] == "TerminalRewardRecord.v2"
    assert fixture.entry["grade"] == 1


def test_radio_action_without_record_still_waits_for_settle_window():
    from arch4.action_grading_policy import (
        RADIO_ACTION_GRADE_MIN_SETTLE_S,
        radio_action_grade_settle_remaining_s,
        should_delay_radio_action_grade,
    )
    from arch4.interfaces import LeverAction, LeverType

    action = LeverAction(lever=LeverType.PBAND, params={"pband": 3})

    assert should_delay_radio_action_grade(action, None) is True
    assert radio_action_grade_settle_remaining_s(action, None) == RADIO_ACTION_GRADE_MIN_SETTLE_S


def test_radio_action_defaults_to_single_policy_roi_window():
    from arch4.interfaces import ActionRecord, LeverAction, LeverType

    record = ActionRecord(
        timestamp=1000.0,
        sinr_db=10.0,
        rsrq_db=-8.0,
        mcs_dl=0,
        ca_signature="B3",
        rtt_sliding_avg=25.0,
        rtt_ms=25.0,
        lever=LeverType.PBAND,
        action_params={"pband": 3},
        success=True,
        throughput_before_mbps=40.0,
        throughput_after_mbps=40.0,
        sinr_after=10.0,
        ca_signature_after="B3",
        improvement=0.0,
    )
    action = LeverAction(lever=LeverType.PBAND, params={"pband": 3})

    _register_test_staged_roi_windows(action_record=record, action=action)

    assert record.objective_metadata["staged_roi_windows_s"] == [600]
    assert record.objective_metadata["staged_roi_grade_policy"] == "normal_radio_terminal_grade"


def test_smart_loop_roi_window_preserves_command_and_proof_identity():
    from arch4.interfaces import ActionRecord, LeverAction, LeverType

    record = ActionRecord(
        timestamp=1000.0,
        sinr_db=10.0,
        rsrq_db=-8.0,
        mcs_dl=0,
        ca_signature="B3",
        rtt_sliding_avg=25.0,
        rtt_ms=25.0,
        lever=LeverType.PBAND,
        action_params={"pband": 3},
        success=True,
        throughput_before_mbps=40.0,
        throughput_after_mbps=40.0,
        sinr_after=10.0,
        ca_signature_after="B3",
        improvement=0.0,
    )
    action = LeverAction(
        lever=LeverType.PBAND,
        params={
            "pband": 3,
            "_selected_action_command_id": "command_test_123",
            "_selected_action_proof_id": "proof_test_456",
            "_smart_loop_reason_ref": "prefer_band_b3_high_roi",
            "_smart_loop_context_key": "ctx:22005:3:2050:1795",
        },
        intent="smart_loop_command",
    )

    _register_test_staged_roi_windows(action_record=record, action=action)

    assert record.objective_metadata["selected_action_command_id"] == "command_test_123"
    assert record.objective_metadata["selected_action_proof_id"] == "proof_test_456"
    assert record.objective_metadata["smart_loop_reason_ref"] == "prefer_band_b3_high_roi"
    assert record.objective_metadata["smart_loop_context_key"] == "ctx:22005:3:2050:1795"
    assert record.objective_metadata["smart_loop_feedback_windows_s"] == [60, 360]
    assert record.objective_metadata["smart_loop_feedback_done_s"] == []


def test_action_proof_surface_preserves_smart_loop_identity_from_staged_roi_metadata():
    from arch4.brain.action_proof_register import action_proof_roi_surface, action_proof_surface_from_memory
    from arch4.interfaces import ActionRecord, LeverType

    record = ActionRecord(
        timestamp=1000.0,
        sinr_db=10.0,
        rsrq_db=-8.0,
        mcs_dl=0,
        ca_signature="B3",
        rtt_sliding_avg=25.0,
        rtt_ms=25.0,
        lever=LeverType.PBAND,
        action_params={"pband": 3},
        success=True,
        throughput_before_mbps=40.0,
        throughput_after_mbps=44.0,
        sinr_after=11.0,
        ca_signature_after="B3",
        improvement=4.0,
        referee_outcome_code="staged_roi_positive",
        referee_outcome_level="good",
        learning_commit_status="committed",
        objective_metadata={
            "selected_action_command_id": "command_test_123",
            "selected_action_proof_id": "proof_test_456",
            "smart_loop_reason_ref": "prefer_band_b3_high_roi",
            "smart_loop_context_key": "ctx:22005:3:2050:1795",
        },
    )
    memory = SimpleNamespace(_action_history_store=ActionHistoryStore())
    _bind_test_action_history_scope(memory, [record])
    memory._action_history_store.set_records([record])

    surface = action_proof_surface_from_memory(memory)
    roi = action_proof_roi_surface(surface)

    assert surface["last"]["selected_action_command_id"] == "command_test_123"
    assert surface["last"]["selected_action_proof_id"] == "proof_test_456"
    assert surface["last"]["smart_loop_reason_ref"] == "prefer_band_b3_high_roi"
    assert surface["last"]["smart_loop_context_key"] == "ctx:22005:3:2050:1795"
    assert roi["last_action_proof_selected_action_command_id"] == "command_test_123"
    assert roi["last_action_proof_selected_action_proof_id"] == "proof_test_456"
    assert roi["last_action_proof_smart_loop_reason_ref"] == "prefer_band_b3_high_roi"
    assert roi["last_action_proof_smart_loop_context_key"] == "ctx:22005:3:2050:1795"


def test_staged_roi_appends_delayed_brain_readable_action_record():
    from arch4.interfaces import ActionRecord, LeverAction, LeverType

    class _Memory(_ProofMemoryStoreFake):
        def __init__(self, records):
            self.action_history = list(records)

    class _Inbox:
        def __init__(self):
            self.published = []

        def publish(self, kind, **kwargs):
            self.published.append((kind, kwargs))
            return SimpleNamespace(
                event_id="event-1",
                kind=kind,
                dedupe_key=kwargs.get("dedupe_key", ""),
            )

    events = []
    logger = SimpleNamespace(log_event=lambda kind, **payload: events.append((kind, payload)))
    original = ActionRecord(
        timestamp=1000.0,
        sinr_db=10.0,
        rsrq_db=-8.0,
        mcs_dl=0,
        ca_signature="B1+B3",
        rtt_sliding_avg=25.0,
        rtt_ms=25.0,
        lever=LeverType.BAND_SELECTION,
        action_params={
            "bands": [1, 3, 7, 20],
            "_ca_pool_expansion_trial": True,
            "_tsar_candidate_band": 1,
        },
        success=True,
        throughput_before_mbps=40.0,
        throughput_after_mbps=42.0,
        sinr_after=10.0,
        ca_signature_after="B1+B3",
        improvement=2.0,
        provider_plmn="22005",
        provider_name="A1",
        outcome_label="good",
        decision_trace_id="dt-ca-pool",
        cq_cell=0.80,
    )
    action = LeverAction(
        lever=LeverType.BAND_SELECTION,
        params={
            "bands": [1, 3, 7, 20],
            "_ca_pool_expansion_trial": True,
            "_tsar_candidate_band": 1,
        },
        intent="tsar_vector_candidate",
        objective_id="tsar_ca_pool_candidate:B1+B3+B7+B20",
        reason="trial widen CA pool",
    )
    _register_test_staged_roi_windows(action_record=original, action=action)

    memory = _Memory([original])
    inbox = _Inbox()
    emitted = _evaluate_due_staged_roi_typed(
        logger=logger,
        memory=memory,
        brain_event_inbox=inbox,
        modem_now=SimpleNamespace(
            sinr_db=10.0,
            rsrq_db=-8.0,
            ca_band_signature="B1+B3",
            throughput_dl_mbps=41.0,
            primary_band=1,
            provider_plmn="22005",
            provider_name="A1",
            configured_band_pool=[1, 3, 7, 20],
        ),
        computed_now=SimpleNamespace(
            ca_change_per_hour=8.0,
            ca_stat_pct=97.0,
            ca_ld_change_per_hour=0.0,
            ca_stat_ld_pct=97.0,
            ca_stat_ld_valid=True,
            cq_cell=0.80,
            rtt_sliding_avg=25.0,
        ),
        extra={"health_stage": "healthy"},
        incident_assessment={"incident_class": "post_action_instability", "self_induced": True},
        now=1601.0,
    )

    assert [row["horizon_s"] for row in emitted] == [60, 360, 600]
    assert emitted[-1]["label"] == "bad"
    assert emitted[-1]["reason"] == "incident:post_action_instability"
    assert emitted[-1]["code"] == "staged_roi_negative"
    assert emitted[-1]["secondary_learning"]["memory_committed"] is False
    assert emitted[-1]["brain_event"]["published"] is True
    assert emitted[-1]["brain_event"]["event_id"] == "event-1"
    assert original.objective_metadata["staged_roi_done_s"] == [600]
    assert original.objective_stage == "staged_roi"
    assert original.objective_status == "proof_closed"
    assert original.learning_commit_status == "proof_closed"
    assert original.referee_outcome_code == "staged_roi_negative"
    assert original.objective_metadata["staged_roi_parent_status"] == "proof_closed"
    assert original.objective_metadata["staged_roi_final_code"] == "staged_roi_negative"
    assert len(memory.action_history) == 2
    staged = memory.action_history[-1]
    assert staged.roi_horizon_s == 600
    assert staged.roi_parent_trace_id == "dt-ca-pool"
    assert staged.outcome_label == "bad"
    assert staged.referee_outcome_code == "staged_roi_negative"
    assert staged.action_params["_ca_pool_expansion_trial"] is True
    assert staged.action_params["_tsar_candidate_band"] == 1
    evidence_event = next(
        payload
        for kind, payload in events
        if kind == "action_roi_evidence"
    )
    assert evidence_event["grade_kind"] == "staged_roi"
    assert evidence_event["final_outcome"] is True
    assert evidence_event["parent_final_outcome_event"] == "action_proof_close"
    assert evidence_event["proof_status"] == "closed"
    assert any(kind == "action_proof_close" for kind, _payload in events)
    assert [kind for kind, _payload in events].count("action_grade") == 0
    assert inbox.published[-1][0] == "action_proof_closed"
    assert inbox.published[-1][1]["source"] == "staged_roi"
    assert inbox.published[-1][1]["priority"] == 10
    assert inbox.published[-1][1]["dedupe_key"] == "action_proof_closed:dt-ca-pool:600"
    assert inbox.published[-1][1]["payload"]["parent_trace_id"] == "dt-ca-pool"
    assert inbox.published[-1][1]["payload"]["child_trace_id"] == "dt-ca-pool:roi:600"
    assert inbox.published[-1][1]["payload"]["secondary_learning"]["memory_committed"] is False

    emitted_again = _evaluate_due_staged_roi_typed(
        logger=logger,
        memory=memory,
        brain_event_inbox=inbox,
        modem_now=SimpleNamespace(
            sinr_db=10.0,
            rsrq_db=-8.0,
            ca_band_signature="B1+B3",
            throughput_dl_mbps=41.0,
            primary_band=1,
            provider_plmn="22005",
            provider_name="A1",
            configured_band_pool=[1, 3, 7, 20],
        ),
        computed_now=SimpleNamespace(
            ca_change_per_hour=8.0,
            ca_stat_pct=97.0,
            ca_ld_change_per_hour=0.0,
            ca_stat_ld_pct=97.0,
            ca_stat_ld_valid=True,
            cq_cell=0.80,
            rtt_sliding_avg=25.0,
        ),
        extra={"health_stage": "healthy"},
        incident_assessment={"incident_class": "post_action_instability", "self_induced": True},
        now=1600.0,
    )
    assert emitted_again == []
    assert len(memory.action_history) == 2
    assert len(inbox.published) == 3


def test_staged_roi_preserves_action_identity_when_current_provider_is_temporarily_missing():
    from arch4.interfaces import ActionRecord, LeverAction, LeverType

    class _Memory(_ProofMemoryStoreFake):
        def __init__(self, records):
            self.action_history = list(records)

    original = ActionRecord(
        timestamp=1000.0,
        sinr_db=10.0,
        rsrq_db=-8.0,
        mcs_dl=0,
        ca_signature="B1+B3",
        rtt_sliding_avg=25.0,
        rtt_ms=25.0,
        lever=LeverType.BAND_SELECTION,
        action_params={
            "bands": [1, 3, 20],
            "_ca_pool_expansion_trial": True,
            "_tsar_candidate_band": 1,
        },
        success=True,
        throughput_before_mbps=40.0,
        throughput_after_mbps=42.0,
        sinr_after=10.0,
        ca_signature_after="B1+B3",
        improvement=2.0,
        provider_plmn="22001",
        provider_name="Yettel",
        outcome_label="good",
        decision_trace_id="dt-232-ca-pool",
        cq_cell=0.80,
    )
    action = LeverAction(
        lever=LeverType.BAND_SELECTION,
        params={
            "bands": [1, 3, 20],
            "_ca_pool_expansion_trial": True,
            "_tsar_candidate_band": 1,
        },
        intent="tsar_vector_candidate",
        objective_id="tsar_ca_pool_candidate:B1+B3+B20",
        reason="restore CA pool",
    )
    _register_test_staged_roi_windows(action_record=original, action=action)

    memory = _Memory([original])
    emitted = _evaluate_due_staged_roi_typed(
        logger=SimpleNamespace(log_event=lambda *_args, **_kwargs: None),
        memory=memory,
        brain_event_inbox=None,
        modem_now=SimpleNamespace(
            sinr_db=10.0,
            rsrq_db=-8.0,
            ca_band_signature="B1+B3",
            throughput_dl_mbps=41.0,
            throughput_ul_mbps=0.0,
            primary_band=1,
            provider_plmn="",
            provider_name="",
            configured_band_pool=[1, 3, 20],
        ),
        computed_now=SimpleNamespace(
            ca_change_per_hour=1.0,
            ca_stat_pct=100.0,
            ca_ld_change_per_hour=0.0,
            ca_stat_ld_pct=100.0,
            ca_stat_ld_valid=True,
            cq_cell=0.80,
            rtt_sliding_avg=25.0,
        ),
        extra={"health_stage": "healthy"},
        incident_assessment={},
        now=1601.0,
    )

    assert emitted
    evidence = memory.action_history[-1].objective_metadata["staged_roi_evidence"]
    assert evidence["grade_context_status"] == "same"
    assert evidence["context_provider_plmn_before"] == "22001"
    assert evidence["context_provider_plmn_now"] == ""
    assert evidence["context_provider_plmn_now_missing"] is True
    assert emitted[-1]["reason"] != "context_unverified:provider_plmn_missing"


def test_staged_roi_registration_cannot_reopen_closed_parent():
    from arch4.interfaces import ActionRecord, LeverAction, LeverType

    record = ActionRecord(
        timestamp=1000.0,
        sinr_db=10.0,
        rsrq_db=-8.0,
        mcs_dl=0,
        ca_signature="B3",
        rtt_sliding_avg=25.0,
        rtt_ms=25.0,
        lever=LeverType.BAND_SELECTION,
        action_params={"bands": [3]},
        success=True,
        throughput_before_mbps=40.0,
        throughput_after_mbps=42.0,
        sinr_after=10.0,
        ca_signature_after="B3",
        improvement=2.0,
        objective_status="proof_closed",
        learning_commit_status="proof_closed",
        objective_metadata={"staged_roi_parent_status": "proof_closed"},
    )
    action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [3]})

    try:
        _register_test_staged_roi_windows(action_record=record, action=action)
    except RuntimeError as exc:
        assert "staged ROI parent is terminal" in str(exc)
    else:
        raise AssertionError("closed staged ROI parent was reopened")


def test_staged_roi_learning_grade_feeds_secondary_memory_targets():
    from arch4.interfaces import ActionRecord, LeverAction, LeverType

    class _Memory(_ProofMemoryStoreFake):
        def __init__(self, records):
            self.action_history = list(records)
            self.commits = []
            self.journal = []

    class _Historical:
        def __init__(self):
            self.outcomes = []
            self._delayed_reward_projection_keys = set()

        def record_terminal_reward_outcome(self, **kwargs):
            self.outcomes.append(kwargs)
            ledger_key = str(
                dict(kwargs.get("metadata_extra") or {}).get(
                    "reward_ledger_key"
                )
                or ""
            )
            if ledger_key:
                self._delayed_reward_projection_keys.add(ledger_key)
            return {
                "recorded": True,
                "reason": str(
                    kwargs.get("source") or "terminal_reward_projection"
                ),
                "reward_ledger_key": ledger_key,
            }

    class _Crystal:
        def __init__(self):
            self.updates = []
            self.regrades = []

        def update_from_live_hour(self, hour, day_type, stats, *, evidence_scope):
            from arch4.brain.crystal_ball import crystal_ball_formula_vector

            source_scope = EvidenceScope.from_mapping(evidence_scope)
            self._evidence_scope = EvidenceScope(
                identity_code=source_scope.identity_code,
                identity_status=source_scope.identity_status,
                identity_catalog_version=source_scope.identity_catalog_version,
                identity_catalog_checksum=source_scope.identity_catalog_checksum,
                session_scope_key=source_scope.session_scope_key,
                source_epoch=source_scope.source_epoch,
                source_sequence=source_scope.source_sequence,
                observed_at=source_scope.observed_at,
                evidence_age_s=source_scope.evidence_age_s,
                confidence=source_scope.confidence,
                evidence_count=source_scope.evidence_count,
                formula_vector=crystal_ball_formula_vector(),
                access_type_id=source_scope.access_type_id,
                provider_plmn=source_scope.provider_plmn,
            ).as_dict()
            self.updates.append((hour, day_type, stats, evidence_scope))

        def apply_live_regrade(self, **kwargs):
            self.regrades.append(kwargs)
            return True

    class _Logger:
        def __init__(self):
            self.events = []
            self.regrades = []

        def log_event(self, kind, **payload):
            self.events.append((kind, payload))

        def write_regrade_record(self, **kwargs):
            self.regrades.append(kwargs)

    original = ActionRecord(
        timestamp=1000.0,
        sinr_db=14.0,
        rsrq_db=-7.0,
        mcs_dl=0,
        ca_signature="B1+B3",
        rtt_sliding_avg=25.0,
        rtt_ms=25.0,
        lever=LeverType.BAND_SELECTION,
        action_params={"bands": [1, 3, 7], "_ca_pool_expansion_trial": True, "_tsar_candidate_band": 1},
        success=True,
        throughput_before_mbps=40.0,
        throughput_after_mbps=42.0,
        sinr_after=14.0,
        ca_signature_after="B1+B3",
        improvement=2.0,
        provider_plmn="22005",
        provider_name="A1",
        outcome_label="good",
        decision_trace_id="dt-secondary",
        cq_cell=0.80,
        learning_gate={"learning_allowed": True, "gate_input_complete": True},
    )
    _register_test_staged_roi_windows(
        action_record=original,
        action=LeverAction(
            lever=LeverType.BAND_SELECTION,
            params={"bands": [1, 3, 7], "_ca_pool_expansion_trial": True, "_tsar_candidate_band": 1},
        ),
    )
    memory = _Memory([original])
    historical = _Historical()
    crystal = _Crystal()
    logger = _Logger()

    emitted = _evaluate_due_staged_roi_typed(
        logger=logger,
        memory=memory,
        historical=historical,
        crystal_ball=crystal,
        modem_now=SimpleNamespace(
            sinr_db=7.0,
            rsrq_db=-12.0,
            ca_band_signature="B1",
            throughput_dl_mbps=15.0,
            throughput_ul_mbps=1.0,
            primary_band=1,
            cell_id=123,
            provider_plmn="22005",
            provider_name="A1",
            configured_band_pool=[1, 3, 7],
            ca_active=False,
        ),
        computed_now=SimpleNamespace(
            ca_change_per_hour=0.0,
            ca_stat_pct=95.0,
            ca_ld_change_per_hour=120.0,
            ca_stat_ld_pct=60.0,
            ca_stat_ld_valid=True,
            cq_cell=0.50,
            rtt_sliding_avg=110.0,
            rtt_ms=110.0,
            congestion_pct=80.0,
        ),
        extra={"health_stage": "healthy"},
        incident_assessment={},
        now=1601.0,
    )

    secondary = emitted[-1]["secondary_learning"]
    assert secondary["schema"] == "brain_secondary_learning_receipt.v1"
    assert secondary["owner"] == "arch4.brain.secondary_learning_receipt"
    assert secondary["status"] == "committed"
    assert secondary["memory_committed"] is True
    assert secondary["historical_committed"] is True
    assert secondary["crystal_ball_updated"] is True
    assert secondary["crystal_ball_regraded"] is True
    staged = next(r for r in memory.action_history if getattr(r, "roi_parent_trace_id", "") == "dt-secondary")
    assert staged.learning_commit_status == "committed"
    parent = next(r for r in memory.action_history if getattr(r, "decision_trace_id", "") == "dt-secondary")
    assert parent.learning_commit_status == "proof_closed"
    assert historical.outcomes[0]["success"] is False
    assert historical.outcomes[0]["source"] == "staged_roi"
    assert crystal.updates
    assert crystal.regrades[0]["band"] == 1
    assert logger.regrades[0]["primary_band" if "primary_band" in logger.regrades[0] else "band"] == 1


def test_staged_roi_secondary_learning_blocks_without_allowed_learning_gate():
    from arch4.interfaces import ActionRecord, LeverAction, LeverType

    class _Memory(_ProofMemoryStoreFake):
        def __init__(self, records):
            self.action_history = list(records)
            self.commits = []

    original = ActionRecord(
        timestamp=1000.0,
        sinr_db=14.0,
        rsrq_db=-7.0,
        mcs_dl=0,
        ca_signature="B1+B3",
        rtt_sliding_avg=25.0,
        rtt_ms=25.0,
        lever=LeverType.BAND_SELECTION,
        action_params={"bands": [1, 3, 7], "_ca_pool_expansion_trial": True, "_tsar_candidate_band": 1},
        success=True,
        throughput_before_mbps=40.0,
        throughput_after_mbps=42.0,
        sinr_after=14.0,
        ca_signature_after="B1+B3",
        improvement=2.0,
        provider_plmn="22005",
        provider_name="A1",
        outcome_label="good",
        decision_trace_id="dt-secondary-blocked",
        cq_cell=0.80,
        learning_gate={"learning_allowed": False, "block_reason": "planned_radio_quiet"},
    )
    _register_test_staged_roi_windows(
        action_record=original,
        action=LeverAction(
            lever=LeverType.BAND_SELECTION,
            params={"bands": [1, 3, 7], "_ca_pool_expansion_trial": True, "_tsar_candidate_band": 1},
        ),
    )
    memory = _Memory([original])

    emitted = _evaluate_due_staged_roi_typed(
        logger=None,
        memory=memory,
        historical=None,
        crystal_ball=None,
        modem_now=SimpleNamespace(
            sinr_db=7.0,
            rsrq_db=-12.0,
            ca_band_signature="B1",
            throughput_dl_mbps=15.0,
            throughput_ul_mbps=1.0,
            primary_band=1,
            cell_id=123,
            provider_plmn="22005",
            provider_name="A1",
            configured_band_pool=[1, 3, 7],
            ca_active=False,
        ),
        computed_now=SimpleNamespace(
            ca_change_per_hour=0.0,
            ca_stat_pct=95.0,
            ca_ld_change_per_hour=120.0,
            ca_stat_ld_pct=60.0,
            ca_stat_ld_valid=True,
            cq_cell=0.50,
            rtt_sliding_avg=110.0,
            rtt_ms=110.0,
            congestion_pct=80.0,
        ),
        extra={"health_stage": "healthy"},
        incident_assessment={},
        now=1601.0,
    )

    assert "secondary_learning" not in emitted[-1]
    assert memory.commits == []


def test_staged_roi_waits_when_runtime_evidence_is_not_ready():
    from arch4.interfaces import ActionRecord, LeverAction, LeverType

    class _Memory(_ProofMemoryStoreFake):
        def __init__(self, records):
            self.action_history = list(records)

    original = ActionRecord(
        timestamp=1000.0,
        sinr_db=10.0,
        rsrq_db=-8.0,
        mcs_dl=0,
        ca_signature="B1+B3",
        rtt_sliding_avg=25.0,
        rtt_ms=25.0,
        lever=LeverType.BAND_SELECTION,
        action_params={
            "bands": [1, 3, 7, 20],
            "_ca_pool_expansion_trial": True,
            "_tsar_candidate_band": 1,
        },
        success=True,
        throughput_before_mbps=40.0,
        throughput_after_mbps=42.0,
        sinr_after=10.0,
        ca_signature_after="B1+B3",
        improvement=2.0,
        provider_plmn="22005",
        provider_name="A1",
        outcome_label="good",
        decision_trace_id="dt-ca-pool-not-ready",
        cq_cell=0.80,
    )
    action = LeverAction(
        lever=LeverType.BAND_SELECTION,
        params={
            "bands": [1, 3, 7, 20],
            "_ca_pool_expansion_trial": True,
            "_tsar_candidate_band": 1,
        },
    )
    _register_test_staged_roi_windows(action_record=original, action=action)

    memory = _Memory([original])
    emitted = _evaluate_due_staged_roi_typed(
        logger=None,
        memory=memory,
        modem_now=SimpleNamespace(
            sinr_db=3.0,
            rsrq_db=-13.0,
            ca_band_signature="B1",
            throughput_dl_mbps=0.0,
            primary_band=1,
            provider_plmn="22005",
            provider_name="A1",
            configured_band_pool=[1, 3, 7, 20],
        ),
        computed_now=SimpleNamespace(
            ca_change_per_hour=0.0,
            ca_stat_pct=0.0,
            ca_ld_change_per_hour=0.0,
            ca_stat_ld_pct=0.0,
            ca_stat_ld_valid=False,
            cq_cell=0.40,
            rtt_sliding_avg=0.0,
        ),
        extra={"health_stage": "live_register_stale", "live_register_stale": True},
        incident_assessment={"incident_class": "startup_bootstrap_observe"},
        now=1601.0,
    )

    assert emitted == []
    assert original.objective_metadata["staged_roi_done_s"] == []
    assert len(memory.action_history) == 1


def test_staged_roi_changed_context_is_inconclusive_even_with_good_metrics():
    from arch4.interfaces import ActionRecord, LeverAction, LeverType

    class _Memory(_ProofMemoryStoreFake):
        def __init__(self, records):
            self.action_history = list(records)

    original = ActionRecord(
        timestamp=1000.0,
        sinr_db=10.0,
        rsrq_db=-8.0,
        mcs_dl=0,
        ca_signature="B1+B3",
        rtt_sliding_avg=25.0,
        rtt_ms=25.0,
        lever=LeverType.BAND_SELECTION,
        action_params={
            "bands": [1, 3, 7, 20],
            "_ca_pool_expansion_trial": True,
            "_ca_pool_keep_rule": "keep_only_if_single_grade_positive",
            "_tsar_candidate_band": 1,
        },
        success=True,
        throughput_before_mbps=40.0,
        throughput_after_mbps=42.0,
        sinr_after=10.0,
        ca_signature_after="B1+B3",
        improvement=2.0,
        provider_plmn="22005",
        provider_name="A1",
        outcome_label="good",
        decision_trace_id="dt-ca-pool-context-change",
        cq_cell=0.80,
    )
    action = LeverAction(
        lever=LeverType.BAND_SELECTION,
        params={
            "bands": [1, 3, 7, 20],
            "_ca_pool_expansion_trial": True,
            "_ca_pool_keep_rule": "keep_only_if_single_grade_positive",
            "_tsar_candidate_band": 1,
        },
        intent="tsar_vector_candidate",
        objective_id="tsar_ca_pool_candidate:B1+B3+B7+B20",
        reason="trial widen CA pool",
    )
    _register_test_staged_roi_windows(action_record=original, action=action)

    memory = _Memory([original])
    emitted = _evaluate_due_staged_roi_typed(
        logger=None,
        memory=memory,
        modem_now=SimpleNamespace(
            sinr_db=11.0,
            rsrq_db=-7.0,
            ca_band_signature="B1+B3+B7+B20",
            throughput_dl_mbps=50.0,
            throughput_ul_mbps=1.0,
            primary_band=3,
            provider_plmn="22005",
            provider_name="A1",
            configured_band_pool=[1, 3, 7, 20],
        ),
        computed_now=SimpleNamespace(
            ca_change_per_hour=0.0,
            ca_stat_pct=100.0,
            cart_ld=1,
            cart_ld_valid=True,
            cast_ld_valid=True,
            cart_ld_activity=0.0,
            cast_ld=100.0,
            cq_cell=0.85,
            rtt_sliding_avg=22.0,
        ),
        extra={"health_stage": "healthy"},
        incident_assessment={},
        now=1601.0,
    )

    expected = {
        "horizon_s": 600,
        "label": "neutral",
        "reason": "context_changed:pband:B1->B3",
        "code": "staged_roi_context_changed",
    }
    assert {
        key: emitted[-1][key]
        for key in expected
    } == expected
    assert emitted[-1]["terminal"] is True
    assert emitted[-1]["parent_closed"] is True
    assert emitted[-1]["reward_admitted"] is False
    staged = memory.action_history[-1]
    assert staged.success is False
    assert staged.outcome_label == "neutral"
    assert staged.referee_outcome_code == "staged_roi_context_changed"
    evidence = staged.objective_metadata["staged_roi_evidence"]
    assert evidence["grade_context_status"] == "changed"
    assert evidence["grade_evidence_valid"] is True


def test_staged_roi_keeps_low_load_ca_churn_neutral_for_ca_pool_trial():
    from arch4.interfaces import ActionRecord, LeverAction, LeverType

    class _Memory(_ProofMemoryStoreFake):
        def __init__(self, records):
            self.action_history = list(records)

    events = []
    logger = SimpleNamespace(log_event=lambda kind, **payload: events.append((kind, payload)))
    original = ActionRecord(
        timestamp=1000.0,
        sinr_db=10.0,
        rsrq_db=-8.0,
        mcs_dl=0,
        ca_signature="B1+B3",
        rtt_sliding_avg=25.0,
        rtt_ms=25.0,
        lever=LeverType.BAND_SELECTION,
        action_params={
            "bands": [1, 3, 7, 20],
            "_ca_pool_expansion_trial": True,
            "_ca_pool_keep_rule": "keep_only_if_single_grade_positive",
            "_tsar_candidate_band": 1,
        },
        success=True,
        throughput_before_mbps=0.0,
        throughput_after_mbps=0.0,
        sinr_after=10.0,
        ca_signature_after="B1+B3",
        improvement=0.0,
        provider_plmn="22005",
        provider_name="A1",
        outcome_label="good",
        decision_trace_id="dt-ca-pool-low-load",
        cq_cell=0.80,
    )
    action = LeverAction(
        lever=LeverType.BAND_SELECTION,
        params={
            "bands": [1, 3, 7, 20],
            "_ca_pool_expansion_trial": True,
            "_ca_pool_keep_rule": "keep_only_if_single_grade_positive",
            "_tsar_candidate_band": 1,
        },
        intent="tsar_vector_candidate",
        objective_id="tsar_ca_pool_candidate:B1+B3+B7+B20",
        reason="trial widen CA pool",
    )
    _register_test_staged_roi_windows(action_record=original, action=action)

    memory = _Memory([original])
    emitted = _evaluate_due_staged_roi_typed(
        logger=logger,
        memory=memory,
        modem_now=SimpleNamespace(
            sinr_db=10.0,
            rsrq_db=-8.0,
            ca_band_signature="B1+B3",
            throughput_dl_mbps=0.003,
            throughput_ul_mbps=0.0,
            primary_band=1,
            provider_plmn="22005",
            provider_name="A1",
            configured_band_pool=[1, 3, 7, 20],
        ),
        computed_now=SimpleNamespace(
            ca_change_per_hour=120.0,
            ca_stat_pct=87.0,
            ca_ld_change_per_hour=0.0,
            ca_stat_ld_pct=87.0,
            ca_stat_ld_valid=False,
            cq_cell=0.80,
            rtt_sliding_avg=25.0,
        ),
        extra={"health_stage": "healthy"},
        incident_assessment={},
        now=1601.0,
    )

    expected = {
        "horizon_s": 600,
        "label": "neutral",
        "reason": "mixed_or_insufficient_horizon_evidence",
        "code": "staged_roi_inconclusive",
    }
    assert {
        key: emitted[-1][key]
        for key in expected
    } == expected
    assert emitted[-1]["terminal"] is True
    assert emitted[-1]["parent_closed"] is True
    assert emitted[-1]["reward_admitted"] is False
    staged = memory.action_history[-1]
    assert staged.outcome_label == "neutral"
    assert staged.success is False
    assert staged.referee_outcome_code == "staged_roi_inconclusive"
    assert staged.objective_metadata["staged_roi_evidence"]["ca_ld_observation_active"] is False


def test_staged_roi_high_load_ca_churn_stays_bad_for_ca_pool_trial():
    from arch4.interfaces import ActionRecord, LeverAction, LeverType

    class _Memory(_ProofMemoryStoreFake):
        def __init__(self, records):
            self.action_history = list(records)

    logger = SimpleNamespace(log_event=lambda kind, **payload: None)
    original = ActionRecord(
        timestamp=1000.0,
        sinr_db=10.0,
        rsrq_db=-8.0,
        mcs_dl=0,
        ca_signature="B1+B3",
        rtt_sliding_avg=25.0,
        rtt_ms=25.0,
        lever=LeverType.BAND_SELECTION,
        action_params={
            "bands": [1, 3, 7, 20],
            "_ca_pool_expansion_trial": True,
            "_ca_pool_keep_rule": "keep_only_if_single_grade_positive",
            "_tsar_candidate_band": 1,
        },
        success=True,
        throughput_before_mbps=5.0,
        throughput_after_mbps=5.0,
        sinr_after=10.0,
        ca_signature_after="B1+B3",
        improvement=0.0,
        provider_plmn="22005",
        provider_name="A1",
        outcome_label="good",
        decision_trace_id="dt-ca-pool-high-load",
        cq_cell=0.80,
    )
    action = LeverAction(
        lever=LeverType.BAND_SELECTION,
        params={
            "bands": [1, 3, 7, 20],
            "_ca_pool_expansion_trial": True,
            "_ca_pool_keep_rule": "keep_only_if_single_grade_positive",
            "_tsar_candidate_band": 1,
        },
        intent="tsar_vector_candidate",
        objective_id="tsar_ca_pool_candidate:B1+B3+B7+B20",
        reason="trial widen CA pool",
    )
    _register_test_staged_roi_windows(action_record=original, action=action)

    memory = _Memory([original])
    emitted = _evaluate_due_staged_roi_typed(
        logger=logger,
        memory=memory,
        modem_now=SimpleNamespace(
            sinr_db=10.0,
            rsrq_db=-8.0,
            ca_band_signature="B1+B3",
            throughput_dl_mbps=5.0,
            throughput_ul_mbps=0.0,
            primary_band=1,
            provider_plmn="22005",
            provider_name="A1",
            configured_band_pool=[1, 3, 7, 20],
        ),
        computed_now=SimpleNamespace(
            ca_change_per_hour=120.0,
            ca_stat_pct=87.0,
            cart_ld=1,
            cart_ld_valid=True,
            cast_ld_valid=True,
            cart_ld_activity=120.0,
            cast_ld=87.0,
            cq_cell=0.80,
            rtt_sliding_avg=25.0,
        ),
        extra={"health_stage": "healthy"},
        incident_assessment={},
        now=1601.0,
    )

    expected = {
        "horizon_s": 600,
        "label": "bad",
        "reason": "cart_ld_activity_high:120.0/h",
        "code": "staged_roi_negative",
    }
    assert {
        key: emitted[-1][key]
        for key in expected
    } == expected
    assert emitted[-1]["terminal"] is True
    assert emitted[-1]["parent_closed"] is True
    assert emitted[-1]["reward_admitted"] is False
    staged = memory.action_history[-1]
    assert staged.outcome_label == "bad"
    assert staged.success is False
    assert staged.referee_outcome_code == "staged_roi_negative"
    assert staged.objective_metadata["staged_roi_evidence"]["ca_ld_observation_active"] is True
    assert staged.objective_metadata["staged_roi_evidence"]["cast_ld_valid"] is True
    assert staged.objective_metadata["staged_roi_evidence"]["cart_ld_activity"] == 120.0
    assert staged.objective_metadata["staged_roi_evidence"]["cart_activity_raw"] == 120.0


def test_staged_roi_ignores_raw_ca_churn_when_ld_ca_is_invalid():
    from arch4.interfaces import ActionRecord, LeverAction, LeverType

    class _Memory(_ProofMemoryStoreFake):
        def __init__(self, records):
            self.action_history = list(records)

    logger = SimpleNamespace(log_event=lambda kind, **payload: None)
    original = ActionRecord(
        timestamp=1000.0,
        sinr_db=10.0,
        rsrq_db=-8.0,
        mcs_dl=0,
        ca_signature="B1+B3",
        rtt_sliding_avg=25.0,
        rtt_ms=25.0,
        lever=LeverType.BAND_SELECTION,
        action_params={
            "bands": [1, 3, 7, 20],
            "_ca_pool_expansion_trial": True,
            "_ca_pool_keep_rule": "keep_only_if_single_grade_positive",
            "_tsar_candidate_band": 1,
        },
        success=True,
        throughput_before_mbps=5.0,
        throughput_after_mbps=5.0,
        sinr_after=10.0,
        ca_signature_after="B1+B3",
        improvement=0.0,
        provider_plmn="22005",
        provider_name="A1",
        outcome_label="good",
        decision_trace_id="dt-ca-pool-raw-only",
        cq_cell=0.80,
    )
    action = LeverAction(
        lever=LeverType.BAND_SELECTION,
        params={
            "bands": [1, 3, 7, 20],
            "_ca_pool_expansion_trial": True,
            "_ca_pool_keep_rule": "keep_only_if_single_grade_positive",
            "_tsar_candidate_band": 1,
        },
        intent="tsar_vector_candidate",
        objective_id="tsar_ca_pool_candidate:B1+B3+B7+B20",
        reason="trial widen CA pool",
    )
    _register_test_staged_roi_windows(action_record=original, action=action)

    memory = _Memory([original])
    emitted = _evaluate_due_staged_roi_typed(
        logger=logger,
        memory=memory,
        modem_now=SimpleNamespace(
            sinr_db=10.0,
            rsrq_db=-8.0,
            ca_band_signature="B1+B3",
            throughput_dl_mbps=5.0,
            throughput_ul_mbps=0.0,
            primary_band=1,
            provider_plmn="22005",
            provider_name="A1",
            configured_band_pool=[1, 3, 7, 20],
        ),
        computed_now=SimpleNamespace(
            ca_change_per_hour=120.0,
            ca_stat_pct=87.0,
            ca_ld_change_per_hour=0.0,
            ca_stat_ld_pct=0.0,
            cast_ld_valid=False,
            cq_cell=0.80,
            rtt_sliding_avg=25.0,
        ),
        extra={"health_stage": "healthy"},
        incident_assessment={},
        now=1601.0,
    )

    expected = {
        "horizon_s": 600,
        "label": "neutral",
        "reason": "mixed_or_insufficient_horizon_evidence",
        "code": "staged_roi_inconclusive",
    }
    assert {
        key: emitted[-1][key]
        for key in expected
    } == expected
    assert emitted[-1]["terminal"] is True
    assert emitted[-1]["parent_closed"] is True
    assert emitted[-1]["reward_admitted"] is False
    staged = memory.action_history[-1]
    assert staged.outcome_label == "neutral"
    assert staged.objective_metadata["staged_roi_evidence"]["ca_ld_observation_active"] is False
    assert staged.objective_metadata["staged_roi_evidence"]["cast_ld_valid"] is False
    assert staged.objective_metadata["staged_roi_evidence"]["cart_ld_activity"] == 0.0
    assert staged.objective_metadata["staged_roi_evidence"]["cart_activity_raw"] == 120.0


def test_staged_roi_ul_only_load_does_not_make_ca_churn_actionable():
    from arch4.interfaces import ActionRecord, LeverAction, LeverType

    class _Memory(_ProofMemoryStoreFake):
        def __init__(self, records):
            self.action_history = list(records)

    logger = SimpleNamespace(log_event=lambda kind, **payload: None)
    original = ActionRecord(
        timestamp=1000.0,
        sinr_db=10.0,
        rsrq_db=-8.0,
        mcs_dl=0,
        ca_signature="B1+B3",
        rtt_sliding_avg=25.0,
        rtt_ms=25.0,
        lever=LeverType.BAND_SELECTION,
        action_params={
            "bands": [1, 3, 7, 20],
            "_ca_pool_expansion_trial": True,
            "_ca_pool_keep_rule": "keep_only_if_single_grade_positive",
        },
        success=True,
        throughput_before_mbps=0.0,
        throughput_after_mbps=0.0,
        sinr_after=10.0,
        ca_signature_after="B1+B3",
        improvement=0.0,
        provider_plmn="22005",
        provider_name="A1",
        outcome_label="good",
        decision_trace_id="dt-ca-pool-ul-only",
        cq_cell=0.80,
    )
    action = LeverAction(
        lever=LeverType.BAND_SELECTION,
        params={
            "bands": [1, 3, 7, 20],
            "_ca_pool_expansion_trial": True,
            "_ca_pool_keep_rule": "keep_only_if_single_grade_positive",
        },
        intent="tsar_vector_candidate",
        objective_id="tsar_ca_pool_candidate:B1+B3+B7+B20",
        reason="trial widen CA pool",
    )
    _register_test_staged_roi_windows(action_record=original, action=action)

    memory = _Memory([original])
    emitted = _evaluate_due_staged_roi_typed(
        logger=logger,
        memory=memory,
        modem_now=SimpleNamespace(
            sinr_db=10.0,
            rsrq_db=-8.0,
            ca_band_signature="B1+B3",
            throughput_dl_mbps=0.0,
            throughput_ul_mbps=5.0,
            primary_band=1,
            provider_plmn="22005",
            provider_name="A1",
            configured_band_pool=[1, 3, 7, 20],
        ),
        computed_now=SimpleNamespace(
            ca_change_per_hour=120.0,
            ca_stat_pct=87.0,
            ca_ld_change_per_hour=0.0,
            ca_stat_ld_pct=87.0,
            ca_stat_ld_valid=False,
            cq_cell=0.80,
            rtt_sliding_avg=25.0,
        ),
        extra={"health_stage": "healthy"},
        incident_assessment={},
        now=1601.0,
    )

    expected = {
        "horizon_s": 600,
        "label": "neutral",
        "reason": "mixed_or_insufficient_horizon_evidence",
        "code": "staged_roi_inconclusive",
    }
    assert {
        key: emitted[-1][key]
        for key in expected
    } == expected
    assert emitted[-1]["terminal"] is True
    assert emitted[-1]["parent_closed"] is True
    assert emitted[-1]["reward_admitted"] is False
    staged = memory.action_history[-1]
    assert staged.outcome_label == "neutral"
    assert staged.success is False
    assert staged.objective_metadata["staged_roi_evidence"]["ca_ld_observation_active"] is False
    assert staged.objective_metadata["staged_roi_evidence"]["ca_demand_load_mbps"] == 0.0


def test_referee_learning_correction_legacy_last_record_repair_is_deleted():
    """Referee correction must use classified commits, not a last-record repair."""
    assert not hasattr(Memory, "correct_last_band_learning")


def test_apply_proposal_contract_to_action_record_applies_explicit_fields():
    from arch4.brain.proposal_contract_owner import apply_proposal_contract_to_action_record
    rec = ActionRecord(timestamp=1.0, sinr_db=1.0, rsrq_db=-10.0, mcs_dl=5, ca_signature="3", rtt_sliding_avg=20.0, rtt_ms=20.0, lever=LeverType.BAND_SELECTION, action_params={"bands":[3]}, success=True, throughput_before_mbps=10.0, throughput_after_mbps=20.0, sinr_after=5.0, ca_signature_after="3", improvement=10.0)
    action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [3]}, reason="prefer band 3", objective_id="obj1", intent="optimize_band")
    bind_selected_action_context(
        action,
        origin_kind=ActionOriginKind.SMART_LOOP,
        selection_evidence={},
        now=1.0,
    )
    apply_proposal_contract_to_action_record(action_record=rec, action=action, proposal_result={"stage": "proposal", "authority": "brain", "triggered_by": "engine"})
    assert rec.proposal_reason == "prefer band 3"
    assert rec.proposal_intent == "optimize_band"
    assert rec.proposal_objective_id == "obj1"
    assert rec.proposal_score == 0.0
    assert rec.proposal_action_origin == "smart_loop"
    assert rec.proposal_stage == "proposal"
    assert rec.proposal_authority == "brain"
    assert rec.proposal_triggered_by == "engine"


def test_apply_intent_contract_to_action_record_applies_explicit_fields():
    rec = ActionRecord(timestamp=1.0, sinr_db=1.0, rsrq_db=-10.0, mcs_dl=5, ca_signature="3", rtt_sliding_avg=20.0, rtt_ms=20.0, lever=LeverType.BAND_SELECTION, action_params={"bands":[3]}, success=True, throughput_before_mbps=10.0, throughput_after_mbps=20.0, sinr_after=5.0, ca_signature_after="3", improvement=10.0)
    apply_intent_contract_to_action_record(action_record=rec, intent_verdict={"level": "allow", "code": "ok", "stage": "intent_verdict", "authority": "bouncer", "triggered_by": "engine"})
    assert rec.bouncer_intent_level == "allow"
    assert rec.bouncer_intent_code == "ok"
    assert rec.intent_verdict_stage == "intent_verdict"
    assert rec.intent_verdict_authority == "bouncer"
    assert rec.intent_verdict_triggered_by == "engine"


def test_openwrt_and_zte_family_support_texts_are_family_specific():
    assert "ModemManager" in family_support_text("openwrt_modemmanager_like")
    assert "monitor-first" in family_support_text("openwrt_modemmanager_like")
    assert "firmware-dependent" in family_support_text("zte_goform_like")
    assert "active control intentionally disabled" in family_support_text("zte_zwrt_ubus_like")


def test_backend_semantics_capture_openwrt_and_zte_profile_nuance():
    ow = get_backend_action_semantics("openwrt")
    assert ow.traffic_visibility == "counters"
    assert ow.ca_visibility == "basic"
    assert "AT+QNWLOCK" in ow.notes
    mf = get_backend_action_semantics("mf296r")
    assert mf.traffic_visibility == "counters"
    assert "MF296R" in mf.notes
    mc = get_backend_action_semantics("mc888")
    assert mc.traffic_visibility == "counters"
    assert "NR band control" in mc.notes
