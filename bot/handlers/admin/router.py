# bot/handlers/admin/router.py - core admin infra + registration orchestrator
from pyrogram.client import Client
from pyrogram import filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified
from bot.data.state import _mark_settings_key_dirty
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
    toggle_country_status,
    get_required_channel,
    set_required_channel,
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
    _storage,
    SQLITE_STORAGE_PATH,
    save_storage,
    save_storage_now,
)
from bot.config import ADMIN_ID, SESSION_2FA_PASSWORD, get_2fa_password, CHANNEL_WITHDRAW_REPORT
from bot.utils.i18n import admin_t, admin_str, set_admin_lang, get_admin_lang
from bot.handlers.admin.design import hd, panel
import os
import json
import sqlite3
import logging
import asyncio
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------- Country Code Validation Helper ----------------

def _validate_admin_country_code(country: str):
    """
    Validate an admin-entered country code against the real country database.
    Returns None when valid, otherwise a friendly error message.
    """
    from bot.utils.country_data import is_supported_country_code

    country = (country or "").strip()
    if not country.startswith("+"):
        return (
            "❌ Country code must start with `+` (e.g. `+880`)\n\n"
            "Format: `+CountryCode Rate`  e.g. `+880 30`"
        )
    if not is_supported_country_code(country):
        return (
            f"❌ `{country}` is not a valid country code.\n\n"
            "It does not exist in the real country-code database. "
            "Check the code and try again.\n\n"
            "Format: `+CountryCode Rate`  e.g. `+880 30`"
        )
    return None


def _admin_country_label(country: str) -> str:
    """Country code + real country name + flag, for admin confirmations."""
    from bot.utils.country_data import get_country_info

    name, flag = get_country_info(country)
    return f"{country} {flag} ({name})"

# ---------------- Admin State Storage ----------------
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, List

# ============================================================
# MULTI-ADMIN SYSTEM - Custom Filter for All Admins
# ============================================================

def is_admin(user_id: int) -> bool:
    """
    Check if a user is an admin.
    Returns True if user is in admin list or is the primary ADMIN_ID.
    """
    if user_id == ADMIN_ID:
        return True
    admins = _storage.get("admins", [])
    return user_id in admins

def get_all_admins() -> List[int]:
    """Get list of all admin IDs including primary admin."""
    admins = list(_storage.get("admins", []))
    if ADMIN_ID not in admins:
        admins.insert(0, ADMIN_ID)
    return admins

def admin_filter_func(_, __, update) -> bool:
    """
    Custom filter function to check if user is an admin.
    Works with both Message and CallbackQuery.
    """
    if hasattr(update, 'from_user') and update.from_user:
        return is_admin(update.from_user.id)
    return False

# Create custom admin filter that works for all admins
admin_filter = filters.create(admin_filter_func, name="AdminFilter")

# Bot commands that must NEVER be treated as free-text input by the
# user verification, admin panel, or withdraw handlers.
# Centralized in ONE place so every text handler excludes the exact same
# command set -- this stops a single message (e.g. /cancel) from firing
# multiple handlers at once.
NON_TEXT_INPUT_COMMANDS = ["admin", "start", "account", "withdraw", "cap", "support", "cancel"]

# UTC offsets (whole hours) offered in the admin panel for the /account time.
UTC_OFFSETS = list(range(-12, 15))

async def _log_admin_action(
    admin_id: int,
    action: str,
    target_user_id: int = None,
    message: str = "",
    details: dict = None,
):
    """Persist an admin action to the SQLite admin_logs table (fire-and-forget)."""
    try:
        from bot.utils.database import log_admin_action
        await log_admin_action(admin_id, action, target_user_id, message, details)
    except Exception:
        pass

@dataclass
class AdminStateManager:
    """Thread-safe admin state manager."""
    _states: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    _monitor_enabled: bool = True
    _lock: threading.RLock = field(default_factory=threading.RLock)
    
    def get_state(self, user_id: int) -> Optional[Dict[str, str]]:
        """Get state for a specific admin user."""
        with self._lock:
            return self._states.get(user_id)
    
    def set_state(self, user_id: int, state: Dict[str, str]) -> None:
        """Set state for a specific admin user."""
        with self._lock:
            self._states[user_id] = state
    
    def clear_state(self, user_id: int) -> None:
        """Clear state for a specific admin user."""
        with self._lock:
            self._states.pop(user_id, None)

    def clear_all(self) -> None:
        """Clear state for every admin user."""
        with self._lock:
            self._states.clear()

    def get_monitor_status(self) -> bool:
        """Get current monitor status (storage-backed, HIGH-12 fix)."""
        with self._lock:
            try:
                from bot.data.state import _storage
                return bool(_storage.get("monitor_enabled", True))
            except Exception:
                return self._monitor_enabled

    def set_monitor_status(self, enabled: bool) -> None:
        """Set monitor status and persist it immediately (HIGH-12 fix).

        ``save_storage_now`` (not the 0.5s debounce) because the classic
        failure is exactly "toggle OFF then restart right away" - the state
        must already be on disk when the new process boots."""
        with self._lock:
            self._monitor_enabled = bool(enabled)
            try:
                from bot.data.state import _storage, _mark_settings_key_dirty, save_storage_now
                _storage["monitor_enabled"] = bool(enabled)
                _mark_settings_key_dirty("monitor_enabled")
                save_storage_now()
            except Exception:
                pass

    def toggle_monitor_status(self) -> bool:
        """Atomically toggle monitor status, persist it, return new state."""
        with self._lock:
            new_state = not self.get_monitor_status()
            self.set_monitor_status(new_state)
            return new_state

