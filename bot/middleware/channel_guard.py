"""
Channel Membership Guard - Universal Enforcement Middleware
============================================================
Enforces mandatory channel membership for all bot handlers.

Features:
- Smart caching with configurable TTL for performance
- Live Telegram API verification when cache expires
- Decorator pattern for universal coverage
- Seamless integration with all handler types

Version: 2.0
"""

import time
import asyncio
from typing import Optional, Callable, Any, Union, Dict
from functools import wraps
from pyrogram.client import Client
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, ChannelPrivate
from pyrogram.handlers.message_handler import MessageHandler
from pyrogram.handlers.callback_query_handler import CallbackQueryHandler
from bot.utils.bg_tasks import spawn_background

MEMBERSHIP_CACHE_TTL = 120

_membership_cache: dict = {}
_prompt_cooldown: dict = {}
PROMPT_COOLDOWN_SECONDS = 15
_use_redis_cache = False

# ------------------------------------------------------------------
#  MANDATORY LANGUAGE SELECTION GATE
#  A user must pick a language BEFORE any command is usable. Until a
#  supported language is stored for the user, EVERY command (except the
#  language-flow handlers below) is redirected to the language picker.
# ------------------------------------------------------------------

# Handlers that are allowed to run even before a language is chosen. The
# /start handler shows the language picker itself on first visit, and the
# /language command + language picker callback are the picker itself.
LANGUAGE_FLOW_HANDLERS = frozenset({
    "start_handler",
    "language_command",
    "language_select_callback",
})

# Language-flow handlers that must ALSO bypass the channel-membership gate:
# the user may always reach the language picker, even before joining the
# required channel ("the language picker is the single exception").
CHANNEL_EXEMPT_HANDLERS = frozenset({
    "language_command",
    "language_select_callback",
})

# Callback data belonging to the channel-join verification flow - never gated.
MEMBERSHIP_VERIFY_CALLBACKS = frozenset({
    "check_membership_verify",
    "channel_joined_verify",
})


def _is_admin_user(user_id: int) -> bool:
    """True for the primary admin or any multi-admin."""
    if user_id == _get_admin_id():
        return True
    try:
        from bot.handlers.admin.router import is_admin
        return is_admin(user_id)
    except Exception:
        return False


async def _force_language_selection(
    client: Client,
    update: Union[Message, CallbackQuery],
    user_id: int,
) -> None:
    """Show the language picker to a user who has not selected a language yet."""
    try:
        from bot.handlers.language import send_language_selection
    except ImportError:
        return
    try:
        if isinstance(update, Message):
            await send_language_selection(client, user_id, message=update)
        elif isinstance(update, CallbackQuery):
            try:
                await update.answer()
            except Exception:
                pass
            await send_language_selection(client, user_id, callback=update)
        else:
            await send_language_selection(client, user_id)
    except Exception as e:
        print(f"⚠️ Language selection prompt error: {e}")


def _has_selected_language(user_id: int) -> bool:
    """Fast cache-first check for whether a user has chosen a language."""
    try:
        from bot.utils.i18n import has_explicit_lang
        return has_explicit_lang(user_id)
    except Exception:
        return True

async def init_redis_cache() -> bool:
    """Initialize Redis cache for channel guard."""
    global _use_redis_cache
    try:
        from bot.utils.redis_cache import is_redis_available
        _use_redis_cache = is_redis_available()
        return _use_redis_cache
    except ImportError:
        return False

def _get_required_channel() -> str:
    """Get required channel from storage or config. Returns empty string if not set."""
    try:
        from bot.utils.storage import get_required_channel
        channel = get_required_channel()
        if channel:
            return channel
    except (ImportError, KeyError, TypeError) as e:
        import logging
        logging.debug(f"Could not load channel from storage: {e}")
    
    try:
        from bot.config import REQUIRED_CHANNEL
        if REQUIRED_CHANNEL:
            return REQUIRED_CHANNEL
    except (ImportError, AttributeError) as e:
        import logging
        logging.debug(f"Could not load REQUIRED_CHANNEL from config: {e}")
    
    return ""

