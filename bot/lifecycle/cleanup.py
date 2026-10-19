"""Periodic cleanup of old session files and stale data."""
import asyncio
import glob
import logging
import os
import time

from bot.core.context import shutdown_event

logger = logging.getLogger(__name__)

# P5-47: the cleanup dirs are anchored to the PROJECT ROOT (bot/lifecycle/
# cleanup.py -> two levels up), not to the current working directory. Started
# from another CWD, the relative "sessions"/"verified_sessions" paths silently
# pointed at non-existent folders (nothing cleaned, ever) - and a CWD that
# happened to contain different folders of the same names could have been
# swept instead.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Only raw pyrogram session files (regenerable from session strings) are
# cleaned, and only after this retention window. Export artifacts (.SESSION /
# .json written by session_io) hold the actual verification payloads and are
# NEVER deleted here - they are the protection against data loss when the bot
# is banned/offline for a long time.
SESSION_FILE_RETENTION_HOURS = int(os.environ.get("SESSION_FILE_RETENTION_HOURS", "720"))

# P5-47: 'bot' was removed from the sweep - it is the SOURCE TREE, not a
# session dir; a stray file there is a bug to fix, not data to silently
# delete after 30 days.
_CLEANUP_DIRS = ("sessions", "verified_sessions")


async def cleanup_old_sessions():
    """Periodically clean up old session files and data."""
    from bot.utils.storage import cleanup_old_data

    while not shutdown_event.is_set():
        try:
            # Run off the event loop: at the successful_numbers cap the prune
            # sorts ~500k entries under _storage_lock (~1-2s) - never stall
            # the loop that drives live verifications.
            cleaned_count = await asyncio.to_thread(cleanup_old_data, 30)
            if cleaned_count > 0:
                logger.info(f"Cleaned up {cleaned_count} old data entries")

            total_removed = 0
            for dir_name in _CLEANUP_DIRS:
                dir_path = os.path.join(_PROJECT_ROOT, dir_name)
                if os.path.isdir(dir_path):
                    for pattern in ["*.session", "*.session-journal"]:
                        files = glob.glob(os.path.join(dir_path, pattern))
                        for f in files:
                            try:
                                mtime = os.path.getmtime(f)
                                age_seconds = time.time() - mtime
                                if age_seconds > SESSION_FILE_RETENTION_HOURS * 3600:
                                    os.remove(f)
                                    total_removed += 1
                            except OSError:
                                pass
            if total_removed > 0:
                logger.info(f"Cleaned up {total_removed} pyrogram session files (older than {SESSION_FILE_RETENTION_HOURS} hours)")

            await asyncio.sleep(3600)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")
            await asyncio.sleep(300)
