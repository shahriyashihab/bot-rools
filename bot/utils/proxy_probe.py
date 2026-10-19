"""Real proxy-protocol liveness probe for the admin panel.

A bare ``asyncio.open_connection(host, port)`` only proves that SOMETHING
answers on the port - it cannot distinguish a working SOCKS/HTTP proxy from a
random service listening on the same port (false "ACTIVE"). This module runs
the actual protocol handshake the proxy must support (SOCKS5 greeting + auth +
CONNECT, SOCKS4 CONNECT, or HTTP CONNECT) so the admin "live check" reports
meaningful results: the proxy really forwards a tunneled connection.

Scheme verdicts MUST match the real transport: only socks5 / socks4 / http
are dialable by Pyrogram+PySocks, so an ``https`` dict is reported as
unsupported instead of silently probed as plain HTTP.

It is a LEAF: imports only the standard library, so the admin panel can use it
without any circular-import risk.
"""
import asyncio
import base64
import struct
from typing import Any, Dict, Tuple

# P4-41: neutral reachability targets for the CONNECT step. Telegram DC
# endpoints are used because the proxies exist to carry MTProto traffic; IPv4
# literals keep SOCKS remote-DNS out of the picture so a proxy without DNS
# support is still tested accurately. MULTIPLE official DC IPs are tried: a
# proxy that blocks exactly one IP (or is filtered on one route) is no longer
# reported dead while it can actually carry traffic (the old single-IP probe
# produced false "INACTIVE" verdicts). ACTIVE = ANY target connects.
PROBE_TARGET_HOSTS = (
    "149.154.167.50",   # DC2 (Amyatest) - the historical single target
    "149.154.175.53",   # DC1
    "91.108.56.130",    # DC5
)
PROBE_TARGET_PORT = 443


def _parse_proxy_args(proxy: Dict[str, Any]) -> Tuple[str, int, str, str]:
    scheme = str(proxy.get("scheme") or "socks5").lower()
    host = str(proxy.get("hostname") or "").strip("[]")
    try:
        port = int(proxy.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    user = str(proxy.get("username") or "")
    password = str(proxy.get("password") or "")
    return scheme, host, port, user, password


async def _drain_until_crlf(reader: asyncio.StreamReader, timeout: float) -> bytes:
    """Read bytes until a CRLFCRLF header terminator (HTTP).

    Bounded by a TOTAL wall-clock budget (`timeout`), not a per-read timeout:
    a slow-trickling proxy can otherwise stall the caller for 8192 × timeout.
    Raises ``asyncio.TimeoutError`` when the budget is exhausted.
    """
    data = bytearray()
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        data += await asyncio.wait_for(reader.read(1), timeout=remaining)
        if data.endswith(b"\r\n\r\n"):
            return bytes(data)
        if len(data) > 8192:
            raise RuntimeError("HTTP proxy header too large")


async def _probe_socks5(
    host: str, port: int, user: str, password: str, timeout: float
) -> Tuple[bool, str]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout
    )
    try:
        need_auth = bool(user)
        methods = [0x00]
        if need_auth:
            methods = [0x02, 0x00]
        writer.write(bytes([0x05, len(methods)] + methods))
        await writer.drain()
        resp = await asyncio.wait_for(reader.read(2), timeout=timeout)
        if len(resp) < 2 or resp[0] != 0x05:
            return False, "SOCKS5 greeting rejected"
        if resp[1] == 0x02:
            if not need_auth:
                return False, "proxy demands auth but none configured"
            ub = user.encode("utf-8")
            pb = password.encode("utf-8")
            writer.write(bytes([0x01, len(ub)]) + ub + bytes([len(pb)]) + pb)
            await writer.drain()
            auth_resp = await asyncio.wait_for(reader.read(2), timeout=timeout)
            if len(auth_resp) < 2 or auth_resp[1] != 0x00:
                return False, "SOCKS5 auth rejected"
        elif resp[1] != 0x00:
            return False, f"SOCKS5 method negotiation failed (0x{resp[1]:02x})"

        # P4-41: try every official DC target - the FIRST success wins.
        last_err = "no target reachable"
        for target_host in PROBE_TARGET_HOSTS:
            target = _ipv4_bytes(target_host)
            req = bytes([0x05, 0x01, 0x00, 0x01]) + target + struct.pack(">H", PROBE_TARGET_PORT)
            writer.write(req)
            await writer.drain()
            head = await asyncio.wait_for(reader.read(4), timeout=timeout)
            if len(head) >= 4 and head[0] == 0x05 and head[1] == 0x00:
                atyp = head[3]
                if atyp == 0x01:
                    await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
                elif atyp == 0x03:
                    ln = (await asyncio.wait_for(reader.read(1), timeout=timeout))[0]
                    await asyncio.wait_for(reader.readexactly(ln), timeout=timeout)
                elif atyp == 0x04:
                    await asyncio.wait_for(reader.readexactly(16), timeout=timeout)
                await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
                return True, f"SOCKS5 CONNECT OK ({target_host})"
            if len(head) >= 2:
                last_err = f"SOCKS5 CONNECT {target_host} failed (0x{head[1]:02x})"
        return False, last_err
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def _ipv4_bytes(host: str) -> bytes:
    """IPv4 literal -> 4 bytes. Validates the octets instead of raising a
    bare ValueError on a bad host (P4: deterministic error message)."""
    parts = host.split(".")
    if len(parts) != 4:
        raise ValueError(f"invalid IPv4 literal: {host!r}")
    octets = []
    for part in parts:
        value = int(part)
        if not 0 <= value <= 255:
            raise ValueError(f"invalid IPv4 octet in {host!r}")
        octets.append(value)
    return bytes(octets)


