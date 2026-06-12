"""Detector tests on synthetic latency series with controlled randomness."""

from __future__ import annotations

import time

import numpy as np

from merezha.anomaly import HybridDetector, build_features, robust_z


def make_series(
    n: int, base: float = 20.0, noise: float = 1.5, seed: int = 7
) -> tuple[list[float], list[float]]:
    rng = np.random.default_rng(seed)
    latencies = np.clip(base + rng.normal(0.0, noise, n), 1.0, None).tolist()
    now = time.time()
    ts = [now - (n - i) * 10.0 for i in range(n)]
    return ts, latencies


def test_robust_z_ignores_outliers_in_history() -> None:
    history = np.array([20.0] * 50 + [500.0])  # one huge outlier in the baseline
    z_clean = robust_z(21.0, np.array([20.0] * 50))
    z_dirty = robust_z(21.0, history)
    # The MAD-based score barely moves when the baseline is contaminated.
    assert abs(z_dirty - z_clean) < 1.0


def test_spike_is_flagged_by_robust_z() -> None:
    detector = HybridDetector(min_train_samples=10_000)  # force the statistical path
    ts, latencies = make_series(150)
    ts.append(time.time())
    latencies.append(200.0)
    verdict = detector.evaluate("t", ts, latencies, success=True)
    assert verdict.is_anomaly
    assert verdict.method == "robust_z"
    assert verdict.score is not None and verdict.score > 3.5


def test_normal_sample_passes() -> None:
    detector = HybridDetector(min_train_samples=10_000)
    ts, latencies = make_series(150)
    ts.append(time.time())
    latencies.append(20.4)
    verdict = detector.evaluate("t", ts, latencies, success=True)
    assert not verdict.is_anomaly
    assert verdict.method == "ok"


def test_failed_probe_is_always_an_anomaly() -> None:
    detector = HybridDetector()
    verdict = detector.evaluate("t", [], [], success=False)
    assert verdict.is_anomaly
    assert verdict.method == "failure"
    assert verdict.score is None


def test_warmup_is_never_flagged() -> None:
    detector = HybridDetector()
    ts, latencies = make_series(5)
    verdict = detector.evaluate("t", ts, latencies, success=True)
    assert not verdict.is_anomaly
    assert verdict.method == "warmup"


def test_isolation_forest_trains_and_caches() -> None:
    detector = HybridDetector(min_train_samples=120, retrain_every=50)
    ts, latencies = make_series(200)
    detector.evaluate("t", ts, latencies, success=True)
    assert "t" in detector._models
    first_model = detector._models["t"]

    # A couple more samples should NOT trigger a retrain yet.
    ts2, lat2 = make_series(205)
    detector.evaluate("t", ts2, lat2, success=True)
    assert detector._models["t"] is first_model

    # ... but +retrain_every samples should.
    ts3, lat3 = make_series(260)
    detector.evaluate("t", ts3, lat3, success=True)
    assert detector._models["t"] is not first_model


def test_build_features_shape_and_jitter() -> None:
    ts, latencies = make_series(30)
    features = build_features(np.asarray(ts), np.asarray(latencies))
    assert features.shape == (30, 4)
    assert features[0, 1] == 0.0  # first jitter value is defined as 0
    # hour-of-day encoding stays on the unit circle
    assert np.allclose(features[:, 2] ** 2 + features[:, 3] ** 2, 1.0)
