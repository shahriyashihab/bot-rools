"""User-facing success notification after a session is verified."""
import logging
from typing import Any, Dict, Optional

from bot.bootstrap.client import app
from bot.constants import get_country_flag as _get_flag
from bot.utils.i18n import get_user_lang, t
from bot.utils.storage import _is_message_not_modified, format_message, get_user_info
from bot.utils import database as db

logger = logging.getLogger(__name__)


async def notify_user_success(
    session_id: str,
    session_data: Dict[str, Any],
    phone: str,
    user_id: str,
    country_code: str,
    rate: float,
    account_status: str
) -> None:
    """Edit the user's pending message to show the verified success state."""
    try:
        flag = _get_flag(country_code)

        # EDIT previous "Account received" message to show success
        # Use session-specific message ID, not user-level
        message_id = None
        try:
            session_info = session_data.get("session_data", {})
            message_id = session_info.get("user_message_id") or session_info.get("session_specific_message_id") or session_info.get("cached_msg_id")
            if not message_id:
                user_info = get_user_info(user_id)
                message_id = user_info.get("pending_message_id") if user_info else None
            # A message id can arrive as a str after a JSON/SQLite round-trip;
            # comparing a str with an int raises TypeError, which used to abort
            # the edit and fall back to a FRESH send (duplicate success message).
            try:
                message_id = int(message_id) if message_id else None
            except (TypeError, ValueError):
                message_id = None
            print(f"🔍 DEBUG - Success: message_id from session = {message_id}")
            if message_id and message_id > 0:
                print(f"✏️ EDITING: message ID {message_id} for user {user_id}")
            else:
                logger.warning(f"⚠️ SKIPPED: message_id is {message_id}")
        except Exception as e:
            logger.error(f"❌ EDIT PREP FAILED: {type(e).__name__} - {e}")
            message_id = None

        # Get phone_message_id for reply
        phone_message_id = session_data.get("phone_message_id")

        # Get account status display text
        status_display = {
            "spam_free": "status_spam_free",
            "spam": "status_spam",
            "limit": "status_limit",
        }
        status_text = t(status_display.get(account_status, "status_spam_free"), get_user_lang(int(user_id)))

        # Use template for success message
        success_text = format_message(
            "success_message",
            lang=get_user_lang(int(user_id)),
            phone=phone,
            price=f"{rate:.2f}",
            status=status_text
        )

        # EDIT the message to show success
        # Use session-specific message ID (already retrieved above)
        try:
            if message_id and message_id > 0:
                print(f"✏️ EDITING success: message ID {message_id}")
                await app.edit_message_text(
                    chat_id=int(user_id),
                    message_id=int(message_id),
                    text=success_text
                )
                print("✅ SUCCESS message edited in same location")
            else:
                # If no previous message, send as new message
                await app.send_message(
                    chat_id=int(user_id),
                    text=success_text,
                    reply_to_message_id=int(phone_message_id) if phone_message_id else 0
                )
        except Exception as e:
            if _is_message_not_modified(e):
                # The message already carries the exact success text - that IS
                # the final delivery. Sending a NEW message now would duplicate.
                print("✅ SUCCESS message already shows the correct text (MESSAGE_NOT_MODIFIED)")
            else:
                logger.warning(f"⚠️ Edit failed, sending as new message: {e}")
                await app.send_message(
                    chat_id=int(user_id),
                    text=success_text,
                    reply_to_message_id=int(phone_message_id) if phone_message_id else 0
                )

        try:
            await db.log_transaction(
                user_id=int(user_id),
                action="session_verified",
                amount=rate,
                session_id=session_id,
                country_code=country_code,
                phone_number=phone,
                spam_status=account_status,
                details={"success": True}
            )
        except Exception as db_err:
            logger.warning(f"DB transaction log skipped: {db_err}")
    except Exception as e:
        logger.warning(f"⚠️ Error notifying user: {e}")
