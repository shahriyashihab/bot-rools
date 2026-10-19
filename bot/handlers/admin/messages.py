# bot/handlers/admin/messages.py - message template editor

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
from bot.data.languages import is_supported_language, build_language_label, DEFAULT_LANGUAGE
from bot.utils.i18n import admin_str
from bot.utils.text import truncate
from bot.utils.country_data import get_flag
from bot.config import ADMIN_ID, SESSION_2FA_PASSWORD, get_2fa_password, CHANNEL_WITHDRAW_REPORT, EXTRA_APIS, API_ID
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
from bot.utils.bg_tasks import spawn_background

logger = logging.getLogger(__name__)

# Template names for display
TEMPLATE_DISPLAY_NAMES = {
    "account_received_message": "📲 Account Received Message",
    "success_message": "✅ Success Message",
    "multi_device_message": "⚠️ Multi Device Message",
    "support_message": "🆘 Support Message",
    "account_info_message": "🎰 Account Info Message",
    "verification_failed_message": "🚫 Verification Failed Message",
    "welcome_message": "🎉 Welcome Message",
    "invalid_otp_message": "❌ Invalid OTP Message",
    "processing_message": "🔄 Processing Message",
    "banned_user_message": "🚫 Banned User Message",
    "withdraw_success_message": "✅ Withdraw Success Message",
    "no_balance_message": "❌ No Balance Message",
    "twofa_already_active_message": "❌ 2FA Already Active Message",
    
    "enter_phone_message": "📱 Enter Phone Message",
    "enter_otp_message": "🔢 Enter OTP Message",
    "otp_sent_message": "📤 OTP Sent Message",
    "waiting_verification_message": "⏳ Waiting Verification Message",
    "twofa_setup_message": "🔐 2FA Setup Message",
    "spam_check_message": "🔍 Spam Check Message",
    "withdraw_disabled_message": "❌ Withdraw Disabled Message",
    "withdraw_method_message": "💳 Withdraw Method Message",
    "enter_wallet_message": "📬 Enter Wallet Message",
    "invalid_wallet_message": "❌ Invalid Wallet Message",
    "session_timeout_message": "⏰ Session Timeout Message",
    "country_not_available_message": "🌍 Country Not Available Message",
    "join_channel_message": "📢 Join Channel Message",
    "min_withdraw_message": "⚠️ Min Withdraw Message",
    "enter_2fa_password_message": "🔑 Enter 2FA Password Message"
}

# Placeholder info for each template
TEMPLATE_PLACEHOLDERS = {
    "account_received_message": "{phone}, {flag}, {timeout}, {received_time}, {eta_min_sec}",
    "success_message": "{phone}, {price}, {status}",
    "multi_device_message": "{phone}",
    "support_message": "No placeholders",
    "account_info_message": "{user_id}, {balance}, {sessions}, {joined}, {time}",
    "verification_failed_message": "{phone}, {country_code}, {flag}",
    "welcome_message": "No placeholders",
    "invalid_otp_message": "No placeholders",
    "processing_message": "No placeholders",
    "banned_user_message": "No placeholders",
    "withdraw_success_message": "{amount}, {method}",
    "no_balance_message": "No placeholders",
    "twofa_already_active_message": "No placeholders",
    
    "enter_phone_message": "No placeholders",
    "enter_otp_message": "{timeout}",
    "otp_sent_message": "{phone}, {country_code}, {flag}, {timeout}",
    "waiting_verification_message": "{phone}, {country_code}, {flag}, {timeout}",
    "twofa_setup_message": "{phone}",
    "spam_check_message": "{phone}",
    "withdraw_disabled_message": "No placeholders",
    "withdraw_method_message": "{balance}",
    "enter_wallet_message": "{method}",
    "invalid_wallet_message": "{error}, {method}",
    "session_timeout_message": "{phone}, {country_code}, {flag}, {timeout}",
    "country_not_available_message": "{country_code}, {flag}",
    "join_channel_message": "{channel}",
    "min_withdraw_message": "{balance}, {min_amount}, {needed}",
    "enter_2fa_password_message": "{phone}"
}

