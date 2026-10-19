"""Refactored from bot.utils.storage: User account storage operations."""
from typing import Tuple

from bot.data.state import Any, Dict, List, Union, _balance_credit_lock, _do_save, _do_save_background, _mark_dirty, _parse_date, _storage, _storage_lock, datetime, logger, save_storage, save_storage_now, thread_safe, timedelta

def get_user_info(user_id, create_if_missing: bool = True) -> Dict[str, Any]:
    """Get user information with thread-safe access.
    
    Args:
        user_id: The user ID to look up
        create_if_missing: If True, create a default user record if not found.
                          If False, return empty dict for unknown users.
    """
    user_id = str(user_id)
    if user_id not in _storage["users"]:
        if create_if_missing:
            _storage["users"][user_id] = {
                "balance": 0,
                "sessions": 0,
                "banned": False,
                "joined": datetime.now().isoformat(),
                "success_count": 0,
                "channel_joined": False
            }
            _mark_dirty("users", user_id)
            save_storage()
        else:
            return {}
    return _storage["users"].get(user_id, {})

def update_user_info(user_id, **kwargs) -> None:
    """Update user information with thread-safe access.

    P0-4: the read-modify-write previously ran WITHOUT ``_storage_lock`` - a
    concurrent update (ban flag vs balance edit) could lose one side's write
    (last-writer-wins on the whole kwargs merge). The mutation now happens
    inside the same lock domain every other user mutation uses.
    """
    user_id = str(user_id)
    with _storage_lock:
        if user_id not in _storage["users"]:
            _storage["users"][user_id] = {
                "balance": 0,
                "sessions": 0,
                "banned": False,
                "joined": datetime.now().isoformat(),
                "success_count": 0,
                "channel_joined": False
            }
        _storage["users"][user_id].update(kwargs)
        _mark_dirty("users", user_id)
    save_storage()

def get_all_users() -> List[str]:
    """Get list of all user IDs."""
    return list(_storage["users"].keys())

def ban_user(user_id) -> None:
    """Ban a user."""
    update_user_info(user_id, banned=True)

def unban_user(user_id) -> None:
    """Unban a user."""
    update_user_info(user_id, banned=False)

def is_user_banned(user_id) -> bool:
    """Check if user is banned."""
    user = get_user_info(user_id)
    return user.get("banned", False)

def delete_user(user_id) -> None:
    """Delete a user."""
    user_id = str(user_id)
    if user_id in _storage["users"]:
        del _storage["users"][user_id]
        _mark_dirty("users", None)  # deletion → full table sync
        save_storage()

def add_balance_for_user(user_id, amount: float) -> None:
    """Add or subtract balance from user.

    Money-critical: persists immediately (save_storage_now) instead of using
    the 0.5s debounced save, so the balance survives a hard kill/restart.

    F-7 FIX: the balance was previously READ outside ``_storage_lock``
    (via ``get_user_info``) and written inside it - a concurrent credit
    between the read and the write was silently lost (lost-update). The whole
    read-modify-write now runs inside the same lock domain. ``amount`` may be
    negative (admin "subtracted" path) but NaN/+inf are rejected.
    """
    user_id = str(user_id)
    try:
        amount_f = float(amount)
    except (TypeError, ValueError):
        return
    if amount_f != amount_f or amount_f == float("inf") or amount_f == float("-inf"):
        return

    with _storage_lock:
        if user_id not in _storage["users"]:
            get_user_info(user_id)
        try:
            current = float(_storage["users"][user_id].get("balance") or 0)
        except (TypeError, ValueError):
            current = 0.0
        _storage["users"][user_id]["balance"] = current + amount_f
        _mark_dirty("users", user_id)
    save_storage_now()
    
    try:
        from bot.utils.fast_cache import invalidate_user_cache
        invalidate_user_cache(int(user_id))
    except (ImportError, ValueError, TypeError):
        pass

