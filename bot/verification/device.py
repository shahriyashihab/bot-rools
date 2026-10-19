"""
Device count / multi-device detection.
=====================================================================
Queries GetAuthorizations for a session to count logged-in devices and
detect whether the freshly-created session is the only one (SUCCESS) or
whether extra devices are active (MULTI-DEVICE / 24h wait).
"""

import asyncio
import re
import time
from datetime import datetime
from typing import Optional

from pyrogram.client import Client

from bot.constants import is_raw_api_available
from bot.utils.device_generator import generate_device_info
from bot.utils.net_timeouts import (
    connect_with_timeout,
    invoke_with_timeout,
    op_with_timeout,
    teardown_with_timeout,
)


# ============================================================
# Definitive dead-session error classification (exact names)
# ============================================================
# The old substring matching ("AUTH_KEY" in text, "INVALID" in text,
# "UNREGISTERED" in text) rejected GOOD sessions whenever an unrelated error
# happened to contain the fragment (e.g. API_ID_INVALID, PHONE_CODE_INVALID,
# API_ID_PUBLISHED_FLOOD). This classifier matches WHOLE RPC error NAMES only.
_DEAD_SESSION_RPC_NAMES = frozenset({
    # Session/key is dead (the account may still be fine, but THIS session is
    # unusable - both mean the delivered verdict is definitive).
    "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_INVALID",
    "AUTH_KEY_DUPLICATED",
    "AUTH_KEY_PERM_EMPTY",
    "SESSION_REVOKED",
    "SESSION_EXPIRED",
    # Account-level death.
    "USER_DEACTIVATED",
    "USER_DEACTIVATED_BAN",
    "USER_DELETED",
    "PHONE_NUMBER_BANNED",
    "PHONE_NUMBER_UNOCCUPIED",
    "PHONE_NUMBER_UNREGISTERED",
})

# Fallback markers for error texts that carry no parseable RPC name. Only
# UNAMBIGUOUS phrases qualify - never bare "INVALID"/"BANNED"/"UNREGISTERED".
_DEAD_SESSION_TEXT_MARKERS = (
    "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_INVALID",
    "AUTH_KEY_DUPLICATED",
    "SESSION_REVOKED",
    "SESSION_EXPIRED",
    "USER_DEACTIVATED",
    "USER_DELETED",
    "PHONE_NUMBER_BANNED",
    "PHONE_NUMBER_UNOCCUPIED",
    "PHONE_NUMBER_UNREGISTERED",
)

_RPC_NAME_RE = re.compile(r"(?:\[\d+\s+|(?<=\n)|^)([A-Z][A-Z0-9_]{4,})\b")


