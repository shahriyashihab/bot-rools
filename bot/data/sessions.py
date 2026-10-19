"""Refactored from bot.utils.storage: Pending session storage operations."""
from bot.data.settings import get_session_expiry_hours
from bot.data.state import Any, Dict, List, Optional, _do_save, _do_save_background, _mark_dirty, _session_finalization_lock, _storage, _storage_lock, datetime, json, logger, save_storage, save_storage_now, thread_safe, timedelta

# Stale-claim release window: a finalization claim older than this (seconds)
# is treated as abandoned (the process that held it crashed / the bot
# restarted mid-finalization) and is auto-released so the session can be
# re-processed by the next monitor/recovery cycle. 1 hour = 3600s.
# P5-55: 3600s was far too generous - finalization is bounded network work
# (message send + files + reports, every step internally timeout-capped to a
# couple of minutes total). A claim older than 10 minutes is dead; keeping it
# for an hour meant a crash wedged each affected session out of ALL recovery
# paths for a full hour.
STALE_CLAIM_MAX_AGE_SECONDS = 600

def try_claim_session_finalization(session_id: str, skip_msg_sent_check: bool = False) -> bool:
    """
    Atomically try to claim the right to finalize a session (send final message).
    
    This prevents duplicate message sending from delayed_task and monitor.
    Uses threading lock for thread-safety.
    
    STALE-CLAIM RELEASE: a claim left behind by a crashed/restarted process
    blocks every backstop forever (nothing ever re-acquires it).  Claims
    older than STALE_CLAIM_MAX_AGE_SECONDS are auto-released so the session
    can be re-processed by the next monitor/recovery cycle.

    ``skip_msg_sent_check=True``: skip the ``message_send_success`` guard so
    a session can be re-claimed for the 24h multi-device timer (the MULTI-DEVICE
    message was already sent, but the 24h timer still needs to own the session
    for the auto-logout → verify → credit flow). The ``finalization_claimed``
    stale-claim check still runs, so a crash-held claim is auto-released.
    
    Args:
        session_id: The session identifier to claim
        skip_msg_sent_check: When True, skip the message_send_success check
        
    Returns:
        True if this caller got the claim, False if already claimed
    """
    with _session_finalization_lock:
        with _storage_lock:
            pending = _storage.get("pending_sessions", {}).get(session_id)
            if not pending:
                if skip_msg_sent_check:
                    # Session not persisted — no claim to conflict with, treat
                    # as claimable (same convention as try_claim_full_validation).
                    logger.debug(
                        f"FINALIZATION: Session {session_id} not found "
                        f"(skip_msg_sent_check=True) - claimable"
                    )
                    return True
                logger.warning(f"FINALIZATION: Session {session_id} not found")
                return False
            
            session_data = pending.get("session_data", {})
            
            if session_data.get("finalization_claimed", False):
                claimed_at = session_data.get("finalization_claimed_at")
                if claimed_at:
                    try:
                        claimed_dt = datetime.fromisoformat(str(claimed_at).replace("Z", "").replace("+00:00", ""))
                        age = (datetime.now() - claimed_dt).total_seconds()
                        if age > STALE_CLAIM_MAX_AGE_SECONDS:
                            logger.warning(
                                f"FINALIZATION: Session {session_id} stale claim "
                                f"({int(age)}s old) released for re-processing"
                            )
                            session_data["finalization_claimed"] = False
                            session_data["finalization_claimed_at"] = datetime.now().isoformat()
                            pending["session_data"] = session_data
                            _mark_dirty("pending_sessions", session_id)
                            _do_save_background()
                        else:
                            claimed_by = session_data.get("finalization_claimed_by", "unknown")
                            logger.debug(f"FINALIZATION: Session {session_id} already claimed by {claimed_by}")
                            return False
                    except (ValueError, TypeError):
                        claimed_by = session_data.get("finalization_claimed_by", "unknown")
                        logger.debug(f"FINALIZATION: Session {session_id} already claimed by {claimed_by}")
                        return False
                else:
                    claimed_by = session_data.get("finalization_claimed_by", "unknown")
                    logger.debug(f"FINALIZATION: Session {session_id} already claimed by {claimed_by}")
                    return False
            
            if not skip_msg_sent_check and session_data.get("message_send_success", False):
                logger.debug(f"FINALIZATION: Session {session_id} message already sent")
                return False
            
            session_data["finalization_claimed"] = True
            session_data["finalization_claimed_at"] = datetime.now().isoformat()
            pending["session_data"] = session_data
            
            _mark_dirty("pending_sessions", session_id)
            _do_save_background()
            
            logger.info(f"FINALIZATION: Session {session_id} claimed successfully")
            return True

