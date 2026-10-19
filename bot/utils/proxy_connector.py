"""
Proxy-aware connection helper for MTProto clients.
=====================================================================
Wraps ``Client(...)`` creation + ``connect_with_timeout`` so the whole
verification pipeline transparently routes a country's sessions through
its configured proxy chain:

    country pool proxies -> global default proxy -> direct (fail-open)

Key safety property: when the proxy system is unused (no default, no
country pool, master switch off) or the country is closed, the candidate
list is exactly ``[None]`` and behaviour is byte-identical to today -
one direct connect attempt with the same 45s ceiling.

The helper never swallows errors: if every candidate fails it re-raises
the last error, so existing ``except`` / retry / anti-spam logic in the
callers keeps working unchanged.
"""

import logging
import time
import urllib.parse
from typing import Any, Callable, Optional

from bot.data.proxies import (
    breaker_cooldown_remaining,
    get_connect_plan,
    proxy_display_key,
    record_proxy_attempt,
)
from bot.utils.net_timeouts import connect_with_timeout, teardown_with_timeout

logger = logging.getLogger(__name__)

# A dead proxy usually fails fast (refused / blackhole). The per-proxy budget
# is small: at 40-50k sessions/day a full chain of dead proxies must not stall
# a user for minutes. Worst case is now bounded by:
#   15s (first) + 4x8s (rest) + 8s (default) + 30s (direct) = 85s
# (was ~195s), and typically far less because refused connections fail in <1s
# and breaker-open proxies are skipped instantly by the connect plan. The
# FIRST candidate gets a larger budget (15s) so a healthy but slow proxy is
# never breaker-opened merely for latency.
_PER_PROXY_CONNECT_TIMEOUT = 8
_FIRST_CANDIDATE_CONNECT_TIMEOUT = 15
_DIRECT_CONNECT_TIMEOUT = 30

# P4-42: per-CONNECT cumulative budget. When the whole fleet is dead the old
# connector walked EVERY candidate on EVERY connect (breaker-open probes at 3s
# each + default + 30s direct ~= 18-48s per phase). Once ~25s have elapsed the
# remaining proxy candidates are skipped and the walk falls through to
# whatever is left (the direct candidate or the error path).
_CONNECT_CUMULATIVE_BUDGET_SECONDS = 25.0


def get_pinned_proxy_url(session_data, session_info) -> Optional[str]:
    """Pinned proxy URL for a pending session, or None when there is no pin.

    Reads the ``proxy_url`` persisted at login time (P4-41) from either the
    pending-session row (``session_data``) or its nested session_data dict
    (``session_info``) - the two accessor shapes the monitor and recovery
    paths use. None = no pin -> normal pool resolution (current behavior).
    """
    for source in (session_info, session_data):
        if isinstance(source, dict):
            try:
                url = source.get("proxy_url")
            except Exception:
                url = None
            if isinstance(url, str) and url.strip():
                return url.strip()
    return None


def _serialize_proxy_url(parsed: dict) -> str:
    """Round-trip a parsed proxy dict back into a URL string.

    Percent-encoding is reapplied to the credentials so the pinned URL
    survives ``parse_proxy_url`` (which percent-DECODES) unchanged - the same
    host/port/credentials, never a masked or truncated form.
    """
    try:
        host = f"[{parsed['hostname']}]" if ":" in str(parsed.get("hostname", "")) else parsed.get("hostname", "")
        port = parsed.get("port") or 0
        if "username" in parsed:
            user = urllib.parse.quote(str(parsed.get("username") or ""), safe="")
            pwd = urllib.parse.quote(str(parsed.get("password") or ""), safe="")
            return f"{parsed.get('scheme', 'socks5')}://{user}:{pwd}@{host}:{port}"
        return f"{parsed.get('scheme', 'socks5')}://{host}:{port}"
    except Exception:
        return ""


