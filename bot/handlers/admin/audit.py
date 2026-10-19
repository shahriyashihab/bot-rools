# bot/handlers/admin/audit.py - session view, cleanup, export, audit logs

import os
import re
import json
import shutil
import sqlite3
import logging
import asyncio
import tempfile
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
    get_session_export_formats_panel,
    get_session_format_action_panel,
    SESSION_EXPORT_FORMAT_LABELS,
    _create_db_snapshot,
)
from bot.handlers.admin.dashboard import _db_available

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Module-level session export helpers (used by both register_audit
# callbacks and register_audit_features text handler).
# Placed here so the text handler in register_audit_features can
# call _export_sessions without a NameError (the old code nested
# these inside register_audit, making them invisible to features).
# ------------------------------------------------------------------

async def _collect_export_sessions(export_type: str):
    """Return (sessions, type_name) for active / pending / all."""
    if export_type == "active":
        return get_exportable_sessions(), "Active"
    elif export_type == "pending":
        return get_pending_exportable_sessions(), "Pending"
    else:
        active_sessions = get_exportable_sessions()
        pending_sessions = get_pending_exportable_sessions()
        sessions = {**active_sessions, **pending_sessions}
        return sessions, "All"


async def _export_sessions(client: Client, query: CallbackQuery, export_type: str,
                           limit: Optional[int] = None, fmt: Optional[str] = None):
    import zipfile
    import os
    import asyncio
    import re
    from datetime import datetime

    # Validate fmt
    ALLOWED_FMTS = {"telethon", "pyrogram", "tdata", "txt"}
    fmt = fmt if fmt in ALLOWED_FMTS else None

    chat_id = query.message.chat.id
    msg_id = query.message.id
    admin_id = query.from_user.id

    async def _edit(text, reply_markup=None):
        return await _edit_by_ids(client, chat_id, msg_id, text,
                                  reply_markup=reply_markup, admin_id=admin_id)

    sessions, type_name = await _collect_export_sessions(export_type)

    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            await _edit(
                "❌ **Invalid export count!**\n\n"
                "Please enter a positive number of sessions to export.",
                reply_markup=get_session_management_buttons()
            )
            return
        items = list(sessions.items())[:limit]
        sessions = dict(items)
        type_name = f"Manual ({len(sessions)})"

    if not sessions:
        await _edit(
            f"❌ No {type_name} sessions to export!",
            reply_markup=get_session_management_buttons()
        )
        return

    fmt_label = SESSION_EXPORT_FORMAT_LABELS.get(fmt, "All formats") if fmt else "All formats"
    await _edit(
        f"📦 **{type_name} Sessions Export**\n\n"
        f"⏳ {len(sessions)} sessions being processed...\n"
        f"Generating: {fmt_label}\n"
        f"Please wait..."
    )

    safe_type = re.sub(r"[^A-Za-z0-9]", "_", type_name.lower())
    fmt_suffix = f"_{fmt}" if fmt else ""
    zip_filename = f"sessions_{safe_type}{fmt_suffix}.zip"
    # MEDIUM-25: /tmp does not exist on Windows; use the OS temp dir.
    zip_path = os.path.join(tempfile.gettempdir(), zip_filename)
    base_dir = os.path.join(tempfile.gettempdir(), f"session_export_{safe_type}{fmt_suffix}")
    upload_success = False
    session_ids_exported = []
    files_added = 0
    if fmt:
        format_counts = {fmt: 0}
    else:
        format_counts = {"telethon": 0, "pyrogram": 0, "tdata": 0, "txt": 0, "json": 0}

    def _create_zip_blocking():
        """Blocking ZIP creation - runs in separate thread."""
        nonlocal files_added
        local_files_added = 0
        local_session_ids = []
        manifest = []
        used_names = set()
        os.makedirs(base_dir, exist_ok=True)

        from bot.services.session_formats import generate_all_session_formats
        from bot.services.session_io import build_session_data_full
        from bot.utils.device_generator import generate_device_info
        from bot import config
        import json as _json

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for session_id, session_data in sessions.items():
                local_session_ids.append(session_id)
                session_inner = session_data.get("session_data") or {}
                phone = str(session_data.get("phone") or session_inner.get("phone") or f"unknown_{len(local_session_ids)}")
                session_string = session_data.get("session_string") or ""
                user_id = session_inner.get("user_id") or session_data.get("user_id") or 0
                first_name = session_inner.get("first_name") or session_data.get("first_name") or ""

                base = re.sub(r"[^A-Za-z0-9._@+\-]", "_", phone)
                if base in used_names:
                    base = f"{base}_{session_id[-6:]}"
                used_names.add(base)

                session_dir = os.path.join(base_dir, base)
                os.makedirs(session_dir, exist_ok=True)

                formats = generate_all_session_formats(
                    phone=phone,
                    session_string=session_string,
                    user_id=user_id,
                    api_id=config.API_ID,
                    api_hash=config.API_HASH,
                    session_id=session_id,
                    out_dir=session_dir,
                    first_name=first_name,
                    telethon_file=session_data.get("telethon_file") or "",
                    pyrogram_file=session_data.get("session_file") or "",
                    formats={fmt} if fmt else None,
                )

                for fmt_key, fpath in formats.items():
                    if fpath and os.path.exists(fpath):
                        zipf.write(fpath, f"{base}/{os.path.basename(fpath)}")
                        local_files_added += 1
                        if fmt_key in format_counts:
                            format_counts[fmt_key] += 1

                # Per-session JSON only when exporting ALL formats
                if not fmt:
                    try:
                        device_info = generate_device_info()
                        full = build_session_data_full(
                            phone=phone,
                            session_info=session_inner,
                            session_string=session_string,
                            twofa_password=session_data.get("twofa_password") or "",
                            country_code=session_data.get("country_code") or "",
                            rate=session_data.get("rate") or 0,
                            account_status=session_data.get("status") or "pending",
                            status_details=session_data.get("status_details") or {},
                            user_id=str(session_data.get("user_id") or ""),
                            device_info=device_info,
                        )
                        json_path = os.path.join(session_dir, f"{base}.json")
                        with open(json_path, "w", encoding="utf-8") as f:
                            f.write(_json.dumps(full, indent=2, ensure_ascii=False))
                        zipf.write(json_path, f"{base}/{base}.json")
                        local_files_added += 1
                        format_counts["json"] += 1
                    except Exception as e:
                        print(f"JSON export error for {phone}: {e}")

                manifest.append({
                    "session_id": session_id,
                    "phone": phone,
                    "country_code": session_data.get("country_code") or "",
                    "status": session_data.get("status") or "pending",
                    "spam_status": session_data.get("spam_status") or "",
                    "user_id": str(user_id),
                    "formats": list(formats.keys()),
                })

            manifest_path = os.path.join(base_dir, "manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as f:
                _json.dump(manifest, f, ensure_ascii=False, indent=2)
            zipf.write(manifest_path, "manifest.json")
            local_files_added += 1

        return local_session_ids, local_files_added

    try:
        # IMPORTANT: load the telethon-export import chain (bot.verification ->
        # otp -> reports -> bootstrap.client, which instantiates the Pyrogram
        # Client and needs the RUNNING event loop) HERE in the main thread.
        # Inside asyncio.to_thread there is no event loop, so a lazy import of
        # session_string_to_telethon there raised
        # "There is no current event loop in thread 'asyncio_0'" and the whole
        # admin export silently produced an empty ZIP.
        from bot.verification.telethon_export import session_string_to_telethon  # noqa: F401

        session_ids_exported, files_added = await asyncio.to_thread(_create_zip_blocking)

        if os.path.exists(zip_path):
            zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            if zip_size_mb > 49:
                await _edit(
                    f"⚠️ *ZIP file too large ({zip_size_mb:.1f} MB)!*\n\n"
                    f"Telegram allows maximum 50 MB.\n"
                    f"Please try with fewer sessions."
                )
                return

        await _edit(
            f"📦 **{type_name} Sessions Export**\n\n"
            f"⏳ ZIP created ({files_added} files)\n"
            f"📤 Uploading now..."
        )

        if fmt:
            format_summary = f"{fmt_label}: {format_counts.get(fmt, 0)}"
        else:
            format_summary = (
                f"📘 Telethon: {format_counts['telethon']}\n"
                f"📗 Pyrogram: {format_counts['pyrogram']}\n"
                f"📙 TData: {format_counts['tdata']}\n"
                f"📕 TXT: {format_counts['txt']}\n"
                f"📄 JSON: {format_counts['json']}"
            )

        await client.send_document(
            chat_id=query.from_user.id,
            document=zip_path,
            caption=(
                f"📦 **{type_name} Sessions Export Complete!**\n\n"
                f"✅ Sessions: **{len(session_ids_exported)}**\n"
                f"📁 Files: **{files_added}**\n\n"
                f"🧾 Formats included:\n{format_summary}\n\n"
                f"⚠️ Deleting sessions from storage..."
            ),
            file_name=zip_filename
        )
        upload_success = True

        deleted_count = delete_exported_sessions(session_ids_exported)

        def _cleanup_session_files():
            """Blocking file cleanup - runs in separate thread."""
            for session_id in session_ids_exported:
                session_data = sessions.get(session_id, {})
                session_file = session_data.get("session_file", "")
                telethon_file = session_data.get("telethon_file", "")

                for file_path in [session_file, telethon_file]:
                    if file_path and os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except Exception:
                            pass

        await asyncio.to_thread(_cleanup_session_files)

        await _edit(
            f"✅ *Export Complete!*\n\n"
            f"📦 {len(session_ids_exported)} {type_name} sessions exported and deleted.\n"
            f"🗑️ Deleted from storage: **{deleted_count}**\n\n"
            f"You can export again when new sessions arrive.",
            reply_markup=get_session_management_buttons()
        )

    except Exception as e:
        error_msg = str(e)
        print(f"Session export error: {e}")

        if not upload_success:
            try:
                await _edit(
                    f"❌ **Export Error!**\n\n"
                    f"Error: {error_msg}\n\n"
                    f"Sessions were not deleted, please try again."
                )
            except Exception:
                pass

    finally:
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except Exception:
                pass
        try:
            shutil.rmtree(base_dir, ignore_errors=True)
        except Exception:
            pass


def register_audit(app: Client):

    @app.on_callback_query(admin_filter & filters.regex(r"^menu_session$"))
    async def menu_session_callback(client: Client, query: CallbackQuery):
        await query.answer()
        from bot.handlers.admin.design import hd
        await _edit_callback(query,
            hd("📦 Session Management", "Main › Sessions"),
            reply_markup=get_session_management_buttons()
        )

    # ---------------- Advanced Tools Menu ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^menu_advanced_tools$"))
    async def menu_advanced_tools_callback(client: Client, query: CallbackQuery):
        await query.answer()
        from bot.handlers.admin.design import hd
        await _edit_callback(query,
            hd("🛠️ Advanced Tools", "Main › Advanced"),
            reply_markup=get_advanced_tools_buttons()
        )

    # ---------------- Settings & Configuration Menu ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^view_pending_sessions$"))
    async def view_pending_sessions_callback(client: Client, query: CallbackQuery):
        """⏳ View Pending Sessions"""
        await query.answer()
        try:
            pending = get_all_pending_sessions()
            if not pending:
                await _edit_callback(query, 
                    "⏳ **Pending Sessions**\n\n"
                    "✅ No pending sessions.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back to Advanced Tools", callback_data="menu_advanced_tools")]
                    ])
                )
                return
            
            from bot.utils.country_data import get_flag
            
            lines = []
            status_counts = {"pending": 0, "approved": 0, "rejected": 0, "timeout": 0}
            
            for session_id, data in list(pending.items())[:30]:
                phone = data.get("phone", "N/A")
                country_code = data.get("country_code", "N/A")
                status = data.get("status", "pending")
                submitted = data.get("submitted_at", data.get("created_at", "N/A"))
                user_id = data.get("user_id", "N/A")
                flag = get_flag(country_code) if country_code != "N/A" else "🌍"
                
                status_counts[status] = status_counts.get(status, 0) + 1
                
                status_icon = {"pending": "⏳", "approved": "✅", "rejected": "❌", "timeout": "⏰"}.get(status, "❓")
                
                if submitted != "N/A":
                    try:
                        submitted = submitted[:16].replace("T", " ")
                    except (AttributeError, TypeError):
                        pass
                
                lines.append(
                    f"{status_icon} **{phone}** {flag}\n"
                    f"   👤 User: `{user_id}` | 🌍 {country_code}\n"
                    f"   📅 {submitted}"
                )
            
            header = (
                f"⏳ **Pending Sessions**\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 Total: **{len(pending)}**\n"
                f"⏳ Pending: {status_counts.get('pending', 0)} | ✅ Approved: {status_counts.get('approved', 0)}\n"
                f"❌ Rejected: {status_counts.get('rejected', 0)} | ⏰ Timeout: {status_counts.get('timeout', 0)}\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            )
            
            full_msg = header + "\n\n".join(lines[:15])
            if len(pending) > 15:
                full_msg += f"\n\n⚠️ {len(pending) - 15} more sessions..."
            
            buttons = [
                [InlineKeyboardButton("🧹 Cleanup Old Sessions", callback_data="cleanup_old_sessions")],
                [InlineKeyboardButton("🔙 Back to Advanced Tools", callback_data="menu_advanced_tools")]
            ]
            
            if len(full_msg) > 4000:
                await _edit_callback(query, header)
                for chunk in lines[:15]:
                    await query.message.reply(chunk)
                await _edit_callback(query, "✅ All sessions displayed.", reply_markup=InlineKeyboardMarkup(buttons))
            else:
                await _edit_callback(query, full_msg, reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            await _edit_callback(query, f"❌ Error: {str(e)}")


    @app.on_callback_query(admin_filter & filters.regex(r"^cleanup_old_sessions$"))
    async def cleanup_old_sessions_callback(client: Client, query: CallbackQuery):
        """🧹 Cleanup Old Sessions - Confirmation"""
        await query.answer()
        pending = get_all_pending_sessions()
        total_pending = len(pending)
        
        await _edit_callback(query,
            "--- CLEANUP OLD SESSIONS ---\n\n"
            f"Stuck sessions older than {int(os.getenv('STUCK_SESSION_CLEANUP_HOURS', '72') or 72)}h will be deleted. Sure?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Yes, Cleanup", callback_data="confirm_cleanup_sessions"),
                 InlineKeyboardButton("Cancel", callback_data="menu_advanced_tools")]
            ])
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^confirm_cleanup_sessions$"))
    async def confirm_cleanup_sessions_callback(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            before_count = len(get_all_pending_sessions())
            # P7-71: the button previously hard-coded 24h, silently ignoring
            # the admin-configured stuck-session retention (the hourly net
            # uses max_age_hours=72). Use the SAME retention as the hourly
            # net, with an env override for explicit control.
            import os
            retention_hours = int(os.getenv("STUCK_SESSION_CLEANUP_HOURS", "72") or 72)
            cleaned = cleanup_old_stuck_sessions(max_age_hours=retention_hours)
            after_count = len(get_all_pending_sessions())

            await _edit_callback(query,
                "--- CLEANUP COMPLETE ---\n\n"
                f"Before: {before_count} sessions\n"
                f"Cleaned: {cleaned} sessions\n"
                f"After: {after_count} sessions\n\n"
                f"Stuck sessions older than {retention_hours} hours have been deleted.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Advanced Tools", callback_data="menu_advanced_tools")]
                ])
            )
        except Exception as e:
            await _edit_callback(query, f"Cleanup Error: {str(e)}")


    @app.on_callback_query(admin_filter & filters.regex(r"^backup_export_data$"))
    async def backup_export_data_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "Preparing backup..."))
        
        import asyncio
        from datetime import datetime
        
        try:
            from bot.utils.storage import export_storage_json

            def _read_and_prepare_backup():
                """Non-blocking file operations in separate thread"""
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_filename = f"backup_storage_{timestamp}.json"
                backup_path = os.path.join(tempfile.gettempdir(), backup_filename)

                data = export_storage_json()

                stats = {
                    "users": len(data.get("users", {})),
                    "countries": len(data.get("countries", {})),
                    "pending_sessions": len(data.get("pending_sessions", {})),
                    "withdrawals": len(data.get("withdrawals", [])),
                    "admin_logs": len(data.get("admin_logs", []))
                }

                with open(backup_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

                file_size = os.path.getsize(backup_path)

                return backup_path, backup_filename, stats, file_size, timestamp
            
            backup_path, backup_filename, stats, file_size, timestamp = await asyncio.to_thread(_read_and_prepare_backup)
            
            file_size_kb = file_size / 1024
            file_size_str = f"{file_size_kb:.1f} KB" if file_size_kb < 1024 else f"{file_size_kb/1024:.2f} MB"
            
            caption = (
                "--- STORAGE BACKUP ---\n"
                f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Size: {file_size_str}\n\n"
                f"Users: {stats['users']}\n"
                f"Countries: {stats['countries']}\n"
                f"Pending: {stats['pending_sessions']}\n"
                f"Withdrawals: {stats['withdrawals']}\n"
                f"Admin Logs: {stats['admin_logs']}\n\n"
                "Backup created successfully."
            )
            
            await client.send_document(
                chat_id=query.message.chat.id,
                document=backup_path,
                caption=caption,
                file_name=backup_filename
            )
            
            try:
                os.remove(backup_path)
            except OSError:
                pass
            
        except FileNotFoundError:
            await _edit_callback(query, 
                "--- BACKUP ERROR ---\nNo data available to export.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Advanced Tools", callback_data="menu_advanced_tools")]
                ])
            )
        except json.JSONDecodeError as e:
            await _edit_callback(query, 
                f"--- BACKUP ERROR ---\nBackup data is corrupted.\nError: {str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Advanced Tools", callback_data="menu_advanced_tools")]
                ])
            )
        except Exception as e:
            await _edit_callback(query, 
                f"--- BACKUP ERROR ---\n{str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Advanced Tools", callback_data="menu_advanced_tools")]
                ])
            )

    # ============================================================
    # DOWNLOAD DATABASE FILE - 100% COMPLETE BACKUP
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^download_db_file$"))
    async def download_db_file_callback(client: Client, query: CallbackQuery):
        """Download the real data/bot_data.db file (100% complete backup).

        Sends a consistent snapshot of the live database (all tables, all data,
        including anything still in the WAL file). Restore by replacing
        data/bot_data.db with this file while the bot is stopped.
        """
        await query.answer(admin_str(query.from_user.id, "Preparing database file..."))

        import asyncio

        try:
            db_path = SQLITE_STORAGE_PATH

            if not os.path.isfile(db_path):
                await _edit_callback(query,
                    "--- DOWNLOAD DB ERROR ---\nDatabase file not found.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Back to Advanced Tools", callback_data="menu_advanced_tools")]
                    ])
                )
                return

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            snapshot_path = os.path.join(tempfile.gettempdir(), f"bot_data_backup_{timestamp}.db")

            def _prepare_snapshot():
                if not _create_db_snapshot(db_path, snapshot_path):
                    return None
                size_bytes = os.path.getsize(snapshot_path)
                if size_bytes > 1024 * 1024:
                    size_str = f"{size_bytes / (1024 * 1024):.2f} MB"
                else:
                    size_str = f"{size_bytes / 1024:.1f} KB"
                return size_str

            size_str = await asyncio.to_thread(_prepare_snapshot)

            if size_str is None:
                await _edit_callback(query,
                    "--- DOWNLOAD DB ERROR ---\nFailed to create a snapshot of the database.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Back to Advanced Tools", callback_data="menu_advanced_tools")]
                    ])
                )
                return

            caption = (
                "--- DATABASE FILE BACKUP (100%) ---\n"
                f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Size: {size_str}\n"
                f"File: {os.path.basename(snapshot_path)}\n\n"
                "Contains ALL data: users, balances, countries + rates,\n"
                "templates, sessions, transactions, withdrawals, settings.\n\n"
                "Restore: stop the bot, then replace data/bot_data.db with this file.\n"
                "WARNING: This file contains session_strings. Keep it private."
            )

            await client.send_document(
                chat_id=query.message.chat.id,
                document=snapshot_path,
                caption=caption,
                file_name=os.path.basename(snapshot_path)
            )

            try:
                os.remove(snapshot_path)
            except OSError:
                pass

        except Exception as e:
            logger.error(f"Download DB file error: {e}")
            await _edit_callback(query,
                f"--- DOWNLOAD DB ERROR ---\n{str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Advanced Tools", callback_data="menu_advanced_tools")]
                ])
            )

    # ============================================================
    # ⚙️ SETTINGS & CONFIG - FULLY IMPLEMENTED CALLBACKS
    # ============================================================
    

    @app.on_callback_query(admin_filter & filters.regex(r"^export_active_sessions$"))
    async def export_active_sessions_callback(client: Client, query: CallbackQuery):
        """Export Active -> choose a format (level 2)."""
        await query.answer()
        text, markup = get_session_export_formats_panel("active")
        await _edit_callback(query, text, reply_markup=markup)


    @app.on_callback_query(admin_filter & filters.regex(r"^export_pending_sessions$"))
    async def export_pending_sessions_callback(client: Client, query: CallbackQuery):
        """Export Pending -> choose a format (level 2)."""
        await query.answer()
        text, markup = get_session_export_formats_panel("pending")
        await _edit_callback(query, text, reply_markup=markup)


    @app.on_callback_query(admin_filter & filters.regex(r"^export_all_sessions$"))
    async def export_all_sessions_callback(client: Client, query: CallbackQuery):
        """Export All -> choose a format (level 2)."""
        await query.answer()
        text, markup = get_session_export_formats_panel("all")
        await _edit_callback(query, text, reply_markup=markup)


    # ---------------- Session export sub-menus (formats + All/Manual) ----------------

    @app.on_callback_query(admin_filter & filters.regex(r"^sesx_pool_(active|pending|all)$"))
    async def sesx_pool_callback(client: Client, query: CallbackQuery):
        """Back to the format picker for a pool."""
        await query.answer()
        m = re.match(r"^sesx_pool_(active|pending|all)$", str(query.data))
        if not m:
            return
        pool = m.group(1)
        text, markup = get_session_export_formats_panel(pool)
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^sesx_fmt_(active|pending|all)_(telethon|pyrogram|tdata|txt)$"))
    async def sesx_fmt_callback(client: Client, query: CallbackQuery):
        """A format was picked -> show All / Manual action panel (level 3)."""
        await query.answer()
        m = re.match(r"^sesx_fmt_(active|pending|all)_(telethon|pyrogram|tdata|txt)$", str(query.data))
        if not m:
            return
        pool, fmt = m.group(1), m.group(2)
        text, markup = get_session_format_action_panel(pool, fmt)
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^sesx_all_(active|pending|all)_(telethon|pyrogram|tdata|txt)$"))
    async def sesx_all_callback(client: Client, query: CallbackQuery):
        """All Export: export every session of the pool in the chosen format."""
        await query.answer(admin_str(query.from_user.id, "Preparing export..."))
        m = re.match(r"^sesx_all_(active|pending|all)_(telethon|pyrogram|tdata|txt)$", str(query.data))
        if not m:
            return
        pool, fmt = m.group(1), m.group(2)
        await _export_sessions(client, query, pool, fmt=fmt)

    @app.on_callback_query(admin_filter & filters.regex(r"^sesx_manual_(active|pending|all)_(telethon|pyrogram|tdata|txt)$"))
    async def sesx_manual_callback(client: Client, query: CallbackQuery):
        """Manually Export: admin types how many sessions (of the pool+format)
        should be exported. 100 available -> type 10 or 20 -> only those go out."""
        await query.answer()
        m = re.match(r"^sesx_manual_(active|pending|all)_(telethon|pyrogram|tdata|txt)$", str(query.data))
        if not m:
            return
        pool, fmt = m.group(1), m.group(2)
        _set_admin_state(query.from_user.id, query, "manual_export_count",
                         export_pool=pool, export_format=fmt)
        fmt_label = SESSION_EXPORT_FORMAT_LABELS.get(fmt, fmt)
        pool_label = {"active": "Active", "pending": "Pending", "all": "All"}.get(pool, pool)
        await _edit_callback(query,
            f"✍️ **Manually Export - {fmt_label}**\n\n"
            f"Pool: {pool_label}\n\n"
            f"Enter how many sessions to export (1-1000).\n\n"
            f"Type /admin to cancel."
        )

    # ---------------- Approve/Reject Withdraw Callbacks ----------------


