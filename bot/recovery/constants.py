"""Refactored from bot.utils.pending_recovery: Recovery constants."""
import os


def _env_int(name: str, default: int) -> int:
    """Read an env var as a positive-safe int, NEVER raising on bad input.

    A non-numeric / negative value (e.g. a typo in .env) falls back to the
    provided default instead of crashing the bot at import time.
    """
    try:
        value = int(os.getenv(name, str(default)) or str(default))
        return value if value >= 0 else default
    except (TypeError, ValueError):
        return default


# Legacy constant kept for backwards compatibility. Real batch size is
# computed by get_recovery_parallel_limit() below (API-scaled, min 20).
PARALLEL_LIMIT = 5
DELAY_BETWEEN_BATCHES = 5.0
DELAY_BETWEEN_SESSIONS = 1.0
MAX_SESSION_AGE_HOURS = 72
NOTIFY_USERS_ON_RECOVERY = False

# H2 + P0-2: a session that never received an OTP used to be hard-deleted
# after only 3 minutes with no notification. The user is now warned first
# (recovery/cleanup.py) and the window is env-tunable. 15 minutes was still
# too aggressive for slow SMS gateways AND it meant every bot outage longer
# than 15 minutes permanently destroyed all waiting submissions - the window
# is now 120 minutes by default (NO_OTP_EXPIRY_MINUTES env), comfortably
# longer than any realistic downtime/transfer, with the user warned at the
# halfway point.
EXPIRED_NO_OTP_MINUTES = _env_int("NO_OTP_EXPIRY_MINUTES", 120)
# The user gets an explanatory message this long before the auto-delete
# (defaults to half of the expiry window).
NO_OTP_WARNING_MINUTES = max(1, EXPIRED_NO_OTP_MINUTES // 2)

EXPIRED_SESSION_WITH_SESSION_STRING_HOURS = 72

# ---------------------------------------------------------------------------
# Multi-device 24h auto-logout window
# ---------------------------------------------------------------------------
MULTI_DEVICE_24H_WINDOW_HOURS = 24
MULTI_DEVICE_GRACE_MINUTES = 20
# Outer budget: how many GENUINE logout failures a multi-device session may
# accumulate before it is rejected. Timing conditions (Telegram's own 24h
# "fresh authorization" rule, long FloodWaits) are recorded as deferrals via
# ``md_logout_not_before`` and do NOT consume any of these attempts, so this
# budget is only ever spent on real errors. 3 was far too tight - three
# network blips could reject a perfectly good account.
MULTI_DEVICE_LOGOUT_MAX_RETRIES = 12
# Inner budget: attempts inside ONE run_auto_logout() call (each attempt does
# getAuthorizations -> resetAuthorization sweep -> fresh verification).
MULTI_DEVICE_LOGOUT_INNER_ATTEMPTS = 5
# A deferred multi-device session is retried no sooner than this.
MULTI_DEVICE_LOGOUT_RETRY_MINUTES = 15
# Deferrals consume no retry budget ON PURPOSE (waiting for Telegram's own 24h
# freshness window is not a failure). But "unbounded" and "safe" are not the
# same thing: a session that returns DEFER on every single cycle was never
# rejected AND never credited - the user simply never got paid and nothing
# logged an error. This caps the total number of deferrals; past it a deferral
# is downgraded to a genuine failure so the normal bounded-retry -> reject chain
# takes over. 48 x >=60s of real waiting is far more than any legitimate
# freshness window (24h is covered by ONE deferral, not 48).
MULTI_DEVICE_MAX_DEFERRALS = 48


# Recovery batch size scales with the API pool (env + admin-panel runtime
# APIs): ``max(RECOVERY_PARALLEL_MIN, RECOVERY_PER_API_PARALLEL * <API count>)``.
# An explicit RECOVERY_PARALLEL_LIMIT env override (if > 0) wins for tuning.


RECOVERY_PER_API_PARALLEL = _env_int("RECOVERY_PER_API_PARALLEL", 2)
RECOVERY_PARALLEL_MIN = _env_int("RECOVERY_PARALLEL_MIN", 20)


def get_recovery_parallel_limit() -> int:
    """Batch size for the startup recovery loop, scaled with the API pool."""
    override = _env_int("RECOVERY_PARALLEL_LIMIT", 0)
    if override > 0:
        return override
    api_count = 1
    try:
        from bot.utils.api_rotation import get_api_manager
        manager = get_api_manager()
        if manager.apis:
            api_count = len(manager.apis)
    except Exception:
        pass
    return max(RECOVERY_PARALLEL_MIN, RECOVERY_PER_API_PARALLEL * api_count)
