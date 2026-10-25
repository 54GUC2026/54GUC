from arch4.canonical_identity_display import build_canonical_identity_display
from arch4.runtime_identity import (
    build_runtime_session_scope,
    build_runtime_identity,
    merge_runtime_session_scope,
    parse_runtime_session_scope_key,
    runtime_device_namespace_key,
    runtime_identity_from_capabilities_payload,
    runtime_physical_device_changed,
    runtime_session_scope_changed,
    runtime_session_scope_is_strict_enrichment,
    runtime_session_scope_key,
    runtime_identity_from_sample_sources,
)


def test_device_namespace_ignores_provider_firmware_and_sim_changes() -> None:
    first = {
        "backend": "b818",
        "serial": "DEVICE-1",
        "firmware": "1.0",
        "provider_plmn": "22005",
        "imsi": "220050000000001",
    }
    later = {
        **first,
        "firmware": "2.0",
        "provider_plmn": "22003",
        "imsi": "220030000000002",
    }

    assert runtime_device_namespace_key(first) == "backend=b818|serial=DEVICE-1"
    assert runtime_device_namespace_key(later) == runtime_device_namespace_key(first)
    assert runtime_physical_device_changed(first, later) is False


def test_device_namespace_changes_only_for_conflicting_permanent_identity() -> None:
    previous = {"backend": "b818", "serial": "DEVICE-1", "imei": "860000000000001"}
    replacement = {"backend": "b818", "serial": "DEVICE-2", "imei": "860000000000002"}

    assert runtime_physical_device_changed(previous, replacement) is True
    assert runtime_device_namespace_key(previous) != runtime_device_namespace_key(replacement)


def test_device_namespace_is_explicitly_provisional_before_serial_or_imei() -> None:
    scope = {
        "target": "192.168.1.1",
        "backend": "mf296r",
        "model": "MF296R",
        "provider_plmn": "22005",
    }

    assert runtime_device_namespace_key(scope) == (
        "target=192.168.1.1|backend=mf296r|model=mf296r"
    )


def test_build_runtime_identity_marks_family_known_model_unknown():
    out = build_runtime_identity(
        backend="b818",
        profile_name="bootstrap",
        runtime_dir="/tmp/guc",
    ).to_dict()

    assert out["backend"] == "b818"
    assert out["backend_family"] == "huawei_hilink_like"
    assert out["identity_state"] == "family_known_model_unknown"
    assert out["runtime_owner"]["profile_name"] == "bootstrap"


def test_runtime_identity_from_capabilities_payload_uses_runtime_identity_and_owner():
    payload = {
        "backend": "mf296r",
        "runtime_identity": {
            "vendor": "ZTE",
            "model": "MF296R",
            "firmware": "BD_RSAV1.0.0B12",
            "provider_name": "A1",
            "provider_plmn": "22005",
        },
        "recognition": {
            "family_hint": "zte_goform_like",
            "confidence": 0.72,
        },
        "runtime_dir": "/tmp/runtime",
        "memory_dir": "/tmp/memory",
        "instance_id": "abc123",
        "profile_name": "bootstrap",
    }

    out = runtime_identity_from_capabilities_payload(payload)

    assert out["backend_family"] == "zte_goform_like"
    assert out["identity_state"] == "identified"
    assert out["identity_confidence"] == 1.0
    assert out["provider_name"] == "A1"
    assert out["provider_plmn"] == "22005"
    assert out["runtime_owner"]["instance_id"] == "abc123"


def test_runtime_identity_from_capabilities_payload_keeps_resolved_card_as_metadata_only():
    payload = {
        "backend": "b818",
        "runtime_identity": {
            "vendor": "Huawei",
            "model": "generic",
            "firmware": "generic-fw",
            "provider_name": "legacy",
            "provider_plmn": "00000",
        },
        "resolved_identity_card": {
            "backend": "b818",
            "family": "huawei_hilink_like",
            "vendor": "Huawei",
            "model": "B535-333",
            "firmware": "11.0.5.51",
            "provider_name": "A1",
            "provider_plmn": "22005",
            "profile_id": "huawei_b535_hilink_baseline",
            "profile_provenance": "documented",
            "exact_identity": False,
            "provisional": True,
            "provisional_reason": "model_specific_needs_exact_firmware",
        },
        "recognition": {
            "family_hint": "wrong_family",
            "confidence": 0.72,
        },
        "runtime_dir": "/tmp/runtime",
        "memory_dir": "/tmp/memory",
    }

    out = runtime_identity_from_capabilities_payload(payload)

    assert out["backend_family"] == "huawei_hilink_like"
    assert out["model"] == "generic"
    assert out["firmware"] == "generic-fw"
    assert out["provider_name"] == "legacy"
    assert out["provider_plmn"] == "00000"
    assert out["resolved_profile_id"] == "huawei_b535_hilink_baseline"
    assert out["resolved_profile_provenance"] == "documented"
    assert out["resolved_identity_exact"] is False
    assert out["resolved_identity_provisional"] is True
    assert out["resolved_identity_provisional_reason"] == "model_specific_needs_exact_firmware"


