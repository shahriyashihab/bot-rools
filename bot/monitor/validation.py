"""Full validation flow controller: validate, device-check, approve or reject."""
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from bot import config
from bot.services.notifications import notify_user_success
from bot.services.reports import build_success_report
from bot.services.session_io import write_and_send_session_files
from bot.utils.session_handler import validate_session
from bot.utils.storage import (
    replace_pending_session_data,
    get_country_setting_bool,
    try_claim_channel_report,
    release_channel_report,
    try_claim_full_validation,
    release_full_validation,
)
from bot.monitor.approval import run_account_status_and_approve
from bot.monitor.auto_logout import run_auto_logout
from bot.monitor.device_check import run_device_check
from bot.monitor.multi_device import send_multi_device_report
from bot.monitor.rejector import handle_rejection
from bot.data.proxies import record_proxy_session_outcome
from bot.verification.device import (
    VERDICT_UNKNOWN,
    VERDICT_REJECTED,
)

logger = logging.getLogger(__name__)

# Every raw network call on this path must be bounded. A pyrogram
# ``connect()`` against a dead DC hangs forever with no internal timeout, and
# this coroutine is awaited straight from the monitor cycle - one wedged
# connect used to stall the whole monitor (and with it every pending payout).
CONNECT_TIMEOUT_SECONDS = 45
DISCONNECT_TIMEOUT_SECONDS = 30

# A non-multi-device session whose live device check PERMANENTLY returns
# UNKNOWN used to pend forever: the claim is released, the next monitor cycle
# re-checks it, UNKNOWN again - unbounded re-checking, no user notification,
# no terminal outcome. This bounds it: after NON_MD_UNKNOWN_MAX_ATTEMPTS
# consecutive UNKNOWN outcomes (~15 monitor cycles) the session is marked
# device_check_exhausted (admin notified, session stays PENDING - UNKNOWN is
# never a rejection) and the monitor stops hammering the API every cycle.
NON_MD_UNKNOWN_MAX_ATTEMPTS = 15

# Fast in-process re-entry guard (kept for speed). The AUTHORITATIVE guard is
# the persistent try_claim_full_validation marker in storage, which survives
# restarts - the old in-memory-only guard was lost on restart, so the re-armed
# deadline timer AND the monitor cycle AND the recovery scan could all enter
# run_full_validation for the same session at the same time.
_full_validation_running: set = set()


def _release_pending_claim(session_id: str, phone: str = "") -> None:
    """Release the finalization claim so a later cycle can retry this session.

    ``finalization_claimed`` lives INSIDE ``session_data`` (that is where
    ``try_claim_session_finalization`` reads/writes it), so it must be cleared
    with ``update_pending_session_field``; the top-level
    ``set_pending_session_field`` would silently no-op and wedge the session in
    pending forever, because every backstop (monitor direct path, deadline
    timer, stuck recovery, 72h cleanup) refuses to touch a claimed session.
    ``currently_processing`` IS top-level and gates the monitor's direct path.
    """
    try:
        from bot.utils.storage import (
            set_pending_session_field,
            update_pending_session_field,
        )
        update_pending_session_field(session_id, "finalization_claimed", False)
        set_pending_session_field(session_id, "currently_processing", False)
    except Exception as unclaim_err:
        print(f"⚠️ FULL VALIDATION: could not release claim for {phone or session_id}: {unclaim_err}")


async def _notify_unknown_review(
    session_id: str,
    session_data: Dict[str, Any],
    session_info: Dict[str, Any],
    phone: str,
    user_id: str,
    country_code: str,
) -> bool:
    """Notify the admin once a session's UNKNOWN device verdict is bounded.

    ``device_check_exhausted`` stops the per-cycle re-checks; the admin is told
    the session awaits manual review. Per the delivery rule the user receives
    NO interim message - the session stays pending and resolves later into one
    of the three genuine verdict messages (SUCCESS / MULTI-DEVICE / REJECTED);
    the 72h finalizer remains the later backstop.

    Idempotent: once the ``non_md_review_notified`` marker is persisted (either
    level), neither the admin DM nor the marker work runs again - otherwise the
    admin would be DM'd on EVERY monitor cycle for up to 72h per session.

    Returns True when the admin notification was delivered.
    """
    # Marker check MUST come first: this function is called on every monitor
    # cycle for exhausted/UNKNOWN rows, so the admin DM (below) must not fire
    # repeatedly.
    already_told = bool(
        session_data.get("non_md_review_notified")
        or session_info.get("non_md_review_notified")
    )
    if already_told:
        return False

    sent_any = False
    try:
        from bot.bootstrap.client import app as _app
        from bot.config import ADMIN_ID
        if ADMIN_ID:
            await _app.send_message(
                int(ADMIN_ID),
                f"🛡️ *Session awaiting review (UNKNOWN device verdict)*\n\n"
                f"📱 Phone: `{phone}`\n"
                f"🌍 Country: {country_code}\n"
                f"🆔 Session: `{session_id}`\n\n"
                f"The live device check could not get a definitive answer and "
                f"the re-check budget is spent. The session stays PENDING - "
                f"the user has NOT been rejected.",
            )
            sent_any = True
    except Exception as admin_err:
        logger.warning(f"⚠️ FULL VALIDATION: admin notification failed for {phone}: {admin_err}")

    # Per the delivery rule the user NEVER receives interim messages (e.g.
    # "awaiting manual review"). The session stays PENDING and resolves later
    # into one of the three genuine verdict messages (SUCCESS / MULTI-DEVICE
    # / REJECTED). Only the ADMIN is informed above. The marker is still
    # persisted so this scan stays idempotent.
    try:
        from bot.utils.storage import (
            set_pending_session_field as _set_review,
            update_pending_session_field as _update_review,
        )
        _update_review(session_id, "non_md_review_notified", True)
        _set_review(session_id, "non_md_review_notified", True)
    except Exception as _mark_err:
        logger.warning(f"⚠️ FULL VALIDATION: could not persist review marker for {phone}: {_mark_err}")
    session_info["non_md_review_notified"] = True
    print(f"🛡️ FULL VALIDATION: {phone} bounded as UNKNOWN - admin notified, user silent (pending)")
    return sent_any


