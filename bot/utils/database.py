"""
Database Module - SQLite Only
=============================
File-based SQLite via aiosqlite - no external server needed.

Features:
- Async SQLite via aiosqlite
- Full schema: users, sessions, pending_sessions, transaction_logs,
  admin_logs, failed_verifications, withdrawal_logs, session_analytics, bot_metrics
"""

import os
import logging
import sys
from datetime import datetime
from typing import Dict, Any, Optional, List, Union
import json

logger = logging.getLogger(__name__)


def _resolve_db_path() -> str:
    """``SQLITE_DB_PATH`` or the production default - never for ad-hoc code.

    Same tripwire as ``bot.data.state``: an ad-hoc ``python -c "import bot..."``
    or a REPL launched from the repo root would otherwise silently write the
    LIVE database (it already leaked synthetic rows into production once).
    Duplicated rather than imported to keep this module dependency-free.
    """
    explicit = os.environ.get("SQLITE_DB_PATH")
    if explicit:
        return explicit
    argv0 = (sys.argv[0] if sys.argv else "") or ""
    if argv0 in ("-c", ""):
        raise RuntimeError(
            "SQLITE_DB_PATH is not set and this is an ad-hoc `python -c` / REPL "
            "process, which would write the PRODUCTION database at "
            "'data/bot_data.db'. Export SQLITE_DB_PATH to a throwaway path first."
        )
    return "data/bot_data.db"


DB_PATH = _resolve_db_path()

_sqlite_db: Any = None

_BACKEND = "sqlite"
_initialized = False


# ============================================================
# At-rest encryption for session strings (async layer)
# ============================================================
# The sync storage layer (bot.data.state) Fernet-encrypts `session_string`
# when persisting pending_sessions; the async layer's own `sessions` table
# previously stored it in PLAINTEXT - an inconsistent secret surface. These
# helpers reuse the SAME key derivation and ciphertext format, so:
#  - encryption is active when SESSION_SECRET/STORAGE_ENCRYPTION_KEY is set,
#  - a value is encrypted only if not already a Fernet token (the try/except
#    decrypt round-trip makes legacy plaintext rows readable forever).

def _get_async_fernet():
    """Fernet instance shared with the sync layer's key derivation."""
    global _async_fernet_instance
    if _async_fernet_instance is not None:
        return _async_fernet_instance
    key = os.getenv("STORAGE_ENCRYPTION_KEY") or os.getenv("SESSION_SECRET")
    if not key:
        return None
    try:
        import base64
        import hashlib
        from cryptography.fernet import Fernet
        derived_key = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
        _async_fernet_instance = Fernet(derived_key)
        return _async_fernet_instance
    except ImportError:
        logger.warning("cryptography not installed - async-layer encryption disabled")
        return None
    except Exception as e:
        logger.error(f"Async Fernet init error: {e}")
        return None

_async_fernet_instance = None


def _is_fernet_token(value: str) -> bool:
    """Cheap structural check: Fernet tokens start with 'gAAAAA' (version+timestamp b64)."""
    return value.startswith("gAAAAA")


def _encrypt_session_string(value: str) -> str:
    """Encrypt a session string for at-rest storage (no-op without a key)."""
    if not value or not isinstance(value, str):
        return value
    fernet = _get_async_fernet()
    if not fernet:
        return value
    try:
        return fernet.encrypt(value.encode()).decode()
    except Exception as e:
        logger.error(f"Session string encryption error: {e}")
        return value


def _decrypt_session_string(value: str) -> str:
    """Decrypt an at-rest session string; legacy plaintext passes through."""
    if not value or not isinstance(value, str) or not _is_fernet_token(value):
        return value
    fernet = _get_async_fernet()
    if not fernet:
        return value
    try:
        return fernet.decrypt(value.encode()).decode()
    except Exception as e:
        logger.warning(f"Session string decryption failed (wrong key? keeping raw): {e}")
        return value


# ============================================================
# SQLite Schema
# ============================================================

