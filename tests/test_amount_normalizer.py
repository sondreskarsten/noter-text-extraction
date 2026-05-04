"""Norwegian financial amount parser tests. These are the spec — every case
that passes here is guaranteed correct in production output."""
import pytest
from noter_text_extraction.amount_normalizer import normalize_amount, detect_scale


class TestBasic:
    def test_simple_integer(self):
        assert normalize_amount("1234") == 1234.0

    def test_norwegian_decimal_comma(self):
        assert normalize_amount("145,65") == 145.65

    def test_norwegian_thousands_space(self):
        assert normalize_amount("1 234 567") == 1234567.0

    def test_norwegian_thousands_nbsp(self):
        assert normalize_amount("1\u00a0234\u00a0567") == 1234567.0

    def test_norwegian_combined(self):
        assert normalize_amount("1 234,56") == 1234.56

    def test_norwegian_period_thousands(self):
        # 145.650 in Norwegian context = 145650
        assert normalize_amount("145.650") == 145650.0

    def test_norwegian_period_thousands_long(self):
        assert normalize_amount("1.234.567") == 1234567.0

    def test_english_format_compat(self):
        assert normalize_amount("1,234.56") == 1234.56

    def test_english_thousands_no_decimal(self):
        assert normalize_amount("1,234,567") == 1234567.0


class TestNegatives:
    def test_parens_negative(self):
        assert normalize_amount("(1 234)") == -1234.0

    def test_parens_negative_decimal(self):
        assert normalize_amount("(1 234,56)") == -1234.56

    def test_dash_negative(self):
        assert normalize_amount("-1234") == -1234.0

    def test_negative_decimal(self):
        assert normalize_amount("-1 234,56") == -1234.56


class TestPlaceholders:
    def test_dash_returns_none(self):
        assert normalize_amount("-") is None

    def test_em_dash_returns_none(self):
        assert normalize_amount("—") is None

    def test_empty_returns_none(self):
        assert normalize_amount("") is None

    def test_na_returns_none(self):
        assert normalize_amount("N/A") is None

    def test_whitespace_only_returns_none(self):
        assert normalize_amount("   ") is None


class TestUnitDecorations:
    def test_kr_suffix(self):
        assert normalize_amount("1 234 kr") == 1234.0

    def test_nok_suffix(self):
        assert normalize_amount("1 234,56 NOK") == 1234.56

    def test_pct_suffix(self):
        # Note: % is preserved as-is (3,9 %), no auto /100 conversion —
        # this matches noter_v5b spec ("values as printed")
        assert normalize_amount("3,9 %") == 3.9


class TestPassthroughs:
    def test_int_passthrough(self):
        assert normalize_amount(1234) == 1234.0

    def test_float_passthrough(self):
        assert normalize_amount(1234.56) == 1234.56

    def test_none_passthrough(self):
        assert normalize_amount(None) is None

    def test_bool_returns_none(self):
        # Bool is a numeric subclass in Python but not what we want
        assert normalize_amount(True) is None


class TestScale:
    def test_explicit_scale_thousands(self):
        # Note declared "Beløp i 1000 NOK" → caller passes scale=1000
        assert normalize_amount("3,5", scale=1000) == 3500.0

    def test_explicit_scale_millions(self):
        assert normalize_amount("3,5", scale=1_000_000) == 3_500_000.0

    def test_no_scale_default(self):
        assert normalize_amount("3,5") == 3.5


class TestDetectScale:
    def test_belop_i_1000_nok(self):
        assert detect_scale("Beløp i 1000 NOK") == 1000.0

    def test_tall_i_hele_tusen(self):
        assert detect_scale("Tall i hele tusen") == 1000.0

    def test_tnok(self):
        assert detect_scale("Note 5 - Driftsinntekter (tNOK)") == 1000.0

    def test_mnok(self):
        assert detect_scale("Note 5 (MNOK)") == 1_000_000.0

    def test_millioner(self):
        assert detect_scale("Tall i millioner kr") == 1_000_000.0

    def test_no_scale_keyword(self):
        assert detect_scale("Note 4 Pensjonskostnader") == 1.0

    def test_empty_returns_one(self):
        assert detect_scale("") == 1.0


class TestEdgeCases:
    def test_ambiguous_period_three_digits(self):
        # 1.234 — could be 1.234 (decimal) or 1234 (thousands)
        # Norwegian convention: bare period with exactly 3 digits is ambiguous,
        # but if there's no comma, treat as thousands separator
        assert normalize_amount("1.234") == 1234.0

    def test_ambiguous_period_two_digits(self):
        # 1.23 — must be decimal (only 2 digits after period)
        assert normalize_amount("1.23") == 1.23

    def test_zero(self):
        assert normalize_amount("0") == 0.0

    def test_zero_with_comma(self):
        assert normalize_amount("0,00") == 0.0

    def test_negative_zero(self):
        assert normalize_amount("(0)") == 0.0

    def test_garbage_returns_none(self):
        assert normalize_amount("foo bar") is None

    def test_partial_garbage(self):
        # "1 234 kr 56" — invalid format
        assert normalize_amount("1 234 kr 56") is None

    def test_leading_plus(self):
        assert normalize_amount("+1234") == 1234.0
