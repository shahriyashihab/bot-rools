"""Refactored from bot.utils.storage: Withdrawal request and config storage operations."""
import random

from bot.data.state import (
    Any, Dict, List, Optional, _do_save_background, _mark_dirty,
    _mark_settings_key_dirty, _storage, _storage_lock, datetime, save_storage,
    save_storage_now, thread_safe,
)

# H4: the in-memory withdrawals list used to grow FOREVER (only viewed as
# [-50:]) and was full-replaced on EVERY sync. It is now bounded: every
# PENDING request is always kept (money-critical: approve/reject must find
# it), and the newest terminal history entries beyond this cap are dropped
# from the sync store. The durable ledger already lives in the async
# ``withdrawal_logs`` table (90-day retention, written on every request), so
# pruning only ever trims what the admin views would show.
WITHDRAWALS_MAX_ENTRIES = int(__import__("os").environ.get("WITHDRAWALS_MAX_ENTRIES", "2000") or 2000)


def _prune_withdraw_history_locked() -> int:
    """Drop the OLDEST settled entries once the list exceeds its cap.

    MUST hold _storage_lock. Pending/processing requests are NEVER dropped.
    Returns the number of entries removed (0 when under the cap).
    """
    lst = _storage.get("withdrawals")
    if not isinstance(lst, list) or len(lst) <= WITHDRAWALS_MAX_ENTRIES:
        return 0
    excess = len(lst) - WITHDRAWALS_MAX_ENTRIES
    kept = []
    for w in lst:
        if excess > 0 and (not isinstance(w, dict) or w.get("status") != "pending"):
            excess -= 1
            continue
        kept.append(w)
    _storage["withdrawals"] = kept
    return len(lst) - len(kept)


def _make_request_id() -> str:
    """Return a short, unique, alphanumeric transaction number.

    Format: W + 7 random uppercase alphanumeric chars (e.g., W8K9M2P1).
    36^7 = 78 billion combinations - collision virtually impossible.
    Used as stable identity for approve/reject callbacks and Telegram search.
    """
    import string
    chars = string.ascii_uppercase + string.digits
    return 'W' + ''.join(random.choices(chars, k=7))

def try_deduct_balance_for_withdraw(user_id, amount: float) -> bool:
    """
    Atomically check balance and deduct for withdrawal.
    Prevents double-spend by doing check-and-deduct in one locked operation.

    P0-3: the check-and-deduct previously ran WITHOUT ``_storage_lock`` - two
    concurrent withdraw requests (or a withdraw racing a credit) could both
    pass the balance check and deduct twice (double-spend). The whole
    check-and-deduct now happens inside the same lock domain every other
    money path uses.

    Returns:
        True if balance was sufficient and deducted, False otherwise
    """
    user_id = str(user_id)
    with _storage_lock:
        if user_id not in _storage["users"]:
            return False

        try:
            current_balance = float(_storage["users"][user_id].get("balance") or 0)
        except (TypeError, ValueError):
            current_balance = 0.0
        try:
            amount_f = float(amount)
        except (TypeError, ValueError):
            return False
        if current_balance < amount_f or amount_f <= 0:
            return False

        _storage["users"][user_id]["balance"] = current_balance - amount_f
        _mark_dirty("users", user_id)

    _do_save_background()

    try:
        from bot.utils.fast_cache import invalidate_user_cache
        invalidate_user_cache(int(user_id))
    except (ImportError, ValueError, TypeError):
        pass

    return True

def record_withdraw_request_only(user_id, method: str, amount: float, wallet_address: str, country_breakdown: Optional[Dict] = None) -> None:
    """Record withdrawal request without deducting balance (balance already deducted atomically).

    Money-critical: persists immediately so a pending withdrawal request is never lost.

    ``country_breakdown`` is the payment-accurate snapshot of which countries'
    numbers produced this balance (per-country count + total of the ACTUAL
    prices each session was credited at). It is stored inside the request so
    the admin panel, approve/reject reports and logs always show the same
    numbers - even if the session tracker changes later.
    """
    request = {
        "request_id": _make_request_id(),
        "user_id": str(user_id),
        "method": method,
        "amount": amount,
        "wallet_address": wallet_address,
        "timestamp": datetime.now().isoformat(),
        "status": "pending"
    }
    if country_breakdown:
        request["country_breakdown"] = country_breakdown
    with _storage_lock:
        _storage["withdrawals"].append(request)
        # H4: bound the list + mark the table so the bounded full-replace
        # sync actually runs (the table is no longer rewritten on every
        # unrelated save).
        _prune_withdraw_history_locked()
        _mark_dirty("withdrawals", None)
    save_storage_now()