SQLITE_SCHEMA = """
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

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE NOT NULL,
    user_id INTEGER NOT NULL,
    phone TEXT,
    country_code TEXT,
    spam_status TEXT,
    session_string TEXT,
    data TEXT DEFAULT '{}',
    verified_at TEXT DEFAULT (datetime('now')),
    created_at TEXT DEFAULT (datetime('now'))
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

CREATE TABLE IF NOT EXISTS transaction_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    session_id TEXT,
    action TEXT,
    amount REAL,
    country_code TEXT,
    phone_number TEXT,
    spam_status TEXT,
    details TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS admin_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER,
    action TEXT,
    target_user_id INTEGER,
    details TEXT DEFAULT '{}',
    message TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS failed_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    phone_number TEXT,
    country_code TEXT,
    reason TEXT,
    error_type TEXT,
    details TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS withdrawal_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    method TEXT,
    amount REAL,
    wallet_address TEXT,
    status TEXT DEFAULT 'pending',
    processed_by INTEGER,
    processed_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS session_analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT DEFAULT (date('now')),
    country_code TEXT,
    total_sessions INTEGER DEFAULT 0,
    successful_sessions INTEGER DEFAULT 0,
    failed_sessions INTEGER DEFAULT 0,
    total_earnings REAL DEFAULT 0,
    avg_spam_score REAL,
    spam_score_sum REAL DEFAULT 0,
    spam_score_count INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(date, country_code)
);

CREATE TABLE IF NOT EXISTS bot_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name TEXT,
    metric_value REAL,
    metric_type TEXT,
    recorded_at TEXT DEFAULT (datetime('now'))
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

CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id);
CREATE INDEX IF NOT EXISTS idx_session_tracker_user ON session_tracker(user_id);

CREATE INDEX IF NOT EXISTS idx_users_banned ON users(banned);
CREATE INDEX IF NOT EXISTS idx_users_channel_joined ON users(channel_joined);
CREATE INDEX IF NOT EXISTS idx_users_created ON users(created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_country ON sessions(country_code);
CREATE INDEX IF NOT EXISTS idx_sessions_verified ON sessions(verified_at);
CREATE INDEX IF NOT EXISTS idx_pending_user ON pending_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_sessions(status);
CREATE INDEX IF NOT EXISTS idx_pending_created ON pending_sessions(created_at);
CREATE INDEX IF NOT EXISTS idx_transaction_logs_user ON transaction_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_transaction_logs_created ON transaction_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_transaction_logs_country ON transaction_logs(country_code);
CREATE INDEX IF NOT EXISTS idx_admin_logs_admin ON admin_logs(admin_id);
CREATE INDEX IF NOT EXISTS idx_admin_logs_created ON admin_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_failed_verifications_user ON failed_verifications(user_id);
CREATE INDEX IF NOT EXISTS idx_withdrawal_logs_user ON withdrawal_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_withdrawal_logs_status ON withdrawal_logs(status);
CREATE INDEX IF NOT EXISTS idx_session_analytics_date ON session_analytics(date);
CREATE INDEX IF NOT EXISTS idx_session_analytics_country ON session_analytics(country_code);
"""


# ============================================================
# SQLite Helper Functions
# ============================================================

def _ensure_db_dir():
    os.makedirs(os.path.dirname(DB_PATH) or "data", exist_ok=True)


async def _sqlite_execute(sql: str, params: tuple = ()) -> Any:
    if not _sqlite_db:
        return None
    cursor = await _sqlite_db.execute(sql, params)
    await _sqlite_db.commit()
    return cursor


