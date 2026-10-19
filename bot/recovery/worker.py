"""Refactored from bot.utils.pending_recovery: Single-session recovery worker."""
import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, Tuple

from pyrogram.client import Client

from bot import config
from bot.data.proxies import record_proxy_session_outcome
from bot.recovery.constants import (
    DELAY_BETWEEN_SESSIONS,
    MULTI_DEVICE_24H_WINDOW_HOURS,
    MULTI_DEVICE_GRACE_MINUTES,
)
from bot.recovery.validity import check_session_validity_with_2fa
from bot.services.reports import (
    build_multi_device_report,
    build_rejected_report,
    build_success_report,
    send_report_to_channel,
)
# NOTE: MUST stay module-level. A function-scoped `from ... import X` makes X a
# LOCAL for the WHOLE function body, so any use of X on a path that did not
# execute the import statement raises UnboundLocalError.
from bot.services.session_reporting import (
    _all_attempted_formats_delivered,
    report_pending_or_multidevice,
    report_success_session,
)
from bot.utils.country_data import get_flag
from bot.utils.fast_cache import sync_user_from_storage
from bot.utils.i18n import get_user_lang, t
from bot.utils.proxy_connector import get_pinned_proxy_url
from bot.utils.session_handler import (
    determine_final_message_type,
    get_country_rate,
    get_rotated_api,
)
from bot.utils.storage import (
    _deadline_is_in_future,
    _is_message_not_modified,
    approve_pending_session,
    atomic_credit_balance_for_session,
    format_message,
    get_pending_session,
    is_session_message_already_sent,
    get_country_setting_bool,
    get_country_setting_bool,
    mark_session_finalization_claimed_by,
    mark_session_message_sent,
    reject_pending_session,
    replace_pending_session_data,
    try_claim_session_finalization,
    update_pending_session_field,
    try_claim_channel_report,
    release_channel_report,
)
from bot.verification.device import (
    live_device_verdict,
    VERDICT_SUCCESS,
    VERDICT_MULTI_DEVICE,
    VERDICT_REJECTED,
    VERDICT_UNKNOWN,
)
from bot.utils.bg_tasks import spawn_background

logger = logging.getLogger(__name__)

# Recovery concurrency cap scales with the API pool like the verification
# check phase (bot.verification.state): ``max(RECOVERY_CONCURRENCY_MIN,
# RECOVERY_PER_API_CONCURRENCY * <API count>)``. The API count includes both
# .env EXTRA_API_* credentials and admin-panel runtime-added APIs, so adding
# APIs automatically raises how many sessions recovery can process at once.
# An explicit RECOVERY_MAX_CONCURRENCY env override still wins for manual
# tuning.


def _env_int(name: str, default: int) -> int:
    """Read an env var as a positive-safe int, NEVER raising on bad input."""
    try:
        value = int(os.getenv(name, str(default)) or str(default))
        return value if value >= 0 else default
    except (TypeError, ValueError):
        return default


_RECOVERY_PER_API_CONCURRENCY = _env_int("RECOVERY_PER_API_CONCURRENCY", 2)
_RECOVERY_CONCURRENCY_MIN = _env_int("RECOVERY_CONCURRENCY_MIN", 20)


def _resolve_recovery_concurrency() -> int:
    override = _env_int("RECOVERY_MAX_CONCURRENCY", 0)
    if override > 0:
        return override
    api_count = 1
    try:
        from bot.utils.api_rotation import get_api_manager
        manager = get_api_manager()
        if manager.apis:
            api_count = len(manager.apis)
    except Exception:
        pass
    return max(_RECOVERY_CONCURRENCY_MIN, _RECOVERY_PER_API_CONCURRENCY * api_count)


_recovery_semaphore = asyncio.Semaphore(_resolve_recovery_concurrency())
_api_rotation_lock = asyncio.Lock()

# Error markers that prove the SESSION/ACCOUNT is definitively dead (not a
# transient network/flood issue). Used to classify recovery exceptions so a
# good account is never rejected on a temporary blip.
_DEFINITIVE_DEAD_MARKERS = (
    "AUTH_KEY",
    "SESSION_REVOKED",
    "USER_DEACTIVATED",
    "USER_DELETED",
    "BANNED",
    "DEACTIVAT",
    "AUTH_KEY_UNREGISTERED",
    "UNREGISTERED",
    "PHONE_NUMBER_UNOCCUPIED",
)


def _is_definitive_dead_session_error(exc) -> bool:
    """True only when the exception proves the session/account is dead."""
    try:
        from pyrogram.errors import AuthKeyUnregistered, UserDeactivated, SessionRevoked
        if isinstance(exc, (AuthKeyUnregistered, UserDeactivated, SessionRevoked)):
            return True
    except Exception:
        pass
    try:
        s = str(exc).upper()
    except Exception:
        return False
    return any(m in s for m in _DEFINITIVE_DEAD_MARKERS)


def _unresolved_md_session(session_data: Dict[str, Any], inner: Dict[str, Any]) -> bool:
    """True when a session is a multi-device hold that has NOT been resolved.

    The session is flagged (has_extra_devices) OR its MULTI-DEVICE verdict was
    already delivered but the 24h flow never completed (no balance, no logged-out
    or expired markers).  ``balance_credited`` is the only terminal gate.
    """
    md_flagged = bool(inner.get("has_extra_devices") or session_data.get("has_extra_devices"))
    md_typed_sent = bool(
        inner.get("message_type") == "MULTI-DEVICE"
        and inner.get("message_send_success") is True
    )
    if not (md_flagged or md_typed_sent):
        return False
    if session_data.get("balance_credited") is True or inner.get("balance_credited") is True:
        return False
    return True