def register_messages(app: Client):

    # Per-admin template editing language (default: admin panel language).
    _template_edit_lang = {}

    def _get_tpl_edit_lang(uid: int) -> str:
        lang = _template_edit_lang.get(uid, "")
        if not is_supported_language(lang):
            try:
                from bot.utils.i18n import get_admin_lang
                lang = get_admin_lang(uid)
            except Exception:
                lang = "en"
        return lang if is_supported_language(lang) else "en"

    @app.on_callback_query(admin_filter & filters.regex(r"^tpl_lang_set_(.+)$"))
    async def set_tpl_lang_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data_str = str(query.data) if query.data else ""
        rest = data_str[len("tpl_lang_set_"):]
        from bot.data.languages import LANGUAGE_CODES
        code = None
        key_len = len(rest)
        for candidate in LANGUAGE_CODES:
            if rest.endswith(f"_{candidate}") and len(candidate) > len(code or ""):
                code = candidate
                key_len = len(rest) - len(candidate) - 1
        if code is None:
            sep = rest.rfind("_")
            template_key = rest[:sep]
            code = rest[sep + 1:]
        else:
            template_key = rest[:key_len]
        if not is_supported_language(code):
            code = "en"
        _template_edit_lang[uid] = code
        await query.answer()
        await _edit_callback(query,
            f"🌐 Language set: {build_language_label(code)}\n\n"
            f"Now editing *{admin_str(uid, TEMPLATE_DISPLAY_NAMES.get(template_key, template_key))}* in this language.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Edit This Message", callback_data=f"start_edit_{template_key}"),
                 InlineKeyboardButton("🔄 Reset to Default", callback_data=f"reset_tpl_{template_key}")],
                [InlineKeyboardButton("🌐 Change Language", callback_data=f"tpl_pick_{template_key}"),
                 InlineKeyboardButton("🔙 Back to Messages", callback_data="messagesettings")],
            ])
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^tpl_pick_(.+)$"))
    async def pick_tpl_lang_callback(client: Client, query: CallbackQuery):
        data_str = str(query.data) if query.data else ""
        template_key = data_str[len("tpl_pick_"):]
        await query.answer()
        from bot.data.languages import LANGUAGES, build_language_label, build_language_grid_rows
        rows = []
        for row in build_language_grid_rows():
            rows.append([
                InlineKeyboardButton(build_language_label(code), callback_data=f"tpl_lang_set_{template_key}_{code}")
                for code in row
            ])
        rows.append([InlineKeyboardButton("🔙 Back to Message", callback_data=f"edit_tpl_{template_key}")])
        await _edit_callback(query,
            "🌐 **Select Template Language**\n\n"
            "Choose which language version of this message to edit:",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^menu_messaging$"))
    async def menu_messaging_callback(client: Client, query: CallbackQuery):
        await query.answer()
        from bot.handlers.admin.design import hd
        await _edit_callback(query,
            hd("📣 Messaging & Channel", "Main › Messaging"),
            reply_markup=get_messaging_buttons()
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^messagesettings$"))
    async def message_settings_callback(client: Client, query: CallbackQuery):
        await query.answer()
        templates = get_all_message_templates()
        
        buttons = []
        for key, data in templates.items():
            display_name = admin_str(query.from_user.id, TEMPLATE_DISPLAY_NAMES.get(key, key))
            status = "✏️" if data["is_custom"] else "📋"
            buttons.append([InlineKeyboardButton(f"{status} {display_name}", callback_data=f"edit_tpl_{key}")])
        
        buttons.append([InlineKeyboardButton("🔙 Back to Messaging", callback_data="menu_messaging")])
        
        await _edit_callback(query,
            "📝 Message Settings\n\n"
            "📋 = Default | ✏️ = Custom",
            reply_markup=InlineKeyboardMarkup(buttons)
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^edit_tpl_(.+)$"))
    async def edit_template_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data_str = str(query.data) if query.data else ""
        template_key = data_str.replace("edit_tpl_", "")
        
        await query.answer()
        
        tpl_lang = _get_tpl_edit_lang(uid)
        from bot.utils.storage import get_message_template
        from bot.data.templates import _custom_entry
        current = get_message_template(template_key, tpl_lang)
        custom = _custom_entry(template_key)
        is_custom = tpl_lang in custom
        
        display_name = admin_str(uid, TEMPLATE_DISPLAY_NAMES.get(template_key, template_key))
        placeholders = TEMPLATE_PLACEHOLDERS.get(template_key, "None")
        status_text = admin_str(uid, "✏️ Custom") if is_custom else admin_str(uid, "📋 Default")
        
        buttons = [
            [InlineKeyboardButton("✏️ Edit This Message", callback_data=f"start_edit_{template_key}")],
            [InlineKeyboardButton("🔄 Reset to Default", callback_data=f"reset_tpl_{template_key}")],
            [InlineKeyboardButton("🌐 Change Language", callback_data=f"tpl_pick_{template_key}")],
            [InlineKeyboardButton("🔙 Back to Messages", callback_data="messagesettings")]
        ]
        
        await _edit_callback(query,
            f"📝 **{display_name}**\n\n"
            f"{status_text} | {build_language_label(tpl_lang)}\n"
            f"Placeholders: `{placeholders}`\n\n"
            f"**Current Message:**\n"
            f"```\n{current[:500]}{'...' if len(current) > 500 else ''}\n```",
            reply_markup=InlineKeyboardMarkup(buttons)
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^start_edit_(.+)$"))
    async def start_edit_template_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data_str = str(query.data) if query.data else ""
        template_key = data_str.replace("start_edit_", "")
        tpl_lang = _get_tpl_edit_lang(uid)
        
        await query.answer()
        
        display_name = admin_str(uid, TEMPLATE_DISPLAY_NAMES.get(template_key, template_key))
        placeholders = TEMPLATE_PLACEHOLDERS.get(template_key, "None")
        
        await _edit_callback(query,
            f"✏️ **Edit: {display_name}**\n\n"
            f"Language: {build_language_label(tpl_lang)}\n\n"
            f"Enter new message:\n"
            f"Placeholders: `{placeholders}`\n\n"
            f"Type /admin to cancel"
        )
        _set_admin_state(uid, query, "edit_template", template_key=template_key, template_lang=tpl_lang)


    @app.on_callback_query(admin_filter & filters.regex(r"^reset_tpl_(.+)$"))
    async def reset_template_callback(client: Client, query: CallbackQuery):
        data_str = str(query.data) if query.data else ""
        template_key = data_str.replace("reset_tpl_", "")
        
        await query.answer()
        
        uid = query.from_user.id
        tpl_lang = _get_tpl_edit_lang(uid)
        display_name = admin_str(uid, TEMPLATE_DISPLAY_NAMES.get(template_key, template_key))
        
        buttons = [
            [InlineKeyboardButton("✅ Yes, Reset", callback_data=f"confirm_reset_{template_key}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"edit_tpl_{template_key}")]
        ]
        
        await _edit_callback(query, 
            f"⚠️ **Reset Confirmation**\n\n"
            f"Are you sure you want to reset *{display_name}* "
            f"({build_language_label(tpl_lang)}) to default?",
            reply_markup=InlineKeyboardMarkup(buttons)
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^confirm_reset_(.+)$"))
    async def confirm_reset_template_callback(client: Client, query: CallbackQuery):
        data_str = str(query.data) if query.data else ""
        template_key = data_str.replace("confirm_reset_", "")
        
        if template_key.startswith("tracker"):
            return
        
        await query.answer()
        
        uid = query.from_user.id
        tpl_lang = _get_tpl_edit_lang(uid)
        reset_message_template(template_key, lang=tpl_lang)
        try:
            from bot.services.translator import mark_template_lang_manual
            mark_template_lang_manual(template_key, tpl_lang, manual=False)
        except Exception as meta_err:
            print(f"⚠️ Template meta update error: {meta_err}")
        display_name = TEMPLATE_DISPLAY_NAMES.get(template_key, template_key)
        
        try:
            from bot.utils.fast_cache import refresh_all_templates
            refresh_all_templates()
            print(f"✅ Template '{template_key}' ({tpl_lang}) reset and cache refreshed")
        except Exception as cache_error:
            print(f"⚠️ Cache refresh error: {cache_error}")
        
        await _edit_callback(query, 
            f"✅ **{display_name}** ({build_language_label(tpl_lang)}) successfully reset to default!"
        )

    # ---------------- Callback Queries ----------------

    @app.on_message(ADMIN_TEXT_FILTER)
    async def handle_messages_input(client: Client, message: Message):
        user_id = message.from_user.id
        text = (message.text or "").strip()
        if user_id not in admin_state:
            message.continue_propagation()
            return
        action = admin_state[user_id]["action"]
        if action not in {"edit_template"}:
            message.continue_propagation()
            return
        message._handled_by_admin = True
        try:
            if action == "edit_template":
                template_key = admin_state[user_id].get("template_key", "")
                template_lang = admin_state[user_id].get("template_lang", DEFAULT_LANGUAGE)
                if not is_supported_language(template_lang):
                    template_lang = DEFAULT_LANGUAGE
                display_name = TEMPLATE_DISPLAY_NAMES.get(template_key, template_key)
                
                set_message_template(template_key, text, lang=template_lang)
                
                try:
                    from bot.services.translator import mark_template_lang_manual, auto_translate_template_async
                    # The uploaded template is the source of truth. The language
                    # used to upload it is the source language for translation:
                    # store it verbatim (done above), protect it from being
                    # overwritten by auto-translation, and translate it into
                    # every OTHER supported language in the background.
                    mark_template_lang_manual(template_key, template_lang)
                    spawn_background(
                        auto_translate_template_async(template_key, text, source_lang=template_lang),
                        "admin-template-autotranslate",
                    )
                except Exception as translate_err:
                    print(f"⚠️ Auto-translate setup error: {translate_err}")
                
                try:
                    from bot.utils.fast_cache import preload_templates
                    preload_templates()
                    print(f"✅ Template '{template_key}' ({template_lang}) saved and cache refreshed")
                except Exception as cache_error:
                    print(f"⚠️ Cache refresh error: {cache_error}")
                
                await _panel_reply(client, user_id, message, 
                    f"✅ **{display_name}** ({build_language_label(template_lang)}) updated successfully!\n\n"
                    f"**New Message:**\n"
                    f"```\n{truncate(text, 500)}\n```"
                )

        except Exception as e:
            await _panel_reply(client, user_id, message, f"\u274c Error: {str(e)}")
            _admin_manager.clear_state(user_id)
            return
        _admin_manager.clear_state(user_id)

