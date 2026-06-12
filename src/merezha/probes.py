"""Async network probes with phase-by-phase latency breakdown.

No third-party networking libraries here on purpose: probes are built
directly on ``asyncio`` streams so that every phase of a connection —
DNS resolution, TCP handshake, TLS handshake, time-to-first-byte — can
be timed independently, the way ``curl -w`` does it.

Two probe kinds are supported:

* ``tcp``  — DNS + TCP connect. Works against any TCP service
  (databases, DNS resolvers, SSH, custom ports) without speaking its
  protocol.
* ``http`` — the full waterfall. A minimal ``GET`` request is written
  by hand and the response status line is read to measure TTFB.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass, field

from .config import Target

DEFAULT_TIMEOUT = 5.0
USER_AGENT = "merezha/0.1 (+https://github.com/illiatkachuk/merezha)"

PHASE_ORDER = ("dns", "tcp", "tls", "ttfb")


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single probe against one target."""

    target: str
    host: str
    kind: str  # "http" | "tcp"
    success: bool
    ts: float  # unix timestamp when the probe started
    latency_ms: float | None = None
    phases: dict[str, float] = field(default_factory=dict)  # phase -> ms
    status_code: int | None = None
    resolved_ip: str | None = None
    error: str | None = None


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _describe(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    name = type(exc).__name__
    msg = str(exc).strip()
    return f"{name}: {msg}" if msg else name


async def _resolve(host: str, port: int) -> tuple[str, float]:
    """Resolve *host* and return ``(ip, elapsed_ms)``, preferring IPv4."""
    loop = asyncio.get_running_loop()
    start = time.perf_counter()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    elapsed = (time.perf_counter() - start) * 1000.0
    for family, _type, _proto, _canon, sockaddr in infos:
        if family == socket.AF_INET:
            return sockaddr[0], elapsed
    return infos[0][4][0], elapsed


async def _close(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def tcp_probe(
    target: str, host: str, port: int, timeout: float = DEFAULT_TIMEOUT
) -> ProbeResult:
    """DNS + TCP connect latency. Protocol-agnostic."""
    ts = time.time()
    phases: dict[str, float] = {}
    writer: asyncio.StreamWriter | None = None
    try:
        if is_ip_literal(host):
            ip = host
        else:
            ip, phases["dns"] = await asyncio.wait_for(_resolve(host, port), timeout)

        start = time.perf_counter()
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        phases["tcp"] = (time.perf_counter() - start) * 1000.0

        return ProbeResult(
            target=target,
            host=host,
            kind="tcp",
            success=True,
            ts=ts,
            latency_ms=sum(phases.values()),
            phases=phases,
            resolved_ip=ip,
        )
    except Exception as exc:
        return ProbeResult(
            target=target, host=host, kind="tcp", success=False, ts=ts, error=_describe(exc)
        )
    finally:
        await _close(writer)


async def http_probe(
    target: str,
    host: str,
    port: int = 443,
    timeout: float = DEFAULT_TIMEOUT,
    path: str = "/",
) -> ProbeResult:
    """Full HTTP(S) waterfall: DNS -> TCP -> TLS -> first response byte.

    TLS is negotiated when ``port == 443``; any other port is treated as
    plain HTTP. The request is written by hand so no HTTP client library
    sits between us and the timing.
    """
    ts = time.time()
    phases: dict[str, float] = {}
    use_tls = port == 443
    writer: asyncio.StreamWriter | None = None
    try:
        if is_ip_literal(host):
            ip = host
        else:
            ip, phases["dns"] = await asyncio.wait_for(_resolve(host, port), timeout)

        start = time.perf_counter()
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        phases["tcp"] = (time.perf_counter() - start) * 1000.0

        if use_tls:
            ctx = ssl.create_default_context()
            start = time.perf_counter()
            await asyncio.wait_for(writer.start_tls(ctx, server_hostname=host), timeout)
            phases["tls"] = (time.perf_counter() - start) * 1000.0

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        start = time.perf_counter()
        writer.write(request.encode("ascii"))
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout)
        phases["ttfb"] = (time.perf_counter() - start) * 1000.0

        status_code: int | None = None
        parts = status_line.decode("latin-1", errors="replace").split()
        if len(parts) >= 2 and parts[1].isdigit():
            status_code = int(parts[1])

        return ProbeResult(
            target=target,
            host=host,
            kind="http",
            success=True,
            ts=ts,
            latency_ms=sum(phases.values()),
            phases=phases,
            status_code=status_code,
            resolved_ip=ip,
        )
    except Exception as exc:
        return ProbeResult(
            target=target, host=host, kind="http", success=False, ts=ts, error=_describe(exc)
        )
    finally:
        await _close(writer)


async def run_probe(t: Target, timeout: float = DEFAULT_TIMEOUT) -> ProbeResult:
    """Dispatch a probe according to the target's ``kind``."""
    if t.kind == "http":
        return await http_probe(t.name, t.host, t.port, timeout)
    return await tcp_probe(t.name, t.host, t.port, timeout)
