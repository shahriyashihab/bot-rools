"""Stuck-session detection and timeout recovery (session never became ready).

A "stuck" session is one whose background verification task never reported a
verdict: ``ready_to_send``/``device_check_completed`` never turned true and its
max wait window (``timeout_seconds`` + 180s) has passed. Two outcomes exist:

* it still has a ``session_string``/session file - the account is verifiable, so
  a real recovery is run and the row is NEVER rejected on our own failure;
* it has nothing to verify - the user is told, a timeout report goes to the
  rejected channel, and the row is resolved as ``rejected``.

Retry policy (finding H8): both outcomes consume a BOUNDED attempt counter
(``stuck_recovery_attempts``, max ``MAX_STUCK_RECOVERY_ATTEMPTS``) spaced by
``STUCK_RECOVERY_RETRY_COOLDOWN_SECONDS``, instead of the old single
``stuck_recovery_attempted`` boolean that a single network blip burned forever.
"""
import logging
import os
from datetime import datetime
from typing import Dict

from bot import config
from bot.bootstrap.client import app
from bot.services.reports import build_timeout_report, send_report_to_channel
# Imported at MODULE level on purpose. A function-scoped ``from ... import X``
# makes X a local for the WHOLE function under CPython 3.12 (LOAD_FAST_CHECK),
# so any branch that runs before the import statement raises UnboundLocalError -
# the exact bug class that broke the multi-device counter and the OTP error
# messages elsewhere in this codebase. This file previously imported these four
# names inside two different functions; keeping them here removes the hazard.
from bot.utils.storage import (
    get_pending_session,
    get_user_info,
    reject_pending_session,
    replace_pending_session_data,
    set_pending_session_field,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# H8: stuck recovery used to be a ONE-SHOT. ``stuck_recovery_attempted`` was
# written BEFORE ``recover_single_session`` ran and nothing ever cleared it, so a
# single transient failure - a network blip in the seconds right after the board
# came back up, which is exactly when this net matters most - permanently burned
# the safety net. A perfectly good account then stayed pending forever with the
# user unpaid and no error anywhere.
#
# Both branches now consume a BOUNDED counter instead, with a cooldown between
# attempts so retries actually span a useful window.
# ---------------------------------------------------------------------------
MAX_STUCK_RECOVERY_ATTEMPTS = 5

# The monitor ticks every MONITOR_INTERVAL_SECONDS (default 60), so a bare
# counter would burn all five attempts inside five minutes - far too fast for a
# flaky network to come back. 120s spreads them over ~10 minutes.
STUCK_RECOVERY_RETRY_COOLDOWN_SECONDS = 120

# ``stuck_recovery_processing`` is set, awaited across, then cleared in a
# ``finally``. If the board is KILLED inside that window the flag survives in the
# database and the session is skipped by this net forever - the same wedge H8
# describes, reached a different way (and it is precisely the "board died while
# holding a task" flow). A processing claim older than this is treated as dead.
STUCK_RECOVERY_PROCESSING_STALE_SECONDS = 300

# P2-22: sessions with NO verifiable data anywhere (no session string and no
# session file - typically because the process was killed between OTP-accept
# and the persistence save) get an EXTRA grace window before rejection, on
# top of the normal ``timeout + 180s``. Storage reload / orphan reconcile /
# disk-file recovery need time to run; rejecting at +180s destroyed good
# accounts on data-absence. Env-tunable.
NO_SESSION_DATA_EXTRA_GRACE_SECONDS = int(
    os.getenv("STUCK_NO_DATA_EXTRA_GRACE_SECONDS", "3600") or 3600
)


def _stuck_attempts_used(session_data: Dict) -> int:
    """How many stuck-recovery attempts this session has already consumed.

    Rows written before the counter existed carry only the legacy boolean
    ``stuck_recovery_attempted``. Those are exactly the H8 victims - the flag was
    set, recovery then failed transiently, and the row is STILL pending - so the
    boolean counts as one consumed attempt and the remaining retries are granted.
    """
    try:
        used = int(session_data.get("stuck_recovery_attempts") or 0)
    except (TypeError, ValueError):
        used = 0
    if used <= 0 and session_data.get("stuck_recovery_attempted"):
        used = 1
    return max(used, 0)


def _stuck_cooldown_active(session_data: Dict, current_time: datetime) -> bool:
    """True while the cooldown after the previous attempt has not elapsed.

    A missing/corrupt timestamp means "no cooldown" - never block a retry
    because a bookkeeping field could not be parsed.
    """
    last = session_data.get("stuck_recovery_last_attempt_at")
    if not last:
        return False
    try:
        elapsed = (current_time - datetime.fromisoformat(str(last))).total_seconds()
    except (ValueError, TypeError):
        return False
    return 0 <= elapsed < STUCK_RECOVERY_RETRY_COOLDOWN_SECONDS


def _stuck_processing_stale(session_data: Dict, current_time: datetime) -> bool:
    """True when a ``stuck_recovery_processing`` claim is old enough to be dead.

    A claim with no timestamp is from a build that could not record one (or from
    a crashed run), so it is treated as stale - keeping the session reachable is
    always safer than wedging it out of every retry path.
    """
    started = session_data.get("stuck_recovery_processing_at")
    if not started:
        return True
    try:
        elapsed = (current_time - datetime.fromisoformat(str(started))).total_seconds()
    except (ValueError, TypeError):
        return True
    return elapsed >= STUCK_RECOVERY_PROCESSING_STALE_SECONDS


def _record_stuck_attempt(session_id: str, attempts_used: int, current_time: datetime) -> None:
    """Persist one consumed stuck-recovery attempt (counter + timestamp)."""
    set_pending_session_field(session_id, "stuck_recovery_attempts", attempts_used + 1)
    set_pending_session_field(
        session_id, "stuck_recovery_last_attempt_at", current_time.isoformat()
    )


async def _reject_stuck_session(
    session_id: str,
    session_data: Dict[str, dict],
    current_time: datetime,
) -> None:
    """Notify the user, send the timeout report and reject a stuck session.

    Guarantees a stuck session can never hang in pending forever: it is
    resolved (rejected) the moment its max wait window passes.
    """
    phone = session_data.get("phone", "UNKNOWN")
    user_id = session_data.get("user_id", 0)
    country_code = session_data.get("country_code", "UNKNOWN")

    try:
        from bot.utils.country_data import get_flag
        from bot.utils.i18n import get_user_lang
        from bot.utils.storage import format_message
        flag = get_flag(country_code)

        fresh = get_pending_session(session_id) or {}
        session_info = fresh.get("session_data", {})

        # A STUCK session that times out is a REJECTED session whose cause is
        # the timeout. Per the delivery rule the user receives the SAME
        # genuine REJECTED message used everywhere else - never a separate
        # "session timeout" message type.
        stuck_message = format_message(
            "verification_failed_message",
            lang=get_user_lang(int(user_id)),
            phone=phone,
            country_code=country_code,
            flag=flag,
        )

        session_info["ready_to_send"] = True
        session_info["device_check_completed"] = True
        # Canonical verdict type: a stuck timeout IS a rejection. Keeping the
        # marker inside the 3 canonical types (SUCCESS/MULTI-DEVICE/REJECTED)
        # keeps channel-report retries and downstream checks consistent.
        session_info["message_type"] = "REJECTED"
        session_info["precomputed_message"] = stuck_message
        session_info["stuck_detected_at"] = current_time.isoformat()

        cached_msg_id = session_info.get("user_message_id") or session_info.get("session_specific_message_id") or session_info.get("cached_msg_id")
        if not cached_msg_id:
            user_info = get_user_info(str(user_id))
            cached_msg_id = user_info.get("pending_message_id") if user_info else None
        # Coerce BEFORE comparing. `"123" > 0` raises TypeError on a str id, and
        # the whole send block sits in a try/except - so the user would simply
        # never be told their session timed out (finding M4).
        try:
            cached_msg_id = int(cached_msg_id or 0)
        except (TypeError, ValueError):
            cached_msg_id = 0
        session_info["cached_msg_id"] = cached_msg_id

        replace_pending_session_data(session_id, session_info)

        message_sent = False
        try:
            if cached_msg_id > 0:
                try:
                    await app.edit_message_text(
                        chat_id=int(user_id),
                        message_id=int(cached_msg_id),
                        text=stuck_message
                    )
                    message_sent = True
                except Exception:
                    await app.send_message(chat_id=int(user_id), text=stuck_message)
                    message_sent = True
            else:
                await app.send_message(chat_id=int(user_id), text=stuck_message)
                message_sent = True

            if message_sent:
                session_info["message_send_success"] = True
                session_info["message_sent_at"] = current_time.isoformat()
                replace_pending_session_data(session_id, session_info)
        except Exception as stuck_msg_err:
            logger.warning(f"⚠️ STUCK RECOVERY: Failed to send message: {stuck_msg_err}")

        try:
            await send_report_to_channel(
                config.get_channel("CHANNEL_REJECTED"),
                build_timeout_report(
                    phone, country_code,
                    user_id=user_id,
                    timeout_seconds=int(session_data.get("timeout_seconds", 0) or 600),
                )
            )
        except Exception as channel_err:
            logger.warning(f"⚠️ STUCK RECOVERY: Channel report failed: {channel_err}")

    except Exception as stuck_reject_err:
        logger.warning(f"⚠️ STUCK RECOVERY: Rejection failed for {session_id}: {stuck_reject_err}")
    finally:
        # The rejection lives HERE, not in the try above, so the docstring's
        # promise is actually kept. Building the message (i18n/flag lookup) or
        # the channel report can throw; the old code then jumped straight to the
        # except with the rejection never reached, and the row stayed pending
        # with its one-shot flag already burned - wedged forever. A fresh status
        # read guards against overwriting a concurrent credit/approval made by
        # pending_recovery in the meantime.
        try:
            latest = get_pending_session(session_id) or {}
            if latest.get("status", "pending") == "pending" and not latest.get("balance_credited"):
                if reject_pending_session(session_id, "Background task timeout (stuck recovery)"):
                    print(f"✅ STUCK RECOVERY: Session {session_id} marked as rejected")
            elif latest.get("status") != "rejected":
                print(f"⏭️ STUCK RECOVERY: {session_id} settled meanwhile "
                      f"(status={latest.get('status')}, credited="
                      f"{bool(latest.get('balance_credited'))}) - not rejecting")
        except Exception as final_reject_err:
            logger.error(
                f"❌ STUCK RECOVERY: could not resolve {session_id}: {final_reject_err}"
            )
        set_pending_session_field(session_id, "stuck_recovery_processing", False)


async def run_stuck_recovery(pending_sessions: Dict[str, dict], current_time: datetime):
    """Detect sessions that passed their max wait window without becoming ready.

    Owns its own for-loop so every ``continue`` keeps its original semantics.
    """
    for session_id, session_data in list(pending_sessions.items()):
        if session_data.get("status") != "pending":
            continue

        session_info = session_data.get("session_data", {})

        ready_to_send = session_info.get("ready_to_send", False)
        device_check_done = session_info.get("device_check_completed", False)

        if session_info.get("message_send_success"):
            # A multi-device session whose has_extra_devices flag was LOST
            # (Orphan D reset) but whose MULTI-DEVICE message was delivered is
            # invisible to every path. Past submitted_at + 26h it is genuinely
            # stuck: reset the sent/precomputed markers so the stuck path below
            # re-verifies it with a fresh live check (full validation flow).
            _md_flag_present = bool(
                session_info.get("has_extra_devices") or session_data.get("has_extra_devices")
            )
            if not _md_flag_present and session_info.get("message_type") == "MULTI-DEVICE":
                _submitted_str = (
                    session_data.get("submitted_at")
                    or session_info.get("submitted_at")
                    or session_data.get("created_at")
                )
                _submitted_dt = None
                if _submitted_str:
                    try:
                        _submitted_dt = datetime.fromisoformat(
                            str(_submitted_str).replace("Z", "").replace("+00:00", "")
                        )
                    except (ValueError, TypeError):
                        _submitted_dt = None
                if _submitted_dt and (current_time - _submitted_dt).total_seconds() > 26 * 3600:
                    print(f"🛡️ STUCK RECOVERY: {session_id} MULTI-DEVICE message sent but "
                          f"extra-device flag lost past 26h - resetting for live re-check")
                    session_info["message_send_success"] = False
                    session_info["message_sent_at"] = None
                    session_info["ready_to_send"] = False
                    session_info["precomputed_message"] = None
                    session_info["message_type"] = None
                    try:
                        replace_pending_session_data(session_id, session_info)
                    except Exception:
                        pass
                else:
                    continue
            else:
                continue

        if session_data.get("stuck_recovery_processing"):
            if not _stuck_processing_stale(session_data, current_time):
                continue
            # The run that set this claim never finished (board killed mid-task).
            # Without this the session would be skipped by THIS net forever.
            logger.warning(
                f"⚠️ STUCK RECOVERY: clearing a stale processing claim on {session_id} "
                f"(no completion within {STUCK_RECOVERY_PROCESSING_STALE_SECONDS}s)"
            )
            set_pending_session_field(session_id, "stuck_recovery_processing", False)
            session_data["stuck_recovery_processing"] = False

        if not ready_to_send or not device_check_done:
            try:
                submitted_at_str = session_data.get("submitted_at")
                if submitted_at_str:
                    submitted_at = datetime.fromisoformat(submitted_at_str)
                    timeout_seconds = session_data.get("timeout_seconds", 600)

                    max_wait_time = timeout_seconds + 180
                    time_since_submit = (current_time - submitted_at).total_seconds()

                    attempts_used = _stuck_attempts_used(session_data)
                    if attempts_used >= MAX_STUCK_RECOVERY_ATTEMPTS:
                        # Exhausted. The row stays PENDING on purpose (never
                        # rejected on our own repeated failure) so pending_recovery
                        # and reconcile keep owning it.
                        continue
                    if _stuck_cooldown_active(session_data, current_time):
                        continue

                    if time_since_submit > max_wait_time:
                        # 🔥 CRITICAL FIX: The persisted ``delayed_task_active``
                        # flag is set True when the delayed 2FA/spam task starts
                        # and cleared on EVERY exit path. If the board was KILLED
                        # mid-task, the flag survives in storage forever - and the
                        # old code skipped such sessions indefinitely (the exact
                        # "board died while holding a task" wedge). The flag is
                        # only authoritative while the task is actually running
                        # IN THIS PROCESS; a stale flag after a restart must not
                        # block stuck recovery.
                        if session_info.get("delayed_task_active") == True:
                            try:
                                from bot.verification.twofa import is_delayed_task_in_process
                                _task_alive = is_delayed_task_in_process(session_id)
                            except Exception:
                                _task_alive = False
                            if _task_alive:
                                print(f"⏭️ STUCK SKIP: delayed task REALLY in process for {session_id} - skipping")
                                continue
                            print(f"🛡️ STUCK RECOVERY: delayed_task_active is STALE for {session_id} "
                                  f"(task no longer in process) - clearing and proceeding")
                            session_info["delayed_task_active"] = False
                            try:
                                replace_pending_session_data(session_id, session_info)
                            except Exception:
                                pass

                        logger.warning(f"⚠️ STUCK SESSION DETECTED: {session_id} "
                                       f"(attempt {attempts_used + 1}/{MAX_STUCK_RECOVERY_ATTEMPTS})")
                        print(f"   - Phone: {session_data.get('phone', 'UNKNOWN')}")
                        print(f"   - Submitted: {time_since_submit:.0f}s ago")
                        print(f"   - Max wait: {max_wait_time}s")
                        print(f"   - ready_to_send: {ready_to_send}, device_check_completed: {device_check_done}")

                        fresh_session = get_pending_session(session_id)
                        if not fresh_session or fresh_session.get("status") != "pending":
                            logger.warning(f"⚠️ STUCK RECOVERY: Session {session_id} no longer pending, skipping")
                            continue

                        fresh_info = fresh_session.get("session_data", {})
                        if fresh_info.get("message_send_success"):
                            logger.warning(f"⚠️ STUCK RECOVERY: Session {session_id} already sent message, skipping")
                            continue

                        # CRITICAL: If session has session_string, let pending_recovery handle it
                        # Don't reject sessions that can be verified
                        # Check at ROOT level (where session_string is actually stored)
                        # AND inside session_data dict for backwards compatibility
                        original_session_info = session_data.get("session_data", {})
                        has_session_string = (
                            # ROOT level - this is where session_string is actually stored!
                            session_data.get("session_string") or
                            fresh_session.get("session_string") or
                            session_data.get("session_file") or
                            fresh_session.get("session_file") or
                            # Inside session_data dict (legacy/backup check)
                            fresh_info.get("session_string") or
                            fresh_info.get("session_exported") or
                            original_session_info.get("session_string") or
                            original_session_info.get("session_exported")
                        )
                        if has_session_string:
                            print(f"✅ STUCK RECOVERY: Session {session_id} has session_string - running recovery now")
                            _record_stuck_attempt(session_id, attempts_used, current_time)
                            try:
                                from bot.recovery.worker import recover_single_session
                                ok, result_type = await recover_single_session(session_id, fresh_session, app)
                                # CRITICAL: NEVER reject a session because a
                                # recovery attempt failed. The worker classifies
                                # definitive dead-session errors itself (and
                                # rejects + reports them internally, returning
                                # REJECTED). A failed attempt here is a
                                # transient/inconclusive result (PENDING), so a
                                # good account must stay pending for the monitor
                                # to retry with a fresh live check - never be
                                # rejected on a network blip.
                                if not ok and result_type not in (
                                    "ALREADY_SENT", "ALREADY_CLAIMED",
                                    "NO_SESSION", "NO_USER", "PENDING", "REJECTED",
                                ):
                                    logger.warning(f"⚠️ STUCK RECOVERY: unexpected recovery result "
                                                   f"{result_type!r} for {session_id} - keeping pending")
                            except Exception as stuck_recover_err:
                                logger.warning(f"⚠️ STUCK RECOVERY: recovery exception for {session_id}: "
                                               f"{stuck_recover_err} - keeping pending")
                            continue

                        # P2-22 (a): DISK RECOVERY - the session string may exist
                        # in the ``.session`` file written at OTP time even when
                        # the DB row never got it (killed between accept and
                        # save). The account user id is used as a safety filter
                        # so a filename collision cannot recover someone else's
                        # session.
                        recovered_string = None
                        try:
                            from bot.services.session_formats import find_session_string_on_disk
                            _expected_uid = (
                                fresh_info.get("user_id")
                                or original_session_info.get("user_id")
                            )
                            try:
                                _expected_uid = int(_expected_uid) if _expected_uid is not None else None
                            except (TypeError, ValueError):
                                _expected_uid = None
                            recovered_string = find_session_string_on_disk(
                                session_data.get("phone") or fresh_session.get("phone") or "",
                                expected_account_user_id=_expected_uid,
                            )
                        except Exception as disk_err:
                            logger.warning(f"⚠️ STUCK RECOVERY: disk recovery lookup failed for {session_id}: {disk_err}")
                        if recovered_string:
                            try:
                                set_pending_session_field(session_id, "session_string", recovered_string)
                                print(f"✅ STUCK RECOVERY: session string RECOVERED from disk for {session_id} - running recovery now")
                                _record_stuck_attempt(session_id, attempts_used, current_time)
                                from bot.recovery.worker import recover_single_session
                                await recover_single_session(session_id, get_pending_session(session_id) or fresh_session, app)
                            except Exception as rec_err:
                                logger.warning(f"⚠️ STUCK RECOVERY: post-disk-recovery error for {session_id}: {rec_err} - keeping pending")
                            continue

                        # P2-22 (b): still nothing verifiable. Give the extra
                        # grace window before rejecting - the account may be
                        # perfectly fine and the data may still come back
                        # (storage reload, orphan reconcile, late save).
                        no_data_deadline = max_wait_time + NO_SESSION_DATA_EXTRA_GRACE_SECONDS
                        if time_since_submit <= no_data_deadline:
                            logger.debug(
                                f"🛡️ STUCK RECOVERY: {session_id} has no verifiable data yet - "
                                f"grace {NO_SESSION_DATA_EXTRA_GRACE_SECONDS}s before resolution"
                            )
                            continue

                        set_pending_session_field(session_id, "stuck_recovery_processing", True)
                        set_pending_session_field(
                            session_id, "stuck_recovery_processing_at", current_time.isoformat()
                        )
                        _record_stuck_attempt(session_id, attempts_used, current_time)
                        await _reject_stuck_session(session_id, session_data, current_time)

            except Exception as stuck_check_err:
                logger.warning(f"⚠️ Error checking stuck session: {stuck_check_err}")
