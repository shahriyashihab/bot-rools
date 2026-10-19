"""
Hybrid Proxy System - storage + resolution engine.
=====================================================================
Default + Override + Pool + Fallback proxy management.

The whole proxy configuration lives in ONE top-level ``_storage["proxy"]``
blob (persisted through the generic SQLite ``settings`` table, exactly like
every other global setting). Structure:

    {
        "enabled": True,                       # master switch (whole system)
        "default_proxy": "socks5://u:p@h:p",   # global default (optional)
        "countries": {                         # per-country override pools
            "+880": {
                "enabled": True,               # country pool master switch
                "proxies": [                   # ordered pool (max 5)
                    {"url": "socks5://...", "enabled": True},
                    ...
                ]
            }
        }
    }

Resolution rules (in order of precedence):
  1. Master switch OFF -> no proxy at all (direct connection, zero change).
  2. The country must be OPEN (capacity on). A closed country NEVER gets a
     proxy - the admin must open the country first (Country & Pricing).
  3. Walk the country fallback chain (exact code -> same-territory codes ->
     base code, e.g. +1876 -> +1) and use the first pool that is enabled and
     has at least one enabled proxy. The POOL OWNER country must itself be
     OPEN too: closing a country immediately stops its pool from serving
     other countries' sessions via the fallback chain.
  4. Otherwise fall back to the global default proxy (when set).
  5. Otherwise -> None (direct connection).

Supported proxy schemes: socks5 / socks4 / http (CONNECT). The ``https``
scheme (TLS-to-proxy) is NOT supported by the MTProto transport and is
rejected at validation time - see ``parse_proxy_url``.

This module is a LEAF for storage: it only touches ``_storage`` and the
country-data helpers, so it can be imported from the admin panel, the
verification pipeline and the monitor without circular-import risk.
"""

import os
import re
import copy
import logging
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from bot.data.state import _mark_settings_key_dirty, _storage, save_storage, register_settings_consistency_lock

logger = logging.getLogger(__name__)

# Maximum number of proxies allowed in one country pool.
PROXY_MAX_POOL = 5

# Kill-switch for the fail-open DIRECT fallback (P4-39). Default "1" preserves
# the historical behavior: when every proxy candidate died, the connect plan
# still ends with ``None`` (direct connection) as the last resort. When set to
# "0", ``get_connect_plan`` NEVER appends ``None`` - a plan with no remaining
# proxy candidates is returned EMPTY and the connector raises a clear
# "no proxies available" error, which every caller already treats as
# transient/inconclusive (never as an account verdict).
PROXY_ALLOW_DIRECT = os.getenv("PROXY_ALLOW_DIRECT", "1") != "0"

# Health counters (record_proxy_attempt / record_proxy_session_outcome) fire
# on EVERY connect attempt - 2+ calls per account, tens of thousands per day
# at scale. Persisting on every call was heavy write amplification. This
# throttle bounds the save FREQUENCY caused by health counters alone while
# staying NON-BLOCKING: save_storage() only arms the debounced background
# writer, so hot connect paths (which may run on the event loop) never wait
# on disk. A crash can lose the last interval of health statistics - an
# acceptable trade for informational counters (config and money persist
# synchronously through other paths).
_HEALTH_PERSIST_INTERVAL_SECONDS = 15.0
_health_persist_lock = threading.Lock()
_health_persist_last = [0.0]
_health_persist_timer: List[Optional[threading.Timer]] = [None]


def _persist_health_debounced() -> None:
    """Persist health counters at most once per interval (non-blocking)."""
    try:
        due = False
        now = time.time()
        with _health_persist_lock:
            if now - _health_persist_last[0] >= _HEALTH_PERSIST_INTERVAL_SECONDS:
                _health_persist_last[0] = now
                _health_persist_timer[0] = None
                due = True
            elif _health_persist_timer[0] is None:
                delay = max(0.5, _HEALTH_PERSIST_INTERVAL_SECONDS - (now - _health_persist_last[0]))
                timer = threading.Timer(delay, _persist_health_debounced)
                timer.daemon = True
                _health_persist_timer[0] = timer
                timer.start()
        if due:
            # S1: row-level settings persistence - mark ONLY the proxy key.
            _mark_settings_key_dirty("proxy")
            save_storage()
    except Exception:
        logger.debug("proxy health: throttled persist failed", exc_info=True)

__all__ = [
    "PROXY_MAX_POOL",
    "PROXY_ALLOW_DIRECT",
    "is_proxy_system_enabled",
    "set_proxy_system_enabled",
    "get_default_proxy",
    "set_default_proxy",
    "clear_default_proxy",
    "get_proxy_countries",
    "get_country_proxy_config",
    "get_country_proxies",
    "set_country_proxy",
    "add_country_proxy",
    "remove_country_proxy",
    "toggle_country_proxy_proxy",
    "toggle_country_proxy",
    "delete_country_proxy",
    "parse_proxy_url",
    "mask_proxy_url",
    "breaker_cooldown_remaining",
    "is_proxy_applicable",
    "get_proxy_connect_candidates",
    "resolve_proxy_for_country",
    "record_proxy_attempt",
    "record_proxy_session_outcome",
    "get_country_proxy_health",
    "get_country_proxy_session_stats",
    "get_country_proxy_health_summary",
    "proxy_display_key",
    "get_connect_plan",
    "is_proxy_system_down",
    "set_proxy_system_down",
]