def _md_window_passed(session_data: Dict[str, Any], inner: Dict[str, Any]) -> bool:
    """True when the 24h + grace window has elapsed for a multi-device session."""
    anchor = (
        session_data.get("added_at")
        or inner.get("added_at")
        or session_data.get("submitted_at")
        or inner.get("submitted_at")
        or session_data.get("created_at")
        or ""
    )
    if not anchor:
        return False
    try:
        anchor_dt = datetime.fromisoformat(str(anchor).replace("Z", "").replace("+00:00", ""))
    except (ValueError, TypeError):
        return False
    hours = (datetime.now() - anchor_dt).total_seconds() / 3600
    return hours >= MULTI_DEVICE_24H_WINDOW_HOURS + (MULTI_DEVICE_GRACE_MINUTES / 60.0)


async def recover_single_session(
    session_id: str,
    session_data: Dict[str, Any],
    bot_client: Client
) -> Tuple[bool, str]:
    """
    Recover a single pending session with rate limiting via semaphore.
    Uses lock to ensure unique API credentials per task.
    
    Returns:
        (success, result_type) - result_type: SUCCESS, MULTI-DEVICE, REJECTED,
        PENDING (no definitive verdict - retry later), or a skip marker.
    """
    async with _recovery_semaphore:
        async with _api_rotation_lock:
            api_id, api_hash, api_name = get_rotated_api()
        # Rate-limit spacing must NOT hold the API rotation lock: holding it
        # here serializes every recovery worker (only one can sleep at a time),
        # which throttles a large backlog to ~1 session/second. Sleeping under
        # the semaphore (but outside the lock) keeps the spacing while letting
        # all workers proceed in parallel.
        await asyncio.sleep(DELAY_BETWEEN_SESSIONS)
        return await _recover_single_session_impl(session_id, session_data, bot_client, api_id, api_hash)


def refresh_recovery_concurrency() -> int:
    """Grow the recovery semaphore to match the CURRENT API pool size.

    Grow-only (shrinking is unsafe). Call after ``add_api_runtime`` so adding
    more APIs at runtime immediately raises how many sessions recovery can
    process in parallel - without a restart. An explicit
    RECOVERY_MAX_CONCURRENCY override still wins.
    """
    target = _resolve_recovery_concurrency()
    try:
        current = getattr(_recovery_semaphore, "_value", 0)
    except Exception:
        return target
    if target > current:
        for _ in range(target - current):
            try:
                _recovery_semaphore.release()
            except Exception:
                break
        print(f"🔧 RECOVERY concurrency grown: {current} → {target}")
    return target

