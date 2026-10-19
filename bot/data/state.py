"""
Storage Module - JSON-based persistent storage with thread-safe operations.
===========================================================================
Provides user management, session handling, and transaction logging.

Features:
- Thread-safe file operations with RLock (reentrant)
- Debounced saves for performance
- Automatic migration of storage schema
- Balance credit atomicity
- Optional encryption for sensitive data
- Data retention policy support
"""

import json
import os
import glob
import logging
import threading
import sqlite3
import sys
import time
import base64
import copy
import hashlib
import atexit
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Union
from functools import wraps

from bot import config

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30

_storage: Dict[str, Any] = {
    "users": {},
    "countries": {},
    "withdrawals": [],
    "admin_logs": [],
    "failed_verifications": [],
    "transaction_logs": [],
    "pending_sessions": {},
    "daily_counters": {},
    "daily_success_counters": {},
    "successful_numbers": {},
    "maintenance": False,
    "required_channel": "",
    "withdraw_sessions": {}
}

_save_pending = False
_storage_lock = threading.RLock()
_save_lock = threading.Lock()
_balance_credit_lock = threading.Lock()
_session_finalization_lock = threading.Lock()
_save_timer: Optional[threading.Timer] = None

# Failed-save retry bookkeeping (see _do_save / _arm_failed_save_retry).
# A failed incremental sync re-arms the debounce timer (with backoff) up to
# MAX_SAVE_RETRIES times instead of silently dropping the dirty marks; when
# the budget is exhausted the state is snapshotted to the fallback JSON and
# _save_pending is cleared so later saves start a fresh episode.
_save_retry_count = 0
MAX_SAVE_RETRIES = 3
SAVE_RETRY_BACKOFF_BASE = 1.0

# Serializes SQLite writes. The hot path runs _do_save without holding
# _storage_lock (so the event loop never blocks), but multiple threads
# (debounce Timer, background worker, explicit save_storage_now) can reach
# the sync functions at once; this lock guarantees they never start
# overlapping transactions on the shared connection.
_db_write_lock = threading.Lock()

# ============================================================
# Settings-value consistency locks
# ============================================================
# A settings-table value can be a LIVE nested dict that background threads
# keep mutating (e.g. the "proxy" blob: the recovery worker records health
# counters under the proxy module's own lock). The sync path snapshots the
# top-level reference under _storage_lock but then json-serializes it
# lock-free - so a concurrent mutation could abort the dump with
# "dictionary changed size during iteration" and fail the whole save cycle.
#
# A domain module can register its own mutation lock for a settings key:
# the serializer then holds that lock while (de)serializing that key,
# making mutate-vs-serialize a SINGLE lock domain. Lock ordering is safe by
# construction: mutators take domain-lock -> _storage_lock (brief), while
# the serializer takes _storage_lock (snapshot, released) and THEN the
# domain lock - the two locks are never held simultaneously in reverse.
_settings_consistency_locks: Dict[str, Any] = {}


def register_settings_consistency_lock(key: str, lock: Any) -> None:
    """Register the mutation lock that guards a live settings-table value.

    Called once at import time by the owning domain module (e.g. the proxy
    system registers its blob lock for the "proxy" key). The serializer in
    ``_db_sync_tables`` holds this lock while converting the value for
    storage, so a snapshot can never race the mutators.
    """
    if key and lock is not None:
        with _storage_lock:
            _settings_consistency_locks[key] = lock


def _registered_consistency_locks() -> list:
    """Snapshot of every registered settings-consistency lock.

    Kept as the canonical read-safe accessor for the registry: a caller that
    must hold EVERY domain lock at once (e.g. diagnostic tooling) uses this
    instead of touching the private dict. Registrations happen once at import
    time, so the read is stable; the copy under _storage_lock keeps it
    formally read-safe.
    """
    with _storage_lock:
        return list(_settings_consistency_locks.values())


def _is_message_not_modified(exc: Any) -> bool:
    """True when ``exc`` is a Telegram MESSAGE_NOT_MODIFIED error.

    Editing a message to the text it already carries raises this: the desired
    content IS already delivered, so re-delivery paths must treat it as
    SUCCESS and must NOT fall through to a fresh ``send_message`` (which would
    duplicate the message). One shared check so every caller matches.
    """
    try:
        text = str(exc)
    except Exception:
        return False
    return "MESSAGE_NOT_MODIFIED" in text


def _deadline_is_in_future(deadline_at: Any) -> bool:
    """True when ``deadline_at`` (ISO-8601 str or epoch seconds) is in the future.

    Delivery paths consult this before surfacing a precomputed verdict: while
    the exact deadline has not passed the row must stay pending (the deadline
    timer delivers it at the right moment) instead of being sent early.
    Missing or unparseable values return False so a corrupt timestamp never
    blocks a needed delivery.
    """
    if not deadline_at:
        return False
    if isinstance(deadline_at, (int, float)):
        return time.time() < float(deadline_at)
    try:
        deadline_dt = datetime.fromisoformat(
            str(deadline_at).replace("Z", "").replace("+00:00", "")
        )
        return deadline_dt > datetime.now()
    except (ValueError, TypeError):
        return False


def _serialize_settings_value(key: str, value: Any) -> str:
    """Return the storage-ready JSON string for one settings value.

    The JSON serialization happens INSIDE the registered consistency lock
    (when one exists for this key - e.g. "proxy", "user_rate_limits"), so the
    live nested dict is dumped in a quiescent state. This must cover the WHOLE
    serialize step: when no encryption key is configured the encrypt helper
    returns the plain dict, and a caller-side json.dumps would then race the
    domain mutators again.
    """
    lock = _settings_consistency_locks.get(key)
    if lock is not None:
        with lock:
            if key == "proxy":
                value = _at_rest_encrypt_proxy(value)
            return json.dumps(value, ensure_ascii=False, default=str)
    if key == "proxy":
        value = _at_rest_encrypt_proxy(value)
    return json.dumps(value, ensure_ascii=False, default=str)

# ============================================================
# Incremental-sync dirty tracking
# ============================================================
# Instead of rewriting EVERY row of EVERY table on every save (which turns
# into a multi-second full-DB rewrite with 10k-60k pending sessions and users,
# blocking the event loop through the direct _do_save() callers), each mutation
# marks the rows it touched. _db_sync_dirty() then only writes those rows.
#
#   _dirty_rows[table] == None          -> entire table dirty (full sync)
#   _dirty_rows[table] == set of keys   -> only those rows dirty (row upsert)
#
# A periodic full sync (_db_sync_all) runs in the background as a safety net so
# any missed dirty mark is eventually persisted. Table-wide (None) marking is
# required for DELETIONS: the incremental path can only upsert, so a deleted
# row must trigger the full "delete rows not in memory" path.
_dirty_rows: Dict[str, Any] = {
    "users": set(),
    "pending_sessions": set(),
    "countries": set(),
    "withdrawals": set(),  # mutation sites mark full (None); empty set = unchanged
    "session_tracker": set(),  # row-level incremental upsert (user_id key)
    "successful_numbers": set(),  # row-level incremental upsert (phone key)
    "settings": set(),
}


def _mark_dirty(table: str, key: Any = None) -> None:
    """Mark a table (or one row of it) as needing a write on the next save.

    ``key is None`` marks the WHOLE table dirty (used for deletions and for
    small tables that are always full-replaced). Otherwise only that row is
    marked, so the next sync upserts just it instead of the whole table.
    """
    with _storage_lock:
        if key is None:
            _dirty_rows[table] = None
        else:
            current = _dirty_rows.get(table)
            if current is None:
                return  # already fully dirty - no need to track individual keys
            current.add(key)


def _mark_table_dirty(table: str) -> None:
    """Mark an entire table as dirty (full sync on next save)."""
    with _storage_lock:
        _dirty_rows[table] = None


def _restore_dirty_rows(dirty: Dict[str, Any]) -> None:
    """Re-add a failed sync's dirty snapshot so the next save retries it.

    ``_db_sync_dirty`` snapshots AND resets the dirty marks before writing;
    if that write fails the marks would be lost and the mutations would only
    reach the DB via the next full sync - or never, if the process dies first.
    This merges the snapshot back with ``_mark_dirty`` semantics: a fully
    dirty table (None) is re-marked wholesale, row-level sets are merged, and
    a table that became fully dirty during the write window needs nothing.
    """
    with _storage_lock:
        for table, rows in dirty.items():
            if rows is None:
                _dirty_rows[table] = None
                continue
            current = _dirty_rows.get(table)
            if current is None:
                continue  # already fully dirty - individual keys are covered
            current.update(rows)


def _snapshot_dirty_rows() -> Dict[str, Any]:
    """Atomically snapshot the dirty state and reset it.

    Called under ``_storage_lock`` by the sync routine. Returns a shallow copy
    of the dirty map (the sets are copies so the reset cannot lose entries
    mutated concurrently).
    """
    snap: Dict[str, Any] = {}
    for table, dirty in _dirty_rows.items():
        if dirty is None:
            snap[table] = None
        else:
            snap[table] = set(dirty)
        _dirty_rows[table] = set()
    return snap

# True when the in-memory _storage is a faithful, complete copy of the DB
# (i.e. it was successfully loaded from SQLite, or the DB is empty/fresh).
# When False the in-memory state must NEVER be written back to a non-empty DB,
# otherwise _db_sync_all's full-replace would silently wipe existing rows.
_storage_trusted = False

# ============================================================
# SQLite persistence layer
# SQLite is the ONLY storage. All state (users, countries,
# templates, settings, sessions) lives in bot_data.db.
# ============================================================

def _resolve_storage_path(env_name: str, production_default: str) -> str:
    """Resolve a storage path, refusing the PRODUCTION default for ad-hoc code.

    ``tests/conftest.py`` redirects ``SQLITE_DB_PATH`` for pytest, but nothing
    protected an ad-hoc ``python -c "import bot..."`` or an interactive REPL run
    from the repo root: those pick up the production default and write LIVE user
    balances, pending sessions and credited numbers. That is not hypothetical -
    it already happened once and leaked synthetic rows into the real database.

    A throwaway path is therefore MANDATORY for those two entry points. Normal
    entry points (``python -m bot``, ``python main.py``, a systemd/Termux
    service) are untouched because ``sys.argv[0]`` names a real script.
    """
    explicit = os.environ.get(env_name)
    if explicit:
        return explicit
    argv0 = (sys.argv[0] if sys.argv else "") or ""
    if argv0 in ("-c", ""):
        raise RuntimeError(
            f"{env_name} is not set and this is an ad-hoc `python -c` / REPL "
            f"process, which would write the PRODUCTION store at "
            f"{production_default!r}. Export {env_name} (and "
            f"STORAGE_FALLBACK_JSON) to a throwaway path first."
        )
    return production_default


