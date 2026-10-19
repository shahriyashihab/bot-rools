"""
Rate Limiter Module - High-Scale Rate Limiting
===============================================
Per-user rate limiting using Redis for 10,000+ concurrent users.

Features:
- Redis-based distributed rate limiting
- Configurable limits per action
- Sliding window rate limiting
- Graceful fallback to in-memory when Redis unavailable
- User-friendly error messages
"""

import time
import asyncio
import logging
from typing import Dict, Optional, Tuple, Union
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps

logger = logging.getLogger(__name__)

DEFAULT_REQUESTS_PER_MINUTE = 30
DEFAULT_REQUESTS_PER_HOUR = 300
BURST_LIMIT = 10
BURST_WINDOW = 5

ACTION_LIMITS = {
    "start": {"per_minute": 5, "per_hour": 60},
    "verify_phone": {"per_minute": 3, "per_hour": 30},
    "verify_otp": {"per_minute": 5, "per_hour": 50},
    "withdraw": {"per_minute": 2, "per_hour": 20},
    "account": {"per_minute": 10, "per_hour": 120},
    "callback": {"per_minute": 30, "per_hour": 500},
    "message": {"per_minute": 20, "per_hour": 300},
    "admin": {"per_minute": 60, "per_hour": 1000},
}

_in_memory_limits: Dict[str, Dict[str, list]] = defaultdict(lambda: defaultdict(list))
_blocked_users: Dict[int, datetime] = {}
_in_memory_lock = asyncio.Lock()


async def check_rate_limit_redis(
    user_id: Union[int, str],
    action: str = "message",
    custom_limits: Optional[Dict[str, int]] = None
) -> Tuple[bool, str, int]:
    """
    Check rate limit using Redis.
    
    Args:
        user_id: User ID to check
        action: Action type (message, verify_phone, etc.)
        custom_limits: Optional custom limits override
        
    Returns:
        Tuple of (is_allowed, error_message, retry_after_seconds)
    """
    try:
        from bot.utils.redis_cache import check_rate_limit, is_redis_available
        
        if not is_redis_available():
            return await check_rate_limit_memory(user_id, action, custom_limits)
        
        limits = custom_limits or ACTION_LIMITS.get(action, {
            "per_minute": DEFAULT_REQUESTS_PER_MINUTE,
            "per_hour": DEFAULT_REQUESTS_PER_HOUR
        })
        
        minute_allowed, minute_remaining = await check_rate_limit(
            user_id, f"{action}_min", limits.get("per_minute", 30), 60
        )
        
        if not minute_allowed:
            return False, _get_rate_limit_message(action, "minute"), 60
        
        hour_allowed, hour_remaining = await check_rate_limit(
            user_id, f"{action}_hour", limits.get("per_hour", 300), 3600
        )
        
        if not hour_allowed:
            return False, _get_rate_limit_message(action, "hour"), 3600
        
        return True, "", 0
        
    except Exception as e:
        logger.debug(f"Redis rate limit error, falling back to memory: {e}")
        return await check_rate_limit_memory(user_id, action, custom_limits)


async def check_rate_limit(
    user_id: Union[int, str],
    action: str = "message",
    custom_limits: Optional[Dict[str, int]] = None
) -> Tuple[bool, str, int]:
    """Unified rate-limit entry point (Redis when available, else memory).

    P7-66: this module was previously dead code - no handler ever called it,
    so /start, OTP submit and callbacks had NO per-user flood control at all.
    Handlers now call ``check_rate_limit`` for the abusive paths (see
    handlers/cap.py and the OTP/phone entry points).
    """
    try:
        from bot.utils.redis_cache import is_redis_available
        if is_redis_available():
            return await check_rate_limit_redis(user_id, action, custom_limits)
    except ImportError:
        pass
    return await check_rate_limit_memory(user_id, action, custom_limits)


async def check_rate_limit_memory(
    user_id: Union[int, str],
    action: str = "message",
    custom_limits: Optional[Dict[str, int]] = None
) -> Tuple[bool, str, int]:
    """
    Fallback in-memory rate limiting.
    
    Args:
        user_id: User ID to check
        action: Action type
        custom_limits: Optional custom limits
        
    Returns:
        Tuple of (is_allowed, error_message, retry_after_seconds)
    """
    async with _in_memory_lock:
        limits = custom_limits or ACTION_LIMITS.get(action, {
            "per_minute": DEFAULT_REQUESTS_PER_MINUTE,
            "per_hour": DEFAULT_REQUESTS_PER_HOUR
        })
        
        key = f"{user_id}:{action}"
        now = time.time()
        
        timestamps = _in_memory_limits[key]["timestamps"]
        
        minute_ago = now - 60
        hour_ago = now - 3600
        _in_memory_limits[key]["timestamps"] = [t for t in timestamps if t > hour_ago]
        timestamps = _in_memory_limits[key]["timestamps"]
        
        recent_minute = [t for t in timestamps if t > minute_ago]
        if len(recent_minute) >= limits.get("per_minute", 30):
            oldest = min(recent_minute)
            retry_after = int(60 - (now - oldest)) + 1
            return False, _get_rate_limit_message(action, "minute"), retry_after
        
        if len(timestamps) >= limits.get("per_hour", 300):
            oldest = min(timestamps)
            retry_after = int(3600 - (now - oldest)) + 1
            return False, _get_rate_limit_message(action, "hour"), retry_after
        
        timestamps.append(now)
        return True, "", 0


