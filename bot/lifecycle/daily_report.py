"""Daily automated report scheduler.

Sends a compact summary of bot stats to every admin every day at 09:00 local
time when the admin has enabled the daily report via the Settings →
``daily_report_toggle`` button.

The toggle only flips the ``daily_report_enabled`` flag in storage; this loop
is what actually consumes it. Without it the button was a no-op.
"""
import asyncio
import logging
from datetime import datetime

from bot.core.context import shutdown_event

logger = logging.getLogger(__name__)

# How often the loop wakes up to check whether it is time to send the report.
CHECK_INTERVAL_SECONDS = 60

# Hour (24h) at which the daily report is sent.
REPORT_HOUR = 9


def _get_storage():
    from bot.utils.storage import _storage, save_storage
    return _storage, save_storage


def _report_enabled() -> bool:
    storage, _ = _get_storage()
    return bool(storage.get("daily_report_enabled", False))


def _last_sent_date() -> str:
    storage, _ = _get_storage()
    return str(storage.get("daily_report_last_sent", "") or "")


def _mark_sent(today: str) -> None:
    storage, save_storage = _get_storage()
    storage["daily_report_last_sent"] = today
    from bot.data.state import _mark_settings_key_dirty
    _mark_settings_key_dirty("daily_report_last_sent")
    save_storage()


def _daily_report_due(now: datetime) -> bool:
    """True when the daily report should be sent at ``now``.

    Enabled + past the configured hour + not already sent today.
    """
    today = now.strftime("%Y-%m-%d")
    return _report_enabled() and now.hour >= REPORT_HOUR and _last_sent_date() != today


def _build_daily_report() -> str:
    """Build the plain-text daily summary from live storage data."""
    from bot.utils.storage import (
        get_total_users,
        get_all_pending_sessions,
        get_pending_withdraw_requests,
        get_all_balances_report,
    )

    try:
        user_count, active_count = get_total_users()
    except Exception:
        user_count, active_count = 0, 0

    try:
        pending_sessions = len(get_all_pending_sessions())
    except Exception:
        pending_sessions = 0

    try:
        pending_withdraws = len(get_pending_withdraw_requests())
    except Exception:
        pending_withdraws = 0

    try:
        _, _, _, total_balance = get_all_balances_report()
    except Exception:
        total_balance = 0.0

    storage, _ = _get_storage()
    today = datetime.now().strftime("%Y-%m-%d")

    new_users_today = 0
    try:
        for data in storage.get("users", {}).values():
            joined = str(data.get("joined", ""))[:10]
            if joined == today:
                new_users_today += 1
    except Exception:
        pass

    today_sessions = 0
    today_verified = 0
    try:
        for slist in storage.get("session_tracker", {}).values():
            for s in slist:
                vat = str(s.get("verified_at", ""))[:10]
                if vat == today:
                    today_sessions += 1
                    if s.get("spam_status") == "spam_free":
                        today_verified += 1
    except Exception:
        pass

    daily_attempts = 0
    try:
        for entry in storage.get("daily_counters", {}).values():
            if entry.get("date") == today:
                try:
                    daily_attempts += int(entry.get("count", 0))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass

    countries = storage.get("countries", {})
    countries_open = sum(1 for d in countries.values() if d.get("open", False))

    withdraw_disabled = bool(storage.get("withdraw_disabled", False))
    from bot.handlers.admin.router import get_monitor_status
    monitor_on = get_monitor_status()

    lines = [
        "=== DAILY REPORT ===",
        f"Date: {today}",
        "----------------------",
        f"Total Users: {user_count:,}",
        f"Active Users: {active_count:,}",
        f"New Users Today: {new_users_today}",
        f"Sessions Today: {today_sessions}",
        f"Verification Attempts Today: {daily_attempts}",
        f"Pending Sessions: {pending_sessions}",
        f"Pending Withdrawals: {pending_withdraws}",
        f"Total Balance: ${total_balance:,.2f}",
        f"Countries: {countries_open}/{len(countries)} open",
        f"Monitor: {'ON' if monitor_on else 'OFF'}",
        f"Withdraw: {'OPEN' if not withdraw_disabled else 'CLOSED'}",
        "----------------------",
        "Generated automatically.",
    ]
    return "\n".join(lines)


async def _send_daily_report() -> None:
    """Send the daily report to every admin. Never raises."""
    from bot.bootstrap.client import app
    from bot.handlers.admin.router import get_all_admins

    report = _build_daily_report()
    sent = 0
    for admin_id in get_all_admins():
        try:
            await app.send_message(chat_id=admin_id, text=report)
            sent += 1
        except Exception as e:
            logger.warning(f"Daily report send failed for admin {admin_id}: {e}")
    if sent:
        print(f"📊 DAILY REPORT: Sent to {sent} admin(s)")
    else:
        logger.warning("Daily report enabled but could not be sent to any admin")


async def daily_report_loop():
    """Background loop: send the daily report once per day at 09:00."""
    while not shutdown_event.is_set():
        try:
            now = datetime.now()
            if _daily_report_due(now):
                await _send_daily_report()
                _mark_sent(now.strftime("%Y-%m-%d"))
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Daily report loop error: {e}")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
