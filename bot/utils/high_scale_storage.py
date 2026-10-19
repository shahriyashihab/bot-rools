"""
High-Scale Storage Module - SQLite-Backed Storage
==================================================
SQLite database-backed storage with optional Redis caching.
JSON file storage is the final fallback.

Features:
- SQLite backend for users, sessions, pending_sessions
- Redis cache layer for hot data (optional)
- Async/await throughout
- Migration from JSON to database
- Fallback to JSON storage if DB unavailable
"""

import json
import logging
from typing import Dict, Any, Optional, List, Union

logger = logging.getLogger(__name__)

_initialized = False
_use_database = False
_db_backend = None
_fallback_to_json = True


async def init_high_scale_storage() -> bool:
    """Initialize high-scale storage with database backend and Redis cache."""
    global _initialized, _use_database, _db_backend

    try:
        from bot.utils.redis_cache import init_redis
        redis_ok = await init_redis()
        if redis_ok:
            logger.info("Redis cache initialized for high-scale storage")
        else:
            logger.warning("Redis not available, using in-memory cache")
    except ImportError:
        logger.warning("Redis module not available")

    try:
        from bot.utils.database import init_database, get_backend
        db_ok = await init_database()
        if db_ok:
            _use_database = True
            _db_backend = get_backend()
            logger.info(f"Database initialized for high-scale storage (backend: {_db_backend})")
        else:
            logger.warning("Database not available, using JSON fallback")
    except ImportError:
        logger.warning("Database module not available")

    _initialized = True
    return _use_database


async def get_user(user_id: Union[int, str]) -> Dict[str, Any]:
    """
    Get user data with cache-first strategy.
    1. Check Redis cache
    2. If miss, check SQLite database
    3. If miss, check JSON storage
    4. Create new user if not found
    """
    user_id = str(user_id)

    try:
        from bot.utils.redis_cache import get_user_cache, set_user_cache, is_redis_available

        if is_redis_available():
            cached = await get_user_cache(user_id)
            if cached:
                return cached
    except ImportError:
        pass

    if _use_database:
        user_data = await _get_user_from_db(user_id)
        if user_data:
            try:
                from bot.utils.redis_cache import set_user_cache
                await set_user_cache(user_id, user_data)
            except ImportError:
                pass
            return user_data

    from bot.utils.storage import get_user_info
    user_data = get_user_info(int(user_id))

    try:
        from bot.utils.redis_cache import set_user_cache
        await set_user_cache(user_id, user_data)
    except ImportError:
        pass

    return user_data


async def _get_user_from_db(user_id: str) -> Optional[Dict[str, Any]]:
    """Get user from SQLite database."""
    try:
        from bot.utils.database import get_user_by_id
        return await get_user_by_id(int(user_id))
    except Exception as e:
        logger.debug(f"DB user fetch error: {e}")
    return None


async def update_user(user_id: Union[int, str], **kwargs) -> bool:
    """Update user data in all storage layers."""
    user_id = str(user_id)

    from bot.utils.storage import update_user_info
    update_user_info(int(user_id), **kwargs)

    try:
        from bot.utils.redis_cache import invalidate_user_cache
        await invalidate_user_cache(user_id)
    except ImportError:
        pass

    if _use_database:
        await _upsert_user_to_db(user_id, kwargs)

    return True


async def _upsert_user_to_db(user_id: str, data: Dict[str, Any]) -> bool:
    """Upsert user to SQLite database."""
    try:
        from bot.utils.database import upsert_user
        return await upsert_user(int(user_id), data)
    except Exception as e:
        logger.debug(f"DB user upsert error: {e}")
    return False


