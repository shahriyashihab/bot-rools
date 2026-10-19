"""Bounded network primitives for MTProto clients.

Neither pyrogram's ``Client.connect()`` nor telethon's ``connect()`` has an
internal timeout. Against a blackholed / migrating datacenter - very common on
mobile links and on the high-risk countries this bot serves - the await simply
never returns. Every one of those calls is awaited from a coroutine that the
monitor cycle, the recovery worker or a live user handler is waiting on, so a
single wedged connect stalls unrelated pending sessions indefinitely (this was
reproduced empirically: the test suite hung for >10 minutes on exactly that
shape in ``verification/telethon_export.py``).

This module is a LEAF: it imports nothing from ``bot``, so it can be used from
any layer without circular-import risk.
"""
import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Telegram's own handshake normally completes in <5s; 45s is generous enough to
# absorb a DC migration plus a slow mobile link, and short enough that a dead
# endpoint is detected within one monitor cycle.
CONNECT_TIMEOUT_SECONDS = 45
# A single raw API invoke (getAuthorizations, resetAuthorization, ...).
API_CALL_TIMEOUT_SECONDS = 60
# Disconnect must never be the thing that hangs a cleanup path.
DISCONNECT_TIMEOUT_SECONDS = 30


async def connect_with_timeout(
    client: Any,
    label: str = "connect",
    timeout: Optional[float] = None,
) -> None:
    """``await client.connect()`` with a hard ceiling.

    Raises ``asyncio.TimeoutError`` (which is the builtin ``TimeoutError`` on
    Python 3.11+, hence caught by existing ``except Exception`` handlers) when
    the handshake does not complete in time.
    """
    limit = CONNECT_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        await asyncio.wait_for(client.connect(), timeout=limit)
    except asyncio.TimeoutError:
        logger.warning(f"⚠️ {label}: connect timed out after {limit}s")
        raise


async def invoke_with_timeout(
    client: Any,
    request: Any,
    label: str = "invoke",
    timeout: Optional[float] = None,
) -> Any:
    """``await client.invoke(request)`` with a hard ceiling."""
    limit = API_CALL_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        return await asyncio.wait_for(client.invoke(request), timeout=limit)
    except asyncio.TimeoutError:
        logger.warning(f"⚠️ {label}: API call timed out after {limit}s")
        raise


async def call_with_timeout(
    awaitable: Any,
    label: str = "call",
    timeout: Optional[float] = None,
    raise_on_timeout: bool = False,
) -> Any:
    """Bound any awaitable.

    Default (``raise_on_timeout=False``): the timeout is swallowed into ``None``.
    That is what best-effort teardown needs (``safe_disconnect``, a
    ``disconnect()`` in a ``finally``) - a timeout there must never propagate and
    mask the real result.

    With ``raise_on_timeout=True`` the ``asyncio.TimeoutError`` is re-raised so
    the caller's existing ``except Exception`` / retry logic treats a hung call
    as the failure it is. Use this for OPERATIONS (``send_code``, ``sign_in``,
    ``get_me``, ``enable_cloud_password``, ...) where a silent ``None`` would be
    read as a successful-but-empty result and corrupt the verdict.
    """
    limit = DISCONNECT_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        return await asyncio.wait_for(awaitable, timeout=limit)
    except asyncio.TimeoutError:
        if raise_on_timeout:
            logger.warning(f"⚠️ {label}: timed out after {limit}s")
            raise
        logger.warning(f"⚠️ {label}: timed out after {limit}s (ignored)")
        return None


async def op_with_timeout(
    awaitable: Any,
    label: str = "op",
    timeout: Optional[float] = None,
) -> Any:
    """A bounded OPERATION on an account client: a timeout RAISES.

    Shorthand for ``call_with_timeout(..., raise_on_timeout=True)`` with the
    API-call ceiling. Every ``get_me`` / ``sign_in`` / ``send_code`` /
    ``enable_cloud_password`` / ``send_message`` on a user session goes through
    here so a hung request becomes a normal handled error instead of a coroutine
    that owns a pending session and never returns.
    """
    limit = API_CALL_TIMEOUT_SECONDS if timeout is None else timeout
    return await call_with_timeout(awaitable, label, limit, raise_on_timeout=True)


async def teardown_with_timeout(client: Any, label: str = "client") -> None:
    """Bounded, never-raising ``client.disconnect()``.

    Safe to call from a ``finally`` / bare ``except`` block: neither a timeout
    nor a disconnect error can escape.
    """
    if client is None:
        return
    try:
        await call_with_timeout(client.disconnect(), f"disconnect:{label}")
    except Exception as err:
        logger.debug(f"disconnect note ({label}): {err}")
