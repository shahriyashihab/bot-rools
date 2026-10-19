"""Pending-session monitor orchestrator: scheduling + per-session dispatch."""
import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Dict

from bot.bootstrap.client import app
from bot import config
from bot.monitor.deadline_timer import ensure_deadline_timer, is_timer_running as is_deadline_timer_running
from bot.monitor.fast_path import try_fast_path_finalization
from bot.monitor.multi_device import evaluate_multi_device_window
from bot.monitor.twenty_four_hour import ensure_24h_timer
from bot.monitor.recovery_scan import run_recovery_scan
from bot.monitor.stuck_recovery import run_stuck_recovery
from bot.monitor.validation import run_full_validation
from bot.data.sessions import STALE_CLAIM_MAX_AGE_SECONDS
from bot.utils.storage import (
    get_all_pending_sessions, reject_pending_session, set_pending_session_field,
    update_pending_session_field,
)
from bot.handlers.admin.router import get_monitor_status

logger = logging.getLogger(__name__)

PENDING_FETCH_INTERVAL = 3

# Monitor cycle interval: how often the pending-session monitor loop wakes up
# to scan due sessions, run stuck recovery and the channel-report safety net.
# 60s keeps every session within a minute of its deadline even if the exact
# per-session deadline timer is lost (restart race); can be tuned with
# MONITOR_INTERVAL_SECONDS (seconds, default 60).  A non-numeric or negative
# value in the env var silent-falls back to 60 so a typo never crashes the
# monitor at import time.
try:
    _raw = float(os.getenv("MONITOR_INTERVAL_SECONDS", "60") or "60")
    MONITOR_CYCLE_INTERVAL = max(10.0, _raw)
except (TypeError, ValueError):
    MONITOR_CYCLE_INTERVAL = 60.0

# P2-18: a session whose record is missing critical data (session_string /
# phone) is NOT rejected instantly. A crash between OTP-accept and persistence
# can leave the row without its session string while the ACCOUNT itself is
# perfectly fine; the old "Invalid session data" instant reject destroyed such
# accounts on data-absence instead of evidence. Recovery paths (stuck recovery
# with its disk-file lookup, orphan reconcile, storage reload) get a grace
# window to restore the data; only after the window expires is the session
# rejected through the normal (user-notifying) rejection path.
_MISSING_DATA_GRACE_HOURS = float(os.getenv("MISSING_DATA_GRACE_HOURS", "6") or "6")


class _MissingSessionDataError(Exception):
    """Raised when a pending record lacks critical fields (missing list attached)."""

    def __init__(self, missing):
        self.missing = list(missing)
        super().__init__(f"missing: {', '.join(self.missing)}")


def _handle_missing_critical_data(session_id: str, missing) -> None:
    """Grace-window handling for records with missing critical data.

    First observation: persist ``data_missing_since`` and keep the session
    pending. Subsequent cycles: once the grace window expires WITHOUT the data
    being restored, reject through the standard path (user notification +
    rejected-channel report), exactly like any other rejection.
    """
    from bot.utils.storage import get_pending_session, replace_pending_session_data
    try:
        current = get_pending_session(session_id) or {}
        info = current.get("session_data", {}) or {}
        first_missing = info.get("data_missing_since")
        if not first_missing:
            info["data_missing_since"] = datetime.now().isoformat()
            info["data_missing_fields"] = ",".join(missing)
            replace_pending_session_data(session_id, info)
            set_pending_session_field(session_id, "currently_processing", False)
            logger.warning(
                f"🛡️ Session {session_id}: missing {missing} - grace window started "
                f"({_MISSING_DATA_GRACE_HOURS}h), NOT rejecting (recovery paths may restore it)"
            )
            return
        since = datetime.fromisoformat(str(first_missing).replace("Z", "").replace("+00:00", ""))
        hours = (datetime.now() - since).total_seconds() / 3600.0
        if hours >= _MISSING_DATA_GRACE_HOURS:
            logger.error(
                f"❌ Session {session_id}: still missing {missing} after {hours:.1f}h - rejecting"
            )
            reject_pending_session(
                session_id,
                f"Session data missing after grace window: {', '.join(missing)}",
            )
        else:
            set_pending_session_field(session_id, "currently_processing", False)
            logger.debug(
                f"🛡️ Session {session_id}: still missing {missing} "
                f"(grace {hours:.1f}h / {_MISSING_DATA_GRACE_HOURS}h)"
            )
    except Exception as e:
        logger.warning(f"Missing-data grace handling failed for {session_id}: {e}")


