"""Crash-window reconciliation for sessions left in an inconsistent state.

A hard shutdown / restart can interrupt a session between two side effects
(message delivered vs balance credited vs files reported). Those windows are
tiny but real, and they previously left the session stuck in ``pending``
forever (the monitor, recovery scan, stuck recovery and expiry cleanup all
skip ``message_send_success=True`` sessions) before being silently deleted.

This module reconciles such orphaned sessions. It is idempotent and strictly
conservative:

- A session is NEVER credited unless its persisted verdict is genuinely
  ``SUCCESS`` (live-checked at decision time) AND it is still unresolved.
- ``atomic_credit_balance_for_session`` itself refuses to credit a session
  whose status is ``rejected`` / ``expired``, so no bad account can ever be
  credited by this path.
- A session is NEVER rejected here based on a guess; the only rejection done
  is completing a verdict that was already persisted as ``REJECTED``.

Orphan classes handled:

A) ``message_type=SUCCESS`` + outcome already COMMITTED (``message_send_success``
   OR ``status=approved``) + balance not credited
   -> the outcome was promised to the user / persisted but the balance credit
      never ran (crash between "mark sent"/"approve" and "credit", or an
      approve-before-credit ordering bug). Credit it now (idempotent) and mark
      the channel report pending so the safety net delivers files.

B) ``message_send_success=True`` + ``message_type`` REJECTED + status pending
   -> the rejection message was delivered but the status never flipped.
      Flip it to rejected and (re)send the rejection channel report.

C) balance credited / status approved + ``message_type=SUCCESS`` +
   ``message_send_success != True``
   -> balance was credited but the user message / files never went out.
      Deliver the user message and mark the channel report pending.

A and C are evaluated in SEQUENCE, not as alternatives: one row can be both
uncredited and undelivered, and treating them as mutually exclusive left
``status=approved`` + ``message_send_success=True`` + no credit permanently
unrepairable (the user was never paid).

D) ``message_send_success=True`` + no persisted verdict + status pending
   (legacy sessions created before verdict persistence was hardened)
   -> outcome unknown. Reset the sent marker so the monitor re-verifies the
      session with a fresh live check (never guessed). The account's real
      state decides the outcome.
"""
import logging
from datetime import datetime
from typing import Dict

from bot.utils.storage import (
    _deadline_is_in_future,
    _is_message_not_modified,
    get_all_pending_sessions,
    get_pending_session,
    get_user_info,
    atomic_credit_balance_for_session,
    reject_pending_session,
    mark_session_message_sent,
    update_pending_session_field,
    replace_pending_session_data,
    format_message,
)
from bot.utils.i18n import get_user_lang, t
from bot.utils.session_handler import get_country_rate
from bot.utils.country_data import get_flag

logger = logging.getLogger(__name__)


