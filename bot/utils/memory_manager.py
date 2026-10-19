"""
Memory Manager Module - High-Scale Resource Management
=======================================================
Manages memory, sessions, and cache for 10,000+ concurrent users.

Features:
- Active session cleanup with TTL
- Periodic garbage collection
- Cache size monitoring
- Memory usage tracking
- Automatic cleanup of stale data
"""

import gc
import os
import sys
import asyncio
import logging
import time
from typing import Dict, Any, Optional, Set
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

MAX_ACTIVE_SESSIONS = 50000
SESSION_TTL_SECONDS = 3600
MAX_CACHE_SIZE_MB = 500
CLEANUP_INTERVAL_SECONDS = 300
MAX_PENDING_SESSIONS = 100000
PENDING_SESSION_TTL_HOURS = 48

_active_sessions: Dict[int, Dict[str, Any]] = {}
_session_last_activity: Dict[int, float] = {}
_cleanup_task: Optional[asyncio.Task] = None
_manager_lock = asyncio.Lock()
_stats = {
    "sessions_cleaned": 0,
    "gc_runs": 0,
    "last_cleanup": None,
    "peak_sessions": 0
}

BACKUP_INTERVAL_SECONDS = int(os.environ.get("DB_BACKUP_INTERVAL_SECONDS", "3600"))
RETENTION_CLEANUP_INTERVAL_SECONDS = int(os.environ.get("DB_RETENTION_CLEANUP_INTERVAL_SECONDS", "86400"))
LOG_RETENTION_DAYS = int(os.environ.get("DB_LOG_RETENTION_DAYS", "90"))
_last_db_backup: float = 0.0
_last_retention_cleanup: float = 0.0


async def register_session(user_id: int, session_data: Dict[str, Any]) -> bool:
    """Register an active session."""
    async with _manager_lock:
        _active_sessions[user_id] = session_data
        _session_last_activity[user_id] = time.time()
        
        current_count = len(_active_sessions)
        if current_count > _stats["peak_sessions"]:
            _stats["peak_sessions"] = current_count
        
        if current_count >= MAX_ACTIVE_SESSIONS:
            await _evict_oldest_sessions(count=100)
        
        return True


async def update_session_activity(user_id: int) -> None:
    """Update last activity timestamp for a session."""
    if user_id in _active_sessions:
        _session_last_activity[user_id] = time.time()


async def get_session(user_id: int) -> Optional[Dict[str, Any]]:
    """Get active session data."""
    async with _manager_lock:
        if user_id in _active_sessions:
            _session_last_activity[user_id] = time.time()
            return _active_sessions[user_id]
        return None


async def remove_session(user_id: int) -> bool:
    """Remove an active session."""
    async with _manager_lock:
        if user_id in _active_sessions:
            del _active_sessions[user_id]
            _session_last_activity.pop(user_id, None)
            return True
        return False


async def _evict_oldest_sessions(count: int = 100) -> int:
    """Evict oldest sessions based on last activity."""
    if not _session_last_activity:
        return 0
    
    sorted_sessions = sorted(
        _session_last_activity.items(),
        key=lambda x: x[1]
    )
    
    evicted = 0
    for user_id, _ in sorted_sessions[:count]:
        if user_id in _active_sessions:
            del _active_sessions[user_id]
            del _session_last_activity[user_id]
            evicted += 1
    
    logger.info(f"Evicted {evicted} oldest sessions")
    return evicted


async def cleanup_expired_sessions() -> int:
    """Clean up sessions that have exceeded TTL."""
    async with _manager_lock:
        now = time.time()
        cutoff = now - SESSION_TTL_SECONDS
        
        expired_users = [
            user_id for user_id, last_activity in _session_last_activity.items()
            if last_activity < cutoff
        ]
        
        for user_id in expired_users:
            _active_sessions.pop(user_id, None)
            _session_last_activity.pop(user_id, None)
        
        cleaned = len(expired_users)
        if cleaned > 0:
            _stats["sessions_cleaned"] += cleaned
            logger.info(f"Cleaned up {cleaned} expired sessions")
        
        return cleaned


