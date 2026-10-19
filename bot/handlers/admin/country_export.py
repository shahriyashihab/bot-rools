# bot/handlers/admin/country_export.py - country based session export

import os
import re
import json
import shutil
import logging
import asyncio
import tempfile
from datetime import datetime
from typing import Dict, Any, List, Set, Tuple, Optional

from pyrogram.client import Client
from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message

from bot.utils.storage import (
    get_all_pending_sessions,
    _storage,
)
from bot.utils.country_data import get_flag, get_country_name
from bot.utils.country_codes import get_country_display_code
from bot.handlers.admin.router import (
    admin_filter,
    admin_state,
    _admin_manager,
    _log_admin_action,
    _edit_callback,
    _edit_by_ids,
    _panel_reply,
    _set_admin_state,
    ADMIN_TEXT_FILTER,
)

logger = logging.getLogger(__name__)
from bot.utils.i18n import admin_str

# Ordered export categories shown on the country detail page.
# key -> (label, emoji)
COUNTRY_EXPORT_CATEGORIES = [
    ("total", "All Numbers", "📊"),
    ("success", "Success", "✅"),
    ("spam_free", "Spam Free", "🟢"),
    ("spam", "Spam", "⚠️"),
    ("limit", "Limit", "🔒"),
    ("multi_device", "Multi Device", "📱"),
    ("pending", "Pending Session", "⏳"),
    ("rejected", "Rejected", "❌"),
]

# Categories that can be exported individually (each gets its own ZIP).
EXPORTABLE_CATEGORIES = [c[0] for c in COUNTRY_EXPORT_CATEGORIES if c[0] != "total"]

# Export formats offered on the country category pages (level 3) and their
# All/Manual action panels (level 4). Exactly the same set as Session
# Management so both areas behave identically.
EXPORT_FORMAT_KEYS = ["telethon", "pyrogram", "tdata", "txt"]
EXPORT_FORMAT_LABELS = {
    "telethon": "📘 Telethon Export",
    "pyrogram": "📗 Program (Pyrogram) Export",
    "tdata": "📙 TData Export",
    "txt": "📕 TXT Export",
}


def _category_label(category: str) -> str:
    """Human label for an export category key."""
    for key, label, _emoji in COUNTRY_EXPORT_CATEGORIES:
        if key == category:
            return label
    return category


def _get_session_country(session: Dict[str, Any]) -> str:
    """Best-effort country code for a pending session."""
    cc = session.get("country_code") or ""
    if cc:
        return cc
    sd = session.get("session_data") or {}
    cc = sd.get("country_code") or ""
    if cc:
        return cc
    phone = session.get("phone") or sd.get("phone") or ""
    if phone:
        try:
            from bot.utils.country_data import get_country_code_from_phone
            return get_country_code_from_phone(str(phone).replace("*", "").replace("-", "").replace(" ", "")) or ""
        except Exception:
            return ""
    return ""


def _get_session_spam_status(session: Dict[str, Any]) -> str:
    """Spam status from the top-level field or session_data."""
    top = session.get("spam_status") or ""
    if top and top in ("spam_free", "spam", "limit"):
        return top
    sd = session.get("session_data") or {}
    val = sd.get("spam_status") or ""
    if val in ("spam_free", "spam", "limit"):
        return val
    return ""


def _get_session_message_type(session: Dict[str, Any]) -> str:
    sd = session.get("session_data") or {}
    return str(sd.get("message_type") or "").upper()


def session_categories(session: Dict[str, Any]) -> Set[str]:
    """Return the set of export categories a single session belongs to.

    A session can belong to several categories at once (e.g. a spam account
    that was rejected, or a spam account that was paid -> spam + success).
    """
    cats: Set[str] = set()
    status = session.get("status", "pending")
    spam_status = _get_session_spam_status(session)
    has_extra = bool((session.get("session_data") or {}).get("has_extra_devices", False))
    msg_type = _get_session_message_type(session)
    balance_credited = bool(session.get("balance_credited", False))

    if spam_status == "spam":
        cats.add("spam")
    elif spam_status == "spam_free":
        cats.add("spam_free")
    elif spam_status == "limit":
        cats.add("limit")

    if has_extra or msg_type in ("MULTI-DEVICE", "24HR"):
        cats.add("multi_device")

    if balance_credited or status == "approved":
        cats.add("success")

    if status in ("rejected", "expired", "timeout"):
        cats.add("rejected")

    if status == "pending" and not balance_credited:
        cats.add("pending")

    cats.add("total")
    return cats


