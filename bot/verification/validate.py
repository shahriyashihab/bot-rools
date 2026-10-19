"""
Session validation.
=====================================================================
Validates whether a Pyrogram session string is still logged in, with
ANTI-SPAM protection and FloodWait-aware retries.

Return contract:
    True  - session is definitively still logged in
    False - session is definitively invalid (auth key unregistered/revoked,
            account deactivated/deleted, or no user data)
    None  - INCONCLUSIVE (transient network error / flood wait / unexpected
            failure). Callers MUST NOT reject on this: keep the session
            pending and let a later monitor cycle retry with a fresh check.
"""

from datetime import datetime

from pyrogram.client import Client
from pyrogram.errors import AuthKeyUnregistered, FloodWait, SessionRevoked, UserDeactivated

from bot.utils.anti_spam import (
    between_api_calls_delay,
    human_delay,
    safe_disconnect,
    smart_flood_wait_handler,
)
from bot.utils.device_generator import generate_device_info
from bot.utils.net_timeouts import op_with_timeout
from bot.verification.api import get_rotated_api


async def validate_session(session_string: str, twofa_password: str, country_code: str = None, preferred_proxy_url: str = None):
    """
    Validate if a session is still logged in with ANTI-SPAM protection.

    Args:
        session_string: Pyrogram session string
        twofa_password: 2FA password (unused here, kept for signature parity)
        country_code: Optional country code for proxy routing. When provided
                      the validation client uses the country's proxy chain.
                      None or empty = direct connection (today's behaviour).

    Returns:
        True / False / None - see module docstring for the contract.
    """
    client = None
    try:
        await human_delay(0.5, 1.0, "pre-validate")

        api_id, api_hash, api_name = get_rotated_api()
        print(f"🔄 VALIDATE: Using {api_name} for session validation")

        # P7-75: deterministic per-phone fingerprint. validate.py has no phone
        # param, so seed from the session string (stable for the account).
        device_info = generate_device_info(seed=session_string[:24], country_code=country_code)

        def _make_validate_client(proxy):
            kwargs = dict(
                name=f"validate_{int(datetime.now().timestamp())}",
                api_id=api_id,
                api_hash=api_hash,
                session_string=session_string,
                in_memory=True,
                device_model=device_info["device_model"],
                system_version=device_info["system_version"],
                app_version="Telegram Android 0.28.1.1771-arm64-v8a",
            )
            if proxy:
                kwargs["proxy"] = proxy
            return Client(**kwargs)

        from bot.utils.proxy_connector import connect_with_proxy_chain
        client = await connect_with_proxy_chain(
            _make_validate_client, country_code, "validate-session",
            preferred_url=preferred_proxy_url,
            allow_breaker_self_heal=False,
        )

        await between_api_calls_delay("validate-connect")

        try:
            for attempt in range(2):
                try:
                    me = await op_with_timeout(client.get_me(), "validate-get-me")
                    await safe_disconnect(client, "validate-success")

                    if me and me.id:
                        print(f"✅ Session validated: {me.id} is still logged in")
                        return True
                    else:
                        print(f"❌ Session validation failed: No user data")
                        return False
                except FloodWait as e:
                    if await smart_flood_wait_handler(e, "validate_session"):
                        continue
                    else:
                        # FloodWait too long - a transient rate limit, NOT proof
                        # the session is dead. Do not return False.
                        await safe_disconnect(client, "validate-flood")
                        print("⚠️ VALIDATE: FloodWait too long - inconclusive, keeping session pending")
                        return None
                except (AuthKeyUnregistered, UserDeactivated, SessionRevoked) as e:
                    await safe_disconnect(client, "validate-dead")
                    print(f"❌ Session validation failed (definitive): {e}")
                    return False
                except Exception:
                    if attempt < 1:
                        await human_delay(0.5, 1, "validate-retry")
                    else:
                        # Transient error after retries - inconclusive, NOT death.
                        await safe_disconnect(client, "validate-transient")
                        print("⚠️ VALIDATE: transient error after retries - inconclusive, keeping session pending")
                        return None

        except (AuthKeyUnregistered, UserDeactivated, SessionRevoked) as e:
            # Raised outside get_me (e.g. during connect) - definitive death.
            await safe_disconnect(client, "validate-dead")
            print(f"❌ Session validation failed (definitive): {e}")
            return False
        except Exception as e:
            # Transient infra error - inconclusive, NOT death.
            print(f"⚠️ VALIDATE: transient validation error for session: {e} - inconclusive, keeping pending")
            return None

    except (AuthKeyUnregistered, UserDeactivated, SessionRevoked) as e:
        print(f"❌ Session validation failed (definitive): {e}")
        return False
    except Exception as e:
        print(f"⚠️ VALIDATE: session validation inconclusive: {e} - keeping session pending")
        return None
    finally:
        if client is not None:
            try:
                await safe_disconnect(client, "validate-cleanup")
            except Exception:
                pass
