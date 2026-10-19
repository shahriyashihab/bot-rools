"""Refactored from bot.utils.pending_recovery: No-OTP session cleanup helpers."""
import logging
from datetime import datetime

from pyrogram.client import Client

from bot.recovery.constants import EXPIRED_NO_OTP_MINUTES, NO_OTP_WARNING_MINUTES
from bot.utils.storage import get_all_pending_sessions, set_pending_session_field

logger = logging.getLogger(__name__)


def _safe_int(value, default: int = 0) -> int:
    """Safely convert value to int, return default if fails."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _localized(user_id, key: str, **kwargs) -> str:
    """Render ``key`` in the user's own language, falling back to English."""
    from bot.utils.i18n import get_user_lang
    from bot.utils.storage import format_message
    lang = "en"
    try:
        lang = get_user_lang(int(user_id))
    except (TypeError, ValueError):
        pass
    return format_message(key, lang=lang, **kwargs)


async def _notify(bot_client, user_id, text: str, label: str) -> bool:
    """Best-effort DM. A blocked/deleted chat must never abort the sweep."""
    client = bot_client
    if client is None:
        try:
            from bot.bootstrap.client import app as client
        except Exception as err:
            logger.warning(f"⚠️ NO-OTP {label}: no client available: {err}")
            return False
    try:
        await client.send_message(chat_id=int(user_id), text=text)
        return True
    except Exception as err:
        logger.warning(f"⚠️ NO-OTP {label}: could not notify {user_id}: {err}")
        return False


async def cleanup_no_otp_sessions(bot_client: Client = None) -> int:
    """Warn, then cancel, submissions whose OTP never arrived.

    A submission with no ``session_string`` never completed the login step. It
    used to be hard-deleted with NO notification at all, so a slow SMS gateway
    (routine on high-risk countries) silently destroyed the user's submission and
    they were never told why. The flow is now:

    * at ``NO_OTP_WARNING_MINUTES`` the user gets a localized warning naming the
      exact number of minutes left (idempotent via the persisted ``no_otp_warned``
      flag, because this sweep runs hourly AND on every startup), and
    * at ``EXPIRED_NO_OTP_MINUTES`` the submission is cancelled and the user is
      told - deletion happens even if the message could not be delivered, so a
      user who blocked the bot cannot leak rows forever.

    Returns the number of sessions deleted.
    """
    from bot.utils.storage import delete_pending_session

    all_pending = get_all_pending_sessions()
    now = datetime.now()
    deleted_count = 0
    warned_count = 0

    for session_id, session_data in list(all_pending.items()):
        if session_data.get("status") != "pending":
            continue

        inner_data = session_data.get("session_data", {})
        if not isinstance(inner_data, dict):
            inner_data = {}
        session_string = session_data.get("session_string") or inner_data.get("session_string")

        if session_string:
            continue

        submitted_str = session_data.get("submitted_at") or session_data.get("created_at")
        if not submitted_str:
            continue

        try:
            clean_str = str(submitted_str).replace("Z", "").replace("+00:00", "")
            submitted_at = datetime.fromisoformat(clean_str)
        except (ValueError, TypeError):
            continue

        age_minutes = (now - submitted_at).total_seconds() / 60
        phone = session_data.get("phone", "Unknown")
        user_id = session_data.get("user_id")

        if age_minutes > EXPIRED_NO_OTP_MINUTES:
            # Tell the user FIRST, then delete. The delete is unconditional: a
            # failed DM must not keep the row alive forever.
            if user_id:
                await _notify(
                    bot_client,
                    user_id,
                    _localized(
                        user_id,
                        "no_otp_cancelled_message",
                        phone=phone,
                        minutes=EXPIRED_NO_OTP_MINUTES,
                    ),
                    "cancel",
                )
            try:
                if delete_pending_session(session_id):
                    deleted_count += 1
                    print(f"🗑️ AUTO-DELETE: {phone} - no OTP after {int(age_minutes)} min")
            except Exception as e:
                print(f"⚠️ Delete error: {e}")
            continue

        if age_minutes < NO_OTP_WARNING_MINUTES:
            continue

        already_warned = bool(
            session_data.get("no_otp_warned") or inner_data.get("no_otp_warned")
        )
        if already_warned or not user_id:
            continue

        minutes_left = max(1, int(round(EXPIRED_NO_OTP_MINUTES - age_minutes)))
        sent = await _notify(
            bot_client,
            user_id,
            _localized(
                user_id, "no_otp_warning_message", phone=phone, minutes=minutes_left
            ),
            "warn",
        )
        # Persist the flag either way: a user who blocked the bot must not be
        # retried on every hourly sweep AND every startup forever.
        try:
            set_pending_session_field(session_id, "no_otp_warned", True)
            session_data["no_otp_warned"] = True
        except Exception as err:
            logger.warning(f"⚠️ NO-OTP warn: could not persist flag for {session_id}: {err}")
        if sent:
            warned_count += 1
            print(f"⏳ NO-OTP WARNING: {phone} - {minutes_left} min left before cancel")

    if warned_count > 0:
        print(f"⏳ Warned {warned_count} sessions about a missing OTP")
    if deleted_count > 0:
        print(f"🧹 Cancelled {deleted_count} sessions (no OTP given)")

    return deleted_count