_admin_manager = AdminStateManager()
class _AdminStateProxy:
    """Thread-safe proxy for admin state dict that enforces RLock."""
    def __init__(self, manager: AdminStateManager):
        self._manager = manager
    
    def __contains__(self, user_id: int) -> bool:
        return self._manager.get_state(user_id) is not None
    
    def __getitem__(self, user_id: int):
        state = self._manager.get_state(user_id)
        if state is None:
            raise KeyError(user_id)
        return state
    
    def __setitem__(self, user_id: int, value: Dict[str, str]) -> None:
        self._manager.set_state(user_id, value)
    
    def __delitem__(self, user_id: int) -> None:
        self._manager.clear_state(user_id)
    
    def get(self, user_id: int, *args):
        state = self._manager.get_state(user_id)
        if state is not None:
            return state
        if args:
            return args[0]
        return None

    def clear(self) -> None:
        """Clear the admin state for every user."""
        self._manager.clear_all()

admin_state = _AdminStateProxy(_admin_manager)

# ============================================================
# IN-PLACE PANEL EDITING HELPERS
# The admin panel uses ONE message that edits in place instead
# of stacking new messages below.
# ============================================================

def _set_admin_state(user_id: int, query, action: str, **extra) -> None:
    """Set an admin's pending action and remember which message is the panel."""
    state = {
        "action": action,
        "panel_chat_id": query.message.chat.id,
        "panel_msg_id": query.message.id,
    }
    state.update(extra)
    admin_state[user_id] = state

def _get_panel_ref(user_id: int) -> Optional[tuple]:
    """Return (chat_id, message_id) of the admin panel message, if known."""
    state = admin_state.get(user_id)
    if not state:
        return None
    cid = state.get("panel_chat_id")
    mid = state.get("panel_msg_id")
    if cid and mid:
        return cid, mid
    return None

def _extract_admin_id(message) -> int:
    """Best-effort admin id from a message/query for language lookups.

    A callback/edited panel message is the BOT's own message, so its
    ``from_user`` is the bot, NOT the admin. In a private chat the chat id IS
    the admin's user id, so a positive chat id is preferred over ``from_user``.
    For a message the admin actually sent (text/command handlers) the sender is
    the admin, and for group callbacks the chat id is negative, so those fall
    back to ``from_user``.
    """
    try:
        if message is None:
            return 0
        chat = getattr(message, "chat", None)
        if chat is not None:
            chat_id = getattr(chat, "id", 0)
            if chat_id and chat_id > 0:
                return int(chat_id)
        sender = getattr(message, "from_user", None)
        if sender is not None:
            return int(sender.id)
        if chat is not None:
            return int(getattr(chat, "id", 0))
        return 0
    except Exception:
        return 0


def _localize(admin_id: int, text, reply_markup=None):
    """Translate panel text and button labels for an admin."""
    from bot.utils.i18n import admin_str, admin_translate_markup
    if admin_id <= 0:
        return text, reply_markup
    return admin_str(admin_id, text), admin_translate_markup(admin_id, reply_markup)

async def _edit_in_place(message, text, reply_markup=None, admin_id=None, **kwargs):
    """Edit the panel message in place; fall back to a new message when impossible.

    ``admin_id`` (the real admin, e.g. ``query.from_user.id``) is used for
    language localization. When omitted it is inferred from the message, which
    for callback messages is the bot's own message whose ``from_user`` is the
    bot - so explicit ``admin_id`` is strongly preferred from callbacks.
    """
    if not text:
        return None
    if admin_id is None:
        admin_id = _extract_admin_id(message)
    text, reply_markup = _localize(admin_id, text, reply_markup)
    if len(text) > 4096:
        try:
            return await message.reply(text, reply_markup=reply_markup, **kwargs)
        except Exception:
            return None
    try:
        return await message.edit_text(text, reply_markup=reply_markup, **kwargs)
    except MessageNotModified:
        try:
            return await message.edit_reply_markup(reply_markup)
        except Exception:
            return None
        return None
    except Exception:
        try:
            return await message.reply(text, reply_markup=reply_markup, **kwargs)
        except Exception:
            return None

async def _edit_callback(query, text, reply_markup=None, **kwargs):
    """Edit the panel message for a callback query, localized to its sender.

    Callback messages belong to the bot, so the admin id is taken explicitly
    from ``query.from_user.id`` to guarantee the admin's panel language applies
    everywhere (including group chats).
    """
    return await _edit_in_place(
        query.message,
        text,
        reply_markup=reply_markup,
        admin_id=query.from_user.id,
        **kwargs,
    )

async def _edit_by_ids(client, chat_id, message_id, text, reply_markup=None, admin_id=None, **kwargs):
    """Edit a message by ids; fall back to sending a new message when impossible."""
    if not text or not chat_id or not message_id:
        return None
    if admin_id is None:
        admin_id = int(chat_id) if chat_id and int(chat_id) > 0 else 0
    text, reply_markup = _localize(admin_id, text, reply_markup)
    if len(text) > 4096:
        try:
            return await client.send_message(chat_id, text, reply_markup=reply_markup, **kwargs)
        except Exception:
            return None
    try:
        return await client.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup, **kwargs)
    except MessageNotModified:
        try:
            return await client.edit_message_reply_markup(chat_id, message_id, reply_markup=reply_markup)
        except Exception:
            return None
        return None
    except Exception:
        try:
            return await client.send_message(chat_id, text, reply_markup=reply_markup, **kwargs)
        except Exception:
            return None

