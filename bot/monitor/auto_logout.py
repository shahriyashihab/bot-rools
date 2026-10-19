"""24h multi-device auto-logout: keep ONLY the bot's own authorization.

Hard requirement: once the 24h hold has elapsed, the account must keep the
bot's own device/session and EVERY other authorization must be terminated.
A transient network hiccup, a FloodWait, or Telegram's 24h "fresh
authorization" rule must never be mistaken for "cleanup impossible" (that
would reject a perfectly good account), and the bot's own session must never
be terminated by accident.

Guarantees
----------
1. ``account.resetAuthorization`` is NEVER invoked with a falsy hash. In
   MTProto ``hash == 0`` addresses the CURRENT authorization, so the old
   ``hash=device.get('hash', 0)`` fallback could log the BOT itself out.
2. The current authorization is excluded twice over: ``current is True`` OR
   ``hash == 0``.
3. Telegram refuses resetAuthorization until 24h after the CURRENT
   authorization was created (``FRESH_RESET_AUTHORISATION_FORBIDDEN``). The
   eligibility moment is read from the authorization itself (``date_created``)
   instead of being guessed from ``submitted_at``; a short remaining wait is
   performed inline. This is a TIMING condition, never a failed attempt.
4. Success is declared only after a FRESH ``account.getAuthorizations`` shows
   zero extra authorizations AND the bot's own authorization still present.
   An inconclusive probe is NEVER read as "no extra devices".
5. A permanently dead session (revoked / banned / unregistered auth key) sets
   ``md_session_dead`` so the caller rejects it instead of retrying forever.
6. Every network call is bounded by ``asyncio.wait_for``.
7. ``get_pending_session`` / ``set_pending_session_field`` are MODULE-level
   imports. They used to be function-local imports inside the success branch,
   which made the names function-LOCAL for the entire body, so the failure
   branch raised ``UnboundLocalError``: ``md_logout_attempts`` never
   incremented and multi-device sessions stayed pending forever.
8. A deferral always points into the FUTURE (``MIN_DEFER_SECONDS``) and is
   bounded by ``MULTI_DEVICE_MAX_DEFERRALS``. A past ``md_logout_not_before``
   made every gate read "already elapsed", retry instantly and defer again -
   a retry-free infinite loop in which the session was never rejected AND never
   credited. Past the budget a deferral is downgraded to a genuine failure so
   the ordinary bounded-retry -> reject chain resolves the session.
9. No path can leave the bookkeeping untouched: even an ImportError on the
   pyrogram raw account API counts an attempt, so the session always converges
   on SUCCESS or REJECTED instead of staying pending forever.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from bot.utils.anti_spam import (
    between_api_calls_delay,
    human_delay,
    safe_disconnect,
    smart_flood_wait_handler,
)
from bot.utils.device_generator import generate_device_info
from bot.utils.session_handler import get_rotated_api
from bot.utils.storage import get_pending_session, set_pending_session_field

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 45
API_CALL_TIMEOUT_SECONDS = 60
DISCONNECT_TIMEOUT_SECONDS = 30

# Telegram's 24h freshness rule for account.resetAuthorization, plus a safety
# margin (server clock vs. the returned date_created can differ by seconds).
FRESH_AUTH_WINDOW_HOURS = 24
FRESH_AUTH_MARGIN_SECONDS = 180
# Longest wait performed INSIDE one run; anything longer is deferred to a later
# cycle via md_logout_not_before (which consumes NO retry).
#
# This used to be 900s, then 240s. run_auto_logout is awaited from the SINGLE
# monitor loop (monitor/orchestrator.py) via run_full_validation, so sleeping
# here stalls stuck-recovery, the channel-report safety net and every other
# pending session for the same duration. 30s keeps the monitor responsive
# while still covering the immediate Telegram freshness-window edge case in
# one shot. Anything longer becomes a deferral, and the dedicated per-session
# 24h timer (monitor/twenty_four_hour.py) owns the wait in its own task where
# blocking costs nothing.
MAX_INLINE_WAIT_SECONDS = 30

# A deferral must always point into the FUTURE. `_defer` used to store whatever
# it was handed verbatim, and the fresh-window branch hands it ``eligible``,
# which is in the PAST exactly when the error is spurious or the server clock
# skewed. Every gate then read "deferral already elapsed", retried at once,
# deferred again - a retry-free infinite loop in which no counter ever moved and
# the session was never resolved.
MIN_DEFER_SECONDS = 60

# Errors meaning the session itself is gone - retrying can never help.
_DEAD_SESSION_MARKERS = (
    "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_INVALID",
    "AUTH_KEY_DUPLICATED",
    "AUTH_KEY_PERM_EMPTY",
    "SESSION_REVOKED",
    "SESSION_EXPIRED",
    "USER_DEACTIVATED",
    "USER_DEACTIVATED_BAN",
)
# Errors meaning "not allowed yet, come back later" - a TIMING condition.
_FRESH_WINDOW_MARKERS = (
    "FRESH_RESET_AUTHORISATION_FORBIDDEN",
    "FRESH_RESET_AUTHORIZATION_FORBIDDEN",
    "FRESH_AUTH_KEY_FORBIDDEN",
)
# Errors meaning that particular authorization is already gone.
_STALE_HASH_MARKERS = (
    "HASH_INVALID",
    "AUTHORIZATION_INVALID",
    "AUTH_HASH_INVALID",
)


def _err_text(err: BaseException) -> str:
    return f"{type(err).__name__} {err}".upper()


def _is_dead_session_error(err: BaseException) -> bool:
    text = _err_text(err)
    return any(marker in text for marker in _DEAD_SESSION_MARKERS)


def _is_fresh_window_error(err: BaseException) -> bool:
    text = _err_text(err)
    return any(marker in text for marker in _FRESH_WINDOW_MARKERS)


def _is_stale_hash_error(err: BaseException) -> bool:
    text = _err_text(err)
    return any(marker in text for marker in _STALE_HASH_MARKERS)


def _flood_seconds(err: BaseException) -> int:
    try:
        return int(getattr(err, "value", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _persist(session_id: str, field: str, value: Any) -> None:
    """Best-effort persist of a top-level pending-session field."""
    try:
        set_pending_session_field(session_id, field, value)
    except Exception as err:
        logger.warning(f"⚠️ auto-logout: could not persist {field}={value!r}: {err}")


def _read_md_counter(session_id: str, session_data: Dict[str, Any], field: str) -> int:
    """Highest value of ``field`` across storage, its inner dict and memory.

    Pending-session bookkeeping lives at TWO levels (top-level row and the
    nested ``session_data`` blob) and the in-memory copy the monitor is holding
    can be newer than either. Taking the max means the split can never lose a
    count and therefore never un-bound a bounded loop.
    """
    stored = get_pending_session(session_id) or {}
    inner = stored.get("session_data") or {}
    current = 0
    for source in (stored, inner, session_data):
        try:
            current = max(current, int((source or {}).get(field, 0) or 0))
        except (TypeError, ValueError):
            continue
    return current


def _count_failed_attempt(session_id: str, session_data: Dict[str, Any], reason: str) -> int:
    """Increment the bounded md_logout_attempts counter (genuine failures ONLY).

    Timing conditions (Telegram freshness window, long FloodWait) must use
    ``_defer`` instead so a healthy account is never rejected for waiting.
    """
    retry_count = _read_md_counter(session_id, session_data, "md_logout_attempts") + 1
    session_data["md_logout_attempts"] = retry_count
    _persist(session_id, "md_logout_attempts", retry_count)
    _persist(session_id, "md_last_logout_error", str(reason)[:300])
    logger.error(f"❌ auto-logout failed ({reason}) - attempt #{retry_count}")
    return retry_count


def _defer(session_id: str, session_data: Dict[str, Any],
           not_before: Optional[datetime], reason: str) -> bool:
    """Record "retry after <not_before>" WITHOUT consuming a retry.

    Returns True when a deferral was recorded. Returns False when the deferral
    budget is exhausted: the deferral is then cleared and downgraded to a
    genuine failure, so the ordinary bounded-retry -> reject chain resolves the
    session instead of it deferring forever.
    """
    from bot.recovery.constants import MULTI_DEVICE_MAX_DEFERRALS

    used = _read_md_counter(session_id, session_data, "md_defer_count")
    if used >= MULTI_DEVICE_MAX_DEFERRALS:
        logger.error(
            f"❌ auto-logout: deferral budget exhausted for {session_id} "
            f"({used}/{MULTI_DEVICE_MAX_DEFERRALS}) - treating '{reason}' as a real "
            f"failure so the session can no longer defer forever"
        )
        # Clear the gate itself, otherwise every caller keeps short-circuiting on
        # md_logout_not_before and the counted failure below is never reached.
        session_data.pop("md_logout_not_before", None)
        _persist(session_id, "md_logout_not_before", "")
        _count_failed_attempt(
            session_id, session_data,
            f"deferral budget exhausted after {used} deferrals ({reason})",
        )
        return False

    # A deferral must point into the future or it is not a deferral at all.
    floor = datetime.now() + timedelta(seconds=MIN_DEFER_SECONDS)
    if not_before is None or not_before < floor:
        not_before = floor
    iso = not_before.replace(microsecond=0).isoformat()
    session_data["md_logout_not_before"] = iso
    _persist(session_id, "md_logout_not_before", iso)
    _persist(session_id, "md_last_logout_error", str(reason)[:300])
    session_data["md_defer_count"] = used + 1
    _persist(session_id, "md_defer_count", used + 1)
    wait_s = max(0.0, (not_before - datetime.now()).total_seconds())
    print(f"⏸️ auto-logout deferred {wait_s:.0f}s ({reason}) - NO retry consumed "
          f"[deferral {used + 1}/{MULTI_DEVICE_MAX_DEFERRALS}]")
    return True


def _mark_dead(session_id: str, session_data: Dict[str, Any], reason: str) -> None:
    """Flag a permanently unusable session so the caller rejects it."""
    session_data["md_session_dead"] = True
    session_data["md_session_dead_reason"] = str(reason)[:300]
    _persist(session_id, "md_session_dead", True)
    _persist(session_id, "md_session_dead_reason", str(reason)[:300])
    logger.error(f"💀 auto-logout: session permanently unusable ({reason})")


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "").replace("+00:00", "")
        ).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _authorization_entries(result: Any) -> Optional[List[Dict[str, Any]]]:
    """Normalise ``account.Authorizations`` into plain dicts.

    Returns ``None`` when the payload has no usable ``authorizations`` field.
    The caller MUST treat that as inconclusive - never as "no extra devices",
    which would wrongly declare success and credit an uncleaned account.
    """
    authorizations = getattr(result, "authorizations", None)
    if authorizations is None:
        return None
    entries: List[Dict[str, Any]] = []
    for auth in authorizations:
        try:
            raw_hash = int(getattr(auth, "hash", 0) or 0)
        except (TypeError, ValueError):
            raw_hash = 0
        # hash == 0 IS the current authorization in MTProto, so it counts as
        # "current" even if the boolean flag is missing on this layer.
        current_flag = bool(getattr(auth, "current", False))
        is_current = current_flag or raw_hash == 0
        try:
            created = int(getattr(auth, "date_created", 0) or 0)
        except (TypeError, ValueError):
            created = 0
        entries.append({
            "hash": raw_hash,
            "is_current": is_current,
            "current_flag": current_flag,
            "date_created": created,
            "device": (
                f"{getattr(auth, 'device_model', None) or 'Unknown'}"
                f" - {getattr(auth, 'app_name', None) or 'Unknown'}"
            ),
        })
    return entries


def _split(entries: List[Dict[str, Any]]):
    """Return ``(own, extras)``: the bot's authorization and every other one.

    The explicit ``current is True`` flag is preferred over the ``hash == 0``
    convention. Both mark the current authorization, but if a payload ever
    contains a NON-current entry with ``hash == 0`` (malformed layer, hostile
    response), picking the first ``is_current`` match could hand the wrong
    ``date_created`` to ``_eligible_at`` and mis-time the whole freshness gate.
    The extras filter is unchanged - it still excludes anything current OR
    hash-0, so no bot authorization can ever land in the termination list.
    """
    own = next((e for e in entries if e.get("current_flag")), None)
    if own is None:
        own = next((e for e in entries if e["is_current"]), None)
    extras = [e for e in entries if not e["is_current"] and e["hash"] != 0]
    return own, extras


def _eligible_at(own: Optional[Dict[str, Any]]) -> Optional[datetime]:
    """When Telegram will allow resetting OTHER authorizations.

    Derived from the BOT authorization's own ``date_created`` - the exact value
    Telegram's 24h freshness rule is based on (``submitted_at`` is always a few
    seconds earlier, which is why a run fired at exactly submitted_at+24h used
    to be refused). ``None`` means unknown; then we just try and react to
    FRESH_RESET_AUTHORISATION_FORBIDDEN if it arrives.
    """
    if not own:
        return None
    created = own.get("date_created") or 0
    if created <= 0:
        return None
    try:
        created_dt = datetime.fromtimestamp(created)
    except (OverflowError, OSError, ValueError):
        return None
    return created_dt + timedelta(
        hours=FRESH_AUTH_WINDOW_HOURS, seconds=FRESH_AUTH_MARGIN_SECONDS
    )


async def _invoke(client, request, timeout: float = API_CALL_TIMEOUT_SECONDS):
    """Invoke a raw request with a hard timeout (no unbounded awaits)."""
    return await asyncio.wait_for(client.invoke(request), timeout=timeout)


async def _terminate_extras(client, phone: str, inner_attempts: int, raw: Dict[str, Any]):
    """Terminate every non-current authorization on the account.

    Returns ``(status, detail, removed, retry_at)`` where ``status`` is one of
    ``"SUCCESS"`` / ``"DEFER"`` / ``"FAIL"`` / ``"DEAD"``.
    """
    GetAuthorizations = raw["GetAuthorizations"]
    ResetAuthorization = raw["ResetAuthorization"]
    FloodWait = raw["FloodWait"]

    removed = 0
    last_detail = "no definitive answer"

    for attempt in range(1, inner_attempts + 1):
        # ---------- authoritative device list ----------
        try:
            entries = _authorization_entries(await _invoke(client, GetAuthorizations()))
        except FloodWait as flood:
            wait = _flood_seconds(flood)
            if not await smart_flood_wait_handler(flood, "auto-logout-probe"):
                return ("DEFER", f"FloodWait {wait}s on getAuthorizations", removed,
                        datetime.now() + timedelta(seconds=max(wait, 60)))
            continue
        except asyncio.TimeoutError:
            last_detail = f"getAuthorizations timed out after {API_CALL_TIMEOUT_SECONDS}s"
            await human_delay(2, 5, "logout-retry-delay")
            continue
        except Exception as err:
            if _is_dead_session_error(err):
                return "DEAD", f"getAuthorizations: {err}", removed, None
            if _is_fresh_window_error(err):
                return ("DEFER", f"getAuthorizations: {err}", removed,
                        datetime.now() + timedelta(minutes=15))
            last_detail = f"getAuthorizations: {err}"
            await human_delay(2, 5, "logout-retry-delay")
            continue

        if entries is None:
            # Inconclusive payload - MUST NOT be read as "no extra devices".
            last_detail = "getAuthorizations returned no authorizations field"
            await human_delay(2, 5, "logout-retry-delay")
            continue

        own, extras = _split(entries)
        if own is None:
            # The bot's own authorization is not in the list, so we cannot
            # prove the bot device survives. Never declare success from here.
            return ("FAIL", "bot authorization missing from getAuthorizations",
                    removed, None)

        if not extras:
            print(f"✅ {phone}: only the bot device remains "
                  f"({len(entries)} authorization(s) total)")
            return "SUCCESS", "no extra authorizations present", removed, None

        # ---------- Telegram 24h freshness gate ----------
        eligible = _eligible_at(own)
        if eligible and datetime.now() < eligible:
            wait_s = (eligible - datetime.now()).total_seconds()
            if wait_s > MAX_INLINE_WAIT_SECONDS:
                return ("DEFER",
                        f"Telegram 24h freshness window opens in {wait_s / 60:.1f} min",
                        removed, eligible)
            print(f"⏳ {phone}: waiting {wait_s:.0f}s for Telegram's 24h freshness "
                  f"window before terminating {len(extras)} device(s)")
            await asyncio.sleep(wait_s)

        print(f"📱 {phone}: terminating {len(extras)} extra device(s) "
              f"(attempt {attempt}/{inner_attempts})")
        await between_api_calls_delay("pre-logout-loop")

        for index, device in enumerate(extras, start=1):
            target_hash = device["hash"]
            # HARD SAFETY: hash 0 / falsy addresses the CURRENT authorization in
            # MTProto - i.e. the BOT's own session. Never send it.
            if not target_hash:
                logger.warning(f"⚠️ {phone}: refusing resetAuthorization(hash=0) - "
                               f"that would log the bot itself out")
                continue
            try:
                await human_delay(0.5, 1, f"logout-device-{index}")
                result = await _invoke(client, ResetAuthorization(hash=target_hash))
                if result is False:
                    last_detail = f"resetAuthorization returned False for {device['device']}"
                    logger.warning(f"  ⚠️ {phone}: {last_detail}")
                    continue
                removed += 1
                print(f"  ✅ Terminated extra device: {device['device']}")
            except FloodWait as flood:
                wait = _flood_seconds(flood)
                if not await smart_flood_wait_handler(flood, "auto-logout"):
                    return ("DEFER", f"FloodWait {wait}s while terminating devices",
                            removed, datetime.now() + timedelta(seconds=max(wait, 60)))
                continue
            except asyncio.TimeoutError:
                last_detail = f"resetAuthorization timed out for {device['device']}"
                logger.warning(f"  ⚠️ {phone}: {last_detail}")
                continue
            except Exception as err:
                if _is_fresh_window_error(err):
                    # ``eligible`` is derived from the authorization's own
                    # date_created and is in the PAST exactly when this error is
                    # spurious or the server clock is skewed. Deferring to a past
                    # instant is not a deferral - it retries immediately, defers
                    # again, and loops forever without consuming a retry. Fall
                    # back to a real interval whenever eligible is not ahead.
                    now = datetime.now()
                    retry_at = eligible if (eligible and eligible > now) else (
                        now + timedelta(minutes=15)
                    )
                    return ("DEFER", f"FRESH_RESET_AUTHORISATION_FORBIDDEN ({err})",
                            removed, retry_at)
                if _is_dead_session_error(err):
                    return "DEAD", f"resetAuthorization: {err}", removed, None
                if _is_stale_hash_error(err):
                    print(f"  ℹ️ {device['device']} already gone ({err})")
                    continue
                # One bad device must NEVER abort the whole sweep (the old code
                # `break`-ed here, leaving the remaining devices logged in).
                last_detail = f"resetAuthorization {device['device']}: {err}"
                logger.warning(f"  ⚠️ {phone}: {last_detail} - continuing with next device")
                continue

        # ---------- authoritative post-check (never trust local bookkeeping) ----------
        await between_api_calls_delay("post-logout-verify")
        try:
            after = _authorization_entries(await _invoke(client, GetAuthorizations()))
        except Exception as err:
            if _is_dead_session_error(err):
                return "DEAD", f"verify getAuthorizations: {err}", removed, None
            last_detail = f"verify getAuthorizations: {err}"
            await human_delay(2, 4, "logout-retry-delay")
            continue
        if after is None:
            last_detail = "verify getAuthorizations returned no authorizations field"
            await human_delay(2, 4, "logout-retry-delay")
            continue

        own_after, extras_after = _split(after)
        if own_after is None:
            return ("FAIL", "bot authorization disappeared after logout sweep",
                    removed, None)
        if not extras_after:
            print(f"✅ POST-LOGOUT VERIFIED for {phone}: only the bot device remains "
                  f"({removed} terminated)")
            return "SUCCESS", f"{removed} device(s) terminated", removed, None

        last_detail = f"{len(extras_after)} extra device(s) still present"
        logger.warning(f"⚠️ {phone}: {last_detail} (attempt {attempt}/{inner_attempts})")
        await human_delay(2, 4, "logout-retry-delay")

    return "FAIL", last_detail, removed, None


async def run_auto_logout(
    session_id: str,
    session_data: Dict[str, Any],
    session_info: Dict[str, Any],
    session_string: str,
    phone: str,
    user_id: str,
    rate: float,
    country_code: str,
    preferred_proxy_url: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any], Dict[str, Any]]:
    """Terminate every authorization except the bot's own.

    Returns ``(logout_success, session_data, session_info)``.

    Flags written into ``session_data`` (and persisted to storage):

    ``has_extra_devices_logged_out``
        True ONLY after a fresh authoritative check confirmed zero extras and
        the bot's own authorization still alive.
    ``md_logout_attempts``
        Bounded counter, incremented ONLY for genuine failures.
    ``md_logout_not_before``
        ISO timestamp - a deferral that consumes NO retry.
    ``md_session_dead`` / ``md_session_dead_reason``
        The session can never be cleaned up; the caller must reject it.
    """
    # The raw-API imports must not be able to escape BEFORE the bookkeeping
    # below can run. An ImportError here (pyrogram build without the raw
    # account.* functions) used to propagate out of this function, get swallowed
    # by run_full_validation's blanket handler, and leave md_logout_attempts
    # untouched - so the session deferred to nobody and stayed pending forever.
    try:
        from pyrogram.client import Client
        from pyrogram.errors import FloodWait
        from pyrogram.raw.functions.account.get_authorizations import GetAuthorizations
        from pyrogram.raw.functions.account.reset_authorization import ResetAuthorization
    except Exception as import_err:
        _count_failed_attempt(
            session_id, session_data,
            f"pyrogram raw account API unavailable: {import_err}",
        )
        return False, session_data, session_info

    from bot.recovery.constants import (
        MULTI_DEVICE_LOGOUT_INNER_ATTEMPTS,
        MULTI_DEVICE_LOGOUT_MAX_RETRIES,
    )

    if not session_string:
        _count_failed_attempt(session_id, session_data, "no session string available")
        return False, session_data, session_info

    # Honour a deferral recorded by an earlier run (freshness window / flood) so
    # the API is not hammered every monitor cycle. NOTE: no long inline sleep
    # here - this function is awaited from the single monitor loop, so waiting
    # would stall every other pending session. The dedicated per-session 24h
    # timer (monitor/twenty_four_hour.py) performs the wait in its own task.
    not_before = _parse_iso(
        session_data.get("md_logout_not_before")
        or session_info.get("md_logout_not_before")
    )
    if not_before and datetime.now() < not_before:
        remaining = (not_before - datetime.now()).total_seconds()
        if remaining > MAX_INLINE_WAIT_SECONDS:
            print(f"⏸️ 24h auto-logout for {phone}: Telegram window opens in "
                  f"{remaining / 60:.1f} min - deferring (no retry consumed)")
            return False, session_data, session_info
        print(f"⏳ 24h auto-logout for {phone}: waiting {remaining:.0f}s for "
              f"Telegram's freshness window")
        await asyncio.sleep(remaining)

    print(f"⏰ 24 HOURS PASSED for {phone}! Keeping ONLY the bot device, "
          f"terminating every other authorization...")

    client = None
    status, detail, removed, retry_at = "FAIL", "not started", 0, None
    try:
        await human_delay(1, 2, "pre-logout-connect")
        # P7-75: deterministic per-phone device fingerprint.
        device_info = generate_device_info(seed=phone, country_code=country_code)
        api_id, api_hash, _ = get_rotated_api()

        def _make_logout_client(proxy):
            kwargs = dict(
                name=f"logout_{int(datetime.now().timestamp())}",
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
            _make_logout_client, country_code, f"auto-logout:{phone}",
            preferred_url=preferred_proxy_url,
            allow_breaker_self_heal=False,
        )
        try:
            await between_api_calls_delay("logout-connect")
            status, detail, removed, retry_at = await _terminate_extras(
                client, phone, int(MULTI_DEVICE_LOGOUT_INNER_ATTEMPTS),
                {
                    "GetAuthorizations": GetAuthorizations,
                    "ResetAuthorization": ResetAuthorization,
                    "FloodWait": FloodWait,
                },
            )
        except asyncio.TimeoutError:
            status, detail = "FAIL", f"connect timed out after {CONNECT_TIMEOUT_SECONDS}s"
        except Exception as conn_err:
            if _is_dead_session_error(conn_err):
                status, detail = "DEAD", f"connect: {conn_err}"
            else:
                status, detail = "FAIL", f"connect: {conn_err}"
    except Exception as unexpected:
        if _is_dead_session_error(unexpected):
            status, detail = "DEAD", f"unexpected: {unexpected}"
        else:
            status, detail = "FAIL", f"unexpected: {unexpected}"
        logger.warning(f"⚠️ 24hr auto-logout error for {phone}: {unexpected}")
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(
                    safe_disconnect(client, "auto-logout"),
                    timeout=DISCONNECT_TIMEOUT_SECONDS,
                )
            except Exception:
                pass

    if status == "SUCCESS":
        session_data["has_extra_devices_logged_out"] = True
        session_info["has_extra_devices_logged_out"] = True
        session_data["md_devices_logged_out"] = removed
        session_data.pop("md_logout_not_before", None)
        session_info.pop("md_logout_not_before", None)
        _persist(session_id, "has_extra_devices_logged_out", True)
        _persist(session_id, "md_devices_logged_out", removed)
        # Clear the deferral so nothing re-gates this session later.
        _persist(session_id, "md_logout_not_before", "")
        _persist(session_id, "md_defer_count", 0)
        print(f"✅ 24hr auto-logout complete for {phone} ({detail}) - "
              f"continuing to normal verification flow")
        return True, session_data, session_info

    if status == "DEFER":
        if _defer(session_id, session_data, retry_at, detail):
            return False, session_data, session_info
        # The deferral budget is spent: _defer already recorded a GENUINE
        # failure, so report the counter instead of counting it a second time.
        exhausted = _read_md_counter(session_id, session_data, "md_logout_attempts")
        print(f"🔁 Logout retry counter for {phone}: "
              f"{exhausted}/{MULTI_DEVICE_LOGOUT_MAX_RETRIES} "
              f"(deferral budget exhausted: {detail})")
        return False, session_data, session_info

    if status == "DEAD":
        _mark_dead(session_id, session_data, detail)
        return False, session_data, session_info

    retry_count = _count_failed_attempt(session_id, session_data, detail)
    print(f"🔁 Logout retry counter for {phone}: "
          f"{retry_count}/{MULTI_DEVICE_LOGOUT_MAX_RETRIES} ({detail})")
    return False, session_data, session_info
