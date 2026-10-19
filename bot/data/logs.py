"""Refactored from bot.utils.storage: Transaction and audit log storage operations."""
from bot.data.state import _mark_settings_key_dirty, _storage, datetime, save_storage, thread_safe

# Hard cap for the in-memory log lists. These lists are ALSO pruned by age
# (cleanup_old_data, hourly) but at 40-50k sessions/day a single day of
# traffic would still accumulate hundreds of thousands of dicts in memory
# between cleanups. The cap bounds memory at any volume; the durable
# per-event ledger lives in the async DB tables (90-day retention), so the
# in-memory cap only ever trims what the admin views would show anyway.
LOG_MAX_ENTRIES = 5000

def append_bounded(key: str, entry: dict, cap: int = LOG_MAX_ENTRIES) -> None:
    """Append an entry to an in-memory log list, keeping at most ``cap`` entries."""
    lst = _storage.setdefault(key, [])
    lst.append(entry)
    if len(lst) > cap:
        del lst[: len(lst) - cap]
    # S1: row-level settings persistence - mark ONLY this log key so the
    # incremental sync upserts this blob instead of rewriting every settings
    # row (the old blanket mark re-serialized up to ~5-10MB per save).
    _mark_settings_key_dirty(key)

def get_failed_verifications() -> str:
    """Get list of failed verifications."""
    return "\n".join([f"User {v['user_id']} - {v['reason']}" for v in _storage["failed_verifications"][-10:]]) or "No failed verifications"

def get_transaction_logs() -> str:
    """Get transaction logs."""
    return "\n".join([f"{t.get('timestamp', 'N/A')}: User {t.get('user_id', 'N/A')} - {t.get('action', 'N/A')}" for t in _storage["transaction_logs"][-20:]]) or "No transactions"

def log_transaction(user_id, action: str, amount: float = 0, **kwargs) -> None:
    """Log a transaction."""
    append_bounded("transaction_logs", {
        "user_id": str(user_id),
        "action": action,
        "amount": amount,
        "timestamp": datetime.now().isoformat(),
        **kwargs
    })
    save_storage()

def log_failed_verification(user_id, reason: str, **kwargs) -> None:
    """Log a failed verification."""
    append_bounded("failed_verifications", {
        "user_id": str(user_id),
        "reason": reason,
        "timestamp": datetime.now().isoformat(),
        **kwargs
    })
    save_storage()

def view_admin_logs() -> str:
    """View admin logs."""
    logs = _storage.get("admin_logs", [])[-20:]
    if not logs:
        return "No admin logs."
    return "\n".join([f"{l.get('timestamp', 'N/A')}: {l.get('action', 'N/A')}" for l in logs])
