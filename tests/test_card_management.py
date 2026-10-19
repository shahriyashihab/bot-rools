"""Focused tests for the Card Name Management System.

Covers: lifecycle (activate / duplicate / block / reactivate / history),
availability gate, statistics windows, fixed-grid pagination geometry,
search normalization and the admin panel navigation contract.
"""

from datetime import datetime, timedelta

import pytest

from bot.data.cards import (
    all_cards,
    block_card,
    blocked_cards,
    get_card,
    get_card_by_id,
    get_card_statistics,
    is_card_available,
    is_valid_card_name,
    month_start,
    normalize_card_name,
    set_block_reason,
    today_start,
    week_start,
    activate_card,
)
from bot.data.state import _storage, _storage_lock
from bot.handlers.admin.menu import get_admin_panel_buttons
from bot.handlers.admin.cards import (
    BD_PER_PAGE,
    CD_PER_PAGE,
    build_block_details_panel,
    build_block_copy_text,
    build_blocked_card_view,
    build_card_details_panel,
    build_card_menu,
    build_card_view,
    build_summary_view,
)


# ---------------------------------------------------------------------------
# Card Name format rule (shared with validate_wallet_address)
# ---------------------------------------------------------------------------

class TestCardNameRule:
    @pytest.mark.parametrize("value", ["BT", "AF", "A6", "7K", "AB", "AB12", "1A", "A123456789"])
    def test_valid(self, value):
        assert is_valid_card_name(value) is True

    @pytest.mark.parametrize("value", ["A", "1", "12", "123456", "a6", "Ab", "A-6", "A_6", "A 6", "A@6", "A.6"])
    def test_invalid(self, value):
        assert is_valid_card_name(value) is False

    def test_normalize_is_case_and_space_tolerant(self):
        assert normalize_card_name("  bt ") == "BT"


# ---------------------------------------------------------------------------
# Lifecycle: activate / duplicate / block / reactivate / history
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_activate_new_card(self):
        result = activate_card("BT", owner_username="@card_owner", channel_id="-1001234567890")
        assert result["status"] == "created"
        rec = result["record"]
        assert rec["card_name"] == "BT"
        assert rec["owner_username"] == "@card_owner"
        assert rec["channel_id"] == "-1001234567890"
        assert rec["status"] == "active"
        assert rec["activated_at"]
        assert rec["blocked_at"] is None
        assert is_card_available("BT") is True

    def test_duplicate_active_not_created(self):
        first = activate_card("DUP1", owner_username="@a", channel_id="-1001234567890")
        second = activate_card("DUP1", owner_username="@b", channel_id="-100999")
        assert second.get("error") == "already_active"
        # Existing record untouched: no owner/channel overwrite.
        rec = get_card("DUP1")
        assert rec["owner_username"] == "@a"
        assert rec["channel_id"] == "-1001234567890"

    def test_block_preserves_record_and_history(self):
        activate_card("BLK1", owner_username="@o", channel_id="-1001234567890")
        rec_before = get_card("BLK1")
        blocked = block_card("BLK1", blocked_by=42, reason="Admin request")
        assert blocked is not None
        assert blocked["status"] == "blocked"
        assert blocked["blocked_by"] == "42"
        assert blocked["block_reason"] == "Admin request"
        assert blocked["blocked_at"]
        assert blocked["activated_at"] == rec_before["activated_at"]
        assert not is_card_available("BLK1")
        # Still visible in the all-cards view and in blocked list.
        assert get_card("BLK1") is not None
        assert any(c["card_name"] == "BLK1" for c in blocked_cards())

    def test_reactivate_reuses_record_and_keeps_history(self):
        activate_card("RE1", owner_username="@o", channel_id="-1001234567890")
        rec1 = get_card("RE1")
        cid = rec1["id"]
        block_card("RE1", blocked_by=42, reason="Card no longer available")
        result = activate_card("RE1")
        assert result["status"] == "reactivated"
        rec2 = get_card("RE1")
        assert rec2["id"] == cid  # same record, no duplicate
        assert rec2["status"] == "active"
        assert rec2["owner_username"] == "@o"
        assert rec2["channel_id"] == "-1001234567890"
        events = [e["event"] for e in rec2["history"]]
        assert events == ["activated", "blocked", "activated"]
        # Old block reason still present inside history.
        assert rec2["history"][1]["reason"] == "Card no longer available"

    def test_block_twice_appends_history_without_erasing(self):
        activate_card("RE2", owner_username="@o", channel_id="-1001234567890")
        block_card("RE2", blocked_by=1, reason="First block")
        activate_card("RE2")
        block_card("RE2", blocked_by=1, reason="Second block")
        rec = get_card("RE2")
        events = [e["event"] for e in rec["history"]]
        assert events == ["activated", "blocked", "activated", "blocked"]
        assert rec["history"][1]["reason"] == "First block"
        assert rec["history"][3]["reason"] == "Second block"

    def test_block_unknown_card_is_noop(self):
        assert block_card("NEVER-EXISTED") is None

    def test_set_block_reason_updates_only_last_block_event(self):
        activate_card("RSN1", owner_username="@o", channel_id="-1001234567890")
        block_card("RSN1", blocked_by=1, reason="First block")
        activate_card("RSN1")
        block_card("RSN1", blocked_by=1, reason="Admin request")
        set_block_reason("RSN1", "Invalid channel information")
        rec = get_card("RSN1")
        assert rec["block_reason"] == "Invalid channel information"
        assert rec["history"][1]["reason"] == "First block"
        assert rec["history"][3]["reason"] == "Invalid channel information"

    def test_stable_short_id_for_callbacks(self):
        activate_card("IDCHK", owner_username="@o", channel_id="-1001234567890")
        rec = get_card("IDCHK")
        assert rec["id"] and len(rec["id"]) <= 12
        assert get_card_by_id(rec["id"])["card_name"] == "IDCHK"