async def _panel_reply(client, user_id: int, fallback_message, text, reply_markup=None, **kwargs):
    """Reply to an admin action by editing the panel message in place."""
    text, reply_markup = _localize(int(user_id), text, reply_markup)
    ref = _get_panel_ref(user_id)
    if ref:
        cid, mid = ref
        res = await _edit_by_ids(client, cid, mid, text, reply_markup=reply_markup, admin_id=user_id, **kwargs)
        if res is not None:
            return res
    try:
        return await fallback_message.reply(text, reply_markup=reply_markup, **kwargs)
    except Exception:
        return None

def get_monitor_status() -> bool:
    """Get current monitor status."""
    return _admin_manager.get_monitor_status()

def set_monitor_status(enabled: bool) -> None:
    """Set monitor status."""
    _admin_manager.set_monitor_status(enabled)

def toggle_monitor_status() -> bool:
    """Atomically toggle monitor status and return new state."""
    return _admin_manager.toggle_monitor_status()

# ---------------- Split Package Wiring ----------------
# These imports live AFTER the shared infra above so the domain
# modules (which import back from bot.handlers.admin.router) resolve
# every shared name without circular-import errors.
ADMIN_TEXT_FILTER = admin_filter & filters.text & ~filters.command(NON_TEXT_INPUT_COMMANDS)
FEATURE_ACTIONS = {"search_phone", "search_country", "session_detail", "edit_mod_rules", "import_countries", "alert_threshold", "manual_export_count", "country_manual_export_count", "api_add_input"}

from bot.handlers.admin.menu import (
    get_admin_panel_buttons,
    get_settings_config_buttons,
)
from bot.handlers.admin.dashboard import _build_dashboard_message
from bot.handlers.admin.users import register_users, register_users_features
from bot.handlers.admin.countries import register_countries, register_countries_features
from bot.handlers.admin.withdraw import register_withdraw, register_withdraw_features
from bot.handlers.admin.monitor import register_monitor, register_monitor_features
from bot.handlers.admin.messages import register_messages
from bot.handlers.admin.channels import register_channels
from bot.handlers.admin.broadcast import register_broadcast, register_broadcast_features
from bot.handlers.admin.audit import register_audit, register_audit_features
from bot.handlers.admin.country_export import register_country_export
from bot.handlers.admin.session_modes import register_session_modes
from bot.handlers.admin.dashboard import register_dashboard_features
from bot.handlers.admin.proxy import register_proxy
from bot.handlers.admin.cards import register_cards

