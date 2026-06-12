"""MCP server: let AI assistants query and diagnose your network.

Exposes Merezha's probes and stored measurements as Model Context
Protocol tools, so any MCP client (Claude Desktop, IDE agents, custom
agents) can ask questions like *"why was my connection slow last
night?"* and get real local data instead of guesses.

Run it with::

    merezha mcp

The server speaks MCP over stdio and only ever reads the local SQLite
database plus runs the same probes as the CLI — nothing is sent
anywhere.
"""

from __future__ import annotations

import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .probes import PHASE_ORDER, http_probe, tcp_probe
from .storage import Storage

INSTRUCTIONS = (
    "Tools for observing the user's local network connectivity. "
    "Use check_host for live measurements, and network_status / "
    "latency_history / recent_anomalies for data collected by the "
    "'merezha monitor' background process."
)


def build_server(db_path: Path) -> FastMCP:
    server = FastMCP("merezha", instructions=INSTRUCTIONS)

    @server.tool()
    async def check_host(host: str, port: int = 443, kind: str = "auto") -> str:
        """Run a live latency check against a host right now.

        Returns a DNS / TCP / TLS / TTFB phase breakdown in milliseconds.
        kind: "http" for a full waterfall, "tcp" for connect-only,
        "auto" to pick based on the port.
        """
        kind = kind.lower()
        if kind == "auto":
            kind = "http" if port in (80, 443) else "tcp"
        probe = http_probe if kind == "http" else tcp_probe
        result = await probe(host, host, port)
        if not result.success:
            return f"Probe of {host}:{port} FAILED — {result.error}"
        lines = [f"Probe of {host}:{port} ({kind}) succeeded:"]
        for name in PHASE_ORDER:
            if name in result.phases:
                lines.append(f"  {name.upper():<5} {result.phases[name]:8.1f} ms")
        lines.append(f"  TOTAL {result.latency_ms:8.1f} ms")
        if result.status_code is not None:
            lines.append(f"  HTTP status: {result.status_code}")
        if result.resolved_ip:
            lines.append(f"  Resolved IP: {result.resolved_ip}")
        return "\n".join(lines)

    @server.tool()
    def network_status() -> str:
        """Current status of all monitored targets (latest sample + 1h stats)."""
        with Storage(db_path) as storage:
            targets = storage.targets()
            if not targets:
                return (
                    "No measurements stored yet. Start `merezha monitor` "
                    "to begin collecting data."
                )
            lines = ["Monitored targets (last sample + last hour):"]
            for name in targets:
                last = storage.latest_sample(name)
                stats = storage.stats(name, hours=1.0)
                state = "UP" if last is not None and last["success"] else "DOWN"
                age = "" if last is None else f", {time.time() - last['ts']:.0f}s ago"
                avg = "n/a" if stats.avg_ms is None else f"{stats.avg_ms:.1f} ms"
                p95 = "n/a" if stats.p95_ms is None else f"{stats.p95_ms:.1f} ms"
                lines.append(
                    f"- {name}: {state}{age} | 1h avg {avg}, p95 {p95}, "
                    f"loss {stats.loss_pct:.1f}% over {stats.samples} samples"
                )
            return "\n".join(lines)

    @server.tool()
    def latency_history(target: str, hours: float = 24.0) -> str:
        """Latency statistics for one monitored target over the last N hours."""
        with Storage(db_path) as storage:
            stats = storage.stats(target, hours=hours)
            if stats.samples == 0:
                known = ", ".join(storage.targets()) or "(none)"
                return f"No samples for '{target}' in the last {hours:g}h. Known targets: {known}"
            return (
                f"{target}, last {hours:g}h: {stats.samples} samples, "
                f"{stats.failures} failures ({stats.loss_pct:.1f}% loss), "
                f"avg {stats.avg_ms:.1f} ms, p95 {stats.p95_ms:.1f} ms, "
                f"min {stats.min_ms:.1f} ms, max {stats.max_ms:.1f} ms"
            )

    @server.tool()
    def recent_anomalies(hours: float = 24.0) -> str:
        """Anomalies flagged by the statistical/ML detectors in the last N hours."""
        with Storage(db_path) as storage:
            rows = storage.recent_anomalies(hours=hours)
            if not rows:
                return f"No anomalies detected in the last {hours:g}h."
            lines = [f"{len(rows)} anomalies in the last {hours:g}h:"]
            for row in rows[:30]:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["ts"]))
                lat = "n/a" if row["latency_ms"] is None else f"{row['latency_ms']:.0f} ms"
                lines.append(
                    f"- {stamp} | {row['target']} | {row['method']} | {lat} | "
                    f"{row['description']}"
                )
            if len(rows) > 30:
                lines.append(f"... and {len(rows) - 30} more")
            return "\n".join(lines)

    return server


__all__ = ["build_server"]
