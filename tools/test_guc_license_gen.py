"""
tools/test_guc_license_gen.py
═══════════════════════════════════════════════════════════════════════════════
Tests for 54GUC license generator logic.

Tests the core cryptographic functions — no GUI/desktop automation.
Run this file through the repository's selected native test runner.
"""

import pytest


VALID_HMAC_KEY = "AABBCCDDEE1122334455"


class TestGenerateToken:
    """Test token generation from modem serial."""

    def test_token_is_16_chars_uppercase_hex(self):
        """Token should be exactly 16 uppercase hex characters."""
        from tools.guc_license_gen import generate_token

        token = generate_token("867648043234567")

        assert len(token) == 16
        assert token.isupper()
        assert all(c in "0123456789ABCDEF" for c in token)

    def test_token_is_deterministic(self):
        """Same serial should always produce same token."""
        from tools.guc_license_gen import generate_token

        serial = "867648043234567"
        token1 = generate_token(serial)
        token2 = generate_token(serial)

        assert token1 == token2

    def test_different_serials_produce_different_tokens(self):
        """Different serials should produce different tokens."""
        from tools.guc_license_gen import generate_token

        token1 = generate_token("867648043234567")
        token2 = generate_token("867648043234568")

        assert token1 != token2

    def test_token_strips_whitespace(self):
        """Leading/trailing whitespace should be stripped."""
        from tools.guc_license_gen import generate_token

        token_clean = generate_token("867648043234567")
        token_padded = generate_token("  867648043234567  ")

        assert token_clean == token_padded

    def test_empty_serial_produces_token(self):
        """Empty serial should still produce a valid token (degenerate case)."""
        from tools.guc_license_gen import generate_token

        token = generate_token("")

        assert len(token) == 16
        assert token.isupper()


class TestGenerateLicense:
    """Test license key generation."""

    def test_returns_nonce_and_key_tuple(self):
        """Should return (nonce, key) tuple."""
        from tools.guc_license_gen import generate_license

        result = generate_license("867648043234567")

        assert isinstance(result, tuple)
        assert len(result) == 2
        nonce, key = result
        assert isinstance(nonce, str)
        assert isinstance(key, str)

    def test_nonce_is_16_chars_uppercase_hex(self):
        """Nonce should be exactly 16 uppercase hex characters."""
        from tools.guc_license_gen import generate_license

        nonce, _ = generate_license("867648043234567")

        assert len(nonce) == 16
        assert nonce.isupper()
        assert all(c in "0123456789ABCDEF" for c in nonce)

    def test_key_is_20_chars_uppercase_hex(self):
        """Key should retain the established vB5.3 HMAC shape."""
        from tools.guc_license_gen import generate_license

        _, key = generate_license("867648043234567")

        assert len(key) == 20
        assert key.isupper()
        assert all(c in "0123456789ABCDEF" for c in key)

    def test_nonce_is_random(self):
        """Nonces should be unique across calls."""
        from tools.guc_license_gen import generate_license

        serial = "867648043234567"
        nonces = set()

        for _ in range(10):
            nonce, _ = generate_license(serial)
            nonces.add(nonce)

        # All 10 should be different (astronomically unlikely to collide)
        assert len(nonces) == 10

    def test_different_nonces_produce_different_keys(self):
        """Same serial with different nonces should produce different keys."""
        from tools.guc_license_gen import generate_license

        serial = "867648043234567"
        keys = set()

        for _ in range(5):
            _, key = generate_license(serial)
            keys.add(key)

        assert len(keys) == 5


