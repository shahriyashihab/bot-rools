from pyrogram.client import Client
from pyrogram.types import Message
from bot.utils.storage import get_required_channel
from datetime import datetime, timezone, timedelta
import asyncio
from bot.utils.bg_tasks import spawn_background


async def register_account(client: Client, message: Message):
    """
    Handles /account command - ULTRA FAST (< 1ms response)
    NEVER awaits external API or storage before sending reply
    ALL I/O operations deferred to background tasks
    """
    user_id = message.chat.id

    from bot.utils.fast_cache import (
        is_user_banned_fast,
        get_cached_template,
        get_cached_user,
        get_user_balance_fast,
        background_channel_check,
        background_warm_user_cache,
        format_message_fast,
    )
    from bot.utils.i18n import get_user_lang

    if is_user_banned_fast(user_id):
        await message.reply(get_cached_template("banned_user_message", get_user_lang(user_id)))
        return

    # Compute payment-accurate account stats from the REAL stored session data
    from bot.utils.storage import get_user_account_stats
    stats = get_user_account_stats(user_id)

    # Date/time with the admin-configured timezone
    from bot.data.settings import get_account_timezone_offset, get_account_timezone_label
    tz_offset = get_account_timezone_offset()
    tz = timezone(timedelta(hours=tz_offset))
    now = datetime.now(tz)
    date_str = now.strftime("%m/%d/%y")
    time_str = now.strftime("%H:%M:%S") + f" ({get_account_timezone_label()})"

    lang = get_user_lang(user_id)
    text = format_message_fast(
        "account_stats_message", lang,
        user_id=user_id,
        verified=stats['verified'],
        unverified=stats['unverified'],
        available_balance=f"{stats['available_balance']:.2f}",
        frozen=stats['frozen'],
        frozen_balance=f"{stats['frozen_balance']:.2f}",
        date=date_str,
        time=time_str,
    )

    await message.reply(text)

    channel = get_required_channel()
    spawn_background(background_channel_check(client, user_id, channel), "account-channel-check")