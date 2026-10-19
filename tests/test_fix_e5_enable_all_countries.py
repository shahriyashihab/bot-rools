"""E-5 regression tests: "Enable All Countries" must actually persist.

Before the fix, the handler mutated ``open=True`` on every configured country
and called ``save_storage()`` WITHOUT ``_mark_dirty`` - the incremental sync
only writes dirty rows, so NOTHING reached the database and a restart rolled
every country back to closed (live-proven during the audit:
``after reload: open = {'+91': False, '+880': False}``).

The logic now lives in ``bot.data.countries.enable_all_countries`` which
marks each row dirty and persists immediately.
"""

import pytest

from bot.data import _mark_dirty, _storage, save_storage_now
from bot.data.countries import enable_all_countries
from bot.data import state as state_mod


COUNTRIES = {
    "+E5A": {"open": False, "spam_free_rate": 30, "spam_rate": 15, "limit_rate": 10},
    "+E5B": {"open": True, "spam_free_rate": 30, "spam_rate": 15, "limit_rate": 10},
    "+E5C": {"open": False, "permanently_closed": True,
             "spam_free_rate": 1, "spam_rate": 1, "limit_rate": 1},
}


@pytest.fixture()
def countries():
    _storage["countries"].update({k: dict(v) for k, v in COUNTRIES.items()})
    for code in COUNTRIES:
        _mark_dirty("countries", code)
    save_storage_now()
    yield list(COUNTRIES.keys())
    for code in COUNTRIES:
        _storage["countries"].pop(code, None)
    save_storage_now()


def _open_flags() -> dict:
    return {c: _storage["countries"][c].get("open") for c in COUNTRIES}


class TestEnableAllPersistence:
    def test_returns_enabled_and_skipped_counts(self, countries):
        """enabled counts CHANGED rows only (+E5A was already open)."""
        stats = enable_all_countries()
        assert stats["enabled"] == 1  # +E5A flipped False -> True
        assert stats["skipped"] == 1  # +E5C is permanently closed

    def test_all_non_closed_countries_open(self, countries):
        enable_all_countries()
        flags = _open_flags()
        assert flags["+E5A"] is True
        assert flags["+E5B"] is True
        assert flags["+E5C"] is False  # permanently closed: skip, never reopen

    def test_persists_across_simulated_restart(self, countries):
        """THE original bug: before the fix a reload showed every flag False.
        Dirty rows + immediate save must survive a full load from SQLite."""
        enable_all_countries()
        state_mod.load_storage()  # simulate restart
        flags = _open_flags()
        assert flags["+E5A"] is True
        assert flags["+E5B"] is True
        assert flags["+E5C"] is False

    def test_idempotent_second_call(self, countries):
        """Second call changes nothing (already open) and still skips closed."""
        enable_all_countries()
        stats = enable_all_countries()
        assert stats["enabled"] == 0  # nothing left to flip
        assert stats["skipped"] == 1