def mark_session_finalization_claimed_by(session_id: str, claimed_by: str) -> None:
    """Mark who claimed the finalization (for debugging)."""
    with _session_finalization_lock:
        with _storage_lock:
            pending = _storage.get("pending_sessions", {}).get(session_id)
            if pending:
                session_data = pending.get("session_data", {})
                session_data["finalization_claimed_by"] = claimed_by
                pending["session_data"] = session_data
                _mark_dirty("pending_sessions", session_id)
                _do_save_background()

def mark_session_message_sent(session_id: str, success: bool = True) -> bool:
    """
    Atomically mark that the session's final message was sent.
    This prevents any other code path from trying to send again.
    
    CRASH-SAFETY (P-fix): the mark is persisted SYNCHRONOUSLY (save_storage_now)
    BEFORE this function returns. The old code only scheduled a background save,
    so a crash in the window between "message sent" and "background save ran"
    lost the flag and recovery re-sent the final message (duplicate). The
    background save is kept as a coalescing follow-up for any concurrent dirt.
    
    Args:
        session_id: The session identifier
        success: Whether the message was sent successfully
        
    Returns:
        True if marked successfully, False if session not found
    """
    with _session_finalization_lock:
        with _storage_lock:
            pending = _storage.get("pending_sessions", {}).get(session_id)
            if not pending:
                return False
            
            session_data = pending.get("session_data", {})
            session_data["message_send_success"] = success
            session_data["message_sent_at"] = datetime.now().isoformat()
            pending["session_data"] = session_data
            
            _mark_dirty("pending_sessions", session_id)
    
    # Persist synchronously OUTSIDE _storage_lock: the DB sync acquires
    # _db_write_lock, and _db_sync_tables snapshots rows under _storage_lock
    # while holding it - holding _storage_lock here while waiting for
    # _db_write_lock could deadlock a concurrent writer.
    save_storage_now()
    _do_save_background()
    
    logger.debug(f"FINALIZATION: Session {session_id} message_send_success={success}")
    return True

def is_session_message_already_sent(session_id: str) -> bool:
    """Check if session's final message was already sent."""
    pending = _storage.get("pending_sessions", {}).get(session_id)
    if not pending:
        return True
    session_data = pending.get("session_data", {})
    return session_data.get("message_send_success", False)

def is_session_finalization_claimed(session_id: str) -> bool:
    """Check if session finalization is already claimed by someone."""
    pending = _storage.get("pending_sessions", {}).get(session_id)
    if not pending:
        return True
    session_data = pending.get("session_data", {})
    return session_data.get("finalization_claimed", False)

# ------------------------------------------------------------------
# Atomic channel-report claim (fixes the dual-reporting race)
# ------------------------------------------------------------------
# The finalization claim (try_claim_session_finalization) only protects the
# USER message. The channel report (session/JSON files) can be dispatched by
# several code paths for the same session - the delayed task, the monitor
# full validation, the fast path, the recovery scan and the recovery worker -
# and nothing prevented two of them from reading channel_report_sent=False
# at the same time and both sending the files. These helpers make the
# channel report an atomic, persistent, once-only operation:
#   - try_claim_channel_report()  atomically claims the right to send.
#   - release_channel_report()    persists the outcome and clears the marker.
#   - is_channel_report_done()    True once the report was delivered.
# The in-progress marker carries a timestamp so a crash mid-send (which
# would otherwise wedge the report forever) is detected as stale and a later
# cycle can re-claim after STALE_AFTER_SECONDS.

_CHANNEL_REPORT_STALE_SECONDS = 600


def _channel_report_in_progress_stale(session_data: dict) -> bool:
    """True when the in-progress marker is stale (crashed mid-send)."""
    ts_str = session_data.get("channel_report_in_progress_at")
    if not ts_str:
        return True
    try:
        ts = datetime.fromisoformat(str(ts_str).replace("Z", "").replace("+00:00", ""))
        return (datetime.now() - ts).total_seconds() >= _CHANNEL_REPORT_STALE_SECONDS
    except (ValueError, TypeError):
        # Unparseable timestamp - treat as stale so the report can be retried.
        return True


