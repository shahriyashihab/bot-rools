# bot/handlers/admin/core_config.py
"""Country Pricing -> CORE CONFIG -> Selected Country Configuration.

A country SELECTOR followed by a ONE-COUNTRY configuration screen. This is
NOT a bulk all-countries control: every callback below carries the selected
country code, and every read/write is scoped to exactly that country.

Flow (all in-place edits of the same panel message):
    Country Pricing [ CORE CONFIG ]
      -> Open-country grid (2 columns x 10 rows = 20 per page)
        -> Select one country -> that country's configuration screen
          -> [ Back to Countries ] -> grid (same page) -> another country ...

Design rules honoured here:
  - Grid labels are ALWAYS "FLAG + SPACE + 4-char uppercase code" from the
    frozen mapping in bot/utils/country_codes.py (never runtime-truncated).
  - Only OPEN countries are listed (existing get_open_countries semantics;
    capacity 0 = unlimited and is still an open country).
  - Toggles use the unified "✅ Feature: ON / ❌ Feature: OFF" format from
    design.sw, refresh the same screen in place, and never show a popup.
  - Two-button rows use equal-width labels (50/50); single buttons are one
    per row (100%).
  - Screen text uses bold-italic (**__...__**) markdown.

Callback namespaces (all new, none collide with existing ones):
    core_config, cc_list_<page>, cc_cfg_<code>, cc_g_<page>_<code>
    cc_rate_in_<type>_<code>, cc_all_in_<code>, cc_cap_in_<code>,
    cc_time_in_<code>, cc_px_in_<code>,
    cc_t_<flag>_<code>, cc_platform_<code>,
    cc_api_set_<code>, cc_api_del_<code>, cc_api_del_yes_<code>,
    cc_upload_<what>_<code>, cc_names_clear_<code>, cc_bios_clear_<code>,
    cc_cancel_<code>
"""

import re
import logging
from typing import Dict, List, Optional, Tuple

from pyrogram.client import Client
from pyrogram import filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from bot.handlers.admin.router import (
    admin_filter,
    admin_state,
    _admin_manager,
    _log_admin_action,
    _set_admin_state,
    _edit_callback,
    _panel_reply,
    NON_TEXT_INPUT_COMMANDS,
)
from bot.utils.storage import (
    _storage,
    save_storage,
    get_open_countries,
    get_country_rates,
    set_country_rate_by_type,
    set_country_rates,
    set_country_timeout,
    get_country_capacity,
    get_country_timeout,
    set_country_setting_bool,
    get_country_setting_bool,
    set_country_platform,
    get_country_platform,
    set_country_custom_api,
    get_country_custom_api,
    remove_country_custom_api,
    get_country_name_list,
    set_country_name_list,
    get_country_bio_list,
    set_country_bio_list,
    set_country_capacity,
    get_country_photo_enabled,
    get_country_photo_count,
    get_country_photos_used,
    get_country_photo_zip_meta,
    import_country_photos_zip,
    remove_country_photos,
    set_country_photo_zip_meta,
)
from bot.utils.country_data import get_flag, get_country_info
from bot.utils.country_codes import get_country_display_code
from bot.data.proxies import get_country_proxy_config

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
#  Grid geometry - FIXED: 2 columns x 10 rows = 20 countries per page.
# ------------------------------------------------------------------
CC_GRID_COLS = 2
CC_GRID_ROWS = 10
CC_PER_PAGE = CC_GRID_COLS * CC_GRID_ROWS

# Stable ordering: the natural sorted order of the country codes (existing
# storage key order, lexicographic). Never re-shuffled between refreshes.
def _sorted_open_countries() -> List[str]:
    return sorted(get_open_countries())

# Non-clickable placeholder that keeps an odd last page visually balanced.
_CC_PLACEHOLDER = "ㅤ"


def _bi(text: str) -> str:
    """Bold-italic markdown for screen text."""
    return f"**__{text}__**"


def _sw(name: str, state: bool) -> str:
    """Unified switch label: '✅ Name: ON' / '❌ Name: OFF'."""
    return f"{'✅' if state else '❌'} {name}: {'ON' if state else 'OFF'}"


# ------------------------------------------------------------------
#  Panel builders
# ------------------------------------------------------------------