async def _sqlite_fetchone(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    if not _sqlite_db:
        return None
    cursor = await _sqlite_db.execute(sql, params)
    row = await cursor.fetchone()
    if row is None:
        return None
    return _sqlite_row_to_dict(row, cursor)


async def _sqlite_fetchall(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    if not _sqlite_db:
        return []
    cursor = await _sqlite_db.execute(sql, params)
    rows = await cursor.fetchall()
    return [_sqlite_row_to_dict(row, cursor) for row in rows]


def _sqlite_row_to_dict(row: tuple, cursor) -> Dict[str, Any]:
    cols = [desc[0] for desc in cursor.description]
    result = dict(zip(cols, row))
    for key, value in result.items():
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, (dict, list)):
                    result[key] = parsed
            except (json.JSONDecodeError, TypeError):
                pass
    return result


# ============================================================
# Initialize / Close
# ============================================================

async def init_database() -> bool:
    global _sqlite_db, _initialized, _BACKEND

    try:
        import aiosqlite
    except ImportError:
        logger.error("aiosqlite not installed - cannot initialize database")
        _BACKEND = None
        _initialized = False
        return False

    try:
        _ensure_db_dir()
        _sqlite_db = await aiosqlite.connect(DB_PATH)
        await _sqlite_db.execute("PRAGMA journal_mode=WAL")
        # busy_timeout: the sync storage layer (bot.data.state) writes the SAME
        # database file from its own threads with busy_timeout=5000. Without an
        # equivalent wait here, the async layer's writes fail instantly with
        # "database is locked" whenever they collide with a sync-layer write
        # storm (guaranteed at 40-50k sessions/day).
        await _sqlite_db.execute("PRAGMA busy_timeout=5000")
        await _sqlite_db.execute("PRAGMA foreign_keys=ON")
        await _sqlite_db.executescript(SQLITE_SCHEMA)
        # Migration: add spam_score_sum / spam_score_count to session_analytics
        # on databases created before the true-weighted-average fix. Existing
        # rows keep their (slightly biased) avg_spam_score; the counters start
        # at 0 so the next update recomputes from the fresh session only. New
        # databases already have the columns via SQLITE_SCHEMA above.
        try:
            cols = await _sqlite_db.execute(
                "PRAGMA table_info(session_analytics)"
            )
            col_names = {row[1] for row in await cols.fetchall()}
            if "spam_score_sum" not in col_names:
                await _sqlite_db.execute(
                    "ALTER TABLE session_analytics ADD COLUMN spam_score_sum REAL DEFAULT 0"
                )
            if "spam_score_count" not in col_names:
                await _sqlite_db.execute(
                    "ALTER TABLE session_analytics ADD COLUMN spam_score_count INTEGER DEFAULT 0"
                )
        except Exception as mig_err:
            logger.warning(f"session_analytics migration skipped: {mig_err}")
        await _sqlite_db.commit()
        _BACKEND = "sqlite"
        _initialized = True
        logger.info(f"SQLite database initialized at {DB_PATH}")
        return True
    except Exception as e:
        logger.error(f"SQLite init failed: {e}")
        if _sqlite_db:
            try:
                await _sqlite_db.close()
            except Exception:
                pass
            _sqlite_db = None
        _BACKEND = None
        _initialized = False
        return False


async def close_database() -> None:
    global _sqlite_db, _initialized, _BACKEND
    if _sqlite_db:
        await _sqlite_db.close()
        _sqlite_db = None
        logger.info("SQLite connection closed")
    _initialized = False
    _BACKEND = None if not _initialized else "sqlite"


def is_database_ready() -> bool:
    return _initialized and _BACKEND is not None


def get_backend() -> Optional[str]:
    return _BACKEND


# ============================================================
# Transaction Logging
# ============================================================

async def log_transaction(
    user_id: int,
    action: str,
    amount: float = 0.0,
    session_id: str = "",
    country_code: str = "",
    phone_number: str = "",
    spam_status: str = "",
    details: Optional[Dict] = None
) -> bool:
    if not _initialized:
        return False

    try:
        await _sqlite_execute("""
            INSERT INTO transaction_logs
            (user_id, session_id, action, amount, country_code, phone_number, spam_status, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, session_id, action, amount, country_code, phone_number, spam_status,
              json.dumps(details or {})))
        return True
    except Exception as e:
        logger.error(f"Error logging transaction: {e}")
    return False


# ============================================================
# Admin Action Logging
# ============================================================

async def log_admin_action(
    admin_id: int,
    action: str,
    target_user_id: Optional[int] = None,
    message: str = "",
    details: Optional[Dict] = None
) -> bool:
    if not _initialized:
        return False

    try:
        await _sqlite_execute("""
            INSERT INTO admin_logs (admin_id, action, target_user_id, message, details)
            VALUES (?, ?, ?, ?, ?)
        """, (admin_id, action, target_user_id, message, json.dumps(details or {})))
        return True
    except Exception as e:
        logger.error(f"Error logging admin action: {e}")
    return False


# ============================================================
# Failed Verification Logging
# ============================================================

async def log_failed_verification(
    user_id: int,
    reason: str,
    phone_number: str = "",
    country_code: str = "",
    error_type: str = "",
    details: Optional[Dict] = None
) -> bool:
    if not _initialized:
        return False

    try:
        await _sqlite_execute("""
            INSERT INTO failed_verifications
            (user_id, phone_number, country_code, reason, error_type, details)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, phone_number, country_code, reason, error_type,
              json.dumps(details or {})))
        return True
    except Exception as e:
        logger.error(f"Error logging failed verification: {e}")
    return False