async def run_garbage_collection() -> Dict[str, Any]:
    """Run Python garbage collection and return stats."""
    gc.collect()
    _stats["gc_runs"] += 1
    
    gc_stats = {
        "gc_runs_total": _stats["gc_runs"],
        "gc_counts": gc.get_count(),
        "gc_threshold": gc.get_threshold(),
        "objects_collected": gc.collect(0)
    }
    
    return gc_stats


def get_memory_usage() -> Dict[str, Any]:
    """Get current memory usage statistics."""
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        max_rss = usage.ru_maxrss / 1024
    except ImportError:
        max_rss = 0
    
    active_sessions_size = sys.getsizeof(_active_sessions)
    for k, v in _active_sessions.items():
        active_sessions_size += sys.getsizeof(k) + sys.getsizeof(v)
    
    return {
        "active_sessions_count": len(_active_sessions),
        "peak_sessions": _stats["peak_sessions"],
        "active_sessions_size_kb": active_sessions_size / 1024,
        "max_rss_mb": max_rss,
        "sessions_cleaned_total": _stats["sessions_cleaned"],
        "gc_runs_total": _stats["gc_runs"],
        "last_cleanup": _stats["last_cleanup"]
    }


async def cleanup_pending_sessions_storage() -> int:
    """Clean up old SETTLED pending rows from storage.

    P0-1: the old loop deleted ANY row older than the TTL - including rows
    still ``status="pending"`` with a session string and UNPAID balance. That
    silently destroyed real money: the 72h expiry flow, reconcile and the
    safety nets all skip rows that vanish from storage. Only SETTLED rows
    (approved / rejected / expired, or already credited) are pruned here;
    pending rows are NEVER touched - their cleanup belongs exclusively to the
    recovery paths (finalize_expired_sessions / stuck recovery).
    """
    try:
        from bot.utils.storage import get_all_pending_sessions, _storage, save_storage, _storage_lock, _mark_dirty

        with _storage_lock:
            pending = _storage.get("pending_sessions", {})
            now = datetime.now()
            cutoff = now - timedelta(hours=PENDING_SESSION_TTL_HOURS)

            sessions_to_remove = []
            skipped_pending = 0
            for session_id, data in pending.items():
                # NEVER delete a live pending row (P0-1): it may hold an
                # unverified session and an unpaid user.
                if data.get("status", "pending") == "pending" and not data.get("balance_credited"):
                    skipped_pending += 1
                    continue
                created_str = data.get("created_at") or data.get("timestamp")
                if created_str:
                    try:
                        created = datetime.fromisoformat(created_str)
                        if created < cutoff:
                            sessions_to_remove.append(session_id)
                    except (ValueError, TypeError):
                        pass

            for session_id in sessions_to_remove:
                del pending[session_id]

            if sessions_to_remove:
                _mark_dirty("pending_sessions", None)  # deletions → full table sync
                save_storage()
                logger.info(
                    f"Cleaned up {len(sessions_to_remove)} settled pending rows older than "
                    f"{PENDING_SESSION_TTL_HOURS}h ({skipped_pending} live pending rows protected)"
                )

            return len(sessions_to_remove)

    except Exception as e:
        logger.error(f"Error cleaning pending sessions: {e}")
        return 0


async def cleanup_rate_limiter() -> int:
    """Clean up rate limiter data."""
    try:
        from bot.utils.rate_limiter import cleanup_old_rate_limits
        return await cleanup_old_rate_limits()
    except ImportError:
        return 0


async def cleanup_fast_cache() -> int:
    """Purge expired entries from the ultra-fast cache (H5 fix).

    The fast-cache maps had TTL reads but NO eviction: every user ever seen
    stayed cached forever (preload_user_data loaded all of them at boot).
    The periodic sweep here now purges expired entries and re-asserts the
    hard caps every cycle.
    """
    try:
        from bot.utils.fast_cache import cleanup_expired_cache
        return await asyncio.to_thread(cleanup_expired_cache)
    except (ImportError, RuntimeError, asyncio.CancelledError):
        return 0
    except Exception as e:
        logger.debug(f"fast cache cleanup skipped: {e}")
        return 0


