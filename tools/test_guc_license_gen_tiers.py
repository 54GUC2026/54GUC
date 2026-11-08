from arch4.licensing import license_tier_from_nonce, validate_license
from tools.guc_license_gen import (
    format_license_block,
    generate_license,
    generate_token,
    validate_license_output,
)


def test_tool_generates_tier_bound_nonce_for_trusted_tester():
    serial = "867648043234567"

    nonce, key = generate_license(serial, tier="tester_trusted")

    assert license_tier_from_nonce(nonce) == "tester_trusted"
    assert validate_license(serial, nonce, key) is True


def test_tool_format_block_includes_tier_line_when_present():
    serial = "867648043234567"
    token = generate_token(serial)
    nonce, key = generate_license(serial, tier="tester_trusted")

    block = format_license_block(serial, token, nonce, key, tier="tester_trusted")

    assert "Tier:   tester_trusted" in block
    assert f"Nonce:  {nonce}" in block
    assert f"Key:    {key}" in block


def test_tool_validate_output_accepts_tier_bound_nonce():
    serial = "867648043234567"
    token = generate_token(serial)
    nonce, key = generate_license(serial, tier="internal_full")

    assert validate_license_output(token, nonce, key) is None
