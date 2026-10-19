"""Refactored from bot.utils.storage: Daily verification counter storage operations."""
import os
from bot.data.state import (
    RETENTION_DAYS, _mark_dirty, _mark_settings_key_dirty, _parse_date,
    _storage, _storage_lock, datetime, logger, save_storage, timedelta,
)

# Session tracker retention limits. The tracker grows by one entry per
# CREDITED session, so at 40-50k sessions/day an unbounded tracker would hold
# millions of dicts in memory. Both limits are env-tunable. Pruned entries are
# already-settled credits; the withdraw country breakdown reconciles any
# missing history as an explicit "Other" line, so pruning never breaks money.
SESSION_TRACKER_RETENTION_DAYS = int(os.environ.get("SESSION_TRACKER_RETENTION_DAYS", "14") or 14)
SESSION_TRACKER_MAX_ENTRIES = int(os.environ.get("SESSION_TRACKER_MAX_ENTRIES", "120000") or 120000)

# H6: successful_numbers is the ONLY structure that grows with every accepted
# account forever (users grow per-USER, the tracker has a cap). Without a cap
# it is a multi-hundred-MB memory + startup-parse sink at 25k accounts/day.
# The cap keeps the newest entries (the dedupe window scales inversely with
# volume: ~20 days at 25k/day, effectively unbounded at low volume). 10%
# headroom between prunes keeps the (rare) full-replace sync infrequent.
SUCCESSFUL_NUMBERS_MAX_ENTRIES = int(os.environ.get("SUCCESSFUL_NUMBERS_MAX_ENTRIES", "500000") or 500000)

def get_daily_verification_count(country_code: str) -> int:
    """
    Get today's verification-attempt count for a country.

    The counter is persisted in storage so the daily cap survives bot restarts
    (an in-memory counter would reset to zero and let users bypass the cap).
    The count auto-resets when the calendar date changes.
    """
    with _storage_lock:
        daily_counters = _storage.setdefault("daily_counters", {})
        today = datetime.now().strftime("%Y-%m-%d")
        entry = daily_counters.get(country_code)
        if not entry or entry.get("date") != today:
            return 0
        try:
            return int(entry.get("count", 0))
        except (TypeError, ValueError):
            return 0

def increment_daily_verification_count(country_code: str) -> int:
    """Increment today's verification-attempt count for a country and persist it."""
    with _storage_lock:
        daily_counters = _storage.setdefault("daily_counters", {})
        today = datetime.now().strftime("%Y-%m-%d")
        entry = daily_counters.get(country_code)
        if not entry or entry.get("date") != today:
            entry = {"date": today, "count": 0}
            daily_counters[country_code] = entry
        try:
            current = int(entry.get("count", 0))
        except (TypeError, ValueError):
            current = 0
        entry["count"] = current + 1
        # S1: mark only this settings key (row-level persistence).
        _mark_settings_key_dirty("daily_counters")
        save_storage()
        return int(entry["count"])

def reset_daily_verification_count(country_code: str) -> None:
    """Reset today's verification-attempt count for a country (admin use)."""
    with _storage_lock:
        daily_counters = _storage.setdefault("daily_counters", {})
        daily_counters[country_code] = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "count": 0
        }
        _mark_settings_key_dirty("daily_counters")
        save_storage()

def get_daily_success_count(country_code: str) -> int:
    """
    Get the current SUCCESS-account count for a country (since last reset).

    Only accounts that actually SUCCEED (balance credited) count toward the
    admin-configured country capacity. Rejected / failed / multi-device
    accounts never count here - they do not consume capacity.

    The counter is PERSISTENT: it NEVER auto-resets at midnight. Once a
    country's capacity is filled, it stays blocked until the admin gives the
    country a new capacity again (which resets this counter) or manually
    resets it. The stored ``date`` field is kept for information only.
    """
    with _storage_lock:
        success_counters = _storage.setdefault("daily_success_counters", {})
        entry = success_counters.get(country_code)
        if not entry:
            return 0
        try:
            return int(entry.get("count", 0))
        except (TypeError, ValueError):
            return 0

def increment_daily_success_count(country_code: str) -> int:
    """Increment the persistent SUCCESS-account count for a country and persist it.

    The count is never auto-reset (no midnight reset). It only goes back to 0
    when the admin gives the country a new capacity again (or manually resets
    it), so a filled capacity stays blocked until the admin re-opens it.
    """
    with _storage_lock:
        success_counters = _storage.setdefault("daily_success_counters", {})
        today = datetime.now().strftime("%Y-%m-%d")
        entry = success_counters.get(country_code)
        if not entry:
            entry = {"date": today, "count": 0}
            success_counters[country_code] = entry
        else:
            entry["date"] = today
        try:
            current = int(entry.get("count", 0))
        except (TypeError, ValueError):
            current = 0
        entry["count"] = current + 1
        # S1: mark only this settings key (row-level persistence).
        _mark_settings_key_dirty("daily_success_counters")
        save_storage()
        return int(entry["count"])

def reset_daily_success_count(country_code: str) -> None:
    """Reset today's SUCCESS-account count for a country (admin use)."""
    with _storage_lock:
        success_counters = _storage.setdefault("daily_success_counters", {})
        success_counters[country_code] = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "count": 0
        }
        _mark_settings_key_dirty("daily_success_counters")
        save_storage()