# ---------------------------------------------------------------------------
# Statistics (computed from existing withdrawal records only)
# ---------------------------------------------------------------------------

def _inject_withdrawals(records):
    with _storage_lock:
        _storage["withdrawals"] = records


class TestStatistics:
    @pytest.fixture(autouse=True)
    def _setup(self):
        activate_card("ST1", owner_username="@o", channel_id="-1001234567890")
        now = datetime.now()

        def ts(days_ago):
            return (now - timedelta(days=days_ago)).isoformat()

        _inject_withdrawals([
            {"method": "CARDNAME", "wallet_address": "ST1", "amount": 100.0, "status": "approved", "timestamp": ts(0)},
            {"method": "CARDNAME", "wallet_address": "ST1", "amount": 50.0, "status": "approved", "timestamp": ts(0)},
            {"method": "CARDNAME", "wallet_address": "ST1", "amount": 25.0, "status": "rejected", "timestamp": ts(0)},
            {"method": "CARDNAME", "wallet_address": "ST1", "amount": 10.0, "status": "pending", "timestamp": ts(0)},
            {"method": "CARDNAME", "wallet_address": "st1", "amount": 20.0, "status": "approved", "timestamp": ts(3)},
            {"method": "CARDNAME", "wallet_address": "ST1", "amount": 15.0, "status": "approved", "timestamp": ts(10)},
            {"method": "CARDNAME", "wallet_address": "ST1", "amount": 40.0, "status": "approved", "timestamp": ts(40)},
            # Must NOT count toward ST1:
            {"method": "CARDNAME", "wallet_address": "OTHER", "amount": 999.0, "status": "approved", "timestamp": ts(0)},
            {"method": "USDT", "wallet_address": "ST1", "amount": 888.0, "status": "approved", "timestamp": ts(0)},
        ])
        yield

    def test_all_time_totals(self):
        s = get_card_statistics("ST1")
        assert s["requests"] == 7
        assert s["approved"] == 5
        assert s["rejected"] == 1
        assert s["pending"] == 1
        assert s["requested"] == pytest.approx(260.0)
        assert s["approved_amount"] == pytest.approx(225.0)
        assert s["rejected_amount"] == pytest.approx(25.0)
        assert s["pending_amount"] == pytest.approx(10.0)

    def test_today_window(self):
        s = get_card_statistics("ST1", since=today_start())
        assert s["requests"] == 4
        assert s["requested"] == pytest.approx(185.0)
        assert s["approved_amount"] == pytest.approx(150.0)

    def test_last_7_days_window(self):
        s = get_card_statistics("ST1", since=week_start())
        assert s["requests"] == 5
        assert s["requested"] == pytest.approx(205.0)

    def test_last_month_window(self):
        s = get_card_statistics("ST1", since=month_start())
        assert s["requests"] == 6
        assert s["requested"] == pytest.approx(220.0)
        assert s["approved_amount"] == pytest.approx(185.0)

    def test_other_cards_and_methods_not_counted(self):
        s = get_card_statistics("OTHER")
        assert s["requests"] == 1 and s["approved_amount"] == pytest.approx(999.0)
        assert get_card_statistics("NEVER")["requests"] == 0