def _rpc_error_name(exc) -> str:
    """Extract the RPC error NAME from a pyrogram exception ('' if unknown)."""
    for attr in ("name", "NAME", "id", "ID"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    match = _RPC_NAME_RE.search(str(exc))
    if match:
        return match.group(1).upper()
    return ""


def classify_dead_session_error(exc) -> Optional[str]:
    """Return the RPC error name when the error DEFINITIVELY means the
    session/account is dead, else None (transient/indeterminate)."""
    if exc is None:
        return None
    name = _rpc_error_name(exc)
    if name in _DEAD_SESSION_RPC_NAMES:
        return name
    text = str(exc).upper()
    for marker in _DEAD_SESSION_TEXT_MARKERS:
        if marker in text:
            return marker
    return None


def _dead_session_error_code(rpc_name: str) -> str:
    """Map a dead-session RPC name to the caller-facing error code."""
    if rpc_name.startswith("AUTH_KEY") or rpc_name in ("SESSION_REVOKED", "SESSION_EXPIRED"):
        return "AUTH_KEY_INVALID"
    if rpc_name in ("USER_DEACTIVATED", "USER_DEACTIVATED_BAN", "USER_DELETED", "PHONE_NUMBER_BANNED"):
        return "USER_BANNED"
    return "UNREGISTERED"


async def run_device_check(
    client: Client,
    phone: str,
    session_string: str,
    session_api_id,
    session_api_hash,
    is_session_valid: bool = True,
    country_code: Optional[str] = None,
    preferred_proxy_url: Optional[str] = None,
) -> tuple:
    """
    Count devices authorized on the session and flag extra devices.
    
    Returns:
        (is_session_valid, has_extra_devices, device_count,
         extra_devices_list, device_check_error, device_check_duration)
    """
    device_check_start = time.monotonic()
    device_check_error = None
    has_extra_devices = False
    extra_devices_list = []
    device_count = 1
    try:
        device_check_client = None
        try:
            await op_with_timeout(client.get_me(), "device-check-get-me")
            device_check_client = client
            print(f"✅ Original client connected for device check")
        except Exception:
            # P7-75: the device profile is derived from the phone so the SAME
            # auth key always presents the SAME fingerprint on every step.
            device_info = generate_device_info(seed=phone, country_code=country_code)
            if country_code:
                def _make_dc_client(proxy):
                    kwargs = dict(
                        name=f"device_check_{phone}_{int(datetime.now().timestamp())}",
                        api_id=session_api_id,
                        api_hash=session_api_hash,
                        session_string=session_string,
                        in_memory=True,
                        device_model=device_info["device_model"],
                        system_version=device_info["system_version"],
                        app_version="Telegram Android 10.14.5",
                    )
                    if proxy:
                        kwargs["proxy"] = proxy
                    return Client(**kwargs)
                from bot.utils.proxy_connector import connect_with_proxy_chain
                device_check_client = await connect_with_proxy_chain(
                    _make_dc_client, country_code, f"device-check:{phone}",
                    preferred_url=preferred_proxy_url,
                    allow_breaker_self_heal=False,
                )
            else:
                device_check_client = Client(
                    name=f"device_check_{phone}_{int(datetime.now().timestamp())}",
                    api_id=session_api_id,
                    api_hash=session_api_hash,
                    session_string=session_string,
                    in_memory=True,
                    device_model=device_info["device_model"],
                    system_version=device_info["system_version"],
                    app_version="Telegram Android 10.14.5",
                )
                await connect_with_timeout(device_check_client, "device-check")
            print(f"✅ New client created for device check (using same API)")
        
        try:
            auth_sessions = None
            device_check_error = None
            max_retries = 3
            
            for attempt in range(max_retries):
                try:
                    if not is_raw_api_available():
                        pyro_ver = __import__('pyrogram').__version__
                        print(f"⚠️ GetAuthorizations requires Pyrogram/Pyrofork v2.1+ (current: v{pyro_ver})")
                        print(f"⚠️ CRITICAL: Multi-device detection will NOT work until upgraded!")
                        print(f"⚠️ Run: pip install --upgrade pyrofork")
                        device_check_error = "PYROGRAM_VERSION_TOO_LOW"
                        break
                    
                    from pyrogram.raw.functions.account.get_authorizations import GetAuthorizations
                    auth_sessions = await invoke_with_timeout(device_check_client, GetAuthorizations(), "device-check-auths")
                    print(f"✅ GetAuthorizations succeeded on attempt {attempt + 1}/{max_retries}")
                    break  # Success!
                    
                except (AttributeError, ImportError) as import_err:
                    print(f"⚠️ GetAuthorizations import error (attempt {attempt + 1}/{max_retries}): {import_err}")
                    device_check_error = "IMPORT_ERROR"
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                    else:
                        print(f"❌ GetAuthorizations unavailable after {max_retries} attempts")
                        print(f"⚠️ Please install: pip install pyrofork")
                        break
                        
                except Exception as api_err:
                    error_str = str(api_err).upper()
                    print(f"⚠️ GetAuthorizations API error (attempt {attempt + 1}/{max_retries}): {api_err}")

                    # P2-19: classify by EXACT RPC error name. Only a
                    # definitive dead-session error invalidates the session;
                    # "INVALID"-fragment matches (API_ID_INVALID etc.) and
                    # unknown errors are transient - retried, never guessed.
                    dead = classify_dead_session_error(api_err)
                    if dead:
                        print(f"❌ Definitive dead-session error ({dead}) - session TRULY invalid")
                        is_session_valid = False
                        device_check_error = _dead_session_error_code(dead)
                        break  # No retry - session is dead

                    elif "FLOOD_WAIT" in error_str or "FLOODWAIT" in error_str:
                        print(f"⚠️ FLOOD_WAIT - Rate limited, waiting...")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(3)
                        else:
                            device_check_error = "FLOOD_WAIT"
                            print(f"⚠️ Device check failed due to flood wait")

                    elif "NETWORK" in error_str or "TIMEOUT" in error_str or "CONNECTION" in error_str:
                        print(f"⚠️ NETWORK/TIMEOUT - Temporary error, retrying...")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2)
                        else:
                            device_check_error = "NETWORK_ERROR"
                            print(f"⚠️ Device check failed due to network error")

                    else:
                        # Unknown/unclassified error (includes everything that
                        # merely CONTAINS "invalid"/"unregistered" fragments):
                        # transient for this purpose - retry, NEVER invalidate.
                        print(f"⚠️ Unclassified error (treated as transient): {api_err}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(1)
                        else:
                            device_check_error = "UNKNOWN_ERROR"
                            print(f"⚠️ Device check failed after {max_retries} attempts (no definitive verdict)")
            
            # Process results if successful
            if auth_sessions and hasattr(auth_sessions, 'authorizations'):
                device_count = len(auth_sessions.authorizations)
                print(f"📱 Device count: {device_count}")
                if device_count > 1:
                    has_extra_devices = True
                    for auth in auth_sessions.authorizations:
                        device_model = getattr(auth, 'device_model', 'Unknown')
                        app_name = getattr(auth, 'app_name', 'Unknown')
                        extra_devices_list.append({
                            "hash": auth.hash,
                            "device": f"{device_model} - {app_name}",
                            "location": getattr(auth, 'country', 'Unknown'),
                            "created": datetime.fromtimestamp(auth.date_created).strftime("%Y-%m-%d %H:%M:%S"),
                            "is_current": auth.current
                        })
                    print(f"⚠️ EXTRA DEVICES DETECTED: {device_count} devices")
        except Exception as auth_err:
            print(f"⚠️ Device check internal error (not session-critical, likely network): {auth_err}")
            device_check_error = device_check_error or "UNKNOWN_DEVICE_CHECK_ERROR"
            # Do NOT set is_session_valid=False - network errors don't invalidate sessions
        
        if device_check_client and device_check_client != client:
            try:
                await teardown_with_timeout(device_check_client, "device-check")
            except Exception:
                pass  # Disconnect may fail if already disconnected
    except Exception as check_err:
        print(f"⚠️ Device check connection error (not session-critical, likely network): {check_err}")
        device_check_error = device_check_error or "DEVICE_CHECK_CONNECTION_ERROR"
        # Do NOT set is_session_valid=False - connection errors don't invalidate sessions
    
    device_check_duration = time.monotonic() - device_check_start
    print(f"✅ Device check completed in {device_check_duration:.3f}s")
    
    return (
        is_session_valid,
        has_extra_devices,
        device_count,
        extra_devices_list,
        device_check_error,
        device_check_duration,
    )


VERDICT_SUCCESS = "SUCCESS"
VERDICT_MULTI_DEVICE = "MULTI-DEVICE"
VERDICT_REJECTED = "REJECTED"
VERDICT_UNKNOWN = "UNKNOWN"


def _build_extra_devices_list(auth_sessions) -> list:
    """Extract the non-current (extra) authorized devices from GetAuthorizations."""
    extra_devices_list = []
    for auth in auth_sessions.authorizations:
        if getattr(auth, 'current', False):
            continue
        device_model = getattr(auth, 'device_model', 'Unknown')
        app_name = getattr(auth, 'app_name', 'Unknown')
        extra_devices_list.append({
            "hash": auth.hash,
            "device": f"{device_model} - {app_name}",
            "location": getattr(auth, 'country', 'Unknown'),
            "created": datetime.fromtimestamp(auth.date_created).strftime("%Y-%m-%d %H:%M:%S"),
            "is_current": auth.current,
        })
    return extra_devices_list


async def live_device_verdict(
    session_string: str,
    api_id,
    api_hash,
    phone: str,
    max_attempts: int = 2,
    country_code: Optional[str] = None,
    preferred_proxy_url: Optional[str] = None,
) -> dict:
    """Authoritative, guess-free live device verdict at DECISION time.

    Creates a fresh in-memory client and runs ``GetAuthorizations`` RIGHT NOW.
    This is the only check that may decide a session's final outcome, because
    it runs at the moment the verdict is needed (deadline / monitor cycle /
    recovery), NOT seconds after OTP submit while the user's phone is still
    logged in.

    Returns a dict:
        {"verdict", "device_count", "extra_devices", "error"}

    Verdict semantics (100% live-check-based, never guessed):
        SUCCESS        - GetAuthorizations succeeded and the ONLY authorized
                         device is this session's own client (no extra device).
        MULTI-DEVICE   - GetAuthorizations succeeded and at least one OTHER
                         device is currently authorized on the account.
        REJECTED       - GetAuthorizations returned a definitive account-level
                         error: auth key unregistered/revoked, or the account
                         is banned/deactivated/deleted.
        UNKNOWN        - No definitive answer was obtainable (transient network
                         error, flood wait, pyrogram too old, etc). Callers MUST
                         treat this as "do not decide" and keep the session
                         pending for a later retry. NEVER map UNKNOWN to SUCCESS
                         or REJECTED.
    """
    temp_client = None
    try:
        # P7-75: deterministic per-phone device fingerprint.
        device_info = generate_device_info(seed=phone)

        def _make_verdict_client(proxy):
            kwargs = dict(
                name=f"live_device_{phone}_{int(datetime.now().timestamp())}",
                api_id=api_id,
                api_hash=api_hash,
                session_string=session_string,
                in_memory=True,
                device_model=device_info["device_model"],
                system_version=device_info["system_version"],
                app_version="Telegram Android 10.14.5",
            )
            if proxy:
                kwargs["proxy"] = proxy
            return Client(**kwargs)

        from bot.utils.proxy_connector import connect_with_proxy_chain
        temp_client = await connect_with_proxy_chain(
            _make_verdict_client, country_code, f"live-device-verdict:{phone}",
            preferred_url=preferred_proxy_url,
            allow_breaker_self_heal=False,
        )
    except Exception as connect_err:
        print(f"🔍 LIVE VERDICT connect error for {phone}: {connect_err}")
        return {
            "verdict": VERDICT_UNKNOWN,
            "device_count": 0,
            "extra_devices": [],
            "error": f"CONNECTION_ERROR: {connect_err}",
        }

    try:
        if not is_raw_api_available():
            pyro_ver = __import__('pyrogram').__version__
            print(f"⚠️ LIVE VERDICT: GetAuthorizations requires Pyrogram/Pyrofork v2.1+ (current: v{pyro_ver})")
            return {
                "verdict": VERDICT_UNKNOWN,
                "device_count": 0,
                "extra_devices": [],
                "error": "PYROGRAM_VERSION_TOO_LOW",
            }

        from pyrogram.raw.functions.account.get_authorizations import GetAuthorizations

        last_error = None
        for attempt in range(max_attempts):
            try:
                auth_sessions = await invoke_with_timeout(temp_client, GetAuthorizations(), "live-verdict-auths")
                extra_devices_list = _build_extra_devices_list(auth_sessions)
                device_count = len(auth_sessions.authorizations)
                if extra_devices_list:
                    print(f"📱 LIVE VERDICT for {phone}: {len(extra_devices_list)} EXTRA device(s), total={device_count}")
                    return {
                        "verdict": VERDICT_MULTI_DEVICE,
                        "device_count": device_count,
                        "extra_devices": extra_devices_list,
                        "error": None,
                    }
                print(f"✅ LIVE VERDICT for {phone}: only this session (total={device_count}) -> SUCCESS")
                return {
                    "verdict": VERDICT_SUCCESS,
                    "device_count": device_count,
                    "extra_devices": [],
                    "error": None,
                }
            except Exception as api_err:
                error_str = str(api_err).upper()
                last_error = error_str
                # P2-19: definitive account-level errors are identified by
                # EXACT RPC error NAME - a bare "INVALID"/"UNREGISTERED"/
                # "AUTH_KEY" fragment can no longer produce a false REJECT.
                dead = classify_dead_session_error(api_err)
                if dead:
                    error_code = _dead_session_error_code(dead)
                    print(f"❌ LIVE VERDICT for {phone}: {dead} -> REJECTED ({error_code})")
                    return {
                        "verdict": VERDICT_REJECTED,
                        "device_count": 0,
                        "extra_devices": [],
                        "error": error_code,
                    }
                # Everything else is transient/indeterminate: NEVER guess.
                print(f"⚠️ LIVE VERDICT for {phone} attempt {attempt + 1}/{max_attempts} error: {api_err}")
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2)

        print(f"🛡️ LIVE VERDICT for {phone}: no definitive answer -> UNKNOWN ({last_error})")
        return {
            "verdict": VERDICT_UNKNOWN,
            "device_count": 0,
            "extra_devices": [],
            "error": f"TRANSIENT_ERROR: {last_error}",
        }
    finally:
        if temp_client:
            try:
                await teardown_with_timeout(temp_client, "live-verdict")
            except Exception:
                pass


