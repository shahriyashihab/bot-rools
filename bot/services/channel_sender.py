"""Channel message sending with retry logic."""
import os
import asyncio
import logging
from typing import Optional, Union

from pyrogram.errors import FloodWait
from pyrogram.types import Message

from bot.bootstrap.client import app

logger = logging.getLogger(__name__)


async def send_to_channel_with_retry(
    chat_id: Union[int, str],
    message: Optional[str] = None,
    document: Optional[str] = None,
    caption: Optional[str] = None,
    max_retries: int = 3
) -> Optional[Message]:
    """
    Send message to channel with robust retry mechanism.

    Args:
        chat_id: Channel ID or username
        message: Text message to send
        document: Path to document file
        caption: Caption for document
        max_retries: Maximum retry attempts (default 3)

    Returns:
        Sent message object on success, or None ONLY after every retry failed.
        The return value is meaningful: callers MUST check it so a failed
        session/JSON delivery is never silently treated as success.
    """
    if chat_id is None:
        logger.warning("send_to_channel_with_retry: chat_id is None, skipping")
        return None

    last_error = None
    for attempt in range(max(1, max_retries)):
        try:
            if document:
                if not os.path.exists(document):
                    logger.error(f"Document file not found: {document}")
                    raise FileNotFoundError(f"Document file not found: {document}")
                result = await app.send_document(chat_id=chat_id, document=document, caption=caption or "")
                if result is not None:
                    return result
                raise RuntimeError("send_document returned None")
            else:
                if not message:
                    raise ValueError("Message cannot be empty")
                result = await app.send_message(chat_id=chat_id, text=message)
                if result is not None:
                    return result
                raise RuntimeError("send_message returned None")
        except FloodWait as e:
            wait_time = int(e.value) if isinstance(e.value, (int, float)) else 2
            # Cap a single FloodWait sleep so a huge wait cannot hang the
            # success report forever; the retry loop will re-attempt after.
            wait_time = min(wait_time, 60)
            logger.warning(f"FloodWait: waiting {wait_time}s before retry (attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(wait_time)
            last_error = e
        except Exception as e:
            last_error = e
            logger.error(f"Send to channel error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
    logger.error(f"send_to_channel_with_retry: giving up after {max_retries} attempt(s) (chat_id={chat_id}, last_error={last_error})")
    return None
