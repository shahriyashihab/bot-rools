"""MEDIUM #37/#42 regression tests: rejected-report claim + $0-rate settle.

#37: the rejected TEXT report had no atomic claim - two dispatch paths
(rejector, delayed task, recovery worker, reconcile) could post the same
rejected report twice. ``try_claim_rejected_report`` is once-only.

#42: Orphan A refused rate <= 0, wedging legitimate $0-rate free countries
(or Accept-Restricted-OFF limited accounts) for 72h+ with repeated CRITICAL
logs. Negative rates are still refused; 0.0 settles cleanly.
"""

import pytest

from bot.data import _storage, save_storage_now
from bot.data.sessions import try_claim_rejected_report


class TestRejectedReportClaim:
    def test_first_claim_wins(self):
        sid = "M37CLAIM"
        _storage.setdefault("pending_sessions", {})[sid] = {
            "user_id": "990201", "status": "pending", "session_data": {}}
        save_storage_now()
        try:
            assert try_claim_rejected_report(sid) is True
            # second claim for the same session must lose
            assert try_claim_rejected_report(sid) is False
        finally:
            _storage["pending_sessions"].pop(sid, None)
            save_storage_now()

    def test_claim_persisted_in_session_data(self):
        sid = "M37PERSIST"
        _storage.setdefault("pending_sessions", {})[sid] = {
            "user_id": "990201", "status": "pending", "session_data": {}}
        save_storage_now()
        try:
            try_claim_rejected_report(sid)
            info = _storage["pending_sessions"][sid]["session_data"]
            assert info["rejected_report_sent"] is True
        finally:
            _storage["pending_sessions"].pop(sid, None)
            save_storage_now()

    def test_unknown_session_claim_fails_cleanly(self):
        assert try_claim_rejected_report("M37GHOST") is False


class TestOrphanAZeroRatePolicy:
    """Source-level: the reconcile branch must credit 0.0 (explicit config)
    while still refusing negative resolved rates."""

    def _reconcile_src(self):
        return open("bot/recovery/reconcile.py", encoding="utf-8").read()

    def test_negative_rate_still_refused(self):
        src = self._reconcile_src()
        assert "if rate < 0:" in src  # negative refused
        assert "resolved rate is negative" in src

    def test_zero_rate_settles_now(self):
        src = self._reconcile_src()
        # the wedging CHECK is gone from code (a comment mentioning history is fine)
        assert "if rate <= 0:" not in src
        assert "explicit operator config" in src

    def test_zero_rate_would_settle_end_to_end(self):
        """Functional: with rate resolved to 0.0 and the credit machinery
        stubbed through, the row must be creditable (no wedge)."""
        from bot.data.users import atomic_credit_balance_for_session
        _storage.setdefault("pending_sessions", {})["M42ZERO"] = {
            "user_id": "990202", "status": "pending",
            "balance_credited": False, "session_data": {}}
        save_storage_now()
        try:
            credited = atomic_credit_balance_for_session("M42ZERO", 990202, 0.0)
            row = _storage["pending_sessions"]["M42ZERO"]
            assert credited is True
            assert row.get("balance_credited") is True
        finally:
            _storage["pending_sessions"].pop("M42ZERO", None)
            save_storage_now()


class TestReconcileWindowsAndGuards:
    def _reconcile_src(self):
        return open("bot/recovery/reconcile.py", encoding="utf-8").read()

    def test_md_window_follows_expiry_setting(self):
        """#41: the hardcoded 24h window now uses get_session_expiry_hours."""
        src = self._reconcile_src()
        assert "get_session_expiry_hours" in src
        assert "24 + (20 / 60.0)" not in src

    def test_orphan_b_respects_live_claim(self):
        """#38: a live finalizer's active claim must not be raced."""
        src = self._reconcile_src()
        assert "finalization actively claimed by a live path" in src

    def test_rejected_report_claimed_before_send(self):
        src = self._reconcile_src()
        assert "try_claim_rejected_report(session_id)" in src
