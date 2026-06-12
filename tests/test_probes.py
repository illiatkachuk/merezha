"""Probe tests against throwaway local asyncio servers — no real network needed."""

from __future__ import annotations

import asyncio

from merezha.probes import http_probe, is_ip_literal, tcp_probe


async def _start_tcp_server(handler) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _silent_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.close()


async def _http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.readline()  # request line is enough for us
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    await writer.drain()
    writer.close()


async def test_tcp_probe_success() -> None:
    server, port = await _start_tcp_server(_silent_handler)
    async with server:
        result = await tcp_probe("local", "127.0.0.1", port)
    assert result.success
    assert result.kind == "tcp"
    assert result.latency_ms is not None and result.latency_ms >= 0.0
    assert "tcp" in result.phases
    assert "dns" not in result.phases  # IP literals skip resolution
    assert result.resolved_ip == "127.0.0.1"


async def test_tcp_probe_resolves_hostnames() -> None:
    server, port = await _start_tcp_server(_silent_handler)
    async with server:
        result = await tcp_probe("local", "localhost", port)
    assert result.success
    assert "dns" in result.phases


async def test_tcp_probe_connection_refused() -> None:
    server, port = await _start_tcp_server(_silent_handler)
    server.close()
    await server.wait_closed()  # port is now guaranteed free
    result = await tcp_probe("dead", "127.0.0.1", port)
    assert not result.success
    assert result.error
    assert result.latency_ms is None


async def test_http_probe_waterfall() -> None:
    server, port = await _start_tcp_server(_http_handler)
    async with server:
        result = await http_probe("local", "127.0.0.1", port)
    assert result.success
    assert result.status_code == 200
    assert "tcp" in result.phases and "ttfb" in result.phases
    assert "tls" not in result.phases  # plain HTTP on a non-443 port
    assert result.latency_ms == sum(result.phases.values())


async def test_http_probe_timeout() -> None:
    async def _sleepy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(5)
        writer.close()

    server, port = await _start_tcp_server(_sleepy)
    async with server:
        result = await http_probe("slow", "127.0.0.1", port, timeout=0.2)
    assert not result.success
    assert result.error == "timeout"


def test_is_ip_literal() -> None:
    assert is_ip_literal("8.8.8.8")
    assert is_ip_literal("::1")
    assert not is_ip_literal("example.com")
