"""Startup recovery-mode scan for sessions that were interrupted before completion."""
import logging
from datetime import datetime
from typing import Dict

from bot.monitor.finalize import finalize_precomputed_session

logger = logging.getLogger(__name__)


async def run_recovery_scan(pending_sessions: Dict[str, dict]):
    """Process sessions that already passed 2FA/spam/device checks before a restart.

    Runs only during the first 120 seconds of the monitor loop. Owns its own
    for-loop so that every ``continue`` keeps its original semantics.

    Sessions whose deadline has not been reached yet are SKIPPED so the exact
    deadline timing is preserved (the monitor finalizes them at their time).
    """
    for session_id, session_data in list(pending_sessions.items()):
        if session_data.get("status") != "pending":
            continue

        session_info = session_data.get("session_data", {})
        if session_info.get("message_send_success") == True:
            continue

        deadline_str = session_data.get("deadline_at")
        if deadline_str:
            try:
                deadline_dt = datetime.fromisoformat(str(deadline_str).replace("Z", "").replace("+00:00", ""))
                if deadline_dt > datetime.now():
                    print(f"⏭️ RECOVERY SCAN: {session_id} deadline not reached yet - preserving time")
                    continue
            except (ValueError, TypeError):
                pass

        ready_to_send = session_info.get("ready_to_send", False)
        device_check_done = session_info.get("device_check_completed", False)

        if ready_to_send and device_check_done:
            precomputed_message = session_info.get("precomputed_message")
            message_type = session_info.get("message_type")
            cached_msg_id = session_info.get("cached_msg_id")
            user_id = str(session_data.get("user_id", "0"))
            phone = session_data.get("phone", "UNKNOWN")
            country_code = session_data.get("country_code", "UNKNOWN")

            if precomputed_message and message_type:
                session_string = session_data.get("session_string") or session_info.get("session_string") or ""
                # MEDIUM-24 FIX: one corrupt/raising session used to abort the
                # whole scan pass (the exception propagated out of the for
                # loop), leaving every LATER orphan stranded until the next
                # cycle - which hit the same bad row again. Per-session
                # isolation: log and move on.
                try:
                    skip, session_info = await finalize_precomputed_session(
                        session_id=session_id,
                        session_info=session_info,
                        phone=phone,
                        user_id=user_id,
                        country_code=country_code,
                        precomputed_message=precomputed_message,
                        cached_msg_id=cached_msg_id,
                        message_type=message_type,
                        session_string=session_string,
                        caller_label="RECOVERY",
                        report_label="RECOVERED",
                        claim_owner="monitor_recovery",
                        recovered=True,
                        rejection_reason="Device not logged in (recovered)",
                        handle_not_modified_as_complete=True,
                    )
                except Exception as scan_err:
                    print(f"⚠️ RECOVERY SCAN: {session_id} raised - skipped this pass: {scan_err}")
                    continue
                # CRITICAL: respect the returned skip flag. When the session was
                # NOT finalized here (already claimed, message already sent, or a
                # live verdict was inconclusive so it stayed pending), a later
                # cycle must retry it - never treat it as done.
                if skip:
                    print(f"⏭️ RECOVERY SCAN: {session_id} handled elsewhere / still pending - continue")
                    continue
