"""Channel-guard activation - must run before any handler registration."""
from pyrogram.client import Client

from bot.middleware.channel_guard import apply_channel_guard


def apply_guard(app: Client) -> None:
    """Apply the universal channel guard to the app.

    Must be called before ANY handler module is imported/registered.
    """
    apply_channel_guard(app)
    print("✅ Universal Channel Guard activated on app")
