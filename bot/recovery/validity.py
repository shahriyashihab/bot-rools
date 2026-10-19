"""Refactored from bot.utils.pending_recovery: Session validity and 2FA verification for recovery."""
import asyncio
import time
from typing import Dict, Optional, Tuple

from pyrogram.client import Client
from pyrogram.errors import AuthKeyUnregistered, FloodWait, SessionRevoked, UserDeactivated

from bot.utils.device_generator import generate_device_info
from bot.utils.net_timeouts import (
    call_with_timeout,
    connect_with_timeout,
    invoke_with_timeout,
    op_with_timeout,
)
from bot.utils.storage import get_country_setting_bool
from bot.verification.spam_check import classify_spambot_response
from bot.verification.device import classify_dead_session_error, _dead_session_error_code


async def _disconnect(client, label: str = "recovery-validity") -> None:
    """Bounded, never-raising teardown.

    ``client.disconnect()`` has NO internal timeout. Every disconnect in this
    module sits on the path that resolves a pending session, so a socket
    teardown that never returns left the account permanently unfinalized even
    though the validity verdict was already computed.
    """
    try:
        await call_with_timeout(client.disconnect(), f"disconnect:{label}")
    except Exception:
        pass


async def setup_2fa_for_recovery(client: Client, phone: str, twofa_password: str) -> Tuple[bool, str]:
    """
    Setup 2FA during recovery if not already done.
    
    Returns:
        (success, message)
    """
    try:
        for attempt in range(3):
            try:
                await op_with_timeout(client.enable_cloud_password(twofa_password), "recovery-2fa-enable")
                print(f"✅ RECOVERY 2FA: Successfully enabled for {phone}")
                return True, "2FA enabled"
            except FloodWait as e:
                wait_time = int(getattr(e, 'value', 60) or 60)
                if wait_time <= 60:
                    await asyncio.sleep(wait_time)
                else:
                    print(f"⚠️ RECOVERY 2FA: FloodWait too long: {wait_time}s for {phone}")
                    return False, f"FloodWait {wait_time}s"
            except Exception as e:
                error_str = str(e).upper()
                if "PASSWORD_HASH_INVALID" in error_str or "SRP_ID_INVALID" in error_str:
                    print(f"✅ RECOVERY 2FA: Already has 2FA for {phone}")
                    return True, "2FA already exists"
                if "FRESH_CHANGE_PASSWORD_FORBIDDEN" in error_str:
                    print(f"⚠️ RECOVERY 2FA: Fresh session, 2FA restricted for {phone}")
                    return True, "2FA restricted on fresh session"
                if attempt < 2:
                    await asyncio.sleep(2)
                else:
                    print(f"⚠️ RECOVERY 2FA: Failed for {phone}: {e}")
                    return False, str(e)
        return False, "Max attempts reached"
    except Exception as e:
        print(f"❌ RECOVERY 2FA: Error for {phone}: {e}")
        return False, str(e)