# ============================================================
# Withdrawal Logging
# ============================================================

async def log_withdrawal(
    user_id: int,
    method: str,
    amount: float,
    wallet_address: str,
    status: str = "pending"
) -> bool:
    if not _initialized:
        return False

    try:
        await _sqlite_execute("""
            INSERT INTO withdrawal_logs (user_id, method, amount, wallet_address, status)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, method, amount, wallet_address, status))
        return True
    except Exception as e:
        logger.error(f"Error logging withdrawal: {e}")
    return False


# ============================================================
# Session Analytics
# ============================================================

async def update_session_analytics(
    country_code: str,
    successful: bool,
    amount: float = 0.0,
    spam_score: float = 0.0
) -> bool:
    if not _initialized:
        return False

    # True weighted mean (sum/count) instead of a rolling average-of-averages.
    # Two extra columns (spam_score_sum / spam_score_count) track the running
    # totals so the reported average is correct however the scores vary.
    try:
        await _sqlite_execute("""
            INSERT INTO session_analytics (date, country_code, total_sessions, successful_sessions,
                                           failed_sessions, total_earnings, avg_spam_score,
                                           spam_score_sum, spam_score_count)
            VALUES (date('now'), ?, 1, ?, ?, ?, ?, ?, 1)
            ON CONFLICT (date, country_code) DO UPDATE SET
                total_sessions = session_analytics.total_sessions + 1,
                successful_sessions = session_analytics.successful_sessions + ?,
                failed_sessions = session_analytics.failed_sessions + ?,
                total_earnings = session_analytics.total_earnings + ?,
                spam_score_sum = session_analytics.spam_score_sum + ?,
                spam_score_count = session_analytics.spam_score_count + 1,
                avg_spam_score = (session_analytics.spam_score_sum + ?) / (session_analytics.spam_score_count + 1),
                updated_at = datetime('now')
        """, (country_code,
              1 if successful else 0, 0 if successful else 1, amount, spam_score, spam_score,
              1 if successful else 0, 0 if successful else 1, amount, spam_score, spam_score))
        return True
    except Exception as e:
        logger.error(f"Error updating session analytics: {e}")
    return False


async def record_session_analytics(
    session_id: str,
    country_code: str,
    successful: bool,
    amount: float = 0.0,
    spam_score: float = 0.0,
) -> bool:
    """Record a session into session_analytics for the daily/country stats.

    Thin async wrapper around ``update_session_analytics`` so every
    verification path (monitor, delayed task, recovery) can log the real
    per-session outcome with one call. ``spam_score`` is passed as-is from
    the caller (the real score recorded by ``check_account_status``); when it
    is unknown the caller passes 0.0 rather than a made-up number.
    """
    try:
        return await update_session_analytics(
            country_code=country_code,
            successful=successful,
            amount=amount,
            spam_score=spam_score,
        )
    except Exception as e:
        logger.error(f"Error recording session analytics: {e}")
    return False


async def record_metric(metric_name: str, metric_value: float, metric_type: str = "gauge") -> bool:
    if not _initialized:
        return False

    try:
        await _sqlite_execute("""
            INSERT INTO bot_metrics (metric_name, metric_value, metric_type)
            VALUES (?, ?, ?)
        """, (metric_name, metric_value, metric_type))
        return True
    except Exception as e:
        logger.error(f"Error recording metric: {e}")
    return False


# ============================================================
# Query Functions - Transactions
# ============================================================

async def get_recent_transactions(limit: int = 50) -> List[Dict[str, Any]]:
    if not _initialized:
        return []

    try:
        return await _sqlite_fetchall("""
            SELECT * FROM transaction_logs
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))
    except Exception as e:
        logger.error(f"Error getting transactions: {e}")
    return []


