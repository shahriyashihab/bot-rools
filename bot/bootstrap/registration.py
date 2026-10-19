"""Handler registration - imports every handler module in original order.

Importing these modules triggers their ``@app.on_message`` / ``@app.on_callback_query``
decorators at import time. This module must only be imported AFTER the channel
guard has been applied (bootstrap/client.py does that automatically).

P7-78 - the two-layer handler tree (NO duplicate registration):
    * ``bot/handlers/<name>.py``          -> LIBRARY layer: plain async
      coroutine functions only (register_start / register_account ...), they
      NEVER register pyrogram handlers themselves.
    * ``bot/handlers/commands/<name>.py`` -> REGISTRAR layer: the ONLY place
      a command's @app.on_message decorator lives; it calls into the library
      function.
A third party adding a decorator to a library module would silently
double-register that command (both handlers fire for one message) - keep the
rule: decorators belong ONLY in commands/ and explicit register_* calls.
"""

# Debug handler (group -1000) - runs for every message
from bot.handlers.debug import debug_all_messages

# Auto-start monitor (group -999) - runs FIRST, before all other handlers
from bot.handlers.monitor_hooks import auto_start_monitor

# Command handlers (group 0) - protected by universal channel guard
from bot.handlers.commands.start import start_handler
from bot.handlers.commands.account import account_handler
from bot.handlers.commands.withdraw import withdraw_handler
from bot.handlers.commands.cancel import cancel_handler

# Support handler registration
from bot.handlers.support import register_support
from bot.bootstrap.client import app as _app
register_support(_app)

# Language selection flow (language picker + /language command)
from bot.handlers.language import register_language_handlers
register_language_handlers(_app)

# Admin and cap handlers registration
from bot.handlers.admin import register_admin_handlers
from bot.handlers.cap import register_cap_handler
register_admin_handlers(_app)
register_cap_handler(_app)

# Channel handlers (group 0) - skipped by channel guard
from bot.handlers.channel.cache import auto_cache_channel
from bot.handlers.channel.forward import cache_forwarded_channel
from bot.handlers.channel.test import test_channels_handler

# Membership verify callback
from bot.handlers.callbacks.membership import handle_channel_membership_callback

# Text input handler (group 1) - registered after session handlers
from bot.handlers.text.input import handle_text_input

# Withdraw handlers (callback queries and wallet input) - AFTER session handler
from bot.handlers.withdraw import register_withdraw_handlers, register_channel_withdraw_handlers
register_withdraw_handlers(_app)
register_channel_withdraw_handlers(_app)

__all__ = [
    "debug_all_messages",
    "auto_start_monitor",
    "start_handler",
    "account_handler",
    "withdraw_handler",
    "cancel_handler",
    "auto_cache_channel",
    "cache_forwarded_channel",
    "test_channels_handler",
    "handle_channel_membership_callback",
    "handle_text_input",
]
