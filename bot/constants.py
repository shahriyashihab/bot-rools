"""
Bot Constants
==============

Central location for all magic numbers and configuration constants.
"""

DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 3600
MIN_TIMEOUT_SECONDS = 60

# Per-country account capacity: 0 = unlimited (no explicit account cap).
DEFAULT_COUNTRY_CAPACITY = 0

MAX_RATE_USD = 10000.0
MIN_RATE_USD = 0.0

DEFAULT_VERIFICATION_DELAY = 5
MAX_VERIFICATION_RETRIES = 3

TWOFA_TARGET_SECONDS = 120.0
SPAM_CHECK_TARGET_SECONDS = 180.0
DEVICE_CHECK_OFFSET_SECONDS = 1.0

SESSION_MONITOR_INTERVAL = 60
SESSION_RECOVERY_WINDOW_SECONDS = 300

MAX_CONSECUTIVE_FAILS = 5
COOLDOWN_MULTIPLIER_MINUTES = 2
MAX_COOLDOWN_MINUTES = 15

MIN_VERIFICATION_INTERVAL = 30
MAX_VERIFICATIONS_PER_COUNTRY_DAY = 200

EXPONENTIAL_BACKOFF_MAX_MULTIPLIER = 4.0
EXPONENTIAL_BACKOFF_INCREMENT = 0.5
MAX_FLOOD_WAIT_SECONDS = 300

SPAMBOT_RESPONSE_WAIT = 2.5
SPAMBOT_HISTORY_LIMIT = 3

DIALOG_CHECK_LIMIT = 5
MIN_DIALOGS_FOR_SCORE_BONUS = 3

SPAM_SCORE_CLEAN_BONUS = -30
SPAM_SCORE_SPAM_PENALTY = 70
SPAM_SCORE_LIMIT_PENALTY = 50
SPAM_SCORE_PEER_FLOOD_PENALTY = 80
SPAM_SCORE_RESTRICTED_PENALTY = 40
SPAM_SCORE_SCAM_PENALTY = 60
SPAM_SCORE_FAKE_PENALTY = 50
SPAM_SCORE_PREMIUM_BONUS = -20

SPAM_THRESHOLD_SPAM = 70
SPAM_THRESHOLD_LIMIT = 30

USD_TO_TRX_DEFAULT = 57.0
USD_TO_CARD_NAME_DEFAULT = 108.0
USD_TO_USDT_BEP20_DEFAULT = 1.0

APP_VERSION = "Telegram Android 10.14.5"
APP_VERSION_LEGACY = "Telegram Android 0.28.1.1771-arm64-v8a"

MESSAGE_TYPE_VERIFIED = "VERIFIED"
MESSAGE_TYPE_REJECTED = "REJECTED"
MESSAGE_TYPE_MULTI_DEVICE = "MULTI-DEVICE"
MESSAGE_TYPE_SPAM = "SPAM"
MESSAGE_TYPE_EXPIRED = "EXPIRED"

SESSION_STATUS_PENDING = "pending"
SESSION_STATUS_VERIFIED = "verified"
SESSION_STATUS_REJECTED = "rejected"
SESSION_STATUS_EXPIRED = "expired"

ACCOUNT_STATUS_SPAM_FREE = "spam_free"
ACCOUNT_STATUS_SPAM = "spam"
ACCOUNT_STATUS_LIMIT = "limit"

WITHDRAW_STATUS_PENDING = "pending"
WITHDRAW_STATUS_COMPLETED = "completed"
WITHDRAW_STATUS_REJECTED = "rejected"

CACHE_TTL_SECONDS = 300
CACHE_VERSION = "2.1"

SESSION_CLEANUP_INTERVAL_HOURS = 1
SESSION_RETENTION_DAYS = 30
SESSION_FILE_MAX_AGE_DAYS = 7

DATA_RETENTION_DAYS = 30

BROADCAST_DELAY_SECONDS = 0.3
BROADCAST_FLOOD_WAIT_DEFAULT = 30
BROADCAST_MAX_FAILURES = 10

MESSAGE_MAX_LENGTH = 4096
MESSAGE_CHUNK_SIZE = 4000

CHANNEL_CHECK_COOLDOWN_SECONDS = 15
MEMBERSHIP_CACHE_TTL_SECONDS = 60

COUNTRY_CODE_FLAGS = {
    "+880": "🇧🇩", "+1": "🇺🇸", "+44": "🇬🇧", "+91": "🇮🇳", "+86": "🇨🇳",
    "+81": "🇯🇵", "+82": "🇰🇷", "+33": "🇫🇷", "+49": "🇩🇪", "+39": "🇮🇹",
    "+34": "🇪🇸", "+61": "🇦🇺", "+55": "🇧🇷", "+51": "🇵🇪", "+52": "🇲🇽",
    "+234": "🇳🇬", "+20": "🇪🇬", "+27": "🇿🇦", "+60": "🇲🇾", "+65": "🇸🇬",
    "+66": "🇹🇭", "+84": "🇻🇳", "+63": "🇵🇭", "+31": "🇳🇱", "+48": "🇵🇱",
    "+351": "🇵🇹", "+972": "🇮🇱", "+420": "🇨🇿", "+45": "🇩🇰", "+593": "🇪🇨",
    "+507": "🇵🇦", "+598": "🇺🇾", "+244": "🇦🇴", "+1242": "🇧🇸", "+1246": "🇧🇧",
    "+1441": "🇧🇲", "+1345": "🇰🇾", "+1868": "🇹🇹", "+1876": "🇯🇲", "+1809": "🇩🇴",
    "+1829": "🇩🇴", "+1849": "🇩🇴", "+1787": "🇵🇷"
}


def get_country_flag(country_code: str) -> str:
    """Get flag emoji for a country code."""
    return COUNTRY_CODE_FLAGS.get(country_code, "🌍")


PYROGRAM_RAW_AVAILABLE = False
PYROGRAM_VERSION = ""
try:
    import pyrogram
    PYROGRAM_VERSION = getattr(pyrogram, "__version__", "unknown")
    # Probe the actual import instead of guessing from the version number.
    # Pyrogram 2.0.x and pyrofork 2.1.x+ both ship the raw GetAuthorizations
    # function; a version-heuristic here (major>=2 and minor>=1) wrongly
    # disabled multi-device detection on otherwise-capable installs, which
    # made every live device verdict return UNKNOWN and wedged sessions
    # in pending forever when the admin enabled the device check.
    # Probe BOTH raw functions the multi-device flow needs. GetAuthorizations
    # alone only proves the account can be READ; the 24h cleanup guarantee also
    # needs ResetAuthorization to terminate the extra devices. A build that
    # shipped one but not the other reported "raw API available" and then raised
    # ImportError deep inside run_auto_logout.
    try:
        from pyrogram.raw.functions.account.get_authorizations import GetAuthorizations  # noqa: F401
        from pyrogram.raw.functions.account.reset_authorization import ResetAuthorization  # noqa: F401
        PYROGRAM_RAW_AVAILABLE = True
    except Exception:
        PYROGRAM_RAW_AVAILABLE = False
except Exception:
    pass


def safe_import_raw(raw_module_path: str):
    try:
        return __import__(raw_module_path, fromlist=["_"])
    except (ImportError, AttributeError) as e:
        print(f"RAW API UNAVAILABLE: {raw_module_path} - {e}. Pyrogram v2.3+ required, current: {PYROGRAM_VERSION}")
        return None


def is_raw_api_available() -> bool:
    return PYROGRAM_RAW_AVAILABLE
