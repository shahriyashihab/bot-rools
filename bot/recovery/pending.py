"""Refactored from bot.utils.pending_recovery: Pending session selection for recovery."""
import logging
import os
from datetime import datetime
from typing import Dict, List, Tuple

from bot.recovery.constants import (
    MAX_SESSION_AGE_HOURS,
    MULTI_DEVICE_24H_WINDOW_HOURS,
    MULTI_DEVICE_GRACE_MINUTES,
)
from bot.utils.storage import get_all_pending_sessions

logger = logging.getLogger(__name__)


def get_pending_sessions_for_recovery() -> List[Tuple[str, Dict]]:
    """
    Get all pending sessions that need recovery.
    
    ENHANCED CRITERIA - Include ALL sessions with session_string:
    - Status is 'pending'
    - Has session_string or session_file
    - Message not successfully sent yet
    - Not older than MAX_SESSION_AGE_HOURS
    
    CRITICAL: Do NOT skip ready_to_send sessions - they also need recovery!
    If session has precomputed_message, we'll send it directly.
    If not, we'll re-verify and send appropriate message.
    """
    all_pending = get_all_pending_sessions()
    recovery_list = []
    
    cutoff = datetime.now()
    
    for session_id, session_data in all_pending.items():
        try:
            if session_data.get("status") != "pending":
                continue
            
            inner_data = session_data.get("session_data", {})
            
            if inner_data.get("message_send_success") == True:
                continue
            
            # CRITICAL: session_string is stored at ROOT level, not inside session_data!
            session_string = (
                session_data.get("session_string") or  # ROOT level (where it's actually stored)
                inner_data.get("session_string")  # Fallback check inside session_data
            )
            session_file = session_data.get("session_file") or inner_data.get("session_file")
            
            has_session = bool(session_string)
            if not has_session and session_file:
                has_session = os.path.exists(session_file)
            
            if not has_session:
                continue
            
            submitted_str = session_data.get("submitted_at")
            if submitted_str:
                try:
                    submitted_at = datetime.fromisoformat(str(submitted_str).replace("Z", "").replace("+00:00", ""))
                    age_hours = (cutoff - submitted_at).total_seconds() / 3600
                    if age_hours > MAX_SESSION_AGE_HOURS:
                        continue
                    # Skip multi-device sessions past 24h+5min window (handled by expiry)
                    if inner_data.get("has_extra_devices"):
                        added_str = session_data.get("added_at") or inner_data.get("added_at") or submitted_str
                        added_at = datetime.fromisoformat(str(added_str).replace("Z", "").replace("+00:00", ""))
                        age_hours_md = (cutoff - added_at).total_seconds() / 3600
                        window_hours = MULTI_DEVICE_24H_WINDOW_HOURS + (MULTI_DEVICE_GRACE_MINUTES / 60.0)
                        if age_hours_md >= window_hours:
                            continue
                # P7-73: a bare `except: pass` silently swallowed EVERY error
                # here - including KeyboardInterrupt/SystemExit - and let a
                # corrupt added_at silently skip the 24h-window guard. Catch
                # the expected parse errors explicitly and log anything else.
                except (ValueError, TypeError) as parse_err:
                    logger.debug(f"Recovery eligibility: date parse skipped for {session_id}: {parse_err}")

            # PRESERVE REMAINING TIME: sessions whose deadline is still in the
            # future must NOT be finalized early by recovery. The monitor keeps
            # them pending and finalizes them at their exact deadline.
            deadline_str = session_data.get("deadline_at")
            if deadline_str:
                try:
                    deadline_dt = datetime.fromisoformat(str(deadline_str).replace("Z", "").replace("+00:00", ""))
                    if deadline_dt > cutoff:
                        continue
                except (ValueError, TypeError):
                    pass

            recovery_list.append((session_id, session_data))
            
        except Exception as e:
            print(f"⚠️ RECOVERY: Error checking session {session_id}: {e}")
            continue
    
    recovery_list.sort(
        key=lambda x: x[1].get("submitted_at", ""),
        reverse=True
    )
    
    return recovery_list