async def recheck_devices_at_deadline(
    session_string: str,
    api_id,
    api_hash,
    phone: str,
) -> tuple:
    """Fresh device-count re-check with a temporary client (deadline upgrade).

    Backwards-compatible wrapper over :func:`live_device_verdict` kept for any
    legacy callers. Returns ``(is_session_valid, has_extra_devices,
    device_count, extra_devices_list, device_check_error,
    device_check_duration)``.

    The verdict is 100% live-check based - there is NO conservative guessing.
    A definitive account error maps to ``is_session_valid=False``
    (``device_check_error`` set), and a transient/unknown result maps to
    ``device_check_error="DEVICE_CHECK_UNKNOWN"`` so callers that feed it into
    ``determine_final_message_type`` receive UNKNOWN (never a guessed
    SUCCESS/MULTI-DEVICE/REJECTED).
    """
    result = await live_device_verdict(
        session_string=session_string,
        api_id=api_id,
        api_hash=api_hash,
        phone=phone,
    )
    verdict = result["verdict"]
    if verdict == VERDICT_SUCCESS:
        return (True, False, 1, [], None, 0.0)
    if verdict == VERDICT_MULTI_DEVICE:
        return (
            True,
            True,
            int(result.get("device_count", 2) or 2),
            result.get("extra_devices", []) or [],
            None,
            0.0,
        )
    if verdict == VERDICT_REJECTED:
        return (False, False, 1, [], result.get("error") or "AUTH_KEY_INVALID", 0.0)
    return (True, False, 1, [], "DEVICE_CHECK_UNKNOWN", 0.0)
