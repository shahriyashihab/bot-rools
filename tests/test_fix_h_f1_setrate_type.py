"""F-1 regression tests: set_country_rate_by_type validation + hierarchy.

Audit HIGH #13: the handler/data path stored ANY numeric value - a negative
rate reduced user balances per verified session, and limit > spam_free broke
the pricing ladder (free >= limit >= spam).
"""

import pytest

from bot.data import _mark_dirty, _storage, save_storage_now
from bot.data.countries import get_country_rates, set_country_rate_by_type


COUNTRY = "+H13"


@pytest.fixture()
def country():
    _storage["countries"][COUNTRY] = {
        "open": True, "spam_free_rate": 30.0, "spam_rate": 15.0, "limit_rate": 10.0,
    }
    _mark_dirty("countries", COUNTRY)
    save_storage_now()
    yield COUNTRY
    _storage["countries"].pop(COUNTRY, None)
    save_storage_now()


class TestValidation:
    def test_negative_rate_refused_entirely(self, country):
        with pytest.raises(ValueError):
            set_country_rate_by_type(country, "spam_rate", -5.0)
        rates = get_country_rates(country)
        assert rates["spam_rate"] == 15.0  # untouched

    def test_nan_and_inf_refused(self, country):
        for bad in (float("nan"), float("inf")):
            with pytest.raises(ValueError):
                set_country_rate_by_type(country, "limit_rate", bad)
        assert get_country_rates(country)["limit_rate"] == 10.0

    def test_unknown_rate_type_refused(self, country):
        with pytest.raises(ValueError):
            set_country_rate_by_type(country, "bogus_type", 5.0)

    def test_zero_rate_is_valid(self, country):
        """$0 is a legitimate free-country rate - must NOT be rejected."""
        stored = set_country_rate_by_type(country, "limit_rate", 0.0)
        assert stored == 0.0
        assert get_country_rates(country)["limit_rate"] == 0.0


class TestHierarchy:
    def test_limit_above_free_gets_clamped(self, country):
        """limit=99 with free=30 -> clamped to 30 (free stays the top)."""
        stored = set_country_rate_by_type(country, "limit_rate", 99.0)
        assert stored == 30.0
        rates = get_country_rates(country)
        assert rates["limit_rate"] <= rates["spam_free_rate"]

    def test_spam_above_limit_gets_clamped(self, country):
        set_country_rate_by_type(country, "spam_rate", 50.0)
        rates = get_country_rates(country)
        assert rates["spam_rate"] <= rates["limit_rate"]  # 10
        assert rates["spam_rate"] <= rates["spam_free_rate"]

    def test_spam_above_limit_gets_clamped(self, country):
        # free=30, limit=10 -> spam=12 breaks the ladder (spam must be <=
        # limit) so it is clamped to the limit, mirroring set_country_rates.
        stored = set_country_rate_by_type(country, "spam_rate", 12.0)
        assert stored == 10.0
        rates = get_country_rates(country)
        assert rates["spam_rate"] <= rates["limit_rate"]
        assert rates["spam_rate"] <= rates["spam_free_rate"]


class TestNewCountry:
    def test_creates_country_with_valid_rate(self):
        stored = set_country_rate_by_type("+H13NEW", "spam_free_rate", 7.5)
        assert stored == 7.5
        assert _storage["countries"]["+H13NEW"]["open"] is True
        assert _storage["countries"]["+H13NEW"]["spam_free_rate"] == 7.5
        _storage["countries"].pop("+H13NEW", None)
        save_storage_now()