async def run_full_validation(
    session_id: str,
    session_data: Dict[str, Any],
    session_string: str,
    twofa_password: str,
    user_id: str,
    phone: str,
    country_code: str,
    rate: float,
    md_ready_for_24h: bool
) -> Tuple[bool, Dict[str, Any]]:
    """Run the full validation flow for a deadline-reached session.

    Returns ``(skip, session_data)`` where ``skip=True`` means the caller must
    ``continue`` to the next pending session.
    """
    if session_id in _full_validation_running:
        print(f"⏭️ FULL VALIDATION: Session {session_id} already being processed - skipping")
        return True, session_data
    _full_validation_running.add(session_id)
    try:
        # Persistent re-entry guard: survives restarts so a re-armed deadline
        # timer, the monitor cycle and the recovery scan can NEVER run full
        # validation for the same session at the same time. The md_ready_for_24h
        # path previously bypassed every guard - this closes that gap too.
        if not try_claim_full_validation(session_id):
            print(f"⏭️ FULL VALIDATION: Session {session_id} validation in progress elsewhere - skipping")
            return True, session_data
        try:
            return await _run_full_validation_impl(
                session_id, session_data, session_string, twofa_password,
                user_id, phone, country_code, rate, md_ready_for_24h,
            )
        finally:
            release_full_validation(session_id)
    except Exception as e:
        # CRITICAL: an unexpected exception AFTER the finalization claim was
        # acquired must NEVER leave the claim + currently_processing markers
        # behind. Every backstop (monitor direct path, deadline timer, stuck
        # recovery, 72h cleanup) refuses to touch a claimed session, so that
        # would wedge the session in pending forever. Release both so a later
        # cycle retries it fresh.
        logger.error(f"❌ FULL VALIDATION unexpected error for {session_id}: {e}")
        print(f"🛡️ FULL VALIDATION: unexpected error for {phone} - releasing claim, keeping session pending")
        _release_pending_claim(session_id, phone)
        return True, session_data
    finally:
        _full_validation_running.discard(session_id)


async def _apply_profile_via_temp_client(session_string: str, country_code: str, phone: str, session_id: str, session_info: Optional[Dict], preferred_proxy_url: Optional[str] = None) -> None:
    """Profile-ONLY pass for sessions that skip 2FA provisioning.

    When the admin disabled 2FA for a country, the monitor never calls
    ``_provision_2fa`` - but a restart-finalized session still deserves the
    country's Name/Bio/Photo (the delayed task that would have applied it
    never ran). This connects a temp client through the pinned proxy chain,
    applies the profile, and disconnects. Never raises.
    """
    from pyrogram.client import Client

    from bot.utils.anti_spam import safe_disconnect
    from bot.utils.device_generator import generate_device_info
    from bot.utils.session_handler import get_rotated_api

    temp_client = None
    try:
        device_info = generate_device_info(seed=phone, country_code=country_code)
        api_id, api_hash, _ = get_rotated_api()

        def _make_profile_client(proxy):
            kwargs = dict(
                name=f"profile_only_{int(datetime.now().timestamp())}",
                api_id=api_id,
                api_hash=api_hash,
                session_string=session_string,
                in_memory=True,
                device_model=device_info["device_model"],
                system_version=device_info["system_version"],
                app_version="Telegram Android 0.28.1.1771-arm64-v8a",
            )
            if proxy:
                kwargs["proxy"] = proxy
            return Client(**kwargs)

        from bot.utils.proxy_connector import connect_with_proxy_chain
        temp_client = await connect_with_proxy_chain(
            _make_profile_client, country_code or None, "profile-only",
            preferred_url=preferred_proxy_url,
            allow_breaker_self_heal=False,
        )
        await _apply_profile_after_provision(
            temp_client, country_code, phone, session_id, session_info
        )
    except Exception as e:
        logger.warning(f"⚠️ PROFILE-ONLY: could not apply profile for {phone}: {e}")
    finally:
        if temp_client is not None:
            try:
                await asyncio.wait_for(
                    safe_disconnect(temp_client, "profile-only"),
                    timeout=DISCONNECT_TIMEOUT_SECONDS,
                )
            except Exception:
                pass


