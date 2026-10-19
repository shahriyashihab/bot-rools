"""
Pending Session Recovery System
===============================
This module is now a backwards-compatible re-export shim.  All recovery logic
lives in the ``bot.recovery`` package (constants, validity, worker, pending,
notify, run, cleanup, finalize).  Every public name is forwarded so existing
``from bot.utils.pending_recovery import ...`` calls keep working unchanged.
"""

from bot.recovery import *  # noqa: F401,F403
import bot.recovery as _recovery


def __getattr__(name):
    """Forward any name to the bot.recovery package."""
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_recovery, name)