async def get_recent_admin_logs(limit: int = 50) -> List[Dict[str, Any]]:
    if not _initialized:
        return []

    try:
        return await _sqlite_fetchall("""
            SELECT * FROM admin_logs
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))
    except Exception as e:
        logger.error(f"Error getting admin logs: {e}")
    return []


async def get_failed_verifications(limit: int = 50) -> List[Dict[str, Any]]:
    if not _initialized:
        return []

    try:
        return await _sqlite_fetchall("""
            SELECT * FROM failed_verifications
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))
    except Exception as e:
        logger.error(f"Error getting failed verifications: {e}")
    return []


async def get_pending_withdrawals() -> List[Dict[str, Any]]:
    if not _initialized:
        return []

    try:
        return await _sqlite_fetchall("""
            SELECT * FROM withdrawal_logs
            WHERE status = 'pending'
            ORDER BY created_at DESC
        """)
    except Exception as e:
        logger.error(f"Error getting pending withdrawals: {e}")
    return []


# ============================================================
# Query Functions - Statistics
# ============================================================

async def get_daily_stats(days: int = 7) -> List[Dict[str, Any]]:
    if not _initialized:
        return []

    try:
        return await _sqlite_fetchall("""
            SELECT
                date,
                SUM(total_sessions) as total_sessions,
                SUM(successful_sessions) as successful_sessions,
                SUM(failed_sessions) as failed_sessions,
                SUM(total_earnings) as total_earnings
            FROM session_analytics
            WHERE date >= date('now', ?)
            GROUP BY date
            ORDER BY date DESC
        """, (f"-{days} days",))
    except Exception as e:
        logger.error(f"Error getting daily stats: {e}")
    return []


async def get_country_stats() -> List[Dict[str, Any]]:
    if not _initialized:
        return []

    try:
        return await _sqlite_fetchall("""
            SELECT
                country_code,
                SUM(total_sessions) as total_sessions,
                SUM(successful_sessions) as successful_sessions,
                SUM(failed_sessions) as failed_sessions,
                SUM(total_earnings) as total_earnings,
                AVG(avg_spam_score) as avg_spam_score
            FROM session_analytics
            GROUP BY country_code
            ORDER BY total_earnings DESC
        """)
    except Exception as e:
        logger.error(f"Error getting country stats: {e}")
    return []


async def get_time_range_stats(days: int = 30) -> List[Dict[str, Any]]:
    if not _initialized:
        return []

    try:
        return await _sqlite_fetchall("""
            SELECT
                date,
                SUM(total_sessions) as total_sessions,
                SUM(successful_sessions) as successful_sessions,
                SUM(failed_sessions) as failed_sessions,
                SUM(total_earnings) as total_earnings,
                AVG(avg_spam_score) as avg_spam_score
            FROM session_analytics
            WHERE date >= date('now', ?)
            GROUP BY date
            ORDER BY date DESC
        """, (f"-{days} days",))
    except Exception as e:
        logger.error(f"Error getting time range stats: {e}")
    return []


# ============================================================
# Query Functions - Users
# ============================================================

async def upsert_user(user_id: int, data: Dict[str, Any]) -> bool:
    if not _initialized:
        return False

    try:
        await _sqlite_execute("""
            INSERT INTO users (user_id, balance, sessions, banned, joined, success_count, channel_joined, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                balance = excluded.balance,
                sessions = excluded.sessions,
                banned = excluded.banned,
                success_count = excluded.success_count,
                channel_joined = excluded.channel_joined,
                data = excluded.data,
                updated_at = datetime('now')
        """, (
            user_id,
            data.get("balance", 0),
            data.get("sessions", 0),
            1 if data.get("banned", False) else 0,
            data.get("joined", datetime.now().isoformat()),
            data.get("success_count", 0),
            1 if data.get("channel_joined", False) else 0,
            json.dumps(data)
        ))
        return True
    except Exception as e:
        logger.error(f"Error upserting user: {e}")
    return False


async def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    if not _initialized:
        return None

    try:
        return await _sqlite_fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
    except Exception as e:
        logger.error(f"Error getting user: {e}")
    return None


