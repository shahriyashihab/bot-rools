"""MEDIUM #43/#44/#45 regression tests: indicators are no-ops + pagination.

#43: page-indicator buttons ("2/5") used to carry a PAGE-1 callback and
reset the list. Now they are no-ops across all screens.
#44: View All Countries paginates (20/page) instead of head-truncating.
#45: Total Users (50/page) and Ban List (30/page) paginate; the phone
search truncation is explicit.
"""

import re

import pytest

from bot.data import _storage


def _src(path):
    return open(path, encoding="utf-8").read()


class TestIndicatorNoOps:
    def test_cards_indicators_noop(self):
        src = _src("bot/handlers/admin/cards.py")
        assert 'nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=refresh_data or _NOOP))' in src
        assert '_nav_row(page, total_pages, "card_cd_page")' in src
        assert '_nav_row(page, total_pages, "card_bd_page")' in src

    def test_proxy_indicator_noop(self):
        src = _src("bot/handlers/admin/proxy.py")
        assert 'callback_data="px_noop"))' in src
        assert 'InlineKeyboardButton(f"{page}/{total_pages}", callback_data="px_country_add")' not in src

    def test_country_export_indicator_noop(self):
        src = _src("bot/handlers/admin/country_export.py")
        assert 'InlineKeyboardButton(f"{page}/{total_pages}", callback_data="country_export_menu")' not in src

    def test_pending_withdraw_indicator_noop_with_handler(self):
        handler_src = _src("bot/handlers/admin/withdraw.py")
        menu_src = _src("bot/handlers/admin/menu.py")
        # the button lives in menu.py's nav row; the no-op handler in withdraw.py
        assert 'InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="wd_noop")' in menu_src
        assert re.search(r"filters\.regex\(r\"\^wd_noop\$\"\)", handler_src)
        assert 'callback_data="pendingwithdraw"))' not in menu_src

    def test_core_config_indicator_keeps_same_page(self):
        """core_config's indicator navigates to ITS OWN page (correct)."""
        src = _src("bot/handlers/admin/core_config.py")
        assert 'callback_data=f"cc_list_{page}"' in src


class TestViewAllCountriesPagination:
    def test_handler_is_paginated(self):
        src = _src("bot/handlers/admin/countries.py")
        assert re.search(r"\^view_all_countries\$\|\^vac_page_\\d\+\$", src)
        assert "vac_noop" in src
        assert "page_lines = lines[page * per_page:(page + 1) * per_page]" in src
        # the old hard truncation must be gone
        assert 'lines[:20]' not in src


class TestUserListsPagination:
    def test_totalusers_paginated(self):
        src = _src("bot/handlers/admin/users.py")
        assert re.search(r"\^totalusers\$\|\^tu_page_\\d\+\$", src)
        assert "tu_noop" in src
        assert "users_list[page * per_page:(page + 1) * per_page]" in src
        assert "+ {len(users_list) - 50} more users..." not in src

    def test_ban_list_paginated(self):
        src = _src("bot/handlers/admin/users.py")
        assert re.search(r"\^ban_list\$\|\^bl_page_\\d\+\$", src)
        assert "bl_noop" in src
        assert "banned_users[page * per_page:(page + 1) * per_page]" in src
        assert "... and {len(banned_users) - 30} more" not in src

    def test_phone_search_truncation_is_explicit(self):
        src = _src("bot/handlers/admin/users.py")
        assert "Showing first 20 of" in src
