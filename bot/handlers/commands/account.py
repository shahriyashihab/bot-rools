"""Account command handler - registered via bootstrap registration."""
import logging

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import Message

from bot.bootstrap.client import app
from bot.handlers.account import register_account

logger = logging.getLogger(__name__)


@app.on_message(filters.command("account") & filters.private)
async def account_handler(client: Client, message: Message):
    """Account handler - Universal guard already enforces channel membership with force_check=True"""
    try:
        await register_account(client, message)
    except Exception as e:
        logger.error(f"ERROR in account_handler: {e}")
        import traceback
        traceback.print_exc()
        try:
            from bot.utils.i18n import user_t
            await message.reply(user_t("start_first_error_message", message.chat.id))
        except Exception:
            pass