def create_withdraw_request_atomic(user_id, method: str, amount: float, wallet_address: str, country_breakdown: Optional[Dict] = None) -> Dict[str, Any]:
    """Deduct the balance AND record the pending request in ONE lock domain.

    F-6 FIX: the old flow deducted the balance synchronously but recorded the
    request inside a spawned background task. A hard kill between the two
    steps left the balance deducted with NO request anywhere - silent user
    money loss. Now the check-and-deduct, the record append and the dirty
    marks all commit inside a single ``_storage_lock`` acquisition and
    persist immediately with ``save_storage_now``: either both exist or
    neither does.

    Returns:
        {"success": True, "request_id": ...} or
        {"success": False, "error": "unknown_user" | "invalid_amount" | "insufficient"}
    """
    user_id = str(user_id)
    try:
        amount_f = float(amount)
    except (TypeError, ValueError):
        return {"success": False, "error": "invalid_amount"}
    if amount_f != amount_f or amount_f == float("inf") or amount_f <= 0:
        return {"success": False, "error": "invalid_amount"}

    result: Dict[str, Any] = {"success": False, "error": "unknown_user"}
    with _storage_lock:
        if user_id not in _storage["users"]:
            return {"success": False, "error": "unknown_user"}
        try:
            current = float(_storage["users"][user_id].get("balance") or 0)
        except (TypeError, ValueError):
            current = 0.0
        if current < amount_f:
            return {"success": False, "error": "insufficient"}

        request = {
            "request_id": _make_request_id(),
            "user_id": user_id,
            "method": method,
            "amount": amount_f,
            "wallet_address": wallet_address,
            "timestamp": datetime.now().isoformat(),
            "status": "pending",
        }
        if country_breakdown:
            request["country_breakdown"] = country_breakdown

        # Commit the money and the record together.
        _storage["users"][user_id]["balance"] = current - amount_f
        _storage["withdrawals"].append(request)
        _prune_withdraw_history_locked()
        _mark_dirty("users", user_id)
        _mark_dirty("withdrawals", None)
        result = {"success": True, "request_id": request["request_id"]}

    save_storage_now()

    try:
        from bot.utils.fast_cache import invalidate_user_cache
        invalidate_user_cache(int(user_id))
    except (ImportError, ValueError, TypeError):
        pass
    return result


