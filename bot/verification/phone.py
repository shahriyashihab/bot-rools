"""
Phone submission & verification start.
=====================================================================
Real-world phone validation (phonenumbers library) and the
anti-spam-guarded start_phone_verification flow that sends the OTP
code through a rotated API and records the waiting session.
"""

import re
import logging
from datetime import datetime
from typing import Optional

from pyrogram.client import Client
from pyrogram.errors import PhoneNumberInvalid, FloodWait

_logger = logging.getLogger(__name__)

from bot.utils.anti_spam import (
    between_api_calls_delay,
    complete_anti_spam_verification_flow,
    get_country_risk_profile,
    human_delay,
    post_otp_delay,
    pre_connect_delay,
    pre_send_code_delay,
    record_verification_attempt,
    safe_disconnect,
    smart_flood_wait_handler,
)
from bot.utils.country_data import (
    get_country_code_from_phone,
    get_country_info,
    get_flag,
    normalize_phone,
)
from bot.utils.device_generator import get_device_for_country
from bot.utils.i18n import get_user_lang, t
from bot.utils.net_timeouts import connect_with_timeout, op_with_timeout
from bot.utils.storage import get_open_countries, is_country_open
from bot.verification.api import get_rotated_api
from bot.utils.api_rotation import record_api_usage
from bot.verification.state import _active_sessions

# Telegram Desktop's own api_id/api_hash. Using this when creating the user
# session makes the generated tdata open in Telegram Desktop without the
# account being logged out (the session's app matches the Desktop app).
TDESKTOP_API_ID = 2040
TDESKTOP_API_HASH = "b18441a1ff607e10a989891a5462e627"


def validate_phone_with_phonenumbers(phone: str, lang: str = "en"):
    """
    Real-world phone validation using the `phonenumbers` library (ITU data).

    Rejects (returns an error string):
      - numbers whose calling code is not assigned to any country (+900, +999)
      - numbers that are too short / too long for their country (+1555123456)
      - numbers that are structurally invalid for their country
      - unparseable input

    Returns None when the number is a real, valid number.
    When the `phonenumbers` library is not installed the regex check already
    performed by the caller remains the only gate (returns None here).
    """
    try:
        import phonenumbers as _pn
    except ImportError:
        return None

    from bot.utils.country_data import COUNTRY_DATA

    try:
        parsed = _pn.parse(phone, None)
    except Exception:
        return t("invalid_phone_format_message", lang)

    # Calling code that has no assigned region at all (e.g. +900, +999).
    if not _pn.region_code_for_number(parsed):
        return t("country_code_not_supported", lang)

    # The calling code must exist in our real country-code database.
    cc_str = f"+{parsed.country_code}"
    if cc_str not in COUNTRY_DATA and not any(c.startswith(cc_str) for c in COUNTRY_DATA):
        return t("country_code_not_supported", lang)

    # Length gate: reject numbers that are impossible for their country
    # (e.g. a 9-digit national number for a +1 / NANP country).
    if not _pn.is_possible_number(parsed):
        return t("invalid_phone_check_message", lang)

    # Structural gate: reject numbers that are not real (valid) for their
    # country, e.g. +9001234567 which parses to Turkey but is not a real
    # Turkish number.
    if not _pn.is_valid_number(parsed):
        return t("invalid_phone_check_message", lang)

    return None


