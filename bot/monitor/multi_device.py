"""Multi-device 24h (+grace) verification window logic."""
import logging
from datetime import datetime
from typing import Any, Dict, List, Tuple

from bot import config
from bot.bootstrap.client import app
from bot.data.proxies import record_proxy_session_outcome
# MODULE-level on purpose: a function-scoped ``from ... import X`` makes X local
# for the WHOLE function under CPython 3.12 (LOAD_FAST_CHECK), so any branch
# reached before the import raises UnboundLocalError - the exact bug class that
# broke md_logout_attempts in auto_logout.py.
from bot.utils.storage import get_pending_session, set_pending_session_field

logger = logging.getLogger(__name__)

# How many times the terminal multi-device rejection may be re-issued after a
# crash before we stop trying. handle_rejection rejects the row as its FIRST
# action, so one healthy attempt always settles it; this only bounds a pathology.
_MAX_EXPIRY_REISSUES = 5


def _coerce_msg_id(value: Any) -> int:
    """Coerce a persisted message id to int.

    Message ids come back from JSON/SQLite as ``str`` often enough that a bare
    ``value > 0`` comparison raises ``TypeError`` (str vs int) and silently
    aborted the whole notification block. 0 means "no usable id".
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_iso(value: Any):
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "").replace("+00:00", "")
        ).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _max_counter(field: str, *sources: Any) -> int:
    """Highest int value of ``field`` across the given dicts (0 on garbage)."""
    used = 0
    for source in sources:
        try:
            used = max(used, int((source or {}).get(field, 0) or 0))
        except (TypeError, ValueError):
            continue
    return used


def _deferral_blocks(
    session_data: Dict[str, Any],
    session_info: Dict[str, Any],
    current_time: datetime,
) -> bool:
    """True while an auto-logout deferral must still be honoured.

    A deferral is a TIMING condition (Telegram's own 24h fresh-authorization
    rule, a long FloodWait), never a failure - so it consumes no retry budget
    and must never reject the account. It is bounded twice over:

    * ``auto_logout._defer`` refuses to record more than
      ``MULTI_DEVICE_MAX_DEFERRALS`` of them and clears the gate when the budget
      is spent, and
    * the same budget is re-checked HERE, independently, so even a stale
      ``md_logout_not_before`` left behind inside the nested session_data blob
      (which nothing clears) cannot make a session skip forever. Deferring
      without a bound meant a session was never rejected AND never credited.
    """
    from bot.recovery.constants import MULTI_DEVICE_MAX_DEFERRALS

    not_before = _parse_iso(
        session_data.get("md_logout_not_before")
        or session_info.get("md_logout_not_before")
    )
    if not not_before or current_time >= not_before:
        return False
    if _max_counter("md_defer_count", session_data, session_info) >= MULTI_DEVICE_MAX_DEFERRALS:
        logger.error(
            f"❌ {session_data.get('phone', 'Unknown')}: deferral budget spent - "
            f"ignoring md_logout_not_before so the bounded retry/reject chain runs"
        )
        return False
    return True


def _needs_expiry_rejection(session_id: str, session_data: Dict[str, Any]) -> bool:
    """True when the terminal multi-device rejection still has to be issued.

    ``multi_device_expired`` used to be persisted BEFORE ``handle_rejection``
    ran, guarded by ``if not multi_device_expired``. Anything that threw between
    those two lines - or the board being killed - left the flag set with the row
    still ``pending``, and the session became permanently invisible to this rule.
    No other scanner picks it up either: cleanup_old_stuck_sessions,
    finalize.py's 72h expiry and reconcile.py all skip ``has_extra_devices``
    sessions. So the flag is now written AFTER the rejection lands, and a row
    that is still pending re-issues it - the crash window self-heals.
    ``handle_rejection`` calls ``reject_pending_session`` as its FIRST action, so
    a re-issue can never double-notify a user who was already told.
    """
    info = session_data.get("session_data") or {}
    if not (session_data.get("multi_device_expired") or
            (isinstance(info, dict) and info.get("multi_device_expired"))):
        return True
    try:
        latest = get_pending_session(session_id) or {}
    except Exception as err:
        logger.warning(f"⚠️ {session_id}: expiry re-check failed: {err}")
        return False
    if latest.get("status", "pending") != "pending":
        return False
    reissues = _max_counter("md_expiry_reissues", session_data, latest)
    if reissues >= _MAX_EXPIRY_REISSUES:
        logger.error(
            f"❌ {session_id}: multi-device rejection failed to land "
            f"{reissues} times - giving up so the loop cannot spin"
        )
        return False
    logger.error(
        f"🛠️ {session_id}: multi_device_expired is set but the row is still "
        f"pending - the previous rejection never landed; re-issuing it"
    )
    session_data["md_expiry_reissues"] = reissues + 1
    try:
        set_pending_session_field(session_id, "md_expiry_reissues", reissues + 1)
    except Exception:
        pass
    return True


def _mark_expired(session_id: str, session_data: Dict[str, Any]) -> None:
    """Persist ``multi_device_expired`` AFTER the rejection actually landed."""
    session_data["multi_device_expired"] = True
    try:
        set_pending_session_field(session_id, "multi_device_expired", True)
    except Exception as err:
        logger.warning(f"⚠️ {session_id}: could not persist multi_device_expired: {err}")



async def evaluate_multi_device_window(
    session_id: str,
    session_data: Dict[str, Any],
    session_info: Dict[str, Any],
    current_time: datetime
) -> Tuple[bool, bool]:
    """Evaluate the multi-device 24h window for a pending session.

    Multi-device sessions must be fully verified & balance credited within
    24h + grace. The exact-24h countdown is anchored to the account receive
    time (``submitted_at``). If the window passes without verification the
    session resolves to the standard REJECTED outcome - there is no separate
    EXPIRED outcome.

    Returns ``(md_ready_for_24h, skip)`` where ``skip=True`` means the caller
    must ``continue`` to the next pending session.
    """
    # Exact-24h timer is executing the flow right now - let it finish.
    from bot.monitor.twenty_four_hour import is_timer_running
    if is_timer_running(session_id):
        return False, True

    md_ready_for_24h = False
    if session_info.get("has_extra_devices"):
        already_logged_out = (
            session_data.get("has_extra_devices_logged_out") or
            session_info.get("has_extra_devices_logged_out")
        )
        md_time_str = (
            session_data.get("submitted_at") or
            session_info.get("submitted_at") or
            session_data.get("created_at") or
            session_data.get("added_at") or
            session_info.get("added_at")
        )
        md_age_hours = None
        if md_time_str:
            try:
                md_added_dt = datetime.fromisoformat(
                    str(md_time_str).replace("Z", "").replace("+00:00", "")
                ).replace(tzinfo=None)
                md_age_hours = (current_time - md_added_dt).total_seconds() / 3600
            except (ValueError, TypeError):
                pass

        if md_age_hours is not None:
            from bot.utils.pending_recovery import (
                MULTI_DEVICE_24H_WINDOW_HOURS,
                MULTI_DEVICE_GRACE_MINUTES,
                MULTI_DEVICE_LOGOUT_MAX_RETRIES
            )
            md_window_hours = MULTI_DEVICE_24H_WINDOW_HOURS + (MULTI_DEVICE_GRACE_MINUTES / 60.0)

            if md_age_hours >= md_window_hours:
                # 24h + grace fully passed and the account was never verified.
                if already_logged_out:
                    # Devices already logged out - finish verify + credit
                    md_ready_for_24h = True
                else:
                    # The session itself is permanently unusable (auth key
                    # revoked / account banned): no number of retries can ever
                    # log the extra devices out, so resolve it NOW instead of
                    # burning the retry budget.
                    if (session_data.get("md_session_dead") or
                            session_info.get("md_session_dead")):
                        if _needs_expiry_rejection(session_id, session_data):
                            reason_detail = str(
                                session_data.get("md_session_dead_reason")
                                or session_info.get("md_session_dead_reason")
                                or "session no longer usable"
                            )[:120]
                            from bot.monitor.rejector import handle_rejection
                            await handle_rejection(
                                session_id,
                                session_data,
                                session_data.get("phone", "UNKNOWN"),
                                str(session_data.get("user_id", "0")),
                                session_data.get("country_code", "UNKNOWN"),
                                float(session_data.get("rate", 0) or 0),
                                f"Multi-device cleanup impossible ({reason_detail})",
                                report_label="Multi-Device",
                            )
                            # Only AFTER the rejection landed - see
                            # _needs_expiry_rejection for why the old ordering
                            # wedged the session permanently.
                            _mark_expired(session_id, session_data)
                        return md_ready_for_24h, True

                    # A previous auto-logout run deferred itself because
                    # Telegram's own 24h "fresh authorization" rule had not
                    # opened yet (or a long FloodWait was hit). That is a
                    # TIMING condition, not a failure: wait, never reject -
                    # but only within the bounded deferral budget.
                    if _deferral_blocks(session_data, session_info, current_time):
                        not_before = _parse_iso(
                            session_data.get("md_logout_not_before")
                            or session_info.get("md_logout_not_before")
                        )
                        wait_min = (not_before - current_time).total_seconds() / 60
                        print(f"⏸️ {session_data.get('phone', 'Unknown')}: multi-device "
                              f"auto-logout deferred by Telegram for {wait_min:.1f} more "
                              f"min - waiting (no retry consumed, no rejection)")
                        return md_ready_for_24h, True

                    # Window passed but devices not logged out yet.
                    # Give auto-logout a bounded number of GENUINE-failure
                    # chances before expiring (deferrals do not count).
                    # _max_counter, not an ``or`` chain: a stored 0 at the top
                    # level used to fall through to the nested blob and a stale
                    # nested value could then under-report the real count.
                    md_logout_attempts = _max_counter(
                        "md_logout_attempts", session_data, session_info
                    )
                    if md_logout_attempts < MULTI_DEVICE_LOGOUT_MAX_RETRIES:
                        print(f"🔄 {session_data.get('phone', 'Unknown')}: window passed but auto-logout retries left ({md_logout_attempts}/{MULTI_DEVICE_LOGOUT_MAX_RETRIES}) - attempting logout")
                        md_ready_for_24h = True
                    else:
                        # The 24h window is over and the account could not be
                        # cleaned up/verified - resolve to the standard REJECTED
                        # outcome (no EXPIRED outcome exists).
                        if _needs_expiry_rejection(session_id, session_data):
                            from bot.monitor.rejector import handle_rejection
                            await handle_rejection(
                                session_id,
                                session_data,
                                session_data.get("phone", "UNKNOWN"),
                                str(session_data.get("user_id", "0")),
                                session_data.get("country_code", "UNKNOWN"),
                                float(session_data.get("rate", 0) or 0),
                                "Multi-device 24h verification window missed",
                                report_label="Multi-Device",
                            )
                            _mark_expired(session_id, session_data)
                        return md_ready_for_24h, True
            elif md_age_hours < MULTI_DEVICE_24H_WINDOW_HOURS and not already_logged_out:
                # Still inside the 24h hold - message already sent, no balance yet
                if session_info.get("message_send_success") == True:
                    return md_ready_for_24h, True
                # Message not delivered yet: fall through so it can be sent
            else:
                # 24h hold reached (within grace) or devices already logged out.
                # An outstanding deferral must still be honoured here: this
                # branch used to hand the session straight to run_auto_logout,
                # which then slept inside the SINGLE monitor loop waiting for
                # Telegram's fresh-authorization window to open - stalling
                # stuck-recovery and every other pending session with it.
                if not already_logged_out and _deferral_blocks(
                    session_data, session_info, current_time
                ):
                    not_before = _parse_iso(
                        session_data.get("md_logout_not_before")
                        or session_info.get("md_logout_not_before")
                    )
                    wait_min = (not_before - current_time).total_seconds() / 60
                    print(f"⏸️ {session_data.get('phone', 'Unknown')}: inside grace but "
                          f"auto-logout deferred by Telegram for {wait_min:.1f} more min "
                          f"- waiting (no retry consumed, no rejection)")
                    return md_ready_for_24h, True
                md_ready_for_24h = True

    return md_ready_for_24h, False


async def send_multi_device_report(
    session_id: str,
    session_data: Dict[str, Any],
    extra_devices_list: List[dict],
    device_count: int,
    phone: str,
    user_id: str,
    rate: float,
    country_code: str,
    hours_elapsed: float
) -> None:
    """Show the multi-device message to the user (24h hold, no balance).

    Mutates ``session_data["session_data"]`` and persists it, exactly as the
    original inline block did.
    """
    from bot.utils.i18n import get_user_lang
    from bot.utils.storage import format_message, replace_pending_session_data, get_user_info
    from bot.services.reports import build_multi_device_report

    try:
        _inner_px = session_data.get("session_data") or {}
        _px_used = session_data.get("proxy_used") or _inner_px.get("proxy_used") or ""
        _px_attr = session_data.get("proxy_attribution") or _inner_px.get("proxy_attribution") or country_code
        record_proxy_session_outcome(_px_attr, _px_used, "multi_device")
    except Exception:
        pass

    session_data["session_data"]["has_extra_devices"] = True
    session_data["session_data"]["extra_devices"] = extra_devices_list
    session_data["session_data"]["device_count"] = device_count
    session_data["session_data"]["message_type"] = "MULTI-DEVICE"
    session_data["session_data"]["is_valid"] = True

    # The exact-24h countdown is anchored to the account receive time
    # (submitted_at); ``added_at`` is only kept as a legacy diagnostic marker.
    # Preserve an existing value so a restart does not shift it.
    if not (session_data.get("added_at") or session_data["session_data"].get("added_at")):
        session_data["session_data"]["added_at"] = datetime.now().isoformat()

    replace_pending_session_data(session_id, session_data["session_data"])

    try:
        report = build_multi_device_report(
            phone, country_code, device_count,
            user_id=user_id,
            rate=rate,
            hours_left=24 - hours_elapsed,
            extra_devices_list=extra_devices_list,
        )
        # Honor the admin-selected Multidevice Session Mode format (Session
        # Management -> Multidevice, max 1). The selected format's FILE is
        # retried internally - a text report is NEVER substituted for a file.
        # On failure the pending flag is persisted so the monitor safety net
        # re-delivers the file later.
        _inner = session_data.get("session_data") or {}
        if not isinstance(_inner, dict):
            _inner = {}
        session_string = session_data.get("session_string") or _inner.get("session_string") or ""
        from bot.services.session_reporting import report_pending_or_multidevice_safe
        # CRITICAL: atomically claim the right to send the multidevice channel
        # report (file). Only ONE path may deliver it - the 24h timer, the
        # monitor cycle and the delayed task can all reach this session.
        from bot.utils.storage import try_claim_channel_report, release_channel_report
        if not try_claim_channel_report(session_id):
            print(f"⏭️ Multi-device report already claimed/sent for {phone} - skipping")
            sent = None
        else:
            try:
                sent = await report_pending_or_multidevice_safe(
                    bot_client=app,
                    mode="multidevice",
                    channel=config.get_channel("CHANNEL_24HR_UP"),
                    phone=phone,
                    session_string=session_string,
                    report_text=report,
                    telethon_file=None,
                    pyrogram_file=None,
                    user_id=user_id,
                )
            finally:
                release_channel_report(session_id, sent=sent is not None)
        if sent is not None:
            _inner["channel_report_sent"] = True
            _inner["channel_report_pending"] = False
            print(f"📤 Multi-device report sent for {phone}")
        else:
            _inner["channel_report_sent"] = False
            _inner["channel_report_pending"] = True
            print(f"⚠️ Multi-device report FAILED for {phone} after retries - "
                  f"channel_report_pending=True (monitor safety net will retry)")
        session_data["session_data"] = _inner
        from bot.utils.storage import replace_pending_session_data
        replace_pending_session_data(session_id, _inner)
    except Exception as e:
        logger.warning(f"⚠️ Failed to send multi-device report: {e}")

    # ------------------------------------------------------------------
    # Notify the user. ``message_send_success`` is what arms the exact-24h
    # timer (see monitor/twenty_four_hour.py), so this block MUST end with the
    # flag set whenever the user actually has the message in front of them.
    #
    # Previously this only tried edit_message_text on a possibly-``str``
    # message id: ``prev_msg_id > 0`` raised TypeError (str vs int) and the
    # whole block was swallowed, and a stale/deleted message id meant the user
    # was never told anything at all while the 24h timer stayed disarmed
    # forever. Now: ids are coerced, an edit that cannot land falls back to a
    # fresh send, and the new id is persisted so later edits target it.
    # ------------------------------------------------------------------
    inner = session_data.get("session_data") or {}
    if not isinstance(inner, dict):
        inner = {}
        session_data["session_data"] = inner

    try:
        user_notification = format_message(
            "multi_device_message",
            lang=get_user_lang(int(user_id)),
            phone=phone
        )
    except Exception as e:
        logger.error(f"❌ Multi-device message could not be rendered: {e}")
        user_notification = (
            f"⏳ {phone}\n\nYour account has other active devices. "
            f"They will be logged out automatically after 24 hours, "
            f"then your balance will be credited."
        )

    prev_msg_id = _coerce_msg_id(
        inner.get("user_message_id")
        or inner.get("session_specific_message_id")
        or inner.get("cached_msg_id")
    )
    if prev_msg_id <= 0:
        try:
            user_info_obj = get_user_info(user_id)
            prev_msg_id = _coerce_msg_id(
                (user_info_obj or {}).get("pending_message_id")
            )
        except Exception:
            prev_msg_id = 0

    delivered = False
    if prev_msg_id > 0:
        try:
            await app.edit_message_text(
                chat_id=int(user_id),
                message_id=prev_msg_id,
                text=user_notification
            )
            delivered = True
            print("✅ Multi-device message sent to user (edited)")
        except Exception as e:
            # The message already carries this exact text -> the user IS
            # informed; that is a success, not a failure.
            if "MESSAGE_NOT_MODIFIED" in str(e).upper():
                delivered = True
                print("✅ Multi-device message already present for user")
            else:
                logger.warning(
                    f"⚠️ Multi-device edit failed for {phone} "
                    f"(msg {prev_msg_id}): {e} - sending a fresh message"
                )

    if not delivered:
        try:
            new_msg = await app.send_message(
                chat_id=int(user_id),
                text=user_notification
            )
            new_id = _coerce_msg_id(getattr(new_msg, "id", 0))
            if new_id > 0:
                inner["user_message_id"] = new_id
                inner["cached_msg_id"] = new_id
            delivered = True
            print("✅ Multi-device message sent to user (new message)")
        except Exception as e:
            logger.error(f"❌ Multi-device notification failed for {phone}: {e}")

    if delivered:
        inner["message_send_success"] = True
    else:
        # Leave the flag falsy so the monitor safety net retries next cycle.
        inner["message_send_success"] = False

    session_data["session_data"] = inner
    try:
        replace_pending_session_data(session_id, inner)
    except Exception as e:
        logger.error(f"❌ Failed to persist multi-device notify state: {e}")