SQLITE_STORAGE_PATH = _resolve_storage_path("SQLITE_DB_PATH", "data/bot_data.db")

# Fallback JSON snapshot used whenever the normal SQLite write is refused
# (overwrite-guard protecting a populated DB) or fails (corrupt/read-only DB).
# This guarantees no state is ever silently lost: the latest in-memory state is
# always durably snapshotted and restored on startup if SQLite cannot be loaded.
FALLBACK_STORAGE_PATH = _resolve_storage_path(
    "STORAGE_FALLBACK_JSON", "data/bot_data.fallback.json"
)

_sqlite_conn: Optional[sqlite3.Connection] = None
_sqlite_lock = threading.Lock()

_db_file_sig: Optional[tuple] = None

# Top-level _storage keys that live in dedicated relational tables.
# admin_logs / transaction_logs / failed_verifications have NO dedicated
# table, so they persist as generic settings (JSON blobs) like everything else.
_SQLITE_TABLE_KEYS = {
    "users", "countries", "pending_sessions", "withdrawals", "session_tracker",
    "successful_numbers",
}

_STORAGE_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance REAL DEFAULT 0,
    sessions INTEGER DEFAULT 0,
    banned INTEGER DEFAULT 0,
    joined TEXT DEFAULT (datetime('now')),
    success_count INTEGER DEFAULT 0,
    channel_joined INTEGER DEFAULT 0,
    data TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pending_sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    phone TEXT,
    country_code TEXT,
    status TEXT DEFAULT 'pending',
    data TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS countries (
    country_code TEXT PRIMARY KEY,
    data TEXT DEFAULT '{}',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    method TEXT,
    amount REAL,
    wallet_address TEXT,
    status TEXT DEFAULT 'pending',
    data TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS session_tracker (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    session_id TEXT,
    data TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT '{}',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS successful_numbers (
    phone TEXT PRIMARY KEY,
    user_id TEXT,
    verified_at TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_storage_withdrawals_user ON withdrawals(user_id);
CREATE INDEX IF NOT EXISTS idx_storage_session_tracker_user ON session_tracker(user_id);

-- session_tracker: enforce ONE row per (user_id, session_id) so the hot save
-- path can upsert a single credited session instead of rewriting the whole
-- table on every credit (the old DELETE-all + re-INSERT turned into a
-- multi-second full-table rewrite at 40-50k sessions/day). The dedupe DELETE
-- migrates any duplicate rows a legacy DB may hold before the unique index
-- can be created (runs once per process, then is a no-op).
DELETE FROM session_tracker WHERE id NOT IN (
    SELECT MIN(id) FROM session_tracker GROUP BY user_id, session_id
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_storage_session_tracker_uid_sid
    ON session_tracker(user_id, session_id);
"""


def _load_fallback_json() -> Optional[Dict[str, Any]]:
    """Load the fallback JSON snapshot, if any.

    P6-61: when SESSION_SECRET is set the snapshot is Fernet-encrypted on
    disk (the whole fallback file contains every password + session string,
    so plaintext on disk is unacceptable). Legacy PLAINTEXT snapshots are
    still readable for a seamless migration; if decryption fails the caller
    treats the snapshot as corrupt (wrong key) and never loads secrets.

    Returns the parsed dict, or None when the file is absent/corrupt.
    """
    try:
        with open(FALLBACK_STORAGE_PATH, "r", encoding="utf-8") as f:
            raw = f.read()
        data = None
        if raw.startswith("gAAAAA"):
            # Encrypted snapshot - decrypt before parsing.
            try:
                key = os.getenv("SESSION_SECRET") or os.getenv("STORAGE_ENCRYPTION_KEY") or ""
                if not key:
                    logger.error(
                        "Fallback snapshot is ENCRYPTED but no SESSION_SECRET is set - "
                        "refusing to load (cannot decrypt). Restore the correct key."
                    )
                    return None
                import base64 as _b64
                import hashlib as _hashlib
                from cryptography.fernet import Fernet as _Fernet
                derived = _b64.urlsafe_b64encode(_hashlib.sha256(key.encode()).digest())
                plaintext = _Fernet(derived).decrypt(raw.encode()).decode()
                data = json.loads(plaintext)
            except Exception as dec_err:
                logger.error(
                    f"Fallback snapshot decryption failed ({dec_err}) - "
                    f"SESSION_SECRET may have changed. NOT loading (data stays in SQLite)."
                )
                return None
        else:
            data = json.loads(raw)
        if isinstance(data, dict):
            return data
        logger.warning(f"Fallback storage JSON is not a dict: {FALLBACK_STORAGE_PATH}")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"Could not read fallback storage JSON ({FALLBACK_STORAGE_PATH}): {e}")
    return None


def _proxy_blob_snapshot() -> Optional[Dict[str, Any]]:
    """Deep-copy the live proxy blob under its OWN consistency lock (alone).

    Lock order (B2): the domain lock is taken WITHOUT holding _storage_lock —
    exactly like the settings serializer. Mutators take domain -> _storage_lock
    (brief), so this one-at-a-time order can never deadlock against them.
    Returns None when no proxy blob exists.
    """
    with _storage_lock:
        ref = _storage.get("proxy")
    if not isinstance(ref, dict):
        return None
    lock = _settings_consistency_locks.get("proxy")
    try:
        if lock is not None:
            with lock:
                return copy.deepcopy(ref)
        return copy.deepcopy(ref)
    except Exception:
        return None


def _write_fallback_json() -> bool:
    """Atomically persist the current in-memory state to the fallback JSON file.

    Used only when the normal SQLite write is refused (to protect a populated
    DB from an untrusted/incomplete in-memory state) or fails (corrupt/read-only
    DB). The snapshot survives restarts and is restored on the next startup if
    the SQLite load still fails, so data is never silently lost.

    Consistency (B2-correct): the snapshot is materialized WITHOUT ever holding
    two locks at once —
      1. the proxy blob is deep-copied under its own registered consistency
         lock (alone), then
      2. the remaining storage is deep-copied under _storage_lock (alone),
    so neither the AB-BA deadlock the old call-site ordering risked nor a
    "dictionary changed size during iteration" race can abort the dump.
    """
    try:
        os.makedirs(os.path.dirname(FALLBACK_STORAGE_PATH) or "data", exist_ok=True)
        tmp_path = FALLBACK_STORAGE_PATH + ".tmp"
        # P6-61 + self-audit fix: decide the mode BEFORE creating the tmp file
        # so the skip path never leaves an empty tmp artifact behind.
        key = os.getenv("SESSION_SECRET") or os.getenv("STORAGE_ENCRYPTION_KEY") or ""
        allow_plain = os.getenv("FALLBACK_ALLOW_PLAINTEXT") == "1"
        if not key and not allow_plain:
            logger.error(
                "Fallback snapshot SKIPPED: no SESSION_SECRET set and plaintext is "
                "disallowed (set FALLBACK_ALLOW_PLAINTEXT=1 to force). SQLite data "
                "is unaffected - only the crash-backup file is not written."
            )
            return False
        proxy_snap = _proxy_blob_snapshot()
        with _storage_lock:
            snap = dict(_storage)
            if proxy_snap is not None:
                snap["proxy"] = proxy_snap
            payload = json.dumps(snap, ensure_ascii=False, default=str)
        with open(tmp_path, "w", encoding="utf-8") as f:
            if key:
                import base64 as _b64
                import hashlib as _hashlib
                from cryptography.fernet import Fernet as _Fernet
                derived = _b64.urlsafe_b64encode(_hashlib.sha256(key.encode()).digest())
                f.write(_Fernet(derived).encrypt(payload.encode()).decode())
            else:
                logger.warning(
                    "FALLBACK_ALLOW_PLAINTEXT=1: writing an UNENCRYPTED crash snapshot "
                    "(contains all passwords) - not recommended"
                )
                f.write(payload)
        os.replace(tmp_path, FALLBACK_STORAGE_PATH)
        return True
    except Exception as e:
        logger.error(f"Fallback JSON write failed ({FALLBACK_STORAGE_PATH}): {e}")
        return False


def _get_sqlite_conn() -> sqlite3.Connection:
    """Get a shared thread-safe synchronous SQLite connection (WAL mode)."""
    global _sqlite_conn, _db_file_sig
    if _sqlite_conn is None:
        with _sqlite_lock:
            if _sqlite_conn is None:
                os.makedirs(os.path.dirname(SQLITE_STORAGE_PATH) or "data", exist_ok=True)
                conn = sqlite3.connect(SQLITE_STORAGE_PATH, check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_STORAGE_SQLITE_SCHEMA)
                conn.commit()
                _sqlite_conn = conn
                _record_db_file_sig()
    return _sqlite_conn


def _record_db_file_sig() -> None:
    """Record the identity (device + inode) of the DB file we opened.

    The inode changes when the file is replaced via rename/mv, which is how
    a new database is swapped in. Our own writes keep the same inode.

    NOTE: cp/overwrite-in-place swaps are intentionally NOT detected here.
    Hot-reloading a WAL-mode database that was overwritten in place while this
    process holds a connection is unsafe: closing the stale connection merges
    its leftover WAL frames into the new file. Restoring via copy therefore
    requires a restart (a fresh process always reads the file cleanly). The
    rename/mv path IS safe to hot-reload and is what this detects.
    """
    global _db_file_sig
    try:
        st = os.stat(SQLITE_STORAGE_PATH)
        _db_file_sig = (st.st_dev, st.st_ino)
    except OSError:
        _db_file_sig = None


def database_file_replaced() -> bool:
    """Return True if the DB file on disk differs from the one this process opened."""
    if _db_file_sig is None:
        return False
    try:
        st = os.stat(SQLITE_STORAGE_PATH)
        return (st.st_dev, st.st_ino) != _db_file_sig
    except OSError:
        return False


def reload_storage_from_disk() -> bool:
    """Reopen the SQLite DB and reload all in-memory state if the file was replaced.

    Safe to call periodically; no-op unless the on-disk file actually changed.
    Used so replacing data/bot_data.db takes effect without a full restart.
    """
    if not database_file_replaced():
        return False

    global _storage
    with _storage_lock:
        try:
            close_sqlite_storage()
            load_storage()
            _record_db_file_sig()
            logger.warning("SQLite DB file replaced -> storage reloaded from new database")
            try:
                from bot.utils.fast_cache import (
                    invalidate_user_cache,
                    refresh_all_templates,
                    refresh_withdraw_status,
                )
                invalidate_user_cache()
                refresh_all_templates()
                refresh_withdraw_status()
            except Exception:
                pass
            return True
        except Exception as e:
            logger.error(f"Storage reload from replaced DB failed: {e}")
            return False


def close_sqlite_storage() -> None:
    """Close the shared SQLite connection (call on shutdown or DB reload).

    Checkpoints (and truncates) the WAL first so a replaced DB file never
    replays stale WAL data from the previous database.
    """
    global _sqlite_conn
    with _sqlite_lock:
        if _sqlite_conn is not None:
            try:
                _sqlite_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                _sqlite_conn.close()
            except Exception:
                pass
            _sqlite_conn = None


def _db_has_storage_data() -> bool:
    try:
        conn = _get_sqlite_conn()
        for table in ("users", "countries", "pending_sessions", "settings", "successful_numbers"):
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            if row and row[0] > 0:
                return True
    except Exception as e:
        logger.warning(f"SQLite storage availability check failed: {e}")
    return False


def _db_sync_all_locked() -> bool:
    """Full-sync body; the caller MUST already hold ``_db_write_lock``.

    Extracted so the shutdown flush can run the same full consistency sync
    AFTER acquiring the (non-reentrant) lock itself, without deadlocking on a
    second acquire inside ``_db_sync_all``.

    CRITICAL RACE FIX: the dirty marks are snapshotted BEFORE the sync and
    only the marks that existed at that moment are cleared afterwards. The
    old code cleared ALL marks after the write, so a mutation that happened
    WHILE the full sync was writing (snapshot taken, then lock-free write)
    had its dirty mark erased - the mutation was in memory but never reached
    the DB until the next full sync. Now any mark added during the write
    window survives, so the next incremental sync picks it up.
    """
    with _storage_lock:
        _pre_dirty = {
            _t: (None if _v is None else set(_v))
            for _t, _v in _dirty_rows.items()
        }
    ok = _db_sync_tables(None)
    if ok:
        with _storage_lock:
            for _t, _pre in _pre_dirty.items():
                _cur = _dirty_rows.get(_t)
                if _cur is None:
                    # Table became FULLY dirty during the write (someone called
                    # _mark_dirty(t, None) mid-sync): keep the whole-table mark
                    # so the next incremental sync re-writes everything.
                    continue
                if _pre is None:
                    # Table was fully dirty before the sync and now has
                    # row-level marks (re-marked mid-write): keep them all.
                    continue
                # Both are row-level sets: drop only the keys that were dirty
                # BEFORE the sync (they were just written); keys added during
                # the write window survive for the next incremental sync.
                _cur.difference_update(_pre)
    return ok


def _db_sync_all() -> bool:
    """Persist the full in-memory _storage state to SQLite in ONE atomic transaction.

    Full sync - writes every row of every table and deletes rows no longer in
    memory. Used as the periodic safety net and for explicit full flushes.
    """
    with _db_write_lock:
        return _db_sync_all_locked()


def _db_sync_dirty() -> bool:
    """Persist ONLY the dirty rows to SQLite in one atomic transaction.

    Incremental sync: tables marked fully dirty (None) get the full-sync path
    (write-all + delete-stale); tables with row-level dirty sets get per-row
    upserts. This is what the hot save path calls - it stays fast no matter how
    many users/pending sessions exist.

    FAILURE SAFETY: ``_snapshot_dirty_rows`` resets the marks BEFORE the write
    so concurrent mutators can keep marking rows during it. If the write then
    fails (exception or False from ``_db_sync_tables``), every row from the
    snapshot is re-marked dirty (``_restore_dirty_rows``) so the caller's retry
    timer persists them - a failed save can never silently lose mutations.
    """
    with _db_write_lock:
        with _storage_lock:
            dirty = _snapshot_dirty_rows()
        try:
            ok = _db_sync_tables(dirty)
        except Exception as e:
            logger.error(f"SQLite incremental sync raised: {e}")
            ok = False
        if not ok:
            _restore_dirty_rows(dirty)
            logger.warning("SQLite incremental sync failed; dirty rows re-marked for retry")
        return ok


# ------------------------------------------------------------------
# S2 fix: delete-stale without the SQLite host-parameter ceiling
# ------------------------------------------------------------------
# The full-sync delete of "rows no longer in memory" used to build one
# NOT IN (...) placeholder per in-memory row:
#
#     DELETE FROM users WHERE user_id NOT IN (?,?,?,...)   # one ? per row
#
# SQLite caps host parameters per statement (SQLITE_LIMIT_VARIABLE_NUMBER,
# 32766 on modern bundled builds). users/successful_numbers pass ~32k rows
# within ~2 days at 25,000 accounts/day; past that EVERY full sync failed
# permanently ("too many SQL variables"), the consistency net never deleted
# anything again, and the failure loop retried forever. The in-memory keys are
# now staged into a TEMP table (chunked inserts) and the delete becomes a
# subquery anti-join with ZERO host parameters.
_SYNC_LIVE_KEYS_TEXT = "_sync_live_keys_text"
_SYNC_LIVE_KEYS_INT = "_sync_live_keys_int"

# Host parameters per staging INSERT batch (comfortably below any known limit).
_SQLITE_PARAM_CHUNK = 500


def _stage_live_keys(conn: sqlite3.Connection, keys, integer: bool) -> None:
    """Stage the in-memory row keys into a TEMP table for the anti-join."""
    table = _SYNC_LIVE_KEYS_INT if integer else _SYNC_LIVE_KEYS_TEXT
    conn.execute(
        f"CREATE TEMP TABLE IF NOT EXISTS {table} (k {'INTEGER' if integer else 'TEXT'} PRIMARY KEY)"
    )
    conn.execute(f"DELETE FROM {table}")
    chunk = []
    for key in keys:
        try:
            chunk.append((int(key),) if integer else (str(key),))
        except (TypeError, ValueError):
            continue  # same rows the old int-cast loop skipped
        if len(chunk) >= _SQLITE_PARAM_CHUNK:
            conn.executemany(f"INSERT OR IGNORE INTO {table} (k) VALUES (?)", chunk)
            chunk = []
    if chunk:
        conn.executemany(f"INSERT OR IGNORE INTO {table} (k) VALUES (?)", chunk)


def _delete_stale_rows(conn: sqlite3.Connection, table: str, column: str, integer: bool = False) -> None:
    """Delete rows whose key is no longer present in the staged TEMP table."""
    staged = _SYNC_LIVE_KEYS_INT if integer else _SYNC_LIVE_KEYS_TEXT
    conn.execute(f"DELETE FROM {table} WHERE {column} NOT IN (SELECT k FROM {staged})")
    conn.execute(f"DELETE FROM {staged}")


def _db_sync_tables(dirty: Optional[Dict[str, Any]]) -> bool:
    """Execute the SQLite sync for the given dirty map (None = all tables).

    Runs WITHOUT holding ``_storage_lock`` for the DB write: each table's data
    is snapshotted under a brief lock (microseconds), then written lock-free.
    The event loop can keep mutating ``_storage`` during the write; rows
    mutated mid-write are re-marked dirty by their mutators and caught by the
    next save. SQLite + busy_timeout serialize concurrent writers.
    """
    try:
        conn = _get_sqlite_conn()
        with conn:
            users_dirty = dirty.get("users") if dirty is not None else None
            pending_dirty = dirty.get("pending_sessions") if dirty is not None else None
            countries_dirty = dirty.get("countries") if dirty is not None else None
            settings_dirty = dirty.get("settings") if dirty is not None else None
            successful_numbers_dirty = dirty.get("successful_numbers") if dirty is not None else None

            # --- users -------------------------------------------------
            if users_dirty is None:
                with _storage_lock:
                    user_items = list(_storage.get("users", {}).items())
                    _mem_user_ids: set = set()
                    for _uid, _ in user_items:
                        try:
                            _mem_user_ids.add(int(_uid))
                        except (ValueError, TypeError):
                            pass
                for uid, data in user_items:
                    if not isinstance(data, dict):
                        continue
                    try:
                        conn.execute("""
                            INSERT INTO users
                                (user_id, balance, sessions, banned, joined, success_count,
                                 channel_joined, data, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                            ON CONFLICT (user_id) DO UPDATE SET
                                balance = excluded.balance,
                                sessions = excluded.sessions,
                                banned = excluded.banned,
                                joined = excluded.joined,
                                success_count = excluded.success_count,
                                channel_joined = excluded.channel_joined,
                                data = excluded.data,
                                updated_at = datetime('now')
                        """, (
                            int(uid),
                            data.get("balance", 0),
                            data.get("sessions", 0),
                            1 if data.get("banned", False) else 0,
                            data.get("joined", ""),
                            data.get("success_count", 0),
                            1 if data.get("channel_joined", False) else 0,
                            json.dumps(data, ensure_ascii=False, default=str),
                        ))
                    except (ValueError, TypeError, Exception) as e:
                        logger.debug(f"SQLite user sync skipped {uid}: {e}")

                # --- users: delete rows no longer present in memory --------
                # S2: TEMP-table anti-join (no host-parameter ceiling).
                if _mem_user_ids:
                    _stage_live_keys(conn, _mem_user_ids, integer=True)
                    _delete_stale_rows(conn, "users", "user_id", integer=True)
                else:
                    conn.execute("DELETE FROM users")
            elif users_dirty:
                with _storage_lock:
                    dirty_user_items = []
                    for uid in list(users_dirty):
                        _data = _storage.get("users", {}).get(str(uid))
                        if isinstance(_data, dict):
                            dirty_user_items.append((uid, _data))
                for uid, data in dirty_user_items:
                    try:
                        conn.execute("""
                            INSERT INTO users
                                (user_id, balance, sessions, banned, joined, success_count,
                                 channel_joined, data, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                            ON CONFLICT (user_id) DO UPDATE SET
                                balance = excluded.balance,
                                sessions = excluded.sessions,
                                banned = excluded.banned,
                                joined = excluded.joined,
                                success_count = excluded.success_count,
                                channel_joined = excluded.channel_joined,
                                data = excluded.data,
                                updated_at = datetime('now')
                        """, (
                            int(uid),
                            data.get("balance", 0),
                            data.get("sessions", 0),
                            1 if data.get("banned", False) else 0,
                            data.get("joined", ""),
                            data.get("success_count", 0),
                            1 if data.get("channel_joined", False) else 0,
                            json.dumps(data, ensure_ascii=False, default=str),
                        ))
                    except (ValueError, TypeError, Exception) as e:
                        logger.debug(f"SQLite user sync skipped {uid}: {e}")

            # --- countries ---------------------------------------------
            if countries_dirty is None:
                with _storage_lock:
                    country_items = list(_storage.get("countries", {}).items())
                    _mem_cc: set = set(_storage.get("countries", {}).keys())
                for cc, cdata in country_items:
                    conn.execute(
                        "INSERT OR REPLACE INTO countries (country_code, data, updated_at) VALUES (?, ?, datetime('now'))",
                        (str(cc), json.dumps(cdata, ensure_ascii=False, default=str))
                    )

                # --- countries: delete rows no longer present in memory ----
                if _mem_cc:
                    _stage_live_keys(conn, _mem_cc, integer=False)
                    _delete_stale_rows(conn, "countries", "country_code", integer=False)
                else:
                    conn.execute("DELETE FROM countries")
            elif countries_dirty:
                with _storage_lock:
                    dirty_country_items = []
                    for cc in list(countries_dirty):
                        _cdata = _storage.get("countries", {}).get(str(cc))
                        if isinstance(_cdata, dict):
                            dirty_country_items.append((cc, _cdata))
                for cc, cdata in dirty_country_items:
                    conn.execute(
                        "INSERT OR REPLACE INTO countries (country_code, data, updated_at) VALUES (?, ?, datetime('now'))",
                        (str(cc), json.dumps(cdata, ensure_ascii=False, default=str))
                    )

            # --- pending_sessions ---------------------------------------
            if pending_dirty is None:
                with _storage_lock:
                    pending_items = list(_storage.get("pending_sessions", {}).items())
                    _mem_sids: set = set(_storage.get("pending_sessions", {}).keys())
                for sid, pdata in pending_items:
                    if not isinstance(pdata, dict):
                        continue
                    row_data = _at_rest_encrypt_pending(pdata)
                    conn.execute("""
                        INSERT INTO pending_sessions
                            (session_id, user_id, phone, country_code, status, data, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                        ON CONFLICT (session_id) DO UPDATE SET
                            user_id = excluded.user_id,
                            phone = excluded.phone,
                            country_code = excluded.country_code,
                            status = excluded.status,
                            data = excluded.data,
                            updated_at = datetime('now')
                    """, (
                        str(sid),
                        row_data.get("user_id", 0),
                        row_data.get("phone", ""),
                        row_data.get("country_code", ""),
                        row_data.get("status", "pending"),
                        json.dumps(row_data, ensure_ascii=False, default=str),
                    ))

                # --- pending_sessions: delete rows no longer in memory ------
                if _mem_sids:
                    _stage_live_keys(conn, _mem_sids, integer=False)
                    _delete_stale_rows(conn, "pending_sessions", "session_id", integer=False)
                else:
                    conn.execute("DELETE FROM pending_sessions")
            elif pending_dirty:
                with _storage_lock:
                    dirty_pending_items = []
                    for sid in list(pending_dirty):
                        _pdata = _storage.get("pending_sessions", {}).get(str(sid))
                        if isinstance(_pdata, dict):
                            dirty_pending_items.append((sid, _pdata))
                for sid, pdata in dirty_pending_items:
                    row_data = _at_rest_encrypt_pending(pdata)
                    conn.execute("""
                        INSERT INTO pending_sessions
                            (session_id, user_id, phone, country_code, status, data, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                        ON CONFLICT (session_id) DO UPDATE SET
                            user_id = excluded.user_id,
                            phone = excluded.phone,
                            country_code = excluded.country_code,
                            status = excluded.status,
                            data = excluded.data,
                            updated_at = datetime('now')
                    """, (
                        str(sid),
                        row_data.get("user_id", 0),
                        row_data.get("phone", ""),
                        row_data.get("country_code", ""),
                        row_data.get("status", "pending"),
                        json.dumps(row_data, ensure_ascii=False, default=str),
                    ))

            # --- withdrawals (full replace, only when actually marked) ----
            # H4: this table was rewritten (DELETE-all + re-INSERT) on EVERY
            # sync even when nothing changed, and the in-memory list grew
            # without bound. Mutations (record/approve/reject/prune) now mark
            # the table dirty themselves; the empty-set case skips the rewrite.
            _withdrawals_dirty = dirty.get("withdrawals") if dirty is not None else None
            if _withdrawals_dirty is None or _withdrawals_dirty:
                with _storage_lock:
                    _withdrawals = list(_storage.get("withdrawals", []))
                conn.execute("DELETE FROM withdrawals")
                for w in _withdrawals:
                    if not isinstance(w, dict):
                        continue
                    conn.execute(
                        "INSERT INTO withdrawals (user_id, method, amount, wallet_address, status, data) VALUES (?, ?, ?, ?, ?, ?)",
                        (w.get("user_id"), w.get("method", ""), w.get("amount", 0),
                         w.get("wallet_address", ""), w.get("status", "pending"),
                         json.dumps(w, ensure_ascii=False, default=str))
                    )

            # --- session_tracker (row-level incremental upsert) -----------
            # Full sync (dirty None) rewrites the table + deletes users no
            # longer in memory (deletions need this path); incremental sync
            # upserts only the dirty users' rows, so a credit at 40-50k
            # sessions/day writes ONE row instead of the whole table.
            tracker_dirty = dirty.get("session_tracker") if dirty is not None else None
            if tracker_dirty is None:
                with _storage_lock:
                    _tracker_items = list(_storage.get("session_tracker", {}).items())
                    _mem_tracker_uids: set = set()
                    for _tuid in _tracker_items:
                        try:
                            _mem_tracker_uids.add(int(_tuid[0]))
                        except (ValueError, TypeError):
                            continue
                for uid, slist in _tracker_items:
                    if not isinstance(slist, list):
                        continue
                    try:
                        uid_int = int(uid)
                    except (ValueError, TypeError):
                        continue
                    for s in slist:
                        if not isinstance(s, dict):
                            continue
                        conn.execute(
                            "INSERT INTO session_tracker (user_id, session_id, data) VALUES (?, ?, ?) "
                            "ON CONFLICT (user_id, session_id) DO UPDATE SET data = excluded.data",
                            (uid_int, str(s.get("session_id", "")),
                             json.dumps(s, ensure_ascii=False, default=str))
                        )
                if _mem_tracker_uids:
                    _stage_live_keys(conn, _mem_tracker_uids, integer=True)
                    _delete_stale_rows(conn, "session_tracker", "user_id", integer=True)
                else:
                    conn.execute("DELETE FROM session_tracker")
            elif tracker_dirty:
                with _storage_lock:
                    _tracker_dirty_items = []
                    for _tuid in list(tracker_dirty):
                        _tlist = _storage.get("session_tracker", {}).get(str(_tuid))
                        if isinstance(_tlist, list):
                            _tracker_dirty_items.append((_tuid, _tlist))
                for _tuid, _tlist in _tracker_dirty_items:
                    try:
                        uid_int = int(_tuid)
                    except (ValueError, TypeError):
                        continue
                    for s in _tlist:
                        if not isinstance(s, dict):
                            continue
                        conn.execute(
                            "INSERT INTO session_tracker (user_id, session_id, data) VALUES (?, ?, ?) "
                            "ON CONFLICT (user_id, session_id) DO UPDATE SET data = excluded.data",
                            (uid_int, str(s.get("session_id", "")),
                             json.dumps(s, ensure_ascii=False, default=str))
                        )

            # --- successful_numbers (row-level incremental upsert) ---------
            # Each successful phone number is ONE row (phone PK), so a growing
            # registry never re-writes the whole blob on every save. Full sync
            # (dirty None) rewrites the table + deletes rows no longer in
            # memory; incremental sync upserts only the dirty phones.
            if successful_numbers_dirty is None:
                with _storage_lock:
                    _sn_items = list(_storage.get("successful_numbers", {}).items())
                    _mem_sn: set = set(_storage.get("successful_numbers", {}).keys())
                for _phone, _sdata in _sn_items:
                    if not isinstance(_sdata, dict):
                        continue
                    conn.execute("""
                        INSERT INTO successful_numbers (phone, user_id, verified_at, updated_at)
                        VALUES (?, ?, ?, datetime('now'))
                        ON CONFLICT (phone) DO UPDATE SET
                            user_id = excluded.user_id,
                            verified_at = excluded.verified_at,
                            updated_at = datetime('now')
                    """, (
                        str(_phone),
                        str(_sdata.get("user_id", "") or ""),
                        str(_sdata.get("verified_at", "") or ""),
                    ))
                if _mem_sn:
                    _stage_live_keys(conn, _mem_sn, integer=False)
                    _delete_stale_rows(conn, "successful_numbers", "phone", integer=False)
                else:
                    conn.execute("DELETE FROM successful_numbers")
            elif successful_numbers_dirty:
                with _storage_lock:
                    _dirty_sn_items = []
                    for phone in list(successful_numbers_dirty):
                        _sdata = _storage.get("successful_numbers", {}).get(str(phone))
                        if isinstance(_sdata, dict):
                            _dirty_sn_items.append((phone, _sdata))
                for _phone, _sdata in _dirty_sn_items:
                    conn.execute("""
                        INSERT INTO successful_numbers (phone, user_id, verified_at, updated_at)
                        VALUES (?, ?, ?, datetime('now'))
                        ON CONFLICT (phone) DO UPDATE SET
                            user_id = excluded.user_id,
                            verified_at = excluded.verified_at,
                            updated_at = datetime('now')
                    """, (
                        str(_phone),
                        str(_sdata.get("user_id", "") or ""),
                        str(_sdata.get("verified_at", "") or ""),
                    ))

            # --- generic settings (everything else) ----------------------
            if settings_dirty is None:
                with _storage_lock:
                    _settings_items = [
                        (k, v) for k, v in _storage.items() if k not in _SQLITE_TABLE_KEYS
                    ]
                for key, value in _settings_items:
                    stored = _serialize_settings_value(key, value)
                    conn.execute(
                        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                        (str(key), stored)
                    )

                # --- settings: delete stale keys no longer in memory ---------
                with _storage_lock:
                    _mem_settings: set = {k for k in _storage.keys() if k not in _SQLITE_TABLE_KEYS}
                if _mem_settings:
                    _stage_live_keys(conn, _mem_settings, integer=False)
                    _delete_stale_rows(conn, "settings", "key", integer=False)
                else:
                    conn.execute("DELETE FROM settings")
            elif settings_dirty:
                with _storage_lock:
                    _dirty_settings_items = []
                    for key in list(settings_dirty):
                        if key in _SQLITE_TABLE_KEYS:
                            continue
                        _value = _storage.get(key)
                        if _value is not None:
                            _dirty_settings_items.append((key, _value))
                for key, value in _dirty_settings_items:
                    stored = _serialize_settings_value(key, value)
                    conn.execute(
                        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                        (str(key), stored)
                    )
        return True
    except Exception as e:
        logger.error(f"SQLite storage sync failed: {e}")
        return False


def _db_load_all() -> Dict[str, Any]:
    """Load full storage state from SQLite.

    Returns None when ANY table failed to load. The caller (``load_storage``)
    treats ``None`` as a failed load, keeps ``_storage_trusted = False`` and
    falls back to the JSON snapshot - so a partially-loaded in-memory state is
    never written back over a non-empty DB (a full sync would wipe every
    table whose load failed).
    """
    _load_ok = True
    data: Dict[str, Any] = {
        "users": {},
        "countries": {},
        "withdrawals": [],
        "admin_logs": [],
        "failed_verifications": [],
        "transaction_logs": [],
        "pending_sessions": {},
        "daily_counters": {},
        "daily_success_counters": {},
        "session_tracker": {},
        "successful_numbers": {},
        "maintenance": False,
        "required_channel": "",
        "withdraw_sessions": {},
    }
    conn = _get_sqlite_conn()

    try:
        for row in conn.execute(
            "SELECT user_id, balance, sessions, banned, joined, success_count, channel_joined, data FROM users"
        ):
            uid, balance, sessions, banned, joined, success_count, channel_joined, data_json = row
            try:
                u = json.loads(data_json) if data_json else {}
            except (json.JSONDecodeError, TypeError):
                u = {}
            if not isinstance(u, dict):
                u = {}
            if "balance" not in u and balance is not None:
                u["balance"] = balance
            if "sessions" not in u and sessions is not None:
                u["sessions"] = sessions
            if "banned" not in u:
                u["banned"] = bool(banned)
            if "joined" not in u and joined:
                u["joined"] = joined
            if "success_count" not in u and success_count is not None:
                u["success_count"] = success_count
            if "channel_joined" not in u:
                u["channel_joined"] = bool(channel_joined)
            data["users"][str(uid)] = u
    except Exception as e:
        _load_ok = False
        logger.error(f"SQLite users load failed: {e}")

    try:
        for row in conn.execute("SELECT country_code, data FROM countries"):
            try:
                parsed = json.loads(row[1])
                if isinstance(parsed, dict):
                    data["countries"][row[0]] = parsed
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception as e:
        _load_ok = False
        logger.error(f"SQLite countries load failed: {e}")

    try:
        for row in conn.execute("SELECT session_id, data FROM pending_sessions"):
            try:
                parsed = json.loads(row[1])
                if isinstance(parsed, dict):
                    data["pending_sessions"][row[0]] = _at_rest_decrypt_pending(parsed)
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception as e:
        _load_ok = False
        logger.error(f"SQLite pending_sessions load failed: {e}")

    try:
        for row in conn.execute("SELECT data FROM withdrawals"):
            try:
                parsed = json.loads(row[0])
                if isinstance(parsed, dict):
                    data["withdrawals"].append(parsed)
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception as e:
        _load_ok = False
        logger.error(f"SQLite withdrawals load failed: {e}")

    try:
        for row in conn.execute("SELECT user_id, data FROM session_tracker"):
            try:
                parsed = json.loads(row[1])
                if isinstance(parsed, dict):
                    data["session_tracker"].setdefault(str(row[0]), []).append(parsed)
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception as e:
        _load_ok = False
        logger.error(f"SQLite session_tracker load failed: {e}")

    # successful_numbers: row-per-phone table (the in-memory dict stays the
    # fast O(1) lookup for is_number_successful; persistence is row-level).
    try:
        for row in conn.execute("SELECT phone, user_id, verified_at FROM successful_numbers"):
            phone, uid, vat = row
            data["successful_numbers"][phone] = {
                "user_id": str(uid) if uid is not None else "",
                "verified_at": str(vat) if vat is not None else "",
            }
    except Exception as e:
        _load_ok = False
        logger.error(f"SQLite successful_numbers load failed: {e}")

    try:
        for row in conn.execute("SELECT key, value FROM settings"):
            key, value = row
            try:
                parsed = json.loads(value)
                if key == "successful_numbers" and isinstance(parsed, dict):
                    # LEGACY MIGRATION: pre-table builds stored the registry as
                    # one settings blob. Merge any phone that is not already in
                    # the dedicated table so no number is lost, then the next
                    # full settings sync drops the old settings row (it is not
                    # in _SQLITE_TABLE_KEYS-memory anymore). Mark the dedicated
                    # table dirty so the migrated rows are persisted on the
                    # very next save (not only the periodic full sync).
                    migrated = False
                    for phone, entry in parsed.items():
                        if phone not in data["successful_numbers"]:
                            data["successful_numbers"][phone] = entry
                            migrated = True
                    if migrated:
                        with _storage_lock:
                            _dirty_rows["successful_numbers"] = None
                    continue
                if key == "proxy":
                    # The proxy blob may be encrypted at rest (settings row).
                    parsed = _at_rest_decrypt_proxy(parsed)
                data[key] = parsed
            except (json.JSONDecodeError, TypeError):
                if key == "proxy":
                    data[key] = _at_rest_decrypt_proxy(value)
                else:
                    data[key] = value
    except Exception as e:
        _load_ok = False
        logger.error(f"SQLite settings load failed: {e}")

    if not _load_ok:
        return None

    return data



def export_storage_json() -> Dict[str, Any]:
    """Return a deep-copy snapshot of the current in-memory storage state.

    Used by the admin "Backup / Export Data" feature to generate a
    downloadable JSON from SQLite-backed data (no file on disk).

    B3 fix: the snapshot is materialized lock-correct — the proxy blob is
    deep-copied under its registered consistency lock (alone), everything else
    under _storage_lock (alone) — so a concurrent health mutation can never
    raise "dictionary changed size during iteration" and abort (or corrupt)
    the export. The old fallback ``json.dumps(dict(_storage))`` shallow copy
    was equally unprotected and is gone.

    Proxy credentials are redacted (passwords replaced with ``****``) so the
    export never leaks secrets — including the watchdog snapshot's raw URLs,
    which previously passed through unredacted. The structure is preserved
    for re-import, but the passwords must be re-entered after a restore.
    """
    proxy_snap = _proxy_blob_snapshot()
    with _storage_lock:
        try:
            snap = dict(_storage)
            if proxy_snap is not None:
                snap["proxy"] = proxy_snap
            data = json.loads(json.dumps(snap, default=str))
        except Exception:
            data = json.loads(json.dumps(dict(_storage), default=str))
    try:
        _redact_proxy_export(data)
    except Exception:
        pass
    return data


def _redact_proxy_export(data: Dict[str, Any]) -> None:
    """In-place redaction of proxy credentials in an export dict.

    Covers every location a raw proxy URL can reach the export:
      - the global default proxy,
      - every per-country pool entry,
      - the persisted watchdog snapshot (``watchdog.dead_by_cc[*].proxy``),
        which stores RAW pool/default URLs with passwords for the admin
        alert diffing (B3b: these previously leaked through the export).
    """
    proxy = data.get("proxy")
    if not isinstance(proxy, dict):
        return

    def _mask(value: Any) -> Any:
        if not isinstance(value, str) or not value:
            return value
        try:
            from bot.data.proxies import mask_proxy_url
            return mask_proxy_url(value)
        except Exception:
            return "<redacted-proxy>"

    if "default_proxy" in proxy and proxy["default_proxy"]:
        proxy["default_proxy"] = _mask(proxy["default_proxy"])
    countries = proxy.get("countries")
    if isinstance(countries, dict):
        for cc, cfg in countries.items():
            if not isinstance(cfg, dict):
                continue
            pool = cfg.get("proxies")
            if isinstance(pool, list):
                for entry in pool:
                    if isinstance(entry, dict) and entry.get("url"):
                        entry["url"] = _mask(entry["url"])
    watchdog = proxy.get("watchdog")
    if isinstance(watchdog, dict):
        dead_by_cc = watchdog.get("dead_by_cc")
        if isinstance(dead_by_cc, dict):
            for entries in dead_by_cc.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("proxy"):
                        entry["proxy"] = _mask(entry["proxy"])


def get_storage_db_size() -> int:
    """Size in bytes of the SQLite storage file (source of truth)."""
    try:
        return os.path.getsize(SQLITE_STORAGE_PATH)
    except OSError:
        return 0


BACKUP_DIR = os.path.join(os.path.dirname(SQLITE_STORAGE_PATH) or "data", "backups")
BACKUP_RETENTION = max(1, int(os.environ.get("DB_BACKUP_RETENTION", "5")))


def backup_database() -> Optional[str]:
    """Create a WAL-safe snapshot backup of the live database.

    Uses the SQLite Online Backup API (Connection.backup), the same 100%
    mechanism used by the admin "Download DB File" feature, so the copy is
    consistent even while the bot is running and includes data still in the
    WAL file. Older backups beyond BACKUP_RETENTION are pruned.

    Returns the backup file path, or None on failure.
    """
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dst_path = os.path.join(BACKUP_DIR, f"bot_data-{ts}.db")

        src = _get_sqlite_conn()
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()

        backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "bot_data-*.db")))
        for old in backups[:-BACKUP_RETENTION]:
            try:
                os.remove(old)
            except OSError:
                pass

        logger.info(f"DB backup created: {dst_path}")
        return dst_path
    except Exception as e:
        logger.error(f"DB backup failed: {e}")
        return None


def thread_safe(func):
    """Decorator to make storage operations thread-safe."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        with _storage_lock:
            return func(*args, **kwargs)
    return wrapper


_fernet_instance = None


def _get_fernet() -> Optional[Any]:
    """Get Fernet instance for encryption if a key is available.

    Key sources (in priority order): STORAGE_ENCRYPTION_KEY (legacy alias),
    then SESSION_SECRET. Either one alone is enough to enable at-rest
    encryption of the session strings stored in the DB.
    """
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    key = os.getenv("STORAGE_ENCRYPTION_KEY") or os.getenv("SESSION_SECRET")
    if not key:
        return None
    
    try:
        from cryptography.fernet import Fernet
        derived_key = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
        _fernet_instance = Fernet(derived_key)
        return _fernet_instance
    except ImportError:
        logger.warning("cryptography not installed - encryption disabled")
        return None
    except Exception as e:
        logger.error(f"Fernet init error: {e}")
        return None


def _encrypt_value(value: str) -> str:
    """Encrypt sensitive values using Fernet (AES-128-CBC with HMAC)."""
    fernet = _get_fernet()
    if not fernet:
        return value
    try:
        encrypted = fernet.encrypt(value.encode())
        return encrypted.decode()
    except Exception as e:
        logger.error(f"Encryption error: {e}")
        return value


def _decrypt_value(value: str) -> str:
    """Decrypt sensitive values using Fernet."""
    fernet = _get_fernet()
    if not fernet:
        return value
    try:
        decrypted = fernet.decrypt(value.encode())
        return decrypted.decode()
    except Exception as e:
        logger.error(f"Decryption error: {e}")
        return value


# P6-62: every field that holds a credential in a pending row (root level
# AND inside the nested session_data blob) is encrypted at rest together with
# the session string.
_PENDING_SECRET_FIELDS = ("twofa_password", "2fa_password")


def _at_rest_encrypt_pending(pdata: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of a pending_session dict with all CREDENTIAL fields encrypted.

    Covers the root ``session_string``/``twofa_password``/``2fa_password``
    AND the same keys inside the nested ``session_data`` blob (P6-62: the 2FA
    password previously landed in the SQLite data column in plaintext).

    Called ONLY when serializing to SQLite (at-rest protection). The in-memory
    `_storage` always keeps the plaintext so every reader works unchanged.
    No-op when no encryption key is configured.
    """
    copy = None  # lazily created on first change

    def _enc(value):
        if not isinstance(value, str) or not value:
            return value
        encrypted = _encrypt_value(value)
        return value if encrypted == value else encrypted

    ss = pdata.get("session_string")
    if isinstance(ss, str) and ss:
        encrypted = _enc(ss)
        if encrypted != ss:
            copy = dict(pdata) if copy is None else copy
            copy["session_string"] = encrypted
    for field in _PENDING_SECRET_FIELDS:
        val = pdata.get(field)
        if isinstance(val, str) and val:
            encrypted = _enc(val)
            if encrypted != val:
                copy = dict(pdata) if copy is None else copy
                copy[field] = encrypted
    inner = pdata.get("session_data")
    if isinstance(inner, dict):
        inner_changed = False
        inner_copy = dict(inner)
        for field in _PENDING_SECRET_FIELDS:
            val = inner.get(field)
            if isinstance(val, str) and val:
                encrypted = _enc(val)
                if encrypted != val:
                    inner_copy[field] = encrypted
                    inner_changed = True
        if inner_changed:
            copy = dict(pdata) if copy is None else copy
            copy["session_data"] = inner_copy
    return copy if copy is not None else pdata


def _at_rest_decrypt_pending(pdata: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of a pending_session dict loaded from SQLite with ALL
    credential fields decrypted back to plaintext.

    Backward compatible: plaintext (legacy) values pass through untouched.
    If a value looks like it was encrypted (Fernet token) but cannot be
    decrypted, the key has changed - log CRITICAL and keep the raw value so
    the operator can diagnose instead of silently sending broken sessions.
    """
    fernet = _get_fernet()
    if fernet is None:
        return pdata
    copy = None  # lazily created on first change

    def _dec(value, label):
        if not isinstance(value, str) or not value or not value.startswith("gAAAAA"):
            return value
        try:
            return fernet.decrypt(value.encode()).decode()
        except Exception:
            logger.critical(
                f"Stored {label} cannot be decrypted with the current key "
                f"(SESSION_SECRET/STORAGE_ENCRYPTION_KEY may have changed). Keeping raw value."
            )
            return value

    ss = pdata.get("session_string")
    if isinstance(ss, str) and ss.startswith("gAAAAA"):
        decrypted = _dec(ss, "session string")
        if decrypted != ss:
            copy = dict(pdata) if copy is None else copy
            copy["session_string"] = decrypted
    for field in _PENDING_SECRET_FIELDS:
        val = pdata.get(field)
        if isinstance(val, str) and val.startswith("gAAAAA"):
            decrypted = _dec(val, field)
            if decrypted != val:
                copy = dict(pdata) if copy is None else copy
                copy[field] = decrypted
    inner = pdata.get("session_data")
    if isinstance(inner, dict):
        inner_copy = None
        for field in _PENDING_SECRET_FIELDS:
            val = inner.get(field)
            if isinstance(val, str) and val.startswith("gAAAAA"):
                decrypted = _dec(val, f"session_data.{field}")
                if decrypted != val:
                    if inner_copy is None:
                        inner_copy = dict(inner)
                    inner_copy[field] = decrypted
        if inner_copy is not None:
            copy = dict(pdata) if copy is None else copy
            copy["session_data"] = inner_copy
    return copy if copy is not None else pdata


def _at_rest_encrypt_proxy(blob: Any) -> Any:
    """Encrypt the whole proxy blob for at-rest storage (settings row).

    In-memory ``_storage["proxy"]`` always stays a plaintext dict so every
    reader works unchanged; only the serialized settings row is encrypted.
    Returns the ORIGINAL blob when no encryption key is configured (legacy
    plaintext behavior) so the system degrades gracefully.
    """
    if not isinstance(blob, dict):
        return blob
    try:
        payload = json.dumps(blob, ensure_ascii=False, default=str)
        encrypted = _encrypt_value(payload)
        if encrypted == payload:
            return blob
        return encrypted
    except Exception as e:
        logger.error(f"Proxy blob encryption error: {e}")
        return blob


def _at_rest_decrypt_proxy(value: Any) -> Any:
    """Decrypt a proxy blob loaded from a settings row.

    Backward compatible: legacy plaintext JSON dicts pass through untouched.
    When the value is a Fernet token it is decrypted back to the dict. If the
    key changed, log CRITICAL and keep the raw value (operator intervention).
    """
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return value
    fernet = _get_fernet()
    looks_encrypted = value.startswith("gAAAAA")
    if fernet is None:
        return value
    try:
        decrypted = fernet.decrypt(value.encode()).decode()
        return json.loads(decrypted)
    except Exception:
        if looks_encrypted:
            logger.critical(
                "Stored proxy blob cannot be decrypted with the current key "
                "(SESSION_SECRET/STORAGE_ENCRYPTION_KEY may have changed). Keeping raw value."
            )
        return value


def _db_core_tables_have_data() -> bool:
    """Return True when the DB holds real business rows (users/countries/pending/withdrawals/tracker)."""
    try:
        conn = _get_sqlite_conn()
        for table in ("users", "countries", "pending_sessions", "withdrawals", "session_tracker", "successful_numbers"):
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            if row and row[0] > 0:
                return True
    except Exception:
        pass
    return False


def _storage_core_is_empty() -> bool:
    """Return True when in-memory _storage has no business data (users/countries/etc.).

    Structural-only keys (required_channel, admins, message_templates, ...) are
    ignored here: a fresh install legitimately saves those without any users.
    """
    return not (
        _storage.get("users")
        or _storage.get("countries")
        or _storage.get("pending_sessions")
        or _storage.get("withdrawals")
        or _storage.get("session_tracker")
        or _storage.get("successful_numbers")
    )


def _do_save() -> bool:
    """Persist dirty storage state to SQLite incrementally.

    Uses ``_db_sync_dirty`` (row-level upserts) so the write cost is
    proportional to the NUMBER of changed rows, NOT the total dataset size.
    With 60k pending sessions and 60k users, a single-session update writes
    ONE row (not 120k) — keeping the event loop responsive and the Timer
    thread fast.

    Falls back to ``_db_sync_all`` (full sync) every MAX_DIRTY_SAVES_BEFORE_FULL
    saves as a safety net, so any missed dirty-mark is eventually caught.

    CRITICAL DESIGN: ``_storage_lock`` is held ONLY for the brief counter
    update and the overwrite-guard checks. The actual DB write (``_db_sync_dirty``
    or ``_db_sync_all``) runs WITHOUT holding ``_storage_lock`` — each function
    snapshots the data it needs under a brief lock at its start, then writes
    lock-free.  This means the event loop thread can mutate ``_storage`` and
    call ``_mark_dirty`` / ``try_claim_*`` even while a full sync is writing
    60k rows to SQLite — the bot never freezes.

    Returns: True if the state was durably persisted (to SQLite or, when the
    overwrite-guard refuses the DB write or the DB write fails, to the fallback
    JSON snapshot). False only when every persistence path failed.

    FAILED-SAVE RETRY: when the incremental SQLite sync fails, the dirty rows
    have already been re-marked by ``_db_sync_dirty`` and this function re-arms
    the debounce timer (with backoff, max ``MAX_SAVE_RETRIES``) INSTEAD of
    clearing ``_save_pending`` - so a transient DB failure (lock contention,
    I/O error) retries automatically and the mutation is never left without a
    pending persistence. After the retry budget is exhausted the fallback JSON
    snapshot is written (final safety net) and ``_save_pending`` is cleared;
    the full consistency sync (``_db_sync_all_locked``) remains the periodic
    backstop on top of all of this.
    """
    global _full_save_counter, _save_pending, _save_retry_count
    do_full = False
    need_fallback = False
    with _storage_lock:
        if _full_save_counter is None:
            _full_save_counter = 0
        _full_save_counter += 1
        do_full = _full_save_counter >= MAX_DIRTY_SAVES_BEFORE_FULL
        if do_full:
            _full_save_counter = 0

        # Overwrite-guard (fast — microsecond-level in the common path).
        # The SQLite COUNT queries only run when they can actually matter:
        # (a) the in-memory state is untrusted (startup/corrupt-load path), or
        # (b) memory has no business data (fresh install / full wipe). In the
        # normal trusted + populated case both DB queries are skipped entirely,
        # so a save never does extra SQLite reads under load.
        if (not _storage_trusted and _db_has_storage_data()) or (
            _storage_core_is_empty() and _db_core_tables_have_data()
        ):
            need_fallback = True

    # B2 (deadlock fix): the fallback snapshot write happens AFTER releasing
    # _storage_lock. The old code called _write_fallback_json() while HOLDING
    # _storage_lock; the writer acquires the registered settings-consistency
    # locks (the proxy blob lock), while every proxy mutator holds that blob
    # lock and then takes _storage_lock (via _persist -> save_storage) — a
    # textbook AB-BA deadlock. The guard DECISION is still made atomically
    # under the lock above; only the write moved out.
    guard_ok = not need_fallback
    guard_fallback = False
    if need_fallback:
        guard_fallback = _write_fallback_json()

    # Sync WITHOUT _storage_lock — event loop stays responsive. The sync
    # functions themselves serialize concurrent writers via _db_write_lock.
    if guard_ok:
        try:
            if do_full:
                # A full consistency sweep is due (safety net). Never run it
                # inline here — save_storage_now / admin paths call _do_save on
                # the event loop thread, and a 60k-row full rewrite would freeze
                # the bot for seconds. The current dirty rows are still persisted
                # inline right now (fast), and the full sweep runs in background.
                _request_background_full_sync()
            success = _db_sync_dirty()
        except Exception as e:
            logger.error(f"SQLite storage sync raised an exception: {e}")
            success = False
        if not success:
            if _arm_failed_save_retry():
                # Retry timer armed; the dirty marks were re-marked by
                # _db_sync_dirty, and _save_pending is intentionally NOT
                # cleared so the timer owns the next attempt. The fallback
                # JSON snapshot is attempted once the retry budget is
                # exhausted (below), as the final durability net.
                return False
            # Retry budget exhausted: fallback JSON is the final net.
            if _write_fallback_json():
                logger.critical(
                    f"SQLite storage sync failed {MAX_SAVE_RETRIES + 1} consecutive "
                    "times; state snapshot saved to fallback JSON instead"
                )
                success = True
            else:
                logger.critical(
                    f"SQLite storage sync failed {MAX_SAVE_RETRIES + 1} consecutive "
                    "times AND fallback JSON write failed; state could NOT be persisted"
                )
                success = False
    else:
        success = guard_fallback
        if guard_fallback:
            logger.critical(
                "REFUSED to overwrite a populated database with incomplete/empty "
                "in-memory storage; state snapshot saved to fallback JSON instead "
                "(trusted=%s core_empty=%s). DB preserved on disk.",
                _storage_trusted, _storage_core_is_empty(),
            )
        else:
            logger.critical(
                "REFUSED to overwrite a populated database AND fallback JSON write "
                "failed; in-memory state could NOT be persisted "
                "(trusted=%s core_empty=%s).",
                _storage_trusted, _storage_core_is_empty(),
            )

    with _storage_lock:
        _save_pending = False
    if success:
        _save_retry_count = 0
    return success


def _arm_failed_save_retry() -> bool:
    """Re-arm the debounce timer after a failed save (backoff, bounded).

    Returns True when a retry was armed (the caller must leave
    ``_save_pending`` set so the timer owns the next attempt), False when the
    retry budget is exhausted (the caller should fall through to the fallback
    JSON snapshot and clear ``_save_pending``).
    """
    global _save_retry_count, _save_timer
    with _save_lock:
        if _save_retry_count >= MAX_SAVE_RETRIES:
            _save_retry_count = 0
            return False
        _save_retry_count += 1
        if _save_timer:
            _save_timer.cancel()
        delay = SAVE_RETRY_BACKOFF_BASE * _save_retry_count
        _save_timer = threading.Timer(delay, _do_save)
        _save_timer.daemon = True
        _save_timer.start()
    logger.error(
        f"SQLite storage sync failed; dirty rows re-marked. Retrying in "
        f"{delay:.1f}s (attempt {_save_retry_count}/{MAX_SAVE_RETRIES})."
    )
    return True


# Full-sync safety net: every N dirty saves we run a full consistency sync.
# Catches any row that was mutated but never marked dirty. Deliberately high:
# the incremental hot path plus the periodic full sync in memory_manager
# already guarantee consistency, so this counter is a rare backstop, not the
# primary mechanism.
MAX_DIRTY_SAVES_BEFORE_FULL = 2000
_full_save_counter: Optional[int] = None

# ------------------------------------------------------------------
# Coalescing background-save worker
# ------------------------------------------------------------------
# _do_save_background() spawns ONE worker thread that loops until no more
# saves are pending. Bursts of claim/mark/credit calls (which at 60k sessions
# per day number in the hundreds of thousands) therefore coalesce into a
# single sequential writer instead of spawning a new thread per call. The
# worker reads the CURRENT in-memory _storage at execution time, so any state
# mutated after its snapshot is caught by the next loop pass — nothing is
# lost, and the event loop is never blocked.
_bg_save_lock = threading.Lock()
_bg_save_needed = False
_bg_save_running = False


def _do_save_background() -> None:
    """Schedule a durable save on a background daemon thread (non-blocking).

    The in-memory ``_storage`` is ALREADY mutated by the caller. This fires a
    single coalescing worker thread that runs ``_do_save`` (incremental DB
    write) without blocking the event loop. A crash before the worker
    completes loses only the most recent mutations — same durability window
    as ``save_storage``'s debounce timer.

    Use this in hot-path functions (claim, mark, add, update, reject) that
    currently call ``_do_save()`` directly and block the event loop.
    """
    global _bg_save_needed, _bg_save_running
    with _bg_save_lock:
        _bg_save_needed = True
        if _bg_save_running:
            return
        _bg_save_running = True
    t = threading.Thread(target=_bg_save_worker, daemon=True)
    t.start()


def _bg_save_worker() -> None:
    """Background worker: keep saving while new saves are requested.

    On exit it re-checks ``_bg_save_needed`` INSIDE the lock: a caller that
    set the flag just as this worker decided to stop must not be left with a
    pending save and no worker — the worker loops one more time instead of
    returning, or (if it already released the lock) the caller spawns a fresh
    worker because ``_bg_save_running`` is False again.
    """
    global _bg_save_needed, _bg_save_running
    while True:
        with _bg_save_lock:
            if not _bg_save_needed:
                _bg_save_running = False
                if _bg_save_needed:
                    # A request raced in just as we decided to stop: keep the
                    # worker running to pick it up.
                    _bg_save_running = True
                    continue
                return
            _bg_save_needed = False
        try:
            _do_save()
        except Exception as e:
            logger.error(f"Background save worker error: {e}")


# ------------------------------------------------------------------
# Background full-sync sweeper
# ------------------------------------------------------------------
# The counter-based full consistency sweep (every MAX_DIRTY_SAVES_BEFORE_FULL
# saves) must NEVER run on the event loop thread: _do_save is also called by
# save_storage_now() / admin handlers, and a 60k-row full rewrite there would
# freeze the bot for seconds. This fires the sweep on its own daemon thread,
# coalesced (at most one at a time).
_bg_full_sync_lock = threading.Lock()
_bg_full_sync_running = False


def _request_background_full_sync() -> None:
    """Schedule a full consistency sync on a background daemon thread."""
    global _bg_full_sync_running
    with _bg_full_sync_lock:
        if _bg_full_sync_running:
            return
        _bg_full_sync_running = True
    t = threading.Thread(target=_bg_full_sync_worker, daemon=True)
    t.start()


def _bg_full_sync_worker() -> None:
    """Run the full consistency sync; never blocks the event loop."""
    global _bg_full_sync_running
    try:
        _db_sync_all()
    except Exception as e:
        logger.error(f"Background full-sync error: {e}")
    finally:
        with _bg_full_sync_lock:
            _bg_full_sync_running = False


def _ensure_default_keys() -> None:
    """Ensure default top-level keys exist after loading."""
    _storage.setdefault("users", {})
    _storage.setdefault("countries", {})
    _storage.setdefault("withdrawals", [])
    _storage.setdefault("admin_logs", [])
    _storage.setdefault("failed_verifications", [])
    _storage.setdefault("transaction_logs", [])
    _storage.setdefault("pending_sessions", {})
    _storage.setdefault("daily_counters", {})
    _storage.setdefault("daily_success_counters", {})
    _storage.setdefault("session_tracker", {})
    _storage.setdefault("successful_numbers", {})
    _storage.setdefault("extra_apis", [])
    _storage.setdefault("maintenance", False)
    _storage.setdefault("required_channel", "")
    # S1: any structural key that had to be created gets marked for the next
    # save (fresh installs persist their defaults on the first write).
    _mark_all_settings_keys_dirty()


def load_storage() -> None:
    """Load storage state from SQLite (the only storage), with a fallback snapshot.

    If SQLite is empty (fresh install) defaults are used. If the DB holds data
    but it cannot be loaded (corrupt / parse failure / unopenable file), the
    fallback JSON snapshot is restored so the in-memory state is never silently
    emptied. In that case the overwrite-guard keeps writes away from the
    populated DB (saves go to the fallback snapshot) until a successful SQLite
    load marks the state trusted again.

    IMPORTANT: _storage is mutated in place (clear + update), never rebound.
    Domain modules (bot/data/users.py etc.) hold references to this exact dict
    object; rebinding it would leave them pointing at stale state that a later
    save would use to full-replace (and wipe) the real data.
    """
    global _storage, _storage_trusted
    with _storage_lock:
        loaded = None
        try:
            if _db_has_storage_data():
                loaded = _db_load_all()
                if not loaded:
                    loaded = None
        except Exception as e:
            logger.warning(f"SQLite storage load failed ({e}); trying fallback snapshot")

        if loaded is not None:
            _storage.clear()
            _storage.update(loaded)
            _ensure_default_keys()
            _storage_trusted = True
            logger.info(f"Storage loaded from SQLite ({SQLITE_STORAGE_PATH})")
            return

        if _db_has_storage_data():
            # DB holds real rows but they could not be loaded into memory.
            fallback = _load_fallback_json()
            if fallback is not None:
                _storage.clear()
                _storage.update(fallback)
                _ensure_default_keys()
                _storage_trusted = False
                logger.critical(
                    "DB contains data but it could not be loaded; restored from fallback "
                    "JSON snapshot. Writes are directed to the fallback snapshot until a "
                    "successful SQLite load marks the state trusted."
                )
            else:
                _storage_trusted = False
                logger.critical(
                    "DB contains data but it could not be loaded into memory and no "
                    "fallback snapshot exists. In-memory state is NOT trusted: writes "
                    "to the DB are blocked to protect existing data."
                )
            return

        # DB is empty or could not even be verified (e.g. corrupt file). Prefer
        # a recent fallback snapshot so no data is silently lost; stay untrusted
        # so a populated-but-unopenable DB is never overwritten.
        fallback = _load_fallback_json()
        if fallback is not None:
            _storage.clear()
            _storage.update(fallback)
            _ensure_default_keys()
            _storage_trusted = False
            logger.critical(
                "SQLite storage could not be verified (empty or unreadable); restored "
                "from fallback JSON snapshot. Writes are directed to the fallback "
                "snapshot until a successful SQLite load marks the state trusted."
            )
        else:
            _ensure_default_keys()
            _storage_trusted = True


# ---------------------------------------------------------------
# Row-level dirty tracking for the settings table (S1 fix).
# ---------------------------------------------------------------
# Every save_storage()/save_storage_now() used to mark the WHOLE settings
# table dirty; each save then re-serialized and re-wrote every settings blob
# (user_rate_limits with up to 50k entries, 3x5000-entry log lists, the
# Fernet-encrypted proxy blob, counters, templates...). At scale that turned
# every counter increment into a multi-MB JSON dump. The settings row keys are
# now tracked like every other table: mutators mark the exact key they changed
# and the incremental sync upserts only those rows.
#
# The legacy "mark everything" behavior remains available through
# _mark_table_dirty("settings") for bulk/import paths, and a periodic full
# sync still guarantees any unmarked key eventually persists.
def _mark_settings_key_dirty(key: str) -> None:
    """Mark ONE settings-table key as needing persistence (row-level)."""
    if not key or key in _SQLITE_TABLE_KEYS:
        return
    with _storage_lock:
        current = _dirty_rows.get("settings")
        if current is None:
            return  # already fully dirty - nothing to add
        current.add(key)


def _storage_keys_not_in_tables() -> set:
    """Snapshot (under _storage_lock) of the settings-table keys in memory."""
    return {k for k in _storage.keys() if k not in _SQLITE_TABLE_KEYS}


def _mark_all_settings_keys_dirty() -> None:
    """Mark every currently-present settings key dirty (row-level).

    Used when a module mutated several settings keys but did not track them
    individually; cheaper than a full-table mark because the sync still only
    writes rows that actually exist instead of re-serializing everything and
    diffing against the DB.
    """
    with _storage_lock:
        current = _dirty_rows.get("settings")
        if current is None:
            return
        current.update(_storage_keys_not_in_tables())


def save_storage(immediate: bool = False) -> None:
    """Save storage to file - debounced for speed (saves after 0.5s delay).

    On shutdown, call save_storage_now() or use immediate=True to force an
    instant write. An atexit handler is registered for best-effort flush.

    S1 (row-level settings): instead of marking the WHOLE settings table
    dirty on every call (which re-serialized every settings blob - including
    the 50k-entry rate-limit store and the encrypted proxy blob - on every
    save), only keys that a mutator actually changed get marked. Domain
    modules mark their own settings keys via _mark_settings_key_dirty /
    _mark_all_settings_keys_dirty. A periodic full sync (background) remains
    the safety net for any legacy unmarked mutation.
    users/pending_sessions/countries rely on their domain functions marking
    dirty rows.
    """
    global _save_pending, _save_timer

    if immediate:
        _do_save()
        return

    with _save_lock:
        if _save_pending:
            return
        _save_pending = True

        if _save_timer:
            _save_timer.cancel()

        _save_timer = threading.Timer(0.5, _do_save)
        _save_timer.daemon = True
        _save_timer.start()


def save_storage_now() -> None:
    """Force immediate save - use for critical operations (money, shutdown)."""
    global _save_timer
    with _save_lock:
        if _save_timer:
            _save_timer.cancel()
            _save_timer = None
    _do_save()


def _flush_pending_save():
    """Flush any pending save on shutdown."""
    global _save_pending, _save_timer
    if _save_pending:
        if _save_timer:
            _save_timer.cancel()
            _save_timer = None
        _do_save()
    # Final full consistency sync so a graceful shutdown never leaves any
    # row (even an unmarked one) behind in memory only.
    # CRITICAL: set dirty marks WITHOUT holding _storage_lock across the
    # sync call. The background worker may hold _db_write_lock (inside
    # _db_sync_dirty) while waiting for _storage_lock; if we held
    # _storage_lock here while waiting for _db_write_lock (inside
    # _db_sync_all), we would deadlock.
    try:
        # OVERWRITE-GUARD (final-review fix): the shutdown full sync runs ONLY
        # when the in-memory state is TRUSTED. A failed load left untrusted
        # (possibly near-empty) memory while the DB stays populated — a
        # shutdown full-replace in that state would wipe real user balances.
        # When untrusted, the fallback JSON (written by the save paths) is the
        # durability net instead.
        if not _storage_trusted:
            logger.warning(
                "Storage final full sync SKIPPED: in-memory state is untrusted "
                "(populated DB protected); fallback JSON holds the latest snapshot"
            )
            return
        with _storage_lock:
            _dirty_rows["users"] = None
            _dirty_rows["pending_sessions"] = None
            _dirty_rows["countries"] = None
            _dirty_rows["settings"] = None
            _dirty_rows["withdrawals"] = None
            _dirty_rows["session_tracker"] = None
            _dirty_rows["successful_numbers"] = None
        # If a daemon worker (Timer thread, background worker) was killed
        # mid-write during shutdown, _db_write_lock may be permanently held.
        # Use try_lock with a timeout to avoid hanging the process forever.
        locked = _db_write_lock.acquire(timeout=5)
        if locked:
            try:
                # CRITICAL: call the *_locked* variant here. _db_write_lock is
                # a NON-REENTRANT Lock: calling _db_sync_all() now would try to
                # re-acquire the lock this thread already holds and deadlock
                # forever, hanging every graceful shutdown/restart.
                _db_sync_all_locked()
            finally:
                _db_write_lock.release()
        else:
            logger.warning("Storage final flush skipped: _db_write_lock busy (daemon worker killed mid-write)")
    except Exception as e:
        logger.error(f"Storage final flush failed: {e}")


atexit.register(_flush_pending_save)


# P7-76: the storage load previously ran at IMPORT time. Any ad-hoc script
# importing a bot module then touched/created the production SQLite file, and
# a corrupt import-time load could crash unrelated tooling. The load is now
# EXPLICIT: bot/__main__.py (and the test harness) call ``init_storage()``.
# For safety, any domain module that reads _storage before init gets an
# auto-load on first access through the accessor below.
_storage_initialized = False


def init_storage() -> None:
    """Explicitly load storage from SQLite (call once at process startup)."""
    global _storage_initialized
    if _storage_initialized:
        return
    load_storage()
    _storage_initialized = True


def ensure_storage_loaded() -> None:
    """Auto-load guard for early readers (idempotent, thread-safe)."""
    if not _storage_initialized:
        init_storage()


# MEDIUM-22: the P7-76 comment above claims the load is explicit, but the
# call below was still a bare ``load_storage()`` - which left the
# ``_storage_initialized`` flag FALSE forever, so ``ensure_storage_loaded()``
# would have re-loaded the whole DB on the first guarded reader. Route the
# import-time load through ``init_storage()`` (idempotent, sets the flag);
# ``bot/__main__.py main()`` also calls it explicitly, and tests keep working
# through the import-time init (conftest sets the env vars before importing).
init_storage()


def is_fallback_snapshot_enabled() -> bool:
    """True when crash-backup snapshots can be written (MEDIUM-23).

    A missing SESSION_SECRET silently disables the fallback JSON crash-backup
    (the error log is easy to miss). Expose the state so the admin panel can
    show it on the DB Health screen."""
    key = os.getenv("SESSION_SECRET") or os.getenv("STORAGE_ENCRYPTION_KEY") or ""
    allow_plain = os.getenv("FALLBACK_ALLOW_PLAINTEXT") == "1"
    return bool(key or allow_plain)


def _parse_date(date_str: Optional[str]) -> datetime:
    """Parse ISO date string, return epoch if invalid.

    P4-46: normalizes AWARE -> NAIVE (the same guard sessions.py:426 uses) so
    an aware timestamp can never raise "can't compare offset-naive and
    offset-aware datetimes" inside the retention/active-user cutoffs that
    consume this value - those callers are not wrapped in try/except.
    """
    if not date_str:
        return datetime.min
    try:
        parsed = datetime.fromisoformat(str(date_str))
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed
    except (ValueError, TypeError):
        return datetime.min