async def cleanup_channel_guard_caches() -> int:
    """Purge the channel-guard membership/prompt caches (C2 fix).

    ``cleanup_membership_cache()`` existed but NOTHING ever called it: the
    membership cache grew with every unique user. Wired into the periodic
    cleanup here.
    """
    try:
        from bot.middleware.channel_guard import cleanup_membership_cache
        return await asyncio.to_thread(cleanup_membership_cache)
    except (ImportError, RuntimeError, asyncio.CancelledError):
        return 0
    except Exception as e:
        logger.debug(f"channel guard cleanup skipped: {e}")
        return 0


async def cleanup_redis_cache() -> int:
    """Clean up expired Redis cache entries."""
    try:
        from bot.utils.redis_cache import is_redis_available, get_redis_client
        if not is_redis_available():
            return 0
        client = get_redis_client()
        if client is None:
            return 0
        deleted = 0
        for pattern in ["session:*", "cache:*"]:
            try:
                keys = await client.keys(pattern)
                if not keys:
                    continue
                for key in keys:
                    try:
                        ttl = await client.ttl(key)
                        if ttl is not None and ttl <= 0:
                            await client.delete(key)
                            deleted += 1
                    except Exception:
                        continue
            except Exception:
                continue
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} expired Redis entries")
        return deleted
    except ImportError:
        return 0
    except Exception as e:
        logger.debug(f"Redis cleanup skipped: {e}")
        return 0


async def periodic_cleanup() -> None:
    """Periodic cleanup task that runs in the background."""
    global _last_db_backup, _last_retention_cleanup
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            
            sessions_cleaned = await cleanup_expired_sessions()
            
            pending_cleaned = await cleanup_pending_sessions_storage()
            
            rate_limit_cleaned = await cleanup_rate_limiter()

            cache_cleaned = await cleanup_fast_cache()
            guard_cleaned = await cleanup_channel_guard_caches()

            # H4 support: prune the bounded withdrawals history list so the
            # pending-only invariant + cap hold even if no new request runs
            # the prune path.
            try:
                from bot.data.withdrawals import _prune_withdraw_history_locked, _mark_dirty
                from bot.data.state import _storage_lock
                with _storage_lock:
                    pruned_w = _prune_withdraw_history_locked()
                if pruned_w > 0:
                    _mark_dirty("withdrawals", None)
                    logger.info(f"Pruned {pruned_w} old withdrawal history entries")
            except (ImportError, RuntimeError, asyncio.CancelledError):
                pass
            except Exception as e:
                logger.debug(f"withdrawal prune skipped: {e}")
            
            db_reloaded = False
            try:
                from bot.utils.storage import reload_storage_from_disk
                db_reloaded = await asyncio.to_thread(reload_storage_from_disk)
            except (ImportError, RuntimeError, asyncio.CancelledError):
                pass

            # Periodic FULL consistency sync (every ~10 minutes). The hot save
            # path is incremental (dirty-row only) for speed; this full sync is
            # the safety net that persists ANY row that was ever mutated without
            # a dirty mark (e.g. a legacy direct _storage mutation). Off-loaded
            # to a thread so a large DB never freezes the event loop.
            #
            # OVERWRITE-GUARD (final-review fix): the forced full sync runs
            # ONLY when the in-memory state is TRUSTED. A load failure leaves
            # _storage untrusted (possibly near-empty) while the DB stays
            # populated — full-replacing in that state would wipe real user
            # balances. The _do_save overwrite-guard protects its own paths;
            # this safety net must respect the same contract. When untrusted,
            # _do_save (called by the save paths) routes persistence to the
            # fallback JSON instead.
            try:
                from bot.data.state import _db_sync_all, _storage_lock, _dirty_rows, _storage_trusted
                if _storage_trusted:
                    with _storage_lock:
                        _dirty_rows["users"] = None
                        _dirty_rows["pending_sessions"] = None
                        _dirty_rows["countries"] = None
                        _dirty_rows["settings"] = None
                        _dirty_rows["withdrawals"] = None
                        _dirty_rows["successful_numbers"] = None
                    await asyncio.to_thread(_db_sync_all)
                else:
                    logger.warning("Periodic full sync SKIPPED: in-memory state is untrusted (DB protected)")
            except Exception:
                pass

            now = time.time()

            # Periodic WAL-safe DB backup so even a catastrophic failure or
            # permanent ban never costs more than BACKUP_INTERVAL_SECONDS of data.
            # The backup copies the whole DB file - run it off the event loop so
            # a 60k-row database never freezes the bot while backing up.
            if now - _last_db_backup >= BACKUP_INTERVAL_SECONDS:
                try:
                    from bot.utils.storage import backup_database
                    await asyncio.to_thread(backup_database)
                    _last_db_backup = now
                except (ImportError, RuntimeError, asyncio.CancelledError):
                    pass

            # Retention cleanup of the append-only async log/analytics tables.
            if now - _last_retention_cleanup >= RETENTION_CLEANUP_INTERVAL_SECONDS:
                try:
                    from bot.utils.database import cleanup_old_records
                    await cleanup_old_records(days=LOG_RETENTION_DAYS)
                    _last_retention_cleanup = now
                except (ImportError, RuntimeError, asyncio.CancelledError):
                    pass
            
            gc_stats = await run_garbage_collection()
            
            _stats["last_cleanup"] = datetime.now().isoformat()
            
            if sessions_cleaned > 0 or pending_cleaned > 0 or rate_limit_cleaned > 0:
                logger.info(
                    f"Cleanup completed: {sessions_cleaned} sessions, "
                    f"{pending_cleaned} pending, {rate_limit_cleaned} rate limits"
                )
            if cache_cleaned > 0 or guard_cleaned > 0:
                logger.debug(f"Cache cleanup: {cache_cleaned} fast-cache, {guard_cleaned} membership entries purged")
            if db_reloaded:
                logger.warning("Storage reloaded after DB file replacement")
            
        except asyncio.CancelledError:
            logger.info("Periodic cleanup task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in periodic cleanup: {e}")
            await asyncio.sleep(60)