def cleanup_old_data(days: int = RETENTION_DAYS) -> int:
    """
    Clean up data older than specified days.
    Returns number of items cleaned.
    """
    cleaned = 0
    cutoff = datetime.now() - timedelta(days=days)
    
    with _storage_lock:
        old_count = len(_storage.get("transaction_logs", []))
        _storage["transaction_logs"] = [
            log for log in _storage.get("transaction_logs", [])
            if _parse_date(log.get("timestamp")) > cutoff
        ]
        cleaned += old_count - len(_storage["transaction_logs"])
        _mark_settings_key_dirty("transaction_logs")

        old_count = len(_storage.get("failed_verifications", []))
        _storage["failed_verifications"] = [
            v for v in _storage.get("failed_verifications", [])
            if _parse_date(v.get("timestamp")) > cutoff
        ]
        cleaned += old_count - len(_storage["failed_verifications"])
        _mark_settings_key_dirty("failed_verifications")

        old_count = len(_storage.get("admin_logs", []))
        _storage["admin_logs"] = [
            log for log in _storage.get("admin_logs", [])
            if _parse_date(log.get("timestamp")) > cutoff
        ]
        cleaned += old_count - len(_storage["admin_logs"])
        _mark_settings_key_dirty("admin_logs")

        # --- session_tracker retention (age + global size cap) ------------
        tracker_pruned = _prune_session_tracker_locked()
        cleaned += tracker_pruned

        # --- successful_numbers size cap (H6) ------------------------------
        # Keeps memory and startup parse time bounded at high daily volume.
        # Newest entries (by verified_at) are kept; the dedupe window shrinks
        # gracefully instead of the process growing without bound.
        sn = _storage.get("successful_numbers")
        if isinstance(sn, dict) and len(sn) > SUCCESSFUL_NUMBERS_MAX_ENTRIES:
            keep = int(SUCCESSFUL_NUMBERS_MAX_ENTRIES * 0.9)
            ranked = sorted(
                sn.items(),
                # `or ""` guards a hand-edited/legacy entry whose verified_at
                # is None - such entries sort oldest and are pruned first.
                key=lambda kv: ((kv[1] or {}).get("verified_at") or "") if isinstance(kv[1], dict) else "",
                reverse=True,
            )
            _storage["successful_numbers"] = dict(ranked[:keep])
            _mark_dirty("successful_numbers", None)  # deletions → full-replace sync
            cleaned += len(sn) - keep
            logger.warning(
                f"successful_numbers capped: kept newest {keep} of {len(sn)} "
                f"(SUCCESSFUL_NUMBERS_MAX_ENTRIES={SUCCESSFUL_NUMBERS_MAX_ENTRIES})"
            )

        if cleaned > 0:
            save_storage()
            logger.info(f"Cleaned up {cleaned} old data entries (>{days} days)")

    return cleaned


def _prune_session_tracker_locked() -> int:
    """Prune the session tracker by age and global size. MUST hold _storage_lock.

    - Age: entries older than SESSION_TRACKER_RETENTION_DAYS are dropped.
    - Size: when the tracker exceeds SESSION_TRACKER_MAX_ENTRIES entries in
      total, the oldest entries (by verified_at) are dropped first, keeping
      memory bounded at any daily volume.

    Entries with an unparseable/missing verified_at are treated as oldest
    (datetime.min) so corrupt entries can never anchor themselves forever.
    Marks the tracker fully dirty (None) when anything was pruned, because
    deletions can only reach the DB through the full-replace sync path.
    """
    tracker = _storage.get("session_tracker")
    if not isinstance(tracker, dict) or not tracker:
        return 0

    age_cutoff = datetime.now() - timedelta(days=SESSION_TRACKER_RETENTION_DAYS)
    pruned = 0

    for uid in list(tracker.keys()):
        slist = tracker.get(uid)
        if not isinstance(slist, list):
            continue
        if not slist:
            tracker.pop(uid, None)
            continue
        kept = [s for s in slist if _parse_date(s.get("verified_at") if isinstance(s, dict) else None) > age_cutoff]
        if len(kept) != len(slist):
            pruned += len(slist) - len(kept)
            if kept:
                tracker[uid] = kept
            else:
                tracker.pop(uid, None)

    total = sum(len(v) for v in tracker.values() if isinstance(v, list))
    if total > SESSION_TRACKER_MAX_ENTRIES:
        # Flatten with user binding, sort oldest-first, keep only the newest.
        flat = []
        for uid, slist in tracker.items():
            if not isinstance(slist, list):
                continue
            for s in slist:
                if isinstance(s, dict):
                    flat.append((_parse_date(s.get("verified_at")), uid, s))
        flat.sort(key=lambda t: t[0])
        excess = len(flat) - SESSION_TRACKER_MAX_ENTRIES
        pruned += excess
        survivors: dict = {}
        for _, uid, s in flat[excess:]:
            survivors.setdefault(uid, []).append(s)
        _storage["session_tracker"] = survivors

    if pruned > 0:
        # Deletions require the full-replace sync path.
        _mark_dirty("session_tracker", None)
        logger.info(
            f"Session tracker pruned: {pruned} entries removed "
            f"(retention {SESSION_TRACKER_RETENTION_DAYS}d, cap {SESSION_TRACKER_MAX_ENTRIES})"
        )
    return pruned