def atomic_deduct_balance_by_admin(user_id, amount: float) -> Tuple[bool, str, float]:
    """Admin "Deduct Balance": atomically subtract a STRICTLY POSITIVE amount.

    BUG1 fix: the admin handler previously computed ``current - amount`` with
    NO sign check, so a negative amount INCREASED the balance (money minting).
    The whole read-check-write now runs inside ``_storage_lock``:
      - rejects amount <= 0 (no sign-flip games possible)
      - rejects overdraft (amount > current balance)
      - commits the new balance + marks dirty inside the same lock domain

    Returns:
        (success, error_code, new_balance) - error_code is "" on success,
        "unknown_user" / "invalid_amount" / "insufficient" otherwise.
    """
    user_id = str(user_id)
    with _storage_lock:
        if user_id not in _storage["users"]:
            return False, "unknown_user", 0.0
        try:
            amount_f = float(amount)
        except (TypeError, ValueError):
            return False, "invalid_amount", 0.0
        # NaN fails every comparison and +inf overflows the overdraft check's
        # intent - both are non-deductible garbage, reject before arithmetic.
        if amount_f != amount_f or amount_f == float("inf"):
            return False, "invalid_amount", 0.0
        if amount_f <= 0:
            return False, "invalid_amount", 0.0
        try:
            current = float(_storage["users"][user_id].get("balance") or 0)
        except (TypeError, ValueError):
            current = 0.0
        if amount_f > current:
            return False, "insufficient", current
        new_balance = current - amount_f
        _storage["users"][user_id]["balance"] = new_balance
        _mark_dirty("users", user_id)

    save_storage_now()

    try:
        from bot.utils.fast_cache import invalidate_user_cache
        invalidate_user_cache(int(user_id))
    except (ImportError, ValueError, TypeError):
        pass
    return True, "", new_balance