def test_runtime_identity_from_sample_sources_uses_live_runtime_identity_not_status_aliases():
    out = runtime_identity_from_sample_sources(
        base_status={
            "provider_name": "AttachedProvider",
            "provider_plmn": "12345",
            "router_model": "B818-263",
            "firmware": "11.0.1.1",
        },
        extra={
            "provider": "legacy-provider",
            "plmn": "00000",
            "runtime_identity": {
                "backend": "b818",
                "backend_family": "huawei_hilink_like",
                "vendor": "Huawei",
                "model": "B818-263",
                "firmware": "11.0.1.1",
                "provider_name": "LiveProvider",
                "provider_plmn": "54321",
                "identity_confidence": 0.81,
            },
        },
        runtime_health={"backend": "b818"},
        capabilities={
            "recognition": {"confidence": 0.81},
            "support": {"family": "huawei_hilink_like"},
        },
        runtime_dir="/tmp/runtime",
        memory_dir="/tmp/memory",
    )

    assert out["backend"] == "b818"
    assert out["backend_family"] == "huawei_hilink_like"
    assert out["provider_name"] == "LiveProvider"
    assert out["provider_plmn"] == "54321"
    assert out["model"] == "B818-263"
    assert out["identity_confidence"] == 0.81


def test_canonical_identity_display_parses_string_false_flags_as_false():
    out = build_canonical_identity_display(
        capabilities={
            "runtime_identity": {},
            "resolved_identity_card": {
                "exact_identity": "false",
                "provisional": "false",
                "firmware_revalidation_required": "false",
            },
            "onboarding_status": {
                "collect_evidence": "false",
                "calibration_refresh_required": "false",
                "evidence_sufficient": "false",
            },
        }
    )

    assert out["resolved_identity_exact"] is False
    assert out["resolved_identity_provisional"] is False
    assert out["firmware_revalidation_required"] is False
    assert out["collect_evidence"] is False
    assert out["calibration_refresh_required"] is False
    assert out["evidence_sufficient"] is False


def test_runtime_identity_from_sample_sources_keeps_resolved_card_as_metadata_only():
    out = runtime_identity_from_sample_sources(
        base_status={
            "provider_name": "StaleProvider",
            "provider_plmn": "00000",
            "router_model": "Fallback",
            "firmware": "legacy-fw",
            "model": "Legacy-100",
        },
        extra={
            "provider_name": "LegacyExtra",
            "provider_plmn": "99999",
            "model": "Legacy-200",
            "firmware": "legacy-extra-fw",
        },
        capabilities={
            "resolved_identity_card": {
                "backend": "b535",
                "family": "huawei_hilink_like",
                "vendor": "Huawei",
                "model": "B535-333",
                "firmware": "11.0.5.51(H470SP6C9831)",
                "provider_name": "A1",
                "provider_plmn": "22005",
                "profile_id": "huawei_b535_333_h470sp6c9831",
                "profile_provenance": "exact_firmware",
                "exact_identity": True,
                "provisional": False,
                "provisional_reason": "exact firmware match",
            },
            "support": {
                "family": "wrong_family",
            },
            "recognition": {
                "family_hint": "wrong_family_hint",
                "confidence": 0.91,
            },
        },
        backend_hint="b535_hint",
        backend_family_hint="hint_family",
        runtime_dir="/tmp/runtime",
        memory_dir="/tmp/memory",
        instance_id="instance-1",
        profile_name="bootstrap",
    )

    assert out["backend"] == "b535_hint"
    assert out["backend_family"] == "hint_family"
    assert out["vendor"] == ""
    assert out["model"] == ""
    assert out["firmware"] == ""
    assert out["provider_name"] == ""
    assert out["provider_plmn"] == ""
    assert out["identity_confidence"] == 0.4
    assert out["resolved_profile_id"] == "huawei_b535_333_h470sp6c9831"
    assert out["resolved_profile_provenance"] == "exact_firmware"
    assert out["resolved_identity_exact"] is True
    assert out["resolved_identity_provisional"] is False
    assert out["resolved_identity_provisional_reason"] == "exact firmware match"


