"""
Ultra-Fast Response Cache System v2.1
=====================================
Target: < 1 millisecond response time for all handlers

Strategy:
1. Memory-based caching for instant lookup
2. NEVER block on storage/I/O - always return optimistic defaults
3. Background async updates (non-blocking)
4. Pre-loaded templates at startup
5. Trust cache first, verify later pattern
6. Proper cache invalidation on data changes

CRITICAL: All functions must return immediately without ANY blocking I/O
"""

import time
import logging
import asyncio
import threading
from typing import Dict, Any, Optional, Set, Callable, List

logger = logging.getLogger(__name__)

_cache_lock = threading.RLock()

_user_cache: Dict[int, Dict[str, Any]] = {}
_channel_cache: Dict[int, bool] = {}
_template_cache: Dict[str, str] = {}
_cache_timestamps: Dict[str, float] = {}
_banned_users: Set[int] = set()
_invalidation_callbacks: List[Callable] = []

CACHE_TTL = 120
CACHE_VERSION = "2.2"

# H5: the cache dicts used to grow WITHOUT BOUND - preload_user_data() loaded
# EVERY user at boot and no path ever evicted expired entries, so memory grew
# with the cumulative (not concurrent) user count. Entries are now bounded by
# TTL eviction (periodic sweep) plus a hard FIFO cap on inserts. A cache miss
# always falls back to the in-memory storage lookup, so eviction changes
# nothing functionally.
USER_CACHE_MAX_ENTRIES = int(__import__("os").environ.get("FAST_CACHE_MAX_USERS", "20000") or 20000)
CHANNEL_CACHE_MAX_ENTRIES = int(__import__("os").environ.get("FAST_CACHE_MAX_CHANNELS", "20000") or 20000)
# The ban set grows with the cumulative banned count; bound it too. Eviction
# is safe: is_user_banned_fast falls back to live storage on a miss.
BANNED_CACHE_MAX_ENTRIES = int(__import__("os").environ.get("FAST_CACHE_MAX_BANNED", "200000") or 200000)


def _bound_banned_locked() -> None:
    """Keep the ban set bounded (arbitrary eviction - storage is the truth).

    Reentrant-safe: callers may or may not already hold _cache_lock (RLock).
    """
    with _cache_lock:
        if len(_banned_users) > BANNED_CACHE_MAX_ENTRIES:
            target = int(BANNED_CACHE_MAX_ENTRIES * 0.9)
            while len(_banned_users) > target:
                _banned_users.pop()


def _evict_oldest_locked(cache: dict, prefix: str) -> None:
    """FIFO-evict the oldest inserted entry (dicts preserve insertion order).

    MUST hold _cache_lock. O(1): the first key is the oldest insert.
    """
    if not cache:
        return
    oldest = next(iter(cache))
    cache.pop(oldest, None)
    _cache_timestamps.pop(f"{prefix}{oldest}", None)


def _enforce_cap_locked(cache: dict, cap: int, prefix: str) -> None:
    while len(cache) > cap:
        _evict_oldest_locked(cache, prefix)


def cleanup_expired_cache() -> int:
    """Purge expired entries from every fast-cache map; return the count.

    Called from the memory manager's periodic cleanup. Pure in-memory work
    (a scan of the timestamp map) - safe to run on the event loop.
    """
    now = time.monotonic()
    removed = 0
    with _cache_lock:
        expired = [k for k, ts in _cache_timestamps.items() if now - ts >= CACHE_TTL]
        for k in expired:
            _cache_timestamps.pop(k, None)
            if k.startswith("user_"):
                try:
                    _user_cache.pop(int(k[5:]), None)
                except ValueError:
                    pass
            elif k.startswith("channel_"):
                try:
                    _channel_cache.pop(int(k[8:]), None)
                except ValueError:
                    pass
            removed += 1
        # Expired entries gone: re-assert the hard caps so an idle cache
        # cannot stay over the limit between sweeps.
        _enforce_cap_locked(_user_cache, USER_CACHE_MAX_ENTRIES, "user_")
        _enforce_cap_locked(_channel_cache, CHANNEL_CACHE_MAX_ENTRIES, "channel_")
    return removed


def register_invalidation_callback(callback: Callable) -> None:
    """Register a callback to be called when cache is invalidated."""
    with _cache_lock:
        _invalidation_callbacks.append(callback)