def atomic_credit_balance_for_session(session_id: str, user_id: Union[int, str], amount: float) -> bool:
    """
    Atomically credit balance for a session.
    
    Args:
        session_id: The session identifier
        user_id: The user to credit balance to
        amount: The amount to credit
        
    Returns:
        True if balance was credited, False if already credited or session not found
    """
    with _balance_credit_lock:
        with _storage_lock:
            pending = _storage.get("pending_sessions", {}).get(session_id)
            if not pending:
                logger.warning(f"ATOMIC CREDIT: Session {session_id} not found")
                return False
            
            if pending.get("balance_credited", False):
                logger.debug(f"ATOMIC CREDIT: Balance already credited for session {session_id}")
                return False
            
            current_status = pending.get("status", "pending")
            if current_status in ("expired", "rejected"):
                logger.warning(f"ATOMIC CREDIT: Session {session_id} already {current_status} - refusing credit")
                return False
            
            # CRITICAL ORDERING: do the ARITHMETIC FIRST, set the flags only
            # after it has provably succeeded. The old order wrote
            # ``balance_credited=True`` + ``status="approved"`` before touching
            # the balance, so a non-numeric ``balance`` (or ``amount``) raised
            # mid-way and left the row claiming "already credited" with no money
            # added and no ``_mark_dirty`` - the guard at the top of this
            # function then refused every retry, and because nothing was marked
            # dirty the DB never learned about it either. The user simply never
            # got paid and no scanner could repair it.
            user_id = str(user_id)
            if user_id not in _storage["users"]:
                get_user_info(user_id)

            try:
                credit_amount = float(amount)
            except (TypeError, ValueError):
                logger.error(
                    f"ATOMIC CREDIT: non-numeric amount {amount!r} for session "
                    f"{session_id} - refusing credit (row left untouched, retryable)"
                )
                return False

            raw_balance = _storage["users"][user_id].get("balance", 0)
            try:
                current_balance = float(raw_balance or 0)
            except (TypeError, ValueError):
                logger.warning(
                    f"ATOMIC CREDIT: user {user_id} balance was non-numeric "
                    f"({type(raw_balance).__name__}) - treating it as 0 so the "
                    f"credit still lands"
                )
                current_balance = 0.0
            new_balance = current_balance + credit_amount

            try:
                current_sessions = int(_storage["users"][user_id].get("sessions", 0) or 0)
            except (TypeError, ValueError):
                current_sessions = 0

            # Arithmetic done - commit money and flags together.
            _storage["users"][user_id]["balance"] = new_balance
            _storage["users"][user_id]["sessions"] = current_sessions + 1
            pending["balance_credited"] = True
            pending["status"] = "approved"

            # Everything below is BOOKKEEPING (stats, trackers, counters). The
            # money is already committed above, so a failure here must never
            # abort the function - otherwise the flags would be set in memory
            # while ``_mark_dirty``/``_do_save_background`` never ran and the
            # credit would be lost on the next restart. Best-effort, logged.
            phone = pending.get("phone", "") or (pending.get("session_data") or {}).get("phone", "")
            final_country = "other"
            try:
                country_code = pending.get("country_code")
                if not country_code and phone:
                    from bot.utils.country_data import get_country_code_from_phone
                    clean_phone = phone.replace("*", "").replace("-", "").replace(" ", "")
                    country_code = get_country_code_from_phone(clean_phone)

                if "country_sessions" not in _storage["users"][user_id]:
                    _storage["users"][user_id]["country_sessions"] = {}
                country_sessions = _storage["users"][user_id]["country_sessions"]
                final_country = country_code if country_code else "other"
                country_sessions[final_country] = country_sessions.get(final_country, 0) + 1

                if "session_tracker" not in _storage:
                    _storage["session_tracker"] = {}
                if user_id not in _storage["session_tracker"]:
                    _storage["session_tracker"][user_id] = []

                spam_status = pending.get("spam_status", "unknown")
                session_info = pending.get("session_data", {}) or {}
                spam_details = session_info.get("spam_status_details", {}) or {}

                _storage["session_tracker"][user_id].append({
                    "session_id": session_id,
                    "phone": phone,
                    "country_code": final_country,
                    "amount": credit_amount,
                    "spam_status": spam_status,
                    "verified_at": datetime.now().isoformat(),
                    "is_fake": spam_details.get("is_fake", False),
                    "is_scam": spam_details.get("is_scam", False),
                    "is_premium": spam_details.get("is_premium", False)
                })

                if phone:
                    # Permanently register the successful number so the same-number
                    # rule can block it for every user (no repeated credit farming).
                    successful_numbers = _storage.get("successful_numbers")
                    if not isinstance(successful_numbers, dict):
                        successful_numbers = {}
                    successful_numbers[phone] = {
                        "user_id": user_id,
                        "verified_at": datetime.now().isoformat(),
                    }
                    _storage["successful_numbers"] = successful_numbers
            except Exception as bookkeeping_err:
                logger.error(
                    f"ATOMIC CREDIT: bookkeeping failed for session {session_id} "
                    f"(money IS credited, stats may be incomplete): {bookkeeping_err}"
                )

            # CRITICAL (capacity): a SUCCESS account is now truly verified and
            # credited. Only successful accounts consume the admin-configured
            # country capacity - rejected / failed accounts never do. This is
            # the single authoritative point: every SUCCESS path (delayed task,
            # monitor finalize, approval, recovery worker) credits through this
            # function and returns False for duplicates, so the counter can
            # never be double-incremented for one account.
            try:
                from bot.data.counters import increment_daily_success_count
                success_count = increment_daily_success_count(final_country)
                logger.info(f"ATOMIC CREDIT: country {final_country} SUCCESS today: {success_count}")
            except Exception as success_err:
                logger.warning(f"ATOMIC CREDIT: could not record country success counter: {success_err}")

            # Mark every mutated row dirty and persist on a background thread
            # (non-blocking for the event loop, durable within ~50-100ms).
            # session_tracker is marked at ROW level (this user only): the
            # sync upserts just this user's rows instead of rewriting the
            # whole table, which at 40-50k sessions/day would be a
            # multi-second full-table rewrite on every single credit.
            _mark_dirty("pending_sessions", session_id)
            _mark_dirty("users", str(user_id))
            _mark_dirty("session_tracker", user_id)
            if phone:
                _mark_dirty("successful_numbers", phone)
            _do_save_background()

            logger.info(f"ATOMIC CREDIT: ${credit_amount} credited to user {user_id} for session {session_id} (country: {final_country})")
            logger.info(f"SESSION TRACKER: Added session {session_id} for user {user_id}")
            return True

def reset_user_channel_joined(user_id=None) -> int:
    """Reset channel_joined flag for one or all users. Returns count of affected users."""
    count = 0
    if user_id is not None:
        user_id = str(user_id)
        if user_id in _storage.get("users", {}):
            _storage["users"][user_id]["channel_joined"] = False
            _mark_dirty("users", user_id)
            count = 1
    else:
        for uid in _storage.get("users", {}):
            if _storage["users"][uid].get("channel_joined", False):
                _storage["users"][uid]["channel_joined"] = False
                _mark_dirty("users", uid)
                count += 1
    if count > 0:
        save_storage()
    return count