async def check_session_validity_with_2fa(
    session_string: str, 
    api_id: int, 
    api_hash: str,
    twofa_password: str,
    twofa_already_done: bool = False,
    phone: str = "UNKNOWN",
    country_code: Optional[str] = None,
    preferred_proxy_url: Optional[str] = None
) -> Tuple[bool, str, Dict]:
    """
    ENHANCED: Check session validity AND setup 2FA if not done.
    
    Full recovery verification:
    1. Check session validity
    2. Setup 2FA (if not already done)
    3. Check spam status
    4. Check multi-device
    
    ``country_code`` is optional: when provided the recovery client uses the
    country's proxy chain (pool -> default -> direct); None = direct, exactly
    as before.
    
    Returns:
        (is_valid, spam_status, details)
    """
    details = {
        "device_count": 1,
        "has_extra_devices": False,
        "spam_status": "limit",
        "is_valid": True,
        "error": None,
        "twofa_setup": False,
        "twofa_result": None,
        "device_check_error": None
    }
    
    temp_client = None

    try:
        # P7-75: deterministic per-phone device fingerprint.
        device_info = generate_device_info(seed=phone, country_code=country_code)

        def _make_recovery_client(proxy):
            kwargs = dict(
                name=f"recovery_full_{int(time.time() * 1000)}",
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
            _make_recovery_client, country_code, f"recovery-validity:{phone}",
            preferred_url=preferred_proxy_url,
            allow_breaker_self_heal=False,
        )
        
        try:
            me = await op_with_timeout(temp_client.get_me(), "recovery-validity-get-me")
            if not me:
                details["is_valid"] = False
                details["error"] = "Account not found"
                await _disconnect(temp_client)
                return False, "invalid", details
        except Exception as e:
            # P2-19/P4-44: exact RPC-error-NAME classification. The old
            # substring matching ("AUTH_KEY"/"INVALID"/"BANNED"/...) could
            # reject a GOOD account when an unrelated error merely contained
            # the fragment (e.g. API_ID_INVALID, PHONE_CODE_INVALID). Only a
            # definitive dead-session error invalidates the session; a
            # transport/network failure must NOT produce a rejected verdict.
            if classify_dead_session_error(e):
                details["is_valid"] = False
                details["error"] = str(e)
                try:
                    await _disconnect(temp_client)
                except:
                    pass
                return False, "invalid", details
        
        if not twofa_already_done:
            if not get_country_setting_bool(country_code, "twofa_check"):
                print(f"🔕 TWO FACTOR DISABLED: Skipping 2FA setup for {phone}")
                details["twofa_setup"] = True
                details["twofa_result"] = "2FA disabled by admin"
                # P4-46b: 2FA is OFF but the CORE CONFIG profile is a separate
                # feature - the client is live right now, so apply the country's
                # Name/Bio/Photo here too. Without this, a 2FA-disabled country
                # finalized through recovery would ship with a default profile.
                _apply_profile = True
            else:
                print(f"🔐 RECOVERY: Setting up 2FA for {phone}...")
                twofa_success, twofa_msg = await setup_2fa_for_recovery(temp_client, phone, twofa_password)
                details["twofa_setup"] = twofa_success
                details["twofa_result"] = twofa_msg
                # Profile only after a CONFIRMED 2FA (or when 2FA is disabled
                # above) - never on a failed/unprovisioned 2FA.
                _apply_profile = twofa_success
            if _apply_profile:
                try:
                    from bot.services.country_profile import apply_country_profile
                    _applied = await apply_country_profile(temp_client, country_code, phone)
                    if _applied:
                        details["applied_profile"] = dict(_applied)
                        if _applied.get("name"):
                            try:
                                _me = await op_with_timeout(
                                    temp_client.get_me(), "recovery-profile-refresh-me")
                                if _me is not None:
                                    details["first_name"] = (getattr(_me, "first_name", "") or "")
                                    details["last_name"] = (getattr(_me, "last_name", "") or "")
                            except Exception as _refresh_err:
                                print(f"⚠️ RECOVERY PROFILE: could not refresh account info: {_refresh_err}")
                        print(f"👤 RECOVERY PROFILE: applied for {phone} ({country_code}): {_applied}")
                except Exception as _profile_err:
                    print(f"⚠️ RECOVERY PROFILE: could not apply profile for {phone}: {_profile_err}")
        else:
            details["twofa_setup"] = True
            details["twofa_result"] = "Already completed before restart"
            print(f"✅ RECOVERY: 2FA was already done for {phone}")
        
        try:
            if not get_country_setting_bool(country_code, "accept_spam"):
                # CORE CONFIG: Accept Spam OFF for this country - skip the
                # @SpamBot check entirely and apply the full (spam_free) rate.
                details["spam_status"] = "spam_free"
                print(f"🔕 RECOVERY: Accept Spam OFF for {phone} ({country_code}) - using spam_free rate")
            else:
                print(f"🔍 RECOVERY: Checking spam status for {phone}...")
                await op_with_timeout(temp_client.send_message("spambot", "/start"), "recovery-spambot-start")

                # A single 2.5s sleep could miss @SpamBot's reply on a slow DC or a
                # congested network, silently defaulting a spam-free account to
                # "limit". Poll for the bot's reply up to 4 times (with a small
                # delay) so the response has a realistic chance of being read.
                messages = []
                for _poll in range(4):
                    await asyncio.sleep(2.0)
                    try:
                        async for msg in temp_client.get_chat_history("spambot", limit=3):
                            if msg.from_user and msg.from_user.is_bot and (msg.text or "").strip():
                                messages.append(msg.text or "")
                        if messages:
                            break
                    except Exception:
                        pass

                if messages:
                    verdict = classify_spambot_response(" ".join(messages))
                    if verdict == "clean":
                        details["spam_status"] = "spam_free"
                        print(f"✅ RECOVERY: {phone} is SPAM FREE")
                    elif verdict == "limited":
                        details["spam_status"] = "limit"
                        print(f"⚠️ RECOVERY: {phone} has LIMIT")
                    elif verdict == "spam":
                        details["spam_status"] = "spam"
                        print(f"❌ RECOVERY: {phone} is SPAM")
                    else:
                        # Unrecognised reply (unknown language): conservative
                        # middle classification, never the old English-only
                        # "anything else = spam" false positive.
                        details["spam_status"] = "limit"
                        print(f"⚠️ RECOVERY: {phone} SpamBot reply unrecognised - conservative limit")
        except Exception as spam_err:
            print(f"⚠️ RECOVERY: Spam check failed for {phone}: {spam_err}")
            details["spam_status"] = "limit"
        
        if get_country_setting_bool(country_code, "device_check"):
            try:
                print(f"📱 RECOVERY: Checking multi-device for {phone}...")
                auth_sessions = None
                device_check_error = None
                max_retries = 3

                for attempt in range(max_retries):
                    try:
                        from pyrogram.raw.functions.account.get_authorizations import GetAuthorizations
                        auth_sessions = await invoke_with_timeout(
                            temp_client, GetAuthorizations(),
                            f"recovery-authorizations:{phone}",
                        )
                        print(f"✅ RECOVERY: GetAuthorizations succeeded on attempt {attempt + 1}/{max_retries}")
                        break  # Success!

                    except (ImportError, AttributeError) as raw_err:
                        print(f"⚠️ RECOVERY: GetAuthorizations import error (attempt {attempt + 1}/{max_retries}): {raw_err}")
                        print(f"⚠️ RECOVERY: Pyrogram/Pyrofork v2.1+ required")
                        print(f"⚠️ Please install: pip install pyrofork")
                        device_check_error = "PYROGRAM_VERSION_TOO_LOW"
                        break  # Cannot retry import error

                    except Exception as invoke_err:
                        error_str = str(invoke_err).upper()
                        print(f"⚠️ RECOVERY: Device check API error (attempt {attempt + 1}/{max_retries}): {invoke_err}")

                        # P2-19: exact-name classification. Only definitive
                        # dead-session errors terminate; everything else
                        # (including bare "UNREGISTERED"-fragment matches) is
                        # retried and left inconclusive - never a guessed
                        # invalid verdict.
                        dead = classify_dead_session_error(invoke_err)
                        if dead:
                            print(f"❌ RECOVERY: {dead} - session/account definitively invalid")
                            details["is_valid"] = False
                            details["error"] = str(invoke_err)
                            device_check_error = _dead_session_error_code(dead)
                            break  # No retry - session is dead

                        elif "FLOOD_WAIT" in error_str or "FLOODWAIT" in error_str:
                            print(f"⚠️ RECOVERY: FLOOD_WAIT - Rate limited")
                            if attempt < max_retries - 1:
                                await asyncio.sleep(3)
                            else:
                                device_check_error = "FLOOD_WAIT"
                                print(f"⚠️ RECOVERY: Device check failed due to flood wait")

                        elif "NETWORK" in error_str or "TIMEOUT" in error_str or "CONNECTION" in error_str:
                            print(f"⚠️ RECOVERY: NETWORK/TIMEOUT - Temporary error")
                            if attempt < max_retries - 1:
                                await asyncio.sleep(2)
                            else:
                                device_check_error = "NETWORK_ERROR"
                                print(f"⚠️ RECOVERY: Device check failed due to network error")

                        else:
                            print(f"⚠️ RECOVERY: Unclassified error (treated as transient): {invoke_err}")
                            if attempt < max_retries - 1:
                                await asyncio.sleep(1)
                            else:
                                device_check_error = "UNKNOWN_ERROR"
                                print(f"⚠️ RECOVERY: Device check failed after {max_retries} attempts (no definitive verdict)")

                if auth_sessions and hasattr(auth_sessions, 'authorizations'):
                    details["device_count"] = len(auth_sessions.authorizations)
                    if details["device_count"] > 1:
                        details["has_extra_devices"] = True
                        print(f"📱 RECOVERY: {phone} has {details['device_count']} devices (MULTI-DEVICE)")
                    else:
                        print(f"✅ RECOVERY: {phone} has only 1 device")

                # Propagate the device-check outcome so the final validation can
                # classify inconclusive failures as UNKNOWN (pending retry) - only
                # definitive AUTH_KEY_INVALID / USER_BANNED remain REJECTED.
                details["device_check_error"] = device_check_error
            except Exception as device_err:
                print(f"⚠️ RECOVERY: Multi-device check exception for {phone}: {device_err}")
                # Outer exception - should not happen with inner retry logic
        else:
            print(f"📱 DEVICE CHECK DISABLED: Skipping device check for {phone}")
        
        try:
            await _disconnect(temp_client)
        except:
            pass
        
        return details["is_valid"], details["spam_status"], details
        
    except (AuthKeyUnregistered, UserDeactivated, SessionRevoked) as e:
        details["is_valid"] = False
        details["error"] = str(e)
        return False, "invalid", details
    except FloodWait as e:
        wait_time = getattr(e, 'value', 30) or 30
        print(f"⚠️ RECOVERY: FloodWait {wait_time}s for {phone}")
        details["error"] = f"FloodWait {wait_time}s"
        # FloodWait is a transient rate limit, NOT proof the session is dead.
        # Mark the check inconclusive so the final validation maps it to
        # UNKNOWN -> the session stays pending for a later monitor retry.
        details["device_check_error"] = "FLOOD_WAIT"
        return True, "flood_wait", details
    except Exception as e:
        # Transient/unexpected error (network blip, connection refused, timeout).
        # This is NOT proof the session is dead: the typed dead-session
        # exceptions (AuthKeyUnregistered / UserDeactivated / SessionRevoked)
        # are handled above. Mark the check inconclusive so the final validation
        # maps it to UNKNOWN -> the session stays pending for a later monitor
        # retry. NEVER map a transient error to REJECTED.
        print(f"⚠️ RECOVERY: Session check transient error for {phone}: {e}")
        details["error"] = str(e)
        details["device_check_error"] = "NETWORK_ERROR"
        details["twofa_setup"] = None  # 2FA was never determined - do not report as failed
        return True, "error", details
    finally:
        if temp_client:
            try:
                await _disconnect(temp_client)
            except:
                pass