async def batch_upsert_users(users: List[Dict[str, Any]]) -> int:
    if not _initialized or not users:
        return 0

    upserted = 0
    try:
        for user in users:
            try:
                await _sqlite_execute("""
                    INSERT INTO users (user_id, balance, sessions, banned, success_count, channel_joined, data)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (user_id) DO UPDATE SET
                        balance = excluded.balance,
                        sessions = excluded.sessions,
                        banned = excluded.banned,
                        success_count = excluded.success_count,
                        channel_joined = excluded.channel_joined,
                        data = excluded.data,
                        updated_at = datetime('now')
                """, (
                    user.get("user_id"),
                    user.get("balance", 0),
                    user.get("sessions", 0),
                    1 if user.get("banned", False) else 0,
                    user.get("success_count", 0),
                    1 if user.get("channel_joined", False) else 0,
                    json.dumps(user)
                ))
                upserted += 1
            except Exception as e:
                logger.debug(f"Error upserting user {user.get('user_id')}: {e}")
    except Exception as e:
        logger.error(f"Error in batch upsert: {e}")
    return upserted


# ============================================================
# Query Functions - Sessions
# ============================================================

async def upsert_session(session_id: str, data: Dict[str, Any]) -> bool:
    if not _initialized:
        return False

    try:
        await _sqlite_execute("""
            INSERT INTO sessions (session_id, user_id, phone, country_code, spam_status, session_string, data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (session_id) DO UPDATE SET
                spam_status = excluded.spam_status,
                session_string = excluded.session_string,
                data = excluded.data
        """, (
            session_id,
            data.get("user_id"),
            data.get("phone", ""),
            data.get("country_code", ""),
            data.get("spam_status", ""),
            # At-rest protection (parity with the sync layer's pending_sessions).
            _encrypt_session_string(data.get("session_string", "")),
            json.dumps(data)
        ))
        return True
    except Exception as e:
        logger.error(f"Error upserting session: {e}")
    return False


async def upsert_pending_session(session_id: str, data: Dict[str, Any]) -> bool:
    if not _initialized:
        return False

    try:
        await _sqlite_execute("""
            INSERT INTO pending_sessions (session_id, user_id, phone, country_code, status, data)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (session_id) DO UPDATE SET
                status = excluded.status,
                data = excluded.data,
                updated_at = datetime('now')
        """, (
            session_id,
            data.get("user_id"),
            data.get("phone", ""),
            data.get("country_code", ""),
            data.get("status", "pending"),
            json.dumps(data)
        ))
        return True
    except Exception as e:
        logger.error(f"Error upserting pending session: {e}")
    return False


async def get_pending_session_by_id(session_id: str) -> Optional[Dict[str, Any]]:
    if not _initialized:
        return None

    try:
        return await _sqlite_fetchone("SELECT * FROM pending_sessions WHERE session_id = ?", (session_id,))
    except Exception as e:
        logger.error(f"Error getting pending session: {e}")
    return None


async def delete_pending_session(session_id: str) -> bool:
    if not _initialized:
        return False

    try:
        await _sqlite_execute("DELETE FROM pending_sessions WHERE session_id = ?", (session_id,))
        return True
    except Exception as e:
        logger.error(f"Error deleting pending session: {e}")
    return False


async def cleanup_old_pending_sessions(hours: int = 24) -> int:
    if not _initialized:
        return 0

    try:
        cursor = await _sqlite_execute("""
            DELETE FROM pending_sessions
            WHERE created_at < datetime('now', ?)
        """, (f"-{hours} hours",))
        count = cursor.rowcount if cursor else 0
        if count > 0:
            logger.info(f"Cleaned up {count} old pending sessions (SQLite)")
        return count
    except Exception as e:
        logger.error(f"Error cleaning up pending sessions: {e}")
    return 0