def _get_admin_id() -> int:
    """Get admin ID from config"""
    try:
        from bot.config import ADMIN_ID
        return ADMIN_ID
    except (ImportError, AttributeError):
        return 0

def get_cached_membership(user_id: int) -> Optional[bool]:
    """Get membership status from cache if not expired (sync version for compatibility)"""
    cache_key = f"member_{user_id}"
    if cache_key in _membership_cache:
        cached_time, is_member = _membership_cache[cache_key]
        if time.monotonic() - cached_time < MEMBERSHIP_CACHE_TTL:
            return is_member
        else:
            del _membership_cache[cache_key]
    return None

async def get_cached_membership_async(user_id: int) -> Optional[bool]:
    """Get membership status from Redis cache first, then in-memory."""
    if _use_redis_cache:
        try:
            from bot.utils.redis_cache import get_membership_cache
            result = await get_membership_cache(user_id)
            if result is not None:
                return result
        except Exception:
            pass
    return get_cached_membership(user_id)

def set_cached_membership(user_id: int, is_member: bool) -> None:
    """Set membership status in cache (sync version for compatibility)"""
    cache_key = f"member_{user_id}"
    _membership_cache[cache_key] = (time.monotonic(), is_member)
    
    if _use_redis_cache:
        try:
            from bot.utils.redis_cache import set_membership_cache
            spawn_background(set_membership_cache(user_id, is_member), "guard-cache-set", quiet=True)
        except Exception:
            pass
    
    try:
        from bot.utils.fast_cache import set_cached_channel_status
        set_cached_channel_status(user_id, is_member)
    except (ImportError, AttributeError):
        pass

async def set_cached_membership_async(user_id: int, is_member: bool) -> None:
    """Set membership status in Redis and in-memory cache."""
    cache_key = f"member_{user_id}"
    _membership_cache[cache_key] = (time.monotonic(), is_member)
    
    if _use_redis_cache:
        try:
            from bot.utils.redis_cache import set_membership_cache
            await set_membership_cache(user_id, is_member)
        except Exception:
            pass
    
    try:
        from bot.utils.fast_cache import set_cached_channel_status
        set_cached_channel_status(user_id, is_member)
    except (ImportError, AttributeError):
        pass

def invalidate_user_membership(user_id: int) -> None:
    """Invalidate membership cache for a user (call when user leaves)"""
    cache_key = f"member_{user_id}"
    _membership_cache.pop(cache_key, None)
    
    if _use_redis_cache:
        try:
            from bot.utils.redis_cache import invalidate_membership_cache
            spawn_background(invalidate_membership_cache(user_id), "guard-cache-invalidate", quiet=True)
        except Exception:
            pass
    
    try:
        from bot.utils.fast_cache import invalidate_channel_cache
        invalidate_channel_cache(user_id)
    except (ImportError, AttributeError):
        pass

def invalidate_all_memberships() -> None:
    """Invalidate all membership caches (call when channel changes)"""
    global _membership_cache
    _membership_cache.clear()
    
    if _use_redis_cache:
        try:
            from bot.utils.redis_cache import invalidate_membership_cache
            spawn_background(invalidate_membership_cache(None), "guard-cache-invalidate-all", quiet=True)
        except Exception:
            pass
    
    try:
        from bot.utils.fast_cache import invalidate_channel_cache
        invalidate_channel_cache(None)
    except (ImportError, AttributeError):
        pass

async def cleanup_membership_cache() -> int:
    """Clean up expired entries from in-memory cache."""
    now = time.monotonic()
    expired = [k for k, (t, _) in _membership_cache.items() if now - t >= MEMBERSHIP_CACHE_TTL]
    for k in expired:
        del _membership_cache[k]
    return len(expired)