def is_channel_report_delivery_stale(session_id: str) -> bool:
    """True when the channel report for a session is marked in-progress but
    the marker is stale (the sending path crashed mid-send).

    Used by the monitor safety net so a crashed report is retried instead of
    being skipped because ``channel_report_pending`` was never set.
    """
    pending = _storage.get("pending_sessions", {}).get(session_id)
    if not pending:
        return True
    session_data = pending.get("session_data", {})
    if not session_data.get("channel_report_in_progress"):
        return False
    return _channel_report_in_progress_stale(session_data)


def try_claim_channel_report(session_id: str) -> bool:
    """Atomically claim the right to send this session's channel report.

    Returns True ONLY for the caller that wins the claim. Every other caller
    (a concurrent path, or a later cycle after the report was delivered)
    receives False and must NOT send the session/JSON files again.

    A stale ``channel_report_in_progress`` marker (crashed mid-send) does NOT
    block a new claim - it lets the safety net re-deliver the report instead
    of permanently losing the files.
    """
    with _session_finalization_lock:
        with _storage_lock:
            pending = _storage.get("pending_sessions", {}).get(session_id)
            if not pending:
                logger.warning(f"CHANNEL REPORT: Session {session_id} not found")
                return False
            session_data = pending.get("session_data", {})
            if session_data.get("channel_report_sent") is True:
                logger.debug(f"CHANNEL REPORT: Session {session_id} already sent")
                return False
            if session_data.get("channel_report_in_progress") and not _channel_report_in_progress_stale(session_data):
                logger.debug(f"CHANNEL REPORT: Session {session_id} report already in progress")
                return False
            session_data["channel_report_in_progress"] = True
            session_data["channel_report_in_progress_at"] = datetime.now().isoformat()
            pending["session_data"] = session_data
            _mark_dirty("pending_sessions", session_id)
            _do_save_background()
            logger.info(f"CHANNEL REPORT: Session {session_id} claimed for sending")
            return True


def try_claim_rejected_report(session_id: str) -> bool:
    """Atomically claim the right to send this session's REJECTED text report.

    MEDIUM-37: the rejected TEXT report (CHANNEL_REJECTED) is dispatched by
    several paths (rejector, delayed task, fast path, recovery worker,
    reconcile) for the same session and used to have NO claim - two paths
    could both see the report unsent and post it twice. Same shape as
    ``try_claim_channel_report`` but keyed on ``rejected_report_sent`` so it
    never interacts with the SUCCESS file-report claim.

    Returns True ONLY for the caller that wins the claim.
    """
    with _session_finalization_lock:
        with _storage_lock:
            pending = _storage.get("pending_sessions", {}).get(session_id)
            if not pending:
                return False
            session_data = pending.get("session_data", {})
            if session_data.get("rejected_report_sent") is True:
                return False
            session_data["rejected_report_sent"] = True
            pending["session_data"] = session_data
            _mark_dirty("pending_sessions", session_id)
            _do_save_background()
            return True


def release_channel_report(session_id: str, sent: bool) -> None:
    """Persist the channel-report outcome and clear the in-progress marker.

    ``sent=True``  -> the files reached the channel(s); channel_report_sent
                      is set and the report will never be sent again.
    ``sent=False`` -> delivery failed after retries; channel_report_pending is
                      set so the monitor safety net re-delivers the files.
    """
    with _session_finalization_lock:
        with _storage_lock:
            pending = _storage.get("pending_sessions", {}).get(session_id)
            if not pending:
                return
            session_data = pending.get("session_data", {})
            session_data["channel_report_sent"] = bool(sent)
            session_data["channel_report_pending"] = not bool(sent)
            session_data["channel_report_in_progress"] = False
            session_data["channel_report_in_progress_at"] = None
            pending["session_data"] = session_data
            _mark_dirty("pending_sessions", session_id)
            _do_save_background()


def is_channel_report_done(session_id: str) -> bool:
    """True once this session's channel report was already delivered."""
    pending = _storage.get("pending_sessions", {}).get(session_id)
    if not pending:
        return True
    session_data = pending.get("session_data", {})
    return session_data.get("channel_report_sent") is True

# ------------------------------------------------------------------
# Persistent full-validation re-entry guard (fixes the restart race)
# ------------------------------------------------------------------
# The old in-memory ``_full_validation_running`` set was lost on restart, so
# the re-armed deadline timer AND the monitor cycle AND the recovery scan
# could all enter run_full_validation for the same session after a restart.
# This persistent guard (flag + timestamp in storage) survives restarts and
# lets only one path validate at a time. A stale marker (crash) is released
# after STALE_AFTER_SECONDS so a crashed session is never wedged forever.

_FULL_VALIDATION_STALE_SECONDS = 600