class TestCanonicalKeyDerivation:
    """Test the generator's canonical vB5.3 derivation wrapper."""

    def test_key_derivation_is_deterministic(self):
        """Same serial + nonce should always produce same key."""
        from tools.guc_license_gen import _derive_key

        serial = "867648043234567"
        nonce = "A1B2C3D4E5F67890"

        key1 = _derive_key(serial, nonce)
        key2 = _derive_key(serial, nonce)

        assert key1 == key2

    def test_key_is_20_chars_uppercase_hex(self):
        """Derived key should retain the established HMAC shape."""
        from tools.guc_license_gen import _derive_key

        key = _derive_key("867648043234567", "A1B2C3D4E5F67890")

        assert len(key) == 20
        assert key.isupper()
        assert all(c in "0123456789ABCDEF" for c in key)

    def test_different_serials_produce_different_keys(self):
        """Different serials with same nonce → different keys."""
        from tools.guc_license_gen import _derive_key

        nonce = "A1B2C3D4E5F67890"
        key1 = _derive_key("867648043234567", nonce)
        key2 = _derive_key("867648043234568", nonce)

        assert key1 != key2

    def test_different_nonces_produce_different_keys(self):
        """Same serial with different nonces → different keys."""
        from tools.guc_license_gen import _derive_key

        serial = "867648043234567"
        key1 = _derive_key(serial, "A1B2C3D4E5F67890")
        key2 = _derive_key(serial, "B2C3D4E5F6789012")

        assert key1 != key2

    def test_strips_whitespace_from_inputs(self):
        """Whitespace should be stripped from serial and nonce."""
        from tools.guc_license_gen import _derive_key

        key_clean = _derive_key("867648043234567", "A1B2C3D4E5F67890")
        key_padded = _derive_key("  867648043234567  ", "  A1B2C3D4E5F67890  ")

        assert key_clean == key_padded


class TestLicenseValidationCompatibility:
    """Test that generated licenses validate against arch4/licensing.py."""

    def test_generated_license_validates(self):
        """License generated by tool should validate via arch4 licensing module."""
        from tools.guc_license_gen import generate_license
        from arch4.licensing import validate_license

        serial = "867648043234567"
        nonce, key = generate_license(serial)

        assert validate_license(serial, nonce, key) is True

    def test_wrong_serial_fails_validation(self):
        """License should fail validation with wrong serial."""
        from tools.guc_license_gen import generate_license
        from arch4.licensing import validate_license

        serial = "867648043234567"
        nonce, key = generate_license(serial)

        # Wrong serial
        assert validate_license("867648043234568", nonce, key) is False

    def test_wrong_nonce_fails_validation(self):
        """License should fail validation with wrong nonce."""
        from tools.guc_license_gen import generate_license
        from arch4.licensing import validate_license

        serial = "867648043234567"
        nonce, key = generate_license(serial)

        # Wrong nonce
        assert validate_license(serial, "0000000000000000", key) is False

    def test_wrong_key_fails_validation(self):
        """License should fail validation with wrong key."""
        from tools.guc_license_gen import generate_license
        from arch4.licensing import validate_license

        serial = "867648043234567"
        nonce, key = generate_license(serial)

        # Wrong key
        assert validate_license(serial, nonce, "00000000000000000000") is False

    def test_tokens_match_between_modules(self):
        """Token generation should match between tool and arch4 licensing."""
        from tools.guc_license_gen import generate_token
        from arch4.licensing import generate_token as arch4_generate_token

        serial = "867648043234567"

        tool_token = generate_token(serial)
        arch4_token = arch4_generate_token(serial)

        assert tool_token == arch4_token


class TestEdgeCases:
    """Test edge cases and unusual inputs."""

    def test_very_long_serial(self):
        """Very long serial should work."""
        from tools.guc_license_gen import generate_license, generate_token
        from arch4.licensing import validate_license

        # 100-character serial
        serial = "A" * 100

        token = generate_token(serial)
        assert len(token) == 16

        nonce, key = generate_license(serial)
        assert validate_license(serial, nonce, key) is True

    def test_special_characters_in_serial(self):
        """Serial with special characters should work."""
        from tools.guc_license_gen import generate_license
        from arch4.licensing import validate_license

        serial = "IMEI-867648:043/234567"
        nonce, key = generate_license(serial)

        assert validate_license(serial, nonce, key) is True

    def test_unicode_in_serial(self):
        """Unicode characters in serial should work."""
        from tools.guc_license_gen import generate_license
        from arch4.licensing import validate_license

        serial = "модем-12345"
        nonce, key = generate_license(serial)

        assert validate_license(serial, nonce, key) is True

    def test_case_sensitivity_in_key_validation(self):
        """Established hexadecimal nonce and key input is case-normalized."""
        from tools.guc_license_gen import generate_license
        from arch4.licensing import validate_license

        serial = "867648043234567"
        nonce, key = generate_license(serial)

        assert validate_license(serial, nonce, key.lower()) is True
        assert validate_license(serial, nonce.lower(), key) is True

    def test_whitespace_only_serial_is_rejected(self):
        """Canonical generation requires a real modem identity."""
        from tools.guc_license_gen import generate_license, generate_token

        serial_whitespace = "   "

        token = generate_token(serial_whitespace)
        assert len(token) == 16

        with pytest.raises(ValueError, match="license_identity_required"):
            generate_license(serial_whitespace)