def _get_user_pending_session_stats(user_id: str) -> Dict[str, int]:
    """Get pending session statistics for a specific user."""
    i = str(user_id)
    pending_sessions = _storage.get("pending_sessions", {})

    stats = {
        "waiting_24hr": 0,
        "unverified": 0,
        "rejected": 0
    }

    for session_data in list(pending_sessions.values()):
        if str(session_data.get("user_id", "")) != i:
            continue

        if session_data.get("balance_credited", False):
            continue

        status = session_data.get("status", "pending")
        session_info = session_data.get("session_data", {})
        message_type = session_info.get("message_type", "")

        if status in ("rejected", "timeout", "expired"):
            stats["rejected"] += 1
        elif message_type in ("MULTI-DEVICE", "24HR"):
            stats["waiting_24hr"] += 1
        elif status == "pending":
            stats["unverified"] += 1

    return stats

def get_user_stats(user_id=None) -> Union[Dict[str, Any], str]:
    """Get user statistics. If user_id provided, get stats for that user."""
    if user_id is not None:
        user_id = str(user_id)
        user = _storage.get("users", {}).get(user_id)
        if not user:
            return f"User {user_id} not found."
        
        from bot.utils.country_data import get_flag
        
        pending_stats = _get_user_pending_session_stats(user_id)
        
        sessions_count = user.get('sessions', 0)
        country_sessions = user.get('country_sessions', {})
        try:
            user_balance = float(user.get('balance') or 0)
        except (TypeError, ValueError):
            user_balance = 0.0
        
        country_lines = []
        for cc, count in sorted(country_sessions.items(), key=lambda x: x[1], reverse=True):
            if cc == "other":
                country_lines.append(f"🌍 Other: {count}")
            else:
                flag = get_flag(cc)
                country_lines.append(f"{flag} {cc}: {count}")
        country_text = "\n".join(country_lines) if country_lines else "No successful sessions"
        
        return (
            f"📊 **User Stats:**\n"
            f"👤 User ID: `{user_id}`\n"
            f"💰 Balance: **${user_balance:.2f}**\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📈 **Session Summary:**\n"
            f"✅ Total Successful: {sessions_count}\n"
            f"⏳ 24 Hours: {pending_stats['waiting_24hr']}\n"
            f"⚠️ Unverified: {pending_stats['unverified']}\n"
            f"❌ Rejected: {pending_stats['rejected']}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🌍 **Successful Sessions by Country:**\n"
            f"{country_text}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🚫 Banned: {'Yes' if user.get('banned') else 'No'}\n"
            f"📅 Joined: {user.get('joined', 'N/A')}"
        )
    
    users = _storage.get("users", {})
    total = len(users)
    banned = sum(1 for u in users.values() if u.get("banned", False))
    active = total - banned
    try:
        total_balance = sum(float(u.get("balance") or 0) for u in users.values())
        total_sessions = sum(int(u.get("sessions") or 0) for u in users.values())
    except (TypeError, ValueError):
        total_balance = sum(u.get("balance", 0) for u in users.values() if isinstance(u.get("balance"), (int, float)))
        total_sessions = sum(u.get("sessions", 0) for u in users.values() if isinstance(u.get("sessions"), (int, float)))
    
    return {
        "total": total,
        "active": active,
        "banned": banned,
        "total_balance": total_balance,
        "total_sessions": total_sessions
    }

def zero_all_balances() -> int:
    """Zero all user balances and reset session tracker.
    Returns count of affected users.

    R5 FIX: the whole read-mutate cycle now runs inside ``_storage_lock``
    (an in-flight credit could previously land after the snapshot and be
    resurrected by the tracker reset) and persists with ``save_storage_now``
    (the debounced save left a 0.5s crash window with HALF-reset state).
    """
    count = 0
    affected_users = []
    with _storage_lock:
        for user_id in _storage["users"]:
            if _storage["users"][user_id].get("balance", 0) > 0:
                _storage["users"][user_id]["balance"] = 0
                _storage["users"][user_id]["sessions"] = 0
                _mark_dirty("users", user_id)
                affected_users.append(user_id)
                count += 1
        _storage["session_tracker"] = {}
        _mark_dirty("session_tracker", None)
    save_storage_now()
    
    try:
        from bot.utils.fast_cache import invalidate_user_cache
        for uid in affected_users:
            try:
                invalidate_user_cache(int(uid))
            except (ValueError, TypeError):
                pass
    except ImportError:
        pass
    
    return count

