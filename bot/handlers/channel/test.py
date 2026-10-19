"""Test channels command handler (Admin only) - registered via bootstrap registration."""
import logging

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import Message

from bot import config
from bot.bootstrap.client import app
from bot.config import ADMIN_ID

logger = logging.getLogger(__name__)


@app.on_message(filters.command("test_channels") & filters.private)
async def test_channels_handler(client: Client, message: Message):
    """Test all channel connections"""
    if message.chat.id != ADMIN_ID:
        return

    from bot.utils.i18n import admin_t
    await message.reply(admin_t("testing_channels_message", message.chat.id))

    channels = {
        "CHANNEL_SUBMIT": config.get_channel("CHANNEL_SUBMIT"),
        "CHANNEL_VERIFIED": config.get_channel("CHANNEL_VERIFIED"),
        "CHANNEL_REJECTED": config.get_channel("CHANNEL_REJECTED"),
        "CHANNEL_WITHDRAW_REPORT": config.get_channel("CHANNEL_WITHDRAW_REPORT"),
        "CHANNEL_24HR_UP": config.get_channel("CHANNEL_24HR_UP")
    }

    results = []
    for name, channel_id in channels.items():
        try:
            chat = await client.get_chat(channel_id)
            results.append(f"✅ {name}: {chat.title}")
        except Exception as e:
            results.append(f"❌ {name}: {str(e)[:40]}")

    result_text = "\n".join(results)
    from bot.utils.i18n import admin_t
    await message.reply(admin_t("channel_status_message", message.chat.id, result_text=result_text))
