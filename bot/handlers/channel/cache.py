"""Auto-cache channel messages - registered via bootstrap registration."""
import logging

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import Message

from bot import config
from bot.bootstrap.client import app

logger = logging.getLogger(__name__)


@app.on_message(filters.channel)
async def auto_cache_channel(client: Client, message: Message):
    """Automatically cache any channel the bot receives messages from"""
    chat_id = message.chat.id
    chat_title = message.chat.title or "Unknown"

    channel_names = {
        config.get_channel("CHANNEL_SUBMIT"): "CHANNEL_SUBMIT",
        config.get_channel("CHANNEL_VERIFIED"): "CHANNEL_VERIFIED",
        config.get_channel("CHANNEL_REJECTED"): "CHANNEL_REJECTED",
        config.get_channel("CHANNEL_WITHDRAW_REPORT"): "CHANNEL_WITHDRAW_REPORT",
        config.get_channel("CHANNEL_24HR_UP"): "CHANNEL_24HR_UP"
    }

    config_name = channel_names.get(chat_id, "Other")
    print(f"✅ AUTO-CACHED CHANNEL: {chat_title} (ID: {chat_id}) - {config_name}")