async def run_channel_report_safety_net(pending_sessions: Dict[str, dict]) -> None:
    """Per-cycle safety net: re-deliver channel reports that never arrived.

    A session whose user message was delivered (``message_send_success``) but
    whose channel files never reached any channel (``channel_report_pending``
    and not ``channel_report_sent``) is retried here every cycle. This is the
    last-resort guarantee that session/JSON files NEVER permanently miss a
    channel - no text report is ever substituted for a file. It also survives
    restarts because the pending marker is persisted in storage.
    """
    from bot.services.session_reporting import retry_channel_report

    for session_id, session_data in list(pending_sessions.items()):
        if not isinstance(session_data, dict):
            continue
        info = session_data.get("session_data") or {}
        if not isinstance(info, dict):
            continue
        if info.get("channel_report_sent") is True:
            continue
        message_type = info.get("message_type") or ""

        # PENDING reports (the initial CHANNEL_SUBMIT submission) are retried
        # even before the final user verdict message is delivered: the session
        # file must reach the admin's pending channel. The user was already
        # told "processing" at OTP time; the delayed task later overwrites
        # message_type with the final verdict (SUCCESS/MULTI-DEVICE/REJECTED)
        # and edits the pending channel message in place.
        if message_type == "PENDING":
            if info.get("channel_report_pending") is not True:
                # Not marked pending and not in-progress → nothing to retry.
                if not info.get("channel_report_in_progress"):
                    continue
                from bot.utils.storage import is_channel_report_delivery_stale
                try:
                    if not is_channel_report_delivery_stale(session_id):
                        continue
                except Exception:
                    continue
        elif message_type in ("SUCCESS", "MULTI-DEVICE"):
            if info.get("message_send_success") is not True:
                continue
            if info.get("channel_report_pending") is not True:
                if info.get("channel_report_in_progress"):
                    from bot.utils.storage import is_channel_report_delivery_stale
                    try:
                        if not is_channel_report_delivery_stale(session_id):
                            continue
                    except Exception:
                        continue
        else:
            continue
        try:
            await retry_channel_report(session_id, session_data)
        except Exception as safety_err:
            logger.warning(f"⚠️ Channel-report safety net error for {session_id}: {safety_err}")


