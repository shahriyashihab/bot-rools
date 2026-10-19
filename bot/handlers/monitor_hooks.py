"""Auto-start pending session monitor on first bot message (backup)."""
import logging

from pyrogram import filters
from pyrogram.types import Message

from bot.bootstrap.client import app
from bot.monitor.state import start_monitor_on_activity

logger = logging.getLogger(__name__)


@app.on_message(group=-999)  # Run FIRST, before all other handlers
async def auto_start_monitor(client, message: Message):
    """Auto-start pending session monitor on first bot message (backup)."""
    start_monitor_on_activity()
