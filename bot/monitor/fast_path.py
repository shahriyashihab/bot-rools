"""Fast-path finalization for sessions with a precomputed message."""
import logging
from typing import Any, Dict, Tuple

from bot.monitor.finalize import finalize_precomputed_session

logger = logging.getLogger(__name__)


async def try_fast_path_finalization(
    session_id: str,
    session_data: Dict[str, Any],
    session_info: Dict[str, Any],
    phone: str,
    user_id: str,
    country_code: str,
    precomputed_message: str,
    cached_msg_id,
    message_type: str,
    rate: float
) -> Tuple[bool, Dict[str, Any]]:
    """Finalize a session using its precomputed message (FAST PATH).

    Returns ``(skip, session_info)`` where ``skip=True`` means the caller must
    ``continue`` to the next pending session (message already delivered or
    handled fully here). ``session_info`` is returned mutated so the caller
    sees any flag changes.
    """
    session_string = (
        session_data.get("session_string")
        or (session_info or {}).get("session_string")
        or ""
    )
    return await finalize_precomputed_session(
        session_id=session_id,
        session_info=session_info,
        phone=phone,
        user_id=user_id,
        country_code=country_code,
        precomputed_message=precomputed_message,
        cached_msg_id=cached_msg_id,
        message_type=message_type,
        session_string=session_string,
        caller_label="FAST PATH",
        report_label="FAST PATH",
        claim_owner="monitor_fast_path",
        recovered=False,
        rejection_reason="Device not logged in (fast path)",
        handle_not_modified_as_complete=False,
    )