def _get_rate_limit_message(action: str, window: str) -> str:
    """Get user-friendly rate limit message."""
    if window == "minute":
        if action == "verify_phone":
            return "⏳ *Rate Limit Exceeded*\n\nToo many phone verification attempts. Please wait 1 minute."
        elif action == "verify_otp":
            return "⏳ *Rate Limit Exceeded*\n\nToo many OTP attempts. Please wait 1 minute."
        elif action == "withdraw":
            return "⏳ *Rate Limit Exceeded*\n\nToo many withdrawal requests. Please wait 1 minute."
        else:
            return "⏳ *Rate Limit Exceeded*\n\nPlease wait 1 minute before trying again."
    else:
        if action == "verify_phone":
            return "⏳ *Rate Limit Exceeded*\n\nToo many phone verifications this hour. Please try later."
        elif action == "withdraw":
            return "⏳ *Rate Limit Exceeded*\n\nToo many withdrawals this hour. Please try later."
        else:
            return "⏳ *Rate Limit Exceeded*\n\nPlease try again later."


async def is_user_blocked(user_id: int) -> bool:
    """Check if user is temporarily blocked."""
    async with _in_memory_lock:
        if user_id in _blocked_users:
            if datetime.now() < _blocked_users[user_id]:
                return True
            else:
                del _blocked_users[user_id]
        return False


async def block_user_temporarily(user_id: int, duration_minutes: int = 5) -> None:
    """Temporarily block a user for abuse."""
    async with _in_memory_lock:
        _blocked_users[user_id] = datetime.now() + timedelta(minutes=duration_minutes)
        logger.warning(f"User {user_id} temporarily blocked for {duration_minutes} minutes")


async def unblock_user(user_id: int) -> None:
    """Unblock a user."""
    async with _in_memory_lock:
        if user_id in _blocked_users:
            del _blocked_users[user_id]


async def check_burst_limit(user_id: Union[int, str]) -> bool:
    """Check if user is within burst limit (rapid successive requests)."""
    async with _in_memory_lock:
        key = f"{user_id}:burst"
        now = time.time()
        
        timestamps = _in_memory_limits[key]["timestamps"]
        cutoff = now - BURST_WINDOW
        _in_memory_limits[key]["timestamps"] = [t for t in timestamps if t > cutoff]
        timestamps = _in_memory_limits[key]["timestamps"]
        
        if len(timestamps) >= BURST_LIMIT:
            return False
        
        timestamps.append(now)
        return True


def rate_limit(action: str = "message", custom_limits: Optional[Dict[str, int]] = None):
    """
    Decorator for rate limiting async functions.
    
    Usage:
        @rate_limit("verify_phone")
        async def handle_phone_verification(user_id, phone):
            ...
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            user_id = kwargs.get("user_id") or (args[0] if args else None)
            
            if user_id is None:
                return await func(*args, **kwargs)
            
            if await is_user_blocked(int(user_id)):
                return False, "🚫 *Temporarily Blocked*\n\nYou have been temporarily blocked.", 0
            
            allowed, message, retry_after = await check_rate_limit_redis(
                user_id, action, custom_limits
            )
            
            if not allowed:
                return False, message, retry_after
            
            return await func(*args, **kwargs)
        
        return wrapper
    return decorator


async def get_user_rate_status(user_id: Union[int, str], action: str = "message") -> Dict:
    """Get rate limit status for a user."""
    async with _in_memory_lock:
        key = f"{user_id}:{action}"
        now = time.time()
        
        timestamps = _in_memory_limits.get(key, {}).get("timestamps", [])
        
        minute_ago = now - 60
        hour_ago = now - 3600
        
        recent_minute = len([t for t in timestamps if t > minute_ago])
        recent_hour = len([t for t in timestamps if t > hour_ago])
        
        limits = ACTION_LIMITS.get(action, {
            "per_minute": DEFAULT_REQUESTS_PER_MINUTE,
            "per_hour": DEFAULT_REQUESTS_PER_HOUR
        })
        
        return {
            "action": action,
            "requests_this_minute": recent_minute,
            "requests_this_hour": recent_hour,
            "limit_per_minute": limits["per_minute"],
            "limit_per_hour": limits["per_hour"],
            "is_blocked": await is_user_blocked(int(user_id)) if isinstance(user_id, int) or str(user_id).isdigit() else False
        }


async def cleanup_old_rate_limits() -> int:
    """Clean up expired rate limit data."""
    async with _in_memory_lock:
        cleaned = 0
        now = time.time()
        hour_ago = now - 3600
        
        keys_to_delete = []
        for key in list(_in_memory_limits.keys()):
            timestamps = _in_memory_limits[key].get("timestamps", [])
            valid_timestamps = [t for t in timestamps if t > hour_ago]
            
            if not valid_timestamps:
                keys_to_delete.append(key)
                cleaned += 1
            else:
                _in_memory_limits[key]["timestamps"] = valid_timestamps
        
        for key in keys_to_delete:
            del _in_memory_limits[key]
        
        expired_blocks = [uid for uid, exp in _blocked_users.items() if datetime.now() >= exp]
        for uid in expired_blocks:
            del _blocked_users[uid]
            cleaned += 1
        
        return cleaned


async def get_rate_limiter_stats() -> Dict:
    """Get rate limiter statistics."""
    async with _in_memory_lock:
        total_keys = len(_in_memory_limits)
        total_blocked = len(_blocked_users)
        
        total_timestamps = sum(
            len(data.get("timestamps", []))
            for data in _in_memory_limits.values()
        )
        
        return {
            "tracked_user_actions": total_keys,
            "total_timestamps": total_timestamps,
            "blocked_users": total_blocked,
            "memory_usage_estimate": f"{total_timestamps * 8 / 1024:.2f} KB"
        }


print("✅ Rate Limiter Module Loaded")
print(f"   Actions configured: {len(ACTION_LIMITS)}")
print(f"   Default per-minute limit: {DEFAULT_REQUESTS_PER_MINUTE}")
