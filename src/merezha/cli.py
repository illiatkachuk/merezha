"""Merezha command-line interface."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .anomaly import HybridDetector
from .collector import Collector
from .config import DEFAULT_CONFIG_PATH, Config, default_db_path, load_config, write_template
from .dashboard import Dashboard
from .probes import PHASE_ORDER, ProbeResult, http_probe, tcp_probe
from .storage import Storage

app = typer.Typer(
    help="Merezha — AI-native network observability.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

ConfigOption = Annotated[
    Path,
    typer.Option("--config", "-c", help="Path to merezha.toml (defaults are used if missing)."),
]
DbOption = Annotated[
    Path | None,
    typer.Option("--db", help="Path to the SQLite database (default: ~/.merezha/merezha.db)."),
]


def _open_storage(config: Config, db: Path | None) -> Storage:
    return Storage(db if db is not None else config.db_path)


# --------------------------------------------------------------------------- init


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Where to write the config.")] = DEFAULT_CONFIG_PATH,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a starter merezha.toml into the current directory."""
    if write_template(path, force=force):
        console.print(f"[green]Wrote[/green] {path} — edit your targets, then run "
                      f"[bold]merezha monitor[/bold].")
    else:
        console.print(f"[yellow]{path} already exists.[/yellow] Use --force to overwrite.")
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- check


@app.command()
def check(
    host: Annotated[str, typer.Argument(help="Hostname or IP to probe.")],
    port: Annotated[int, typer.Option("--port", "-p", help="TCP port.")] = 443,
    kind: Annotated[
        str, typer.Option("--kind", "-k", help="Probe kind: auto, http or tcp.")
    ] = "auto",
    json_out: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")] = False,
) -> None:
    """One-shot diagnostic with a DNS → TCP → TLS → TTFB phase breakdown."""
    kind = kind.lower()
    if kind not in {"auto", "http", "tcp"}:
        console.print("[red]--kind must be one of: auto, http, tcp[/red]")
        raise typer.Exit(code=2)
    if kind == "auto":
        kind = "http" if port in (80, 443) else "tcp"

    probe = http_probe if kind == "http" else tcp_probe
    result = asyncio.run(probe(host, host, port))

    if json_out:
        console.print_json(json.dumps(dataclasses.asdict(result)))
    else:
        console.print(_render_check(result, port))
    raise typer.Exit(code=0 if result.success else 1)


def _render_check(result: ProbeResult, port: int) -> Panel:
    title = f"{result.host}:{port} ({result.kind})"
    if not result.success:
        return Panel(f"[red]probe failed:[/red] {result.error}", title=title, border_style="red")

    phases = [(name, result.phases[name]) for name in PHASE_ORDER if name in result.phases]
    peak = max(value for _, value in phases)
    lines = []
    for name, value in phases:
        bar = "▇" * max(1, round(value / peak * 26))
        lines.append(f"[bold]{name.upper():<5}[/bold] [cyan]{bar:<26}[/cyan] {value:>8.1f} ms")
    lines.append("─" * 47)
    extras = []
    if result.status_code is not None:
        extras.append(f"HTTP {result.status_code}")
    if result.resolved_ip:
        extras.append(result.resolved_ip)
    lines.append(
        f"[bold]TOTAL[/bold] {'':<26} {result.latency_ms:>8.1f} ms   " + "   ".join(extras)
    )
    return Panel("\n".join(lines), title=title, border_style="green")


# --------------------------------------------------------------------------- monitor


@app.command()
def monitor(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    once: Annotated[
        bool, typer.Option("--once", help="Run a single probe round, print a snapshot and exit.")
    ] = False,
) -> None:
    """Continuously probe all targets with a live dashboard and anomaly detection."""
    cfg = load_config(config)
    if not config.exists():
        console.print(
            f"[dim]No {config} found — monitoring default targets. "
            f"Run [bold]merezha init[/bold] to customise.[/dim]"
        )
    try:
        asyncio.run(_run_monitor(cfg, once=once))
    except KeyboardInterrupt:
        console.print("\n[dim]stopped.[/dim]")


