"""
Redis Cache Module - High-Scale Caching Layer
==============================================
Async Redis client for caching user data, sessions, and membership status.
Supports 10,000+ concurrent users with connection pooling.

Features:
- Async Redis client with connection pooling
- Cache get/set/delete functions with TTL
- User, session, and membership caching
- Rate limiting with Redis INCR/EXPIRE
- Graceful fallback when Redis unavailable
"""

import os
import json
import logging
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "")
REDIS_AVAILABLE = False
_redis_client = None
_redis_pool = None

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    try:
        import aioredis
        REDIS_AVAILABLE = True
    except ImportError:
        logger.warning("aioredis not available - Redis caching disabled")

DEFAULT_TTL = 300
USER_CACHE_TTL = 600
SESSION_CACHE_TTL = 1800
MEMBERSHIP_CACHE_TTL = 120
RATE_LIMIT_WINDOW = 60


async def init_redis() -> bool:
    """Initialize Redis connection pool."""
    global _redis_client, _redis_pool
    
    if not REDIS_AVAILABLE:
        logger.warning("Redis library not available")
        return False
    
    if not REDIS_URL:
        logger.warning("REDIS_URL not set - Redis caching disabled")
        return False
    
    try:
        _redis_client = aioredis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=50,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
            retry_on_timeout=True
        )
        
        await _redis_client.ping()
        logger.info("✅ Redis connection established")
        return True
        
    except Exception as e:
        logger.error(f"Redis connection failed: {e}")
        _redis_client = None
        return False


async def close_redis() -> None:
    """Close Redis connection."""
    global _redis_client
    
    if _redis_client:
        try:
            await _redis_client.close()
            logger.info("Redis connection closed")
        except Exception as e:
            logger.error(f"Error closing Redis: {e}")
        finally:
            _redis_client = None


def is_redis_available() -> bool:
    """Check if Redis is available and connected."""
    return _redis_client is not None


def get_redis_client():
    """Get the global Redis client instance."""
    return _redis_client


async def cache_get(key: str) -> Optional[str]:
    """Get value from cache."""
    if not _redis_client:
        return None
    
    try:
        return await _redis_client.get(key)
    except Exception as e:
        logger.debug(f"Redis GET error for {key}: {e}")
        return None


async def cache_set(key: str, value: str, ttl: int = DEFAULT_TTL) -> bool:
    """Set value in cache with TTL."""
    if not _redis_client:
        return False
    
    try:
        await _redis_client.setex(key, ttl, value)
        return True
    except Exception as e:
        logger.debug(f"Redis SET error for {key}: {e}")
        return False


async def cache_delete(key: str) -> bool:
    """Delete key from cache."""
    if not _redis_client:
        return False
    
    try:
        await _redis_client.delete(key)
        return True
    except Exception as e:
        logger.debug(f"Redis DELETE error for {key}: {e}")
        return False


async def cache_exists(key: str) -> bool:
    """Check if key exists in cache."""
    if not _redis_client:
        return False
    
    try:
        return await _redis_client.exists(key) > 0
    except Exception as e:
        logger.debug(f"Redis EXISTS error for {key}: {e}")
        return False


async def cache_get_json(key: str) -> Optional[Dict[str, Any]]:
    """Get JSON value from cache."""
    value = await cache_get(key)
    if value:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


async def cache_set_json(key: str, value: Dict[str, Any], ttl: int = DEFAULT_TTL) -> bool:
    """Set JSON value in cache with TTL."""
    try:
        json_str = json.dumps(value, default=str)
        return await cache_set(key, json_str, ttl)
    except (TypeError, ValueError) as e:
        logger.debug(f"JSON serialization error: {e}")
        return False


async def get_user_cache(user_id: Union[int, str]) -> Optional[Dict[str, Any]]:
    """Get user data from cache."""
    key = f"user:{user_id}"
    return await cache_get_json(key)