async def check_session_validity(session_string: str, api_id: int, api_hash: str, country_code: Optional[str] = None, preferred_proxy_url: Optional[str] = None) -> Tuple[bool, str, Dict]:
    """
    Check if a session is still valid and get spam/device status.
    (Legacy function - kept for compatibility)
    
    ``country_code`` is optional: when provided the recovery client uses the
    country's proxy chain (pool -> default -> direct); None = direct, exactly
    as before.
    
    Returns:
        (is_valid, spam_status, details)
    """
    details = {
        "device_count": 1,
        "has_extra_devices": False,
        "spam_status": "limit",
        "is_valid": True,
        "error": None
    }
    
    temp_client = None
    try:
        # P7-75: deterministic fingerprint (legacy path has no phone param -
        # seed from the session string, stable for this account).
        device_info = generate_device_info(seed=session_string[:24], country_code=country_code)

        def _make_legacy_client(proxy):
            kwargs = dict(
                name=f"recovery_check_{int(time.time() * 1000)}",
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
            _make_legacy_client, country_code, "recovery-check",
            preferred_url=preferred_proxy_url,
            allow_breaker_self_heal=False,
        )
        
        try:
            me = await op_with_timeout(temp_client.get_me(), "recovery-validity-get-me")
            if not me:
                details["is_valid"] = False
                details["error"] = "Account not found"
                await _disconnect(temp_client)
                return False, "invalid", details
        except Exception as e:
            # P2-19: exact RPC error names only (legacy check_session_validity
            # path) - fragment matches such as "INVALID" must never mark a
            # good session invalid.
            if classify_dead_session_error(e):
                details["is_valid"] = False
                details["error"] = str(e)
                try:
                    await _disconnect(temp_client)
                except:
                    pass
                return False, "invalid", details
        
        try:
            await op_with_timeout(temp_client.send_message("spambot", "/start"), "recovery-spambot-start")
            await asyncio.sleep(2)
            
            messages = []
            try:
                async for msg in temp_client.get_chat_history("spambot", limit=3):
                    if msg.from_user and msg.from_user.is_bot:
                        messages.append(msg.text or "")
            except:
                pass
            
            if messages:
                verdict = classify_spambot_response(" ".join(messages))
                if verdict == "clean":
                    details["spam_status"] = "spam_free"
                elif verdict == "limited":
                    details["spam_status"] = "limit"
                else:
                    details["spam_status"] = "spam"
        except Exception as spam_err:
            print(f"⚠️ RECOVERY: Spam check failed: {spam_err}")
            details["spam_status"] = "limit"
        
        try:
            from pyrogram.raw.functions.account.get_authorizations import GetAuthorizations
            auth_sessions = await invoke_with_timeout(
                temp_client, GetAuthorizations(), f"recovery-devices:{phone}"
            )
            
            if auth_sessions and hasattr(auth_sessions, 'authorizations'):
                details["device_count"] = len(auth_sessions.authorizations)
                if details["device_count"] > 1:
                    details["has_extra_devices"] = True
        except Exception as device_err:
            # P2-19/P4-44: exact-name classification only. A bare
            # "INVALID"/"BANNED"/"AUTH_KEY" fragment (e.g. API_ID_INVALID,
            # PHONE_CODE_INVALID) must never mark a good session invalid.
            if classify_dead_session_error(device_err):
                details["is_valid"] = False
                details["error"] = str(device_err)
                try:
                    await _disconnect(temp_client)
                except:
                    pass
                return False, "invalid", details
        
        try:
            await _disconnect(temp_client)
        except:
            pass
        
        return details["is_valid"], details["spam_status"], details
        
    except (AuthKeyUnregistered, UserDeactivated, SessionRevoked) as e:
        details["is_valid"] = False
        details["error"] = str(e)
        return False, "invalid", details
    except FloodWait as e:
        wait_time = getattr(e, 'value', 30) or 30
        print(f"⚠️ RECOVERY: FloodWait {wait_time}s - marking as unverified")
        details["error"] = f"FloodWait {wait_time}s"
        details["is_valid"] = False
        return False, "flood_wait", details
    except Exception as e:
        # P2-19/P4-44: exact-name classification only. A network/transport
        # error (dead proxy chain, connect timeout) must NEVER produce a
        # rejected verdict - only a definitive dead-session RPC name does.
        # Everything else stays transient ("error"), which the recovery worker
        # maps to UNKNOWN so the session remains pending for a later retry.
        print(f"⚠️ RECOVERY: Session check error: {e}")
        details["error"] = str(e)
        details["is_valid"] = False
        if classify_dead_session_error(e):
            return False, "invalid", details
        return False, "error", details
    finally:
        if temp_client:
            try:
                await _disconnect(temp_client)
            except:
                pass
