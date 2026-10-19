# bot/handlers/admin/channels.py - channel management (required + report channels)

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
    toggle_country_status,
    get_required_channel,
    set_required_channel,
    get_channel_override,
    set_channel_override,
    clear_channel_override,
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
from bot.config import (
    ADMIN_ID,
    SESSION_2FA_PASSWORD,
    get_2fa_password,
    CHANNEL_WITHDRAW_REPORT,
    EXTRA_APIS,
    API_ID,
    CHANNEL_KEYS,
    CHANNEL_LABELS,
    get_channel,
)
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


def _validate_channel_input(text: str):
    """Validate a channel value entered by the admin.

    Accepts a username (``@channel``) or a numeric channel ID (e.g.
    ``-1001234567890`` / ``1001234567890``). Returns ``(value, None)`` on
    success or ``(None, error_message)`` on failure.
    """
    val = (text or "").strip()
    if not val:
        return None, "Channel value cannot be empty."
    if val.startswith("@"):
        if len(val) < 2:
            return None, "Channel username must include the name after @ (e.g. @newchannel)"
        return val, None
    digits = val.lstrip("-")
    if digits.isdigit():
        try:
            return int(val), None
        except ValueError:
            return None, "Invalid numeric channel ID."
    return None, "Channel must be a username like @channel or a numeric ID like -1001234567890"


def _channel_display_value(name: str) -> str:
    """Effective value for a channel + where it comes from (Override/Config)."""
    override = get_channel_override(name)
    if override:
        return f"`{override}` (Override)"
    val = get_channel(name)
    if val in (None, "", 0):
        return "`Not set`"
    return f"`{val}` (Config)"


def _build_channel_management_text() -> str:
    """Text listing every configurable channel and its current value."""
    lines = ["--- CHANNEL MANAGEMENT ---\n"]
    for name in CHANNEL_KEYS:
        label = CHANNEL_LABELS.get(name, name)
        lines.append(f"{label}\n   {_channel_display_value(name)}\n")
    return "\n".join(lines)