async def _run_monitor(cfg: Config, once: bool) -> None:
    with _open_storage(cfg, None) as storage:
        detector = HybridDetector(
            z_threshold=cfg.z_threshold, min_train_samples=cfg.min_train_samples
        )
        collector = Collector(cfg, storage, detector)
        dashboard = Dashboard(storage, cfg)

        if once:
            await collector.run(on_tick=dashboard.on_tick, max_rounds=1)
            console.print(dashboard.render())
            return

        with Live(dashboard.render(), console=dashboard.console, refresh_per_second=2) as live:

            def _on_tick(tick: list) -> None:
                dashboard.on_tick(tick)
                live.update(dashboard.render())

            await collector.run(on_tick=_on_tick)


# --------------------------------------------------------------------------- history


@app.command()
def history(
    target: Annotated[str, typer.Argument(help="Target name from your config.")],
    hours: Annotated[float, typer.Option("--hours", "-H", help="Window size in hours.")] = 24.0,
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    db: DbOption = None,
) -> None:
    """Stored statistics and recent samples for one target."""
    cfg = load_config(config)
    with _open_storage(cfg, db) as storage:
        stats = storage.stats(target, hours=hours)
        if stats.samples == 0:
            console.print(
                f"[yellow]No samples for '{target}' in the last {hours:g}h.[/yellow] "
                f"Known targets: {', '.join(storage.targets()) or '(none)'}"
            )
            raise typer.Exit(code=1)

        summary = (
            f"samples [bold]{stats.samples}[/bold] · failures [bold]{stats.failures}[/bold] "
            f"({stats.loss_pct:.1f}% loss) · avg [bold]{stats.avg_ms:.1f} ms[/bold] · "
            f"p95 [bold]{stats.p95_ms:.1f} ms[/bold] · "
            f"min/max {stats.min_ms:.1f}/{stats.max_ms:.1f} ms"
        )
        console.print(Panel(summary, title=f"{target} · last {hours:g}h"))

        table = Table(title="latest samples", header_style="bold")
        for col in ("time", "ok", "latency", "dns", "tcp", "tls", "ttfb", "error"):
            table.add_column(col, justify="right" if col not in ("time", "error") else "left")
        for row in storage.recent_samples(target, limit=12):
            table.add_row(
                time.strftime("%H:%M:%S", time.localtime(row["ts"])),
                "[green]✓[/green]" if row["success"] else "[red]✗[/red]",
                *(
                    "—" if row[key] is None else f"{row[key]:.1f}"
                    for key in ("latency_ms", "dns_ms", "tcp_ms", "tls_ms", "ttfb_ms")
                ),
                row["error"] or "",
            )
        console.print(table)

        anomalies = storage.recent_anomalies(hours=hours, target=target)
        colour = "red" if anomalies else "green"
        console.print(f"[{colour}]{len(anomalies)} anomalies in the last {hours:g}h.[/{colour}]")


# --------------------------------------------------------------------------- anomalies


@app.command()
def anomalies(
    hours: Annotated[float, typer.Option("--hours", "-H", help="Window size in hours.")] = 24.0,
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    db: DbOption = None,
) -> None:
    """Anomalies flagged by the detectors across all targets."""
    cfg = load_config(config)
    with _open_storage(cfg, db) as storage:
        rows = storage.recent_anomalies(hours=hours)
        if not rows:
            console.print(f"[green]No anomalies in the last {hours:g}h.[/green]")
            return
        table = Table(title=f"anomalies · last {hours:g}h", header_style="bold")
        for col in ("time", "target", "method", "latency", "z", "description"):
            table.add_column(col)
        for row in rows:
            table.add_row(
                time.strftime("%m-%d %H:%M:%S", time.localtime(row["ts"])),
                f"[bold]{row['target']}[/bold]",
                row["method"],
                "—" if row["latency_ms"] is None else f"{row['latency_ms']:.0f} ms",
                "—" if row["score"] is None else f"{row['score']:.1f}",
                row["description"],
            )
        console.print(table)


# --------------------------------------------------------------------------- mcp


@app.command()
def mcp(db: DbOption = None) -> None:
    """Start the MCP server (stdio) so AI assistants can query your network."""
    try:
        from .mcp_server import build_server
    except ImportError:
        console.print(
            "[red]The MCP extra is not installed.[/red] Run: pip install 'merezha[mcp]'"
        )
        raise typer.Exit(code=1) from None
    server = build_server(db if db is not None else default_db_path())
    server.run()


# --------------------------------------------------------------------------- version


@app.command()
def version() -> None:
    """Print the installed Merezha version."""
    console.print(f"merezha {__version__}")
