"""SQLite persistence for probe samples and detected anomalies.

Plain ``sqlite3`` from the standard library — no ORM. The schema is two
tables: raw ``samples`` (one row per probe) and ``anomalies`` (one row
per detector hit). Everything stays in a single local file; Merezha
never sends your measurements anywhere.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .probes import ProbeResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    host TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts REAL NOT NULL,
    success INTEGER NOT NULL,
    latency_ms REAL,
    dns_ms REAL,
    tcp_ms REAL,
    tls_ms REAL,
    ttfb_ms REAL,
    status_code INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_target_ts ON samples (target, ts);

CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    ts REAL NOT NULL,
    latency_ms REAL,
    score REAL,
    method TEXT NOT NULL,
    description TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anomalies_target_ts ON anomalies (target, ts);
"""


@dataclass(frozen=True)
class TargetStats:
    """Aggregated statistics for one target over a time window."""

    target: str
    samples: int
    failures: int
    avg_ms: float | None
    p95_ms: float | None
    min_ms: float | None
    max_ms: float | None

    @property
    def loss_pct(self) -> float:
        return 100.0 * self.failures / self.samples if self.samples else 0.0


def _percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile on an already sorted list."""
    idx = round(q * (len(sorted_values) - 1))
    return sorted_values[idx]


class Storage:
    """Thin wrapper around a single SQLite connection."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- writes ------------------------------------------------------------

    def insert_sample(self, r: ProbeResult) -> None:
        p = r.phases
        self._conn.execute(
            "INSERT INTO samples (target, host, kind, ts, success, latency_ms,"
            " dns_ms, tcp_ms, tls_ms, ttfb_ms, status_code, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r.target,
                r.host,
                r.kind,
                r.ts,
                int(r.success),
                r.latency_ms,
                p.get("dns"),
                p.get("tcp"),
                p.get("tls"),
                p.get("ttfb"),
                r.status_code,
                r.error,
            ),
        )
        self._conn.commit()

    def insert_anomaly(
        self,
        target: str,
        ts: float,
        latency_ms: float | None,
        score: float | None,
        method: str,
        description: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO anomalies (target, ts, latency_ms, score, method, description)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (target, ts, latency_ms, score, method, description),
        )
        self._conn.commit()

    # -- reads -------------------------------------------------------------

    def targets(self) -> list[str]:
        rows = self._conn.execute("SELECT DISTINCT target FROM samples ORDER BY target").fetchall()
        return [row["target"] for row in rows]

    def recent_samples(self, target: str, limit: int = 120) -> list[sqlite3.Row]:
        """Last *limit* samples for *target*, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM samples WHERE target = ? ORDER BY ts DESC LIMIT ?",
            (target, limit),
        ).fetchall()
        return list(reversed(rows))

    def latest_sample(self, target: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM samples WHERE target = ? ORDER BY ts DESC LIMIT 1",
            (target,),
        ).fetchone()

    def recent_anomalies(self, hours: float = 24.0, target: str | None = None) -> list[sqlite3.Row]:
        since = time.time() - hours * 3600.0
        if target is None:
            rows = self._conn.execute(
                "SELECT * FROM anomalies WHERE ts >= ? ORDER BY ts DESC", (since,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM anomalies WHERE ts >= ? AND target = ? ORDER BY ts DESC",
                (since, target),
            ).fetchall()
        return rows

    def stats(self, target: str, hours: float = 1.0) -> TargetStats:
        since = time.time() - hours * 3600.0
        rows = self._conn.execute(
            "SELECT success, latency_ms FROM samples WHERE target = ? AND ts >= ?",
            (target, since),
        ).fetchall()
        total = len(rows)
        failures = sum(1 for row in rows if not row["success"])
        latencies = sorted(
            row["latency_ms"] for row in rows if row["success"] and row["latency_ms"] is not None
        )
        if not latencies:
            return TargetStats(target, total, failures, None, None, None, None)
        return TargetStats(
            target=target,
            samples=total,
            failures=failures,
            avg_ms=sum(latencies) / len(latencies),
            p95_ms=_percentile(latencies, 0.95),
            min_ms=latencies[0],
            max_ms=latencies[-1],
        )