def register_core(app: Client):

    @app.on_message(filters.command("admin") & admin_filter)
    async def open_admin_panel(client: Client, message: Message):
        ref = _get_panel_ref(message.from_user.id)
        _admin_manager.clear_state(message.from_user.id)
        dashboard = _build_dashboard_message(message.from_user.id)
        if ref:
            cid, mid = ref
            res = await _edit_by_ids(client, cid, mid, dashboard, reply_markup=get_admin_panel_buttons(), admin_id=message.from_user.id)
            if res is not None:
                return
        dashboard, markup = _localize(message.from_user.id, dashboard, get_admin_panel_buttons())
        await message.reply_text(dashboard, reply_markup=markup)

    # ---------------- Cancel Operation ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^admin_cancel_operation$"))
    async def admin_cancel_operation_callback(client: Client, query: CallbackQuery):
        await query.answer()
        _admin_manager.clear_state(query.from_user.id)
        await _edit_callback(query,
            "✅ Operation cancelled.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Open Admin Panel", callback_data="back_admin")]
            ])
        )

    # ---------------- Category Menu Callbacks ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^menu_settings_config$"))
    async def menu_settings_config_callback(client: Client, query: CallbackQuery):
        await query.answer()
        await _edit_callback(query,
            hd("🔧 Settings & Configuration", "Main › Settings"),
            reply_markup=get_settings_config_buttons()
        )

    # ---------------- Admin Panel Language Selection ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^admin_lang_select$"))
    async def admin_lang_select_callback(client: Client, query: CallbackQuery):
        """Show the 13-language selection keyboard for the admin panel."""
        await query.answer()
        from bot.data.languages import LANGUAGES, build_language_label, build_language_grid_rows
        from pyrogram.types import InlineKeyboardButton
        current = get_admin_lang(query.from_user.id)
        buttons = []
        for row in build_language_grid_rows():
            row_buttons = []
            for code in row:
                label = build_language_label(code)
                if code == current:
                    label = f"✅ {label}"
                row_buttons.append(InlineKeyboardButton(label, callback_data=f"admin_lang_set_{code}"))
            buttons.append(row_buttons)
        buttons.append([InlineKeyboardButton("🔙 Back to Settings", callback_data="menu_settings_config")])
        text = admin_t("admin_language_select_message", query.from_user.id)
        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(buttons))

    @app.on_callback_query(admin_filter & filters.regex(r"^admin_lang_set_"))
    async def admin_lang_set_callback(client: Client, query: CallbackQuery):
        """Save the selected admin-panel language and confirm."""
        from bot.data.languages import LANGUAGES, build_language_label, is_supported_language
        data = query.data or ""
        code = data[len("admin_lang_set_"):]
        if not is_supported_language(code):
            await query.answer()
            return
        set_admin_lang(query.from_user.id, code)
        await query.answer()
        label = build_language_label(code)
        text = admin_t("admin_language_saved", query.from_user.id, language=label)
        await _edit_callback(query, text, reply_markup=get_settings_config_buttons())

    # ---------------- Core Features v2 Menu ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^open_core_features$"))
    async def open_core_features_callback(client: Client, query: CallbackQuery):
        await query.answer()
        from bot.handlers.admin.dashboard import _build_realtime_monitor
        from bot.handlers.admin.menu import get_realtime_buttons
        msg = _build_realtime_monitor(query.from_user.id)
        await _edit_callback(query, msg, reply_markup=get_realtime_buttons())

    # ============================================================
    # 🛠️ ADVANCED TOOLS - FULLY IMPLEMENTED CALLBACKS
    # ============================================================
    

    @app.on_callback_query(admin_filter & filters.regex(r"^set_2fa_password$"))
    async def set_2fa_password_callback(client: Client, query: CallbackQuery):
        """🔑 Set 2FA Password"""
        await query.answer()
        current_password = get_2fa_password()
        masked_password = current_password[:4] + "*" * (len(current_password) - 4) if len(current_password) > 4 else "***"

        await _edit_callback(query,
            hd("🔐 2FA Password", "Main › Settings › 2FA") + "\n\n"
            f"Current: `{masked_password}`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Set New Password", callback_data="set_2fa_password_input")],
                [InlineKeyboardButton("◀ Back to Settings", callback_data="menu_settings_config")]
            ])
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^set_2fa_password_input$"))
    async def set_2fa_password_input_callback(client: Client, query: CallbackQuery):
        """🔑 Set 2FA Password - Input Mode"""
        await query.answer()
        uid = query.from_user.id
        _set_admin_state(uid, query, "set_2fa_password")
        await _edit_callback(query,
            hd("🔑 Set New 2FA Password", "Main › Settings › 2FA › Set New") + "\n\n"
            "Enter new password (min 8 chars):\n"
            "❌ Type /admin to cancel"
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^country_wise_stats$"))
    async def country_wise_stats_callback(client: Client, query: CallbackQuery):
        """🌍 Country-wise Stats - Page 1"""
        await _show_country_stats_page(client, query, page=1)
    

    @app.on_callback_query(admin_filter & filters.regex(r"^country_stats_page_(\d+)$"))
    async def country_stats_page_callback(client: Client, query: CallbackQuery):
        """🌍 Country-wise Stats - Paginated"""
        import re
        data_str = str(query.data) if query.data else ""
        match = re.match(r"^country_stats_page_(\d+)$", data_str)
        page = int(match.group(1)) if match else 1
        await _show_country_stats_page(client, query, page=page)
    
    async def _show_country_stats_page(client: Client, query: CallbackQuery, page: int = 1):
        """Helper function to show country stats with pagination"""
        await query.answer()
        
        ITEMS_PER_PAGE = 10
        
        try:
            from bot.utils.country_data import get_flag
            
            session_tracker = _storage.get("session_tracker", {})
            if session_tracker is None:
                session_tracker = {}
            
            country_stats = {}
            
            for user_id, sessions in session_tracker.items():
                if not isinstance(sessions, list):
                    continue
                    
                for session in sessions:
                    if not isinstance(session, dict):
                        continue
                    
                    cc = session.get("country_code", "other") or "other"
                    
                    try:
                        amount = float(session.get("amount", 0) or 0)
                    except (ValueError, TypeError):
                        amount = 0.0
                    
                    if cc not in country_stats:
                        country_stats[cc] = {"count": 0, "earnings": 0.0}
                    country_stats[cc]["count"] += 1
                    country_stats[cc]["earnings"] += amount
            
            if not country_stats:
                await _edit_callback(query, 
                    hd("🗺️ Country-wise Stats", "Main › Settings › Country Stats") + "\n\n"
                    "No session data yet.\n"
                    "Stats will appear here when users verify.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("◀ Back to Settings", callback_data="menu_settings_config")]
                    ])
                )
                return
            
            sorted_stats = sorted(country_stats.items(), key=lambda x: x[1].get("count", 0), reverse=True)
            
            total_sessions = sum(s.get("count", 0) for s in country_stats.values())
            total_earnings = sum(s.get("earnings", 0) for s in country_stats.values())
            total_countries = len(country_stats)
            
            total_pages = (total_countries + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
            page = max(1, min(page, total_pages))
            
            start_idx = (page - 1) * ITEMS_PER_PAGE
            end_idx = start_idx + ITEMS_PER_PAGE
            page_stats = sorted_stats[start_idx:end_idx]
            
            lines = []
            for idx, (cc, stats) in enumerate(page_stats, start=start_idx + 1):
                try:
                    flag = get_flag(cc) if cc and cc != "other" else "-"
                except (KeyError, IndexError):
                    flag = "-"
                count = stats.get("count", 0)
                earnings = stats.get("earnings", 0.0)
                lines.append(f"{idx}. {flag} {cc}: {count} sessions | ${earnings:.2f}")
            
            pagination_buttons = []
            if page > 1:
                pagination_buttons.append(InlineKeyboardButton("Previous", callback_data=f"country_stats_page_{page - 1}"))
            if page < total_pages:
                pagination_buttons.append(InlineKeyboardButton("Next", callback_data=f"country_stats_page_{page + 1}"))
            
            buttons = []
            if pagination_buttons:
                buttons.append(pagination_buttons)
            buttons.append([InlineKeyboardButton("◀ Back to Settings", callback_data="menu_settings_config")])
            
            await _edit_callback(query, 
                hd("🗺️ Country-wise Stats", "Main › Settings › Country Stats") + "\n"
                f"Sessions: {total_sessions}\n"
                f"Earnings: ${total_earnings:.2f}\n"
                f"Countries: {total_countries}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Page {page}/{total_pages}:\n" + "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            await _edit_callback(query, 
                hd("🗺️ Country-wise Stats", "Main › Settings › Country Stats") + f"\n\nError: {str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ Back to Settings", callback_data="menu_settings_config")]
                ])
            )


    @app.on_callback_query(admin_filter & filters.regex(r"^daily_report_toggle$"))
    async def daily_report_toggle_callback(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            current_status = _storage.get("daily_report_enabled", False)
            new_status = not current_status
            _storage["daily_report_enabled"] = new_status
            _mark_settings_key_dirty("daily_report_enabled")
            save_storage()

            from bot.handlers.admin.design import hd, sw
            await _edit_callback(query,
                hd("📊 Daily Report", "Main › Settings › Daily Report", sw("Daily Report", new_status)),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(sw("Daily Report", new_status), callback_data="daily_report_toggle")],
                    [InlineKeyboardButton("◀ Back to Settings", callback_data="menu_settings_config")]
                ])
            )
        except Exception as e:
            await _edit_callback(query, f"Toggle Error: {str(e)}")


    @app.on_callback_query(admin_filter & filters.regex(r"^account_timezone$"))
    async def account_timezone_callback(client: Client, query: CallbackQuery):
        """Show the UTC timezone selection grid for the /account time."""
        await query.answer()
        from bot.data.settings import get_account_timezone_label, get_account_timezone_offset
        current = get_account_timezone_label()
        text = (
            hd("🕐 Account Timezone", "Main › Settings › Timezone") + "\n\n"
            f"Current: {current}\n"
            "Select the /account time UTC:"
        )
        current_offset = get_account_timezone_offset()
        buttons = []
        row = []
        for offset in UTC_OFFSETS:
            label = f"UTC{offset:+d}"
            if offset == current_offset:
                label = f"✅ {label}"
            row.append(InlineKeyboardButton(label, callback_data=f"account_tz_set_{offset}"))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("◀ Back to Settings", callback_data="menu_settings_config")])
        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(buttons))

    @app.on_callback_query(admin_filter & filters.regex(r"^account_tz_set_(-?\d+)$"))
    async def account_tz_set_callback(client: Client, query: CallbackQuery):
        """Save the admin-selected UTC offset for the /account time."""
        await query.answer()
        match = query.matches[0] if query.matches else None
        if not match:
            await _edit_callback(query, "❌ Invalid request")
            return
        offset = int(match.group(1))
        from bot.data.settings import set_account_timezone_offset
        set_account_timezone_offset(offset)
        await _edit_callback(query,
            "✅ *Timezone Updated*\n\n"
            f"The /account command will now show: `UTC{offset:+d}`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🕐 Account Timezone", callback_data="account_timezone")],
                [InlineKeyboardButton("◀ Back to Settings", callback_data="menu_settings_config")]
            ])
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^add_admin_input$"))
    async def add_admin_input_callback(client: Client, query: CallbackQuery):
        await query.answer()
        uid = query.from_user.id
        _set_admin_state(uid, query, "add_new_admin")
        await _edit_callback(query, 
            hd("👥 Add Admin", "Main › Settings › Admins › Add") + "\n\n"
            "Enter new admin User ID:\n"
            "Example: 123456789\n\n"
            "Type /admin to cancel"
        )

    # ---------------- Remove Admin Callbacks ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^remove_admin$"))
    async def remove_admin_callback(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            admins = _storage.get("admins", [ADMIN_ID])
            if ADMIN_ID not in admins:
                admins.append(ADMIN_ID)
                _storage["admins"] = admins
                _mark_settings_key_dirty("admins")
                save_storage()
            
            if len(admins) <= 1:
                await _edit_callback(query,
                    "--- REMOVE ADMIN ---\n\n"
                    "Only primary admin exists.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Back to Settings", callback_data="menu_settings_config")]
                    ])
                )
                return

            buttons = []
            for admin_id in admins:
                if admin_id == ADMIN_ID:
                    buttons.append([InlineKeyboardButton(f"{admin_id} (Primary - Protected)", callback_data="protected_admin")])
                else:
                    buttons.append([InlineKeyboardButton(f"Remove {admin_id}", callback_data=f"confirm_remove_admin_{admin_id}")])

            buttons.append([InlineKeyboardButton("◀ Back to Settings", callback_data="menu_settings_config")])

            await _edit_callback(query,
                hd("👥 Remove Admin", "Main › Settings › Admins › Remove") + "\n\n"
                f"Total Admins: {len(admins)}",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            await _edit_callback(query, f"❌ Error: {str(e)}")


    @app.on_callback_query(admin_filter & filters.regex(r"^protected_admin$"))
    async def protected_admin_callback(client: Client, query: CallbackQuery):
        """Primary Admin protection notice"""
        await query.answer(admin_str(query.from_user.id, "⚠️ Primary Admin cannot be removed!"), show_alert=True)


    @app.on_callback_query(admin_filter & filters.regex(r"^confirm_remove_admin_(\d+)$"))
    async def confirm_remove_admin_callback(client: Client, query: CallbackQuery):
        """❌ Confirm Remove Admin"""
        await query.answer()
        try:
            match = query.matches[0] if query.matches else None
            if not match:
                await _edit_callback(query, "❌ Invalid request")
                return
            
            admin_to_remove = int(match.group(1))
            
            if admin_to_remove == ADMIN_ID:
                await _edit_callback(query, 
                    hd("⛔ Restricted", "Main › Settings › Admins › Remove") + "\n\n"
                    "Primary Admin cannot be removed!",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("◀ Back to Settings", callback_data="menu_settings_config")]
                    ])
                )
                return
            
            admins = _storage.get("admins", [ADMIN_ID])
            
            if admin_to_remove not in admins:
                await _edit_callback(query, 
                    f"User `{admin_to_remove}` is not in the admin list!",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("◀ Back to Settings", callback_data="menu_settings_config")]
                    ])
                )
                return
            
            admins.remove(admin_to_remove)
            _storage["admins"] = admins
            _mark_settings_key_dirty("admins")
            save_storage()
            
            admin_lines = [f"`{aid}`" + (" (Primary)" if aid == ADMIN_ID else "") for aid in admins]
            
            await _edit_callback(query, 
                hd("✅ Admin Removed", "Main › Settings › Admins") + "\n\n"
                f"Removed: `{admin_to_remove}`\n"
                f"Remaining: {len(admins)}\n\n"
                "Admin List:\n" + "\n".join(admin_lines),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Remove Another", callback_data="remove_admin")],
                    [InlineKeyboardButton("◀ Back to Settings", callback_data="menu_settings_config")]
                ])
            )
        except Exception as e:
            await _edit_callback(query, f"Error: {str(e)}")

    # ---------------- Session Export Callbacks ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^back_admin$"))
    async def back_to_admin_callback(client: Client, query: CallbackQuery):
        await query.answer()
        await _edit_callback(query, _build_dashboard_message(query.from_user.id), reply_markup=get_admin_panel_buttons())

    # ---------------- Set Country Rate Type Callbacks ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^multiadmin_mgmt$"))
    async def multi_admin_mgmt_callback(client: Client, query: CallbackQuery):
        await query.answer()
        
        admins = _storage.get("admins", [ADMIN_ID])

        text = hd("👥 Multi-Admin Management", "Main › Settings › Admins") + "\n\n"
        text += f"Total Admins: `{len(admins)}`\n"
        for admin_id in admins:
            admin_type = admin_str(query.from_user.id, "👑 Owner") if admin_id == ADMIN_ID else admin_str(query.from_user.id, "👤 Admin")
            text += f"  `{admin_id}` - {admin_type}\n"

        buttons = [
            [InlineKeyboardButton("➕ Add Admin", callback_data="add_admin_input"),
             InlineKeyboardButton("➖ Remove Admin", callback_data="remove_admin")],
            [InlineKeyboardButton("◀ Back to Settings", callback_data="menu_settings_config")]
        ]

        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(buttons))

    # ============================================================
    # FEATURE 3: Send Sessions to User
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^show_2fa_secret$"))
    async def admin_action_show_2fa_secret(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        current = get_2fa_password() or ""
        if not current:
            await _edit_callback(query,
                hd("👁️ View 2FA Password", "Main › Settings › 2FA") + "\n\n"
                "❌ No 2FA password is set yet.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ Back to Settings", callback_data="menu_settings_config")]
                ])
            )
        else:
            await _edit_callback(query,
                hd("👁️ View 2FA Password", "Main › Settings › 2FA") + "\n\n"
                f"`{current}`"
            )

    @app.on_message(ADMIN_TEXT_FILTER)
    async def handle_settings_input(client: Client, message: Message):
        user_id = message.from_user.id
        text = (message.text or "").strip()
        if user_id not in admin_state:
            message.continue_propagation()
            return
        action = admin_state[user_id]["action"]
        if action not in {"set_2fa_password", "add_new_admin"}:
            message.continue_propagation()
            return
        message._handled_by_admin = True
        try:
            if action == "set_2fa_password":
                new_password = text.strip()
                if len(new_password) < 8:
                    await _panel_reply(client, user_id, message, 
                        "❌ **Password too short!**\n\n"
                        "Password must be at least 8 characters.\n"
                        "Try again or type /admin to cancel."
                    )
                    return
                
                _storage["_2fa_password_persistent"] = new_password
                _mark_settings_key_dirty("_2fa_password_persistent")
                save_storage()

                await _panel_reply(client, user_id, message, 
                    f"✅ **2FA Password Updated!**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🔑 **New Password:** `{new_password[:4]}{'*' * (len(new_password) - 4)}`\n\n"
                    f"✅ This password is now active immediately for all new verifications.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back to Settings", callback_data="menu_settings_config")]
                    ])
                )
                _admin_manager.clear_state(user_id)

            elif action == "add_new_admin":
                try:
                    new_admin_id = int(text.strip())
                    
                    admins = _storage.get("admins", [ADMIN_ID])
                    if ADMIN_ID not in admins:
                        admins.append(ADMIN_ID)
                    
                    if new_admin_id in admins:
                        await _panel_reply(client, user_id, message, f"⚠️ User `{new_admin_id}` is already an Admin!")
                        _admin_manager.clear_state(user_id)
                        return
                    
                    admins.append(new_admin_id)
                    _storage["admins"] = admins
                    _mark_settings_key_dirty("admins")
                    save_storage()
                    
                    await _log_admin_action(
                        user_id, "add_new_admin", target_user_id=new_admin_id
                    )
                    admin_lines = [f"👤 `{admin_id}`" for admin_id in admins]
                    
                    await _panel_reply(client, user_id, message, 
                        f"✅ **New Admin Added!**\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"👑 New Admin: `{new_admin_id}`\n\n"
                        f"📊 **Total Admins:** {len(admins)}\n\n"
                        f"**Admin List:**\n" + "\n".join(admin_lines),
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔙 Back to Settings", callback_data="menu_settings_config")]
                        ])
                    )
                    _admin_manager.clear_state(user_id)
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ Enter a valid User ID (numbers only)")
                    _admin_manager.clear_state(user_id)
                    return

        except Exception as e:
            await _panel_reply(client, user_id, message, f"\u274c Error: {str(e)}")
            _admin_manager.clear_state(user_id)
            return
        _admin_manager.clear_state(user_id)


