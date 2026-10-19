"""Debug handler that logs ALL incoming messages."""
import logging

from pyrogram import filters
from pyrogram.types import Message

from bot.bootstrap.client import app
from bot.utils.text import truncate

logger = logging.getLogger(__name__)


@app.on_message(group=-1000)
async def debug_all_messages(client, message: Message):
    """Debug handler to log ALL incoming messages."""
    preview = truncate(message.text, 50)
    print(f"🔔 DEBUG: Received message from {message.chat.id}: {preview if message.text else 'NO TEXT'}")
