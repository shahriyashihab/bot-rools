"""Pending session field update helper."""
import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def _safe_update_pending_session(session_id: str, field: str, value: Any):
    """Safely update pending session field with error handling."""
    try:
        from bot.utils.storage import update_pending_session_field
        await asyncio.to_thread(update_pending_session_field, session_id, field, value)
    except Exception as e:
        logger.warning(f"⚠️ Background update_pending_session_field failed for {session_id}: {e}")
