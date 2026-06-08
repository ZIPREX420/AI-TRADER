"""Each detector fires on its documented criterion."""

from __future__ import annotations

import pytest

from solalpha.foundation.clock import FakeClock
from solalpha.foundation.config import (
    SignalsClusterConfig,
    SignalsFlowAnomalyConfig,
    SignalsPrePumpConfig,
)
from solalpha.signal.detectors import (
    ClusterDetector,
    FlowAnomalyDetector,
    PrePumpDetector,
)
from solalpha.signal.smart_wallet_scorer import SmartWalletScorer

pytestmark = pytest.mark.unit


class _StubScorer:
    """A stand-in `SmartWalletScorer` that treats every wallet as smart."""

    def is_smart(self, _wallet: str) -> bool:
        return True

    def smart_set(self) -> frozenset[str]:
        return frozenset()


async def test_prepump_fires_on_buy_pressure(make_swap: object) -> None:
    cfg = SignalsPrePumpConfig(
        window_s=60, min_buy_pressure_ratio=2.0, min_liquidity_slope_pct_per_min=-1.0
    )
    d = PrePumpDetector(cfg)
    for i in range(20):
        await d.observe(make_swap(seconds_offset=10 + i, signature=f"s{i}", slot=i + 1))  # type: ignore[operator]
    clock = FakeClock()
    sigs = d.poll(clock.now())
    # Sufficient buy volume with no sells -> ratio is huge, slope positive.
    assert len(sigs) == 1
    assert sigs[0].detector == "prepump"
    assert sigs[0].features["buy_pressure_ratio"] > cfg.min_buy_pressure_ratio


async def test_cluster_requires_n_distinct_wallets(make_swap: object) -> None:
    cfg = SignalsClusterConfig(wallets_required=3, window_s=60, min_total_buy_usd=100.0)
    scorer = _StubScorer()
    d = ClusterDetector(cfg, scorer)  # type: ignore[arg-type]
    # Two wallets is below the threshold.
    await d.observe(make_swap(wallet="W1", seconds_offset=0))  # type: ignore[operator]
    await d.observe(make_swap(wallet="W2", seconds_offset=1, signature="s2"))  # type: ignore[operator]
    sigs = d.poll(FakeClock().now())
    assert sigs == []
    # Add a third wallet -> fires.
    await d.observe(make_swap(wallet="W3", seconds_offset=2, signature="s3"))  # type: ignore[operator]
    sigs = d.poll(FakeClock().now())
    assert len(sigs) == 1
    assert sigs[0].detector == "cluster"
    assert sigs[0].features["unique_smart_wallets"] >= 3


async def test_cluster_ignores_non_smart_wallets(make_swap: object) -> None:
    class _Strict:
        def is_smart(self, w: str) -> bool:
            return w == "W1"

        def smart_set(self) -> frozenset[str]:
            return frozenset({"W1"})

    cfg = SignalsClusterConfig(wallets_required=2, window_s=60, min_total_buy_usd=10.0)
    d = ClusterDetector(cfg, _Strict())  # type: ignore[arg-type]
    await d.observe(make_swap(wallet="W1", seconds_offset=0))  # type: ignore[operator]
    await d.observe(make_swap(wallet="W2", seconds_offset=1, signature="s2"))  # type: ignore[operator]
    # Only W1 counts -- single wallet, below threshold.
    assert d.poll(FakeClock().now()) == []


async def test_flow_anomaly_fires_on_spike(make_swap: object) -> None:
    cfg = SignalsFlowAnomalyConfig(baseline_window_s=120, z_threshold=2.0)
    d = FlowAnomalyDetector(cfg)
    # Calm baseline.
    for i in range(60):
        await d.observe(make_swap(seconds_offset=i, signature=f"b{i}", slot=i + 1, usd_value=5.0))  # type: ignore[operator]
    # Spike in the most recent bucket.
    for k in range(5):
        await d.observe(
            make_swap(seconds_offset=60, signature=f"spike{k}", slot=999 + k, usd_value=500.0)  # type: ignore[operator]
        )
    from datetime import timedelta

    clock = FakeClock()
    sigs = d.poll(clock.now() + timedelta(seconds=61))
    assert len(sigs) == 1
    assert sigs[0].features["z_score"] >= cfg.z_threshold


async def test_smart_wallet_scorer_is_smart_threshold(store: object, clock: FakeClock) -> None:
    # `store` is the connected fixture from conftest.
    await store.execute(  # type: ignore[attr-defined]
        "INSERT INTO smart_wallets (wallet, added_at, source, weight, score, last_active_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Wsmart", clock.now().isoformat(), "test", 1.0, 0.9, clock.now().isoformat()),
    )
    await store.execute(  # type: ignore[attr-defined]
        "INSERT INTO smart_wallets (wallet, added_at, source, weight, score, last_active_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Wdumb", clock.now().isoformat(), "test", 1.0, 0.05, clock.now().isoformat()),
    )
    scorer = SmartWalletScorer(
        store,
        clock,
        min_score_smart=0.2,
        decay_half_life_days=365.0,  # type: ignore[arg-type]
    )
    await scorer.refresh()
    assert scorer.is_smart("Wsmart")
    assert not scorer.is_smart("Wdumb")
    assert "Wsmart" in scorer.smart_set()
