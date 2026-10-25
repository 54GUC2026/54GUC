"""Reviewed seed data for the Modem Live device-identity Matrix.

Every value here is reviewed. Modem Live may add a complete connected-device
tuple with a stable code. No other subsystem may allocate or publish one.
Changes require a new catalog generation, the previous checksum, and evidence.
"""

from __future__ import annotations

from dataclasses import replace

from arch4.modem.live_lane_contract import (
    BETA_MODEM_LIVE_ACCESS_PROFILES,
    FAMILY_LIVE_ACCESS_PROFILE_DEFAULTS,
)
from arch4.modem_live.compatibility_identity import (
    ReviewedAccessTypeEntry,
    ReviewedIdentityEntry,
    ReviewedIdentityRegistry,
    ReviewedLegacyIdentityMigration,
)


REVIEW_DOCUMENT = (
    "docs/CGF_MUST_READ/MODEM_LIVE_COMPATIBILITY_IDENTITY_REGISTRY_2026-07-15.md"
)
# These identifiers are catalog data, not documentation links. Moving the
# readable documents must not change an existing catalog generation.
GENERATION_1_REVIEW_DOCUMENT = (
    "docs/MUST_READ/MODEM_LIVE_COMPATIBILITY_IDENTITY_REGISTRY_2026-07-15.md"
)
GENERATION_2_REVIEW_DOCUMENT = (
    "docs/MUST_READ/54GUC_NO_HARDWARE_ARCHITECTURE_COMPLETION_PROGRAM_2026-07-16.md"
)
GENERATION_3_REVIEW_DOCUMENT = (
    "docs/CGF_MUST_READ/CGF_LEAST_COST_ARCHITECTURE_CLOSURE_PLAN_2026-07-31.md"
)
GENERATION_4_REVIEW_DOCUMENT = (
    "docs/CGF_MUST_READ/"
    "CGF_PROFILE_BOUND_HILINK_DIALECT_AND_B535333_EVIDENCE_PLAN_2026-08-04.md"
)
GENERATION_5_REVIEW_DOCUMENT = (
    "docs/CGF_MUST_READ/"
    "CGF_OPENWRT_DUAL_VIRTUAL_APPLIANCE_REPLACEMENT_2026-09-06.md"
)
GENERATION_6_REVIEW_DOCUMENT = "docs/CGF_MUST_READ/CGF_NX520V_PHASE_I_2026-10-04.md"
GENERATION_7_REVIEW_DOCUMENT = "docs/CGF_MUST_READ/CGF_XIAOMI_CONDITIONAL_ACCESS_2026-10-06.md"
GENERATION_8_REVIEW_DOCUMENT = "docs/CGF_MUST_READ/CGF_R20_SIX_ROUTER_SCOPE_COMPLETION_2026-10-07.md"
GENERATION_9_REVIEW_DOCUMENT = "docs/CGF_MUST_READ/CGF_NX520_SRBA1_250902_NATIVE_CONTRACT_2026-10-09.md"
GENERATION_10_REVIEW_DOCUMENT = "docs/CGF_MUST_READ/CGF_R205_CANONICAL_EXECUTION_PLAN_2026-10-10.md"
GENERATION_11_REVIEW_DOCUMENT = "docs/CGF_MUST_READ/CGF_R207_VALIDATION_PLAN_2026-10-11.md"
PRODUCTION_CATALOG_V11_CHECKSUM = "0a226b5871fdd8efa94efbb207eb63fdf0c1c49c651af39b2814e9ba421e50a4"
PRODUCTION_CATALOG_V10_CHECKSUM = "dd5addec7f0b93e7d2b7629cd1e89ef2b7e1c06798d6bad9a0175c3083a3e303"
PRODUCTION_CATALOG_V9_CHECKSUM = "71ad5bca9217425a13aed724d709dba44b5c1a9f7b8d3702214804f853007464"
PRODUCTION_CATALOG_V8_CHECKSUM = "7e2f07791b574cac93ccd4464e5bc6ec6ac9e9f60e5321534e56dc65c1057a81"
PRODUCTION_CATALOG_V7_CHECKSUM = "f5e79fb48ebc87d9fce6b326785f46231f75a691c1baa439a1c5fe452a11b644"
PRODUCTION_CATALOG_V6_CHECKSUM = "2e1472bdc89bd005cdac924d47ae25f293ccdb42e0a0d2f6f63ef2fd6e5906b0"
GENERATION_1_CHECKSUM = (
    "9ed6c8632c77858471c6353911d51f1b9d4d91e8e8f50c3e563aa74fe9e00053"
)
PRODUCTION_CATALOG_V2_CHECKSUM = (
    "db593b5ab0ae06ba48e527cf97de9ff1992a872b35d3a52ef6d47d2e8cc34fe4"
)
PRODUCTION_CATALOG_V3_CHECKSUM = (
    "ff88d71e21725a9458d9a0821cbcd83e0afac741bf30ed4ba80ad709d515dd75"
)
PRODUCTION_CATALOG_V4_CHECKSUM = (
    "a5dabac97ae48d93d7e726c198df346529d4837da669a57831062fabf07cff8f"
)
PRODUCTION_CATALOG_V5_CHECKSUM = (
    "b0a2bf142c3334ce728e2a25b26e1b877870f4bbf5fdd6d9db1d2f3dcc20c4bc"
)


