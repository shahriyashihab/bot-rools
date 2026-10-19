# bot/handlers/admin/cards.py - Card Name Management (Admin Panel)
#
# Navigation (exactly as specified):
#
#   Admin Panel
#       -> [ Card Name ]
#           -> [ Active Card ]     -> inputs -> Confirm/Cancel
#           -> [ Card Details ]    -> 40 cards/page (20 left + 20 right),
#                                     Search, selected card detail screen
#           -> [ Block Details ]   -> 20 blocked records/page, copy view
#
# There is NO separate main-menu "Block Card" button: blocking happens ONLY
# from the selected card's details screen (Confirm Block / Cancel).
#
# Visual system: bold compact headings, fixed two-column grids with a
# non-clickable placeholder padding the last page, uniform "◀ Back" /
# "Next ▶" nav and short uniform labels - the same rules core_config.py
# uses, so every screen stays perfectly aligned.

import re
import logging
from typing import List, Tuple

from pyrogram.client import Client
from pyrogram import filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from bot.handlers.admin.router import (
    admin_filter,
    admin_state,
    _admin_manager,
    _set_admin_state,
    _edit_callback,
    _panel_reply,
    _log_admin_action,
    ADMIN_TEXT_FILTER,
)
from bot.data.cards import (
    CARD_NAME_TRANSPORT_MAX,
    activate_card,
    all_cards,
    block_card,
    blocked_cards,
    format_card_dt,
    get_card,
    get_card_by_id,
    get_card_statistics,
    is_valid_card_name,
    month_start,
    normalize_card_name,
    period_label,
    set_block_reason,
    today_start,
    week_start,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------
# Fixed grid geometry
#   Card Details : 2 cols x 20 rows = 40 cards per page (20 left + 20 right)
#   Block Details: 2 cols x 10 rows = 20 blocked records per page
# ---------------------------------------------------------------
CD_GRID_COLS = 2
CD_GRID_ROWS = 20
CD_PER_PAGE = CD_GRID_COLS * CD_GRID_ROWS

BD_GRID_COLS = 2
BD_GRID_ROWS = 10
BD_PER_PAGE = BD_GRID_COLS * BD_GRID_ROWS

# Non-clickable placeholder that keeps every page a clean rectangle
# (same trick core_config.py uses for odd last pages).
_PLACEHOLDER = "ㅤ"
_NOOP = "card_noop"

SYMBOL_ACTIVE = "✅"
SYMBOL_BLOCKED = "⛔"

_SEPARATOR = "━━━━━━━━━━━━━━━━━━━━"


def _bi(text: str) -> str:
    """Bold-italic markdown for screen text (same style as core_config)."""
    return f"**__{text}__**"


# Grid buttons must respect Telegram's 64-char BUTTON TEXT limit. The display
# label is a stable, bounded preview only - the real Card Name is never
# altered in storage, always reachable by id in callbacks, and always shown
# in full inside the details and copy views (spec requirement).
_BUTTON_MAX_NAME = 10


def _display_name(name: str, max_len: int = _BUTTON_MAX_NAME) -> str:
    """Bounded display label for buttons (full name stays in details/copy)."""
    name = str(name or "?")
    if len(name) <= max_len:
        return name
    return name[:max_len] + "…"


def _padded_grid(items: List[Tuple[str, str, bool]], cols: int, rows: int) -> List[List[InlineKeyboardButton]]:
    """Build a fixed rectangle grid from (name, id, active) tuples.

    The final page is padded with non-clickable placeholders so the layout
    never goes crooked. The real Card Name is never truncated in data - the
    callback carries the stable short id and the full name is always shown
    in the details and copy views; buttons carry only the bounded display
    label so a very long name can never break the page render.
    """
    per_page = cols * rows
    padded = items + [None] * (per_page - len(items))
    buttons: List[List[InlineKeyboardButton]] = []
    for row in range(rows):
        row_buttons = []
        for col in range(cols):
            item = padded[row * cols + col]
            if item is None:
                row_buttons.append(InlineKeyboardButton(_PLACEHOLDER, callback_data=_NOOP))
            else:
                name, cid, active = item
                sym = SYMBOL_ACTIVE if active else SYMBOL_BLOCKED
                row_buttons.append(
                    InlineKeyboardButton(f"💳 {_display_name(name)} {sym}", callback_data=f"card_v_{cid}")
                )
        buttons.append(row_buttons)
    return buttons


def _pin_card(uid: int, card_id: str) -> None:
    """Pin a card into admin state WITHOUT dropping the panel refs.

    MEDIUM-27: ``admin_state[uid] = {...}`` used to REPLACE the whole dict,
    wiping ``panel_chat_id`` / ``panel_msg_id`` that ``_set_admin_state`` had
    stored - the next ``_panel_reply`` then fell back to a NEW message instead
    of editing the panel in place.
    """
    prev = admin_state.get(uid) or {}
    admin_state[uid] = {
        "action": "card_view",
        "view_card_id": card_id,
        "panel_chat_id": prev.get("panel_chat_id"),
        "panel_msg_id": prev.get("panel_msg_id"),
    }


def _nav_row(page: int, total_pages: int, prefix: str, refresh_data: str = None) -> List[InlineKeyboardButton]:
    """Uniform nav row: Back | page | Next (placeholders keep 3-cell width).

    MEDIUM-43: the middle indicator used to carry a PAGE-1 callback (e.g.
    ``card_details``), so tapping "2/5" silently jumped back to page 1. Pass
    ``refresh_data=None`` (the default now) to make the indicator a no-op.
    """
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Back", callback_data=f"{prefix}_{page - 1}"))
    else:
        nav.append(InlineKeyboardButton(_PLACEHOLDER, callback_data=_NOOP))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=refresh_data or _NOOP))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"{prefix}_{page + 1}"))
    else:
        nav.append(InlineKeyboardButton(_PLACEHOLDER, callback_data=_NOOP))
    return nav