async def pending_session_monitor():
    """
    Background task to monitor pending sessions and validate them after timeout

    RECOVERY MODE: If bot restarts with pending sessions that have already completed
    2FA/spam check/device check, they will be processed immediately (within 60 seconds)
    instead of waiting for the full country timeout.
    """
    print("🔄 Pending session monitor started...")
    startup_time = datetime.now()
    recovery_done = False
    last_cleanup_time = datetime.now()
    last_pending_fetch_time = datetime.min
    pending_sessions_cache = {}
    monitor_first_iteration = True

    while True:
        try:
            try:
                if not monitor_first_iteration:
                    await asyncio.sleep(MONITOR_CYCLE_INTERVAL)
                monitor_first_iteration = False
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1)
                continue

            current_time = datetime.now()

            # P5-49: self-watchdog - if the monitor task itself died (crash
            # outside its try/except, cancellation by a bug), respawn it.
            # Running this INSIDE the loop means a healthy loop is a no-op.
            try:
                from bot.monitor.state import ensure_monitor_alive
                await ensure_monitor_alive()
            except Exception:
                pass

            # P5-50: the DURABILITY safety nets (hourly finalize/reconcile and
            # the channel-report net) run even when the admin toggles the
            # monitor OFF - the toggle governs per-session dispatch, not data
            # repair. The old gate silently stopped all crash reconciliation.
            if (current_time - last_cleanup_time).total_seconds() > 3600:
                try:
                    await run_hourly_cleanup()
                    last_cleanup_time = current_time
                except Exception as cleanup_err:
                    logger.warning(f"⚠️ Cleanup error: {cleanup_err}")

            if (current_time - last_pending_fetch_time).total_seconds() >= PENDING_FETCH_INTERVAL:
                pending_sessions_cache = get_all_pending_sessions()
                last_pending_fetch_time = current_time
            pending_sessions = pending_sessions_cache
            await run_channel_report_safety_net(pending_sessions)

            if not get_monitor_status():
                continue

            seconds_since_startup = (current_time - startup_time).total_seconds()
            # Recovery scan window MUST match its guard: the scan is allowed to
            # run for the first 120 seconds (the guard below), so the completion
            # flag must also flip at 120s. The old 60s flag meant the scan only
            # ever ran ~2 cycles (0s and 60s), so a failure on the first cycle
            # was never given a second chance within the advertised window.
            if not recovery_done and seconds_since_startup < 120:
                await run_recovery_scan(pending_sessions)

            if not recovery_done and seconds_since_startup >= 120:
                recovery_done = True
                print("✅ RECOVERY MODE: Initial recovery scan window closed (120s)")

            await run_stuck_recovery(pending_sessions, current_time)

            await process_pending_sessions(pending_sessions, current_time)

        except Exception as e:
            logger.error(f"❌ Error in pending session monitor: {e}")
            # P5-56: the message and the actual sleep must agree (the old
            # "60 seconds" log slept 30s).
            print(f"📍 Monitor error - recovering in {MONITOR_CYCLE_INTERVAL:.0f} seconds...")
            try:
                await asyncio.sleep(MONITOR_CYCLE_INTERVAL)
            except asyncio.CancelledError:
                break  # Exit if task is cancelled
            continue


async def run_hourly_cleanup() -> None:
    """Run the hourly expired/stuck/stale session cleanup."""
    from bot.utils.pending_recovery import finalize_expired_sessions
    print("🧹 HOURLY: Running expired session cleanup...")
    expired_stats = await finalize_expired_sessions(app)
    total_expired = expired_stats.get("deleted_no_otp", 0) + expired_stats.get("expired_old", 0)
    if total_expired > 0:
        print(f"🧹 HOURLY CLEANUP: Finalized {total_expired} expired sessions with user notifications")

    from bot.utils.storage import cleanup_old_stuck_sessions
    cleaned = cleanup_old_stuck_sessions(max_age_hours=72)
    if cleaned > 0:
        print(f"🧹 AUTO CLEANUP: Removed {cleaned} old stuck sessions")

    from bot.utils.session_handler import cleanup_stale_active_sessions
    await cleanup_stale_active_sessions()

    # Crash-window reconciliation: settle sessions whose message/credit/report
    # was interrupted by a restart (idempotent; runs hourly as a safety net on
    # top of the startup run).
    try:
        from bot.recovery.reconcile import reconcile_orphaned_sessions
        await reconcile_orphaned_sessions(app)
    except Exception as reconcile_err:
        logger.warning(f"⚠️ HOURLY RECONCILE error: {reconcile_err}")


async def process_pending_sessions(pending_sessions: Dict[str, dict], current_time: datetime) -> None:
    """Main per-session processing loop.

    Each session is processed inside its OWN try/except. Without that isolation
    a single bad row (unparseable timestamp, Telegram error, storage hiccup)
    raised out of the whole loop into monitor_pending_sessions' outer handler,
    which sleeps 30s and starts the next cycle - so every session AFTER the bad
    one was never processed, and ``run_channel_report_safety_net`` (which runs
    after this call) was skipped too. One broken session could therefore stall
    payouts and channel reports for every other session indefinitely.
    """
    for session_id, session_data in list(pending_sessions.items()):
        try:
            await _process_one_pending_session(session_id, session_data, current_time)
        except Exception as session_err:
            logger.error(
                f"❌ MONITOR: session {session_id} raised - isolating it and "
                f"continuing with the rest: {session_err}",
                exc_info=True,
            )
            continue


