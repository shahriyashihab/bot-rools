# bot/handlers/admin/countries.py - country rates & pricing management

import os
import json
import sqlite3
import logging
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional
import threading
from dataclasses import dataclass, field

from pyrogram.client import Client
from pyrogram import filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified

from bot.utils.storage import (
    get_user_info,
    update_user_info,
    get_all_users,
    ban_user,
    unban_user,
    set_country_rates,
    set_country_rate_by_type,
    get_country_rates,
    set_country_timeout,
    add_balance_for_user,
    get_user_stats,
    zero_all_balances,
    toggle_withdraw,
    get_total_users,
    get_all_users_list,
    get_withdraw_logs,
    approve_withdraw_request,
    reject_withdraw_request,
    disable_all_countries,
    enable_all_countries,
    toggle_country_status,
    get_required_channel,
    set_required_channel,
    is_all_countries_paused,
    get_min_withdraw_amounts,
    set_min_withdraw_for_method,
    get_all_message_templates,
    set_message_template,
    reset_message_template,
    DEFAULT_MESSAGE_TEMPLATES,
    get_all_balances_report,
    is_spam_check_enabled,
    toggle_spam_check,
    get_exportable_sessions,
    get_pending_exportable_sessions,
    get_session_export_counts,
    delete_exported_sessions,
    get_all_pending_sessions,
    cleanup_old_stuck_sessions,
    mark_session_message_sent,
    get_auto_approve_enabled,
    set_auto_approve_enabled,
    get_session_expiry_hours,
    set_session_expiry_hours,
    get_stuck_session_expiry_hours,
    set_stuck_session_expiry_hours,
    _storage,
    SQLITE_STORAGE_PATH,
    save_storage,
    save_storage_now,
)
from bot.utils.country_data import get_flag
from bot.config import ADMIN_ID, SESSION_2FA_PASSWORD, get_2fa_password, CHANNEL_WITHDRAW_REPORT, EXTRA_APIS, API_ID
from bot.utils.i18n import admin_str
from bot.handlers.admin.router import (
    admin_filter,
    admin_state,
    _admin_manager,
    is_admin,
    get_all_admins,
    admin_filter_func,
    _log_admin_action,
    _set_admin_state,
    _get_panel_ref,
    _edit_in_place,
    _edit_callback,
    _edit_by_ids,
    _panel_reply,
    get_monitor_status,
    set_monitor_status,
    toggle_monitor_status,
    _validate_admin_country_code,
    _admin_country_label,
    AdminStateManager,
    _AdminStateProxy,
    NON_TEXT_INPUT_COMMANDS,
    ADMIN_TEXT_FILTER,
    FEATURE_ACTIONS,
)
from bot.handlers.admin.menu import (
    get_admin_panel_buttons,
    get_user_management_buttons,
    get_country_pricing_buttons,
    get_withdraw_finance_buttons,
    get_system_controls_buttons,
    get_messaging_buttons,
    get_advanced_tools_buttons,
    get_settings_config_buttons,
    get_session_management_buttons,
    _create_db_snapshot,
)

logger = logging.getLogger(__name__)

def _build_capacity_ui_panel():
    """Build the Capacity UI selection panel (text + button rows).

    Rendered dynamically from the registered capacity UIs (bot.handlers.cap
    ``CAPACITY_UIS``) so any future UI (#3, #4, ...) automatically appears
    here without touching the admin code.
    """
    from bot.utils.storage import get_capacity_ui
    from bot.handlers.cap import CAPACITY_UIS

    active = get_capacity_ui()

    lines = [
        "🎛️ Capacity UI",
        "",
        f"Currently Active: **{active}**",
    ]
    for ui_id in sorted(CAPACITY_UIS):
        cfg = CAPACITY_UIS[ui_id]
        marker = "✅" if ui_id == active else "⚪"
        lines.append(f"{marker} **{ui_id}.** {cfg['name']}")

    buttons = []
    for ui_id in sorted(CAPACITY_UIS):
        cfg = CAPACITY_UIS[ui_id]
        label = f"{'✅' if ui_id == active else '🔘'} {ui_id}. {cfg['name']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"capacity_ui_set_{ui_id}")])
    buttons.append([InlineKeyboardButton("🔙 Back to Country & Pricing", callback_data="menu_country")])

    return "\n".join(lines), buttons


