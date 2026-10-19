"""Cancel command handler - registered via bootstrap registration."""
import logging

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import Message

from bot.bootstrap.client import app
from bot.handlers import admin as admin_module

logger = logging.getLogger(__name__)


@app.on_message(filters.command("cancel") & filters.private & ~admin_module.admin_filter)
async def cancel_handler(client: Client, message: Message):
    """Cancel handler - Universal guard already enforces channel membership with force_check=True"""
    user_id = message.chat.id

    from bot.utils.session_handler import cleanup_session
    await cleanup_session(user_id)

    from bot.handlers.withdraw import withdraw_sessions
    from bot.utils.storage import clear_withdraw_state
    if user_id in withdraw_sessions:
        withdraw_sessions.pop(user_id, None)
    clear_withdraw_state(user_id)

    from bot.utils.i18n import user_t
    await message.reply(user_t("cancel_session_message", user_id))
    # Hard stop: /cancel must never reach any other handler
    # (e.g. main.handle_text_input in group 1).
    message.stop_propagation()
