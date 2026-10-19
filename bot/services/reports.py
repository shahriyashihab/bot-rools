"""Unified channel report builders + senders (single source of truth).

Every verification/rejection/multi-device report - regardless of which code
path finished the session (monitor fast path, full validation, recovery scan
or startup recovery) - is built and sent through this module. This guarantees
every channel always receives the same report format instead of the
copy-pasted, slightly-different formats that used to live in
``monitor/finalize.py``, ``monitor/multi_device.py``, ``monitor/rejector.py``
and ``recovery/worker.py``.
"""
import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from bot import config
from bot.constants import get_country_flag
from bot.services.channel_sender import send_to_channel_with_retry

logger = logging.getLogger(__name__)

_SPAM_DISPLAY = {
    "spam_free": "✅ Spam Free",
    "spam": "⚠️ Spam",
    "limit": "🔒 Limit",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _label(report_label: str) -> str:
    return f" ({report_label})" if report_label else ""


def build_success_report(
    phone: str,
    country_code: str,
    rate: float,
    spam_status: str,
    *,
    report_label: str = "",
    name: Optional[str] = None,
    account_id: Any = None,
    twofa_enabled: Optional[bool] = None,
    verified_by: Any = None,
) -> str:
    """Build the canonical SUCCESS report sent to CHANNEL_VERIFIED."""
    flag = get_country_flag(country_code)
    spam_display = _SPAM_DISPLAY.get(spam_status, "🔒 Limit")
    lines = [
        f"✅ **Session Verified{_label(report_label)}**",
        "",
        f"📱 Phone: {phone}",
        f"🌍 Country: {country_code} {flag}",
    ]
    if name:
        lines.append(f"👤 Name: {name}")
    if account_id is not None:
        lines.append(f"🆔 Account ID: {account_id}")
    lines.append(f"📊 Status: {spam_display}")
    lines.append(f"💰 Rate: ${rate:.2f}")
    if twofa_enabled is not None:
        lines.append(f"🔐 2FA: {'✅ Enabled' if twofa_enabled else '❌ Failed'}")
    if verified_by is not None:
        lines.append(f"✅ Verified by: {verified_by}")
    lines.append(f"⏰ Time: {_now()}")
    return "\n".join(lines)


def build_rejected_report(
    phone: str,
    country_code: str,
    reason: str,
    *,
    report_label: str = "",
    user_id: Any = None,
    rate: Optional[float] = None,
    name: Optional[str] = None,
    account_id: Any = None,
    twofa_enabled: Optional[bool] = None,
    device_count: Any = None,
    spam_status: Optional[str] = None,
    verified_by: Any = None,
) -> str:
    """Build the canonical REJECTED report sent to CHANNEL_REJECTED."""
    flag = get_country_flag(country_code)
    lines = [
        f"❌ **Session Rejected{_label(report_label)}**",
        "",
        f"📱 Phone: {phone}",
        f"🌍 Country: {country_code} {flag}",
    ]
    if name:
        lines.append(f"👤 Name: {name}")
    if account_id is not None:
        lines.append(f"🆔 Account ID: {account_id}")
    if user_id is not None:
        lines.append(f"👤 User: {user_id}")
    if spam_status is not None:
        lines.append(f"📊 Status: {_SPAM_DISPLAY.get(spam_status, '🔒 Limit')}")
    if rate is not None:
        lines.append(f"💰 Rate: ${rate:.2f}")
    if twofa_enabled is not None:
        lines.append(f"🔐 2FA: {'✅ Enabled' if twofa_enabled else '❌ Failed'}")
    if device_count is not None:
        lines.append(f"📱 Devices: {device_count}")
    lines.append(f"⚠️ Reason: {reason}")
    if verified_by is not None:
        lines.append(f"✅ Verified by: {verified_by}")
    lines.append(f"⏰ Time: {_now()}")
    return "\n".join(lines)


def build_multi_device_report(
    phone: str,
    country_code: str,
    device_count: Any,
    *,
    report_label: str = "",
    user_id: Any = None,
    rate: Optional[float] = None,
    hours_left: Optional[float] = None,
    extra_devices_list: Optional[List[Dict[str, Any]]] = None,
    name: Optional[str] = None,
    account_id: Any = None,
    twofa_enabled: Optional[bool] = None,
    spam_status: Optional[str] = None,
    verified_by: Any = None,
) -> str:
    """Build the canonical MULTI-DEVICE report sent to CHANNEL_24HR_UP."""
    flag = get_country_flag(country_code)
    lines = [
        f"📱 **Multiple Devices Detected{_label(report_label)}**",
        "",
        f"📱 Phone: {phone}",
        f"🌍 Country: {country_code} {flag}",
    ]
    if name:
        lines.append(f"👤 Name: {name}")
    if account_id is not None:
        lines.append(f"🆔 Account ID: {account_id}")
    if user_id is not None:
        lines.append(f"👤 User: {user_id}")
    if spam_status is not None:
        lines.append(f"📊 Status: {_SPAM_DISPLAY.get(spam_status, '🔒 Limit')}")
    if rate is not None:
        lines.append(f"💰 Rate: ${rate:.2f}")
    if twofa_enabled is not None:
        lines.append(f"🔐 2FA: {'✅ Enabled' if twofa_enabled else '❌ Failed'}")
    lines.append(f"📊 Devices: {device_count}")
    if extra_devices_list:
        for idx, device in enumerate(extra_devices_list, 1):
            is_mark = "✅ Our Device" if device.get("is_current") else "❌ Extra Device"
            lines.append(
                f"{idx}. {device.get('device', 'Unknown')}\n"
                f"   {device.get('location', 'Unknown')}\n"
                f"   {device.get('created', 'Unknown')}\n"
                f"   {is_mark}"
            )
    lines.append("ℹ️ Session Valid - Balance NOT added")
    if hours_left is not None:
        lines.append(f"⏳ {hours_left:.1f}h remaining until auto-cleanup")
    if verified_by is not None:
        lines.append(f"✅ Verified by: {verified_by}")
    lines.append(f"⏰ Time: {_now()}")
    return "\n".join(lines)


def build_expired_report(
    phone: str,
    country_code: str,
    *,
    age_hours: Optional[float] = None,
) -> str:
    """Build the multi-device 24h window expiry report (CHANNEL_REJECTED)."""
    flag = get_country_flag(country_code)
    lines = [
        f"⏰ **Multi-Device 24h Window Missed - Expired**",
        "",
        f"📱 Phone: {phone}",
        f"🌍 Country: {country_code} {flag}",
    ]
    if age_hours is not None:
        lines.append(f"⏱️ Age: {int(age_hours)} hours")
    lines.append("❌ 24h verification window passed without balance")
    lines.append("📋 Status: Rejected/Expired")
    return "\n".join(lines)


def build_expired_stuck_report(
    phone: str,
    country_code: str,
    *,
    age_hours: Optional[float] = None,
    reason: str = "Session expired",
) -> str:
    """Build the stuck/old-session expiry report (CHANNEL_REJECTED)."""
    flag = get_country_flag(country_code)
    lines = [
        f"⏰ **Session Expired (Stuck)**",
        "",
        f"📱 Phone: {phone}",
        f"🌍 Country: {country_code} {flag}",
    ]
    if age_hours is not None:
        lines.append(f"⏱️ Age: {int(age_hours)} hours")
    lines.append(f"❌ Reason: {reason}")
    return "\n".join(lines)


def build_task_failure_report(
    phone: str,
    country_code: str,
    *,
    user_id: Any = None,
    session_id: Any = None,
    error: str = "",
    report_label: str = "",
) -> str:
    """Build the background-task failure report (CHANNEL_REJECTED)."""
    flag = get_country_flag(country_code)
    lines = [
        f"❌ **Task Failure{_label(report_label)}**",
        "",
        f"📱 Phone: {phone}",
        f"🌍 Country: {country_code} {flag}",
    ]
    if user_id is not None:
        lines.append(f"👤 User: {user_id}")
    if session_id is not None:
        lines.append(f"🆔 Session: {session_id}")
    lines.append(f"❌ Error: {error[:500]}")
    lines.append(f"⏰ Time: {_now()}")
    return "\n".join(lines)


def build_pending_report(
    phone: str,
    country_code: str,
    *,
    title: str = "Pending Session",
    name: Optional[str] = None,
    account_id: Any = None,
    user_id: Any = None,
    timeout_seconds: Optional[int] = None,
    twofa_enabled: Optional[bool] = None,
    spam_status: Optional[str] = None,
    rate: Optional[float] = None,
    rate_note: Optional[str] = None,
) -> str:
    """Build the pending/submitted session report (CHANNEL_SUBMIT).

    When ``twofa_enabled`` / ``spam_status`` / ``rate`` are not known yet
    (right after OTP submit) placeholder lines are shown; once the delayed
    2FA + spam check finishes they are replaced by real values.
    """
    flag = get_country_flag(country_code)
    lines = [
        f"📥 **{title}**",
        "",
    ]
    if name:
        lines.append(f"👤 Name: {name}")
    if account_id is not None:
        lines.append(f"🆔 Account ID: {account_id}")
    lines.append(f"📱 Phone: {phone}")
    lines.append(f"🌍 Country: {country_code} {flag}")
    if twofa_enabled is None:
        lines.append("🔐 2FA: ⏳ Setting up...")
    else:
        lines.append(f"🔐 2FA: {'✅ Enabled' if twofa_enabled else '❌ Failed'}")
    if spam_status is None:
        lines.append("📊 Spam Status: 🔁 Checking...")
    else:
        lines.append(f"📊 Spam Status: {_SPAM_DISPLAY.get(spam_status, spam_status)}")
    if rate is None:
        lines.append("💰 Rate: Calculating...")
    else:
        rate_line = f"💰 Rate: ${rate:.2f}"
        if rate_note:
            rate_line += f" ({rate_note})"
        lines.append(rate_line)
    if timeout_seconds is not None:
        lines.append(f"⏰ Timeout: {timeout_seconds}s")
    if user_id is not None:
        lines.append(f"✅ Verified by: {user_id}")
    lines.append(f"📅 Time: {_now()}")
    return "\n".join(lines)


def build_timeout_report(
    phone: str,
    country_code: str,
    *,
    user_id: Any = None,
    timeout_seconds: Optional[float] = None,
) -> str:
    """Build the stuck-recovery timeout report (CHANNEL_REJECTED)."""
    flag = get_country_flag(country_code)
    lines = [
        f"❌ **Session Timeout**",
        "",
        f"📱 Phone: {phone}",
        f"🌍 Country: {country_code} {flag}",
    ]
    if user_id is not None:
        lines.append(f"👤 User: {user_id}")
    if timeout_seconds is not None:
        lines.append(f"⏰ Timeout: {timeout_seconds:.0f}s")
    lines.append(f"⏰ Time: {_now()}")
    return "\n".join(lines)


async def _send_document(chat_id, document: str, caption: str, bot_client) -> bool:
    """Send a document reliably.

    Returns True ONLY when the document actually reached the channel. When
    ``bot_client`` is None the shared retry path (send_to_channel_with_retry)
    is used, whose return value is checked so a silent failure is never
    mistaken for success (previously the JSON send result was ignored and a
    failed JSON was reported as delivered).
    """
    if bot_client is not None:
        try:
            result = await bot_client.send_document(chat_id=chat_id, document=document, caption=caption)
            return result is not None
        except Exception as e:
            logger.warning(f"_send_document (bot_client) failed for {document}: {e}")
            return False
    result = await send_to_channel_with_retry(chat_id=chat_id, document=document, caption=caption)
    return result is not None


async def send_json_alongside(
    chat_id, bot_client, caption: str,
    phone: str, session_string: str,
    account_user_id, verified_by_user,
    session_info,
    suffix: str = "",
    twofa_password: str = "",
    country_code: str = "",
    rate=None,
    account_status: str = "",
    status_details=None,
) -> bool:
    """Generate and send the JSON file alongside the session file.

    The JSON is the 'program' format consumed by account-management tools:
    session_str, app_id, app_hash, device info, etc. Without it the buyer
    receives only the .session file and cannot use the account.

    ``account_user_id``  = the ACCOUNT's real Telegram user id (goes into the
                           JSON ``user_id`` field).
    ``verified_by_user`` = the operator who verified the account (goes into the
                           JSON ``verified_by_user`` field).
    ``session_info``     = account metadata (first_name, country_code, ...).
    ``suffix``           = optional filename suffix so each format gets its OWN
                           JSON file, e.g. ``<phone>_telethon.json`` and
                           ``<phone>_pyrogram.json`` (before this fix both
                           formats shared one filename, so the second format
                           silently overwrote the first one on disk and the
                           JSON could be lost between write and send).
    ``twofa_password`` / ``country_code`` / ``rate`` / ``account_status`` /
    ``status_details``   = the ACCOUNT's real values; they win over whatever
                           is (or is not) inside ``session_info``, so the JSON
                           never carries global defaults (e.g. rate=0) when the
                           caller has the real data.

    Returns ``True`` when the JSON was actually sent, ``False`` otherwise.
    """
    import json as _json
    from bot.services.session_io import build_session_data_full
    from bot.utils.device_generator import generate_device_info
    from bot import config as _config
    try:
        safe = str(phone).replace("+", "")
        device_info = generate_device_info()
        info = dict(session_info or {})
        # The account's real user id MUST land in the JSON ``user_id`` field.
        # Prefer the explicit account_user_id; fall back to parsing it from the
        # session string itself when the caller only has the operator's id.
        if not account_user_id:
            try:
                from bot.services.session_formats import parse_pyrogram_session
                _parsed = parse_pyrogram_session(session_string)
                if _parsed and _parsed.get("user_id"):
                    account_user_id = _parsed["user_id"]
            except Exception:
                pass
        if account_user_id and not info.get("user_id"):
            info["user_id"] = account_user_id
        # The twoFA field must reflect the ACCOUNT's real state. When 2FA is
        # disabled by the admin (or its setup failed on the account), NO
        # password was ever set, so the JSON must not claim one - a buyer
        # trying to log in with a bogus password would fail and blame the
        # session. Only stamp the password when 2FA is actually active.
        try:
            from bot.data.settings import is_two_factor_enabled as _is_2fa_enabled
            from bot.data.countries import get_country_setting_bool as _cc_twofa
            _cc = country_code or info.get("country_code", "")
            if _cc:
                _2fa_enabled = bool(_cc_twofa(_cc, "twofa_check"))
            else:
                _2fa_enabled = bool(_is_2fa_enabled())
        except Exception:
            _2fa_enabled = True
        _twofa_final = ""
        if _2fa_enabled:
            _twofa_final = (
                twofa_password
                or info.get("twofa_password")
                or info.get("2fa_password")
                or (_config.get_2fa_password() if hasattr(_config, "get_2fa_password") else "")
            )
            if info.get("twofa_enabled") is False:
                _twofa_final = ""
        data = build_session_data_full(
            phone=phone,
            session_info=info,
            session_string=session_string,
            twofa_password=_twofa_final,
            country_code=country_code or info.get("country_code", ""),
            rate=float(rate if rate is not None else (info.get("rate", 0) or 0)),
            account_status=account_status or info.get("account_status", "spam_free"),
            status_details=status_details or info.get("status_details") or {},
            user_id=str(verified_by_user or 0),
            device_info=device_info,
        )
        os.makedirs("verified_sessions", exist_ok=True)
        name = f"{safe}.json" if not suffix else f"{safe}_{suffix}.json"
        json_path = os.path.join("verified_sessions", name)
        with open(json_path, "w", encoding="utf-8") as f:
            f.write(_json.dumps(data, indent=2, ensure_ascii=False, default=str))
        # CRITICAL: the JSON MUST actually reach the channel. The old code
        # ignored the send result and returned True unconditionally, so a
        # failed JSON was silently reported as delivered and the buyer
        # received a session file without its JSON. Retry a few times and
        # only return True when the send is confirmed.
        for attempt in range(3):
            ok = await _send_document(chat_id, json_path, caption, bot_client)
            if ok:
                print(f"📤 JSON file sent alongside session for {phone} -> {name} (attempt {attempt + 1})")
                return True
            logger.error(f"send_json_alongside: send attempt {attempt + 1}/3 FAILED for {phone} -> {name}; retrying...")
            await asyncio.sleep(min(1.5 * (attempt + 1), 4.0))
        logger.error(f"send_json_alongside: JSON FAILED to reach channel after 3 attempts for {phone} -> {name}")
        return False
    except Exception as e:
        logger.error("send_json_alongside FAILED for %s: %s", phone, e)
        return False


async def send_report_to_channel(
    chat_id,
    report: str,
    *,
    session_string: Optional[str] = None,
    phone: Optional[str] = None,
    suffix: str = "",
    bot_client=None,
    user_id=None,
    session_info: Optional[dict] = None,
    attach_session: bool = False,
    attach_json: bool = False,
    twofa_password: str = "",
    country_code: str = "",
    rate=None,
    account_status: str = "",
) -> bool:
    """Send a report to a channel, optionally attaching a session file.

    DEFENSIVE DEFAULT (session+JSON rule): session files and their JSON are
    ONLY produced when BOTH ``attach_session=True`` AND ``attach_json=True``
    are passed explicitly. A caller that merely forwards ``session_string`` /
    ``phone`` (e.g. a rejected/multidevice/pending report path) never ships a
    session file or a JSON by accident - it always falls through to a plain
    text message. The JSON (device-info + session data, the format consumed
    by account-management programs) goes ONLY to the Telethon / Program
    success channels; rejected, multidevice, pending and txt/tdata reports
    must NOT receive a JSON.

    ``session_info``     = account metadata; its ``user_id`` is the ACCOUNT's
                           real Telegram user id (stamped into the Telethon
                           entity and the JSON ``user_id`` field).
    ``user_id``          = the operator who verified the account (goes to the
                           JSON ``verified_by_user`` field).
    ``twofa_password`` / ``country_code`` / ``rate`` / ``account_status`` =
                           the ACCOUNT's real values forwarded into the JSON so
                           the buyer's JSON never carries global defaults.

    All errors are swallowed and logged; ``False`` is returned on total failure.
    """
    if chat_id is None:
        logger.warning("send_report_to_channel: chat_id is None, skipping")
        return False

    from bot.utils.session_handler import session_string_to_telethon
    from bot.services.session_formats import generate_pyrogram_file

    sent = False

    # Session files (and their JSON) are ONLY sent for explicit SUCCESS
    # reporting - never for rejected/multidevice/pending/txt/tdata reports.
    if session_string and phone and attach_session:
        os.makedirs("sessions", exist_ok=True)
        stamp = int(time.time() * 1000)
        safe_phone = str(phone).replace("+", "")
        suffix_part = f"{suffix}_" if suffix else ""

        # The ACCOUNT's real user id: prefer session_info (account data), then
        # parse it from the session string itself (authoritative), and only
        # then fall back to the explicit user_id argument (the operator).
        account_user_id = None
        if isinstance(session_info, dict):
            account_user_id = session_info.get("user_id") or session_info.get("account_id")
        if not account_user_id:
            try:
                from bot.services.session_formats import parse_pyrogram_session
                _parsed = parse_pyrogram_session(session_string or "")
                if _parsed and _parsed.get("user_id"):
                    account_user_id = _parsed["user_id"]
            except Exception:
                pass
        if not account_user_id:
            account_user_id = user_id
        try:
            account_user_id = int(account_user_id or 0) or None
        except (TypeError, ValueError):
            account_user_id = None
        # ``user_id`` is the operator who verified the account (goes to
        # JSON ``verified_by_user``).
        operator_user_id = user_id
        account_first_name = ""
        if isinstance(session_info, dict):
            account_first_name = session_info.get("first_name") or ""

        try:
            temp_telethon_file = f"sessions/{safe_phone}_{stamp}_{suffix_part}telethon.session"
            # Resolve the ACCOUNT's REAL access hash so the delivered Telethon
            # file never carries the placeholder entity hash=0.
            from bot.verification.telethon_export import resolve_user_access_hash
            account_hash = await resolve_user_access_hash(
                session_string, phone, user_id=account_user_id,
            )
            result = await asyncio.to_thread(
                session_string_to_telethon,
                session_string, temp_telethon_file,
                user_id=account_user_id,
                phone=phone,
                first_name=account_first_name,
                access_hash=account_hash,
            )
            if result and os.path.exists(temp_telethon_file):
                if await _send_document(chat_id, temp_telethon_file, report, bot_client):
                    sent = True
                    print(f"📤 Telethon session file sent to {chat_id} for {phone}")
                    if attach_json:
                        json_suffix = f"{suffix}_telethon" if suffix else "telethon"
                        json_ok = await send_json_alongside(chat_id, bot_client, report,
                                                           phone, session_string,
                                                           account_user_id, operator_user_id,
                                                           session_info,
                                                           suffix=json_suffix,
                                                           twofa_password=twofa_password,
                                                           country_code=country_code,
                                                           rate=rate,
                                                           account_status=account_status)
                        if not json_ok:
                            logger.error(f"⚠️ JSON FAILED to reach channel for {phone} -> {chat_id} (session went, JSON did not)")
                else:
                    logger.warning(f"⚠️ Telethon session file send FAILED for {phone} -> {chat_id}")
        except Exception as tel_err:
            logger.warning(f"⚠️ Telethon file error: {tel_err}")

        if not sent:
            try:
                temp_session_file = f"sessions/{safe_phone}_{stamp}_{suffix_part}pyrogram.session"
                # Write a VALID Pyrogram SQLite .session (not a text file).
                temp_session_file = generate_pyrogram_file(
                    session_string, temp_session_file,
                    user_id=account_user_id, phone=phone) or temp_session_file
                if temp_session_file and os.path.exists(temp_session_file):
                    if await _send_document(chat_id, temp_session_file, report, bot_client):
                        sent = True
                        print(f"📤 Session file sent to {chat_id} for {phone}")
                        if attach_json:
                            json_suffix = f"{suffix}_pyrogram" if suffix else "pyrogram"
                            json_ok = await send_json_alongside(chat_id, bot_client, report,
                                                               phone, session_string,
                                                               account_user_id, operator_user_id,
                                                               session_info,
                                                               suffix=json_suffix,
                                                               twofa_password=twofa_password,
                                                               country_code=country_code,
                                                               rate=rate,
                                                               account_status=account_status)
                            if not json_ok:
                                logger.error(f"⚠️ JSON FAILED to reach channel for {phone} -> {chat_id} (session went, JSON did not)")
                    else:
                        logger.warning(f"⚠️ Session file send FAILED for {phone} -> {chat_id}")
            except Exception as pyr_err:
                logger.warning(f"⚠️ Session file error: {pyr_err}")

    if not sent:
        try:
            if bot_client is not None:
                result = await bot_client.send_message(chat_id=chat_id, text=report)
                sent = result is not None
            else:
                sent = (await send_to_channel_with_retry(chat_id=chat_id, message=report)) is not None
        except Exception as msg_err:
            sent = False
            logger.warning(f"⚠️ Channel report error: {msg_err}")

    return sent