def _selectable_access_profiles() -> dict[str, object]:
    selected: dict[str, object] = {}
    for profile in (
        *BETA_MODEM_LIVE_ACCESS_PROFILES.values(),
        *FAMILY_LIVE_ACCESS_PROFILE_DEFAULTS.values(),
    ):
        profile_id = str(getattr(profile, "profile_id", "") or "").strip()
        if not profile_id:
            raise RuntimeError("Modem Live access profile_id is required")
        previous = selected.setdefault(profile_id, profile)
        if previous != profile:
            raise RuntimeError(f"conflicting Modem Live access profile:{profile_id}")
    return selected


_SELECTABLE_ACCESS_PROFILES = _selectable_access_profiles()
_UNKNOWN_PROFILE_ID = "unknown_probe_only_access_v1"

def _reviewed_profile(profile_id: str):
    profile = _SELECTABLE_ACCESS_PROFILES.get(profile_id)
    if profile is None:
        raise RuntimeError(f"reviewed Modem Live access profile is missing:{profile_id}")
    return profile


def _reviewed_access_type(
    *,
    access_type_id: str,
    profile_id: str,
    display_name: str,
    review_reference: str = "",
) -> ReviewedAccessTypeEntry:
    profile = _reviewed_profile(profile_id)
    return ReviewedAccessTypeEntry.reviewed(
        access_type_id=access_type_id,
        classifier_key=profile_id,
        display_name=display_name,
        semantic_vector={
            "profile_id": profile_id,
            "backend": str(profile.backend),
            "backend_family": str(profile.backend_family),
            "access_contract": str(profile.access_type),
            "discovery_mode": str(profile.discovery_mode),
            "session_refresh_method": str(profile.session_refresh_method),
            "direct_modem_writes_allowed": bool(profile.direct_modem_writes_allowed),
            "readback_required": bool(profile.readback_required),
            "effective_match_required": bool(profile.effective_match_required),
        },
        review_reference=review_reference or f"{GENERATION_2_REVIEW_DOCUMENT}#s1-execution-inventory-and-rca-code-state-at-7e514982",
    )

B818_ACCESS_TYPE = ReviewedAccessTypeEntry.reviewed(
    access_type_id="0x25A9C4F638548871",
    classifier_key="b818_hilink_access_v1",
    display_name="Huawei B818 HiLink HTTP/XML",
    semantic_vector={
        "transport_encoding": "hilink_http_xml",
        "authentication_session": "ses_tok_info_state_login",
        "serialization_locking": "single_modem_io_lane",
        "read_vocabulary": "b818_hilink_access_v1",
        "write_vocabulary": "b818_hilink_access_v1",
        "response_parser_units": "b818_hilink_xml_v1",
        "retry_timeout_semantics": "b818_access_profile_v1",
        "mutation_acknowledgement": "xml_ok_plus_effective_readback",
        "target_confirmation": "required",
        "error_semantics": "backend_feature_result_v1",
    },
    review_reference=f"{GENERATION_1_REVIEW_DOCUMENT}#b818-263-h220sp1c11",
)

B535_232_ACCESS_TYPE = ReviewedAccessTypeEntry.reviewed(
    access_type_id="0x084D210F8D9B4036",
    classifier_key="b535_232_hilink_access_v1",
    display_name="Huawei B535-232 HiLink HTTP/XML",
    semantic_vector={
        "transport_encoding": "hilink_http_xml",
        "authentication_session": "ses_tok_info_state_login",
        "serialization_locking": "single_modem_io_lane",
        "read_vocabulary": "b535_232_hilink_access_v1",
        "write_vocabulary": "b535_232_hilink_access_v1",
        "response_parser_units": "b535_232_hilink_xml_v1",
        "retry_timeout_semantics": "b535_232_access_profile_v1",
        "mutation_acknowledgement": "xml_ok_plus_effective_readback",
        "target_confirmation": "required",
        "error_semantics": "backend_feature_result_v1",
    },
    review_reference=f"{GENERATION_1_REVIEW_DOCUMENT}#b535-232-h191sp64c983",
)