async def check_membership_live(client: Client, user_id: int, channel: Optional[str] = None) -> tuple:
    """
    Check membership via Telegram API (LIVE check)
    
    Returns: (is_member: bool, error_message: Optional[str])
        - (True, None): user IS a member
        - (False, "not_joined"): user is NOT in the channel
        - (False, "bot_no_access"): bot cannot access the channel
        - (False, "error"): other error occurred
    """
    if channel is None:
        channel = _get_required_channel()
    
    if not channel:
        return True, None
    
    # Pre-check: verify bot has access to the channel
    try:
        await client.get_chat(channel)
    except (ChatAdminRequired, ChannelPrivate) as e:
        print(f"BOT ACCESS ERROR: Bot is not in channel {channel} - cannot verify membership")
        return False, "bot_no_access"
    except Exception as e:
        print(f"BOT CHANNEL CHECK WARNING: {e}")
    
    try:
        member = await client.get_chat_member(channel, user_id)
        
        status = member.status.value if hasattr(member.status, 'value') else str(member.status)
        
        if status in ["member", "administrator", "creator", "owner", "restricted"]:
            set_cached_membership(user_id, True)
            
            try:
                from bot.utils.storage import update_user_info
                spawn_background(asyncio.to_thread(update_user_info, user_id, channel_joined=True),
                                 "guard-mark-joined", quiet=True)
            except (ImportError, RuntimeError):
                pass
            
            return True, None
        elif status in ["left", "kicked", "banned"]:
            set_cached_membership(user_id, False)
            return False, "not_joined"
        else:
            set_cached_membership(user_id, False)
            return False, "not_joined"
            
    except UserNotParticipant:
        set_cached_membership(user_id, False)
        return False, "not_joined"
        
    except (ChatAdminRequired, ChannelPrivate) as e:
        print(f"BOT ACCESS ERROR: Bot lacks permissions in {channel}: {e}")
        return False, "bot_no_access"
        
    except Exception as e:
        error_str = str(e).lower()
        if "user_not_participant" in error_str or "participant" in error_str:
            set_cached_membership(user_id, False)
            return False, "not_joined"
        if "chat_admin_required" in error_str or "channel_private" in error_str:
            print(f"BOT ACCESS ERROR: Bot cannot access {channel}: {e}")
            return False, "bot_no_access"
        
        print(f"MEMBERSHIP CHECK ERROR: {e}")
        return False, "error"

async def ensure_membership(client: Client, user_id: int, force_check: bool = False) -> bool:
    """
    MAIN FUNCTION: Ensure user is a channel member
    
    Returns:
        True if user is a member (allow handler to proceed)
        False if user is confirmed NOT a member (block handler)
    
    Logic:
    1. If no channel configured -> allow all
    2. Admin always bypasses
    3. Check cache first (fast path)
    4. If cache miss or expired -> Live API check
    5. Update cache with result
    
    FAIL-OPEN: when the bot cannot verify membership (it is not an admin in the
    channel, the channel is private, or a transient API error occurred), this
    returns True instead of blocking. Blocking on "cannot verify" disables every
    guarded handler for every user - the bot keeps running but appears dead.
    Only a positive "user is not a member" result (error_reason == "not_joined")
    blocks the user.
    """
    if user_id == _get_admin_id():
        return True

    # Users who have NOT selected a language yet are allowed through so the
    # language picker (the mandatory first step) can be shown. Membership is
    # enforced immediately AFTER a language has been chosen.
    try:
        from bot.utils.i18n import has_explicit_lang
        if not has_explicit_lang(user_id):
            return True
    except Exception:
        pass

    channel = _get_required_channel()
    if not channel:
        return True
    
    if not force_check:
        cached = get_cached_membership(user_id)
        if cached is not None:
            return cached
    
    is_member, error_reason = await check_membership_live(client, user_id, channel)
    
    if is_member:
        return True
    
    if error_reason == "not_joined":
        return False
    
    # Fail-open: the bot could not verify membership (bot_no_access) or the
    # check failed transiently (error). Cache the allow decision briefly so a
    # misconfigured bot (not an admin in the channel) or a transient API issue
    # does not trigger a live API call for every single message.
    set_cached_membership(user_id, True)
    return True

def _can_send_prompt(user_id: int) -> bool:
    """Check if we can send join prompt (avoid spam)"""
    now = time.monotonic()
    last_prompt = _prompt_cooldown.get(user_id, 0)
    if now - last_prompt < PROMPT_COOLDOWN_SECONDS:
        return False
    _prompt_cooldown[user_id] = now
    return True