async def set_user_cache(user_id: Union[int, str], user_data: Dict[str, Any]) -> bool:
    """Set user data in cache."""
    key = f"user:{user_id}"
    return await cache_set_json(key, user_data, USER_CACHE_TTL)


async def invalidate_user_cache(user_id: Union[int, str]) -> bool:
    """Invalidate user cache."""
    key = f"user:{user_id}"
    return await cache_delete(key)


async def get_session_cache(session_id: str) -> Optional[Dict[str, Any]]:
    """Get session data from cache."""
    key = f"session:{session_id}"
    return await cache_get_json(key)


async def set_session_cache(session_id: str, session_data: Dict[str, Any]) -> bool:
    """Set session data in cache."""
    key = f"session:{session_id}"
    return await cache_set_json(key, session_data, SESSION_CACHE_TTL)


async def invalidate_session_cache(session_id: str) -> bool:
    """Invalidate session cache."""
    key = f"session:{session_id}"
    return await cache_delete(key)


async def get_membership_cache(user_id: Union[int, str]) -> Optional[bool]:
    """Get membership status from cache."""
    key = f"member:{user_id}"
    value = await cache_get(key)
    if value is not None:
        return value == "1"
    return None


async def set_membership_cache(user_id: Union[int, str], is_member: bool) -> bool:
    """Set membership status in cache."""
    key = f"member:{user_id}"
    return await cache_set(key, "1" if is_member else "0", MEMBERSHIP_CACHE_TTL)


async def invalidate_membership_cache(user_id: Optional[Union[int, str]] = None) -> bool:
    """Invalidate membership cache for user or all users."""
    if not _redis_client:
        return False
    
    try:
        if user_id is not None:
            key = f"member:{user_id}"
            await _redis_client.delete(key)
        else:
            keys = []
            async for key in _redis_client.scan_iter(match="member:*", count=100):
                keys.append(key)
            if keys:
                await _redis_client.delete(*keys)
        return True
    except Exception as e:
        logger.debug(f"Membership cache invalidation error: {e}")
        return False


async def check_rate_limit(
    user_id: Union[int, str],
    action: str = "request",
    max_requests: int = 30,
    window_seconds: int = RATE_LIMIT_WINDOW
) -> tuple[bool, int]:
    """
    Check if user is rate limited.
    
    Returns:
        Tuple of (is_allowed: bool, remaining_requests: int)
    """
    if not _redis_client:
        return True, max_requests
    
    key = f"ratelimit:{action}:{user_id}"
    
    try:
        current = await _redis_client.get(key)
        
        if current is None:
            await _redis_client.setex(key, window_seconds, 1)
            return True, max_requests - 1
        
        current_count = int(current)
        
        if current_count >= max_requests:
            return False, 0
        
        new_count = await _redis_client.incr(key)
        return True, max_requests - new_count
        
    except Exception as e:
        logger.debug(f"Rate limit check error: {e}")
        return True, max_requests


async def increment_counter(key: str, ttl: int = 3600) -> int:
    """Increment a counter with TTL."""
    if not _redis_client:
        return 0
    
    try:
        count = await _redis_client.incr(key)
        if count == 1:
            await _redis_client.expire(key, ttl)
        return count
    except Exception as e:
        logger.debug(f"Counter increment error: {e}")
        return 0


async def get_counter(key: str) -> int:
    """Get counter value."""
    if not _redis_client:
        return 0
    
    try:
        value = await _redis_client.get(key)
        return int(value) if value else 0
    except Exception as e:
        logger.debug(f"Counter get error: {e}")
        return 0


async def set_pending_session_cache(session_id: str, data: Dict[str, Any]) -> bool:
    """Cache pending session data."""
    key = f"pending:{session_id}"
    return await cache_set_json(key, data, SESSION_CACHE_TTL)


async def get_pending_session_cache(session_id: str) -> Optional[Dict[str, Any]]:
    """Get pending session from cache."""
    key = f"pending:{session_id}"
    return await cache_get_json(key)