async def get_pending_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Get pending session with cache-first strategy."""
    try:
        from bot.utils.redis_cache import get_pending_session_cache
        cached = await get_pending_session_cache(session_id)
        if cached:
            return cached
    except ImportError:
        pass

    if _use_database:
        session = await _get_pending_session_from_db(session_id)
        if session:
            try:
                from bot.utils.redis_cache import set_pending_session_cache
                await set_pending_session_cache(session_id, session)
            except ImportError:
                pass
            return session

    from bot.utils.storage import get_pending_session as storage_get_pending
    return storage_get_pending(session_id)


async def _get_pending_session_from_db(session_id: str) -> Optional[Dict[str, Any]]:
    """Get pending session from SQLite database."""
    try:
        from bot.utils.database import get_pending_session_by_id
        return await get_pending_session_by_id(session_id)
    except Exception as e:
        logger.debug(f"DB pending session fetch error: {e}")
    return None


async def add_pending_session(session_id: str, data: Dict[str, Any]) -> bool:
    """Add pending session to all storage layers."""
    from bot.utils.storage import add_pending_session as storage_add_pending
    storage_add_pending(session_id, data)

    try:
        from bot.utils.redis_cache import set_pending_session_cache
        await set_pending_session_cache(session_id, data)
    except ImportError:
        pass

    if _use_database:
        await _insert_pending_session_to_db(session_id, data)

    return True


async def _insert_pending_session_to_db(session_id: str, data: Dict[str, Any]) -> bool:
    """Insert pending session to SQLite database."""
    try:
        from bot.utils.database import upsert_pending_session
        return await upsert_pending_session(session_id, data)
    except Exception as e:
        logger.debug(f"DB pending session insert error: {e}")
    return False


async def update_pending_session(session_id: str, **kwargs) -> bool:
    """Update pending session in all storage layers."""
    from bot.utils.storage import update_pending_session_field
    for key, value in kwargs.items():
        update_pending_session_field(session_id, key, value)

    try:
        from bot.utils.redis_cache import delete_pending_session_cache
        await delete_pending_session_cache(session_id)
    except ImportError:
        pass

    return True


async def get_all_pending_sessions() -> Dict[str, Dict[str, Any]]:
    """Get all pending sessions."""
    from bot.utils.storage import get_all_pending_sessions as storage_get_all
    return storage_get_all()


async def check_membership_cached(user_id: Union[int, str]) -> Optional[bool]:
    """Check membership from cache."""
    try:
        from bot.utils.redis_cache import get_membership_cache
        return await get_membership_cache(user_id)
    except ImportError:
        return None


async def set_membership_cached(user_id: Union[int, str], is_member: bool) -> bool:
    """Set membership in cache."""
    try:
        from bot.utils.redis_cache import set_membership_cache
        return await set_membership_cache(user_id, is_member)
    except ImportError:
        return False


async def invalidate_membership_cached(user_id: Optional[Union[int, str]] = None) -> bool:
    """Invalidate membership cache."""
    try:
        from bot.utils.redis_cache import invalidate_membership_cache
        return await invalidate_membership_cache(user_id)
    except ImportError:
        return False


async def batch_get_users(user_ids: List[Union[int, str]]) -> Dict[str, Dict[str, Any]]:
    """Batch get multiple users efficiently."""
    results = {}

    try:
        from bot.utils.redis_cache import cache_pipeline_get, is_redis_available

        if is_redis_available():
            keys = [f"user:{uid}" for uid in user_ids]
            cached = await cache_pipeline_get(keys)

            missing_ids = []
            for i, uid in enumerate(user_ids):
                key = f"user:{uid}"
                if cached.get(key):
                    try:
                        results[str(uid)] = json.loads(cached[key])
                    except json.JSONDecodeError:
                        missing_ids.append(uid)
                else:
                    missing_ids.append(uid)

            for uid in missing_ids:
                results[str(uid)] = await get_user(uid)
        else:
            for uid in user_ids:
                results[str(uid)] = await get_user(uid)

    except ImportError:
        for uid in user_ids:
            from bot.utils.storage import get_user_info
            results[str(uid)] = get_user_info(int(uid))

    return results


async def batch_update_users(updates: Dict[str, Dict[str, Any]]) -> int:
    """Batch update multiple users efficiently."""
    updated = 0
    for user_id, data in updates.items():
        if await update_user(user_id, **data):
            updated += 1
    return updated


async def migrate_json_to_database() -> Dict[str, Any]:
    """
    Migrate data from JSON storage to SQLite database.
    Returns migration statistics.
    """
    if not _use_database:
        return {"success": False, "error": "Database not available"}

    from bot.utils.storage import _storage
    from bot.utils.database import upsert_user, upsert_pending_session

    stats = {
        "users_migrated": 0,
        "sessions_migrated": 0,
        "errors": [],
        "backend": _db_backend
    }

    users = _storage.get("users", {})
    for user_id, user_data in users.items():
        try:
            await upsert_user(int(user_id), user_data)
            stats["users_migrated"] += 1
        except Exception as e:
            stats["errors"].append(f"User {user_id}: {e}")

    pending = _storage.get("pending_sessions", {})
    for session_id, session_data in pending.items():
        try:
            await upsert_pending_session(session_id, session_data)
            stats["sessions_migrated"] += 1
        except Exception as e:
            stats["errors"].append(f"Session {session_id}: {e}")

    stats["success"] = len(stats["errors"]) == 0
    logger.info(f"Migration completed ({_db_backend}): {stats['users_migrated']} users, {stats['sessions_migrated']} sessions")

    return stats


async def get_storage_stats() -> Dict[str, Any]:
    """Get storage statistics from all layers."""
    stats = {
        "initialized": _initialized,
        "database_enabled": _use_database,
        "database_backend": _db_backend,
        "fallback_to_json": _fallback_to_json
    }

    try:
        from bot.utils.redis_cache import get_cache_stats
        stats["redis"] = await get_cache_stats()
    except ImportError:
        stats["redis"] = {"available": False}

    try:
        from bot.utils.storage import _storage
        stats["json_storage"] = {
            "users_count": len(_storage.get("users", {})),
            "pending_sessions_count": len(_storage.get("pending_sessions", {})),
            "countries_count": len(_storage.get("countries", {}))
        }
    except ImportError:
        stats["json_storage"] = {"error": "storage module not available"}

    if _use_database:
        try:
            from bot.utils.database import get_db_health_stats
            stats["database"] = await get_db_health_stats()
        except ImportError:
            stats["database"] = {"error": "database module not available"}

    return stats


async def close_high_scale_storage() -> None:
    """Close all storage connections."""
    try:
        from bot.utils.redis_cache import close_redis
        await close_redis()
    except ImportError:
        pass

    try:
        from bot.utils.database import close_database
        await close_database()
    except ImportError:
        pass

    logger.info("High-scale storage closed")


print("High-Scale Storage Module Loaded")
print("   Database backend: SQLite")
print("   Redis cache: Available (requires init)")
print("   JSON fallback: Enabled")
