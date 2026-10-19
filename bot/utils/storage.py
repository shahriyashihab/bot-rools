"""
Storage Module - JSON-based persistent storage with thread-safe operations.
===========================================================================
This module is now a backwards-compatible re-export shim.  All storage logic
lives in the ``bot.data`` package (state, users, sessions, countries,
withdrawals, settings, logs, templates, counters).  Every public name is
forwarded so that existing ``from bot.utils.storage import ...`` calls keep
working unchanged.
"""

from bot.data import *  # noqa: F401,F403
from bot.data import state as _state
import bot.data as _data


def __getattr__(name):
    """Forward any name (including underscore-private ones) to bot.data."""
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_data, name)


__all__ = list(_data.__all__)


logger.info(
    "Storage module loaded with thread-safe operations and retention policy support"
)
