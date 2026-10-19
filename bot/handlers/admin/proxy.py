"""
Proxy System admin panel - placed under Advanced Tools.
=====================================================================
Default + Override + Pool + Fallback: full professional proxy management.

Capacity gate: a proxy can only be opened for a country that is open
(capacity ON). The admin must enable the country in Country & Pricing
first. A closed country's proxy config is preserved but never activated.

Add Proxy to Country (simplified flow):
    - Shows ONLY the open countries as a fixed 2-column x 20-row grid
      (40 per page, CORE CONFIG visual style): ``flag + 6-char stable
      label + ✅ ON / ❌ OFF`` per button. ON = at least one ENABLED
      proxy in that country's pool, so no separate legend is needed.
    - Tapping a country opens its proxy page. Proxies are stored as a POOL
      (max 5 per country): every proxy added is appended (never replaces),
      and when one proxy fails the next one is tried automatically
      (round-robin + fail-over inside the connector).
    - Each proxy is shown as ``#1 … #5`` with its own delete button
      (per-proxy remove, never a one-click wipe).
    - Check Your Proxy reports session outcomes (success / rejected /
      multi-device) per proxy, and Proxy Health Check shows per-proxy
      performance with an instant active/inactive live test.
"""

import asyncio
import re
import time
import logging

from pyrogram.client import Client
from pyrogram import filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from bot.data.proxies import (
    is_proxy_system_enabled,
    set_proxy_system_enabled,
    get_default_proxy,
    set_default_proxy,
    clear_default_proxy,
    get_proxy_countries,
    get_country_proxy_config,
    get_country_proxies,
    add_country_proxy,
    remove_country_proxy,
    toggle_country_proxy_proxy,
    toggle_country_proxy,
    delete_country_proxy,
    parse_proxy_url,
    mask_proxy_url,
    proxy_display_key,
    get_country_proxy_health,
    get_country_proxy_health_summary,
    get_country_proxy_session_stats,
    PROXY_MAX_POOL,
)
from bot.utils.proxy_probe import probe_proxy
from bot.utils.country_data import get_country_info
from bot.utils.country_codes import get_country_proxy_label
from bot.data.countries import is_country_open, get_country_capacity, get_open_countries
from bot.handlers.admin.router import (
    admin_filter,
    admin_state,
    _admin_manager,
    _log_admin_action,
    _set_admin_state,
    _edit_callback,
    _panel_reply,
    ADMIN_TEXT_FILTER,
)
from bot.handlers.admin.design import hd

logger = logging.getLogger(__name__)

# ---- Actions that are handled by this module's text-input handler ----
_PROXY_TEXT_ACTIONS = {"px_default_set", "px_country_add_url", "px_pool_add"}

# ------------------------------------------------------------------
# Add-Proxy-to-Country grid geometry (visual reference: CORE CONFIG grid).
# Fixed 2-column x 20-row rectangle, 40 countries per page. The CORE
# CONFIG grid itself keeps its own 2x10 layout: untouched.
# ------------------------------------------------------------------
PX_GRID_COLUMNS = 2
PX_GRID_ROWS = 20
PX_PER_PAGE = PX_GRID_COLUMNS * PX_GRID_ROWS  # 40 countries per page

# Non-clickable placeholder for incomplete final pages (grid stays a
# clean rectangle) - same trick as the CORE CONFIG grid.
_PX_PLACEHOLDER = "ㅤ"


def _px_bi(text: str) -> str:
    """Bold-italic markdown for screen text (matches CORE CONFIG style)."""
    return f"**__{text}__**"


# ------------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------------

def _status_emoji(state: bool) -> str:
    return "🟢" if state else "🔴"


