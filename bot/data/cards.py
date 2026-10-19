"""Card Name Management - registry, availability and statistics (data layer).

Domain module following the bot/data split (state, users, withdrawals, ...).
Storage lives under the generic ``cards`` settings key, which is NOT part of
``_SQLITE_TABLE_KEYS`` - so it persists as a JSON blob in the SQLite settings
table exactly like ``min_withdraw`` and other settings. Nothing else in the
storage schema changes.

Record shape (keyed by the normalized Card Name):

    {
      "id": "c1a2b3c4d5",                  # stable short callback identifier
      "card_name": "BT",                   # canonical (uppercase) name
      "owner_username": "@card_owner",
      "channel_id": "-1001234567890",
      "status": "active" | "blocked",
      "activated_at": ISO timestamp,
      "blocked_at": ISO timestamp | None,
      "blocked_by": admin id | None,
      "block_reason": str | None,
      "history": [ {event, at, by, reason}, ... ]   # capped at CARD_HISTORY_MAX_EVENTS (newest kept)
    }

Blocking never deletes a record and reactivation never erases block history:
every status change appends an immutable history event.
"""

import re
import os
import secrets
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from bot.data.state import (
    _mark_settings_key_dirty,
    _storage,
    _storage_lock,
    register_settings_consistency_lock,
    save_storage_now,
)

# ---------------------------------------------------------------------------
# Card Name validation - THE single business rule used everywhere:
#   - minimum 2 characters, maximum UNLIMITED (business rule)
#   - uppercase A-Z and digits 0-9 only
#   - at least one uppercase English letter (digits alone are invalid)
#   - no lowercase, spaces, dots, hyphens, underscores or special characters
# Telegram's 4096-char message limit is the only technical transport bound;
# it is never enforced as a business maximum here.
# ---------------------------------------------------------------------------
CARD_NAME_PATTERN = r"^(?=.*[A-Z])[A-Z0-9]{2,}$"

# TECHNICAL TRANSPORT LIMIT - NOT the business maximum.
# The business rule stays min 2 / max UNLIMITED. This bound exists only
# because Telegram messages are capped at 4096 characters: the details,
# summary and copy screens embed the full Card Name plus fixed text, so a
# pathological name longer than this bound could no longer be displayed or
# copied as one message. 1024 sits far above any realistic Card Name and
# safely below every Telegram message limit involved. Enforced at the two
# INPUT surfaces (user withdrawal validation + admin activation input) with
# a distinct error message; never described as a business rule.
CARD_NAME_TRANSPORT_MAX = 1024

# Withdrawal requests are stored with method "CARDNAME" (see withdraw flow).
CARD_WITHDRAW_METHOD = "CARDNAME"

# Consistency lock for the "cards" settings blob (same pattern as the
# "proxy" blob): mutators acquire this lock BEFORE _storage_lock, and the
# settings serializer holds it while (de)serializing the blob, making
# mutate-vs-serialize a single lock domain.
_cards_lock = threading.RLock()

# Registered at import so the serializer always sees a quiescent blob.
register_settings_consistency_lock("cards", _cards_lock)


def is_valid_card_name(name: str) -> bool:
    """True when ``name`` satisfies the exact Card Name business rule."""
    if not isinstance(name, str):
        return False
    return re.match(CARD_NAME_PATTERN, name) is not None


def normalize_card_name(name: str) -> str:
    """Consistent normalization: trim surrounding whitespace + uppercase.

    Valid values are already uppercase-only, so this never alters a valid
    value; it only makes lookups (admin search, withdrawal checks) tolerant
    of stray surrounding whitespace / lowercase input.
    """
    return (name or "").strip().upper()


# ---------------------------------------------------------------------------
# Registry access (thread-safe, same lock domain as every other money path)
# ---------------------------------------------------------------------------

def _cards() -> Dict[str, Dict[str, Any]]:
    """Registry accessor - lazy init happens ONLY under _cards_lock.

    RLock (reentrant) so mutators that already hold it can call this
    freely; lock-free readers (e.g. the withdraw hot path) also serialize
    against the settings serializer through the registered consistency
    lock, never corrupting a full-sync snapshot.
    """
    with _cards_lock:
        cards = _storage.get("cards")
        if not isinstance(cards, dict):
            cards = {}
            _storage["cards"] = cards
        return cards


def _new_card_id(cards: Dict[str, Dict[str, Any]]) -> str:
    """Short, unique, callback-safe identifier (Telegram callback_data <= 64B)."""
    while True:
        cid = "c" + secrets.token_hex(5)  # 10 hex chars -> ~1e12 space
        if cid not in cards and get_card_by_id(cid) is None:
            return cid