async def connect_with_proxy_chain(
    make_client: Callable[[Optional[dict]], Any],
    country_code: Optional[str],
    label: str,
    preferred_url: Optional[str] = None,
    allow_breaker_self_heal: bool = True,
) -> Any:
    """Create a Client and connect it, walking the country's proxy chain.

    Args:
        make_client: sync factory ``make_client(proxy_dict_or_None) -> Client``.
        country_code: session country (e.g. ``+880``); None = direct.
        label:        log label, e.g. ``send-code:+88017...``.
        preferred_url: P4-41 per-session PINNED proxy URL. When provided (and
            the proxy is enabled + not breaker-open) the plan sticks to it,
            so every phase of the same session dials the SAME proxy instead of
            re-rolling the pool.
        allow_breaker_self_heal: True only for the LOGIN phase (P4-42): a
            breaker-open candidate gets one short-budget self-heal attempt.
            Check phases pass False and skip breaker-open proxies instantly.

    Returns the CONNECTED client. Raises the last candidate error when every
    candidate (including the direct fail-open) failed. With
    ``PROXY_ALLOW_DIRECT=0`` and no usable proxy left, raises a clear
    "no proxies available" error (callers treat it as transient).

    Each candidate uses the same bounded ``connect_with_timeout``; a failed
    proxy is disconnected best-effort before the next candidate is tried. The
    connected client is tagged with the proxy display key (``_bot_proxy_key``),
    the dialable URL (``_bot_proxy_url``, the session pin) and the attribution
    country (``_bot_proxy_attribution``) so callers can record which proxy a
    session actually used AND under which country the health stats must be
    stored (pool-owner code, not the session country, when a fallback pool
    matched).
    """
    attribution, candidates = get_connect_plan(country_code or "", preferred_url=preferred_url)
    if not candidates:
        # PROXY_ALLOW_DIRECT=0 (strict mode) with no usable proxy left: fail
        # loudly instead of silently dialing direct. Every caller already
        # treats a connect error as transient/inconclusive, never as an
        # account verdict.
        raise RuntimeError(
            f"connect_with_proxy_chain: no proxies available for {label} "
            "(PROXY_ALLOW_DIRECT=0 and no usable proxy candidate)"
        )

    last_error: Optional[Exception] = None
    connect_started = time.monotonic()
    budget_logged = False
    breaker_self_heal_used = False

    for idx, proxy in enumerate(candidates):
        client = None
        client_built = False
        try:
            if proxy is not None:
                # P4-42: per-CONNECT cumulative budget - stop walking the dead
                # list once ~25s have elapsed (log once, skip the rest).
                if time.monotonic() - connect_started >= _CONNECT_CUMULATIVE_BUDGET_SECONDS:
                    if not budget_logged:
                        logger.warning(
                            f"🌐 {label}: cumulative connect budget "
                            f"({_CONNECT_CUMULATIVE_BUDGET_SECONDS}s) exceeded - "
                            "skipping remaining proxy candidates"
                        )
                        budget_logged = True
                    continue
                # A breaker-open proxy in the candidate list means EVERY proxy
                # was open (self-healing retry). Only the FIRST candidate of
                # the LOGIN phase gets the short self-heal budget; every other
                # breaker-open candidate is skipped instantly so a dead fleet
                # cannot be re-walked by every verification phase (P4-42).
                remaining = breaker_cooldown_remaining(attribution, proxy)
                if remaining > 0:
                    if not allow_breaker_self_heal or breaker_self_heal_used:
                        logger.debug(f"🌐 {label}: skipping breaker-open candidate {idx + 1}")
                        continue
                    breaker_self_heal_used = True
                    # It was already tried seconds ago and failed 3+ times; a
                    # single quick retry with a short budget keeps the
                    # self-heal while not punishing the user with the full
                    # timeout. (P4-40)
                    proxy_budget = max(3, _PER_PROXY_CONNECT_TIMEOUT // 3)
                elif idx == 0:
                    proxy_budget = _FIRST_CANDIDATE_CONNECT_TIMEOUT
                else:
                    proxy_budget = _PER_PROXY_CONNECT_TIMEOUT
                # P4-36: a failure while BUILDING the client (bad session
                # string, bad proxy dict, OOM) is NOT a proxy-connect failure.
                # The old code recorded it against the proxy health map and
                # could open the breaker for a perfectly healthy proxy.
                try:
                    client = make_client(proxy)
                    client_built = True
                except Exception as build_err:
                    last_error = build_err
                    logger.warning(
                        f"🌐 {label}: candidate {idx + 1} CLIENT BUILD failed "
                        f"(not counted as proxy failure): {build_err}"
                    )
                    continue
                await connect_with_timeout(
                    client,
                    f"{label}:proxy{idx + 1}",
                    timeout=proxy_budget,
                )
                logger.debug(f"🌐 {label}: connected via proxy candidate {idx + 1}")
                record_proxy_attempt(attribution, proxy, True)
                _tag_client(client, proxy, attribution)
                return client
            # Direct candidate (last resort when proxies exist, or the only
            # candidate when the system is unused).
            try:
                client = make_client(None)
                client_built = True
            except Exception as build_err:
                last_error = build_err
                logger.warning(f"🌐 {label}: DIRECT client build failed (no proxy involved): {build_err}")
                continue
            await connect_with_timeout(client, label, timeout=_DIRECT_CONNECT_TIMEOUT)
            _tag_client(client, None, attribution)
            return client
        except Exception as err:
            if proxy is not None and client_built:
                record_proxy_attempt(attribution, proxy, False)
            last_error = err
            logger.warning(
                f"🌐 {label}: candidate {idx + 1} "
                f"({'proxy ' + str(idx + 1) if proxy else 'direct'}) failed: {err}"
            )
            if client is not None:
                try:
                    await teardown_with_timeout(client, f"{label}-failover")
                except Exception:
                    pass

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"connect_with_proxy_chain: no candidates for {label}")


def _tag_client(client: Any, proxy: Optional[dict], attribution: Optional[str]) -> None:
    """Best-effort tag of the used proxy key + attribution country on the client."""
    if client is None:
        return
    try:
        client._bot_proxy_key = proxy_display_key(proxy) if proxy else ""
        client._bot_proxy_attribution = attribution or ""
        client._bot_proxy_url = _serialize_proxy_url(proxy) if proxy else ""
    except Exception:
        pass