B535_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0xC4188B886C756A9C",
    profile_id="b535_hilink_access_v1",
    display_name="Huawei B535 HiLink access profile",
)
B311_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0xC96FF3D27920F248",
    profile_id="b311_hilink_access_v1",
    display_name="Huawei B311 HiLink access profile",
)
B530_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0x55F795D86E450F65",
    profile_id="b530_hilink_access_v1",
    display_name="Huawei B530 HiLink access profile",
)
H112_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0x465B9B5B932E93DE",
    profile_id="h112_hilink_access_v1",
    display_name="Huawei H112 HiLink access profile",
)
H122_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0xB41316E073FE9F15",
    profile_id="h122_hilink_access_v1",
    display_name="Huawei H122 HiLink access profile",
)
H155_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0xAC2C4A3CEA89283F",
    profile_id="h155_hilink_access_v1",
    display_name="Huawei H155 HiLink access profile",
)
MIKROTIK_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0x554B700F610798C1",
    profile_id="mikrotik_routeros_at_access_v1",
    display_name="MikroTik RouterOS plus AT access profile",
)
ROUTEROS_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0x3ABE04DDAC24D514",
    profile_id="routeros_at_access_v1",
    display_name="RouterOS plus AT access profile",
)
ZTE_MF286R_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0xF687946402FD49E8",
    profile_id="zte_mf286r_goform_access_v1",
    display_name="ZTE MF286R goform access profile",
)
_MF296R_ACCESS_PROFILE = _reviewed_profile("zte_mf296r_goform_access_v1")
ZTE_MF296R_ACCESS_TYPE = ReviewedAccessTypeEntry.reviewed(
    access_type_id="0xC1A025528C5F63CD",
    classifier_key="zte_mf296r_goform_access_v1",
    display_name="ZTE MF296R native Goform access profile",
    semantic_vector={
        "profile_id": "zte_mf296r_goform_access_v1",
        "backend": str(_MF296R_ACCESS_PROFILE.backend),
        "backend_family": str(_MF296R_ACCESS_PROFILE.backend_family),
        "access_contract": str(_MF296R_ACCESS_PROFILE.access_type),
        "discovery_mode": str(_MF296R_ACCESS_PROFILE.discovery_mode),
        "session_refresh_method": str(_MF296R_ACCESS_PROFILE.session_refresh_method),
        "http_connection_mode": str(_MF296R_ACCESS_PROFILE.http_connection_mode),
        "direct_modem_writes_allowed": bool(
            _MF296R_ACCESS_PROFILE.direct_modem_writes_allowed
        ),
        "readback_required": bool(_MF296R_ACCESS_PROFILE.readback_required),
        "effective_match_required": bool(_MF296R_ACCESS_PROFILE.effective_match_required),
    },
    review_reference=(
        f"{GENERATION_3_REVIEW_DOCUMENT}#mf296r-exact-webuisession-owner-correction-2026-08-02"
    ),
)
ZTE_MC801A_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0xD213D3FED98397E0",
    profile_id="zte_mc801a_goform_access_v1",
    display_name="ZTE MC801A goform access profile",
)
ZTE_MC888_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0xCE128ECC8A8AE60E",
    profile_id="zte_mc888_goform_access_v1",
    display_name="ZTE MC888 goform access profile",
)
HUAWEI_FAMILY_PROBE_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0x1866264E3DAA978D",
    profile_id="huawei_hilink_family_runtime_probe_access_v1",
    display_name="Huawei HiLink family runtime-probe access profile",
)
ROUTEROS_FAMILY_PROBE_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0xD259736AC87658D3",
    profile_id="routeros_at_family_runtime_probe_access_v1",
    display_name="RouterOS AT family runtime-probe access profile",
)
ZTE_GOFORM_FAMILY_PROBE_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0x680E6B1E6F24FDCD",
    profile_id="zte_goform_family_runtime_probe_access_v1",
    display_name="ZTE goform family runtime-probe access profile",
)
ZTE_ZWRT_FAMILY_PROBE_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0x48086217B11A5585",
    profile_id="zte_zwrt_ubus_family_runtime_probe_access_v1",
    display_name="ZTE ZWRT UBUS family runtime-probe access profile",
)
# Catalog generations 2-4 recorded the former OpenWrt access claim. Keep that
# historical generation immutable while generation 5 replaces the live claim
# with the reviewed remote, read-only rpcd boundary.
_OPENWRT_FAMILY_PROBE_ACCESS_TYPE_V4 = ReviewedAccessTypeEntry.reviewed(
    access_type_id="0xEE206338A5153D64",
    classifier_key="openwrt_modemmanager_family_runtime_probe_access_v1",
    display_name="OpenWrt ModemManager family runtime-probe access profile",
    semantic_vector={
        "profile_id": "openwrt_modemmanager_family_runtime_probe_access_v1",
        "backend": "openwrt_modemmanager_like",
        "backend_family": "openwrt_modemmanager_like",
        "access_contract": "openwrt_modemmanager_single_lane",
        "discovery_mode": "family_runtime_probe",
        "session_refresh_method": "modemmanager_session",
        "direct_modem_writes_allowed": True,
        "readback_required": True,
        "effective_match_required": True,
    },
    review_reference=(
        f"{GENERATION_2_REVIEW_DOCUMENT}"
        "#s1-execution-inventory-and-rca-code-state-at-7e514982"
    ),
)
OPENWRT_FAMILY_PROBE_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0xA1A4396B6D4CB5F2",
    profile_id="openwrt_modemmanager_rpcd_read_only_access_v1",
    display_name="OpenWrt ModemManager rpcd read-only access profile",
)
ZYXEL_FAMILY_PROBE_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0x4DE4D5C5098DEF93",
    profile_id="zyxel_webui_family_runtime_probe_access_v1",
    display_name="Zyxel WebUI family runtime-probe access profile",
)