def build_country_export_stats() -> Dict[str, Dict[str, int]]:
    """Compute per-country counts for every export category.

    Reads the live pending_sessions storage and returns a dict of
    ``country_code -> {category: count, "total": count}``. Only countries that
    actually have sessions appear here.
    """
    stats: Dict[str, Dict[str, int]] = {}
    pending = get_all_pending_sessions()

    for session_id, session in pending.items():
        if not isinstance(session, dict):
            continue
        cc = _get_session_country(session)
        if not cc:
            cc = "other"
        bucket = stats.setdefault(cc, {})
        for cat in session_categories(session):
            bucket[cat] = bucket.get(cat, 0) + 1

    return stats


def get_country_category_sessions(country_code: str, category: str) -> List[Dict[str, Any]]:
    """Return the pending sessions of a country that belong to a category."""
    result = []
    pending = get_all_pending_sessions()
    for session_id, session in pending.items():
        if not isinstance(session, dict):
            continue
        if _get_session_country(session) != country_code:
            continue
        if category in session_categories(session):
            result.append(session)
    return result


def get_all_country_sessions(country_code: str) -> List[Dict[str, Any]]:
    """Return ALL pending sessions of a country (for manual-count export)."""
    result = []
    pending = get_all_pending_sessions()
    for session_id, session in pending.items():
        if not isinstance(session, dict):
            continue
        if _get_session_country(session) != country_code:
            continue
        result.append(session)
    return result


def _open_countries() -> List[str]:
    """Country codes that are currently open, sorted."""
    countries = _storage.get("countries", {})
    return sorted(cc for cc, data in countries.items() if isinstance(data, dict) and data.get("open", False))


# ------------------------------------------------------------------
# Country-selector grid geometry (visual reference: CORE CONFIG grid in
# bot/handlers/admin/core_config.py - flag + 4-char display code, fixed
# two-column rectangle, non-clickable padding on incomplete final pages).
# The CORE CONFIG grid itself keeps its own 2x10 layout: untouched.
# ------------------------------------------------------------------
GRID_COLUMNS = 2
GRID_ROWS = 20
PER_PAGE = GRID_COLUMNS * GRID_ROWS  # 40 countries per page

# Non-clickable placeholder that keeps an incomplete final page a clean
# rectangle (same trick as the CORE CONFIG grid).
_CE_PLACEHOLDER = "ㅤ"


def _bi(text: str) -> str:
    """Bold-italic markdown for screen text (matches CORE CONFIG style)."""
    return f"**__{text}__**"