def register_admin_commands(app: Client):

    @app.on_message(filters.command("cancel") & admin_filter)
    async def cancel_command(client: Client, message: Message):
        _admin_manager.clear_state(message.from_user.id)
        try:
            from bot.utils.session_handler import cleanup_session
            await cleanup_session(message.from_user.id)
        except Exception:
            pass
        await message.reply(admin_str(message.from_user.id, "✅ Operation cancelled.\n\nAny pending input or verification session has been cleared."))
        # Hard stop: /cancel must never reach any other handler
        # (e.g. main.handle_text_input in group 1).
        message.stop_propagation()
    

    @app.on_message(filters.command("revenue") & admin_filter)
    async def revenue_command(client: Client, message: Message):
        args = message.text.split()
        period = args[1].lower() if len(args) > 1 else "today"

        try:
            from bot.utils.storage import get_session_revenue
            if period == "today":
                revenue = get_session_revenue("today")
                label = admin_str(message.from_user.id, "Today")
            elif period == "week":
                revenue = get_session_revenue("week")
                label = admin_str(message.from_user.id, "Last 7 Days")
            elif period == "month":
                revenue = get_session_revenue("month")
                label = admin_str(message.from_user.id, "Last 30 Days")
            else:
                await message.reply(admin_str(message.from_user.id, "Usage: `/revenue today|week|month`"))
                return
        except Exception:
            revenue = 0.0
            label = admin_str(message.from_user.id, "Today")

        text = admin_t("admin_revenue_title", message.from_user.id) + "\n"
        text += "━━━━━━━━━━━━━━━━━━━━━\n\n"
        text += admin_t("admin_revenue_line", message.from_user.id, label=label, revenue=f"{revenue:.2f}") + "\n\n"
        text += admin_t("admin_revenue_note", message.from_user.id)

        await message.reply(text)
    

    @app.on_message(filters.command("alert") & admin_filter)
    async def alert_command(client: Client, message: Message):
        args = message.text.split()
        if len(args) != 3:
            await message.reply(admin_str(message.from_user.id, "Usage: `/alert low_balance true` or `/alert high_reject_rate false`"))
            return
        
        alert_type = args[1].lower()
        enabled = args[2].lower() == "true"
        
        if "alert_settings" not in _storage:
            _storage["alert_settings"] = {
                "low_balance_alert": True,
                "high_reject_rate_alert": True,
                "api_error_alert": True,
            }
        
        if alert_type in _storage["alert_settings"]:
            _storage["alert_settings"][alert_type] = enabled
            _mark_settings_key_dirty("alert_settings")
            save_storage()
            alert_label = admin_str(
                message.from_user.id,
                {
                    "low_balance_alert": "Low Balance Alert",
                    "high_reject_rate_alert": "High Reject Rate Alert",
                    "api_error_alert": "Api Error Alert",
                }.get(alert_type, alert_type.replace('_', ' ').title()),
            )
            status = admin_str(message.from_user.id, "enabled" if enabled else "disabled")
            await message.reply(admin_t("admin_alert_toggled", message.from_user.id, alert=alert_label, status=status))
        else:
            await message.reply(admin_t("admin_alert_unknown", message.from_user.id, alert_type=alert_type))
    

    @app.on_message(filters.command("alert_threshold") & admin_filter)
    async def alert_threshold_command(client: Client, message: Message):
        args = message.text.split()
        if len(args) != 3:
            await message.reply(admin_str(message.from_user.id, "Usage: `/alert_threshold low_balance 100`"))
            return
        
        alert_type = args[1].lower()
        try:
            threshold = float(args[2])
            
            if "alert_thresholds" not in _storage:
                _storage["alert_thresholds"] = {}
            
            _storage["alert_thresholds"][alert_type] = threshold
            _mark_settings_key_dirty("alert_thresholds")
            save_storage()
            
            alert_label = admin_str(
                message.from_user.id,
                {
                    "low_balance_alert": "Low Balance Alert",
                    "high_reject_rate_alert": "High Reject Rate Alert",
                    "api_error_alert": "Api Error Alert",
                }.get(alert_type, alert_type.replace('_', ' ').title()),
            )
            await message.reply(admin_t("admin_threshold_set", message.from_user.id, alert=alert_label, threshold=threshold))
        except ValueError:
            await message.reply(admin_str(message.from_user.id, "❌ Invalid threshold value"))
    

    @app.on_message(filters.command("addadmin") & admin_filter)
    async def add_admin_command(client: Client, message: Message):
        args = message.text.split()
        if len(args) != 2:
            await message.reply(admin_str(message.from_user.id, "Usage: `/addadmin 123456789`"))
            return
        
        try:
            new_admin_id = int(args[1])
            
            admins = _storage.get("admins", [ADMIN_ID])
            if ADMIN_ID not in admins:
                admins.append(ADMIN_ID)
            
            if new_admin_id in admins:
                await message.reply(admin_t("admin_already_admin", message.from_user.id, user_id=new_admin_id))
                return
            
            admins.append(new_admin_id)
            _storage["admins"] = admins
            _mark_settings_key_dirty("admins")
            save_storage()
            
            await message.reply(admin_t("admin_added", message.from_user.id, user_id=new_admin_id))
        except ValueError:
            await message.reply(admin_str(message.from_user.id, "❌ Invalid User ID"))
    

    @app.on_message(filters.command("removeadmin") & admin_filter)
    async def remove_admin_command(client: Client, message: Message):
        args = message.text.split()
        if len(args) != 2:
            await message.reply(admin_str(message.from_user.id, "Usage: `/removeadmin 123456789`"))
            return
        
        try:
            admin_id = int(args[1])
            
            admins = _storage.get("admins", [ADMIN_ID])
            if admin_id not in admins:
                await message.reply(admin_t("admin_not_admin", message.from_user.id, user_id=admin_id))
                return
            
            if admin_id == ADMIN_ID:
                await message.reply(admin_str(message.from_user.id, "❌ Cannot remove the main admin!"))
                return
            
            admins.remove(admin_id)
            _storage["admins"] = admins
            _mark_settings_key_dirty("admins")
            save_storage()
            
            await message.reply(admin_t("admin_removed", message.from_user.id, user_id=admin_id))
        except ValueError:
            await message.reply(admin_str(message.from_user.id, "❌ Invalid User ID"))
    

    @app.on_message(filters.command("adminlist") & admin_filter)
    async def admin_list_command(client: Client, message: Message):
        admins = _storage.get("admins", [ADMIN_ID])
        
        text = admin_t("admin_list_title", message.from_user.id) + "\n"
        text += "━━━━━━━━━━━━━━━━━━━━━\n\n"
        text += admin_t("admin_list_total", message.from_user.id, count=len(admins)) + "\n\n"
        
        for admin_id in admins:
            admin_type = admin_t("admin_owner_label", message.from_user.id) if admin_id == ADMIN_ID else admin_t("admin_label", message.from_user.id)
            text += f"  `{admin_id}` - {admin_type}\n"
        
        await message.reply(text)



