# bot/handlers/admin/session_modes.py
"""Admin panel: Session Management -> session reporting modes.

Three sub-menus control which session formats are reported and where:

- Pending Session Mode    (max ONE format, report -> CHANNEL_SUBMIT)
- Multidevice Session Mode (max ONE format, report -> CHANNEL_24HR_UP)
- Success Session Mode     (min ONE, max 4 formats; each format -> its own
                            channel: telethon->CHANNEL_VERIFIED,
                            pyrogram->CHANNEL_SUCCESS_PYROGRAM,
                            tdata->CHANNEL_SUCCESS_TDATA, txt->CHANNEL_SUCCESS_TXT)

Pending & Multidevice use radio-style buttons (select one, tap again to
deselect / "None"). Success uses checkbox-style buttons with a min-1 rule
(the last enabled format cannot be turned off).
"""
import logging

from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from bot.config import ADMIN_ID
from bot.data.settings import (
    SESSION_FORMATS,
    get_session_pending_type,
    set_session_pending_type,
    get_session_multidevice_type,
    set_session_multidevice_type,
    get_session_success_types,
    toggle_session_success_type,
)

logger = logging.getLogger(__name__)

FORMAT_LABELS = {
    "telethon": "📘 Telethon",
    "pyrogram": "📗 Pyrogram",
    "tdata": "📙 TData",
    "txt": "📕 TXT File",
}

MODE_TITLES = {
    "pending": ("⏳ Pending Session Mode", "Main › Sessions › Pending"),
    "multidevice": ("📱 Multidevice Session Mode", "Main › Sessions › Multidevice"),
    "success": ("✅ Success Session Mode", "Main › Sessions › Success"),
}

MODE_DESCRIPTIONS = {
    "pending": "Format for PENDING reports (max 1).",
    "multidevice": "Format for MULTIDEVICE reports (max 1).",
    "success": "Formats for SUCCESS reports (min 1, max 4).",
}


def _channel_label(mode: str) -> str:
    from bot import config
    # Success mode routes EACH enabled format to its OWN channel, so showing
    # only CHANNEL_VERIFIED misled the admin into thinking every success file
    # lands in one channel. Show all four per-format channels instead.
    if mode == "success":
        channel_map = {
            "telethon": "CHANNEL_VERIFIED",
            "pyrogram": "CHANNEL_SUCCESS_PYROGRAM",
            "tdata": "CHANNEL_SUCCESS_TDATA",
            "txt": "CHANNEL_SUCCESS_TXT",
        }
        lines = []
        for fmt, key in channel_map.items():
            ch = config.get_channel(key)
            label = FORMAT_LABELS.get(fmt, fmt)
            if ch:
                lines.append(f"🟢 {label} → {ch}")
            else:
                lines.append(f"🔴 {label} → not configured")
        return "\n".join(lines) if lines else "🔴 No success channels configured"

    channel_key = {
        "pending": "CHANNEL_SUBMIT",
        "multidevice": "CHANNEL_24HR_UP",
    }.get(mode)
    ch = config.get_channel(channel_key) if channel_key else None
    if ch:
        return f"🟢 Channel: {ch}"
    return "🔴 Channel not configured"


