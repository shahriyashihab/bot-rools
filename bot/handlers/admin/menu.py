# bot/handlers/admin/menu.py - inline keyboard builders (pure helpers)

import os
import json
import sqlite3
import logging

from pyrogram.client import Client
from pyrogram import filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from bot.utils.storage import (
    get_total_users,
    get_all_users_list,
    is_spam_check_enabled,
    get_session_export_counts,
    get_min_withdraw_amounts,
    get_withdraw_logs,
    is_all_countries_paused,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
#  Unified panel design system (see bot/handlers/admin/design.py)
#  Every button uses one style: emoji + Action Title.
#  Back buttons are always "◀ Back" / "🔙 Main Menu".
#  NOTE: callback_data values are NEVER renamed (handlers depend on them).
# ------------------------------------------------------------------

MAIN_MENU = "🔙 Main Menu"
BACK_MAIN = "back_admin"


def _btn(text, data):
    """Uniform single button row."""
    return [InlineKeyboardButton(text, callback_data=data)]


def _back_btn(text=MAIN_MENU, data=BACK_MAIN):
    """Uniform back button row."""
    return _btn(text, data)


def get_admin_panel_buttons():
    """Main dashboard buttons - 2 per row, uniform professional pairs."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 User Management", callback_data="menu_user"),
         InlineKeyboardButton("🌍 Country & Pricing", callback_data="menu_country")],
        [InlineKeyboardButton("💰 Finance & Withdrawals", callback_data="menu_withdraw"),
         InlineKeyboardButton("📦 Session Management", callback_data="menu_session")],
        [InlineKeyboardButton("⚙️ System Controls", callback_data="menu_system"),
         InlineKeyboardButton("📣 Messaging & Channel", callback_data="menu_messaging")],
        [InlineKeyboardButton("🛠️ Advanced Tools", callback_data="menu_advanced_tools"),
         InlineKeyboardButton("🔧 Settings & Config", callback_data="menu_settings_config")],
        [InlineKeyboardButton("💳 Card Name", callback_data="card_menu"),
         InlineKeyboardButton("📡 Live Monitor", callback_data="open_core_features")],
    ])


def get_user_management_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Ban Management", callback_data="ban_management"),
         InlineKeyboardButton("➕ Add Balance", callback_data="adjustbalance")],
        [InlineKeyboardButton("➖ Deduct Balance", callback_data="removebalance"),
         InlineKeyboardButton("📊 User Stats", callback_data="userstats")],
        [InlineKeyboardButton("📜 Activity Log", callback_data="useractivitylogs"),
         InlineKeyboardButton("🔢 Total Users", callback_data="totalusers")],
        [InlineKeyboardButton("♻️ Zero All Balances", callback_data="zerobalance")],
        _back_btn(),
    ])


def get_ban_management_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Ban User", callback_data="ban"),
         InlineKeyboardButton("✅ Unban User", callback_data="unban")],
        [InlineKeyboardButton("📋 Ban List", callback_data="ban_list")],
        [InlineKeyboardButton("◀ Back to User Management", callback_data="menu_user")],
    ])


def get_country_pricing_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💱 Set Country Price", callback_data="setrate"),
         InlineKeyboardButton("👤 User Rates", callback_data="set_user_rate_menu")],
        [InlineKeyboardButton("⏱️ Session Timeout", callback_data="settimeout"),
         InlineKeyboardButton("🔀 Toggle Country", callback_data="togglecountry")],
        [InlineKeyboardButton("🧩 CORE CONFIG", callback_data="core_config")],
        [InlineKeyboardButton("🤖 Auto-Approve", callback_data="auto_approve_settings"),
         InlineKeyboardButton("⛔ Disable All", callback_data="disablecountries")],
        [InlineKeyboardButton("➕ Bulk Add Countries", callback_data="bulk_add_countries"),
         InlineKeyboardButton("🗑️ Delete Open Countries", callback_data="delete_all_open_confirm")],
        [InlineKeyboardButton("🎛️ Capacity UI", callback_data="capacity_ui_menu")],
        _back_btn(),
    ])


def get_country_pricing_status_line() -> str:
    """Status line for the Country & Pricing header (open-country count).

    The old per-screen duplicate of the All Countries ON/OFF switch was
    removed from this menu (the master switch stays in System Controls);
    the header now shows the open-country count instead.
    """
    from bot.utils.storage import get_open_countries
    return f"🟢 Open: {len(get_open_countries())} countries"


def get_user_rate_menu_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add User Rate", callback_data="add_custom_user_rate"),
         InlineKeyboardButton("➖ Remove User Rate", callback_data="remove_custom_user_rate")],
        [InlineKeyboardButton("👁️ See All User Rates", callback_data="see_all_custom_rates")],
        [InlineKeyboardButton("◀ Back to Country & Pricing", callback_data="menu_country")],
    ])


def get_withdraw_finance_buttons():
    from bot.data.withdrawals import is_withdraw_enabled
    from bot.handlers.admin.design import sw
    wd_label = sw("Withdraw", is_withdraw_enabled())
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(wd_label, callback_data="togglewithdraw"),
         InlineKeyboardButton("👤 Per-User Control", callback_data="user_withdraw_control")],
        [InlineKeyboardButton("💵 Min Withdraw", callback_data="minwithdraw"),
         InlineKeyboardButton("📑 All Balances", callback_data="allbalances")],
        [InlineKeyboardButton("🧾 Withdraw Logs", callback_data="withdrawlogs")],
        [InlineKeyboardButton("📈 Revenue Dashboard", callback_data="revenue_dashboard")],
        _back_btn(),
    ])


def get_system_controls_buttons():
    from bot.handlers.admin.router import get_monitor_status
    from bot.handlers.admin.design import sw
    from bot.utils.storage import (
        is_two_factor_enabled,
        is_device_check_enabled,
        is_same_number_enabled,
    )
    monitor_label = sw("Monitor", get_monitor_status())
    spam_label = sw("Spam Check", is_spam_check_enabled())
    countries_label = sw("All Countries", not is_all_countries_paused())
    twofa_label = sw("2FA", is_two_factor_enabled())
    device_label = sw("Device Check", is_device_check_enabled())
    same_label = sw("Same Number", is_same_number_enabled())

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(monitor_label, callback_data="togglemonitor"),
         InlineKeyboardButton(countries_label, callback_data="togglecountries")],
        [InlineKeyboardButton(spam_label, callback_data="togglespamcheck"),
         InlineKeyboardButton(twofa_label, callback_data="two_factor_toggle")],
        [InlineKeyboardButton(device_label, callback_data="device_check_toggle"),
         InlineKeyboardButton(same_label, callback_data="same_number_toggle")],
        [InlineKeyboardButton("🔔 Alert System Config", callback_data="alert_system_config")],
        _back_btn(),
    ])


def get_messaging_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Broadcast Message", callback_data="broadcast"),
         InlineKeyboardButton("✉️ Send to User", callback_data="sendtouser")],
        [InlineKeyboardButton("📌 Set Required Channel", callback_data="setchannel"),
         InlineKeyboardButton("📺 Channel Management", callback_data="menu_channels")],
        [InlineKeyboardButton("📝 Message Templates", callback_data="messagesettings")],
        _back_btn(),
    ])


def get_advanced_tools_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳ Pending Sessions", callback_data="view_pending_sessions"),
         InlineKeyboardButton("🧹 Cleanup Old Sessions", callback_data="cleanup_old_sessions")],
        [InlineKeyboardButton("💾 Backup / Export Data", callback_data="backup_export_data"),
         InlineKeyboardButton("📥 Download DB File", callback_data="download_db_file")],
        [InlineKeyboardButton("🌐 Country Based Export", callback_data="country_export_menu")],
        [InlineKeyboardButton("🔌 API Management", callback_data="api_mgmt_menu"),
         InlineKeyboardButton("🌐 Proxy System", callback_data="px_menu")],
        _back_btn(),
    ])


def get_api_management_buttons():
    """API Management sub-menu - Add / Remove / List Check inside ONE menu."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add API", callback_data="api_add"),
         InlineKeyboardButton("➖ Remove API", callback_data="api_remove")],
        [InlineKeyboardButton("📋 API List & Check", callback_data="api_list")],
        [InlineKeyboardButton("◀ Back to Advanced Tools", callback_data="menu_advanced_tools")],
    ])


