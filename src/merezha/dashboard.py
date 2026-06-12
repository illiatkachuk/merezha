"""Live terminal dashboard built with Rich.

One table row per target with status, latency stats, packet loss, a
Unicode sparkline of the recent latency trend and the current detector
verdict, plus a panel with the latest anomalies underneath.
"""

from __future__ import annotations

import time
from collections import deque

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table

from .anomaly import Verdict
from .config import Config
from .probes import ProbeResult
from .storage import Storage

SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 24) -> str:
    """Render *values* as a Unicode sparkline, newest sample on the right."""
    vals = values[-width:]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return SPARK_CHARS[0] * len(vals)
    scale = len(SPARK_CHARS) - 1
    return "".join(SPARK_CHARS[int((v - lo) / (hi - lo) * scale)] for v in vals)


def _fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} ms"


class Dashboard:
    def __init__(self, storage: Storage, config: Config) -> None:
        self.storage = storage
        self.config = config
        self.console = Console()
        self._verdicts: dict[str, Verdict] = {}
        self._anomaly_log: deque[str] = deque(maxlen=6)
        self._rounds = 0

    # -- collector callback --------------------------------------------------

    def on_tick(self, tick: list[tuple[ProbeResult, Verdict]]) -> None:
        self._rounds += 1
        for result, verdict in tick:
            self._verdicts[result.target] = verdict
            if verdict.is_anomaly:
                stamp = time.strftime("%H:%M:%S", time.localtime(result.ts))
                self._anomaly_log.appendleft(
                    f"[red]{stamp}[/red]  [bold]{result.target}[/bold]  {verdict.description}"
                )

    # -- rendering -------------------------------------------------------------

    def render(self) -> RenderableType:
        title = (
            f"[bold]merezha[/bold] · {len(self.config.targets)} targets · "
            f"round {self._rounds} · interval {self.config.interval:g}s · Ctrl+C to stop"
        )
        table = Table(title=title, title_justify="left", expand=True, header_style="bold")
        table.add_column("target")
        table.add_column("host", style="dim")
        table.add_column("status", justify="center")
        table.add_column("last", justify="right")
        table.add_column("avg 1h", justify="right")
        table.add_column("p95 1h", justify="right")
        table.add_column("loss 1h", justify="right")
        table.add_column("trend", no_wrap=True)
        table.add_column("detector")

        for target in self.config.targets:
            rows = self.storage.recent_samples(target.name, limit=self.config.history_window)
            stats = self.storage.stats(target.name, hours=1.0)
            last = rows[-1] if rows else None

            if last is None:
                status = "[dim]…[/dim]"
                last_ms = "—"
            elif last["success"]:
                status = "[green]● up[/green]"
                last_ms = _fmt_ms(last["latency_ms"])
            else:
                status = "[red]● DOWN[/red]"
                last_ms = "—"

            series = [
                row["latency_ms"]
                for row in rows
                if row["success"] and row["latency_ms"] is not None
            ]
            loss = f"{stats.loss_pct:.0f}%" if stats.samples else "—"
            loss_cell = f"[red]{loss}[/red]" if stats.failures else loss

            table.add_row(
                f"[bold]{target.name}[/bold]",
                f"{target.host}:{target.port}",
                status,
                last_ms,
                _fmt_ms(stats.avg_ms),
                _fmt_ms(stats.p95_ms),
                loss_cell,
                f"[cyan]{sparkline(series)}[/cyan]",
                self._verdict_cell(target.name),
            )

        body = "\n".join(self._anomaly_log) if self._anomaly_log else "[dim]none detected yet[/dim]"
        panel = Panel(
            body,
            title="recent anomalies",
            border_style="red" if self._anomaly_log else "dim",
        )
        return Group(table, panel)

    def _verdict_cell(self, target_name: str) -> str:
        verdict = self._verdicts.get(target_name)
        if verdict is None:
            return "[dim]—[/dim]"
        if verdict.is_anomaly:
            return f"[red]⚠ {verdict.method}[/red]"
        if verdict.method == "warmup":
            return "[yellow]warming up[/yellow]"
        score = verdict.score if verdict.score is not None else 0.0
        return f"[green]ok[/green] [dim]z={score:+.1f}[/dim]"