# socks5://user:pass@host:port  |  http://host:port  |  [ipv6] support
#
# Scheme policy: only schemes the MTProto transport (Pyrogram + PySocks) can
# actually dial are accepted. PySocks exposes exactly SOCKS5 / SOCKS4 / HTTP
# proxy types - there is NO HTTPS type - so ``https://`` (TLS-to-proxy) is
# rejected at validation time instead of failing at every real connect.
_PROXY_URL_RE = re.compile(
    r"^(?P<scheme>socks5|socks4|http)://"
    r"(?:(?P<user>[^:@/\s]+):(?P<pass>[^@/\s]*)@)?"
    r"(?P<host>\[[^\]]+\]|[^:@/\s]+)"
    r":(?P<port>\d{1,5})$",
    re.IGNORECASE,
)

# Lenient scheme extractor for entries already inside a stored pool (legacy
# migration checks): anything not matching a supported scheme is dropped.
_URL_SCHEME_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9+.-]*)://")

# In-memory round-robin cursor per matched country code. NOT persisted: a
# restart simply continues from the pool head, which is perfectly fine.
# Mutated ONLY while holding ``_PROXY_BLOB_LOCK`` so a rotation can never be
# lost to a concurrent connect or produce an out-of-range cursor when the
# pool size changes.
_RR_CURSOR: Dict[str, int] = {}

# Serializes EVERY mutation of the proxy blob (``_storage["proxy"]``: config
# AND health) against:
#   1. other proxy-blob mutators (async event loop + recovery worker thread +
#      debounce save timer can all reach these paths concurrently), and
#   2. the persistence snapshot: the blob is json-serialized for the SQLite
#      settings row on a TIMER thread while worker threads mutate nested
#      dicts. Without a shared lock "dictionary changed size during
#      iteration" aborts the whole save cycle. ``register_settings_
#      consistency_lock`` hands this lock to bot.data.state so the settings
#      serializer holds it while dumping the "proxy" key (single lock domain
#      for mutate-vs-serialize; the lock is an RLock so locked mutators can
#      call locked helpers re-entrantly).
# NOTE: ``entry["ok"] = entry.get("ok", 0) + 1`` is a three-step non-atomic
# sequence (the GIL only makes single dict ops atomic) - the lock is what
# makes the read-modify-write safe.
_PROXY_BLOB_LOCK = threading.RLock()

# Wire the persistence layer to the same lock domain (no circular import:
# state.py never imports proxies at module level).
register_settings_consistency_lock("proxy", _PROXY_BLOB_LOCK)

# Maximum number of tracked proxy keys kept per country per map (attempts /
# sessions). Health data grows every time the admin rotates a proxy in/out, so
# without a cap the blob grows forever. The most recently touched entries win;
# older ones are pruned.
_HEALTH_MAX_KEYS_PER_COUNTRY = 50


# ------------------------------------------------------------------
# Low-level helpers
# ------------------------------------------------------------------

def _get_root() -> Dict[str, Any]:
    """Return the normalized proxy config root (never None)."""
    with _PROXY_BLOB_LOCK:
        root = _storage.get("proxy")
        if not isinstance(root, dict):
            root = {}
            _storage["proxy"] = root
        return root


def _get_countries_root(root: Dict[str, Any]) -> Dict[str, Any]:
    """Return the normalized countries sub-root (caller holds the blob lock)."""
    countries = root.get("countries")
    if not isinstance(countries, dict):
        countries = {}
        root["countries"] = countries
    return countries


def _normalize_pool(pool: Any) -> List[Dict[str, Any]]:
    """Coerce any stored pool into a clean list of {url, enabled} dicts.

    Migration guard: entries whose scheme the MTProto transport cannot dial
    (e.g. legacy ``https://`` rows written before validation rejected the
    scheme) are dropped once here, so resolution never even considers a
    proxy that is guaranteed to fail at connect time.
    """
    if not isinstance(pool, list):
        return []
    clean: List[Dict[str, Any]] = []
    for entry in pool:
        if not (isinstance(entry, dict) and entry.get("url")):
            continue
        url = str(entry.get("url", "")).strip()
        scheme_m = _URL_SCHEME_RE.match(url)
        scheme = scheme_m.group(1).lower() if scheme_m else ""
        if scheme not in ("socks5", "socks4", "http"):
            # Never log the URL itself (it carries credentials).
            logger.warning(
                f"Proxy pool: dropped entry with unsupported scheme '{scheme or '?'}' "
                "(cannot be dialed by the MTProto transport)"
            )
            continue
        clean.append({"url": url, "enabled": bool(entry.get("enabled", True))})
    if len(clean) > PROXY_MAX_POOL:
        # P4-37: NEVER truncate silently. Every dropped entry is logged with
        # its MASKED url so the admin can see exactly what fell off the pool;
        # add_country_proxy already refuses to exceed the cap, so this only
        # fires for a legacy/oversized stored pool.
        for dropped in clean[PROXY_MAX_POOL:]:
            logger.warning(
                f"Proxy pool: entry beyond the {PROXY_MAX_POOL}-proxy cap is INACTIVE "
                f"and will not be dialed: {mask_proxy_url(dropped.get('url', ''))}"
            )
        logger.warning(
            f"Pool has {len(clean)} entries, capping the DIAL list to {PROXY_MAX_POOL} - "
            "remove proxies or delete the country config to make room."
        )
    return clean[:PROXY_MAX_POOL]


def _persist() -> None:
    """Persist the proxy config blob (thread-safe via storage's own lock)."""
    # S1: mark ONLY the proxy settings key (row-level persistence) - the old
    # blanket settings mark re-serialized every settings blob on every proxy
    # config change.
    _mark_settings_key_dirty("proxy")
    save_storage()


# ------------------------------------------------------------------
# Master switch / default proxy
# ------------------------------------------------------------------

def is_proxy_system_enabled() -> bool:
    """Master switch of the whole proxy system."""
    with _PROXY_BLOB_LOCK:
        root = _storage.get("proxy")
        if not isinstance(root, dict):
            return False
        return bool(root.get("enabled", False))


