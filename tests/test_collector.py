"""End-to-end test: collector probes a real local server, persists and detects."""

from __future__ import annotations

import asyncio
from pathlib import Path

from merezha.anomaly import HybridDetector
from merezha.collector import Collector
from merezha.config import Config, Target
from merezha.storage import Storage


async def _silent_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.close()


async def test_collector_round_trip(tmp_path: Path) -> None:
    server = await asyncio.start_server(_silent_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    config = Config(
        interval=0.01,
        targets=[Target("local", "127.0.0.1", port, "tcp")],
        data_dir=tmp_path,
    )
    ticks: list[list] = []

    async with server:
        with Storage(config.db_path) as storage:
            collector = Collector(config, storage, HybridDetector())
            await collector.run(on_tick=ticks.append, max_rounds=3)

            assert len(ticks) == 3
            stats = storage.stats("local", hours=1.0)
            assert stats.samples == 3
            assert stats.failures == 0
            # 3 samples is below the warmup threshold -> nothing should be flagged
            assert storage.recent_anomalies(hours=1.0) == []
            result, verdict = ticks[-1][0]
            assert result.success
            assert verdict.method == "warmup"


async def test_collector_records_failures_as_anomalies(tmp_path: Path) -> None:
    server = await asyncio.start_server(_silent_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()  # nothing listens here any more

    config = Config(
        interval=0.01,
        targets=[Target("dead", "127.0.0.1", port, "tcp")],
        data_dir=tmp_path,
    )
    with Storage(config.db_path) as storage:
        collector = Collector(config, storage, HybridDetector())
        await collector.run(max_rounds=2)

        stats = storage.stats("dead", hours=1.0)
        assert stats.samples == 2
        assert stats.failures == 2
        anomalies = storage.recent_anomalies(hours=1.0)
        assert len(anomalies) == 2
        assert all(row["method"] == "failure" for row in anomalies)
