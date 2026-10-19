"""Forwarded channel cache handler (Admin only) - registered via bootstrap registration."""
import logging

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import Message

from bot import config
from bot.bootstrap.client import app
from bot.config import ADMIN_ID

logger = logging.getLogger(__name__)


@app.on_message(filters.private & filters.forwarded)
async def cache_forwarded_channel(client: Client, message: Message):
    """Cache channel when admin forwards a message from it"""
    user_id = message.chat.id

    if user_id != ADMIN_ID:
        return

    if message.forward_from_chat:
        chat = message.forward_from_chat
        chat_id = chat.id
        chat_title = chat.title or "Unknown"
        chat_type = chat.type

        print(f"✅ FORWARD CACHED: {chat_title} (ID: {chat_id}, Type: {chat_type})")

        channel_names = {
            config.get_channel("CHANNEL_SUBMIT"): "CHANNEL_SUBMIT (Pending)",
            config.get_channel("CHANNEL_VERIFIED"): "CHANNEL_VERIFIED",
            config.get_channel("CHANNEL_REJECTED"): "CHANNEL_REJECTED",
            config.get_channel("CHANNEL_WITHDRAW_REPORT"): "CHANNEL_WITHDRAW_REPORT",
            config.get_channel("CHANNEL_24HR_UP"): "CHANNEL_24HR_UP"
        }

        config_name = channel_names.get(chat_id, "Unknown Config")

        from bot.utils.i18n import admin_t
        await message.reply(
            admin_t("channel_cached_message", user_id,
                    chat_title=chat_title, chat_id=chat_id, chat_type=chat_type,
                    config_name=config_name)
        )
    else:
        from bot.utils.i18n import admin_t
        await message.reply(admin_t("not_forwarded_message", user_id))