_REVIEWED_ACCESS_TYPES = (
    B818_ACCESS_TYPE,
    B535_232_ACCESS_TYPE,
    B535_ACCESS_TYPE,
    B311_ACCESS_TYPE,
    B530_ACCESS_TYPE,
    H112_ACCESS_TYPE,
    H122_ACCESS_TYPE,
    H155_ACCESS_TYPE,
    MIKROTIK_ACCESS_TYPE,
    ROUTEROS_ACCESS_TYPE,
    ZTE_MF286R_ACCESS_TYPE,
    ZTE_MF296R_ACCESS_TYPE,
    ZTE_MC801A_ACCESS_TYPE,
    ZTE_MC888_ACCESS_TYPE,
    HUAWEI_FAMILY_PROBE_ACCESS_TYPE,
    ROUTEROS_FAMILY_PROBE_ACCESS_TYPE,
    ZTE_GOFORM_FAMILY_PROBE_ACCESS_TYPE,
    ZTE_ZWRT_FAMILY_PROBE_ACCESS_TYPE,
    _OPENWRT_FAMILY_PROBE_ACCESS_TYPE_V4,
    OPENWRT_FAMILY_PROBE_ACCESS_TYPE,
    ZYXEL_FAMILY_PROBE_ACCESS_TYPE,
)

_REVIEWED_ACCESS_TYPES_V3 = tuple(
    row
    for row in _REVIEWED_ACCESS_TYPES
    if row is not OPENWRT_FAMILY_PROBE_ACCESS_TYPE
)
TPLINK_NX520V_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0x7A263F61C04E95B2",
    profile_id="nx520v_eu1_260408_dev2_access_v1",
    display_name="TP-Link NX520v EU1 260408 encrypted DEV2 conditional access",
    review_reference=f"{GENERATION_6_REVIEW_DOCUMENT}#only-legal-owners",
)
_REVIEWED_ACCESS_TYPES_V6 = _REVIEWED_ACCESS_TYPES + (TPLINK_NX520V_ACCESS_TYPE,)
XIAOMI_CB0401V2_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0x92F36DE128B5C407", profile_id="xiaomi_cb0401v2_3_0_88_stock_access_v1",
    display_name="Xiaomi CB0401V2 3.0.88 conditional stock LuCI access",
    review_reference=f"{GENERATION_7_REVIEW_DOCUMENT}#only-legal-owners",
)
_REVIEWED_ACCESS_TYPES_V7 = _REVIEWED_ACCESS_TYPES_V6 + (XIAOMI_CB0401V2_ACCESS_TYPE,)
TPLINK_NX520_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0x41A283D5E7B6C902", profile_id="nx520_srba1_20250915_dev2_access_v1",
    display_name="TP-Link NX520 SRBA1 20250915 explicit DEV2 conditional access",
    review_reference=f"{GENERATION_8_REVIEW_DOCUMENT}#unreleased-catalog-consolidation",
)
H173_C983_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0xB156738CD429A0E2", profile_id="h173_c983_scram_hilink_access_v1",
    display_name="H173 C983 selected SCRAM conditional HiLink access",
    review_reference=f"{GENERATION_8_REVIEW_DOCUMENT}#unreleased-catalog-consolidation",
)
G5B_B03_ACCESS_TYPE = _reviewed_access_type(
    access_type_id="0x5D083C6742BEA901", profile_id="g5b_b03_md5_goform_access_v1",
    display_name="G5B B03 selected MD5 conditional GoForm access",
    review_reference=f"{GENERATION_8_REVIEW_DOCUMENT}#unreleased-catalog-consolidation",
)
_REVIEWED_ACCESS_TYPES_V8 = _REVIEWED_ACCESS_TYPES_V7 + (TPLINK_NX520_ACCESS_TYPE, H173_C983_ACCESS_TYPE, G5B_B03_ACCESS_TYPE)

