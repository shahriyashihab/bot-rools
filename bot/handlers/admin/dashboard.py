# bot/handlers/admin/dashboard.py - stats overview & realtime monitor

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
    get_pending_withdraw_requests,
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
    get_storage_db_size,
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
    is_spam_check_enabled,
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
    get_realtime_buttons,
)

logger = logging.getLogger(__name__)


def _count_sessions_today() -> int:
    """Count session_tracker entries verified today (not all-time)."""
    today = datetime.now().strftime("%Y-%m-%d")
    count = 0
    try:
        for slist in _storage.get("session_tracker", {}).values():
            for s in slist:
                try:
                    if str(s.get("verified_at", ""))[:10] == today:
                        count += 1
                except Exception:
                    pass
    except Exception:
        pass
    return count


def _count_failed_today() -> int:
    """Count failed verifications logged today."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        return sum(
            1 for v in _storage.get("failed_verifications", [])
            if str(v.get("timestamp", ""))[:10] == today
        )
    except Exception:
        return 0


def _build_dashboard_message(admin_id: int = 0) -> str:
    """Build the rich admin dashboard message with live stats."""
    from bot.utils.i18n import admin_t
    try:
        user_count, active_count = get_total_users()
    except Exception:
        user_count, active_count = 0, 0
    
    try:
        pending = get_all_pending_sessions()
        pending_sessions = len(pending)
        pending_processing = sum(
            1 for d in pending.values() if d.get("currently_processing")
        )
    except Exception:
        pending_sessions = 0
        pending_processing = 0
    
    try:
        _, _, _, total_balance = get_all_balances_report()
    except Exception:
        total_balance = 0.0
    
    try:
        withdraw_reqs = get_pending_withdraw_requests()
        pending_withdraws = len(withdraw_reqs)
    except Exception:
        pending_withdraws = 0
    
    try:
        today_sessions = _count_sessions_today()
    except Exception:
        today_sessions = 0
    
    monitor = f"{'🟢' if get_monitor_status() else '🔴'} {admin_t('status_running', admin_id) if get_monitor_status() else admin_t('status_paused', admin_id)}"
    spam = f"{'🟢' if is_spam_check_enabled() else '🔴'} {admin_t('status_enabled', admin_id) if is_spam_check_enabled() else admin_t('status_disabled', admin_id)}"
    wd_on = not _storage.get("withdraw_disabled", False)
    wd_status = f"{'🟢' if wd_on else '🔴'} {admin_t('status_open', admin_id) if wd_on else admin_t('status_closed', admin_id)}"
    monitor_short = "🟢 ON" if get_monitor_status() else "🔴 OFF"
    spam_short = "🟢 ON" if is_spam_check_enabled() else "🔴 OFF"
    wd_short = "🟢 ON" if wd_on else "🔴 OFF"

    countries_configured = len(_storage.get("countries", {}))
    countries_open = sum(1 for d in _storage.get("countries", {}).values() if d.get("open", False))

    return admin_t(
        "admin_dashboard_message", admin_id,
        users=f"{user_count:,}",
        active=f"{active_count:,}",
        sessions=f"{today_sessions}",
        balance=f"{total_balance:,.2f}",
        withdraw=f"{pending_withdraws}",
        status_monitor=monitor,
        status_monitor_short=monitor_short,
        status_spam=spam_short,
        status_wd=wd_short,
        countries_open=f"{countries_open}",
        countries_configured=f"{countries_configured}",
        pending_sessions=f"{pending_sessions}",
        queue_waiting=f"{pending_sessions}",
        queue_processing=f"{pending_processing}",
    )


def _db_available():
    try:
        from bot.utils.database import is_database_ready
        return is_database_ready()
    except Exception:
        return False



def _get_session_queue_stats():
    """Real session-queue stats computed from the actual pending-session storage.

    The old implementation read a ``SessionQueueManager`` singleton that was
    never started anywhere (dead code) - it always returned zeros. These stats
    reflect what the monitor is really doing right now:
      - queue_size  : pending sessions NOT yet claimed by any processing path
      - processing  : pending sessions currently claimed/being processed
      - completed   : sessions already approved/verified
      - failed      : sessions rejected
    """
    try:
        from bot.utils.storage import get_all_pending_sessions
        pending = get_all_pending_sessions()
        queue_size = 0
        processing = 0
        completed = 0
        failed = 0
        for data in pending.values():
            if not isinstance(data, dict):
                continue
            status = data.get("status", "pending")
            info = data.get("session_data", {}) or {}
            if status == "pending":
                if (
                    info.get("currently_processing")
                    or data.get("currently_processing")
                    or info.get("delayed_task_active")
                ):
                    processing += 1
                else:
                    queue_size += 1
            elif status == "rejected":
                failed += 1
            else:
                completed += 1
        return {
            "queue_size": queue_size,
            "processing": processing,
            "completed": completed,
            "failed": failed,
            "stats": {
                "total_processed": completed + failed,
                "total_success": completed,
                "total_failed": failed,
                "total_retried": 0,
            },
        }
    except Exception:
        return {"queue_size": 0, "processing": 0, "completed": 0, "failed": 0, "stats": {}}



def _build_realtime_monitor(admin_id: int = 0) -> str:
    """Build the real-time system monitor message with live stats."""
    from bot.utils.i18n import admin_t
    try:
        user_count, active_count = get_total_users()
    except Exception:
        user_count, active_count = 0, 0

    monitor = f"{'🟢' if get_monitor_status() else '🔴'} {admin_t('status_running', admin_id) if get_monitor_status() else admin_t('status_paused', admin_id)}"
    spam = f"{'🟢' if is_spam_check_enabled() else '🔴'} {admin_t('status_enabled', admin_id) if is_spam_check_enabled() else admin_t('status_disabled', admin_id)}"
    wd_on = not _storage.get("withdraw_disabled", False)
    wd_status = f"{'🟢' if wd_on else '🔴'} {admin_t('status_open', admin_id) if wd_on else admin_t('status_closed', admin_id)}"

    try:
        from bot.utils.api_rotation import get_api_manager
        mgr = get_api_manager()
        total_apis = mgr.get_stats().get("total_apis", 1 + len(EXTRA_APIS))
    except Exception:
        total_apis = 1 + len(EXTRA_APIS)
    apis_status = str(total_apis)
    db_status = "✅" if _db_available() else "❌"

    try:
        storage_size = get_storage_db_size()
    except Exception:
        storage_size = "N/A"
    if isinstance(storage_size, (int, float)):
        if storage_size >= 1024 * 1024 * 1024:
            storage_size = f"{storage_size / (1024 * 1024 * 1024):.1f} GB"
        elif storage_size >= 1024 * 1024:
            storage_size = f"{storage_size / (1024 * 1024):.1f} MB"
        elif storage_size >= 1024:
            storage_size = f"{storage_size / 1024:.1f} KB"
        else:
            storage_size = f"{storage_size} B"

    try:
        pending = get_all_pending_sessions()
        pending_wait = len(pending)
        pending_processing = sum(
            1 for d in pending.values() if d.get("currently_processing")
        )
    except Exception:
        pending_wait = 0
        pending_processing = 0

    countries_configured = len(_storage.get("countries", {}))
    countries_open = sum(1 for d in _storage.get("countries", {}).values() if d.get("open", False))

    try:
        _, _, _, total_balance = get_all_balances_report()
    except Exception:
        total_balance = 0.0

    try:
        wd = get_pending_withdraw_requests()
        pending_wd = len(wd)
    except Exception:
        pending_wd = 0

    completed = _count_sessions_today()
    failed = _count_failed_today()
    processed = completed + failed
    success_pct = (completed / processed * 100) if processed else 0.0

    # P4-44b: proxy fleet state from the watchdog's last persisted probe pass
    # (ts/total/dead_count/dead_by_cc) so the LIVE MONITOR shows the admin
    # whether the proxy system is healthy.
    try:
        from bot.monitor.proxy_watchdog import get_proxy_watchdog_snapshot
        _wd_snap = get_proxy_watchdog_snapshot()
        proxy_total = int(_wd_snap.get("total", 0) or 0)
        proxy_dead = int(_wd_snap.get("dead_count", 0) or 0)
    except Exception:
        proxy_total = 0
        proxy_dead = 0

    last_updated = datetime.now().strftime("%H:%M:%S")

    return admin_t(
        "admin_realtime_monitor_message", admin_id,
        users=f"{user_count:,}",
        active=f"{active_count:,}",
        status_monitor=monitor,
        status_spam=spam,
        status_wd=wd_status,
        apis=apis_status,
        status_db=db_status,
        storage=storage_size,
        sessions=f"{completed}",
        queue_waiting=f"{pending_wait}",
        queue_processing=f"{pending_processing}",
        countries_open=f"{countries_open}",
        countries_configured=f"{countries_configured}",
        proxy_total=f"{proxy_total}",
        proxy_dead=f"{proxy_dead}",
        balance=f"{total_balance:,.2f}",
        pending_wd=f"{pending_wd}",
        pending=f"{pending_wait}",
        completed=f"{completed}",
        failed=f"{failed}",
        processed=f"{processed}",
        success=f"{success_pct:.1f}%",
        last_updated=last_updated,
    )



def register_dashboard_features(app: Client):

    @app.on_callback_query(admin_filter & filters.regex(r"^realtime_refresh$"))
    async def realtime_refresh_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "Refreshing..."))
        msg = _build_realtime_monitor(query.from_user.id)
        await _edit_callback(query, msg, reply_markup=get_realtime_buttons())

    # ============================================================
    # 6. DATABASE HEALTH CHECK
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^db_health_check$"))
    async def db_health_check_callback(client: Client, query: CallbackQuery):
        await query.answer(admin_str(query.from_user.id, "DB check running..."))

        lines = ["=== DATABASE HEALTH CHECK ===\n"]
        db_ok = False

        try:
            from bot.utils.database import get_db_health_stats
            stats = await get_db_health_stats()
            db_ok = stats.get("connected", False)

            lines.append(f"Status: {'Connected' if db_ok else 'Not Connected'}")
            lines.append(f"Backend: {stats.get('backend', 'N/A')}")
            lines.append(f"Journal Mode: {stats.get('journal_mode', 'N/A')}")
            lines.append(f"Foreign Keys: {stats.get('foreign_keys', 'N/A')}")
            lines.append(f"Integrity Check: {stats.get('integrity', 'N/A')}")
            lines.append(f"Indexes: {stats.get('index_count', 0)}")
            lines.append(f"DB Size: {stats.get('total_size', 'N/A')}")
            lines.append(f"WAL Size: {stats.get('wal_size', 'N/A')}")

            tables = stats.get("tables", {})
            if tables:
                lines.append("\n--- TABLES (real row counts) ---")
                total_rows = 0
                for tname, tdata in sorted(tables.items()):
                    rows = tdata.get("estimated_rows", 0)
                    total_rows += rows
                    lines.append(f"  {tname}: {rows:,} rows")
                lines.append(f"\nTotal rows across all tables: {total_rows:,}")
            else:
                lines.append("No table data available.")

            if stats.get("error"):
                lines.append(f"\nError: {stats['error']}")

        except Exception as e:
            lines.append(f"DB check failed: {str(e)}")

        try:
            from bot.utils.storage import get_storage_db_size
            size_bytes = get_storage_db_size()
            if size_bytes > 0:
                lines.append(f"\nSQLite Storage File: {size_bytes / 1024:.1f} KB")
        except Exception:
            pass

        # MEDIUM-23: surface the crash-backup (fallback JSON) state - a
        # missing SESSION_SECRET silently disabled it before.
        try:
            from bot.data.state import is_fallback_snapshot_enabled
            if is_fallback_snapshot_enabled():
                lines.append("Crash Backup (fallback JSON): ✅ Enabled")
            else:
                lines.append(
                    "Crash Backup (fallback JSON): ❌ DISABLED - set SESSION_SECRET "
                    "in .env and restart, or FALLBACK_ALLOW_PLAINTEXT=1"
                )
        except Exception:
            pass

        lines.append(f"\nLast Check: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        await _edit_callback(query, 
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Refresh", callback_data="db_health_check")],
                [InlineKeyboardButton("Back to Monitor", callback_data="realtime_refresh")],
            ])
        )

    # ============================================================
    # 2. TIME-RANGE STATISTICS (Today/Week/Month)
    # ============================================================

    @app.on_callback_query(admin_filter & filters.regex(r"^time_range_stats$"))
    async def time_range_stats_callback(client: Client, query: CallbackQuery):
        await query.answer()

        buttons = [
            [InlineKeyboardButton(admin_str(query.from_user.id, "Today"), callback_data="trs_today"),
             InlineKeyboardButton(admin_str(query.from_user.id, "Last 7 Days"), callback_data="trs_7days")],
            [InlineKeyboardButton(admin_str(query.from_user.id, "Last 30 Days"), callback_data="trs_30days"),
             InlineKeyboardButton(admin_str(query.from_user.id, "Last 90 Days"), callback_data="trs_90days")],
            [InlineKeyboardButton(admin_str(query.from_user.id, "Back to Monitor"), callback_data="realtime_refresh")],
        ]

        await _edit_callback(query,
            "=== TIME-RANGE STATISTICS ===\n\n"
            "Select a time range:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )


    @app.on_callback_query(admin_filter & filters.regex(r"^trs_(today|7days|30days|90days)$"))
    async def trs_range_callback(client: Client, query: CallbackQuery):
        import re
        match = re.match(r"^trs_(today|7days|30days|90days)$", str(query.data))
        range_key = match.group(1) if match else "today"

        range_map = {"today": 1, "7days": 7, "30days": 30, "90days": 90}
        range_names = {"today": "Today", "7days": "Last 7 Days", "30days": "Last 30 Days", "90days": "Last 90 Days"}
        days = range_map.get(range_key, 1)
        range_name = admin_str(query.from_user.id, range_names.get(range_key, "Unknown"))

        await query.answer(admin_str(query.from_user.id, f"Loading {range_name}..."))

        await _show_time_range_stats(query, days, range_name)


    async def _show_time_range_stats(query: CallbackQuery, days: int, range_name: str):
        lines = [f"=== STATISTICS: {range_name} ===\n"]

        try:
            from bot.utils.database import get_time_range_stats, get_hourly_transaction_stats
            stats = await get_time_range_stats(days)

            if stats:
                total_sessions = sum(s.get("total_sessions", 0) or 0 for s in stats)
                total_success = sum(s.get("successful_sessions", 0) or 0 for s in stats)
                total_failed = sum(s.get("failed_sessions", 0) or 0 for s in stats)
                total_earnings = sum(float(s.get("total_earnings", 0) or 0) for s in stats)
                success_rate = (total_success / total_sessions * 100) if total_sessions > 0 else 0

                lines.append(f"Total Sessions: {total_sessions:,}")
                lines.append(f"Successful: {total_success:,} ({success_rate:.1f}%)")
                lines.append(f"Failed: {total_failed:,}")
                lines.append(f"Total Earnings: ${total_earnings:,.2f}\n")

                lines.append("--- DAILY BREAKDOWN ---")
                for s in stats[:min(len(stats), 30)]:
                    date_str = str(s.get("date", "N/A"))
                    if isinstance(date_str, str) and len(date_str) > 10:
                        date_str = date_str[:10]
                    lines.append(
                        f"  {date_str}: {s.get('total_sessions', 0)} sessions "
                        f"(${float(s.get('total_earnings', 0) or 0):.2f})"
                    )
            else:
                try:
                    hstats = await get_hourly_transaction_stats(days * 24)
                    if hstats:
                        total_txns = sum(h.get("total_transactions", 0) for h in hstats)
                        total_amt = sum(float(h.get("total_amount", 0) or 0) for h in hstats)
                        lines.append(f"Total Transactions: {total_txns:,}")
                        lines.append(f"Total Amount: ${total_amt:,.2f}\n")
                        lines.append("--- HOURLY BREAKDOWN ---")
                        for h in hstats[:24]:
                            hour = str(h.get("hour", "N/A"))
                            if isinstance(hour, str) and len(hour) > 16:
                                hour = hour[11:16]
                            lines.append(
                                f"  {hour}: {h.get('total_transactions', 0)} txns "
                                f"(${float(h.get('total_amount', 0) or 0):.2f})"
                            )
                    else:
                        lines.append("No analytics data available yet.")
                        lines.append("Sessions will appear here once verified.")
                except Exception:
                    lines.append("No analytics data available yet.")

        except Exception as e:
            lines.append(f"Error loading stats: {str(e)}")

        tracker = _storage.get("session_tracker", {})
        if tracker:
            # CRITICAL FIX: the "Real-time Tracker" line must count ONLY
            # sessions verified TODAY, not the all-time totals. The old code
            # summed every entry in the tracker (which grows without bound),
            # so the admin saw a misleading inflated "today" number.
            today = datetime.now().strftime("%Y-%m-%d")
            today_count = 0
            countries_set = set()
            for sessions in tracker.values():
                for s in sessions:
                    if not isinstance(s, dict):
                        continue
                    try:
                        if str(s.get("verified_at", ""))[:10] != today:
                            continue
                    except Exception:
                        continue
                    today_count += 1
                    cc = s.get("country_code")
                    if cc:
                        countries_set.add(cc)
            if today_count > 0:
                lines.append(f"\nReal-time Tracker (today): {today_count} sessions across {len(countries_set)} countries")
            else:
                lines.append("\nReal-time Tracker: no sessions verified today.")

        lines.append(f"\nLast Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        await _edit_callback(query, 
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back to Time Range", callback_data="time_range_stats")],
            ])
        )

    # ============================================================
    # 3. ENHANCED USER SEARCH (Name/Phone/Country)
    # ============================================================

