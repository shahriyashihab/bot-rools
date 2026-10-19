"""HIGH-10/11/16 + HIGH-12 regression tests: guard flows and persistence.

HIGH-10: "Zero All Balances" - one tap used to wipe every balance + the
session tracker with NO confirmation and NO audit log.
HIGH-11: "Disable All Countries" - one tap used to pause verification
globally with no confirmation / log.
HIGH-16: "px_prx_del" - deleted a proxy IMMEDIATELY by index with no digest
guard; a double-tap removed a DIFFERENT proxy.
HIGH-12: the monitor ON/OFF toggle lived only in memory - a restart silently
re-enabled it.

The confirmation/digest flows are handler-internal (pyrogram callbacks), so
these tests enforce the invariants at the source level (the dangerous action
may only ever run from the *_yes executor, which must carry the guard/log)
plus a live persistence test for the monitor toggle.
"""

import re

import pytest

USERS_SRC = "bot/handlers/admin/users.py"
COUNTRIES_SRC = "bot/handlers/admin/countries.py"
PROXY_SRC = "bot/handlers/admin/proxy.py"


def _block(path: str, pattern: str) -> str:
    src = open(path, encoding="utf-8").read()
    m = re.search(pattern, src, re.S)
    assert m, f"pattern not found in {path}: {pattern}"
    return m.group(0)


class TestZeroBalanceGuard:
    def test_tap_does_not_wipe_directly(self):
        block = _block(
            USERS_SRC,
            r"async def admin_action_zerobalance\b.*?(?=async def admin_action_zerobalance_yes)",
        )
        assert "zero_all_balances(" not in block
        assert "zerobalance_yes" in block  # routes to the confirm executor

    def test_executor_wipes_and_logs(self):
        block = _block(
            USERS_SRC,
            r"async def admin_action_zerobalance_yes.*?(?=\n    @app|\Z)",
        )
        assert "zero_all_balances(" in block
        assert "_log_admin_action" in block


class TestDisableCountriesGuard:
    def test_tap_does_not_disable_directly(self):
        block = _block(
            COUNTRIES_SRC,
            r"async def admin_action_disablecountries\b.*?(?=async def admin_action_disablecountries_yes)",
        )
        assert "disable_all_countries(" not in block
        assert "disablecountries_yes" in block

    def test_executor_disables_and_logs(self):
        block = _block(
            COUNTRIES_SRC,
            r"async def admin_action_disablecountries_yes.*?(?=\n    @app|\Z)",
        )
        assert "disable_all_countries(" in block
        assert "_log_admin_action" in block


class TestProxyDeleteGuard:
    def test_tap_does_not_delete_directly(self):
        block = _block(
            PROXY_SRC,
            r"async def px_prx_del_callback.*?(?=async def px_prx_del_yes_callback)",
        )
        assert "remove_country_proxy(" not in block
        assert "px_prx_del_yes_" in block  # routes to the guarded executor
        assert "_url_digest(pool[idx]['url'])" in block  # digest carried

    def test_executor_verifies_digest_and_logs(self):
        block = _block(
            PROXY_SRC,
            r"async def px_prx_del_yes_callback.*?(?=\n    # =|\Z)",
        )
        assert "_url_digest(pool[idx][\"url\"]) != confirmed_digest" in block
        assert "remove_country_proxy(" in block
        assert "_log_admin_action" in block


class TestMonitorTogglePersistence:
    def test_toggle_survives_simulated_restart(self):
        from bot.handlers.admin.router import (
            get_monitor_status,
            toggle_monitor_status,
        )
        from bot.data import state as state_mod

        original = get_monitor_status()
        try:
            new_state = toggle_monitor_status()
            assert new_state != original
            state_mod.load_storage()  # simulate restart
            assert get_monitor_status() == new_state, (
                "monitor ON/OFF lost on restart (HIGH-12 regression)"
            )
        finally:
            set_back = not get_monitor_status()
            from bot.handlers.admin.router import set_monitor_status
            set_monitor_status(original)
            assert get_monitor_status() == original
