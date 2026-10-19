"""F-7 + R5 regression tests: balance read-modify-write lock discipline.

F-7 (audit HIGH #8): ``add_balance_for_user`` read the balance OUTSIDE
``_storage_lock`` (via ``get_user_info``) and wrote it inside - a concurrent
credit between read and write was silently lost.

R5 (audit HIGH #9): ``zero_all_balances`` mutated unlocked with the debounced
save - an in-flight credit could be resurrected by the tracker reset, and a
crash in the 0.5s window left HALF-reset state.
"""

import threading

import pytest

from bot.data import _mark_dirty, _storage, save_storage_now
from bot.data.users import (
    add_balance_for_user,
    get_user_info,
    zero_all_balances,
)


def _set_balance(uid: str, value: float) -> None:
    get_user_info(uid)
    _storage["users"][uid]["balance"] = value
    _mark_dirty("users", uid)
    save_storage_now()


class TestAddBalanceLocking:
    def test_concurrent_credit_and_debit_both_land(self):
        uid = "990031"
        _set_balance(uid, 50.0)
        barrier = threading.Barrier(2)

        def credit():
            barrier.wait()
            add_balance_for_user(uid, 30.0)

        def debit():
            barrier.wait()
            add_balance_for_user(uid, -20.0)

        t1, t2 = threading.Thread(target=credit), threading.Thread(target=debit)
        t1.start(); t2.start(); t1.join(); t2.join()

        # Old code: one update lost -> 50+30=80 or 50-20=30. Fixed: both land.
        assert float(_storage["users"][uid]["balance"]) == 60.0
        _storage["users"].pop(uid, None); save_storage_now()

    def test_parallel_credits_no_lost_update(self):
        uid = "990032"
        _set_balance(uid, 0.0)
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            add_balance_for_user(uid, 10.0)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert float(_storage["users"][uid]["balance"]) == 40.0
        _storage["users"].pop(uid, None); save_storage_now()

    def test_creates_missing_user_on_positive_credit(self):
        uid = "990033"
        add_balance_for_user(uid, 15.0)
        assert float(_storage["users"][uid]["balance"]) == 15.0
        _storage["users"].pop(uid, None); save_storage_now()

    def test_garbage_amount_ignored(self):
        uid = "990034"
        _set_balance(uid, 10.0)
        for bad in (float("nan"), float("inf"), float("-inf"), "abc"):
            add_balance_for_user(uid, bad)
        assert float(_storage["users"][uid]["balance"]) == 10.0
        _storage["users"].pop(uid, None); save_storage_now()


class TestZeroAllBalances:
    def test_zeroes_and_clears_tracker(self):
        uid = "990041"
        _set_balance(uid, 25.0)
        _storage.setdefault("session_tracker", {})[uid] = [{"amount": 5.0}]
        _mark_dirty("session_tracker", None)
        save_storage_now()

        count = zero_all_balances()
        assert count >= 1
        assert float(_storage["users"][uid]["balance"]) == 0.0
        assert _storage.get("session_tracker") == {}
        _storage["users"].pop(uid, None); save_storage_now()

    def test_zeroed_state_survives_simulated_restart(self):
        """save_storage_now (was debounced): a kill right after zeroing must
        not resurrect any balance."""
        from bot.data import state as state_mod

        uid = "990042"
        _set_balance(uid, 30.0)
        zero_all_balances()
        state_mod.load_storage()
        assert float(_storage["users"][uid]["balance"]) == 0.0
        _storage["users"].pop(uid, None); save_storage_now()

    def test_zero_beats_inflight_credit(self):
        """A credit racing the zero must not resurrect money after the wipe."""
        uid = "990043"
        _set_balance(uid, 10.0)
        results = []
        barrier = threading.Barrier(2)

        def credit():
            barrier.wait()
            add_balance_for_user(uid, 100.0)

        def wipe():
            barrier.wait()
            results.append(zero_all_balances())

        t1, t2 = threading.Thread(target=credit), threading.Thread(target=wipe)
        t1.start(); t2.start(); t1.join(); t2.join()

        # Whichever order, both updates are serialized under _storage_lock
        # and nothing is lost:
        #   wipe first (10->0), credit after  -> 0 + 100 = 100
        #   credit first (10->110), wipe last -> wiped -> 0
        # The old race could leave 10.0 (credit lost to the wipe).
        final = float(_storage["users"][uid]["balance"])
        assert final in (0.0, 100.0)  # both legal serialized orders
        assert final != 10.0  # old lost-update outcome impossible
        _storage["users"].pop(uid, None); save_storage_now()
