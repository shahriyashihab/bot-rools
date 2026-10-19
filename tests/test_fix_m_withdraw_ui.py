"""MEDIUM #17/#18/#19/#20/#29/#30/#31/#49 regression tests (withdraw UI).

#17: setrate_all confirmation shows the STORED (clamped) values.
#18: the >4000-char pending-withdraw panel keeps its first chunk (the old
     "Actions:" edit erased it) and splits overflow on line boundaries.
#19: the uwc_ callback parse is guarded (no crash on malformed data).
#20: set_user_withdraw_config returns False for unknown users and the
     handler reports it (no more false success).
#29: withdrawlogs maps every known status and shows unknown ones verbatim.
#30: approve/reject notify the user (templates exist, handlers send).
#31: a second click on a settled request answers "already settled" instead
     of replacing the screen with an error.
#49: revenue failures are shown as unavailable, never as silent $0.00.
"""

import pytest

from bot.data import _storage
from bot.data.withdrawals import set_user_withdraw_config, get_min_withdraw_for_method
from bot.utils.i18n import t


class TestSetRateAllShowsStoredValues:
    def test_confirmation_uses_stored_rates(self):
        src = open("bot/handlers/admin/countries.py", encoding="utf-8").read()
        assert "stored = get_country_rates(country)" in src
        assert "Hierarchy auto-adjusted" in src
        # the raw typed values must NOT be rendered directly in the body
        assert "f\"✅ Spam Free Rate: ${spam_free_rate}\\n\"" not in src


class TestPendingWithdrawPanel:
    def test_first_chunk_kept_with_buttons(self):
        src = open("bot/handlers/admin/withdraw.py", encoding="utf-8").read()
        assert 'await _edit_callback(query, text[:4000], reply_markup=InlineKeyboardMarkup(buttons))' in src
        assert 'await _edit_callback(query, "Actions:",' not in src
        # overflow splits on line boundaries
        assert "rfind(\"\\n\")" in src


class TestUwcParseGuard:
    def test_parse_is_guarded(self):
        src = open("bot/handlers/admin/withdraw.py", encoding="utf-8").read()
        assert "except (IndexError, ValueError):\n            await _edit_callback(query, \"⚠️ Malformed request" in src


class TestUserWithdrawConfigFalseSuccess:
    def test_unknown_user_returns_false(self):
        assert set_user_withdraw_config("990301", min_amt=5.0) is False
        assert "990301" not in _storage["users"]

    def test_known_user_returns_true(self):
        from bot.data.users import get_user_info
        uid = "990302"
        get_user_info(uid)
        try:
            assert set_user_withdraw_config(uid, min_amt=5.0) is True
        finally:
            _storage["users"].pop(uid, None)
            save_storage_now_safely()

    def test_handler_reports_false(self):
        src = open("bot/handlers/admin/withdraw.py", encoding="utf-8").read()
        assert "not found - withdraw settings only exist for real users" in src


def save_storage_now_safely():
    from bot.data import save_storage_now
    save_storage_now()


class TestWithdrawLogsMapping:
    def test_all_statuses_mapped(self):
        src = open("bot/handlers/admin/withdraw.py", encoding="utf-8").read()
        assert '"expired": "EXPIRED"' in src
        assert "str(status).upper() or 'UNKNOWN'" in src
        # the old silently-wrong mapping is gone
        assert '"PENDING" if status == "pending" else "REJECTED"' not in src


class TestUserNotification:
    def test_templates_exist(self):
        approved = t("withdraw_approved_user", "en", amount="5.00", method="USDT", wallet="TW")
        rejected = t("withdraw_rejected_user", "en", amount="5.00", method="USDT", wallet="TW")
        assert "Withdraw Approved" in approved and "5.00" in approved
        assert "Withdraw Rejected" in rejected and "refunded" in rejected
        assert not approved.startswith("["), "raw key fallback"
        assert not rejected.startswith("["), "raw key fallback"

    def test_handlers_send_and_never_block(self):
        src = open("bot/handlers/admin/withdraw.py", encoding="utf-8").read()
        assert "withdraw_approved_user" in src
        assert "withdraw_rejected_user" in src
        assert "User approve-notify skipped" in src  # failure is non-fatal

    def test_second_click_is_friendly(self):
        src = open("bot/handlers/admin/withdraw.py", encoding="utf-8").read()
        assert "Already settled (or list changed)" in src


class TestRevenueHonesty:
    def test_period_failure_not_shown_as_zero(self):
        src = open("bot/handlers/admin/withdraw.py", encoding="utf-8").read()
        assert "revenue is None" in src
        assert "Revenue data unavailable" in src
        assert "total_balance_error" in src