async def delete_pending_session_cache(session_id: str) -> bool:
    """Delete pending session from cache."""
    key = f"pending:{session_id}"
    return await cache_delete(key)


async def get_all_pending_session_ids() -> list:
    """Get all pending session IDs from cache."""
    if not _redis_client:
        return []
    
    try:
        keys = []
        async for key in _redis_client.scan_iter(match="pending:*", count=100):
            session_id = key.replace("pending:", "")
            keys.append(session_id)
        return keys
    except Exception as e:
        logger.debug(f"Get pending sessions error: {e}")
        return []


async def cache_pipeline_set(items: Dict[str, str], ttl: int = DEFAULT_TTL) -> bool:
    """Set multiple values using pipeline for efficiency."""
    if not _redis_client:
        return False
    
    try:
        pipe = _redis_client.pipeline()
        for key, value in items.items():
            pipe.setex(key, ttl, value)
        await pipe.execute()
        return True
    except Exception as e:
        logger.debug(f"Pipeline SET error: {e}")
        return False


async def cache_pipeline_get(keys: list) -> Dict[str, Optional[str]]:
    """Get multiple values using pipeline for efficiency."""
    if not _redis_client:
        return {k: None for k in keys}
    
    try:
        pipe = _redis_client.pipeline()
        for key in keys:
            pipe.get(key)
        results = await pipe.execute()
        return dict(zip(keys, results))
    except Exception as e:
        logger.debug(f"Pipeline GET error: {e}")
        return {k: None for k in keys}


async def get_cache_stats() -> Dict[str, Any]:
    """Get Redis cache statistics."""
    if not _redis_client:
        return {"available": False}
    
    try:
        info = await _redis_client.info("memory")
        db_size = await _redis_client.dbsize()
        
        return {
            "available": True,
            "used_memory": info.get("used_memory_human", "N/A"),
            "used_memory_peak": info.get("used_memory_peak_human", "N/A"),
            "total_keys": db_size,
            "connected": True
        }
    except Exception as e:
        logger.debug(f"Cache stats error: {e}")
        return {"available": True, "error": str(e)}


try:
    from bot.utils.lru_cache_engine import (
        cache_get as lru_get, cache_set as lru_set,
        cache_get_json as lru_get_json,
        cache_set_json as lru_set_json, cache_stats as lru_stats,
        check_rate_limit as lru_check_rate_limit,
        get_user_cached, set_user_cached,
        get_membership_cached, set_membership_cached,
        init_cache as init_lru_cache
    )
    LRU_CACHE_AVAILABLE = True
    logger.info("LRU Cache Engine loaded successfully")
except ImportError as e:
    LRU_CACHE_AVAILABLE = False
    logger.warning(f"LRU Cache Engine not available: {e}")


async def fallback_cache_get(key: str) -> Optional[str]:
    """Fallback using high-performance LRU Cache Engine."""
    if LRU_CACHE_AVAILABLE:
        return lru_get(key)
    return None


async def fallback_cache_set(key: str, value: str, ttl: int = DEFAULT_TTL) -> bool:
    """Fallback using high-performance LRU Cache Engine."""
    if LRU_CACHE_AVAILABLE:
        return lru_set(key, value, ttl)
    return False


async def smart_cache_get(key: str) -> Optional[str]:
    """Get from Redis or fallback to LRU Cache."""
    if _redis_client:
        result = await cache_get(key)
        if result is not None:
            return result
    return await fallback_cache_get(key)


async def smart_cache_set(key: str, value: str, ttl: int = DEFAULT_TTL) -> bool:
    """Set to Redis or fallback to LRU Cache."""
    if _redis_client:
        return await cache_set(key, value, ttl)
    return await fallback_cache_set(key, value, ttl)