async def _process_one_pending_session(
    session_id: str, session_data: dict, current_time: datetime
) -> None:
    """Process ONE pending session (deadline reached → fast path or full validation).

    Every ``return`` in this function is the old loop's ``continue``.
    """
    if session_data.get("status") != "pending":
        return

    # Arm the precise 24h multi-device timer (idempotent; re-arms after
    # restart with the remaining time anchored to persisted added_at).
    await ensure_24h_timer(session_id, session_data)

    # Arm the exact per-session deadline timer (idempotent; re-arms after
    # restart from the persisted deadline so an exact-deadline check that
    # was pending at restart is not lost).
    await ensure_deadline_timer(session_id, session_data)

    session_info = session_data.get("session_data", {})

    # ===== MULTI-DEVICE 24h WINDOW HANDLING =====
    md_ready_for_24h, md_skip = await evaluate_multi_device_window(
        session_id, session_data, session_info, current_time
    )
    if md_skip:
        return

    # If the exact-deadline timer is already armed and running for this
    # session it will handle the deadline; skip to avoid a duplicate check
    # in the same window the timer fires.
    if is_deadline_timer_running(session_id) and not md_ready_for_24h:
        return

    # CRITICAL (restart safety): ``currently_processing`` is PERSISTED, so
    # a process that crashed/restarted mid-finalization leaves it True
    # forever and the monitor skips the session in every cycle.  Release it
    # when it is older than STALE_CLAIM_MAX_AGE_SECONDS so a deadline-passed
    # session is re-processed promptly after a restart.
    #
    # P5-53: this applies to MULTI-DEVICE sessions too. The old
    # ``and not md_ready_for_24h`` exclusion wedged a multi-device session
    # whose finalization crashed mid-claim: every cycle skipped it and only
    # the 72h backup ever resolved it (up to 48 extra hours). A claim older
    # than the stale window is dead regardless of the session type - the 24h
    # timer flow has its own is_timer_running guard against live races.
    if session_data.get("currently_processing"):
        cp_at = session_data.get("currently_processing_at") or session_info.get("finalization_claimed_at")
        cp_stale = False
        if cp_at:
            try:
                cp_dt = datetime.fromisoformat(str(cp_at).replace("Z", "").replace("+00:00", ""))
                if (current_time - cp_dt).total_seconds() > STALE_CLAIM_MAX_AGE_SECONDS:
                    cp_stale = True
            except (ValueError, TypeError):
                pass
        else:
            # No timestamp at all: legacy row - treat as stale so it can never
            # wedge forever.
            cp_stale = True
        if not cp_stale:
            return
        set_pending_session_field(session_id, "currently_processing", False)
        update_pending_session_field(session_id, "finalization_claimed", False)
        print(f"🛡️ MONITOR: stale processing markers released for {session_id} - reprocessing")

    if session_info.get("message_send_success") == True and session_info.get("message_sent_at") and not md_ready_for_24h:
        return

    # If delayed task is still actively processing, skip (task will handle it)
    # But if deadline has passed, task may be stuck - let monitor handle it
    if session_info.get("delayed_task_active") == True:
        is_stuck = False
        try:
            deadline_at = session_data.get("deadline_at")
            if deadline_at:
                deadline = datetime.fromisoformat(str(deadline_at).replace("Z", "").replace("+00:00", ""))
                if current_time > deadline:
                    is_stuck = True
        except Exception:
            pass
        if not is_stuck:
            return  # Task still running, deadline not passed
        else:
            logger.warning(f"⚠️ Session {session_id}: delayed_task_active but deadline passed - task may be stuck, monitor processing")

    try:
        deadline_at = session_data.get("deadline_at")
        if deadline_at:
            deadline = datetime.fromisoformat(str(deadline_at).replace("Z", "").replace("+00:00", ""))
        else:
            submitted_at = datetime.fromisoformat(str(session_data["submitted_at"]).replace("Z", "").replace("+00:00", ""))
            timeout_seconds = session_data["timeout_seconds"]
            deadline = submitted_at + timedelta(seconds=timeout_seconds)

        time_remaining = (deadline - current_time).total_seconds()
    except Exception as e:
        logger.warning(f"⚠️ Error parsing session times: {e}")
        return

    if time_remaining <= 0:
        # Mark as currently processing to prevent duplicate checks
        session_data["currently_processing"] = True
        set_pending_session_field(session_id, "currently_processing", True)
        set_pending_session_field(session_id, "currently_processing_at", datetime.now().isoformat())

        timeout_seconds = session_data.get("timeout_seconds", 0)
        logger.debug(f"⏱️ Session {session_id}: deadline reached at {deadline.strftime('%H:%M:%S')} (timeout: {timeout_seconds}s - READY)")

        try:
            session_string = session_data.get("session_string")
            session_info = session_data.get("session_data", {})
            # The 2FA password is set at session creation from config, but
            # a legacy/corrupted record may lack the root field. NEVER
            # reject a good account because of that - fall back to the
            # config value exactly like the full-validation path does
            # (validation.py), so both monitor paths behave identically.
            # Check every spelling (twofa_password / 2fa_password) in both
            # the root row and the nested session_data blob for consistency
            # with recovery/worker.py.
            twofa_password = (
                session_data.get("twofa_password")
                or session_data.get("2fa_password")
                or session_info.get("twofa_password")
                or session_info.get("2fa_password")
                or config.get_2fa_password()
            )
            user_id = str(session_data.get("user_id", "0"))
            phone = session_data.get("phone", "UNKNOWN")
            country_code = session_data.get("country_code", "UNKNOWN")
            rate = float(session_data.get("rate", 0))

            precomputed_message = session_info.get("precomputed_message")
            cached_msg_id = session_info.get("cached_msg_id")
            message_type = session_info.get("message_type")

            # A MULTI-DEVICE precomputed message whose extra-device flag was
            # lost (has_extra_devices falsy, no balance credited) is stale by
            # definition - the fast path must NOT re-send the MD text; fall
            # through to the live full validation below.
            _md_flag_lost = bool(
                message_type == "MULTI-DEVICE"
                and not (session_info.get("has_extra_devices") or session_data.get("has_extra_devices"))
                and not session_data.get("balance_credited")
            )
            if precomputed_message and message_type and not md_ready_for_24h and not _md_flag_lost:
                fast_handled, session_info = await try_fast_path_finalization(
                    session_id, session_data, session_info, phone, user_id, country_code,
                    precomputed_message, cached_msg_id, message_type, rate
                )
                if fast_handled:
                    return

            if not session_string or not twofa_password or not phone:
                missing = []
                if not session_string:
                    missing.append("session_string")
                if not twofa_password:
                    missing.append("twofa_password")
                if not phone:
                    missing.append("phone")
                raise _MissingSessionDataError(missing)

            logger.debug(f"🔍 Session {session_id}: Phone={phone}, User={user_id}, Rate=${rate}")
        except _MissingSessionDataError as e:
            # P2-18: data-absence is NOT evidence of a bad account. Start (or
            # consult) the grace window instead of the old instant reject.
            _handle_missing_critical_data(session_id, e.missing)
            return
        except (KeyError, ValueError, TypeError) as e:
            # Malformed timestamps/fields: log and keep pending. Rejecting on
            # a parse failure is data-absence, not Telegram evidence; the 72h
            # finalize path remains the bounded backstop.
            logger.error(f"❌ CRITICAL: Error extracting session data for {session_id}: {e}")
            return
        except Exception as e:
            logger.error(f"❌ CRITICAL: Unexpected error in session extraction: {e}")
            return

        skip, session_data = await run_full_validation(
            session_id, session_data, session_string, twofa_password,
            user_id, phone, country_code, rate, md_ready_for_24h
        )
        if skip:
            return