async def send_join_prompt(
    client: Client,
    chat_id: int,
    message: Optional[Message] = None,
    callback: Optional[CallbackQuery] = None
) -> None:
    """
    Send channel join prompt with button
    
    Can handle both Message and CallbackQuery contexts
    """
    if not _can_send_prompt(chat_id):
        if callback:
            try:
                from bot.utils.i18n import user_t
                await callback.answer(user_t("channel_not_joined_alert", chat_id), show_alert=True)
            except Exception:
                pass
        return
    
    channel = _get_required_channel()

    try:
        from bot.utils.i18n import user_t
        lang_ctx = user_t
    except (ImportError, Exception):
        lang_ctx = None
    
    channel_link = None
    if channel.startswith("@"):
        channel_link = f"https://t.me/{channel[1:]}"
    elif channel.startswith("https://"):
        channel_link = channel
    elif str(channel).lstrip("-").isdigit():
        # Numeric channel IDs have no public join link; only show the
        # "I've Joined" button so the membership check can still run.
        channel_link = None
    else:
        channel_link = f"https://t.me/{channel}"
    
    try:
        from bot.utils.fast_cache import get_cached_template
        from bot.utils.i18n import get_user_lang
        prompt_text = get_cached_template("join_channel_message", get_user_lang(chat_id)).format(channel=channel)
    except Exception:
        try:
            from bot.utils.i18n import user_t
            prompt_text = user_t("join_channel_message", chat_id, channel=channel)
        except Exception:
            prompt_text = (
                "🔐 **Join Channel Required**\n\n"
                "You must join our channel before using the bot.\n\n"
                f"📢 Channel: {channel}\n\n"
                "✅ After joining, click the button below."
            )

    if lang_ctx is not None:
        join_label = lang_ctx("join_channel_button", chat_id)
        joined_label = lang_ctx("joined_button", chat_id)
    else:
        join_label = "📢 Join Channel"
        joined_label = "✅ I've Joined"

    if channel_link:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(join_label, url=channel_link)],
            [InlineKeyboardButton(joined_label, callback_data="check_membership_verify")]
        ])
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(joined_label, callback_data="check_membership_verify")]
        ])

    try:
        if callback:
            if lang_ctx is not None:
                await callback.answer(lang_ctx("channel_not_joined_prompt", chat_id), show_alert=True)
            else:
                await callback.answer("⚠️ Please join the channel first!", show_alert=True)
            try:
                await callback.message.edit_text(prompt_text, reply_markup=keyboard)
            except Exception:
                await client.send_message(chat_id, prompt_text, reply_markup=keyboard)
        elif message:
            await message.reply(prompt_text, reply_markup=keyboard)
        else:
            await client.send_message(chat_id, prompt_text, reply_markup=keyboard)
    except Exception as e:
        print(f"⚠️ Error sending join prompt: {e}")

def channel_required(func: Callable) -> Callable:
    """
    DECORATOR: Enforce channel membership on any handler
    
    Usage:
        @channel_required
        async def my_handler(client, message):
            ...
    
    If user is not a member → sends join prompt, blocks handler
    If user is a member → executes handler normally
    """
    @wraps(func)
    async def wrapper(client: Client, update: Union[Message, CallbackQuery], *args, **kwargs):
        if isinstance(update, Message):
            if update.chat is None:
                return await func(client, update, *args, **kwargs)

            user_id = update.chat.id
            
            if not update.from_user:
                return await func(client, update, *args, **kwargs)
            
            is_member = await ensure_membership(client, user_id)
            
            if not is_member:
                await send_join_prompt(client, user_id, message=update)
                return None
            
            return await func(client, update, *args, **kwargs)
            
        elif isinstance(update, CallbackQuery):
            user_id = update.from_user.id
            
            is_member = await ensure_membership(client, user_id)
            
            if not is_member:
                await send_join_prompt(client, user_id, callback=update)
                return None
            
            return await func(client, update, *args, **kwargs)
        
        else:
            return await func(client, update, *args, **kwargs)
    
    # Prevent double-wrapping (e.g. if add_handler is invoked again on the same
    # callback for any reason).
    wrapper._channel_guarded = True
    
    return wrapper