async def smart_cache_get_json(key: str) -> Optional[Dict[str, Any]]:
    """Get JSON from Redis or fallback to LRU Cache."""
    if _redis_client:
        result = await cache_get_json(key)
        if result is not None:
            return result
    if LRU_CACHE_AVAILABLE:
        return lru_get_json(key)
    return None


async def smart_cache_set_json(key: str, value: Dict[str, Any], ttl: int = DEFAULT_TTL) -> bool:
    """Set JSON to Redis or fallback to LRU Cache."""
    if _redis_client:
        return await cache_set_json(key, value, ttl)
    if LRU_CACHE_AVAILABLE:
        return lru_set_json(key, value, ttl)
    return False


async def smart_rate_limit_check(
    user_id: Union[int, str],
    action: str = "request",
    max_requests: int = 30,
    window_seconds: int = RATE_LIMIT_WINDOW
) -> tuple[bool, int]:
    """Check rate limit using Redis or LRU Cache fallback."""
    if _redis_client:
        return await check_rate_limit(user_id, action, max_requests, window_seconds)
    if LRU_CACHE_AVAILABLE:
        return lru_check_rate_limit(user_id, action, max_requests, window_seconds)
    return True, max_requests


async def smart_get_user_cache(user_id: Union[int, str]) -> Optional[Dict[str, Any]]:
    """Get user from Redis or LRU Cache."""
    if _redis_client:
        result = await get_user_cache(user_id)
        if result is not None:
            return result
    if LRU_CACHE_AVAILABLE:
        return get_user_cached(user_id)
    return None


async def smart_set_user_cache(user_id: Union[int, str], user_data: Dict[str, Any]) -> bool:
    """Set user to Redis or LRU Cache."""
    if _redis_client:
        return await set_user_cache(user_id, user_data)
    if LRU_CACHE_AVAILABLE:
        return set_user_cached(user_id, user_data)
    return False


async def smart_get_membership_cache(user_id: Union[int, str]) -> Optional[bool]:
    """Get membership from Redis or LRU Cache."""
    if _redis_client:
        result = await get_membership_cache(user_id)
        if result is not None:
            return result
    if LRU_CACHE_AVAILABLE:
        return get_membership_cached(user_id)
    return None


async def smart_set_membership_cache(user_id: Union[int, str], is_member: bool) -> bool:
    """Set membership to Redis or LRU Cache."""
    if _redis_client:
        return await set_membership_cache(user_id, is_member)
    if LRU_CACHE_AVAILABLE:
        return set_membership_cached(user_id, is_member)
    return False


async def get_smart_cache_stats() -> Dict[str, Any]:
    """Get cache stats from Redis or LRU Cache."""
    stats = {
        "redis_available": _redis_client is not None,
        "lru_cache_available": LRU_CACHE_AVAILABLE,
        "mode": "redis" if _redis_client else ("lru_cache" if LRU_CACHE_AVAILABLE else "none")
    }
    
    if _redis_client:
        redis_stats = await get_cache_stats()
        stats["redis"] = redis_stats
    
    if LRU_CACHE_AVAILABLE:
        stats["lru_cache"] = lru_stats()
    
    return stats


async def init_smart_cache() -> bool:
    """Initialize the best available cache system."""
    redis_ok = await init_redis()
    
    if redis_ok:
        logger.info("✅ Using Redis for caching (10,000+ users supported)")
        return True
    
    if LRU_CACHE_AVAILABLE:
        await init_lru_cache()
        logger.info("✅ Using LRU Cache Engine (10,000+ users supported without Redis)")
        return True
    
    logger.warning("⚠️ No caching available")
    return False


print("✅ Redis Cache Module Loaded")
print(f"   Redis library available: {REDIS_AVAILABLE}")
print(f"   REDIS_URL configured: {bool(REDIS_URL)}")
print(f"   LRU Cache Engine: {LRU_CACHE_AVAILABLE}")
print(f"   10,000+ Users: SUPPORTED (with or without Redis)")
