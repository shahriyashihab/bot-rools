"""Fire-and-forget task helper with a STRONG reference.

``asyncio`` keeps only a weak reference to a task. A bare
``asyncio.create_task(coro())`` whose return value is discarded can therefore be
garbage-collected while it is still awaiting, which silently cancels it - and
because nobody holds the task, the cancellation is never reported either. On the
money paths of this bot that meant a channel report, a user notification or a
balance audit record could simply vanish under memory pressure (Termux/Android,
where this runs, hits GC far more often than a desktop).

``spawn_background`` keeps every scheduled task in a module-level set until it
finishes and logs whatever it raised, so a failure is loud instead of invisible.

This module is a LEAF: it imports nothing from ``bot``.
"""
import asyncio
import logging
from typing import Any, Awaitable, Optional, Set

logger = logging.getLogger(__name__)

_background_tasks: Set[asyncio.Task] = set()


def spawn_background(
    coro: Awaitable[Any],
    label: str = "background",
    quiet: bool = False,
) -> Optional[asyncio.Task]:
    """Schedule ``coro`` with a retained reference and error logging.

    Returns the task, or ``None`` when there is no running event loop (in which
    case the coroutine is closed so Python does not warn about it never being
    awaited). Never raises.
    """
    try:
        task = asyncio.create_task(coro, name=label)
    except RuntimeError:
        # No running loop (sync context / shutdown). Close the coroutine so it
        # does not emit "coroutine was never awaited".
        try:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
        if not quiet:
            logger.warning(f"⚠️ spawn_background({label}): no running event loop - skipped")
        return None

    _background_tasks.add(task)

    def _done(finished: asyncio.Task) -> None:
        _background_tasks.discard(finished)
        if finished.cancelled():
            if not quiet:
                logger.warning(f"⚠️ background task cancelled: {label}")
            return
        exc = finished.exception()
        if exc is not None and not quiet:
            logger.error(f"❌ background task failed ({label}): {exc}")

    task.add_done_callback(_done)
    return task


def pending_background_count() -> int:
    """How many spawned tasks are still running (diagnostics / tests)."""
    return len(_background_tasks)
