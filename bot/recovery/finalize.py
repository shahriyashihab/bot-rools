"""Refactored from bot.utils.pending_recovery: Expired session finalization."""
from datetime import datetime
from typing import Dict

from pyrogram.client import Client

from bot import config
from bot.recovery.cleanup import _safe_int, cleanup_no_otp_sessions
from bot.services.reports import (
    build_expired_stuck_report,
    send_report_to_channel,
)
from bot.utils.country_data import get_flag
from bot.utils.i18n import get_user_lang
from bot.utils.storage import (
    format_message,
    get_all_pending_sessions,
    get_stuck_session_expiry_hours,
    is_session_message_already_sent,
    mark_session_finalization_claimed_by,
    mark_session_message_sent,
    reject_pending_session,
    save_storage,
    try_claim_session_finalization,
)
async def finalize_expired_sessions(bot_client: Client) -> Dict[str, int]:
    """
    Handle stuck sessions that have session_string but somehow weren't processed.
    
    Only handles:
    - Sessions WITH session_string older than 72 hours → Reject (stuck for too long)
    
    For sessions WITHOUT session_string:
    - cleanup_no_otp_sessions() handles them: the user is WARNED at
      NO_OTP_WARNING_MINUTES and the submission is cancelled (with a
      notification) at EXPIRED_NO_OTP_MINUTES. It is never a silent delete.
    
    Returns:
        Stats dict with counts
    """
    stats = {
        "total_checked": 0,
        "deleted_no_otp": 0,
        "expired_old": 0,
        "already_done": 0,
        "already_claimed": 0,
        "notify_failed": 0,
        "errors": 0
    }
    
    deleted = await cleanup_no_otp_sessions(bot_client)
    stats["deleted_no_otp"] = deleted
    
    all_pending = get_all_pending_sessions()
    now = datetime.now()
    
    sessions_to_expire = []
    
    for session_id, session_data in all_pending.items():
        stats["total_checked"] += 1
        
        if session_data.get("status") != "pending":
            continue
        
        inner_data = session_data.get("session_data", {})
        
        if inner_data.get("message_send_success") == True:
            stats["already_done"] += 1
            continue
        
        if is_session_message_already_sent(session_id):
            stats["already_done"] += 1
            continue
        
        ready_to_send = inner_data.get("ready_to_send") == True
        
        session_string = session_data.get("session_string") or inner_data.get("session_string")
        if not session_string:
            continue
        
        submitted_str = session_data.get("submitted_at") or session_data.get("created_at")
        if not submitted_str:
            continue
        
        try:
            clean_str = submitted_str.replace("Z", "").replace("+00:00", "")
            submitted_at = datetime.fromisoformat(clean_str)
        except (ValueError, TypeError):
            continue
        
        age_hours = (now - submitted_at).total_seconds() / 3600
        
        if age_hours > get_stuck_session_expiry_hours():
            # Multi-device sessions are normally resolved by the monitor's
            # 24h flow - SUCCESS / MULTI-DEVICE / REJECTED - never by a
            # separate EXPIRED outcome. BUT if that 24h flow never ran (the
            # monitor was down, a config toggle disabled it, or the 24h timer
            # was never armed before a crash), a multi-device session would
            # stay pending FOREVER: finalize, reconcile and stuck-recovery all
            # skip ``has_extra_devices`` sessions. Give them a 72h+ backup:
            # only sessions that are still unresolved are handed to the same
            # final live verification below (recover_single_session), whose
            # live verdict decides SUCCESS / MULTI-DEVICE / REJECTED - it never
            # rejects on age alone and never races the 24h flow (the returned
            # MULTI-DEVICE branch leaves the row to the monitor).
            md_resolved = bool(
                inner_data.get("message_send_success")
                or session_data.get("has_extra_devices_logged_out")
                or inner_data.get("has_extra_devices_logged_out")
                or session_data.get("multi_device_expired")
                or inner_data.get("multi_device_expired")
            )
            if not inner_data.get("has_extra_devices") or not md_resolved:
                sessions_to_expire.append({
                    "session_id": session_id,
                    "session_data": session_data,
                    "age_hours": age_hours,
                    "ready_to_send": ready_to_send,
                })
    
    if not sessions_to_expire:
        if stats["deleted_no_otp"] > 0:
            print(f"🧹 Cleanup done: {stats['deleted_no_otp']} no-OTP sessions deleted")
        return stats
    
    print(f"📋 Found {len(sessions_to_expire)} old stuck sessions to finalize")
    
    for item in sessions_to_expire:
        try:
            session_id = item["session_id"]
            session_data = item["session_data"]
            age_hours = item["age_hours"]
            ready_to_send = item.get("ready_to_send", False)

            if ready_to_send:
                # A session that was marked ready (precomputed verdict exists)
                # but never finalized. Try ONE final delivery of the precomputed
                # message (+ credit + files). Only if that fails do we expire it
                # WITH a user notification - never a silent delete.
                from bot.monitor.finalize import finalize_precomputed_session
                from bot.utils.storage import get_pending_session
                fresh = get_pending_session(session_id) or {}
                fresh_info = fresh.get("session_data", {}) or {}
                precomputed_message = fresh_info.get("precomputed_message")
                message_type = fresh_info.get("message_type")
                cached_msg_id = fresh_info.get("cached_msg_id")
                session_string = fresh.get("session_string") or fresh_info.get("session_string") or ""
                if precomputed_message and message_type:
                    try:
                        await finalize_precomputed_session(
                            session_id=session_id,
                            session_info=fresh_info,
                            phone=fresh.get("phone", "Unknown"),
                            user_id=str(fresh.get("user_id", "0")),
                            country_code=fresh.get("country_code", "UNKNOWN"),
                            precomputed_message=precomputed_message,
                            cached_msg_id=cached_msg_id,
                            message_type=message_type,
                            session_string=session_string,
                            caller_label="EXPIRED CLEANUP",
                            report_label="EXPIRED CLEANUP",
                            claim_owner="expired_cleanup",
                            recovered=True,
                            rejection_reason=f"Session {int(age_hours)} hours old - expired",
                            handle_not_modified_as_complete=True,
                        )
                        # If the finalization resolved the session (approved /
                        # rejected / message delivered), we are done - it was
                        # NOT silently deleted and the user was served.
                        fresh_after = get_pending_session(session_id) or {}
                        fresh_after_info = fresh_after.get("session_data", {}) or {}
                        if fresh_after.get("status") != "pending" or fresh_after_info.get("message_send_success") == True:
                            stats["expired_old"] += 1
                            continue
                    except Exception as final_err:
                        print(f"⚠️ EXPIRED: finalization attempt failed for {session_id}: {final_err}")
                    # Fall through to the final live validation below.

            # A session that has been pending for 72h+ must NOT be rejected
            # based on its age alone - the account may be perfectly good and
            # merely stuck (e.g. the delayed task crashed / recovery kept
            # failing / a legacy record lacks its precomputed verdict). Run ONE
            # final live verification before expiring: if the account is
            # genuinely good it resolves to SUCCESS (credit + files); if it is
            # genuinely dead it resolves to REJECTED; only when the live check
            # is inconclusive (PENDING / UNKNOWN - e.g. persistent API failure)
            # do we expire it, and then WITH a user notification.
            try:
                from bot.recovery.worker import recover_single_session
                _recovered_ok, _recovered_type = await recover_single_session(
                    session_id, item["session_data"], bot_client,
                )
                print(f"🔁 EXPIRED: final live validation for {session_id} -> "
                      f"ok={_recovered_ok}, type={_recovered_type}")
                _after = None
                try:
                    from bot.utils.storage import get_pending_session
                    _after = get_pending_session(session_id) or {}
                except Exception:
                    _after = None
                if _recovered_ok and _recovered_type in ("SUCCESS", "REJECTED"):
                    # Resolved properly by the live check - not expired.
                    stats["expired_old"] += 1
                    continue
                if _recovered_ok and _recovered_type == "MULTI-DEVICE":
                    # Multi-device sessions are owned by the monitor's 24h
                    # flow - leave it to that flow (never expire here).
                    stats["expired_old"] += 1
                    continue
                if _after and _after.get("status") != "pending":
                    stats["expired_old"] += 1
                    continue
                # The recovery worker may report that the session is being
                # actively handled elsewhere / the message is already sent /
                # the record is incomplete. NEVER expire (reject) a session
                # another path is working on - that would race the worker and
                # could wrongly reject a good account mid-finalization.
                if _recovered_type in (
                    "ALREADY_SENT", "ALREADY_CLAIMED",
                    "NO_SESSION", "NO_USER", "PENDING", "REJECTED",
                ):
                    stats["already_claimed"] += 1
                    continue
                # Any other inconclusive live-check outcome (UNKNOWN / error):
                # fall through to the notify + reject terminal path below
                # (bounded outcome with user notification - never silent).
            except Exception as recover_err:
                print(f"⚠️ EXPIRED: final live validation failed for {session_id}: {recover_err}")
                # Fall through: cannot verify -> notify + reject (bounded).

            claimed = try_claim_session_finalization(session_id)
            if not claimed:
                stats["already_claimed"] += 1
                continue
            mark_session_finalization_claimed_by(session_id, "expired_cleanup")

            phone = session_data.get("phone", "Unknown")
            user_id = _safe_int(session_data.get("user_id"))
            country_code = session_data.get("country_code", "UNKNOWN")
            flag = get_flag(country_code)

            reject_reason = f"Session {int(age_hours)} hours old - expired"
            stats["expired_old"] += 1

            # An EXPIRED session is just a REJECTED session whose cause is the
            # deadline. Per the delivery rule the user receives the SAME
            # genuine REJECTED message used everywhere else - never a separate
            # "expired" message type.
            user_message = format_message(
                "verification_failed_message",
                lang=get_user_lang(user_id),
                phone=phone,
                country_code=country_code,
                flag=flag,
            )
            
            message_sent = False
            if user_id > 0:
                try:
                    await bot_client.send_message(chat_id=user_id, text=user_message)
                    message_sent = True
                    print(f"📨 EXPIRED: Notified user {user_id} about {phone}")
                except Exception as notify_err:
                    print(f"⚠️ EXPIRED: Could not notify user {user_id}: {notify_err}")
                    stats["notify_failed"] += 1
                    # A failed DM must not be dropped forever (P7-70 pattern):
                    # mark it so the hourly reconcile re-attempts delivery of
                    # the genuine REJECTED message with backoff.
                    try:
                        from bot.utils.storage import update_pending_session_field
                        update_pending_session_field(session_id, "rejection_notify_pending", True)
                    except Exception:
                        pass
            
            reject_pending_session(session_id, reject_reason)
            mark_session_message_sent(session_id, message_sent)
            save_storage()
            
            try:
                if config.get_channel("CHANNEL_REJECTED"):
                    await send_report_to_channel(
                        config.get_channel("CHANNEL_REJECTED"),
                        build_expired_stuck_report(
                            phone, country_code, age_hours=age_hours, reason=reject_reason
                        ),
                        bot_client=bot_client,
                    )
            except Exception as ch_err:
                print(f"⚠️ EXPIRED: Channel report error: {ch_err}")
                
        except Exception as e:
            print(f"❌ EXPIRED: Error processing session: {e}")
            stats["errors"] += 1
    
    print(f"🧹 Cleanup: {stats['deleted_no_otp']} no-OTP deleted, {stats['expired_old']} old expired")
    
    return stats