async def _apply_profile_after_provision(temp_client, country_code: str, phone: str, session_id: str, session_info: Optional[Dict]) -> None:
    """Apply the country's Name/Bio/Photo after a successful 2FA provisioning.

    Closes the restart gap: the delayed 2FA task (``twofa.py``) normally
    applies the CORE CONFIG profile while its client is live, but after a
    bot restart that task may never run and the monitor path finalizes the
    session instead - which previously shipped the account with its DEFAULT
    profile. This runs while ``temp_client`` is still connected, then
    refreshes the pending row (``applied_profile_name/bio/photo`` + the new
    first/last name) so every downstream report/JSON/tdata carries the
    applied profile, exactly like ``twofa.py:496-520`` does.
    Never raises; a profile glitch can never affect the verdict.
    """
    try:
        from bot.services.country_profile import apply_country_profile
        _applied = await apply_country_profile(temp_client, country_code, phone)
        if not _applied or session_info is None or not session_id:
            return
        if _applied.get("name"):
            session_info["applied_profile_name"] = _applied["name"]
        if _applied.get("bio"):
            session_info["applied_profile_bio"] = _applied["bio"]
        if _applied.get("photo"):
            session_info["applied_profile_photo"] = _applied["photo"]
        if _applied.get("name"):
            # Re-fetch so every downstream report shows the NEW name, not the
            # pre-application one (same contract as twofa.py:509-518).
            try:
                from bot.utils.net_timeouts import op_with_timeout
                me = await op_with_timeout(temp_client.get_me(), "profile-refresh-me")
                if me is not None:
                    session_info["first_name"] = (getattr(me, "first_name", "") or "")
                    session_info["last_name"] = (getattr(me, "last_name", "") or "")
                    print(f"👤 PROFILE: refreshed account info after name/bio ({phone})")
            except Exception as _refresh_err:
                print(f"⚠️ PROFILE: could not refresh account info: {_refresh_err}")
        try:
            replace_pending_session_data(session_id, session_info)
            print(f"👤 PROFILE: post-restart profile saved for {phone} ({country_code})")
        except Exception as _save_err:
            print(f"⚠️ PROFILE: could not save post-restart profile: {_save_err}")
    except Exception as _profile_err:
        print(f"⚠️ PROFILE: unexpected error for {phone}: {_profile_err}")


async def _provision_2fa(session_string: str, twofa_password: str, phone: str, country_code: str = "", preferred_proxy_url: Optional[str] = None, session_id: str = "", session_info: Optional[Dict] = None, profile_country_code: Optional[str] = None) -> bool:
    """Enable 2FA on a session that was never provisioned (post-restart path).

    The delayed 2FA setup runs inside the live verification task; after a
    restart it may never have executed. This provisions it on demand so SUCCESS
    is never reached without a confirmed 2FA password. Returns True when 2FA is
    confirmed enabled (newly provisioned or already present on the account).
    """
    from pyrogram.client import Client

    from bot.recovery.validity import setup_2fa_for_recovery
    from bot.utils.anti_spam import between_api_calls_delay, safe_disconnect
    from bot.utils.device_generator import generate_device_info
    from bot.utils.session_handler import get_rotated_api

    temp_client = None
    try:
        # P7-75: deterministic per-phone device fingerprint.
        device_info = generate_device_info(seed=phone, country_code=country_code)
        api_id, api_hash, _ = get_rotated_api()

        def _make_2fa_client(proxy):
            kwargs = dict(
                name=f"2fa_full_{int(datetime.now().timestamp())}",
                api_id=api_id,
                api_hash=api_hash,
                session_string=session_string,
                in_memory=True,
                device_model=device_info["device_model"],
                system_version=device_info["system_version"],
                app_version="Telegram Android 0.28.1.1771-arm64-v8a",
            )
            if proxy:
                kwargs["proxy"] = proxy
            return Client(**kwargs)

        # Route the 2FA-provisioning client through the same country proxy
        # chain as every other check (no proxy configured -> direct). P4-41:
        # stick to the session's pinned proxy when one exists.
        from bot.utils.proxy_connector import connect_with_proxy_chain
        temp_client = await connect_with_proxy_chain(
            _make_2fa_client, country_code or None, "2fa-provision",
            preferred_url=preferred_proxy_url,
            allow_breaker_self_heal=False,
        )
        await between_api_calls_delay("2fa-provision-connect")

        ok, _ = await setup_2fa_for_recovery(temp_client, phone, twofa_password)
        if ok:
            # P4-46: the delayed task may never have run (restart) - apply the
            # country profile NOW while this client is still connected, so a
            # restart-finalized session is never shipped with a default profile.
            # ``profile_country_code`` keeps the REAL country even when the
            # client itself dialed direct (retry path passes country_code="").
            await _apply_profile_after_provision(
                temp_client, profile_country_code if profile_country_code is not None else country_code,
                phone, session_id, session_info,
            )
        return ok
    except asyncio.TimeoutError:
        logger.warning(f"⚠️ 2FA provision connect timed out after {CONNECT_TIMEOUT_SECONDS}s for {phone}")
        return False
    except Exception as e:
        logger.warning(f"⚠️ 2FA provision error for {phone}: {e}")
        return False
    finally:
        if temp_client is not None:
            try:
                await asyncio.wait_for(
                    safe_disconnect(temp_client, "2fa-provision"),
                    timeout=DISCONNECT_TIMEOUT_SECONDS,
                )
            except Exception:
                pass