async def cleanup_old_records(days: int = 90) -> Dict[str, int]:
    """Delete historical/analytics rows older than ``days`` from async tables.

    Applies retention to the append-only log/analytics tables that are never
    otherwise pruned. ``sessions`` is only cleaned when explicitly requested
    because it references session strings still used by recovery.

    Each table uses its own timestamp column:
      - transaction_logs / admin_logs / failed_verifications /
        withdrawal_logs use ``created_at``
      - session_analytics uses ``updated_at``
      - bot_metrics uses ``recorded_at``

    Returns a dict of {table: rows_deleted}.
    """
    if not _initialized:
        return {}

    retention_tables = (
        ("transaction_logs", "created_at"),
        ("admin_logs", "created_at"),
        ("failed_verifications", "created_at"),
        ("session_analytics", "updated_at"),
        ("bot_metrics", "recorded_at"),
        ("withdrawal_logs", "created_at"),
    )
    deleted: Dict[str, int] = {}
    for table, ts_col in retention_tables:
        try:
            cursor = await _sqlite_execute(
                f"DELETE FROM {table} WHERE {ts_col} < datetime('now', ?)",
                (f"-{days} days",),
            )
            count = cursor.rowcount if cursor else 0
            if count:
                deleted[table] = count
                logger.info(f"Retention cleanup: deleted {count} rows from {table} (older than {days} days)")
        except Exception as e:
            logger.error(f"Retention cleanup failed for {table}: {e}")
    return deleted


# ============================================================
# Query Functions - Search & Details
# ============================================================

async def search_users_by_phone(phone_pattern: str, limit: int = 50) -> List[Dict[str, Any]]:
    if not _initialized:
        return []

    try:
        return await _sqlite_fetchall("""
            SELECT DISTINCT
                s.user_id,
                s.phone,
                s.country_code,
                s.session_id,
                'verified' as source
            FROM sessions s
            WHERE s.phone LIKE ?
            UNION
            SELECT DISTINCT
                ps.user_id,
                ps.phone,
                ps.country_code,
                ps.session_id,
                'pending' as source
            FROM pending_sessions ps
            WHERE ps.phone LIKE ?
            LIMIT ?
        """, (f"%{phone_pattern}%", f"%{phone_pattern}%", limit))
    except Exception as e:
        logger.error(f"Error searching users by phone: {e}")
    return []


async def get_session_detail(session_id: str) -> Optional[Dict[str, Any]]:
    if not _initialized:
        return None

    try:
        row = await _sqlite_fetchone("""
            SELECT s.*,
                (SELECT COUNT(*) FROM failed_verifications fv WHERE fv.phone_number = s.phone) as error_count
            FROM sessions s
            WHERE s.session_id = ?
        """, (session_id,))

        if row:
            # The async layer encrypts session_string at rest; decrypt for the
            # caller so admin detail views show the working value.
            if row.get("session_string"):
                row["session_string"] = _decrypt_session_string(row["session_string"])
            errors = await _sqlite_fetchall("""
                SELECT * FROM failed_verifications
                WHERE phone_number = ?
                ORDER BY created_at DESC LIMIT 10
            """, (row.get("phone", ""),))
            row["errors"] = errors or []
            txns = await _sqlite_fetchall("""
                SELECT * FROM transaction_logs
                WHERE session_id = ?
                ORDER BY created_at DESC LIMIT 5
            """, (session_id,))
            row["transactions"] = txns or []
            return row

        pending_row = await _sqlite_fetchone("""
            SELECT ps.*,
                (SELECT COUNT(*) FROM failed_verifications fv WHERE fv.phone_number = ps.phone) as error_count
            FROM pending_sessions ps
            WHERE ps.session_id = ?
        """, (session_id,))

        if pending_row:
            pending_row["session_type"] = "pending"
            errors = await _sqlite_fetchall("""
                SELECT * FROM failed_verifications
                WHERE phone_number = ?
                ORDER BY created_at DESC LIMIT 10
            """, (pending_row.get("phone", ""),))
            pending_row["errors"] = errors or []
            return pending_row

    except Exception as e:
        logger.error(f"Error getting session detail: {e}")
    return None


# ============================================================
# Metrics & Health
# ============================================================

async def log_bot_metric(metric_name: str, metric_value: float, metric_type: str = "gauge") -> bool:
    return await record_metric(metric_name, metric_value, metric_type)


async def get_latest_metrics(limit: int = 30) -> List[Dict[str, Any]]:
    if not _initialized:
        return []

    try:
        return await _sqlite_fetchall("""
            SELECT metric_name, metric_value, metric_type, recorded_at
            FROM bot_metrics
            ORDER BY recorded_at DESC
            LIMIT ?
        """, (limit,))
    except Exception as e:
        logger.error(f"Error getting metrics: {e}")
    return []