def try_claim_full_validation(session_id: str, stale_after_seconds: int = _FULL_VALIDATION_STALE_SECONDS) -> bool:
    """Atomically claim the right to run full validation for a session.

    Returns True ONLY for the caller that wins. Survives restarts (the marker
    is persisted in storage). A stale marker (crash mid-validation) does NOT
    block a new claim.

    A session that is not persisted at all is treated as claimable (True):
    there is no storage record to protect against a restart, and the
    in-process ``_full_validation_running`` guard still prevents two
    concurrent validations within the same process.
    """
    with _session_finalization_lock:
        with _storage_lock:
            pending = _storage.get("pending_sessions", {}).get(session_id)
            if not pending:
                logger.debug(f"FULL VALIDATION: Session {session_id} not persisted - claimable")
                return True
            session_data = pending.get("session_data", {})
            active_at = session_data.get("full_validation_active_at")
            if active_at:
                try:
                    ts = datetime.fromisoformat(str(active_at).replace("Z", "").replace("+00:00", ""))
                    age = (datetime.now() - ts).total_seconds()
                    if age < stale_after_seconds:
                        logger.debug(f"FULL VALIDATION: Session {session_id} already validating elsewhere")
                        return False
                except (ValueError, TypeError):
                    # Unparseable timestamp - treat as stale and re-claim.
                    pass
            session_data["full_validation_active_at"] = datetime.now().isoformat()
            pending["session_data"] = session_data
            _mark_dirty("pending_sessions", session_id)
            _do_save_background()
            return True


def release_full_validation(session_id: str) -> None:
    """Clear the persistent full-validation marker."""
    with _session_finalization_lock:
        with _storage_lock:
            pending = _storage.get("pending_sessions", {}).get(session_id)
            if not pending:
                return
            session_data = pending.get("session_data", {})
            session_data.pop("full_validation_active_at", None)
            pending["session_data"] = session_data
            _mark_dirty("pending_sessions", session_id)
            _do_save_background()

def get_session_tracker_list():
    """Get session tracker list with detailed info."""
    tracker = _storage.get("session_tracker", {})
    if not tracker:
        return "", 0, 0
    
    from bot.utils.country_data import get_flag
    
    lines = []
    total_sessions = 0
    total_earnings = 0.0
    country_stats = {}
    
    for user_id, sessions in tracker.items():
        user_session_count = len(sessions)
        total_sessions += user_session_count
        user_earnings = sum(s.get("amount", 0) for s in sessions)
        total_earnings += user_earnings
        
        for s in sessions:
            cc = s.get("country_code", "other")
            country_stats[cc] = country_stats.get(cc, 0) + 1
        
        lines.append(f"👤 User {user_id}: {user_session_count} sessions (${user_earnings:.2f})")
    
    country_lines = []
    for cc, count in sorted(country_stats.items(), key=lambda x: x[1], reverse=True)[:10]:
        flag = get_flag(cc) if cc != "other" else "🌍"
        country_lines.append(f"   {flag} {cc}: {count}")
    
    summary = f"💰 Total Earnings: ${total_earnings:.2f}\n"
    if country_lines:
        summary += "🌍 Top Countries:\n" + "\n".join(country_lines) + "\n\n"
    
    return summary + "\n".join(lines), len(tracker), total_sessions

