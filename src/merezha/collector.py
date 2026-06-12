"""The monitoring loop: probe -> persist -> detect -> notify.

The :class:`Collector` runs all probes of a round concurrently with
``asyncio.gather``, writes every result to SQLite, asks the detector to
judge it against that target's recent history, persists anomalies and
finally hands the round to an optional callback (the dashboard).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable

from .anomaly import HybridDetector, Verdict
from .config import Config
from .probes import ProbeResult, run_probe
from .storage import Storage

# How many recent samples the detector sees per target. Large enough for a
# stable baseline, small enough that retraining stays effectively free.
DETECTOR_WINDOW = 360

TickCallback = Callable[[list[tuple[ProbeResult, Verdict]]], None]


class Collector:
    def __init__(self, config: Config, storage: Storage, detector: HybridDetector) -> None:
        self.config = config
        self.storage = storage
        self.detector = detector

    async def run(
        self,
        on_tick: TickCallback | None = None,
        max_rounds: int | None = None,
    ) -> None:
        """Run probe rounds forever (or for *max_rounds* rounds)."""
        rounds = 0
        while True:
            results = await asyncio.gather(*(run_probe(t) for t in self.config.targets))
            tick: list[tuple[ProbeResult, Verdict]] = []
            for result in results:
                self.storage.insert_sample(result)
                verdict = self._detect(result)
                tick.append((result, verdict))
            if on_tick is not None:
                on_tick(tick)
            rounds += 1
            if max_rounds is not None and rounds >= max_rounds:
                return
            await asyncio.sleep(self.config.interval)

    def _detect(self, result: ProbeResult) -> Verdict:
        rows = self.storage.recent_samples(result.target, limit=DETECTOR_WINDOW)
        ts = [row["ts"] for row in rows if row["success"] and row["latency_ms"] is not None]
        latencies = [
            row["latency_ms"] for row in rows if row["success"] and row["latency_ms"] is not None
        ]
        verdict = self.detector.evaluate(result.target, ts, latencies, result.success)
        if verdict.is_anomaly:
            score = verdict.score
            if score is not None and not math.isfinite(score):
                score = None
            self.storage.insert_anomaly(
                target=result.target,
                ts=result.ts,
                latency_ms=result.latency_ms,
                score=score,
                method=verdict.method,
                description=verdict.description,
            )
        return verdict