# ---------------------------------------------------------------------------
# Pagination geometry - fixed grids (20 left + 20 right = 40 / 20 per page)
# ---------------------------------------------------------------------------

def _card_buttons(markup):
    """All non-placeholder card buttons of a grid page."""
    out = []
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.callback_data and btn.callback_data.startswith("card_v_"):
                out.append(btn)
    return out


@pytest.fixture(autouse=True)
def _fresh_card_state():
    """Hermetic per-test state: no cross-class card/withdrawal pollution."""
    with _storage_lock:
        _storage["cards"] = {}
        _storage["withdrawals"] = []
    yield
    with _storage_lock:
        _storage["cards"] = {}
        _storage["withdrawals"] = []


class TestPagination:
    @pytest.fixture(autouse=True)
    def _many_cards(self):
        self.ids = []
        for i in range(65):
            name = f"PG{i:03d}A"  # always >= 2 chars with a letter
            activate_card(name, owner_username="@o", channel_id="-1001234567890")
            self.ids.append(name)
        yield

    def test_card_details_page_1_is_40_cards_in_20_rows_of_2(self):
        text, markup = build_card_details_panel(page=0)
        buttons = _card_buttons(markup)
        assert CD_PER_PAGE == 40
        assert len(buttons) == 40
        card_rows = markup.inline_keyboard[:20]
        assert len(card_rows) == 20  # card grid rows (nav/search/back follow)
        for row in card_rows:
            assert len(row) == 2

    def test_card_details_page_2_keeps_rectangle(self):
        text, markup = build_card_details_panel(page=1)
        buttons = _card_buttons(markup)
        assert len(buttons) == 25  # 65 total
        # Padding keeps every card row 2 cells wide.
        card_rows = markup.inline_keyboard[:20]
        assert len(card_rows) == 20
        for row in card_rows:
            assert len(row) == 2

    def test_card_details_header_counts_and_pages(self):
        # NOTE: counts are cumulative across the whole test class fixture
        # (65 spec cards + earlier test classes' cards live in the same DB),
        # so assert against the CURRENT total, not a magic number.
        total = len(all_cards())
        text, _ = build_card_details_panel(page=0)
        assert f"Total Cards: {total}" in text
        assert "Page: 1/2" in text
        text2, _ = build_card_details_panel(page=1)
        assert "Page: 2/2" in text2

    def test_card_details_search_button_and_back(self):
        _, markup = build_card_details_panel(page=0)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "card_search" in callbacks
        assert "card_menu" in callbacks

    def test_block_details_exactly_20_per_page(self):
        # Block the first 25 cards -> 2 pages: 20 + 5.
        for name in self.ids[:25]:
            block_card(name)
        assert BD_PER_PAGE == 20
        text, markup = build_block_details_panel(page=0)
        buttons = _card_buttons(markup)
        assert len(buttons) == 20
        block_rows = markup.inline_keyboard[:10]  # 2 cols x 10 rows
        assert len(block_rows) == 10
        for row in block_rows:
            assert len(row) == 2
        text2, markup2 = build_block_details_panel(page=1)
        assert len(_card_buttons(markup2)) == 5
        assert "Page: 2/2" in text2

    def test_block_details_excludes_active_cards(self):
        for name in self.ids[:3]:
            block_card(name)
        text, markup = build_block_details_panel(page=0)
        buttons = _card_buttons(markup)
        names = [b.text for b in buttons]
        assert len(names) == 3
        assert all("⛔" in n for n in names)


