"""
Exact per-session country-timeout deadline timer.

The 5-minute monitor poll alone cannot guarantee that a session is processed
at the exact second its country-timeout deadline passes. After a restart the
delayed background task no longer exists to fire at the exact deadline, so
this module arms a per-session background timer that fires at the exact
``deadline_at`` and runs the standard fast-path / full-validation flow.

Design notes
------------
- ``ensure_deadline_timer`` is called every monitor cycle for every pending
  session; it is idempotent (arms at most one task per session).
- The countdown is anchored to the persisted ``deadline_at``
  (``submitted_at`` + ``timeout_seconds``), so a restart does NOT reset it -
  the timer is simply re-armed with the remaining time.
- Sessions whose deadline already passed (e.g. the bot was offline) are
  processed immediately on the next cycle.
- In live mode the delayed 2FA/spam-check background task is still alive
  in-process and is the authoritative deadline handler (it fires at the exact
  same instant). The timer defers to it via ``is_delayed_task_in_process`` so
  the two never race.
- Concurrency is guarded two ways: ``_running`` makes the monitor skip the
  session while the timer executes, and ``run_full_validation`` itself is
  non-reentrant per session (see monitor/validation.py), so a monitor cycle
  that lands in the exact same second cannot double-process it.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict

from bot.utils.storage import get_pending_session

logger = logging.getLogger(__name__)

_armed: Dict[str, asyncio.Task] = {}
_running: set = set()


async def cancel_all_deadline_timers() -> int:
    """Cancel every armed deadline timer (graceful shutdown, P5-45).

    Returns the number of tasks cancelled. The timers re-arm from persisted
    ``deadline_at`` on the next boot, so cancelling here loses nothing.
    """
    tasks = list(_armed.values())
    _armed.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return len(tasks)


def is_timer_running(session_id: str) -> bool:
    """True while the deadline timer is actively executing the flow for a session."""
    return session_id in _running


def _resolve_deadline(session_data: dict) -> datetime:
    """Resolve the exact country-timeout deadline for a pending session."""
    deadline_str = session_data.get("deadline_at")
    if deadline_str:
        return datetime.fromisoformat(str(deadline_str).replace("Z", "").replace("+00:00", ""))
    submitted_str = session_data.get("submitted_at")
    if submitted_str:
        timeout_seconds = float(session_data.get("timeout_seconds", 0) or 0)
        return datetime.fromisoformat(
            str(submitted_str).replace("Z", "").replace("+00:00", "")
        ) + timedelta(seconds=timeout_seconds)
    raise ValueError("no deadline_at / submitted_at")


async def ensure_deadline_timer(session_id: str, session_data: dict) -> None:
    """Arm the precise deadline timer for a pending session (idempotent)."""
    if session_id in _armed:
        return

    if session_data.get("status") != "pending":
        return

    info = session_data.get("session_data", {})
    if info.get("message_send_success"):
        return

    # Only arm for sessions that actually have a usable session string; the
    # no-OTP sessions are cleaned up separately and have nothing to verify.
    if not (session_data.get("session_string") or info.get("session_string")):
        return

    try:
        deadline = _resolve_deadline(session_data)
    except (ValueError, TypeError):
        return

    delay = (deadline - datetime.now()).total_seconds()
    if delay < 0:
        delay = 0.0

    task = asyncio.create_task(_wait_and_run(session_id, delay))
    _armed[session_id] = task
    logger.debug(f"⏰ DEADLINE TIMER ARMED for {session_id} (fires in {delay:.0f}s)")


async def _wait_and_run(session_id: str, delay: float) -> None:
    try:
        if delay > 0:
            await asyncio.sleep(delay)
        await _run_at_target(session_id)
    except asyncio.CancelledError:
        pass
    finally:
        _armed.pop(session_id, None)


async def _run_at_target(session_id: str) -> None:
    if session_id in _running:
        return
    _running.add(session_id)
    try:
        # In live mode the in-process delayed task fires at the exact same
        # deadline and is authoritative - never race it. BUT if that task
        # hangs/stalls past the deadline (network hang without timeout), the
        # deadline must still be honoured by the monitor: defer only for a
        # bounded grace, then run the flow ourselves (run_full_validation is
        # non-reentrant per session, so a live task finishing late is safe).
        from bot.verification.twofa import is_delayed_task_in_process
        if is_delayed_task_in_process(session_id):
            _session = get_pending_session(session_id) or {}
            _deadline_passed = False
            try:
                _deadline = _resolve_deadline(_session)
                _deadline_passed = datetime.now() > _deadline + timedelta(seconds=300)
            except (ValueError, TypeError):
                _deadline_passed = False
            if not _deadline_passed:
                print(f"⏸️ DEADLINE TIMER: {session_id} delayed task in process - deferring")
                return
            print(f"🛡️ DEADLINE TIMER: {session_id} delayed task looks STUCK "
                  f"(deadline +300s passed) - running the flow now")

        session = get_pending_session(session_id) or {}
        if session.get("status") != "pending":
            return
        info = session.get("session_data", {})
        if info.get("message_send_success"):
            return

        session_string = session.get("session_string") or info.get("session_string")
        if not session_string:
            return

        phone = session.get("phone", "UNKNOWN")
        user_id = str(session.get("user_id", "0"))
        country_code = session.get("country_code", "UNKNOWN")
        rate = float(session.get("rate", 0) or 0)
        # 2FA password lookup must be consistent with recovery/worker.py: check
        # every spelling (twofa_password / 2fa_password) in both the root row
        # and the nested session_data blob before falling back to config.
        twofa_password = (
            session.get("twofa_password")
            or session.get("2fa_password")
            or info.get("twofa_password")
            or info.get("2fa_password")
            or ""
        )

        print(f"⏰ EXACT DEADLINE REACHED for {phone}: running verification")

        # Mirror the monitor's own processing guard so the next monitor cycle
        # does not re-enter the session while this timer handles it.
        from bot.utils.storage import set_pending_session_field
        set_pending_session_field(session_id, "currently_processing", True)
        set_pending_session_field(session_id, "currently_processing_at", datetime.now().isoformat())

        # Precomputed message already exists -> fast path (identical content to
        # what the delayed task would have sent). Otherwise full validation.
        precomputed_message = info.get("precomputed_message")
        message_type = info.get("message_type")
        cached_msg_id = info.get("cached_msg_id")
        ready_to_send = info.get("ready_to_send")
        if precomputed_message and message_type and ready_to_send:
            from bot.monitor.fast_path import try_fast_path_finalization
            await try_fast_path_finalization(
                session_id, session, info, phone, user_id, country_code,
                precomputed_message, cached_msg_id, message_type, rate,
            )
        else:
            from bot.monitor.validation import run_full_validation
            await run_full_validation(
                session_id, session, session_string, twofa_password,
                user_id, phone, country_code, rate, md_ready_for_24h=False,
            )
    except Exception as e:
        logger.warning(f"⚠️ DEADLINE TIMER error for {session_id}: {e}")
    finally:
        _running.discard(session_id)