def set_proxy_system_enabled(enabled: bool) -> bool:
    """Persist the master switch. Returns the new state."""
    with _PROXY_BLOB_LOCK:
        _get_root()["enabled"] = bool(enabled)
        _persist()
    return bool(enabled)


def get_default_proxy() -> str:
    """Global default proxy URL ('' when unset)."""
    with _PROXY_BLOB_LOCK:
        root = _storage.get("proxy")
        if not isinstance(root, dict):
            return ""
        return str(root.get("default_proxy", "") or "").strip()


def set_default_proxy(url: str) -> bool:
    """Set the global default proxy URL after validating it.

    Returns True on success, False when the URL is invalid (nothing is saved).
    """
    ok, parsed, _ = parse_proxy_url(url)
    if not ok or parsed is None:
        return False
    with _PROXY_BLOB_LOCK:
        _get_root()["default_proxy"] = str(url).strip()
        _persist()
    return True


def clear_default_proxy() -> bool:
    """Remove the global default proxy. Returns True when one was removed."""
    with _PROXY_BLOB_LOCK:
        root = _get_root()
        if root.get("default_proxy"):
            root["default_proxy"] = ""
            _persist()
            return True
        return False


# ------------------------------------------------------------------
# Per-country pools
# ------------------------------------------------------------------

def get_proxy_countries() -> List[str]:
    """Country codes that have a stored proxy configuration (exact codes)."""
    with _PROXY_BLOB_LOCK:
        countries = _get_countries_root(_get_root())
        return sorted(k for k in countries.keys() if isinstance(k, str) and k)


def get_country_proxy_config(country_code: str) -> Optional[Dict[str, Any]]:
    """Return a SNAPSHOT of the raw config dict for the EXACT country code.

    The returned dict is a normalized copy (mutating it never touches
    storage); taken under the blob lock so a concurrent mutation can never
    yield a half-updated view.
    """
    if not country_code:
        return None
    with _PROXY_BLOB_LOCK:
        countries = _get_countries_root(_get_root())
        cfg = countries.get(country_code)
        if not isinstance(cfg, dict):
            return None
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "proxies": _normalize_pool(cfg.get("proxies")),
        }


def get_country_proxies(country_code: str) -> List[Dict[str, Any]]:
    """Normalized proxy list for the EXACT country code ([] when none)."""
    cfg = get_country_proxy_config(country_code)
    return cfg["proxies"] if cfg else []


def set_country_proxy(country_code: str, url: str) -> Tuple[bool, str]:
    """Replace a country's pool with a single validated proxy.

    If the country does not exist in the board's country database, or is not
    open (capacity OFF), nothing is saved - the proxy can only be opened for
    an OPEN country. Returns ``(ok, message)``.
    """
    gate = _proxy_country_gate(country_code)
    if gate is not None:
        return False, gate

    ok, parsed, err = parse_proxy_url(url)
    if not ok or parsed is None:
        return False, f"❌ Invalid proxy URL: {err}"

    with _PROXY_BLOB_LOCK:
        countries = _get_countries_root(_get_root())
        countries[country_code] = {
            "enabled": True,
            "proxies": [{"url": str(url).strip(), "enabled": True}],
        }
        _persist()
    return True, ""


def add_country_proxy(country_code: str, url: str) -> Tuple[bool, str]:
    """Append a validated proxy to a country's pool (max ``PROXY_MAX_POOL``).

    Same open-country gate as :func:`set_country_proxy`. Returns
    ``(ok, message)``.
    """
    gate = _proxy_country_gate(country_code)
    if gate is not None:
        return False, gate

    ok, parsed, err = parse_proxy_url(url)
    if not ok or parsed is None:
        return False, f"❌ Invalid proxy URL: {err}"

    with _PROXY_BLOB_LOCK:
        countries = _get_countries_root(_get_root())
        cfg = countries.get(country_code)
        if not isinstance(cfg, dict):
            cfg = {"enabled": True, "proxies": []}
            countries[country_code] = cfg
        pool = _normalize_pool(cfg.get("proxies"))
        if len(pool) >= PROXY_MAX_POOL:
            return False, f"❌ Max {PROXY_MAX_POOL} proxies allowed per country. Remove one first."
        clean_url = str(url).strip()
        # Canonical duplicate detection: two URLs that resolve to the SAME
        # endpoint with the SAME credentials (differing only by scheme/host
        # case or percent-encoding) must not both be added - they would be
        # round-robined against themselves. Credentials differing in case are
        # NOT duplicates.
        ok, parsed_new, _ = parse_proxy_url(clean_url)
        if ok and parsed_new is not None:
            new_identity = _canonical_identity(parsed_new)
            for existing in pool:
                e_ok, e_parsed, _ = parse_proxy_url(existing["url"])
                if e_ok and e_parsed is not None and _canonical_identity(e_parsed) == new_identity:
                    return False, "⚠️ This proxy URL already exists for this country."
        elif any(p["url"] == clean_url for p in pool):
            return False, "⚠️ This proxy URL already exists for this country."
        pool.append({"url": clean_url, "enabled": True})
        cfg["proxies"] = pool
        cfg["enabled"] = True
        _persist()
    return True, ""


def remove_country_proxy(country_code: str, index: int) -> Tuple[bool, str]:
    """Remove one proxy from a country pool by 0-based index."""
    with _PROXY_BLOB_LOCK:
        cfg = get_country_proxy_config(country_code)
        if cfg is None:
            return False, "❌ No proxy config for this country."
        pool = cfg["proxies"]
        if index < 0 or index >= len(pool):
            return False, "❌ Proxy index out of range."
        countries = _get_countries_root(_get_root())
        if country_code not in countries:
            return False, "❌ No proxy config for this country."
        new_pool = [p for i, p in enumerate(pool) if i != index]
        countries[country_code]["proxies"] = new_pool
        if not new_pool:
            # An empty pool means the country has no working proxy: remove the
            # whole config so it never shows as "configured".
            del countries[country_code]
            _drop_country_health(country_code)
        _persist()
    return True, ""


