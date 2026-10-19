"""BUG1 regression tests: admin "Deduct Balance" can never mint money.

Before the fix, the admin handler computed ``current - amount`` with no sign
check, so ``-10`` PASSED the ``amount > current_balance`` guard and ADDED
money (50 -> 60, verified live during the audit). The protection now lives in
the data layer (``atomic_deduct_balance_by_admin``): strictly positive
amounts only, check-and-debit inside ``_storage_lock``, overdraft refused,
unknown users refused, immediate persistence.
"""

import threading

import pytest

from bot.data import _mark_dirty, _storage, save_storage_now
from bot.data.users import (
    atomic_deduct_balance_by_admin,
    add_balance_for_user,
    get_user_info,
)


@pytest.fixture()
def user_with_balance():
    """User '991001' holding exactly $50."""
    uid = "991001"
    get_user_info(uid)  # ensure record exists
    _storage["users"][uid]["balance"] = 50.0
    _mark_dirty("users", uid)
    save_storage_now()
    yield uid
    _storage["users"].pop(uid, None)
    save_storage_now()


def _balance(uid: str) -> float:
    return float(_storage["users"][uid].get("balance") or 0)


class TestNegativeAmountMinting:
    """THE bug: negative amounts must never add money."""

    def test_negative_amount_refused(self, user_with_balance):
        ok, err, new_balance = atomic_deduct_balance_by_admin(user_with_balance, -10.0)
        assert ok is False
        assert err == "invalid_amount"
        # The critical assertion: balance is STILL 50 - money was NOT minted.
        assert _balance(user_with_balance) == 50.0

    @pytest.mark.parametrize("bad_amount", [-0.01, -1, -1000, 0, 0.0])
    def test_zero_and_negative_variants_refused(self, user_with_balance, bad_amount):
        ok, err, _ = atomic_deduct_balance_by_admin(user_with_balance, bad_amount)
        assert ok is False
        assert err == "invalid_amount"
        assert _balance(user_with_balance) == 50.0

    def test_nan_refused(self, user_with_balance):
        ok, err, _ = atomic_deduct_balance_by_admin(user_with_balance, float("nan"))
        assert ok is False
        assert err == "invalid_amount"
        assert _balance(user_with_balance) == 50.0

    def test_infinity_refused(self, user_with_balance):
        ok, err, _ = atomic_deduct_balance_by_admin(user_with_balance, float("inf"))
        assert ok is False
        assert _balance(user_with_balance) == 50.0

    def test_non_numeric_refused(self, user_with_balance):
        ok, err, _ = atomic_deduct_balance_by_admin(user_with_balance, "abc")
        assert ok is False
        assert err == "invalid_amount"
        assert _balance(user_with_balance) == 50.0


class TestHappyPath:
    def test_valid_deduct(self, user_with_balance):
        ok, err, new_balance = atomic_deduct_balance_by_admin(user_with_balance, 20.0)
        assert ok is True
        assert err == ""
        assert new_balance == 30.0
        assert _balance(user_with_balance) == 30.0

    def test_exact_balance_allowed(self, user_with_balance):
        ok, err, new_balance = atomic_deduct_balance_by_admin(user_with_balance, 50.0)
        assert ok is True
        assert new_balance == 0.0


class TestGuards:
    def test_overdraft_refused(self, user_with_balance):
        ok, err, _ = atomic_deduct_balance_by_admin(user_with_balance, 60.0)
        assert ok is False
        assert err == "insufficient"
        assert _balance(user_with_balance) == 50.0

    def test_unknown_user_refused_and_not_created(self):
        ok, err, _ = atomic_deduct_balance_by_admin("994044", 5.0)
        assert ok is False
        assert err == "unknown_user"
        assert "994044" not in _storage["users"]


class TestConcurrency:
    def test_parallel_deduct_no_lost_update(self, user_with_balance):
        """Two simultaneous deductions of 10 from 50 must yield exactly 30
        (the old unlocked read-modify-write could drop one update)."""
        barrier = threading.Barrier(2)
        results = []

        def worker():
            barrier.wait()
            results.append(atomic_deduct_balance_by_admin(user_with_balance, 10.0))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for ok, _, _ in results if ok) == 2
        assert _balance(user_with_balance) == 30.0

    def test_deduct_racing_a_credit_stays_consistent(self, user_with_balance):
        """A concurrent +30 credit and -20 deduct must both land: 50+30-20=60."""
        barrier = threading.Barrier(2)

        def credit():
            barrier.wait()
            add_balance_for_user(user_with_balance, 30.0)

        def deduct():
            barrier.wait()
            atomic_deduct_balance_by_admin(user_with_balance, 20.0)

        t1 = threading.Thread(target=credit)
        t2 = threading.Thread(target=deduct)
        t1.start(); t2.start(); t1.join(); t2.join()

        assert _balance(user_with_balance) == 60.0


class TestPersistence:
    def test_persistence_survives_reload(self, user_with_balance):
        """save_storage_now must put the deduction on disk, not just in memory."""
        from bot.data import state as state_mod

        atomic_deduct_balance_by_admin(user_with_balance, 10.0)
        # Simulate a restart: reload everything from SQLite.
        state_mod.load_storage()
        assert _balance(user_with_balance) == 40.0