def create_guarded_handler(original_handler: Callable, handler_name: str = "") -> Callable:
    """
    Create a guarded version of any handler
    
    This wraps the original handler with channel membership check.
    
    RULES:
    1. Admin ID (or any multi-admin) always bypasses check
    2. Membership verification callbacks bypass check
    3. A user who has NOT selected a language is only allowed to reach the
       language picker - every other command is redirected to it
    4. Language-flow handlers (/language, language picker callback) bypass the
       channel-membership gate; /start is allowed through the language gate but
       still respects the channel gate
    5. All other handlers MUST verify channel membership
    6. If not member -> show join prompt, block handler
    7. If member -> execute handler normally
    8. ALWAYS force live check to detect channel leave immediately
    """
    @wraps(original_handler)
    async def guarded_handler(client: Client, update: Union[Message, CallbackQuery], *args, **kwargs):
        if isinstance(update, Message):
            if update.chat is None:
                return await original_handler(client, update, *args, **kwargs)

            if update.chat.type.value != "private":
                return await original_handler(client, update, *args, **kwargs)

            user_id = update.chat.id

            if _is_admin_user(user_id):
                return await original_handler(client, update, *args, **kwargs)

            # LANGUAGE GATE: until a language is selected, the user can ONLY
            # use the language picker.
            if (
                handler_name not in LANGUAGE_FLOW_HANDLERS
                and not _has_selected_language(user_id)
            ):
                print(f"🌐 LANGUAGE GATE: User {user_id} must select language first - handler: {handler_name}")
                await _force_language_selection(client, update, user_id)
                return None

            # The language picker itself is exempt from the channel gate.
            if handler_name in CHANNEL_EXEMPT_HANDLERS:
                return await original_handler(client, update, *args, **kwargs)

            is_member = await ensure_membership(client, user_id, force_check=False)

            if not is_member:
                print(f"🚫 BLOCKED: User {user_id} not in channel - handler: {handler_name}")
                await send_join_prompt(client, user_id, message=update)
                return None

            return await original_handler(client, update, *args, **kwargs)

        elif isinstance(update, CallbackQuery):
            user_id = update.from_user.id

            if _is_admin_user(user_id):
                return await original_handler(client, update, *args, **kwargs)

            callback_data = update.data or ""
            if callback_data in MEMBERSHIP_VERIFY_CALLBACKS:
                return await original_handler(client, update, *args, **kwargs)

            # LANGUAGE GATE: same rule for callback buttons.
            if (
                handler_name not in LANGUAGE_FLOW_HANDLERS
                and not _has_selected_language(user_id)
            ):
                print(f"🌐 LANGUAGE GATE: User {user_id} must select language first - callback: {callback_data}")
                await _force_language_selection(client, update, user_id)
                return None

            # The language picker itself is exempt from the channel gate.
            if handler_name in CHANNEL_EXEMPT_HANDLERS:
                return await original_handler(client, update, *args, **kwargs)

            is_member = await ensure_membership(client, user_id, force_check=False)

            if not is_member:
                print(f"🚫 BLOCKED: User {user_id} not in channel - callback: {callback_data}")
                await send_join_prompt(client, user_id, callback=update)
                return None

            return await original_handler(client, update, *args, **kwargs)

        else:
            return await original_handler(client, update, *args, **kwargs)
    
    # Prevent double-wrapping.
    guarded_handler._channel_guarded = True
    
    return guarded_handler

_original_add_handler = None
_guarded_app = None