def toggle_country_proxy_proxy(country_code: str, index: int) -> Tuple[bool, str]:
    """Enable/disable ONE proxy inside a country pool."""
    with _PROXY_BLOB_LOCK:
        cfg = get_country_proxy_config(country_code)
        if cfg is None:
            return False, "❌ No proxy config for this country."
        pool = cfg["proxies"]
        if index < 0 or index >= len(pool):
            return False, "❌ Proxy index out of range."
        countries = _get_countries_root(_get_root())
        if country_code not in countries:
            return False, "❌ No proxy config for this country."
        # Write the normalized pool back (with the toggle applied): display
        # and storage stay identical even when a legacy entry was dropped by
        # normalization, so an index can never point at the wrong proxy.
        new_pool = [
            {"url": p["url"], "enabled": (not p["enabled"]) if i == index else p["enabled"]}
            for i, p in enumerate(pool)
        ]
        countries[country_code]["proxies"] = new_pool
        _persist()
    return True, ""


def toggle_country_proxy(country_code: str) -> Tuple[bool, str]:
    """Enable/disable the whole proxy pool of a country.

    Enabling is gated on the country being open: a closed country's proxy can
    never be turned back on until the admin opens the country first.
    """
    with _PROXY_BLOB_LOCK:
        cfg = get_country_proxy_config(country_code)
        if cfg is None:
            return False, "❌ No proxy config for this country."
        new_state = not cfg["enabled"]
        if new_state:
            gate = _proxy_country_gate(country_code)
            if gate is not None:
                return False, gate
        countries = _get_countries_root(_get_root())
        countries[country_code]["enabled"] = new_state
        _persist()
    return True, ""


def delete_country_proxy(country_code: str) -> bool:
    """Remove ALL proxy config of a country. Returns True when removed."""
    with _PROXY_BLOB_LOCK:
        countries = _get_countries_root(_get_root())
        if country_code in countries:
            del countries[country_code]
            _drop_country_health(country_code)
            _persist()
            return True
        return False


def _proxy_country_gate(country_code: str) -> Optional[str]:
    """The capacity gate: a proxy can only be opened for an OPEN country.

    Returns None when the country may receive a proxy, otherwise a friendly
    error message explaining what the admin must do first.
    """
    from bot.data.countries import is_country_open

    if not country_code:
        return "❌ Country code is required (e.g. `+880`)."
    if not is_country_open(country_code):
        return (
            f"❌ **{country_code} is not open (capacity is OFF).**\n\n"
            "A proxy can only be opened for a country that is already open.\n"
            "1) Go to 🌍 Country & Pricing\n"
            "2) Open the country (Set Country Price / Toggle Country)\n"
            "3) Then add the proxy here."
        )
    return None


# -------------------------------
# Circuit-breaker for dead proxies
# (H1 — auto-skip after consecutive failures)
# -------------------------------
_BREAKER_FAIL_THRESHOLD = 3
_BREAKER_COOLDOWN_SECONDS = 600


def _is_breaker_open(country_attribution: str, proxy: Optional[Dict[str, Any]]) -> bool:
    """True when the proxy is in breaker cooldown and should be skipped."""
    return breaker_cooldown_remaining(country_attribution, proxy) > 0


def breaker_cooldown_remaining(country_attribution: str, proxy: Optional[Dict[str, Any]]) -> float:
    """Seconds of breaker cooldown LEFT for this proxy (0 when not open).

    P4-40: the connector uses this to give a breaker-open candidate (only
    present in the plan during self-healing retries) a SHORT budget instead
    of the full per-proxy timeout.
    """
    if not proxy:
        return 0.0
    with _PROXY_BLOB_LOCK:
        health = _get_health_root().get(country_attribution, {})
        attempts = health.get("attempts") or {}
        key = _proxy_key(proxy)
        entry = attempts.get(key)
        if not entry:
            return 0.0
        try:
            consec = int(entry.get("consecutive_fail", 0) or 0)
        except (TypeError, ValueError):
            consec = 0
        if consec < _BREAKER_FAIL_THRESHOLD:
            return 0.0
        last_ts = entry.get("last_fail_ts", "")
        if not last_ts:
            return 0.0
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last_ts)).total_seconds()
        except Exception:
            return 0.0
        remaining = _BREAKER_COOLDOWN_SECONDS - elapsed
        return max(0.0, remaining)



# ------------------------------------------------------------------
# Proxy URL parsing (Pyrogram-compatible)
# ------------------------------------------------------------------