async def start_phone_verification(phone: str, user_id: int):
    """
    Start phone verification process for a user with ANTI-SPAM protection
    
    Args:
        phone: Phone number with country code
        user_id: Telegram user ID
    
    Returns:
        tuple: (success: bool, message: str)
    """
    lang = get_user_lang(int(user_id))
    phone = normalize_phone(phone)
    
    if not re.match(r'^\+[1-9]\d{6,15}$', phone):
        return False, t("invalid_phone_format_message", lang), None

    real_world_error = validate_phone_with_phonenumbers(phone, lang)
    if real_world_error:
        return False, real_world_error, None
    
    country_code = get_country_code_from_phone(phone)
    
    if not country_code:
        return False, t("country_not_supported_full", lang), None

    from bot.utils.storage import is_same_number_enabled, is_number_successful
    if is_same_number_enabled() and is_number_successful(phone):
        print(f"🔢 SAME NUMBER: {phone} already succeeded before - blocked")
        return False, t("same_number_already_used_message", lang, phone=phone), None

    can_proceed, rate_limit_msg = await complete_anti_spam_verification_flow(country_code, user_id, lang)
    if not can_proceed:
        return False, rate_limit_msg, None

    # P4-39: proxy-system-down gate. When the watchdog has detected ALL
    # proxies dead for 2+ consecutive passes, new phone submissions are
    # rejected with a user-facing message until the fleet recovers.
    try:
        from bot.data.proxies import is_proxy_system_down
        if is_proxy_system_down():
            return False, t("proxy_system_down", lang), None
    except ImportError:
        pass

    # P7-66: the unified rate limiter (dead code until now) guards the
    # phone-submission entry - 3/min, 30/hour per user. Admins bypass.
    try:
        from bot.handlers.admin.router import is_admin
        if not is_admin(user_id):
            from bot.utils.rate_limiter import check_rate_limit
            _allowed, _rl_msg, _retry_after = await check_rate_limit(user_id, "verify_phone")
            if not _allowed:
                return False, _rl_msg, None
    except ImportError:
        pass

    if not is_country_open(country_code):
        country_name, flag = get_country_info(country_code)
        open_countries = get_open_countries()
        open_count = len(open_countries)
        
        return False, t(
            "country_not_open_message", lang,
            country_code=country_code,
            country_name=country_name,
            flag=flag,
            open_count=open_count,
        ), None
    
    if user_id in _active_sessions and phone in _active_sessions[user_id]:
        try:
            old_client = _active_sessions[user_id][phone].get("client")
            if old_client:
                await safe_disconnect(old_client, "cleanup-old")
        except Exception:
            pass  # Old session cleanup is non-critical
        del _active_sessions[user_id][phone]
        if not _active_sessions[user_id]:
            del _active_sessions[user_id]
    
    try:
        risk_profile = get_country_risk_profile(country_code)
        _logger.debug(f"🛡️ ANTI-SPAM: Starting verification for {country_code} (Risk Level: {risk_profile['risk_level']})")
        
        await pre_connect_delay(country_code)
        
        # CRITICAL (LOGIN ROTATION): the login/OTP session is created with the
        # NEXT API from the TRUE ROUND-ROBIN pool, exactly like the check
        # phases (spam/validate/device/recovery). With thousands of sessions
        # per day this spreads the login load across ALL APIs so no single API
        # carries the whole traffic - drastically reducing freeze/ban rate.
        # A country with a per-country custom API (CORE CONFIG) uses its own
        # credentials instead of the rotation.
        api_id, api_hash, api_name = get_rotated_api(country_code)
        _logger.debug(f"🔄 ROUND-ROBIN LOGIN: Using {api_name} (ID: {api_id}) for {phone}")
        
        device_info = get_device_for_country(country_code)
        _logger.debug(f"📱 Using device: {device_info['device_model']} ({device_info['system_version']})")
        
        session_name = f"user_{user_id}_{int(datetime.now().timestamp())}"

        def _make_login_client(proxy):
            kwargs = dict(
                name=session_name,
                api_id=api_id,
                api_hash=api_hash,
                in_memory=True,
                device_model=device_info["device_model"],
                system_version=device_info["system_version"],
                app_version="Telegram Android 0.28.1.1771-arm64-v8a",
                lang_code=device_info.get("lang_code", "en")
            )
            if proxy:
                kwargs["proxy"] = proxy
            return Client(**kwargs)

        # Route this country's login through its configured proxy pool
        # (country pool -> default proxy -> direct fail-open). When the proxy
        # system is unused this connects directly exactly as before.
        from bot.utils.proxy_connector import connect_with_proxy_chain
        client = await connect_with_proxy_chain(
            _make_login_client, country_code, f"send-code:{phone}"
        )
        _logger.debug(f"✅ Client connected for {phone} via {api_name}")
        
        await pre_send_code_delay(country_code)
        
        phone_code_hash: Optional[str] = None
        try:
            for attempt in range(3):
                try:
                    # Bounded: send_code has no internal timeout, so a blackholed
                    # DC left the user's submission awaiting forever - no error,
                    # no "code sent" message, and the retry loop never ran.
                    sent_code = await op_with_timeout(
                        client.send_code(phone), f"send-code:{phone}"
                    )
                    phone_code_hash = sent_code.phone_code_hash
                    _logger.debug(f"✅ OTP code sent to {phone}")
                    record_api_usage(api_name=api_name, success=True)
                    break
                except FloodWait as e:
                    wait_time = getattr(e, 'value', 30) or 30
                    if not await smart_flood_wait_handler(e, "send_code"):
                        record_api_usage(api_name=api_name, success=False, flood=True)
                        await safe_disconnect(client, "flood-fail")
                        return False, t("too_many_requests_short", lang, wait_time=wait_time), None
                except Exception:
                    if attempt < 2:
                        await human_delay(1, 2, f"retry-send-code-{attempt+1}")
                    else:
                        raise
            
            if phone_code_hash is None:
                await safe_disconnect(client, "no-code-hash")
                record_api_usage(api_name=api_name, success=False)
                return False, t("failed_to_send_otp", lang), None
        except PhoneNumberInvalid:
            await safe_disconnect(client, "invalid-phone")
            record_verification_attempt(user_id, country_code, False)
            record_api_usage(api_name=api_name, success=False)
            return False, t("invalid_phone_error", lang), None
        except Exception as e:
            await safe_disconnect(client, "send-code-error")
            # P2-20: only a USER-fault error counts toward the fail cooldown.
            # FloodWait/generic infra failures must not lock the user out.
            from bot.utils.anti_spam import is_user_fault_error
            if is_user_fault_error(e):
                record_verification_attempt(user_id, country_code, False)
            error_str = str(e).upper()
            if "FLOOD" in error_str:
                record_api_usage(api_name=api_name, success=False, flood=True)
                return False, t("too_many_requests", lang), None
            elif "PHONE_NUMBER_BANNED" in error_str:
                record_api_usage(api_name=api_name, success=False)
                return False, t("phone_banned_error", lang), None
            record_api_usage(api_name=api_name, success=False)
            return False, t("generic_otp_error", lang, error=str(e)), None
        
        await post_otp_delay(country_code)
        
        flag = get_flag(country_code)
        
        _active_sessions.setdefault(user_id, {})[phone] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": phone_code_hash,
            "country_code": country_code,
            "step": "waiting_otp",
            "started_at": datetime.now().isoformat(),
            "phone_message_id": None,
            "session_name": session_name,
            "risk_level": risk_profile["risk_level"],
            "api_id": api_id,
            "api_hash": api_hash,
            "api_name": api_name,
            "proxy_used": getattr(client, "_bot_proxy_key", "") or "",
            "proxy_attribution": getattr(client, "_bot_proxy_attribution", "") or "",
            # P4-41: per-session proxy pin - the DIALABLE URL of the proxy this
            # login actually used ("" = direct). Persisted into the pending row
            # so every later phase dials the SAME proxy (stickiness).
            "proxy_url": getattr(client, "_bot_proxy_url", "") or "",
        }
        
        _active_sessions[user_id]["_latest_waiting_phone"] = phone
        _logger.debug(f"📍 Marked {phone} as latest waiting phone for user {user_id}")
        
        _logger.debug(f"🛡️ ANTI-SPAM: OTP sent successfully for {phone} with proper delays")
        
        return True, t("otp_code_sent", lang, flag=flag, phone=phone), None
        
    except Exception as e:
        # P2-20: only user-fault errors count toward the fail cooldown;
        # bot/Telegram-side failures never punish the user.
        from bot.utils.anti_spam import is_user_fault_error
        if is_user_fault_error(e):
            record_verification_attempt(user_id, country_code, False)
        error_str = str(e).upper()
        if "FLOOD" in error_str:
            return False, t("too_many_requests_later", lang), None
        return False, t("generic_otp_error", lang, error=str(e)), None


async def start_session_verification(user_id: int, phone: str, phone_message_id = None):
    """Wrapper for start_phone_verification that accepts phone_message_id (ignored)"""
    return await start_phone_verification(phone, user_id)