def build_session_mode_panel(mode: str):
    """Build (text, buttons) for one of the three session mode panels."""
    title, crumb = MODE_TITLES.get(mode, (mode, "Main › Sessions"))
    description = MODE_DESCRIPTIONS.get(mode, "")
    from bot.handlers.admin.design import hd

    if mode == "pending":
        selected = get_session_pending_type()
    elif mode == "multidevice":
        selected = get_session_multidevice_type()
    else:
        selected = get_session_success_types()

    lines = [hd(title, crumb), "", description, "", _channel_label(mode), ""]

    # Current selection summary
    if mode in ("pending", "multidevice"):
        if selected:
            lines.append(f"Current: {FORMAT_LABELS.get(selected, selected)}")
        else:
            lines.append("Current: (none selected)")
    else:
        lines.append("Enabled: " + (", ".join(FORMAT_LABELS.get(f, f) for f in selected) or "none"))
    lines.append("")

    buttons = []
    for fmt in SESSION_FORMATS:
        label = FORMAT_LABELS[fmt]
        if mode in ("pending", "multidevice"):
            on = (selected == fmt)
            mark = "✅" if on else "🔘"
            label = f"{mark} {label}"
            cb = f"sm_set_{mode}_{fmt}"
        else:
            on = fmt in selected
            mark = "✅" if on else "⬜"
            label = f"{mark} {label}"
            cb = f"sm_toggle_success_{fmt}"
        buttons.append([InlineKeyboardButton(label, callback_data=cb)])

    if mode in ("pending", "multidevice"):
        buttons.append([InlineKeyboardButton("❌ None (disable)", callback_data=f"sm_clear_{mode}")])
    buttons.append([InlineKeyboardButton("🔙 Back to Session Management", callback_data="menu_session")])

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _edit_panel(query: CallbackQuery, mode: str):
    text, markup = build_session_mode_panel(mode)
    try:
        from bot.handlers.admin.router import _edit_callback
        return _edit_callback(query, text, reply_markup=markup)
    except Exception:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(
                query.message.edit_text(text, reply_markup=markup))
        except Exception as e:
            logger.error("session mode panel edit error: %s", e)


def register_session_modes(app):
    from bot.handlers.admin.router import admin_filter, _log_admin_action

    @app.on_callback_query(admin_filter & filters.regex(r"^session_mode_pending$"))
    async def session_mode_pending_callback(client, query: CallbackQuery):
        await query.answer()
        await _edit_panel(query, "pending")

    @app.on_callback_query(admin_filter & filters.regex(r"^session_mode_multidevice$"))
    async def session_mode_multidevice_callback(client, query: CallbackQuery):
        await query.answer()
        await _edit_panel(query, "multidevice")

    @app.on_callback_query(admin_filter & filters.regex(r"^session_mode_success$"))
    async def session_mode_success_callback(client, query: CallbackQuery):
        await query.answer()
        await _edit_panel(query, "success")

    @app.on_callback_query(admin_filter & filters.regex(r"^sm_set_pending_(\w+)$"))
    async def sm_set_pending_callback(client, query: CallbackQuery):
        await query.answer()
        if not query.matches:
            return
        fmt = query.matches[0].group(1)
        if fmt not in SESSION_FORMATS:
            return
        new_val = set_session_pending_type(fmt)
        await _log_admin_action(query.from_user.id, "session_pending_mode_set", message=f"format={new_val}")
        await _edit_panel(query, "pending")

    @app.on_callback_query(admin_filter & filters.regex(r"^sm_clear_pending$"))
    async def sm_clear_pending_callback(client, query: CallbackQuery):
        await query.answer()
        set_session_pending_type("")
        await _log_admin_action(query.from_user.id, "session_pending_mode_clear", message="format=")
        await _edit_panel(query, "pending")

    @app.on_callback_query(admin_filter & filters.regex(r"^sm_set_multidevice_(\w+)$"))
    async def sm_set_multidevice_callback(client, query: CallbackQuery):
        await query.answer()
        if not query.matches:
            return
        fmt = query.matches[0].group(1)
        if fmt not in SESSION_FORMATS:
            return
        new_val = set_session_multidevice_type(fmt)
        await _log_admin_action(query.from_user.id, "session_multidevice_mode_set", message=f"format={new_val}")
        await _edit_panel(query, "multidevice")

    @app.on_callback_query(admin_filter & filters.regex(r"^sm_clear_multidevice$"))
    async def sm_clear_multidevice_callback(client, query: CallbackQuery):
        await query.answer()
        set_session_multidevice_type("")
        await _log_admin_action(query.from_user.id, "session_multidevice_mode_clear", message="format=")
        await _edit_panel(query, "multidevice")

    @app.on_callback_query(admin_filter & filters.regex(r"^sm_toggle_success_(\w+)$"))
    async def sm_toggle_success_callback(client, query: CallbackQuery):
        await query.answer()
        if not query.matches:
            return
        fmt = query.matches[0].group(1)
        if fmt not in SESSION_FORMATS:
            return
        new_types = toggle_session_success_type(fmt)
        await _log_admin_action(query.from_user.id, "session_success_mode_toggle", message=f"format={fmt} enabled={new_types}")
        await _edit_panel(query, "success")