def parse_proxy_url(url: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """Validate a proxy URL and convert it to a Pyrogram ``proxy`` dict.

    Supported formats (schemes: socks5 / socks4 / http):
        socks5://user:pass@host:port
        socks5://host:port
        http://user:pass@host:port

    The ``https://`` scheme is intentionally REJECTED: the MTProto transport
    (PySocks) has no TLS-to-proxy proxy type, so an https proxy can never
    complete a real connect. Accepting it would only produce proxies that
    pass validation yet fail at every transport handshake.

    Returns ``(ok, parsed_dict_or_None, error_message)``.
    """
    if not url or not isinstance(url, str):
        return False, None, "URL cannot be empty."
    url = url.strip()
    if " " in url:
        return False, None, "URL must not contain spaces."

    match = _PROXY_URL_RE.match(url)
    if not match:
        scheme_m = _URL_SCHEME_RE.match(url)
        if scheme_m and scheme_m.group(1).lower() == "https":
            return (
                False,
                None,
                "The `https` proxy scheme is not supported.\n"
                "The MTProto transport cannot dial TLS-to-proxy endpoints.\n"
                "Use plain `http://` (CONNECT) or `socks5://` instead.",
            )
        return (
            False,
            None,
            "Format: `scheme://[user:pass@]host:port`\n"
            "Example: `socks5://user:pass@1.2.3.4:1080`",
        )

    scheme = match.group("scheme").lower()
    host = match.group("host").strip("[]")
    port = int(match.group("port"))
    user = match.group("user")
    password = match.group("pass")

    if not (1 <= port <= 65535):
        return False, None, "Port must be between 1 and 65535."

    parsed: Dict[str, Any] = {
        "scheme": scheme,
        "hostname": host,
        "port": port,
    }
    if user is not None:
        # Percent-decoded credentials: proxy passwords routinely contain
        # reserved characters (@, /, :) that cannot appear raw in a URL. The
        # admin encodes them (user%40x, p%2Fss) and Pyrogram receives the
        # decoded bytes, which is what the proxy protocol actually wants.
        parsed["username"] = urllib.parse.unquote(user)
        parsed["password"] = urllib.parse.unquote(password or "")
    return True, parsed, ""


def _render_host(host: str) -> str:
    """Render a host for display/keys, keeping IPv6 addresses bracketed."""
    host = host or ""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def mask_proxy_url(url: str) -> str:
    """Display a proxy URL without leaking its password.

    ``socks5://user:****@host:port`` / ``socks5://host:port`` (IPv6 hosts are
    bracketed so the port is unambiguous).

    When the URL cannot be parsed (corrupt/legacy entry) a best-effort
    regex-based masking is applied so the raw password NEVER reaches the UI.
    """
    if not url:
        return "—"
    ok, parsed, _ = parse_proxy_url(url)
    if ok and parsed is not None:
        host = _render_host(parsed["hostname"])
        port = parsed["port"]
        scheme = parsed["scheme"]
        if "username" in parsed:
            return f"{scheme}://{parsed['username']}:****@{host}:{port}"
        return f"{scheme}://{host}:{port}"
    return _mask_unparseable_url(url)


_AUTH_RE = re.compile(r"^(?P<prefix>[A-Za-z][A-Za-z0-9+.-]*://)(?P<user>[^@/:?\s]+)(?::(?P<pass>[^@/?\s]*))?@(?P<rest>.+)$")


def _mask_unparseable_url(url: str) -> str:
    """Best-effort credential masking for URLs that fail strict parsing.

    Guarantees: the password substring (and any ``:pass@`` segment) is
    replaced with ``****``; when even the structure is unrecognizable, the
    whole string collapses to a safe `<unparseable-proxy>` placeholder.
    """
    m = _AUTH_RE.match(url.strip())
    if m and m.group("pass") is not None:
        return f"{m.group('prefix')}{m.group('user')}:****@{m.group('rest')}"
    if m:
        return url.strip()
    if "@" in url:
        # Unknown shape but carries credentials - do not echo it back.
        return "<unparseable-proxy>"
    return url.strip()


# ------------------------------------------------------------------
# Resolution engine
# ------------------------------------------------------------------

def _matched_pool(country_code: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Find the first enabled pool along the country's fallback chain.

    The POOL OWNER country must itself be OPEN: a country that the admin
    closed must stop serving traffic immediately, including traffic for
    other countries that used to fall back to it (e.g. closing +1 must stop
    +1876 sessions from riding the +1 pool).

    Returns ``(matched_code, normalized_pool_config)`` or None.
    """
    if not country_code:
        return None
    from bot.data.countries import get_country_fallback_chain, is_country_open

    for code in get_country_fallback_chain(country_code):
        if not is_country_open(code):
            # The pool owner is closed (or unknown to the board): its proxy
            # config is preserved but never activated - same invariant as the
            # add/toggle gate.
            continue
        cfg = get_country_proxy_config(code)
        if cfg is None:
            continue
        enabled_proxies = [p for p in cfg["proxies"] if p.get("enabled")]
        if cfg["enabled"] and enabled_proxies:
            return code, cfg
    return None


def is_proxy_applicable(country_code: str) -> bool:
    """True when a proxy MAY be applied to this country right now.

    Both the master switch AND the country's open (capacity) state must be on.
    """
    if not is_proxy_system_enabled():
        return False
    if not country_code:
        return False
    from bot.data.countries import is_country_open

    return is_country_open(country_code)


def get_connect_plan(country_code: str, preferred_url: Optional[str] = None) -> Tuple[str, List[Optional[Dict[str, Any]]]]:
    """Full connect plan: ``(attribution_country, candidates)``.

    ``attribution_country`` is the code under which health/breaker stats must
    be recorded for this plan. When a country pool matched, that is the POOL
    OWNER code (a +1876 session routed through a +1 pool is attributed to +1,
    so the admin sees the usage on the configured country page). When only the
    default proxy applies, the requested country is used.

    ``preferred_url`` (P4-41): the per-session PINNED proxy. When provided,
    parseable and not breaker-open, the plan is exactly ``[that_proxy, None]``
    (stick to the proxy that handled the login, direct as last resort when the
    operator allows it) and the round-robin cursor is NOT advanced, so later
    phases of the SAME session reuse the same proxy instead of re-rolling the
    pool. A dead (breaker-open) or unparseable pin is ignored and resolution
    falls through to the normal pool path - a dead pin never blocks the flow.

    ``PROXY_ALLOW_DIRECT=0`` removes the trailing ``None`` everywhere; a plan
    with no proxy candidates left is returned EMPTY so the connector raises a
    clear "no proxies available" error (callers treat it as transient).
    """
    attribution = country_code or ""
    if not is_proxy_applicable(country_code):
        # System unused / country closed: direct exactly as before, unless the
        # operator forbade direct connections altogether (strict mode).
        return attribution, ([] if not PROXY_ALLOW_DIRECT else [None])

    candidates: List[Optional[Dict[str, Any]]] = []
    seen: set = set()

    if preferred_url:
        ok, parsed, _ = parse_proxy_url(preferred_url)
        if ok and parsed is not None and not _is_breaker_open(attribution, parsed):
            if PROXY_ALLOW_DIRECT:
                return attribution, [parsed, None]
            return attribution, [parsed]

    # Snapshot the round-robin rotation under the blob lock (the cursor and
    # pool are both part of the proxy state), then do the parsing/circuit-
    # breaker work outside the lock.
    with _PROXY_BLOB_LOCK:
        matched = _matched_pool(country_code)
        if matched is not None:
            matched_code, cfg = matched
            attribution = matched_code
            pool = [p for p in cfg["proxies"] if p.get("enabled")]
            if pool:
                cursor = _RR_CURSOR.get(matched_code, 0)
                if cursor >= len(pool):
                    cursor = 0
                _RR_CURSOR[matched_code] = (cursor + 1) % len(pool)
                ordered = pool[cursor:] + pool[:cursor]

    if matched is not None and pool:
        usable = []
        for p in ordered:
            ok, parsed, _ = parse_proxy_url(p["url"])
            if ok and parsed is not None:
                key = _candidate_key(parsed)
                if key not in seen:
                    seen.add(key)
                    usable.append(parsed)
        # Circuit breaker: drop proxies in cooldown, but never drop ALL of
        # them (that would force permanent direct connections and the dead
        # ones would never be retried/self-healed).
        active = [p for p in usable if not _is_breaker_open(attribution, p)]
        candidates = active if active else usable

    default_url = get_default_proxy()
    if default_url:
        ok, parsed, _ = parse_proxy_url(default_url)
        if ok and parsed is not None:
            key = _candidate_key(parsed)
            if key not in seen:
                seen.add(key)
                if not _is_breaker_open(attribution, parsed):
                    candidates.append(parsed)
                else:
                    # A breaker-open default still participates when it is the
                    # ONLY candidate (self-healing retry).
                    if not candidates:
                        candidates.append(parsed)

    if candidates:
        if PROXY_ALLOW_DIRECT:
            candidates.append(None)  # fail-open: direct connection as last resort
    else:
        # No proxy candidates remain. Default mode keeps the historical direct
        # fallback; PROXY_ALLOW_DIRECT=0 returns an EMPTY plan so the connector
        # raises a clear "no proxies available" error (transient for callers).
        candidates = [None] if PROXY_ALLOW_DIRECT else []

    return attribution, candidates


def _candidate_key(parsed: Dict[str, Any]) -> tuple:
    """Dedupe identity INCLUDING the password (two entries that differ only by
    credentials are different proxies and must both be tried)."""
    return (
        parsed.get("scheme"),
        parsed.get("hostname"),
        parsed.get("port"),
        parsed.get("username"),
        parsed.get("password"),
    )


def _canonical_identity(parsed: Dict[str, Any]) -> tuple:
    """Case/encoding-insensitive identity for DUPLICATE detection only.

    Hostnames and schemes are case-insensitive in practice (DNS), so two URLs
    that differ only by scheme/host casing or percent-encoding of the SAME
    credentials describe the same proxy endpoint. Credentials themselves stay
    case-sensitive (``user:Pass`` vs ``user:pass`` can be two different proxy
    accounts on the same host, so both are allowed).
    """
    return (
        str(parsed.get("scheme") or "").lower(),
        str(parsed.get("hostname") or "").lower(),
        parsed.get("port"),
        parsed.get("username"),
        parsed.get("password"),
    )


def get_proxy_connect_candidates(country_code: str) -> List[Optional[Dict[str, Any]]]:
    """Ordered candidate list for connecting a session of ``country_code``.

    Order: enabled pool proxies (starting at the round-robin cursor, with
    breaker-open proxies skipped) -> the global default proxy -> ``None``
    (direct connection) as the final fail-open safety net. When the proxy
    system is off / the country is closed / nothing is configured, the list
    contains ONLY ``None`` - i.e. exactly today's behaviour, so an unused
    proxy system cannot change anything.

    The trailing ``None`` is only added when at least one proxy candidate
    exists (it is the documented "all proxies dead" fallback). When no proxy
    is configured the caller simply connects directly.
    """
    _, candidates = get_connect_plan(country_code)
    return candidates


def resolve_proxy_for_country(country_code: str) -> Optional[Dict[str, Any]]:
    """Best single proxy dict for a country right now (rotation applied).

    Returns a Pyrogram ``proxy`` dict or None (direct connection).
    """
    candidates = get_proxy_connect_candidates(country_code)
    for c in candidates:
        if c is not None:
            return c
    return None


# ------------------------------------------------------------------
# Proxy health tracking (connection attempts + session outcomes)
# ------------------------------------------------------------------
# Live usage is recorded into ``_storage["proxy"]["health"]`` so the admin
# panel can answer two questions per country:
#
#   1. Proxy Health Check -> how reliable is the proxy when the bot tries
#      to connect / send a code (ok vs fail per masked proxy URL).
#   2. Check Your Proxy    -> what happened to the sessions that used this
#      country's proxy (success / rejected / multi-device).
#
# Storage shape (all keys optional, created lazily):
#
#     "health": {
#         "+880": {
#             "attempts": {
#                 "socks5://user:****@1.2.3.4:1080": {
#                     "ok": 10, "fail": 2, "last_ts": "2026-09-02T10:00:00"
#                 }
#             },
#             "sessions": {
#                 "socks5://user:****@1.2.3.4:1080": {
#                     "success": 5, "rejected": 1, "multi_device": 2, "total": 8
#                 }
#             }
#         }
#     }
#
# The tracking is best-effort and fire-and-forget: a failure to record must
# NEVER break the connection pipeline, so every public record_* helper
# swallows its own exceptions.
# ------------------------------------------------------------------

def _get_health_root() -> Dict[str, Any]:
    """Return the normalized health sub-root inside the proxy blob."""
    with _PROXY_BLOB_LOCK:
        root = _get_root()
        health = root.get("health")
        if not isinstance(health, dict):
            health = {}
            root["health"] = health
        return health


def _drop_country_health(country_code: str) -> None:
    """Remove ALL tracked health data of a country (attempts + sessions).

    Called when a country's proxy config is fully removed so deleted proxies
    never leave orphaned stats behind. Never raises.
    """
    try:
        with _PROXY_BLOB_LOCK:
            health = _get_health_root()
            if country_code in health:
                del health[country_code]
    except Exception:
        logger.debug("proxy health: drop failed", exc_info=True)


def _prune_health_map(cc_data: Dict[str, Any], map_name: str) -> None:
    """Cap one per-country map (``attempts`` / ``sessions``) to the newest keys.

    Keys without a usable timestamp sort last so they are the first evicted.
    Breaker-open entries in the ``attempts`` map are PRESERVED even when they
    sort old: evicting one would silently reset its consecutive-failure
    counter and a dead proxy would be retried immediately instead of staying
    in cooldown. Never raises (fire-and-forget, like the rest of health
    tracking).
    """
    try:
        mapping = cc_data.get(map_name)
        if not isinstance(mapping, dict):
            return
        if len(mapping) <= _HEALTH_MAX_KEYS_PER_COUNTRY:
            return

        def _sort_key(item):
            entry = item[1]
            ts = ""
            if isinstance(entry, dict):
                ts = entry.get("last_ts") or entry.get("last_fail_ts") or ""
            return ts

        # Preserve circuit-breaker state across pruning: any attempts entry
        # that is currently in breaker cooldown must survive the cap.
        preserved: Dict[str, Any] = {}
        if map_name == "attempts":
            now = datetime.now()
            for key, entry in mapping.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    consec = int(entry.get("consecutive_fail", 0) or 0)
                except (TypeError, ValueError):
                    consec = 0
                if consec < _BREAKER_FAIL_THRESHOLD:
                    continue
                last_fail = entry.get("last_fail_ts") or ""
                try:
                    within_cooldown = (
                        now - datetime.fromisoformat(last_fail)
                    ) < timedelta(seconds=_BREAKER_COOLDOWN_SECONDS)
                except (TypeError, ValueError):
                    within_cooldown = False
                if within_cooldown:
                    preserved[key] = entry

        ordered = sorted(mapping.items(), key=_sort_key, reverse=True)
        keep = dict(ordered[:_HEALTH_MAX_KEYS_PER_COUNTRY])
        for key, entry in preserved.items():
            if key not in keep:
                keep[key] = entry
        cc_data[map_name] = keep
    except Exception:
        logger.debug("proxy health: prune failed", exc_info=True)


def _proxy_key(proxy: Optional[Dict[str, Any]]) -> str:
    """Stable breaker/health key for a parsed proxy dict.

    P4-35: the masked URL alone is AMBIGUOUS - two pool entries that differ
    only by password (two accounts on the same host) produced the SAME key,
    so one bad credential opened the breaker for the healthy neighbour. The
    key therefore carries a short digest of the FULL identity (including the
    password) after the masked prefix: still human-readable, but no collision.
    Legacy keys (without the digest) simply age out of the health map.
    """
    if not proxy:
        return ""
    scheme = str(proxy.get("scheme") or "socks5")
    host = _render_host(str(proxy.get("hostname") or "?"))
    port = proxy.get("port") or 0
    if "username" in proxy:
        masked = f"{scheme}://{proxy['username']}:****@{host}:{port}"
    else:
        masked = f"{scheme}://{host}:{port}"
    try:
        import hashlib
        identity = "|".join(
            str(proxy.get(k) or "") for k in ("scheme", "hostname", "port", "username", "password")
        )
        digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:8]
        return f"{masked}#{digest}"
    except Exception:
        return masked


def proxy_display_key(proxy: Optional[Dict[str, Any]]) -> str:
    """Public masked key for a parsed proxy dict, or ``""`` for None."""
    return _proxy_key(proxy)


def record_proxy_attempt(country_code: str, proxy: Optional[Dict[str, Any]], success: bool, from_probe: bool = False) -> None:
    """Record one connection attempt (success or failure) for a country proxy.

    Called from the connector choke point for every proxy candidate tried.
    ``country_code`` is the ATTRIBUTION code (pool-owner code when the
    candidate came from a matched pool, otherwise the session country code).
    Fire-and-forget: never raises.

    ``from_probe=True`` marks a WATCHDOG PROBE result (P4-43): the health
    counters (ok/fail, last_ts) still update and a successful probe resets the
    consecutive-failure streak, but a probe FAILURE does NOT increment
    ``consecutive_fail`` - probes alone can never open the circuit breaker for
    a healthy-but-slow proxy. Only live traffic failures open the breaker.

    Thread-safe: the read-modify-write on the health dict is serialized under
    the proxy-blob lock (the SAME lock the persistence snapshot holds while
    serializing the settings row), so concurrent callers cannot lose counter
    increments and the JSON serializer can never observe a half-mutated map.
    """
    try:
        if not country_code or not proxy:
            return
        key = _proxy_key(proxy)
        if not key:
            return
        with _PROXY_BLOB_LOCK:
            health = _get_health_root()
            cc_data = health.setdefault(country_code, {})
            attempts = cc_data.setdefault("attempts", {})
            entry = attempts.setdefault(key, {"ok": 0, "fail": 0, "consecutive_fail": 0, "last_ts": "", "last_fail_ts": ""})
            if success:
                entry["ok"] = int(entry.get("ok", 0)) + 1
                entry["consecutive_fail"] = 0
            else:
                entry["fail"] = int(entry.get("fail", 0)) + 1
                if not from_probe:
                    entry["consecutive_fail"] = int(entry.get("consecutive_fail", 0) or 0) + 1
                    entry["last_fail_ts"] = datetime.now().isoformat(timespec="seconds")
            entry["last_ts"] = datetime.now().isoformat(timespec="seconds")
            _prune_health_map(cc_data, "attempts")
        _persist_health_debounced()
    except Exception:
        logger.debug("proxy health: attempt recording failed", exc_info=True)


def record_proxy_session_outcome(attribution_code: str, proxy_url: str, outcome: str) -> None:
    """Record a session outcome per attribution country and proxy.

    ``attribution_code`` is the code under which all health stats are stored
    (pool-owner code when the session used a matched pool, otherwise the
    session country code).  This MUST match the ``country_code`` that was
    passed to ``record_proxy_attempt`` so both attempts and session outcomes
    appear under the same health key.

    ``proxy_url`` may be an empty string when the caller does not know which
    proxy was actually used — it is then aggregated under a ``"direct"`` bucket
    (clear label, not ``"default"`` which was the legacy name for the same
    thing).  Fire-and-forget: never raises.

    Thread-safe: serialized under the proxy-blob lock (same lock domain as
    attempt recording and the persistence snapshot).
    """
    try:
        if not attribution_code or outcome not in ("success", "rejected", "multi_device"):
            return
        if not proxy_url:
            key = "direct"
        else:
            key = mask_proxy_url(proxy_url)
        with _PROXY_BLOB_LOCK:
            health = _get_health_root()
            cc_data = health.setdefault(attribution_code, {})
            sessions = cc_data.setdefault("sessions", {})
            entry = sessions.setdefault(key, {"success": 0, "rejected": 0, "multi_device": 0, "total": 0, "last_ts": ""})
            entry[outcome] = int(entry.get(outcome, 0)) + 1
            entry["total"] = int(entry.get("total", 0)) + 1
            entry["last_ts"] = datetime.now().isoformat(timespec="seconds")
            _prune_health_map(cc_data, "sessions")
        _persist_health_debounced()
    except Exception:
        logger.debug("proxy health: session outcome recording failed", exc_info=True)


def get_country_proxy_health(country_code: str) -> Dict[str, Any]:
    """Return a deep-copy snapshot of the health blob for one country.

    The copy is taken under the blob lock: the admin panel can then format
    it freely without racing a concurrent mutation.
    """
    if not country_code:
        return {}
    with _PROXY_BLOB_LOCK:
        data = _get_health_root().get(country_code)
        if not isinstance(data, dict):
            return {}
        return copy.deepcopy(data)


def get_country_proxy_session_stats(country_code: str) -> Dict[str, int]:
    """Aggregate session outcomes for a country across all its proxies."""
    data = get_country_proxy_health(country_code)
    sessions = data.get("sessions") or {}
    total = {"success": 0, "rejected": 0, "multi_device": 0, "total": 0}
    for entry in sessions.values():
        if not isinstance(entry, dict):
            continue
        total["success"] += int(entry.get("success", 0))
        total["rejected"] += int(entry.get("rejected", 0))
        total["multi_device"] += int(entry.get("multi_device", 0))
        total["total"] += int(entry.get("total", 0))
    return total


def get_country_proxy_health_summary(country_code: str) -> Dict[str, Any]:
    """Compute a human-friendly health summary for one country.

    Returns a dict ready for the admin panel:
        {
            "attempts": [{"proxy": str, "ok": int, "fail": int, "total": int,
                          "percent": float, "last_ts": str}, ...],
            "total_attempts": int, "total_ok": int, "total_fail": int,
            "overall_percent": float,
        }
    """
    data = get_country_proxy_health(country_code)
    attempts = data.get("attempts") or {}
    rows = []
    total_ok = 0
    total_fail = 0
    for key, entry in sorted(attempts.items()):
        if not isinstance(entry, dict):
            continue
        ok = int(entry.get("ok", 0))
        fail = int(entry.get("fail", 0))
        t = ok + fail
        total_ok += ok
        total_fail += fail
        rows.append({
            "proxy": key,
            "ok": ok,
            "fail": fail,
            "total": t,
            "percent": round((ok / t * 100) if t > 0 else 0.0, 1),
            "last_ts": entry.get("last_ts", ""),
        })
    overall_t = total_ok + total_fail
    return {
        "attempts": rows,
        "total_attempts": overall_t,
        "total_ok": total_ok,
        "total_fail": total_fail,
        "overall_percent": round((total_ok / overall_t * 100) if overall_t > 0 else 0.0, 1),
    }


def is_proxy_system_down() -> bool:
    """True when the watchdog has gated new phone submissions (all proxies dead).

    Reads the persisted flag that survives restarts. The flag is set by the
    watchdog when it observes ``dead_count == total > 0`` for two consecutive
    passes and cleared on full recovery. Phone.py checks this gate at the top
    of ``start_phone_verification`` to reject new submissions with a
    user-facing message while the proxy infrastructure is down.
    """
    with _PROXY_BLOB_LOCK:
        root = _storage.get("proxy")
        if not isinstance(root, dict):
            return False
        return bool(root.get("proxy_system_down", False))


def set_proxy_system_down(down: bool) -> None:
    """Persist the all-proxies-dead gate (watchdog writes, phone gate reads).

    Uses the debounced persist helper (same as health counters) so the write
    is non-blocking on the hot watchdog path. Cleared on full recovery.
    """
    with _PROXY_BLOB_LOCK:
        _get_root()["proxy_system_down"] = bool(down)
        _persist()
