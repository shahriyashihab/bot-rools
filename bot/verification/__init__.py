"""
Verification package - session verification workflow split into small modules.
================================================================================
This package contains the code previously living in
``bot.utils.session_handler`` (2,801 lines).  Each module has a single
responsibility:

- state.py            shared in-memory session registry + concurrency cap
- api.py              get_rotated_api (round-robin API rotation)
- rates.py            get_country_rate / get_country_timeout
- messages.py         determine_final_message_type / build_final_message
- telethon_export.py  session_string_to_telethon / pyrogram_to_telethon
- spam_check.py       check_account_status (spam / limit / spam_free)
- device.py           run_device_check (device count / multi-device)
- steps.py            get_session_step / is_session_active / cleanup_session
- phone.py            validate_phone_with_phonenumbers / start_phone_verification
- otp.py              verify_otp_and_setup_2fa
- twofa.py            delayed_2fa_and_spam_check + impl
- validate.py         validate_session

Every public name is re-exported here so that ``bot.utils.session_handler``
can stay a thin backwards-compatible shim.
"""

from bot.verification.api import get_rotated_api
from bot.verification.messages import build_final_message, determine_final_message_type
from bot.verification.otp import verify_otp_and_setup_2fa
from bot.verification.phone import (
    start_phone_verification,
    start_session_verification,
    validate_phone_with_phonenumbers,
)
from bot.verification.rates import get_country_rate, get_country_timeout
from bot.verification.spam_check import check_account_status
from bot.verification.steps import (
    cleanup_session,
    cleanup_stale_active_sessions,
    get_session_step,
    is_session_active,
)
from bot.verification.telethon_export import (
    get_user_access_hash,
    pyrogram_to_telethon,
    session_string_to_telethon,
)
from bot.verification.twofa import delayed_2fa_and_spam_check
from bot.verification.validate import validate_session

__all__ = [
    "get_rotated_api",
    "build_final_message",
    "determine_final_message_type",
    "verify_otp_and_setup_2fa",
    "start_phone_verification",
    "start_session_verification",
    "validate_phone_with_phonenumbers",
    "get_country_rate",
    "get_country_timeout",
    "check_account_status",
    "cleanup_session",
    "cleanup_stale_active_sessions",
    "get_session_step",
    "is_session_active",
    "get_user_access_hash",
    "pyrogram_to_telethon",
    "session_string_to_telethon",
    "delayed_2fa_and_spam_check",
    "validate_session",
]