def get_total_users():
    """Get total user count and active count (returns tuple for backward compatibility)."""
    users = _storage.get("users", {})
    total = len(users)
    cutoff = datetime.now() - timedelta(hours=24)
    def _has_sessions(u):
        try:
            return int(u.get("sessions") or 0) > 0
        except (TypeError, ValueError):
            return False
    active = sum(1 for u in users.values() 
                 if _parse_date(u.get("joined")) > cutoff or _has_sessions(u))
    return total, active

def get_all_users_list(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Get paginated list of users with their info.
    
    Args:
        limit: Maximum number of users to return (default 50)
        offset: Number of users to skip (default 0)
    """
    users = _storage.get("users", {})
    items = list(users.items())
    sliced = items[offset:offset + limit]
    return [{"user_id": uid, **data} for uid, data in sliced]

def get_all_balances_report():
    """Get report of all user balances.

    Returns:
        tuple: (report_text, total_users, users_with_balance, total_balance)
    """
    users = _storage.get("users", {})
    lines = []
    total_balance = 0
    users_with_balance = 0
    total_users = len(users)
    
    for user_id, data in users.items():
        try:
            balance = float(data.get("balance") or 0)
        except (TypeError, ValueError):
            balance = 0.0
        if balance > 0:
            username = data.get("username", "")
            first_name = data.get("first_name", "Unknown")
            display_name = f"@{username}" if username else first_name
            lines.append(f"👤 {display_name} ({user_id}): ${balance:.2f}")
            total_balance += balance
            users_with_balance += 1
    
    report = "\n".join(lines[:50]) if lines else ""
    
    return report, total_users, users_with_balance, round(total_balance, 2)


def get_user_account_stats(user_id) -> Dict[str, Any]:
    """Compute the professional /account statistics for a user.

    Counts are derived from the REAL stored session records (pending_sessions),
    never approximated:

      - verified          : sessions that were SUCCESSFULLY credited
                            (status == "approved"). Includes clean, limit and
                            spam accounts - they all succeed (lower pay for
                            limit/spam), so they count as verified.
      - unverified        : sessions that failed and are NOT ban/frozen
                            (2FA, multi-device, device logged out, expired...).
      - frozen            : sessions that were BANNED or FROZEN by Telegram.
                            Only these feed the frozen count + frozen balance.
      - frozen_balance    : sum of the rate of ban/frozen sessions. These were
                            NEVER credited to the user's available balance.
      - available_balance : the user's current credited balance.

    Frozen identification: a session is ban/frozen when it was NOT credited AND
    its ``spam_status`` reports a dead account ("spam" = deactivated/banned,
    or explicit "banned"/"frozen"/"deleted"). "limit" and "spam_free" accounts
    that were credited are NOT frozen.
    """
    user_id = str(user_id)
    frozen_statuses = {"spam", "banned", "frozen", "deleted", "deactivated"}

    verified = 0
    unverified = 0
    frozen = 0
    frozen_balance = 0.0

    pending = _storage.get("pending_sessions", {})
    for session in pending.values():
        try:
            if str(session.get("user_id")) != user_id:
                continue
        except (TypeError, ValueError):
            continue
        status = session.get("status", "pending")
        spam_status = str(session.get("spam_status", "") or "").strip().lower()
        try:
            rate = float(session.get("rate") or 0)
        except (TypeError, ValueError):
            rate = 0.0

        if status == "approved":
            verified += 1
            continue

        # Not credited. Ban/frozen accounts belong to the frozen bucket only.
        if spam_status in frozen_statuses:
            frozen += 1
            frozen_balance = round(frozen_balance + rate, 2)
        else:
            unverified += 1

    user = _storage.get("users", {}).get(user_id, {})
    available_balance = float(user.get("balance") or 0)

    return {
        "verified": verified,
        "unverified": unverified,
        "frozen": frozen,
        "frozen_balance": round(frozen_balance, 2),
        "available_balance": available_balance,
        "total_submitted": verified + unverified + frozen,
    }
