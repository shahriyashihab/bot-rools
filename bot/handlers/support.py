from pyrogram.client import Client
from pyrogram import filters
from pyrogram.types import Message
from bot.config import ADMIN_ID

def register_support(app: Client):
    """Register support handler - PROTECTED BY UNIVERSAL CHANNEL GUARD"""

    @app.on_message(filters.private & filters.command("support"))
    async def support_handler(client: Client, message: Message):
        """Support handler - Universal guard already enforces channel membership with force_check=True"""
        from bot.utils.fast_cache import get_cached_template
        from bot.utils.i18n import get_user_lang
        text = get_cached_template("support_message", get_user_lang(message.chat.id))
        await message.reply_text(text)
