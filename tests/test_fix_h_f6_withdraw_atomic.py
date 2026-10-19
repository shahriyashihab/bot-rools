"""F-6 regression tests: withdraw deduct + record are ONE atomic commit.

Before the fix, ``handle_wallet_input`` deducted the balance synchronously
but the pending request record was written inside the spawned background
task. A crash between the two steps left the balance deducted with NO
request anywhere - silent user money loss (audit HIGH #7 / R4).
"""

import threading

import pytest

from bot.data import _mark_dirty, _storage, save_storage_now
from bot.data.users import get_user_info
from bot.data.withdrawals import create_withdraw_request_atomic


@pytest.fixture()
def funded_user():
    uid = "990021"
    get_user_info(uid)
    _storage["users"][uid]["balance"] = 100.0
    _mark_dirty("users", uid)
    save_storage_now()
    yield uid
    _storage["users"].pop(uid, None)
    _storage["withdrawals"][:] = [
        w for w in _storage["withdrawals"] if w.get("user_id") != uid
    ]
    save_storage_now()


def _pending(uid: str):
    return [w for w in _storage["withdrawals"]
            if w.get("user_id") == uid and w.get("status") == "pending"]


class TestAtomicDeductAndRecord:
    def test_success_creates_both(self, funded_user):
        result = create_withdraw_request_atomic(
            funded_user, "usdt", 40.0, "TWALLET",
            country_breakdown={"+880": {"count": 2, "total": 40.0}},
        )
        assert result["success"] is True
        assert result["request_id"].startswith("w")
        assert float(_storage["users"][funded_user]["balance"]) == 60.0
        pend = _pending(funded_user)
        assert len(pend) == 1
        assert float(pend[0]["amount"]) == 40.0
        assert pend[0]["country_breakdown"]["+880"]["count"] == 2

    def test_both_survive_simulated_restart(self, funded_user):
        """THE crash-window: after the call, a restart must show the deducted
        balance AND the pending request together (old code: record lost)."""
        from bot.data import state as state_mod

        create_withdraw_request_atomic(funded_user, "usdt", 40.0, "TWALLET")
        state_mod.load_storage()
        assert float(_storage["users"][funded_user]["balance"]) == 60.0
        assert len(_pending(funded_user)) == 1


class TestGuards:
    def test_insufficient_refused_no_record(self, funded_user):
        result = create_withdraw_request_atomic(funded_user, "usdt", 999.0, "T")
        assert result["success"] is False
        assert result["error"] == "insufficient"
        assert float(_storage["users"][funded_user]["balance"]) == 100.0
        assert len(_pending(funded_user)) == 0

    def test_unknown_user_refused_not_created(self):
        result = create_withdraw_request_atomic("990499", "usdt", 10.0, "T")
        assert result["success"] is False
        assert result["error"] == "unknown_user"
        assert "990499" not in _storage["users"]

    def test_negative_and_zero_refused(self, funded_user):
        for bad in (-5.0, 0.0, float("nan"), float("inf")):
            result = create_withdraw_request_atomic(funded_user, "usdt", bad, "T")
            assert result["success"] is False
            assert result["error"] == "invalid_amount"
        assert float(_storage["users"][funded_user]["balance"]) == 100.0
        assert len(_pending(funded_user)) == 0


class TestConcurrent:
    def test_double_submit_only_one_wins(self, funded_user):
        """Two racing submits when the balance allows only one: exactly one
        request may be created and one deduction may land."""
        barrier = threading.Barrier(2)
        results = []

        def worker():
            barrier.wait()
            results.append(create_withdraw_request_atomic(
                funded_user, "usdt", 80.0, "TWALLET"))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for r in results if r["success"]) == 1
        assert float(_storage["users"][funded_user]["balance"]) == 20.0
        assert len(_pending(funded_user)) == 1