def test_runtime_identity_from_capabilities_payload_prefers_canonical_runtime_identity_over_legacy_nested_identity():
    payload = {
        "backend": "b818",
        "runtime_identity": {
            "backend": "b818",
            "backend_family": "huawei_hilink_like",
            "vendor": "Huawei",
            "model": "B818-263",
            "firmware": "11.0.1.1(H220SP1C11)",
            "provider_name": "A1",
            "provider_plmn": "22005",
        },
        "identity": {
            "vendor": "WrongVendor",
            "model": "WrongModel",
            "firmware": "wrong-fw",
            "provider": "WrongProvider",
            "plmn": "99999",
        },
    }

    out = runtime_identity_from_capabilities_payload(payload)

    assert out["model"] == "B818-263"
    assert out["firmware"] == "11.0.1.1(H220SP1C11)"
    assert out["provider_name"] == "A1"
    assert out["provider_plmn"] == "22005"


def test_runtime_identity_rejects_placeholder_provider_name_without_capability_fallback():
    out = runtime_identity_from_sample_sources(
        extra={
            "runtime_identity": {
                "backend": "b818",
                "provider_name": "-",
                "provider_plmn": "22005",
            }
        },
        capabilities={
            "runtime_identity": {
                "provider_name": "A1",
                "provider_plmn": "22005",
            }
        },
        backend_hint="b818",
    )

    assert out["provider_name"] == ""
    assert out["provider_plmn"] == "22005"


def test_runtime_identity_from_capabilities_payload_requires_canonical_runtime_identity_owner():
    payload = {
        "backend": "b818",
        "vendor": "WrongVendor",
        "model": "WrongModel",
        "firmware": "wrong-fw",
        "provider_name": "WrongProvider",
        "provider_plmn": "99999",
        "identity": {
            "vendor": "NestedWrongVendor",
            "model": "NestedWrongModel",
            "firmware": "nested-wrong-fw",
            "provider_name": "NestedWrongProvider",
            "provider_plmn": "88888",
            "serial": "WRONG-SERIAL",
            "imei": "WRONG-IMEI",
        },
    }

    out = runtime_identity_from_capabilities_payload(payload)

    assert out["backend"] == "b818"
    assert out["vendor"] == ""
    assert out["model"] == ""
    assert out["firmware"] == ""
    assert out["provider_name"] == ""
    assert out["provider_plmn"] == ""
    assert out.get("serial") is None
    assert out.get("imei") is None


def test_runtime_identity_from_capabilities_payload_promotes_connection_access_only_from_identity():
    payload = {
        "backend": "b818",
        "identity": {
            "iface": "http_xml_webui",
            "target": "192.168.10.1",
            "model": "WrongModel",
            "firmware": "wrong-fw",
            "serial": "WRONG-SERIAL",
            "provider_name": "WrongProvider",
            "provider_plmn": "99999",
        },
    }

    out = runtime_identity_from_capabilities_payload(payload)

    assert out["iface"] == "http_xml_webui"
    assert out["target"] == "192.168.10.1"
    assert out["model"] == ""
    assert out["firmware"] == ""
    assert out.get("serial") is None
    assert out["provider_name"] == ""
    assert out["provider_plmn"] == ""


def test_runtime_identity_from_capabilities_payload_ignores_boolean_plmn_support_flag():
    payload = {
        "backend": "b818",
        "provider_name": "A1",
        "plmn": True,
        "runtime_identity": {
            "backend": "b818",
            "backend_family": "huawei_hilink_like",
            "vendor": "Huawei",
            "model": "B818-263",
            "firmware": "11.0.1.1(H220SP1C11)",
            "provider_name": "A1",
        },
        "identity": {
            "plmn": "22005",
        },
    }

    out = runtime_identity_from_capabilities_payload(payload)

    assert out["provider_name"] == "A1"
    assert out["provider_plmn"] == ""


def test_runtime_identity_from_sample_sources_promotes_connection_access_only_from_capability_identity():
    out = runtime_identity_from_sample_sources(
        capabilities={
            "identity": {
                "iface": "http_xml_webui",
                "target": "192.168.10.1",
                "model": "WrongModel",
                "firmware": "wrong-fw",
                "serial": "WRONG-SERIAL",
                "provider_name": "WrongProvider",
                "provider_plmn": "99999",
            }
        },
        backend_hint="b818",
    )

    assert out["iface"] == "http_xml_webui"
    assert out["target"] == "192.168.10.1"
    assert out["model"] == ""
    assert out["firmware"] == ""
    assert out.get("serial") is None
    assert out["provider_name"] == ""
    assert out["provider_plmn"] == ""