def _safe_int(value, default: int = -1) -> int:
    """Parse a callback-data integer safely (LOW-5: no crash on bad payload)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _url_digest(url: str) -> str:
    """8-char digest of a proxy URL (P4-39 TOCTOU guard, callback-safe)."""
    import hashlib
    return hashlib.sha1(str(url or "").encode("utf-8")).hexdigest()[:8]


def _country_has_proxy(country_code: str) -> bool:
    """True when the country has at least one ENABLED proxy configured."""
    cfg = get_country_proxy_config(country_code)
    if not cfg:
        return False
    return any(p.get("enabled") for p in cfg["proxies"])


async def _probe_one(label: str, url: str, masked: str = "", attribution: str = ""):
    """Probe a single proxy URL and return a ``(label, masked, status, detail)`` row.

    Runs the real protocol handshake with the shared probe ceiling
    (``proxy_probe.DEFAULT_PROBE_TIMEOUT_SECONDS``, 15s - the same budget the
    watchdog and the connector's first candidate use, so a manual test can
    never contradict the watchdog's verdict for a slow-but-alive proxy); any
    probe error is captured (never raised) so one dead proxy cannot fail the
    whole batch.

    P4-34: when ``attribution`` (a country code) is provided the probe result
    is recorded into the SAME health stats / circuit breaker the live traffic
    uses - a manual test that finds a dead proxy immediately opens the
    breaker, and a successful test resets its failure streak. Default-proxy
    probes pass no attribution (the default is attributed per-country by the
    connect plan; a manual default probe must not skew one country's stats).
    """
    ok, parsed, _ = parse_proxy_url(url)
    if not ok or parsed is None:
        return label, mask_proxy_url(url), "❌ INACTIVE", "invalid URL"
    try:
        started = time.monotonic()
        probe_ok, probe_detail = await probe_proxy(parsed)
        latency_ms = (time.monotonic() - started) * 1000
        if attribution:
            try:
                from bot.data.proxies import record_proxy_attempt
                record_proxy_attempt(attribution, parsed, probe_ok)
            except Exception:
                pass
        if probe_ok:
            return label, masked or mask_proxy_url(url), "🟢 ACTIVE", f"{latency_ms:.0f} ms"
        return label, masked or mask_proxy_url(url), "🔴 INACTIVE", probe_detail
    except Exception:
        return label, masked or mask_proxy_url(url), "🔴 INACTIVE", "no response"


def _health_bar(pct: float, width: int = 10) -> str:
    """Visual health bar (█ filled / ░ empty) for a percentage."""
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round(pct / 100 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _country_proxy_status_line(country_code: str) -> str:
    """One-line status: flag + name + pool count + open/closed."""
    name, flag = get_country_info(country_code)
    open_s = "🟢 ON" if is_country_open(country_code) else "🔴 OFF"
    cfg = get_country_proxy_config(country_code)
    pool = cfg["proxies"] if cfg else []
    total = len(pool)
    enabled = sum(1 for p in pool if p.get("enabled"))
    return f"{flag} **{name}** ({country_code}) — {open_s} — {enabled}/{total} proxies"


def _build_main_menu() -> str:
    """Build the proxy system main menu text."""
    enabled = is_proxy_system_enabled()
    default = get_default_proxy()
    countries = get_proxy_countries()
    lines = [
        hd("🌐 Proxy System", "Main › Advanced › Proxy"),
        "",
        f"System: {_status_emoji(enabled)} **{'ON' if enabled else 'OFF'}**",
        f"🌍 Countries with proxy: **{len(countries)}**",
    ]
    if default:
        lines.append(f"⚙️ Default: `{mask_proxy_url(default)}`")
    else:
        lines.append("⚙️ Default: — (not set)")
    # P4-33: aggregated watchdog snapshot - dead count without running a probe.
    try:
        from bot.monitor.proxy_watchdog import get_proxy_watchdog_snapshot
        snap = get_proxy_watchdog_snapshot()
        if snap.get("ts"):
            dead = int(snap.get("dead_count", 0) or 0)
            total = int(snap.get("total", 0) or 0)
            dead_icon = "💀" if dead else "🟢"
            lines.append(f"{dead_icon} Watchdog: **{dead}/{total} dead** (🕒 {snap.get('ts')})")
    except Exception:
        pass
    return "\n".join(lines)


def _build_main_buttons() -> InlineKeyboardMarkup:
    """Dynamic buttons for the proxy main menu."""
    enabled = is_proxy_system_enabled()
    default = get_default_proxy()
    buttons = [
        [
            InlineKeyboardButton(
                f"{_status_emoji(enabled)} System: {'ON' if enabled else 'OFF'}",
                callback_data="px_sys_toggle",
            ),
            InlineKeyboardButton("⚙️ Default", callback_data="px_default"),
        ],
        [InlineKeyboardButton("➕ Add Proxy to Country", callback_data="px_country_add")],
        [InlineKeyboardButton("📋 Proxy List & Status", callback_data="px_list")],
        [InlineKeyboardButton("🧪 Test All Proxies", callback_data="px_test_all")],
        [InlineKeyboardButton("◀ Back to Advanced Tools", callback_data="menu_advanced_tools")],
    ]
    return InlineKeyboardMarkup(buttons)


def _build_default_screen() -> str:
    """Text for the 'Default Proxy' sub-screen."""
    default = get_default_proxy()
    if default:
        ok, parsed, _ = parse_proxy_url(default)
        if ok and parsed:
            scheme = parsed["scheme"]
            host = parsed["hostname"]
            if ":" in host:
                host = f"[{host}]"
            port = parsed["port"]
            auth = " (with auth)" if "username" in parsed else ""
            info = f"`{mask_proxy_url(default)}`\n→ {scheme.upper()} {host}:{port}{auth}"
        else:
            # Corrupt/legacy stored URL: never echo it back (it may contain
            # raw credentials) - ask the admin to set it again instead.
            info = "⚠️ The stored default proxy URL is **invalid**.\nSet it again to fix it."
    else:
        info = "No global default proxy set.\nUsed when a country has no proxy pool."
    return (
        hd("⚙️ Default Proxy", "Main › Advanced › Proxy › Default") + "\n\n"
        f"{info}"
    )


def _build_default_buttons() -> InlineKeyboardMarkup:
    default = get_default_proxy()
    buttons = []
    if default:
        buttons.append([InlineKeyboardButton("✏️ Change Default", callback_data="px_default_set"),
                        InlineKeyboardButton("🗑️ Clear Default", callback_data="px_default_clear")])
    else:
        buttons.append([InlineKeyboardButton("✏️ Set Default", callback_data="px_default_set")])
    buttons.append([InlineKeyboardButton("◀ Back to Proxy System", callback_data="px_menu")])
    return InlineKeyboardMarkup(buttons)


# ------------------------------------------------------------------
#  Add Proxy to Country - open-country grid (flag + 6-char label + ON/OFF)
# ------------------------------------------------------------------

def _sorted_open_countries() -> list:
    """Open country codes sorted by display name (professional ordering).

    Only codes starting with ``+`` are included: callback-query data patterns
    ``px_csel_`` etc. expect a plus sign followed by digits. Codes that do not
    start with ``+`` are silently skipped (they are data anomalies and cannot
    be used with the proxy admin panel without a callback-format mismatch).
    """
    codes = [c for c in get_open_countries() if c.startswith("+")]

    def _key(cc: str):
        try:
            name, _ = get_country_info(cc)
            return (name or cc).lower()
        except Exception:
            return cc.lower()

    return sorted(set(codes), key=_key)


def _build_add_country_list(page: int = 1):
    """Build the 'Add Proxy to Country' open-country list (text + buttons).

    Returns ``(text, button_rows)``. Compact header only - no country-name
    text list, no legend, no separators. Every open country appears once in
    a fixed 2-column x 20-row grid as ``flag + 6-char stable label + ON/OFF``
    (the ON/OFF state inside the button replaces the old legend). Tapping a
    country still opens that country's proxy page via the unchanged
    ``px_csel_`` callback; the real country code stays the identifier, the
    6-char label is display-only.
    """
    open_codes = _sorted_open_countries()
    if not open_codes:
        text = (
            _px_bi("➕ ADD PROXY TO COUNTRY") + "\n"
            + _px_bi("MAIN › ADVANCED › PROXY › ADD") + "\n\n"
            + _px_bi("❌ NO OPEN COUNTRIES") + "\n"
            + "Open a country in Country & Pricing first."
        )
        rows = [[InlineKeyboardButton("◀ BACK TO PROXY SYSTEM", callback_data="px_menu")]]
        return text, rows

    total_pages = max(1, (len(open_codes) + PX_PER_PAGE - 1) // PX_PER_PAGE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * PX_PER_PAGE
    page_codes = open_codes[start:start + PX_PER_PAGE]

    lines = [
        _px_bi("➕ ADD PROXY TO COUNTRY"),
        _px_bi("MAIN › ADVANCED › PROXY › ADD"),
        _px_bi("SELECT A COUNTRY"),
        _px_bi(f"PAGE {page}/{total_pages}"),
    ]

    # Pad the final page so the grid stays an aligned 2-column rectangle.
    padded = page_codes + [None] * (PX_PER_PAGE - len(page_codes))
    rows = []
    for row in range(PX_GRID_ROWS):
        row_buttons = []
        for col in range(PX_GRID_COLUMNS):
            cc = padded[row * PX_GRID_COLUMNS + col]
            if cc is None:
                row_buttons.append(InlineKeyboardButton(_PX_PLACEHOLDER, callback_data="px_noop"))
                continue
            _name, flag = get_country_info(cc)
            label = get_country_proxy_label(cc)
            on = _country_has_proxy(cc)
            # ON padded to the same width as OFF so every label is equal-length.
            status = "✅ ON  " if on else "❌ OFF "
            row_buttons.append(
                InlineKeyboardButton(f"{flag} {label} {status}", callback_data=f"px_csel_{cc}")
            )
        rows.append(row_buttons)

    # Page indicator row (Prev / page / Next) with placeholder balance.
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"px_cpage_{page - 1}"))
    else:
        nav.append(InlineKeyboardButton(_PX_PLACEHOLDER, callback_data="px_noop"))
    # MEDIUM-43: the page indicator is a no-op (tapping "2/3" used to reset
    # to page 1 via the px_country_add callback).
    nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="px_noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"px_cpage_{page + 1}"))
    else:
        nav.append(InlineKeyboardButton(_PX_PLACEHOLDER, callback_data="px_noop"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("◀ BACK TO PROXY SYSTEM", callback_data="px_menu")])
    return "\n".join(lines), rows


# ------------------------------------------------------------------
#  Country proxy page (Add / Remove / Check / Health)
# ------------------------------------------------------------------

def _build_country_proxy_page(country_code: str):
    """Country detail page: per-proxy list (#1..#5) with individual delete."""
    name, flag = get_country_info(country_code)
    pool_cfg = get_country_proxy_config(country_code)
    pool = pool_cfg["proxies"] if pool_cfg else []
    enabled_count = sum(1 for p in pool if p.get("enabled"))
    system_on = is_proxy_system_enabled()

    lines = [
        hd(f"{flag} {name}", f"Main › Advanced › Proxy › {country_code}"),
        "",
        f"System: {_status_emoji(system_on)} **{'ON' if system_on else 'OFF'}**",
        f"📦 Pool: **{len(pool)}/{PROXY_MAX_POOL}** ({enabled_count} active)",
        "",
    ]
    if pool:
        lines.append("**Proxies:**")
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        for i, p in enumerate(pool, start=1):
            mark = "✅" if p.get("enabled") else "⛔"
            lines.append(f"{mark} **#{i}** `{mask_proxy_url(p['url'])}`")
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        lines.append("🗑️ Tap a proxy number below to delete it.")
    else:
        lines.append("❌ **Proxy Not Set**")
        lines.append("")
    if len(pool) >= PROXY_MAX_POOL:
        lines.append("⚠️ **Max 5 proxies reached** - delete one to add another.")

    rows = []
    # One delete button per proxy (never a one-click wipe).
    for i, p in enumerate(pool, start=1):
        rows.append([InlineKeyboardButton(f"🗑️ Delete #{i}",
                                          callback_data=f"px_cdel_{country_code}_{i - 1}")])
    if len(pool) < PROXY_MAX_POOL:
        rows.append([InlineKeyboardButton("➕ Add Proxy", callback_data=f"px_cadd_{country_code}")])
    rows.append([InlineKeyboardButton("📊 Check Your Proxy", callback_data=f"px_cstat_{country_code}"),
                 InlineKeyboardButton("🩺 Health Check", callback_data=f"px_chk_{country_code}")])
    rows.append([InlineKeyboardButton("◀ Back to Add Proxy", callback_data="px_country_add")])
    return "\n".join(lines), rows


# ------------------------------------------------------------------
#  Check Your Proxy - session outcome report
# ------------------------------------------------------------------

def _build_check_your_proxy(country_code: str):
    """Professional report: country-wide + per-proxy (#1..#5) outcomes."""
    name, flag = get_country_info(country_code)
    try:
        from bot.handlers.admin.country_export import build_country_export_stats
        stats = build_country_export_stats().get(country_code, {})
    except Exception:
        stats = {}
    tracked = get_country_proxy_session_stats(country_code)
    health = get_country_proxy_health(country_code)
    sessions_map = health.get("sessions") or {}
    pool_cfg = get_country_proxy_config(country_code)
    pool = pool_cfg["proxies"] if pool_cfg else []

    success = int(stats.get("success", 0))
    rejected = int(stats.get("rejected", 0))
    multi_device = int(stats.get("multi_device", 0))
    pending = int(stats.get("pending", 0))
    total = int(stats.get("total", 0))

    lines = [
        hd(f"📊 Check Your Proxy — {flag} {name}",
           f"Main › Advanced › Proxy › {country_code} › Check"),
        "",
        "📈 Board session totals (this country, from the session registry):",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"✅ **Success**        {success}",
        f"❌ **Rejected**       {rejected}",
        f"📱 **Multi-Device**   {multi_device}",
        f"⏳ **Pending**        {pending}",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"📈 **Total Sessions** {total}",
    ]
    settled = success + rejected + multi_device
    if settled > 0:
        rate = round(success / settled * 100, 1)
        lines.append(f"🎯 Settlement Success Rate: **{rate}%**")
    lines.append("")

    # Proxy-attributed tracked outcomes. Attribution follows the proxy pool
    # owner (a +1876 session routed through a +1 pool is stored under +1), so
    # fallback sessions appear on the configured country's page.
    # B1 FIX: health/session keys are the DIGEST-suffixed display keys
    # (``mask#<sha1-8>``, see _proxy_key), not the plain masked URL - the old
    # lookup never matched, so every pool proxy showed "no data" and the real
    # rows appeared under "extra keys".
    pool_keys = {proxy_display_key(parse_proxy_url(p["url"])[1] or {}) for p in pool}
    extra_keys = [k for k in sessions_map.keys() if k not in pool_keys]
    if sessions_map:
        lines.append("**Proxy-attributed outcomes:**")
        for i, p in enumerate(pool, start=1):
            mark = "✅" if p.get("enabled") else "⛔"
            entry = sessions_map.get(proxy_display_key(parse_proxy_url(p["url"])[1] or {})) or {}
            lines.append(
                f"   {mark} **#{i}**  →  "
                f"✅ {entry.get('success', 0)}  "
                f"❌ {entry.get('rejected', 0)}  "
                f"📱 {entry.get('multi_device', 0)}  "
                f"∑ {entry.get('total', 0)}"
            )
        # Any proxy used by this country that is NOT part of its current pool
        # (e.g. the global default proxy, or a proxy removed after use) plus
        # the direct-connection bucket (legacy key "default" == "direct").
        for key in sorted(extra_keys):
            entry = sessions_map.get(key) or {}
            if key in ("direct", "default"):
                label = "🛰️ **Direct (no proxy)**"
            else:
                label = f"⚙️ **{key}**"
            lines.append(
                f"   {label}  →  "
                f"✅ {entry.get('success', 0)}  "
                f"❌ {entry.get('rejected', 0)}  "
                f"📱 {entry.get('multi_device', 0)}  "
                f"∑ {entry.get('total', 0)}"
            )
        lines.append("")

    if int(tracked.get("total", 0)) > 0:
        lines.append(f"✅ {tracked['success']}   ❌ {tracked['rejected']}   "
                     f"📱 {tracked['multi_device']}   (∑ {tracked['total']})")
        lines.append("")

    rows = [
        [InlineKeyboardButton("🩺 Proxy Health Check", callback_data=f"px_chk_{country_code}"),
         InlineKeyboardButton("➕ Add Proxy", callback_data=f"px_cadd_{country_code}")],
        [InlineKeyboardButton("◀ Back to Country", callback_data=f"px_csel_{country_code}")],
    ]
    return "\n".join(lines), rows


# ------------------------------------------------------------------
#  Proxy Health Check - performance report + instant live test
# ------------------------------------------------------------------

def _build_health_check(country_code: str):
    """Health report: per-proxy health + session outcomes."""
    name, flag = get_country_info(country_code)
    pool_cfg = get_country_proxy_config(country_code)
    pool = pool_cfg["proxies"] if pool_cfg else []
    health = get_country_proxy_health(country_code)
    attempts_map = health.get("attempts") or {}
    sessions_map = health.get("sessions") or {}
    summary = get_country_proxy_health_summary(country_code)

    lines = [
        hd(f"🩺 Proxy Health Check — {flag} {name}",
           f"Main › Advanced › Proxy › {country_code} › Health"),
        "",
    ]

    # B1 FIX: digest-suffixed display keys (see _proxy_key) - the old plain
    # masked-URL lookup never matched the health map keys.
    pool_keys = {proxy_display_key(parse_proxy_url(p["url"])[1] or {}) for p in pool}
    extra_keys = [k for k in attempts_map.keys() if k not in pool_keys]
    extra_sess_keys = [k for k in sessions_map.keys() if k not in pool_keys]

    if not pool and not attempts_map and not sessions_map:
        lines.append("❌ **No proxy set for this country.**")
        lines.append("")
        lines.append("Add a proxy first to run a health check.")
        rows = [
            [InlineKeyboardButton("➕ Add Proxy", callback_data=f"px_cadd_{country_code}")],
            [InlineKeyboardButton("◀ Back to Country", callback_data=f"px_csel_{country_code}")],
        ]
        return "\n".join(lines), rows

    if pool:
        lines.append(f"📦 Pool: **{len(pool)}/{PROXY_MAX_POOL}**")
    lines.append("")

    # Overall
    if summary["total_attempts"] > 0:
        pct = summary["overall_percent"]
        lines.append("📡 **Overall Performance**")
        lines.append(f"   {_health_bar(pct)} **{pct:.1f}%**")
        lines.append(f"   ✅ Success: {summary['total_ok']}   ❌ Fail: {summary['total_fail']}   "
                     f"∑ {summary['total_attempts']}")
        lines.append("")
    else:
        lines.append("📡 **No connection attempts recorded yet.**")
        lines.append("")

    # Per-proxy: health + session outcomes
    lines.append("**Per proxy (health + sessions):**")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    for i, p in enumerate(pool, start=1):
        mark = "✅" if p.get("enabled") else "⛔"
        masked = mask_proxy_url(p["url"])
        lines.append(f"{mark} **#{i}** `{masked}`")
        key = proxy_display_key(parse_proxy_url(p["url"])[1] or {})
        a = attempts_map.get(key)
        if a:
            ok = int(a.get("ok", 0)); fail = int(a.get("fail", 0)); t = ok + fail
            pct_p = round(ok / t * 100, 1) if t > 0 else 0.0
            lines.append(f"   📡 {_health_bar(pct_p)} {pct_p:.1f}%  (✅ {ok}  ❌ {fail}  ∑ {t})")
            if a.get("last_ts"):
                lines.append(f"   🕐 Last: `{a['last_ts']}`")
        else:
            lines.append("   📡 No connection data yet")
        s = sessions_map.get(key)
        if s:
            lines.append(f"   📊 ✅ {s.get('success',0)}  ❌ {s.get('rejected',0)}  "
                         f"📱 {s.get('multi_device',0)}  (∑ {s.get('total',0)})")
        else:
            lines.append("   📊 No session outcomes yet")
        lines.append("")

    # Extra keys not in the current pool (default proxy, direct, removed proxies)
    for key in sorted(extra_keys + extra_sess_keys):
        if key in pool_keys:
            continue
        a = attempts_map.get(key)
        s = sessions_map.get(key)
        if key in ("direct", "default"):
            label = "🛰️ **Direct (no proxy)**"
        else:
            label = f"⚙️ **{key}**"
        lines.append(f"{label}")
        if a:
            ok = int(a.get("ok", 0)); fail = int(a.get("fail", 0)); t = ok + fail
            pct_p = round(ok / t * 100, 1) if t > 0 else 0.0
            lines.append(f"   📡 {_health_bar(pct_p)} {pct_p:.1f}%  (✅ {ok}  ❌ {fail}  ∑ {t})")
            if a.get("last_ts"):
                lines.append(f"   🕐 Last: `{a['last_ts']}`")
        else:
            lines.append("   📡 No connection data yet")
        if s:
            lines.append(f"   📊 ✅ {s.get('success',0)}  ❌ {s.get('rejected',0)}  "
                         f"📱 {s.get('multi_device',0)}  (∑ {s.get('total',0)})")
        else:
            lines.append("   📊 No session outcomes yet")
        lines.append("")

    # Overall session outcomes
    sess_total = get_country_proxy_session_stats(country_code)
    if sess_total.get("total", 0) > 0:
        lines.append("📊 **All proxies combined sessions:**")
        lines.append(f"   ✅ {sess_total['success']}  ❌ {sess_total['rejected']}  "
                     f"📱 {sess_total['multi_device']}  (∑ {sess_total['total']})")
        lines.append("")

    # Note: the attempt counter counts proxy CONNECTIONS, not codes sent.
    lines.append("ℹ️ Counts = proxy CONNECTIONS (2 per account: code send + device check).")
    lines.append("")

    rows = [
        [InlineKeyboardButton("⚡ Instant Live Check", callback_data=f"px_chk_live_{country_code}")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"px_chk_{country_code}")],
        [InlineKeyboardButton("📊 Check Your Proxy", callback_data=f"px_cstat_{country_code}")],
        [InlineKeyboardButton("◀ Back to Country", callback_data=f"px_csel_{country_code}")],
    ]
    return "\n".join(lines), rows


def _build_live_check_screen(country_code: str, results: list):
    """Format the instant live-check results (active/inactive + latency)."""
    name, flag = get_country_info(country_code)
    lines = [
        hd(f"⚡ Live Check — {flag} {name}",
           f"Main › Advanced › Proxy › {country_code} › Health › Live"),
        "",
    ]
    if not results:
        lines.append("No proxies to test for this country - add one first.")
    for label, masked, status, detail in results:
        lines.append(f"{label} `{masked}`")
        lines.append(f"   {status} {detail}")
        lines.append("")
    rows = [
        [InlineKeyboardButton("⚡ Run Again", callback_data=f"px_chk_live_{country_code}"),
         InlineKeyboardButton("🔄 Refresh Report", callback_data=f"px_chk_{country_code}")],
        [InlineKeyboardButton("◀ Back to Country", callback_data=f"px_csel_{country_code}")],
    ]
    return "\n".join(lines), rows


# ------------------------------------------------------------------
#  Proxy list (advanced, per-country pools)
# ------------------------------------------------------------------

def _build_country_list() -> str:
    """Text for the proxy country list (advanced view)."""
    countries = get_proxy_countries()
    if not countries:
        return (
            hd("📋 Proxy List", "Main › Advanced › Proxy › List") + "\n\n"
            "No proxy configurations yet.\n\n"
            "Use ➕ **Add Proxy to Country** to get started."
        )
    lines = [hd("📋 Proxy List", "Main › Advanced › Proxy › List"), "", "**Countries with proxy:**", ""]
    for cc in countries:
        lines.append(f"▪️ {_country_proxy_status_line(cc)}")
    lines.append("")
    return "\n".join(lines)


def _build_country_list_buttons() -> InlineKeyboardMarkup:
    countries = get_proxy_countries()
    buttons = []
    for cc in countries:
        name, flag = get_country_info(cc)
        buttons.append([InlineKeyboardButton(f"{flag} {name} ({cc})", callback_data=f"px_manage_{cc}")])
    if not countries:
        buttons.append([InlineKeyboardButton("➕ Add Proxy to Country", callback_data="px_country_add")])
    buttons.append([InlineKeyboardButton("◀ Back to Proxy System", callback_data="px_menu")])
    return InlineKeyboardMarkup(buttons)


def _build_manage_screen(country_code: str) -> str:
    """Advanced country detail text (pool management)."""
    name, flag = get_country_info(country_code)
    pool_cfg = get_country_proxy_config(country_code)
    open_s = "🟢 ON" if is_country_open(country_code) else "🔴 OFF"
    cap = get_country_capacity(country_code)
    cap_text = f"{cap}" if cap > 0 else "Unlimited"
    pool_enabled = pool_cfg["enabled"] if pool_cfg else False
    pool = pool_cfg["proxies"] if pool_cfg else []
    pool_status = _status_emoji(pool_enabled) + (" ON" if pool_enabled else " OFF")

    lines = [
        hd(f"{flag} {name}", f"Main › Advanced › Proxy › {country_code}"),
        "",
        f"Status: {open_s} (Capacity: {cap_text})",
        f"Pool: {pool_status} | Proxies: {len(pool)}/{PROXY_MAX_POOL}",
        "",
    ]
    if not pool:
        lines.append("No proxies configured yet.")
    else:
        for i, p in enumerate(pool):
            mark = "✅" if p.get("enabled") else "⛔"
            lines.append(f"`#{i + 1}` {mark} `{mask_proxy_url(p['url'])}`")
    lines.append("")
    return "\n".join(lines)


def _build_manage_buttons(country_code: str) -> InlineKeyboardMarkup:
    pool_cfg = get_country_proxy_config(country_code)
    pool = pool_cfg["proxies"] if pool_cfg else []
    pool_enabled = pool_cfg["enabled"] if pool_cfg else False
    is_open = is_country_open(country_code)

    buttons = []
    for i, p in enumerate(pool):
        toggle_label = "⛔ Disable" if p.get("enabled") else "✅ Enable"
        one_btn = [
            InlineKeyboardButton(f"#{i + 1} {toggle_label}",
                                 callback_data=f"px_prx_toggle_{country_code}_{i}"),
            InlineKeyboardButton(f"🗑️ #{i + 1}",
                                 callback_data=f"px_prx_del_{country_code}_{i}"),
        ]
        buttons.append(one_btn)

    if is_open and len(pool) < PROXY_MAX_POOL:
        buttons.append([InlineKeyboardButton("➕ Add Proxy", callback_data=f"px_pool_add_{country_code}")])
    elif not is_open:
        buttons.append([InlineKeyboardButton("🔒 Country Closed — Open in Country & Pricing first",
                                             callback_data="px_menu")])

    if pool_cfg:
        from bot.handlers.admin.design import sw
        buttons.append([InlineKeyboardButton(sw("Pool", pool_enabled), callback_data=f"px_pool_toggle_{country_code}"),
                        InlineKeyboardButton("🗑️ Remove All Proxies", callback_data=f"px_pool_del_{country_code}")])

    buttons.append([InlineKeyboardButton("🧪 Test Proxies", callback_data=f"px_test_{country_code}")])
    buttons.append([InlineKeyboardButton("◀ Back to Proxy List", callback_data="px_list"),
                    InlineKeyboardButton("🔙 Back to Proxy System", callback_data="px_menu")])
    return InlineKeyboardMarkup(buttons)


# ------------------------------------------------------------------
#  Registration
# ------------------------------------------------------------------

def register_proxy(app: Client):

    # ==================== Main Menu ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_menu$"))
    async def px_menu_callback(client: Client, query: CallbackQuery):
        await query.answer()
        await _edit_callback(query, _build_main_menu(), reply_markup=_build_main_buttons())

    # ==================== Master Switch ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_sys_toggle$"))
    async def px_sys_toggle_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        new_state = set_proxy_system_enabled(not is_proxy_system_enabled())
        await query.answer()
        await _log_admin_action(uid, "proxy_sys_toggle", message=f"enabled={new_state}")
        await _edit_callback(query, _build_main_menu(), reply_markup=_build_main_buttons())

    # ==================== Default Proxy ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_default$"))
    async def px_default_callback(client: Client, query: CallbackQuery):
        await query.answer()
        await _edit_callback(query, _build_default_screen(), reply_markup=_build_default_buttons())

    @app.on_callback_query(admin_filter & filters.regex(r"^px_default_set$"))
    async def px_default_set_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        await query.answer()
        await _edit_callback(query,
            "--- SET DEFAULT PROXY ---\n\n"
            "Send the proxy URL:\n"
            "Format: `socks5://user:pass@host:port`\n"
            "Supported: socks5, socks4, http\n\n"
            "Type /admin to cancel."
        )
        _set_admin_state(uid, query, "px_default_set")

    @app.on_callback_query(admin_filter & filters.regex(r"^px_default_clear$"))
    async def px_default_clear_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        if clear_default_proxy():
            await query.answer()
            await _log_admin_action(uid, "proxy_default_clear")
        else:
            await query.answer("No default proxy was set")
        await _edit_callback(query, _build_main_menu(), reply_markup=_build_main_buttons())

    # ==================== Add Proxy to Country (open-country list) ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_country_add$"))
    async def px_country_add_callback(client: Client, query: CallbackQuery):
        await query.answer()
        text, rows = _build_add_country_list(1)
        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(rows))

    @app.on_callback_query(admin_filter & filters.regex(r"^px_cpage_(\d+)$"))
    async def px_cpage_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_cpage_(\d+)$", str(query.data))
        page = _safe_int(m.group(1), 1) if m else 1
        await query.answer()
        text, rows = _build_add_country_list(page)
        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(rows))

    @app.on_callback_query(admin_filter & filters.regex(r"^px_noop$"))
    async def px_noop_callback(client: Client, query: CallbackQuery):
        """Non-clickable grid/page padding placeholder - answers only."""
        await query.answer()

    # ==================== Country proxy page ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_csel_(\+\d+)$"))
    async def px_csel_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_csel_(\+\d+)$", str(query.data))
        if not m:
            return
        cc = m.group(1)
        await query.answer()
        text, rows = _build_country_proxy_page(cc)
        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(rows))

    # ==================== Add Proxy (append to pool) ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_cadd_(\+\d+)$"))
    async def px_cadd_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_cadd_(\+\d+)$", str(query.data))
        if not m:
            return
        uid = query.from_user.id
        cc = m.group(1)
        if not is_country_open(cc):
            await query.answer("❌ Country is CLOSED — open it first in Country & Pricing",
                               show_alert=True)
            return
        pool = get_country_proxies(cc)
        if len(pool) >= PROXY_MAX_POOL:
            await query.answer(f"❌ Max {PROXY_MAX_POOL} proxies reached. Delete one first.",
                               show_alert=True)
            return
        await query.answer()
        await _edit_callback(query,
            f"--- ADD PROXY FOR {cc} ---\n\n"
            "Send the proxy URL:\n"
            "Format: `socks5://user:pass@host:port`\n"
            f"📦 Pool: {len(get_country_proxies(cc))}/{PROXY_MAX_POOL} (auto fail-over)\n\n"
            "Type /admin to cancel."
        )
        _set_admin_state(uid, query, "px_country_add_url", country_code=cc)

    # ==================== Delete single proxy (per-proxy) ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_cdel_(\+\d+)_(\d+)$"))
    async def px_cdel_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_cdel_(\+\d+)_(\d+)$", str(query.data))
        if not m:
            return
        cc = m.group(1)
        idx = _safe_int(m.group(2), -1)
        pool = get_country_proxies(cc)
        if idx < 0 or idx >= len(pool):
            await query.answer("❌ Proxy not found", show_alert=True)
            return
        name, flag = get_country_info(cc)
        masked = mask_proxy_url(pool[idx]["url"])
        await query.answer()
        buttons = [
            # P4-39: the confirm callback carries an 8-char digest of the url
            # it was built from (fits the 64-byte callback limit). The
            # executor verifies the pool at that index STILL holds a url with
            # the same digest before deleting - a concurrent pool edit between
            # the two taps can no longer delete the wrong proxy (TOCTOU).
            [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"px_cdel_yes_{cc}_{idx}_{_url_digest(pool[idx]['url'])}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"px_csel_{cc}")],
        ]
        await _edit_callback(query,
            f"⚠️ **Delete Proxy #{idx + 1} for {flag} {name} ({cc})?**\n\n"
            f"`{masked}`\n\n"
            "Only this proxy will be removed. Cannot be undone.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^px_cdel_yes_(\+\d+)_(\d+)_(.+)$"))
    async def px_cdel_yes_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_cdel_yes_(\+\d+)_(\d+)_(.+)$", str(query.data))
        if not m:
            return
        uid = query.from_user.id
        cc = m.group(1)
        try:
            idx = int(m.group(2))
        except (TypeError, ValueError):
            await query.answer("❌ Invalid proxy index", show_alert=True)
            return
        confirmed_digest = m.group(3)
        pool = get_country_proxies(cc)
        # P4-39: verify the entry at this index is STILL the one the admin
        # confirmed. A concurrent edit shifted indices -> refuse instead of
        # silently deleting a different proxy.
        if idx < 0 or idx >= len(pool):
            await query.answer("❌ Proxy not found (pool changed) - reopen the list", show_alert=True)
            return
        if _url_digest(pool[idx]["url"]) != confirmed_digest:
            await query.answer(
                "⚠️ Pool changed since you tapped Delete - the proxy at this index is now a "
                "different one. Nothing was deleted; reopen the list and try again.",
                show_alert=True,
            )
            return
        ok, msg = remove_country_proxy(cc, idx)
        await query.answer()
        if ok:
            await _log_admin_action(uid, "proxy_delete", message=f"{cc} #{idx + 1}")
        text, rows = _build_country_proxy_page(cc)
        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(rows))

    # ==================== Check Your Proxy ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_cstat_(\+\d+)$"))
    async def px_cstat_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_cstat_(\+\d+)$", str(query.data))
        if not m:
            return
        cc = m.group(1)
        await query.answer()
        text, rows = _build_check_your_proxy(cc)
        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(rows))

    # ==================== Proxy Health Check ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_chk_(\+\d+)$"))
    async def px_chk_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_chk_(\+\d+)$", str(query.data))
        if not m:
            return
        cc = m.group(1)
        await query.answer()
        text, rows = _build_health_check(cc)
        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(rows))

    @app.on_callback_query(admin_filter & filters.regex(r"^px_chk_live_(\+\d+)$"))
    async def px_chk_live_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_chk_live_(\+\d+)$", str(query.data))
        if not m:
            return
        cc = m.group(1)
        await query.answer("Running live check...")

        # Proxies to test: EVERY pool entry of this country (enabled or not),
        # so the admin sees live status for #1..#5. Falls back to the global
        # default only when the country has no pool at all.
        tests = []
        cfg = get_country_proxy_config(cc)
        if cfg and cfg["proxies"]:
            for i, p in enumerate(cfg["proxies"]):
                prefix = "#" if p.get("enabled") else "⛔#"
                tests.append((f"{prefix}{i + 1}", p["url"], cc))
        if not tests:
            default = get_default_proxy()
            if default:
                tests.append(("⚙️ Default", default, ""))  # no attribution

        tasks = [_probe_one(label, url, attribution=attr) for label, url, attr in tests]
        results = await asyncio.gather(*tasks)

        text, rows = _build_live_check_screen(cc, results)
        await _edit_callback(query, text, reply_markup=InlineKeyboardMarkup(rows))

    # ==================== List (advanced) ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_list$"))
    async def px_list_callback(client: Client, query: CallbackQuery):
        await query.answer()
        await _edit_callback(query, _build_country_list(), reply_markup=_build_country_list_buttons())

    # ==================== Country Manage (advanced pool) ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_manage_(\+\d+)$"))
    async def px_manage_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_manage_(\+\d+)$", str(query.data))
        if not m:
            return
        cc = m.group(1)
        if get_country_proxy_config(cc) is None:
            await query.answer("❌ No proxy config for this country", show_alert=True)
            return
        await query.answer()
        await _edit_callback(query, _build_manage_screen(cc), reply_markup=_build_manage_buttons(cc))

    # ==================== Add Proxy to Pool (URL input) ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_pool_add_(\+\d+)$"))
    async def px_pool_add_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_pool_add_(\+\d+)$", str(query.data))
        if not m:
            return
        uid = query.from_user.id
        cc = m.group(1)
        if not is_country_open(cc):
            await query.answer("❌ Country is CLOSED — open it first in Country & Pricing",
                               show_alert=True)
            return
        pool = get_country_proxies(cc)
        if len(pool) >= PROXY_MAX_POOL:
            await query.answer(f"❌ Max {PROXY_MAX_POOL} proxies reached. Remove one first.",
                               show_alert=True)
            return
        await query.answer()
        await _edit_callback(query,
            f"--- ADD PROXY FOR {cc} ---\n\n"
            f"Send the proxy URL:\n"
            f"Format: `socks5://user:pass@host:port`\n"
            f"Pool: {len(pool)}/{PROXY_MAX_POOL}\n\n"
            "Type /admin to cancel."
        )
        _set_admin_state(uid, query, "px_pool_add", country_code=cc)

    # ==================== Toggle Pool ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_pool_toggle_(\+\d+)$"))
    async def px_pool_toggle_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_pool_toggle_(\+\d+)$", str(query.data))
        if not m:
            return
        uid = query.from_user.id
        cc = m.group(1)
        ok, msg = toggle_country_proxy(cc)
        await query.answer()
        if ok:
            await _log_admin_action(uid, "proxy_pool_toggle", message=cc)
        await _edit_callback(query, _build_manage_screen(cc), reply_markup=_build_manage_buttons(cc))

    # ==================== Delete whole pool ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_pool_del_(\+\d+)$"))
    async def px_pool_del_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_pool_del_(\+\d+)$", str(query.data))
        if not m:
            return
        cc = m.group(1)
        await query.answer()
        buttons = [
            [InlineKeyboardButton("✅ Yes, Remove All", callback_data=f"px_pool_del_yes_{cc}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"px_manage_{cc}")],
        ]
        await _edit_callback(query,
            f"⚠️ **Remove ALL proxy config for {cc}?**\n\n"
            "This action cannot be undone.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^px_pool_del_yes_(\+\d+)$"))
    async def px_pool_del_yes_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_pool_del_yes_(\+\d+)$", str(query.data))
        if not m:
            return
        uid = query.from_user.id
        cc = m.group(1)
        removed = delete_country_proxy(cc)
        if removed:
            await query.answer()
            await _log_admin_action(uid, "proxy_pool_delete", message=cc)
        else:
            await query.answer("Nothing to remove", show_alert=True)
        await _edit_callback(query, _build_country_list(), reply_markup=_build_country_list_buttons())

    # ==================== Toggle single proxy ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_prx_toggle_(\+\d+)_(\d+)$"))
    async def px_prx_toggle_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_prx_toggle_(\+\d+)_(\d+)$", str(query.data))
        if not m:
            return
        uid = query.from_user.id
        cc, idx = m.group(1), _safe_int(m.group(2), -1)
        ok, msg = toggle_country_proxy_proxy(cc, idx)
        await query.answer()
        if ok:
            await _log_admin_action(uid, "proxy_prx_toggle", message=f"{cc}#{idx}")
        await _edit_callback(query, _build_manage_screen(cc), reply_markup=_build_manage_buttons(cc))

    # ==================== Delete single proxy ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_prx_del_(\+\d+)_(\d+)$"))
    async def px_prx_del_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_prx_del_(\+\d+)_(\d+)$", str(query.data))
        if not m:
            return
        cc = m.group(1)
        idx = _safe_int(m.group(2), -1)
        pool = get_country_proxies(cc)
        if idx < 0 or idx >= len(pool):
            await query.answer("❌ Proxy not found", show_alert=True)
            return
        masked = mask_proxy_url(pool[idx]["url"])
        await query.answer()
        # HIGH-16 FIX: this used to delete IMMEDIATELY by index with no
        # confirmation and no digest guard - a double-tap (or a shifted list)
        # silently removed a DIFFERENT proxy. Mirror the px_cdel flow: confirm
        # first, carry an 8-char digest of the exact url, verify before delete.
        buttons = [
            [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"px_prx_del_yes_{cc}_{idx}_{_url_digest(pool[idx]['url'])}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"px_manage_{cc}")],
        ]
        await _edit_callback(query,
            f"⚠️ **Delete Proxy #{idx + 1} for {cc}?**\n\n"
            f"`{masked}`\n\n"
            "Only this proxy will be removed. Cannot be undone.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^px_prx_del_yes_(\+\d+)_(\d+)_(.+)$"))
    async def px_prx_del_yes_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_prx_del_yes_(\+\d+)_(\d+)_(.+)$", str(query.data))
        if not m:
            return
        uid = query.from_user.id
        cc = m.group(1)
        try:
            idx = int(m.group(2))
        except (TypeError, ValueError):
            await query.answer("❌ Invalid proxy index", show_alert=True)
            return
        confirmed_digest = m.group(3)
        pool = get_country_proxies(cc)
        # TOCTOU guard: the entry at this index must STILL be the proxy the
        # admin confirmed. A shifted list (concurrent add/delete) refuses
        # instead of deleting a different proxy.
        if idx < 0 or idx >= len(pool):
            await query.answer("❌ Proxy not found (list changed) - reopen the screen", show_alert=True)
            return
        if _url_digest(pool[idx]["url"]) != confirmed_digest:
            await query.answer(
                "⚠️ List changed since you tapped Delete - the proxy at this position is now a "
                "different one. Nothing was deleted; reopen the screen and try again.",
                show_alert=True,
            )
            return
        ok, msg = remove_country_proxy(cc, idx)
        await query.answer(msg if not ok else "✅ Removed")
        if ok:
            await _log_admin_action(uid, "proxy_prx_del", message=f"{cc} #{idx + 1}")
        if get_country_proxy_config(cc) is not None:
            await _edit_callback(query, _build_manage_screen(cc), reply_markup=_build_manage_buttons(cc))
        else:
            await _edit_callback(query, _build_country_list(), reply_markup=_build_country_list_buttons())

    # ==================== Test proxies ====================

    @app.on_callback_query(admin_filter & filters.regex(r"^px_test_all$"))
    async def px_test_all_callback(client: Client, query: CallbackQuery):
        uid = query.from_user.id
        await query.answer("Testing...")
        all_urls = []

        default = get_default_proxy()
        if default:
            all_urls.append(("⚙️ Default", default))

        for cc in get_proxy_countries():
            pool = get_country_proxies(cc)
            for i, p in enumerate(pool):
                # Test EVERY pool entry (enabled or not) exactly like the live
                # per-country check, so a disabled proxy's status is visible.
                prefix = f"{cc} #{i + 1}"
                if not p.get("enabled"):
                    prefix = f"{cc} ⛔#{i + 1}"
                all_urls.append((prefix, p["url"]))

        if not all_urls:
            await _edit_callback(query,
                "🧪 **Proxy Test Results**\n\n"
                "No proxies configured to test.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ Back to Proxy System", callback_data="px_menu")]
                ])
            )
            return

        tasks = [_probe_one(label, url) for label, url in all_urls]
        raw = await asyncio.gather(*tasks)

        # Re-map to the "Reachable/Unreachable" wording of this screen.
        results = []
        for label, masked, status, detail in raw:
            if status.startswith("🟢"):
                results.append((label, "✅ Reachable", detail))
            else:
                results.append((label, "❌ Unreachable", detail))

        # P4-38: the output grows with the fleet (countries x 5 proxies) and
        # used to exceed Telegram's 4096-char edit limit (MESSAGE_TOO_LONG).
        # Bound it: summary header + per-entry lines up to a char budget,
        # overflow collapsed into a counter.
        ok_count = sum(1 for _, s, _ in results if s.startswith("✅"))
        fail_count = len(results) - ok_count
        budget = 3300  # leave room for header/buttons/escape margin
        lines = [
            "🧪 **Proxy Test Results**",
            f"Total: {len(results)} | ✅ {ok_count} | ❌ {fail_count}",
            "━━━━━━━━━━━━━━━━━━━━\n",
        ]
        hidden = 0
        for label, status, detail in results:
            line = f"{status} **{label}**\n   `{detail}`"
            if budget - len(line) < 120:
                hidden = len(results) - sum(1 for l in lines if l.startswith(("✅", "❌")))
                lines.append(f"…and {hidden} more (use the per-country Test button)")
                break
            lines.append(line)
            budget -= len(line) + 1
        lines.append("")

        await _log_admin_action(uid, "proxy_test_all", message=f"{len(results)} tested")
        await _edit_callback(query,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Test Again", callback_data="px_test_all")],
                [InlineKeyboardButton("◀ Back to Proxy System", callback_data="px_menu")],
            ])
        )

    @app.on_callback_query(admin_filter & filters.regex(r"^px_test_(\+\d+)$"))
    async def px_test_country_callback(client: Client, query: CallbackQuery):
        m = re.match(r"^px_test_(\+\d+)$", str(query.data))
        if not m:
            return
        cc = m.group(1)
        await query.answer("Testing...")
        pool = get_country_proxies(cc)
        if not pool:
            await _edit_callback(query,
                "🧪 **Proxy Test Results**\n\n"
                f"No proxies configured for {cc}.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ Back", callback_data=f"px_manage_{cc}")]
                ])
            )
            return

        tasks = []
        for i, p in enumerate(pool):
            prefix = f"#{i + 1}"
            if not p.get("enabled"):
                prefix = f"⛔#{i + 1}"
            tasks.append(_probe_one(prefix, p["url"], attribution=cc))
        raw = await asyncio.gather(*tasks)

        results = []
        for label, masked, status, detail in raw:
            if status.startswith("🟢"):
                results.append((label, "✅ Reachable", detail))
            else:
                results.append((label, "❌ Unreachable", detail))

        lines = [f"🧪 **Proxy Test — {cc}**", "━━━━━━━━━━━━━━━━━━━━\n"]
        for label, status, detail in results:
            lines.append(f"{status} {label}\n   `{detail}`")

        await _edit_callback(query,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Test Again", callback_data=f"px_test_{cc}")],
                [InlineKeyboardButton("◀ Back", callback_data=f"px_manage_{cc}")],
            ])
        )

    # ==================== TEXT INPUT HANDLER ====================

    @app.on_message(ADMIN_TEXT_FILTER)
    async def proxy_text_handler(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id not in admin_state:
            message.continue_propagation()
            return
        action = admin_state[user_id].get("action", "")
        if action not in _PROXY_TEXT_ACTIONS:
            message.continue_propagation()
            return

        message._handled_by_admin = True
        text = (message.text or "").strip()

        try:
            if action == "px_default_set":
                ok, parsed, err = parse_proxy_url(text)
                if not ok or parsed is None:
                    await _panel_reply(client, user_id, message,
                        f"❌ **Invalid proxy URL.**\n\n{err}\n\n"
                        "Format: `socks5://user:pass@host:port`\n"
                        "Type /admin to cancel."
                    )
                    return
                set_default_proxy(text)
                await _log_admin_action(user_id, "proxy_default_set", message=mask_proxy_url(text))
                await _panel_reply(client, user_id, message,
                    f"✅ **Default Proxy Set**\n\n"
                    f"`{mask_proxy_url(text)}`\n\n"
                    "This default now applies to every country without its own pool.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("◀ Back to Proxy System", callback_data="px_menu")]
                    ])
                )

            elif action == "px_country_add_url":
                cc = admin_state[user_id].get("country_code", "").strip()
                if not cc:
                    await _panel_reply(client, user_id, message,
                        "❌ Session expired. Please start again from Add Proxy to Country."
                    )
                    _admin_manager.clear_state(user_id)
                    return

                ok, msg = add_country_proxy(cc, text)
                if ok:
                    pool = get_country_proxies(cc)
                    name, flag = get_country_info(cc)
                    masked = mask_proxy_url(text)
                    await _log_admin_action(user_id, "proxy_add", message=f"{cc} {masked}")
                    buttons = [
                        [InlineKeyboardButton("🩺 Proxy Health Check", callback_data=f"px_chk_{cc}")],
                        [InlineKeyboardButton("📊 Check Your Proxy", callback_data=f"px_cstat_{cc}")],
                        [InlineKeyboardButton("◀ Back to Country", callback_data=f"px_csel_{cc}")],
                    ]
                    if len(pool) < PROXY_MAX_POOL:
                        buttons.insert(0, [
                            InlineKeyboardButton("➕ Add Another", callback_data=f"px_cadd_{cc}")
                        ])
                    await _panel_reply(client, user_id, message,
                        f"✅ **Proxy #{len(pool)} added for {flag} {name} ({cc})**\n\n"
                        f"`{masked}`\n\n"
                        f"Pool: {len(pool)}/{PROXY_MAX_POOL}\n\n"
                        "If this proxy fails, the next one is tried automatically.",
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                else:
                    # Invalid URL / full pool / duplicate: keep the state so the
                    # admin can simply paste a corrected URL to retry.
                    await _panel_reply(client, user_id, message,
                        f"{msg}\n\nType /admin to cancel."
                    )
                    return

            elif action == "px_pool_add":
                cc = admin_state[user_id].get("country_code", "").strip()
                if not cc:
                    await _panel_reply(client, user_id, message,
                        "❌ Session expired. Please start again from Add Proxy to Country."
                    )
                    _admin_manager.clear_state(user_id)
                    return

                ok, msg = add_country_proxy(cc, text)
                if ok:
                    pool = get_country_proxies(cc)
                    name, flag = get_country_info(cc)
                    masked = mask_proxy_url(text)
                    await _log_admin_action(
                        user_id, "proxy_add", message=f"{cc} {masked}"
                    )
                    # NOTE: _panel_reply must run BEFORE the state is cleared at
                    # the end of this handler, because it edits the panel via
                    # _get_panel_ref(user_id) which reads admin_state.
                    await _panel_reply(client, user_id, message,
                        f"✅ **Proxy added for {flag} {name} ({cc})**\n\n"
                        f"`{masked}`\n\n"
                        f"Pool: {len(pool)}/{PROXY_MAX_POOL}",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🌐 Manage Proxy", callback_data=f"px_manage_{cc}")]
                        ])
                    )
                else:
                    # Invalid URL / full pool / duplicate: keep the state so the
                    # admin can simply paste a corrected URL to retry.
                    await _panel_reply(client, user_id, message,
                        f"{msg}\n\nType /admin to cancel."
                    )
                    return

        except Exception as e:
            await _panel_reply(client, user_id, message, f"❌ Error: {str(e)}")
            _admin_manager.clear_state(user_id)
            return

        _admin_manager.clear_state(user_id)
