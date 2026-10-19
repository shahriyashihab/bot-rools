"""
Session Handler Module - Telegram Session Verification System
==============================================================
This module is now a backwards-compatible re-export shim.  All verification
logic lives in the ``bot.verification`` package (state, api, rates, messages,
telethon_export, spam_check, device, steps, phone, otp, twofa, validate).
Every public name is forwarded so that existing ``from
bot.utils.session_handler import ...`` calls keep working unchanged.
"""

from bot.verification import *  # noqa: F401,F403
import bot.verification as _verification


def __getattr__(name):
    """Forward any name (including underscore-private ones) to bot.verification."""
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_verification, name)


__all__ = list(_verification.__all__)