def _build_channel_management_buttons() -> InlineKeyboardMarkup:
    """Buttons to change / reset every configurable channel."""
    rows = []
    for name in CHANNEL_KEYS:
        row = [InlineKeyboardButton(f"✏️ {name.replace('CHANNEL_', '').replace('_', ' ')}",
                                    callback_data=f"channel_change_{name}")]
        if get_channel_override(name):
            row.append(InlineKeyboardButton("↩️ Reset", callback_data=f"channel_reset_{name}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("Back to Messaging", callback_data="menu_messaging")])
    rows.append([InlineKeyboardButton("Back to Main Menu", callback_data="back_admin")])
    return InlineKeyboardMarkup(rows)


def register_channels(app: Client):

    # ---------------- Channel Management Menu ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^menu_channels$"))
    async def menu_channels_callback(client: Client, query: CallbackQuery):
        await query.answer()
        from bot.handlers.admin.design import hd
        text = _build_channel_management_text()
        await _edit_callback(query,
            hd("📺 Channel Management", "Main › Messaging › Channels") + "\n\n" + text,
            reply_markup=_build_channel_management_buttons()
        )

    # ---------------- Change a specific channel ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^channel_change_([A-Z0-9_]+)$"))
    async def channel_change_callback(client: Client, query: CallbackQuery):
        await query.answer()
        import re as _re
        m = _re.match(r"^channel_change_([A-Z0-9_]+)$", str(query.data))
        if not m:
            return
        name = m.group(1)
        if name not in CHANNEL_KEYS:
            await _edit_callback(query, f"❌ Unknown channel: {name}")
            return

        uid = query.from_user.id
        current = get_channel(name)
        _set_admin_state(uid, query, "set_channel_input", channel_key=name)

        await _edit_callback(query,
            "--- CHANGE CHANNEL ---\n\n"
            f"{CHANNEL_LABELS.get(name, name)}\n"
            f"Current: `{current if current not in (None, '', 0) else 'Not set'}`\n\n"
            "Enter new channel (`@channel` or `-100...`):\n\n"
            "Type /admin to cancel"
        )

    # ---------------- Reset a channel override ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^channel_reset_([A-Z0-9_]+)$"))
    async def channel_reset_callback(client: Client, query: CallbackQuery):
        await query.answer()
        import re as _re
        m = _re.match(r"^channel_reset_([A-Z0-9_]+)$", str(query.data))
        if not m:
            return
        name = m.group(1)
        if name not in CHANNEL_KEYS:
            await _edit_callback(query, f"❌ Unknown channel: {name}")
            return

        if clear_channel_override(name):
            await _log_admin_action(query.from_user.id, "channel_reset", message=f"{name} -> Config")
            await _edit_callback(query,
                f"✅ `{name}` reset to config value.\n\n"
                f"{CHANNEL_LABELS.get(name, name)}\n"
                f"Now: {_channel_display_value(name)}",
                reply_markup=_build_channel_management_buttons()
            )
        else:
            await _edit_callback(query,
                f"ℹ️ `{name}` has no override to reset.",
                reply_markup=_build_channel_management_buttons()
            )

    # ---------------- Legacy "Set Channel" (required channel) ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^setchannel$"))
    async def admin_action_setchannel(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        current = get_required_channel()
        _set_admin_state(uid, query, "setchannel")
        await _edit_callback(query,
            "--- SET REQUIRED CHANNEL ---\n\n"
            f"Current: `{current if current else 'Not set'}`\n\n"
            "Enter new channel (`@channel` or `-100...`):"
        )

    # ---------------- Text Input Handling ----------------

    @app.on_message(ADMIN_TEXT_FILTER)
    async def handle_channels_input(client: Client, message: Message):
        user_id = message.from_user.id
        text = (message.text or "").strip()
        if user_id not in admin_state:
            message.continue_propagation()
            return
        action = admin_state[user_id]["action"]
        if action not in {"setchannel", "set_channel_input"}:
            message.continue_propagation()
            return
        message._handled_by_admin = True
        try:
            if action == "set_channel_input":
                channel_key = admin_state[user_id].get("channel_key", "REQUIRED_CHANNEL")
                if channel_key not in CHANNEL_KEYS:
                    await _panel_reply(client, user_id, message, f"❌ Unknown channel: {channel_key}")
                    _admin_manager.clear_state(user_id)
                    return

                new_value, error = _validate_channel_input(text)
                if error:
                    await _panel_reply(client, user_id, message, f"❌ {error}")
                    return

                old_value = get_channel(channel_key)
                set_channel_override(channel_key, new_value)
                await _log_admin_action(
                    user_id, "set_channel_override",
                    message=f"{channel_key} -> {new_value} (was {old_value})",
                )

                # Invalidate cached memberships so the new required channel
                # applies immediately to every user.
                if channel_key == "REQUIRED_CHANNEL":
                    try:
                        from bot.middleware.channel_guard import invalidate_all_memberships
                        invalidate_all_memberships()
                    except Exception:
                        pass

                await _panel_reply(client, user_id, message,
                    f"✅ **Channel updated!**\n\n"
                    f"📌 Channel: `{channel_key}`\n"
                    f"{CHANNEL_LABELS.get(channel_key, '')}\n\n"
                    f"🔴 Old: `{old_value if old_value not in (None, '', 0) else 'Not set'}`\n"
                    f"🟢 New: `{new_value}`\n\n"
                    f"Applies immediately - no restart needed.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Channel Management", callback_data="menu_channels")],
                        [InlineKeyboardButton("Back to Messaging", callback_data="menu_messaging")],
                    ])
                )
                _admin_manager.clear_state(user_id)

            elif action == "setchannel":
                new_channel, error = _validate_channel_input(text)
                if error:
                    await _panel_reply(client, user_id, message, f"❌ {error}")
                    return
                old_channel = get_required_channel()
                set_channel_override("REQUIRED_CHANNEL", new_channel)
                total_users, _ = get_total_users()
                await _log_admin_action(
                    user_id, "setchannel", message=f"{old_channel} -> {new_channel}"
                )
                try:
                    from bot.middleware.channel_guard import invalidate_all_memberships
                    invalidate_all_memberships()
                except Exception:
                    pass
                await _panel_reply(client, user_id, message,
                    f"✅ Channel updated successfully!\n\n"
                    f"🔴 Old channel: {old_channel}\n"
                    f"🟢 New channel: {new_channel}\n\n"
                    f"👥 All {total_users} users must now join {new_channel} to use the bot."
                )

        except Exception as e:
            await _panel_reply(client, user_id, message, f"\u274c Error: {str(e)}")
            _admin_manager.clear_state(user_id)
            return
        _admin_manager.clear_state(user_id)