def register_admin_text_fallback(app: Client):
    @app.on_message(ADMIN_TEXT_FILTER)
    async def admin_text_fallback(client: Client, message: Message):
        # P4-47 STALE-STATE GATE. This handler is the LAST admin text handler
        # to run: by now every real input handler (settings, users, proxy,
        # core_config, ...) has already consumed its own action and either
        # processed the text or continue_propagation()-ed it. Reaching here
        # with an admin_state entry therefore means the state is STALE - a
        # view/no-input action (or a bug) that no handler owns.
        #
        # The old behavior returned silently after setting
        # ``message._handled_by_admin = True``. A plain ``return`` STOPS
        # pyrogram propagation, so the user-verification handler (group 1)
        # never saw the message: an admin who browsed the admin panel and
        # then typed their PHONE NUMBER had that number swallowed with no
        # reply at all - no code was ever sent ("start-এর পরে নম্বর মরে যায়").
        #
        # The fix: clear the stale state and CONTINUE propagation so the
        # message reaches the normal user flow (phone -> code sent), exactly
        # like a non-admin user. The poison flag is set ONLY on the paths
        # that genuinely consumed admin input, never here.
        user_id = message.from_user.id
        if user_id not in admin_state:
            message.continue_propagation()
            return
        action = admin_state[user_id].get("action", "")
        if action in FEATURE_ACTIONS:
            # A real input action that reached the fallback unhandled: keep
            # the flag semantics (do not let group-1 treat it as user input),
            # but still continue so the message is not silently lost.
            message._handled_by_admin = True
            message.continue_propagation()
            return
        # Stale view/unknown action: clear it and let the user flow proceed.
        _admin_manager.clear_state(user_id)
        message.continue_propagation()

def register_admin_features(app: Client):
    """Register the v2 feature handlers (dashboard, search, audit, etc.)."""
    register_dashboard_features(app)
    register_users_features(app)
    register_audit_features(app)
    register_monitor_features(app)
    register_countries_features(app)
    register_withdraw_features(app)
    register_broadcast_features(app)
    from bot.handlers.admin.core_config import register_core_config
    register_core_config(app)

def register_admin_handlers(app: Client):
    """Register every admin panel handler, preserving original order semantics.

    Ordering rules kept from the original monolith:
      * admin text handlers come before the features text handlers,
      * the text fallback runs after all text handlers (clears stale state),
      * command handlers are registered last.
    """
    register_core(app)
    register_users(app)
    register_countries(app)
    register_withdraw(app)
    register_monitor(app)
    register_messages(app)
    register_channels(app)
    register_broadcast(app)
    register_audit(app)
    register_country_export(app)
    register_session_modes(app)
    register_proxy(app)
    register_cards(app)
    register_admin_features(app)
    register_admin_text_fallback(app)
    register_admin_commands(app)

