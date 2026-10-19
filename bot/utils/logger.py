"""
Professional Logging Module for Telegram Session Bot
=====================================================

Provides structured logging with:
- Multiple log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- Formatted output with timestamps
- File and console handlers
- Emoji indicators for easy visual scanning
"""

import logging
import sys
from datetime import datetime
from typing import Optional

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_loggers = {}


def get_logger(name: str = "bot") -> logging.Logger:
    """
    Get or create a logger with the specified name.
    
    Args:
        name: Logger name (e.g., 'bot', 'session_handler', 'storage')
    
    Returns:
        Configured logger instance
    """
    if name in _loggers:
        return _loggers[name]
    
    logger = logging.getLogger(name)
    
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        logger.addHandler(console_handler)
    
    _loggers[name] = logger
    return logger


def log_info(message: str, logger_name: str = "bot") -> None:
    """Log an info message with ℹ️ indicator"""
    get_logger(logger_name).info(f"ℹ️ {message}")


def log_success(message: str, logger_name: str = "bot") -> None:
    """Log a success message with ✅ indicator"""
    get_logger(logger_name).info(f"✅ {message}")


def log_warning(message: str, logger_name: str = "bot") -> None:
    """Log a warning message with ⚠️ indicator"""
    get_logger(logger_name).warning(f"⚠️ {message}")


def log_error(message: str, logger_name: str = "bot", exc_info: bool = False) -> None:
    """Log an error message with ❌ indicator"""
    get_logger(logger_name).error(f"❌ {message}", exc_info=exc_info)


def log_critical(message: str, logger_name: str = "bot", exc_info: bool = True) -> None:
    """Log a critical message with 🔥 indicator"""
    get_logger(logger_name).critical(f"🔥 {message}", exc_info=exc_info)


def log_debug(message: str, logger_name: str = "bot") -> None:
    """Log a debug message with 🔍 indicator"""
    get_logger(logger_name).debug(f"🔍 {message}")


def log_session_event(
    event_type: str,
    phone: str,
    user_id: int,
    details: Optional[str] = None,
    success: bool = True
) -> None:
    """
    Log a session-related event with structured format.
    
    Args:
        event_type: Type of event (OTP_SENT, 2FA_SETUP, VERIFIED, REJECTED, etc.)
        phone: Phone number
        user_id: Telegram user ID
        details: Additional details
        success: Whether the event was successful
    """
    logger = get_logger("session")
    indicator = "✅" if success else "❌"
    msg = f"{indicator} [{event_type}] Phone: {phone} | User: {user_id}"
    if details:
        msg += f" | {details}"
    
    if success:
        logger.info(msg)
    else:
        logger.warning(msg)


def log_api_call(
    operation: str,
    success: bool = True,
    duration_ms: Optional[float] = None,
    error: Optional[str] = None
) -> None:
    """
    Log an API call with timing information.
    
    Args:
        operation: Name of the API operation
        success: Whether the call succeeded
        duration_ms: Duration in milliseconds
        error: Error message if failed
    """
    logger = get_logger("api")
    
    if success:
        duration_str = f" ({duration_ms:.1f}ms)" if duration_ms else ""
        logger.debug(f"✅ API: {operation}{duration_str}")
    else:
        logger.warning(f"❌ API: {operation} failed - {error}")


def log_balance_change(
    user_id: int,
    amount: float,
    operation: str,
    new_balance: float
) -> None:
    """
    Log a balance change event.
    
    Args:
        user_id: User ID
        amount: Amount changed
        operation: Type of operation (credit, debit, withdraw)
        new_balance: New balance after change
    """
    logger = get_logger("balance")
    sign = "+" if amount > 0 else ""
    logger.info(f"💰 User {user_id}: {sign}${amount:.2f} ({operation}) | Balance: ${new_balance:.2f}")


bot_logger = get_logger("bot")
session_logger = get_logger("session")
api_logger = get_logger("api")
storage_logger = get_logger("storage")
