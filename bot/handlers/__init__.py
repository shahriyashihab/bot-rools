"""
Bot Handlers Package
====================

Contains all command and callback handlers for the bot.

Modules:
    - start: /start command handler
    - account: /account command handler  
    - withdraw: /withdraw command and callbacks
    - admin: Admin panel and management
    - support: /support command handler
    - cap: /cap capacity display handler
"""

from bot.handlers.start import register_start
from bot.handlers.account import register_account
from bot.handlers.support import register_support
from bot.handlers.cap import register_cap_handler

__all__ = [
    "register_start",
    "register_account",
    "register_support",
    "register_cap_handler",
]
