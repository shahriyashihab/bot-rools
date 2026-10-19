"""
Shared in-memory state for the session verification workflow.
=================================================================
Holds the global active-session registry and the concurrency cap that
bounds how many delayed (2FA + spam-check) Telegram logins run at once.
"""

import asyncio
import os
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

_active_sessions: Dict[int, Dict[str, Dict[str, Any]]] = {}
_active_sessions_lock = asyncio.Lock()
# The REAL user-facing OTP grace period (enforced by verification/steps.py).
# It used to be 600s while the cleanup sweep advertised a 15-minute window, so
# the extra 5 minutes were pure fiction: the in-memory session was dropped at
# 10 minutes and a code that arrived afterwards had nowhere to go. 900s makes
# the window the user is told about the window they actually get.
_SESSION_MAX_IDLE_SECONDS = 900

# ==========================================================================
# Concurrency cap for the delayed (2FA + spam-check) CHECK PHASE.
#
# The cap no longer ignores how many Telegram APIs are available. Capacity
# scales linearly with the number of APIs in the rotation pool:
#
#     max_concurrency = PER_API_CONCURRENCY * <number of APIs>
#
# So adding APIs (EXTRA_API_*) automatically raises how many sessions can run
# their connection-holding check phase at the same time. An explicit
# DELAYED_TASK_MAX_CONCURRENCY env override still wins for manual tuning.
#
# NOTE: this cap bounds only the SHORT check phase (2FA -> spam -> device).
# The deadline wait is a cheap per-session timer that holds NO connection and
# NO slot, so thousands of pending sessions can wait without being capped.
# ==========================================================================
_PER_API_CONCURRENCY = int(os.getenv("PER_API_CONCURRENCY", "10") or "10")


def _resolve_max_concurrency() -> int:
    """Compute the check-phase concurrency cap from the API pool size."""
    override = int(os.getenv("DELAYED_TASK_MAX_CONCURRENCY", "0") or "0")
    if override > 0:
        return override

    api_count = 1
    try:
        from bot.utils.api_rotation import get_api_manager
        manager = get_api_manager()
        if manager.apis:
            api_count = len(manager.apis)
    except Exception:
        pass

    return max(1, _PER_API_CONCURRENCY * api_count)


_DELAYED_TASK_MAX_CONCURRENCY = _resolve_max_concurrency()
_delayed_task_semaphore = asyncio.Semaphore(_DELAYED_TASK_MAX_CONCURRENCY)

# ==========================================================================
# Concurrency cap for LIVE DEVICE VERDICTS.
#
# The authoritative decision (deadline recheck, monitor full validation,
# recovery recheck) needs a short-lived GetAuthorizations connection. At a
# country-timeout deadline thousands of sessions can become due at once, so
# the verdicts must be bounded or the cluster would open an unbounded number
# of simultaneous Telegram connections.
#
# Verdicts are quick (one request each) and do NOT hold the delayed-task
# semaphore slot (that slot is released before the deadline wait), so a
# separate, higher cap is used. It scales with the API pool like the check
# phase but is allowed to go higher via DEVICE_VERDICT_MAX_CONCURRENCY.
# ==========================================================================
_VERDICT_PER_API_CONCURRENCY = int(os.getenv("VERDICT_PER_API_CONCURRENCY", "20") or "20")


def _resolve_verdict_max_concurrency() -> int:
    """Compute the live-verdict concurrency cap from the API pool size."""
    override = int(os.getenv("DEVICE_VERDICT_MAX_CONCURRENCY", "0") or "0")
    if override > 0:
        return override

    api_count = 1
    try:
        from bot.utils.api_rotation import get_api_manager
        manager = get_api_manager()
        if manager.apis:
            api_count = len(manager.apis)
    except Exception:
        pass

    return max(8, _VERDICT_PER_API_CONCURRENCY * api_count)


_DEVICE_VERDICT_MAX_CONCURRENCY = _resolve_verdict_max_concurrency()
_device_verdict_semaphore = asyncio.Semaphore(_DEVICE_VERDICT_MAX_CONCURRENCY)


def _grow_semaphore(sem: asyncio.Semaphore, target: int) -> int:
    """Grow an asyncio.Semaphore to allow at least ``target`` concurrent holders.

    asyncio.Semaphore has no official resize API, so we add capacity by
    releasing extra permits (safe: only ever INCREASES the limit; a permit
    released above the original limit simply lets one more waiter proceed).
    Returns the new effective capacity.
    """
    try:
        current = getattr(sem, "_value", 0)
    except Exception:
        return target
    if target > current:
        for _ in range(target - current):
            try:
                sem.release()
            except Exception:
                break
        logger.info(f"🔧 Concurrency semaphore grown: {current} → {target}")
    return target


def refresh_concurrency_limits() -> Dict[str, int]:
    """Recompute both concurrency caps from the CURRENT API pool size and grow
    the semaphores to match.

    Call this after ``add_api_runtime`` / ``remove_api_runtime`` so adding more
    APIs at runtime immediately raises how many sessions can run their check
    phase and live-verdict phase in parallel — without a restart.

    Shrinking is intentionally NOT performed: removing permits from a semaphore
    that has waiters is unsafe (a waiter could acquire a permit that another
    holder must then release). Removing APIs still takes effect after a
    restart, and the only cost of a temporarily-too-large cap is a few extra
    concurrent connections for a short window.
    """
    new_delayed = _resolve_max_concurrency()
    new_verdict = _resolve_verdict_max_concurrency()

    global _DELAYED_TASK_MAX_CONCURRENCY, _DEVICE_VERDICT_MAX_CONCURRENCY
    _DELAYED_TASK_MAX_CONCURRENCY = _grow_semaphore(_delayed_task_semaphore, new_delayed)
    _DEVICE_VERDICT_MAX_CONCURRENCY = _grow_semaphore(_device_verdict_semaphore, new_verdict)
    return {
        "delayed_task_max_concurrency": _DELAYED_TASK_MAX_CONCURRENCY,
        "device_verdict_max_concurrency": _DEVICE_VERDICT_MAX_CONCURRENCY,
    }