async def get_hourly_transaction_stats(hours: int = 24) -> List[Dict[str, Any]]:
    if not _initialized:
        return []

    try:
        return await _sqlite_fetchall("""
            SELECT
                strftime('%Y-%m-%d %H:00:00', created_at) as hour,
                COUNT(*) as total_transactions,
                SUM(CASE WHEN action = 'session_verified' THEN 1 ELSE 0 END) as verified_sessions,
                COALESCE(SUM(amount), 0) as total_amount
            FROM transaction_logs
            WHERE created_at >= datetime('now', ?)
            GROUP BY strftime('%Y-%m-%d %H', created_at)
            ORDER BY hour DESC
        """, (f"-{hours} hours",))
    except Exception as e:
        logger.error(f"Error getting hourly stats: {e}")
    return []


async def get_user_sessions_count(user_id: int) -> int:
    if not _initialized:
        return 0

    try:
        v_row = await _sqlite_fetchone("SELECT COUNT(*) as cnt FROM sessions WHERE user_id = ?", (user_id,))
        p_row = await _sqlite_fetchone("SELECT COUNT(*) as cnt FROM pending_sessions WHERE user_id = ?", (user_id,))
        v = v_row["cnt"] if v_row else 0
        p = p_row["cnt"] if p_row else 0
        return v + p
    except Exception as e:
        logger.error(f"Error getting user sessions count: {e}")
    return 0


async def get_db_health_stats() -> Dict[str, Any]:
    result = {
        "connected": _initialized,
        "backend": _BACKEND or "none",
        "tables": {},
        "total_size": "N/A",
        "journal_mode": "N/A",
        "foreign_keys": "N/A",
        "integrity": "N/A",
        "index_count": 0,
        "wal_size": "N/A",
    }

    if not _initialized:
        return result

    def _fmt_size(num_bytes: int) -> str:
        if num_bytes is None or num_bytes < 0:
            return "N/A"
        if num_bytes > 1024 * 1024:
            return f"{num_bytes / (1024*1024):.1f} MB"
        if num_bytes > 1024:
            return f"{num_bytes / 1024:.1f} KB"
        return f"{num_bytes} bytes"

    try:
        import os as _os

        file_size = _os.path.getsize(DB_PATH) if _os.path.exists(DB_PATH) else 0
        result["total_size"] = _fmt_size(file_size)

        wal_path = DB_PATH + "-wal"
        shm_path = DB_PATH + "-shm"
        result["wal_size"] = _fmt_size(_os.path.getsize(wal_path) if _os.path.exists(wal_path) else 0)
        result["shm_size"] = _fmt_size(_os.path.getsize(shm_path) if _os.path.exists(shm_path) else 0)

        jm = await _sqlite_fetchone("PRAGMA journal_mode")
        result["journal_mode"] = str(jm["journal_mode"]) if jm else "N/A"

        fk = await _sqlite_fetchone("PRAGMA foreign_keys")
        result["foreign_keys"] = "ON" if fk and fk.get("foreign_keys") in (1, "1", True) else "OFF"

        try:
            qc = await _sqlite_fetchone("PRAGMA quick_check")
            result["integrity"] = str(qc.get("quick_check", "N/A")) if qc else "N/A"
        except Exception:
            result["integrity"] = "N/A"

        idx = await _sqlite_fetchone(
            "SELECT COUNT(*) as cnt FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        )
        result["index_count"] = idx["cnt"] if idx else 0

        table_rows = await _sqlite_fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        for trow in table_rows or []:
            table = trow["name"]
            row = await _sqlite_fetchone(f'SELECT COUNT(*) as cnt FROM "{table}"')
            result["tables"][table] = {
                "total_size": "N/A",
                "data_size": "N/A",
                "estimated_rows": row["cnt"] if row else 0,
            }
        result["db_file"] = DB_PATH
    except Exception as e:
        logger.error(f"DB health check error: {e}")
        result["error"] = str(e)

    return result


# ============================================================
# Module Load Info
# ============================================================

print(f"Database Module Loaded")
print(f"   Backend: SQLite only")
print(f"   SQLite DB path: {DB_PATH}")