def get_settings_config_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 Set 2FA Password", callback_data="set_2fa_password"),
         InlineKeyboardButton("👁️ View 2FA Password", callback_data="show_2fa_secret")],
        [InlineKeyboardButton("📊 Daily Report Toggle", callback_data="daily_report_toggle"),
         InlineKeyboardButton("👥 Multi-Admin", callback_data="multiadmin_mgmt")],
        [InlineKeyboardButton("🗺️ Country-wise Stats", callback_data="country_wise_stats"),
         InlineKeyboardButton("🕐 Account Timezone", callback_data="account_timezone")],
        [InlineKeyboardButton("🌐 Admin Panel Language", callback_data="admin_lang_select")],
        _back_btn(),
    ])


def get_session_management_buttons():
    counts = get_session_export_counts()
    active_count = counts.get("active", 0)
    pending_count = counts.get("pending", 0)

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Export Active ({active_count})", callback_data="export_active_sessions"),
         InlineKeyboardButton(f"⏳ Export Pending ({pending_count})", callback_data="export_pending_sessions")],
        [InlineKeyboardButton(f"📦 Export All ({active_count + pending_count})", callback_data="export_all_sessions"),
         InlineKeyboardButton("⏳ Pending Session Mode", callback_data="session_mode_pending")],
        [InlineKeyboardButton("📱 Multidevice Session Mode", callback_data="session_mode_multidevice"),
         InlineKeyboardButton("✅ Success Session Mode", callback_data="session_mode_success")],
        [InlineKeyboardButton("🧹 Cleanup Old Sessions", callback_data="cleanup_old_sessions"),
         InlineKeyboardButton("📤 Send Sessions", callback_data="send_sessions_menu")],
        [InlineKeyboardButton("⏱️ Expiry Settings", callback_data="session_expiry_settings"),
         InlineKeyboardButton("🔄 Refresh Counts", callback_data="menu_session")],
        _back_btn(),
    ])


