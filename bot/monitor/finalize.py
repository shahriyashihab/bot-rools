"""Shared claim → send → report finalization for precomputed sessions (DRY).

The 150+ line block that was copy-pasted between the recovery-mode scan
(``monitor/recovery_scan.py``) and the fast-path finalization
(``monitor/fast_path.py``) now lives here once. Both callers are thin
wrappers around :func:`finalize_precomputed_session`, and the SUCCESS credit
sub-block is delegated to :mod:`bot.services.accounting`.
"""
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Tuple

from bot import config
from bot.bootstrap.client import app
from bot.services.accounting import credit_and_log_session
from bot.services.reports import (
    build_multi_device_report,
    build_rejected_report,
    build_success_report,
    send_report_to_channel,
)
from bot.utils.session_handler import get_country_rate
from bot.utils.storage import (
    _deadline_is_in_future,
    _is_message_not_modified,
    approve_pending_session,
    reject_pending_session,
    try_claim_session_finalization,
    mark_session_finalization_claimed_by,
    mark_session_message_sent,
    is_session_message_already_sent,
    replace_pending_session_data,
    get_pending_session,
    get_country_setting_bool,
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
from bot.utils.proxy_connector import get_pinned_proxy_url

logger = logging.getLogger(__name__)


async def finalize_precomputed_session(
    session_id: str,
    session_info: Dict[str, Any],
    phone: str,
    user_id: str,
    country_code: str,
    precomputed_message: str,
    cached_msg_id,
    message_type: str,
    caller_label: str,
    report_label: str,
    claim_owner: str,
    recovered: bool = False,
    rejection_reason: str = "Device not logged in",
    handle_not_modified_as_complete: bool = False,
    session_string: str = "",
) -> Tuple[bool, Dict[str, Any]]:
    """Claim finalization rights, deliver the precomputed message and report.

    Returns ``(skip, session_info)``. ``skip=True`` means the caller must
    ``continue`` to the next pending session. ``skip=False`` means the
    caller should fall back to the full validation flow (only relevant for
    the fast path).
    """
    # ===== EARLY-DELIVERY GUARD =====
    # A precomputed verdict persisted BEFORE its exact deadline (the delayed
    # task persists the verdict, then sleeps to the deadline and delivers)
    # must NEVER be surfaced early. While the deadline is still in the future
    # this finalizer keeps the row pending - the deadline timer delivers it at
    # the right moment. Callers that filter first (recovery scan, fast path)
    # are unaffected; this makes the guard unconditional for future callers.
    try:
        _deadline_root = get_pending_session(session_id) or {}
        _deadline_value = (
            _deadline_root.get("deadline_at")
            or session_info.get("deadline_at")
        )
    except Exception:
        _deadline_value = session_info.get("deadline_at")
    if _deadline_is_in_future(_deadline_value):
        print(f"⏳ {caller_label}: deadline not reached for {phone} - keeping session pending (deadline timer delivers)")
        return True, session_info

    # A precomputed MULTI-DEVICE verdict whose extra-device flag was lost
    # (has_extra_devices falsy in both levels) and which has no balance credited
    # is a stale hold decision that must NOT be re-sent - the account's real
    # state may have changed. Fall back to the full live validation flow.
    try:
        _root_md_check = get_pending_session(session_id) or {}
    except Exception:
        _root_md_check = {}
    _md_flag_lost = bool(
        message_type == "MULTI-DEVICE"
        and not (session_info.get("has_extra_devices") or (_root_md_check.get("session_data") or {}).get("has_extra_devices"))
        and not (_root_md_check.get("balance_credited") or session_info.get("balance_credited"))
    )
    if _md_flag_lost:
        print(f"🛡️ {caller_label}: MULTI-DEVICE precomputed but extra-device flag "
              f"lost for {phone} - falling back to full validation")
        return False, session_info

    # ===== AUTHORITATIVE LIVE DEVICE VERDICT =====
    # The precomputed message_type is ONLY trusted when it was produced by the
    # live delayed task at the deadline (``device_check_live`` marker). A
    # session WITHOUT that marker (legacy pre-fix session) had its verdict
    # computed during the EARLY check phase, seconds after the OTP code was
    # submitted, while the user's own phone was STILL logged in - so
    # MULTI-DEVICE may be a false positive. Re-run the live check NOW and
    # adopt its verdict. On an inconclusive (UNKNOWN) verdict we NEVER guess:
    # the session stays pending for a later monitor retry.
    # The admin device-check toggle can suppress this re-check for ordinary
    # sessions, but NOT for one already flagged ``has_extra_devices`` - that
    # account's 24h device-cleanup guarantee must still be verified live.
    # A ``device_check_live`` verdict older than 15 minutes is ALSO re-checked:
    # the marker alone must not approve a session forever on a stale decision.
    _device_check_live = session_info.get("device_check_live")
    _device_check_stale = False
    if _device_check_live:
        _verdict_ts = session_info.get("device_check_live_at") or session_info.get("device_check_completed_at")
        if _verdict_ts:
            try:
                _verdict_dt = datetime.fromisoformat(str(_verdict_ts).replace("Z", "").replace("+00:00", ""))
                _device_check_stale = (datetime.now() - _verdict_dt).total_seconds() > 900
            except (ValueError, TypeError):
                _device_check_stale = False
    if session_string and (not _device_check_live or _device_check_stale) and (
        get_country_setting_bool(country_code, "device_check") or session_info.get("has_extra_devices")
    ):
        try:
            from bot import config as _config
            _root_session = get_pending_session(session_id) or {}
            _api_id = (
                _root_session.get("api_id")
                or session_info.get("api_id")
                or _config.API_ID
            )
            _api_hash = (
                _root_session.get("api_hash")
                or session_info.get("api_hash")
                or _config.API_HASH
            )
            _verdict = await live_device_verdict(
                session_string, _api_id, _api_hash, phone,
                preferred_proxy_url=get_pinned_proxy_url(_root_session, session_info),
            )
            _v = _verdict.get("verdict")
            print(f"🔍 {caller_label} LIVE VERDICT for {phone}: {_v} "
                  f"(precomputed was {message_type}, error={_verdict.get('error') or 'None'})")

            if _v == VERDICT_UNKNOWN:
                print(f"🛡️ {caller_label}: no definitive device verdict for {phone} - keeping session pending")
                return True, session_info
            if _v == VERDICT_REJECTED:
                print(f"❌ {caller_label}: live check says session invalid for {phone} - rejecting")
                message_type = "REJECTED"
                from bot.utils.storage import format_message
                from bot.utils.i18n import get_user_lang
                precomputed_message = format_message(
                    "verification_failed_message",
                    get_user_lang(int(user_id)),
                    phone=phone,
                    country_code=country_code,
                    flag="",
                )
            elif _v == VERDICT_SUCCESS:
                if message_type != "SUCCESS":
                    print(f"✅ {caller_label}: live check: {phone} has only this session - UPGRADING to SUCCESS")
                    from bot.utils.i18n import get_user_lang, t
                    _lang = get_user_lang(int(user_id))
                    _spam_status = session_info.get("spam_status", "limit")
                    _status_display = {
                        "spam_free": t("status_spam_free", _lang),
                        "spam": t("status_spam", _lang),
                        "limit": t("status_limit", _lang),
                    }.get(_spam_status, t("status_spam_free", _lang))
                    from bot.utils.storage import format_message
                    _upgrade_rate = get_country_rate(
                        country_code, _spam_status, user_id=user_id,
                    )
                    message_type = "SUCCESS"
                    precomputed_message = format_message(
                        "success_message",
                        _lang,
                        phone=phone,
                        price=f"{_upgrade_rate:.2f}",
                        status=_status_display,
                    )
            elif _v == VERDICT_MULTI_DEVICE:
                if message_type != "MULTI-DEVICE":
                    print(f"📱 {caller_label}: live check confirms extra devices for {phone} - keeping MULTI-DEVICE (24h hold)")
                    message_type = "MULTI-DEVICE"
                    # The precomputed message (e.g. a SUCCESS text from the early
                    # check) no longer matches the live verdict. Rebuild it so the
                    # user is never shown a SUCCESS message for a multi-device hold.
                    from bot.utils.storage import format_message
                    from bot.utils.i18n import get_user_lang
                    precomputed_message = format_message(
                        "multi_device_message",
                        get_user_lang(int(user_id)),
                        phone=phone,
                    )
                print(f"🔍 {caller_label}: live check: {phone} still has extra devices - keeping MULTI-DEVICE (24h hold)")
                session_info["device_count"] = int(_verdict.get("device_count", 2) or 2)
                session_info["extra_devices"] = _verdict.get("extra_devices", []) or []
                # Flag the 24h hold explicitly so the exact-24h timer
                # (twenty_four_hour.py) arms and later auto-logouts + verifies.
                session_info["has_extra_devices"] = True
                session_info["is_valid"] = True
            session_info["message_type"] = message_type
            session_info["precomputed_message"] = precomputed_message
            session_info["device_check_live"] = True
            session_info["device_check_live_at"] = datetime.now().isoformat()
            if message_type == "SUCCESS":
                session_info["has_extra_devices"] = False
                session_info["extra_devices"] = []
                session_info["device_count"] = 1
                session_info["is_valid"] = True
            elif message_type == "REJECTED":
                session_info["has_extra_devices"] = False
                session_info["extra_devices"] = []
                session_info["device_count"] = 1
                session_info["is_valid"] = False
            replace_pending_session_data(session_id, session_info)
            print(f"✅ {caller_label}: live verdict {message_type} persisted for {phone}")
        except Exception as recheck_err:
            # Never guess from an exception: keep the session pending.
            print(f"⚠️ {caller_label}: live verdict error for {phone}: {recheck_err} - keeping session pending")
            return True, session_info

    if is_session_message_already_sent(session_id):
        print(f"⏭️ {caller_label}: Message already sent for {phone} - skipping")
        return True, session_info

    claimed = try_claim_session_finalization(session_id)
    if not claimed:
        print(f"⏭️ {caller_label}: Finalization already claimed for {phone} - skipping")
        return True, session_info

    mark_session_finalization_claimed_by(session_id, claim_owner)

    if recovered:
        print(f"🔄 RECOVERY MODE: Found interrupted session {session_id} for {phone}")
        print(f"   - 2FA completed: {session_info.get('twofa_completed', False)}")
        print(f"   - Spam check completed: {session_info.get('spam_check_completed', False)}")
        print(f"   - Device check completed: {session_info.get('device_check_completed', False)}")
        print(f"   - Message type: {message_type}")
        print("   - Processing immediately...")
    else:
        print(f"🔒 {caller_label}: Claimed finalization rights for {phone}")
        print(f"⚡ {caller_label}: Using precomputed {message_type} message for {phone}")

    try:
        # Persist the decision BEFORE delivering: a crash between the message
        # send and the credit/report steps must leave a self-describing record
        # (message_type + precomputed_message) so the reconciliation scan and
        # the channel-report safety net can complete the session instead of
        # wedging it in pending forever.
        session_info["message_type"] = message_type
        session_info["precomputed_message"] = precomputed_message
        session_info["ready_to_send"] = True
        replace_pending_session_data(session_id, session_info)

        # CRITICAL: coerce BEFORE comparing. Persisted message ids come back as
        # str from JSON/SQLite, so ``cached_msg_id > 0`` on a string raises
        # TypeError and the whole send block falls into the outer handler.
        try:
            _cached_int = int(cached_msg_id or 0)
        except (TypeError, ValueError):
            _cached_int = 0
        if _cached_int > 0:
            try:
                await app.edit_message_text(chat_id=int(user_id), message_id=_cached_int, text=precomputed_message)
            except Exception as _edit_err:
                if _is_message_not_modified(_edit_err):
                    # The original message already carries the exact final text -
                    # that IS the delivery. Do NOT fall through to a duplicate
                    # send_message (applies even when handle_not_modified_as_complete
                    # is False on the fast path).
                    print(f"✅ {caller_label}: {message_type} message already shows the final verdict for {phone} (MESSAGE_NOT_MODIFIED)")
                else:
                    raise
        else:
            await app.send_message(chat_id=int(user_id), text=precomputed_message)

        mark_session_message_sent(session_id, True)

        print(f"✅ {caller_label}: {message_type} message sent for {phone}")

        persisted_session = get_pending_session(session_id) or {}
        persisted_info = persisted_session.get("session_data", {})
        already_reported = persisted_info.get("channel_report_sent", False)

        session_info["message_send_success"] = True
        session_info["message_sent_at"] = datetime.now().isoformat()
        if recovered:
            session_info["recovered_by_monitor"] = True
        session_info["channel_report_sent"] = True if already_reported else False
        replace_pending_session_data(session_id, session_info)

        if already_reported:
            print(f"⏭️ {caller_label}: Channel report already sent for {phone} - skipping report")
            return True, session_info

        if message_type == "SUCCESS":
            spam_status = session_info.get("spam_status", "limit")
            fresh_rate = get_country_rate(country_code, spam_status, user_id=user_id)

            credit_ok = await credit_and_log_session(
                session_id=session_id,
                user_id=int(user_id),
                country_code=country_code,
                rate=fresh_rate,
                phone=phone,
                spam_status=spam_status,
                log_action="session_verified",
                include_log=True,
            )

            try:
                current_status = get_pending_session(session_id) or {}
                # Approve ONLY when the money is settled (credited now, or
                # already credited by another path). Flipping an UNCREDITED row
                # to "approved" is exactly what hides it from every repair
                # scanner - they all filter status == "pending" - so a failed
                # credit would never be retried and the user would never be paid.
                settled = bool(credit_ok) or bool(current_status.get("balance_credited"))
                if settled:
                    if current_status.get("status", "pending") == "pending":
                        approve_pending_session(session_id)
                else:
                    logger.error(
                        f"❌ {caller_label}: balance credit did NOT land for {phone} - "
                        f"leaving status='pending' so reconciliation retries it"
                    )
            except Exception as verify_err:
                logger.warning(f"⚠️ {caller_label}: approve_pending_session failed: {verify_err}")

            if not already_reported:
                # CRITICAL: atomically claim the right to send the success
                # channel report (session/JSON files). The delayed task, the
                # monitor full validation, the recovery scan and this fast
                # path can all reach the same session - only ONE may send.
                if not try_claim_channel_report(session_id):
                    print(f"⏭️ {caller_label}: channel report already claimed/sent for {phone} - skipping files")
                    # Never optimistically mark sent: mirror the stored truth.
                    # If the winning path is still in progress, keep pending so
                    # the safety net can re-deliver if it never completes.
                    try:
                        _stored_now = get_pending_session(session_id) or {}
                        _stored_info = _stored_now.get("session_data", {}) or {}
                        if _stored_info.get("channel_report_sent") is True:
                            session_info["channel_report_sent"] = True
                            session_info["channel_report_pending"] = False
                        else:
                            session_info["channel_report_sent"] = False
                            session_info["channel_report_pending"] = True
                    except Exception:
                        # C2 FIX: a read failure proves NOTHING about delivery.
                        # The old fallback claimed sent=True, so the safety net
                        # skipped files that may never have been sent (permanent
                        # session-file loss). Keep the pending marker instead -
                        # the safety net re-checks stored truth and re-delivers.
                        session_info["channel_report_sent"] = False
                        session_info["channel_report_pending"] = True
                    replace_pending_session_data(session_id, session_info)
                else:
                    try:
                        # CRITICAL: SUCCESS must deliver the session files + JSON
                        # (every admin-enabled format), never a text-only report.
                        # The delayed task normally does this; when the fast path /
                        # recovery scan finalizes a session it MUST send the same
                        # per-format files (telethon+JSON, pyrogram+JSON, tdata,
                        # txt) so the buyer always receives a usable account.
                        _root = get_pending_session(session_id) or {}
                        _twofa = (
                            _root.get("twofa_password")
                            or session_info.get("twofa_password")
                            or session_info.get("2fa_password")
                            or ""
                        )
                        success_report = build_success_report(
                            phone, country_code, fresh_rate, spam_status,
                            report_label=report_label,
                            name=(session_info.get("first_name") or ""),
                            account_id=session_info.get("user_id") or session_info.get("account_id"),
                            twofa_enabled=session_info.get("twofa_enabled"),
                            verified_by=user_id,
                        )
                        files_sent = False
                        outcomes = []
                        if session_string:
                            from bot.services.session_reporting import (
                                _all_attempted_formats_delivered,
                                report_success_session,
                            )
                            # Retry the whole per-format dispatch up to 3 times so a
                            # transient channel failure can never permanently lose
                            # the session files.
                            for _attempt in range(1, 4):
                                outcomes = await report_success_session(
                                    bot_client=app,
                                    phone=phone,
                                    user_id=user_id,
                                    session_info=session_info,
                                    session_string=session_string,
                                    twofa_password=_twofa,
                                    country_code=country_code,
                                    rate=fresh_rate,
                                    account_status=spam_status,
                                    status_details=session_info.get("status_details") or {},
                                    success_report=success_report,
                                    telethon_file=None,
                                    pyrogram_file=None,
                                    session_id=session_id,
                                )
                                files_sent = _all_attempted_formats_delivered(outcomes)
                                if files_sent:
                                    break
                                print(f"🔁 {caller_label}: SUCCESS report attempt {_attempt}/3 failed for {phone} - retrying")
                                if _attempt < 3:
                                    await asyncio.sleep(min(2.0 * _attempt, 6.0))
                        # NEVER substitute a text report for a file. When the files
                        # could not be delivered, persist channel_report_pending so
                        # the monitor's per-cycle safety net keeps retrying until
                        # the files actually reach the channels.
                        session_info["channel_report_sent"] = files_sent
                        session_info["channel_report_pending"] = not files_sent
                        replace_pending_session_data(session_id, session_info)
                        print(f"📤 {caller_label}: SUCCESS report sent for {phone} "
                              f"(files_sent={files_sent}, outcomes={outcomes})")
                    except Exception as channel_err:
                        logger.warning(f"⚠️ {caller_label}: Channel report failed: {channel_err}")
                        session_info["channel_report_pending"] = True
                    finally:
                        release_channel_report(session_id, sent=session_info.get("channel_report_sent", False))

        elif message_type in ("REJECTED", "REJECTION"):
            try:
                current_status = get_pending_session(session_id) or {}
                if current_status.get("status", "pending") == "pending":
                    reject_pending_session(session_id, rejection_reason)
            except Exception as rej_status_err:
                logger.warning(f"⚠️ {caller_label}: reject_pending_session failed: {rej_status_err}")

            if not already_reported:
                try:
                    await send_report_to_channel(
                        chat_id=config.get_channel("CHANNEL_REJECTED"),
                        report=build_rejected_report(
                            phone, country_code, rejection_reason,
                            report_label=report_label, user_id=user_id,
                        ),
                    )
                    session_info["channel_report_sent"] = True
                    replace_pending_session_data(session_id, session_info)
                    print(f"📤 {caller_label}: Rejection report sent for {phone}")
                except Exception as rej_err:
                    logger.warning(f"⚠️ {caller_label}: Rejection report failed: {rej_err}")

        elif message_type == "MULTI-DEVICE":
            if not already_reported:
                # CRITICAL: atomically claim the right to send the multidevice
                # channel report (file). Only ONE path may deliver it.
                if not try_claim_channel_report(session_id):
                    print(f"⏭️ {caller_label}: multidevice channel report already claimed/sent for {phone} - skipping")
                    session_info["channel_report_sent"] = True
                    session_info["channel_report_pending"] = False
                    replace_pending_session_data(session_id, session_info)
                else:
                    try:
                        _md_report = build_multi_device_report(
                            phone, country_code,
                            session_info.get("device_count", "Unknown"),
                            report_label=report_label, user_id=user_id,
                        )
                        # Honor the admin-selected Multidevice Session Mode format
                        # (Session Management -> Multidevice, max 1). The selected
                        # format's FILE is retried internally - a text report is
                        # NEVER substituted for a file. On failure the pending flag
                        # is persisted so the monitor safety net re-delivers it.
                        from bot.services.session_reporting import report_pending_or_multidevice_safe
                        _md_sent = await report_pending_or_multidevice_safe(
                            bot_client=app,
                            mode="multidevice",
                            channel=config.get_channel("CHANNEL_24HR_UP"),
                            phone=phone,
                            session_string=session_string or "",
                            report_text=_md_report,
                            telethon_file=None,
                            pyrogram_file=None,
                            user_id=user_id,
                        )
                        session_info["channel_report_sent"] = _md_sent is not None
                        session_info["channel_report_pending"] = _md_sent is None
                        print(f"📤 {caller_label}: Multi-device report sent for {phone} "
                              f"(delivered={_md_sent is not None})")
                    except Exception as md_err:
                        logger.warning(f"⚠️ {caller_label}: Multi-device report failed: {md_err}")
                        session_info["channel_report_pending"] = True
                    finally:
                        replace_pending_session_data(session_id, session_info)
                        release_channel_report(session_id, sent=session_info.get("channel_report_sent", False))
        else:
            logger.warning(f"⚠️ {caller_label}: Unknown message_type '{message_type}' for {phone} - skipping (no balance added)")

        return True, session_info
    except Exception as err:
        error_str = str(err)
        # CRITICAL: never leave the finalization claim behind on an exception.
        # The claim was acquired above and, unless the message was already
        # marked sent, every backstop would refuse to touch this session and
        # it would wedge in pending forever. Release it so the fallback
        # (full validation / a later monitor cycle) can re-claim and retry.
        try:
            from bot.utils.storage import update_pending_session_field
            if not session_info.get("message_send_success"):
                update_pending_session_field(session_id, "finalization_claimed", False)
        except Exception as unclaim_err:
            logger.warning(f"⚠️ {caller_label}: could not release claim after error: {unclaim_err}")

        if handle_not_modified_as_complete and "MESSAGE_NOT_MODIFIED" in error_str:
            print(f"✅ {caller_label}: Message already sent for {phone} - marking complete")
            try:
                from bot.utils.storage import set_pending_session_field
                session_info["message_send_success"] = True
                if recovered:
                    session_info["recovered_by_monitor"] = True
                # CRITICAL: only mark the channel report as sent when it was
                # REALLY already delivered. MESSAGE_NOT_MODIFIED means the USER
                # message was already sent by a previous attempt - the channel
                # files may still be missing (a crash between "user message"
                # and "channel report"). Marking channel_report_sent=True here
                # would make the safety net skip the session and the files
                # would be permanently lost. Mark pending instead so the
                # per-cycle safety net re-delivers them.
                try:
                    _preexisting = (get_pending_session(session_id) or {}).get("session_data", {})
                    if _preexisting.get("channel_report_sent") is True:
                        session_info["channel_report_sent"] = True
                        session_info["channel_report_pending"] = False
                    else:
                        session_info["channel_report_sent"] = False
                        session_info["channel_report_pending"] = True
                except Exception:
                    # C2 FIX: read failure proves nothing about delivery - the
                    # pending marker keeps the safety net's re-delivery alive.
                    session_info["channel_report_sent"] = False
                    session_info["channel_report_pending"] = True
                replace_pending_session_data(session_id, session_info)
                if recovered:
                    set_pending_session_field(session_id, "recovered_by_monitor", True)
            except Exception as mark_err:
                logger.warning(f"⚠️ Could not mark session complete: {mark_err}")
            return True, session_info
        if handle_not_modified_as_complete:
            logger.warning(f"⚠️ {caller_label} failed for {phone}: {err}")
            # The message was NOT delivered and the session is still pending.
            # Clear the monitor's "currently_processing" marker so the next
            # monitor cycle retries it immediately instead of waiting for the
            # 1h stale-claim release (which would idle the session for an hour).
            try:
                from bot.utils.storage import set_pending_session_field
                if not session_info.get("message_send_success"):
                    set_pending_session_field(session_id, "currently_processing", False)
            except Exception as clear_err:
                logger.warning(f"⚠️ {caller_label}: could not clear currently_processing: {clear_err}")
            return True, session_info
        logger.warning(f"⚠️ {caller_label} failed: {err} - falling back to full validation")
        return False, session_info
