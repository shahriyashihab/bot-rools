# bot/handlers/admin/broadcast.py - broadcast & send-to-user

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
from bot.utils.text import truncate
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

def register_broadcast(app: Client):

    @app.on_callback_query(admin_filter & filters.regex(r"^broadcast$"))
    async def admin_action_broadcast(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        await _edit_callback(query, 
            "--- BROADCAST ---\n\n"
            "Enter the message to broadcast:"
        )
        _set_admin_state(uid, query, "broadcast")

    @app.on_callback_query(admin_filter & filters.regex(r"^sendtouser$"))
    async def admin_action_sendtouser(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        data = str(query.data)

        await query.answer()

        await _edit_callback(query,
            "--- SEND TO USER ---\n\n"
            "Enter the User ID:\n"
            "Example: `123456789`"
        )
        _set_admin_state(uid, query, "sendtouser_step1")

    @app.on_message(ADMIN_TEXT_FILTER)
    async def handle_broadcast_input(client: Client, message: Message):
        user_id = message.from_user.id
        text = (message.text or "").strip()
        if user_id not in admin_state:
            message.continue_propagation()
            return
        action = admin_state[user_id]["action"]
        if action not in {"broadcast", "sendtouser_step1", "sendtouser_step2", "send_sessions_uid"}:
            message.continue_propagation()
            return
        message._handled_by_admin = True
        try:
            if action == "broadcast":
                from pyrogram.errors import FloodWait
                import asyncio
                from bot.constants import BROADCAST_DELAY_SECONDS, MAX_FLOOD_WAIT_SECONDS
                users = get_all_users()
                total = len(users)
                sent, failed = 0, 0
                
                if total == 0:
                    await _panel_reply(client, user_id, message, "❌ No users!")
                else:
                    progress_msg = await _panel_reply(client, user_id, message, 
                        f"📢 **Broadcast starting...**\n\n"
                        f"👥 Total Users: {total}\n"
                        f"⏳ Progress: 0%"
                    )
                    if progress_msg is None:
                        progress_msg = await message.reply(
                            admin_str(user_id, f"📢 **Broadcast starting...**\n\n"
                                f"👥 Total Users: {total}\n"
                                f"⏳ Progress: 0%"
                            )
                        )
                    
                    aborted = False
                    for i, user_id_str in enumerate(users):
                        try:
                            await client.send_message(int(user_id_str), text)
                            sent += 1
                        except FloodWait as e:
                            # H7 FIX: honor the FULL Telegram-ordered wait (up
                            # to MAX_FLOOD_WAIT_SECONDS). The old code capped
                            # the sleep at 30s and retried into the flood
                            # repeatedly, which at 25k+ recipients turned the
                            # broadcast into a sustained flood loop and could
                            # earn the bot a longer ban. A genuinely huge wait
                            # (beyond the cap) aborts the broadcast safely -
                            # the admin can re-run it later.
                            wait_time = int(getattr(e, 'value', 30) or 30)
                            if wait_time > MAX_FLOOD_WAIT_SECONDS:
                                failed += (total - sent - failed) or 1
                                aborted = True
                                try:
                                    await progress_msg.edit_text(
                                        admin_str(user_id, f"📢 **Broadcast ABORTED** ⛔\n\n"
                                            f"Telegram ordered a {wait_time}s flood wait.\n"
                                            f"✅ Sent so far: {sent} / {total}\n\n"
                                            f"Re-run the broadcast later to reach the rest.")
                                    )
                                except Exception:
                                    pass
                                break
                            await asyncio.sleep(wait_time)
                            try:
                                await client.send_message(int(user_id_str), text)
                                sent += 1
                            except Exception:
                                failed += 1
                        except Exception:
                            failed += 1
                        
                        if not aborted and ((i + 1) % 20 == 0 or i + 1 == total):
                            percent = int((i + 1) / total * 100)
                            try:
                                await progress_msg.edit_text(
                                    admin_str(user_id, f"📢 **Broadcast running...**\n\n"
                                        f"👥 Total: {total}\n"
                                        f"✅ Sent: {sent}\n"
                                        f"❌ Failed: {failed}\n"
                                        f"⏳ Progress: {percent}%"
                                    )
                                )
                            except Exception:
                                pass  # Edit may fail if too fast
                        
                        # H7 FIX: honor the configured per-message delay
                        # (BROADCAST_DELAY_SECONDS = 0.3s) instead of the old
                        # 0.05s (~20 msg/s) that tripped sustained flood limits.
                        await asyncio.sleep(BROADCAST_DELAY_SECONDS)
                    
                    if aborted:
                        # The ABORTED panel above is final - do not overwrite it
                        # with a "Complete!" message (the admin must see the
                        # re-run instruction).
                        await _log_admin_action(
                            user_id, "broadcast", message=f"ABORTED total={total} sent={sent} failed={failed}"
                        )
                    else:
                        try:
                            await progress_msg.edit_text(
                                admin_str(user_id, f"📢 **Broadcast Complete!** ✅\n\n"
                                    f"👥 Total Users: {total}\n"
                                    f"✅ Sent: {sent}\n"
                                    f"❌ Failed: {failed}\n"
                                    f"📊 Success Rate: {int(sent/total*100) if total > 0 else 0}%"
                                )
                            )
                        except Exception:
                            await _panel_reply(client, user_id, message, 
                                f"📢 **Broadcast Complete!** ✅\n\n"
                                f"✅ Sent: {sent}\n"
                                f"❌ Failed: {failed}"
                            )
                        await _log_admin_action(
                            user_id, "broadcast", message=f"total={total} sent={sent} failed={failed}"
                        )

            elif action == "sendtouser_step1":
                try:
                    target_user_id = int(text)
                    _cur_state = admin_state[user_id]
                    admin_state[user_id] = {
                        "action": "sendtouser_step2",
                        "target_user_id": target_user_id,
                        "panel_chat_id": _cur_state.get("panel_chat_id"),
                        "panel_msg_id": _cur_state.get("panel_msg_id"),
                    }
                    await _panel_reply(client, user_id, message,
                        f"✅ User ID: `{target_user_id}` set!\n\n"
                        f"Now enter the message:\n\n"
                        f"⚠️ Type /admin to cancel"
                    )
                    return
                except ValueError:
                    await _panel_reply(client, user_id, message, "❌ Enter a valid User ID (numbers only)")
                    return

            elif action == "sendtouser_step2":
                target_user_id = admin_state[user_id].get("target_user_id")
                if not target_user_id:
                    await _panel_reply(client, user_id, message, "❌ Error: Target user not found. Try again with /admin")
                else:
                    try:
                        await client.send_message(target_user_id, text)
                        await _panel_reply(client, user_id, message, 
                            f"✅ **Message sent successfully!**\n\n"
                            f"👤 User ID: `{target_user_id}`\n"
                            f"📝 Message:\n```\n{truncate(text, 200)}\n```"
                        )
                    except Exception as e:
                        await _panel_reply(client, user_id, message, f"❌ Failed to send message!\n\nError: {str(e)}")

            elif action == "send_sessions_uid":
                try:
                    target_uid = int(text.strip())
                except (ValueError, TypeError):
                    await _panel_reply(client, user_id, message, "❌ Enter a valid User ID (numbers only)")
                    return

                _cur_state = admin_state[user_id]
                admin_state[user_id] = {
                    "action": "send_sessions_choice",
                    "target_uid": target_uid,
                    "panel_chat_id": _cur_state.get("panel_chat_id"),
                    "panel_msg_id": _cur_state.get("panel_msg_id"),
                }

                await _panel_reply(
                    client, user_id, message,
                    f"✅ Target user set: `{target_uid}`\n\n"
                    f"📤 **Which sessions to send?**",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Send Active Sessions", callback_data="send_sessions_active"),
                         InlineKeyboardButton("Send Pending Sessions", callback_data="send_sessions_pending")],
                        [InlineKeyboardButton("Send All Sessions", callback_data="send_sessions_all"),
                         InlineKeyboardButton("Cancel", callback_data="admin_cancel_operation")],
                    ])
                )
                return

        except Exception as e:
            await _panel_reply(client, user_id, message, f"\u274c Error: {str(e)}")
            _admin_manager.clear_state(user_id)
            return
        _admin_manager.clear_state(user_id)


