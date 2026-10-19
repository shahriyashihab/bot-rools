"""Main entry point - run with ``python -m bot`` or ``python bot/__main__.py``."""
import asyncio
import atexit
import logging
import os
import signal
import sys
import time

try:
    import fcntl  # Unix-only; unavailable on Windows
except ImportError:  # pragma: no cover - platform-specific
    fcntl = None

try:
    import msvcrt  # Windows-only; unavailable on Unix
except ImportError:  # pragma: no cover - platform-specific
    msvcrt = None

from bot import config
from bot.config import ADMIN_ID

logger = logging.getLogger(__name__)


def _pid_is_alive(pid):
    """Return True when a process with this PID exists.

    ``os.kill(pid, 0)`` is a pure liveness probe on both POSIX and Windows
    (CPython implements sig=0 as an OpenProcess existence check on Windows).
    On Windows an ALIVE process owned by another user can surface as
    PermissionError instead of success - treat that as alive rather than
    steal a live instance's lock.
    """
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _read_lock_pid(lock_path):
    """Read the PID recorded in ``lock_path`` (None if absent/unparseable).

    The lock writer writes its PID right after the atomic O_EXCL create, so a
    file that exists but is momentarily empty may belong to a process still
    mid-acquire - poll briefly before declaring it stale.
    """
    for _ in range(5):
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                try:
                    return int(content)
                except (ValueError, TypeError):
                    return None
        except (IOError, OSError, PermissionError):
            return None
        time.sleep(0.05)
    return None


def _create_lock_exclusive(lock_path):
    """Atomically create+own the lock file. Returns the fd, or None on conflict/error.

    ``O_CREAT | O_EXCL`` fails with FileExistsError if the file already
    exists - the atomic create-exclusive avoids the old truncate-before-lock
    race where two starting processes could both truncate the same file and
    both believe they own the lock.
    """
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return None
    except (IOError, OSError, PermissionError):
        return None
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    except (IOError, OSError):
        try:
            os.close(fd)
        except Exception:
            pass
        return None
    return fd


def _acquire_pid_lock():
    """Acquire a PID lock to prevent double bot instances.

    STALE-LOCK SAFE + RACE SAFE (P-fix): the old implementation opened the
    candidate with ``open(candidate, "w")`` (TRUNCATING any existing lock file
    BEFORE locking), then locked byte 0 and only afterwards wrote the PID. A
    crash could leave a stale 0-byte lock file whose lock then failed -
    producing the misleading "Another bot instance is already running" FATAL
    with no live instance to blame. Two processes could also race through the
    truncate-before-lock window and both start.

    The new implementation:
      1. Opens the candidate ONLY via ``O_CREAT | O_EXCL`` (atomic create -
         never truncates an existing file, so no truncate race).
      2. When the file already exists it reads the recorded PID and verifies
         liveness with ``os.kill(pid, 0)``. An alive holder -> FATAL exit
         naming the PID. A dead PID (stale lock from a crash) or an empty
         leftover -> remove + retry the atomic create once.
      3. After acquiring, writes the PID immediately and registers atexit
         removal (graceful shutdown cleans the lock file).

    The lock path is anchored to the project root (this file is
    bot/__main__.py, so the project root is two levels up), NOT to the current
    working directory. A relative path would silently create the lock in a
    different location when the bot is started from another directory, allowing
    two instances to run at once.

    If the project directory is not writable (read-only deploys), fall back to
    the OS temp directory (MEDIUM-25: /tmp does not exist on Windows) so the
    bot still starts while keeping single-instance protection.
    """
    if fcntl is None and msvcrt is None:
        print("WARNING: No file-locking module available; "
              "running without single-instance protection (a second instance "
              "is NOT prevented).")
        return

    import tempfile
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lock_candidates = [
        os.path.join(project_root, "bot", "bot.lock"),
        os.path.join(tempfile.gettempdir(), "telegram_session_bot.lock"),
    ]

    def fatal_held(pid, candidate):
        print(f"FATAL: Another bot instance is already running (PID {pid}, lock: {candidate})")
        print("If you are sure no other instance is running, delete the lock file and try again.")
        sys.exit(1)

    lock_fd = None
    lock_path = None
    for candidate in lock_candidates:
        fd = _create_lock_exclusive(candidate)
        if fd is not None:
            lock_fd = fd
            lock_path = candidate
            break
        # File exists: another instance may hold it. Verify from the PID.
        holder_pid = _read_lock_pid(candidate)
        if holder_pid is not None and holder_pid > 0 and _pid_is_alive(holder_pid):
            fatal_held(holder_pid, candidate)
        # Stale lock (dead PID or empty leftover from a crash): remove and
        # retry the atomic create ONCE.
        try:
            os.remove(candidate)
        except (IOError, OSError, PermissionError):
            continue
        fd = _create_lock_exclusive(candidate)
        if fd is not None:
            lock_fd = fd
            lock_path = candidate
            break
        # The retry lost the race: another process created the file between
        # our remove and retry. Verify it is live; an alive holder (or one we
        # cannot prove dead - e.g. still mid-acquire) means FATAL, never two
        # running instances.
        holder_pid = _read_lock_pid(candidate)
        if holder_pid is not None and holder_pid > 0 and _pid_is_alive(holder_pid):
            fatal_held(holder_pid, candidate)
        print(f"FATAL: Another bot instance is starting (lock file contested: {candidate})")
        print("If you are sure no other instance is running, delete the lock file and try again.")
        sys.exit(1)

    if lock_fd is None:
        print("WARNING: Could not create a PID lock file; "
              "running WITHOUT single-instance protection (a second instance "
              "is NOT prevented).")
        return

    def release_lock():
        # Close FIRST on Windows: deleting an open file can raise
        # PermissionError, which would leave a stale lock behind.
        try:
            os.close(lock_fd)
        except Exception:
            pass
        try:
            if lock_path:
                os.unlink(lock_path)
        except Exception:
            pass

    atexit.register(release_lock)


