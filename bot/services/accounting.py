"""Accounting service: session balance credit + analytics + transaction log.

Extracted from the SUCCESS branches that were duplicated across
``monitor/fast_path.py``, ``monitor/recovery_scan.py`` and
``monitor/approval.py``. Every verification path (fast path, recovery,
full validation) now credits balance, updates the daily/country analytics
and writes the transaction log through this single module.
"""
import asyncio
import logging
from typing import Optional

from bot.utils.database import record_session_analytics, log_transaction, upsert_session
from bot.utils.fast_cache import sync_user_from_storage
from bot.utils.storage import (
    atomic_credit_balance_for_session,
    get_session_spam_score,
    get_pending_session,
)
from bot.utils.bg_tasks import spawn_background

logger = logging.getLogger(__name__)


async def credit_and_log_session(
    session_id: str,
    user_id: int,
    country_code: str,
    rate: float,
    phone: str = "",
    spam_status: str = "",
    log_action: str = "session_verified",
    include_log: bool = True,
) -> bool:
    """Atomically credit a user's balance for a verified session.

    On a successful (non-duplicate) credit this also refreshes the user
    cache, records the session analytics row and (unless ``include_log`` is
    False) queues the transaction log entry.

    Returns ``True`` only when the balance was actually credited.
    """
    credit_success = atomic_credit_balance_for_session(session_id, int(user_id), rate)
    if not credit_success:
        logger.warning("⚠️ Balance already credited or session not found - SKIPPING duplicate credit")
        return False

    sync_user_from_storage(int(user_id))
    print(f"💰 Balance ${rate} credited to user {user_id}")

    try:
        await record_session_analytics(
            session_id=session_id,
            country_code=country_code,
            successful=True,
            amount=rate,
            spam_score=get_session_spam_score(session_id),
        )
    except Exception as analytics_err:
        logger.warning(f"⚠️ session analytics update failed: {analytics_err}")

    try:
        pending = get_pending_session(session_id) or {}
        await upsert_session(session_id, {
            "user_id": int(user_id),
            "phone": phone or pending.get("phone", ""),
            "country_code": country_code or pending.get("country_code", ""),
            "spam_status": spam_status or pending.get("spam_status", ""),
            "session_string": pending.get("session_string", ""),
        })
    except Exception as session_err:
        logger.warning(f"⚠️ session upsert failed: {session_err}")

    if include_log:
        try:
            spawn_background(log_transaction(
                user_id=int(user_id),
                action=log_action,
                amount=rate,
                session_id=session_id,
                country_code=country_code,
                phone_number=phone,
                spam_status=spam_status,
            ), "accounting-log-transaction")
        except Exception as log_err:
            logger.warning(f"⚠️ DB Log error: {log_err}")

    return True
