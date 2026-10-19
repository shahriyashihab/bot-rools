"""Client bootstrap - creates the single Pyrogram Client instance."""
import os
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from pyrogram.client import Client

from bot import config
from bot.bootstrap.guard import apply_guard


def _resolve_workers() -> int:
    """Resolve the Pyrogram update-processing worker count.

    Pyrogram's default is 4 workers; for thousands of concurrent users that is
    the bottleneck that serializes message handling. We raise it and allow the
    operator to tune it via ``PYROGRAM_WORKERS`` (env / .env). A non-numeric or
    too-small value silently falls back to a sane floor so a typo never
    crashes the bot at import time.
    """
    raw = os.getenv("PYROGRAM_WORKERS", "16") or "16"
    try:
        workers = int(raw)
    except (TypeError, ValueError):
        workers = 16
    return max(4, workers)


app = Client(
    "session_bot_v2",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    in_memory=True,
    workers=_resolve_workers(),
)

# Hard guard: this client MUST stay in-memory. File-based session storage breaks
# on read-only deployments with "attempt to write a readonly database", which
# crashes handle_updates() and makes every handler unresponsive. If this ever
# triggers, the deployment is running stale code and must be updated.
if not app.in_memory:
    raise RuntimeError(
        "FATAL: Main client must use in_memory=True (MemoryStorage). "
        "File-based sessions fail on read-only deploys. "
        "Update to the latest code (commit with 'in-memory client storage')."
    )

# ========== APPLY UNIVERSAL CHANNEL GUARD ==========
# This MUST be called BEFORE any handlers are registered. It monkey-patches
# add_handler to wrap ALL subsequently registered handlers with the channel
# membership check. Every handler module below imports `app` from this file,
# so this call must run at import time of this module, before any handler
# module is imported.
apply_guard(app)