def _resolve_rate(session_data: dict, info: dict, country_code: str, user_id: str) -> float:
    """Return the correct rate for a SUCCESS settlement, stored-rate fallback."""
    try:
        spam_status = (
            info.get("spam_status")
            or info.get("account_status")
            or session_data.get("spam_status")
            or session_data.get("account_status")
            or "limit"
        )
        return float(get_country_rate(country_code, spam_status, user_id=user_id) or 0)
    except Exception:
        pass
    try:
        return float(session_data.get("rate", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _md_window_passed(session_data: dict, info: dict) -> bool:
    """True when the multi-device settlement window + grace has elapsed.

    MEDIUM-41: the window used to be hardcoded (24h + 20min grace). It now
    follows the admin's Session Expiry setting (same anchor the deadline
    timer uses) plus a fixed 20-minute grace, so changing the expiry setting
    moves the recovery window with it."""
    anchor = (
        session_data.get("added_at")
        or info.get("added_at")
        or session_data.get("submitted_at")
        or info.get("submitted_at")
        or session_data.get("created_at")
        or ""
    )
    if not anchor:
        return False
    try:
        anchor_dt = datetime.fromisoformat(str(anchor).replace("Z", "").replace("+00:00", ""))
    except (ValueError, TypeError):
        return False
    try:
        from bot.data.settings import get_session_expiry_hours
        window_hours = max(1, int(get_session_expiry_hours() or 24))
    except Exception:
        window_hours = 24
    hours = (datetime.now() - anchor_dt).total_seconds() / 3600
    return hours >= window_hours + (20 / 60.0)


async def _deliver_user_message(
    session_id: str,
    info: dict,
    phone: str,
    user_id: str,
    country_code: str,
    rate: float,
) -> bool:
    """Deliver the SUCCESS message to the user (edit original or send new)."""
    from bot.bootstrap.client import app

    lang = get_user_lang(int(user_id))
    spam_status = info.get("spam_status", "limit") or "limit"
    status_display = {
        "spam_free": t("status_spam_free", lang),
        "spam": t("status_spam", lang),
        "limit": t("status_limit", lang),
    }.get(spam_status, t("status_spam_free", lang))
    message = format_message(
        "success_message",
        lang,
        phone=phone,
        price=f"{rate:.2f}",
        status=status_display,
    )

    cached_msg_id = (
        info.get("user_message_id")
        or info.get("session_specific_message_id")
        or info.get("cached_msg_id")
    )
    if not cached_msg_id:
        try:
            user_info = get_user_info(user_id)
            cached_msg_id = user_info.get("pending_message_id") if user_info else None
        except Exception:
            cached_msg_id = None

    try:
        if cached_msg_id and int(cached_msg_id) > 0:
            try:
                await app.edit_message_text(
                    chat_id=int(user_id),
                    message_id=int(cached_msg_id),
                    text=message,
                )
                return True
            except Exception as edit_err:
                if _is_message_not_modified(edit_err):
                    # The original message already carries the exact success
                    # text - that IS the delivery. Sending a NEW message now
                    # would duplicate it.
                    return True
        await app.send_message(chat_id=int(user_id), text=message)
        return True
    except Exception as send_err:
        logger.warning(f"⚠️ RECONCILE: could not deliver success message for {session_id}: {send_err}")
        return False


async def reconcile_orphaned_sessions(bot_client=None) -> Dict[str, int]:
    """Resolve sessions left inconsistent by a crash/restart. Idempotent.

    Returns a stats dict. Safe to run repeatedly (startup + hourly).
    """
    stats = {
        "checked": 0,
        "credited": 0,
        "rejected": 0,
        "message_sent": 0,
        "marked_pending": 0,
        "reset_for_recheck": 0,
        "skipped": 0,
        "errors": 0,
    }

    try:
        all_pending = get_all_pending_sessions()
    except Exception as e:
        logger.warning(f"⚠️ RECONCILE: could not read pending sessions: {e}")
        return stats

    for session_id, session_data in list(all_pending.items()):
        if not isinstance(session_data, dict):
            continue
        try:
            stats["checked"] += 1

            status = session_data.get("status", "pending")
            info = session_data.get("session_data") or {}
            if not isinstance(info, dict):
                info = {}

            # H1 FIX: resolve the row's OWN identity ONCE, before any branch
            # that needs it. The rejected-notify retry below previously read
            # ``user_id`` / ``phone`` / ``country_code`` locals that were only
            # assigned further down (in the pending/approved branch): the
            # first pass raised NameError (swallowed) and every LATER pass
            # reused values leaked from the PREVIOUS loop iteration - editing
            # session X's rejection message into session Y's chat and then
            # marking X's row delivered. Row-local identity now always exists.
            phone = session_data.get("phone", "UNKNOWN")
            user_id = str(session_data.get("user_id", "0") or "0")
            country_code = session_data.get("country_code", "UNKNOWN")
            if phone is None:
                phone = "UNKNOWN"

            # Multi-device sessions are owned by the 24h flow - never touch
            # their credit/status here. BUT a crash between the user message
            # and the channel file report would leave the multidevice FILE
            # permanently undelivered (the 24h flow only re-reports at the 24h
            # mark; the safety net needs channel_report_pending=True to act).
            # Hand that one gap to the monitor's per-cycle safety net without
            # touching anything the 24h flow owns.
            if info.get("has_extra_devices") or session_data.get("has_extra_devices"):
                if (info.get("message_send_success") is True
                        and info.get("channel_report_sent") is not True):
                    try:
                        update_pending_session_field(session_id, "channel_report_pending", True)
                        stats["marked_pending"] += 1
                        print(f"🔁 RECONCILE: {session_id} multi-device file report gap - "
                              f"marked channel_report_pending for the safety net")
                    except Exception as md_gap_err:
                        logger.warning(f"⚠️ RECONCILE: could not mark multi-device report "
                                       f"pending for {session_id}: {md_gap_err}")
                # A multi-device session past its 24h+grace window that is still
                # unresolved (message delivered, no balance, no logged-out /
                # expired markers) must NOT be parked forever: reset the sent
                # marker + precomputed fields (Orphan D pattern) so the monitor
                # re-checks it with a fresh live verdict.
                if (info.get("message_send_success") is True
                        and _md_window_passed(session_data, info)
                        and not (session_data.get("balance_credited")
                                 or session_data.get("has_extra_devices_logged_out")
                                 or info.get("has_extra_devices_logged_out")
                                 or session_data.get("multi_device_expired")
                                 or info.get("multi_device_expired"))):
                    update_pending_session_field(session_id, "message_send_success", False)
                    update_pending_session_field(session_id, "message_sent_at", None)
                    update_pending_session_field(session_id, "finalization_claimed", False)
                    update_pending_session_field(session_id, "ready_to_send", False)
                    update_pending_session_field(session_id, "precomputed_message", None)
                    update_pending_session_field(session_id, "message_type", None)
                    try:
                        from bot.utils.storage import set_pending_session_field
                        set_pending_session_field(session_id, "currently_processing", False)
                    except Exception:
                        pass
                    stats["reset_for_recheck"] += 1
                    print(f"🔁 RECONCILE: {session_id} multi-device past 24h+grace window "
                          f"and unresolved - reset for live re-check")
                stats["skipped"] += 1
                continue

            # Only unresolved/approved sessions are candidates.
            if status not in ("pending", "approved"):
                # P7-70: a REJECTED row whose user notification failed carries
                # ``rejection_notify_pending`` - retry the delivery here
                # (hourly, with the marker cleared on success so it never
                # loops forever).
                if (status == "rejected"
                        and (session_data.get("rejection_notify_pending") is True
                             or info.get("rejection_notify_pending") is True)):
                    try:
                        from bot.utils.i18n import get_user_lang
                        from bot.utils.country_data import get_flag
                        lang = get_user_lang(int(user_id)) if str(user_id).isdigit() else "en"
                        rejection_msg = format_message(
                            "verification_failed_message",
                            lang,
                            phone=phone,
                            country_code=country_code,
                            flag=get_flag(country_code),
                        )
                        cached_msg_id = (
                            info.get("user_message_id")
                            or info.get("session_specific_message_id")
                            or info.get("cached_msg_id")
                        )
                        try:
                            cached_msg_id = int(cached_msg_id or 0)
                        except (TypeError, ValueError):
                            cached_msg_id = 0
                        from bot.bootstrap.client import app as _recon_app
                        if cached_msg_id > 0:
                            try:
                                await _recon_app.edit_message_text(
                                    chat_id=int(user_id), message_id=cached_msg_id, text=rejection_msg)
                                delivered = True
                            except Exception as edit_err:
                                if _is_message_not_modified(edit_err):
                                    delivered = True
                                else:
                                    try:
                                        await _recon_app.send_message(chat_id=int(user_id), text=rejection_msg)
                                        delivered = True
                                    except Exception:
                                        delivered = False
                        else:
                            try:
                                await _recon_app.send_message(chat_id=int(user_id), text=rejection_msg)
                                delivered = True
                            except Exception:
                                delivered = False
                        if delivered:
                            update_pending_session_field(session_id, "rejection_notify_pending", False)
                            mark_session_message_sent(session_id, True)
                            stats["message_sent"] += 1
                            print(f"✅ RECONCILE: rejected-session notification delivered for {phone}")
                        else:
                            stats["skipped"] += 1
                    except Exception as notify_err:
                        logger.warning(f"⚠️ RECONCILE: rejected-notify retry failed for {session_id}: {notify_err}")
                else:
                    stats["skipped"] += 1
                continue

            message_type = info.get("message_type") or ""
            message_sent = info.get("message_send_success") is True
            balance_credited = session_data.get("balance_credited") is True

            # (phone / user_id / country_code are resolved at the top of the
            # iteration - see the H1 note above.)

            if message_type == "SUCCESS":
                # A SUCCESS verdict is "settled" once the bot has made an
                # outward commitment: either the user was told SUCCESS
                # (message_send_success) or the row was flipped to
                # ``approved``. Both are irreversible promises, so the credit
                # MUST exist. The two orphan classes below are checked in
                # sequence (not as an if/elif): a row can be BOTH uncredited
                # AND undelivered, and the old mutually-exclusive shape let
                # `status == "approved" + message_sent` fall through to
                # ``skipped`` with the money never credited - permanently.
                settled = bool(message_sent or status == "approved")
                handled = False

                if settled and not balance_credited:
                    # ---- Orphan A: outcome committed, credit never ran ----
                    rate = _resolve_rate(session_data, info, country_code, user_id)
                    if rate < 0:
                        # P0-5: a NEGATIVE resolved rate is corrupt config -
                        # refuse loudly and keep the row repairable.
                        logger.critical(
                            f"🚨 RECONCILE: {session_id} ({phone}) has a committed SUCCESS "
                            f"outcome but the resolved rate is negative (country={country_code}, "
                            f"stored={session_data.get('rate')}) - NOT crediting. "
                            f"Fix the country rate config; the row stays repairable."
                        )
                        stats["errors"] += 1
                        handled = False
                    else:
                        # MEDIUM-42: a resolved 0.0 is an EXPLICIT operator
                        # choice (free country at $0, or Accept Restricted OFF
                        # for a limited account). The old `rate <= 0` refusal
                        # wedged such rows for 72h+ with repeated CRITICAL
                        # logs. Credit 0 (no money moves) and approve so the
                        # row settles like every other path.
                        credited = False
                        try:
                            credited = atomic_credit_balance_for_session(session_id, int(user_id), rate)
                        except Exception as credit_err:
                            logger.warning(f"⚠️ RECONCILE: credit error for {session_id}: {credit_err}")
                        if credited:
                            handled = True
                            balance_credited = True
                            status = "approved"
                            stats["credited"] += 1
                            if rate == 0:
                                print(f"✅ RECONCILE: settled {phone} at $0 rate "
                                      f"(explicit operator config) - approved without payment")
                            else:
                                print(f"✅ RECONCILE: credited balance for {phone} "
                                      f"(outcome was committed, credit was interrupted)")
                            # Files were never delivered (crash happened before the
                            # report block) - hand them to the safety net.
                            if info.get("channel_report_sent") is not True:
                                update_pending_session_field(session_id, "channel_report_pending", True)
                                stats["marked_pending"] += 1
                        else:
                            handled = False
                            stats["errors"] += 1

                if (balance_credited or status == "approved") and (
                    not message_sent or info.get("reconcile_message_pending") is True
                ):
                    # ---- Orphan C: balance credited but user message/files missing ----
                    handled = True
                    rate = _resolve_rate(session_data, info, country_code, user_id)
                    delivered = await _deliver_user_message(
                        session_id, info, phone, user_id, country_code, rate,
                    )
                    # Files must NEVER be lost: the monitor safety net only
                    # retries session files when message_send_success=True, so
                    # always mark it once the outcome (balance) is final.
                    mark_session_message_sent(session_id, True)
                    if delivered:
                        try:
                            update_pending_session_field(session_id, "reconcile_message_pending", False)
                        except Exception as _clear_err:
                            pass
                        stats["message_sent"] += 1
                        print(f"✅ RECONCILE: success message delivered for {phone}")
                    else:
                        # Message delivery failed now (transient / user unreachable).
                        # Keep a separate marker so the NEXT reconciliation cycle
                        # re-attempts the message while the files are already
                        # guaranteed via the safety net.
                        try:
                            update_pending_session_field(session_id, "reconcile_message_pending", True)
                        except Exception as _mark_err:
                            pass
                        print(f"⚠️ RECONCILE: success message delivery failed for {phone} "
                              f"- files safe via safety net, message will be retried")
                    # Files: hand to the safety net if they never arrived
                    # (it is idempotent and skips already-delivered reports).
                    if info.get("channel_report_sent") is not True:
                        update_pending_session_field(session_id, "channel_report_pending", True)
                        stats["marked_pending"] += 1

                if not handled:
                    stats["skipped"] += 1

            elif message_type in ("REJECTED", "REJECTION"):
                if status == "pending":
                    # ---- Orphan B: REJECTED verdict persisted but status never
                    # flipped (crash after persist-before-send, or after send
                    # but before reject). Deliver the rejection message if it
                    # was not yet sent, then flip to rejected + report. This is
                    # idempotent and safe: reject_pending_session does not
                    # depend on the finalization claim, so even a claim still
                    # held by a crashed process cannot block the resolution.
                    # A REJECTED verdict was persisted only after a live check
                    # found the account dead, so flipping is never a guess.
                    # MEDIUM-38 RACE GUARD: if a LIVE finalizer is actively
                    # working this row right now (finalization claimed and NOT
                    # stale), leave the delivery to it - the old code raced it
                    # and both paths delivered the same rejection message.
                    if not message_sent:
                        from bot.data.sessions import (
                            _channel_report_in_progress_stale,
                            _CHANNEL_REPORT_STALE_SECONDS,
                        )
                        _claimed = info.get("finalization_claimed") is True
                        _claimed_at = info.get("finalization_claimed_at")
                        _claim_stale = True
                        if _claimed and _claimed_at:
                            try:
                                _ts = datetime.fromisoformat(str(_claimed_at).replace("Z", "").replace("+00:00", ""))
                                _claim_stale = (datetime.now() - _ts).total_seconds() >= _CHANNEL_REPORT_STALE_SECONDS
                            except (ValueError, TypeError):
                                _claim_stale = True
                        if _claimed and not _claim_stale:
                            print(f"⏳ RECONCILE: {session_id} ({phone}) finalization actively claimed by a live path - not racing it")
                            stats["skipped"] += 1
                            continue
                    if not message_sent and _deadline_is_in_future(session_data.get("deadline_at")):
                        # EARLY-DELIVERY GUARD: a REJECTED verdict persisted
                        # BEFORE its exact deadline must not surface early.
                        # Leave the row for the deadline timer, which delivers
                        # the rejection message at the right moment.
                        print(f"⏳ RECONCILE: {session_id} ({phone}) REJECTED verdict deadline not reached - leaving for deadline timer")
                        stats["skipped"] += 1
                        continue
                    if not message_sent:
                        from bot.utils.i18n import get_user_lang
                        from bot.utils.country_data import get_flag
                        lang = get_user_lang(int(user_id)) if user_id.isdigit() else "en"
                        rejection_msg = format_message(
                            "verification_failed_message",
                            lang,
                            phone=phone,
                            country_code=country_code,
                            flag=get_flag(country_code),
                        )
                        cached_msg_id = (
                            info.get("user_message_id")
                            or info.get("session_specific_message_id")
                            or info.get("cached_msg_id")
                        )
                        try:
                            from bot.bootstrap.client import app
                            if cached_msg_id and int(cached_msg_id) > 0:
                                try:
                                    await app.edit_message_text(
                                        chat_id=int(user_id),
                                        message_id=int(cached_msg_id),
                                        text=rejection_msg,
                                    )
                                except Exception as edit_err:
                                    if _is_message_not_modified(edit_err):
                                        # Already carries the rejection text -
                                        # treat as delivered (no duplicate send).
                                        pass
                                    else:
                                        await app.send_message(chat_id=int(user_id), text=rejection_msg)
                            else:
                                await app.send_message(chat_id=int(user_id), text=rejection_msg)
                            mark_session_message_sent(session_id, True)
                        except Exception as reject_msg_err:
                            logger.warning(f"⚠️ RECONCILE: could not deliver rejection message for {session_id}: {reject_msg_err}")
                    reject_pending_session(session_id, "Session invalid (reconciled)")
                    stats["rejected"] += 1
                    print(f"✅ RECONCILE: flipped {phone} to rejected (verdict was REJECTED)")
                    # MEDIUM-37: claim the rejected text report atomically so a
                    # concurrent path (worker/rejector already processing the
                    # same row) cannot post a duplicate report.
                    from bot.data.sessions import try_claim_rejected_report
                    if not try_claim_rejected_report(session_id):
                        pass  # another path owns the report - skip silently
                    else:
                        try:
                            from bot.services.reports import build_rejected_report, send_report_to_channel
                            from bot import config
                            if config.get_channel("CHANNEL_REJECTED"):
                                await send_report_to_channel(
                                    config.get_channel("CHANNEL_REJECTED"),
                                    build_rejected_report(
                                        phone, country_code, "Session invalid",
                                        report_label="RECONCILE", user_id=user_id,
                                    ),
                                    bot_client=bot_client,
                                )
                        except Exception as report_err:
                            logger.warning(f"⚠️ RECONCILE: rejection report failed for {session_id}: {report_err}")
                else:
                    stats["skipped"] += 1

            elif message_sent and status == "pending" and not balance_credited:
                # ---- Orphan D (legacy): sent marker but no verdict persisted ----
                # The outcome is unknown. Reset the marker so the monitor runs a
                # fresh live check and lets the account's real state decide.
                # A MULTI-DEVICE-typed session whose extra-device flag was lost
                # is only reset AFTER its 24h+grace window: inside the window
                # the delivered MD message is still valid and the hourly reset
                # would make the monitor re-send the MD text forever.
                _md_typed = info.get("message_type") == "MULTI-DEVICE"
                if _md_typed and not _md_window_passed(session_data, info):
                    stats["skipped"] += 1
                    continue
                update_pending_session_field(session_id, "message_send_success", False)
                update_pending_session_field(session_id, "message_sent_at", None)
                update_pending_session_field(session_id, "finalization_claimed", False)
                update_pending_session_field(session_id, "ready_to_send", False)
                update_pending_session_field(session_id, "precomputed_message", None)
                update_pending_session_field(session_id, "message_type", None)
                try:
                    from bot.utils.storage import set_pending_session_field
                    set_pending_session_field(session_id, "currently_processing", False)
                except Exception:
                    pass
                stats["reset_for_recheck"] += 1
                print(f"🔁 RECONCILE: {phone} has no persisted verdict - reset for live re-check")
            else:
                stats["skipped"] += 1

        except Exception as e:
            stats["errors"] += 1
            logger.warning(f"⚠️ RECONCILE: error for {session_id}: {e}")

    if stats["credited"] or stats["rejected"] or stats["message_sent"] or stats["reset_for_recheck"]:
        print(f"🔁 RECONCILE: checked={stats['checked']} credited={stats['credited']} "
              f"rejected={stats['rejected']} message_sent={stats['message_sent']} "
              f"marked_pending={stats['marked_pending']} reset={stats['reset_for_recheck']}")

    return stats