# ------------------------------------------------------------------
#  Session export sub-menus (level 2: pick a format, level 3: All/Manual)
#
#  Main buttons  (export_active/pending/all_sessions)  -> format panel
#  Format panel  (sesx_fmt_<pool>_<format>)            -> action panel
#  Action panel  (sesx_all_... / sesx_manual_...)      -> run export
# ------------------------------------------------------------------

SESSION_EXPORT_FORMATS = [
    ("telethon", "📘 Telethon Export"),
    ("pyrogram", "📗 Program (Pyrogram) Export"),
    ("tdata", "📙 TData Export"),
    ("txt", "📕 TXT Export"),
]

SESSION_EXPORT_FORMAT_LABELS = dict(SESSION_EXPORT_FORMATS)

SESSION_POOL_TITLES = {
    "active": "✅ Export Active",
    "pending": "⏳ Export Pending",
    "all": "📦 Export All",
}

SESSION_POOL_CRUMBS = {
    "active": "Main › Sessions › Export › Active",
    "pending": "Main › Sessions › Export › Pending",
    "all": "Main › Sessions › Export › All",
}


def _session_pool_count(pool: str) -> int:
    """Number of sessions available in an export pool (active/pending/all)."""
    counts = get_session_export_counts()
    if pool == "active":
        return int(counts.get("active", 0))
    if pool == "pending":
        return int(counts.get("pending", 0))
    return int(counts.get("active", 0)) + int(counts.get("pending", 0))


def get_session_export_formats_panel(pool: str):
    """Level 2: choose one export format for a session pool.

    Returns ``(text, InlineKeyboardMarkup)``. ``pool`` is one of
    ``active`` / ``pending`` / ``all``.
    """
    from bot.handlers.admin.design import hd

    count = _session_pool_count(pool)
    title = f"{SESSION_POOL_TITLES[pool]} ({count})"
    crumb = SESSION_POOL_CRUMBS[pool]
    lines = [
        hd(title, crumb),
        "",
        f"Available: {count} sessions",
    ]
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"sesx_fmt_{pool}_{key}")]
        for key, label in SESSION_EXPORT_FORMATS
    ]
    buttons.append([InlineKeyboardButton("🔙 Back to Session Management", callback_data="menu_session")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def get_session_format_action_panel(pool: str, fmt: str):
    """Level 3: All export vs. manually export for one (pool, format).

    Returns ``(text, InlineKeyboardMarkup)``.
    """
    from bot.handlers.admin.design import hd

    count = _session_pool_count(pool)
    fmt_label = SESSION_EXPORT_FORMAT_LABELS.get(fmt, fmt)
    title = f"{SESSION_POOL_TITLES[pool]} ({count})"
    crumb = SESSION_POOL_CRUMBS[pool]
    lines = [
        hd(title, crumb),
        "",
        f"Format: {fmt_label}",
    ]
    buttons = [
        [InlineKeyboardButton("⚡ All Export", callback_data=f"sesx_all_{pool}_{fmt}"),
         InlineKeyboardButton("✍️ Manually Export", callback_data=f"sesx_manual_{pool}_{fmt}")],
        [InlineKeyboardButton("🔙 Back to Formats", callback_data=f"sesx_pool_{pool}")],
        [InlineKeyboardButton("🔙 Back to Session Management", callback_data="menu_session")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _create_db_snapshot(src_path: str, dst_path: str) -> bool:
    """Create a consistent snapshot of the SQLite database file.

    Uses the SQLite Online Backup API (Connection.backup) so the copy is
    safe to take while the bot is running and ALWAYS includes data still
    sitting in the WAL file (-wal), i.e. a 100% up-to-date backup.
    """
    try:
        src = sqlite3.connect(src_path)
        try:
            dst = sqlite3.connect(dst_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return True
    except Exception as e:
        logger.error(f"DB snapshot error: {e}")
        return False


def get_realtime_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="realtime_refresh"),
         InlineKeyboardButton("🩺 DB Health", callback_data="db_health_check")],
        [InlineKeyboardButton("📅 Time-Range Stats", callback_data="time_range_stats"),
         InlineKeyboardButton("🔍 Search User", callback_data="enhanced_search")],
        [InlineKeyboardButton("📄 Session Detail", callback_data="session_detail_view"),
         InlineKeyboardButton("🧾 Audit Log", callback_data="enhanced_audit_log")],
        [InlineKeyboardButton("🔌 API Rotation", callback_data="api_rotation_monitor"),
         InlineKeyboardButton("🎯 Queue Priority", callback_data="queue_priority_view")],
        [InlineKeyboardButton("⚙️ Moderation Rules", callback_data="auto_moderation_rules"),
         InlineKeyboardButton("🌍 Bulk Country Config", callback_data="bulk_country_config")],
        _back_btn(),
    ])
