"""Storage tests against a temporary on-disk database."""

from __future__ import annotations

import time
from pathlib import Path

from merezha.probes import ProbeResult
from merezha.storage import Storage


def _result(target: str = "t", latency: float | None = 20.0, success: bool = True) -> ProbeResult:
    phases = {"dns": 5.0, "tcp": latency - 5.0} if success and latency is not None else {}
    return ProbeResult(
        target=target,
        host="example.com",
        kind="http",
        success=success,
        ts=time.time(),
        latency_ms=latency if success else None,
        phases=phases,
        status_code=200 if success else None,
        error=None if success else "timeout",
    )


def test_insert_and_read_back(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite") as storage:
        storage.insert_sample(_result(latency=42.0))
        rows = storage.recent_samples("t")
        assert len(rows) == 1
        row = rows[0]
        assert row["latency_ms"] == 42.0
        assert row["dns_ms"] == 5.0
        assert bool(row["success"]) is True
        assert storage.targets() == ["t"]


def test_recent_samples_order_and_limit(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite") as storage:
        for latency in (10.0, 20.0, 30.0, 40.0):
            storage.insert_sample(_result(latency=latency))
        rows = storage.recent_samples("t", limit=3)
        assert [row["latency_ms"] for row in rows] == [20.0, 30.0, 40.0]  # oldest first
        latest = storage.latest_sample("t")
        assert latest is not None and latest["latency_ms"] == 40.0


def test_stats_and_loss(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite") as storage:
        for latency in (10.0, 20.0, 30.0):
            storage.insert_sample(_result(latency=latency))
        storage.insert_sample(_result(success=False))
        stats = storage.stats("t", hours=1.0)
        assert stats.samples == 4
        assert stats.failures == 1
        assert stats.loss_pct == 25.0
        assert stats.avg_ms == 20.0
        assert stats.min_ms == 10.0 and stats.max_ms == 30.0
        assert stats.p95_ms == 30.0


def test_stats_empty_window(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite") as storage:
        stats = storage.stats("ghost", hours=1.0)
        assert stats.samples == 0
        assert stats.avg_ms is None
        assert stats.loss_pct == 0.0


def test_anomalies_roundtrip_and_window(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite") as storage:
        now = time.time()
        storage.insert_anomaly("t", now, 200.0, 5.2, "robust_z", "spike")
        storage.insert_anomaly("t", now - 48 * 3600, 180.0, 4.0, "robust_z", "old spike")
        recent = storage.recent_anomalies(hours=24.0)
        assert len(recent) == 1
        assert recent[0]["description"] == "spike"
        assert storage.recent_anomalies(hours=72.0, target="t")[-1]["description"] == "old spike"