async def _recover_single_session_impl(
    session_id: str,
    session_data: Dict[str, Any],
    bot_client: Client,
    api_id: int,
    api_hash: str
) -> Tuple[bool, str]:
    """
    Internal implementation of single session recovery.
    
    ENHANCED v2.0: Full verification flow during recovery:
    1. Session validity check
    2. 2FA setup (if not done before restart)
    3. Spam check
    4. Multi-device check
    5. Send appropriate message to user
    """
    try:
        phone = session_data.get("phone", "UNKNOWN")
        user_id = session_data.get("user_id")
        country_code = session_data.get("country_code", "UNKNOWN")
        
        inner_session_data = session_data.get("session_data", {})

        # A multi-device session whose 24h+grace window fully passed without
        # resolution (MULTI-DEVICE message delivered / flag set, no balance,
        # no logged-out/expired markers) is NOT terminal: its message-sent
        # marker alone must not exclude it from a fresh live re-verification.
        _md_recovery_past_window = bool(
            _unresolved_md_session(session_data, inner_session_data)
            and inner_session_data.get("message_send_success") is True
            and _md_window_passed(session_data, inner_session_data)
        )
        
        # CRITICAL: session_string is stored at ROOT level, not inside session_data!
        session_string = (
            session_data.get("session_string") or  # ROOT level (where it's actually stored)
            inner_session_data.get("session_string")  # Fallback check inside session_data
        )
        
        # P7-77: the flag is written BOTH top-level (twofa.py's completion save
        # writes the root row) and inside session_data - reading only the
        # inner blob made recovery RE-run 2FA setup on sessions that had
        # already done it (PasswordAlreadyExists then confused the verdict).
        # Check both locations, every spelling.
        twofa_already_done = bool(
            session_data.get("twofa_completed")
            or session_data.get("twofa_enabled")
            or inner_session_data.get("twofa_completed", False)
            or inner_session_data.get("twofa_enabled", False)
        )
        # 2FA password lookup is inconsistent across the codebase: the ROOT row
        # stores it as ``twofa_password`` while the inner session_data blob
        # stores it as ``2fa_password`` (and some paths write the other name).
        # Check every spelling in both places so a session saved by one path is
        # always readable by recovery - a missed lookup made 2FA setup fail and
        # rejected good accounts.
        twofa_password = (
            session_data.get("twofa_password")
            or session_data.get("2fa_password")
            or inner_session_data.get("twofa_password")
            or inner_session_data.get("2fa_password")
            or config.get_2fa_password()
        )
        
        if not session_string:
            # Check for session file at ROOT level and inside session_data
            session_file = session_data.get("session_file") or inner_session_data.get("session_file")
            if session_file and os.path.exists(session_file):
                try:
                    # CRITICAL: session files can be EITHER a plain-text session
                    # string OR a binary Telethon/Pyrogram SQLite database. Reading
                    # a binary file in text mode raises UnicodeDecodeError and can
                    # stall the whole recovery task. Read bytes first, then decode.
                    with open(session_file, 'rb') as f:
                        raw = f.read()
                    if raw:
                        session_string = raw.decode('utf-8').strip()
                except (UnicodeDecodeError, OSError, ValueError, AttributeError):
                    # Binary session database (or unreadable file) - not a string.
                    session_string = None
        
        if not session_string:
            print(f"❌ RECOVERY: No session_string for {phone} - skipping")
            return False, "NO_SESSION"
        
        if not user_id:
            print(f"❌ RECOVERY: No user_id for {phone} - skipping")
            return False, "NO_USER"
        
        if is_session_message_already_sent(session_id):
            if not _md_recovery_past_window:
                print(f"⏭️ RECOVERY: Message already sent for {phone} - skipping")
                return True, "ALREADY_SENT"
            print(f"🔄 RECOVERY: {phone} MULTI-DEVICE message already delivered but the "
                  f"24h flow never resolved - re-verifying live")

        claimed = try_claim_session_finalization(session_id, skip_msg_sent_check=_md_recovery_past_window)
        if not claimed:
            print(f"⏭️ RECOVERY: Already being processed {phone} - skipping")
            return True, "ALREADY_CLAIMED"
        
        mark_session_finalization_claimed_by(session_id, "pending_recovery")
        
        print(f"🔄 RECOVERY: Processing {phone} (user: {user_id})")
        print(f"   - 2FA already done: {twofa_already_done}")
        print(f"   - Using enhanced recovery with full verification flow")
        
        # CRITICAL: Get the original message ID to EDIT instead of sending new message
        # Check multiple possible locations where the message ID could be stored
        # IMPORTANT: user_message_id is stored at ROOT level, not inside session_data!
        cached_msg_id = (
            # ROOT level (where user_message_id is actually stored after account_received)
            session_data.get("user_message_id") or
            session_data.get("message_id") or
            session_data.get("phone_message_id") or
            # Inside session_data dict (backup locations)
            inner_session_data.get("user_message_id") or
            inner_session_data.get("cached_msg_id") or
            inner_session_data.get("session_specific_message_id")
        )
        print(f"   - Cached message ID for editing: {cached_msg_id}")
        
        # Check if session already has precomputed message from before restart
        precomputed_message = inner_session_data.get("precomputed_message")
        ready_to_send = inner_session_data.get("ready_to_send", False)
        device_check_done = inner_session_data.get("device_check_completed", False)
        precomputed_type = inner_session_data.get("message_type")
        
        # Flag to track if precomputed message was successfully sent
        precomputed_sent = False
        
        if (ready_to_send and device_check_done and precomputed_message and precomputed_type
                and not _md_recovery_past_window):
            # EARLY-DELIVERY GUARD: a precomputed verdict persisted BEFORE its
            # exact deadline (the delayed task persists the verdict, then sleeps
            # to the deadline and delivers) must NEVER be surfaced early by
            # recovery. While the deadline is still in the future, release the
            # claim and leave the row for the deadline timer.
            if _deadline_is_in_future(
                session_data.get("deadline_at") or inner_session_data.get("deadline_at")
            ):
                print(f"⏳ RECOVERY: {phone} precomputed verdict deadline not reached - keeping session pending (deadline timer delivers)")
                update_pending_session_field(session_id, "finalization_claimed", False)
                return False, "PENDING"
            # AUTHORITATIVE LIVE DEVICE VERDICT =====
            # The precomputed verdict is trusted ONLY when marked
            # ``device_check_live`` (produced by the live delayed task at the
            # deadline). A legacy session without that marker may carry a false
            # verdict decided during the EARLY check, right after the OTP
            # code submit while the user's own phone was still logged in. By the
            # time recovery runs the user has normally logged out - re-verify
            # NOW with a fresh live GetAuthorizations and adopt its verdict.
            # This applies to EVERY precomputed type (SUCCESS, MULTI-DEVICE,
            # REJECTED) so no stale decision is ever delivered to the user.
            # An inconclusive (UNKNOWN) verdict is NEVER guessed: the claim is
            # released and the session stays pending for a later monitor retry.
            # A ``device_check_live`` verdict older than 15 minutes is re-checked
            # too - the marker alone must not finalize a session on a stale
            # decision after a long downtime.
            _device_check_live = inner_session_data.get("device_check_live")
            _device_check_stale = False
            if _device_check_live:
                _verdict_ts = (
                    inner_session_data.get("device_check_live_at")
                    or inner_session_data.get("device_check_completed_at")
                )
                if _verdict_ts:
                    try:
                        _verdict_dt = datetime.fromisoformat(
                            str(_verdict_ts).replace("Z", "").replace("+00:00", "")
                        )
                        _device_check_stale = (datetime.now() - _verdict_dt).total_seconds() > 900
                    except (ValueError, TypeError):
                        _device_check_stale = False
            if (not _device_check_live or _device_check_stale) and get_country_setting_bool(country_code, "device_check"):
                try:
                    verdict = await live_device_verdict(session_string, api_id, api_hash, phone, country_code=country_code, preferred_proxy_url=get_pinned_proxy_url(session_data, inner_session_data))
                    v = verdict.get("verdict")
                    print(f"🔍 RECOVERY LIVE VERDICT for {phone}: {v} "
                          f"(precomputed was {precomputed_type}, error={verdict.get('error') or 'None'})")
                    if v == VERDICT_UNKNOWN:
                        print(f"🛡️ RECOVERY: no definitive device verdict for {phone} - keeping session pending")
                        update_pending_session_field(session_id, "finalization_claimed", False)
                        return False, "PENDING"
                    if v == VERDICT_REJECTED:
                        print(f"❌ RECOVERY: live check says session invalid for {phone} - rejecting")
                        precomputed_type = "REJECTED"
                        precomputed_message = format_message(
                            "verification_failed_message",
                            lang=get_user_lang(int(user_id)),
                            phone=phone,
                            country_code=country_code,
                            flag=get_flag(country_code),
                        )
                        inner_session_data["message_type"] = "REJECTED"
                        inner_session_data["precomputed_message"] = precomputed_message
                        inner_session_data["is_valid"] = False
                        inner_session_data["has_extra_devices"] = False
                        inner_session_data["extra_devices"] = []
                        inner_session_data["device_count"] = 1
                    elif v == VERDICT_SUCCESS:
                        print(f"✅ RECOVERY: live check: {phone} has only this session -> SUCCESS (precomputed was {precomputed_type})")
                        _lang = get_user_lang(int(user_id))
                        _spam_status = inner_session_data.get("spam_status", "limit")
                        _status_display = {
                            "spam_free": t("status_spam_free", _lang),
                            "spam": t("status_spam", _lang),
                            "limit": t("status_limit", _lang),
                        }.get(_spam_status, t("status_spam_free", _lang))
                        _upgrade_rate = get_country_rate(country_code, _spam_status, user_id=user_id)
                        precomputed_type = "SUCCESS"
                        precomputed_message = format_message(
                            "success_message",
                            lang=_lang,
                            phone=phone,
                            price=f"{_upgrade_rate:.2f}",
                            status=_status_display,
                        )
                        inner_session_data["message_type"] = "SUCCESS"
                        inner_session_data["precomputed_message"] = precomputed_message
                        inner_session_data["has_extra_devices"] = False
                        inner_session_data["extra_devices"] = []
                        inner_session_data["device_count"] = 1
                        inner_session_data["is_valid"] = True
                    else:  # VERDICT_MULTI_DEVICE
                        print(f"📱 RECOVERY: live check: {phone} still has extra devices - keeping MULTI-DEVICE (24h hold)")
                        if precomputed_type != "MULTI-DEVICE":
                            # A stale precomputed SUCCESS (or REJECTED) text must
                            # never be shown for a multi-device hold - rebuild the
                            # message from the live verdict so the user always sees
                            # the correct outcome text.
                            _md_lang = get_user_lang(int(user_id))
                            precomputed_message = format_message(
                                "multi_device_message",
                                _md_lang,
                                phone=phone,
                            )
                            precomputed_type = "MULTI-DEVICE"
                            inner_session_data["message_type"] = "MULTI-DEVICE"
                            inner_session_data["precomputed_message"] = precomputed_message
                        inner_session_data["has_extra_devices"] = True
                        inner_session_data["device_count"] = int(verdict.get("device_count", 2) or 2)
                        inner_session_data["extra_devices"] = verdict.get("extra_devices", []) or []
                    inner_session_data["device_check_live"] = True
                    inner_session_data["device_check_live_at"] = datetime.now().isoformat()
                    try:
                        replace_pending_session_data(session_id, inner_session_data)
                    except Exception as persist_err:
                        print(f"⚠️ RECOVERY LIVE VERDICT: could not persist verdict: {persist_err}")
                    print(f"✅ RECOVERY LIVE VERDICT: {precomputed_type} persisted for {phone}")
                except Exception as recheck_err:
                    print(f"⚠️ RECOVERY LIVE VERDICT error for {phone}: {recheck_err} - keeping session pending")
                    update_pending_session_field(session_id, "finalization_claimed", False)
                    return False, "PENDING"

            print(f"✅ RECOVERY: Found precomputed message for {phone} - sending directly")
            try:
                # CRITICAL: Try to EDIT original message first, only send new if edit fails
                message_sent = False
                if cached_msg_id and int(cached_msg_id) > 0:
                    try:
                        await bot_client.edit_message_text(
                            chat_id=int(user_id),
                            message_id=int(cached_msg_id),
                            text=precomputed_message
                        )
                        message_sent = True
                        print(f"✅ RECOVERY: EDITED original message for {phone}")
                    except Exception as edit_err:
                        if _is_message_not_modified(edit_err):
                            # The message already carries the exact final text -
                            # that IS a successful delivery. Sending a NEW
                            # message here would duplicate it.
                            message_sent = True
                            print(f"✅ RECOVERY: original message already shows the final verdict for {phone} (MESSAGE_NOT_MODIFIED)")
                        else:
                            print(f"⚠️ RECOVERY: Edit failed, sending new: {edit_err}")
                
                if not message_sent:
                    await bot_client.send_message(chat_id=int(user_id), text=precomputed_message)
                    print(f"✅ RECOVERY: Sent NEW message for {phone}")
                
                mark_session_message_sent(session_id, True)
                print(f"✅ RECOVERY: Precomputed {precomputed_type} message sent for {phone}")
                precomputed_sent = True
                
                # Use precomputed values instead of re-verification
                spam_status = inner_session_data.get("spam_status", "limit")
                has_extra_devices = inner_session_data.get("has_extra_devices", False)
                
                # Determine validity based on precomputed message type
                if precomputed_type == "REJECTED":
                    is_valid = False
                else:
                    is_valid = True
                
                # Create a details dict for compatibility with downstream code
                details = {
                    "has_extra_devices": has_extra_devices,
                    "spam_status": spam_status,
                    "device_count": inner_session_data.get("device_count", 1),
                    "is_valid": is_valid
                }
            except Exception as pre_err:
                print(f"⚠️ RECOVERY: Precomputed message send failed: {pre_err} - will re-verify")
                precomputed_sent = False
        
        # Only do full verification if precomputed message was not sent
        if not precomputed_sent:
            is_valid, spam_status, details = await check_session_validity_with_2fa(
                session_string=session_string,
                api_id=api_id,
                api_hash=api_hash,
                twofa_password=twofa_password,
                twofa_already_done=twofa_already_done,
                phone=phone,
                country_code=country_code,
                preferred_proxy_url=get_pinned_proxy_url(session_data, inner_session_data),
            )
            
            if details.get("twofa_setup"):
                try:
                    update_pending_session_field(session_id, "twofa_completed", True)
                    update_pending_session_field(session_id, "twofa_enabled", True)
                    print(f"✅ RECOVERY: 2FA status saved for {phone}")
                except Exception as save_err:
                    print(f"⚠️ RECOVERY: Could not save 2FA status: {save_err}")

            # P4-46: persist the profile the validity check applied (name/bio/
            # photo) so the delivered report/JSON carries it, like the live path.
            _applied_profile = details.get("applied_profile") or {}
            if _applied_profile:
                try:
                    if _applied_profile.get("name"):
                        update_pending_session_field(
                            session_id, "applied_profile_name", _applied_profile["name"])
                    if _applied_profile.get("bio"):
                        update_pending_session_field(
                            session_id, "applied_profile_bio", _applied_profile["bio"])
                    if _applied_profile.get("photo"):
                        update_pending_session_field(
                            session_id, "applied_profile_photo", _applied_profile["photo"])
                    if details.get("first_name"):
                        update_pending_session_field(
                            session_id, "first_name", details.get("first_name") or "")
                        update_pending_session_field(
                            session_id, "last_name", details.get("last_name") or "")
                    print(f"👤 RECOVERY: profile application saved for {phone}")
                except Exception as prof_err:
                    print(f"⚠️ RECOVERY: Could not save profile application: {prof_err}")
        
        flag = get_flag(country_code)
        has_extra_devices = details.get("has_extra_devices", False)
        device_count = details.get("device_count", 1)
        spam_status = details.get("spam_status", "limit")
        device_check_error = details.get("device_check_error")
        
        # 🔥 FINAL VALIDATION - single shared decision (identical to live path)
        # 2FA must have succeeded: use the stored flag when present, otherwise
        # the recovery attempt's own 2FA result.
        stored_twofa = inner_session_data.get("twofa_enabled")
        if stored_twofa is not None:
            twofa_flag = stored_twofa
        else:
            twofa_flag = details.get("twofa_setup")
        if not get_country_setting_bool(country_code, "twofa_check"):
            # 2FA disabled by the admin: never let a missing/failed 2FA setup
            # reject the session. Report as enabled so the final validation
            # classifies the outcome normally.
            twofa_flag = True
        message_type = determine_final_message_type(
            is_session_valid=is_valid,
            has_extra_devices=has_extra_devices,
            device_count=device_count,
            device_check_error=device_check_error,
            twofa_enabled=twofa_flag,
        )

        print(f"\n🔍 RECOVERY FINAL VALIDATION - Confirming results...")
        print(f"   - is_valid: {is_valid}")
        print(f"   - has_extra_devices: {has_extra_devices}")
        print(f"   - device_count: {device_count}")
        print(f"   - spam_status: {spam_status}")
        print(f"   - device_check_error: {device_check_error or 'None'}")
        if message_type == "REJECTED" and not is_valid:
            print(f"✅ VALIDATION: REJECTED (is_valid=False)")
        print(f"✅ RECOVERY VALIDATION RESULT: {message_type} (is_valid={is_valid}, extra_devices={has_extra_devices}, device_count={device_count})")
        
        print(f"📝 RECOVERY FINAL MESSAGE TYPE: {message_type}\n")

        # UNKNOWN means the device verdict was inconclusive (transient error).
        # NEVER guess: release the claim and keep the session pending so the
        # monitor retries it later with a fresh live check.
        if message_type == "UNKNOWN":
            print(f"🛡️ RECOVERY: no definitive device verdict for {phone} - keeping session pending")
            try:
                update_pending_session_field(session_id, "finalization_claimed", False)
            except Exception as unclaim_err:
                print(f"⚠️ RECOVERY: could not release claim for {phone}: {unclaim_err}")
            return False, "PENDING"
        
        # Send message based on message_type
        if message_type == "REJECTED":
            # Only send user message if not already sent via precomputed
            if not precomputed_sent:
                final_message = format_message(
                    "verification_failed_message",
                    lang=get_user_lang(int(user_id)),
                    phone=phone,
                    country_code=country_code,
                    flag=flag
                )

                # Persist the decision BEFORE delivering so a crash mid-delivery
                # leaves a self-describing record (reconciliation completes it).
                inner_session_data["message_type"] = "REJECTED"
                inner_session_data["precomputed_message"] = final_message
                inner_session_data["is_valid"] = False
                inner_session_data["has_extra_devices"] = False
                inner_session_data["ready_to_send"] = True
                try:
                    replace_pending_session_data(session_id, inner_session_data)
                except Exception:
                    pass
                
                try:
                    # CRITICAL: Try to EDIT original message first
                    message_sent = False
                    if cached_msg_id and int(cached_msg_id) > 0:
                        try:
                            await bot_client.edit_message_text(
                                chat_id=int(user_id),
                                message_id=int(cached_msg_id),
                                text=final_message
                            )
                            message_sent = True
                            print(f"✅ RECOVERY: EDITED original message for rejection")
                        except Exception:
                            pass
                    
                    if not message_sent:
                        await bot_client.send_message(chat_id=int(user_id), text=final_message)
                    
                    mark_session_message_sent(session_id, True)
                    print(f"❌ RECOVERY: {phone} REJECTED - session invalid")
                except Exception as send_err:
                    print(f"⚠️ RECOVERY: Failed to send rejection to {user_id}: {send_err}")
            else:
                print(f"❌ RECOVERY: {phone} REJECTED - precomputed message already sent")
            
            # Always reject the session
            reject_pending_session(session_id, "Session invalid")

            try:
                _px = session_data.get("proxy_used") or inner_session_data.get("proxy_used") or ""
                _px_attr = session_data.get("proxy_attribution") or inner_session_data.get("proxy_attribution") or country_code
                record_proxy_session_outcome(_px_attr, _px, "rejected")
            except Exception:
                pass

            # REJECTED sessions NEVER ship a session file or JSON - the
            # channel gets a text report only (per the "session+JSON only for
            # Telethon / Program SUCCESS" rule).
            report = build_rejected_report(phone, country_code, "Session invalid", user_id=user_id)
            await send_report_to_channel(
                chat_id=config.get_channel("CHANNEL_REJECTED"),
                report=report,
                bot_client=bot_client,
            )

            return True, "REJECTED"
        
        elif message_type == "MULTI-DEVICE":
            # Only send user message if not already sent via precomputed. A
            # past-window unresolved MD session already HAS the MULTI-DEVICE
            # text in front of the user - never duplicate it; just refresh the
            # flags so the monitor's 24h flow owns the session again.
            md_message_sent = False
            if not precomputed_sent and not _md_recovery_past_window:
                final_message = format_message(
                    "multi_device_message",
                    lang=get_user_lang(int(user_id)),
                    phone=phone
                )
                
                # Persist the decision BEFORE delivering so a crash mid-delivery
                # leaves a self-describing record (reconciliation + safety net).
                inner_session_data["message_type"] = "MULTI-DEVICE"
                inner_session_data["precomputed_message"] = final_message
                inner_session_data["has_extra_devices"] = True
                inner_session_data["is_valid"] = True
                inner_session_data["ready_to_send"] = True
                try:
                    replace_pending_session_data(session_id, inner_session_data)
                except Exception:
                    pass

                try:
                    # CRITICAL: Try to EDIT original message first
                    message_sent = False
                    if cached_msg_id and int(cached_msg_id) > 0:
                        try:
                            await bot_client.edit_message_text(
                                chat_id=int(user_id),
                                message_id=int(cached_msg_id),
                                text=final_message
                            )
                            message_sent = True
                            print(f"✅ RECOVERY: EDITED original message for multi-device")
                        except Exception:
                            pass
                    
                    if not message_sent:
                        await bot_client.send_message(chat_id=int(user_id), text=final_message)
                    
                    mark_session_message_sent(session_id, True)
                    md_message_sent = True
                    print(f"📱 RECOVERY: {phone} MULTI-DEVICE")
                except Exception as send_err:
                    print(f"⚠️ RECOVERY: Failed to send multi-device to {user_id}: {send_err}")
            elif _md_recovery_past_window:
                # The MULTI-DEVICE text was already delivered by an earlier run
                # (that is why this session is being re-verified) - the message
                # is NOT re-sent; the sent marker is kept.
                md_message_sent = True
                print(f"📱 RECOVERY: {phone} MULTI-DEVICE - message already delivered, not re-sending")
            else:
                md_message_sent = True
                print(f"📱 RECOVERY: {phone} MULTI-DEVICE - precomputed message already sent")
            
            # Persist multi-device state so the exact-24h timer (and the monitor
            # window check) can pick it up after a recovery-driven detection.
            # Preserve an existing added_at so a restart does not reset the
            # 24h countdown.
            try:
                inner_session_data["has_extra_devices"] = True
                # CRITICAL: only mark message_send_success when the message was
                # ACTUALLY delivered. If the send failed, leaving it False lets
                # the monitor re-send the message (multi_device.py falls through
                # when message_send_success is not True) and keeps the 24h timer
                # from arming on a message the user never saw.
                if md_message_sent:
                    inner_session_data["message_send_success"] = True
                if not (inner_session_data.get("added_at") or session_data.get("added_at")):
                    inner_session_data["added_at"] = datetime.now().isoformat()
                # Release the finalization claim: a MULTI-DEVICE session is NOT
                # finalized (it is on a 24h hold), so the claim must be released
                # for the 24h timer / monitor cycle to pick it up later. Without
                # this, the 24h timer's try_claim_session_finalization sees the
                # stale claim and skips the session forever (the auto-logout and
                # credit flow never runs).
                inner_session_data["finalization_claimed"] = False
                replace_pending_session_data(session_id, inner_session_data)
            except Exception as persist_err:
                print(f"⚠️ RECOVERY: Could not persist multi-device state: {persist_err}")
            
            # Send the multidevice report honoring the admin-selected format
            # (Session Management -> Multidevice Session Mode, max 1). No JSON.
            report = build_multi_device_report(
                phone, country_code, device_count, user_id=user_id
            )
            # CRITICAL: atomically claim the right to send the channel report.
            # Only ONE path may deliver the multidevice file to CHANNEL_24HR_UP.
            md_claimed = try_claim_channel_report(session_id)
            if not md_claimed:
                print(f"⏭️ RECOVERY: multidevice channel report already claimed/sent for {phone} - skipping")
                try:
                    _stored_md = get_pending_session(session_id) or {}
                    _stored_md_info = _stored_md.get("session_data", {}) or {}
                    inner_session_data["channel_report_sent"] = bool(_stored_md_info.get("channel_report_sent"))
                    inner_session_data["channel_report_pending"] = not bool(_stored_md_info.get("channel_report_sent"))
                except Exception:
                    # C2 FIX: read failure proves nothing about delivery - keep
                    # the pending marker so the safety net can re-deliver.
                    inner_session_data["channel_report_sent"] = False
                    inner_session_data["channel_report_pending"] = True
            else:
                try:
                    # report_pending_or_multidevice: module-level import.
                    multi_sent = await report_pending_or_multidevice(
                        bot_client=bot_client,
                        mode="multidevice",
                        channel=config.get_channel("CHANNEL_24HR_UP"),
                        phone=phone,
                        session_string=session_string or "",
                        report_text=report,
                        telethon_file=None,
                        pyrogram_file=None,
                        user_id=user_id,
                    )
                    inner_session_data["channel_report_sent"] = multi_sent is not None
                    inner_session_data["channel_report_pending"] = multi_sent is None
                    if multi_sent is None:
                        print(f"⚠️ RECOVERY: multidevice report NOT sent for {phone} "
                              f"(format could not be produced) - channel_report_pending=True")
                    else:
                        print(f"📤 RECOVERY: multidevice report sent for {phone}")
                except Exception as md_report_err:
                    print(f"⚠️ RECOVERY: multidevice report error for {phone}: {md_report_err}")
                    inner_session_data["channel_report_sent"] = False
                    inner_session_data["channel_report_pending"] = True
                finally:
                    release_channel_report(session_id, sent=inner_session_data.get("channel_report_sent", False))
            try:
                replace_pending_session_data(session_id, inner_session_data)
            except Exception as persist_err:
                print(f"⚠️ RECOVERY: Could not persist multidevice flags: {persist_err}")

            try:
                _px_md = session_data.get("proxy_used") or inner_session_data.get("proxy_used") or ""
                _px_md_attr = session_data.get("proxy_attribution") or inner_session_data.get("proxy_attribution") or country_code
                record_proxy_session_outcome(_px_md_attr, _px_md, "multi_device")
            except Exception:
                pass

            return True, "MULTI-DEVICE"
        
        else:  # message_type == "SUCCESS"
            rate = get_country_rate(country_code, spam_status, user_id=user_id)

            # CORE CONFIG: "Accept Restricted" OFF for this country refuses the
            # payout for a limited account (status unchanged, rate forced to 0
            # so the credit is 0 and the pricing math stays untouched).
            try:
                if (
                    spam_status == "limit"
                    and not get_country_setting_bool(country_code, "accept_restricted")
                ):
                    print(f"⛔ RECOVERY: ACCEPT RESTRICTED OFF for {country_code}: "
                          f"limited account {phone} credited at 0")
                    rate = 0.0
            except Exception:
                pass

            _lang = get_user_lang(int(user_id))
            spam_display = {
                "spam_free": t("status_spam_free", _lang),
                "spam": t("status_spam", _lang),
                "limit": t("status_limit", _lang),
            }.get(spam_status, t("status_limit", _lang))
            
            # Always credit balance and approve session
            credit_success = False
            try:
                credit_success = atomic_credit_balance_for_session(session_id, int(user_id), rate)
                if credit_success:
                    try:
                        _px_s = session_data.get("proxy_used") or inner_session_data.get("proxy_used") or ""
                        _px_s_attr = session_data.get("proxy_attribution") or inner_session_data.get("proxy_attribution") or country_code
                        record_proxy_session_outcome(_px_s_attr, _px_s, "success")
                    except Exception:
                        pass
                    sync_user_from_storage(int(user_id))
                    print(f"💰 RECOVERY: ${rate} credited to user {user_id}")
                    try:
                        from bot.utils.database import record_session_analytics, upsert_session
                        from bot.utils.storage import get_session_spam_score
                        await record_session_analytics(
                            session_id=session_id,
                            country_code=country_code,
                            successful=True,
                            amount=rate,
                            spam_score=get_session_spam_score(session_id),
                        )
                        await upsert_session(session_id, {
                            "user_id": int(user_id),
                            "phone": phone,
                            "country_code": country_code,
                            "spam_status": spam_status,
                            "session_string": session_string,
                        })
                    except Exception as analytics_err:
                        print(f"⚠️ RECOVERY: session analytics update failed: {analytics_err}")
            except Exception as credit_err:
                print(f"⚠️ RECOVERY: Failed to credit balance: {credit_err}")
            
            # Only send user message if not already sent via precomputed
            if not precomputed_sent:
                final_message = format_message(
                    "success_message",
                    lang=_lang,
                    phone=phone,
                    price=f"{rate:.2f}",
                    status=spam_display
                )

                # Persist the decision BEFORE delivering so a crash mid-delivery
                # leaves a self-describing record (reconciliation completes it).
                inner_session_data["message_type"] = "SUCCESS"
                inner_session_data["precomputed_message"] = final_message
                inner_session_data["is_valid"] = True
                inner_session_data["has_extra_devices"] = False
                inner_session_data["ready_to_send"] = True
                try:
                    replace_pending_session_data(session_id, inner_session_data)
                except Exception:
                    pass
                
                try:
                    # CRITICAL: Try to EDIT original message first
                    message_sent = False
                    if cached_msg_id and int(cached_msg_id) > 0:
                        try:
                            await bot_client.edit_message_text(
                                chat_id=int(user_id),
                                message_id=int(cached_msg_id),
                                text=final_message
                            )
                            message_sent = True
                            print(f"✅ RECOVERY: EDITED original message for success")
                        except Exception:
                            pass
                    
                    if not message_sent:
                        await bot_client.send_message(chat_id=int(user_id), text=final_message)
                    
                    mark_session_message_sent(session_id, True)
                    print(f"✅ RECOVERY: {phone} SUCCESS - ${rate} ({spam_status})")
                except Exception as send_err:
                    print(f"⚠️ RECOVERY: Failed to send success to {user_id}: {send_err}")
            else:
                print(f"✅ RECOVERY: {phone} SUCCESS - ${rate} ({spam_status}) - precomputed message already sent")
            
            # Approve ONLY when the money is actually settled.
            #
            # ``atomic_credit_balance_for_session`` already sets
            # ``status="approved"`` when it succeeds, so this is a belt-and-
            # braces no-op on the happy path. On the failure path it MATTERS:
            # flipping an uncredited row to ``approved`` hides it from every
            # repair scanner (pending.py, finalize.py, reconcile.py and
            # orchestrator.py all filter ``status == "pending"``), so the failed
            # credit would never be retried and the user would never be paid.
            settled = bool(credit_success)
            if not settled:
                _settle_row = get_pending_session(session_id) or {}
                settled = bool(_settle_row.get("balance_credited")) or \
                    _settle_row.get("status") == "approved"
            if settled:
                approve_pending_session(session_id)
            else:
                print(f"⚠️ RECOVERY: NOT approving {phone} - balance credit did not land; "
                      f"row stays pending so reconciliation retries it")
            
            # Always send the per-format success report (honors the admin's
            # Success Session Mode selection: telethon+JSON, pyrogram+JSON,
            # tdata, txt - each to its own channel).
            report = build_success_report(
                phone, country_code, rate, spam_status, verified_by=user_id,
                name=(inner_session_data.get("first_name") or ""),
                account_id=inner_session_data.get("user_id") or inner_session_data.get("account_id"),
            )
            # CRITICAL: atomically claim the right to send the success channel
            # report. Only ONE path may deliver the files (the delayed task,
            # the monitor full validation, the fast path, the recovery scan
            # and this worker all target the same session).
            report_claimed = try_claim_channel_report(session_id)
            if not report_claimed:
                print(f"⏭️ RECOVERY: SUCCESS channel report already claimed/sent for {phone} - skipping files")
                try:
                    _stored_sc = get_pending_session(session_id) or {}
                    _stored_sc_info = _stored_sc.get("session_data", {}) or {}
                    inner_session_data["channel_report_sent"] = bool(_stored_sc_info.get("channel_report_sent"))
                    inner_session_data["channel_report_pending"] = not bool(_stored_sc_info.get("channel_report_sent"))
                except Exception:
                    # C2 FIX: read failure proves nothing about delivery - keep
                    # the pending marker so the safety net can re-deliver.
                    inner_session_data["channel_report_sent"] = False
                    inner_session_data["channel_report_pending"] = True
            else:
                try:
                    # report_success_session: module-level import.
                    outcomes = []
                    for _attempt in range(1, 4):
                        outcomes = await report_success_session(
                            bot_client=bot_client,
                            phone=phone,
                            user_id=user_id,
                            session_info=inner_session_data,
                            session_string=session_string,
                            twofa_password=twofa_password,
                            country_code=country_code,
                            rate=rate,
                            account_status=spam_status,
                            status_details=inner_session_data.get("status_details") or {},
                            success_report=report,
                            telethon_file=None,
                            pyrogram_file=None,
                            session_id=session_id,
                        )
                        if _all_attempted_formats_delivered(outcomes):
                            break
                        print(f"🔁 RECOVERY: SUCCESS report attempt {_attempt}/3 failed for {phone} - retrying")
                        if _attempt < 3:
                            await asyncio.sleep(min(2.0 * _attempt, 6.0))
                    files_sent = _all_attempted_formats_delivered(outcomes)
                    # Never substitute a text report for a file: persist the pending
                    # marker so the monitor safety net re-delivers the files.
                    inner_session_data["channel_report_sent"] = files_sent
                    inner_session_data["channel_report_pending"] = not files_sent
                except Exception as success_report_err:
                    print(f"⚠️ RECOVERY: SUCCESS report error for {phone}: {success_report_err}")
                    inner_session_data["channel_report_sent"] = False
                    inner_session_data["channel_report_pending"] = True
                finally:
                    release_channel_report(session_id, sent=inner_session_data.get("channel_report_sent", False))
            try:
                replace_pending_session_data(session_id, inner_session_data)
            except Exception as persist_err:
                print(f"⚠️ RECOVERY: Could not persist SUCCESS report flags: {persist_err}")
            print(f"📤 RECOVERY: SUCCESS per-format report for {phone}: {inner_session_data.get('channel_report_sent')}")

            # Always log transaction
            try:
                from bot.utils.database import log_transaction
                spawn_background(log_transaction(
                    user_id=int(user_id),
                    action="session_verified",
                    amount=rate,
                    session_id=session_id,
                    country_code=country_code,
                    phone_number=phone,
                    spam_status=spam_status
                ), "recovery-log-transaction")
            except:
                pass
            
            return True, "SUCCESS"
            
    except Exception as e:
        # Classify the exception: definitive dead-session errors (auth key
        # revoked, account banned/deleted, etc.) cause a REJECTED outcome so
        # the session is never left pending forever. Transient/unexpected
        # errors (network blip, flood, storage glitch) keep the session pending
        # so the monitor retries with a fresh live check - a good account is
        # NEVER rejected on a temporary blip.
        if _is_definitive_dead_session_error(e):
            print(f"❌ RECOVERY: session definitively dead for {session_id}: {e}")
            try:
                phone = session_data.get("phone", "UNKNOWN")
                user_id = str(session_data.get("user_id", "0") or "0")
                country_code = session_data.get("country_code", "UNKNOWN")
                if user_id and user_id != "0":
                    final_message = format_message(
                        "verification_failed_message",
                        lang=get_user_lang(int(user_id)),
                        phone=phone,
                        country_code=country_code,
                        flag=get_flag(country_code),
                    )
                    try:
                        await bot_client.send_message(chat_id=int(user_id), text=final_message)
                    except Exception:
                        pass
                reject_pending_session(session_id, f"Session invalid: {e}")
                try:
                    _px_e = session_data.get("proxy_used") or (session_data.get("session_data") or {}).get("proxy_used") or ""
                    _px_e_attr = session_data.get("proxy_attribution") or (session_data.get("session_data") or {}).get("proxy_attribution") or country_code
                    record_proxy_session_outcome(_px_e_attr, _px_e, "rejected")
                except Exception:
                    pass
                report = build_rejected_report(
                    phone, country_code, "Session invalid",
                    user_id=user_id or 0,
                )
                await send_report_to_channel(
                    chat_id=config.get_channel("CHANNEL_REJECTED"),
                    report=report,
                    bot_client=bot_client,
                )
            except Exception as inner_err:
                logger.warning(f"⚠️ RECOVERY: rejection-after-exception failed for {session_id}: {inner_err}")
            return True, "REJECTED"
        # Transient/unexpected error: never reject a possibly-good account.
        # Release the claim and keep the session pending for a later retry.
        print(f"⚠️ RECOVERY: transient error for {session_id}: {e} - keeping session pending")
        try:
            update_pending_session_field(session_id, "finalization_claimed", False)
        except Exception as unclaim_err:
            logger.warning(f"⚠️ RECOVERY: could not release claim after transient error: {unclaim_err}")
        return False, "PENDING"
