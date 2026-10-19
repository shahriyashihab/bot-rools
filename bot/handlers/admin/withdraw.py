# bot/handlers/admin/withdraw.py - withdraw settings, approvals, revenue

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
from bot.utils.country_data import get_flag
from bot.utils.text import safe_text
from bot.config import ADMIN_ID, SESSION_2FA_PASSWORD, get_2fa_password, get_channel, EXTRA_APIS, API_ID
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

def _finance_screen_text():
    """Compact Finance & Withdrawals menu screen text."""
    from bot.handlers.admin.design import hd, sw
    from bot.data.withdrawals import is_withdraw_enabled
    return hd("💰 Finance & Withdrawals", "Main › Finance", sw("Withdraw", is_withdraw_enabled()))


def register_withdraw(app: Client):

    @app.on_callback_query(admin_filter & filters.regex(r"^menu_withdraw$"))
    async def menu_withdraw_callback(client: Client, query: CallbackQuery):
        await query.answer()
        await _edit_callback(query,
            _finance_screen_text(),
            reply_markup=get_withdraw_finance_buttons()
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^wapprove_([A-Za-z0-9]+)$"))
    async def approve_withdraw_callback(client: Client, query: CallbackQuery):
        """Approve a pending withdraw request by request_id or legacy index"""
        await query.answer(admin_str(query.from_user.id, "⏳ Processing..."))
        try:
            from bot.utils.storage import approve_withdraw_request
            from bot.utils.fast_cache import invalidate_user_cache
            
            match = query.matches[0] if query.matches else None
            if not match:
                await _edit_callback(query, "❌ Invalid request")
                return
            
            raw = match.group(1)
            # request_id values start with 'w' + digits; plain digits = legacy index
            index = raw if raw.startswith("w") else int(raw)
            result = approve_withdraw_request(index)
            
            if result.get("success"):
                user_id = result.get("user_id", "N/A")
                amount = float(result.get("amount") or 0)
                method = result.get("method", "N/A")
                wallet = result.get("wallet_address", "N/A")
                timestamp = result.get("timestamp", "")
                breakdown = result.get("country_breakdown")
                
                from bot.data.withdrawals import format_country_breakdown
                bd_text = format_country_breakdown(breakdown, include_header=True)
                
                try:
                    invalidate_user_cache(int(user_id))
                except (ValueError, TypeError):
                    pass
                
                approve_text = (
                    f"✅ **WITHDRAW APPROVED**\n\n"
                    f"👤 User ID: `{user_id}`\n"
                    f"💰 Amount: *${amount:.2f}*\n"
                    f"💳 Method: *{method}*\n"
                    f"📬 Wallet: `{wallet}`\n"
                    f"🕒 Submitted: {timestamp}\n"
                    f"🕒 Approved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                if bd_text:
                    approve_text += f"\n\n{bd_text}"

                # MEDIUM-30: the user previously NEVER heard about the
                # decision - the panel showed a success but the user sat in
                # "pending" forever. Best-effort notification, never blocking.
                try:
                    from bot.utils.i18n import get_user_lang, t
                    from bot.bootstrap.client import app as _app
                    await _app.send_message(
                        chat_id=int(user_id),
                        text=t("withdraw_approved_user", get_user_lang(int(user_id)),
                               amount=f"{amount:.2f}", method=method, wallet=wallet),
                    )
                except Exception as user_notify_err:
                    logger.warning(f"User approve-notify skipped for {user_id}: {user_notify_err}")

                await _edit_callback(query,
                    approve_text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🧾 Withdraw Logs", callback_data="withdrawlogs")],
                        [InlineKeyboardButton("Back to Withdraw & Finance", callback_data="menu_withdraw")]
                    ])
                )

                if get_channel("CHANNEL_WITHDRAW_REPORT"):
                    try:
                        await client.send_message(
                            chat_id=get_channel("CHANNEL_WITHDRAW_REPORT"),
                            text=approve_text
                        )
                    except Exception as channel_err:
                        logger.error(f"Failed to send approve report to channel: {channel_err}")
            else:
                # MEDIUM-31: a second click (already-settled request) used to
                # REPLACE the success screen with an error. Keep the outcome
                # visible and tell the admin to re-render instead.
                err = result.get("error", "Unknown error")
                if err == "Request not found":
                    await query.answer("⚠️ Already settled (or list changed) - reopen Withdraw Logs", show_alert=True)
                else:
                    await _edit_callback(query, f"Error: {err}")
        except Exception as e:
            await _edit_callback(query, f"Approve Error: {str(e)}")


    @app.on_callback_query(admin_filter & filters.regex(r"^wreject_([A-Za-z0-9]+)$"))
    async def reject_withdraw_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "Processing..."))
        try:
            from bot.utils.storage import reject_withdraw_request
            from bot.utils.fast_cache import invalidate_user_cache
            
            match = query.matches[0] if query.matches else None
            if not match:
                await _edit_callback(query, "Invalid request")
                return
            
            raw = match.group(1)
            # request_id values start with 'w' + digits; plain digits = legacy index
            index = raw if raw.startswith("w") else int(raw)
            result = reject_withdraw_request(index)
            
            if result.get("success"):
                user_id = result.get("user_id", "N/A")
                amount = float(result.get("amount") or 0)
                method = result.get("method", "N/A")
                wallet = result.get("wallet_address", "N/A")
                timestamp = result.get("timestamp", "")
                breakdown = result.get("country_breakdown")
                
                from bot.data.withdrawals import format_country_breakdown
                bd_text = format_country_breakdown(breakdown, include_header=True)
                
                try:
                    invalidate_user_cache(int(user_id))
                except (ValueError, TypeError):
                    pass
                
                reject_text = (
                    f"❌ **WITHDRAW REJECTED**\n\n"
                    f"👤 User ID: `{user_id}`\n"
                    f"💰 Amount: *${amount:.2f}*\n"
                    f"💳 Method: *{method}*\n"
                    f"📬 Wallet: `{wallet}`\n"
                    f"🔄 Refunded: *${amount:.2f}*\n"
                    f"🕒 Submitted: {timestamp}\n"
                    f"🕒 Rejected: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                if bd_text:
                    reject_text += f"\n\n{bd_text}"

                # MEDIUM-30: best-effort user notification (never blocking).
                try:
                    from bot.utils.i18n import get_user_lang, t
                    from bot.bootstrap.client import app as _app
                    await _app.send_message(
                        chat_id=int(user_id),
                        text=t("withdraw_rejected_user", get_user_lang(int(user_id)),
                               amount=f"{amount:.2f}", method=method, wallet=wallet),
                    )
                except Exception as user_notify_err:
                    logger.warning(f"User reject-notify skipped for {user_id}: {user_notify_err}")

                await _edit_callback(query,
                    reject_text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🧾 Withdraw Logs", callback_data="withdrawlogs")],
                        [InlineKeyboardButton("Back to Withdraw & Finance", callback_data="menu_withdraw")]
                    ])
                )

                if get_channel("CHANNEL_WITHDRAW_REPORT"):
                    try:
                        await client.send_message(
                            chat_id=get_channel("CHANNEL_WITHDRAW_REPORT"),
                            text=reject_text
                        )
                    except Exception as channel_err:
                        logger.error(f"Failed to send reject report to channel: {channel_err}")
            else:
                # MEDIUM-31: second click - see approve handler note.
                err = result.get("error", "Unknown error")
                if err == "Request not found":
                    await query.answer("⚠️ Already settled (or list changed) - reopen Withdraw Logs", show_alert=True)
                else:
                    await _edit_callback(query, f"Error: {err}")
        except Exception as e:
            await _edit_callback(query, f"Reject Error: {str(e)}")


    # ---------------- Toggle Monitor Callback ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^setmin_(usdt|trx|cardname|binance)$"))
    async def set_min_withdraw_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data_str = str(query.data) if query.data else ""
        method = data_str.replace("setmin_", "")
        method_names = {"usdt": "USDT (BEP20)", "trx": "TRX", "cardname": "CARD NAME", "binance": "Binance"}
        method_name = method_names.get(method, method.upper())
        
        await query.answer()
        await _edit_callback(query, 
            f"--- SET MIN WITHDRAW: {method_name} ---\n\n"
            f"Enter minimum withdraw amount:\n"
            f"Example: 10 (means $10 minimum)"
        )
        _set_admin_state(uid, query, "setmin", method=method)


    @app.on_callback_query(admin_filter & filters.regex(r"^withdrawlogs$"))
    async def admin_action_withdrawlogs(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        logs = get_withdraw_logs()
            
        header = (
            "--- WITHDRAW LOGS ---\n"
            f"Total Logs: {len(logs)}\n"
            "---------------------\n"
        )
            
        if logs:
            details = "RECENT WITHDRAWS:\n"
            from bot.data.withdrawals import format_country_breakdown
            for idx, log in enumerate(reversed(logs[-20:]), 1):
                user_id = log.get('user_id', 'N/A')
                amount = float(log.get('amount') or 0)
                method = log.get('method', 'N/A')
                status = log.get('status', 'N/A')
                timestamp = log.get('timestamp', 'N/A')
                wallet_address = log.get('wallet_address', 'N/A')
                breakdown = log.get('country_breakdown')
                    
                status_labels = {
                    "approved": "APPROVED",
                    "pending": "PENDING",
                    "rejected": "REJECTED",
                    "expired": "EXPIRED",
                }
                status_text = status_labels.get(status, f"⚠️ {str(status).upper() or 'UNKNOWN'}")
                    
                details += (
                    f"{idx}. User:`{user_id}` | ${amount:.2f} | {method} | {status_text}\n"
                    f"   Wallet:`{wallet_address}` | {timestamp}\n"
                )
                bd_text = format_country_breakdown(breakdown, include_header=False)
                if bd_text:
                    details += bd_text + "\n"
            if len(logs) > 20:
                details += f"Showing last 20 of {len(logs)}...\n"
        else:
            details = "No withdraw logs found."
            
        response = header + details
            
        if len(response) > 4000:
            await _edit_callback(query, response[:4000])
            for i in range(4000, len(response), 4000):
                await query.message.reply(response[i:i+4000])
        else:
            await _edit_callback(query, response, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back to Withdraw & Finance", callback_data="menu_withdraw")]
            ]))

    @app.on_callback_query(admin_filter & filters.regex(r"^togglewithdraw$"))
    async def admin_action_togglewithdraw(client: Client, query: CallbackQuery):
        uid = query.from_user.id

        await query.answer()

        try:
            status = toggle_withdraw()

            from bot.utils.fast_cache import force_refresh_withdraw_status
            force_refresh_withdraw_status(status)

            await _edit_callback(query,
                _finance_screen_text(),
                reply_markup=get_withdraw_finance_buttons()
            )
        except Exception as e:
            print(f"Error toggling withdraw: {e}")
            import traceback
            traceback.print_exc()
            await _edit_callback(query, f"Error: {str(e)}")

    @app.on_callback_query(admin_filter & filters.regex(r"^minwithdraw$"))
    async def admin_action_minwithdraw(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        amounts = get_min_withdraw_amounts()
        buttons = [
            [InlineKeyboardButton(f"USDT - ${amounts.get('usdt', 0)}", callback_data="setmin_usdt"),
             InlineKeyboardButton(f"TRX - ${amounts.get('trx', 0)}", callback_data="setmin_trx")],
            [InlineKeyboardButton(f"CARD - ${amounts.get('cardname', 0)}", callback_data="setmin_cardname"),
             InlineKeyboardButton(f"Binance - ${amounts.get('binance', 0)}", callback_data="setmin_binance")],
            [InlineKeyboardButton("Back to Withdraw & Finance", callback_data="menu_withdraw")]
        ]
        await _edit_callback(query,
            "--- MIN WITHDRAW SETTINGS ---\n\n"
            "Which method minimum to edit?",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^user_withdraw_control$"))
    async def admin_action_user_withdraw_control(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        await _edit_callback(query,
            "--- USER WITHDRAW CONTROL ---\n\n"
            "Enter the User ID:"
        )
        _set_admin_state(uid, query, "user_withdraw_step1")

    @app.on_callback_query(admin_filter & filters.regex(r"^uwc_.*$"))
    async def admin_action_uwc__(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        # MEDIUM-19: `int(parts[1])` used to raise past the handler on a
        # malformed callback (bot-internal, but belt-and-braces) - wrap the
        # whole parse so a bad callback degrades to a clean notice.
        parts = data.split("_")
        try:
            target_uid = int(parts[1])
        except (IndexError, ValueError):
            await _edit_callback(query, "⚠️ Malformed request - reopen the screen.")
            return
        cmd = parts[2] if len(parts) > 2 else ""

        if cmd == "toggle":
            from bot.utils.storage import get_user_withdraw_config, set_user_withdraw_config
            current = get_user_withdraw_config(target_uid).get("disabled", False)
            set_user_withdraw_config(target_uid, disabled=not current)
            await query.answer()

            config = get_user_withdraw_config(target_uid)
            from bot.handlers.admin.design import sw
            withdraw_on = not config.get("disabled")
            min_amt = admin_str(uid, config.get("min", "Default"))
            max_amt = admin_str(uid, config.get("max", "Unlimited"))
            buttons = [
                [InlineKeyboardButton(sw("Withdraw", withdraw_on), callback_data=f"uwc_{target_uid}_toggle")],
                [InlineKeyboardButton(f"Min: ${min_amt}", callback_data=f"uwc_{target_uid}_min"),
                 InlineKeyboardButton(f"Max: ${max_amt}", callback_data=f"uwc_{target_uid}_max")],
                [InlineKeyboardButton("Back", callback_data="menu_withdraw")]
            ]
            await _edit_callback(query,
                f"--- USER WITHDRAW SETTINGS ---\n"
                f"User: `{target_uid}`",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        elif cmd == "min":
            await _edit_callback(query, f"Enter MINIMUM withdraw for `{target_uid}`:")
            _set_admin_state(uid, query, "user_withdraw_min", target_uid=target_uid)
        elif cmd == "max":
            await _edit_callback(query, f"Enter MAXIMUM withdraw for `{target_uid}`:")
            _set_admin_state(uid, query, "user_withdraw_max", target_uid=target_uid)

    @app.on_callback_query(admin_filter & filters.regex(r"^allbalances$"))
    async def admin_action_allbalances(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        report, total_users, users_with_balance, total_balance = get_all_balances_report()
            
        header = (
            "--- ALL BALANCES REPORT ---\n"
            f"Total Users: {total_users}\n"
            f"Users with Balance: {users_with_balance}\n"
            f"Total Unreleased: ${total_balance:,.2f}\n"
            "---------------------------\n"
        )
            
        if not report:
            await _edit_callback(query, header + "No users with balance.")
        else:
            full_message = header + report
            if len(full_message) > 4000:
                await _edit_callback(query, header)
                for i in range(0, len(report), 4000):
                    await query.message.reply(report[i:i+4000])
            else:
                await _edit_callback(query, full_message)

    @app.on_message(ADMIN_TEXT_FILTER)
    async def handle_withdraw_input(client: Client, message: Message):
        user_id = message.from_user.id
        text = (message.text or "").strip()
        if user_id not in admin_state:
            message.continue_propagation()
            return
        action = admin_state[user_id]["action"]
        if action not in {"setmin", "user_withdraw_step1", "user_withdraw_min", "user_withdraw_max"}:
            message.continue_propagation()
            return
        message._handled_by_admin = True
        try:
            if action == "setmin":
                method = admin_state[user_id].get("method", "")
                method_names = {"usdt": "USDT (BEP20)", "trx": "TRX", "cardname": "CARD NAME", "binance": "Binance"}
                method_name = method_names.get(method, method.upper())
                
                try:
                    amount = float(text)
                    if amount < 0:
                        await _panel_reply(client, user_id, message, "❌ Amount must be 0 or more!")
                    else:
                        set_min_withdraw_for_method(method, amount)
                        await _log_admin_action(
                            user_id, "setmin", message=f"{method}={amount}"
                        )
                        await _panel_reply(client, user_id, message, 
                            f"✅ Successfully set!\n\n"
                            f"💵 **{method_name}** minimum withdraw: **${amount}**"
                        )
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ Enter a valid number (e.g.: 10 or 15.5)")

            elif action == "user_withdraw_step1":
                try:
                    target_uid = int(text)
                    from bot.utils.storage import get_user_withdraw_config
                    config = get_user_withdraw_config(target_uid)

                    from bot.handlers.admin.design import sw
                    min_amt = admin_str(user_id, config.get("min", "Default"))
                    max_amt = admin_str(user_id, config.get("max", "Unlimited"))

                    buttons = [
                        [InlineKeyboardButton(sw("Withdraw", not config.get("disabled")), callback_data=f"uwc_{target_uid}_toggle")],
                        [InlineKeyboardButton(f"Min: ${min_amt}", callback_data=f"uwc_{target_uid}_min"),
                         InlineKeyboardButton(f"Max: ${max_amt}", callback_data=f"uwc_{target_uid}_max")],
                        [InlineKeyboardButton("🔙 Back", callback_data="menu_withdraw")]
                    ]
                    await _panel_reply(client, user_id, message,
                        f"👤 Settings for User: `{target_uid}`",
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ Invalid User ID!")

            elif action == "user_withdraw_min":
                target_uid = admin_state[user_id].get("target_uid")
                try:
                    amt = float(text)
                except (ValueError, TypeError):
                    await _panel_reply(client, user_id, message, "❌ Invalid Amount!")
                    return
                # MEDIUM-20: an unknown user returned a silent False from the
                # data layer and the admin still saw "✅ Min withdraw set".
                from bot.utils.storage import set_user_withdraw_config
                if not set_user_withdraw_config(target_uid, min_amt=amt):
                    await _panel_reply(client, user_id, message,
                        f"❌ User `{target_uid}` not found - withdraw settings only exist for real users."
                    )
                    return
                await _panel_reply(client, user_id, message, f"✅ Min withdraw set to ${amt} for `{target_uid}`")

            elif action == "user_withdraw_max":
                target_uid = admin_state[user_id].get("target_uid")
                try:
                    amt = float(text)
                except (ValueError, TypeError):
                    await _panel_reply(client, user_id, message, "❌ Invalid Amount!")
                    return
                from bot.utils.storage import set_user_withdraw_config
                if not set_user_withdraw_config(target_uid, max_amt=amt):
                    await _panel_reply(client, user_id, message,
                        f"❌ User `{target_uid}` not found - withdraw settings only exist for real users."
                    )
                    return
                await _panel_reply(client, user_id, message, f"✅ Max withdraw set to ${amt} for `{target_uid}`")

        except Exception as e:
            await _panel_reply(client, user_id, message, f"\u274c Error: {str(e)}")
            _admin_manager.clear_state(user_id)
            return
        _admin_manager.clear_state(user_id)


def register_withdraw_features(app: Client):

    @app.on_callback_query(admin_filter & filters.regex(r"^revenue_dashboard$"))
    async def revenue_dashboard_callback(client: Client, query: CallbackQuery):
        await query.answer()
        
        try:
            from bot.utils.storage import get_all_balances_report
            _, _, _, total_balance = get_all_balances_report()
        except Exception:
            total_balance = 0.0
            total_balance_error = True
        else:
            total_balance_error = False
        
        total_withdrawn = 0.0
        try:
            withdraws = _storage.get("withdrawals", [])
            for w in withdraws:
                if w.get("status") == "approved":
                    total_withdrawn += float(w.get("amount") or 0)
        except Exception:
            pass
        
        try:
            from bot.utils.storage import get_session_revenue
            today_revenue = get_session_revenue("today")
        except Exception:
            today_revenue = 0.0
        
        pending_withdraws = 0
        try:
            withdraws = _storage.get("withdrawals", [])
            for w in withdraws:
                if w.get("status") == "pending":
                    pending_withdraws += float(w.get("amount") or 0)
        except Exception:
            pass
        
        uid = query.from_user.id
        t_today = admin_str(uid, "Today's Revenue:")
        t_balance = admin_str(uid, 'Total User Balance:')
        t_withdrawn = admin_str(uid, 'Total Withdrawn:')
        t_pending = admin_str(uid, 'Pending Withdrawals:')
        t_net = admin_str(uid, 'Net Balance:')
        text = admin_str(uid, "💰 **Revenue Dashboard**") + "\n"
        text += "━━━━━━━━━━━━━━━━━━━━━\n"
        text += f"{t_today} `${today_revenue:.2f} `\n"
        text += f"{t_balance} `${total_balance:.2f} `\n"
        text += f"{t_withdrawn} `${total_withdrawn:.2f} `\n"
        text += f"{t_pending} `${pending_withdraws:.2f} `\n"
        text += f"{t_net} `${total_balance - total_withdrawn:.2f} `"
        # MEDIUM-49: never present a storage failure as a real $0.00 figure.
        if total_balance_error:
            text += "\n⚠️ " + admin_str(
                uid,
                "User-balance figure unavailable (storage error) - shown as $0.00. Retry this screen.",
            )

        buttons = [
            [InlineKeyboardButton(admin_str(uid, "Today"), callback_data="revenue_today"),
             InlineKeyboardButton(admin_str(uid, "Last 7 Days"), callback_data="revenue_week")],
            [InlineKeyboardButton(admin_str(uid, "Last 30 Days"), callback_data="revenue_month")],
            [InlineKeyboardButton(admin_str(uid, "Back to Withdraw & Finance"), callback_data="menu_withdraw")]
        ]
        
        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(buttons))


    @app.on_callback_query(admin_filter & filters.regex(r"^revenue_today$"))
    async def revenue_today_callback(client: Client, query: CallbackQuery):
        await query.answer()
        await _show_revenue_period(query, "today")


    @app.on_callback_query(admin_filter & filters.regex(r"^revenue_week$"))
    async def revenue_week_callback(client: Client, query: CallbackQuery):
        await query.answer()
        await _show_revenue_period(query, "week")


    @app.on_callback_query(admin_filter & filters.regex(r"^revenue_month$"))
    async def revenue_month_callback(client: Client, query: CallbackQuery):
        await query.answer()
        await _show_revenue_period(query, "month")


    async def _show_revenue_period(query: CallbackQuery, period: str):
        uid = query.from_user.id
        label = admin_str(uid, "Today")
        try:
            from bot.utils.storage import get_session_revenue
            if period == "today":
                revenue = get_session_revenue("today")
                label = admin_str(uid, "Today")
            elif period == "week":
                revenue = get_session_revenue("week")
                label = admin_str(uid, "Last 7 Days")
            else:
                revenue = get_session_revenue("month")
                label = admin_str(uid, "Last 30 Days")
        except Exception as rev_err:
            # MEDIUM-49: a failure used to render a silent $0.00 with the
            # "Today" label - the admin believed revenue WAS zero. Show the
            # failure explicitly instead of inventing a number.
            revenue = None
            label = admin_str(uid, "Today")

        if revenue is None:
            text = "💰 **Revenue Report**\n"
            text += "━━━━━━━━━━━━━━━━━━━━━\n"
            text += admin_str(uid, "⚠️ Revenue data unavailable (storage error). Not shown as $0.00.")
        else:
            text = "💰 **Revenue Report**\n"
            text += "━━━━━━━━━━━━━━━━━━━━━\n"
            text += f"{label}: `${revenue:.2f} `"
            text += f"\n**{label} Revenue:** `${revenue:.2f}`"

        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to Revenue Dashboard", callback_data="revenue_dashboard")]
        ]))

    # ============================================================
    # FEATURE 9: Alert System Config
    # ============================================================

