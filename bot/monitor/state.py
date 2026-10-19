"""Shared monitor runtime state."""
import asyncio
import logging

logger = logging.getLogger(__name__)

pending_monitor_task = None
monitor_started = False


def _spawn_monitor(label: str) -> None:
    """Create the monitor task and only THEN mark it started (P5-49).

    The old order (``monitor_started = True`` BEFORE ``create_task``) left a
    window where a crash between the two lines permanently marked the monitor
    as started with NO task behind it - and because ``monitor_started`` was
    never re-checked against a live task, a monitor coroutine that DIED later
    (unhandled exception outside its own try/except) was never restarted
    either. Two fixes:
      1. the flag is set only after the task exists;
      2. ``_ensure_monitor_alive`` re-spawns whenever the task is missing or
         finished - called from the deadline/24h arming paths and startable
         from any cycle, so a dead monitor self-heals within one cycle.
    """
    global pending_monitor_task, monitor_started

    if pending_monitor_task is not None and not pending_monitor_task.done():
        return  # alive - nothing to do

    from bot.monitor.orchestrator import pending_session_monitor, MONITOR_CYCLE_INTERVAL

    pending_monitor_task = asyncio.create_task(pending_session_monitor())
    monitor_started = True
    print(f"🔄 Starting pending session monitor ({label}, {MONITOR_CYCLE_INTERVAL:.0f}s check interval)...")

    def _monitor_died(task: asyncio.Task) -> None:
        global monitor_started
        if task.cancelled():
            logger.warning("Pending session monitor task was CANCELLED")
        else:
            exc = task.exception()
            logger.error(f"❌ Pending session monitor DIED: {exc}")
            # Reset the flag so the next _ensure_monitor_alive() can respawn.
            monitor_started = False

    pending_monitor_task.add_done_callback(_monitor_died)


def start_monitor_immediately():
    """Start the pending session monitor immediately on bot startup."""
    _spawn_monitor("startup")


def start_monitor_on_activity():
    """Auto-start pending session monitor on first bot message (backup)."""
    _spawn_monitor("first activity")


async def ensure_monitor_alive() -> bool:
    """Respawn the monitor when it is missing or has died (P5-49 watchdog).

    Safe to call from any coroutine on the event loop. Returns True when a
    NEW monitor task was started.
    """
    if pending_monitor_task is not None and not pending_monitor_task.done():
        return False
    if monitor_started and pending_monitor_task is not None and pending_monitor_task.done():
        logger.warning("🛡️ MONITOR WATCHDOG: monitor task is dead - respawning")
    _spawn_monitor("watchdog respawn")
    return True