def register_audit_features(app: Client):

    @app.on_callback_query(admin_filter & filters.regex(r"^session_detail_view$"))
    async def session_detail_view_callback(client: Client, query: CallbackQuery):
        await query.answer()
        uid = query.from_user.id
        _set_admin_state(uid, query, "session_detail")
        await _edit_callback(query, 
            "=== SESSION DETAIL VIEW ===\n\n"
            "Enter the Session ID to view details:\n\n"
            "Type /admin to cancel"
        )


    async def _show_session_detail(client, message, session_id):
        session_data = None
        session_type = "pending"

        pending = _storage.get("pending_sessions", {})
        if session_id in pending:
            session_data = pending[session_id]
            session_type = "pending"

        if not session_data:
            tracker = _storage.get("session_tracker", {})
            for uid, sessions in tracker.items():
                for s in sessions:
                    if isinstance(s, dict) and s.get("session_id") == session_id:
                        session_data = {
                            **s,
                            "user_id": uid,
                            "status": "verified",
                        }
                        session_type = "verified"
                        break
                if session_data:
                    break

        if not session_data and _db_available():
            try:
                from bot.utils.database import get_session_detail
                db_detail = await get_session_detail(session_id)
                if db_detail:
                    session_data = db_detail
                    session_type = db_detail.get("session_type", "verified")
            except Exception:
                pass

        if not session_data:
            await _panel_reply(client, message.from_user.id, message, 
                f"Session not found!\n\nID: `{session_id}`\n\n"
                "Please enter a valid Session ID."
            )
            return

        phone = session_data.get("phone", "N/A")
        country_code = session_data.get("country_code", "N/A")
        status = session_data.get("status", session_type)
        user_id = session_data.get("user_id", "N/A")
        flag = get_flag(country_code) if country_code != "N/A" else ""

        submitted = session_data.get("submitted_at") or session_data.get("created_at") or "N/A"
        if isinstance(submitted, str) and len(submitted) > 16:
            submitted = submitted[:16].replace("T", " ")

        spam_status = session_data.get("spam_status", "N/A")
        error_count = session_data.get("error_count", 0)

        lines = [
            "=== SESSION DETAIL ===\n",
            f"Session ID: `{session_id[:30]}...`",
            f"Type: {session_type.upper()}",
            f"Phone: {phone} {flag}",
            f"Country: {country_code}",
            f"User ID: `{user_id}`",
            f"Status: {status}",
            f"Spam: {spam_status}",
            f"Submitted: {submitted}",
            f"Error Count: {error_count}",
        ]

        errors = session_data.get("errors", [])
        if errors:
            lines.append(f"\n--- ERRORS ({len(errors)}) ---")
            for e in errors[:5]:
                reason = e.get("reason", "N/A")[:80]
                err_type = e.get("error_type", "")
                et = f" [{err_type}]" if err_type else ""
                lines.append(f"  {reason}{et}")

        txns = session_data.get("transactions", [])
        if txns:
            lines.append(f"\n--- TRANSACTIONS ({len(txns)}) ---")
            for t in txns[:5]:
                action = t.get("action", "N/A")
                try:
                    amount = float(t.get("amount") or 0)
                except (TypeError, ValueError):
                    amount = 0.0
                lines.append(f"  {action}: ${amount:.2f}")

        user_info = get_user_info(int(user_id), create_if_missing=False) if str(user_id).isdigit() else None
        if user_info:
            lines.append(f"\n--- USER INFO ---")
            try:
                audit_balance = float(user_info.get('balance') or 0)
            except (TypeError, ValueError):
                audit_balance = 0.0
            lines.append(f"  Balance: ${audit_balance:.2f}")
            lines.append(f"  Sessions: {user_info.get('sessions', 0)}")
            lines.append(f"  Banned: {user_info.get('banned', False)}")

        await _panel_reply(client, message.from_user.id, message, "\n".join(lines))

    # ============================================================
    # 5. ENHANCED AUDIT LOG
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^enhanced_audit_log$"))
    async def enhanced_audit_log_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "Loading audit logs..."))
        await _show_enhanced_audit_log(query, page=1)


    @app.on_callback_query(admin_filter & filters.regex(r"^audit_log_page_(\d+)$"))
    async def audit_log_page_callback(client: Client, query: CallbackQuery):
        await query.answer()
        import re
        match = re.match(r"^audit_log_page_(\d+)$", str(query.data))
        page = int(match.group(1)) if match else 1
        await _show_enhanced_audit_log(query, page=page)


    async def _show_enhanced_audit_log(query: CallbackQuery, page: int = 1):
        PER_PAGE = 10
        logs = []

        admin_logs = _storage.get("admin_logs", [])
        for entry in reversed(admin_logs):
            logs.append({
                "type": "admin_action",
                "timestamp": entry.get("timestamp", "N/A"),
                "action": entry.get("action", "N/A"),
                "details": entry,
            })

        txn_logs = _storage.get("transaction_logs", [])
        for entry in reversed(txn_logs):
            logs.append({
                "type": "transaction",
                "timestamp": entry.get("timestamp", "N/A"),
                "action": entry.get("action", "N/A"),
                "details": entry,
            })

        if _db_available():
            try:
                from bot.utils.database import get_recent_transactions
                db_txns = await get_recent_transactions(50)
                for entry in db_txns:
                    ts = str(entry.get("created_at", ""))
                    if len(ts) > 16:
                        ts = ts[:16].replace("T", " ")
                    logs.append({
                        "type": "transaction",
                        "timestamp": ts,
                        "action": entry.get("action", "N/A"),
                        "details": entry,
                    })
            except Exception:
                pass

            try:
                from bot.utils.database import get_recent_admin_logs
                db_logs = await get_recent_admin_logs(50)
                for entry in db_logs:
                    ts = str(entry.get("created_at", ""))
                    if len(ts) > 16:
                        ts = ts[:16].replace("T", " ")
                    logs.append({
                        "type": "db_admin",
                        "timestamp": ts,
                        "action": entry.get("action", "N/A"),
                        "details": entry,
                    })
            except Exception:
                pass

        logs.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)

        total_pages = max(1, (len(logs) + PER_PAGE - 1) // PER_PAGE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * PER_PAGE
        page_logs = logs[start:start + PER_PAGE]

        lines = [
            "=== ENHANCED AUDIT LOG ===\n",
            f"Total Entries: {len(logs)} | Page {page}/{total_pages}\n",
        ]

        if not page_logs:
            lines.append("No log entries found.")
        else:
            for i, entry in enumerate(page_logs, start + 1):
                ts = str(entry.get("timestamp", "N/A"))
                if isinstance(ts, str) and len(ts) > 16:
                    ts = ts[:16].replace("T", " ")
                action = entry.get("action", "N/A")
                etype = entry.get("type", "")

                lines.append(f"#{i} [{etype}] {ts} - {action}")

                details = entry.get("details", {})
                if isinstance(details, dict):
                    uid = details.get("user_id") or details.get("target_user_id") or ""
                    if uid:
                        lines.append(f"    User: `{uid}`")
                    amt = details.get("amount", "")
                    if amt:
                        lines.append(f"    Amount: ${amt}")

        buttons = []
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("Previous", callback_data=f"audit_log_page_{page - 1}"))
        if page < total_pages:
            nav.append(InlineKeyboardButton("Next", callback_data=f"audit_log_page_{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("Back to Monitor", callback_data="realtime_refresh")])

        await _edit_callback(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))

    # ============================================================
    # 7. API ROTATION MONITORING
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^session_expiry_settings$"))
    async def session_expiry_settings_callback(client: Client, query: CallbackQuery):
        await query.answer()
        cleanup_hours = get_session_expiry_hours()
        stuck_hours = get_stuck_session_expiry_hours()
        
        text = "⏰ **Session Expiry Settings**\n"
        text += "━━━━━━━━━━━━━━━━━━━━━\n"
        text += f"Cleanup Expiry: `{cleanup_hours}h`\n"
        text += f"Stuck Finalize: `{stuck_hours}h`"
        
        buttons = [
            [InlineKeyboardButton("Cleanup: 12h", callback_data="session_expiry_set_12"),
             InlineKeyboardButton("Cleanup: 24h", callback_data="session_expiry_set_24")],
            [InlineKeyboardButton("Cleanup: 48h", callback_data="session_expiry_set_48"),
             InlineKeyboardButton("Cleanup: 72h", callback_data="session_expiry_set_72")],
            [InlineKeyboardButton("Finalize: 48h", callback_data="stuck_expiry_set_48"),
             InlineKeyboardButton("Finalize: 72h", callback_data="stuck_expiry_set_72")],
            [InlineKeyboardButton("Finalize: 96h", callback_data="stuck_expiry_set_96"),
             InlineKeyboardButton("Finalize: 120h", callback_data="stuck_expiry_set_120")],
            [InlineKeyboardButton("Back to Session Management", callback_data="menu_session")]
        ]
        
        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(buttons))


    @app.on_callback_query(admin_filter & filters.regex(r"^session_expiry_set_(\d+)$"))
    async def session_expiry_set_callback(client: Client, query: CallbackQuery):
        import re
        await query.answer()
        m = re.match(r"^session_expiry_set_(\d+)$", query.data)
        hours = int(m.group(1)) if m else 24
        set_session_expiry_hours(hours)
        await _edit_callback(query,
            f"✅ Cleanup expiry set to **{get_session_expiry_hours()}h**.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back to Session Expiry Settings", callback_data="session_expiry_settings")]
            ])
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^stuck_expiry_set_(\d+)$"))
    async def stuck_expiry_set_callback(client: Client, query: CallbackQuery):
        import re
        await query.answer()
        m = re.match(r"^stuck_expiry_set_(\d+)$", query.data)
        hours = int(m.group(1)) if m else 72
        set_stuck_session_expiry_hours(hours)
        await _edit_callback(query,
            f"✅ Stuck finalize set to **{get_stuck_session_expiry_hours()}h**.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back to Session Expiry Settings", callback_data="session_expiry_settings")]
            ])
        )



    @app.on_message(ADMIN_TEXT_FILTER)
    async def handle_features_audit_input(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id not in admin_state:
            message.continue_propagation()
            return
        action = admin_state[user_id].get("action", "")
        if action not in {"session_detail", "manual_export_count"}:
            message.continue_propagation()
            return
        message._handled_by_admin = True
        text = message.text.strip()
        try:
            if action == "session_detail":
                await _show_session_detail(client, message, text)
                _admin_manager.clear_state(user_id)

            elif action == "manual_export_count":
                try:
                    count = int(text)
                except ValueError:
                    await _panel_reply(client, user_id, message,
                        "❌ **Invalid number!**\n\n"
                        "Please enter a valid positive integer (e.g. 10, 20, 50).\n"
                        "Type /admin to cancel."
                    )
                    return
                if count <= 0 or count > 1000:
                    await _panel_reply(client, user_id, message,
                        "❌ **Count must be between 1 and 1000!**\n\n"
                        "Please enter a valid number.\n"
                        "Type /admin to cancel."
                    )
                    return

                # Export exactly `count` sessions from the chosen pool+format
                state = admin_state.get(user_id, {})
                pool = state.get("export_pool", "all")
                export_fmt = state.get("export_format") or None
                panel_chat = state.get("panel_chat_id") or message.chat.id
                panel_msg = state.get("panel_msg_id")
                # NOTE: the class body below must NOT reference the handler's
                # `message` parameter by name - assigning `message` in the class
                # body makes it a class-local that shadows the function param and
                # raises `NameError: name 'message' is not defined`. Copy the
                # sender into a differently-named local first.
                _fake_sender = message.from_user

                class _FakeChat:
                    id = panel_chat
                class _FakeMsg:
                    id = panel_msg
                    chat = _FakeChat()
                class _FakeQuery:
                    from_user = _fake_sender
                    message = _FakeMsg()
                    async def answer(self, *a, **kw):
                        return None

                await _export_sessions(client, _FakeQuery(), pool, limit=count, fmt=export_fmt)
                _admin_manager.clear_state(user_id)

        except Exception as e:
            await _panel_reply(client, user_id, message, f"Error: {str(e)}")
            _admin_manager.clear_state(user_id)
