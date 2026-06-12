"""Hybrid anomaly detection for latency time series.

Two detectors cooperate:

1. **Robust z-score** (median + MAD). Works from the very first samples
   and — unlike a plain mean/std z-score — is not skewed by the very
   outliers it is trying to catch.
2. **IsolationForest** (scikit-learn). Activates once enough history
   exists and looks at a small multivariate feature vector
   ``[latency, jitter, sin(hour), cos(hour)]``, which lets it catch
   *contextual* anomalies: latency that is only moderately high but
   unusual together with its jitter and time of day.

Decision rule:

* a failed probe is always an anomaly (``method="failure"``);
* a sample is flagged when the robust z-score alone is above the
  threshold (``method="robust_z"``), **or**
* when IsolationForest flags it *and* the z-score is at least 60% of
  the threshold (``method="hybrid"``).

Requiring agreement at the lower confidence level keeps false positives
down: IsolationForest with ``contamination=0.02`` would otherwise flag
~2% of perfectly normal samples by construction.

Models are kept in memory and retrained every ``retrain_every`` samples
per target — with the small per-target windows Merezha works on, a full
retrain costs milliseconds and avoids stale-model and pickle-versioning
problems entirely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
from sklearn.ensemble import IsolationForest

WARMUP_SAMPLES = 8


@dataclass(frozen=True)
class Verdict:
    """Detector decision for the newest sample of a target."""

    is_anomaly: bool
    score: float | None  # robust z-score of the newest sample (None for failures)
    method: str  # "ok" | "warmup" | "failure" | "robust_z" | "hybrid"
    description: str


def robust_z(value: float, history: np.ndarray) -> float:
    """Robust z-score of *value* against *history* using median/MAD.

    The 0.6745 factor makes MAD a consistent estimator of the standard
    deviation under a normal distribution, so thresholds stay comparable
    to classic z-scores.
    """
    med = float(np.median(history))
    mad = float(np.median(np.abs(history - med)))
    if mad > 0.0:
        return 0.6745 * (value - med) / mad
    std = float(history.std())
    if std > 0.0:
        return (value - med) / std
    return 0.0


def build_features(ts: np.ndarray, latencies: np.ndarray) -> np.ndarray:
    """Feature matrix ``[latency, jitter, sin(hour), cos(hour)]``.

    Hour-of-day is encoded on the unit circle so 23:00 and 01:00 are
    close together, letting the forest learn daily patterns (an evening
    Wi-Fi slowdown that is normal at 21:00 may be anomalous at 04:00).
    """
    jitter = np.abs(np.diff(latencies, prepend=latencies[0]))
    local = [time.localtime(t) for t in ts]
    hours = np.array([lt.tm_hour + lt.tm_min / 60.0 for lt in local])
    angle = 2.0 * np.pi * hours / 24.0
    return np.column_stack([latencies, jitter, np.sin(angle), np.cos(angle)])


@dataclass
class HybridDetector:
    z_threshold: float = 3.5
    min_train_samples: int = 120
    retrain_every: int = 50
    contamination: float = 0.02

    _models: dict[str, IsolationForest] = field(default_factory=dict, repr=False)
    _trained_at: dict[str, int] = field(default_factory=dict, repr=False)

    def evaluate(
        self,
        target: str,
        ts: list[float],
        latencies: list[float],
        success: bool,
    ) -> Verdict:
        """Judge the newest sample of *target*.

        ``ts``/``latencies`` are the chronological history of successful
        samples *including* the newest one.
        """
        if not success:
            return Verdict(True, None, "failure", "probe failed (timeout or connection error)")

        if len(latencies) < WARMUP_SAMPLES:
            return Verdict(False, 0.0, "warmup", "collecting baseline")

        lat = np.asarray(latencies, dtype=float)
        tss = np.asarray(ts, dtype=float)
        value = float(lat[-1])
        z = robust_z(value, lat[:-1])

        ml_flag = False
        if len(lat) >= self.min_train_samples:
            model = self._maybe_train(target, tss, lat)
            features = build_features(tss, lat)
            ml_flag = bool(model.predict(features[-1:])[0] == -1)

        if z >= self.z_threshold:
            return Verdict(
                True,
                z,
                "robust_z",
                f"latency {value:.0f} ms is {z:.1f} robust sigma above baseline",
            )
        if ml_flag and z >= 0.6 * self.z_threshold:
            return Verdict(
                True,
                z,
                "hybrid",
                f"IsolationForest + elevated z-score ({z:.1f}) flagged a contextual anomaly",
            )
        return Verdict(False, z, "ok", "within baseline")

    # -- internals ----------------------------------------------------------

    def _maybe_train(self, target: str, ts: np.ndarray, lat: np.ndarray) -> IsolationForest:
        """Return a model for *target*, retraining when enough new data arrived.

        The newest sample is excluded from training so the model never
        scores a point it has just memorised.
        """
        n = len(lat)
        model = self._models.get(target)
        last_n = self._trained_at.get(target, 0)
        if model is None or n - last_n >= self.retrain_every:
            features = build_features(ts[:-1], lat[:-1])
            model = IsolationForest(
                n_estimators=100,
                contamination=self.contamination,
                random_state=42,
            )
            model.fit(features)
            self._models[target] = model
            self._trained_at[target] = n
        return model