_NX520_250902_PROFILE = _reviewed_profile("nx520_srba1_250902_dev2_access_v2")
TPLINK_NX520_250902_ACCESS_TYPE = ReviewedAccessTypeEntry.reviewed(
    access_type_id="0x60F83C737A510519",
    classifier_key="nx520_srba1_250902_dev2_access_v1",
    display_name="TP-Link NX520 Build 250902 encrypted DEV2 access",
    semantic_vector={**TPLINK_NX520_ACCESS_TYPE.semantic_vector,
        "profile_id":"nx520_srba1_250902_dev2_access_v1",
        "fast_live_interval_s":1.0,
        "dev2_state_read_gap_s":_NX520_250902_PROFILE.dev2_state_read_gap_s,
        "min_inter_request_gap_s":_NX520_250902_PROFILE.min_inter_request_gap_s},
    review_reference=f"{GENERATION_10_REVIEW_DOCUMENT}#f13-exact-nx520-read-pacing-replacement-inventory",
)

# Generation10 is retained only as immutable decode evidence. Runtime selects v2.
TPLINK_NX520_250902_ACCESS_TYPE_V2 = ReviewedAccessTypeEntry.reviewed(
    access_type_id="0xC82319D6A5F047B2",
    classifier_key=_NX520_250902_PROFILE.profile_id,
    display_name="TP-Link NX520 Build 250902 encrypted DEV2 two-second access",
    semantic_vector={**TPLINK_NX520_250902_ACCESS_TYPE.semantic_vector,
        "profile_id":_NX520_250902_PROFILE.profile_id,
        "fast_live_interval_s":_NX520_250902_PROFILE.fast_live_interval_s},
    review_reference=f"{GENERATION_11_REVIEW_DOCUMENT}#before-code-inventory",
)

_expected_reviewed_profiles = set(_SELECTABLE_ACCESS_PROFILES).difference({_UNKNOWN_PROFILE_ID})
_actual_reviewed_profiles = {row.classifier_key for row in (*_REVIEWED_ACCESS_TYPES_V8,TPLINK_NX520_250902_ACCESS_TYPE,TPLINK_NX520_250902_ACCESS_TYPE_V2)}
_retired_reviewed_profiles = {
    "nx520_srba1_250902_dev2_access_v1",
    "openwrt_modemmanager_family_runtime_probe_access_v1",
}
if (
    _actual_reviewed_profiles - _expected_reviewed_profiles
    != _retired_reviewed_profiles
    or _expected_reviewed_profiles - _actual_reviewed_profiles
):
    raise RuntimeError(
        "reviewed Access Type population mismatch:"
        f"missing={sorted(_expected_reviewed_profiles - _actual_reviewed_profiles)}:"
        f"extra={sorted(_actual_reviewed_profiles - _expected_reviewed_profiles)}"
    )

B818_IDENTITY = ReviewedIdentityEntry.reviewed(
    identity_code="0x6125517D386EC407",
    manufacturer="Huawei",
    model="B818-263",
    hardware_sku="WL3B818M",
    software_version="11.0.1.1(H220SP1C11)",
    firmware_version="11.0.1.1(H220SP1C11)",
    access_type_id=B818_ACCESS_TYPE.access_type_id,
    display={
        "manufacturer": "Huawei",
        "model": "B818-263",
        "hardware_sku": "WL3B818M",
        "software_version": "11.0.1.1(H220SP1C11)",
        "firmware_version": "11.0.1.1(H220SP1C11)",
        "webui_evidence": "WEBUI 11.0.1.1(W2SP2C11)",
        "profile_evidence": "huawei_b818_263_h220sp1c11",
    },
    review_reference=f"{GENERATION_1_REVIEW_DOCUMENT}#b818-263-h220sp1c11",
)

B535_232_IDENTITY = ReviewedIdentityEntry.reviewed(
    identity_code="0x001167C6A4188B1E",
    manufacturer="Huawei",
    model="B535-232",
    hardware_sku="WL1B535M",
    software_version="10.0.1.1(H191SP64C983)",
    firmware_version="10.0.1.1(H191SP64C983)",
    access_type_id=B535_232_ACCESS_TYPE.access_type_id,
    display={
        "manufacturer": "Huawei",
        "model": "B535-232",
        "hardware_sku": "WL1B535M",
        "software_version": "10.0.1.1(H191SP64C983)",
        "firmware_version": "10.0.1.1(H191SP64C983)",
        "webui_evidence": "WEBUI 10.0.1.1(W2SP11C7201)",
        "profile_evidence": "huawei_b535_232_h191sp64c983",
    },
    review_reference=f"{GENERATION_1_REVIEW_DOCUMENT}#b535-232-h191sp64c983",
)