def get_card(card_name: str) -> Optional[Dict[str, Any]]:
    """Look up a card by its (any-case) name."""
    return _cards().get(normalize_card_name(card_name))


def get_card_by_id(card_id: str) -> Optional[Dict[str, Any]]:
    """Look up a card by its stable short id."""
    for rec in _cards().values():
        if isinstance(rec, dict) and rec.get("id") == card_id:
            return rec
    return None


def all_cards() -> List[Dict[str, Any]]:
    """All cards (active + blocked), stable lexicographic order by name."""
    return sorted(
        (r for r in _cards().values() if isinstance(r, dict)),
        key=lambda r: str(r.get("card_name", "")),
    )


def blocked_cards() -> List[Dict[str, Any]]:
    """Only blocked cards, same stable order."""
    return [c for c in all_cards() if c.get("status") == "blocked"]


def is_card_available(card_name: str) -> bool:
    """True only when the card exists AND is currently ACTIVE."""
    rec = get_card(card_name)
    return bool(rec and rec.get("status") == "active")


# ---------------------------------------------------------------------------
# Mutations (never delete records; always preserve history)
# ---------------------------------------------------------------------------

def activate_card(
    card_name: str,
    owner_username: str = "",
    channel_id: str = "",
    activated_by: Any = None,
) -> Dict[str, Any]:
    """Activate a new card or reactivate a blocked one.

    Returns ``{"status": "created"|"reactivated"|"already_active",
    "record": <card>}``.

    - New card: all three fields stored, status ACTIVE, activation timestamp
      saved and the card becomes available for user withdrawals.
    - Existing ACTIVE card: no duplicate is created ("already_active").
    - Existing BLOCKED card: the SAME record is reactivated (id, owner,
      channel, statistics and the full block history are preserved), a new
      activation event is appended and a new activation timestamp saved.
    """
    name = normalize_card_name(card_name)
    now = datetime.now().isoformat()
    result: Dict[str, Any] = {"card_name": name, "status": None, "record": None, "error": None}

    # Domain-lock -> _storage_lock (same ordering contract as the proxy blob).
    with _cards_lock:
        with _storage_lock:
            cards = _cards()
            rec = cards.get(name)
            if rec is None:
                rec = {
                    "id": _new_card_id(cards),
                    "card_name": name,
                    "owner_username": owner_username or "",
                    "channel_id": str(channel_id or ""),
                    "status": "active",
                    "activated_at": now,
                    "blocked_at": None,
                    "blocked_by": None,
                    "block_reason": None,
                    "history": [{"event": "activated", "at": now, "by": activated_by, "reason": None}],
                }
                cards[name] = rec
                result["status"] = "created"
            else:
                if rec.get("status") == "active":
                    result["error"] = "already_active"
                    result["record"] = rec
                    return result
                rec["status"] = "active"
                rec["activated_at"] = now
                rec["blocked_at"] = None
                rec["blocked_by"] = None
                rec["block_reason"] = None
                if owner_username:
                    rec["owner_username"] = owner_username
                if channel_id not in (None, ""):
                    rec["channel_id"] = str(channel_id)
                rec.setdefault("history", []).append(
                    {"event": "activated", "at": now, "by": activated_by, "reason": None}
                )
                _cap_card_history(rec)
                result["status"] = "reactivated"
            result["record"] = rec
            _mark_settings_key_dirty("cards")

    save_storage_now()
    return result


# MEDIUM-35: card history is append-only and each activate/block cycle adds an
# event. A very frequently toggled card would grow the row unboundedly (the
# settings blob is full-replace synced). The cap keeps the NEWEST events and
# counts the dropped ones, so the summary stays truthful without a leak.
CARD_HISTORY_MAX_EVENTS = int(os.environ.get("CARD_HISTORY_MAX_EVENTS", "100") or 100)


def _cap_card_history(rec: Dict[str, Any]) -> None:
    history = rec.get("history")
    if not isinstance(history, list) or len(history) <= CARD_HISTORY_MAX_EVENTS:
        return
    dropped = len(history) - CARD_HISTORY_MAX_EVENTS
    rec["history"] = history[-CARD_HISTORY_MAX_EVENTS:]
    rec["history_dropped_count"] = int(rec.get("history_dropped_count") or 0) + dropped


