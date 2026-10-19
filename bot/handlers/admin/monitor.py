# bot/handlers/admin/monitor.py - monitor controls & alert system

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
    toggle_two_factor,
    toggle_device_check,
    toggle_same_number,
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
from bot.utils.i18n import admin_str, admin_translate_markup
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
    get_api_management_buttons,
    get_settings_config_buttons,
    get_session_management_buttons,
    _create_db_snapshot,
)
from bot.handlers.admin.dashboard import _get_session_queue_stats

logger = logging.getLogger(__name__)

def register_monitor(app: Client):

    @app.on_callback_query(admin_filter & filters.regex(r"^menu_system$"))
    async def menu_system_callback(client: Client, query: CallbackQuery):
        await query.answer()
        from bot.handlers.admin.design import hd
        await _edit_callback(query,
            hd("⚙️ System Controls", "Main › System"),
            reply_markup=get_system_controls_buttons()
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^togglemonitor$"))
    async def toggle_monitor_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        is_enabled = toggle_monitor_status()
        status = "ON" if is_enabled else "OFF"
        print(f"Session Monitor toggled: {status}")
        await _log_admin_action(uid, "toggle_monitor", message=f"monitor={status}")
        await query.answer()
        # MEDIUM-26: a failed markup edit used to raise past the toggle - the
        # state changed but the admin saw an error (or Telegram error spam).
        try:
            await query.message.edit_reply_markup(reply_markup=admin_translate_markup(uid, get_system_controls_buttons()))
        except Exception as ui_err:
            print(f"⚠️ Toggle monitor UI refresh failed: {ui_err}")

    # ---------------- Toggle Spam Check Callback ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^togglespamcheck$"))
    async def toggle_spam_check_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        is_enabled = toggle_spam_check()
        print(f"Spam Check {'ENABLED' if is_enabled else 'DISABLED'} by admin")
        await _log_admin_action(uid, "toggle_spam_check", message=f"spam_check={'ON' if is_enabled else 'OFF'}")
        await query.answer()
        try:
            await query.message.edit_reply_markup(reply_markup=admin_translate_markup(uid, get_system_controls_buttons()))
        except Exception as ui_err:
            print(f"⚠️ Toggle spam check UI refresh failed: {ui_err}")

    # ---------------- Toggle Two Factor Callback ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^two_factor_toggle$"))
    async def two_factor_toggle_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        is_enabled = toggle_two_factor()
        print(f"Two Factor toggled: {'ON' if is_enabled else 'OFF'}")
        await _log_admin_action(uid, "toggle_two_factor", message=f"2fa={'ON' if is_enabled else 'OFF'}")
        await query.answer()
        try:
            await query.message.edit_reply_markup(reply_markup=admin_translate_markup(uid, get_system_controls_buttons()))
        except Exception as ui_err:
            print(f"⚠️ Toggle 2FA UI refresh failed: {ui_err}")

    # ---------------- Toggle Device Check Callback ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^device_check_toggle$"))
    async def device_check_toggle_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        is_enabled = toggle_device_check()
        print(f"Device Check toggled: {'ON' if is_enabled else 'OFF'}")
        await _log_admin_action(uid, "toggle_device_check", message=f"device_check={'ON' if is_enabled else 'OFF'}")
        await query.answer()
        try:
            await query.message.edit_reply_markup(reply_markup=admin_translate_markup(uid, get_system_controls_buttons()))
        except Exception as ui_err:
            print(f"⚠️ Toggle device check UI refresh failed: {ui_err}")

    # ---------------- Toggle Same Number Callback ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^same_number_toggle$"))
    async def same_number_toggle_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        is_enabled = toggle_same_number()
        print(f"Same Number toggled: {'ON' if is_enabled else 'OFF'}")
        await _log_admin_action(uid, "toggle_same_number", message=f"same_number={'ON' if is_enabled else 'OFF'}")
        await query.answer()
        try:
            await query.message.edit_reply_markup(reply_markup=admin_translate_markup(uid, get_system_controls_buttons()))
        except Exception as ui_err:
            print(f"⚠️ Toggle same-number UI refresh failed: {ui_err}")

    # ---------------- Quick Ban/Unban from Search User ----------------


def register_monitor_features(app: Client):

    @app.on_callback_query(admin_filter & filters.regex(r"^api_rotation_monitor$"))
    async def api_rotation_monitor_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "Loading API status..."))

        lines = ["=== API ROTATION MONITOR ===\n"]

        try:
            from bot.utils.api_rotation import get_api_manager
            mgr = get_api_manager()

            stats = mgr.get_stats()
            info = mgr.get_current_api_info()
            details = mgr.get_detailed_stats()

            current_label = "N/A"
            if info.get("current_api"):
                current_label = f"{info['current_api']}"
                if info.get("current_api_id"):
                    current_label += f" (ID {str(info['current_api_id'])[:6]}...)"

            lines.append(f"Rotation Mode: {stats.get('rotation_mode', 'N/A')}")
            lines.append(f"Total APIs: {stats.get('total_apis', 0)}")
            lines.append(f"Available Now: {stats.get('available_now', 0)}")
            lines.append(f"Current Index: {stats.get('current_index', 0)}")
            lines.append(f"Current API: {current_label}")
            lines.append(f"Total Rotations: {stats.get('total_rotations', 0)}")
            lines.append(f"Sessions Today: {stats.get('sessions_today', 0)}")
            sr = stats.get("overall_success_rate", 0)
            lines.append(f"Overall Success Rate: {sr:.1%}")
            rc = stats.get('remaining_capacity')
            if rc is None:
                lines.append(f"Remaining Capacity: Unlimited\n")
            else:
                lines.append(f"Remaining Capacity: {rc} sessions\n")

            lines.append("--- API DETAILS ---")
            for api in details:
                marker = "▶️" if api.get("is_current") else "  "
                status_icon = "ON" if api.get("is_available") else "OFF"
                cooldown = " | ⏳ COOLDOWN" if api.get("in_cooldown") else ""
                last_used = api.get('last_used') or 'Never'
                if last_used == 'Never':
                    last_used = admin_str(query.from_user.id, 'Never')
                lines.append(
                    f"{marker} {api.get('name', 'API')}: {status_icon}{cooldown}\n"
                    f"    Today: {api.get('sessions_today', 0)} | "
                    f"Hour: {api.get('sessions_this_hour', 0)} | "
                    f"SR: {api.get('success_rate', 'N/A')}\n"
                    f"    Floods: {api.get('flood_count', 0)} | "
                    f"Last Used: {last_used}"
                )

        except Exception as e:
            lines.append(f"Error loading API data: {str(e)}")

        from bot.config import API_ID, API_HASH
        lines.append(f"\nPrimary API: ID {str(API_ID)[:6]}...")
        # List configured .env EXTRA_API_* credentials (up to 200 slots).
        # Only configured ones are shown, with a summary if there are many.
        configured_extra = 0
        for i in range(1, 201):
            api_id_env = os.getenv(f"EXTRA_API_{i}_ID")
            if api_id_env:
                configured_extra += 1
                if configured_extra <= 5:
                    lines.append(f"Extra API {i}: ID {str(api_id_env)[:6]}... (configured)")
        if configured_extra > 5:
            lines.append(f"… and {configured_extra - 5} more EXTRA APIs configured")
        if configured_extra == 0:
            lines.append("No EXTRA APIs configured (.env)")

        lines.append(f"\nLast Check: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        await _edit_callback(query, 
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Refresh", callback_data="api_rotation_monitor")],
                [InlineKeyboardButton("Back to Monitor", callback_data="realtime_refresh")],
            ])
        )

    # ============================================================
    # API MANAGEMENT (Add / Remove / List Check)
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^api_mgmt_menu$"))
    async def api_mgmt_menu_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "Loading API management..."))
        from bot.handlers.admin.design import hd
        from bot.utils.api_rotation import get_api_manager
        try:
            mgr = get_api_manager()
            stats = mgr.get_stats()
            summary = (
                f"Total APIs: {stats.get('total_apis', 0)} | "
                f"Available: {stats.get('available_now', 0)}\n"
                f"Sessions Today: {stats.get('sessions_today', 0)} | "
                f"Success Rate: {stats.get('overall_success_rate', 0):.1%}"
            )
        except Exception as e:
            summary = f"(API manager unavailable: {e})"
        await _edit_callback(query,
            hd("🔌 API Management", "Main › Advanced › API") + "\n\n"
            + summary,
            reply_markup=get_api_management_buttons()
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^api_add$"))
    async def api_add_callback(client: Client, query: CallbackQuery):
        await query.answer()
        uid = query.from_user.id
        _set_admin_state(uid, query, "api_add_input")
        await _edit_callback(query,
            "➕ **ADD API**\n\n"
            "Send API **ID** and **HASH**:\n"
            "Format: `API_ID API_HASH`\n"
            "Example: `12345678 0123456789abcdef0123456789abcdef`\n\n"
            "Type /cancel to abort"
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^api_remove$"))
    async def api_remove_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "Loading API list..."))
        from bot.utils.api_rotation import get_api_manager
        mgr = get_api_manager()
        apis = mgr.apis
        if not apis:
            await _edit_callback(query,
                "➖ **REMOVE API**\n\n"
                "❌ No APIs in the rotation pool.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ Back to API Management", callback_data="api_mgmt_menu")]
                ])
            )
            return

        lines = [
            "➖ **REMOVE API**",
            "Select an API to remove:",
            "",
        ]
        buttons = []
        for i, api in enumerate(apis):
            lines.append(f"{i + 1}. {api.name} (ID {api.api_id})")
            buttons.append([
                InlineKeyboardButton(
                    f"❌ Remove {api.name} (ID {api.api_id})",
                    callback_data=f"api_remove_confirm_{api.api_id}"
                )
            ])
        lines.append("\n⚠️ APIs defined in `.env` return after a restart "
                     "unless also removed from `.env`.")
        buttons.append([InlineKeyboardButton("◀ Back to API Management", callback_data="api_mgmt_menu")])

        await _edit_callback(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))

    @app.on_callback_query(admin_filter & filters.regex(r"^api_remove_confirm_(\d+)$"))
    async def api_remove_confirm_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "Removing API..."))
        import re
        m = re.match(r"^api_remove_confirm_(\d+)$", query.data)
        if not m:
            return
        target_api_id = int(m.group(1))
        from bot.utils.api_rotation import get_api_manager, remove_api_runtime
        mgr = get_api_manager()
        # Match by api_id (not index) so a stale index after concurrent
        # list changes can never remove the WRONG API.
        if not any(int(a.api_id) == target_api_id for a in mgr.apis):
            await _edit_callback(query,
                "❌ API not found (it may have been removed already).",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔁 Refresh", callback_data="api_remove")],
                    [InlineKeyboardButton("◀ Back to API Management", callback_data="api_mgmt_menu")]
                ])
            )
            return
        ok, msg = remove_api_runtime(api_id=target_api_id)
        await _edit_callback(query,
            msg + "\n\n🔁 The rotation now uses the remaining APIs.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔁 Refresh List", callback_data="api_remove")],
                [InlineKeyboardButton("📋 API List & Check", callback_data="api_list")],
                [InlineKeyboardButton("◀ Back to API Management", callback_data="api_mgmt_menu")]
            ])
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^api_list$"))
    async def api_list_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "Loading API status..."))
        from bot.utils.api_rotation import get_api_manager
        try:
            mgr = get_api_manager()
            stats = mgr.get_stats()
            details = mgr.get_detailed_stats()

            lines = [
                "📋 **API LIST & CHECK**",
                f"Rotation Mode: {stats.get('rotation_mode', 'N/A')}",
                f"Total APIs: {stats.get('total_apis', 0)} | "
                f"Available Now: {stats.get('available_now', 0)}",
                f"Sessions Today: {stats.get('sessions_today', 0)} | "
                f"Success Rate: {stats.get('overall_success_rate', 0):.1%}",
                f"Remaining Capacity: {('Unlimited' if stats.get('remaining_capacity') is None else stats.get('remaining_capacity'))}",
                "",
                "--- API DETAILS ---",
            ]
            if not details:
                lines.append("  No APIs configured.")
            for api in details:
                marker = "▶️" if api.get("is_current") else "  "
                status_icon = "✅ ON" if api.get("is_available") else "❌ OFF"
                cooldown = " | ⏳ COOLDOWN" if api.get("in_cooldown") else ""
                last_used = api.get('last_used') or 'Never'
                lines.append(
                    f"{marker} {api.get('name', 'API')} (ID {api.get('api_id', '?')}): "
                    f"{status_icon}{cooldown}\n"
                    f"    Today: {api.get('sessions_today', 0)} | "
                    f"Hour: {api.get('sessions_this_hour', 0)} | "
                    f"SR: {api.get('success_rate', 'N/A')}\n"
                    f"    Floods: {api.get('flood_count', 0)} | "
                    f"Last Used: {last_used}"
                )
            lines.append(f"\nLast Check: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            lines = [f"❌ Error loading API data: {e}"]

        await _edit_callback(query,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="api_list")],
                [InlineKeyboardButton("◀ Back to API Management", callback_data="api_mgmt_menu")]
            ])
        )

    # ============================================================
    # 8. SESSION VERIFICATION QUEUE PRIORITY
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^queue_priority_view$"))
    async def queue_priority_view_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "Loading queue..."))

        qstats = _get_session_queue_stats()

        lines = [
            "=== SESSION VERIFICATION QUEUE ===\n",
            f"Queue Size: {qstats['queue_size']}",
            f"Processing: {qstats['processing']}",
            f"Completed: {qstats['completed']}",
            f"Failed: {qstats['failed']}\n",
        ]

        stats = qstats.get("stats", {})
        if stats:
            lines.append("--- PERFORMANCE ---")
            lines.append(f"  Total Processed: {stats.get('total_processed', 0)}")
            lines.append(f"  Total Success: {stats.get('total_success', 0)}")
            lines.append(f"  Total Failed: {stats.get('total_failed', 0)}")
            lines.append(f"  Retried: {stats.get('total_retried', 0)}")

        pending = _storage.get("pending_sessions", {})
        status_counts = {}
        for sid, data in pending.items():
            status = data.get("status", "pending")
            status_counts[status] = status_counts.get(status, 0) + 1

        if status_counts:
            lines.append(f"\n--- PENDING SESSION STATUS ---")
            for status, count in sorted(status_counts.items()):
                icon = {"pending": "Pending", "approved": "Approved", "rejected": "Rejected", "timeout": "Timeout"}.get(status, status.title())
                lines.append(f"  {icon}: {count}")

        lines.append(f"\nLast Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        await _edit_callback(query, 
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Refresh", callback_data="queue_priority_view")],
                [InlineKeyboardButton("Back to Monitor", callback_data="realtime_refresh")],
            ])
        )

    # ============================================================
    # 9. AUTO-MODERATION RULES
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^auto_moderation_rules$"))
    async def auto_moderation_rules_callback(client: Client, query: CallbackQuery):
        await query.answer()

        rules = _storage.get("auto_moderation_rules", {})
        if not rules:
            rules = {
                "max_sessions_per_user_per_day": 10,
                "auto_ban_on_spam_country": False,
                "spam_score_threshold": 0.7,
                "auto_reject_high_risk": False,
                "cooldown_after_fail": 60,
            }

        spam_enabled = _storage.get("spam_check_enabled", True)
        countries = _storage.get("countries", {})

        uid = query.from_user.id
        rules_text = "\n".join([
            f"  `{k}`: {admin_str(uid, 'True' if v is True else 'False') if isinstance(v, bool) else v}"
            for k, v in rules.items()
        ])

        lines = [
            "=== AUTO-MODERATION RULES ===\n",
            f"Spam Check: {'ENABLED' if spam_enabled else 'DISABLED'}",
            f"Configured Countries: {len(countries)}\n",
            "--- CURRENT RULES ---",
            rules_text,
            "\n--- COUNTRY RISK PROFILES ---",
        ]

        try:
            from bot.utils.anti_spam import COUNTRY_PROFILES
            t_risk = admin_str(uid, "Risk")
            t_freeze = admin_str(uid, "Freeze")
            t_delay = admin_str(uid, "Delay")
            for code, profile in sorted(COUNTRY_PROFILES.items()):
                name = profile.get("name", code)
                risk = profile.get("risk_level", 0)
                freeze = profile.get("freeze_risk", "unknown")
                delay = f"{profile.get('min_delay', 0)}-{profile.get('max_delay', 0)}s"

                risk_labels = {1: "Low", 2: "Medium", 3: "High", 4: "Critical"}
                risk_label = admin_str(uid, risk_labels.get(risk, str(risk)))
                freeze_label = admin_str(uid, freeze.title())

                is_open = countries.get(code, {}).get("open", False)
                status = admin_str(uid, "Open" if is_open else "Closed")

                lines.append(
                    f"  `{code}` {name}: {t_risk}={risk_label} | "
                    f"{t_freeze}={freeze_label} | {t_delay}={delay} | {status}"
                )
        except Exception:
            lines.append("  " + admin_str(uid, "(Could not load country risk profiles)"))

        buttons = [
            [InlineKeyboardButton("Edit Rules", callback_data="edit_moderation_rules"),
             InlineKeyboardButton("Toggle Spam Check", callback_data="togglespamcheck")],
            [InlineKeyboardButton("Back to Monitor", callback_data="realtime_refresh")],
        ]

        await _edit_callback(query, 
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons)
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^edit_moderation_rules$"))
    async def edit_moderation_rules_callback(client: Client, query: CallbackQuery):
        await query.answer()
        uid = query.from_user.id
        _set_admin_state(uid, query, "edit_mod_rules")

        current = _storage.get("auto_moderation_rules", {
            "max_sessions_per_user_per_day": 10,
            "auto_ban_on_spam_country": False,
            "spam_score_threshold": 0.7,
            "auto_reject_high_risk": False,
            "cooldown_after_fail": 60,
        })

        await _edit_callback(query, 
            "=== EDIT MODERATION RULES ===\n\n"
            "Enter new rules in the format below (JSON):\n\n"
            f"Current: `{json.dumps(current, indent=2)}`\n\n"
            "Example: `{\"max_sessions_per_user_per_day\": 5, \"auto_ban_on_spam_country\": true}`\n\n"
            "Type /admin to cancel"
        )

    # ============================================================
    # 10. BULK COUNTRY CONFIG (Import/Export)
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^alert_system_config$"))
    async def alert_system_config_callback(client: Client, query: CallbackQuery):
        await query.answer()

        alerts = _storage.get("alert_settings", {
            "low_balance_alert": True,
            "high_reject_rate_alert": True,
            "api_error_alert": True,
        })

        from bot.handlers.admin.design import hd, sw
        text = hd("🚨 Alert System", "Main › System › Alerts")

        buttons = []
        for alert_name, enabled in alerts.items():
            buttons.append([
                InlineKeyboardButton(
                    sw(alert_name.replace('_', ' ').title(), enabled),
                    callback_data=f"alert_toggle_{alert_name}"
                )
            ])
        buttons.append([InlineKeyboardButton("Set Alert Threshold", callback_data="alert_threshold_input")])
        buttons.append([InlineKeyboardButton("Back to System Controls", callback_data="menu_system")])

        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(buttons))


    @app.on_callback_query(admin_filter & filters.regex(r"^alert_toggle_(\w+)$"))
    async def alert_toggle_callback(client: Client, query: CallbackQuery):
        await query.answer()
        import re
        m = re.match(r"^alert_toggle_(\w+)$", query.data)
        alert_name = m.group(1) if m else None
        if not alert_name:
            return

        if "alert_settings" not in _storage:
            _storage["alert_settings"] = {
                "low_balance_alert": True,
                "high_reject_rate_alert": True,
                "api_error_alert": True,
            }
        if alert_name not in _storage["alert_settings"]:
            await _edit_callback(query, f"❌ Unknown alert type: `{alert_name}`")
            return

        _storage["alert_settings"][alert_name] = not _storage["alert_settings"][alert_name]
        _mark_settings_key_dirty("alert_settings")
        save_storage()

        from bot.handlers.admin.design import sw
        alerts = _storage["alert_settings"]
        text = hd("🚨 Alert System", "Main › System › Alerts")
        buttons = []
        for name, enabled in alerts.items():
            buttons.append([
                InlineKeyboardButton(
                    sw(name.replace('_', ' ').title(), enabled),
                    callback_data=f"alert_toggle_{name}"
                )
            ])
        buttons.append([InlineKeyboardButton("Set Alert Threshold", callback_data="alert_threshold_input")])
        buttons.append([InlineKeyboardButton("Back to System Controls", callback_data="menu_system")])
        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(buttons))


    @app.on_callback_query(admin_filter & filters.regex(r"^alert_threshold_input$"))
    async def alert_threshold_input_callback(client: Client, query: CallbackQuery):
        await query.answer()
        _set_admin_state(query.from_user.id, query, "alert_threshold")
        thresholds = _storage.get("alert_thresholds", {})
        text = "📊 **Set Alert Threshold**\n\n"
        text += "Send alert name and value:\n"
        text += "`low_balance 100` | `reject_rate 0.3`\n\n"
        if thresholds:
            text += "**Current thresholds:**\n"
            for name, val in thresholds.items():
                text += f"  `{name}`: `{val}`\n"
        text += "\n/cancel to abort"
        await _edit_callback(query, text)

    # ============================================================
    # FEATURE 10: Multi-Admin Management
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^auto_approve_settings$"))
    async def auto_approve_settings_callback(client: Client, query: CallbackQuery):
        await query.answer()
        enabled = get_auto_approve_enabled()

        from bot.handlers.admin.design import hd, sw
        text = hd("🤖 Auto-Approve", "Main › Countries › Auto-Approve", sw("Auto-Approve", enabled))

        buttons = [
            [InlineKeyboardButton(sw("Auto-Approve", enabled),
                                  callback_data="auto_approve_toggle")],
            [InlineKeyboardButton("Back to Session Management", callback_data="menu_session")]
        ]

        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(buttons))


    @app.on_callback_query(admin_filter & filters.regex(r"^auto_approve_toggle$"))
    async def auto_approve_toggle_callback(client: Client, query: CallbackQuery):
        await query.answer()
        new_state = not get_auto_approve_enabled()
        set_auto_approve_enabled(new_state)

        from bot.handlers.admin.design import hd, sw
        text = hd("🤖 Auto-Approve", "Main › Countries › Auto-Approve", sw("Auto-Approve", new_state))

        buttons = [
            [InlineKeyboardButton(sw("Auto-Approve", new_state),
                                  callback_data="auto_approve_toggle")],
            [InlineKeyboardButton("Back to Session Management", callback_data="menu_session")]
        ]

        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(buttons))

    # ============================================================
    # FEATURE 7: Session Expiry Settings
    # ============================================================


    @app.on_message(ADMIN_TEXT_FILTER)
    async def handle_features_monitor_input(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id not in admin_state:
            message.continue_propagation()
            return
        action = admin_state[user_id].get("action", "")
        if action not in {"edit_mod_rules", "alert_threshold", "api_add_input"}:
            message.continue_propagation()
            return
        message._handled_by_admin = True
        text = message.text.strip()
        try:
            if action == "edit_mod_rules":
                # MEDIUM-46: validate the JSON SHAPE before persisting - the
                # old code stored any dict (wrong types later broke the
                # moderation engine) and cleared the admin state on a JSON
                # error, losing the retry step.
                _ALLOWED_RULE_KEYS = {
                    "max_sessions_per_user_per_day": int,
                    "auto_ban_on_spam_country": bool,
                    "spam_score_threshold": (int, float),
                    "auto_reject_high_risk": bool,
                    "cooldown_after_fail": int,
                }
                try:
                    new_rules = json.loads(text)
                    if not isinstance(new_rules, dict) or not new_rules:
                        await _panel_reply(client, user_id, message,
                            "❌ Body must be a non-empty JSON object.\n"
                            'Example: {"cooldown_after_fail": 120}'
                        )
                        return
                    bad_keys = [k for k in new_rules if k not in _ALLOWED_RULE_KEYS]
                    if bad_keys:
                        await _panel_reply(client, user_id, message,
                            "❌ Unknown rule key(s): " + ", ".join(f"`{k}`" for k in bad_keys)
                            + "\nAllowed: " + ", ".join(f"`{k}`" for k in _ALLOWED_RULE_KEYS)
                        )
                        return
                    for key, value in new_rules.items():
                        expected = _ALLOWED_RULE_KEYS[key]
                        if expected is bool:
                            if not isinstance(value, bool):
                                await _panel_reply(client, user_id, message, f"❌ `{key}` must be true or false")
                                return
                        elif isinstance(expected, tuple):
                            if isinstance(value, bool) or not isinstance(value, expected):
                                await _panel_reply(client, user_id, message, f"❌ `{key}` must be a number")
                                return
                        elif not isinstance(value, expected) or isinstance(value, bool):
                            await _panel_reply(client, user_id, message, f"❌ `{key}` must be a whole number")
                            return
                    current = _storage.get("auto_moderation_rules", {})
                    current.update(new_rules)
                    _storage["auto_moderation_rules"] = current
                    _mark_settings_key_dirty("auto_moderation_rules")
                    save_storage()
                    await _panel_reply(client, user_id, message, 
                        "Moderation rules updated!\n\n"
                        f"```json\n{json.dumps(current, indent=2)}\n```",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("Back to Rules", callback_data="auto_moderation_rules")],
                        ])
                    )
                    _admin_manager.clear_state(user_id)
                except json.JSONDecodeError:
                    # MEDIUM-46: KEEP the state so the admin can retry the
                    # same edit without re-navigating the menu.
                    await _panel_reply(client, user_id, message, 
                        "Invalid JSON format! Please try again with valid JSON."
                    )
                    return

            elif action == "alert_threshold":
                parts = text.strip().split()
                if len(parts) != 2:
                    thresholds = _storage.get("alert_thresholds", {})
                    current = "\n".join(
                        f"  `{name}`: `{val}`" for name, val in thresholds.items()
                    ) if thresholds else "  None set"
                    await _panel_reply(client, user_id, message, 
                        "❌ Format: `alert_name value`\n\n"
                        "Examples:\n"
                        "  `low_balance 100` - alert when balance < $100\n"
                        "  `reject_rate 0.3` - alert when reject rate > 30%\n\n"
                        f"**Current thresholds:**\n{current}"
                    )
                    _admin_manager.clear_state(user_id)
                    return

                alert_type = parts[0].strip().lower()
                # MEDIUM-47: whitelist - alert thresholds only apply to known
                # alert names (the alert_settings toggles + the documented
                # examples). Arbitrary names used to be stored silently.
                _KNOWN_ALERT_NAMES = {"low_balance", "reject_rate", "api_error"}
                if alert_type not in _KNOWN_ALERT_NAMES:
                    await _panel_reply(client, user_id, message,
                        "❌ Unknown alert name!\n\n"
                        "Valid names: " + ", ".join(f"`{n}`" for n in sorted(_KNOWN_ALERT_NAMES))
                    )
                    _admin_manager.clear_state(user_id)
                    return
                try:
                    threshold = float(parts[1])
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ Threshold must be a number!")
                    _admin_manager.clear_state(user_id)
                    return

                if "alert_thresholds" not in _storage:
                    _storage["alert_thresholds"] = {}
                _storage["alert_thresholds"][alert_type] = threshold
                _mark_settings_key_dirty("alert_thresholds")
                save_storage()

                await _panel_reply(client, user_id, message, 
                    f"✅ `{alert_type.replace('_', ' ').title()}` threshold set to `{threshold}`",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Back to Alert Settings", callback_data="alert_system_config")]
                    ])
                )
                _admin_manager.clear_state(user_id)

            elif action == "api_add_input":
                from bot.utils.api_rotation import add_api_runtime
                parts = text.split()
                if len(parts) >= 2:
                    api_id_str, api_hash_str = parts[0], parts[1]
                else:
                    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                    if len(lines) >= 2:
                        api_id_str, api_hash_str = lines[0], lines[1]
                    else:
                        api_id_str, api_hash_str = None, None

                if not api_id_str or not api_hash_str:
                    await _panel_reply(client, user_id, message,
                        "❌ Invalid format!\n\n"
                        "Send API ID and API Hash:\n"
                        "`12345678 0123456789abcdef0123456789abcdef`"
                    )
                    _admin_manager.clear_state(user_id)
                    return

                ok, msg = add_api_runtime(api_id_str, api_hash_str)
                if ok:
                    await _panel_reply(client, user_id, message, msg,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("➕ Add Another API", callback_data="api_add")],
                            [InlineKeyboardButton("📋 API List & Check", callback_data="api_list")],
                            [InlineKeyboardButton("◀ Back to API Management", callback_data="api_mgmt_menu")],
                        ])
                    )
                else:
                    await _panel_reply(client, user_id, message, msg,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("◀ Back to API Management", callback_data="api_mgmt_menu")],
                        ])
                    )
                _admin_manager.clear_state(user_id)

        except Exception as e:
            await _panel_reply(client, user_id, message, f"Error: {str(e)}")
            _admin_manager.clear_state(user_id)
