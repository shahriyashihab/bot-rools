"""E-4 regression tests: approve/reject withdrawal race conditions.

Before the fix, ``approve_withdraw_request`` / ``reject_withdraw_request``
ran their pending-check, status flip and refund WITHOUT ``_storage_lock``.
Two concurrent admin clicks could BOTH observe ``status == "pending"``:
approve+reject paid the user AND refunded them; approve+approve was possible
too. The approve path also used the 0.5s debounced save (a crash in that
window erased the approval and let a later reject refund an ALREADY-PAID
payout). Everything now runs inside the lock and persists immediately.
"""

import threading

import pytest

from bot.data import _mark_dirty, _storage, save_storage_now
from bot.data.users import get_user_info
from bot.data.withdrawals import (
    approve_withdraw_request,
    reject_withdraw_request,
)


def _make_pending_request(request_id: str, user_id: str, amount: float = 40.0) -> None:
    get_user_info(user_id)
    _storage["users"][user_id]["balance"] = 100.0
    lst = _storage.setdefault("withdrawals", [])
    lst[:] = [w for w in lst if w.get("request_id") != request_id]
    lst.append({
        "request_id": request_id,
        "user_id": user_id,
        "amount": amount,
        "method": "usdt",
        "wallet_address": "TWALLET",
        "timestamp": "2026-09-07 10:00:00",
        "status": "pending",
    })
    _mark_dirty("users", user_id)
    _mark_dirty("withdrawals", None)
    save_storage_now()


@pytest.fixture()
def pending_request():
    _make_pending_request("wFIXTURE1", "992001")
    yield "wFIXTURE1", "992001"
    _storage["withdrawals"][:] = [
        w for w in _storage["withdrawals"] if w.get("request_id") != "wFIXTURE1"
    ]
    _storage["users"].pop("992001", None)
    save_storage_now()


class TestSequentialBehaviour:
    def test_approve_flips_status_only_no_balance_change(self, pending_request):
        rid, uid = pending_request
        result = approve_withdraw_request(rid)
        assert result["success"] is True
        assert float(_storage["users"][uid]["balance"]) == 100.0  # no double-deduct
        assert _storage["withdrawals"][0]["status"] == "approved"

    def test_reject_refunds_once(self, pending_request):
        rid, uid = pending_request
        result = reject_withdraw_request(rid)
        assert result["success"] is True
        assert result["refunded"] is True
        assert float(_storage["users"][uid]["balance"]) == 140.0

    def test_second_click_of_same_request_fails_cleanly(self, pending_request):
        rid, _ = pending_request
        assert approve_withdraw_request(rid)["success"] is True
        second = reject_withdraw_request(rid)
        assert second["success"] is False
        assert second["error"] == "Request not found"

    def test_unknown_request_id_refused(self):
        result = approve_withdraw_request("wNOPE")
        assert result["success"] is False
        assert result["error"] == "Request not found"


class TestConcurrentRace:
    def test_concurrent_approve_and_reject_only_one_wins(self, pending_request):
        """THE audit scenario: approve + reject fired simultaneously. Exactly
        one must win; the balance must never show BOTH a payout and a refund
        of the same request (i.e. never 100 - 40 + 40 = 100 with an approved
        ledger, which the unlocked code allowed)."""
        rid, uid = pending_request
        results = {}

        def approver():
            results["a"] = approve_withdraw_request(rid)

        def rejector():
            results["r"] = reject_withdraw_request(rid)

        t1 = threading.Thread(target=approver)
        t2 = threading.Thread(target=rejector)
        t1.start(); t2.start(); t1.join(); t2.join()

        approved = results["a"]["success"]
        rejected = results["r"]["success"]
        assert approved != rejected  # exactly one path wins

        ledger_status = _storage["withdrawals"][0]["status"]
        balance = float(_storage["users"][uid]["balance"])

        if approved:
            assert ledger_status == "approved"
            assert balance == 100.0  # payout handled elsewhere; NO local refund
        else:
            assert ledger_status == "rejected"
            assert balance == 140.0  # exactly one refund

    def test_concurrent_double_approve_only_one_wins(self, pending_request):
        rid, uid = pending_request
        results = []

        def worker():
            results.append(approve_withdraw_request(rid))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for r in results if r["success"]) == 1
        assert _storage["withdrawals"][0]["status"] == "approved"

    def test_concurrent_double_reject_refunds_only_once(self, pending_request):
        rid, uid = pending_request
        results = []

        def worker():
            results.append(reject_withdraw_request(rid))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for r in results if r["success"]) == 1
        assert float(_storage["users"][uid]["balance"]) == 140.0  # single refund


class TestPersistence:
    def test_approval_survives_simulated_restart(self, pending_request):
        """save_storage_now (was debounced): a hard kill right after approve
        must not erase the approval (which previously enabled a later refund
        of an already-paid withdrawal)."""
        from bot.data import state as state_mod

        rid, uid = pending_request
        assert approve_withdraw_request(rid)["success"] is True
        state_mod.load_storage()  # simulate restart
        status = next(w["status"] for w in _storage["withdrawals"]
                      if w.get("request_id") == rid)
        assert status == "approved"


class TestLegacyCompatibility:
    def test_request_without_id_still_settles_via_user_timestamp(self):
        """Old rows created before request_id existed must remain settleable."""
        _make_pending_request("wLEGACY", "992002")
        _storage["withdrawals"][-1].pop("request_id")
        save_storage_now()

        result = reject_withdraw_request(0)  # positional index
        assert result["success"] is True

        _storage["withdrawals"][:] = [
            w for w in _storage["withdrawals"] if w.get("user_id") != "992002"
        ]
        _storage["users"].pop("992002", None)
        save_storage_now()

