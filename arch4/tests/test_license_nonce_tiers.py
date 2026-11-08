from arch4.licensing import (
    decode_license_nonce,
    license_tier_from_nonce,
    parse_license_entry,
    resolve_presentation_tier,
    validate_license,
)
from arch4.tests.license_signing_test_support import generate_license


def test_generate_license_with_tester_trusted_tier_is_nonce_bound():
    serial = "867648043234567"

    nonce, key = generate_license(serial, tier="tester_trusted")

    assert len(nonce) == 18
    assert nonce.startswith("TT")
    assert license_tier_from_nonce(nonce) == "tester_trusted"
    assert validate_license(serial, nonce, key) is True


def test_generate_license_with_internal_full_tier_is_nonce_bound():
    serial = "867648043234567"

    nonce, key = generate_license(serial, tier="internal_full")

    assert nonce.startswith("IF")
    assert license_tier_from_nonce(nonce) == "internal_full"
    assert validate_license(serial, nonce, key) is True


def test_legacy_license_without_tier_stays_valid_and_unprefixed():
    serial = "867648043234567"

    nonce, key = generate_license(serial)

    assert len(nonce) == 16
    assert license_tier_from_nonce(nonce) == ""
    assert validate_license(serial, nonce, key) is True
    assert resolve_presentation_tier(licensed=True, stored_tier="") == "internal_full"


def test_tampering_nonce_tier_prefix_breaks_validation():
    serial = "867648043234567"
    nonce, key = generate_license(serial, tier="tester_trusted")
    _tier, body = decode_license_nonce(nonce)
    tampered_nonce = f"IF{body}"

    assert validate_license(serial, tampered_nonce, key) is False


def test_decode_license_nonce_rejects_malformed_tier_nonce():
    assert decode_license_nonce("TT1234") == ("", "")
    assert license_tier_from_nonce("ZZ1234567890ABCDEF") == ""


def test_lowercase_nonce_still_validates():
    serial = "867648043234567"
    nonce, key = generate_license(serial, tier="tester_basic")

    assert validate_license(serial, nonce.lower(), key) is True
    assert decode_license_nonce(nonce.lower()) == ("tester_basic", nonce[2:])


def test_parse_license_entry_accepts_compact_and_generator_block():
    compact = " A1B2C3D4E5F67890-102D8E9F5A584707B178 "
    block = (
        "54GUC License Details\n"
        "Nonce:  A1B2C3D4E5F67890\n"
        "Key:    102D8E9F5A584707B178\n"
    )

    assert parse_license_entry(compact) == (
        "A1B2C3D4E5F67890",
        "102D8E9F5A584707B178",
    )
    assert parse_license_entry(block) == (
        "A1B2C3D4E5F67890",
        "102D8E9F5A584707B178",
    )
    assert parse_license_entry("NONCE-KEY") == ("", "")