def test_runtime_identity_from_sample_sources_copies_modem_live_access_type_contract():
    out = runtime_identity_from_sample_sources(
        extra={
            "runtime_identity": {
                "backend": "b818",
                "backend_family": "huawei_hilink_like",
                "model": "B818-263",
                "firmware": "11.0.1.1(H220SP1C11)",
                "iface": "unknown",
            }
        },
        capabilities={
            "access_type_contract": {
                "schema": "AccessType.v1",
                "owner": "arch4.modem_live.compatibility_identity",
                "display_name": "huawei_http_single_lane",
            }
        },
        backend_hint="b818",
    )

    assert out["iface"] == "unknown"
    assert out["access_type"] == "huawei_http_single_lane"




def test_runtime_identity_from_sample_sources_uses_live_runtime_identity_authority():
    out = runtime_identity_from_sample_sources(
        base_status={
            "provider_name": "StaleProvider",
            "provider_plmn": "00000",
            "router_model": "Legacy-100",
            "firmware": "legacy-fw",
        },
        extra={
            "runtime_identity": {
                "backend": "b818",
                "backend_family": "huawei_hilink_like",
                "vendor": "Huawei",
                "model": "B818-263",
                "firmware": "11.0.1.1(H220SP1C11)",
                "provider_name": "A1",
                "provider_plmn": "22005",
                "serial": "SER123",
                "imei": "IMEI123",
                "imsi": "IMSI123",
            },
        },
        capabilities={
            "resolved_identity_card": {
                "profile_id": "huawei_b818_263_h220sp1c11",
                "profile_provenance": "exact_firmware",
                "exact_identity": True,
                "provisional": False,
            },
        },
        runtime_dir="/tmp/runtime",
        memory_dir="/tmp/memory",
        instance_id="instance-1",
        profile_name="bootstrap",
    )

    assert out["backend"] == "b818"
    assert out["backend_family"] == "huawei_hilink_like"
    assert out["model"] == "B818-263"
    assert out["firmware"] == "11.0.1.1(H220SP1C11)"
    assert out["provider_name"] == "A1"
    assert out["provider_plmn"] == "22005"
    assert out["serial"] == "SER123"
    assert out["imei"] == "IMEI123"
    assert out["imsi"] == "IMSI123"
    assert out["runtime_owner"]["instance_id"] == "instance-1"
    assert out["resolved_profile_id"] == "huawei_b818_263_h220sp1c11"


def test_runtime_identity_from_sample_sources_does_not_use_status_or_capability_plmn_aliases():
    out = runtime_identity_from_sample_sources(
        base_status={
            "provider_name": "A1",
            "provider_plmn": "22005",
            "router_model": "B818-263",
            "firmware": "11.0.1.1(H220SP1C11)",
        },
        capabilities={
            "plmn": True,
            "runtime_identity": {
                "backend": "b818",
                "backend_family": "huawei_hilink_like",
                "provider_name": "A1",
            },
        },
        runtime_dir="/tmp/runtime",
        memory_dir="/tmp/memory",
    )

    assert out["provider_plmn"] == ""


def test_runtime_identity_from_sample_sources_parses_string_false_resolved_flags_as_false():
    out = runtime_identity_from_sample_sources(
        extra={
            "runtime_identity": {
                "backend": "b818",
                "backend_family": "huawei_hilink_like",
                "model": "B818-263",
            },
            "resolved_identity_card": {
                "profile_id": "huawei_b818_263_h220sp1c11",
                "profile_provenance": "exact_firmware",
                "exact_identity": "false",
                "provisional": "false",
            },
        },
        runtime_dir="/tmp/runtime",
        memory_dir="/tmp/memory",
    )

    assert out["resolved_profile_id"] == "huawei_b818_263_h220sp1c11"
    assert out["resolved_identity_exact"] is False
    assert out["resolved_identity_provisional"] is False