def _preload_caches():
    """Preload templates/user data/withdraw status for ultra-fast response."""
    from bot.utils.fast_cache import preload_templates, preload_user_data, refresh_withdraw_status

    logger.info("Preloading templates for ultra-fast response...")
    preload_templates()
    logger.info("Templates cached")
    logger.info("Preloading user data into memory cache...")
    preload_user_data()
    logger.info("User data cached")
    logger.info("Preloading withdraw status...")
    refresh_withdraw_status()
    logger.info("All data cached - Response time target: < 1ms")


def main():
    """Run the bot synchronously (Pyrogram Client)."""
    # P7-76 / MEDIUM-22: the storage entry point is now explicit - the
    # import-time call in bot.data.state routes through this idempotent init.
    from bot.data.state import init_storage
    init_storage()
    _acquire_pid_lock()
    _preload_caches()

    logger.info("Bot is starting...")
    # P6-64: even a token PREFIX narrows a brute-force space - log only the
    # length, never any part of the token itself.
    logger.info(f"Bot Token: *** ({len(config.BOT_TOKEN)} chars, configured)")
    logger.info(f"API ID: {config.API_ID}")
    logger.info(f"Admin ID: {ADMIN_ID}")
    logger.info(f"Required Channel: {config.REQUIRED_CHANNEL}")
    logger.info(f"Pending Channel: {config.CHANNEL_SUBMIT}")
    logger.info(f"Verified Channel: {config.CHANNEL_VERIFIED}")
    logger.info(f"Rejected Channel: {config.CHANNEL_REJECTED}")
    logger.info(f"Withdraw Report Channel: {config.CHANNEL_WITHDRAW_REPORT}")

    logger.info("=" * 50)
    logger.info("Bot starting with Pyrogram Client...")
    logger.info("=" * 50)

    async def _main():
        """Main async entry point for the bot with graceful shutdown."""
        from bot.bootstrap.client import app
        from bot.bootstrap.high_scale import initialize_high_scale_systems
        from bot.core.context import cleanup_tasks, shutdown_event
        from bot.lifecycle.cleanup import cleanup_old_sessions
        from bot.lifecycle.shutdown import graceful_shutdown
        from bot.lifecycle.startup import startup

        loop = asyncio.get_event_loop()

        # P5-44: a repeated Ctrl+C used to spawn a SECOND graceful_shutdown
        # while the first one was mid-flight - two concurrent shutdowns
        # cancelled each other's cleanup tasks and could double-close the
        # SQLite store. The event flag makes shutdown single-flight: the first
        # signal starts it, later signals are ignored (they just log).
        shutdown_started = {"flag": False}

        def _request_shutdown(sig_num):
            if shutdown_started["flag"]:
                logger.warning(f"Signal {sig_num} received again - shutdown already in progress")
                return
            shutdown_started["flag"] = True
            # SELF-AUDIT FIX: graceful_shutdown does ``sig.name`` - a bare int
            # from signal.signal has no .name and would crash the shutdown
            # coroutine BEFORE the shutdown event was even set. Convert to the
            # Signals enum first.
            try:
                sig_obj = signal.Signals(int(sig_num))
            except (ValueError, TypeError):
                sig_obj = None
            asyncio.create_task(graceful_shutdown(sig_obj))

        def _signal_handler(sig_num, _frame):
            # Runs in the main thread outside asyncio: hop back into the loop.
            loop.call_soon_threadsafe(_request_shutdown, sig_num)

        # signal.signal works on EVERY platform (including Windows, where
        # loop.add_signal_handler raises NotImplementedError). SIGBREAK is
        # Windows-only (Ctrl+Break), SIGTERM exists on Unix. Missing signals
        # are simply skipped.
        for sig_name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            sig_const = getattr(signal, sig_name, None)
            if sig_const is None:
                continue
            try:
                signal.signal(sig_const, _signal_handler)
            except (OSError, ValueError, NotImplementedError) as sig_err:
                logger.debug(f"Could not register handler for {sig_name}: {sig_err}")

        await app.start()

        storage_name = type(app.storage).__name__
        logger.info("=" * 50)
        logger.info(f"Client storage: {storage_name}")
        if storage_name != "MemoryStorage":
            logger.error(
                "FATAL: Client is NOT using MemoryStorage. "
                "On read-only deploys this crashes with 'attempt to write a readonly database' "
                "and makes all handlers unresponsive. Update to the latest code and restart."
            )
            raise RuntimeError("Client storage must be MemoryStorage (in_memory=True). Update code and restart.")
        logger.info("Client storage OK (MemoryStorage) - in_memory fix active")
        logger.info("=" * 50)

        await startup()

        await initialize_high_scale_systems()

        cleanup_task = asyncio.create_task(cleanup_old_sessions())
        cleanup_tasks.append(cleanup_task)

        from bot.lifecycle.daily_report import daily_report_loop
        daily_report_task = asyncio.create_task(daily_report_loop())
        cleanup_tasks.append(daily_report_task)

        logger.info("Bot is ready to receive messages!")

        try:
            await shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await app.stop()
            logger.info("Bot stopped")

    try:
        from bot.bootstrap.client import app
        app.run(_main())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    main()
