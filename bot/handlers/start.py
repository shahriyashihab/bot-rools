from pyrogram.client import Client
from pyrogram.types import Message
import asyncio
from bot.utils.bg_tasks import spawn_background

async def register_start(client: Client, message: Message):
    """
    Handles the /start command - ULTRA FAST (< 1ms response)
    Pattern: ALWAYS respond instantly, validate in background
    NEVER awaits external API or storage before sending reply
    ALL I/O operations deferred to background tasks

    Flow: banned check -> language selection (first visit) -> welcome
    """
    user_id = message.chat.id

    # P4-47: /start means "I am a USER now". Any leftover admin panel state
    # (e.g. a view action like "core_config_view" left behind after browsing
    # the admin panel) MUST NOT survive into the user flow - otherwise the
    # admin text fallback / admin guard would swallow the admin's next plain
    # text (their phone submission) and the OTP code would never be sent.
    # Admin == user from this point on. Lazy import: admin handlers register
    # AFTER this module at boot, so the import must happen at call time.
    try:
        from bot.handlers.admin.router import _admin_manager
        _admin_manager.clear_state(user_id)
    except Exception:
        pass

    from bot.utils.fast_cache import (
        is_user_banned_fast, 
        get_cached_template,
        background_save_user,
        background_warm_user_cache
    )
    from bot.handlers.withdraw import withdraw_sessions
    from bot.utils.storage import clear_withdraw_state
    from bot.utils.i18n import get_user_lang, has_explicit_lang
    from bot.handlers.language import send_language_selection

    if user_id in withdraw_sessions:
        withdraw_sessions.pop(user_id, None)
    clear_withdraw_state(user_id)
    
    if is_user_banned_fast(user_id):
        await message.reply(get_cached_template("banned_user_message", get_user_lang(user_id)))
        return

    # First visit: no language selected yet -> show the language picker first.
    if not has_explicit_lang(user_id):
        await send_language_selection(client, user_id, message=message)
        return

    lang = get_user_lang(user_id)
    # Welcome message in the user's chosen language (falls back to English).
    welcome_text = get_cached_template("welcome_message", lang)
    await message.reply(welcome_text)
    
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""
    spawn_background(background_save_user(user_id, username=username, first_name=first_name),
                     "start-save-user")
    spawn_background(background_warm_user_cache(user_id), "start-warm-cache")