def build_country_breakdown(user_id, expected_total: Optional[float] = None) -> Dict[str, Any]:
    """Build the payment-accurate per-country breakdown for a user.

    Reads the session tracker (every successful session stores the REAL amount
    it was credited at, plus its country code) and groups by country:

        {
          "+880": {"count": 5, "total": 2.50},
          "+82":  {"count": 2, "total": 2.00},
        }

    The grand total equals the sum of every credited session, i.e. exactly the
    balance the user built from verified numbers (no current-price guessing).

    When ``expected_total`` is given (the withdraw amount), any difference
    (manual bonus / adjustment credited via ``add_balance_for_user``) is
    returned as an ``"other"`` entry so the displayed total ALWAYS equals the
    amount actually being withdrawn - payment-critical, no mismatch.

    Returns ``{"countries": {...}, "total": float, "numbers": int}``.
    """
    user_id = str(user_id)
    tracker = _storage.get("session_tracker", {}).get(user_id, [])
    countries = {}
    total = 0.0
    numbers = 0
    for entry in tracker:
        try:
            amt = float(entry.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        cc = entry.get("country_code") or "other"
        if cc not in countries:
            countries[cc] = {"count": 0, "total": 0.0}
        countries[cc]["count"] += 1
        countries[cc]["total"] = round(countries[cc]["total"] + amt, 4)
        total = round(total + amt, 4)
        numbers += 1

    if expected_total is not None:
        try:
            expected_total = float(expected_total)
        except (TypeError, ValueError):
            expected_total = None
        if expected_total is not None:
            diff = round(expected_total - total, 2)
            # Manual bonus / adjustment above session earnings -> show it as
            # an explicit "Other" line so totals always reconcile.
            if diff > 0.005:
                countries.setdefault("other", {"count": 0, "total": 0.0})
                countries["other"]["total"] = round(countries["other"].get("total", 0.0) + diff, 2)
                total = round(total + diff, 2)
            # Negative difference (refunds / corrections) -> show as a note so
            # the admin sees the true earnings, not a wrong-looking total.
            elif diff < -0.005:
                countries.setdefault("other", {"count": 0, "total": 0.0})
                countries["other"]["total"] = round(countries["other"].get("total", 0.0) + diff, 2)
                total = round(total + diff, 2)

    return {
        "countries": countries,
        "total": round(total, 2),
        "numbers": numbers,
    }


def format_country_breakdown(breakdown: Optional[Dict], include_header: bool = True) -> str:
    """Render the breakdown as a clean, aligned text block.

    Examples:
        🇧🇩 Bangladesh   5 numbers   $2.50
        🇰🇷 South Korea  2 numbers   $2.00
        ────────────────────────────
        Total: 8 numbers = $5.00

    Returns an empty string when there is no breakdown data.
    """
    if not breakdown or not isinstance(breakdown, dict):
        return ""
    countries = breakdown.get("countries") or {}
    total = breakdown.get("total", 0.0)
    numbers = breakdown.get("numbers", 0)
    if not countries:
        return ""

    from bot.utils.country_data import get_flag, get_country_name

    lines = []
    if include_header:
        lines.append(f"🌍 BALANCE SOURCE — {numbers} number{'s' if numbers != 1 else ''}")
        lines.append("────────────────────────────")
    # Sort by total descending (biggest contributor first) - professional.
    for cc, info in sorted(countries.items(), key=lambda kv: -kv[1].get("total", 0)):
        flag = get_flag(cc) if cc != "other" else "➕"
        name = get_country_name(cc) if cc != "other" else "Other/Bonus"
        cnt = info.get("count", 0)
        tot = info.get("total", 0.0)
        amount_str = f"-${abs(tot):.2f}" if tot < 0 else f"${tot:.2f}"
        if cnt > 0:
            number_word = "number" if cnt == 1 else "numbers"
            lines.append(f"{flag} {name:<16} {cnt} {number_word:<7} {amount_str}")
        else:
            # Manual bonus / adjustment line (no session count)
            label = "Other/Bonus" if tot >= 0 else "Adjustment"
            lines.append(f"{flag} {label:<16} {'':9} {amount_str}")
    lines.append("────────────────────────────")
    lines.append(f"✅ Total: {numbers} numbers = ${total:.2f}")
    return "\n".join(lines)

def force_logout_users() -> None:
    """Force logout all users."""
    from bot.data.logs import append_bounded
    append_bounded("admin_logs", {
        "action": "force_logout_all",
        "timestamp": datetime.now().isoformat()
    })
    save_storage()

def save_withdraw_state(user_id: int, data: Dict[str, Any]) -> None:
    """Persist an in-progress withdraw flow so it survives a bot restart.

    Only JSON-serializable fields are stored (balance, step, method, ...).
    Immediate save so the state survives a hard kill mid-flow.
    """
    _storage.setdefault("withdraw_sessions", {})[str(user_id)] = data
    _mark_settings_key_dirty("withdraw_sessions")
    save_storage_now()

def load_withdraw_state(user_id: int) -> Optional[Dict[str, Any]]:
    """Load a persisted in-progress withdraw flow, if any."""
    return _storage.get("withdraw_sessions", {}).get(str(user_id))

def clear_withdraw_state(user_id: int) -> None:
    """Remove a persisted withdraw flow once it completes or is cancelled."""
    sess = _storage.get("withdraw_sessions")
    if sess and str(user_id) in sess:
        sess.pop(str(user_id), None)
        _mark_settings_key_dirty("withdraw_sessions")
        save_storage()

def is_withdraw_enabled() -> bool:
    """Check if withdrawal is enabled."""
    return not _storage.get("withdraw_disabled", False)

def toggle_withdraw() -> bool:
    """Toggle withdraw enabled/disabled."""
    current = _storage.get("withdraw_disabled", False)
    _storage["withdraw_disabled"] = not current
    _mark_settings_key_dirty("withdraw_disabled")
    save_storage()
    return not _storage["withdraw_disabled"]

def set_user_withdraw_config(user_id, min_amt=None, max_amt=None, disabled=None):
    """Set custom withdraw configuration for a specific user."""
    user_id = str(user_id)
    if user_id not in _storage.get("users", {}):
        return False
    
    if "withdraw_config" not in _storage["users"][user_id]:
        _storage["users"][user_id]["withdraw_config"] = {}
    
    if min_amt is not None:
        _storage["users"][user_id]["withdraw_config"]["min"] = float(min_amt)
    if max_amt is not None:
        _storage["users"][user_id]["withdraw_config"]["max"] = float(max_amt)
    if disabled is not None:
        _storage["users"][user_id]["withdraw_config"]["disabled"] = bool(disabled)
    
    _mark_dirty("users", user_id)
    save_storage()
    try:
        from bot.utils.fast_cache import invalidate_user_cache
        invalidate_user_cache(int(user_id))
    except:
        pass
    return True

def get_user_withdraw_config(user_id) -> Dict[str, Any]:
    """Get custom withdraw configuration for a specific user."""
    user_id = str(user_id)
    user = _storage.get("users", {}).get(user_id, {})
    return user.get("withdraw_config", {})

def get_pending_withdraw_requests() -> List[Dict[str, Any]]:
    """Get pending withdraw requests."""
    return [w for w in _storage.get("withdrawals", []) if w.get("status") == "pending"]

def approve_withdraw_request(index_or_id) -> Dict[str, Any]:
    """Approve a pending withdraw request.

    Accepts either a stable ``request_id`` (preferred - race-free: the id is
    unique per request, so a click always acts on the EXACT request the admin
    saw) or a legacy positional index into the pending list.

    E-4 FIX: the ENTIRE find-and-flip now runs inside ``_storage_lock``.
    Previously the pending-check, status flip and save ran unlocked, so two
    concurrent admin clicks (approve+approve or approve+reject) could BOTH
    observe ``status == "pending"`` - paying AND refunding the same request.
    The status flip is also matched by request_id first (legacy rows fall
    back to the user_id+timestamp pair) and persisted with
    ``save_storage_now`` (a crash inside the old 0.5s debounce window could
    erase the approval and let a later reject refund an ALREADY-PAID payout).

    Args:
        index_or_id: request_id string or index of the pending request

    Returns:
        Dict with request details or error info
    """
    result: Dict[str, Any] = {"success": False, "error": "Request not found"}
    with _storage_lock:
        all_withdrawals = _storage.get("withdrawals", [])
        pending = [w for w in all_withdrawals if w.get("status") == "pending"]

        # Match by stable request_id first (CRITICAL: index-based matching could
        # act on a DIFFERENT request if the pending list changed between the
        # panel render and the admin's click - approving/rejecting the wrong
        # user's withdrawal).
        target_request = None
        if isinstance(index_or_id, str) and str(index_or_id).lower().startswith("w"):
            for w in pending:
                if w.get("request_id") == index_or_id:
                    target_request = w
                    break
        else:
            index = index_or_id
            if index < 0 or index >= len(pending):
                return {"success": False, "error": "Invalid request index"}
            target_request = pending[index]

        if target_request is None:
            return {"success": False, "error": "Request not found"}

        target_id = target_request.get("request_id")
        for w in all_withdrawals:
            if target_id:
                if w.get("request_id") == target_id and w.get("status") == "pending":
                    w["status"] = "approved"
                    w["approved_at"] = datetime.now().isoformat()
                    # H4: the withdrawals table is no longer rewritten on every save -
                    # mutations MUST mark it or the status flip would never persist.
                    _mark_dirty("withdrawals", None)
                    result = {
                        "success": True,
                        "user_id": w.get("user_id"),
                        "amount": w.get("amount"),
                        "method": w.get("method"),
                        "wallet_address": w.get("wallet_address"),
                        "timestamp": w.get("timestamp", ""),
                        "country_breakdown": w.get("country_breakdown"),
                    }
                    break
            else:
                # Legacy row without a request_id: keep the historical
                # (user_id, timestamp) pair matching for backwards compatibility.
                if (w.get("user_id") == target_request.get("user_id") and
                    w.get("timestamp") == target_request.get("timestamp") and
                    w.get("status") == "pending"):
                    w["status"] = "approved"
                    w["approved_at"] = datetime.now().isoformat()
                    _mark_dirty("withdrawals", None)
                    result = {
                        "success": True,
                        "user_id": w.get("user_id"),
                        "amount": w.get("amount"),
                        "method": w.get("method"),
                        "wallet_address": w.get("wallet_address"),
                        "timestamp": w.get("timestamp", ""),
                        "country_breakdown": w.get("country_breakdown"),
                    }
                    break

    # Persist OUTSIDE the lock (save acquires its own locks; holding
    # _storage_lock across the DB write risks lock-order inversion).
    if result.get("success"):
        save_storage_now()
    return result

def reject_withdraw_request(index_or_id) -> Dict[str, Any]:
    """Reject a pending withdraw request and refund balance.

    Accepts either a stable ``request_id`` (preferred - race-free: the id is
    unique per request, so a click always acts on the EXACT request the admin
    saw) or a legacy positional index into the pending list.

    E-4 FIX: the ENTIRE find-and-flip-and-refund now runs inside
    ``_storage_lock``. Previously the pending-check, status flip and refund
    ran unlocked, so a concurrent approve + reject could BOTH observe
    ``status == "pending"`` - the user received the payout AND the refund.

    Args:
        index_or_id: request_id string or index of the pending request

    Returns:
        Dict with request details or error info
    """
    result: Dict[str, Any] = {"success": False, "error": "Request not found"}
    with _storage_lock:
        all_withdrawals = _storage.get("withdrawals", [])
        pending = [w for w in all_withdrawals if w.get("status") == "pending"]

        # Match by stable request_id first (CRITICAL: index-based matching could
        # refund the WRONG user's balance if the pending list changed between the
        # panel render and the admin's click).
        target_request = None
        if isinstance(index_or_id, str) and str(index_or_id).lower().startswith("w"):
            for w in pending:
                if w.get("request_id") == index_or_id:
                    target_request = w
                    break
        else:
            index = index_or_id
            if index < 0 or index >= len(pending):
                return {"success": False, "error": "Invalid request index"}
            target_request = pending[index]

        if target_request is None:
            return {"success": False, "error": "Request not found"}

        user_id = target_request.get("user_id")
        amount = target_request.get("amount", 0)

        target_id = target_request.get("request_id")
        for w in all_withdrawals:
            if target_id:
                if w.get("request_id") == target_id and w.get("status") == "pending":
                    w["status"] = "rejected"
                    w["rejected_at"] = datetime.now().isoformat()

                    if user_id in _storage.get("users", {}):
                        current_balance = _storage["users"][user_id].get("balance", 0)
                        _storage["users"][user_id]["balance"] = current_balance + amount
                        _mark_dirty("users", user_id)

                    _mark_dirty("withdrawals", None)
                    result = {
                        "success": True,
                        "user_id": user_id,
                        "amount": amount,
                        "method": w.get("method"),
                        "wallet_address": w.get("wallet_address", ""),
                        "timestamp": w.get("timestamp", ""),
                        "country_breakdown": w.get("country_breakdown"),
                        "refunded": True
                    }
                    break
            else:
                # Legacy row without a request_id: keep the historical
                # (user_id, timestamp) pair matching for backwards compatibility.
                if (w.get("user_id") == user_id and
                    w.get("timestamp") == target_request.get("timestamp") and
                    w.get("status") == "pending"):
                    w["status"] = "rejected"
                    w["rejected_at"] = datetime.now().isoformat()

                    if user_id in _storage.get("users", {}):
                        current_balance = _storage["users"][user_id].get("balance", 0)
                        _storage["users"][user_id]["balance"] = current_balance + amount
                        _mark_dirty("users", user_id)

                    _mark_dirty("withdrawals", None)
                    result = {
                        "success": True,
                        "user_id": user_id,
                        "amount": amount,
                        "method": w.get("method"),
                        "wallet_address": w.get("wallet_address", ""),
                        "timestamp": w.get("timestamp", ""),
                        "country_breakdown": w.get("country_breakdown"),
                        "refunded": True
                    }
                    break

    if result.get("success"):
        save_storage_now()

        try:
            from bot.utils.fast_cache import invalidate_user_cache
            invalidate_user_cache(int(result["user_id"]))
        except (ImportError, ValueError, TypeError):
            pass

    return result

def get_withdraw_logs() -> List[Dict[str, Any]]:
    """Get all withdraw logs."""
    return _storage.get("withdrawals", [])[-50:]

def get_min_withdraw_amounts() -> Dict[str, float]:
    """Get minimum withdraw amounts for each method."""
    return _storage.get("min_withdraw", {
        "usdt": 10.0,
        "trx": 10.0,
        "cardname": 10.0,
        "binance": 10.0
    })

def get_min_withdraw_for_method(method: str) -> float:
    """Get minimum withdraw for a specific method."""
    mins = get_min_withdraw_amounts()
    return mins.get(method.lower(), 10.0)

def set_min_withdraw_for_method(method: str, amount: float) -> None:
    """Set minimum withdraw for a specific method."""
    if "min_withdraw" not in _storage:
        _storage["min_withdraw"] = {
            "usdt": 10.0,
            "trx": 10.0,
            "cardname": 10.0,
            "binance": 10.0
        }
    _storage["min_withdraw"][method.lower()] = amount
    _mark_settings_key_dirty("min_withdraw")
    save_storage()