def _notify_invalidation(cache_type: str, key: Optional[str] = None) -> None:
    """Notify all registered callbacks of cache invalidation (thread-safe)."""
    with _cache_lock:
        callbacks_copy = _invalidation_callbacks.copy()
    
    for callback in callbacks_copy:
        try:
            callback(cache_type, key)
        except Exception as e:
            logger.warning(f"Cache invalidation callback error: {e}")

def get_cached_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Get user from memory cache - INSTANT (< 0.001ms) - NEVER BLOCKS"""
    with _cache_lock:
        cache_key = f"user_{user_id}"
        if cache_key in _cache_timestamps:
            if time.monotonic() - _cache_timestamps[cache_key] < CACHE_TTL:
                return _user_cache.get(user_id)
        return None


def set_cached_user(user_id: int, data: Dict[str, Any]) -> None:
    """Set user in memory cache - INSTANT (TTL + hard cap enforced)"""
    with _cache_lock:
        _user_cache[user_id] = data
        _cache_timestamps[f"user_{user_id}"] = time.monotonic()
        if len(_user_cache) > USER_CACHE_MAX_ENTRIES:
            _evict_oldest_locked(_user_cache, "user_")


def invalidate_user_cache(user_id: Optional[int] = None) -> None:
    """Invalidate user cache for specific user or all users."""
    with _cache_lock:
        if user_id is not None:
            _user_cache.pop(user_id, None)
            _cache_timestamps.pop(f"user_{user_id}", None)
            _notify_invalidation("user", str(user_id))
        else:
            _user_cache.clear()
            for key in list(_cache_timestamps.keys()):
                if key.startswith("user_"):
                    _cache_timestamps.pop(key, None)
            _notify_invalidation("user", None)


def get_cached_channel_status(user_id: int) -> Optional[bool]:
    """Get channel membership from cache - INSTANT (< 0.001ms) - NEVER BLOCKS"""
    with _cache_lock:
        cache_key = f"channel_{user_id}"
        if cache_key in _cache_timestamps:
            if time.monotonic() - _cache_timestamps[cache_key] < CACHE_TTL:
                return _channel_cache.get(user_id)
        return None


def set_cached_channel_status(user_id: int, is_member: bool) -> None:
    """Set channel membership in cache - INSTANT (TTL + hard cap enforced)"""
    with _cache_lock:
        _channel_cache[user_id] = is_member
        _cache_timestamps[f"channel_{user_id}"] = time.monotonic()
        if len(_channel_cache) > CHANNEL_CACHE_MAX_ENTRIES:
            _evict_oldest_locked(_channel_cache, "channel_")


def invalidate_channel_cache(user_id: Optional[int] = None) -> None:
    """Invalidate channel cache for user or all users."""
    with _cache_lock:
        if user_id is not None:
            _channel_cache.pop(user_id, None)
            _cache_timestamps.pop(f"channel_{user_id}", None)
            _notify_invalidation("channel", str(user_id))
        else:
            _channel_cache.clear()
            for key in list(_cache_timestamps.keys()):
                if key.startswith("channel_"):
                    _cache_timestamps.pop(key, None)
            _notify_invalidation("channel", None)

_preloaded_templates: Dict[str, str] = {}
_withdraw_enabled_cache: bool = True
_withdraw_cache_time: float = 0

_template_defaults = {
    "account_received_message": "✅ *Account Received*\n\n📱 `{phone}`   {flag}\n━━━━━━━━━━━━━━━━━━━━\n💰 Price        →  *Calculating...*\n📊 Status       →  🔄 Processing\n⏰ Received     →  {received_time}\n⏰ ETA          →  ~{eta_min_sec}\n━━━━━━━━━━━━━━━━━━━━\n\n🔔 You will be notified when verified.",
    "welcome_message": "👋 *Welcome!*\n\nSend your phone number to begin verification.\n\nExample: `+8801XXXXXXXXXX`\n\nCommands:\n/account - View your account\n/withdraw - Withdraw balance\n/cap - Available countries\n/cancel - Stop verification",
    "banned_user_message": "🚫 *Access Denied*\n\nYou have been banned from using this bot.\nContact support if you believe this is a mistake.",
    "processing_message": "⏳ *Processing...*\n\nPlease wait while we process your request.\nThis may take a few moments.",
    "account_info_message": "🎰 *Your Account*\n\n👤 User ID: `{user_id}`\n💰 Balance: *${balance}*\n✅ Sessions: *{sessions}*\n📅 Joined: *{joined}*\n🕐 Time: *{time}*\n\nCommands:\n/start - Submit a number\n/withdraw - Withdraw funds\n/cap - View open countries",
    "no_balance_message": "❌ *Insufficient Balance*\n\nYou do not have any balance to withdraw.\n\nSubmit numbers via /start to earn balance.",
    "withdraw_success_message": "✅ *Withdraw Request Submitted*\n\n💰 Amount: *${amount}*\n💳 Method: *{method}*\n\nYour request will be processed shortly.\nCheck /account for updated balance.",
    "support_message": "📞 *Support*\n\nContact the admin for assistance.\nPlease describe your issue clearly.",
    
    "enter_phone_message": "📱 *Enter Your Phone Number*\n\nSend your phone number with country code.\n\nExample: `+8801XXXXXXXXXX`\n\n⚠️ Include the correct country code.\nType /cancel to stop.",
    "enter_otp_message": "🔢 *Enter OTP Code*\n\nEnter the code sent to your Telegram app.\n\n⏰ Timeout: *{timeout} seconds*\n\nType /cancel to stop.",
    "otp_sent_message": "📤 *OTP Code Sent*\n\n📱 Phone: `{phone}`\n🌍 Country: {flag} {country_code}\n\n⏰ Time Remaining: *{timeout} seconds*\n\nEnter the code or type /cancel",
    "waiting_verification_message": "⏳ *Verification In Progress*\n\n📱 Phone: `{phone}`\n🌍 Country: {flag} {country_code}\n\n⏰ Estimated Time: *{timeout} seconds*\n\nYou will be notified when complete.",
    "twofa_setup_message": "🔐 *Setting Up Security*\n\n📱 Phone: `{phone}`\n\n⏳ 2FA is being configured.\nPlease wait approximately 2 minutes.",
    "spam_check_message": "🔍 *Checking Account Status*\n\n📱 Phone: `{phone}`\n\n⏳ Verifying account standing...\nThis ensures fair pricing.",
    "withdraw_disabled_message": "❌ *Withdrawals Unavailable*\n\nWithdrawals are temporarily disabled.\nPlease check back later.",
    "withdraw_method_message": "💳 *Select Withdrawal Method*\n\n💰 Your Balance: *${balance}*\n\nPlease choose your preferred method:",
    "enter_wallet_message": "📬 *Enter {method} Wallet Address*\n\nSend your {method} wallet address below.\n\n⚠️ *Important:*\n• Double-check the address before sending\n• Incorrect addresses cannot be refunded\n• Type /cancel to abort",
    "invalid_wallet_message": "❌ *Invalid Wallet Address*\n\nError: `{error}`\n\n📬 Please send a valid *{method}* address.\n\nType /cancel to abort.",
    "session_timeout_message": "⏰ *Session Expired*\n\n📱 Phone: `{phone}`\n🌍 Country: {flag} {country_code}\n\nNo response received within *{timeout} seconds*.\n\n🔄 Type /start to try again.",
    "country_not_available_message": "❌ *Country Unavailable*\n\n🌍 Country: {flag} {country_code}\n\nThis country is currently closed.\n\nType /cap to see available countries\nOr try a different number with /start",
    "join_channel_message": "📢 *Channel Required*\n\n🔗 Channel: {channel}\n\nYou must join this channel to use the bot.\n\n✅ After joining, click the button below\nor type /start to begin.",
    "min_withdraw_message": "⚠️ *Insufficient Balance*\n\n💰 Your Balance: *${balance}*\n💵 Minimum Withdraw: *${min_amount}*\n\n❌ You need *${needed}* more to withdraw.\n\nSubmit more numbers via /start",
    "enter_2fa_password_message": "🔐 *2FA Password Required*\n\n📱 Phone: `{phone}`\n\nThis account has Two-Factor Authentication.\n\n🔑 Enter the 2FA password:\n\nType /cancel to abort.",
    "success_message": "✅ *Verification Successful*\n\n📱 Phone: `{phone}`\n💰 Balance Added: *${price}*\n📊 Status: *{status}*\n\nYour account has been credited successfully.",
    "multi_device_message": "⚠️ *Multiple Devices Detected*\n\n📱 Phone: `{phone}`\n\nMultiple devices are logged into this account.\nThe session may be invalid.\n\nTry a different number with /start",
    "verification_failed_message": "❌ *Verification Failed*\n\n📱 Phone: `{phone}`\n🌍 Country: {flag} {country_code}\n\nDevice is not logged in. The session has expired.\n\nType /start to try again.",
    "invalid_otp_message": "❌ *Invalid OTP Code*\n\nThe code you entered is incorrect.\nPlease enter the OTP sent to your Telegram app.",
    "twofa_already_active_message": "⚠️ *2FA Already Enabled*\n\nThis account already has Two-Factor Authentication.\n\nPlease use a different number with /start"
}

def get_cached_template(template_name: str, lang: str = "en") -> str:
    """Get message template - INSTANT (< 0.001ms) - NEVER BLOCKS

    Resolution order for every language:
      1. admin-custom template for ``lang`` (per-language override)
      2. built-in translation dictionary for ``lang``
      3. admin-custom English template
      4. built-in default English text

    English uses the preloaded template, then live storage (DB custom ->
    default), then the literal fallback. Storage is the source of truth so
    code-level template updates apply even without a restart.
    """
    if lang and lang != "en":
        try:
            from bot.data.templates import get_message_template
            return get_message_template(template_name, lang)
        except (ImportError, Exception):
            pass
        # Fall through to English handling for untranslated keys.

    if template_name in _preloaded_templates:
        return _preloaded_templates[template_name]

    try:
        from bot.utils.storage import get_message_template
        content = get_message_template(template_name)
        if content:
            _preloaded_templates[template_name] = content
            return content
    except (ImportError, KeyError, TypeError, Exception):
        pass

    return _template_defaults.get(template_name, f"[{template_name}]")

def format_message_fast(template_name: str, lang: str = "en", **kwargs) -> str:
    """Format message template - INSTANT (< 0.001ms) - NEVER BLOCKS
    Uses preloaded templates with regex placeholder replacement (no overlapping key corruption)
    """
    import re
    template = get_cached_template(template_name, lang)
    try:
        for key, value in kwargs.items():
            template = re.sub(r'\{' + re.escape(key) + r'\}', lambda m: str(value), template)
        return template
    except (KeyError, ValueError, TypeError):
        return template

def preload_templates() -> None:
    """Pre-load all common templates into cache at startup - runs ONCE"""
    global _preloaded_templates
    common_templates = [
        "account_received_message",
        "welcome_message",
        "banned_user_message", 
        "processing_message",
        "account_info_message",
        "no_balance_message",
        "withdraw_success_message",
        "support_message",
        "enter_phone_message",
        "enter_otp_message",
        "otp_sent_message",
        "waiting_verification_message",
        "twofa_setup_message",
        "spam_check_message",
        "withdraw_disabled_message",
        "withdraw_method_message",
        "enter_wallet_message",
        "invalid_wallet_message",
        "session_timeout_message",
        "country_not_available_message",
        "join_channel_message",
        "min_withdraw_message",
        "enter_2fa_password_message",
        "success_message",
        "multi_device_message",
        "verification_failed_message",
        "invalid_otp_message",
        "twofa_already_active_message"
    ]
    try:
        from bot.utils.storage import get_message_template
        for template in common_templates:
            try:
                content = get_message_template(template)
                if content:
                    _preloaded_templates[template] = content
            except (KeyError, TypeError) as e:
                print(f"⚠️ Template load warning for {template}: {e}")
    except ImportError as e:
        print(f"⚠️ Storage import error: {e}")

def is_withdraw_enabled_fast() -> bool:
    """Fast withdraw enabled check - INSTANT (< 0.001ms) - NEVER BLOCKS
    Uses cached value, refreshes in background periodically
    Returns cached value even when expired (never defaults to True)
    """
    global _withdraw_enabled_cache, _withdraw_cache_time
    
    current_time = time.monotonic()
    
    if current_time - _withdraw_cache_time >= 60:
        try:
            from bot.utils.storage import is_withdraw_enabled
            _withdraw_enabled_cache = is_withdraw_enabled()
            _withdraw_cache_time = current_time
            print(f"🔄 Withdraw cache auto-refreshed: enabled={_withdraw_enabled_cache}")
        except Exception:
            pass
    
    return _withdraw_enabled_cache

def refresh_withdraw_status() -> None:
    """Refresh withdraw status from storage - call in background"""
    global _withdraw_enabled_cache, _withdraw_cache_time
    try:
        from bot.utils.storage import is_withdraw_enabled
        _withdraw_enabled_cache = is_withdraw_enabled()
        _withdraw_cache_time = time.monotonic()
        print(f"✅ Withdraw cache refreshed: enabled={_withdraw_enabled_cache}")
    except (ImportError, KeyError, TypeError) as e:
        print(f"⚠️ Withdraw status refresh warning: {e}")

def force_refresh_withdraw_status(new_status: bool) -> None:
    """Force update withdraw cache immediately - call after admin toggle"""
    global _withdraw_enabled_cache, _withdraw_cache_time
    _withdraw_enabled_cache = new_status
    _withdraw_cache_time = time.monotonic()
    print(f"🔄 Withdraw cache force updated: enabled={new_status}")

async def background_refresh_withdraw_status() -> None:
    """Refresh withdraw status in background - non-blocking"""
    try:
        await asyncio.to_thread(refresh_withdraw_status)
    except (RuntimeError, asyncio.CancelledError) as e:
        print(f"⚠️ Background withdraw refresh error: {e}")

def warm_user_cache(user_id: int) -> None:
    """Pre-warm user cache from storage (call in background thread)"""
    try:
        from bot.utils.storage import get_user_info
        user_data = get_user_info(user_id)
        if user_data:
            set_cached_user(user_id, user_data)
    except (ImportError, KeyError, TypeError):
        pass  # Silently skip cache warming on error

async def background_channel_check(client, user_id: int, channel: str) -> None:
    """Check channel membership in background - FULLY non-blocking"""
    try:
        await client.get_chat_member(channel, user_id)
        set_cached_channel_status(user_id, True)
        try:
            from bot.utils.storage import update_user_info
            await asyncio.to_thread(update_user_info, user_id, channel_joined=True)
        except (ImportError, RuntimeError, asyncio.CancelledError):
            pass
    except Exception as e:
        error_str = str(e).lower()
        if "user_not_participant" in error_str:
            set_cached_channel_status(user_id, False)
        else:
            pass

async def background_save_user(user_id: int, **kwargs) -> None:
    """Save user data in background - FULLY non-blocking via to_thread.

    P7-74 CACHE COHERENCE: the cache is now updated AFTER storage, never
    before. The old order (cache-first) served a value that could be WRONG
    until the TTL expired when the storage write failed (disk error, storage
    lock contention) - e.g. a "banned=True" that never persisted, or a
    balance shown that was never stored. Storage is the canonical source:
    write it, then re-sync the cache from it.
    """
    try:
        from bot.utils.storage import update_user_info
        await asyncio.to_thread(update_user_info, user_id, **kwargs)
        await asyncio.to_thread(sync_user_from_storage, user_id)
    except (ImportError, RuntimeError, asyncio.CancelledError) as e:
        print(f"⚠️ Background save error for user {user_id}: {e}")

def update_user_balance_cache(user_id: int, new_balance: float) -> None:
    """Update user balance in cache immediately - INSTANT"""
    cached_user = get_cached_user(user_id) or {}
    cached_user["balance"] = new_balance
    set_cached_user(user_id, cached_user)

def update_user_sessions_cache(user_id: int, sessions: int) -> None:
    """Update user sessions count in cache immediately - INSTANT"""
    cached_user = get_cached_user(user_id) or {}
    cached_user["sessions"] = sessions
    set_cached_user(user_id, cached_user)

def sync_user_from_storage(user_id: int) -> None:
    """Sync cache from storage (canonical source of truth) - USE AFTER MUTATIONS"""
    try:
        from bot.utils.storage import get_user_info
        user_data = get_user_info(user_id)
        if user_data:
            set_cached_user(user_id, user_data)
            if user_data.get("channel_joined"):
                set_cached_channel_status(user_id, True)
            if user_data.get("banned"):
                _banned_users.add(user_id)
                _bound_banned_locked()
    except (ImportError, KeyError, TypeError):
        pass  # Sync failure is non-critical

async def sync_user_from_storage_async(user_id: int) -> None:
    """Async version - runs sync in background thread"""
    await asyncio.to_thread(sync_user_from_storage, user_id)

def is_user_likely_member(user_id: int) -> bool:
    """
    Fast check if user is likely a channel member - INSTANT (< 0.001ms)
    Returns cached status. Falls back to False (requires verification) when uncached.
    """
    cached = get_cached_channel_status(user_id)
    if cached is not None:
        return cached

    cached_user = get_cached_user(user_id)
    if cached_user and cached_user.get("channel_joined"):
        set_cached_channel_status(user_id, True)
        return True

    return False

def is_user_banned_fast(user_id: int) -> bool:
    """
    Fast ban check with storage fallback for accuracy.
    Checks memory cache first, then storage if not found.
    """
    if user_id in _banned_users:
        return True
    
    cached_user = get_cached_user(user_id)
    if cached_user and cached_user.get("banned"):
        _banned_users.add(user_id)
        _bound_banned_locked()
        return True
    
    try:
        from bot.utils.storage import is_user_banned
        if is_user_banned(user_id):
            _banned_users.add(user_id)
            _bound_banned_locked()
            return True
    except (ImportError, Exception):
        pass
    
    return False

def mark_user_banned(user_id: int) -> None:
    """Mark user as banned in memory cache - INSTANT"""
    _banned_users.add(user_id)
    _bound_banned_locked()
    cached_user = get_cached_user(user_id)
    if cached_user:
        cached_user["banned"] = True
        set_cached_user(user_id, cached_user)

def mark_user_unbanned(user_id: int) -> None:
    """Mark user as unbanned in memory cache - INSTANT"""
    _banned_users.discard(user_id)
    cached_user = get_cached_user(user_id)
    if cached_user:
        cached_user["banned"] = False
        set_cached_user(user_id, cached_user)

def get_user_balance_fast(user_id: int) -> float:
    """
    Fast balance lookup - checks cache first, then storage if not found
    Returns actual balance from storage if cache miss
    """
    cached_user = get_cached_user(user_id)
    if cached_user and "balance" in cached_user:
        return cached_user.get("balance", 0.0)
    
    try:
        from bot.utils.storage import get_user_info
        user_data = get_user_info(user_id)
        if user_data:
            set_cached_user(user_id, user_data)
            return user_data.get("balance", 0.0)
    except (ImportError, KeyError, TypeError):
        pass  # Return default on error
    
    return 0.0

async def background_warm_user_cache(user_id: int) -> None:
    """Warm user cache in background - non-blocking"""
    try:
        await asyncio.to_thread(warm_user_cache, user_id)
    except (RuntimeError, asyncio.CancelledError):
        pass  # Background warming is non-critical

def preload_user_data() -> None:
    """Pre-load user data into cache at startup - BOUNDED (H5 fix).

    The old version cloned EVERY user row into the cache synchronously at
    boot: it doubled startup time and pinned the whole user table in a
    SECOND copy of memory (on top of _storage) that grew forever. The cache
    now enforces a hard cap on insert (FIFO eviction) and a periodic TTL
    sweep, so this preload is naturally bounded; every cache miss still
    falls back to the in-memory storage lookup, so nothing is slower for
    users beyond the cap.
    """
    try:
        from bot.utils.storage import _storage
        users = _storage.get("users", {})
        loaded = 0
        for user_id_str, user_data in users.items():
            try:
                user_id = int(user_id_str)
            except (ValueError, TypeError):
                continue  # Skip invalid user entries
            if user_data.get("banned"):
                _banned_users.add(user_id)
            if isinstance(user_data, dict):
                set_cached_user(user_id, user_data)
                if user_data.get("channel_joined"):
                    set_cached_channel_status(user_id, True)
                loaded += 1
        _bound_banned_locked()
        print(f"✅ User cache preloaded: {loaded} entries "
              f"(cap {USER_CACHE_MAX_ENTRIES}, banned marked: {len(_banned_users)})")
    except (ImportError, AttributeError) as e:
        print(f"⚠️ User data preload error: {e}")

def update_template_cache(template_name: str, template_text: str) -> None:
    """Update a single template in the preloaded cache - call after saving template"""
    global _preloaded_templates
    _preloaded_templates[template_name] = template_text
    print(f"✅ Template cache updated: {template_name}")

def refresh_all_templates() -> None:
    """Refresh all templates from storage into cache"""
    global _preloaded_templates
    try:
        from bot.utils.storage import get_all_message_templates
        templates = get_all_message_templates()
        for key, data in templates.items():
            _preloaded_templates[key] = data["current"]
        print(f"✅ All {len(templates)} templates refreshed in cache")
    except Exception as e:
        print(f"⚠️ Error refreshing templates: {e}")

async def refresh_templates_async() -> None:
    """Async version - refreshes templates in background thread"""
    await asyncio.to_thread(refresh_all_templates)

print("✅ Ultra-Fast Response Cache System v2.2 Loaded")
print("   - ZERO BLOCKING I/O - All operations instant")
print("   - Memory-based instant lookup, TTL + cap bounded")
print("   - Background async updates")
print("   - Target response time: < 1ms")