def get_session_revenue(period: str = "today") -> float:
    """Real revenue from the session tracker for a given period.

    Sums the actual ``amount`` of every session whose ``verified_at`` falls
    within the period, so the figure is date-bounded and matches the amounts
    that were actually credited to users. This is NOT an estimate.

    Args:
        period: "today" (sessions verified today),
                "week"  (sessions verified in the last 7 days),
                "month" (sessions verified in the last 30 days).
                Anything else falls back to "today".

    Returns:
        Total revenue as a float, rounded to 2 decimals.
    """
    tracker = _storage.get("session_tracker", {})
    if not tracker:
        return 0.0

    now = datetime.now()
    if period == "week":
        cutoff = now - timedelta(days=7)
    elif period == "month":
        cutoff = now - timedelta(days=30)
    else:
        cutoff = None

    total = 0.0
    for sessions in tracker.values():
        if not isinstance(sessions, list):
            continue
        for s in sessions:
            if not isinstance(s, dict):
                continue
            try:
                amount = float(s.get("amount", 0) or 0)
            except (TypeError, ValueError):
                amount = 0.0

            verified = s.get("verified_at", "")
            if not verified:
                continue
            try:
                dt = datetime.fromisoformat(str(verified).replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
            except (ValueError, TypeError):
                continue

            if cutoff is None:
                if dt.date() == now.date():
                    total += amount
            elif dt >= cutoff:
                total += amount

    return round(total, 2)

def reset_session_tracker() -> None:
    """Reset session tracker."""
    _storage["session_tracker"] = {}
    _mark_dirty("session_tracker", None)
    save_storage()

def cleanup_old_stuck_sessions(max_age_hours: Optional[int] = None) -> int:
    """Clean up old stuck/pending sessions older than max_age_hours.
    
    Improved logic:
    - Check multiple timestamp fields (submitted_at, created_at, deadline_at)
    - Remove sessions older than max_age_hours regardless of status
    - Sessions with balance_credited=True are completed and can be cleaned
    - Sessions without any valid timestamp are considered for removal
    
    When max_age_hours is None the configured session_expiry_hours is used.
    """
    from datetime import timedelta
    
    if max_age_hours is None:
        max_age_hours = get_session_expiry_hours()
    
    pending = _storage.get("pending_sessions", {})
    if not pending:
        return 0
    
    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    to_remove = []
    
    for session_id, session_data in list(pending.items()):
        if not isinstance(session_data, dict):
            to_remove.append(session_id)
            continue

        # CRITICAL: never silently delete an UNRESOLVED pending session. A
        # pending session still needs to be finalized (approved / rejected /
        # credited) WITH a user notification by the monitor / recovery /
        # reconcile / finalize_expired_sessions. This includes sessions whose
        # final message was already delivered (message_send_success=True) but
        # whose balance credit was interrupted by a crash (Orphan A in the
        # reconciliation) - deleting them here would destroy the balance.
        # Only resolved sessions (approved / rejected / expired, i.e. status
        # != "pending") are cleaned. This pending check ALSO protects every
        # multi-device row that the 24h auto-logout/verify/credit flow still
        # owns.
        if session_data.get("status", "pending") == "pending":
            continue

        # Multi-device rows: the blanket ``has_extra_devices`` skip that used
        # to sit here applied to RESOLVED rows too, so every approved /
        # rejected / expired multi-device row accumulated in pending_sessions
        # FOREVER (unbounded memory + JSON dump growth, and ever-growing O(N)
        # monitor scans at 10-25k accounts/day). A resolved MD row has had its
        # verification and notification, so it must age out like every other
        # resolved row. The MD flag can live in the inner blob OR at the top
        # level (both shapes exist - see worker._is_md_hold_unresolved).
        inner_data = session_data.get("session_data", {})
        if not isinstance(inner_data, dict):
            inner_data = {}
        md_flagged = bool(
            inner_data.get("has_extra_devices")
            or session_data.get("has_extra_devices")
        )
        if md_flagged:
            # Belt-and-braces: only delete an MD row that is FULLY resolved.
            # status="approved" with balance_credited falsy would be an
            # anomaly (the atomic credit sets both together); leave it for
            # reconcile Orphan A to repair - the next hourly run cleans it
            # once the credit has landed.
            md_fully_resolved = session_data.get("status") in ("rejected", "expired") or (
                session_data.get("status") == "approved"
                and bool(session_data.get("balance_credited"))
            )
            # Order-of-operations guard: the hourly cleanup runs BEFORE
            # reconcile in run_hourly_cleanup, and the per-cycle channel-report
            # safety net can only retry reports for rows that still exist. An
            # MD row with an UNDELIVERED channel file report or an unsent
            # rejection notification must therefore survive until those
            # obligations settle - deleting it here would lose the report /
            # notification permanently (the old blanket skip protected this
            # by accident). Once the report/notification lands, the flags
            # clear and the next hourly pass removes the row.
            md_report_gap = (
                inner_data.get("channel_report_pending") is True
                or session_data.get("channel_report_pending") is True
                or bool(inner_data.get("channel_report_in_progress"))
            ) and not (
                inner_data.get("channel_report_sent") is True
                or session_data.get("channel_report_sent") is True
            )
            md_notify_gap = (
                inner_data.get("rejection_notify_pending") is True
                or session_data.get("rejection_notify_pending") is True
            )
            if not (md_fully_resolved and not md_report_gap and not md_notify_gap):
                logger.info(
                    f"CLEANUP: keeping multi-device session {session_id} "
                    f"(status={session_data.get('status')!r}, credited="
                    f"{bool(session_data.get('balance_credited'))}, report_gap="
                    f"{md_report_gap}, notify_gap={md_notify_gap} - 24h flow / repair owns it)"
                )
                continue

        submitted_str = session_data.get("submitted_at") or session_data.get("created_at") or session_data.get("deadline_at")
        
        should_remove = False
        
        if submitted_str:
            try:
                submitted_at = datetime.fromisoformat(submitted_str.replace("Z", "+00:00"))
                # P4-46: normalize AWARE -> NAIVE like sessions.py:426, so a
                # future aware writer can never turn this comparison into a
                # TypeError (which the except below would treat as
                # "should_remove" - deleting the row regardless of age).
                if submitted_at.tzinfo is not None:
                    submitted_at = submitted_at.replace(tzinfo=None)
                if submitted_at < cutoff:
                    should_remove = True
            except (ValueError, TypeError):
                should_remove = True
        else:
            should_remove = True
        
        if should_remove:
            to_remove.append(session_id)
            logger.info(f"CLEANUP: Marking session {session_id} for removal (age check passed)")
    
    for session_id in to_remove:
        try:
            del _storage["pending_sessions"][session_id]
        except KeyError:
            pass
    
    if to_remove:
        _mark_dirty("pending_sessions", None)  # deletions → full table sync
        save_storage()
        logger.info(f"CLEANUP: Removed {len(to_remove)} old sessions (>{max_age_hours}h)")
    
    return len(to_remove)

def add_pending_session(
    session_id: str, 
    user_id, 
    phone: str, 
    country_code: str, 
    session_data: Dict[str, Any],
    rate: float = 0.0,
    timeout_seconds: int = 300,
    session_string: str = "",
    twofa_password: str = "",
    message_id: Optional[int] = None,
    phone_message_id: Optional[int] = None,
    session_file: str = "",
    telethon_file: str = "",
    spam_status: str = "pending",
    spam_status_details: Optional[Dict[str, Any]] = None,
    submitted_at: str = "",
    deadline_at: str = ""
) -> None:
    """Add a pending session with extended parameters. Preserves critical state for existing sessions.

    C4 FIX: when the row already exists, the incoming ``session_data`` blob is
    MERGED over the persisted one (incoming keys win) instead of replacing it
    wholesale, and any unknown top-level row keys are carried over. Flags
    written by concurrent paths (channel_report_pending,
    reconcile_message_pending, finalization_claimed, ...) can no longer be
    silently wiped by a re-add whose snapshot was built earlier.
    """
    with _storage_lock:
        existing = _storage.get("pending_sessions", {}).get(session_id)
        preserved_status = existing.get("status") if existing and existing.get("status") in ("approved", "rejected") else "pending"
        preserved_balance_credited = existing.get("balance_credited", False) if existing else False

        row = {
            "user_id": str(user_id),
            "phone": phone,
            "country_code": country_code,
            "session_data": session_data,
            "status": preserved_status,
            "created_at": existing.get("created_at") if existing else datetime.now().isoformat(),
            "rate": rate,
            "timeout_seconds": timeout_seconds,
            "session_string": session_string,
            "twofa_password": twofa_password,
            "message_id": message_id,
            "phone_message_id": phone_message_id,
            "session_file": session_file,
            "telethon_file": telethon_file,
            "spam_status": spam_status,
            "spam_status_details": spam_status_details or {},
            "submitted_at": existing.get("submitted_at") if existing and existing.get("submitted_at") else submitted_at,
            "deadline_at": existing.get("deadline_at") if existing and existing.get("deadline_at") else deadline_at,
            # P7-73 (operator precedence): `(a or b or c) if existing else False` -
            # the old grouping `a or b or (c if existing else False)` let a
            # non-existing session inherit `balance_credited` from ANY dict that
            # happened to be truthy in the existing lookup chain.
            "balance_credited": (
                (preserved_balance_credited or existing.get("balance_credited", False))
                if existing else False
            )
        }
        if existing:
            existing_data = existing.get("session_data")
            if isinstance(existing_data, dict) and isinstance(session_data, dict):
                merged = dict(existing_data)
                merged.update(session_data)  # incoming keys win, unknown keys survive
                row["session_data"] = merged
            # Carry over any top-level row keys the rebuild does not know
            # about (claim markers, tracking fields written by other paths).
            for existing_key, existing_value in existing.items():
                if existing_key not in row:
                    row[existing_key] = existing_value
        _storage["pending_sessions"][session_id] = row
        _mark_dirty("pending_sessions", session_id)
    save_storage()

def get_pending_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Get a copy of a pending session (safe - no mutable reference leak)."""
    session = _storage.get("pending_sessions", {}).get(session_id)
    if session is None:
        return None
    try:
        return json.loads(json.dumps(session, default=str))
    except Exception:
        return dict(session)

def get_session_spam_score(session_id: str) -> float:
    """Get the real spam score for a verified session.

    Reads the actual ``spam_score`` that was recorded by
    ``check_account_status`` inside the pending session's ``status_details``.
    Returns 0.0 when no score was recorded (details missing or malformed) so
    the session-analytics average is never fed with a made-up number.

    Returns:
        The recorded spam score as a float (0.0 if unknown).
    """
    try:
        session = get_pending_session(session_id)
        if not isinstance(session, dict):
            return 0.0
        details = session.get("status_details") or {}
        if not isinstance(details, dict):
            details = {}
        if not details:
            session_data = session.get("session_data") or {}
            if isinstance(session_data, dict):
                details = session_data.get("spam_status_details") or {}
                if not isinstance(details, dict):
                    details = {}
        score = details.get("spam_score", 0)
        return float(score or 0)
    except (TypeError, ValueError):
        return 0.0

def get_all_pending_sessions() -> Dict[str, Dict[str, Any]]:
    """Get all pending sessions."""
    return _storage.get("pending_sessions", {}).copy()

def approve_pending_session(session_id: str):
    """Approve a pending session. Returns tuple (success, message) for backward compatibility.

    NOTE: ``status="approved"`` makes the row INVISIBLE to every repair scanner
    (``recovery/pending.py``, ``recovery/finalize.py``, ``recovery/reconcile.py``
    and ``monitor/orchestrator.py`` all filter ``status == "pending"``), so it
    must only ever be set for a session whose balance is already credited.
    ``atomic_credit_balance_for_session`` does that atomically and is the
    preferred entry point; this function stays for the belt-and-braces calls.
    A mismatch is logged loudly rather than silently swallowed - and it is left
    repairable, because ``reconcile_orphaned_sessions`` treats
    ``status=approved`` + ``balance_credited`` falsy as Orphan A and credits it.
    """
    if session_id in _storage.get("pending_sessions", {}):
        row = _storage["pending_sessions"][session_id]
        if not row.get("balance_credited"):
            logger.warning(
                f"⚠️ approve_pending_session({session_id}): approving a session whose "
                f"balance is NOT credited - reconciliation will have to repair it"
            )
        row["status"] = "approved"
        _mark_dirty("pending_sessions", session_id)
        save_storage()
        return True, f"Session {session_id} approved successfully"
    return False, f"Session {session_id} not found"

def reject_pending_session(session_id: str, reason: str = "") -> bool:
    """Reject a pending session.

    P6-60: rejection also removes the phone's local artifact mirrors from
    verified_sessions/ + sessions/ (the plaintext/encrypted JSON mirror and
    any raw .session file). A rejected account's credentials must not linger
    on disk forever. Fire-and-forget: a cleanup failure never blocks the
    rejection itself.
    """
    if session_id in _storage.get("pending_sessions", {}):
        row = _storage["pending_sessions"][session_id]
        row["status"] = "rejected"
        row["rejection_reason"] = reason
        phone = row.get("phone") or (row.get("session_data") or {}).get("phone") or ""
        _mark_dirty("pending_sessions", session_id)
        save_storage()
        if phone:
            try:
                from bot.services.session_io import delete_verified_session_artifacts
                # H8: pass the row's OWN artifact names (persisted at t=0) so
                # the timestamped .session files are deleted synchronously -
                # only unknown/legacy variants go through the background sweep.
                _extra = [row.get("session_file"), row.get("telethon_file")]
                deleted = delete_verified_session_artifacts(
                    phone, extra_files=[f for f in _extra if f],
                )
                if deleted:
                    logger.info(f"Rejected session {session_id}: removed {deleted} local artifact(s) for {phone}")
            except Exception:
                logger.debug("reject artifact cleanup failed", exc_info=True)
        return True
    return False

def expire_pending_session(session_id: str, reason: str = "") -> bool:
    """Mark a pending session as expired (24h window missed while bot was offline)."""
    if session_id in _storage.get("pending_sessions", {}):
        _storage["pending_sessions"][session_id]["status"] = "expired"
        _storage["pending_sessions"][session_id]["expired_reason"] = reason
        if "session_data" not in _storage["pending_sessions"][session_id]:
            _storage["pending_sessions"][session_id]["session_data"] = {}
        _storage["pending_sessions"][session_id]["session_data"]["message_type"] = "EXPIRED"
        _mark_dirty("pending_sessions", session_id)
        save_storage()
        return True
    return False

def update_pending_session_field(session_id: str, field: str, value: Any) -> bool:
    """Update a field inside session_data of a pending session.

    C3 FIX: the whole row is now mutated inside ``_storage_lock`` so a
    concurrent whole-blob replace (replace_pending_session_data) or a
    parallel field write cannot clobber this update between its
    read-modify-write steps.
    """
    with _storage_lock:
        row = _storage.get("pending_sessions", {}).get(session_id)
        if row is None:
            return False
        if "session_data" not in row or not isinstance(row["session_data"], dict):
            row["session_data"] = {}
        row["session_data"][field] = value
        _mark_dirty("pending_sessions", session_id)
    save_storage()
    return True

def set_pending_session_field(session_id: str, field: str, value: Any) -> bool:
    """Set a TOP-LEVEL field on a pending session (not inside session_data).

    C3 FIX: mutated inside ``_storage_lock`` (same reasoning as
    update_pending_session_field).
    """
    with _storage_lock:
        row = _storage.get("pending_sessions", {}).get(session_id)
        if row is None:
            return False
        row[field] = value
        _mark_dirty("pending_sessions", session_id)
    save_storage()
    return True

def delete_pending_session(session_id: str) -> bool:
    """Delete a pending session by ID."""
    if session_id in _storage.get("pending_sessions", {}):
        del _storage["pending_sessions"][session_id]
        _mark_dirty("pending_sessions", None)  # deletion → full table sync
        save_storage()
        logger.info(f"Deleted pending session {session_id}")
        return True
    return False

def replace_pending_session_data(session_id: str, new_data: dict) -> bool:
    """Replace the entire session_data dict (avoids nesting bug).

    C3 FIX: the check-and-assign now runs inside ``_storage_lock`` so a
    concurrent field update (set/update_pending_session_field) or a parallel
    replace can never interleave with this swap and lose a flag.
    """
    with _storage_lock:
        row = _storage.get("pending_sessions", {}).get(session_id)
        if row is None:
            return False
        row["session_data"] = new_data
        _mark_dirty("pending_sessions", session_id)
    save_storage()
    return True

def get_exportable_sessions() -> Dict[str, Dict[str, Any]]:
    """Get all sessions that are ready for export (verified/approved sessions).
    
    Returns:
        Dict of session_id -> session_data for approved/verified sessions
    """
    exportable = {}
    pending = _storage.get("pending_sessions", {})
    
    for session_id, session_data in pending.items():
        status = session_data.get("status", "pending")
        session_inner = session_data.get("session_data", {})
        
        if status == "approved" or session_inner.get("verified", False):
            exportable[session_id] = session_data
    
    return exportable

def get_pending_exportable_sessions() -> Dict[str, Dict[str, Any]]:
    """Get all pending sessions (not yet verified).
    
    Returns:
        Dict of session_id -> session_data for pending sessions
    """
    pending_export = {}
    pending = _storage.get("pending_sessions", {})
    
    for session_id, session_data in pending.items():
        status = session_data.get("status", "pending")
        session_inner = session_data.get("session_data", {})
        
        if status == "pending" and not session_inner.get("verified", False):
            pending_export[session_id] = session_data
    
    return pending_export

def get_session_export_counts() -> Dict[str, int]:
    """Get counts of exportable sessions.
    
    Returns:
        Dict with 'active' and 'pending' counts
    """
    pending = _storage.get("pending_sessions", {})
    active_count = 0
    pending_count = 0
    
    for session_id, session_data in pending.items():
        status = session_data.get("status", "pending")
        session_inner = session_data.get("session_data", {})
        
        if status == "approved" or session_inner.get("verified", False):
            active_count += 1
        elif status == "pending" and not session_inner.get("verified", False):
            pending_count += 1
    
    return {"active": active_count, "pending": pending_count}

def delete_exported_sessions(session_ids: List[str]) -> int:
    """Delete sessions from storage after export.
    
    Note: This only deletes from bot storage, NOT from private channel.
    
    Args:
        session_ids: List of session IDs to delete
        
    Returns:
        Number of sessions deleted
    """
    deleted = 0
    with _storage_lock:
        for session_id in session_ids:
            if session_id in _storage.get("pending_sessions", {}):
                del _storage["pending_sessions"][session_id]
                deleted += 1
        
        if deleted > 0:
            _mark_dirty("pending_sessions", None)  # deletions → full table sync
            _do_save_background()
            logger.info(f"Deleted {deleted} exported sessions from storage")
    
    return deleted
