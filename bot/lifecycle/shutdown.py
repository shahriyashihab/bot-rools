"""Graceful shutdown with proper cleanup and task awaiting."""
import asyncio
import logging

from bot.bootstrap.high_scale import shutdown_high_scale_systems
from bot.core.context import cleanup_tasks, shutdown_event
from bot.monitor.state import pending_monitor_task

logger = logging.getLogger(__name__)


async def graceful_shutdown(sig=None):
    """Handle graceful shutdown with proper cleanup and task awaiting."""
    if sig:
        logger.info(f"Received signal {sig.name}, initiating graceful shutdown...")
    else:
        logger.info("Initiating graceful shutdown...")

    shutdown_event.set()

    all_tasks = []

    # P5-45: the per-session deadline/24h timers live in their OWN registries
    # (not in cleanup_tasks) and previously kept running through the whole
    # shutdown - they could fire verdicts WHILE the storage was being saved
    # and closed. Cancel + await them FIRST.
    try:
        from bot.monitor.deadline_timer import cancel_all_deadline_timers
        cancelled = await cancel_all_deadline_timers()
        if cancelled:
            logger.info(f"Cancelled {cancelled} deadline timer(s)")
    except Exception as e:
        logger.warning(f"Deadline timer cancellation error: {e}")
    try:
        from bot.monitor.twenty_four_hour import cancel_all_24h_timers
        cancelled = await cancel_all_24h_timers()
        if cancelled:
            logger.info(f"Cancelled {cancelled} 24h timer(s)")
    except Exception as e:
        logger.warning(f"24h timer cancellation error: {e}")

    if pending_monitor_task and not pending_monitor_task.done():
        logger.info("Cancelling pending session monitor...")
        pending_monitor_task.cancel()
        all_tasks.append(pending_monitor_task)

    for task in cleanup_tasks:
        if not task.done():
            logger.info("Cancelling cleanup task...")
            task.cancel()
            all_tasks.append(task)

    if all_tasks:
        logger.info(f"Waiting for {len(all_tasks)} background tasks to complete...")
        results = await asyncio.gather(*all_tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, asyncio.CancelledError):
                logger.debug(f"Task {i} cancelled successfully")
            elif isinstance(result, Exception):
                logger.warning(f"Task {i} error during shutdown: {result}")

    try:
        from bot.utils.storage import save_storage_now
        save_storage_now()
        logger.info("Storage saved successfully")
    except Exception as e:
        logger.error(f"Error saving storage: {e}")

    try:
        from bot.utils.storage import backup_database
        backup_database()
    except Exception as e:
        logger.error(f"Error creating shutdown backup: {e}")

    try:
        from bot.utils.storage import close_sqlite_storage
        close_sqlite_storage()
    except Exception:
        pass

    try:
        await shutdown_high_scale_systems()
    except Exception as e:
        logger.error(f"Error shutting down high-scale systems: {e}")

    logger.info("Graceful shutdown complete")
