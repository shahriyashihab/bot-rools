"""Start command handler - registered via bootstrap registration."""
import logging

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import Message

from bot.bootstrap.client import app
from bot.handlers.start import register_start

logger = logging.getLogger(__name__)


@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    """Start handler - Universal guard already enforces channel membership with force_check=True"""
    user_id = message.chat.id
    print(f"RECEIVED /start from user {user_id}")

    try:
        from bot.utils.session_handler import cleanup_session
        await cleanup_session(user_id)
        await register_start(client, message)
        print(f"/start handler completed for user {user_id}")
    except Exception as e:
        logger.error(f"ERROR in start_handler: {e}")
        import traceback
        traceback.print_exc()
        try:
            from bot.utils.i18n import user_t
            await message.reply(user_t("generic_error_message", message.chat.id))
        except Exception:
            pass