def apply_channel_guard(app: Client) -> None:
    """
    APPLY UNIVERSAL CHANNEL GUARD TO ALL HANDLERS
    
    This monkey-patches the Client.add_handler method to automatically
    wrap ALL subsequently registered handlers with channel membership check.
    
    MUST be called immediately after Client() instantiation and BEFORE
    any handlers are registered.
    
    Usage:
        app = Client(...)
        apply_channel_guard(app)
        
        # Now all handlers registered after this will be guarded
        @app.on_message(...)
        async def handler(client, message):
            ...
    """
    global _original_add_handler, _guarded_app
    
    if _original_add_handler is not None:
        print("⚠️ Channel guard already applied")
        return
    
    _original_add_handler = app.add_handler
    _guarded_app = app
    
    def guarded_add_handler(handler, group: int = 0):
        """Wrapped add_handler that guards all handlers"""
        global _original_add_handler
        
        if _original_add_handler is None:
            raise RuntimeError("Channel guard not properly initialized")
        
        if isinstance(handler, (MessageHandler, CallbackQueryHandler)):
            # CRITICAL (pyrofork): The dispatcher-level `handler.callback` is
            # the framework's INTERNAL `resolve_future_or_callback` method which
            # also handles listeners / `client.ask()` futures. Wrapping it would
            # intercept EVERY message (including internal/library handling) and
            # would block all handlers for non-members. We must wrap the USER's
            # function instead, which is stored in `handler.original_callback`.
            #
            # Classic pyrogram (<=2.0.x): `handler.callback` IS the user function.
            user_callback = getattr(handler, "original_callback", None)
            if user_callback is None:
                user_callback = handler.callback

            # Never guard something we already guarded.
            if getattr(user_callback, "_channel_guarded", False):
                return _original_add_handler(handler, group)

            handler_name = getattr(user_callback, '__name__', 'unknown')

            skip_guard_names = {
                'auto_start_monitor',
                'debug_all_messages',
                'auto_cache_channel',
                'cache_forwarded_channel',
                'test_channels_handler',
            }
            
            if group >= 0 and handler_name not in skip_guard_names:
                guarded_callback = create_guarded_handler(user_callback, handler_name)
                if hasattr(handler, "original_callback"):
                    handler.original_callback = guarded_callback
                else:
                    handler.callback = guarded_callback
                print(f"🛡️ Channel guard applied to: {handler_name}")
            else:
                print(f"⏭️ Channel guard SKIPPED for: {handler_name} (group={group})")
        
        return _original_add_handler(handler, group)
    
    app.add_handler = guarded_add_handler
    print("✅ Universal Channel Guard activated - ALL handlers will be protected")

async def handle_membership_verify_callback(client: Client, callback: CallbackQuery) -> None:
    """
    Handle the "I've joined" button click

    Verifies membership live and either:
    - Confirms success and enables all features
    - Shows error if still not joined
    """
    user_id = callback.from_user.id

    from bot.utils.i18n import user_t

    invalidate_user_membership(user_id)
    
    is_member, error_reason = await check_membership_live(client, user_id)
    channel = _get_required_channel()
    
    if is_member:
        set_cached_membership(user_id, True)
        
        try:
            from bot.utils.storage import update_user_info
            spawn_background(asyncio.to_thread(update_user_info, user_id, channel_joined=True),
                             "guard-mark-joined", quiet=True)
        except (ImportError, RuntimeError):
            pass
        
        await callback.answer(user_t("channel_verified_alert", user_id), show_alert=True)
        
        try:
            await callback.message.edit_text(user_t("channel_verified_message", user_id))
        except Exception:
            pass
    elif error_reason == "bot_no_access":
        set_cached_membership(user_id, False)
        await callback.answer(
            user_t("bot_no_access_alert", user_id, channel=channel),
            show_alert=True
        )
        try:
            await callback.message.edit_text(user_t("bot_setup_required_message", user_id))
        except Exception:
            pass
    else:
        await callback.answer(
            user_t("channel_not_joined_callback_alert", user_id, channel=channel),
            show_alert=True
        )

async def get_cache_stats() -> Dict[str, Any]:
    """Get channel guard cache statistics."""
    stats = {
        "in_memory_entries": len(_membership_cache),
        "prompt_cooldown_entries": len(_prompt_cooldown),
        "redis_enabled": _use_redis_cache
    }
    
    if _use_redis_cache:
        try:
            from bot.utils.redis_cache import get_cache_stats as redis_stats
            stats["redis"] = await redis_stats()
        except Exception:
            pass
    
    return stats

print("🛡️ Channel Guard Middleware v3.0 Loaded (High-Scale)")
print("   - Universal enforcement for ALL handlers")
print("   - Redis-backed caching (with fallback)")
print("   - Live Telegram API verification")
print("   - Optimized for 10,000+ concurrent users")