def _build_country_list_message(page: int = 1) -> Tuple[str, InlineKeyboardMarkup]:
    """Build the open-countries list message + buttons for a given page.

    Compact header only (no country-name text list, no legend) plus a fixed
    2-column x 20-row grid: flag + stable 4-char display code + dial code.
    Callback data still carries the REAL country code (+XXX), never the
    display label, so country selection semantics are unchanged.
    """
    open_codes = _open_countries()
    if not open_codes:
        return (
            _bi("📦 COUNTRY BASED EXPORT") + "\n"
            + _bi("MAIN › ADVANCED › EXPORT") + "\n\n"
            + _bi("❌ NO OPEN COUNTRIES") + "\n"
            + "Open a country in Country & Pricing first.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ BACK TO ADVANCED TOOLS", callback_data="menu_advanced_tools")]
            ])
        )

    total_pages = max(1, (len(open_codes) + PER_PAGE - 1) // PER_PAGE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * PER_PAGE
    page_codes = open_codes[start:start + PER_PAGE]

    lines = [
        _bi("📦 COUNTRY BASED EXPORT"),
        _bi("MAIN › ADVANCED › EXPORT"),
        _bi("SELECT A COUNTRY"),
        _bi(f"PAGE {page}/{total_pages}"),
    ]

    # Pad the final page so the grid stays an aligned 2-column rectangle.
    padded = page_codes + [None] * (PER_PAGE - len(page_codes))
    rows = []
    for row in range(GRID_ROWS):
        row_buttons = []
        for col in range(GRID_COLUMNS):
            cc = padded[row * GRID_COLUMNS + col]
            if cc is None:
                row_buttons.append(InlineKeyboardButton(_CE_PLACEHOLDER, callback_data="country_export_noop"))
                continue
            flag = get_flag(cc)
            short = get_country_display_code(cc)
            # cc padded to 5 chars (+91 -> "+91  ", +880 -> "+880 ") so the
            # common dial-code widths render equal-looking labels. Longer
            # codes are never truncated - the real code must stay visible.
            row_buttons.append(
                InlineKeyboardButton(f"{flag} {short}  {cc.ljust(5)}", callback_data=f"country_export_{cc}")
            )
        rows.append(row_buttons)

    # Page indicator row (Prev / page / Next) with placeholder balance.
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"country_export_page_{page - 1}"))
    else:
        nav.append(InlineKeyboardButton(_CE_PLACEHOLDER, callback_data="country_export_noop"))
    # MEDIUM-43: the page indicator is a no-op (used to reset to page 1).
    nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="country_export_noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"country_export_page_{page + 1}"))
    else:
        nav.append(InlineKeyboardButton(_CE_PLACEHOLDER, callback_data="country_export_noop"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("◀ BACK TO ADVANCED TOOLS", callback_data="menu_advanced_tools")])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _build_country_detail_message(country_code: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Build the country detail message with per-category counts + export buttons."""
    stats = build_country_export_stats()
    country_stats = stats.get(country_code, {})

    try:
        name = get_country_name(country_code)
        flag = get_flag(country_code)
    except Exception:
        name = country_code
        flag = "🌍"

    lines = [
        f"--- COUNTRY EXPORT: {country_code} ---\n",
        f"{flag} {country_code} ({name})\n",
    ]

    if not country_stats:
        lines.append("No sessions for this country yet.")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to Countries", callback_data="country_export_menu")],
        ])
        return "\n".join(lines), keyboard

    rows = []
    for key, label, emoji in COUNTRY_EXPORT_CATEGORIES:
        count = country_stats.get(key, 0)
        lines.append(f"{emoji} {label}: **{count}**")
        # Only offer an export button when the category actually has sessions.
        # A "Export {label} (0)" button is dead UI: tapping it always shows
        # "No sessions" - hide it entirely for zero-count categories.
        if key != "total" and count > 0:
            rows.append([InlineKeyboardButton(
                f"📦 Export {label} ({count})",
                callback_data=f"country_export_cat_{country_code}_{key}"
            )])

    lines.append("--------------------------\n")
    rows.append([InlineKeyboardButton("Back to Countries", callback_data="country_export_menu")])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _build_country_format_panel(country_code: str, category: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Level 3: choose one export format for a (country, category) pair."""
    try:
        name = get_country_name(country_code)
        flag = get_flag(country_code)
    except Exception:
        name = country_code
        flag = "🌍"
    cat_label = _category_label(category)

    lines = [
        f"--- COUNTRY EXPORT: {country_code} ---\n",
        f"{flag} {country_code} ({name})\n",
        f"Category: {cat_label}\n",
    ]
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"country_export_fmt_{country_code}_{category}_{key}")]
        for key, label in EXPORT_FORMAT_LABELS.items()
    ]
    buttons.append([InlineKeyboardButton("🔙 Back to Country", callback_data=f"country_export_{country_code}")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _build_country_action_panel(country_code: str, category: str, fmt: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Level 4: All export vs. manually export for (country, category, format)."""
    try:
        flag = get_flag(country_code)
    except Exception:
        flag = "🌍"
    cat_label = _category_label(category)
    fmt_label = EXPORT_FORMAT_LABELS.get(fmt, fmt)

    lines = [
        f"--- COUNTRY EXPORT: {country_code} ---\n",
        f"{flag} {country_code}\n",
        f"Category: {cat_label}\n",
        f"Format: {fmt_label}\n",
    ]
    buttons = [
        [InlineKeyboardButton("⚡ All Export", callback_data=f"country_export_all_{country_code}_{category}_{fmt}")],
        [InlineKeyboardButton("✍️ Manually Export", callback_data=f"country_export_manual_{country_code}_{category}_{fmt}")],
        [InlineKeyboardButton("🔙 Back to Formats", callback_data=f"country_export_cat_{country_code}_{category}")],
        [InlineKeyboardButton("🔙 Back to Country", callback_data=f"country_export_{country_code}")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _sanitize_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@+-]", "_", str(value))


def _write_session_files(sessions: List[Dict[str, Any]], out_dir: str, fmt: Optional[str] = None) -> Tuple[int, List[Dict[str, Any]]]:
    """Write session format files into out_dir.

    When ``fmt`` is set (e.g. ``"telethon"``) only that single format is
    produced (format-specific export). When ``fmt`` is ``None`` ALL formats
    are generated (full export).

    Always writes a ``manifest.json`` at the root.
    Returns (files_written, manifest_entries).
    """
    ALLOWED_FMTS = {"telethon", "pyrogram", "tdata", "txt"}
    fmt = fmt if fmt in ALLOWED_FMTS else None
    files_written = 0
    manifest = []
    used_names = set()

    for idx, session in enumerate(sessions):
        if not isinstance(session, dict):
            continue
        phone = str(session.get("phone") or (session.get("session_data") or {}).get("phone") or f"unknown_{idx}")
        session_id = str(session.get("session_id") or session.get("id") or f"sid_{idx}")
        spam_status = _get_session_spam_status(session)
        status = session.get("status", "pending")
        rate = session.get("rate", 0)
        submitted = session.get("submitted_at") or session.get("created_at") or ""
        balance_credited = bool(session.get("balance_credited", False))
        msg_type = _get_session_message_type(session)

        base = _sanitize_filename(f"{phone}")
        if base in used_names:
            base = f"{base}_{session_id[-6:]}"
        used_names.add(base)

        session_dir = os.path.join(out_dir, base)
        os.makedirs(session_dir, exist_ok=True)

        session_string = session.get("session_string") or ""
        session_inner = session.get("session_data") or {}
        user_id = session_inner.get("user_id") or session.get("user_id") or 0
        first_name = session_inner.get("first_name") or session.get("first_name") or ""

        from bot.services.session_formats import generate_all_session_formats
        from bot import config

        formats = generate_all_session_formats(
            phone=phone,
            session_string=session_string,
            user_id=user_id,
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_id=session_id,
            out_dir=session_dir,
            first_name=first_name,
            telethon_file=session.get("telethon_file") or "",
            pyrogram_file=session.get("session_file") or "",
            formats={fmt} if fmt else None,
        )
        files_written += len(formats)

        # Per-session JSON only when exporting ALL formats
        if not fmt:
            try:
                from bot.services.session_io import build_session_data_full
                from bot.utils.device_generator import generate_device_info
                device_info = generate_device_info()
                full = build_session_data_full(
                    phone=phone,
                    session_info=session_inner,
                    session_string=session_string,
                    twofa_password=session.get("twofa_password") or "",
                    country_code=_get_session_country(session),
                    rate=rate,
                    account_status=status,
                    status_details=session.get("status_details") or {},
                    user_id=str(session.get("user_id") or ""),
                    device_info=device_info,
                )
                json_path = os.path.join(session_dir, f"{base}.json")
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(full, f, ensure_ascii=False, indent=2)
                files_written += 1
            except Exception as e:
                logger.warning("Country export: JSON generation failed for %s: %s", phone, e)

        manifest.append({
            "session_id": session_id,
            "phone": phone,
            "country_code": _get_session_country(session),
            "status": status,
            "spam_status": spam_status,
            "message_type": msg_type,
            "balance_credited": balance_credited,
            "rate": rate,
            "submitted_at": submitted,
            "formats": list(formats.keys()),
        })

    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    files_written += 1

    return files_written, manifest


# ------------------------------------------------------------------
# Module-level country export runner. Used by the category callback and
# the manual-count text handler (both inside register_country_export);
# kept at module level so it is reachable and testable.
# ------------------------------------------------------------------

async def _run_country_export(client, _edit, admin_id, country_code, category, sessions, limit=None, fmt=None):
    ALLOWED_FMTS = {"telethon", "pyrogram", "tdata", "txt"}
    fmt = fmt if fmt in ALLOWED_FMTS else None
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            await _edit(
                "❌ **Invalid export count!**\n\n"
                "Please enter a positive number of sessions to export.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Country", callback_data=f"country_export_{country_code}")],
                ])
            )
            return
        sessions = sessions[:limit]
        category = f"manual({len(sessions)})"

    if not sessions:
        await _edit(
            f"❌ No `{category}` sessions for {country_code} to export.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back to Country", callback_data=f"country_export_{country_code}")],
            ])
        )
        return

    fmt_label = EXPORT_FORMAT_LABELS.get(fmt, "ALL formats") if fmt else "ALL formats"
    await _edit(
        f"📦 **Exporting {category} for {country_code}**\n\n"
        f"⏳ {len(sessions)} sessions being processed...\n"
        f"Generating: {fmt_label}\n"
        f"Please wait...",
    )

    # MEDIUM-25: /tmp does not exist on Windows; use the OS temp dir.
    base_dir = os.path.join(tempfile.gettempdir(), f"country_export_{country_code}_{category}")
    os.makedirs(base_dir, exist_ok=True)
    zip_path = os.path.join(tempfile.gettempdir(), f"country_export_{country_code}_{category}_{fmt or 'all'}.zip")

    def _build_zip_blocking():
        files_written, manifest = _write_session_files(sessions, base_dir, fmt=fmt)
        import zipfile
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for dirpath, dirnames, filenames in os.walk(base_dir):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for fn in filenames:
                    full = os.path.join(dirpath, fn)
                    arc = os.path.relpath(full, base_dir).replace("\\", "/")
                    zipf.write(full, arc)
        return files_written, len(manifest)

    try:
        # IMPORTANT: load the telethon-export import chain in the MAIN thread
        # (same reason as audit._export_sessions - the Pyrogram client is
        # instantiated at import time and needs a running event loop, which a
        # worker thread does not have).
        from bot.verification.telethon_export import session_string_to_telethon  # noqa: F401

        files_written, manifest_count = await asyncio.to_thread(_build_zip_blocking)

        if os.path.exists(zip_path):
            zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            if zip_size_mb > 49:
                await _edit(
                    f"⚠️ ZIP file too large ({zip_size_mb:.1f} MB)! Telegram allows maximum 50 MB.\n"
                    f"Try exporting a smaller category.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Back to Country", callback_data=f"country_export_{country_code}")],
                    ])
                )
                return

        await client.send_document(
            chat_id=admin_id,
            document=zip_path,
            caption=(
                f"📦 **Country Export Complete!**\n\n"
                f"🌍 Country: {country_code} {get_flag(country_code)}\n"
                f"🗂️ Category: **{category}**\n"
                f"✅ Sessions: **{manifest_count}**\n"
                f"📁 Files: **{files_written}**\n"
                f"🧾 Format: {fmt_label}\n"
                f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            file_name=f"country_{country_code}_{category}_{fmt or 'all'}.zip"
        )

        await _log_admin_action(
            admin_id,
            "country_export",
            message=f"{country_code} {category} ({manifest_count} sessions)",
        )

        text, markup = _build_country_detail_message(country_code)
        await _edit(text, reply_markup=markup)

    except Exception as e:
        logger.error(f"Country export error: {e}")
        try:
            await _edit(
                f"❌ **Export Error!**\n\n{str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Country", callback_data=f"country_export_{country_code}")],
                ])
            )
        except Exception:
            pass
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except Exception:
                pass


def register_country_export(app: Client):

    @app.on_callback_query(admin_filter & filters.regex(r"^country_export_menu$"))
    async def country_export_menu_callback(client: Client, query: CallbackQuery):
        await query.answer()
        text, markup = _build_country_list_message(page=1)
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^country_export_page_(\d+)$"))
    async def country_export_page_callback(client: Client, query: CallbackQuery):
        await query.answer()
        m = re.match(r"^country_export_page_(\d+)$", str(query.data))
        page = int(m.group(1)) if m else 1
        text, markup = _build_country_list_message(page=page)
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^country_export_noop$"))
    async def country_export_noop_callback(client: Client, query: CallbackQuery):
        """Non-clickable grid/page padding placeholder - answers only."""
        await query.answer()

    @app.on_callback_query(admin_filter & filters.regex(r"^country_export_\+\d+$"))
    async def country_export_detail_callback(client: Client, query: CallbackQuery):
        await query.answer()
        m = re.match(r"^country_export_(\+\d+)$", str(query.data))
        if not m:
            return
        country_code = m.group(1)
        text, markup = _build_country_detail_message(country_code)
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^country_export_cat_\+\d+_(success|spam_free|spam|limit|multi_device|pending|rejected)$"))
    async def country_export_category_callback(client: Client, query: CallbackQuery):
        """Category tapped -> show format submenu (level 3)."""
        await query.answer()
        m = re.match(r"^country_export_cat_(\+\d+)_(.+)$", str(query.data))
        if not m:
            return
        country_code = m.group(1)
        category = m.group(2)
        if category not in EXPORTABLE_CATEGORIES:
            await _edit_callback(query, f"❌ Unknown category: {category}")
            return

        # Check if there are any sessions — if not, show back immediately
        sessions = get_country_category_sessions(country_code, category)
        if not sessions:
            await _edit_callback(query,
                f"❌ No `{category}` sessions for {country_code} to export.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Country", callback_data=f"country_export_{country_code}")],
                ])
            )
            return

        text, markup = _build_country_format_panel(country_code, category)
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^country_export_fmt_\+\d+_(success|spam_free|spam|limit|multi_device|pending|rejected)_(telethon|pyrogram|tdata|txt)$"))
    async def country_export_fmt_callback(client: Client, query: CallbackQuery):
        """Format tapped -> show All / Manual action panel (level 4)."""
        await query.answer()
        m = re.match(r"^country_export_fmt_(\+\d+)_(.+)_(telethon|pyrogram|tdata|txt)$", str(query.data))
        if not m:
            return
        country_code, category, fmt = m.group(1), m.group(2), m.group(3)
        text, markup = _build_country_action_panel(country_code, category, fmt)
        await _edit_callback(query, text, reply_markup=markup)

    @app.on_callback_query(admin_filter & filters.regex(r"^country_export_all_\+\d+_(success|spam_free|spam|limit|multi_device|pending|rejected)_(telethon|pyrogram|tdata|txt)$"))
    async def country_export_all_callback(client: Client, query: CallbackQuery):
        """All Export: export every session of the category in the chosen format."""
        await query.answer(admin_str(query.from_user.id, "Preparing export..."))
        m = re.match(r"^country_export_all_(\+\d+)_(.+)_(telethon|pyrogram|tdata|txt)$", str(query.data))
        if not m:
            return
        country_code, category, fmt = m.group(1), m.group(2), m.group(3)

        sessions = get_country_category_sessions(country_code, category)
        if not sessions:
            await _edit_callback(query,
                f"❌ No `{category}` sessions for {country_code} to export.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Country", callback_data=f"country_export_{country_code}")],
                ])
            )
            return

        async def _edit(text, reply_markup=None):
            return await _edit_callback(query, text, reply_markup=reply_markup)

        await _run_country_export(client, _edit, query.from_user.id, country_code, category, sessions, fmt=fmt)

    @app.on_callback_query(admin_filter & filters.regex(r"^country_export_manual_\+\d+_(success|spam_free|spam|limit|multi_device|pending|rejected)_(telethon|pyrogram|tdata|txt)$"))
    async def country_export_manual_callback(client: Client, query: CallbackQuery):
        """Manually Export: admin types how many sessions (of the category+format)
        should be exported."""
        await query.answer()
        m = re.match(r"^country_export_manual_(\+\d+)_(.+)_(telethon|pyrogram|tdata|txt)$", str(query.data))
        if not m:
            return
        country_code, category, fmt = m.group(1), m.group(2), m.group(3)
        _set_admin_state(query.from_user.id, query, "country_manual_export_count",
                         country_code=country_code, category=category, export_format=fmt)
        fmt_label = EXPORT_FORMAT_LABELS.get(fmt, fmt)
        await _edit_callback(query,
            f"✍️ **Manually Export - {country_code} {get_flag(country_code)}**\n\n"
            f"Category: {_category_label(category)}\n"
            f"Format: {fmt_label}\n\n"
            f"Enter how many sessions to export (1-1000).\n\n"
            f"Type /admin to cancel."
        )

    @app.on_message(ADMIN_TEXT_FILTER)
    async def country_export_manual_input(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id not in admin_state:
            message.continue_propagation()
            return
        state = admin_state[user_id]
        action = state.get("action", "")
        if action != "country_manual_export_count":
            message.continue_propagation()
            return
        message._handled_by_admin = True
        country_code = state.get("country_code") or ""
        category = state.get("category") or ""
        fmt = state.get("export_format") or None
        panel_chat = state.get("panel_chat_id") or message.chat.id
        panel_msg = state.get("panel_msg_id")
        text = (message.text or "").strip()
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

        async def _edit(text, reply_markup=None):
            return await _edit_by_ids(client, panel_chat, panel_msg, text,
                                      reply_markup=reply_markup, admin_id=user_id)

        try:
            if category in EXPORTABLE_CATEGORIES:
                sessions = get_country_category_sessions(country_code, category)
            else:
                sessions = get_all_country_sessions(country_code)
            await _run_country_export(client, _edit, user_id, country_code, category or "manual", sessions, limit=count, fmt=fmt)
        finally:
            _admin_manager.clear_state(user_id)
