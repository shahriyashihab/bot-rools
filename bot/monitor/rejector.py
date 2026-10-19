"""Session rejection flow: notify user + log + report to channel."""
import logging
from typing import Any, Dict

from bot import config
from bot.bootstrap.client import app
from bot.constants import get_country_flag as _get_flag
from bot.data.proxies import record_proxy_session_outcome
from bot.services.reports import build_rejected_report, send_report_to_channel
from bot.utils import database as db
from bot.utils.i18n import get_user_lang
from bot.utils.storage import format_message, get_user_info, reject_pending_session

logger = logging.getLogger(__name__)


async def handle_rejection(
    session_id: str,
    session_data: Dict[str, Any],
    phone: str,
    user_id: str,
    country_code: str,
    rate: float,
    reject_reason: str = "Device not logged in after timeout",
    report_label: str = "",
) -> None:
    """Notify the user, log the failed verification and report to the channel."""
    # Session is NOT logged in - REJECT!
    logger.error(f"❌ Session {session_id} validation failed - device not logged in")

    reject_pending_session(session_id, reject_reason)

    # Notify user - EDIT previous "Account received" message instead of deleting
    # Use session-specific message ID, not user-level
    user_notified = False
    try:
        session_info = session_data.get("session_data", {})
        message_id = session_info.get("user_message_id") or session_info.get("session_specific_message_id") or session_info.get("cached_msg_id")
        if not message_id:
            user_info = get_user_info(user_id)
            message_id = user_info.get("pending_message_id") if user_info else None
        # Message ids come back from JSON/SQLite as ``str`` often enough that the
        # bare ``message_id > 0`` below raised TypeError, which the outer handler
        # swallowed - so the user was NEVER told their account was rejected.
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            message_id = 0
        print(f"🔍 DEBUG - Rejection: message_id from session = {message_id}")

        flag = _get_flag(country_code)

        rejection_text = format_message(
            "verification_failed_message",
            lang=get_user_lang(int(user_id)),
            phone=phone,
            country_code=country_code,
            flag=flag
        )

        if message_id > 0:
            try:
                print(f"✏️ EDITING rejection: message ID {message_id} for user {user_id}")
                await app.edit_message_text(
                    chat_id=int(user_id),
                    message_id=int(message_id),
                    text=rejection_text
                )
                user_notified = True
                print("✅ REJECTION message edited in same location")
            except Exception as e:
                logger.warning(f"⚠️ Edit failed, sending as new message: {e}")
                await app.send_message(chat_id=int(user_id), text=rejection_text)
                user_notified = True
        else:
            logger.warning(f"⚠️ SKIPPED: message_id is {message_id}, sending as new message")
            await app.send_message(chat_id=int(user_id), text=rejection_text)
            user_notified = True
    except Exception as e:
        logger.warning(f"⚠️ Error notifying user {user_id}: {e}")

    # P7-70: a failed notification (FloodWait / user blocked / transient) used
    # to be dropped forever - the user never learned their account was
    # rejected. Persist a marker so the hourly reconcile re-attempts the
    # delivery with backoff (reconcile's Orphan-B path is idempotent).
    if not user_notified:
        try:
            from bot.utils.storage import update_pending_session_field
            update_pending_session_field(session_id, "rejection_notify_pending", True)
            logger.warning(
                f"⚠️ Rejection notification for {phone} failed - marked "
                f"rejection_notify_pending for reconcile retry"
            )
        except Exception:
            pass

    try:
        await db.log_failed_verification(
            user_id=int(user_id),
            reason=reject_reason,
            phone_number=phone,
            country_code=country_code,
            error_type="session_timeout"
        )
    except Exception as db_err:
        logger.warning(f"DB failed verification log skipped: {db_err}")

    try:
        rejection_report = build_rejected_report(
            phone, country_code, reject_reason,
            report_label=report_label or "Not Logged In", user_id=user_id, rate=rate,
        )
        await send_report_to_channel(config.get_channel("CHANNEL_REJECTED"), rejection_report)
        print(f"📤 Rejection report sent to CHANNEL_REJECTED for {phone}")
    except Exception as e:
        logger.warning(f"⚠️ Error sending rejection notice: {e}")

    try:
        _inner = session_data.get("session_data") or {}
        _px_used = session_data.get("proxy_used") or _inner.get("proxy_used") or ""
        _px_attr = session_data.get("proxy_attribution") or _inner.get("proxy_attribution") or country_code
        record_proxy_session_outcome(_px_attr, _px_used, "rejected")
    except Exception:
        pass