# ── Phase 2: Input Validation ────────────────────────────────────────────────


class TestValidateSerialInput:
    """Test validate_serial_input guards."""

    def test_empty_string_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input("") is not None
        assert (
            "serial" in validate_serial_input("").lower()
            or "enter" in validate_serial_input("").lower()
        )

    def test_whitespace_only_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input("   ") is not None

    def test_too_short_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("AB")
        assert err is not None
        assert "short" in err.lower()

    def test_too_long_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("A" * 100)
        assert err is not None
        assert "long" in err.lower()

    def test_valid_imei_accepted(self):
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input("867648043234567") is None

    def test_valid_short_serial_accepted(self):
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input("ABCD") is None

    def test_boundary_4_chars_accepted(self):
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input("1234") is None

    def test_boundary_3_chars_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input("123") is not None

    def test_boundary_64_chars_accepted(self):
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input("ABCD" * 16) is None  # 64 chars, varied

    def test_boundary_65_chars_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input("ABCD" * 16 + "E") is not None  # 65 chars

    def test_none_input_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input(None) is not None

    def test_strips_whitespace_before_length_check(self):
        from tools.guc_license_gen import validate_serial_input

        # "  ABCD  " has leading/trailing whitespace — now returns a warning
        err = validate_serial_input("  ABCD  ")
        assert err is not None
        assert "whitespace" in err.lower()


# ── Phase 2: License Block Format ────────────────────────────────────────────


class TestFormatLicenseBlock:
    """Test the centralized license block formatter."""

    def test_block_contains_all_fields(self):
        from tools.guc_license_gen import format_license_block

        block = format_license_block(
            serial="867648043234567",
            token="ABCDEF1234567890",
            nonce="1122334455667788",
            key="AABBCCDDEE1122334455",
        )
        assert "867648043234567" in block
        assert "ABCDEF1234567890" in block
        assert "1122334455667788" in block
        assert "AABBCCDDEE1122334455" in block

    def test_block_has_header(self):
        from tools.guc_license_gen import format_license_block

        block = format_license_block("s", "t", "n", "k")
        assert "54GUC License Details" in block

    def test_block_has_activation_instructions(self):
        from tools.guc_license_gen import format_license_block

        block = format_license_block("s", "t", "n", "k")
        assert "Settings" in block
        assert "activate" in block.lower()

    def test_block_labels_are_aligned(self):
        from tools.guc_license_gen import format_license_block

        block = format_license_block("s", "t", "n", "k")
        lines = block.strip().splitlines()
        # Serial, Token, Nonce, Key lines should have consistent label widths
        data_lines = [
            line
            for line in lines
            if ":" in line
            and line.split(":")[0].strip() in ("Serial", "Token", "Nonce", "Key")
        ]
        assert len(data_lines) == 4

    def test_block_matches_gui_copy_all_output(self):
        """format_license_block output should match historical _copy_all format."""
        from tools.guc_license_gen import format_license_block

        block = format_license_block(
            serial="TEST123",
            token="AAAA",
            nonce="BBBB",
            key="CCCC",
        )
        assert block.startswith("54GUC License Details\n")
        assert "Serial: TEST123" in block
        assert "Token:  AAAA" in block
        assert "Nonce:  BBBB" in block
        assert "Key:    CCCC" in block


# ── Phase 2: Round-Trip with Validation ──────────────────────────────────────


