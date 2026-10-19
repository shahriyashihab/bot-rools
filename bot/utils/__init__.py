"""
Bot Utilities Package
=====================

Contains utility modules for the Telegram session bot.

Modules:
    - storage: User data and session storage management
    - session_handler: Session verification and management
    - anti_spam: Rate limiting and anti-spam protection
    - fast_cache: High-speed caching for templates and user data
    - device_generator: Device fingerprint generation
    - country_data: Country codes and flags
    - logger: Professional logging system
"""

from bot.utils.storage import (
    get_user_info,
    update_user_info,
    get_country_rate,
    get_country_timeout,
    is_country_open,
    add_balance_for_user,
)
from bot.utils.anti_spam import (
    get_country_risk_profile,
    human_delay,
    should_rate_limit_user,
    record_verification_attempt,
)
from bot.utils.logger import (
    get_logger,
    log_info,
    log_success,
    log_warning,
    log_error,
    log_session_event,
)

__all__ = [
    "get_user_info",
    "update_user_info",
    "get_country_rate",
    "get_country_timeout",
    "is_country_open",
    "add_balance_for_user",
    "get_country_risk_profile",
    "human_delay",
    "should_rate_limit_user",
    "record_verification_attempt",
    "get_logger",
    "log_info",
    "log_success",
    "log_warning",
    "log_error",
    "log_session_event",
]