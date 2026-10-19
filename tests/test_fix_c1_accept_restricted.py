"""C1 regression tests: accept_restricted payout policy on ALL paths.

Before the fix, the per-country CORE CONFIG switch ``accept_restricted``
(ADMIN: CORE CONFIG > Accept Restricted) was enforced ONLY on the approval
and recovery-worker paths. The delayed task / fast path (finalize.py), the
twofa settlement and the reconcile scanner credited the FULL limit_rate -
so a limited account's payout depended on which path settled it.

The policy now lives in ``bot.verification.rates.get_country_rate``, the
single rate choke-point used by every settlement path.
"""

import pytest

from bot.data import _mark_dirty, _storage, save_storage_now
from bot.data.countries import set_country_setting_bool
from bot.verification.rates import get_country_rate


COUNTRY = "+C1FIX"
RATES = {"open": True, "spam_free_rate": 30.0, "spam_rate": 15.0, "limit_rate": 10.0}


@pytest.fixture()
def country():
    _storage["countries"][COUNTRY] = dict(RATES)
    _mark_dirty("countries", COUNTRY)
    save_storage_now()
    set_country_setting_bool(COUNTRY, "accept_restricted", True)
    yield COUNTRY
    _storage["countries"].pop(COUNTRY, None)
    save_storage_now()


class TestPolicyUnified:
    def test_restricted_off_zeroes_limit_payout(self, country):
        set_country_setting_bool(country, "accept_restricted", False)
        assert get_country_rate(country, "limit") == 0.0

    def test_restricted_on_keeps_limit_rate(self, country):
        assert get_country_rate(country, "limit") == 10.0

    def test_spam_free_never_affected(self, country):
        set_country_setting_bool(country, "accept_restricted", False)
        assert get_country_rate(country, "spam_free") == 30.0

    def test_spam_never_affected(self, country):
        set_country_setting_bool(country, "accept_restricted", False)
        assert get_country_rate(country, "spam") == 15.0

    def test_unset_toggle_defaults_to_payout(self, country):
        """No stored toggle -> documented default True -> full limit rate."""
        _storage["countries"][country].pop("accept_restricted", None)
        _mark_dirty("countries", country)
        save_storage_now()
        assert get_country_rate(country, "limit") == 10.0

    def test_unknown_country_unaffected(self):
        """No country record -> 1.00 resolution default; must NOT be zeroed."""
        assert get_country_rate("+NOPE9", "limit") == 1.00


class TestAllSettlementPaths:
    """Every path imports its rate through one of these two bindings."""

    def test_session_handler_binding(self, country):
        """finalize.py / reconcile.py / approval.py / worker.py import via
        bot.utils.session_handler (re-export of verification.rates)."""
        from bot.utils.session_handler import get_country_rate as handler_rate
        set_country_setting_bool(country, "accept_restricted", False)
        assert handler_rate(country, "limit") == 0.0

    def test_twofa_direct_binding(self, country):
        from bot.verification.rates import get_country_rate as rates_rate
        set_country_setting_bool(country, "accept_restricted", False)
        assert rates_rate(country, "limit") == 0.0

    def test_reconcile_resolve_rate_binding(self, country):
        """reconcile._resolve_rate routes through the same policy."""
        from bot.recovery.reconcile import _resolve_rate
        set_country_setting_bool(country, "accept_restricted", False)
        session_data = {"spam_status": "limit", "user_id": "C1USER"}
        info = {"spam_status": "limit"}
        assert _resolve_rate(session_data, info, country, "C1USER") == 0.0


class TestDisplayNotSilentlyChanged:
    """/cap display reads the DATA layer directly and must keep showing the
    configured limit price (the payout policy is a settlement concern)."""

    def test_data_layer_display_keeps_configured_rate(self, country):
        from bot.data.countries import get_country_rate as display_rate
        set_country_setting_bool(country, "accept_restricted", False)
        assert display_rate(country, "limit") == 10.0