def test_canonical_identity_display_does_not_promote_provider_aliases():
    out = build_canonical_identity_display(
        base_status={
            "provider": "WrongProvider",
            "operator": "WrongOperator",
            "plmn": "99999",
            "plmn_name": "WrongPlmn",
        },
        extra={
            "_canonical_status": {
                "provider": "WrongCanonicalProvider",
                "plmn": "88888",
                "plmn_id": "77777",
            }
        },
        capabilities={
            "provider": "WrongCapabilityProvider",
            "plmn": "66666",
        },
    )

    assert out["provider_name"] == ""
    assert out["provider_plmn"] == ""


def test_runtime_session_scope_change_ignores_profile_model_enrichment():
    previous = {
        "backend": "mikrotik",
        "backend_family": "routeros_at_like",
        "model": "mikrotik",
        "target": "192.168.88.1",
    }
    current = {
        "backend": "mikrotik",
        "backend_family": "routeros_at_like",
        "model": "D53G-5HacD2HnD&EG120K-EA",
        "target": "192.168.88.1",
        "serial": "MT123",
    }

    assert runtime_session_scope_changed(previous, current) is False

    merged = merge_runtime_session_scope(previous, current)
    assert merged["model"] == "d53g-5hacd2hnd&eg120k-ea"
    assert merged["serial"] == "MT123"


def test_canonical_session_scope_key_round_trips_and_classifies_provider_enrichment():
    provisional = {
        "backend": "b818",
        "backend_family": "huawei_hilink_like",
        "model": "B818-263",
        "firmware": "11.0.1.1(H220SP1C11)",
        "serial": "VNNDW21224003358",
        "imei": "867193048169697",
        "imsi": "220052782517944",
    }
    confirmed = {
        **provisional,
        "provider_plmn": "22005",
        "provider_name": "A1",
    }
    provisional_key = runtime_session_scope_key(provisional)
    confirmed_key = runtime_session_scope_key(confirmed)

    assert parse_runtime_session_scope_key(provisional_key) == build_runtime_session_scope(
        provisional
    )
    assert parse_runtime_session_scope_key(confirmed_key) == build_runtime_session_scope(
        confirmed
    )
    assert runtime_session_scope_is_strict_enrichment(provisional, confirmed) is True
    assert runtime_session_scope_is_strict_enrichment(confirmed, provisional) is False
    assert runtime_session_scope_is_strict_enrichment(
        confirmed,
        {**confirmed, "imsi": "220051111111111"},
    ) is False


def test_session_scope_key_parser_rejects_noncanonical_or_duplicate_components():
    import pytest

    with pytest.raises(ValueError, match="runtime_session_scope_key"):
        parse_runtime_session_scope_key("session-a")
    with pytest.raises(ValueError, match="runtime_session_scope_key"):
        parse_runtime_session_scope_key("backend=b818|backend=b535")


def test_runtime_session_scope_filters_placeholder_values():
    scope = build_runtime_session_scope(
        {
            "runtime_session_scope": {
                "target": "-",
                "backend": "unknown",
                "backend_family": "routeros_at_like",
                "model": "MikroTik",
                "provider_name": "---",
                "provider_plmn": "unknown",
                "serial": "MT123",
            }
        }
    )

    assert "target" not in scope
    assert "backend" not in scope
    assert "provider_name" not in scope
    assert "provider_plmn" not in scope
    assert scope["backend_family"] == "routeros_at_like"
    assert scope["serial"] == "MT123"


def test_runtime_session_scope_ignores_legacy_identity_and_provider_aliases():
    scope = build_runtime_session_scope(
        {
            "identity": {
                "target": "192.168.8.1",
                "model": "WrongModel",
                "provider": "WrongProvider",
                "plmn": "99999",
                "serial": "WRONG-SERIAL",
            },
            "provider": "WrongTopProvider",
            "plmn": "88888",
            "runtime_identity": {
                "backend": "mikrotik",
                "backend_family": "routeros_at_like",
                "model": "D53G-5HacD2HnD&EG120K-EA",
                "provider_name": "A1",
                "provider_plmn": "22005",
                "serial": "MT123",
            },
        }
    )

    assert scope["backend"] == "mikrotik"
    assert scope["model"] == "d53g-5hacd2hnd&eg120k-ea"
    assert scope["provider_name"] == "a1"
    assert scope["provider_plmn"] == "22005"
    assert scope["serial"] == "MT123"
    assert scope.get("target") is None


