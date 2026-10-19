"""
Delayed 2FA setup + spam-check background workflow.
=====================================================================
After OTP verification, this background task runs the connection-holding
check phase EARLY (2FA setup, spam-status check, device check), releases
the concurrency slot + client connection, then waits on a cheap per-session
timer and sends the final message at the EXACT country timeout anchored to
the OTP code-submit moment (t0 + timeout_seconds). Successful sessions are
credited and reported to the channels.

Concurrency: the slot only bounds the short check phase (API-scaled via
``PER_API_CONCURRENCY * <API count>``). The deadline wait holds no slot and
no connection, so thousands of pending sessions can wait simultaneously.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

from pyrogram.client import Client
from pyrogram.errors import FloodWait

from bot import config
from bot.utils.net_timeouts import teardown_with_timeout
from bot.utils.anti_spam import (
    between_api_calls_delay,
    pre_2fa_setup_delay,
    record_verification_attempt,
)
from bot.utils.api_rotation import record_api_usage
from bot.utils.country_data import get_flag
from bot.utils.device_generator import generate_device_info
from bot.utils.net_timeouts import connect_with_timeout, op_with_timeout, invoke_with_timeout
from bot.services.reports import (
    build_multi_device_report,
    build_pending_report,
    build_rejected_report,
    build_success_report,
)
from bot.utils.storage import (
    _is_message_not_modified,
    add_pending_session,
    atomic_credit_balance_for_session,
    get_auto_approve_enabled,
    get_pending_session,
    get_user_info,
    is_session_message_already_sent,
    get_country_setting_bool,
    mark_session_finalization_claimed_by,
    mark_session_message_sent,
    reject_pending_session,
    replace_pending_session_data,
    set_pending_session_field,
    try_claim_channel_report,
    try_claim_session_finalization,
    release_channel_report,
)
# NOTE: these MUST stay module-level. A function-scoped
# `from ... import X` makes X a LOCAL for the WHOLE function body, so any use
# of X on a path that did not execute the import raises UnboundLocalError.
from bot.utils.fast_cache import sync_user_from_storage
from bot.services.session_reporting import (
    _all_attempted_formats_delivered,
    _retry_success_report_until_delivered,
    report_pending_or_multidevice,
    report_success_session,
)
from bot.verification.device import (
    live_device_verdict,
    VERDICT_SUCCESS,
    VERDICT_MULTI_DEVICE,
    VERDICT_REJECTED,
    VERDICT_UNKNOWN,
)
from bot.verification.messages import build_final_message
from bot.verification.rates import get_country_rate, get_country_timeout
from bot.verification.spam_check import check_account_status
from bot.verification.state import _delayed_task_semaphore, _device_verdict_semaphore
from bot.verification.telethon_export import session_string_to_telethon
from bot.services.session_formats import generate_pyrogram_file
from bot.utils.bg_tasks import spawn_background

logger = logging.getLogger(__name__)

# Live-verdict retry policy. At the deadline we run a fresh GetAuthorizations
# and retry transient failures a few times before falling back to keeping the
# session pending. We NEVER guess: if no definitive answer is obtained, the
# session stays pending and the monitor retries it on the next cycle.
_MAX_LIVE_VERDICT_ATTEMPTS = 4
_LIVE_VERDICT_RETRY_DELAY = 3.0

# How long before the exact deadline the live device verdict is computed.
# Running the verdict BEFORE the deadline (instead of right after it) keeps the
# message delivery ON the deadline: even with hundreds of sessions reaching
# their deadline in the same second, the send is no longer gated behind the
# verdict's connect + GetAuthorizations + retries. The verdict still happens
# within the user's timeout window, so the "log out your phone" deadline is
# practically unchanged.
_VERDICT_LEAD_SECONDS = float(os.getenv("VERDICT_LEAD_SECONDS", "8") or "8")

# In-process registry of session IDs whose delayed 2FA/spam-check task is
# currently running. After a restart the registry is empty (the background task
# no longer exists), which is exactly what lets the exact-deadline timer take
# over instead of waiting on a task that will never fire again.
_in_process_delayed_sessions: set = set()

# Absolute floor for the total country timeout. No country is ever allowed a
# shorter processing window than this. Kept as a module constant so tests can
# lower it (the real config always keeps 20s).
#
# P2-23: raised 20 -> 180. At the old 20s floor the user had ~20 seconds
# between receiving the OTP code and the deadline to log the account out of
# every OTHER device - physically impossible for most users, so virtually
# every session became MULTI-DEVICE (24h hold, no credit) by design. 180s
# gives a realistic logout window while the device verdict still runs live at
# the deadline (a user who refuses to log out is still detected - the floor
# only protects users who never had a fair chance).
MIN_TIMEOUT_SECONDS = 180


def build_initial_session_data(
    user_id: int,
    me,
    phone: str,
    country_code: str,
    twofa_password: str,
    phone_message_id,
    proxy_used: str = "",
    proxy_attribution: str = "",
    proxy_url: str = "",
) -> dict:
    """Build the initial pending-session payload persisted at t=0.

    Shared by ``otp.py`` (persist immediately when the OTP code is accepted)
    and this module's check phase, so the storage record is identical and the
    exact deadline (``submitted_at`` + ``timeout_seconds``) is anchored to the
    OTP code-submit moment either way.

    ``proxy_used`` is the masked proxy key used during the login connect, so
    the session outcome (success/rejected/multi_device) can be attributed to
    the correct proxy line in the admin panel.

    ``proxy_attribution`` is the country code under which the proxy health
    stats must be stored (the pool-owner code when the login used a matched
    fallback pool, otherwise the session country code). It MUST be recorded
    together with ``proxy_used`` so the monitor/recovery outcome recorders
    store attempts and session outcomes under the SAME health key.

    ``proxy_url`` (P4-41) is the DIALABLE URL of the proxy the login actually
    used ("" = direct). Persisted as the session's proxy PIN so every later
    phase (2FA/spam/device/validate/logout/recovery) dials the SAME proxy
    instead of re-rolling the pool.
    """
    initial_session_data = {
        "user_id": me.id,
        "first_name": me.first_name or "",
        "last_name": me.last_name or "",
        "username": me.username or "",
        "phone": phone,
        "country_code": country_code,
        "2fa_password": twofa_password,
        "verified_by_user": user_id,
        "twofa_completed": False,
        "twofa_completed_at": None,
        "spam_check_completed": False,
        "spam_check_completed_at": None,
        "device_check_completed": False,
        "is_processing": True,
        "delayed_task_active": True,
        "cached_msg_id": None,
        "session_specific_message_id": phone_message_id,
        # Set the initial message_type to PENDING so the monitor safety net
        # (orchestrator.py run_channel_report_safety_net) can retry the
        # channel report for this session after a restart. The final verdict
        # (SUCCESS / MULTI-DEVICE / REJECTED) overwrites this later.
        "message_type": "PENDING",
        "proxy_used": proxy_used,
        "proxy_attribution": proxy_attribution or country_code,
        "proxy_url": proxy_url,
    }

    try:
        user_info = get_user_info(user_id)
        if user_info and user_info.get("pending_message_id"):
            initial_session_data["cached_msg_id"] = user_info.get("pending_message_id")
    except (KeyError, TypeError):
        pass  # User info lookup failed, proceed without cached message ID

    return initial_session_data


def is_delayed_task_in_process(session_id: str) -> bool:
    """True if the live delayed 2FA/spam-check task is running for this session."""
    return session_id in _in_process_delayed_sessions


async def delayed_2fa_and_spam_check(
    user_id: int,
    client: Client,
    phone: str,
    country_code: str,
    session_string: str,
    session_filename: Optional[str],
    telethon_filename: Optional[str],
    me,
    bot_client: Client,
    phone_message_id: Optional[int] = None,
    pending_channel_message_id: Optional[int] = None,
    is_document_message: bool = True,
    access_hash: int = 0,
    session_id: Optional[str] = None,
    task_start_time: Optional[datetime] = None,
    task_start_monotonic: Optional[float] = None,
    api_id: Optional[int] = None,
    api_hash: Optional[str] = None,
):
    """
    Delayed 2FA/spam-check workflow wrapper.

    The concurrency cap (scaled by the number of APIs, see
    ``bot.verification.state``) only bounds the SHORT connection-holding check
    phase (2FA -> spam -> device). The impl releases the slot and disconnects
    as soon as the checks are precomputed, so a session never occupies a slot
    or a live Telegram connection for its whole timeout window. Thousands of
    pending sessions can then wait on a cheap per-session timer and each one
    delivers at its OWN exact deadline.
    """
    if session_id:
        _in_process_delayed_sessions.add(session_id)
    try:
        await _delayed_2fa_and_spam_check_impl(
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
            pending_channel_message_id=pending_channel_message_id,
            is_document_message=is_document_message,
            access_hash=access_hash,
            session_id=session_id,
            task_start_time=task_start_time,
            task_start_monotonic=task_start_monotonic,
            api_id=api_id,
            api_hash=api_hash,
        )
    finally:
        if session_id:
            _in_process_delayed_sessions.discard(session_id)

async def _delayed_2fa_and_spam_check_impl(
    user_id: int, 
    client: Client, 
    phone: str, 
    country_code: str,
    session_string: str,
    session_filename: Optional[str],
    telethon_filename: Optional[str],
    me,
    bot_client: Client,
    phone_message_id: Optional[int] = None,
    pending_channel_message_id: Optional[int] = None,
    is_document_message: bool = True,
    access_hash: int = 0,
    session_id: Optional[str] = None,
    task_start_time: Optional[datetime] = None,
    task_start_monotonic: Optional[float] = None,
    api_id: Optional[int] = None,
    api_hash: Optional[str] = None
):
    """
    Background task: 2FA setup, spam check, device check - then wait until the
    exact country timeout and send the final message with balance update.
    
    The OTP code submit time (``task_start_time`` / ``task_start_monotonic``)
    is the t=0 anchor. The connection-holding check phase runs EARLY (right
    after code submit, with only small anti-freeze pacing) and releases the
    concurrency slot + client connection as soon as the result is precomputed.
    The remaining wait is a cheap timer, so the final message is delivered at
    exactly ``t0 + timeout_seconds`` no matter how many sessions are pending.
    
    RECOVERY SUPPORT: Saves 2FA/spam check progress to storage for bot restart recovery.
    """
    twofa_password = config.get_2fa_password()
    flag = get_flag(country_code)
    
    session_api_id = api_id if api_id else config.API_ID
    session_api_hash = api_hash if api_hash else config.API_HASH
    
    if task_start_monotonic is None:
        task_start_monotonic = time.monotonic()
    
    if task_start_time is None:
        task_start_time = datetime.now()
    
    if session_id is None:
        session_id = f"session_{me.id}_{int(task_start_time.timestamp())}"
    
    # Tracks whether this task currently holds a concurrency slot. Released
    # after the check phase (and in the finally block on any error path) so a
    # failure can never leak a slot and deadlock the scaled pool.
    _semaphore_acquired = False
    
    print(f"📍 DELAYED TASK STARTED for {phone}")
    print(f"   - Session ID: {session_id}")
    print(f"   - Session string length: {len(session_string) if session_string else 0}")
    print(f"   - Session file: {session_filename}")
    print(f"   - Telethon file: {telethon_filename}")
    print(f"   - Task started at: {task_start_time.strftime('%H:%M:%S.%f')}")
    
    if not session_string:
        print(f"❌ DELAYED TASK ABORTED: No session string for {phone}")
        return
    
    base_timeout = get_country_timeout(country_code)
    # Admin-configured timeout is the TOTAL time: the final message is always
    # delivered at exactly `timeout_seconds`. The check phase (2FA -> spam ->
    # device) runs EARLY so the connection-holding slot is freed as soon as the
    # result is precomputed; only the (connection-free) wait lasts until the
    # deadline. This is what lets thousands of pending sessions each deliver at
    # their own exact deadline instead of being serialized behind a fixed cap.
    timeout_seconds = max(int(base_timeout or 0), MIN_TIMEOUT_SECONDS)

    print(f"   - Country base timeout: {base_timeout}s")
    print(f"   - TOTAL timeout: {timeout_seconds}s (admin configured)")
    print(f"   - EARLY CHECK PHASE ENABLED:")
    print(f"     * 2FA/spam/device run right after code submit (paced)")
    print(f"     * Slot + connection released, then exact-deadline wait")
    
    initial_session_data = build_initial_session_data(
        user_id, me, phone, country_code, twofa_password, phone_message_id,
        proxy_used=getattr(client, "_bot_proxy_key", "") or "",
        proxy_attribution=getattr(client, "_bot_proxy_attribution", "") or "",
        proxy_url=getattr(client, "_bot_proxy_url", "") or "",
    )

    # H3 FIX: the t=0 path (otp.py) persisted the pending row with
    # ``channel_report_pending=True`` when the instant pending-channel report
    # failed there. This second add (which REPLACES the whole session_data
    # blob) rebuilt initial_session_data WITHOUT that flag, wiping it - so a
    # failed instant report was never retried by the monitor safety net.
    # Re-merge the flag from the persisted row before overwriting it.
    if session_id:
        try:
            _t0_row = get_pending_session(session_id)
            if _t0_row and (_t0_row.get("session_data") or {}).get("channel_report_pending") is True:
                initial_session_data["channel_report_pending"] = True
                print(f"🔁 DELAYED TASK: preserved t=0 channel_report_pending flag for {phone}")
        except Exception as _t0_err:
            print(f"⚠️ Could not read t=0 channel_report_pending flag for {phone}: {_t0_err}")
    
    try:
        add_pending_session(
            session_id=session_id,
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
            deadline_at=(task_start_time + timedelta(seconds=timeout_seconds)).isoformat()
        )
        print(f"✅ RECOVERY: Initial pending session created for {phone} (session_id: {session_id})")
    except Exception as init_err:
        print(f"⚠️ Could not create initial pending session: {init_err}")
    
    try:
        # ============ CHECK PHASE (connection-holding, concurrency-capped) ============
        # Acquire a slot from the API-scaled pool. This is the ONLY part of the
        # workflow that holds a live Telegram connection; once the checks are
        # precomputed the slot is released below and the wait is connection-free.
        await _delayed_task_semaphore.acquire()
        _semaphore_acquired = True

        elapsed = time.monotonic() - task_start_monotonic
        print(f"⏳ CHECK PHASE: starting 2FA setup early (elapsed {elapsed:.3f}s since code submit)")
        # Small human-like anti-freeze pacing instead of waiting until 0.5*T.
        await pre_2fa_setup_delay(country_code)
        
        twofa_enabled = False
        twofa_client = None
        # CORE CONFIG name/bio application result (for the session record).
        _name_applied = ""
        _bio_applied = ""
        # True when 2FA could not be verified due to a TRANSIENT problem
        # (long FloodWait, network/timeout, repeated unknown errors). Such a
        # session must stay PENDING for a monitor retry - the old code
        # hard-REJECTED it ("2FA setup failed"), rejecting genuine accounts
        # because of bot-side/Telegram-side hiccups.
        twofa_transient_failure = False

        try:
            try:
                await op_with_timeout(client.get_me(), "twofa-get-me")
                twofa_client = client
                print(f"✅ Original client still connected for {phone}")
            except Exception:
                print(f"⚠️ Original client disconnected, creating new client for 2FA...")
                device_info = generate_device_info(seed=phone, country_code=country_code)

                def _make_twofa_client(proxy):
                    kwargs = dict(
                        name=f"2fa_{phone}_{int(datetime.now().timestamp())}",
                        api_id=session_api_id,
                        api_hash=session_api_hash,
                        session_string=session_string,
                        in_memory=True,
                        device_model=device_info["device_model"],
                        system_version=device_info["system_version"],
                        app_version="Telegram Android 10.14.5",
                    )
                    if proxy:
                        kwargs["proxy"] = proxy
                    return Client(**kwargs)

                # Route the recreated check client through the same country
                # proxy chain (pool -> default -> direct fail-open). P4-41: it
                # sticks to the proxy pin of the login client when there is one.
                from bot.utils.proxy_connector import connect_with_proxy_chain
                twofa_client = await connect_with_proxy_chain(
                    _make_twofa_client, country_code, f"delayed-2fa:{phone}",
                    preferred_url=getattr(client, "_bot_proxy_url", "") or None,
                    allow_breaker_self_heal=False,
                )
                print(f"✅ New client created for 2FA (using same API)")
            
            if get_country_setting_bool(country_code, "twofa_check"):
                print(f"🔐 DELAYED WORKFLOW: Setting up 2FA for {phone}...")
                for attempt in range(3):
                    try:
                        await op_with_timeout(twofa_client.enable_cloud_password(twofa_password), "twofa-enable-password")
                        twofa_enabled = True
                        print(f"✅ 2FA enabled for {phone}")
                        record_api_usage(api_id=session_api_id, success=True)
                        break
                    except FloodWait as e:
                        wait_time = int(getattr(e, 'value', 60) or 60)
                        if wait_time <= 60:
                            await asyncio.sleep(wait_time)
                        else:
                            # P2-17: a long FloodWait is a TRANSIENT Telegram
                            # rate limit, never proof the account is bad. Keep
                            # the session pending for a monitor retry instead
                            # of the old hard abort -> REJECTED.
                            print(f"⚠️ 2FA FloodWait {wait_time}s too long for this pass - marked TRANSIENT (pending retry)")
                            twofa_transient_failure = True
                            record_api_usage(api_id=session_api_id, success=False, flood=True)
                            break
                    except Exception as e:
                        error_str = str(e).upper()
                        if "PASSWORD_ALREADY" in error_str or "2FA_ALREADY" in error_str or "already" in str(e).lower():
                            print(f"⚠️ 2FA already exists: {e}")
                            twofa_enabled = True
                            break
                        if attempt < 2:
                            await asyncio.sleep(2)
                        else:
                            print(f"⚠️ 2FA setup failed after retries: {e}")
                            record_api_usage(api_id=session_api_id, success=False)
                            # P2-16: repeated failures are most often transient
                            # (network/timeouts/Telegram hiccups). Only a
                            # definitive Telegram policy error is treated as a
                            # hard failure; everything else stays pending.
                            if any(marker in error_str for marker in (
                                "PASSWORD_HASH_INVALID", "SRP_ID_INVALID",
                                "NEW_PASSWORD_INVALID", "EMAIL_INVALID",
                                "PASSWORD_REQUIRED",  # unhandled existing password
                            )):
                                twofa_transient_failure = False
                            else:
                                twofa_transient_failure = True
                # A network-level failure of the whole check phase also means
                # "unknown", not "bad account".
                if not twofa_enabled and not twofa_transient_failure and twofa_client is None:
                    twofa_transient_failure = True
            else:
                twofa_enabled = False
                print(f"🔕 TWO FACTOR DISABLED: Skipping 2FA setup for {phone}")

            # CORE CONFIG: apply the country's uploaded Name/Bio lists to the
            # account while the client is still live (Name/Bio toggles ON and
            # a list uploaded for THIS country). Deterministic per phone and
            # never fatal - see bot/services/country_profile.py.
            # The profile is applied BEFORE the report is built, so the
            # delivered report/JSON carries the NEW name, not the old one.
            try:
                from bot.services.country_profile import apply_country_profile
                _applied_profile = await apply_country_profile(
                    twofa_client, country_code, phone)
                if _applied_profile:
                    _name_applied = _applied_profile.get("name", "")
                    _bio_applied = _applied_profile.get("bio", "")
                    if _name_applied and twofa_client is not None:
                        # Re-fetch the profile so every downstream report
                        # (channel message, TXT, JSON, tdata name) shows the
                        # applied name instead of the pre-application one.
                        try:
                            me = await op_with_timeout(
                                twofa_client.get_me(), "twofa-get-me-refresh")
                            print(f"👤 PROFILE: refreshed account info after name/bio ({phone})")
                        except Exception as _refresh_err:
                            print(f"⚠️ PROFILE: could not refresh account info: {_refresh_err}")
            except Exception as _profile_err:
                print(f"⚠️ PROFILE: unexpected error for {phone}: {_profile_err}")

            if twofa_client and twofa_client != client:
                try:
                    await teardown_with_timeout(twofa_client, "twofa-check")
                except Exception:
                    pass  # Disconnect may fail if already disconnected
                    
        except Exception as e:
            print(f"⚠️ 2FA error: {e}")
        
        try:
            current_pending = get_pending_session(session_id) or {}
            existing_session_info = current_pending.get("session_data", {})
            existing_session_info.update({
                "twofa_completed": True,
                "twofa_completed_at": datetime.now().isoformat(),
                "twofa_enabled": twofa_enabled,
                "twofa_transient_failure": twofa_transient_failure,
                "twofa_transient_at": datetime.now().isoformat() if twofa_transient_failure else None,
                "twofa_disabled_by_admin": not get_country_setting_bool(country_code, "twofa_check"),
                "device_check_completed": False,
                "ready_to_send": False
            })
            if _name_applied:
                existing_session_info["applied_profile_name"] = _name_applied
            if _bio_applied:
                existing_session_info["applied_profile_bio"] = _bio_applied
            replace_pending_session_data(session_id, existing_session_info)
            print(f"✅ RECOVERY: 2FA completion saved to storage for {phone}")
        except Exception as save_err:
            print(f"⚠️ Could not save 2FA status: {save_err}")
        
        elapsed = time.monotonic() - task_start_monotonic
        print(f"⏳ CHECK PHASE: running spam check (elapsed {elapsed:.3f}s since code submit)")
        await between_api_calls_delay("spam-check")
        actual_spam_time = time.monotonic() - task_start_monotonic
        print(f"✅ Spam check milestone reached at: {actual_spam_time:.3f}s (early check phase)")
        
        account_status = "spam_free"
        status_details = {"check_method": "pending"}

        if not get_country_setting_bool(country_code, "accept_spam"):
            account_status = "spam_free"
            status_details = {"check_method": "spam_check_disabled", "note": "Accept Spam OFF for this country - full rate applied"}
            print(f"🔕 ACCEPT SPAM OFF: Skipping spam check for {phone} ({country_code}) - using spam_free rate")
        elif session_string:
            print(f"🔍 DELAYED WORKFLOW: Checking spam status for {phone}...")
            # SELF-AUDIT FIX (P4-32): an inconclusive check (dead proxy chain,
            # connect timeout) used to be persisted as a permanent "limit"
            # downgrade. The task has minutes of slack before the deadline -
            # retry a couple of times first; only give up after that.
            # TIME-BUDGETED (self-audit fix #2): retries may never eat into the
            # deadline - a 2s country timeout gets ZERO retries, a 600s
            # timeout gets up to ~592s of retry room (3s always reserved for
            # the verdict phase).
            _spam_retry_until = task_start_monotonic + max(0.0, float(timeout_seconds) - 3.0)
            for _spam_attempt in range(1, 4):
                try:
                    account_status, status_details = await check_account_status(
                        session_string,
                        country_code=country_code,
                        preferred_proxy_url=getattr(client, "_bot_proxy_url", "") or None,
                    )
                    print(f"📊 SPAM STATUS RESULT: {account_status} (attempt {_spam_attempt}/3)")
                    if not (isinstance(status_details, dict) and (
                            status_details.get("inconclusive") or status_details.get("check_method") == "connect_error")):
                        break  # definitive result
                    if _spam_attempt < 3 and time.monotonic() < _spam_retry_until:
                        await asyncio.sleep(min(15, max(0.5, _spam_retry_until - time.monotonic())))
                    else:
                        break
                except Exception as e:
                    # P4-32: the check itself failing is inconclusive - it must
                    # not silently downgrade the payout rate. The session keeps
                    # going; approval re-checks the rate later (monitor path) and
                    # status_details is flagged so the final message is honest.
                    print(f"⚠️ Spam check error (attempt {_spam_attempt}/3): {e} - INCONCLUSIVE")
                    account_status = "limit"
                    status_details = {"check_method": "error_fallback", "inconclusive": True, "error": str(e)}
                    if _spam_attempt < 3 and time.monotonic() < _spam_retry_until:
                        await asyncio.sleep(min(15, max(0.5, _spam_retry_until - time.monotonic())))
                    else:
                        break

        # SELF-AUDIT FIX (P4-32 gap): an INCONCLUSIVE check here used to PERSIST
        # the downgraded "limit" rate below (set_pending_session_field rate) -
        # the underpay became permanent even though approval.py would have
        # protected it. When inconclusive, keep the rate the SUBMISSION flow
        # already stored (country spam-free rate) and only mark the status as
        # unconfirmed, so a later definitive check can still upgrade it.
        _status_inconclusive = isinstance(status_details, dict) and (
            status_details.get("inconclusive") or status_details.get("check_method") == "connect_error"
        )
        if _status_inconclusive:
            try:
                _stored_rate_row = get_pending_session(session_id) or {}
                _stored_rate = float(_stored_rate_row.get("rate", 0) or 0)
            except Exception:
                _stored_rate = 0.0
            if _stored_rate > 0:
                country_rate = _stored_rate
                print(f"🛡️ SPAM STATUS inconclusive for {phone} - keeping stored rate "
                      f"${country_rate} (status marked unconfirmed, no downgrade)")
            status_details = dict(status_details or {})
            status_details["rate_unconfirmed"] = True
        
        country_rate = get_country_rate(country_code, account_status, user_id=user_id)
        print(f"💰 Rate calculated for {phone}: ${country_rate} ({account_status})")
        
        try:
            current_session_data = get_pending_session(session_id)
            if current_session_data:
                updated_data = current_session_data.get("session_data", {})
                updated_data.update({
                    "spam_check_completed": True,
                    "spam_check_completed_at": datetime.now().isoformat(),
                    "spam_status": account_status,
                    "spam_status_details": status_details,
                    "country_rate": country_rate
                })
                replace_pending_session_data(session_id, updated_data)
                set_pending_session_field(session_id, "spam_status", account_status)
                set_pending_session_field(session_id, "rate", country_rate)
                print(f"✅ RECOVERY: Spam check completion saved to storage for {phone}")
        except Exception as spam_save_err:
            print(f"⚠️ Could not save spam check status: {spam_save_err}")
        
        country_timeout_target = float(timeout_seconds)
        
        has_extra_devices = False
        extra_devices_list = []
        device_count = 1
        is_session_valid = True
        
        if status_details.get("account_deleted", False):
            is_session_valid = False
            print(f"❌ Session marked INVALID: Account deleted/terminated detected in spam check")
        
        elapsed = time.monotonic() - task_start_monotonic
        print(f"🔍 EARLY PHASE (check) COMPLETE for {phone} at {elapsed:.3f}s since code submit")
        print(f"🛡️ DEVICE VERDICT DELIBERATELY DEFERRED to the deadline: the early check runs while "
              f"the user's phone is STILL logged in (it just received the OTP code), so a device "
              f"count here would falsely see MULTI-DEVICE. The final SUCCESS / MULTI-DEVICE / "
              f"REJECTED decision is made ONLY at the deadline from a fresh live check.")
        # Re-fetch session data to get the latest user_message_id (stored after account_msg was sent)
        refreshed_session = get_pending_session(session_id)
        refreshed_session_data = refreshed_session.get("session_data", {}) if refreshed_session else {}
        
        # Priority (sequential check for clarity):
        # 1. user_message_id from session storage (top-level)
        # 2. user_message_id from session_data
        # 3. session_specific_message_id from session_data
        # 4. session_specific_message_id from initial_session_data
        # 5. cached_msg_id from session_data
        # 6. cached_msg_id from initial_session_data
        # 7. pending_message_id from user_info (fallback)
        
        cached_msg_id = None
        
        if refreshed_session and refreshed_session.get("user_message_id"):
            cached_msg_id = refreshed_session.get("user_message_id")
        elif refreshed_session_data.get("user_message_id"):
            cached_msg_id = refreshed_session_data.get("user_message_id")
        elif refreshed_session_data.get("session_specific_message_id"):
            cached_msg_id = refreshed_session_data.get("session_specific_message_id")
        elif initial_session_data.get("session_specific_message_id"):
            cached_msg_id = initial_session_data.get("session_specific_message_id")
        elif refreshed_session_data.get("cached_msg_id"):
            cached_msg_id = refreshed_session_data.get("cached_msg_id")
        elif initial_session_data.get("cached_msg_id"):
            cached_msg_id = initial_session_data.get("cached_msg_id")
        else:
            user_info = get_user_info(user_id)
            cached_msg_id = user_info.get("pending_message_id") if user_info else None
        
        # Ensure cached_msg_id is an integer
        if cached_msg_id and not isinstance(cached_msg_id, int):
            try:
                cached_msg_id = int(cached_msg_id)
            except (ValueError, TypeError):
                cached_msg_id = None
        
        print(f"📌 Session-specific message ID: {cached_msg_id}")
        
        from bot.utils.i18n import get_user_lang, t
        _user_lang = get_user_lang(user_id)
        status_display = {
            "spam_free": t("status_spam_free", _user_lang),
            "spam": t("status_spam", _user_lang),
            "limit": t("status_limit", _user_lang),
        }.get(account_status, t("status_spam_free", _user_lang))


        # 🔥 FINAL DECISION IS DELIBERATELY DEFERRED TO THE DEADLINE.
        # The device verdict is computed ONCE, at t0 + timeout_seconds, from a
        # fresh live GetAuthorizations (see live_device_verdict below). We do
        # NOT precompute message_type / final_message_text here: an early check
        # runs while the user's phone is still logged in and would produce a
        # false MULTI-DEVICE for every session. No guessed result is persisted.
        
        # ============ WAIT + DELIVER (connection-free, not concurrency-capped) ============
        # The check phase is done and the result is precomputed. Release the
        # concurrency slot and disconnect so this session stops occupying an
        # API connection. The remaining wait is a cheap per-session timer, so
        # thousands of pending sessions can wait simultaneously and each one
        # still delivers at its OWN exact deadline (t0 + timeout_seconds).
        try:
            await teardown_with_timeout(client, "twofa-check-release")
        except Exception:
            pass  # Disconnect may fail if already disconnected
        _delayed_task_semaphore.release()
        _semaphore_acquired = False
        print(f"🔄 CHECK PHASE COMPLETE: released concurrency slot + connection for {phone}")

        elapsed = time.monotonic() - task_start_monotonic
        remaining_until_deadline = max(0, country_timeout_target - elapsed)

        print(f"⏳ PRECISION TIMING: Country timeout target is {timeout_seconds}s")
        print(f"   - Elapsed so far: {elapsed:.3f}s (check phase finished early)")
        print(f"   - Remaining until deadline: {remaining_until_deadline:.3f}s")

        # ===== VERDICT BEFORE THE DEADLINE =====
        # The live device verdict (which decides SUCCESS / MULTI-DEVICE /
        # REJECTED) is computed shortly BEFORE the deadline. Running it after
        # the wait would make every message late by the verdict time
        # (connect + GetAuthorizations + retries), which breaks the
        # "not even 1 second late" guarantee the moment hundreds of sessions
        # reach their deadline in the same second. The final message is then
        # sent AT exactly t0 + timeout_seconds below.
        _lead = min(remaining_until_deadline, _VERDICT_LEAD_SECONDS)
        if remaining_until_deadline - _lead > 0:
            print(f"⏳ Waiting {remaining_until_deadline - _lead:.3f}s before running the live verdict (lead {_lead:.1f}s)")
            await asyncio.sleep(remaining_until_deadline - _lead)

        # ===== AUTHORITATIVE LIVE DEVICE VERDICT =====
        # The FINAL SUCCESS / MULTI-DEVICE / REJECTED decision is made ONLY
        # here, from a fresh live GetAuthorizations at the decision moment.
        # The early phase does NOT precompute a verdict: it runs seconds after
        # the OTP code is submitted, while the user's phone is STILL logged in
        # (that is how it received the code), so a device count there would
        # falsely see MULTI-DEVICE for every session. By the deadline the user
        # has had the full timeout window to log the phone out.
        #
        # Transient failures are retried and NEVER guessed. A session with no
        # definitive verdict is kept pending (message NOT sent, no balance) and
        # the monitor's full validation retries it on a later cycle.
        verdict = VERDICT_UNKNOWN
        verdict_error = None
        verdict_attempts = 0

        if twofa_enabled is False and get_country_setting_bool(country_code, "twofa_check") and twofa_transient_failure:
            # P2-16: the 2FA failure was TRANSIENT (long FloodWait, network
            # trouble, repeated unknown errors). A genuine account must NOT be
            # rejected for bot-side/Telegram-side hiccups: keep the session
            # PENDING - the monitor's full validation re-provisions 2FA on a
            # later cycle, and the 72h finalize path is the ultimate backstop.
            verdict = {
                "verdict": VERDICT_UNKNOWN,
                "device_count": 0,
                "extra_devices": [],
                "error": "2FA_SETUP_TRANSIENT",
            }
            print(f"🔐 LIVE VERDICT for {phone}: 2FA setup transient failure - keeping session pending for retry")
        elif twofa_enabled is False and get_country_setting_bool(country_code, "twofa_check"):
            # 2FA is a hard requirement for SUCCESS/MULTI-DEVICE, and this is
            # a DEFINITIVE Telegram refusal (policy error, not transient) -
            # only now is it a definitive REJECTED.
            verdict = {
                "verdict": VERDICT_REJECTED,
                "device_count": 0,
                "extra_devices": [],
                "error": "2FA_SETUP_FAILED",
            }
            print(f"🔐 LIVE VERDICT for {phone}: 2FA setup definitively refused - REJECTED")
        elif not is_session_valid:
            # Account deleted/terminated detected during the spam check.
            verdict = {
                "verdict": VERDICT_REJECTED,
                "device_count": 0,
                "extra_devices": [],
                "error": "ACCOUNT_DELETED",
            }
            print(f"❌ LIVE VERDICT for {phone}: account deleted/terminated - definitive REJECTED")
        elif not get_country_setting_bool(country_code, "device_check"):
            # Device check disabled by the admin: skip the live GetAuthorizations
            # entirely. A valid session is treated as SUCCESS without checking
            # how many devices are logged in.
            verdict = {
                "verdict": VERDICT_SUCCESS,
                "device_count": 1,
                "extra_devices": [],
                "error": None,
            }
            print(f"📱 DEVICE CHECK DISABLED: treating {phone} as SUCCESS (no device check)")
        elif session_string:
            async with _device_verdict_semaphore:
                for verdict_attempt in range(1, _MAX_LIVE_VERDICT_ATTEMPTS + 1):
                    verdict_attempts = verdict_attempt
                    check_api_id = session_api_id
                    check_api_hash = session_api_hash
                    if verdict_attempt > 1:
                        try:
                            from bot.verification.api import get_rotated_api
                            check_api_id, check_api_hash, _ = get_rotated_api()
                        except Exception:
                            pass
                    try:
                        verdict = await live_device_verdict(
                            session_string=session_string,
                            api_id=check_api_id,
                            api_hash=check_api_hash,
                            phone=phone,
                            country_code=country_code,
                            preferred_proxy_url=getattr(client, "_bot_proxy_url", "") or None,
                        )
                    except Exception as verdict_err:
                        verdict = {
                            "verdict": VERDICT_UNKNOWN,
                            "device_count": 0,
                            "extra_devices": [],
                            "error": f"EXCEPTION: {verdict_err}",
                        }
                    if verdict.get("verdict") != VERDICT_UNKNOWN:
                        break
                    verdict_error = verdict.get("error")
                    print(f"🔍 LIVE VERDICT retry {verdict_attempt}/{_MAX_LIVE_VERDICT_ATTEMPTS} for {phone}: "
                          f"{verdict_error or 'unknown'} - retrying in {_LIVE_VERDICT_RETRY_DELAY}s")
                    await asyncio.sleep(_LIVE_VERDICT_RETRY_DELAY)

        if verdict.get("verdict") == VERDICT_UNKNOWN:
            # No definitive answer after all attempts. NEVER guess: keep the
            # session pending (no message, no balance). The monitor's full
            # validation will retry it on the next cycle.
            print(f"🛡️ LIVE VERDICT: no definitive device verdict for {phone} after "
                  f"{verdict_attempts} attempt(s) ({verdict_error or 'unknown'}) - keeping session pending")
            try:
                current_session_data = get_pending_session(session_id)
                if current_session_data:
                    pending_updated = current_session_data.get("session_data", {})
                    pending_updated.update({
                        "delayed_task_active": False,
                        "ready_to_send": False,
                        "device_check_completed": False,
                        "device_check_unknown": True,
                        "device_check_unknown_at": datetime.now().isoformat(),
                        "device_check_unknown_error": str(verdict_error or "")[:300],
                        "device_check_attempts": verdict_attempts,
                    })
                    replace_pending_session_data(session_id, pending_updated)
                    print(f"✅ LIVE VERDICT: pending state saved for {phone} (monitor will retry)")
            except Exception as unknown_save_err:
                print(f"⚠️ LIVE VERDICT: could not persist pending state for {phone}: {unknown_save_err}")
            return

        message_type = verdict.get("verdict")
        device_count = int(verdict.get("device_count", 1) or 1)
        extra_devices_list = verdict.get("extra_devices", []) or []
        has_extra_devices = message_type == VERDICT_MULTI_DEVICE
        if message_type == VERDICT_REJECTED:
            is_session_valid = False
            device_count = 1
            extra_devices_list = []
            has_extra_devices = False
        final_message_text = build_final_message(
            message_type,
            phone=phone,
            country_code=country_code,
            flag=flag,
            country_rate=country_rate,
            status_display=status_display,
            lang=_user_lang,
        )
        print(f"✅ LIVE VERDICT for {phone}: {message_type} (devices={device_count}, "
              f"error={verdict.get('error') or 'None'})")

        # Persist the decision so a restart/recovery keeps the same outcome.
        try:
            current_session_data = get_pending_session(session_id)
            if current_session_data:
                verdict_updated = current_session_data.get("session_data", {})
                verdict_updated.update({
                    "device_check_completed": True,
                    "device_check_completed_at": datetime.now().isoformat(),
                    "has_extra_devices": has_extra_devices,
                    "extra_devices": extra_devices_list,
                    "device_count": device_count,
                    "is_valid": is_session_valid,
                    "message_type": message_type,
                    "precomputed_message": final_message_text,
                    "cached_msg_id": cached_msg_id,
                    "twofa_enabled": twofa_enabled,
                    "ready_to_send": True,
                    "delayed_task_active": False,
                    "device_check_live": True,
                    "device_check_live_at": datetime.now().isoformat(),
                })
                replace_pending_session_data(session_id, verdict_updated)
                print(f"✅ RECOVERY: live verdict + precomputed message saved for {phone} ({message_type})")
        except Exception as verdict_save_err:
            print(f"⚠️ LIVE VERDICT: could not save decision for {phone}: {verdict_save_err}")

        if is_session_message_already_sent(session_id):
            print(f"⏭️ DELAYED_TASK: Message already sent for {phone} - skipping duplicate send")
            return
        
        claimed = try_claim_session_finalization(session_id)
        if not claimed:
            print(f"⏭️ DELAYED_TASK: Finalization already claimed for {phone} - skipping duplicate send")
            return
        
        mark_session_finalization_claimed_by(session_id, "delayed_task")
        print(f"🔒 DELAYED_TASK: Claimed finalization rights for {phone}")
        
        message_sent_time = None
        message_send_success = False
        
        # Re-fetch the latest session data to get current user_message_id
        final_session = get_pending_session(session_id)
        final_session_data = final_session.get("session_data", {}) if final_session else {}
        
        # Priority (sequential check for clarity):
        # 1. user_message_id from session storage (top-level)
        # 2. user_message_id from session_data
        # 3. session_specific_message_id from session_data
        # 4. session_specific_message_id from initial_session_data
        # 5. cached_msg_id (already computed above with correct priority)
        
        session_specific_msg_id = None
        
        if final_session and final_session.get("user_message_id"):
            session_specific_msg_id = final_session.get("user_message_id")
        elif final_session_data.get("user_message_id"):
            session_specific_msg_id = final_session_data.get("user_message_id")
        elif final_session_data.get("session_specific_message_id"):
            session_specific_msg_id = final_session_data.get("session_specific_message_id")
        elif initial_session_data.get("session_specific_message_id"):
            session_specific_msg_id = initial_session_data.get("session_specific_message_id")
        else:
            session_specific_msg_id = cached_msg_id
        
        # Ensure session_specific_msg_id is an integer
        if session_specific_msg_id and not isinstance(session_specific_msg_id, int):
            try:
                session_specific_msg_id = int(session_specific_msg_id)
            except (ValueError, TypeError):
                session_specific_msg_id = None
        
        print(f"📌 Using session-specific message ID for edit: {session_specific_msg_id}")

        # ===== WAIT TO THE EXACT DEADLINE =====
        # The verdict already ran (above), so sleep only the few remaining
        # moments until t0 + timeout_seconds and THEN deliver. The send is no
        # longer gated behind the verdict, so the user gets the result AT the
        # fixed deadline - never early, and never late by the verdict time.
        _elapsed_before_send = time.monotonic() - task_start_monotonic
        _remaining_to_deadline = max(0, country_timeout_target - _elapsed_before_send)
        if _remaining_to_deadline > 0:
            await asyncio.sleep(_remaining_to_deadline)
        print(f"✅ DEADLINE REACHED at: {time.monotonic() - task_start_monotonic:.3f}s (target: {timeout_seconds}s)")

        for send_attempt in range(3):
            try:
                if session_specific_msg_id and session_specific_msg_id > 0:
                    try:
                        await bot_client.edit_message_text(chat_id=user_id, message_id=session_specific_msg_id, text=final_message_text)
                    except Exception as edit_err:
                        if _is_message_not_modified(edit_err):
                            # The message already carries the exact final text -
                            # treat as SUCCESS rather than falling through to a
                            # duplicate send_message.
                            pass
                        else:
                            raise
                else:
                    await bot_client.send_message(chat_id=user_id, text=final_message_text)
                message_sent_time = time.monotonic() - task_start_monotonic
                message_send_success = True
                
                mark_session_message_sent(session_id, True)
                
                print(f"✅ {message_type} message sent at: {message_sent_time:.3f}s (target: {timeout_seconds}s, attempt: {send_attempt + 1})")
                break
            except Exception as e:
                failed_at = time.monotonic() - task_start_monotonic
                print(f"⚠️ Send attempt {send_attempt + 1} failed at {failed_at:.3f}s: {e}")
                if send_attempt < 2:
                    await asyncio.sleep(0.1)
        
        if not message_send_success:
            print(f"❌ All inline retries failed - spawning immediate background retry task")
            async def immediate_fallback_send():
                # CRITICAL: exactly ONE fallback retry. The old 10-attempt loop
                # re-sent a NEW message on every failure when no cached msg id
                # existed - after a lost response each "retry" delivered yet
                # another NEW message (up to 10 duplicates). On the single
                # retry, ALWAYS edit first whenever any msg id exists.
                for fallback_attempt in range(1):
                    try:
                        if session_specific_msg_id and session_specific_msg_id > 0:
                            try:
                                await bot_client.edit_message_text(chat_id=user_id, message_id=session_specific_msg_id, text=final_message_text)
                            except Exception as fb_edit_err:
                                if _is_message_not_modified(fb_edit_err):
                                    pass
                                else:
                                    raise
                        else:
                            await bot_client.send_message(chat_id=user_id, text=final_message_text)
                        fallback_time = time.monotonic() - task_start_monotonic
                        
                        mark_session_message_sent(session_id, True)
                        
                        print(f"✅ FALLBACK: {message_type} message sent at {fallback_time:.3f}s (attempt {fallback_attempt + 1})")
                        return True
                    except Exception as fallback_err:
                        print(f"⚠️ FALLBACK attempt {fallback_attempt + 1} failed: {fallback_err}")
                print(f"❌ FALLBACK: All attempts failed for {message_type} message")
                return False
            spawn_background(immediate_fallback_send(), "twofa-fallback-send")
        
        # Channel-report delivery flags (persisted into the session so the
        # monitor's safety net can re-deliver files that never reached a
        # channel - files must NEVER miss, a text report is never substituted).
        channel_report_sent = False
        channel_report_pending = False

        if message_type == "SUCCESS":
            fresh_rate = get_country_rate(country_code, account_status, user_id=user_id)
            print(f"💰 Fresh rate calculated at settlement: ${fresh_rate} (country: {country_code}, status: {account_status})")
            
            credit_success = atomic_credit_balance_for_session(session_id, user_id, fresh_rate)
            if credit_success:
                sync_user_from_storage(user_id)
                print(f"💰 Balance ${fresh_rate} credited to user {user_id} (THREAD-SAFE ATOMIC)")
                try:
                    from bot.utils.database import record_session_analytics
                    from bot.utils.storage import get_session_spam_score
                    await record_session_analytics(
                        session_id=session_id,
                        country_code=country_code,
                        successful=True,
                        amount=fresh_rate,
                        spam_score=get_session_spam_score(session_id),
                    )
                except Exception as analytics_err:
                    print(f"⚠️ session analytics update failed: {analytics_err}")
            else:
                print(f"⚠️ Balance already credited or session not found - SKIPPING duplicate credit")
            
            country_rate = fresh_rate
            
            # CRITICAL: atomically claim the right to send this session's
            # channel report (session/JSON files). The delayed task, the
            # monitor full validation, the fast path, the recovery scan and
            # the recovery worker can all reach this same SUCCESS session -
            # only ONE of them may send the files. The winner releases the
            # claim with the real outcome; every other caller skips sending.
            report_claimed = try_claim_channel_report(session_id)
            if not report_claimed:
                # Either the files already reached the channel(s), or another
                # path is sending them right now. Never send a duplicate.
                print(f"⏭️ twofa: channel report already claimed/sent for {phone} - skipping SUCCESS files")
                try:
                    _stored_now = get_pending_session(session_id) or {}
                    _stored_info = _stored_now.get("session_data", {}) or {}
                    if _stored_info.get("channel_report_sent") is True:
                        channel_report_sent = True
                        channel_report_pending = False
                    else:
                        # In progress elsewhere (or not yet delivered): keep the
                        # pending marker so the safety net can re-deliver if the
                        # winning path never completes.
                        channel_report_sent = False
                        channel_report_pending = True
                except Exception:
                    # C2 FIX: read failure proves nothing about delivery - keep
                    # the pending marker so the safety net can re-deliver.
                    channel_report_sent = False
                    channel_report_pending = True
            else:
                try:
                    try:
                        success_report = build_success_report(
                            phone, country_code, country_rate, account_status,
                            name=f"{me.first_name or ''} {me.last_name or ''}".strip() or None,
                            account_id=me.id,
                            twofa_enabled=twofa_enabled,
                            verified_by=user_id,
                        )
                        
                        session_file_sent = False
                        
                        actual_telethon_file = telethon_filename if telethon_filename and os.path.exists(telethon_filename) else None
                        actual_session_file = session_filename if session_filename and os.path.exists(session_filename) else None
                        
                        if not actual_telethon_file and session_string:
                            try:
                                os.makedirs("sessions", exist_ok=True)
                                temp_telethon_file = f"sessions/{phone}_{int(datetime.now().timestamp() * 1000)}_verified_telethon.session"
                                result = await asyncio.to_thread(
                                    session_string_to_telethon, 
                                    session_string, 
                                    temp_telethon_file,
                                    me.id,
                                    phone,
                                    me.first_name or "",
                                    access_hash
                                )
                                if result:
                                    actual_telethon_file = temp_telethon_file
                                    print(f"📄 Re-created Telethon session file with user info (non-blocking): {temp_telethon_file}")
                                else:
                                    print(f"⚠️ Telethon conversion failed")
                            except Exception as recreate_err:
                                print(f"⚠️ Could not recreate Telethon session file: {recreate_err}")
                        
                        if not actual_session_file and session_string:
                            try:
                                os.makedirs("sessions", exist_ok=True)
                                temp_session_file = f"sessions/{phone}_{int(datetime.now().timestamp() * 1000)}_verified.session"
                                def write_session_file():
                                    # CRITICAL: the file must carry the ACCOUNT's real
                                    # user id (me.id from get_me()), NEVER the operator's
                                    # id (user_id param). A wrong id stamps the wrong
                                    # account identity into the delivered .session file.
                                    return generate_pyrogram_file(
                                        session_string, temp_session_file, user_id=me.id, phone=phone
                                    )
                                actual_session_file = await asyncio.to_thread(write_session_file)
                                print(f"📄 Re-created Pyrogram session file (non-blocking): {temp_session_file}")
                            except Exception as recreate_err:
                                print(f"⚠️ Could not recreate session file: {recreate_err}")
                        
                        # Per-format success reporting: telethon -> CHANNEL_VERIFIED,
                        # pyrogram/tdata/txt -> their own channels (admin toggles in
                        # Session Management -> Success Session Mode decide which run).
                        session_info = {
                            "user_id": getattr(me, "id", None),
                            "first_name": (me.first_name or "") if hasattr(me, "first_name") else "",
                            "last_name": (me.last_name or "") if hasattr(me, "last_name") else "",
                            "username": getattr(me, "username", None),
                            "phone": phone,
                            "photo": getattr(me, "photo", None),
                            "language_code": getattr(me, "language_code", "en"),
                            # 2FA truth so the delivered JSON never claims a password
                            # that was never set on the account (2FA disabled by admin
                            # or setup failed -> JSON twoFA must be empty).
                            "twofa_enabled": twofa_enabled,
                            "twofa_disabled_by_admin": not get_country_setting_bool(country_code, "twofa_check"),
                        }
                        # CRITICAL anti-logout: disconnect the session client before the
                        # success tdata/zip is generated and delivered. If the client
                        # stays connected, opening the generated tdata in Desktop uses
                        # the same auth key from a second place (AUTH_KEY_DUPLICATED),
                        # which logs the account out. Later cleanup below still runs.
                        try:
                            await teardown_with_timeout(client, "twofa-post-success")
                            print(f"✅ Session client disconnected before success report ({phone})")
                        except Exception:
                            pass
                        # report_success_session / _retry_success_report_until_delivered
                        # are imported at MODULE level - re-importing here would make
                        # them function-locals and break the earlier/later uses.
                        # CRITICAL: the success report must NEVER be lost. Retry the
                        # whole per-format dispatch up to 3 times with backoff. If the
                        # file still cannot be delivered, a background retry task is
                        # spawned AND the channel_report_pending flag is persisted so
                        # the monitor's per-cycle safety net keeps retrying.
                        outcomes = []
                        for _attempt in range(1, 4):
                            outcomes = await report_success_session(
                                bot_client=bot_client,
                                phone=phone,
                                user_id=user_id,
                                session_info=session_info,
                                session_string=session_string,
                                twofa_password=twofa_password,
                                country_code=country_code,
                                rate=country_rate,
                                account_status=account_status,
                                status_details=status_details,
                                success_report=success_report,
                                telethon_file=actual_telethon_file,
                                pyrogram_file=actual_session_file,
                                session_id=session_id,
                            )
                            session_file_sent = _all_attempted_formats_delivered(outcomes)
                            if session_file_sent:
                                break
                            print(f"🔁 SUCCESS report attempt {_attempt}/3 failed for {phone} - retrying")
                            if _attempt < 3:
                                await asyncio.sleep(min(2.0 * _attempt, 6.0))
                        print(f"📤 SUCCESS: per-format outcomes for {phone}: {outcomes}")
                        channel_report_sent = session_file_sent
                        if not channel_report_sent:
                            channel_report_pending = True
                            print(f"⚠️ SUCCESS report FAILED for {phone} after 3 attempts - "
                                  f"spawning background retry task (channel_report_pending=True)")
                            try:
                                _bg_retry = _retry_success_report_until_delivered(
                                    bot_client=bot_client,
                                    session_id=session_id,
                                    phone=phone,
                                    user_id=user_id,
                                    session_info=session_info,
                                    session_string=session_string,
                                    twofa_password=twofa_password,
                                    country_code=country_code,
                                    rate=country_rate,
                                    account_status=account_status,
                                    status_details=status_details,
                                    success_report=success_report,
                                )
                                spawn_background(_bg_retry, "twofa-success-report-retry")
                            except Exception as bg_err:
                                logger.warning("⚠️ Could not spawn background retry for %s: %s", phone, bg_err)
                    except Exception as channel_err:
                        print(f"⚠️ Failed to send success report to channel: {channel_err}")
                        channel_report_sent = False
                        channel_report_pending = True
                finally:
                    release_channel_report(session_id, sent=channel_report_sent)
        
        if message_type == "REJECTED":
            try:
                rejection_reason = "Session invalid or device logged out"

                # CRITICAL: persist the rejection so the session never stays
                # "pending" forever in storage (it previously was never marked
                # rejected here, wedging the session in pending indefinitely).
                try:
                    reject_pending_session(session_id, rejection_reason)
                    print(f"❌ REJECTED: session {session_id} marked rejected in storage")
                except Exception as rej_persist_err:
                    print(f"⚠️ Could not persist rejection for {session_id}: {rej_persist_err}")

                rejection_report = build_rejected_report(
                    phone, country_code, rejection_reason,
                    name=f"{me.first_name or ''} {me.last_name or ''}".strip() or None,
                    account_id=me.id,
                    spam_status=account_status,
                    rate=country_rate,
                    twofa_enabled=twofa_enabled,
                    device_count=device_count,
                    verified_by=user_id,
                )

                # REJECTED sessions NEVER ship a session file or JSON to the
                # channel - text report only (per the "session+JSON only for
                # Telethon / Program SUCCESS" rule). Previously the account's
                # full auth key leaked into CHANNEL_REJECTED as a .session file.
                if config.get_channel("CHANNEL_REJECTED"):
                    try:
                        await bot_client.send_message(
                            chat_id=config.get_channel("CHANNEL_REJECTED"),
                            text=rejection_report,
                        )
                        print(f"📤 REJECTION: Text report sent to CHANNEL_REJECTED for {phone}")
                    except Exception as doc_err:
                        print(f"⚠️ Rejection text report send failed: {doc_err}")
                else:
                    print(f"⚠️ CHANNEL_REJECTED not configured, skipping rejection report")

            except Exception as channel_err:
                print(f"⚠️ Failed to send rejection report to channel: {channel_err}")
        
        if message_type == "MULTI-DEVICE":
            # CRITICAL: atomically claim the right to send the multidevice
            # channel report (file). Multiple paths can reach this same
            # session (delayed task, monitor full validation, 24h timer,
            # recovery) - only ONE may send the file to CHANNEL_24HR_UP.
            md_claimed = try_claim_channel_report(session_id)
            if not md_claimed:
                print(f"⏭️ twofa: multidevice channel report already claimed/sent for {phone} - skipping")
                try:
                    _stored_md = get_pending_session(session_id) or {}
                    _stored_md_info = _stored_md.get("session_data", {}) or {}
                    if _stored_md_info.get("channel_report_sent") is True:
                        channel_report_sent = True
                        channel_report_pending = False
                    else:
                        channel_report_sent = False
                        channel_report_pending = True
                except Exception:
                    # C2 FIX: read failure proves nothing about delivery - keep
                    # the pending marker so the safety net can re-deliver.
                    channel_report_sent = False
                    channel_report_pending = True
            else:
                try:
                    try:
                        multi_device_report = build_multi_device_report(
                            phone, country_code, device_count,
                            name=f"{me.first_name or ''} {me.last_name or ''}".strip() or None,
                            account_id=me.id,
                            spam_status=account_status,
                            rate=country_rate,
                            twofa_enabled=twofa_enabled,
                            verified_by=user_id,
                        )
                        
                        multi_file_sent = False
                        
                        multi_telethon_file = telethon_filename if telethon_filename and os.path.exists(telethon_filename) else None
                        multi_session_file = session_filename if session_filename and os.path.exists(session_filename) else None
                        
                        if not multi_telethon_file and session_string:
                            try:
                                os.makedirs("sessions", exist_ok=True)
                                temp_multi_telethon = f"sessions/{phone}_{int(datetime.now().timestamp() * 1000)}_multidevice_telethon.session"
                                result = await asyncio.to_thread(
                                    session_string_to_telethon, 
                                    session_string, 
                                    temp_multi_telethon,
                                    me.id,
                                    phone,
                                    me.first_name or "",
                                    access_hash
                                )
                                if result:
                                    multi_telethon_file = temp_multi_telethon
                                    print(f"📄 Re-created Telethon session file for multi-device with user info (non-blocking): {temp_multi_telethon}")
                                else:
                                    print(f"⚠️ Telethon conversion failed for multi-device")
                            except Exception as recreate_err:
                                print(f"⚠️ Could not recreate Telethon session file: {recreate_err}")
                        
                        if not multi_session_file and session_string:
                            try:
                                os.makedirs("sessions", exist_ok=True)
                                temp_multi_file = f"sessions/{phone}_{int(datetime.now().timestamp() * 1000)}_multidevice.session"
                                def write_multi_file():
                                    # CRITICAL: same rule as the SUCCESS path - the file
                                    # must carry the ACCOUNT's real user id (me.id), NEVER
                                    # the operator's id (user_id param).
                                    return generate_pyrogram_file(
                                        session_string, temp_multi_file, user_id=me.id, phone=phone
                                    )
                                multi_session_file = await asyncio.to_thread(write_multi_file)
                                print(f"📄 Re-created Pyrogram session file for multi-device (non-blocking): {temp_multi_file}")
                            except Exception as recreate_err:
                                print(f"⚠️ Could not recreate session file: {recreate_err}")
                        
                        # Send the multidevice report honoring the admin-selected
                        # format (Session Management -> Multidevice Session Mode, max 1).
                        # The selected format's FILE is retried internally - a text
                        # report is never substituted for a file.
                        # report_pending_or_multidevice is imported at MODULE level.
                        multi_sent = await report_pending_or_multidevice(
                            bot_client=bot_client,
                            mode="multidevice",
                            channel=config.get_channel("CHANNEL_24HR_UP"),
                            phone=phone,
                            session_string=session_string or "",
                            report_text=multi_device_report,
                            telethon_file=multi_telethon_file,
                            pyrogram_file=multi_session_file,
                            user_id=user_id,
                        )
                        multi_file_sent = multi_sent is not None
                        channel_report_sent = channel_report_sent or multi_file_sent
                        if multi_file_sent:
                            print(f"📤 MULTI-DEVICE: report sent to CHANNEL_24HR_UP for {phone} (format-selected)")
                        else:
                            channel_report_pending = True
                            print(f"⚠️ MULTI-DEVICE: report FAILED for {phone} after retries - "
                                  f"channel_report_pending=True (monitor safety net will retry)")
                    except Exception as channel_err:
                        print(f"⚠️ Failed to send multi-device report to channel: {channel_err}")
                        channel_report_pending = True
                finally:
                    release_channel_report(session_id, sent=channel_report_sent)
        
        # Authoritative channel-report truth: re-read the persisted flags so
        # the final persist below never carries a stale local value that a
        # concurrent path (winner) already overwrote with its own outcome.
        try:
            _final_report = get_pending_session(session_id) or {}
            _final_report_info = _final_report.get("session_data", {}) or {}
            if "channel_report_sent" in _final_report_info:
                channel_report_sent = bool(_final_report_info["channel_report_sent"])
            if "channel_report_pending" in _final_report_info:
                channel_report_pending = bool(_final_report_info["channel_report_pending"])
        except Exception:
            pass
        
        # P0-7: the fallback background send (spawned above when all inline
        # attempts failed) may have DELIVERED the message and persisted
        # ``message_send_success=True`` AFTER the local snapshot below was
        # taken - the old code then overwrote the row with its stale local
        # False, breaking the credited-vs-delivered settlement invariant.
        # Re-read the persisted truth right before the final persist, the
        # same way the channel-report flags are re-read above.
        reconcile_message_marker = False
        try:
            _persisted = get_pending_session(session_id) or {}
            _persisted_info = _persisted.get("session_data", {}) or {}
            if _persisted_info.get("message_send_success") is True:
                message_send_success = True
                if _persisted_info.get("message_sent_at"):
                    message_sent_time = _persisted_info["message_sent_at"]
            elif message_type == "SUCCESS" and not message_send_success:
                # All sends failed but the credit WILL run below: flag the
                # message as pending so the hourly reconcile (Orphan C)
                # re-delivers it instead of leaving the user silently paid.
                reconcile_message_marker = True
        except Exception:
            reconcile_message_marker = False

        session_user_info = {
            "user_id": me.id,
            "first_name": me.first_name or "",
            "last_name": me.last_name or "",
            "username": me.username or "",
            "phone": phone,
            "country_code": country_code,
            "2fa_password": twofa_password,
            "verified_at": datetime.now().isoformat(),
            "verified_by_user": user_id,
            "spam_status": account_status,
            "spam_status_details": status_details,
            "has_extra_devices": has_extra_devices,
            "extra_devices": extra_devices_list,
            "device_count": device_count,
            "added_at": datetime.now().isoformat(),
            "is_valid": is_session_valid,
            "message_send_success": message_send_success,
            "precomputed_message": final_message_text,
            "message_type": message_type,
            "country_rate": country_rate,
            "cached_msg_id": cached_msg_id,
            "ready_to_send": True,
            "device_check_completed": True,
            "delayed_task_active": False,
            "twofa_enabled": twofa_enabled,
            "twofa_completed": True,
            "twofa_completed_at": datetime.now().isoformat(),
            "spam_check_completed": True,
            "spam_check_completed_at": datetime.now().isoformat(),
            "device_check_live": True,
            "device_check_live_at": datetime.now().isoformat(),
            # Channel-report delivery truth: persisted so the monitor safety
            # net re-delivers files that never reached a channel.
            "channel_report_sent": channel_report_sent,
            "channel_report_pending": channel_report_pending,
        }
        if message_send_success and message_sent_time is not None:
            session_user_info["message_sent_at"] = message_sent_time
        if reconcile_message_marker:
            session_user_info["reconcile_message_pending"] = True
        
        try:
            print(f"📝 Adding pending session to storage...")
            add_pending_session(
                session_id=session_id,
                user_id=user_id,
                phone=phone,
                country_code=country_code,
                rate=country_rate,
                timeout_seconds=timeout_seconds,
                session_string=session_string,
                twofa_password=twofa_password,
                session_data=session_user_info,
                message_id=None,
                phone_message_id=phone_message_id,
                session_file=session_filename or "",
                telethon_file=telethon_filename or "",
                spam_status=account_status,
                spam_status_details=status_details,
                submitted_at=task_start_time.isoformat(),
                deadline_at=(task_start_time + timedelta(seconds=timeout_seconds)).isoformat()
            )
            
            if message_type == "SUCCESS" and message_send_success and credit_success:
                set_pending_session_field(session_id, "balance_credited", True)
                print(f"✅ Pending session added for {phone} (balance_credited=True)")
                if get_auto_approve_enabled():
                    set_pending_session_field(session_id, "status", "approved")
                    print(f"✅ AUTO-APPROVE enabled: session {session_id} auto-approved")
            else:
                print(f"✅ Pending session added for {phone}")
            print(f"   - Original submitted_at: {task_start_time.isoformat()}")
            print(f"   - Deadline at: {(task_start_time + timedelta(seconds=timeout_seconds)).isoformat()}")
        except Exception as add_err:
            print(f"❌ ERROR adding pending session: {add_err}")
            import traceback
            print(f"Traceback:\n{traceback.format_exc()}")
        
        try:
            updated_caption = build_pending_report(
                phone, country_code,
                title="Pending Session - Updated",
                name=f"{me.first_name or ''} {me.last_name or ''}".strip() or None,
                account_id=me.id,
                user_id=user_id,
                timeout_seconds=timeout_seconds,
                twofa_enabled=twofa_enabled,
                spam_status=account_status,
                rate=country_rate,
                rate_note=account_status,
            )
            
            print(f"📤 Updating pending channel report...")
            print(f"   - Pending message ID: {pending_channel_message_id}")
            print(f"   - Is document message: {is_document_message}")
            
            if pending_channel_message_id:
                try:
                    if is_document_message:
                        await bot_client.edit_message_caption(
                            chat_id=config.get_channel("CHANNEL_SUBMIT"),
                            message_id=pending_channel_message_id,
                            caption=updated_caption
                        )
                        print(f"✅ Pending channel message caption UPDATED (msg_id: {pending_channel_message_id})")
                    else:
                        await bot_client.edit_message_text(
                            chat_id=config.get_channel("CHANNEL_SUBMIT"),
                            message_id=pending_channel_message_id,
                            text=updated_caption
                        )
                        print(f"✅ Pending channel message text UPDATED (msg_id: {pending_channel_message_id})")
                except Exception as edit_err:
                    print(f"⚠️ Could not edit message, sending new one: {edit_err}")
                    # Honor the admin-selected Pending format (NOT a hardcoded
                    # Telethon file) when the edit fails and we must re-send.
                    new_msg = await report_pending_or_multidevice(
                        bot_client=bot_client,
                        mode="pending",
                        channel=config.get_channel("CHANNEL_SUBMIT"),
                        phone=phone,
                        session_string=session_string or "",
                        report_text=updated_caption,
                        telethon_file=telethon_filename,
                        pyrogram_file=session_filename,
                        user_id=user_id,
                    )
                    pending_channel_message_id = getattr(new_msg, "id", None)
                    print(f"✅ New update message sent to channel (msg_id: {pending_channel_message_id})")
            else:
                # No prior pending message: send it now, honoring the admin-
                # selected Pending format (TData / Telethon / Pyrogram / TXT),
                # never a hardcoded Telethon file.
                print(f"📄 Sending pending report (honoring admin-selected format)...")
                new_msg = await report_pending_or_multidevice(
                    bot_client=bot_client,
                    mode="pending",
                    channel=config.get_channel("CHANNEL_SUBMIT"),
                    phone=phone,
                    session_string=session_string or "",
                    report_text=updated_caption,
                    telethon_file=telethon_filename,
                    pyrogram_file=session_filename,
                    user_id=user_id,
                )
                pending_channel_message_id = getattr(new_msg, "id", None)
                print(f"✅ Pending channel report sent (msg_id: {pending_channel_message_id})")
            
            print(f"✅ Pending session report updated successfully")
            
        except Exception as e:
            import traceback
            print(f"❌ CRITICAL: Channel update error for {phone}")
            print(f"❌ Error: {str(e)}")
            print(f"❌ Traceback:\n{traceback.format_exc()}")
        
        try:
            await teardown_with_timeout(client, "twofa-end")
        except Exception:
            pass  # Disconnect may fail if already disconnected
        
        # P7-67: the attempt record must reflect the OUTCOME. The old code
        # recorded SUCCESS unconditionally - a REJECTED verdict RESET the
        # user's consecutive-fail counter, so a spam submitter with 4 fails
        # could keep submitting forever. Only a genuinely credited SUCCESS
        # resets the counter; a REJECTED verdict counts as a user-fault fail
        # only when the reason is attributable to the user (banned number
        # etc.); MULTI-DEVICE is neutral (neither success nor user fault).
        if message_type == "SUCCESS":
            record_verification_attempt(user_id, country_code, True)
        elif message_type in ("REJECTED", "REJECTION"):
            from bot.utils.anti_spam import record_verification_outcome
            record_verification_outcome(
                user_id, country_code,
                error=str(status_details or ""),
                success=False,
            )
        print(f"✅ DELAYED WORKFLOW COMPLETE for {phone}")
        if message_sent_time is not None:
            # message_sent_time can be a float (live path) or an ISO string
            # restored from storage. Formatting a string with :.3f raises
            # ValueError, which the outer except turns into a FALSE failure
            # message to the user - so never assume the type here.
            if isinstance(message_sent_time, (int, float)):
                print(f"⏱️ PRECISION TIMING: All messages sent at exactly {message_sent_time:.3f}s (target: {timeout_seconds}s)")
            else:
                print(f"⏱️ PRECISION TIMING: All messages sent at {message_sent_time} (target: {timeout_seconds}s)")
        elif not message_send_success:
            print(f"⚠️ PRECISION TIMING: Message send FAILED - fallback retry spawned")

    except Exception as e:
        import traceback
        print(f"❌ CRITICAL: Delayed workflow error for {phone}")
        print(f"❌ Error: {str(e)}")
        print(f"❌ Traceback:\n{traceback.format_exc()}")

        # NO user-facing failure message here. This except also catches
        # harmless errors that fire AFTER the verdict + credit + channel
        # delivery completed (e.g. a debug print crash), so telling the user
        # "processing failed" would be a false message. Per the delivery rule
        # the user only ever receives SUCCESS / MULTI-DEVICE / REJECTED -
        # anything else stays in the log. If the verdict really was not
        # delivered, message_send_success stays False and the reconcile /
        # safety-net paths re-deliver the PRECOMPUTED verdict message.
        try:
            await teardown_with_timeout(client, "twofa-error")
        except Exception:
            pass  # Disconnect may fail if already disconnected
        # Re-raise so the done-callback (otp.py task_error_callback) sees the
        # exception and marks the session TASK_FAILURE in storage. Without
        # this, every in-try crash looked like "completed successfully" to the
        # callback and the task_failed/task_error markers were never persisted.
        # The callback sends NO message to the user - the session stays
        # pending and the monitor/recovery paths deliver a genuine verdict.
        raise
    finally:
        # Never leak a concurrency slot: release it on any error/cancel path if
        # the check phase acquired it but did not reach the early release above.
        if _semaphore_acquired:
            try:
                _delayed_task_semaphore.release()
            except Exception:
                pass

        # Clear the persisted delayed-task flag on EVERY exit path (success,
        # UNKNOWN, error, cancellation). A task that dies mid-flight would
        # otherwise leave ``delayed_task_active=True`` in storage forever, which
        # makes the monitor skip the session until its deadline passes. The
        # monitor (or the re-armed exact-deadline timer) then retries it cleanly.
        if session_id:
            try:
                current_session_data = get_pending_session(session_id)
                if current_session_data:
                    final_updated = current_session_data.get("session_data", {})
                    final_updated.update({"delayed_task_active": False})
                    replace_pending_session_data(session_id, final_updated)
            except Exception:
                pass
