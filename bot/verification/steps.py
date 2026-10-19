"""
Session step state machine and lifecycle helpers.
=====================================================================
Tracks the current step of each in-flight verification (waiting_otp,
processing, etc.), exposes TTL-based cleanup of stale sessions, and
cleans up active sessions for a user.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from pyrogram.client import Client

from bot.utils.anti_spam import safe_disconnect
from bot.utils.net_timeouts import teardown_with_timeout
from bot.verification.state import (
    _active_sessions,
    _active_sessions_lock,
    _SESSION_MAX_IDLE_SECONDS,
)

logger = logging.getLogger(__name__)


def get_session_step(user_id: int, phone: Optional[str] = None) -> Optional[str]:
    """
    Get current step of user's session
    
    Args:
        user_id: Telegram user ID
        phone: Optional phone number. If provided, return step for that specific session.
               If not provided, return "waiting_otp" if ANY session is waiting for OTP.
    """
    if user_id not in _active_sessions:
        return None
    
    if phone:
        if phone in _active_sessions[user_id]:
            return _active_sessions[user_id][phone].get("step")
        return None
    else:
        for key, session_data in _active_sessions[user_id].items():
            if key == "_latest_waiting_phone" or not isinstance(session_data, dict):
                continue
            step = session_data.get("step")
            if step == "waiting_otp":
                return "waiting_otp"
        for key, session_data in _active_sessions[user_id].items():
            if key == "_latest_waiting_phone" or not isinstance(session_data, dict):
                continue
            return session_data.get("step")
        return None


def is_session_active(user_id: int, phone: Optional[str] = None) -> bool:
    """
    Check if user has an active session
    
    Args:
        user_id: Telegram user ID
        phone: Optional phone number. If provided, check specific session.
               If not provided, return True if ANY session exists for user.
    """
    if user_id not in _active_sessions:
        return False
    
    if phone:
        return phone in _active_sessions[user_id]
    else:
        return bool(_active_sessions[user_id])


async def cleanup_session(user_id: int, phone: Optional[str] = None):
    """
    Clean up active session(s)
    
    Args:
        user_id: Telegram user ID
        phone: Optional phone number. If provided, only cleanup that specific session.
               If not provided, cleanup all sessions for user (backward compatibility).
    """
    if user_id not in _active_sessions:
        return
    
    if phone:
        if phone in _active_sessions[user_id]:
            try:
                client = _active_sessions[user_id][phone].get("client")
                if client:
                    await teardown_with_timeout(client, "active-session-cleanup")
            except Exception:
                pass
            del _active_sessions[user_id][phone]
            if not _active_sessions[user_id]:
                del _active_sessions[user_id]
    else:
        for p, session_data in list(_active_sessions[user_id].items()):
            try:
                client = session_data.get("client")
                if client:
                    await teardown_with_timeout(client, "active-session-cleanup")
            except Exception:
                pass
        del _active_sessions[user_id]


async def cleanup_stale_active_sessions():
    """Periodically clean up stale active sessions that exceed max idle time."""
    async with _active_sessions_lock:
        now = datetime.now()
        empty_users = []
        for user_id in list(_active_sessions.keys()):
            phones_to_remove = []
            for phone in list(_active_sessions.get(user_id, {}).keys()):
                if phone == "_latest_waiting_phone":
                    continue
                session_data = _active_sessions[user_id].get(phone, {})
                if not isinstance(session_data, dict):
                    continue
                started_at_str = session_data.get("started_at")
                if started_at_str:
                    try:
                        started_at = datetime.fromisoformat(started_at_str)
                        idle_seconds = (now - started_at).total_seconds()
                        if idle_seconds > _SESSION_MAX_IDLE_SECONDS:
                            client = session_data.get("client")
                            if client:
                                try:
                                    await safe_disconnect(client, "ttl-cleanup")
                                except Exception:
                                    pass
                            phones_to_remove.append(phone)
                            logger.info(f"TTL CLEANUP: Removed stale session for user {user_id}, phone {phone} (idle: {idle_seconds:.0f}s)")
                    except (ValueError, TypeError):
                        phones_to_remove.append(phone)
            for phone in phones_to_remove:
                if user_id in _active_sessions and phone in _active_sessions[user_id]:
                    del _active_sessions[user_id][phone]
            if user_id in _active_sessions and (not _active_sessions[user_id] or list(_active_sessions[user_id].keys()) == ["_latest_waiting_phone"]):
                empty_users.append(user_id)
        for user_id in empty_users:
            _active_sessions.pop(user_id, None)
