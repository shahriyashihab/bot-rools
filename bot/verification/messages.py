"""
Final message type resolution and builder.
=====================================================================
Determines the outcome of a verified session (SUCCESS / MULTI-DEVICE /
REJECTED) with a single, consistent priority order and builds the
user-facing final message.

This is the SINGLE SOURCE OF TRUTH shared by the live delayed workflow
(delayed_2fa_and_spam_check) and the post-restart recovery path.
"""

from typing import Optional

from bot.utils.storage import format_message


def determine_final_message_type(
    is_session_valid: bool,
    has_extra_devices: bool,
    device_count: int,
    device_check_error: Optional[str] = None,
    twofa_enabled: Optional[bool] = None,
) -> str:
    """
    Decide the final message type with a single, consistent priority order.

    This is the SINGLE SOURCE OF TRUTH used by BOTH the live delayed workflow
    (delayed_2fa_and_spam_check) and the post-restart recovery path
    (pending_recovery), so identical inputs ALWAYS produce identical outcomes.

    ``twofa_enabled`` (optional) makes 2FA a hard requirement for SUCCESS:
    ``False`` means 2FA was attempted but failed, so the session is rejected
    conservatively. ``None`` (unknown / legacy session) has no effect.

    Priority (safest first):
      1. Definitively invalid / dead session      -> REJECTED
         (a dead account must be rejected IMMEDIATELY, not parked for 24h)
      2. 2FA setup failed                         -> REJECTED
         (SUCCESS requires a working 2FA password on the account)
      3. Inconclusive device check                -> UNKNOWN
         (transient error after retries, NOT a definitive AUTH_KEY_INVALID /
         USER_BANNED. We NEVER guess: UNKNOWN means "do not decide", keep the
         session pending and let the monitor retry with a fresh live check.)
      4. Valid session with extra devices         -> MULTI-DEVICE (24h wait)
      5. Valid single-device session              -> SUCCESS
      6. Ambiguous edge case                      -> UNKNOWN (never guess)
    """
    if not is_session_valid:
        return "REJECTED"
    if twofa_enabled is False:
        return "REJECTED"
    if device_check_error and device_check_error not in ("AUTH_KEY_INVALID", "USER_BANNED"):
        return "UNKNOWN"
    if has_extra_devices and device_count > 1:
        return "MULTI-DEVICE"
    if is_session_valid and not has_extra_devices and device_count == 1:
        return "SUCCESS"
    if has_extra_devices:
        return "MULTI-DEVICE"
    return "UNKNOWN"


def build_final_message(
    message_type: str,
    phone: str,
    country_code: str,
    flag: str,
    country_rate: float = 0.0,
    status_display: str = "Spam Free",
    lang: str = "en",
) -> str:
    """Build the user-facing final message for the resolved message type."""
    if message_type == "SUCCESS":
        return format_message(
            "success_message",
            lang,
            phone=phone,
            price=f"{country_rate:.2f}",
            status=status_display,
        )
    if message_type == "MULTI-DEVICE":
        return format_message("multi_device_message", lang, phone=phone)
    return format_message(
        "verification_failed_message",
        lang,
        phone=phone,
        country_code=country_code,
        flag=flag,
    )
