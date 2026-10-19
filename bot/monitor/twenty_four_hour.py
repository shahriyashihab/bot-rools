"""Precise 24h multi-device auto-logout + verify timer.

The user requirement is that a multi-device session is verified at EXACTLY
24h after it was detected (not 1 second earlier or later). The 5-minute
monitor poll alone cannot guarantee that precision, so this module arms a
per-session background timer that fires at the exact 24h mark and runs the
standard auto-logout -> verify -> credit flow.

Design notes
------------
- ``ensure_24h_timer`` is called every monitor cycle for every pending
  session; it is idempotent (arms at most one task per session).
- The countdown is anchored to ``submitted_at`` (the moment the account was
  received / OTP verified), so a restart does NOT reset it - the timer is
  simply re-armed with the remaining time.
- Sessions whose 24h mark already passed (e.g. the bot was offline for
  hours) are processed immediately on the next cycle.
- Concurrency is guarded two ways: ``_running`` makes the monitor skip the
  session while the timer executes, and ``run_full_validation`` itself is
  non-reentrant per session (see monitor/validation.py), so a monitor cycle
  that happens to land in the exact same second cannot double-process it.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict

from bot.utils.storage import get_pending_session

logger = logging.getLogger(__name__)

MULTI_DEVICE_HOURS = 24.0

# Hard floor for a re-arm. Telegram itself refuses to terminate other
# authorizations until 24h after the CURRENT (bot) authorization was created,
# and that instant is always a few seconds AFTER ``submitted_at``. Without a
# floor, a run that fired at exactly ``submitted_at + 24h`` was refused, the
# task popped itself out of ``_armed``, and the next monitor cycle re-armed it
# with ``delay=0`` - i.e. a connect + getAuthorizations storm every cycle until
# the retry budget was exhausted and a perfectly good account was rejected.
_REARM_FLOOR_SECONDS = 60.0
# A deferral shorter than this is simply slept through inside the armed task;
# anything longer is left to the next monitor cycle to re-arm.
_MAX_INLINE_DEFER_SECONDS = 900.0

_armed: Dict[str, asyncio.Task] = {}
_running: set = set()


async def cancel_all_24h_timers() -> int:
    """Cancel every armed 24h timer (graceful shutdown, P5-45).

    Returns the number of tasks cancelled. Timers re-arm from the persisted
    ``submitted_at`` anchor on the next boot, so cancelling here loses nothing.
    """
    tasks = list(_armed.values())
    _armed.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return len(tasks)


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "").replace("+00:00", "")
        ).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _logout_not_before(session_data: dict) -> datetime | None:
    """The instant auto-logout is allowed to run again after a deferral.

    ``md_logout_not_before`` is written by ``run_auto_logout`` whenever it hits
    a TIMING condition (Telegram's fresh-authorization rule, a long FloodWait).
    It consumes no retry budget, so the only thing that must happen is to WAIT
    until then - never to retry early and never to reject.
    """
    info = session_data.get("session_data") or {}
    if not isinstance(info, dict):
        info = {}
    return _parse_iso(
        session_data.get("md_logout_not_before")
        or info.get("md_logout_not_before")
    )


def is_timer_running(session_id: str) -> bool:
    """True while the 24h timer is actively executing the flow for a session."""
    return session_id in _running


def _resolve_anchor(session_data: dict) -> datetime:
    """Resolve the 24h countdown anchor (account receive time)."""
    info = session_data.get("session_data", {})
    anchor_str = (
        session_data.get("submitted_at") or
        info.get("submitted_at") or
        session_data.get("created_at") or
        session_data.get("added_at")
    )
    if not anchor_str:
        raise ValueError("no submitted_at / added_at")
    parsed = datetime.fromisoformat(str(anchor_str).replace("Z", "").replace("+00:00", ""))
    return parsed.replace(tzinfo=None)


async def ensure_24h_timer(session_id: str, session_data: dict) -> None:
    """Arm the precise 24h timer for a multi-device session (idempotent)."""
    if session_id in _armed:
        return

    info = session_data.get("session_data", {})
    if not info.get("has_extra_devices"):
        return
    if (
        session_data.get("has_extra_devices_logged_out") or
        info.get("has_extra_devices_logged_out") or
        session_data.get("multi_device_expired") or
        info.get("multi_device_expired")
    ):
        return
    # A session whose auth key is revoked / account deactivated can never be
    # cleaned up. Arming a timer for it would only burn connects; the monitor
    # cycle resolves it to the standard REJECTED outcome instead.
    if session_data.get("md_session_dead") or info.get("md_session_dead"):
        return

    try:
        anchor_dt = _resolve_anchor(session_data)
    except (ValueError, TypeError):
        return

    # NOTE: this deliberately does NOT require ``message_send_success``.
    # Logging the extra devices out at the 24h mark is a hard guarantee that
    # must hold even when the user notification could not be delivered (user
    # blocked the bot, message deleted, Telegram edit error). The countdown is
    # anchored to ``submitted_at``, not to the notification, so gating on
    # delivery only ever meant "never clean this account up".
    if not info.get("message_send_success"):
        print(f"ℹ️ 24H TIMER: {session_id} multi-device message not confirmed delivered yet "
              f"- arming anyway (24h device cleanup is unconditional)")

    now = datetime.now()
    target = anchor_dt + timedelta(hours=MULTI_DEVICE_HOURS)

    # Honor an outstanding deferral: retrying before Telegram's own window
    # opens is guaranteed to be refused again.
    not_before = _logout_not_before(session_data)
    if not_before and not_before > target:
        target = not_before

    delay = (target - now).total_seconds()
    if delay < 0:
        delay = 0.0
    # If the target already passed AND a deferral was recorded, never fire
    # instantly - that is the re-arm storm this floor exists to prevent.
    if delay <= 0 and not_before:
        delay = _REARM_FLOOR_SECONDS

    task = asyncio.create_task(_wait_and_run(session_id, delay))
    _armed[session_id] = task
    print(f"⏰ 24H TIMER ARMED for {session_id} (fires in {delay:.0f}s)")


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
        session = get_pending_session(session_id) or {}
        if session.get("status") != "pending":
            return
        info = session.get("session_data", {})
        if (
            info.get("has_extra_devices_logged_out") or
            session.get("has_extra_devices_logged_out") or
            session.get("multi_device_expired") or
            info.get("multi_device_expired")
        ):
            return
        if session.get("md_session_dead") or info.get("md_session_dead"):
            print(f"💀 24H TIMER: {session_id} session is dead - leaving resolution to the monitor cycle")
            return

        # Re-read the deferral from FRESH storage: the value may have been
        # written after this task was armed. Retrying inside Telegram's closed
        # fresh-authorization window is guaranteed to be refused, so sleep the
        # remainder here (bounded) instead of burning a connect.
        not_before = _logout_not_before(session)
        if not_before:
            wait_s = (not_before - datetime.now()).total_seconds()
            if wait_s > 0:
                if wait_s > _MAX_INLINE_DEFER_SECONDS:
                    print(f"⏸️ 24H TIMER: {session_id} deferred {wait_s / 60:.1f} min by Telegram "
                          f"- re-arming later (no retry consumed)")
                    return
                print(f"⏸️ 24H TIMER: {session_id} deferred {wait_s:.0f}s by Telegram - waiting")
                await asyncio.sleep(wait_s)

        session_string = session.get("session_string") or info.get("session_string")
        if not session_string:
            return

        phone = session.get("phone", "UNKNOWN")
        user_id = str(session.get("user_id", "0"))
        country_code = session.get("country_code", "UNKNOWN")
        rate = float(session.get("rate", 0) or 0)
        twofa_password = (
            session.get("twofa_password")
            or session.get("2fa_password")
            or info.get("twofa_password")
            or info.get("2fa_password")
            or ""
        )

        print(f"⏰ EXACT 24H REACHED for {phone}: running auto-logout + verify")
        from bot.monitor.validation import run_full_validation
        await run_full_validation(
            session_id,
            session,
            session_string,
            twofa_password,
            user_id,
            phone,
            country_code,
            rate,
            md_ready_for_24h=True,
        )
    except Exception as e:
        logger.warning(f"⚠️ 24H TIMER error for {session_id}: {e}")
    finally:
        _running.discard(session_id)
