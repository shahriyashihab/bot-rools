import os
import logging

_log = logging.getLogger("Config")

# ==============================
#   .env FILE LOADER
#   Works on VPS, Replit, Anywhere
# ==============================

_ENV_LOADED = False

def _load_dotenv():
    """Load environment variables from .env file (if exists). Works everywhere.

    Priority matches the documented contract: an already-set environment
    variable (e.g. Replit Secret, shell export, start.sh) WINS over the .env
    file; .env only fills in values that are not set elsewhere.
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    possible_paths = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        ".env",
    ]

    for dotenv_path in possible_paths:
        if os.path.isfile(dotenv_path):
            try:
                # utf-8-sig strips a UTF-8 BOM (safe for files edited on Windows)
                with open(dotenv_path, "r", encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            key, _, value = line.partition("=")
                            key = key.strip()
                            value = value.strip()
                            if value.startswith('"') and value.endswith('"'):
                                value = value[1:-1]
                            elif value.startswith("'") and value.endswith("'"):
                                value = value[1:-1]
                            if key and key not in os.environ:
                                os.environ[key] = value
                print(f"[OK] .env loaded from: {dotenv_path}")
                _ENV_LOADED = True
                return
            except Exception as e:
                _log.warning(f"Could not load .env from {dotenv_path}: {e}")

    _ENV_LOADED = True

_load_dotenv()

# ==============================
#   CREDENTIAL LOADER
#   Priority: Env Var > .env > Replit Secret > EXTRA_API fallback
# ==============================

def load_secret(key, default=None):
    """Load secret from env, .env file, or Replit secrets."""
    value = os.getenv(key)
    if value:
        return value

    try:
        with open(f'/home/runner/{key}', 'r') as f:
            return f.read().strip()
    except (FileNotFoundError, IOError, PermissionError):
        pass
    return default

# ==============================
#        TELEGRAM API
# ==============================

_bot_token = load_secret("BOT_TOKEN")
_api_id = load_secret("API_ID")
_api_hash = load_secret("API_HASH")

if not _api_id or not _api_hash:
    _extra_api_id = os.getenv("EXTRA_API_1_ID")
    _extra_api_hash = os.getenv("EXTRA_API_1_HASH")
    if _extra_api_id and _extra_api_hash:
        print("ℹ️  Using EXTRA_API_1 credentials as primary API")
        _api_id = _extra_api_id
        _api_hash = _extra_api_hash

if not _api_id:
    raise ValueError("API_ID is required! Set API_ID in .env file or environment variables.")
if not _api_hash:
    raise ValueError("API_HASH is required! Set API_HASH in .env file or environment variables.")
if not _bot_token:
    raise ValueError("BOT_TOKEN is required! Set BOT_TOKEN in .env file or environment variables.")

API_ID = int(_api_id)
API_HASH = _api_hash
BOT_TOKEN = _bot_token

# ==============================
#          CHANNELS
# ==============================

def parse_channel_value(value, default):
    if not value:
        return default
    value = value.strip()
    if value.startswith("@"):
        return value
    if value.lstrip('-').isdigit():
        return int(value)
    return default

_channel_submit_env = load_secret("CHANNEL_SUBMIT") or ""
_channel_verified_env = load_secret("CHANNEL_VERIFIED") or ""
_channel_rejected_env = load_secret("CHANNEL_REJECTED") or ""
_channel_withdraw_env = load_secret("CHANNEL_WITHDRAW_REPORT") or ""
_channel_24hr_env = load_secret("CHANNEL_24HR_UP") or ""
_channel_success_pyrogram_env = load_secret("CHANNEL_SUCCESS_PYROGRAM") or ""
_channel_success_tdata_env = load_secret("CHANNEL_SUCCESS_TDATA") or ""
_channel_success_txt_env = load_secret("CHANNEL_SUCCESS_TXT") or ""
_required_channel_env = load_secret("REQUIRED_CHANNEL") or ""

_CHANNEL_DEFAULTS = {
    "CHANNEL_SUBMIT": "",
    "CHANNEL_VERIFIED": "",
    "CHANNEL_REJECTED": "",
    "CHANNEL_WITHDRAW_REPORT": "",
    "CHANNEL_24HR_UP": "",
    "CHANNEL_SUCCESS_PYROGRAM": "",
    "CHANNEL_SUCCESS_TDATA": "",
    "CHANNEL_SUCCESS_TXT": "",
    "REQUIRED_CHANNEL": ""
}

def _get_channel_with_fallback(name, value):
    if value:
        return value
    default = _CHANNEL_DEFAULTS.get(name, "")
    if default:
        return default
    _log.warning(f"{name} is not configured! Set it in .env or environment variables.")
    return ""

CHANNEL_SUBMIT = parse_channel_value(_get_channel_with_fallback("CHANNEL_SUBMIT", _channel_submit_env), None)
CHANNEL_VERIFIED = parse_channel_value(_get_channel_with_fallback("CHANNEL_VERIFIED", _channel_verified_env), None)
CHANNEL_REJECTED = parse_channel_value(_get_channel_with_fallback("CHANNEL_REJECTED", _channel_rejected_env), None)
CHANNEL_WITHDRAW_REPORT = parse_channel_value(_get_channel_with_fallback("CHANNEL_WITHDRAW_REPORT", _channel_withdraw_env), None)
CHANNEL_24HR_UP = parse_channel_value(_get_channel_with_fallback("CHANNEL_24HR_UP", _channel_24hr_env), None)
CHANNEL_SUCCESS_PYROGRAM = parse_channel_value(_get_channel_with_fallback("CHANNEL_SUCCESS_PYROGRAM", _channel_success_pyrogram_env), None)
CHANNEL_SUCCESS_TDATA = parse_channel_value(_get_channel_with_fallback("CHANNEL_SUCCESS_TDATA", _channel_success_tdata_env), None)
CHANNEL_SUCCESS_TXT = parse_channel_value(_get_channel_with_fallback("CHANNEL_SUCCESS_TXT", _channel_success_txt_env), None)
REQUIRED_CHANNEL = _get_channel_with_fallback("REQUIRED_CHANNEL", _required_channel_env)

# All channels that can be overridden at runtime from the admin panel.
# Admin-panel overrides (stored in _storage["channel_overrides"]) always win
# over these .env constants so the admin can change a channel without editing
# .env or restarting the bot.
CHANNEL_KEYS = (
    "CHANNEL_SUBMIT",
    "CHANNEL_VERIFIED",
    "CHANNEL_REJECTED",
    "CHANNEL_WITHDRAW_REPORT",
    "CHANNEL_24HR_UP",
    "CHANNEL_SUCCESS_PYROGRAM",
    "CHANNEL_SUCCESS_TDATA",
    "CHANNEL_SUCCESS_TXT",
    "REQUIRED_CHANNEL",
)

CHANNEL_LABELS = {
    "CHANNEL_SUBMIT": "📥 Submit (Pending) channel",
    "CHANNEL_VERIFIED": "✅ Verified (Success) channel",
    "CHANNEL_REJECTED": "❌ Rejected channel",
    "CHANNEL_WITHDRAW_REPORT": "💸 Withdraw report channel",
    "CHANNEL_24HR_UP": "⏰ 24h / Multi-device channel",
    "CHANNEL_SUCCESS_PYROGRAM": "📗 Success Pyrogram channel",
    "CHANNEL_SUCCESS_TDATA": "📙 Success TData channel",
    "CHANNEL_SUCCESS_TXT": "📕 Success TXT channel",
    "REQUIRED_CHANNEL": "🔐 Required (join) channel",
}


def get_channel(name: str):
    """Get the effective value for a channel.

    The admin panel override (if any) always wins; otherwise the .env config
    constant is returned. ``None``/empty means "not configured".
    """
    if name in CHANNEL_KEYS:
        try:
            from bot.utils.storage import _storage
            overrides = _storage.get("channel_overrides") or {}
            val = overrides.get(name)
            if val:
                return val
        except Exception:
            pass
    return globals().get(name)

# ==============================
#            ADMIN
# ==============================

_admin_id_env = load_secret("ADMIN_ID") or ""
if not _admin_id_env or not _admin_id_env.lstrip('-').isdigit():
    _log.error("CRITICAL: ADMIN_ID not set! Set ADMIN_ID in .env or environment variables.")
    _admin_id_env = "0"
ADMIN_ID = int(_admin_id_env)

# ==============================
#           SECURITY / 2FA
# ==============================

_2fa_password = None
try:
    import json as _json
    import sqlite3 as _sqlite3
    _db_path = os.environ.get("SQLITE_DB_PATH", "data/bot_data.db")
    _conn = _sqlite3.connect(_db_path)
    try:
        _row = _conn.execute(
            "SELECT value FROM settings WHERE key = '_2fa_password_persistent'"
        ).fetchone()
        if _row and _row[0]:
            _parsed = _json.loads(_row[0])
            if isinstance(_parsed, str):
                _2fa_password = _parsed
            elif isinstance(_parsed, dict):
                _2fa_password = _parsed.get("value")
    finally:
        _conn.close()
except Exception:
    pass

if not _2fa_password:
    _2fa_password = os.getenv("SESSION_2FA_PASSWORD")

if not _2fa_password:
    import secrets
    _generated_password = secrets.token_urlsafe(16)
    _log.warning("SESSION_2FA_PASSWORD not set! Generated password will be persisted.")
    _2fa_password = _generated_password

    # P6-59: the generated password MUST be persisted NOW, or the next restart
    # generates a DIFFERENT one and every account whose 2FA was set with the
    # previous password becomes permanently unopenable. Written straight to
    # the settings table (config.py cannot import the storage layer).
    try:
        _persist_conn = _sqlite3.connect(_db_path)
        try:
            _persist_conn.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                " key TEXT PRIMARY KEY,"
                " value TEXT,"
                " updated_at TEXT DEFAULT (datetime('now')))"
            )
            _persist_conn.execute(
                "INSERT INTO settings (key, value, updated_at) "
                "VALUES ('_2fa_password_persistent', ?, datetime('now')) "
                "ON CONFLICT (key) DO UPDATE SET "
                " value = excluded.value, updated_at = datetime('now')",
                (_json.dumps(_generated_password),),
            )
            _persist_conn.commit()
        finally:
            _persist_conn.close()
        _log.info("Generated 2FA password PERSISTED to storage - it now survives restarts")
    except Exception as _persist_err:
        _log.error(
            f"CRITICAL: could not persist the generated 2FA password ({_persist_err}) - "
            f"set SESSION_2FA_PASSWORD explicitly in .env, otherwise accounts verified "
            f"before the next restart will be unopenable!"
        )

SESSION_2FA_PASSWORD = _2fa_password


def get_2fa_password() -> str:
    """
    Get the current 2FA password.
    
    Checks live storage first (allows runtime updates), falls back to
    the value frozen at import time.
    """
    global SESSION_2FA_PASSWORD
    try:
        from bot.utils.storage import _storage
        live_password = _storage.get("_2fa_password_persistent")
        if live_password:
            return live_password
    except Exception:
        pass
    return SESSION_2FA_PASSWORD
VERIFICATION_DELAY = int(os.getenv("VERIFICATION_DELAY", "5"))

# ==============================
#        SUPABASE (Real-time PostgreSQL)
# ==============================

SUPABASE_URL = load_secret("SUPABASE_URL") or ""
SUPABASE_KEY = load_secret("SUPABASE_KEY") or ""
SUPABASE_DB_URL = load_secret("SUPABASE_DB_URL") or ""

# ==============================
#        CURRENCY RATES
# ==============================

USD_TO_TRX = float(os.getenv("USD_TO_TRX", "57.0"))
USD_TO_CARD_NAME = float(os.getenv("USD_TO_CARD_NAME", "108.0"))
USD_TO_USDT_BEP20 = float(os.getenv("USD_TO_USDT_BEP20", "1.0"))

# ==============================
#     SESSION ENCRYPTION KEY
#     (at-rest protection for session strings stored in the DB)
# ==============================

SESSION_SECRET = load_secret("SESSION_SECRET") or ""
# STORAGE_ENCRYPTION_KEY is the legacy alias; SESSION_SECRET wins if both set.
if not SESSION_SECRET:
    SESSION_SECRET = os.getenv("STORAGE_ENCRYPTION_KEY") or ""

# ==============================
#     EXTRA API CREDENTIALS
#     (For API Rotation - supports as many APIs as you add)
# ==============================

def load_extra_apis():
    apis = []
    # Scans EXTRA_API_1..EXTRA_API_200 (gaps allowed). Adding EXTRA_API_6,
    # EXTRA_API_7, ... EXTRA_API_200 to .env automatically extends the rotation
    # pool with no code change - each new API shares the login + check load.
    # APIs can also be added at runtime from the admin panel (add_api_runtime);
    # both sources are merged (deduplicated) by the API rotation manager.
    for i in range(1, 201):
        api_id = os.getenv(f"EXTRA_API_{i}_ID")
        api_hash = os.getenv(f"EXTRA_API_{i}_HASH")
        if api_id and api_hash:
            try:
                apis.append((int(api_id), api_hash))
            except ValueError:
                pass
    return apis

EXTRA_APIS = load_extra_apis()

# ==============================
#     ANTI-FREEZE SETTINGS
# ==============================

ANTI_FREEZE_CONFIG = {
    "enabled": True,
    "2fa_setup_delay_min": 20,
    "2fa_setup_delay_max": 35,
    "session_warmup_enabled": True,
    "session_warmup_delay": 15,
    "verify_human_delay_min": 3.0,
    "verify_human_delay_max": 8.0,
    "otp_typing_delay_per_digit": 0.4,
    "between_action_delay_min": 2.0,
    "between_action_delay_max": 5.0,
    "max_sessions_per_ip_hour": 3,
    "max_sessions_per_api_hour": 25,
    "cooldown_after_fail_seconds": 60,
    "cooldown_after_flood_seconds": 300,
}

# ==============================
#   CONFIG SUMMARY (Startup)
# ==============================

print("=" * 50)
print("  BOT CONFIGURATION LOADED")
print("=" * 50)
print(f"  API_ID       : {'SET' if API_ID else 'MISSING'}")
print(f"  API_HASH     : {'SET' if API_HASH else 'MISSING'}")
print(f"  BOT_TOKEN    : {'SET' if BOT_TOKEN else 'MISSING'}")
print(f"  ADMIN_ID     : {ADMIN_ID}")
print(f"  EXTRA_APIs   : {len(EXTRA_APIS)} loaded")
print(f"  CHANNEL_SUBMIT    : {'SET' if CHANNEL_SUBMIT else 'MISSING'}")
print(f"  CHANNEL_VERIFIED  : {'SET' if CHANNEL_VERIFIED else 'MISSING'}")
print(f"  CHANNEL_REJECTED  : {'SET' if CHANNEL_REJECTED else 'MISSING'}")
print(f"  CHANNEL_WITHDRAW  : {'SET' if CHANNEL_WITHDRAW_REPORT else 'MISSING'}")
print(f"  CHANNEL_24HR_UP   : {'SET' if CHANNEL_24HR_UP else 'MISSING'}")
print(f"  REQUIRED_CHANNEL  : {'SET' if REQUIRED_CHANNEL else 'MISSING'}")
print(f"  2FA Password      : {'SET' if SESSION_2FA_PASSWORD else 'GENERATED'}")
print(f"  SESSION_SECRET    : {'SET' if SESSION_SECRET else 'MISSING (no at-rest encryption)'}")
print("=" * 50)