def block_card(card_name: str, blocked_by: Any = None, reason: str = None) -> Optional[Dict[str, Any]]:
    """Block an ACTIVE card (never deletes; history preserved).

    Returns the updated record, or ``None`` when the card does not exist or
    is already blocked.
    """
    name = normalize_card_name(card_name)
    now = datetime.now().isoformat()
    snapshot: Optional[Dict[str, Any]] = None

    # Domain-lock -> _storage_lock (same ordering contract as the proxy blob).
    with _cards_lock:
        with _storage_lock:
            rec = _cards().get(name)
            if rec is None or rec.get("status") == "blocked":
                return None
            rec["status"] = "blocked"
            rec["blocked_at"] = now
            rec["blocked_by"] = str(blocked_by) if blocked_by is not None else None
            rec["block_reason"] = (reason or "").strip() or None
            rec.setdefault("history", []).append(
                {"event": "blocked", "at": now, "by": blocked_by, "reason": rec["block_reason"]}
            )
            _cap_card_history(rec)
            if not rec.get("id"):
                rec["id"] = _new_card_id(_cards())
            _mark_settings_key_dirty("cards")
            snapshot = dict(rec)

    save_storage_now()
    return snapshot


def set_block_reason(card_name: str, reason: str, by: Any = None) -> Optional[Dict[str, Any]]:
    """Attach/replace the block reason of an already-blocked card.

    The last history event stays authoritative; the reason recorded there is
    updated in place (the event's timestamp/admin are untouched).
    """
    name = normalize_card_name(card_name)
    snapshot: Optional[Dict[str, Any]] = None

    # Domain-lock -> _storage_lock (same ordering contract as the proxy blob).
    with _cards_lock:
        with _storage_lock:
            rec = _cards().get(name)
            if rec is None or rec.get("status") != "blocked":
                return None
            clean = (reason or "").strip()
            rec["block_reason"] = clean or None
            history = rec.setdefault("history", [])
            for event in reversed(history):
                if event.get("event") == "blocked":
                    event["reason"] = rec["block_reason"]
                    if by is not None:
                        event["by"] = by
                    break
            _mark_settings_key_dirty("cards")
            snapshot = dict(rec)

    save_storage_now()
    return snapshot


# ---------------------------------------------------------------------------
# Statistics - computed from the EXISTING withdrawal records (no duplicate
# statistics ledger, no double counting). A withdrawal belongs to a card when
# its method is CARDNAME and its wallet address normalizes to the card name.
# ---------------------------------------------------------------------------

def _parse_ts(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _iter_card_withdrawals(card_name: str):
    name = normalize_card_name(card_name)
    for w in _storage.get("withdrawals", []) or []:
        if not isinstance(w, dict):
            continue
        if str(w.get("method", "")).upper() != CARD_WITHDRAW_METHOD:
            continue
        if normalize_card_name(str(w.get("wallet_address", ""))) != name:
            continue
        yield w


def _empty_stats() -> Dict[str, Any]:
    return {
        "requests": 0,
        "approved": 0,
        "rejected": 0,
        "pending": 0,
        "requested": 0.0,
        "approved_amount": 0.0,
        "rejected_amount": 0.0,
        "pending_amount": 0.0,
    }


def get_card_statistics(card_name: str, since: Optional[datetime] = None) -> Dict[str, Any]:
    """Aggregate withdrawal statistics for one card.

    ``since`` restricts to records whose timestamp is at/after the cutoff
    (used for Today / Last 7 Days / Last 1 Month). Records without a
    parsable timestamp always count toward the all-time totals and are
    excluded from time windows.
    """
    stats = _empty_stats()
    for w in _iter_card_withdrawals(card_name):
        if since is not None:
            ts = _parse_ts(w.get("timestamp"))
            if ts is None or ts < since:
                continue
        status = str(w.get("status", "pending")).lower()
        try:
            amount = float(w.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        stats["requests"] += 1
        stats["requested"] += amount
        if status in ("approved", "rejected", "pending"):
            stats[status] += 1
            stats[f"{status}_amount"] += amount
    return stats


def today_start() -> datetime:
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def week_start() -> datetime:
    """Last 7 days INCLUDING today (today-6 .. today)."""
    return today_start() - timedelta(days=6)


def month_start() -> datetime:
    """Last one month INCLUDING today (today-30 .. today)."""
    return today_start() - timedelta(days=30)


def format_card_dt(value: Any) -> str:
    """'06 September 2026, 10:30 PM' style display for stored ISO timestamps."""
    dt = _parse_ts(value)
    if dt is None:
        return "N/A"
    return dt.strftime("%d %B %Y, %I:%M %p")


def period_label(start: datetime, end: datetime) -> str:
    return f"{start.strftime('%d %B %Y')} - {end.strftime('%d %B %Y')}"