def register_broadcast_features(app: Client):

    @app.on_callback_query(admin_filter & filters.regex(r"^send_sessions_menu$"))
    async def send_sessions_menu_callback(client: Client, query: CallbackQuery):
        await query.answer()
        user_id = query.from_user.id
        _set_admin_state(user_id, query, "send_sessions_uid")
        await _edit_callback(query,
            "📤 Send Sessions to User\n\n"
            "Send target **User ID**:\n"
            "Example: `123456789`\n\n"
            "Cancel via the button below or `/cancel`.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Cancel", callback_data="admin_cancel_operation")]
            ])
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^send_sessions_active$"))
    async def send_sessions_active_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "Sending active sessions..."))
        await _send_sessions_to_user(client, query, "active")


    @app.on_callback_query(admin_filter & filters.regex(r"^send_sessions_pending$"))
    async def send_sessions_pending_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "Sending pending sessions..."))
        await _send_sessions_to_user(client, query, "pending")


    @app.on_callback_query(admin_filter & filters.regex(r"^send_sessions_all$"))
    async def send_sessions_all_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "Sending all sessions..."))
        await _send_sessions_to_user(client, query, "all")


    async def _send_sessions_to_user(client: Client, query: CallbackQuery, session_type: str):
        import os
        import asyncio

        state = admin_state.get(query.from_user.id)
        target_uid = state.get("target_uid") if state else None
        try:
            target_uid = int(target_uid)
        except (TypeError, ValueError):
            target_uid = None

        if not target_uid:
            await _edit_callback(query,
                "❌ Target User ID not set. Start again.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Session Management", callback_data="menu_session")]
                ])
            )
            return

        if session_type == "active":
            sessions = get_exportable_sessions()
            type_name = "Active"
        elif session_type == "pending":
            sessions = get_pending_exportable_sessions()
            type_name = "Pending"
        else:
            active_sessions = get_exportable_sessions()
            pending_sessions = get_pending_exportable_sessions()
            sessions = {**active_sessions, **pending_sessions}
            type_name = "All"

        if not sessions:
            await _edit_callback(query,
                f"❌ No {type_name} sessions to send!",
                reply_markup=get_session_management_buttons()
            )
            return

        await _edit_callback(query,
            f"📤 **Sending {type_name} Sessions**\n\n"
            f"⏳ Sending {len(sessions)} session(s) to `{target_uid}`...\n"
            f"Please wait..."
        )

        sent = 0
        failed = 0
        sent_ids = []

        for session_id, session_data in sessions.items():
            try:
                phone = session_data.get("phone", "")
                session_string = session_data.get("session_string", "")
                twofa_password = session_data.get("twofa_password", "")
                session_inner = session_data.get("session_data", {})
                country_code = session_data.get("country_code", "") or session_inner.get("country_code", "")
                telethon_file = session_data.get("telethon_file", "") or session_inner.get("telethon_file", "")

                caption = (
                    f"📱 **Session Account**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Phone: `{phone}`\n"
                    f"Country: `{country_code}`\n"
                    f"2FA: `{twofa_password}`\n"
                    f"Session ID: `{session_id}`\n"
                )

                # Check EVERY produced artifact, not just telethon_file. A
                # session has up to three files (telethon .session, pyrogram
                # .session, verified JSON). Sending only the telethon file when
                # the buyer needs the JSON - or silently dropping to a session
                # string - loses data. Send each artifact that exists; if none
                # exists fall back to the session string as a last resort.
                #
                # Actual storage keys: the Pyrogram .session is stored at ROOT
                # as ``session_file`` (NOT inside session_data), the Telethon
                # file as ``telethon_file``, and the JSON is never stored in
                # the record - it lives at verified_sessions/<phone>.json and
                # must be derived from the phone number.
                candidate_files = [
                    ("telethon", telethon_file),
                    ("pyrogram", session_data.get("session_file", "") or ""),
                ]
                # JSON files are stored at verified_sessions/<phone>.json
                # (legacy plaintext) or <phone>.json.enc (P6-60 encrypted
                # mirror). Read via load_verified_session_json which handles
                # both, then materialize a temp plaintext copy for delivery.
                if phone:
                    from bot.services.session_io import load_verified_session_json
                    _json_text = load_verified_session_json(phone)
                    if _json_text:
                        try:
                            import tempfile
                            safe_phone = str(phone).replace("+", "")
                            with tempfile.NamedTemporaryFile(
                                mode="w", suffix=f"_{safe_phone}.json",
                                prefix="session_json_", delete=False,
                                encoding="utf-8",
                            ) as _tmp_json:
                                _tmp_json.write(_json_text)
                                _tmp_json_path = _tmp_json.name
                            candidate_files.append(("json", _tmp_json_path))
                        except Exception as json_tmp_err:
                            print(f"❌ Could not materialize session JSON for {session_id}: {json_tmp_err}")
                sent_any = False
                for _label, _path in candidate_files:
                    if _path and os.path.exists(_path):
                        try:
                            await client.send_document(
                                chat_id=target_uid,
                                document=_path,
                                caption=caption if not sent_any else None,
                                file_name=os.path.basename(_path)
                            )
                            sent_any = True
                        except Exception as doc_err:
                            print(f"❌ Error sending {_label} file for {session_id}: {doc_err}")

                if not sent_any and session_string:
                    await client.send_message(
                        chat_id=target_uid,
                        text=f"{caption}\n\n**Session String:**\n`{session_string}`"
                    )
                    sent_any = True

                if not sent_any:
                    raise Exception("No session file or session string available")

                mark_session_message_sent(session_id)
                sent_ids.append(session_id)
                sent += 1
                await asyncio.sleep(1)
            except Exception as e:
                failed += 1
                print(f"❌ Error sending session {session_id} to {target_uid}: {e}")

        deleted_count = delete_exported_sessions(sent_ids) if sent_ids else 0

        await _edit_callback(query,
            f"✅ *Send Complete!*\n\n"
            f"📤 Delivered to user: `{target_uid}`\n"
            f"✅ Sent: **{sent}**\n"
            f"❌ Failed: **{failed}**\n"
            f"🗑️ Removed from storage: **{deleted_count}**",
            reply_markup=get_session_management_buttons()
        )

    # ============================================================
    # FEATURE 5: Auto-Approve Settings
    # ============================================================

