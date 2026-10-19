"""Channel membership verification callback - registered via bootstrap registration."""
import asyncio
import logging

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import CallbackQuery

from bot.bootstrap.client import app
from bot.middleware.channel_guard import (
    invalidate_user_membership,
    check_membership_live,
    set_cached_membership,
)
from bot.utils.bg_tasks import spawn_background

logger = logging.getLogger(__name__)


@app.on_callback_query(filters.regex(r"^(check_membership_verify|channel_joined_verify)$"))
async def handle_channel_membership_callback(client: Client, query: CallbackQuery):
    """
    UNIVERSAL Channel Membership Verification Callback

    This handles both:
    - check_membership_verify (new unified callback)
    - channel_joined_verify (legacy callback for backward compatibility)

    ALWAYS performs LIVE check (never uses cache for this callback).
    After successful verification, all handlers will work automatically.
    """
    user_id = query.from_user.id

    from bot.utils.i18n import user_t, get_user_lang

    invalidate_user_membership(user_id)

    from bot.middleware.channel_guard import check_membership_live
    is_member, error_reason = await check_membership_live(client, user_id)

    from bot.utils.storage import get_required_channel
    channel = get_required_channel()

    if is_member:
        set_cached_membership(user_id, True)

        try:
            from bot.utils.fast_cache import background_save_user
            spawn_background(background_save_user(user_id, channel_joined=True), "membership-save-user")
        except Exception as e:
            print(f"Background save error: {e}")

        await query.answer(user_t("channel_verified_alert", user_id), show_alert=True)

        verified_text = user_t("channel_verified_message", user_id)
        try:
            await query.message.edit_text(verified_text)
        except Exception as e:
            print(f"Could not edit message: {e}")
            try:
                await client.send_message(user_id, verified_text)
            except Exception:
                pass

        # First visit after language selection: greet with the welcome message
        # in the selected language so no extra /start is needed.
        from bot.utils.fast_cache import get_cached_template
        try:
            await client.send_message(
                user_id,
                get_cached_template("welcome_message", get_user_lang(user_id)),
            )
        except Exception as e:
            print(f"Welcome send error: {e}")

        print(f"Channel membership VERIFIED for user {user_id}")
    elif error_reason == "bot_no_access":
        set_cached_membership(user_id, False)

        bot_username = (await client.get_me()).username or "the bot"
        await query.answer(
            user_t("bot_no_access_alert", user_id, channel=channel),
            show_alert=True
        )

        try:
            await query.message.edit_text(
                user_t("bot_setup_required_message", user_id) + f"\n\n@{bot_username}"
            )
        except Exception:
            pass

        print(f"BOT NO ACCESS to {channel} - user {user_id} NOT approved (requires bot admin setup)")
    else:
        set_cached_membership(user_id, False)

        await query.answer(
            user_t("channel_not_joined_callback_alert", user_id, channel=channel),
            show_alert=True
        )
        print(f"Channel membership FAILED for user {user_id}")