async def start_memory_manager() -> None:
    """Start the memory manager background task."""
    global _cleanup_task
    
    if _cleanup_task is not None and not _cleanup_task.done():
        logger.warning("Memory manager already running")
        return
    
    _cleanup_task = asyncio.create_task(periodic_cleanup())
    logger.info("Memory manager started")


async def stop_memory_manager() -> None:
    """Stop the memory manager background task."""
    global _cleanup_task
    
    if _cleanup_task is not None:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None
        logger.info("Memory manager stopped")


def get_manager_stats() -> Dict[str, Any]:
    """Get memory manager statistics."""
    return {
        **get_memory_usage(),
        "cleanup_interval_seconds": CLEANUP_INTERVAL_SECONDS,
        "session_ttl_seconds": SESSION_TTL_SECONDS,
        "max_active_sessions": MAX_ACTIVE_SESSIONS,
        "manager_running": _cleanup_task is not None and not _cleanup_task.done()
    }


async def force_cleanup() -> Dict[str, Any]:
    """Force an immediate cleanup cycle."""
    results = {
        "sessions_cleaned": await cleanup_expired_sessions(),
        "pending_cleaned": await cleanup_pending_sessions_storage(),
        "rate_limits_cleaned": await cleanup_rate_limiter(),
        "gc_stats": await run_garbage_collection(),
        "timestamp": datetime.now().isoformat()
    }
    
    _stats["last_cleanup"] = results["timestamp"]
    return results


async def check_memory_pressure() -> bool:
    """Check if memory usage is too high."""
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        current_mb = usage.ru_maxrss / 1024
        
        if current_mb > MAX_CACHE_SIZE_MB * 2:
            logger.warning(f"High memory pressure: {current_mb:.1f} MB")
            return True
            
    except ImportError:
        pass
    
    if len(_active_sessions) > MAX_ACTIVE_SESSIONS * 0.9:
        logger.warning(f"High session count: {len(_active_sessions)}")
        return True
    
    return False


async def emergency_cleanup() -> Dict[str, Any]:
    """Emergency cleanup when memory pressure is detected."""
    logger.warning("Running emergency cleanup due to memory pressure")
    
    evicted = await _evict_oldest_sessions(count=500)
    
    gc.collect(2)
    
    results = await force_cleanup()
    results["emergency_evicted"] = evicted
    
    return results


print("✅ Memory Manager Module Loaded")
print(f"   Max active sessions: {MAX_ACTIVE_SESSIONS}")
print(f"   Session TTL: {SESSION_TTL_SECONDS}s")
print(f"   Cleanup interval: {CLEANUP_INTERVAL_SECONDS}s")