async def _probe_socks4(
    host: str, port: int, user: str, password: str, timeout: float
) -> Tuple[bool, str]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout
    )
    try:
        last_err = "no target reachable"
        for target_host in PROBE_TARGET_HOSTS:
            ip = _ipv4_bytes(target_host)
            req = bytes([0x04, 0x01]) + struct.pack(">H", PROBE_TARGET_PORT) + ip
            req += (user or "").encode("utf-8") + b"\x00"
            writer.write(req)
            await writer.drain()
            resp = await asyncio.wait_for(reader.readexactly(8), timeout=timeout)
            if len(resp) >= 8 and resp[0] == 0x00 and resp[1] == 0x5A:
                return True, f"SOCKS4 CONNECT OK ({target_host})"
            code = resp[1] if len(resp) > 1 else 0xFF
            last_err = f"SOCKS4 CONNECT {target_host} failed (0x{code:02x})"
        return False, last_err
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _probe_http(
    host: str, port: int, user: str, password: str, timeout: float
) -> Tuple[bool, str]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout
    )
    try:
        last_err = "no target reachable"
        for target_host in PROBE_TARGET_HOSTS:
            target = f"{target_host}:{PROBE_TARGET_PORT}"
            lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
            if user:
                token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
                lines.append(f"Proxy-Authorization: Basic {token}")
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            await writer.drain()
            header = await _drain_until_crlf(reader, timeout)
            status_line = header.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            if " 200 " in status_line:
                return True, f"HTTP CONNECT OK ({target_host})"
            last_err = f"HTTP CONNECT {target_host} failed ({status_line})"
        return False, last_err
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# P4-45: shared default probe ceiling. 15s matches the connector's
# FIRST-candidate connect budget (proxy_connector) - a handshake that needs
# longer than the real connect would ever get cannot be labeled "dead" while
# the live path would still succeed. The watchdog and the admin panel both
# probe through this default so their verdicts never disagree.
DEFAULT_PROBE_TIMEOUT_SECONDS = 15.0


async def probe_proxy(proxy: Dict[str, Any], timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS) -> Tuple[bool, str]:
    """Run the real proxy handshake for one parsed proxy dict.

    Returns ``(ok, detail)`` where ``ok`` means the proxy genuinely completed
    the protocol handshake (greeting/auth/CONNECT) against the probe target.

    The whole handshake runs under ONE total ``asyncio.wait_for`` budget: a
    trickling proxy can otherwise keep the per-read timeouts from ever firing
    for a long time (each small read would get a fresh timeout).
    """
    scheme, host, port, user, password = _parse_proxy_args(proxy)
    if not host or not port:
        return False, "invalid proxy params"
    if scheme == "https":
        # The MTProto transport (PySocks) has no TLS-to-proxy proxy type, so
        # an https:// proxy can never carry a real connection. The probe must
        # report the SAME verdict the real connect will produce - never a
        # false "ACTIVE" for an unusable proxy.
        return False, "https scheme is not supported by the MTProto transport"
    try:
        if scheme == "socks5":
            coro = _probe_socks5(host, port, user, password, timeout)
        elif scheme == "socks4":
            coro = _probe_socks4(host, port, user, password, timeout)
        elif scheme == "http":
            coro = _probe_http(host, port, user, password, timeout)
        else:
            return False, f"unsupported scheme {scheme}"
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        return False, "timeout"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