class TestValidationWithGeneration:
    """Integration: validate → generate → format → validate."""

    def test_full_pipeline(self):
        from tools.guc_license_gen import (
            validate_serial_input,
            generate_token,
            generate_license,
            format_license_block,
        )
        from arch4.licensing import validate_license

        serial = "867648043234567"
        assert validate_serial_input(serial) is None

        token = generate_token(serial)
        nonce, key = generate_license(serial)
        block = format_license_block(serial, token, nonce, key)

        # Block should contain all generated values
        assert nonce in block
        assert key in block

        # Generated license should validate
        assert validate_license(serial, nonce, key) is True

    def test_rejected_serial_never_reaches_generator(self):
        """Validation should catch bad input before crypto runs."""
        from tools.guc_license_gen import validate_serial_input

        for bad in ["", "  ", "AB", "A" * 100, None]:
            err = validate_serial_input(bad)
            assert err is not None, f"Expected rejection for {bad!r}"


# ── Phase 3: Enhanced Validation ─────────────────────────────────────────────


class TestValidateSerialInputPhase3:
    """Extended validation checks from phase 3."""

    def test_control_characters_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("ABCD\x00EF")
        assert err is not None
        assert "control" in err.lower()

    def test_tab_character_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("ABCD\tEFGH")
        assert err is not None
        assert "control" in err.lower()

    def test_newline_in_serial_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("ABCD\nEFGH")
        assert err is not None

    def test_leading_whitespace_warned(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("  ABCDEFGH")
        assert err is not None
        assert "whitespace" in err.lower()

    def test_trailing_whitespace_warned(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("ABCDEFGH  ")
        assert err is not None
        assert "whitespace" in err.lower()

    def test_clean_serial_passes(self):
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input("867648043234567") is None

    def test_printable_special_chars_pass(self):
        from tools.guc_license_gen import validate_serial_input

        # Dashes, slashes, colons are fine in device serials
        assert validate_serial_input("IMEI-867648:043/234567") is None


# ── Phase 3: License Output Validation ───────────────────────────────────────


class TestValidateLicenseOutput:
    """Test post-generation sanity checks."""

    def test_valid_output_passes(self):
        from tools.guc_license_gen import validate_license_output

        assert (
            validate_license_output(
                token="ABCDEF1234567890",
                nonce="1122334455667788",
                key=VALID_HMAC_KEY,
            )
            is None
        )

    def test_short_token_rejected(self):
        from tools.guc_license_gen import validate_license_output

        err = validate_license_output(
            "SHORT", "1122334455667788", "AABBCCDDEE1122334455"
        )
        assert err is not None
        assert "16" in err

    def test_short_nonce_rejected(self):
        from tools.guc_license_gen import validate_license_output

        err = validate_license_output(
            "ABCDEF1234567890", "SHORT", "AABBCCDDEE1122334455"
        )
        assert err is not None
        assert "16" in err

    def test_short_key_rejected(self):
        from tools.guc_license_gen import validate_license_output

        err = validate_license_output("ABCDEF1234567890", "1122334455667788", "SHORT")
        assert err is not None
        assert "expected 20" in err.lower()

    def test_non_hex_token_rejected(self):
        from tools.guc_license_gen import validate_license_output

        err = validate_license_output(
            "ABCDEFGHIJKLMNOP", "1122334455667788", "AABBCCDDEE1122334455"
        )
        assert err is not None
        assert "hex" in err.lower()

    def test_none_token_rejected(self):
        from tools.guc_license_gen import validate_license_output

        err = validate_license_output(None, "1122334455667788", "AABBCCDDEE1122334455")
        assert err is not None

    def test_empty_string_token_rejected(self):
        from tools.guc_license_gen import validate_license_output

        err = validate_license_output("", "1122334455667788", "AABBCCDDEE1122334455")
        assert err is not None

    def test_generated_values_always_pass(self):
        """Values from generate_license should always pass output validation."""
        from tools.guc_license_gen import (
            generate_token,
            generate_license,
            validate_license_output,
        )

        for serial in ["867648043234567", "IMEI-12345678", "модем-99999"]:
            token = generate_token(serial)
            nonce, key = generate_license(serial)
            err = validate_license_output(token, nonce, key)
            assert err is None, (
                f"Generated output failed validation for {serial}: {err}"
            )


# ── Phase 3: Deterministic Format ────────────────────────────────────────────


class TestDeterministicFormat:
    """License block format should be deterministic and parseable."""

    def test_block_is_deterministic(self):
        """Same inputs should always produce same block."""
        from tools.guc_license_gen import format_license_block

        args = (
            "SER123",
            "AAAA1111BBBB2222",
            "CCCC3333DDDD4444",
            "EEEE5555FFFF66667777",
        )
        block1 = format_license_block(*args)
        block2 = format_license_block(*args)
        assert block1 == block2

    def test_block_line_count_is_stable(self):
        """Block should always have exactly 8 lines."""
        from tools.guc_license_gen import format_license_block

        block = format_license_block("S", "T" * 16, "N" * 16, "K" * 20)
        lines = block.strip().splitlines()
        assert len(lines) == 8

    def test_block_parseable_for_fields(self):
        """Should be able to extract fields from block by splitting on ': '."""
        from tools.guc_license_gen import format_license_block

        serial = "867648043234567"
        token = "ABCDEF1234567890"
        nonce = "1122334455667788"
        key = "AABBCCDDEE1122334455"

        block = format_license_block(serial, token, nonce, key)
        fields = {}
        for line in block.splitlines():
            if ": " in line:
                label, value = line.split(": ", 1)
                fields[label.strip()] = value.strip()

        assert fields.get("Serial") == serial
        assert fields.get("Token") == token
        assert fields.get("Nonce") == nonce
        assert fields.get("Key") == key

    def test_block_no_trailing_whitespace_on_lines(self):
        """No line should have trailing whitespace."""
        from tools.guc_license_gen import format_license_block

        block = format_license_block("SER", "T" * 16, "N" * 16, "K" * 20)
        for i, line in enumerate(block.splitlines()):
            assert line == line.rstrip(), f"Line {i} has trailing whitespace: {line!r}"


# ── Phase 4: Export Payload Validation ───────────────────────────────────────


class TestValidateExportPayload:
    """Test clipboard/email payload validation."""

    def test_valid_block_passes(self):
        from tools.guc_license_gen import format_license_block, validate_export_payload

        block = format_license_block("SER123", "A" * 16, "B" * 16, "C" * 20)
        assert validate_export_payload(block) is None

    def test_empty_block_rejected(self):
        from tools.guc_license_gen import validate_export_payload

        assert validate_export_payload("") is not None
        assert validate_export_payload("   ") is not None

    def test_nul_byte_rejected(self):
        from tools.guc_license_gen import format_license_block, validate_export_payload

        block = format_license_block("SER", "A" * 16, "B" * 16, "C" * 20)
        block_with_nul = block + "\x00"
        err = validate_export_payload(block_with_nul)
        assert err is not None
        assert "NUL" in err

    def test_carriage_return_rejected(self):
        from tools.guc_license_gen import validate_export_payload

        block = "Serial: X\r\nToken: Y\r\nNonce: Z\r\nKey: W\r\n"
        err = validate_export_payload(block)
        assert err is not None
        assert "carriage" in err.lower()

    def test_missing_field_rejected(self):
        from tools.guc_license_gen import validate_export_payload

        # Missing Key: field
        block = "Serial: X\nToken: Y\nNonce: Z\n"
        err = validate_export_payload(block)
        assert err is not None
        assert "Key:" in err

    def test_generated_block_always_passes(self):
        """Generated blocks should always pass export validation."""
        from tools.guc_license_gen import (
            generate_token,
            generate_license,
            format_license_block,
            validate_export_payload,
        )

        for serial in ["867648043234567", "IMEI-12345678", "SER-ABC-99"]:
            token = generate_token(serial)
            nonce, key = generate_license(serial)
            block = format_license_block(serial, token, nonce, key)
            err = validate_export_payload(block)
            assert err is None, f"Generated block failed for {serial}: {err}"


# ── Phase 4: Tighter Invalid Input Handling ──────────────────────────────────


class TestTighterInputHandling:
    """Extended edge cases for serial validation."""

    def test_emoji_in_serial_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("SER-📱-12345")
        assert err is not None
        assert "emoji" in err.lower() or "symbol" in err.lower()

    def test_tab_separated_paste_rejected(self):
        """Tab character from spreadsheet paste should be caught."""
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("867648\t043234567")
        assert err is not None
        assert "control" in err.lower()

    def test_cr_lf_in_serial_rejected(self):
        """CR+LF from Windows paste should be caught."""
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("867648\r\n043234567")
        assert err is not None

    def test_null_byte_in_serial_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("8676\x0048043")
        assert err is not None

    def test_backspace_in_serial_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("ABCD\x08EFGH")
        assert err is not None

    def test_valid_hyphenated_serial_accepted(self):
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input("IMEI-867648-043234567") is None

    def test_valid_colon_serial_accepted(self):
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input("MAC:AA:BB:CC:DD:EE:FF") is None


# ── Phase 4: Full Pipeline with Export Validation ────────────────────────────


class TestFullPipelineWithExport:
    """End-to-end: validate → generate → format → export-validate → arch4-validate."""

    def test_complete_pipeline_with_all_checks(self):
        from tools.guc_license_gen import (
            validate_serial_input,
            generate_token,
            generate_license,
            format_license_block,
            validate_license_output,
            validate_export_payload,
        )
        from arch4.licensing import validate_license

        serial = "867648043234567"

        # Step 1: Input validation
        assert validate_serial_input(serial) is None

        # Step 2: Generate
        token = generate_token(serial)
        nonce, key = generate_license(serial)

        # Step 3: Output validation
        assert validate_license_output(token, nonce, key) is None

        # Step 4: Format
        block = format_license_block(serial, token, nonce, key)

        # Step 5: Export validation
        assert validate_export_payload(block) is None

        # Step 6: Arch4 validation
        assert validate_license(serial, nonce, key) is True

    def test_block_field_extraction_round_trip(self):
        """Fields extracted from block should match original values."""
        from tools.guc_license_gen import (
            generate_token,
            generate_license,
            format_license_block,
        )

        serial = "867648043234567"
        token = generate_token(serial)
        nonce, key = generate_license(serial)
        block = format_license_block(serial, token, nonce, key)

        # Parse back
        fields = {}
        for line in block.splitlines():
            if ": " in line:
                label, value = line.split(": ", 1)
                fields[label.strip()] = value.strip()

        assert fields["Serial"] == serial
        assert fields["Token"] == token
        assert fields["Nonce"] == nonce
        assert fields["Key"] == key


# ── Phase 5: Stricter Invalid Input Messaging ─────────────────────────────────


class TestStricterInputMessaging:
    """Phase 5: placeholder and obviously-invalid serials rejected with clear messages."""

    def test_all_zeros_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("0000000000000000")
        assert err is not None
        assert "zeros" in err.lower() or "placeholder" in err.lower()

    def test_short_zeros_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("0000")
        assert err is not None

    def test_placeholder_test_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("test")
        assert err is not None
        assert "placeholder" in err.lower()

    def test_placeholder_none_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("none")
        assert err is not None
        assert "placeholder" in err.lower()

    def test_placeholder_null_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("NULL")
        assert err is not None

    def test_placeholder_na_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("N/A")
        assert err is not None
        # N/A is only 3 chars, caught by length check
        assert "short" in err.lower()

    def test_placeholder_unknown_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("unknown")
        assert err is not None
        assert "placeholder" in err.lower()

    def test_placeholder_serial_word_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("serial")
        assert err is not None
        assert "placeholder" in err.lower()

    def test_placeholder_imei_word_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("IMEI")
        assert err is not None
        assert "placeholder" in err.lower()

    def test_valid_imei_still_passes(self):
        """Real IMEI should not be caught by placeholder check."""
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input("867648043234567") is None

    def test_nonzero_digits_pass(self):
        """Real numeric serial should pass."""
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input("1234567890") is None

    def test_placeholder_case_insensitive(self):
        """Placeholder check should be case-insensitive."""
        from tools.guc_license_gen import validate_serial_input

        for word in ["Test", "TEST", "tEsT", "None", "NONE"]:
            err = validate_serial_input(word)
            assert err is not None, f"Expected rejection for {word!r}"


# ── Phase 5: Clipboard/Export Payload Consistency ─────────────────────────────


class TestClipboardExportConsistency:
    """Phase 5: format_license_block + validate_export_payload round-trip."""

    def test_generated_block_always_valid_export(self):
        """Every generated block should pass export validation."""
        from tools.guc_license_gen import (
            generate_token,
            generate_license,
            format_license_block,
            validate_export_payload,
        )

        serials = [
            "867648043234567",
            "IMEI-12345678",
            "MAC:AA:BB:CC:DD:EE",
            "модем-99999",
            "SER/SLASH/TEST",
        ]
        for serial in serials:
            token = generate_token(serial)
            nonce, key = generate_license(serial)
            block = format_license_block(serial, token, nonce, key)
            err = validate_export_payload(block)
            assert err is None, f"Block for {serial!r} failed export: {err}"

    def test_block_uses_unix_line_endings(self):
        """Block should use \\n only, never \\r."""
        from tools.guc_license_gen import format_license_block

        block = format_license_block("SER", "A" * 16, "B" * 16, "C" * 20)
        assert "\r" not in block
        assert "\n" in block

    def test_block_has_no_nul_bytes(self):
        from tools.guc_license_gen import format_license_block

        block = format_license_block("SER", "A" * 16, "B" * 16, "C" * 20)
        assert "\x00" not in block

    def test_single_field_copy_matches_block_field(self):
        """Individual field values should exactly match what's in the block."""
        from tools.guc_license_gen import (
            generate_token,
            generate_license,
            format_license_block,
        )

        serial = "867648043234567"
        token = generate_token(serial)
        nonce, key = generate_license(serial)
        block = format_license_block(serial, token, nonce, key)

        # Parse fields from block
        fields = {}
        for line in block.splitlines():
            if ": " in line:
                label, value = line.split(": ", 1)
                fields[label.strip()] = value.strip()

        # Each individual value matches what the Copy button would copy
        assert fields["Token"] == token
        assert fields["Nonce"] == nonce
        assert fields["Key"] == key
        assert fields["Serial"] == serial


# ── Phase 5: Deterministic Output Blocks ──────────────────────────────────────


class TestDeterministicOutputPhase5:
    """Phase 5: output block byte-level determinism and structure stability."""

    def test_block_exact_structure_known_inputs(self):
        """Known inputs should produce exact known output structure."""
        from tools.guc_license_gen import format_license_block

        block = format_license_block(
            serial="867648043234567",
            token="ABCDEF1234567890",
            nonce="1122334455667788",
            key="AABBCCDDEE1122334455",
        )
        lines = block.splitlines()
        assert lines[0] == "54GUC License Details"
        assert lines[1] == "---------------------"
        assert lines[2] == "Serial: 867648043234567"
        assert lines[3] == "Token:  ABCDEF1234567890"
        assert lines[4] == "Nonce:  1122334455667788"
        assert lines[5] == "Key:    AABBCCDDEE1122334455"
        assert lines[6] == ""
        assert "activate" in lines[7].lower()

    def test_block_identical_across_calls(self):
        """Same inputs → byte-identical output, 100 times."""
        from tools.guc_license_gen import format_license_block

        args = ("SER-123", "A" * 16, "B" * 16, "C" * 20)
        reference = format_license_block(*args)
        for _ in range(100):
            assert format_license_block(*args) == reference

    def test_validate_output_catches_tampered_token(self):
        """Manually shortening token should fail output validation."""
        from tools.guc_license_gen import validate_license_output

        err = validate_license_output(
            "SHORT", "1122334455667788", "AABBCCDDEE1122334455"
        )
        assert err is not None

    def test_validate_output_catches_lowercase_hex(self):
        """Lowercase hex in fields should fail (we require uppercase)."""
        from tools.guc_license_gen import validate_license_output

        err = validate_license_output(
            "abcdef1234567890", "1122334455667788", "AABBCCDDEE1122334455"
        )
        assert err is not None
        assert "hex" in err.lower()


# ── Phase 6: Release-Ready Polish ──────────────────────────────────────────────


class TestRepeatedCharRejection:
    """Phase 6: repeated single-character serials rejected."""

    def test_all_same_letter_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("AAAA")
        assert err is not None
        assert "all" in err.lower()

    def test_long_repeated_char_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("XXXXXXXXXXXX")
        assert err is not None
        assert "'X'" in err

    def test_all_ones_rejected(self):
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("1111111111")
        assert err is not None

    def test_mixed_chars_pass(self):
        """Serial with at least 2 distinct chars should pass."""
        from tools.guc_license_gen import validate_serial_input

        assert validate_serial_input("AABB") is None
        assert validate_serial_input("1234") is None

    def test_all_zeros_still_caught_by_specific_message(self):
        """All zeros should hit the zeros-specific message, not generic repeated."""
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("0000")
        assert err is not None
        assert "zeros" in err.lower()

    def test_three_chars_too_short_before_repeat_check(self):
        """3-char input is caught by length check before repeat check."""
        from tools.guc_license_gen import validate_serial_input

        err = validate_serial_input("AAA")
        assert err is not None
        assert "short" in err.lower()


class TestBlockTrailingFormat:
    """Phase 6: block formatting edge cases for clipboard/email."""

    def test_block_does_not_end_with_newline(self):
        """Clipboard text should not end with trailing newline."""
        from tools.guc_license_gen import format_license_block

        block = format_license_block("SER", "A" * 16, "B" * 16, "C" * 20)
        assert not block.endswith("\n")

    def test_block_ends_with_activation_instruction(self):
        """Last line should be the activation instruction."""
        from tools.guc_license_gen import format_license_block

        block = format_license_block("SER", "A" * 16, "B" * 16, "C" * 20)
        last_line = block.strip().splitlines()[-1]
        assert "activate" in last_line.lower()

    def test_block_has_blank_line_before_instruction(self):
        """There should be a blank line separating fields from instruction."""
        from tools.guc_license_gen import format_license_block

        block = format_license_block("SER", "A" * 16, "B" * 16, "C" * 20)
        lines = block.splitlines()
        # Line after Key: should be blank
        key_idx = next(i for i, line in enumerate(lines) if line.startswith("Key:"))
        assert lines[key_idx + 1] == ""

    def test_block_label_widths_are_fixed(self):
        """Serial/Token/Nonce/Key values should all start at column 8."""
        from tools.guc_license_gen import format_license_block

        block = format_license_block("SER-12345", "T" * 16, "N" * 16, "K" * 20)
        lines = block.splitlines()
        data_lines = [
            line
            for line in lines
            if any(
                line.startswith(prefix)
                for prefix in ("Serial:", "Token:", "Nonce:", "Key:")
            )
        ]
        assert len(data_lines) == 4
        # Each line has format "Label:  value" or "Label: value" — value starts at col 8
        for line in data_lines:
            colon_pos = line.index(":")
            # After colon, skip spaces to find value start
            rest = line[colon_pos + 1 :]
            value_col = colon_pos + 1 + (len(rest) - len(rest.lstrip()))
            assert value_col == 8, (
                f"Value in {line!r} starts at col {value_col}, expected 8"
            )


class TestDeterministicBlockStructurePhase6:
    """Phase 6: block structure stability across varied inputs."""

    def test_line_count_stable_regardless_of_serial_length(self):
        """Block line count should be 8 regardless of serial length."""
        from tools.guc_license_gen import format_license_block

        for serial_len in (4, 15, 32, 64):
            serial = "X" * serial_len
            block = format_license_block(serial, "A" * 16, "B" * 16, "C" * 20)
            lines = block.strip().splitlines()
            assert len(lines) == 8, (
                f"Serial len {serial_len} produced {len(lines)} lines"
            )

    def test_block_header_always_54guc(self):
        """Header should always be '54GUC License Details'."""
        from tools.guc_license_gen import format_license_block

        block = format_license_block("ANY", "A" * 16, "B" * 16, "C" * 20)
        assert block.startswith("54GUC License Details\n")

    def test_full_pipeline_export_validation_100_serials(self):
        """100 random serials should all produce valid export blocks."""
        import secrets
        from tools.guc_license_gen import (
            generate_token,
            generate_license,
            format_license_block,
            validate_license_output,
            validate_export_payload,
        )

        for _ in range(100):
            serial = secrets.token_hex(8).upper()
            token = generate_token(serial)
            nonce, key = generate_license(serial)

            out_err = validate_license_output(token, nonce, key)
            assert out_err is None, f"Output invalid for {serial}: {out_err}"

            block = format_license_block(serial, token, nonce, key)
            exp_err = validate_export_payload(block)
            assert exp_err is None, f"Export invalid for {serial}: {exp_err}"