def _back_to_menu_row() -> List[InlineKeyboardButton]:
    return [InlineKeyboardButton("🔙 Back to Card Name", callback_data="card_menu")]


# ---------------------------------------------------------------
# Panel builders (pure functions - unit-testable without a client)
# ---------------------------------------------------------------

def build_card_menu() -> Tuple[str, InlineKeyboardMarkup]:
    """CARD NAME menu: exactly Active Card / Card Details / Block Details."""
    text = _bi("💳 CARD NAME")
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Active Card", callback_data="card_active_menu"),
         InlineKeyboardButton("📋 Card Details", callback_data="card_details")],
        [InlineKeyboardButton("⛔ Block Details", callback_data="card_block_details")],
        [InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="back_admin")],
    ])
    return text, markup


def build_card_details_panel(page: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
    """All cards (active + blocked), 40 per page in a fixed 2x20 grid."""
    cards = all_cards()
    total = len(cards)
    total_pages = max(1, (total + CD_PER_PAGE - 1) // CD_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    page_cards = cards[page * CD_PER_PAGE:(page + 1) * CD_PER_PAGE]

    lines = [
        _bi("📋 CARD DETAILS"),
        _bi(f"Total Cards: {total}"),
        _bi(f"Page: {page + 1}/{total_pages}"),
    ]

    items = [(c.get("card_name", "?"), c.get("id", "?"), c.get("status") == "active") for c in page_cards]
    buttons = _padded_grid(items, CD_GRID_COLS, CD_GRID_ROWS)
    buttons.append(_nav_row(page, total_pages, "card_cd_page"))
    buttons.append([InlineKeyboardButton("🔍 Search Card Name", callback_data="card_search")])
    buttons.append(_back_to_menu_row())
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def build_block_details_panel(page: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
    """Only blocked cards, exactly 20 per page in a fixed 2x10 grid."""
    cards = blocked_cards()
    total = len(cards)
    total_pages = max(1, (total + BD_PER_PAGE - 1) // BD_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    page_cards = cards[page * BD_PER_PAGE:(page + 1) * BD_PER_PAGE]

    lines = [
        _bi("⛔ BLOCK DETAILS"),
        _bi(f"Blocked Cards: {total}"),
        _bi(f"Page: {page + 1}/{total_pages}"),
    ]

    items = [(c.get("card_name", "?"), c.get("id", "?"), False) for c in page_cards]
    buttons = _padded_grid(items, BD_GRID_COLS, BD_GRID_ROWS)
    buttons.append(_nav_row(page, total_pages, "card_bd_page"))
    buttons.append(_back_to_menu_row())
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def build_card_view(card: dict) -> Tuple[str, InlineKeyboardMarkup]:
    """Selected card details: structured fields + status controls + summaries."""
    active = card.get("status") == "active"
    name = card.get("card_name", "?")
    stats = get_card_statistics(name)

    lines = [
        _bi("📋 CARD DETAILS"),
        "",
        f"💳 Card Name: **{name}**",
        f"👤 Owner: {card.get('owner_username') or 'N/A'}",
        f"📢 Channel ID: {card.get('channel_id') or 'N/A'}",
        "",
        f"Status: {SYMBOL_ACTIVE if active else SYMBOL_BLOCKED} **{'ACTIVE' if active else 'BLOCKED'}**",
        "",
        "Activated At:",
        format_card_dt(card.get("activated_at")),
        _SEPARATOR,
        f"Withdrawal Requests: {stats['requests']}",
        f"Approved: {stats['approved']}",
        f"Rejected: {stats['rejected']}",
        f"Pending: {stats['pending']}",
        "",
        f"Requested Balance: ${stats['requested']:,.2f}",
        f"Approved Balance: ${stats['approved_amount']:,.2f}",
        f"Rejected Balance: ${stats['rejected_amount']:,.2f}",
        f"Pending Balance: ${stats['pending_amount']:,.2f}",
        _SEPARATOR,
        f"Total Withdrawn: ${stats['approved_amount']:,.2f}",
    ]

    if active:
        controls = [
            InlineKeyboardButton(f"{SYMBOL_ACTIVE} Active Card", callback_data=_NOOP),
            InlineKeyboardButton(f"{SYMBOL_BLOCKED} Block Card", callback_data="card_block_ask"),
        ]
        back_data = "card_details"
    else:
        controls = [
            InlineKeyboardButton(f"{SYMBOL_ACTIVE} Active Card", callback_data="card_reactivate"),
            InlineKeyboardButton(f"{SYMBOL_BLOCKED} Block Card", callback_data=_NOOP),
        ]
        back_data = "card_block_details"

    markup_rows = [
        controls,
        [InlineKeyboardButton("📅 Today Summary", callback_data="card_sum_today")],
        [InlineKeyboardButton("📅 Last 7 Days Summary", callback_data="card_sum_week")],
        [InlineKeyboardButton("📅 Last 1 Month Summary", callback_data="card_sum_month")],
        [InlineKeyboardButton("🔙 Back to Card Details", callback_data=back_data)],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(markup_rows)


def build_blocked_card_view(card: dict) -> Tuple[str, InlineKeyboardMarkup]:
    """Blocked card screen: block info + stats + copy + reactivate."""
    name = card.get("card_name", "?")
    stats = get_card_statistics(name)

    lines = [
        _bi("⛔ BLOCKED CARD"),
        "",
        f"💳 Card Name: **{name}**",
        f"Status: {SYMBOL_BLOCKED} **BLOCKED**",
        "",
        "👤 Owner:",
        card.get("owner_username") or "N/A",
        "",
        "📢 Channel ID:",
        card.get("channel_id") or "N/A",
        _SEPARATOR,
        "Activated At:",
        format_card_dt(card.get("activated_at")),
        "",
        "Blocked At:",
        format_card_dt(card.get("blocked_at")),
        "",
        "Blocked By:",
        card.get("blocked_by") or "Admin",
        "",
        "Block Reason:",
        card.get("block_reason") or "N/A",
        _SEPARATOR,
        f"Total Requests: {stats['requests']}",
        f"Approved Requests: {stats['approved']}",
        f"Rejected Requests: {stats['rejected']}",
        f"Pending Requests: {stats['pending']}",
        "",
        f"Total Requested: ${stats['requested']:,.2f}",
        f"Total Approved: ${stats['approved_amount']:,.2f}",
        f"Total Rejected: ${stats['rejected_amount']:,.2f}",
        f"Total Pending: ${stats['pending_amount']:,.2f}",
    ]

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copy Block Details", callback_data="card_copy")],
        [InlineKeyboardButton("✅ Activate Card", callback_data="card_reactivate")],
        [InlineKeyboardButton("🔙 Back to Block Details", callback_data="card_block_details")],
    ])
    return "\n".join(lines), markup


def build_block_copy_text(card: dict) -> str:
    """Clean structured copy content (compact code block for 1-click copy)."""
    name = card.get("card_name", "?")
    stats = get_card_statistics(name)
    lines = [
        f"Card Name: {name}",
        "Status: BLOCKED",
        f"Owner: {card.get('owner_username') or 'N/A'}",
        f"Channel ID: {card.get('channel_id') or 'N/A'}",
        f"Activated At: {format_card_dt(card.get('activated_at'))}",
        f"Blocked At: {format_card_dt(card.get('blocked_at'))}",
        f"Blocked By: {card.get('blocked_by') or 'Admin'}",
        f"Block Reason: {card.get('block_reason') or 'N/A'}",
        f"Total Requests: {stats['requests']}",
        f"Approved Requests: {stats['approved']}",
        f"Rejected Requests: {stats['rejected']}",
        f"Total Requested: ${stats['requested']:,.2f}",
        f"Total Approved: ${stats['approved_amount']:,.2f}",
        f"Total Rejected: ${stats['rejected_amount']:,.2f}",
        f"Total Pending: ${stats['pending_amount']:,.2f}",
    ]
    return "```\n" + "\n".join(lines) + "\n```"


def build_summary_view(card: dict, period: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Today / Last 7 Days / Last 1 Month summary for one card."""
    name = card.get("card_name", "?")
    titles = {
        "today": "TODAY SUMMARY",
        "week": "LAST 7 DAYS SUMMARY",
        "month": "LAST 1 MONTH SUMMARY",
    }
    paid_labels = {
        "today": "Paid Today",
        "week": "Paid in 7 Days",
        "month": "Paid in Last Month",
    }
    title = titles.get(period, "SUMMARY")

    if period == "today":
        start = today_start()
    elif period == "week":
        start = week_start()
    else:
        start = month_start()

    stats = get_card_statistics(name, since=start)

    if period == "today":
        period_text = f"Date: {start.strftime('%d %B %Y')}"
    else:
        period_text = f"Period:\n{period_label(start, today_start())}"

    lines = [
        _bi(title),
        _bi(f"CARD: {name}"),
        "",
        period_text,
        _SEPARATOR,
        f"Requests: {stats['requests']}",
        f"Approved: {stats['approved']}",
        f"Rejected: {stats['rejected']}",
        f"Pending: {stats['pending']}",
        "",
        f"Requested: ${stats['requested']:,.2f}",
        f"Approved: ${stats['approved_amount']:,.2f}",
        f"Rejected: ${stats['rejected_amount']:,.2f}",
        f"Pending: ${stats['pending_amount']:,.2f}",
        _SEPARATOR,
        f"{paid_labels.get(period, 'Paid')}: ${stats['approved_amount']:,.2f}",
    ]

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Card", callback_data="card_view_back")],
    ])
    return "\n".join(lines), markup


# ---------------------------------------------------------------
# Registration
# ---------------------------------------------------------------

def register_cards(app: Client):

    # ---------------- Card Name menu ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^card_menu$"))
    async def card_menu_cb(client: Client, query: CallbackQuery):
        await query.answer()
        text, markup = build_card_menu()
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^card_noop$"))
    async def card_noop_cb(client: Client, query: CallbackQuery):
        await query.answer()

    # ---------------- 1. Active Card ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^card_active_menu$"))
    async def card_active_menu_cb(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        await query.answer()
        _set_admin_state(uid, query, "card_name_input")
        await _edit_callback(query,
            _bi("🟢 ACTIVE CARD") + "\n\n"
            + _bi("Card Name:") + "\n"
            + "Send the Card Name\n"
            + "Example: `BT`\n\n"
            + "Rules: uppercase A-Z + digits, min 2 chars,\n"
            + "at least one letter (e.g. BT, A6, 7K).\n\n"
            + "/admin to cancel"
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^card_activate_confirm$"))
    async def card_activate_confirm_cb(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        state = admin_state.get(uid) or {}
        if state.get("action") != "card_confirm_activate" or not state.get("card_name"):
            await query.answer("⚠️ Activation session expired. Start again.", show_alert=True)
            return
        name = state.get("card_name", "")
        owner = state.get("owner_username", "")
        channel = state.get("channel_id", "")
        await query.answer()
        result = activate_card(name, owner_username=owner, channel_id=channel, activated_by=uid)
        _admin_manager.clear_state(uid)

        if result.get("error") == "already_active":
            await _edit_callback(query,
                _bi("⚠️ Card already ACTIVE") + "\n\n"
                + f"💳 **{name}** is already active - no duplicate created.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🟢 Active Card", callback_data="card_active_menu")],
                    _back_to_menu_row(),
                ])
            )
            return

        await _log_admin_action(uid, "card_activate", message=f"{name} owner={owner} channel={channel}")
        verb = "Activated" if result.get("status") == "created" else "Reactivated"
        rec = result.get("record") or {}
        await _edit_callback(query,
            _bi(f"✅ Card {verb}") + "\n\n"
            + f"💳 Card: **{rec.get('card_name', name)}**\n"
            + f"👤 Owner: {rec.get('owner_username') or owner}\n"
            + f"📢 Channel: {rec.get('channel_id') or channel}\n"
            + f"Status: {SYMBOL_ACTIVE} **ACTIVE**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🟢 Active Card", callback_data="card_active_menu")],
                _back_to_menu_row(),
            ])
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^card_activate_cancel$"))
    async def card_activate_cancel_cb(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        await query.answer()
        _admin_manager.clear_state(uid)
        await _edit_callback(query,
            _bi("✅ Activation cancelled."),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🟢 Active Card", callback_data="card_active_menu")],
                _back_to_menu_row(),
            ])
        )

    # ---------------- 2. Card Details ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^card_details$"))
    async def card_details_cb(client: Client, query: CallbackQuery):
        await query.answer()
        text, markup = build_card_details_panel(page=0)
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^card_cd_page_(\d+)$"))
    async def card_cd_page_cb(client: Client, query: CallbackQuery):
        m = re.match(r"^card_cd_page_(\d+)$", str(query.data or ""))
        await query.answer()
        text, markup = build_card_details_panel(page=int(m.group(1)) if m else 0)
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^card_search$"))
    async def card_search_cb(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        await query.answer()
        _set_admin_state(uid, query, "card_search_input")
        await _edit_callback(query,
            _bi("🔍 SEARCH CARD NAME") + "\n\n"
            + _bi("Send Card Name:") + "\n"
            + "Example: `BT`\n\n"
            + "/admin to cancel"
        )

    # ---------------- 3. Block Details ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^card_block_details$"))
    async def card_block_details_cb(client: Client, query: CallbackQuery):
        await query.answer()
        text, markup = build_block_details_panel(page=0)
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^card_bd_page_(\d+)$"))
    async def card_bd_page_cb(client: Client, query: CallbackQuery):
        m = re.match(r"^card_bd_page_(\d+)$", str(query.data or ""))
        await query.answer()
        text, markup = build_block_details_panel(page=int(m.group(1)) if m else 0)
        await _edit_callback(query, text, reply_markup=markup)

    # ---------------- Selected card views ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^card_v_([a-z0-9]+)$"))
    async def card_view_cb(client: Client, query: CallbackQuery):
        m = re.match(r"^card_v_([a-z0-9]+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        card = get_card_by_id(m.group(1))
        if not card:
            await query.answer("⚠️ Card not found.", show_alert=True)
            return
        await query.answer()
        # Pin the card so Block/Reactivate/Copy/Summary act on THIS card.
        _pin_card(query.from_user.id, card["id"])
        text, markup = build_card_view(card)
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^card_bv_([a-z0-9]+)$"))
    async def card_blocked_view_cb(client: Client, query: CallbackQuery):
        m = re.match(r"^card_bv_([a-z0-9]+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        card = get_card_by_id(m.group(1))
        if not card or card.get("status") != "blocked":
            await query.answer("⚠️ Card not found.", show_alert=True)
            return
        await query.answer()
        _pin_card(query.from_user.id, card["id"])
        text, markup = build_blocked_card_view(card)
        await _edit_callback(query, text, reply_markup=markup)

    # ---------------- Block / Reactivate actions ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^card_block_ask$"))
    async def card_block_ask_cb(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        state = admin_state.get(uid) or {}
        card = get_card_by_id(state.get("view_card_id", ""))
        if not card or card.get("status") != "active":
            await query.answer("⚠️ No active card selected.", show_alert=True)
            return
        await query.answer()
        _set_admin_state(uid, query, "card_confirm_block", view_card_id=card["id"])
        await _edit_callback(query,
            _bi("⛔ Block this card?") + "\n\n"
            + f"Card: **{card.get('card_name', '?')}**\n"
            + "New withdrawals will be rejected.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⛔ Confirm Block", callback_data="card_block_confirm"),
                 InlineKeyboardButton("❌ Cancel", callback_data="card_block_cancel")],
            ])
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^card_block_confirm$"))
    async def card_block_confirm_cb(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        state = admin_state.get(uid) or {}
        card = get_card_by_id(state.get("view_card_id", ""))
        if not card or card.get("status") != "active":
            await query.answer("⚠️ Card is no longer active.", show_alert=True)
            return
        await query.answer()
        rec = block_card(card["card_name"], blocked_by=uid)
        _admin_manager.clear_state(uid)
        if rec is None:
            await _edit_callback(query,
                _bi("⚠️ Card could not be blocked (already blocked or missing)."),
                reply_markup=InlineKeyboardMarkup(_back_to_menu_row()))
            return
        await _log_admin_action(uid, "card_block", message=card["card_name"])
        _pin_card(uid, card["id"])
        await _edit_callback(query,
            _bi("✅ Card blocked") + "\n\n"
            + f"💳 **{card['card_name']}** is now BLOCKED.\n"
            + "New withdrawals will be rejected.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✍️ Set Block Reason", callback_data="card_reason_ask")],
                [InlineKeyboardButton("📋 Copy Block Details", callback_data="card_copy")],
                [InlineKeyboardButton("🔙 Back to Block Details", callback_data="card_block_details")],
            ])
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^card_block_cancel$"))
    async def card_block_cancel_cb(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        await query.answer()
        state = admin_state.get(uid) or {}
        card = get_card_by_id(state.get("view_card_id", ""))
        _admin_manager.clear_state(uid)
        if card:
            _pin_card(uid, card["id"])
            text, markup = build_card_view(card)
            await _edit_callback(query, text, reply_markup=markup)
        else:
            text, markup = build_card_menu()
            await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^card_reason_ask$"))
    async def card_reason_ask_cb(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        state = admin_state.get(uid) or {}
        card = get_card_by_id(state.get("view_card_id", ""))
        if not card or card.get("status") != "blocked":
            await query.answer("⚠️ Blocked card not found.", show_alert=True)
            return
        await query.answer()
        _set_admin_state(uid, query, "card_block_reason", view_card_id=card["id"])
        await _edit_callback(query,
            _bi("✍️ BLOCK REASON") + "\n\n"
            + _bi(f"Card: {card.get('card_name', '?')}") + "\n\n"
            + "Send a concise reason.\n"
            + "Examples: Owner changed payment details, Card no longer\n"
            + "available, Invalid channel information, Admin request, Other\n\n"
            + "/admin to cancel"
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^card_reactivate$"))
    async def card_reactivate_cb(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        state = admin_state.get(uid) or {}
        card = get_card_by_id(state.get("view_card_id", ""))
        if not card:
            await query.answer("⚠️ Card not found.", show_alert=True)
            return
        await query.answer()
        result = activate_card(card["card_name"], activated_by=uid)
        _admin_manager.clear_state(uid)
        rec = result.get("record") or card
        await _log_admin_action(uid, "card_reactivate", message=card["card_name"])
        _pin_card(uid, rec["id"])
        text, markup = build_card_view(rec)
        prefix = (
            _bi("⚠️ Card already ACTIVE") + "\n\n"
            if result.get("error") == "already_active"
            else _bi("✅ Card reactivated") + "\n\n"
        )
        await _edit_callback(query, prefix + text, reply_markup=markup)

    # ---------------- Copy + summaries ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^card_copy$"))
    async def card_copy_cb(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        state = admin_state.get(uid) or {}
        card = get_card_by_id(state.get("view_card_id", ""))
        if not card or card.get("status") != "blocked":
            await query.answer("⚠️ Blocked card not found.", show_alert=True)
            return
        await query.answer()
        await _edit_callback(query,
            _bi("📋 COPY BLOCK DETAILS") + "\n\n"
            + build_block_copy_text(card),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Blocked Card", callback_data=f"card_bv_{card['id']}")],
            ])
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^card_sum_(today|week|month)$"))
    async def card_sum_cb(client: Client, query: CallbackQuery):
        m = re.match(r"^card_sum_(today|week|month)$", str(query.data or ""))
        uid = query.from_user.id
        state = admin_state.get(uid) or {}
        card = get_card_by_id(state.get("view_card_id", ""))
        if not card:
            await query.answer("⚠️ Card not found.", show_alert=True)
            return
        await query.answer()
        text, markup = build_summary_view(card, m.group(1) if m else "today")
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^card_view_back$"))
    async def card_view_back_cb(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        state = admin_state.get(uid) or {}
        card = get_card_by_id(state.get("view_card_id", ""))
        await query.answer()
        if not card:
            text, markup = build_card_menu()
            await _edit_callback(query, text, reply_markup=markup)
            return
        _pin_card(uid, card["id"])
        if card.get("status") == "blocked":
            text, markup = build_blocked_card_view(card)
        else:
            text, markup = build_card_view(card)
        await _edit_callback(query, text, reply_markup=markup)

    # ---------------- Card Name text inputs ----------------
    # Runs in the SAME handler group as the other admin text handlers.
    # This module is registered inside register_admin_features(), i.e.
    # BEFORE register_admin_text_fallback(), so pending card inputs are
    # consumed here (same pattern as core_config.py).

    @app.on_message(ADMIN_TEXT_FILTER)
    async def card_text_input(client: Client, message: Message):
        uid = message.from_user.id
        state = admin_state.get(uid)
        if not state:
            message.continue_propagation()
            return
        action = state.get("action", "")
        if action not in {"card_name_input", "card_owner_input", "card_channel_input",
                          "card_search_input", "card_block_reason"}:
            message.continue_propagation()
            return
        message._handled_by_admin = True
        text = (message.text or "").strip()

        try:
            if action == "card_name_input":
                # Exact business rule: min 2 chars, A-Z + 0-9, >=1 letter.
                # Invalid input is rejected - never silently repaired.
                # CARD_NAME_TRANSPORT_MAX is the documented technical bound
                # (NOT a business maximum) - screens embed the full name.
                if len(text) > CARD_NAME_TRANSPORT_MAX:
                    await _panel_reply(client, uid, message,
                        _bi("❌ Card name too long.") + "\n"
                        + "Technical Telegram display limit reached.\n"
                        + "Send a shorter name or /admin to cancel")
                    return
                if not is_valid_card_name(text):
                    await _panel_reply(client, uid, message,
                        _bi("❌ Invalid Card Name.") + "\n"
                        + "Use 2+ characters: uppercase A-Z and digits,\n"
                        + "at least one letter. No spaces/symbols/lowercase.\n"
                        + "Try again or /admin to cancel")
                    return
                state["card_name"] = normalize_card_name(text)
                state["action"] = "card_owner_input"
                admin_state[uid] = state
                await _panel_reply(client, uid, message,
                    _bi("👤 OWNER USERNAME") + "\n\n"
                    + _bi("Send the owner's Telegram username:") + "\n"
                    + "Example: `@card_owner`\n\n"
                    + "/admin to cancel")
                return

            if action == "card_owner_input":
                raw = text.lstrip("@")
                if not re.match(r"^[A-Za-z0-9_]{4,32}$", raw):
                    await _panel_reply(client, uid, message,
                        _bi("❌ Invalid username.") + "\n"
                        + "Example: `@card_owner`\n"
                        + "Try again or /admin to cancel")
                    return
                state["owner_username"] = "@" + raw
                state["action"] = "card_channel_input"
                admin_state[uid] = state
                await _panel_reply(client, uid, message,
                    _bi("📢 CHANNEL ID") + "\n\n"
                    + _bi("Send the Telegram channel ID:") + "\n"
                    + "Example: `-1001234567890`\n\n"
                    + "/admin to cancel")
                return

            if action == "card_channel_input":
                if not (re.match(r"^-?\d{6,}$", text) or re.match(r"^@\w{4,}$", text)):
                    await _panel_reply(client, uid, message,
                        _bi("❌ Invalid channel ID.") + "\n"
                        + "Example: `-1001234567890`\n"
                        + "Try again or /admin to cancel")
                    return
                state["channel_id"] = text
                state["action"] = "card_confirm_activate"
                admin_state[uid] = state
                await _panel_reply(client, uid, message,
                    _bi("Activate this card?") + "\n\n"
                    + f"Card: **{state.get('card_name', '')}**\n"
                    + f"Owner: {state.get('owner_username', '')}\n"
                    + f"Channel: {state.get('channel_id', '')}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Confirm", callback_data="card_activate_confirm"),
                         InlineKeyboardButton("❌ Cancel", callback_data="card_activate_cancel")],
                    ]))
                return

            if action == "card_search_input":
                query_name = normalize_card_name(text)
                card = get_card(query_name)
                _admin_manager.clear_state(uid)
                if not card:
                    await _panel_reply(client, uid, message,
                        _bi("Card not found."),
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔍 Search Again", callback_data="card_search")],
                            [InlineKeyboardButton("🔙 Back to Card Details", callback_data="card_details")],
                        ]))
                    return
                admin_state[uid] = {
                    "action": "card_view",
                    "view_card_id": card["id"],
                    "panel_chat_id": state.get("panel_chat_id"),
                    "panel_msg_id": state.get("panel_msg_id"),
                }
                active = card.get("status") == "active"
                sym = SYMBOL_ACTIVE if active else SYMBOL_BLOCKED
                status_word = "ACTIVE" if active else "BLOCKED"
                await _panel_reply(client, uid, message,
                    _bi("Search Result:"),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            f"💳 {_display_name(card.get('card_name'))} — {sym} {status_word}",
                            callback_data=f"card_v_{card['id']}")],
                        [InlineKeyboardButton("🔙 Back to Card Details", callback_data="card_details")],
                    ]))
                return

            if action == "card_block_reason":
                card = get_card_by_id(state.get("view_card_id", ""))
                if not card or card.get("status") != "blocked":
                    _admin_manager.clear_state(uid)
                    await _panel_reply(client, uid, message,
                        _bi("⚠️ Blocked card not found."),
                        reply_markup=InlineKeyboardMarkup(_back_to_menu_row()))
                    return
                # Spec: "collect a concise reason". Long reasons overflow the
                # details/copy screens; backticks would break the ``` copy
                # block. Both are rejected clearly - nothing is modified.
                if len(text) > 200:
                    await _panel_reply(client, uid, message,
                        _bi("❌ Reason too long.") + "\n"
                        + "Send a concise reason (max 200 characters)\n"
                        + "or /admin to cancel")
                    return
                if "`" in text:
                    await _panel_reply(client, uid, message,
                        _bi("❌ Reason cannot contain backtick characters.")
                        + "\nTry again or /admin to cancel")
                    return
                rec = set_block_reason(card["card_name"], text, by=uid)
                _admin_manager.clear_state(uid)
                await _log_admin_action(uid, "card_block_reason", message=f"{card['card_name']}: {text[:80]}")
                _pin_card(uid, card["id"])
                text2, markup = build_blocked_card_view(rec or card)
                await _panel_reply(client, uid, message, text2, reply_markup=markup)
                return

        except Exception as e:
            await _panel_reply(client, uid, message, _bi(f"❌ Error: {e}"),
                               reply_markup=InlineKeyboardMarkup(_back_to_menu_row()))
            _admin_manager.clear_state(uid)
            return
