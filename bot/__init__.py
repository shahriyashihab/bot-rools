"""
Telegram Session Bot Package
============================

A professional Telegram bot for session verification and management.

Modules:
    - handlers: Command and callback handlers
    - utils: Utility functions and helpers
    - middleware: Request middleware (channel guard, etc.)
    - config: Configuration management
"""

__version__ = "2.0.0"
__author__ = "Session Bot Team"

from bot import config

__all__ = [
    "config",
    "__version__",
    "__author__",
]