B818_LEGACY_MIGRATION = ReviewedLegacyIdentityMigration.reviewed(
    legacy_code="0x14BC9D58",
    target_identity_code=B818_IDENTITY.identity_code,
    evidence={
        "manufacturer": "Huawei",
        "model": "B818-263",
        "hardware_sku": "WL3B818M",
        "software_version": "11.0.1.1(H220SP1C11)",
        "firmware_version": "11.0.1.1(H220SP1C11)",
        "profile_id": "huawei_b818_263_h220sp1c11",
        "backend": "b818",
        "webui": "WEBUI 11.0.1.1(W2SP2C11)",
    },
    review_reference=f"{GENERATION_1_REVIEW_DOCUMENT}#legacy-hash-migration",
)

B535_232_LEGACY_MIGRATION = ReviewedLegacyIdentityMigration.reviewed(
    legacy_code="0x16F3DB8B",
    target_identity_code=B535_232_IDENTITY.identity_code,
    evidence={
        "manufacturer": "Huawei",
        "model": "B535-232",
        "hardware_sku": "WL1B535M",
        "software_version": "10.0.1.1(H191SP64C983)",
        "firmware_version": "10.0.1.1(H191SP64C983)",
        "profile_id": "huawei_b535_232_h191sp64c983",
        "backend": "b535",
        "webui": "WEBUI 10.0.1.1(W2SP11C7201)",
    },
    review_reference=f"{GENERATION_1_REVIEW_DOCUMENT}#legacy-hash-migration",
)

_PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V2 = ReviewedIdentityRegistry(
    access_types=tuple(
        row for row in _REVIEWED_ACCESS_TYPES_V3 if row is not ZTE_MF296R_ACCESS_TYPE
    ),
    identities=(B818_IDENTITY, B535_232_IDENTITY),
    legacy_migrations=(B818_LEGACY_MIGRATION, B535_232_LEGACY_MIGRATION),
    catalog_version=2,
    previous_catalog_checksum=GENERATION_1_CHECKSUM,
    created_at=0.0,
    generation_review_reference=(
        f"{GENERATION_2_REVIEW_DOCUMENT}#s1-execution-inventory-and-rca-code-state-at-7e514982"
    ),
)
if (
    _PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V2.catalog_snapshot.catalog_checksum
    != PRODUCTION_CATALOG_V2_CHECKSUM
):
    raise RuntimeError(
        "production Modem Live identity catalog v2 predecessor drift:"
        f"actual={_PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V2.catalog_snapshot.catalog_checksum}"
    )

_PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V3 = ReviewedIdentityRegistry(
    access_types=_REVIEWED_ACCESS_TYPES_V3,
    identities=(B818_IDENTITY, B535_232_IDENTITY),
    legacy_migrations=(B818_LEGACY_MIGRATION, B535_232_LEGACY_MIGRATION),
    catalog_version=3,
    previous_catalog_checksum=PRODUCTION_CATALOG_V2_CHECKSUM,
    created_at=0.0,
    generation_review_reference=(
        f"{GENERATION_3_REVIEW_DOCUMENT}#mf296r-exact-webuisession-owner-correction-2026-08-02"
    ),
)

_PRODUCTION_CATALOG_V3_SNAPSHOT = _PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V3.catalog_snapshot
if _PRODUCTION_CATALOG_V3_SNAPSHOT.catalog_checksum != PRODUCTION_CATALOG_V3_CHECKSUM:
    raise RuntimeError(
        "production Modem Live identity catalog v3 checksum drift:"
        f"expected={PRODUCTION_CATALOG_V3_CHECKSUM}:"
        f"actual={_PRODUCTION_CATALOG_V3_SNAPSHOT.catalog_checksum}"
    )

_PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V4 = ReviewedIdentityRegistry(
    access_types=_REVIEWED_ACCESS_TYPES_V3,
    identities=(B818_IDENTITY, B535_232_IDENTITY),
    legacy_migrations=(B818_LEGACY_MIGRATION, B535_232_LEGACY_MIGRATION),
    catalog_version=4,
    previous_catalog_checksum=PRODUCTION_CATALOG_V3_CHECKSUM,
    created_at=0.0,
    generation_review_reference=(
        f"{GENERATION_4_REVIEW_DOCUMENT}#only-legal-owners"
    ),
)

_PRODUCTION_CATALOG_V4_SNAPSHOT = _PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V4.catalog_snapshot
if (
    _PRODUCTION_CATALOG_V4_SNAPSHOT.catalog_checksum != PRODUCTION_CATALOG_V4_CHECKSUM
):
    raise RuntimeError(
        "production Modem Live identity catalog v4 checksum drift:"
        f"expected={PRODUCTION_CATALOG_V4_CHECKSUM}:"
        f"actual={_PRODUCTION_CATALOG_V4_SNAPSHOT.catalog_checksum}"
    )

_PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V5 = ReviewedIdentityRegistry(
    access_types=_REVIEWED_ACCESS_TYPES,
    identities=(B818_IDENTITY, B535_232_IDENTITY),
    legacy_migrations=(B818_LEGACY_MIGRATION, B535_232_LEGACY_MIGRATION),
    catalog_version=5,
    previous_catalog_checksum=PRODUCTION_CATALOG_V4_CHECKSUM,
    created_at=0.0,
    generation_review_reference=(
        f"{GENERATION_5_REVIEW_DOCUMENT}#only-legal-owners-after-replacement"
    ),
)

_PRODUCTION_CATALOG_V5_SNAPSHOT = _PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V5.catalog_snapshot
if (
    _PRODUCTION_CATALOG_V5_SNAPSHOT.catalog_checksum
    != PRODUCTION_CATALOG_V5_CHECKSUM
):
    raise RuntimeError(
        "production Modem Live identity catalog v5 checksum drift:"
        f"expected={PRODUCTION_CATALOG_V5_CHECKSUM}:"
        f"actual={_PRODUCTION_CATALOG_V5_SNAPSHOT.catalog_checksum}"
    )


_PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V6 = ReviewedIdentityRegistry(
    access_types=_REVIEWED_ACCESS_TYPES_V6,
    identities=(B818_IDENTITY, B535_232_IDENTITY),
    legacy_migrations=(B818_LEGACY_MIGRATION, B535_232_LEGACY_MIGRATION),
    catalog_version=6, previous_catalog_checksum=PRODUCTION_CATALOG_V5_CHECKSUM,
    created_at=0.0,
    generation_review_reference=f"{GENERATION_6_REVIEW_DOCUMENT}#only-legal-owners",
)
if _PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V6.catalog_snapshot.catalog_checksum != PRODUCTION_CATALOG_V6_CHECKSUM:
    raise RuntimeError("production Modem Live identity catalog v6 checksum drift")

_PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V7 = ReviewedIdentityRegistry(
    access_types=_REVIEWED_ACCESS_TYPES_V7,
    identities=(B818_IDENTITY, B535_232_IDENTITY),
    legacy_migrations=(B818_LEGACY_MIGRATION, B535_232_LEGACY_MIGRATION),
    catalog_version=7, previous_catalog_checksum=PRODUCTION_CATALOG_V6_CHECKSUM,
    created_at=0.0,
    generation_review_reference=f"{GENERATION_7_REVIEW_DOCUMENT}#only-legal-owners",
)
if _PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V7.catalog_snapshot.catalog_checksum != PRODUCTION_CATALOG_V7_CHECKSUM:
    raise RuntimeError("production Modem Live identity catalog v7 checksum drift:" + _PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V7.catalog_snapshot.catalog_checksum)

_PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V8 = ReviewedIdentityRegistry(
    access_types=_REVIEWED_ACCESS_TYPES_V8,
    identities=(B818_IDENTITY, B535_232_IDENTITY),
    legacy_migrations=(B818_LEGACY_MIGRATION, B535_232_LEGACY_MIGRATION),
    catalog_version=8, previous_catalog_checksum=PRODUCTION_CATALOG_V7_CHECKSUM,
    created_at=0.0,
    generation_review_reference=f"{GENERATION_8_REVIEW_DOCUMENT}#unreleased-catalog-consolidation",
)
if _PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V8.catalog_snapshot.catalog_checksum != PRODUCTION_CATALOG_V8_CHECKSUM:
    raise RuntimeError("production Modem Live identity catalog v8 checksum drift:" + _PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V8.catalog_snapshot.catalog_checksum)

# Access Type describes the shared protocol, not one installed firmware.
# Keep stable access/identity codes and semantics; change only reviewed wording.
TPLINK_NX520_ACCESS_TYPE = replace(
    TPLINK_NX520_ACCESS_TYPE,
    display_name="TP-Link NX520 encrypted DEV2 conditional access",
    review_reference=f"{GENERATION_9_REVIEW_DOCUMENT}#access-description-correction-inventory",
)
_REVIEWED_ACCESS_TYPES_V9 = tuple(
    TPLINK_NX520_ACCESS_TYPE if row.access_type_id == TPLINK_NX520_ACCESS_TYPE.access_type_id else row
    for row in _REVIEWED_ACCESS_TYPES_V8
)
_PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V9 = ReviewedIdentityRegistry(
    access_types=_REVIEWED_ACCESS_TYPES_V9,
    identities=(B818_IDENTITY, B535_232_IDENTITY),
    legacy_migrations=(B818_LEGACY_MIGRATION, B535_232_LEGACY_MIGRATION),
    catalog_version=9, previous_catalog_checksum=PRODUCTION_CATALOG_V8_CHECKSUM,
    created_at=0.0,
    generation_review_reference=f"{GENERATION_9_REVIEW_DOCUMENT}#access-description-correction-inventory",
)
if _PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V9.catalog_snapshot.catalog_checksum != PRODUCTION_CATALOG_V9_CHECKSUM:
    raise RuntimeError("production Modem Live identity catalog v9 checksum drift:" + _PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V9.catalog_snapshot.catalog_checksum)

_PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V10 = ReviewedIdentityRegistry(
    access_types=(*_REVIEWED_ACCESS_TYPES_V9,TPLINK_NX520_250902_ACCESS_TYPE),
    identities=(B818_IDENTITY,B535_232_IDENTITY),
    legacy_migrations=(B818_LEGACY_MIGRATION,B535_232_LEGACY_MIGRATION),
    catalog_version=10,previous_catalog_checksum=PRODUCTION_CATALOG_V9_CHECKSUM,
    created_at=0.0,generation_review_reference=TPLINK_NX520_250902_ACCESS_TYPE.review_reference,
)
if _PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V10.catalog_snapshot.catalog_checksum != PRODUCTION_CATALOG_V10_CHECKSUM:
    raise RuntimeError("production Modem Live identity catalog v10 checksum drift:" + _PRODUCTION_REVIEWED_IDENTITY_REGISTRY_V10.catalog_snapshot.catalog_checksum)

PRODUCTION_REVIEWED_IDENTITY_REGISTRY = ReviewedIdentityRegistry(
    access_types=(*_REVIEWED_ACCESS_TYPES_V9,TPLINK_NX520_250902_ACCESS_TYPE,TPLINK_NX520_250902_ACCESS_TYPE_V2),
    identities=(B818_IDENTITY,B535_232_IDENTITY),
    legacy_migrations=(B818_LEGACY_MIGRATION,B535_232_LEGACY_MIGRATION),
    catalog_version=11, previous_catalog_checksum=PRODUCTION_CATALOG_V10_CHECKSUM,
    created_at=0.0, generation_review_reference=TPLINK_NX520_250902_ACCESS_TYPE_V2.review_reference,
)
if PRODUCTION_REVIEWED_IDENTITY_REGISTRY.catalog_snapshot.catalog_checksum != PRODUCTION_CATALOG_V11_CHECKSUM:
    raise RuntimeError("production Modem Live identity catalog v11 checksum drift:" + PRODUCTION_REVIEWED_IDENTITY_REGISTRY.catalog_snapshot.catalog_checksum)

__all__ = [
    "GENERATION_11_REVIEW_DOCUMENT", "PRODUCTION_CATALOG_V11_CHECKSUM", "TPLINK_NX520_250902_ACCESS_TYPE_V2",
    "GENERATION_10_REVIEW_DOCUMENT", "PRODUCTION_CATALOG_V10_CHECKSUM", "TPLINK_NX520_250902_ACCESS_TYPE",
    "GENERATION_9_REVIEW_DOCUMENT", "PRODUCTION_CATALOG_V9_CHECKSUM",
    "H173_C983_ACCESS_TYPE",
    "G5B_B03_ACCESS_TYPE",
    "TPLINK_NX520_ACCESS_TYPE", "GENERATION_8_REVIEW_DOCUMENT", "PRODUCTION_CATALOG_V8_CHECKSUM",
    "XIAOMI_CB0401V2_ACCESS_TYPE", "GENERATION_7_REVIEW_DOCUMENT", "PRODUCTION_CATALOG_V7_CHECKSUM",
    "TPLINK_NX520V_ACCESS_TYPE",
    "GENERATION_6_REVIEW_DOCUMENT",
    "PRODUCTION_CATALOG_V6_CHECKSUM",
    "B535_232_ACCESS_TYPE",
    "B535_232_IDENTITY",
    "B535_232_LEGACY_MIGRATION",
    "B535_ACCESS_TYPE",
    "B818_ACCESS_TYPE",
    "B818_IDENTITY",
    "B818_LEGACY_MIGRATION",
    "ZTE_MF296R_ACCESS_TYPE",
    "GENERATION_1_CHECKSUM",
    "GENERATION_1_REVIEW_DOCUMENT",
    "GENERATION_2_REVIEW_DOCUMENT",
    "GENERATION_3_REVIEW_DOCUMENT",
    "GENERATION_4_REVIEW_DOCUMENT",
    "GENERATION_5_REVIEW_DOCUMENT",
    "PRODUCTION_REVIEWED_IDENTITY_REGISTRY",
    "PRODUCTION_CATALOG_V2_CHECKSUM",
    "PRODUCTION_CATALOG_V3_CHECKSUM",
    "PRODUCTION_CATALOG_V4_CHECKSUM",
    "PRODUCTION_CATALOG_V5_CHECKSUM",
    "REVIEW_DOCUMENT",
]
