"""ConfidenceCombiner: weighted blend + deterministic inputs_hash."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from solalpha.domain import DetectorSignal
from solalpha.foundation.clock import FakeClock
from solalpha.foundation.config import SignalsWeightsConfig
from solalpha.signal.combiner import ConfidenceCombiner

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
_WEIGHTS = SignalsWeightsConfig(prepump=0.35, cluster=0.45, flow_anomaly=0.20)


def _ds(detector: str, score: float) -> DetectorSignal:
    return DetectorSignal(detector=detector, mint="M1", score=score, observed_at=_NOW)


async def test_requires_min_distinct_detectors() -> None:
    comb = ConfidenceCombiner(_WEIGHTS, min_distinct_detectors=2)
    await comb.add(_ds("cluster", 0.9))
    # Only one detector -> no signal.
    assert comb.poll(FakeClock(start=_NOW).now()) == []


async def test_combines_two_detectors() -> None:
    comb = ConfidenceCombiner(_WEIGHTS, min_distinct_detectors=2)
    await comb.add(_ds("cluster", 1.0))
    await comb.add(_ds("prepump", 1.0))
    signals = comb.poll(FakeClock(start=_NOW).now())
    assert len(signals) == 1
    s = signals[0]
    assert s.mint == "M1"
    assert s.direction == "buy"
    assert 0.0 < s.confidence <= 1.0
    assert len(s.detectors) == 2


async def test_inputs_hash_deterministic() -> None:
    comb1 = ConfidenceCombiner(_WEIGHTS, min_distinct_detectors=2)
    await comb1.add(_ds("cluster", 0.8))
    await comb1.add(_ds("prepump", 0.6))
    h1 = comb1.poll(FakeClock(start=_NOW).now())[0].inputs_hash

    comb2 = ConfidenceCombiner(_WEIGHTS, min_distinct_detectors=2)
    await comb2.add(_ds("cluster", 0.8))
    await comb2.add(_ds("prepump", 0.6))
    h2 = comb2.poll(FakeClock(start=_NOW).now())[0].inputs_hash

    assert h1 == h2, "identical detector inputs must hash identically"


async def test_inputs_hash_changes_with_score() -> None:
    comb1 = ConfidenceCombiner(_WEIGHTS, min_distinct_detectors=2)
    await comb1.add(_ds("cluster", 0.8))
    await comb1.add(_ds("prepump", 0.6))
    h1 = comb1.poll(FakeClock(start=_NOW).now())[0].inputs_hash

    comb2 = ConfidenceCombiner(_WEIGHTS, min_distinct_detectors=2)
    await comb2.add(_ds("cluster", 0.9))  # different score
    await comb2.add(_ds("prepump", 0.6))
    h2 = comb2.poll(FakeClock(start=_NOW).now())[0].inputs_hash

    assert h1 != h2


async def test_confidence_is_weighted_blend() -> None:
    # Both detectors score 1.0 -> weighted blend should be 1.0.
    comb = ConfidenceCombiner(_WEIGHTS, min_distinct_detectors=2)
    await comb.add(_ds("cluster", 1.0))
    await comb.add(_ds("flow_anomaly", 1.0))
    s = comb.poll(FakeClock(start=_NOW).now())[0]
    assert abs(s.confidence - 1.0) < 1e-6