# ---------------------------------------------------------------------------
# Screens and navigation contract
# ---------------------------------------------------------------------------

class TestScreens:
    def test_admin_panel_has_card_name_button(self):
        markup = get_admin_panel_buttons()
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert "card_menu" in callbacks
        assert any("Card Name" in label for label in labels)

    def test_no_separate_block_card_main_menu_button(self):
        markup = get_admin_panel_buttons()
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert not any(label.strip() == "Block Card" for label in labels)

    def test_card_menu_has_exactly_three_sections(self):
        text, markup = build_card_menu()
        rows = markup.inline_keyboard
        callbacks = [b.callback_data for row in rows for b in row]
        assert callbacks == [
            "card_active_menu", "card_details",   # row 1
            "card_block_details",                 # row 2
            "back_admin",                         # row 3
        ]

    def test_card_view_screen_fields(self):
        activate_card("VIEW1", owner_username="@owner_x", channel_id="-1001234567890")
        rec = get_card("VIEW1")
        text, markup = build_card_view(rec)
        assert "VIEW1" in text
        assert "@owner_x" in text
        assert "-1001234567890" in text
        assert "ACTIVE" in text
        assert "Activated At:" in text
        assert "Withdrawal Requests:" in text
        assert "Requested Balance:" in text
        assert "Total Withdrawn:" in text
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "card_block_ask" in callbacks  # Block actionable when active
        assert "card_sum_today" in callbacks
        assert "card_sum_week" in callbacks
        assert "card_sum_month" in callbacks

    def test_blocked_card_screen_fields(self):
        activate_card("VIEW2", owner_username="@owner_y", channel_id="-1001234567891")
        block_card("VIEW2", blocked_by=7, reason="Owner changed payment details")
        rec = get_card("VIEW2")
        text, markup = build_blocked_card_view(rec)
        assert "BLOCKED" in text
        assert "Owner changed payment details" in text
        assert "Blocked At:" in text
        assert "Blocked By:" in text
        assert "Total Requested:" in text
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "card_copy" in callbacks
        assert "card_reactivate" in callbacks
        assert "card_block_details" in callbacks

    def test_copy_text_is_clean_and_structured(self):
        activate_card("VIEW3", owner_username="@o", channel_id="-1001234567890")
        block_card("VIEW3", blocked_by=7, reason="Admin request")
        rec = get_card("VIEW3")
        copy = build_block_copy_text(rec)
        for line in (
            "Card Name: VIEW3", "Status: BLOCKED", "Owner: @o",
            "Channel ID: -1001234567890", "Activated At:", "Blocked At:",
            "Blocked By: 7", "Block Reason: Admin request",
            "Total Requests: 0", "Total Requested: $0.00",
        ):
            assert line in copy
        assert copy.startswith("```") and copy.endswith("```")

    def test_summary_views_render(self):
        activate_card("SUM1", owner_username="@o", channel_id="-1001234567890")
        rec = get_card("SUM1")
        for period, title in (("today", "TODAY SUMMARY"),
                              ("week", "LAST 7 DAYS SUMMARY"),
                              ("month", "LAST 1 MONTH SUMMARY")):
            text, markup = build_summary_view(rec, period)
            assert title in text
            assert "CARD: SUM1" in text
            assert "Requests:" in text
            assert "Paid" in text
            assert "card_view_back" in [b.callback_data for row in markup.inline_keyboard for b in row]