def _build_core_config_grid(page: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
    """CORE CONFIG screen: header + fixed 2-column grid of open countries."""
    countries = _sorted_open_countries()
    total = len(countries)
    total_pages = max(1, (total + CC_PER_PAGE - 1) // CC_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * CC_PER_PAGE
    page_codes = countries[start:start + CC_PER_PAGE]

    lines = [
        _bi("🧩 CORE CONFIG"),
        _bi(f"Open Countries: {total}"),
        _bi(f"Page: {page + 1}/{total_pages}"),
        "",
        _bi("Select a country to configure:"),
    ]

    # Pad the final page so the grid stays a clean rectangle.
    padded = page_codes + [None] * (CC_PER_PAGE - len(page_codes))
    buttons = []
    for row in range(CC_GRID_ROWS):
        row_buttons = []
        for col in range(CC_GRID_COLS):
            code = padded[row * CC_GRID_COLS + col]
            if code is None:
                row_buttons.append(InlineKeyboardButton(_CC_PLACEHOLDER, callback_data="cc_noop"))
                continue
            flag = get_flag(code)
            short = get_country_display_code(code)
            row_buttons.append(
                InlineKeyboardButton(f"{flag} {short}", callback_data=f"cc_g_{page}_{code}")
            )
        buttons.append(row_buttons)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Back", callback_data=f"cc_list_{page - 1}"))
    else:
        nav.append(InlineKeyboardButton("ㅤ", callback_data="cc_noop"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"cc_list_{page}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"cc_list_{page + 1}"))
    else:
        nav.append(InlineKeyboardButton("ㅤ", callback_data="cc_noop"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("◀ Back to Country & Pricing", callback_data="menu_country")])

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _capacity_display(capacity: int) -> str:
    """'1000' or '∞' for capacity 0 (existing semantics: 0 = unlimited)."""
    try:
        capacity = int(capacity or 0)
    except (TypeError, ValueError):
        capacity = 0
    return f"{capacity:,}" if capacity else "∞"


def _proxy_display(country_code: str) -> str:
    """Compact proxy status without leaking credentials."""
    try:
        cfg = get_country_proxy_config(country_code)
    except Exception:
        cfg = None
    if not cfg or not cfg.get("enabled", False):
        return "Direct"
    pool = cfg.get("proxies") or []
    n = len(pool)
    if n == 0:
        return "Direct"
    return f"{n} configured"


def _platform_display(country_code: str) -> str:
    platform = (get_country_platform(country_code) or "").strip().lower()
    if platform == "android":
        return "Android"
    if platform == "ios":
        return "iOS"
    return "Auto"


def _api_display(country_code: str) -> str:
    api = get_country_custom_api(country_code)
    if not api:
        return "Default"
    return f"Custom #{api.get('api_id')}"


def _build_country_panel(country_code: str, page: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
    """Selected-country configuration screen (header + full control grid)."""
    name, flag = get_country_info(country_code)
    short = get_country_display_code(country_code)
    rates = get_country_rates(country_code)
    capacity = get_country_capacity(country_code)
    timeout = get_country_timeout(country_code)
    is_open = bool((_storage.get("countries", {}).get(country_code) or {}).get("open", False))

    accept_restricted = get_country_setting_bool(country_code, "accept_restricted")
    accept_spam = get_country_setting_bool(country_code, "accept_spam")
    twofa = get_country_setting_bool(country_code, "twofa_check")
    device = get_country_setting_bool(country_code, "device_check")
    accept_gmail = get_country_setting_bool(country_code, "accept_gmail")
    name_on = get_country_setting_bool(country_code, "name_enabled")
    bio_on = get_country_setting_bool(country_code, "bio_enabled")
    photo_on = get_country_photo_enabled(country_code)
    platform = _platform_display(country_code)

    header = "\n".join([
        _bi(f"{flag} {name}"),
        _bi(f"Country: {short} | Code: {country_code}"),
        _bi(f"Status: {'✅ ON' if is_open else '❌ OFF'}"),
        _bi(f"Capacity: {_capacity_display(capacity)}"),
        _bi(f"Time: {timeout}s"),
        "",
        _bi(f"Free Rate: {rates.get('spam_free_rate', 0):g}%"),
        _bi(f"Spam Rate: {rates.get('spam_rate', 0):g}%"),
        _bi(f"Limit Rate: {rates.get('limit_rate', 0):g}%"),
        "",
        _bi(f"Platform: {platform} | API: {_api_display(country_code)}"),
        _bi(f"Proxy: {_proxy_display(country_code)}"),
    ])

    buttons = [
        # Pricing (equal 50/50 pairs)
        [InlineKeyboardButton("Price Free", callback_data=f"cc_rate_in_free_{country_code}"),
         InlineKeyboardButton("Price Limit", callback_data=f"cc_rate_in_limit_{country_code}")],
        [InlineKeyboardButton("Price Spam", callback_data=f"cc_rate_in_spam_{country_code}"),
         InlineKeyboardButton("Price F/S/L", callback_data=f"cc_all_in_{country_code}")],
        # Capacity & time
        [InlineKeyboardButton("Edit Capacity", callback_data=f"cc_cap_in_{country_code}"),
         InlineKeyboardButton("Edit Time", callback_data=f"cc_time_in_{country_code}")],
        [InlineKeyboardButton("Set Proxy", callback_data=f"cc_px_in_{country_code}")],
        # Acceptance toggles (labels carry the state, refresh in place)
        [InlineKeyboardButton(_sw("Accept Restricted", accept_restricted), callback_data=f"cc_t_accept_restricted_{country_code}"),
         InlineKeyboardButton(_sw("Accept Spam", accept_spam), callback_data=f"cc_t_accept_spam_{country_code}")],
        [InlineKeyboardButton(_sw("2FA Check", twofa), callback_data=f"cc_t_twofa_check_{country_code}"),
         InlineKeyboardButton(_sw("Device Check", device), callback_data=f"cc_t_device_check_{country_code}")],
        [InlineKeyboardButton(_sw("Accept Gmail", accept_gmail), callback_data=f"cc_t_accept_gmail_{country_code}")],
        # Platform & API
        [InlineKeyboardButton(f"Platform: {platform}", callback_data=f"cc_platform_{country_code}")],
        [InlineKeyboardButton("Set Custom API", callback_data=f"cc_api_set_{country_code}"),
         InlineKeyboardButton("Remove Custom API", callback_data=f"cc_api_del_{country_code}")],
        # Profile data (Name / Bio / Photo - per the admin's layout)
        [InlineKeyboardButton(_sw("Name", name_on), callback_data=f"cc_t_name_enabled_{country_code}"),
         InlineKeyboardButton(_sw("Bio", bio_on), callback_data=f"cc_t_bio_enabled_{country_code}")],
        [InlineKeyboardButton("Upload Name TXT", callback_data=f"cc_upload_names_{country_code}"),
         InlineKeyboardButton("Upload Bio TXT", callback_data=f"cc_upload_bios_{country_code}")],
        [InlineKeyboardButton(_sw("Photo", photo_on), callback_data=f"cc_t_photo_enabled_{country_code}"),
         InlineKeyboardButton("Remove: N/B/P", callback_data=f"cc_remove_{country_code}")],
        [InlineKeyboardButton("Upload Photo ZIP", callback_data=f"cc_upload_photos_{country_code}"),
         InlineKeyboardButton("Details: N/B/P/F", callback_data=f"cc_details_{country_code}")],
        [InlineKeyboardButton("◀ Back to Countries", callback_data=f"cc_list_{page}")],
    ]

    return header, InlineKeyboardMarkup(buttons)


def _cc_details_view_build(country_code: str):
    """Build the Details: N/B/P/F overview screen (used by the handler and tests)."""
    names_count = len(get_country_name_list(country_code))
    bios_count = len(get_country_bio_list(country_code))
    photos_count = get_country_photo_count(country_code)
    photos_used = get_country_photos_used(country_code)
    meta = get_country_photo_zip_meta(country_code)

    body = "\n".join([
        _bi(f"Name list: {names_count:,} lines"),
        _bi(f"Bio list: {bios_count:,} lines"),
        _bi(f"Photos: {photos_count:,} (used: {photos_used:,})"),
        _bi("ZIP: " + (f"{meta.get('name', '?')} ({meta.get('size_mb', 0):g} MB, "
                       f"{meta.get('imported', 0):,} images)" if meta else "none")),
    ])
    return body, None


def _store_page(country_code: str, page: int) -> int:
    """Remember the grid page the admin came from (for Back to Countries)."""
    try:
        return max(0, int(page))
    except (TypeError, ValueError):
        return 0


def _page_from_state(user_id: int, country_code: str, fallback: int = 0) -> int:
    state = admin_state.get(user_id)
    if state and state.get("country_code") == country_code:
        try:
            return max(0, int(state.get("grid_page", fallback)))
        except (TypeError, ValueError):
            return fallback
    return fallback


# ------------------------------------------------------------------
#  Registration
# ------------------------------------------------------------------

def register_core_config(app: Client):

    # --------------------------------------------------------------
    #  CORE CONFIG entry + country grid
    # --------------------------------------------------------------

    @app.on_callback_query(admin_filter & filters.regex(r"^core_config$"))
    async def core_config_entry(client: Client, query: CallbackQuery):
        await query.answer()
        _admin_manager.clear_state(query.from_user.id)
        text, markup = _build_core_config_grid(page=0)
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_noop$"))
    async def cc_noop(client: Client, query: CallbackQuery):
        await query.answer()

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_list_(\d+)$"))
    async def cc_list_page(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_list_(\d+)$", str(query.data or ""))
        page = int(m.group(1)) if m else 0
        await query.answer()
        text, markup = _build_core_config_grid(page=page)
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_g_(\d+)_(\+\d+)$"))
    async def cc_open_country(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_g_(\d+)_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        page = int(m.group(1))
        country_code = m.group(2)
        uid = query.from_user.id
        # Existence check BEFORE answering (a query can be answered only once).
        if country_code not in _storage.get("countries", {}):
            await query.answer("⚠️ This country no longer exists.", show_alert=True)
            return
        await query.answer()
        # Remember where the admin came from so Back to Countries lands here.
        admin_state[uid] = {
            "action": "core_config_view",
            "country_code": country_code,
            "grid_page": page,
        }
        text, markup = _build_country_panel(country_code, page=page)
        await _edit_callback(query, text, reply_markup=markup)

    # --------------------------------------------------------------
    #  Rate inputs
    # --------------------------------------------------------------

    _RATE_TYPES = {"free": "spam_free_rate", "spam": "spam_rate", "limit": "limit_rate"}
    _RATE_NAMES = {"free": "Free", "spam": "Spam", "limit": "Limit"}

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_rate_in_(free|spam|limit)_(\+\d+)$"))
    async def cc_rate_input(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_rate_in_(free|spam|limit)_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        rate_key, country_code = m.group(1), m.group(2)
        uid = query.from_user.id
        await query.answer()
        _set_admin_state(uid, query, "cc_rate_input",
                         country_code=country_code, rate_key=_RATE_TYPES[rate_key],
                         rate_kind=rate_key, grid_page=_page_from_state(uid, country_code))
        short = get_country_display_code(country_code)
        await _edit_callback(query,
            _bi(f"💰 Price {_RATE_NAMES[rate_key]} — {short}") + "\n\n"
            + _bi(f"Enter the new {_RATE_NAMES[rate_key]} rate for {country_code} only:")
            + "\n`25`  (0 = free)\n\n/admin to cancel",
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_all_in_(\+\d+)$"))
    async def cc_all_rates_input(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_all_in_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        country_code = m.group(1)
        uid = query.from_user.id
        await query.answer()
        _set_admin_state(uid, query, "cc_all_rates_input",
                         country_code=country_code,
                         grid_page=_page_from_state(uid, country_code))
        short = get_country_display_code(country_code)
        await _edit_callback(query,
            _bi(f"💰 Price F/S/L — {short}") + "\n\n"
            + _bi(f"Enter all three rates for {country_code} only:")
            + "\n`30 15 10`\n(Free Spam Limit)\n\n/admin to cancel",
        )

    # --------------------------------------------------------------
    #  Capacity / time / proxy inputs
    # --------------------------------------------------------------

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_cap_in_(\+\d+)$"))
    async def cc_capacity_input(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_cap_in_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        country_code = m.group(1)
        uid = query.from_user.id
        await query.answer()
        _set_admin_state(uid, query, "cc_capacity_input",
                         country_code=country_code,
                         grid_page=_page_from_state(uid, country_code))
        short = get_country_display_code(country_code)
        current = get_country_capacity(country_code)
        await _edit_callback(query,
            _bi(f"🎯 Edit Capacity — {short}") + "\n\n"
            + _bi(f"Current: {_capacity_display(current)}")
            + "\n\n" + _bi(f"New capacity for {country_code} only:")
            + "\n`1000`  (0 = unlimited)\n\n/admin to cancel",
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_time_in_(\+\d+)$"))
    async def cc_time_input(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_time_in_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        country_code = m.group(1)
        uid = query.from_user.id
        await query.answer()
        _set_admin_state(uid, query, "cc_time_input",
                         country_code=country_code,
                         grid_page=_page_from_state(uid, country_code))
        short = get_country_display_code(country_code)
        current = get_country_timeout(country_code)
        await _edit_callback(query,
            _bi(f"⏱️ Edit Time — {short}") + "\n\n"
            + _bi(f"Current: {current}s")
            + "\n\n" + _bi(f"New timeout in seconds for {country_code} only:")
            + "\n`60`  (1–3600)\n\n/admin to cancel",
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_px_in_(\+\d+)$"))
    async def cc_proxy_input(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_px_in_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        country_code = m.group(1)
        uid = query.from_user.id
        await query.answer()
        _set_admin_state(uid, query, "cc_proxy_input",
                         country_code=country_code,
                         grid_page=_page_from_state(uid, country_code))
        short = get_country_display_code(country_code)
        await _edit_callback(query,
            _bi(f"🌐 Set Proxy — {short}") + "\n\n"
            + _bi(f"Send the proxy URL for {country_code} only:")
            + "\n`socks5://user:pass@host:port`\n\n/admin to cancel",
        )

    # --------------------------------------------------------------
    #  Safe toggles - refresh in place, no success popups
    # --------------------------------------------------------------

    _TOGGLE_KEYS = ("accept_restricted", "accept_spam", "twofa_check",
                    "device_check", "accept_gmail", "name_enabled", "bio_enabled")

    @app.on_callback_query(admin_filter & filters.regex(
        r"^cc_t_(accept_restricted|accept_spam|twofa_check|device_check|accept_gmail|name_enabled|bio_enabled|photo_enabled)_(\+\d+)$"))
    async def cc_toggle(client: Client, query: CallbackQuery):
        m = re.match(
            r"^cc_t_(accept_restricted|accept_spam|twofa_check|device_check|accept_gmail|name_enabled|bio_enabled|photo_enabled)_(\+\d+)$",
            str(query.data or ""))
        if not m:
            await query.answer()
            return
        flag_key, country_code = m.group(1), m.group(2)
        uid = query.from_user.id
        await query.answer()
        new_state = not get_country_setting_bool(country_code, flag_key)
        ok = set_country_setting_bool(country_code, flag_key, new_state)
        if not ok:
            await query.answer("⚠️ Country not found.", show_alert=True)
            return
        await _log_admin_action(uid, "cc_toggle",
                                message=f"{country_code} {flag_key}={'ON' if new_state else 'OFF'}")
        # Rebuild + edit the SAME screen (state visible in the button itself).
        page = _page_from_state(uid, country_code)
        admin_state[uid] = {
            "action": "core_config_view",
            "country_code": country_code,
            "grid_page": page,
        }
        text, markup = _build_country_panel(country_code, page=page)
        await _edit_callback(query, text, reply_markup=markup)

    # --------------------------------------------------------------
    #  Platform cycle: Auto -> Android -> iOS -> Auto
    # --------------------------------------------------------------

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_platform_(\+\d+)$"))
    async def cc_platform_cycle(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_platform_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        country_code = m.group(1)
        uid = query.from_user.id
        await query.answer()
        current = (get_country_platform(country_code) or "").lower()
        cycle = {"": "android", "android": "ios", "ios": ""}
        next_platform = cycle.get(current, "")
        ok = set_country_platform(country_code, next_platform)
        if not ok:
            await query.answer("⚠️ Country not found.", show_alert=True)
            return
        await _log_admin_action(uid, "cc_platform",
                                message=f"{country_code} platform={next_platform or 'auto'}")
        page = _page_from_state(uid, country_code)
        admin_state[uid] = {
            "action": "core_config_view",
            "country_code": country_code,
            "grid_page": page,
        }
        text, markup = _build_country_panel(country_code, page=page)
        await _edit_callback(query, text, reply_markup=markup)

    # --------------------------------------------------------------
    #  Custom API (per country; secret never shown in messages)
    # --------------------------------------------------------------

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_api_set_(\+\d+)$"))
    async def cc_api_set(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_api_set_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        country_code = m.group(1)
        uid = query.from_user.id
        await query.answer()
        _set_admin_state(uid, query, "cc_api_input",
                         country_code=country_code,
                         grid_page=_page_from_state(uid, country_code))
        short = get_country_display_code(country_code)
        await _edit_callback(query,
            _bi(f"🔌 Set Custom API — {short}") + "\n\n"
            + _bi(f"Send the API credentials for {country_code} only:")
            + "\n`12345678 0123456789abcdef0123456789abcdef`\n(API_ID API_HASH)\n\n/admin to cancel",
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_api_del_(\+\d+)$"))
    async def cc_api_del(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_api_del_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        country_code = m.group(1)
        uid = query.from_user.id
        await query.answer()
        if not get_country_custom_api(country_code):
            await _edit_callback(query,
                _bi(f"🔌 Remove Custom API") + "\n\n"
                + _bi("No custom API is set for this country."),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ Back", callback_data=f"cc_g_{_page_from_state(uid, country_code)}_{country_code}")],
                ]))
            return
        await _edit_callback(query,
            _bi("🔌 Remove Custom API") + "\n\n"
            + _bi(f"Remove the custom API of {country_code}?"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Remove", callback_data=f"cc_api_del_yes_{country_code}"),
                 InlineKeyboardButton("❌ Cancel", callback_data=f"cc_g_{_page_from_state(uid, country_code)}_{country_code}")],
            ]))

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_api_del_yes_(\+\d+)$"))
    async def cc_api_del_yes(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_api_del_yes_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        country_code = m.group(1)
        uid = query.from_user.id
        await query.answer()
        remove_country_custom_api(country_code)
        await _log_admin_action(uid, "cc_api_removed", message=country_code)
        page = _page_from_state(uid, country_code)
        admin_state[uid] = {
            "action": "core_config_view",
            "country_code": country_code,
            "grid_page": page,
        }
        text, markup = _build_country_panel(country_code, page=page)
        await _edit_callback(query, text, reply_markup=markup)

    # --------------------------------------------------------------
    #  Name / Bio TXT uploads
    # --------------------------------------------------------------

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_upload_(names|bios)_(\+\d+)$"))
    async def cc_upload(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_upload_(names|bios)_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        what, country_code = m.group(1), m.group(2)
        uid = query.from_user.id
        await query.answer()
        _set_admin_state(uid, query, "cc_upload_txt",
                         country_code=country_code, upload_what=what,
                         grid_page=_page_from_state(uid, country_code))
        short = get_country_display_code(country_code)
        label = "Name" if what == "names" else "Bio"
        current_count = len(get_country_name_list(country_code) if what == "names"
                            else get_country_bio_list(country_code))
        await _edit_callback(query,
            _bi(f"📤 Upload {label} TXT — {short}") + "\n\n"
            + _bi(f"Send a .txt file with one {label.lower()} per line for {country_code} only.")
            + f"\n\n" + _bi(f"Current list: {current_count} lines")
            + "\n\n/admin to cancel",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🗑 Clear {label} List", callback_data=f"cc_{what}_clear_{country_code}")],
                [InlineKeyboardButton("◀ Back", callback_data=f"cc_g_{_page_from_state(uid, country_code)}_{country_code}")],
            ]))

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_(names|bios)_clear_(\+\d+)$"))
    async def cc_upload_clear(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_(names|bios)_clear_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        what, country_code = m.group(1), m.group(2)
        await query.answer()
        # P4-46: clear must give VISIBLE feedback - confirm the deletion with
        # the removed count and re-render the upload screen so the stale
        # "Current list: N lines" text cannot mislead the admin. A vanished
        # country (ok=False) reports the failure instead of silently no-oping.
        before = len(get_country_name_list(country_code) if what == "names"
                     else get_country_bio_list(country_code))
        ok = (set_country_name_list(country_code, []) if what == "names"
              else set_country_bio_list(country_code, []))
        await _log_admin_action(query.from_user.id, "cc_list_cleared",
                                message=f"{country_code} {what}")
        label = "Name" if what == "names" else "Bio"
        if not ok:
            await _edit_callback(query, _bi(f"❌ Country {country_code} not found - nothing cleared."))
            return
        page = _page_from_state(query.from_user.id, country_code)
        await _edit_callback(query,
            _bi(f"✅ {label} list cleared for {country_code} ({before} line(s) removed).") + "\n\n"
            + _bi(f"Send a .txt file with one {label.lower()} per line for {country_code} only.")
            + f"\n\n" + _bi(f"Current list: 0 lines")
            + "\n\n/admin to cancel",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🗑 Clear {label} List", callback_data=f"cc_{what}_clear_{country_code}")],
                [InlineKeyboardButton("◀ Back", callback_data=f"cc_g_{page}_{country_code}")],
            ]))

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_cancel_(\+\d+)$"))
    async def cc_cancel_input(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_cancel_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        country_code = m.group(1)
        uid = query.from_user.id
        await query.answer()
        _admin_manager.clear_state(uid)
        page = _page_from_state(uid, country_code)
        admin_state[uid] = {
            "action": "core_config_view",
            "country_code": country_code,
            "grid_page": page,
        }
        text, markup = _build_country_panel(country_code, page=page)
        await _edit_callback(query, text, reply_markup=markup)

    # --------------------------------------------------------------
    #  Remove: N/B/P - three separate delete options (Names/Bios/Photos)
    # --------------------------------------------------------------

    _REMOVE_LABELS = {"names": ("🗑 Remove Name List", "cc_remove_names_"),
                      "bios": ("🗑 Remove Bio List", "cc_remove_bios_"),
                      "photos": ("🗑 Remove Photos", "cc_remove_photos_")}

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_remove_(\+\d+)$"))
    async def cc_remove_menu(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_remove_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        country_code = m.group(1)
        uid = query.from_user.id
        await query.answer()
        page = _page_from_state(uid, country_code)
        names_count = len(get_country_name_list(country_code))
        bios_count = len(get_country_bio_list(country_code))
        photos_count = get_country_photo_count(country_code)
        buttons = [
            [InlineKeyboardButton(f"Remove Name List ({names_count})", callback_data=f"cc_remove_names_{country_code}"),
             InlineKeyboardButton(f"Remove Bio List ({bios_count})", callback_data=f"cc_remove_bios_{country_code}")],
            [InlineKeyboardButton(f"Remove Photos ({photos_count})", callback_data=f"cc_remove_photos_{country_code}")],
            [InlineKeyboardButton("◀ Back", callback_data=f"cc_g_{page}_{country_code}")],
        ]
        await _edit_callback(query,
            _bi("🗑 Remove: N/B/P") + "\n\n"
            + _bi(f"Choose what to delete for {country_code} only:"),
            reply_markup=InlineKeyboardMarkup(buttons))

    async def _cc_do_remove(query, country_code: str, what: str, uid: int):
        """Shared removal worker: names / bios / photos for ONE country."""
        if what == "names":
            set_country_name_list(country_code, [])
            label = "Name list"
        elif what == "bios":
            set_country_bio_list(country_code, [])
            label = "Bio list"
        elif what == "photos":
            removed = remove_country_photos(country_code)
            label = f"{removed} photo(s)"
        else:
            label = "Unknown"
        await _log_admin_action(uid, "cc_removed",
                                message=f"{country_code} {what}")
        page = _page_from_state(uid, country_code)
        admin_state[uid] = {
            "action": "core_config_view",
            "country_code": country_code,
            "grid_page": page,
        }
        text, markup = _build_country_panel(country_code, page=page)
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_remove_(names|bios|photos)_(\+\d+)$"))
    async def cc_remove_item(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_remove_(names|bios|photos)_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        what, country_code = m.group(1), m.group(2)
        uid = query.from_user.id
        # Confirm first: removal is destructive (only for this country).
        counts = {
            "names": len(get_country_name_list(country_code)),
            "bios": len(get_country_bio_list(country_code)),
            "photos": get_country_photo_count(country_code),
        }
        await query.answer()
        page = _page_from_state(uid, country_code)
        label = {"names": "Name list", "bios": "Bio list", "photos": "Photos"}[what]
        await _edit_callback(query,
            _bi(f"🗑 Remove {label} — {country_code}") + "\n\n"
            + _bi(f"This will delete {counts[what]} item(s) for {country_code} only.")
            + "\n\n" + _bi("Continue?"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"cc_remove_yes_{what}_{country_code}"),
                 InlineKeyboardButton("❌ Cancel", callback_data=f"cc_remove_{country_code}")],
            ]))

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_remove_yes_(names|bios|photos)_(\+\d+)$"))
    async def cc_remove_yes(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_remove_yes_(names|bios|photos)_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        what, country_code = m.group(1), m.group(2)
        uid = query.from_user.id
        await query.answer()
        await _cc_do_remove(query, country_code, what, uid)

    # --------------------------------------------------------------
    #  Details: N/B/P/F - four separate info screens
    # --------------------------------------------------------------

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_details_(\+\d+)$"))
    async def cc_details_menu(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_details_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        country_code = m.group(1)
        uid = query.from_user.id
        await query.answer()
        page = _page_from_state(uid, country_code)
        await _edit_callback(query,
            _bi("📊 Details: N/B/P/F") + "\n\n"
            + _bi(f"Choose what to view for {country_code} only:"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Names", callback_data=f"cc_details_names_{country_code}"),
                 InlineKeyboardButton("Bios", callback_data=f"cc_details_bios_{country_code}")],
                [InlineKeyboardButton("Photos", callback_data=f"cc_details_photos_{country_code}"),
                 InlineKeyboardButton("File (ZIP)", callback_data=f"cc_details_file_{country_code}")],
                [InlineKeyboardButton("◀ Back", callback_data=f"cc_g_{page}_{country_code}")],
            ]))

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_details_(names|bios|photos|file)_(\+\d+)$"))
    async def cc_details_view(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_details_(names|bios|photos|file)_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        what, country_code = m.group(1), m.group(2)
        uid = query.from_user.id
        await query.answer()
        page = _page_from_state(uid, country_code)

        def _preview_list(items, limit=15):
            if not items:
                return _bi("(empty)")
            lines = [f"`{i + 1}. {item[:40]}`" for i, item in enumerate(items[:limit])]
            if len(items) > limit:
                lines.append(f"… +{len(items) - limit} more")
            return "\n".join(lines)

        if what == "names":
            items = get_country_name_list(country_code)
            body = _bi(f"Total: {len(items)} lines") + "\n\n" + _preview_list(items)
        elif what == "bios":
            items = get_country_bio_list(country_code)
            body = _bi(f"Total: {len(items)} lines") + "\n\n" + _preview_list(items)
        elif what == "photos":
            total = get_country_photo_count(country_code)
            used = get_country_photos_used(country_code)
            body = (_bi(f"Photos ready: {total:,}") + "\n"
                    + _bi(f"Used (next index): {used:,}"))
        else:  # file (ZIP)
            meta = get_country_photo_zip_meta(country_code)
            if not meta:
                body = _bi("No ZIP imported yet.")
            else:
                body = (_bi(f"File: {meta.get('name', '?')}") + "\n"
                        + _bi(f"Size: {meta.get('size_mb', 0):g} MB") + "\n"
                        + _bi(f"Imported: {meta.get('at', '?')}") + "\n"
                        + _bi(f"Images: {meta.get('imported', 0):,} (skipped {meta.get('skipped', 0):,})"))
        await _edit_callback(query,
            _bi(f"📊 Details — {what.upper()} | {country_code}") + "\n\n" + body,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back", callback_data=f"cc_details_{country_code}")],
            ]))

    # --------------------------------------------------------------
    #  Photo ZIP upload (document handler picks it up)
    # --------------------------------------------------------------

    @app.on_callback_query(admin_filter & filters.regex(r"^cc_upload_photos_(\+\d+)$"))
    async def cc_upload_photos(client: Client, query: CallbackQuery):
        m = re.match(r"^cc_upload_photos_(\+\d+)$", str(query.data or ""))
        if not m:
            await query.answer()
            return
        country_code = m.group(1)
        uid = query.from_user.id
        await query.answer()
        _set_admin_state(uid, query, "cc_upload_zip",
                         country_code=country_code,
                         grid_page=_page_from_state(uid, country_code))
        short = get_country_display_code(country_code)
        total = get_country_photo_count(country_code)
        await _edit_callback(query,
            _bi(f"🖼️ Upload Photo ZIP — {short}") + "\n\n"
            + _bi(f"Send a .zip file containing the profile photos for {country_code} only.")
            + "\n(Inside: .jpg / .jpeg / .png — other files are skipped)"
            + "\n\n" + _bi(f"Current photos: {total:,}")
            + "\n\n/admin to cancel",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back", callback_data=f"cc_g_{_page_from_state(uid, country_code)}_{country_code}")],
            ]))

    # --------------------------------------------------------------
    #  Document handler for the TXT + ZIP uploads (admin-filtered)
    # --------------------------------------------------------------

    # NOTE: NO filters.private here - the admin panel may live in a GROUP
    # (multi-admin setups), and every other admin handler (text/callbacks)
    # works in any chat the panel message is in. Documents must follow the
    # same rule or TXT/ZIP uploads silently never fire for group panels.
    @app.on_message(admin_filter & filters.document, group=5)
    async def cc_document_handler(client: Client, message: Message):
        user_id = message.from_user.id
        state = admin_state.get(user_id)
        if not state or state.get("action") not in ("cc_upload_txt", "cc_upload_zip"):
            return
        country_code = state.get("country_code") or ""
        page = _store_page(country_code, state.get("grid_page", 0))
        doc = message.document

        def _back_markup():
            return InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back", callback_data=f"cc_g_{page}_{country_code}")],
            ])

        if state.get("action") == "cc_upload_zip":
            # --- Photo ZIP upload path ---
            if not doc or not (doc.file_name or "").lower().endswith(".zip"):
                await _panel_reply(client, user_id, message,
                    _bi("❌ Please send a .zip file containing .jpg/.png photos."),
                    reply_markup=_back_markup())
                return
            if (doc.file_size or 0) > 2 * 1024 * 1024 * 1024:
                await _panel_reply(client, user_id, message,
                    _bi("❌ ZIP is too large (max 2 GB). Split it into parts."),
                    reply_markup=_back_markup())
                return
            try:
                zip_path = await message.download(file_name=f"cc_zip_{user_id}.zip")
            except Exception as dl_err:
                await _panel_reply(client, user_id, message,
                    _bi(f"❌ Download failed: {dl_err}"))
                return
            try:
                imported, skipped = import_country_photos_zip(country_code, zip_path)
                try:
                    import datetime as _dt
                    set_country_photo_zip_meta(country_code, {
                        "name": doc.file_name or "photos.zip",
                        "size_mb": round((doc.file_size or 0) / (1024 * 1024), 1),
                        "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "imported": imported,
                        "skipped": skipped,
                    })
                except Exception:
                    pass
            except Exception as zip_err:
                await _panel_reply(client, user_id, message,
                    _bi(f"❌ Could not read ZIP: {zip_err}"),
                    reply_markup=_back_markup())
                return
            finally:
                try:
                    import os as _os
                    _os.remove(zip_path)
                except OSError:
                    pass
            await _log_admin_action(user_id, "cc_zip_imported",
                                    message=f"{country_code} imported={imported} skipped={skipped}")
            await _panel_reply(client, user_id, message,
                _bi(f"✅ Photos imported for {country_code} only: {imported:,}")
                + (f" (skipped {skipped:,})" if skipped else ""),
                reply_markup=_back_markup())
            _admin_manager.clear_state(user_id)
            return

        # --- TXT upload path (names / bios) ---
        what = state.get("upload_what") or ""
        doc = message.document
        if not doc or not (doc.file_name or "").lower().endswith(".txt"):
            await _panel_reply(client, user_id, message,
                _bi("❌ Please send a .txt file."),
                reply_markup=_back_markup())
            return
        try:
            file_path = await message.download(file_name=f"cc_upload_{user_id}_{what}.txt")
        except Exception as dl_err:
            await _panel_reply(client, user_id, message, _bi(f"❌ Download failed: {dl_err}"))
            return
        try:
            # utf-8-sig (P4-46): Windows Notepad saves UTF-8 files WITH a BOM.
            # Reading plain "utf-8" would leave an invisible U+FEFF glued to
            # the FIRST name/bio line (str.strip() does not remove it - it is
            # category Cf, not whitespace) and the bot would apply it as part
            # of the profile. utf-8-sig strips the BOM when present and is
            # byte-identical for BOM-less files.
            with open(file_path, "r", encoding="utf-8-sig", errors="replace") as fh:
                raw_lines = fh.read().splitlines()
        except Exception as read_err:
            await _panel_reply(client, user_id, message, _bi(f"❌ Could not read file: {read_err}"))
            return
        finally:
            try:
                import os as _os
                _os.remove(file_path)
            except OSError:
                pass

        lines = [ln.strip() for ln in raw_lines if ln.strip()]
        if not lines:
            await _panel_reply(client, user_id, message,
                _bi("❌ The file is empty."),
                reply_markup=_back_markup())
            return
        if len(lines) > 5000:
            await _panel_reply(client, user_id, message,
                _bi("❌ Too many lines (max 5000). Split the file and try again."),
                reply_markup=_back_markup())
            return

        if what == "names":
            ok = set_country_name_list(country_code, lines)
        else:
            ok = set_country_bio_list(country_code, lines)
        if not ok:
            await _panel_reply(client, user_id, message,
                _bi("❌ Country not found in storage."))
            _admin_manager.clear_state(user_id)
            return
        await _log_admin_action(user_id, "cc_txt_uploaded",
                                message=f"{country_code} {what}={len(lines)} lines")
        label = "Name" if what == "names" else "Bio"
        await _panel_reply(client, user_id, message,
            _bi(f"✅ {label} list saved: {len(lines)} lines for {country_code} only."),
            reply_markup=_back_markup())
        _admin_manager.clear_state(user_id)

    # --------------------------------------------------------------
    #  Text input handler for all CORE CONFIG input states
    # --------------------------------------------------------------

    # Text inputs run in the SAME handler group as the other admin text
    # handlers (default group 0). Registration ORDER decides which handler
    # sees the message first: this module is registered inside
    # register_admin_features(), i.e. BEFORE register_admin_text_fallback()
    # in register_admin_handlers(), so a pending CORE CONFIG input is
    # consumed here and the stale-state fallback never fires for it.
    # (A later group like 4 would run AFTER the fallback, which CLEARS the
    # admin state on unknown actions - the input would be dropped.)
    # The excluded command set is EXACTLY NON_TEXT_INPUT_COMMANDS, the same
    # list every other admin text handler excludes.
    @app.on_message(admin_filter & filters.text
                    & ~filters.command(NON_TEXT_INPUT_COMMANDS))
    async def cc_text_input(client: Client, message: Message):
        user_id = message.from_user.id
        state = admin_state.get(user_id)
        if not state:
            message.continue_propagation()
            return
        action = state.get("action", "")
        if action not in {"cc_rate_input", "cc_all_rates_input", "cc_capacity_input",
                          "cc_time_input", "cc_proxy_input", "cc_api_input"}:
            message.continue_propagation()
            return
        message._handled_by_admin = True
        text = (message.text or "").strip()
        country_code = state.get("country_code") or ""
        page = _store_page(country_code, state.get("grid_page", 0))

        def _back_markup():
            return InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back to Country", callback_data=f"cc_g_{page}_{country_code}")],
            ])

        try:
            if action == "cc_rate_input":
                try:
                    value = float(text.replace("%", "").strip())
                except ValueError:
                    await _panel_reply(client, user_id, message,
                        _bi("❌ Rate must be a number, e.g. `25`"), reply_markup=_back_markup())
                    return
                rate_key = state.get("rate_key", "spam_free_rate")
                set_country_rate_by_type(country_code, rate_key, value)
                await _log_admin_action(user_id, "cc_rate_set",
                                        message=f"{country_code} {rate_key}={value}")
                rates = get_country_rates(country_code)
                name = _RATE_NAMES.get(state.get("rate_kind", ""), rate_key)
                await _panel_reply(client, user_id, message,
                    _bi(f"✅ {name} rate saved for {country_code} only.")
                    + "\n" + _bi(f"Free: {rates['spam_free_rate']:g}% | "
                                 f"Spam: {rates['spam_rate']:g}% | "
                                 f"Limit: {rates['limit_rate']:g}%"),
                    reply_markup=_back_markup())

            elif action == "cc_all_rates_input":
                parts = text.replace("%", "").split()
                if len(parts) != 3:
                    await _panel_reply(client, user_id, message,
                        _bi("❌ Format: `30 15 10` (Free Spam Limit)"), reply_markup=_back_markup())
                    return
                try:
                    free_r, spam_r, limit_r = (float(p) for p in parts)
                except ValueError:
                    await _panel_reply(client, user_id, message,
                        _bi("❌ All three values must be numbers."), reply_markup=_back_markup())
                    return
                # Atomic: existing hierarchy validation + single record write.
                result = set_country_rates(country_code, free_r, spam_r, limit_r)
                await _log_admin_action(user_id, "cc_rates_set_all",
                                        message=f"{country_code} f={free_r} s={spam_r} l={limit_r}")
                await _panel_reply(client, user_id, message,
                    _bi(f"✅ All rates saved for {country_code} only.")
                    + "\n" + _bi(f"Free: {result['spam_free_rate']:g}% | "
                                 f"Spam: {result['spam_rate']:g}% | "
                                 f"Limit: {result['limit_rate']:g}%"),
                    reply_markup=_back_markup())

            elif action == "cc_capacity_input":
                try:
                    value = int(text)
                except ValueError:
                    await _panel_reply(client, user_id, message,
                        _bi("❌ Capacity must be a number (0 = unlimited)."), reply_markup=_back_markup())
                    return
                if value < 0:
                    await _panel_reply(client, user_id, message,
                        _bi("❌ Capacity cannot be negative."), reply_markup=_back_markup())
                    return
                ok = set_country_capacity(country_code, value)
                if not ok:
                    await _panel_reply(client, user_id, message,
                        _bi("❌ Country not found."), reply_markup=_back_markup())
                    return
                await _log_admin_action(user_id, "cc_capacity_set",
                                        message=f"{country_code} capacity={value}")
                await _panel_reply(client, user_id, message,
                    _bi(f"✅ Capacity saved for {country_code} only: {_capacity_display(value)}"),
                    reply_markup=_back_markup())

            elif action == "cc_time_input":
                try:
                    value = int(text)
                except ValueError:
                    await _panel_reply(client, user_id, message,
                        _bi("❌ Timeout must be a number of seconds."), reply_markup=_back_markup())
                    return
                if value <= 0:
                    await _panel_reply(client, user_id, message,
                        _bi("❌ Timeout must be greater than 0."), reply_markup=_back_markup())
                    return
                if value > 3600:
                    await _panel_reply(client, user_id, message,
                        _bi("❌ Timeout cannot exceed 3600 seconds."), reply_markup=_back_markup())
                    return
                ok, msg = set_country_timeout(country_code, value)
                if not ok:
                    await _panel_reply(client, user_id, message,
                        _bi(f"❌ {msg}"), reply_markup=_back_markup())
                    return
                await _log_admin_action(user_id, "cc_time_set",
                                        message=f"{country_code} timeout={value}s")
                await _panel_reply(client, user_id, message,
                    _bi(f"✅ Time saved for {country_code} only: {value}s"),
                    reply_markup=_back_markup())

            elif action == "cc_proxy_input":
                from bot.data.proxies import set_country_proxy, parse_proxy_url
                ok, parsed, err = parse_proxy_url(text)
                if not ok or parsed is None:
                    await _panel_reply(client, user_id, message,
                        _bi(f"❌ Invalid proxy URL: {err}\nFormat: `socks5://user:pass@host:port`"),
                        reply_markup=_back_markup())
                    return
                ok, msg = set_country_proxy(country_code, text)
                if not ok:
                    await _panel_reply(client, user_id, message,
                        _bi(f"❌ {msg}"), reply_markup=_back_markup())
                    return
                await _log_admin_action(user_id, "cc_proxy_set", message=country_code)
                await _panel_reply(client, user_id, message,
                    _bi(f"✅ Proxy saved for {country_code} only."),
                    reply_markup=_back_markup())

            elif action == "cc_api_input":
                parts = text.split()
                if len(parts) != 2:
                    await _panel_reply(client, user_id, message,
                        _bi("❌ Format: `API_ID API_HASH`"), reply_markup=_back_markup())
                    return
                api_id_raw, api_hash_raw = parts[0], parts[1]
                if not api_id_raw.isdigit() or len(api_hash_raw) < 16:
                    await _panel_reply(client, user_id, message,
                        _bi("❌ API_ID must be numeric and API_HASH at least 16 characters."),
                        reply_markup=_back_markup())
                    return
                ok = set_country_custom_api(country_code, api_id_raw, api_hash_raw)
                if not ok:
                    await _panel_reply(client, user_id, message,
                        _bi("❌ Could not save (country missing or invalid values)."),
                        reply_markup=_back_markup())
                    return
                await _log_admin_action(user_id, "cc_api_set",
                                        message=f"{country_code} api_id={api_id_raw}")
                # Never echo the hash back in the confirmation.
                await _panel_reply(client, user_id, message,
                    _bi(f"✅ Custom API saved for {country_code} only (API_ID {api_id_raw})."),
                    reply_markup=_back_markup())

        except Exception as e:
            await _panel_reply(client, user_id, message, _bi(f"❌ Error: {e}"),
                               reply_markup=_back_markup())
        finally:
            _admin_manager.clear_state(user_id)

    # Keep the input-time grid page fresh for the toggles opened from input
    # screens (state was cleared after each input; view state is rebuilt on
    # every cc_g_ click, so nothing else to persist here).