def test_runtime_session_scope_merge_replaces_placeholder_with_real_value():
    previous = {
        "runtime_session_scope": {
            "target": "-",
            "backend": "unknown",
            "backend_family": "routeros_at_like",
            "model": "mikrotik",
        }
    }
    current = {
        "target": "192.168.88.1",
        "backend": "mikrotik",
        "backend_family": "routeros_at_like",
        "model": "D53G-5HacD2HnD&EG120K-EA",
    }

    merged = merge_runtime_session_scope(previous, current)

    assert merged["target"] == "192.168.88.1"
    assert merged["backend"] == "mikrotik"
    assert merged["model"] == "d53g-5hacd2hnd&eg120k-ea"


def test_runtime_session_scope_change_detects_authoritative_identity_change():
    previous = {
        "backend": "mikrotik",
        "backend_family": "routeros_at_like",
        "model": "D53G-5HacD2HnD&EG120K-EA",
        "target": "192.168.88.1",
        "serial": "MT123",
    }
    current = {
        "backend": "mikrotik",
        "backend_family": "routeros_at_like",
        "model": "D53G-5HacD2HnD&EG120K-EA",
        "target": "192.168.88.1",
        "serial": "MT999",
    }

    assert runtime_session_scope_changed(previous, current) is True


def test_runtime_session_scope_change_ignores_routeros_modem_model_enrichment():
    previous = {
        "backend": "mikrotik",
        "backend_family": "routeros_at_like",
        "model": "D53G-5HacD2HnD&EG120K-EA",
        "target": "192.168.88.1",
    }
    current = {
        "backend": "mikrotik",
        "backend_family": "routeros_at_like",
        "model": "S53UG+5HaxD2HaxD-TC&RG650E-EU",
        "target": "192.168.88.1",
    }

    assert runtime_session_scope_changed(previous, current) is False

    merged = merge_runtime_session_scope(previous, current)
    assert merged["model"] == "s53ug+5haxd2haxd-tc&rg650e-eu"


def test_runtime_session_scope_change_ignores_routeros_late_modem_identity_enrichment():
    previous = {
        "backend": "mikrotik",
        "backend_family": "routeros_at_like",
        "model": "D53G-5HacD2HnD&EG120K-EA",
        "target": "192.168.88.1",
        "serial": "MT123",
        "provider_plmn": "22005",
        "imei": "OLD-IMEI",
    }
    current = {
        "backend": "mikrotik",
        "backend_family": "routeros_at_like",
        "model": "D53G-5HacD2HnD&EG120K-EA",
        "target": "192.168.88.1",
        "serial": "MT123",
        "provider_plmn": "22001",
        "imei": "NEW-IMEI",
        "imsi": "220010123456789",
        "sim_id": "8983812345678901234",
    }

    assert runtime_session_scope_changed(previous, current) is False


def test_runtime_session_scope_merge_preserves_routeros_provider_during_sparse_read():
    previous = {
        "backend": "mikrotik",
        "backend_family": "routeros_at_like",
        "model": "D53G-5HacD2HnD&EG120K-EA",
        "target": "192.168.88.1",
        "serial": "MT123",
        "provider_name": "A1",
        "provider_plmn": "22005",
    }
    current = {
        "backend": "mikrotik",
        "backend_family": "routeros_at_like",
        "model": "D53G-5HacD2HnD&EG120K-EA",
        "target": "192.168.88.1",
        "serial": "MT123",
    }

    assert runtime_session_scope_changed(previous, current) is False

    merged = merge_runtime_session_scope(previous, current)
    assert merged["provider_name"] == "a1"
    assert merged["provider_plmn"] == "22005"


def test_runtime_session_scope_change_detects_non_routeros_specific_model_change_without_strong_ids():
    previous = {
        "backend": "b818",
        "backend_family": "huawei_hilink_like",
        "model": "B818-263",
        "target": "192.168.8.1",
    }
    current = {
        "backend": "b818",
        "backend_family": "huawei_hilink_like",
        "model": "B535-333",
        "target": "192.168.8.1",
    }

    assert runtime_session_scope_changed(previous, current) is True


def test_runtime_session_scope_merge_does_not_replace_specific_model_with_generic_later_tick():
    previous = {
        "backend": "mikrotik",
        "backend_family": "routeros_at_like",
        "model": "D53G-5HacD2HnD&EG120K-EA",
        "target": "192.168.88.1",
        "serial": "MT123",
    }
    current = {
        "backend": "mikrotik",
        "backend_family": "routeros_at_like",
        "model": "mikrotik",
        "target": "192.168.88.1",
    }

    assert runtime_session_scope_changed(previous, current) is False

    merged = merge_runtime_session_scope(previous, current)
    assert merged["model"] == "d53g-5hacd2hnd&eg120k-ea"
    assert merged["serial"] == "MT123"
