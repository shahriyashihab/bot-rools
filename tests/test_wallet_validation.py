"""Focused tests for the payment-method input validation.

Covers the exact rules required by the spec:

1. USDT BEP20  -> ^0x[a-fA-F0-9]{40}$   (TRC20 T... preserved)
2. TRX         -> ^T[a-zA-Z0-9]{33}$ + existing checksum validation
3. Binance ID  -> ^\\d{9,10}$
4. Card Name   -> ^(?=.*[A-Z])[A-Z0-9]{2,}$
"""

import pytest

from bot.handlers.withdraw import validate_wallet_address

# A real TRON address with a VALID Base58Check checksum (TRON USDT contract).
TRON_VALID = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


# ---------------------------------------------------------------------------
# 1. USDT BEP20
# ---------------------------------------------------------------------------

class TestUsdtBep20:
    def test_valid_42_char_address(self):
        ok, res = validate_wallet_address(
            "0x1234567890abcdef1234567890abcdef12345678", "USDT")
        assert ok is True
        assert res == "0x1234567890abcdef1234567890abcdef12345678"

    def test_uppercase_hex_digits_allowed(self):
        ok, _ = validate_wallet_address(
            "0x1234567890ABCDEF1234567890ABCDEF12345678", "USDT")
        assert ok is True

    def test_wrong_prefix_0X_rejected(self):
        ok, _ = validate_wallet_address(
            "0X1234567890abcdef1234567890abcdef12345678", "USDT")
        assert ok is False

    def test_wrong_length_rejected(self):
        ok, _ = validate_wallet_address("0x123", "USDT")
        assert ok is False

    def test_non_hex_characters_rejected(self):
        ok, _ = validate_wallet_address(
            "0xZZZZ567890abcdef1234567890abcdef12345678", "USDT")
        assert ok is False

    def test_41_chars_rejected(self):
        ok, _ = validate_wallet_address("0x" + "a" * 39, "USDT")
        assert ok is False

    def test_43_chars_rejected(self):
        ok, _ = validate_wallet_address("0x" + "a" * 41, "USDT")
        assert ok is False

    # Legacy behavior preserved: USDT still accepts TRC20 T-addresses.
    def test_trc20_still_accepted_under_usdt(self):
        ok, res = validate_wallet_address(TRON_VALID, "USDT")
        assert ok is True
        assert res == TRON_VALID


# ---------------------------------------------------------------------------
# 2. TRX / TRON (checksum must keep running after the format check)
# ---------------------------------------------------------------------------

class TestTrx:
    def test_valid_34_char_checksum_address(self):
        ok, res = validate_wallet_address(TRON_VALID, "TRX")
        assert ok is True
        assert res == TRON_VALID

    def test_wrong_length_rejected(self):
        ok, _ = validate_wallet_address(TRON_VALID[:-1], "TRX")
        assert ok is False

    def test_wrong_prefix_rejected(self):
        ok, _ = validate_wallet_address("X" + TRON_VALID[1:], "TRX")
        assert ok is False

    def test_lowercase_prefix_rejected(self):
        ok, _ = validate_wallet_address(TRON_VALID.lower(), "TRX")
        assert ok is False

    def test_invalid_characters_rejected(self):
        # Base58 alphabet excludes 0, O, I and l.
        ok, _ = validate_wallet_address("T0OI" + "a" * 30, "TRX")
        assert ok is False

    def test_bad_checksum_rejected(self):
        bad = list(TRON_VALID)
        bad[5] = "1" if bad[5] != "1" else "2"
        ok, _ = validate_wallet_address("".join(bad), "TRX")
        assert ok is False


# ---------------------------------------------------------------------------
# 3. Binance ID
# ---------------------------------------------------------------------------

class TestBinanceId:
    @pytest.mark.parametrize("value", ["123456789", "1234567890"])
    def test_valid(self, value):
        ok, res = validate_wallet_address(value, "BINANCE")
        assert ok is True
        assert res == value

    def test_8_digits_rejected(self):
        ok, _ = validate_wallet_address("12345678", "BINANCE")
        assert ok is False

    def test_11_digits_rejected(self):
        ok, _ = validate_wallet_address("12345678901", "BINANCE")
        assert ok is False

    @pytest.mark.parametrize("value", [
        "ABC123456", "12345abc9", "123_456789", "123-456789",
        "123 456789", "123@456789", "123.456789",
    ])
    def test_letters_symbols_whitespace_rejected(self, value):
        ok, _ = validate_wallet_address(value, "BINANCE")
        assert ok is False


# ---------------------------------------------------------------------------
# 4. Card Name
# ---------------------------------------------------------------------------

class TestCardName:
    @pytest.mark.parametrize("value", [
        "BT", "AF", "A6", "7K", "AB", "AB12", "1A", "A123456789", "CARD01",
    ])
    def test_valid_values_pass_and_preserved(self, value):
        ok, res = validate_wallet_address(value, "CARDNAME")
        assert ok is True
        assert res == value

    @pytest.mark.parametrize("value", [
        "A",        # single letter -> below minimum length
        "1",        # single digit
        "12",       # digits only: no uppercase letter
        "123456",   # digits only
        "a6",       # lowercase
        "Ab",       # lowercase
        "abc",      # all lowercase
        "A-6",      # hyphen
        "A_6",      # underscore
        "A 6",      # space
        "A@6",      # symbol
        "A.6",      # dot
    ])
    def test_invalid_values_rejected(self, value):
        ok, _ = validate_wallet_address(value, "CARDNAME")
        assert ok is False

    def test_valid_short_values_not_rejected_by_global_minimum(self):
        # The old generic min-length-5 rule must not reject these.
        for value in ("BT", "AF", "A6", "7K"):
            ok, _ = validate_wallet_address(value, "CARDNAME")
            assert ok is True, f"{value} was rejected by a generic rule"

    def test_business_maximum_is_unlimited(self):
        # 300 chars, still valid: no arbitrary business cap.
        long_name = "A" + "9" * 299
        ok, res = validate_wallet_address(long_name, "CARDNAME")
        assert ok is True
        assert res == long_name

    def test_surrounding_whitespace_trimmed_value_preserved(self):
        ok, res = validate_wallet_address("  BT  ", "CARDNAME")
        assert ok is True
        assert res == "BT"

    def test_internal_invalid_chars_not_stripped(self):
        # "B T" contains an internal space: rejected, not silently fixed.
        ok, res = validate_wallet_address("B T", "CARDNAME")
        assert ok is False
        assert res != "BT"


# ---------------------------------------------------------------------------
# Generic behavior
# ---------------------------------------------------------------------------

class TestGeneric:
    def test_empty_rejected(self):
        for method in ("USDT", "TRX", "BINANCE", "CARDNAME"):
            ok, _ = validate_wallet_address("", method)
            assert ok is False

    def test_whitespace_only_rejected(self):
        ok, _ = validate_wallet_address("   ", "CARDNAME")
        assert ok is False

    def test_unknown_method_legacy_pass_through(self):
        ok, _ = validate_wallet_address("some_wallet_123", "UNKNOWN")
        assert ok is True
