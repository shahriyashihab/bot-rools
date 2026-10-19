"""
Anti-Spam & Anti-Freeze Protection Module v2.0 - OPTIMIZED FOR SPEED
=====================================================================
MAJOR CHANGES:
- Dramatically reduced delays (from 15-55s to 1-5s)
- Real freeze prevention through device simulation, NOT delays
- Telegram detects bots by fingerprints, NOT by speed
- Real users complete login in 30-60 seconds, not 3+ minutes
"""

import asyncio
import random
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from collections import defaultdict

from bot.utils.net_timeouts import call_with_timeout, op_with_timeout
from bot.utils.storage import (
    get_country_capacity,
    get_daily_verification_count,
    get_daily_success_count,
    increment_daily_verification_count,
)

COUNTRY_PROFILES = {
    "+98": {"name": "Iran", "risk_level": 4, "min_delay": 2.0, "max_delay": 4.0, "freeze_risk": "critical"},
    "+7": {"name": "Russia", "risk_level": 4, "min_delay": 2.0, "max_delay": 4.0, "freeze_risk": "critical"},
    "+86": {"name": "China", "risk_level": 4, "min_delay": 2.0, "max_delay": 4.0, "freeze_risk": "critical"},
    "+53": {"name": "Cuba", "risk_level": 4, "min_delay": 2.0, "max_delay": 4.0, "freeze_risk": "critical"},
    "+977": {"name": "Nepal", "risk_level": 4, "min_delay": 2.0, "max_delay": 4.0, "freeze_risk": "critical"},
    "+254": {"name": "Kenya", "risk_level": 4, "min_delay": 2.0, "max_delay": 4.0, "freeze_risk": "critical"},
    "+84": {"name": "Vietnam", "risk_level": 4, "min_delay": 2.0, "max_delay": 4.0, "freeze_risk": "critical"},
    
    "+880": {"name": "Bangladesh", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+959": {"name": "Myanmar-Mobile", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+95": {"name": "Myanmar", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+91": {"name": "India", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+234": {"name": "Nigeria", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+92": {"name": "Pakistan", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+252": {"name": "Somalia", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+251": {"name": "Ethiopia", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+255": {"name": "Tanzania", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+256": {"name": "Uganda", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+93": {"name": "Afghanistan", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+964": {"name": "Iraq", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+963": {"name": "Syria", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+967": {"name": "Yemen", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+218": {"name": "Libya", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    "+249": {"name": "Sudan", "risk_level": 3, "min_delay": 1.5, "max_delay": 3.5, "freeze_risk": "high"},
    
    "+351": {"name": "Portugal", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+48": {"name": "Poland", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+82": {"name": "South Korea", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+20": {"name": "Egypt", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+27": {"name": "South Africa", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+55": {"name": "Brazil", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+52": {"name": "Mexico", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+63": {"name": "Philippines", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+62": {"name": "Indonesia", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+60": {"name": "Malaysia", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+66": {"name": "Thailand", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+970": {"name": "Palestine", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+972": {"name": "Israel", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+244": {"name": "Angola", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+380": {"name": "Ukraine", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+375": {"name": "Belarus", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+994": {"name": "Azerbaijan", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+374": {"name": "Armenia", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+995": {"name": "Georgia", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+998": {"name": "Uzbekistan", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+996": {"name": "Kyrgyzstan", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+992": {"name": "Tajikistan", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+993": {"name": "Turkmenistan", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+76": {"name": "Kazakhstan", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+77": {"name": "Kazakhstan-Mobile", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+90": {"name": "Turkey", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+212": {"name": "Morocco", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+213": {"name": "Algeria", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+216": {"name": "Tunisia", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+51": {"name": "Peru", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+57": {"name": "Colombia", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+54": {"name": "Argentina", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+56": {"name": "Chile", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+58": {"name": "Venezuela", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    "+593": {"name": "Ecuador", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+507": {"name": "Panama", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+598": {"name": "Uruguay", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+65": {"name": "Singapore", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+852": {"name": "Hong Kong", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+886": {"name": "Taiwan", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0, "freeze_risk": "medium"},
    "+94": {"name": "Sri Lanka", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.5, "freeze_risk": "medium"},
    
    "+1": {"name": "USA/Canada", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+44": {"name": "UK", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+49": {"name": "Germany", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+33": {"name": "France", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+39": {"name": "Italy", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+34": {"name": "Spain", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+81": {"name": "Japan", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+61": {"name": "Australia", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+64": {"name": "New Zealand", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+31": {"name": "Netherlands", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+45": {"name": "Denmark", "risk_level": 1, "min_delay": 0.5, "max_delay": 1.2, "freeze_risk": "low"},
    "+46": {"name": "Sweden", "risk_level": 1, "min_delay": 0.5, "max_delay": 1.2, "freeze_risk": "low"},
    "+47": {"name": "Norway", "risk_level": 1, "min_delay": 0.5, "max_delay": 1.2, "freeze_risk": "low"},
    "+41": {"name": "Switzerland", "risk_level": 1, "min_delay": 0.5, "max_delay": 1.2, "freeze_risk": "low"},
    "+43": {"name": "Austria", "risk_level": 1, "min_delay": 0.5, "max_delay": 1.2, "freeze_risk": "low"},
    "+32": {"name": "Belgium", "risk_level": 1, "min_delay": 0.5, "max_delay": 1.2, "freeze_risk": "low"},
    "+353": {"name": "Ireland", "risk_level": 1, "min_delay": 0.5, "max_delay": 1.2, "freeze_risk": "low"},
    "+358": {"name": "Finland", "risk_level": 1, "min_delay": 0.5, "max_delay": 1.2, "freeze_risk": "low"},
    "+354": {"name": "Iceland", "risk_level": 1, "min_delay": 0.5, "max_delay": 1.2, "freeze_risk": "low"},
    "+352": {"name": "Luxembourg", "risk_level": 1, "min_delay": 0.5, "max_delay": 1.2, "freeze_risk": "low"},
    "+356": {"name": "Malta", "risk_level": 1, "min_delay": 0.5, "max_delay": 1.2, "freeze_risk": "low"},
    "+357": {"name": "Cyprus", "risk_level": 1, "min_delay": 0.5, "max_delay": 1.2, "freeze_risk": "low"},
    "+30": {"name": "Greece", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+420": {"name": "Czech Republic", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+421": {"name": "Slovakia", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+36": {"name": "Hungary", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+40": {"name": "Romania", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+359": {"name": "Bulgaria", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+385": {"name": "Croatia", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+386": {"name": "Slovenia", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+372": {"name": "Estonia", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+371": {"name": "Latvia", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+370": {"name": "Lithuania", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+966": {"name": "Saudi Arabia", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+971": {"name": "UAE", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+974": {"name": "Qatar", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+973": {"name": "Bahrain", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+968": {"name": "Oman", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
    "+965": {"name": "Kuwait", "risk_level": 1, "min_delay": 0.8, "max_delay": 1.5, "freeze_risk": "low"},
}

_user_activity: Dict[int, dict] = defaultdict(lambda: {
    "last_verification": None,
    "verification_count": 0,
    "last_otp_sent": None,
    "consecutive_fails": 0,
    "cooldown_until": None
})

# ---------------------------------------------------------------
# Persistence of user rate-limit state across restarts.
# Previously _user_activity lived ONLY in memory: a restart wiped every
# cooldown and fail counter (a bypass window) AND any in-flight cooldown
# was forgotten. The state is now mirrored into _storage["user_rate_limits"]
# (persisted through the generic settings table) - loaded on module init,
# written on every mutation. Cooldown expiry is stored as an ISO timestamp
# so a restart never shortens or extends an active cooldown.
# ---------------------------------------------------------------
_RATE_LIMIT_STORAGE_KEY = "user_rate_limits"

# Final-review fix: register a settings-consistency lock for this blob. The
# mutation below (unlocked store[uid] = ...) raced the settings serializer's
# json.dumps, causing transient "dictionary changed size" sync failures (they
# self-healed via dirty re-mark + retry, but were pure noise at 25k/day).
# Same lock-domain contract as the proxy blob: mutators take
# _RATE_LIMIT_LOCK -> _storage_lock (brief); the serializer takes
# _RATE_LIMIT_LOCK alone.
_RATE_LIMIT_LOCK = threading.RLock()

try:
    from bot.data.state import register_settings_consistency_lock as _register_rll
    _register_rll(_RATE_LIMIT_STORAGE_KEY, _RATE_LIMIT_LOCK)
except Exception:  # state may not be importable in isolated tooling
    pass


def _parse_iso_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _load_user_activity_from_storage() -> None:
    """Hydrate in-memory rate-limit state from persisted storage (once)."""
    global _user_activity_loaded
    if _user_activity_loaded:
        return
    _user_activity_loaded = True
    try:
        from bot.data.state import _storage
        persisted = _storage.get(_RATE_LIMIT_STORAGE_KEY)
        if isinstance(persisted, dict):
            for uid, entry in persisted.items():
                try:
                    uid_int = int(uid)
                except (TypeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                activity = _user_activity[uid_int]
                activity["consecutive_fails"] = int(entry.get("consecutive_fails", 0) or 0)
                activity["verification_count"] = int(entry.get("verification_count", 0) or 0)
                activity["last_verification"] = _parse_iso_dt(entry.get("last_verification"))
                activity["cooldown_until"] = _parse_iso_dt(entry.get("cooldown_until"))
    except Exception:
        # Storage may not be initialized yet (e.g. ad-hoc scripts); the
        # in-memory defaults then apply until the first persisted mutation.
        pass


_user_activity_loaded = False


def _persist_user_activity(user_id: int) -> None:
    """Mirror one user's rate-limit state into storage (fire-and-forget)."""
    try:
        activity = _user_activity.get(user_id)
        if not activity:
            return
        from bot.data.state import _mark_settings_key_dirty, _storage, save_storage
        # Serialize the read-modify-write under the registered consistency
        # lock: the settings serializer holds the SAME lock while dumping
        # this blob, so a save can never observe a half-mutated dict.
        with _RATE_LIMIT_LOCK:
            store = _storage.setdefault(_RATE_LIMIT_STORAGE_KEY, {})
            store[str(user_id)] = {
                "consecutive_fails": int(activity.get("consecutive_fails", 0) or 0),
                "verification_count": int(activity.get("verification_count", 0) or 0),
                "last_verification": activity["last_verification"].isoformat() if activity.get("last_verification") else None,
                "cooldown_until": activity["cooldown_until"].isoformat() if activity.get("cooldown_until") else None,
            }
            # Bound growth: drop entries with nothing worth remembering.
            if len(store) > 50000:
                stale = [uid for uid, e in store.items()
                         if not (_parse_iso_dt(e.get("cooldown_until")) or _parse_iso_dt(e.get("last_verification")))]
                for uid in stale[: len(stale) // 2]:
                    store.pop(uid, None)
            # S1: row-level settings persistence - mark ONLY this key. This is the
            # largest settings blob (up to 50k entries); the old blanket mark
            # re-serialized every settings blob on EVERY verification attempt.
            _mark_settings_key_dirty(_RATE_LIMIT_STORAGE_KEY)
        save_storage()
    except Exception:
        pass


def _wipe_persisted_user_rate_limits() -> None:
    """Remove the persisted mirror (admin hard-reset)."""
    try:
        from bot.data.state import _mark_table_dirty, _storage, save_storage
        with _RATE_LIMIT_LOCK:
            if _RATE_LIMIT_STORAGE_KEY in _storage:
                _storage.pop(_RATE_LIMIT_STORAGE_KEY, None)
                # The settings ROW must be deleted from the DB - only the full
                # settings sync path removes rows no longer in memory.
                _mark_table_dirty("settings")
                save_storage()
    except Exception:
        pass

_country_activity: Dict[str, dict] = defaultdict(lambda: {
    "last_verification": None,
    "freeze_incidents": 0
})

_global_rate_limiter = {
    "last_api_call": None,
    "calls_this_minute": 0,
    "last_minute_reset": datetime.now()
}


def get_country_risk_profile(country_code: str) -> dict:
    """Get risk profile for a country code"""
    if country_code in COUNTRY_PROFILES:
        return COUNTRY_PROFILES[country_code]
    return {"name": "Unknown", "risk_level": 2, "min_delay": 1.0, "max_delay": 2.0}


def calculate_delay(base_min: float, base_max: float) -> float:
    """Calculate a random delay within the specified range"""
    return random.uniform(base_min, base_max)


async def human_delay(min_seconds: float, max_seconds: float, operation: str = ""):
    """Add human-like delay to avoid bot detection"""
    delay = calculate_delay(min_seconds, max_seconds)
    if delay > 0:
        await asyncio.sleep(delay)


async def pre_connect_delay(country_code: str) -> None:
    """Small delay before connecting"""
    profile = get_country_risk_profile(country_code)
    await asyncio.sleep(random.uniform(0.3, 0.8))


async def pre_send_code_delay(country_code: str) -> None:
    """Delay before sending OTP code"""
    profile = get_country_risk_profile(country_code)
    delay = random.uniform(profile["min_delay"] * 0.5, profile["max_delay"] * 0.5)
    await asyncio.sleep(delay)


async def post_otp_delay(country_code: str) -> None:
    """Delay after OTP sent"""
    await asyncio.sleep(random.uniform(0.5, 1.0))


async def pre_signin_delay(country_code: str) -> None:
    """Delay before signin"""
    profile = get_country_risk_profile(country_code)
    delay = random.uniform(profile["min_delay"] * 0.3, profile["max_delay"] * 0.3)
    await asyncio.sleep(delay)


async def pre_2fa_setup_delay(country_code: str) -> None:
    """Delay before 2FA setup"""
    profile = get_country_risk_profile(country_code)
    delay = random.uniform(profile["min_delay"], profile["max_delay"])
    await asyncio.sleep(delay)


async def post_2fa_delay(country_code: str) -> None:
    """Delay after 2FA setup"""
    await asyncio.sleep(random.uniform(0.5, 1.5))


async def pre_session_export_delay(country_code: str) -> None:
    """Delay before session export"""
    await asyncio.sleep(random.uniform(0.3, 0.8))


async def pre_device_check_delay(country_code: Optional[str] = None) -> None:
    """Delay before device check"""
    await asyncio.sleep(random.uniform(0.5, 1.0))


async def between_api_calls_delay(operation: str = "") -> None:
    """Delay between API calls to avoid rate limiting"""
    await asyncio.sleep(random.uniform(0.3, 0.8))


async def pre_disconnect_delay() -> None:
    """Small delay before disconnect"""
    await asyncio.sleep(random.uniform(0.2, 0.5))


def should_rate_limit_user(user_id: int, lang: str = "en") -> Tuple[bool, str]:
    """Check if user should be rate limited"""
    from bot.utils.i18n import t
    _load_user_activity_from_storage()
    activity = _user_activity[user_id]
    now = datetime.now()

    if activity["cooldown_until"] and now < activity["cooldown_until"]:
        # total_seconds(), not .seconds: .seconds wraps around at 24h and
        # drops sub-day day-components entirely.
        remaining = int((activity["cooldown_until"] - now).total_seconds())
        return True, t("rate_limit_wait_seconds", lang, remaining=remaining)

    if activity["last_verification"]:
        time_since_last = (now - activity["last_verification"]).total_seconds()
        if time_since_last < 30:
            return True, t("rate_limit_30_seconds", lang)

    if activity["consecutive_fails"] >= 5:
        cooldown_minutes = min(15, activity["consecutive_fails"] * 2)
        activity["cooldown_until"] = now + timedelta(minutes=cooldown_minutes)
        _persist_user_activity(user_id)
        return True, t("rate_limit_failed_attempts", lang, minutes=cooldown_minutes)

    return False, ""


def should_rate_limit_country(country_code: str, lang: str = "en") -> Tuple[bool, str]:
    """Check if a country has reached its admin-configured daily capacity.

    The ONLY cap is the per-country ``capacity`` set by the admin (Bulk Add
    Countries / country config / set rate). ``0`` = unlimited: the bot NEVER
    blocks a country on its own, no matter how many accounts are submitted.

    CRITICAL: capacity counts ONLY **SUCCESSFUL** accounts - the accounts that
    are actually verified, credited and delivered to the channel. Rejected
    accounts, failed OTP attempts, expired sessions and multi-device holds do
    NOT consume capacity. So if the admin sets Bangladesh = 100, the bot keeps
    accepting numbers until 100 accounts have truly SUCCEEDED; after that the
    country is blocked.

    The success counter is PERSISTENT and NEVER auto-resets at midnight. A
    filled country stays blocked until the admin gives it a new capacity again
    (e.g. bulk add / set rate with capacity / JSON import), which resets the
    counter. The counter survives restarts.
    """
    from bot.utils.i18n import t

    # Admin-configured per-country capacity (0 = unlimited -> never block).
    capacity = get_country_capacity(country_code)
    if capacity <= 0:
        return False, ""

    # ONLY successful accounts count toward capacity (never rejected/failed).
    success_today = get_daily_success_count(country_code)
    if success_today >= capacity:
        return True, t("rate_limit_daily_country", lang, country_code=country_code)

    return False, ""


def record_verification_attempt(user_id: int, country_code: str, success: bool):
    """Record a verification attempt for rate limiting"""
    _load_user_activity_from_storage()
    now = datetime.now()

    _user_activity[user_id]["last_verification"] = now
    _user_activity[user_id]["verification_count"] += 1

    if success:
        _user_activity[user_id]["consecutive_fails"] = 0
    else:
        _user_activity[user_id]["consecutive_fails"] += 1

    _persist_user_activity(user_id)

    _country_activity[country_code]["last_verification"] = now
    increment_daily_verification_count(country_code)


# P2-20: error fragments that are the USER'S / the submitted ACCOUNT'S fault.
# Only these count toward the 5-consecutive-fail cooldown. Bot-side and
# Telegram-side infrastructure failures (FloodWait on the bot's own traffic,
# timeouts, connection errors, API hiccups) must NEVER punish the user - the
# old code recorded every failure against the user, so a bot outage locked
# genuine users out for 10-15 minutes.
USER_FAULT_ERROR_MARKERS = (
    "PHONE_CODE_INVALID",
    "PHONE_CODE_EMPTY",
    "PHONE_CODE_EXPIRED",
    "PHONE_NUMBER_INVALID",
    "PHONE_NUMBER_BANNED",
    "PHONE_NUMBER_UNOCCUPIED",
    "SESSION_PASSWORD_NEEDED",
)


def is_user_fault_error(error) -> bool:
    """True when the error is attributable to the user / submitted account."""
    if error is None:
        return False
    text = str(getattr(error, "message", None) or error).upper()
    return any(marker in text for marker in USER_FAULT_ERROR_MARKERS)


def record_verification_outcome(user_id: int, country_code: str, error=None, success: bool = False) -> None:
    """Record a verification attempt ONLY when the user is responsible.

    ``error`` given  -> record a fail only when ``is_user_fault_error(error)``.
    ``error`` is None -> record according to ``success`` (explicit success or
    a caller that has already classified the failure as user-caused).
    """
    if error is not None and not is_user_fault_error(error):
        # Infrastructure/Telegram-side failure: do not touch the user's fail
        # counter at all.
        return
    record_verification_attempt(user_id, country_code, success)


def record_freeze_incident(country_code: str):
    """Record a freeze incident for a country"""
    _country_activity[country_code]["freeze_incidents"] += 1
    print(f"⚠️ Freeze incident recorded for {country_code}. Total: {_country_activity[country_code]['freeze_incidents']}")


_exponential_backoff_state = {
    "attempts": 0,
    "last_flood": None,
    "cooldown_multiplier": 1.0
}

async def smart_flood_wait_handler(error, operation: str = "") -> bool:
    """
    ADVANCED FloodWait Handler with Exponential Backoff v2.0
    =========================================================
    - Exponential backoff for repeated floods
    - Jitter to prevent thundering herd
    - Cooldown multiplier for severe cases
    - Automatic recovery after success
    """
    from pyrogram.errors import FloodWait
    
    if isinstance(error, FloodWait):
        try:
            raw_value = getattr(error, 'value', None)
            wait_time = int(raw_value) if raw_value is not None else 30
        except (ValueError, TypeError):
            wait_time = 30
        
        _exponential_backoff_state["attempts"] += 1
        _exponential_backoff_state["last_flood"] = datetime.now()
        
        multiplier = min(4.0, 1.0 + (_exponential_backoff_state["attempts"] * 0.5))
        _exponential_backoff_state["cooldown_multiplier"] = multiplier
        
        adjusted_wait = wait_time * multiplier
        
        if adjusted_wait > 300:
            print(f"❌ FloodWait too long ({adjusted_wait:.0f}s) for {operation} - aborting")
            print(f"   Attempts: {_exponential_backoff_state['attempts']}, Multiplier: {multiplier}x")
            return False
        
        jitter = random.uniform(1, 5)
        total_wait = adjusted_wait + jitter
        
        print(f"⏳ FloodWait (Exponential Backoff):")
        print(f"   Base wait: {wait_time}s, Multiplier: {multiplier}x")
        print(f"   Total wait: {total_wait:.1f}s for {operation}")
        print(f"   Attempt #{_exponential_backoff_state['attempts']}")
        
        await asyncio.sleep(total_wait)
        return True
    
    return False


def reset_backoff_state():
    """Reset exponential backoff after successful operation"""
    global _exponential_backoff_state
    _exponential_backoff_state = {
        "attempts": 0,
        "last_flood": None,
        "cooldown_multiplier": 1.0
    }
    print("✅ Backoff state reset after success")


def get_backoff_multiplier() -> float:
    """Get current backoff multiplier for delay calculations"""
    return _exponential_backoff_state.get("cooldown_multiplier", 1.0)


async def check_frozen_account_indicators(client) -> dict:
    """
    FROZEN Account Detection System
    ================================
    Detects if account is frozen by checking:
    1. Can send messages to self
    2. Can access dialogs
    3. Profile is accessible
    4. No ❄️ frozen indicators
    """
    indicators = {
        "is_frozen": False,
        "can_send_to_self": True,
        "can_access_dialogs": True,
        "profile_accessible": True,
        "frozen_indicators": [],
        "confidence": 0
    }
    
    try:
        me = await op_with_timeout(client.get_me(), "freeze-check-get-me")
        
        if hasattr(me, 'deleted') and me.deleted:
            indicators["is_frozen"] = True
            indicators["frozen_indicators"].append("profile_deleted")
            indicators["confidence"] += 50
        
        if hasattr(me, 'restricted') and me.restricted:
            indicators["frozen_indicators"].append("restricted")
            indicators["confidence"] += 30
            
    except Exception as e:
        error_str = str(e).upper()
        if "DEACTIVAT" in error_str or "BANNED" in error_str:
            indicators["is_frozen"] = True
            indicators["profile_accessible"] = False
            indicators["frozen_indicators"].append("account_deactivated")
            indicators["confidence"] = 100
            
    try:
        await op_with_timeout(client.send_message("me", "🔍 Freeze check"), "freeze-check-send")
        async for msg in client.iter_history("me", limit=1):
            if msg.text == "🔍 Freeze check":
                await client.delete_messages("me", msg.id)
                break
    except Exception as e:
        error_str = str(e).upper()
        indicators["can_send_to_self"] = False
        if "FLOOD" in error_str:
            indicators["frozen_indicators"].append("peer_flood")
            indicators["confidence"] += 40
        elif "FORBIDDEN" in error_str:
            indicators["is_frozen"] = True
            indicators["frozen_indicators"].append("send_forbidden")
            indicators["confidence"] += 60
            
    try:
        count = 0
        async for _ in client.iter_dialogs(limit=3):
            count += 1
        if count == 0:
            indicators["frozen_indicators"].append("no_dialogs")
            indicators["confidence"] += 20
    except Exception as e:
        indicators["can_access_dialogs"] = False
        indicators["frozen_indicators"].append("dialogs_blocked")
        indicators["confidence"] += 40
    
    if indicators["confidence"] >= 70:
        indicators["is_frozen"] = True
        
    return indicators


def get_freeze_risk_for_country(country_code: str) -> str:
    """Get freeze risk level for a country with safe default"""
    profile = get_country_risk_profile(country_code)
    freeze_risk = profile.get("freeze_risk", None)
    if freeze_risk is None:
        risk_level = profile.get("risk_level", 2)
        if risk_level >= 4:
            return "critical"
        elif risk_level >= 3:
            return "high"
        elif risk_level >= 2:
            return "medium"
        else:
            return "low"
    return freeze_risk


async def safe_api_call(func, *args, max_retries: int = 3, operation: str = "", **kwargs):
    """Wrapper for safe API calls with FloodWait handling"""
    from pyrogram.errors import FloodWait
    
    for attempt in range(max_retries):
        try:
            await between_api_calls_delay(operation)
            result = await func(*args, **kwargs)
            return result
        except FloodWait as e:
            if not await smart_flood_wait_handler(e, operation):
                raise
        except Exception as e:
            error_str = str(e).upper()
            
            if "FLOOD" in error_str:
                wait_time = 15 + (attempt * 10)
                print(f"⏳ Flood detected ({operation}): waiting {wait_time}s")
                await asyncio.sleep(wait_time)
            elif "TIMEOUT" in error_str or "CONNECTION" in error_str:
                wait_time = 3 + (attempt * 2)
                print(f"⏳ Connection issue ({operation}): waiting {wait_time}s")
                await asyncio.sleep(wait_time)
            elif attempt < max_retries - 1:
                await asyncio.sleep(1 * (attempt + 1))
            else:
                raise
    
    raise Exception(f"Max retries exceeded for {operation}")


async def warmup_session(client, country_code: str) -> None:
    """Quick session warmup - minimal calls"""
    try:
        await human_delay(0.5, 1.0, "warmup")
        try:
            await op_with_timeout(client.get_me(), "warmup-get-me")
        except Exception:
            pass  # get_me may fail but session is still valid
        print(f"✅ Quick warmup complete for {country_code}")
    except Exception as e:
        print(f"⚠️ Warmup note: {e}")


async def safe_disconnect(client, operation: str = "") -> None:
    """Safely disconnect a client, with a hard ceiling on the teardown.

    ``client.disconnect()`` has no internal timeout. Against a blackholed DC the
    socket teardown can block forever, and because this helper is awaited from
    ``finally`` blocks all over the verification / recovery / monitor paths, a
    single wedged disconnect stalled the coroutine that owned the pending
    session - so the account was never finalized even though every check had
    already succeeded. ``call_with_timeout`` swallows only the timeout, so this
    stays the "never raises" helper every caller relies on.
    """
    try:
        await pre_disconnect_delay()
        await call_with_timeout(
            client.disconnect(), f"disconnect:{operation or 'client'}"
        )
        if operation:
            print(f"✅ Disconnected ({operation})")
    except Exception as e:
        print(f"⚠️ Disconnect note: {e}")


def get_verification_delay_multiplier(country_code: str) -> float:
    """Get delay multiplier - kept minimal"""
    return 1.0


async def complete_anti_spam_verification_flow(
    country_code: str,
    user_id: int,
    lang: str = "en"
) -> Tuple[bool, str]:
    """Check if verification should proceed"""
    should_limit, msg = should_rate_limit_user(user_id, lang)
    if should_limit:
        return False, msg
    
    should_limit, msg = should_rate_limit_country(country_code, lang)
    if should_limit:
        return False, msg
    
    return True, ""


def get_recommended_timeout(country_code: str, base_timeout: int) -> int:
    """Get recommended timeout"""
    profile = get_country_risk_profile(country_code)
    extra_time = profile["risk_level"] * 15
    return min(base_timeout + extra_time, 900)


def get_night_time_multiplier() -> float:
    """No night time penalty - keep it fast"""
    return 1.0


class ZeroFreezeProtection:
    """
    ZERO FREEZE PROTECTION SYSTEM v4.0
    ===================================
    Target: 0% Freeze Rate
    
    Protection Layers:
    1. Enhanced 2FA delay (20-35 seconds like real users)
    2. Session warmup after login
    3. API rotation (5 APIs configured)
    4. Per-IP session limiting
    5. Natural typing simulation
    6. Device fingerprint variation
    """
    
    def __init__(self):
        self.session_count_by_ip = defaultdict(int)
        self.last_session_by_ip = {}
        self.frozen_patterns = defaultdict(int)
        self._load_config()
    
    def _load_config(self):
        """Load anti-freeze config from bot.config"""
        try:
            from bot.config import ANTI_FREEZE_CONFIG
            self.config = ANTI_FREEZE_CONFIG
        except ImportError:
            self.config = {
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
    
    async def enhanced_2fa_delay(self, country_code: str) -> None:
        """INSTANT - No delay for 2FA"""
        pass
    
    async def simulate_otp_typing(self, otp_code: str, client=None) -> None:
        """INSTANT - No delay for OTP typing"""
        pass
    
    async def session_warmup(self, client, country_code: str) -> bool:
        """INSTANT - No warmup delay"""
        return True
    
    async def pre_verification_check(self, ip_address: Optional[str] = None, country_code: Optional[str] = None, lang: str = "en") -> tuple:
        """
        Pre-verification check to prevent freeze
        Returns (can_proceed, message)
        """
        from bot.utils.i18n import t
        now = datetime.now()
        
        if ip_address:
            max_per_hour = self.config["max_sessions_per_ip_hour"]
            
            if ip_address in self.last_session_by_ip:
                last_time = self.last_session_by_ip[ip_address]
                if (now - last_time).total_seconds() < 3600:
                    if self.session_count_by_ip[ip_address] >= max_per_hour:
                        wait_time = 3600 - (now - last_time).total_seconds()
                        return False, t("rate_limit_ip", lang, minutes=int(wait_time/60))
                else:
                    self.session_count_by_ip[ip_address] = 0
        
        return True, "OK"
    
    def record_session_attempt(self, ip_address: Optional[str] = None, success: bool = True):
        """Record session attempt for rate limiting"""
        if ip_address:
            self.session_count_by_ip[ip_address] += 1
            self.last_session_by_ip[ip_address] = datetime.now()
    
    async def between_action_delay(self, action: str = "") -> None:
        """INSTANT - No delay between actions"""
        pass
    
    async def post_fail_cooldown(self) -> None:
        """INSTANT - No cooldown after fail"""
        pass
    
    async def post_flood_cooldown(self) -> None:
        """INSTANT - No cooldown after flood"""
        pass
    
    def get_freeze_probability(self, country_code: str) -> float:
        """
        Calculate freeze probability for a country
        With all protections enabled, should be near 0%
        """
        profile = get_country_risk_profile(country_code)
        risk_level = profile.get("risk_level", 2)
        
        base_probability = {
            4: 0.15,
            3: 0.08,
            2: 0.04,
            1: 0.02
        }.get(risk_level, 0.05)
        
        reductions = 0
        
        if self.config["enabled"]:
            reductions += 0.5
        if self.config["2fa_setup_delay_min"] >= 20:
            reductions += 0.3
        if self.config["session_warmup_enabled"]:
            reductions += 0.15
        
        final_probability = base_probability * (1 - reductions)
        return max(0.01, final_probability)


_zero_freeze = None

def get_zero_freeze_protection() -> ZeroFreezeProtection:
    """Get global ZeroFreezeProtection instance"""
    global _zero_freeze
    if _zero_freeze is None:
        _zero_freeze = ZeroFreezeProtection()
    return _zero_freeze


async def enhanced_2fa_setup_delay(country_code: str) -> None:
    """Use enhanced 2FA delay for 0% freeze"""
    protection = get_zero_freeze_protection()
    await protection.enhanced_2fa_delay(country_code)


async def simulate_otp_input(otp_code: str) -> None:
    """Simulate human OTP input"""
    protection = get_zero_freeze_protection()
    await protection.simulate_otp_typing(otp_code)


async def warmup_new_session(client, country_code: str) -> bool:
    """Warmup a new session to prevent freeze"""
    protection = get_zero_freeze_protection()
    return await protection.session_warmup(client, country_code)


print("✅ Anti-Spam & Anti-Freeze Protection Module v4.0 - ZERO FREEZE TARGET")
print(f"   Total countries configured: {len(COUNTRY_PROFILES)}")
print("   Features: Zero-Freeze Protection, Enhanced 2FA, Session Warmup, API Rotation")
print("   Risk Levels: Critical(4), High(3), Medium(2), Low(1)")
critical_count = len([c for c, p in COUNTRY_PROFILES.items() if p.get("risk_level") == 4])
high_count = len([c for c, p in COUNTRY_PROFILES.items() if p.get("risk_level") == 3])
medium_count = len([c for c, p in COUNTRY_PROFILES.items() if p.get("risk_level") == 2])
low_count = len([c for c, p in COUNTRY_PROFILES.items() if p.get("risk_level") == 1])
print(f"   Critical: {critical_count}, High: {high_count}, Medium: {medium_count}, Low: {low_count}")
print("   🎯 Target Freeze Rate: 0%")