def register_countries(app: Client):

    @app.on_callback_query(admin_filter & filters.regex(r"^menu_country$"))
    async def menu_country_callback(client: Client, query: CallbackQuery):
        await query.answer()
        from bot.handlers.admin.design import hd
        from bot.handlers.admin.menu import get_country_pricing_status_line
        await _edit_callback(query,
            hd("🌍 Country & Pricing", "Main › Countries", get_country_pricing_status_line()),
            reply_markup=get_country_pricing_buttons()
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^capacity_ui_menu$"))
    async def capacity_ui_menu_callback(client: Client, query: CallbackQuery):
        await query.answer()
        text, buttons = _build_capacity_ui_panel()
        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(buttons))

    @app.on_callback_query(admin_filter & filters.regex(r"^capacity_ui_set_(\d+)$"))
    async def capacity_ui_set_callback(client: Client, query: CallbackQuery):
        import re as _re
        m = _re.match(r"^capacity_ui_set_(\d+)$", str(query.data))
        if not m:
            return
        uid = query.from_user.id
        from bot.utils.storage import set_capacity_ui
        from bot.handlers.cap import CAPACITY_UIS

        ui_id = int(m.group(1))
        new_id = set_capacity_ui(ui_id)
        cfg = CAPACITY_UIS.get(new_id, {})
        await query.answer()

        await _log_admin_action(uid, "capacity_ui_set", message=f"ui_id={new_id}")

        text, buttons = _build_capacity_ui_panel()
        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(buttons))


    @app.on_callback_query(admin_filter & filters.regex(r"^view_all_countries$|^vac_page_\d+$"))
    async def view_all_countries_callback(client: Client, query: CallbackQuery):
        """📋 View All Countries & Rates (paginated - MEDIUM-44)"""
        await query.answer()
        try:
            countries = _storage.get("countries", {})
            if not countries:
                await _edit_callback(query,
                    "📋 **All Countries & Rates**\n\n"
                    "❌ No countries configured.\n\n"
                    "💡 To add countries:\n"
                    "Country & Pricing Controls → Set Country Price",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back to Advanced Tools", callback_data="menu_advanced_tools")]
                    ])
                )
                return

            from bot.utils.country_data import get_flag
            from bot.data.counters import get_daily_success_count
            import re as _re

            m_page = _re.match(r"^vac_page_(\d+)$", str(query.data or ""))
            page = int(m_page.group(1)) if m_page else 0
            lines = []
            for code, data in sorted(countries.items()):
                flag = get_flag(code)
                spam_free = data.get("spam_free_rate", data.get("rate", 0))
                spam = data.get("spam_rate", 0)
                limit = data.get("limit_rate", 0)
                timeout = data.get("timeout", 300)
                try:
                    capacity = int(data.get("capacity", 0) or 0)
                except (TypeError, ValueError):
                    capacity = 0
                is_open = data.get("open", False)
                status = "🟢 Open" if is_open else "🔴 Closed"
                
                used = 0
                try:
                    used = int(get_daily_success_count(code) or 0)
                except (TypeError, ValueError):
                    used = 0
                # Capacity filled (and not unlimited) -> the country is blocked
                # until the admin gives it a new capacity again.
                capacity_full = capacity > 0 and used >= capacity
                if capacity_full:
                    status = "🔒 Full" if is_open else "🔴 Closed"
                
                capacity_text = f"{capacity:,}" if capacity else "∞"
                
                lines.append(
                    f"{flag} **{code}** {status}\n"
                    f"   ✅ Spam Free: ${spam_free} | ⚠️ Spam: ${spam} | 🔒 Limit: ${limit}\n"
                    f"   🎯 Capacity: {capacity_text} (used: {used:,})"
                    f"{' — 🔒 CAPACITY FULL' if capacity_full else ''}"
                    f" | ⏰ Timeout: {timeout}s"
                )
            
            total_countries = len(countries)
            open_count = sum(1 for d in countries.values() if d.get("open", False))

            header = (
                f"📋 **All Countries & Rates**\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🌍 Total: **{total_countries}** | 🟢 Open: **{open_count}** | 🔴 Closed: **{total_countries - open_count}**\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            )

            # MEDIUM-44: paginate (20/page) instead of head-truncating at 20.
            per_page = 20
            total_pages = max(1, (len(lines) + per_page - 1) // per_page)
            page = max(0, min(page, total_pages - 1))
            page_lines = lines[page * per_page:(page + 1) * per_page]

            full_msg = header + "\n\n".join(page_lines)
            if len(lines) > per_page:
                full_msg += f"\n\n📄 Page {page + 1}/{total_pages} — {len(lines)} countries total"

            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("◀ Back", callback_data=f"vac_page_{page - 1}"))
            else:
                nav.append(InlineKeyboardButton("ㅤ", callback_data="vac_noop"))
            nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="vac_noop"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("Next ▶", callback_data=f"vac_page_{page + 1}"))
            else:
                nav.append(InlineKeyboardButton("ㅤ", callback_data="vac_noop"))

            buttons = [nav] if total_pages > 1 else []
            buttons.append([InlineKeyboardButton("🔙 Back to Advanced Tools", callback_data="menu_advanced_tools")])

            if len(full_msg) > 4000:
                await _edit_callback(query, header)
                for i in range(0, len(page_lines), 10):
                    chunk = "\n\n".join(page_lines[i:i+10])
                    await query.message.reply(chunk)
                await _edit_callback(query, f"✅ Page {page + 1}/{total_pages} displayed.", reply_markup=InlineKeyboardMarkup(buttons))
            else:
                await _edit_callback(query, full_msg, reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            await _edit_callback(query, f"❌ Error: {str(e)}")

    @app.on_callback_query(admin_filter & filters.regex(r"^vac_noop$"))
    async def vac_noop_callback(client: Client, query: CallbackQuery):
        await query.answer()


    @app.on_callback_query(admin_filter & filters.regex(r"^setrate_(spamfree|spam|limit)$"))
    async def set_rate_type_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data_str = str(query.data) if query.data else "setrate_spamfree"
        rate_type = data_str.split("_")[1] if "_" in data_str else "spamfree"
        
        rate_type_names = {
            "spamfree": ("Spam Free", "spam_free_rate"),
            "spam": ("Spam", "spam_rate"),
            "limit": ("Limit", "limit_rate")
        }
        
        name, storage_key = rate_type_names.get(rate_type, ("Unknown", "rate"))
        
        await query.answer()
        await _edit_callback(query, 
            f"--- SET {name.upper()} RATE ---\n\n"
            f"Enter country code and rate:\n"
            f"Format: +880 30 (means {name} Rate = $30 for +880)"
        )
        _set_admin_state(uid, query, "setrate_type", rate_type=storage_key)


    @app.on_callback_query(admin_filter & filters.regex(r"^setrate_all$"))
    async def set_all_rates_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        
        await query.answer()
        await _edit_callback(query,
            "--- SET ALL RATES ---\n\n"
            "Enter country code and three rates:\n"
            "Format: `+880 30 15 10`\n"
            "(SpamFree Spam Limit)"
        )
        _set_admin_state(uid, query, "setrate_all")

    # ---------------- Message Settings Callbacks ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^setrate$"))
    async def admin_action_setrate(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        buttons = [
            [InlineKeyboardButton("Spam Free Rate", callback_data="setrate_spamfree"),
             InlineKeyboardButton("Spam Rate", callback_data="setrate_spam")],
            [InlineKeyboardButton("Limit Rate", callback_data="setrate_limit"),
             InlineKeyboardButton("Set All Rates", callback_data="setrate_all")],
            [InlineKeyboardButton("Back to Country & Pricing", callback_data="menu_country")]
        ]
        await _edit_callback(query,
            "--- SET COUNTRY PRICE ---\n\n"
            "Which account type?",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^settimeout$"))
    async def admin_action_settimeout(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        await _edit_callback(query,
            "--- SET SESSION TIMEOUT ---\n\n"
            "Enter country code and seconds:\n"
            "Format: `+880 60`"
        )
        _set_admin_state(uid, query, "settimeout")

    @app.on_callback_query(admin_filter & filters.regex(r"^togglecountry$"))
    async def admin_action_togglecountry(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        await _edit_callback(query,
            "--- TOGGLE COUNTRY ---\n\n"
            "Country code (e.g. `+880`):"
        )
        _set_admin_state(uid, query, "togglecountry")

    @app.on_callback_query(admin_filter & filters.regex(r"^bulk_add_countries$"))
    async def admin_action_bulk_add_countries(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        await query.answer()

        await _edit_callback(query,
            "--- BULK ADD COUNTRIES ---\n\n"
            "Paste one country per line:\n\n"
            "`+251 0.4 0.3 0.1 1000 300`\n\n"
            "Format: `+Code SpamFree Spam Limit Capacity Timeout`\n"
            "(Capacity 0 = unlimited)\n\n"
            "Type /admin to cancel"
        )
        _set_admin_state(uid, query, "bulk_add_countries")

    @app.on_callback_query(admin_filter & filters.regex(r"^delete_all_open_confirm$"))
    async def admin_action_delete_all_open_confirm(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        await query.answer()

        open_count = sum(
            1 for d in _storage.get("countries", {}).values()
            if isinstance(d, dict) and d.get("open", False)
        )
        if open_count == 0:
            await _edit_callback(query, 
                "No open countries to delete.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back to Country & Pricing", callback_data="menu_country")]
                ])
            )
            return

        await _edit_callback(query,
            f"⚠️ DELETE ALL {open_count} open countries?\n\n"
            "This action CANNOT be undone.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Delete All", callback_data="confirm_delete_all_open"),
                 InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_delete_all_open")]
            ])
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^confirm_delete_all_open$"))
    async def admin_action_confirm_delete_all_open(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        await query.answer()

        from bot.utils.storage import delete_all_open_countries
        deleted = delete_all_open_countries()
        await _log_admin_action(uid, "delete_all_open_countries", message=f"deleted={deleted}")
        await _edit_callback(query,
            f"🗑 Deleted {deleted} open countries.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Country & Pricing", callback_data="menu_country")]
            ])
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^cancel_delete_all_open$"))
    async def admin_action_cancel_delete_all_open(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        await query.answer()

        await _edit_callback(query,
            "✅ Cancelled. No countries were deleted.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Country & Pricing", callback_data="menu_country")]
            ])
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^disablecountries$"))
    async def admin_action_disablecountries(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        # HIGH-11 FIX: one accidental tap used to pause verification GLOBALLY
        # with no confirmation and no audit log. A mis-tap now lands on a
        # confirm step; the executor logs the action.
        total = len(_storage.get("countries", {}))
        await _edit_callback(query,
            "⚠️ **DISABLE ALL COUNTRIES?**\n\n"
            f"Countries: **{total}**\n"
            "Verification pauses GLOBALLY until countries are re-enabled.\n\n"
            "This action CANNOT be undone.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Disable All", callback_data="disablecountries_yes")],
                [InlineKeyboardButton("❌ Cancel", callback_data="menu_country")],
            ])
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^disablecountries_yes$"))
    async def admin_action_disablecountries_yes(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        await query.answer()

        count = disable_all_countries()
        await _log_admin_action(
            uid, "disable_all_countries",
            message=f"disabled {count} country(ies) - verification paused",
        )
        await _edit_callback(query, "--- DISABLE ALL COUNTRIES ---\nAll countries disabled. Verification paused.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Country & Pricing", callback_data="menu_country")]
            ]))


    @app.on_callback_query(admin_filter & filters.regex(r"^togglecountries$"))
    async def admin_action_togglecountries(client: Client, query: CallbackQuery):
        """Temporary master switch: pause/resume ALL countries at once."""
        uid = query.from_user.id

        await query.answer()

        from bot.utils.storage import is_all_countries_paused, toggle_all_countries_pause, get_open_countries
        from bot.handlers.admin.design import hd
        from bot.handlers.admin.menu import get_country_pricing_status_line

        paused = toggle_all_countries_pause()
        open_count = len(get_open_countries())

        await _log_admin_action(
            uid,
            "togglecountries",
            message=f"all_countries_paused={paused}",
            details={"open_countries_after": open_count},
        )

        status_line = get_country_pricing_status_line() if not paused \
            else "⏸️ All Countries paused - each country's previous state is saved."
        await _edit_callback(query,
            hd("🌍 Country & Pricing", "Main › Countries", status_line),
            reply_markup=get_country_pricing_buttons()
        )

    @app.on_message(ADMIN_TEXT_FILTER)
    async def handle_countries_input(client: Client, message: Message):
        user_id = message.from_user.id
        text = (message.text or "").strip()
        if user_id not in admin_state:
            message.continue_propagation()
            return
        action = admin_state[user_id]["action"]
        if action not in {"setrate_type", "setrate_all", "settimeout", "togglecountry", "set_user_rate", "view_custom_rates", "bulk_add_countries"}:
            message.continue_propagation()
            return
        message._handled_by_admin = True
        try:
            if action == "setrate_type":
                parts = text.split()
                if len(parts) < 2:
                    await _panel_reply(client, user_id, message, "❌ Format: +CountryCode Rate (e.g., +880 30)")
                    return
                country = parts[0].strip()
                country_err = _validate_admin_country_code(country)
                if country_err:
                    await _panel_reply(client, user_id, message, country_err)
                    return
                try:
                    rate = float(parts[1])
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ Rate must be a number! (e.g., 30)")
                    return
                if rate <= 0:
                    await _panel_reply(client, user_id, message, "❌ Rate must be a positive number! (0 or negative rates are not allowed)")
                    return
                rate_type = admin_state[user_id].get("rate_type", "spam_free_rate")
                
                rate_type_names = {
                    "spam_free_rate": "Spam Free",
                    "spam_rate": "Spam",
                    "limit_rate": "Limit"
                }
                type_name = rate_type_names.get(rate_type, "Unknown")
                
                # F-1 FIX: validate + re-clamp the hierarchy in the data layer.
                # The confirmation below shows the STORED (possibly re-clamped)
                # values, never the raw typed ones.
                try:
                    stored_rate = set_country_rate_by_type(country, rate_type, rate)
                except ValueError as ve:
                    await _panel_reply(client, user_id, message, f"❌ {ve}")
                    return
                
                rates = get_country_rates(country)
                await _log_admin_action(
                    user_id, "setrate_type", message=f"{country} {type_name}={stored_rate}"
                )
                await _panel_reply(client, user_id, message, 
                    f"✅ **{type_name} Rate set!**\n\n"
                    f"🌍 Country: {country}\n"
                    f"💵 {type_name} Rate: ${stored_rate}\n\n"
                    f"📊 **All Rates (stored):**\n"
                    f"✅ Spam Free: ${rates['spam_free_rate']}\n"
                    f"⚠️ Spam: ${rates['spam_rate']}\n"
                    f"🔒 Limit: ${rates['limit_rate']}"
                )
            
            elif action == "setrate_all":
                parts = text.split()
                if len(parts) < 4:
                    await _panel_reply(client, user_id, message, 
                        "❌ Format: +CountryCode SpamFreeRate SpamRate LimitRate\n\n"
                        "Example: `+880 30 15 10`"
                    )
                    return
                country = parts[0].strip()
                country_err = _validate_admin_country_code(country)
                if country_err:
                    await _panel_reply(client, user_id, message, country_err)
                    return
                try:
                    spam_free_rate = float(parts[1])
                    spam_rate = float(parts[2])
                    limit_rate = float(parts[3])
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ All rates must be numbers!\nExample: `+880 30 15 10`")
                    return
                
                set_country_rates(country, spam_free_rate, spam_rate, limit_rate)

                # MEDIUM-17: set_country_rates SILENTLY clamps the hierarchy
                # (e.g. 10/50/5 becomes 10/5/5). The confirmation used to echo
                # the TYPED values - show the stored truth instead.
                stored = get_country_rates(country)

                await _log_admin_action(
                    user_id,
                    "setrate_all",
                    message=f"{country} free={spam_free_rate} spam={spam_rate} limit={limit_rate}",
                )
                clamp_note = ""
                if (stored['spam_free_rate'], stored['spam_rate'], stored['limit_rate']) != (spam_free_rate, spam_rate, limit_rate):
                    clamp_note = (
                        "\n\n⚠️ Hierarchy auto-adjusted: rates must obey "
                        "Spam Free ≥ Limit ≥ Spam. The STORED values are shown below."
                    )
                await _panel_reply(client, user_id, message,
                    f"✅ **All Rates set!**\n\n"
                    f"🌍 Country: {country}{clamp_note}\n\n"
                    f"✅ Spam Free Rate: ${stored['spam_free_rate']}\n"
                    f"⚠️ Spam Rate: ${stored['spam_rate']}\n"
                    f"🔒 Limit Rate: ${stored['limit_rate']}\n\n"
                    f"Now when accounts in this country are verified:\n"
                    f"• Good account = ${stored['spam_free_rate']}\n"
                    f"• Spam account = ${stored['spam_rate']}\n"
                    f"• Limited account = ${stored['limit_rate']}"
                )

            elif action == "settimeout":
                parts = text.split()
                if len(parts) < 2:
                    await _panel_reply(client, user_id, message, "❌ Format: country_code seconds (e.g.: +880 60)")
                    return
                country = parts[0].strip()
                country_err = _validate_admin_country_code(country)
                if country_err:
                    await _panel_reply(client, user_id, message, country_err)
                    return
                try:
                    timeout_seconds = int(parts[1])
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ Seconds must be a number!")
                    return
                if timeout_seconds <= 0:
                    await _panel_reply(client, user_id, message, "❌ Seconds must be greater than 0!")
                    return
                if timeout_seconds > 3600:
                    await _panel_reply(client, user_id, message, "⚠️ Warning: Setting more than 1 hour!")
                success, msg = set_country_timeout(country, timeout_seconds)
                if not success:
                    await _panel_reply(client, user_id, message, msg)
                    return
                await _log_admin_action(
                    user_id, "settimeout", message=f"{country} {timeout_seconds}s"
                )
                await _panel_reply(client, user_id, message, f"{msg}\n\n✅ Verification for this country will happen after {timeout_seconds} seconds")


            elif action == "togglecountry":
                country = text.strip()
                country_err = _validate_admin_country_code(country)
                if country_err:
                    await _panel_reply(client, user_id, message, country_err)
                    return
                country_code, new_status = toggle_country_status(country)
                if country_code is None:
                    await _panel_reply(client, user_id, message, f"❌ Country {country} not found. Set rate first!")
                    return
                status_text = "🟢 OPEN" if new_status else "🔴 CLOSED"
                await _log_admin_action(
                    user_id, "togglecountry", message=f"{country_code} -> {status_text}"
                )
                await _panel_reply(client, user_id, message, f"✅ Country {country_code} is now {status_text}")

            elif action == "bulk_add_countries":
                lines = text.splitlines()
                added = 0
                skipped = 0
                errors = []
                for raw_line in lines:
                    line = raw_line.strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) < 4 or len(parts) > 6:
                        skipped += 1
                        errors.append(f"  ⚠️ {line} — invalid (need code + 3 rates + capacity + timeout)")
                        continue
                    country = parts[0].strip()
                    country_err = _validate_admin_country_code(country)
                    if country_err:
                        skipped += 1
                        errors.append(f"  ⚠️ {country} — invalid country code")
                        continue
                    try:
                        spam_free_rate = float(parts[1])
                        spam_rate = float(parts[2])
                        limit_rate = float(parts[3])
                    except ValueError:
                        skipped += 1
                        errors.append(f"  ⚠️ {line} — rates must be numbers")
                        continue
                    capacity = None
                    timeout = None
                    if len(parts) >= 5:
                        try:
                            capacity = int(parts[4])
                        except ValueError:
                            skipped += 1
                            errors.append(f"  ⚠️ {line} — capacity must be a number")
                            continue
                        if capacity < 0:
                            skipped += 1
                            errors.append(f"  ⚠️ {line} — capacity cannot be negative")
                            continue
                    if len(parts) >= 6:
                        try:
                            timeout = int(parts[5])
                        except ValueError:
                            skipped += 1
                            errors.append(f"  ⚠️ {line} — timeout must be a number")
                            continue
                        if timeout <= 0:
                            skipped += 1
                            errors.append(f"  ⚠️ {line} — timeout must be greater than 0")
                            continue
                        if timeout > 3600:
                            errors.append(f"  ⚠️ {country} — warning: timeout over 1 hour ({timeout}s)")
                    set_country_rates(
                        country,
                        spam_free_rate,
                        spam_rate,
                        limit_rate,
                        capacity=capacity,
                        timeout=timeout,
                    )
                    added += 1

                result_msg = (
                    f"✅ **Bulk Add Countries Complete**\n\n"
                    f"🟢 Added/Enabled: **{added}**\n"
                    f"⚪ Skipped: **{skipped}**\n\n"
                )
                if errors:
                    result_msg += "Details:\n" + "\n".join(errors[:10]) + "\n"
                result_msg += (
                    "\nAll added countries are now OPEN and ready for verification."
                )
                await _log_admin_action(
                    user_id, "bulk_add_countries", message=f"added={added} skipped={skipped}"
                )
                await _panel_reply(client, user_id, message, result_msg,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back to Country & Pricing", callback_data="menu_country")]
                    ])
                )

            elif action == "set_user_rate":
                try:
                    parts = text.strip().split()
                    if len(parts) != 3:
                        raise ValueError()

                    target_uid = int(parts[0])
                    country_code = parts[1].upper()
                    if not country_code.startswith('+'):
                        await _panel_reply(client, user_id, message,
                            "❌ Country code must start with + (e.g. +880)\n"
                            "Format: `123456789 +880 15.50`")
                        _admin_manager.clear_state(user_id)
                        return
                    price = float(parts[2])

                    if price < 0:
                        await _panel_reply(client, user_id, message, "❌ Price cannot be negative!")
                        _admin_manager.clear_state(user_id)
                        return

                    from bot.utils.country_data import COUNTRY_DATA, get_country_info
                    if country_code not in COUNTRY_DATA:
                        await _panel_reply(client, user_id, message,
                            f"❌ `{country_code}` is not a supported country.\n\n"
                            f"Supported country codes: {len(COUNTRY_DATA)} countries "
                            f"(e.g. +880, +1, +44).\n"
                            f"Format: `123456789 +880 15.50`")
                        _admin_manager.clear_state(user_id)
                        return

                    user_info = get_user_info(target_uid, create_if_missing=False)
                    if not user_info:
                        await _panel_reply(client, user_id, message, f"❌ User `{target_uid}` not found!")
                        _admin_manager.clear_state(user_id)
                        return

                    current_rates = user_info.get("custom_rates", {})
                    if current_rates is None:
                        current_rates = {}
                    current_rates[country_code] = price
                    update_user_info(target_uid, custom_rates=current_rates)
                    save_storage_now()

                    from bot.utils.storage import is_country_open
                    country_name, country_flag = get_country_info(country_code)
                    open_status = "🟢 Open" if is_country_open(country_code) else "🔴 Closed"

                    await _log_admin_action(
                        user_id,
                        "set_user_rate",
                        target_user_id=target_uid,
                        message=f"{country_code}={price:.2f}",
                    )
                    await _panel_reply(client, user_id, message,
                        f"✅ **Individual Rate Set!**\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"👤 User: `{target_uid}`\n"
                        f"🌍 Country: `{country_code}` {country_flag} ({country_name})\n"
                        f"📊 Country Status: {open_status}\n"
                        f"💰 New Price: ${price:.2f}\n\n"
                        f"ℹ️ This rate applies ONLY to `{country_code}` "
                        f"and only while the country is open."
                    )
                    _admin_manager.clear_state(user_id)
                except (ValueError, IndexError):
                    await _panel_reply(client, user_id, message, "❌ Invalid format! Use: `123456789 +880 15.50`")
                    _admin_manager.clear_state(user_id)
                    return

            elif action == "view_custom_rates":
                try:
                    view_uid = int(text.strip())
                except (ValueError, TypeError):
                    await _panel_reply(client, user_id, message, "❌ User ID must be a number!")
                    _admin_manager.clear_state(user_id)
                    return

                view_info = get_user_info(view_uid, create_if_missing=False)
                if not view_info:
                    await _panel_reply(client, user_id, message, f"❌ User `{view_uid}` not found!")
                    _admin_manager.clear_state(user_id)
                    return

                crates = view_info.get("custom_rates") or {}
                if not isinstance(crates, dict) or not crates:
                    await _panel_reply(client, user_id, message,
                        f"ℹ️ User `{view_uid}` has no custom rates set.\n\n"
                        f"Use 'Set Individual User Rate' to add one.")
                    _admin_manager.clear_state(user_id)
                    return

                from bot.utils.country_data import get_flag
                lines = [
                    f"👁️ **Custom Rates — User `{view_uid}`**\n",
                    "━━━━━━━━━━━━━━━━━━━━━\n",
                ]
                for code, price in sorted(crates.items()):
                    flag = get_flag(code)
                    try:
                        price_f = float(price)
                    except (TypeError, ValueError):
                        price_f = 0.0
                    lines.append(f"  {flag} {code} → ${price_f:.2f}")
                lines.append("\nTap a rate below to remove it, or 'Remove All'.")

                buttons = []
                for code in sorted(crates.keys()):
                    try:
                        price_f = float(crates[code])
                    except (TypeError, ValueError):
                        price_f = 0.0
                    buttons.append([
                        InlineKeyboardButton(
                            f"❌ Remove {code} (${price_f:.2f})",
                            callback_data=f"remove_custom_rate_{view_uid}_{code}"
                        )
                    ])
                buttons.append([
                    InlineKeyboardButton("🗑 Remove All", callback_data=f"remove_all_custom_rates_{view_uid}")
                ])
                buttons.append([InlineKeyboardButton("🔙 Back", callback_data="menu_country")])

                await _panel_reply(client, user_id, message, "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup(buttons))
                _admin_manager.clear_state(user_id)
                return

        except Exception as e:
            await _panel_reply(client, user_id, message, f"\u274c Error: {str(e)}")
            _admin_manager.clear_state(user_id)
            return
        _admin_manager.clear_state(user_id)


def register_countries_features(app: Client):

    @app.on_callback_query(admin_filter & filters.regex(r"^bulk_country_config$"))
    async def bulk_country_config_callback(client: Client, query: CallbackQuery):
        await query.answer()

        countries = _storage.get("countries", {})
        total = len(countries)
        open_count = sum(1 for d in countries.values() if d.get("open", False))

        buttons = [
            [InlineKeyboardButton("Export Country Config", callback_data="export_country_config"),
             InlineKeyboardButton("Import Country Config", callback_data="import_country_config")],
            [InlineKeyboardButton("Disable All Countries", callback_data="disablecountries"),
             InlineKeyboardButton("Enable All Configured", callback_data="enable_all_countries")],
            [InlineKeyboardButton("Back to Monitor", callback_data="realtime_refresh")],
        ]

        await _edit_callback(query,
            "=== BULK COUNTRY CONFIG ===\n\n"
            f"Total Configured: {total} | Open: {open_count} | Closed: {total - open_count}\n",
            reply_markup=InlineKeyboardMarkup(buttons)
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^export_country_config$"))
    async def export_country_config_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "Exporting..."))

        countries = _storage.get("countries", {})
        if not countries:
            await _edit_callback(query, 
                "No country configuration to export.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back", callback_data="bulk_country_config")],
                ])
            )
            return

        export_data = {}
        for code, data in sorted(countries.items()):
            export_data[code] = {
                "open": data.get("open", False),
                "spam_free_rate": data.get("spam_free_rate", data.get("rate", 0)),
                "spam_rate": data.get("spam_rate", 0),
                "limit_rate": data.get("limit_rate", 0),
                "capacity": data.get("capacity", 0),
                "timeout": data.get("timeout", 300),
            }

        json_str = json.dumps(export_data, indent=2, ensure_ascii=False)
        filename = "country_config.json"
        # MEDIUM-25: /tmp does not exist on Windows; use the OS temp dir.
        import tempfile
        filepath = os.path.join(tempfile.gettempdir(), filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(json_str)

        await client.send_document(
            chat_id=query.message.chat.id,
            document=filepath,
            caption=(
                f"Country Configuration Export\n"
                f"Countries: {len(export_data)}\n"
                f"Export Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Use Bulk Country Config to import."
            ),
            file_name=filename
        )

        try:
            os.remove(filepath)
        except OSError:
            pass


    @app.on_callback_query(admin_filter & filters.regex(r"^import_country_config$"))
    async def import_country_config_callback(client: Client, query: CallbackQuery):
        await query.answer()
        uid = query.from_user.id
        _set_admin_state(uid, query, "import_countries")
        await _edit_callback(query,
            "=== IMPORT COUNTRY CONFIG ===\n\n"
            "Send country config in JSON:\n\n"
            'Example: `{"+880": {"open": true, "spam_free_rate": 30, "spam_rate": 15, "limit_rate": 10, "capacity": 1000, "timeout": 60}}`\n\n'
            "Type /admin to cancel"
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^enable_all_countries$"))
    async def enable_all_countries_callback(client: Client, query: CallbackQuery):
        await query.answer()
        if not _storage.get("countries", {}):
            await _edit_callback(query, 
                "No countries configured!\n\n"
                "First set country rates via Country & Pricing.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back", callback_data="bulk_country_config")],
                ])
            )
            return

        # E-5 FIX: the inline loop previously mutated open=True without
        # _mark_dirty - the change never reached the DB and a restart rolled
        # everything back. The data-layer function marks + persists.
        stats = enable_all_countries()
        enabled = stats["enabled"]
        skipped = stats["skipped"]

        message = (
            f"{enabled} countries ENABLED!"
        )
        if skipped:
            message += (
                f"\n\n⚠️ {skipped} country(ies) skipped (permanently closed via "
                "Toggle Country). Set a new price for them to re-enable."
            )

        await _edit_callback(query, 
            message,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back", callback_data="bulk_country_config")],
            ])
        )

    # ============================================================
    # INPUT HANDLER for admin_features actions
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^set_user_rate_menu$"))
    async def set_user_rate_menu_callback(client: Client, query: CallbackQuery):
        await query.answer()
        from bot.handlers.admin.design import hd
        from bot.handlers.admin.menu import get_user_rate_menu_buttons
        await _edit_callback(query,
            hd("👤 Set User Rate", "Main › Countries › User Rate"),
            reply_markup=get_user_rate_menu_buttons()
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^add_custom_user_rate$"))
    async def add_custom_user_rate_callback(client: Client, query: CallbackQuery):
        await query.answer()
        user_id = query.from_user.id
        _set_admin_state(user_id, query, "set_user_rate")
        await _edit_callback(query,
            "👤 Set User Rate\n\n"
            "Send User ID, Country Code, Price:\n"
            "`123456789 +880 15.50`\n\n"
            "◀ Use /cancel to abort",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back to Set User Rate", callback_data="set_user_rate_menu")]
            ])
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^remove_custom_user_rate$"))
    async def remove_custom_user_rate_callback(client: Client, query: CallbackQuery):
        await query.answer()
        user_id = query.from_user.id
        _set_admin_state(user_id, query, "view_custom_rates")
        await _edit_callback(query,
            "➖ Remove Custom User Rate\n\n"
            "Send User ID:\n"
            "`123456789`\n\n"
            "◀ Use /cancel to abort",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back to Set User Rate", callback_data="set_user_rate_menu")]
            ])
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^see_all_custom_rates$"))
    async def see_all_custom_rates_callback(client: Client, query: CallbackQuery):
        await query.answer()
        users = _storage.get("users", {})
        from bot.utils.country_data import get_flag

        lines = [
            "👁️ **All Custom User Rates**\n",
            "━━━━━━━━━━━━━━━━━━━━━\n",
        ]
        found = 0
        for uid, data in sorted(users.items()):
            if not isinstance(data, dict):
                continue
            crates = data.get("custom_rates")
            if not isinstance(crates, dict) or not crates:
                continue
            found += 1
            rate_parts = []
            for code, price in sorted(crates.items()):
                flag = get_flag(code) if code != "other" else ""
                try:
                    price_f = float(price)
                except (TypeError, ValueError):
                    price_f = 0.0
                rate_parts.append(f"{flag} {code} ${price_f:.2f}")
            lines.append(f"`{uid}` — {', '.join(rate_parts)}")
            if found >= 30:
                break

        if found == 0:
            lines.append("No custom user rates set yet.")
        else:
            remaining = sum(
                1 for d in users.values()
                if isinstance(d, dict) and isinstance(d.get("custom_rates"), dict) and d.get("custom_rates")
            ) - found
            if remaining > 0:
                lines.append(f"\n... and {remaining} more user(s)")

        lines.append("\nUse 'Add Custom User Rate' to set a new rate.")
        buttons = [
            [InlineKeyboardButton("➕ Add Custom User Rate", callback_data="add_custom_user_rate")],
            [InlineKeyboardButton("◀ Back to Set User Rate", callback_data="set_user_rate_menu")],
        ]
        await _edit_callback(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))

    # ============================================================
    # FEATURE 2b: Remove Individual User Rates (via submenu)
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^remove_custom_rate_(\d+)_(\+\d+)$"))
    async def remove_custom_rate_callback(client: Client, query: CallbackQuery):
        await query.answer()
        import re as _re
        m = _re.match(r"^remove_custom_rate_(\d+)_(\+\d+)$", str(query.data))
        if not m:
            return
        ruid, rcode = m.group(1), m.group(2)
        rinfo = get_user_info(ruid, create_if_missing=False)
        if not rinfo:
            await _edit_callback(query, f"❌ User `{ruid}` not found.")
            return
        crates = dict(rinfo.get("custom_rates") or {})
        if rcode in crates:
            del crates[rcode]
            update_user_info(ruid, custom_rates=crates)
            save_storage_now()
            await _edit_callback(query,
                f"✅ Removed custom rate `{rcode}` for user `{ruid}`.\n\n"
                f"Remaining custom rates: {len(crates)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back to Country & Pricing", callback_data="menu_country")]
                ])
            )
        else:
            await _edit_callback(query,
                f"ℹ️ No custom rate found for `{rcode}`.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back to Country & Pricing", callback_data="menu_country")]
                ])
            )


    @app.on_callback_query(admin_filter & filters.regex(r"^remove_all_custom_rates_(\d+)$"))
    async def remove_all_custom_rates_callback(client: Client, query: CallbackQuery):
        await query.answer()
        import re as _re
        m = _re.match(r"^remove_all_custom_rates_(\d+)$", str(query.data))
        if not m:
            return
        ruid = m.group(1)
        rinfo = get_user_info(ruid, create_if_missing=False)
        if not rinfo:
            await _edit_callback(query, f"❌ User `{ruid}` not found.")
            return
        update_user_info(ruid, custom_rates={})
        save_storage_now()
        await _edit_callback(query,
            f"✅ Removed ALL custom rates for user `{ruid}`.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Country & Pricing", callback_data="menu_country")]
            ])
        )

    # ============================================================
    # FEATURE 4: User Activity Logs
    # ============================================================


    @app.on_message(ADMIN_TEXT_FILTER)
    async def handle_features_countries_input(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id not in admin_state:
            message.continue_propagation()
            return
        action = admin_state[user_id].get("action", "")
        if action not in {"import_countries"}:
            message.continue_propagation()
            return
        message._handled_by_admin = True
        text = message.text.strip()
        try:
            if action == "import_countries":
                try:
                    import_data = json.loads(text)
                    if not isinstance(import_data, dict):
                        await _panel_reply(client, user_id, message, "Invalid format: JSON must be a dictionary of country codes.")
                        _admin_manager.clear_state(user_id)
                        return

                    countries = _storage.get("countries", {})
                    imported = 0
                    skipped = 0
                    from bot.data.countries import validate_capacity
                    for code, data in import_data.items():
                        if not isinstance(data, dict):
                            skipped += 1
                            continue
                        try:
                            _cap = validate_capacity(data.get("capacity", 0))
                            _to = int(data.get("timeout", 300))
                        except (TypeError, ValueError):
                            skipped += 1
                            continue
                        countries[code] = {
                            "open": data.get("open", False),
                            "spam_free_rate": data.get("spam_free_rate", data.get("rate", 0)),
                            "spam_rate": data.get("spam_rate", 0),
                            "limit_rate": data.get("limit_rate", 0),
                            "capacity": _cap,
                            "timeout": _to,
                        }
                        # Imported config explicitly sets a capacity -> reset the
                        # persistent SUCCESS gate so a previously-filled country
                        # re-opens with a fresh count (no midnight auto-reset).
                        try:
                            from bot.data.counters import reset_daily_success_count
                            reset_daily_success_count(code)
                        except Exception:
                            pass
                        imported += 1

                    _storage["countries"] = countries
                    try:
                        from bot.data.state import _mark_dirty
                        _mark_dirty("countries", None)  # whole-table replace
                    except Exception:
                        pass
                    save_storage()

                    await _panel_reply(client, user_id, message, 
                        f"Country config imported!\n\n"
                        f"Imported: {imported}\n"
                        f"Skipped: {skipped}\n"
                        f"Total: {len(countries)} countries configured.",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("View All", callback_data="view_all_countries")],
                            [InlineKeyboardButton("Back to Config", callback_data="bulk_country_config")],
                        ])
                    )
                except json.JSONDecodeError:
                    await _panel_reply(client, user_id, message, 
                        "Invalid JSON format!\n\n"
                        "Make sure you send valid JSON with country codes as keys."
                    )
                _admin_manager.clear_state(user_id)

        except Exception as e:
            await _panel_reply(client, user_id, message, f"Error: {str(e)}")
            _admin_manager.clear_state(user_id)
