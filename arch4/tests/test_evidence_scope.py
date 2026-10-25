from __future__ import annotations

import pytest

from arch4.evidence_scope import (
    EvidencePortability,
    EvidenceScope,
    FormulaVector,
)


def _scope(**overrides) -> EvidenceScope:
    data = {
        "identity_code": "0x0000000000000101",
        "identity_status": "exact",
        "identity_catalog_version": "identity-catalog-v1",
        "identity_catalog_checksum": "a" * 64,
        "session_scope_key": "session-a",
        "source_epoch": "boot-a",
        "source_sequence": 42,
        "observed_at": 1000.0,
        "evidence_age_s": 0.2,
        "confidence": 0.8,
        "evidence_count": 12,
        "formula_vector": FormulaVector.build(
            {"quality": "cq-v6", "prediction": "crystal-v2"}
        ),
        "portability": EvidencePortability.EXACT_IDENTITY,
        "access_type_id": "huawei_hilink_v1",
        "model_family_id": "huawei_b818",
        "provider_plmn": "22005",
        "site_scope_id": "site:home",
        "cell_scope_key": "earfcn=1795|pci=123",
        "serving_band": 3,
        "action_shape_key": "PBAND:3:1+3+20",
    }
    data.update(overrides)
    return EvidenceScope(**data)


def test_formula_vector_is_order_independent_and_tamper_evident() -> None:
    first = FormulaVector.build({"quality": "cq-v6", "prediction": "crystal-v2"})
    second = FormulaVector.build({"prediction": "crystal-v2", "quality": "cq-v6"})
    assert first == second
    assert first.vector_id == second.vector_id
    assert FormulaVector.from_mapping(first.as_dict()) == first

    tampered = first.as_dict()
    tampered["epochs"]["quality"] = "cq-v5"
    with pytest.raises(ValueError, match="checksum"):
        FormulaVector.from_mapping(tampered)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("identity_code", "0x0000000000000000", "exact_identity"),
        ("identity_code", "0x101", "exact_identity"),
        ("identity_status", "provisional", "exact_identity_status"),
        ("identity_catalog_checksum", "", "catalog_reference"),
        ("session_scope_key", "", "session"),
        ("source_sequence", 0, "generation"),
        ("evidence_count", 0, "count"),
        ("confidence", 1.1, "confidence"),
        ("provider_plmn", "provider", "provider_plmn"),
    ],
)
def test_invalid_or_incomplete_scope_fails_closed(field, value, reason) -> None:
    with pytest.raises(ValueError, match=reason):
        _scope(**{field: value})


def test_scope_round_trip_and_tamper_detection() -> None:
    scope = _scope()
    assert EvidenceScope.from_mapping(scope.as_dict()) == scope

    tampered = scope.as_dict()
    tampered["serving_band"] = 20
    with pytest.raises(ValueError, match="checksum"):
        EvidenceScope.from_mapping(tampered)


def test_exact_runtime_match_rejects_device_session_formula_and_context_crossing() -> None:
    scope = _scope()
    assert scope.exact_runtime_match(_scope(source_sequence=43)) is True
    assert scope.exact_runtime_match(
        _scope(identity_code="0x0000000000000102")
    ) is False
    assert scope.exact_runtime_match(_scope(session_scope_key="session-b")) is False
    assert scope.exact_runtime_match(
        _scope(formula_vector=FormulaVector.build({"quality": "cq-v7"}))
    ) is False
    assert scope.exact_runtime_match(_scope(provider_plmn="22001")) is False
    assert scope.exact_runtime_match(_scope(serving_band=20)) is False


def test_exact_generation_match_requires_the_same_accepted_sequence() -> None:
    scope = _scope(source_sequence=42)

    assert scope.exact_generation_match(_scope(source_sequence=42)) is True
    assert scope.exact_generation_match(_scope(source_sequence=43)) is False


def test_broader_portability_must_name_its_compatibility_boundary() -> None:
    portable = _scope(portability=EvidencePortability.SAME_ACCESS_TYPE)
    assert portable.portability is EvidencePortability.SAME_ACCESS_TYPE

    with pytest.raises(ValueError, match="access_type"):
        _scope(
            portability=EvidencePortability.SAME_ACCESS_TYPE,
            access_type_id="",
        )
    with pytest.raises(ValueError, match="model_family"):
        _scope(
            portability=EvidencePortability.SAME_MODEL_FAMILY,
            model_family_id="",
        )
