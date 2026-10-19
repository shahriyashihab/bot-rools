# bot/handlers/admin/users.py - user management (ban/unban/balance/search)

import os
import json
import re
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
from bot.handlers.admin.dashboard import _db_available

logger = logging.getLogger(__name__)

def register_users(app: Client):

    @app.on_callback_query(admin_filter & filters.regex(r"^menu_user$"))
    async def menu_user_callback(client: Client, query: CallbackQuery):
        await query.answer()
        from bot.handlers.admin.design import hd
        await _edit_callback(query,
            hd("👥 User Management", "Main › Users"),
            reply_markup=get_user_management_buttons()
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^search_user$"))
    async def search_user_callback(client: Client, query: CallbackQuery):
        """🔍 Search User"""
        await query.answer()
        uid = query.from_user.id
        _set_admin_state(uid, query, "search_user")
        await _edit_callback(query,
            "🔍 Search User\n\n"
            "Enter User ID:\n"
            "Example: `123456789`\n\n"
            "❌ Type /admin to cancel",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Advanced Tools", callback_data="menu_advanced_tools")]
            ])
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^quick_ban_(\d+)$"))
    async def quick_ban_callback(client: Client, query: CallbackQuery):
        await query.answer()
        data_str = str(query.data) if query.data else ""
        try:
            target_uid = int(data_str.replace("quick_ban_", ""))
            ban_user(target_uid)
            from bot.utils.fast_cache import mark_user_banned
            mark_user_banned(target_uid)
            print(f"User {target_uid} banned via quick action")
            
            await _edit_callback(query, 
                "--- USER BANNED ---\n\n"
                f"User: `{target_uid}`\n"
                f"Status: BANNED\n\n"
                "This user can no longer use the bot.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Unban This User", callback_data=f"quick_unban_{target_uid}")],
                    [InlineKeyboardButton("Back to Advanced Tools", callback_data="menu_advanced_tools")]
                ])
            )
        except Exception as e:
            await _edit_callback(query, f"Ban Error: {str(e)}")


    @app.on_callback_query(admin_filter & filters.regex(r"^quick_unban_(\d+)$"))
    async def quick_unban_callback(client: Client, query: CallbackQuery):
        """Quick unban user from search results"""
        await query.answer()
        data_str = str(query.data) if query.data else ""
        try:
            target_uid = int(data_str.replace("quick_unban_", ""))
            unban_user(target_uid)
            from bot.utils.fast_cache import mark_user_unbanned
            mark_user_unbanned(target_uid)
            print(f"✅ User {target_uid} unbanned via quick action")
            
            await _edit_callback(query, 
                "--- USER UNBANNED ---\n\n"
                f"User: `{target_uid}`\n"
                f"Status: ACTIVE\n\n"
                "This user can now use the bot again.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Ban This User", callback_data=f"quick_ban_{target_uid}")],
                    [InlineKeyboardButton("Back to Advanced Tools", callback_data="menu_advanced_tools")]
                ])
            )
        except Exception as e:
            await _edit_callback(query, f"Unban Error: {str(e)}")

    # ---------------- Min Withdraw Settings Callbacks ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^adjustbalance$"))
    async def admin_action_adjustbalance(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        await _edit_callback(query,
            "--- ADJUST BALANCE ---\n\n"
            "Enter User ID and Amount:\n"
            "Format: `123456 +10` or `123456 -5`"
        )
        _set_admin_state(uid, query, "adjustbalance")

    @app.on_callback_query(admin_filter & filters.regex(r"^userstats$"))
    async def admin_action_userstats(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        await _edit_callback(query,
            "--- USER STATS ---\n\n"
            "Enter the User ID:"
        )
        _set_admin_state(uid, query, "userstats")

    @app.on_callback_query(admin_filter & filters.regex(r"^totalusers$|^tu_page_\d+$"))
    async def admin_action_totalusers(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        total, active = get_total_users()
        users_list = get_all_users_list()

        header = (
            "--- TOTAL USERS REPORT ---\n"
            f"Total Users: {total}\n"
            f"Active (24h): {active}\n"
            "--------------------------\n"
        )

        if users_list:
            # MEDIUM-45: paginate (50/page) instead of head-truncating at 50.
            per_page = 50
            total_pages = max(1, (len(users_list) + per_page - 1) // per_page)
            m_tu = re.match(r"^tu_page_(\d+)$", data)
            page = int(m_tu.group(1)) if m_tu else 0
            page = max(0, min(page, total_pages - 1))
            user_details = f"USER LIST (page {page + 1}/{total_pages}):\n"
            for idx, user in enumerate(users_list[page * per_page:(page + 1) * per_page], page * per_page + 1):
                uid_val = user.get('user_id', 'N/A')
                balance = float(user.get('balance') or 0)
                sessions = user.get('sessions', 0)
                banned = "BANNED" if user.get('banned') else "ACTIVE"
                user_details += (
                    f"{idx}. ID:`{uid_val}` | ${balance:.2f} | {sessions} sess | {banned}\n"
                )
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("◀ Back", callback_data=f"tu_page_{page - 1}"))
            else:
                nav.append(InlineKeyboardButton("ㅤ", callback_data="tu_noop"))
            nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="tu_noop"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("Next ▶", callback_data=f"tu_page_{page + 1}"))
            else:
                nav.append(InlineKeyboardButton("ㅤ", callback_data="tu_noop"))
            buttons = [nav] if total_pages > 1 else []
        else:
            user_details = "No users found."
            buttons = []

        response = header + user_details

        buttons.append([InlineKeyboardButton("Back to User Management", callback_data="menu_user")])
        if len(response) > 4000:
            await _edit_callback(query, response[:4000])
            for i in range(4000, len(response), 4000):
                await query.message.reply(response[i:i+4000])
            await query.message.reply("👆 continued")
        else:
            await _edit_callback(query, response, reply_markup=InlineKeyboardMarkup(buttons))

    @app.on_callback_query(admin_filter & filters.regex(r"^tu_noop$"))
    async def totalusers_noop_callback(client: Client, query: CallbackQuery):
        await query.answer()

    @app.on_callback_query(admin_filter & filters.regex(r"^zerobalance$"))
    async def admin_action_zerobalance(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        # HIGH-10 FIX: this is the most destructive unguarded button in the
        # panel (one tap wiped every balance + the session tracker, no
        # confirmation, no audit log). A mis-tap now lands on a confirm step;
        # the executor logs the action.
        # Impact preview from live storage (before anything is wiped).
        _, total_users, _users_with_balance, total_balance = get_all_balances_report()
        await _edit_callback(query,
            "⚠️ **ZERO ALL BALANCES?**\n\n"
            f"Users affected: **{total_users}**\n"
            f"Total balance wiped: **${total_balance:,.2f}**\n"
            f"Session tracker: **CLEARED**\n\n"
            "This action CANNOT be undone.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Zero Everything", callback_data="zerobalance_yes")],
                [InlineKeyboardButton("❌ Cancel", callback_data="menu_user")],
            ])
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^zerobalance_yes$"))
    async def admin_action_zerobalance_yes(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        await query.answer()

        count = zero_all_balances()
        await _log_admin_action(
            uid, "zerobalance",
            message=f"zeroed {count} user balance(s) + session tracker",
        )
        await _edit_callback(query,
            "--- ZERO ALL BALANCES ---\nAll user balances reset to $0.00",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to User Management", callback_data="menu_user")],
            ])
        )

    @app.on_message(ADMIN_TEXT_FILTER)
    async def handle_users_input(client: Client, message: Message):
        user_id = message.from_user.id
        text = (message.text or "").strip()
        if user_id not in admin_state:
            message.continue_propagation()
            return
        action = admin_state[user_id]["action"]
        if action not in {"ban", "unban", "adjustbalance", "userstats", "activity_logs_user", "search_user", "remove_balance"}:
            message.continue_propagation()
            return
        message._handled_by_admin = True
        try:
            if action == "ban":
                print(f"🔍 Attempting to ban user: {text}")
                try:
                    target_uid = int(text)
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ User ID must be a number!")
                    return
                ban_user(target_uid)
                from bot.utils.fast_cache import mark_user_banned
                mark_user_banned(target_uid)
                print(f"✅ User {text} banned successfully (storage + cache)")
                await _log_admin_action(user_id, "ban", target_user_id=target_uid)
                await _panel_reply(client, user_id, message, f"🚫 User {text} banned")

            elif action == "unban":
                print(f"🔍 Attempting to unban user: {text}")
                try:
                    target_uid = int(text)
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ User ID must be a number!")
                    return
                unban_user(target_uid)
                from bot.utils.fast_cache import mark_user_unbanned
                mark_user_unbanned(target_uid)
                print(f"✅ User {text} unbanned successfully (storage + cache)")
                await _log_admin_action(user_id, "unban", target_user_id=target_uid)
                await _panel_reply(client, user_id, message, f"✔️ User {text} unbanned")

            elif action == "adjustbalance":
                parts = text.split()
                if len(parts) < 2:
                    await _panel_reply(client, user_id, message, 
                        "❌ **Invalid Format!**\n\n"
                        "Correct Format: `UserID Amount`\n\n"
                        "Examples:\n"
                        "• `123456789 +10` (add $10)\n"
                        "• `123456789 -5` (subtract $5)\n"
                        "• `123456789 25` (add $25)"
                    )
                    return
                try:
                    uid = int(parts[0])
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ User ID must be a number!")
                    return
                try:
                    amount_str = parts[1].replace("+", "").strip()
                    amount = float(amount_str)
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ Amount must be a valid number (e.g.: 10, -5, +25)")
                    return

                # MEDIUM-21: get_user_info() used to CREATE a ghost user for a
                # typo'd ID and silently fund it. Refuse unknown users unless
                # the operation is a credit (add_balance_for_user already
                # creates legitimately via other flows).
                old_info = get_user_info(uid, create_if_missing=False)
                if not old_info:
                    await _panel_reply(client, user_id, message,
                        f"❌ User `{uid}` not found!\n"
                        "Balance can only be adjusted for users who already exist\n"
                        "(the user must have interacted with the bot at least once)."
                    )
                    _admin_manager.clear_state(user_id)
                    return
                old_balance = float(old_info.get('balance') or 0)
                add_balance_for_user(uid, amount)
                new_info = get_user_info(uid, create_if_missing=False) or old_info
                new_balance = float(new_info.get('balance') or 0)
                
                action_text = "added" if amount >= 0 else "subtracted"
                await _log_admin_action(
                    user_id,
                    "adjustbalance",
                    target_user_id=uid,
                    message=f"{'+' if amount >= 0 else ''}{amount:+.2f} ({action_text})",
                    details={"old_balance": old_balance, "new_balance": new_balance},
                )
                await _panel_reply(client, user_id, message, 
                    f"✅ **Balance Updated Successfully!**\n\n"
                    f"👤 User ID: `{uid}`\n"
                    f"💵 Old Balance: ${old_balance:.2f}\n"
                    f"{'➕' if amount >= 0 else '➖'} Amount: ${abs(amount):.2f} {action_text}\n"
                    f"💰 New Balance: **${new_balance:.2f}**"
                )

            elif action == "userstats":
                try:
                    uid = int(text)
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ User ID must be a number!")
                    return
                stats = get_user_stats(uid)
                await _panel_reply(client, user_id, message, f"📊 User Stats:\n{stats}")

            elif action == "activity_logs_user":
                try:
                    uid = int(text)
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ User ID must be a number!")
                    _admin_manager.clear_state(user_id)
                    return

                user_info = get_user_info(uid, create_if_missing=False)
                if not user_info:
                    await _panel_reply(client, user_id, message, f"❌ User `{uid}` not found!")
                    _admin_manager.clear_state(user_id)
                    return

                lines = [f"📋 **Activity Logs for User `{uid}`**\n", "━━━━━━━━━━━━━━━━━━━━━\n"]

                txn_logs = []
                try:
                    from bot.utils.database import get_recent_transactions
                    txn_logs = await get_recent_transactions(200)
                except Exception:
                    txn_logs = list(reversed(_storage.get("transaction_logs", [])))
                user_txns = [t for t in txn_logs if str(t.get("user_id", "")) == str(uid)]
                if user_txns:
                    lines.append(f"\n**Transactions ({len(user_txns)}):**")
                    for t in user_txns[:10]:
                        ts = str(t.get("created_at", t.get("timestamp", "N/A")))[:16].replace("T", " ")
                        action = t.get("action", "N/A")
                        try:
                            amount = float(t.get("amount") or 0)
                        except (TypeError, ValueError):
                            amount = 0.0
                        try:
                            lines.append(f"  {ts} - {action} (${amount:.2f})")
                        except (TypeError, ValueError):
                            lines.append(f"  {ts} - {action}")
                else:
                    lines.append("\n**Transactions:** None")

                failed = []
                try:
                    from bot.utils.database import get_failed_verifications
                    failed = await get_failed_verifications(100)
                except Exception:
                    failed = list(reversed(_storage.get("failed_verifications", [])))
                user_fails = [f for f in failed if str(f.get("user_id", "")) == str(uid)]
                if user_fails:
                    lines.append(f"\n**Failed Verifications ({len(user_fails)}):**")
                    for f in user_fails[:5]:
                        ts = str(f.get("created_at", f.get("timestamp", "N/A")))[:16].replace("T", " ")
                        reason = f.get("reason", "N/A")
                        lines.append(f"  {ts} - {reason}")
                else:
                    lines.append("\n**Failed Verifications:** None")

                lines.append(f"\n**Current Status:**")
                try:
                    user_balance = float(user_info.get('balance') or 0)
                except (TypeError, ValueError):
                    user_balance = 0.0
                lines.append(f"  Balance: ${user_balance:.2f}")
                lines.append(f"  Sessions: {user_info.get('sessions', 0)}")
                lines.append(f"  Banned: {'Yes' if user_info.get('banned') else 'No'}")

                await _panel_reply(client, user_id, message, "\n".join(lines))

            elif action == "search_user":
                try:
                    search_uid = int(text.strip())
                    user_info = get_user_info(search_uid, create_if_missing=False)
                    
                    if not user_info:
                        await _panel_reply(client, user_id, message, 
                            f"🔍 **User Not Found!**\n\n"
                            f"👤 User ID `{search_uid}` - No information found.\n"
                            f"This user has not used the bot or has been deleted.",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("🔙 Back to Advanced Tools", callback_data="menu_advanced_tools")]
                            ])
                        )
                        _admin_manager.clear_state(user_id)
                        return
                    
                    from bot.utils.country_data import get_flag
                    
                    balance = float(user_info.get("balance") or 0)
                    sessions = user_info.get("sessions", 0)
                    banned = user_info.get("banned", False)
                    joined = user_info.get("joined") or "N/A"
                    country_sessions = user_info.get("country_sessions", {})
                    
                    banned_status = "🚫 Banned" if banned else "✅ Active"
                    
                    country_lines = []
                    for cc, count in sorted(country_sessions.items(), key=lambda x: x[1], reverse=True):
                        flag = get_flag(cc) if cc != "other" else "🌍"
                        country_lines.append(f"   {flag} {cc}: {count}")
                    country_text = "\n".join(country_lines) if country_lines else "   No sessions"
                    
                    buttons = [
                        [
                            InlineKeyboardButton("🚫 Ban", callback_data=f"quick_ban_{search_uid}"),
                            InlineKeyboardButton("✅ Unban", callback_data=f"quick_unban_{search_uid}")
                        ],
                        [InlineKeyboardButton("🔙 Back to Advanced Tools", callback_data="menu_advanced_tools")]
                    ]
                    
                    await _panel_reply(client, user_id, message, 
                        f"🔍 **User Found!**\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"👤 **User ID:** `{search_uid}`\n"
                        f"💰 **Balance:** ${balance:.2f}\n"
                        f"✅ **Sessions:** {sessions}\n"
                        f"📌 **Status:** {banned_status}\n"
                        f"📅 **Joined:** {joined[:10] if len(joined) > 10 else joined}\n\n"
                        f"🌍 **Country Sessions:**\n{country_text}",
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                    _admin_manager.clear_state(user_id)
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ Enter a valid User ID (numbers only)")
                    _admin_manager.clear_state(user_id)
                    return

            elif action == "remove_balance":
                try:
                    parts = text.strip().split()
                    if len(parts) == 1:
                        target_uid = user_id
                        amount = float(parts[0])
                    elif len(parts) == 2:
                        target_uid = int(parts[0])
                        amount = float(parts[1])
                    else:
                        raise ValueError()

                    # BUG1 fix: negative amounts previously ADDED balance
                    # (money minting). Reject any non-positive amount up front.
                    if amount <= 0:
                        await _panel_reply(client, user_id, message,
                            "❌ Amount must be a positive number!\n"
                            "To add balance use Add Balance (e.g.: `123456789 +10`)"
                        )
                        _admin_manager.clear_state(user_id)
                        return

                    from bot.data.users import atomic_deduct_balance_by_admin
                    ok, err_code, new_balance = atomic_deduct_balance_by_admin(target_uid, amount)

                    if err_code == "unknown_user":
                        await _panel_reply(client, user_id, message, f"❌ User `{target_uid}` not found!")
                        _admin_manager.clear_state(user_id)
                        return
                    if err_code == "invalid_amount":
                        await _panel_reply(client, user_id, message, "❌ Amount must be a positive number!")
                        _admin_manager.clear_state(user_id)
                        return
                    if err_code == "insufficient":
                        user_info = get_user_info(target_uid, create_if_missing=False) or {}
                        current_balance = float(user_info.get("balance") or 0)
                        await _panel_reply(client, user_id, message,
                            f"❌ Insufficient balance!\n"
                            f"Current: ${current_balance:.2f}, Requested: ${amount:.2f}"
                        )
                        _admin_manager.clear_state(user_id)
                        return
                    if not ok:
                        await _panel_reply(client, user_id, message, "❌ Could not remove balance.")
                        _admin_manager.clear_state(user_id)
                        return
                    current_balance = new_balance + amount

                    await _log_admin_action(
                        user_id,
                        "remove_balance",
                        target_user_id=target_uid,
                        message=f"-{amount:.2f}",
                        details={"old_balance": current_balance, "new_balance": new_balance},
                    )
                    await _panel_reply(client, user_id, message, 
                        f"✅ **Balance Removed Successfully!**\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"👤 User: `{target_uid}`\n"
                        f"💸 Amount Removed: ${amount:.2f}\n"
                        f"💰 New Balance: ${new_balance:.2f}"
                    )
                    _admin_manager.clear_state(user_id)
                except (ValueError, IndexError):
                    await _panel_reply(client, user_id, message, "❌ Invalid format! Use: `123456789 50.00` or `50.00`")
                    _admin_manager.clear_state(user_id)
                    return

        except Exception as e:
            await _panel_reply(client, user_id, message, f"\u274c Error: {str(e)}")
            _admin_manager.clear_state(user_id)
            return
        _admin_manager.clear_state(user_id)


def register_users_features(app: Client):

    @app.on_callback_query(admin_filter & filters.regex(r"^enhanced_search$"))
    async def enhanced_search_callback(client: Client, query: CallbackQuery):
        await query.answer()

        buttons = [
            [InlineKeyboardButton("Search by User ID", callback_data="search_user"),
             InlineKeyboardButton("Search by Phone", callback_data="search_by_phone")],
            [InlineKeyboardButton("Search by Country Code", callback_data="search_by_country"),
             InlineKeyboardButton("View All Users", callback_data="totalusers")],
            [InlineKeyboardButton("Back to Monitor", callback_data="realtime_refresh")],
        ]
        await _edit_callback(query,
            "=== ENHANCED USER SEARCH ===\n",
            reply_markup=InlineKeyboardMarkup(buttons)
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^search_by_phone$"))
    async def search_by_phone_callback(client: Client, query: CallbackQuery):
        await query.answer()
        uid = query.from_user.id
        _set_admin_state(uid, query, "search_phone")
        await _edit_callback(query,
            "=== SEARCH BY PHONE ===\n\n"
            "Enter part of the phone number (e.g. 88017):\n\n"
            "Type /admin to cancel"
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^search_by_country$"))
    async def search_by_country_callback(client: Client, query: CallbackQuery):
        await query.answer()
        uid = query.from_user.id
        _set_admin_state(uid, query, "search_country")
        await _edit_callback(query,
            "=== SEARCH BY COUNTRY ===\n\n"
            "Enter country code (e.g. +880, +1):\n\n"
            "Type /admin to cancel"
        )


    async def _do_phone_search(user_id, message, phone_pattern):
        results = []
        users = _storage.get("users", {})
        pending = _storage.get("pending_sessions", {})
        session_tracker = _storage.get("session_tracker", {})

        for uid, uinfo in users.items():
            matched = False
            for sid, sdata in pending.items():
                if str(sdata.get("user_id", "")) == uid and phone_pattern.lower() in str(sdata.get("phone", "")).lower():
                    results.append({
                        "user_id": uid, "phone": sdata.get("phone", "N/A"),
                        "country": sdata.get("country_code", "N/A"),
                        "balance": uinfo.get("balance", 0),
                        "sessions": uinfo.get("sessions", 0),
                        "banned": uinfo.get("banned", False),
                    })
                    matched = True
                    break
            if not matched:
                for tracker_sessions in session_tracker.values():
                    for s in tracker_sessions:
                        if isinstance(s, dict) and s.get("user_id") == int(uid) if uid.isdigit() else False:
                            if phone_pattern.lower() in str(s.get("phone", "")).lower():
                                results.append({
                                    "user_id": uid, "phone": s.get("phone", "N/A"),
                                    "country": s.get("country_code", "N/A"),
                                    "balance": uinfo.get("balance", 0),
                                    "sessions": uinfo.get("sessions", 0),
                                    "banned": uinfo.get("banned", False),
                                })
                                matched = True
                                break
                    if matched:
                        break

        if not results and _db_available():
            try:
                from bot.utils.database import search_users_by_phone
                db_results = await search_users_by_phone(phone_pattern, 20)
                for r in db_results:
                    uid = str(r.get("user_id", ""))
                    uinfo = users.get(uid, {})
                    results.append({
                        "user_id": uid, "phone": r.get("phone", "N/A"),
                        "country": r.get("country_code", "N/A"),
                        "balance": uinfo.get("balance", 0),
                        "sessions": uinfo.get("sessions", 0),
                        "banned": uinfo.get("banned", False),
                    })
            except Exception:
                pass

        return results

    @app.on_callback_query(admin_filter & filters.regex(r"^quick_view_user_(\d+)$"))
    async def quick_view_user_callback(client: Client, query: CallbackQuery):
        await query.answer()
        import re
        match = re.match(r"^quick_view_user_(\d+)$", str(query.data))
        if not match:
            return
        search_uid = int(match.group(1))

        user_info = get_user_info(search_uid, create_if_missing=False)
        if not user_info:
            await _edit_callback(query, 
                f"User `{search_uid}` not found.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Search", callback_data="enhanced_search")],
                ])
            )
            return

        balance = float(user_info.get("balance") or 0)
        sessions = user_info.get("sessions", 0)
        banned = user_info.get("banned", False)
        joined = user_info.get("joined", "N/A")
        country_sessions = user_info.get("country_sessions", {})

        banned_status = "Banned" if banned else "Active"

        country_lines = []
        for cc, count in sorted(country_sessions.items(), key=lambda x: x[1], reverse=True)[:5]:
            flag = get_flag(cc) if cc != "other" else ""
            country_lines.append(f"  {flag} {cc}: {count}")

        buttons = [
            [
                InlineKeyboardButton("Ban", callback_data=f"quick_ban_{search_uid}"),
                InlineKeyboardButton("Unban", callback_data=f"quick_unban_{search_uid}")
            ],
            [InlineKeyboardButton("Back to Search", callback_data="enhanced_search")],
        ]

        await _edit_callback(query, 
            f"=== User Info ===\n\n"
            f"User ID: `{search_uid}`\n"
            f"Balance: ${balance:.2f}\n"
            f"Sessions: {sessions}\n"
            f"Status: {banned_status}\n"
            f"Joined: {joined[:10] if len(str(joined)) > 10 else joined}\n\n"
            f"Country Sessions:\n" + "\n".join(country_lines if country_lines else ["  None"]),
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    print("Admin Features v2.0 registered successfully")

    # ============================================================
    # 10 NEW CRITICAL FEATURES
    # ============================================================
    
    # ============================================================
    # FEATURE 1: Remove Balance (Debit from user)
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^removebalance$"))
    async def remove_balance_callback(client: Client, query: CallbackQuery):
        await query.answer()
        user_id = query.from_user.id
        _set_admin_state(user_id, query, "remove_balance")
        await _edit_callback(query,
            "💸 Remove Balance (Debit)\n\n"
            "Send User ID and Amount:\n"
            "`123456789 50.00`\n\n"
            "Or just Amount for yourself:\n"
            "`10.00`\n\n"
            "/cancel to abort"
        )

    # ============================================================
    # FEATURE 2: Set Individual User Rate
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^useractivitylogs$"))
    async def user_activity_logs_callback(client: Client, query: CallbackQuery):
        await query.answer()
        user_id = query.from_user.id
        _set_admin_state(user_id, query, "activity_logs_user")
        await _edit_callback(query,
            "📊 User Activity Logs\n\n"
            "Send User ID:\n\n"
            "/cancel to abort"
        )

    # ============================================================
    # FEATURE 6: Ban Management
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^ban_management$"))
    async def ban_management_callback(client: Client, query: CallbackQuery):
        await query.answer()
        from bot.handlers.admin.design import hd
        from bot.handlers.admin.menu import get_ban_management_buttons
        await _edit_callback(query,
            hd("🚫 Ban Management", "Main › Users › Ban Management"),
            reply_markup=get_ban_management_buttons()
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^ban_list$|^bl_page_\d+$"))
    async def ban_list_callback(client: Client, query: CallbackQuery):
        await query.answer()
        users = _storage.get("users", {})
        banned_users = [
            (uid, data) for uid, data in users.items()
            if isinstance(data, dict) and data.get("banned")
        ]

        text = "📋 Ban List\n\n"
        text += f"Total Banned Users: `{len(banned_users)}`\n\n"

        buttons = [
            [InlineKeyboardButton("🚫 Ban User", callback_data="ban"),
             InlineKeyboardButton("✅ Unban User", callback_data="unban")],
            [InlineKeyboardButton("◀ Back to Ban Management", callback_data="ban_management")],
        ]

        if banned_users:
            # MEDIUM-45: paginate (30/page) instead of head-truncating at 30.
            per_page = 30
            total_pages = max(1, (len(banned_users) + per_page - 1) // per_page)
            m_bl = re.match(r"^bl_page_(\d+)$", str(query.data or ""))
            page = int(m_bl.group(1)) if m_bl else 0
            page = max(0, min(page, total_pages - 1))
            for uid, data in banned_users[page * per_page:(page + 1) * per_page]:
                username = data.get("username") or data.get("first_name") or "Unknown"
                display = f"@{username}" if data.get("username") else username
                text += f"  `{uid}` — {display}\n"
            if total_pages > 1:
                text += f"\n📄 Page {page + 1}/{total_pages}"
                nav = []
                if page > 0:
                    nav.append(InlineKeyboardButton("◀ Back", callback_data=f"bl_page_{page - 1}"))
                else:
                    nav.append(InlineKeyboardButton("ㅤ", callback_data="bl_noop"))
                nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="bl_noop"))
                if page < total_pages - 1:
                    nav.append(InlineKeyboardButton("Next ▶", callback_data=f"bl_page_{page + 1}"))
                else:
                    nav.append(InlineKeyboardButton("ㅤ", callback_data="bl_noop"))
                buttons.insert(0, nav)
        else:
            text += "No users are currently banned.\n"

        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(buttons))

    @app.on_callback_query(admin_filter & filters.regex(r"^bl_noop$"))
    async def ban_list_noop_callback(client: Client, query: CallbackQuery):
        await query.answer()

    # ------------------------------------------------------------
    # Ban input helpers
    # ------------------------------------------------------------

    @app.on_callback_query(admin_filter & filters.regex(r"^ban$"))
    async def admin_action_ban(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        await _edit_callback(query,
            "--- BAN USER ---\n\n"
            "Enter ID of user to ban:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back to Ban Management", callback_data="ban_management")]
            ])
        )
        _set_admin_state(uid, query, "ban")

    @app.on_callback_query(admin_filter & filters.regex(r"^unban$"))
    async def admin_action_unban(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        await _edit_callback(query,
            "--- UNBAN USER ---\n\n"
            "Enter ID of user to unban:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back to Ban Management", callback_data="ban_management")]
            ])
        )
        _set_admin_state(uid, query, "unban")

    # ============================================================
    # FEATURE 8: Revenue Dashboard
    # ============================================================


    @app.on_message(ADMIN_TEXT_FILTER)
    async def handle_features_users_input(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id not in admin_state:
            message.continue_propagation()
            return
        action = admin_state[user_id].get("action", "")
        if action not in {"search_phone", "search_country"}:
            message.continue_propagation()
            return
        message._handled_by_admin = True
        text = message.text.strip()
        try:
            if action == "search_phone":
                results = await _do_phone_search(user_id, message, text)
                if not results:
                    await _panel_reply(client, user_id, message, 
                        f"No users found with phone pattern: `{text}`",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("Try Again", callback_data="search_by_phone")],
                            [InlineKeyboardButton("Back to Search", callback_data="enhanced_search")],
                        ])
                    )
                else:
                    lines = [f"=== Search Results: '{text}' ===\n", f"Found: {len(results)} users\n"]
                    # MEDIUM-45: the head-truncation is now explicit - the
                    # admin knows there are more results than displayed.
                    shown = results[:20]
                    if len(results) > 20:
                        lines.append(f"(Showing first 20 of {len(results)} — refine the pattern to narrow down)\n")
                    for r in shown:
                        uid = r.get("user_id", "N/A")
                        phone = r.get("phone", "N/A")
                        country = r.get("country", "N/A")
                        balance = float(r.get("balance") or 0)
                        banned = "BANNED" if r.get("banned") else "ACTIVE"
                        flag = get_flag(country) if country != "N/A" else ""
                        lines.append(
                            f"User: `{uid}` | {phone} {flag}\n"
                            f"  Country: {country} | ${balance:.2f} | {banned}"
                        )

                    buttons = []
                    for r in results[:10]:
                        uid = r.get("user_id", "")
                        buttons.append([
                            InlineKeyboardButton(
                                f"View User {uid}",
                                callback_data=f"quick_view_user_{uid}"
                            )
                        ])
                    buttons.append([InlineKeyboardButton("Back to Search", callback_data="enhanced_search")])

                    await _panel_reply(client, user_id, message, 
                        "\n".join(lines),
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )

                _admin_manager.clear_state(user_id)

            elif action == "search_country":
                country_code = text.strip()
                if not country_code.startswith("+"):
                    country_code = "+" + country_code

                results = []
                pending = _storage.get("pending_sessions", {})
                for sid, sdata in pending.items():
                    if str(sdata.get("country_code", "")).upper() == country_code.upper():
                        results.append(sdata)

                if not results:
                    await _panel_reply(client, user_id, message, 
                        f"No sessions found for country: `{country_code}`",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("Try Again", callback_data="search_by_country")],
                            [InlineKeyboardButton("Back to Search", callback_data="enhanced_search")],
                        ])
                    )
                else:
                    flag = get_flag(country_code)
                    lines = [
                        f"=== Country: {country_code} {flag} ===\n",
                        f"Total Sessions: {len(results)}\n",
                    ]
                    for r in results[:20]:
                        phone = r.get("phone", "N/A")
                        status = r.get("status", "N/A")
                        uid = r.get("user_id", "N/A")
                        lines.append(f"  {phone} | User: `{uid}` | {status}")

                    await _panel_reply(client, user_id, message, 
                        "\n".join(lines),
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("Back to Search", callback_data="enhanced_search")],
                        ])
                    )

                _admin_manager.clear_state(user_id)

        except Exception as e:
            await _panel_reply(client, user_id, message, f"Error: {str(e)}")
            _admin_manager.clear_state(user_id)