async def _run_full_validation_impl(
    session_id: str,
    session_data: Dict[str, Any],
    session_string: str,
    twofa_password: str,
    user_id: str,
    phone: str,
    country_code: str,
    rate: float,
    md_ready_for_24h: bool
) -> Tuple[bool, Dict[str, Any]]:
    """Internal implementation of the full validation flow."""
    session_info = session_data.get("session_data", {})

    # P4-41: this session's pinned proxy ("" = none) - every client this flow
    # dials sticks to it instead of re-rolling the pool.
    try:
        from bot.utils.proxy_connector import get_pinned_proxy_url
        _pinned_proxy_url = get_pinned_proxy_url(session_data, session_info)
    except Exception:
        _pinned_proxy_url = None

    # 2FA is a hard requirement for SUCCESS: if it was attempted and failed
    # (flag explicitly False) while 2FA is enabled, the session must NOT be
    # immediately rejected. A transient 2FA failure (long FloodWait, network
    # trouble) is marked ``twofa_transient_failure=True`` by the delayed task;
    # such a session stays PENDING for a later retry. Only a session that
    # failed 2FA without a transient marker is rejected - and even then the
    # on-demand provisioning below gives it one more chance first.
    if session_info.get("twofa_enabled") is False and get_country_setting_bool(country_code, "twofa_check"):
        if session_info.get("twofa_transient_failure"):
            # SELF-AUDIT FIX: the first version of this gate returned EARLY on
            # EVERY cycle, which meant the re-provisioning below could NEVER
            # run again - the session wedged pending until the 72h cleanup.
            # The retry is now TIME-BASED: wait TRANSIENT_RETRY_SECONDS since
            # the transient mark, then clear the flag so the flow below
            # re-provisions 2FA. Bounded: after 8 retries (~4h of waiting)
            # the repeated failure is treated as definitive and the session
            # resolves through the normal rejection path.
            _TRANSIENT_RETRY_SECONDS = 1800
            _TRANSIENT_MAX_RETRIES = 8
            try:
                from datetime import datetime as _dt
                _marked_at = _dt.fromisoformat(str(
                    session_info.get("twofa_transient_at") or ""
                ).replace("Z", "").replace("+00:00", ""))
                _waited = (_dt.now() - _marked_at).total_seconds()
            except (ValueError, TypeError):
                _waited = _TRANSIENT_RETRY_SECONDS  # no timestamp -> retry now
            _retries = int(session_info.get("twofa_transient_retries", 0) or 0)
            if _retries >= _TRANSIENT_MAX_RETRIES:
                session_info["twofa_transient_failure"] = False
                replace_pending_session_data(session_id, session_info)
                print(f"🔐 2FA transient retries exhausted ({_retries}) for {phone} - rejecting")
                await handle_rejection(
                    session_id, session_data, phone, user_id, country_code, rate,
                    "2FA setup failed after repeated transient errors"
                )
                return True, session_data
            if _waited < _TRANSIENT_RETRY_SECONDS:
                print(f"🔐 2FA transient for {phone} - retry #{_retries + 1} in "
                      f"{int(_TRANSIENT_RETRY_SECONDS - _waited)}s (keeping session pending)")
                return True, session_data
            # Cooldown elapsed: clear the flag so THIS cycle re-provisions.
            # SELF-AUDIT FIX: FALL THROUGH to the flow below (do NOT reject) -
            # the flag is cleared so lines 272+ re-validate and provision 2FA.
            session_info["twofa_transient_failure"] = False
            session_info["twofa_transient_retries"] = _retries + 1
            replace_pending_session_data(session_id, session_info)
            print(f"🔐 2FA transient retry #{_retries + 1}/{_TRANSIENT_MAX_RETRIES} for {phone} - re-provisioning now")
        else:
            print(f"🔐 2FA setup failed for {phone} - rejecting (2FA required for success)")
            await handle_rejection(
                session_id, session_data, phone, user_id, country_code, rate,
                "2FA setup failed"
            )
            return True, session_data

    if not md_ready_for_24h:
        from bot.utils.storage import (
            is_session_message_already_sent,
            try_claim_session_finalization,
            mark_session_finalization_claimed_by,
            get_pending_session
        )
        if is_session_message_already_sent(session_id):
            print(f"⏭️ FULL VALIDATION: Message already sent for {phone} - skipping")
            return True, session_data
        claimed = try_claim_session_finalization(session_id)
        if not claimed:
            claim_check = (get_pending_session(session_id) or {}).get("session_data", {})
            claimer = claim_check.get("finalization_claimed_by", "unknown")
            if claimer != "monitor_fast_path":
                print(f"⏭️ FULL VALIDATION: Already being processed by {claimer} for {phone} - skipping")
                return True, session_data
        mark_session_finalization_claimed_by(session_id, "monitor_full_validation")
    else:
        # 24h multi-device timer: only ONE system may own a session at a time.
        # The recovery worker / monitor fast path / delayed task claim
        # ``finalization_claimed`` for the same session - if any of them already
        # owns it, this 24h pass must SKIP and let the owner finish (otherwise
        # the user gets duplicate verdict messages and session_data is clobbered
        # by two writers). ``skip_msg_sent_check=True`` is required because the
        # MULTI-DEVICE message was already delivered at detection time
        # (``message_send_success=True``), which would otherwise block the claim.
        from bot.utils.storage import (
            try_claim_session_finalization,
            mark_session_finalization_claimed_by,
            get_pending_session,
        )
        claimed = try_claim_session_finalization(session_id, skip_msg_sent_check=True)
        if not claimed:
            claim_check = (get_pending_session(session_id) or {}).get("session_data", {})
            claimer = claim_check.get("finalization_claimed_by", "unknown")
            print(f"⏭️ FULL VALIDATION (24h): Session {session_id} already claimed by "
                  f"{claimer} for {phone} - skipping (one worker per session)")
            return True, session_data
        mark_session_finalization_claimed_by(session_id, "monitor_full_validation_24h")

    print(f"⏰ Timeout reached for session {session_id}, validating...")
    print("🔐 Validating session string...")
    is_valid = await validate_session(session_string, twofa_password, country_code, preferred_proxy_url=_pinned_proxy_url)
    print(f"✅ Session validation result: {is_valid}")

    if is_valid is None:
        # Inconclusive (transient network / flood / unexpected error). NEVER
        # reject on this - a healthy session would be destroyed by a network
        # blip. Release the claim and keep the session pending so a later
        # monitor cycle retries with a fresh check.
        print(f"🛡️ FULL VALIDATION: no definitive session-validity answer for {phone} - keeping session pending")
        _release_pending_claim(session_id, phone)
        return True, session_data

    if is_valid:
        # 2FA is a hard requirement for SUCCESS/MULTI-DEVICE. On the monitor
        # path after a restart the delayed 2FA setup may never have run; if it
        # is not confirmed, provision it now and reject if that fails. When the
        # admin disabled 2FA, skip the provisioning entirely.
        twofa_confirmed = session_info.get("twofa_enabled") is True
        if not twofa_confirmed:
            if not get_country_setting_bool(country_code, "twofa_check"):
                session_info["twofa_completed"] = True
                session_info["twofa_enabled"] = False
                session_info["twofa_disabled_by_admin"] = True
                replace_pending_session_data(session_id, session_info)
                twofa_confirmed = True
                print(f"🔕 TWO FACTOR DISABLED: skipping 2FA provisioning for {phone}")
                # P4-46b: 2FA is OFF, but the CORE CONFIG profile is a separate
                # feature - a restart-finalized session must not ship with a
                # default profile just because the delayed task never ran.
                await _apply_profile_via_temp_client(
                    session_string, country_code, phone,
                    session_id, session_info,
                    preferred_proxy_url=_pinned_proxy_url,
                )
            else:
                ok = await _provision_2fa(
                    session_string,
                    twofa_password or config.get_2fa_password(),
                    phone,
                    country_code,
                    preferred_proxy_url=_pinned_proxy_url,
                    session_id=session_id,
                    session_info=session_info,
                )
                session_info["twofa_completed"] = True
                session_info["twofa_enabled"] = ok
                replace_pending_session_data(session_id, session_info)
                if not ok:
                    # P2-16 + proxy-outage fix: a failed provisioning here is
                    # usually the NETWORK (connect timeout / dead proxy chain),
                    # not the account. Rejecting on it destroyed genuine
                    # accounts during infra outages. Instead: try ONCE via a
                    # direct connection bypassing the proxy chain; if that also
                    # fails, keep the session PENDING for a later cycle.
                    logger.warning(f"🔐 2FA provisioning failed for {phone} - retrying via direct connection")
                    ok = await _provision_2fa(
                        session_string,
                        twofa_password or config.get_2fa_password(),
                        phone,
                        "",  # empty country code -> direct (no proxy chain)
                        session_id=session_id,
                        session_info=session_info,
                        profile_country_code=country_code,
                    )
                    session_info["twofa_enabled"] = ok
                    session_info["twofa_transient_failure"] = not ok
                    if not ok:
                        from datetime import datetime as _ts_dt
                        session_info["twofa_transient_at"] = _ts_dt.now().isoformat()
                    replace_pending_session_data(session_id, session_info)
                    if not ok:
                        # The finalization claim is HELD here - release it so a
                        # later monitor cycle can retry this session fresh.
                        print(f"🔐 2FA provisioning failed for {phone} (also direct) - keeping session pending for retry")
                        _release_pending_claim(session_id, phone)
                        return True, session_data

        print(f"🔍 Checking for extra devices on {phone} during timeout verification...")
        print("🛡️ ANTI-SPAM: Applying delays for device check...")
        # A session already flagged multi-device is checked even when the admin
        # device-check toggle is OFF: the 24h "log every other device out" promise
        # cannot be cancelled retroactively by a settings switch.
        _known_md = bool(
            session_info.get("has_extra_devices")
            or session_data.get("has_extra_devices")
        )
        _unknown_exhausted = bool(
            session_data.get("device_check_exhausted")
            or session_info.get("device_check_exhausted")
        )
        if _unknown_exhausted and not _known_md:
            # Bounded UNKNOWN verdicts for a non-MD session are EXHAUSTED: stop
            # re-checking every monitor cycle. The admin was already notified
            # when the bound was hit; the user gets the pending-review notice at
            # submitted_at + 24h. The session stays PENDING (never rejected on
            # an UNKNOWN verdict); the 72h finalizer is the later backstop.
            print(f"🛡️ FULL VALIDATION: device check exhausted for {phone} - "
                  f"NOT re-checking (session stays pending for manual review)")
            try:
                await _notify_unknown_review(session_id, session_data, session_info,
                                             phone, user_id, country_code)
            except Exception as _notify_err:
                logger.warning(f"⚠️ FULL VALIDATION: review notification error for {phone}: {_notify_err}")
            _release_pending_claim(session_id, phone)
            return True, session_data
        has_extra_devices, extra_devices_list, device_count, verdict = await run_device_check(
            session_string, country_code, phone, has_known_extra_devices=_known_md,
            preferred_proxy_url=_pinned_proxy_url,
        )

        # The live verdict is authoritative. UNKNOWN means no definitive answer
        # was obtainable (transient network/flood error) - NEVER guess. Keep the
        # session pending so a later monitor cycle retries it.
        if verdict == VERDICT_UNKNOWN:
            # Multi-device sessions have a hard terminal requirement: they must
            # resolve (verify + credit) within 24h + grace. A device check that
            # PERMANENTLY returns UNKNOWN past the window would otherwise leave
            # the session pending forever (auto-logout is only reached when the
            # verdict is definitive, so md_logout_attempts never increments and
            # the 3-attempt reject rule never fires; both cleaners skip
            # multi-device sessions). Give such a session a BOUNDED number of
            # UNKNOWN retries past the window, then force the auto-logout
            # attempt (it uses its own client, independent of this verdict).
            # If auto-logout fails, md_logout_attempts increments and the
            # existing monitor reject rule (multi_device.py) resolves it.
            if md_ready_for_24h and session_info.get("has_extra_devices"):
                from bot.utils.pending_recovery import MULTI_DEVICE_LOGOUT_MAX_RETRIES
                from bot.utils.storage import get_pending_session as _gps, set_pending_session_field as _spsf
                _cur = _gps(session_id) or {}
                _unknown_n = int((_cur.get("md_unknown_attempts") or session_info.get("md_unknown_attempts") or 0))
                _unknown_n += 1
                try:
                    _spsf(session_id, "md_unknown_attempts", _unknown_n)
                    session_info["md_unknown_attempts"] = _unknown_n
                except Exception as _uerr:
                    print(f"⚠️ FULL VALIDATION: could not persist md_unknown_attempts: {_uerr}")
                if _unknown_n >= int(MULTI_DEVICE_LOGOUT_MAX_RETRIES):
                    print(f"🛡️ FULL VALIDATION: {phone} multi-device + {_unknown_n} UNKNOWN device checks "
                          f"past 24h window - forcing auto-logout attempt (bounded terminal path)")
                    has_extra_devices = True
                    # P5-52: do NOT fabricate a device count that was never
                    # observed. The last LIVE result (this pass device_count,
                    # which stays whatever the check produced / defaulted to)
                    # is reported; auto-logout verifies its own outcome with a
                    # fresh GetAuthorizations afterwards, so nothing depends
                    # on this number being >= 2.
                    # Fall through to the multi-device branch below so
                    # run_auto_logout() executes. On failure it increments
                    # md_logout_attempts -> the monitor reject rule resolves it.
                else:
                    print(f"🛡️ FULL VALIDATION: no definitive device verdict for {phone} "
                          f"(UNKNOWN {_unknown_n}/{MULTI_DEVICE_LOGOUT_MAX_RETRIES} past 24h window) - "
                          f"keeping session pending")
                    _release_pending_claim(session_id, phone)
                    return True, session_data
            else:
                # Non-MD session with a persistent UNKNOWN verdict: the old code
                # released the claim and let the next monitor cycle re-check it
                # FOREVER - no bound, no user notification, no terminal outcome.
                # Count the consecutive UNKNOWN outcomes; past the bound the
                # session is marked device_check_exhausted (admin notified, row
                # stays PENDING - UNKNOWN is never a rejection) and the gate
                # before the device check stops the per-cycle re-checking. Past
                # submitted_at + 24h the user is told the session awaits manual
                # review (once). The 72h finalizer remains the later backstop.
                _unknown_n = int(
                    session_data.get("non_md_unknown_attempts")
                    or session_info.get("non_md_unknown_attempts")
                    or 0
                ) + 1
                try:
                    from bot.utils.storage import set_pending_session_field as _set_attempt
                    _set_attempt(session_id, "non_md_unknown_attempts", _unknown_n)
                    session_info["non_md_unknown_attempts"] = _unknown_n
                except Exception as _uerr:
                    print(f"⚠️ FULL VALIDATION: could not persist non_md_unknown_attempts: {_uerr}")
                _exhausted = bool(
                    session_data.get("device_check_exhausted")
                    or session_info.get("device_check_exhausted")
                )
                if not _exhausted and _unknown_n >= NON_MD_UNKNOWN_MAX_ATTEMPTS:
                    print(f"🛡️ FULL VALIDATION: {phone} device verdict UNKNOWN "
                          f"{_unknown_n}/{NON_MD_UNKNOWN_MAX_ATTEMPTS} times - "
                          f"exhausted, session stays PENDING (never rejected on UNKNOWN)")
                    try:
                        session_info["device_check_exhausted"] = True
                        session_info["device_check_exhausted_at"] = datetime.now().isoformat()
                        from bot.utils.storage import set_pending_session_field as _set_exhausted
                        _set_exhausted(session_id, "device_check_exhausted", True)
                    except Exception as _ex_err:
                        logger.warning(f"⚠️ FULL VALIDATION: could not persist device_check_exhausted for {phone}: {_ex_err}")
                try:
                    await _notify_unknown_review(
                        session_id, session_data, session_info, phone, user_id, country_code
                    )
                except Exception as _notify_err:
                    logger.warning(f"⚠️ FULL VALIDATION: review notification error for {phone}: {_notify_err}")
                # Release the finalization claim so a later monitor/timer cycle
                # can retry this session. Without this a single inconclusive
                # check would wedge the session in pending forever (claim is
                # never re-acquirable).
                _release_pending_claim(session_id, phone)
                return True, session_data

        if verdict == VERDICT_REJECTED:
            print(f"❌ FULL VALIDATION: live device check says session invalid for {phone} - rejecting")
            await handle_rejection(
                session_id, session_data, phone, user_id, country_code, rate,
                "Session invalid (live device check)"
            )
            return True, session_data

        # If MULTIPLE devices detected
        if has_extra_devices:
            print(f"📱 Multiple devices detected ({device_count} total) for {phone}")

            added_time = (
                session_data.get("submitted_at") or
                session_info.get("submitted_at") or
                session_data.get("created_at") or
                session_data.get("added_at") or
                session_info.get("added_at") or
                datetime.now().isoformat()
            )
            hours_elapsed = 0
            try:
                added_dt = datetime.fromisoformat(
                    str(added_time).replace("Z", "").replace("+00:00", "")
                ).replace(tzinfo=None)
                hours_elapsed = (datetime.now() - added_dt).total_seconds() / 3600
            except (ValueError, TypeError):
                pass

            # The flag is written top-level by set_pending_session_field but is
            # also carried in the inner session_data dict (and only the inner
            # copy survives some reload paths), so BOTH must be consulted -
            # reading only one of them re-sent the 24h message to a user whose
            # devices were already cleaned up, or worse, let an uncleaned
            # session fall through to approval.
            already_out = bool(
                session_data.get("has_extra_devices_logged_out")
                or session_info.get("has_extra_devices_logged_out")
            )

            if already_out:
                print(f"🛡️ FULL VALIDATION: {phone} live check shows extra devices "
                      f"despite logged-out flag - clearing stale flag, NOT approving")
                try:
                    from bot.utils.storage import (
                        set_pending_session_field as _set_clear,
                        update_pending_session_field as _update_clear,
                    )
                    _update_clear(session_id, "has_extra_devices_logged_out", False)
                    _set_clear(session_id, "has_extra_devices_logged_out", False)
                except Exception as _clear_err:
                    logger.warning(f"⚠️ FULL VALIDATION: could not clear logged-out flag for {phone}: {_clear_err}")
                session_data["has_extra_devices_logged_out"] = False
                session_info["has_extra_devices_logged_out"] = False
                already_out = False

            if hours_elapsed >= 24:
                logout_success, session_data, session_info = await run_auto_logout(
                    session_id, session_data, session_info, session_string,
                    phone, user_id, rate, country_code,
                    preferred_proxy_url=_pinned_proxy_url,
                )
                already_out = bool(
                    logout_success
                    or session_data.get("has_extra_devices_logged_out")
                    or session_info.get("has_extra_devices_logged_out")
                )
            else:
                print(f"📱 {hours_elapsed:.1f}h since received - showing multi-device message (no balance)")

                await send_multi_device_report(
                    session_id, session_data, extra_devices_list, device_count,
                    phone, user_id, rate, country_code, hours_elapsed
                )

            if not already_out:
                # The extra devices are still logged in, so this session is NOT
                # finalizable yet - it stays pending until the 24h window opens
                # and the auto-logout sweep succeeds.
                #
                # run_auto_logout may have proven the session permanently
                # unusable (auth key revoked / account deactivated). No number
                # of retries can ever clean it up, so resolve it to the standard
                # REJECTED outcome now instead of burning the whole retry budget
                # and only then rejecting 12 cycles later.
                if (session_data.get("md_session_dead") or
                        session_info.get("md_session_dead")):
                    # P5-51: ``multi_device_expired`` is written by THIS
                    # branch only AFTER ``handle_rejection`` lands - never
                    # before. The old pre-set ordering reintroduced the exact
                    # crash-window bug that ``_needs_expiry_rejection``
                    # self-heals: flag set + rejection failed = a pending row
                    # invisible to the expiry rule until the 72h cleanup.
                    if not session_data.get("multi_device_expired"):
                        reason_detail = str(
                            session_data.get("md_session_dead_reason")
                            or session_info.get("md_session_dead_reason")
                            or "session no longer usable"
                        )[:120]
                        print(f"💀 FULL VALIDATION: {phone} multi-device session is dead "
                              f"({reason_detail}) - rejecting, cleanup is impossible")
                        await handle_rejection(
                            session_id, session_data, phone, user_id, country_code, rate,
                            f"Multi-device cleanup impossible ({reason_detail})",
                            report_label="Multi-Device",
                        )
                        try:
                            from bot.utils.storage import set_pending_session_field
                            set_pending_session_field(session_id, "multi_device_expired", True)
                        except Exception as md_err:
                            logger.warning(
                                f"⚠️ FULL VALIDATION: could not persist multi_device_expired "
                                f"for {phone}: {md_err}"
                            )
                    return True, session_data

                # CRITICAL: release the finalization claim before returning.
                # This path is taken on EVERY pre-24h multi-device cycle and on
                # every deferred/failed auto-logout, and it used to return while
                # still holding the claim acquired above - every backstop
                # (monitor direct path, deadline timer, stuck recovery, 72h
                # cleanup) refuses to touch a claimed session, so the account
                # was wedged in pending forever and the user was never paid.
                _release_pending_claim(session_id, phone)
                return True, session_data

        success, rate, account_status, status_details, _ = await run_account_status_and_approve(
            session_id, session_string, phone, user_id, country_code, rate
        )

        if success:
            try:
                _px_used = session_data.get("proxy_used") or session_info.get("proxy_used") or ""
                _px_attr = session_data.get("proxy_attribution") or session_info.get("proxy_attribution") or country_code
                record_proxy_session_outcome(_px_attr, _px_used, "success")
            except Exception:
                pass
            name = f"{session_info.get('first_name') or ''} {session_info.get('last_name') or ''}".strip() or None
            account_id = session_info.get("user_id") or session_info.get("account_id")
            success_report = build_success_report(
                phone, country_code, rate, account_status,
                name=name,
                account_id=account_id,
                twofa_enabled=session_info.get("twofa_enabled"),
                verified_by=user_id,
            )

            # CRITICAL: atomically claim the right to send the success channel
            # report (session/JSON files). The delayed task, the fast path, the
            # recovery scan and this full validation can all reach the same
            # session - only ONE may send the files to the channels.
            report_claimed = try_claim_channel_report(session_id)
            if not report_claimed:
                print(f"⏭️ FULL VALIDATION: channel report already claimed/sent for {phone} - skipping files")
                try:
                    from bot.utils.storage import get_pending_session as _gps
                    _stored = _gps(session_id) or {}
                    _stored_info = _stored.get("session_data", {}) or {}
                    if _stored_info.get("channel_report_sent") is True:
                        files_sent = True
                        io_error = False
                    else:
                        files_sent = False
                        io_error = False
                        session_info["channel_report_pending"] = True
                except Exception:
                    # C2 FIX: read failure proves nothing about delivery - treat
                    # as NOT sent so the safety net re-delivers the files.
                    files_sent = False
                    io_error = False
            else:
                try:
                    files_sent, io_error = await write_and_send_session_files(
                        session_id=session_id,
                        phone=phone,
                        user_id=user_id,
                        session_info=session_info,
                        session_string=session_string,
                        twofa_password=twofa_password,
                        country_code=country_code,
                        rate=rate,
                        account_status=account_status,
                        status_details=status_details,
                        caption=success_report,
                    )
                    if io_error:
                        return True, session_data
                finally:
                    release_channel_report(session_id, sent=files_sent if not io_error else False)
            # Persist the channel-report delivery truth. When files_sent is
            # False, channel_report_pending=True is kept so the monitor's
            # per-cycle safety net re-delivers the files (a text report is
            # NEVER substituted for a file).
            session_info["channel_report_sent"] = files_sent
            session_info["channel_report_pending"] = not files_sent
            replace_pending_session_data(session_id, session_info)

            # Persist the SUCCESS verdict + sent marker BEFORE notifying the user
            # so the channel-report safety net can retry files immediately if
            # they failed (it requires message_send_success=True). The user
            # notification is best-effort; if it fails, the hourly reconciliation
            # will re-deliver it (Orphan C: approved + no message).
            try:
                from bot.utils.storage import mark_session_message_sent
                session_info["message_type"] = "SUCCESS"
                session_info["is_valid"] = True
                session_info["ready_to_send"] = True
                mark_session_message_sent(session_id, True)
                replace_pending_session_data(session_id, session_info)
            except Exception as mark_err:
                logger.warning(f"⚠️ FULL VALIDATION: could not persist success marker for {phone}: {mark_err}")

            # Notify user and EDIT previous message instead of deleting
            await notify_user_success(
                session_id, session_data, phone, user_id, country_code, rate, account_status
            )

    else:
        await handle_rejection(
            session_id, session_data, phone, user_id, country_code, rate,
            "Device not logged in after timeout"
        )

    return False, session_data
