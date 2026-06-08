"""PortfolioSizer: confidence ramp, per-trade cap, DEGRADED_RPC factor."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from solalpha.domain import DetectorSignal, Signal
from solalpha.signal.sizer import PortfolioSizer

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)


def _signal(confidence: float) -> Signal:
    ds = DetectorSignal(detector="cluster", mint="M1", score=confidence, observed_at=_NOW)
    return Signal(
        signal_id="sg-1",
        created_at=_NOW,
        mint="M1",
        direction="buy",
        detectors=(ds,),
        confidence=confidence,
        suggested_usd=0.0,
        rationale="test",
        inputs_hash="h",
        trace_id="t-1",
    )


class _StubMode:
    def __init__(self, mode: str = "PAPER") -> None:
        self.mode = mode


def test_below_min_confidence_sizes_zero(app_config: object) -> None:
    sizer = PortfolioSizer(app_config, _StubMode())  # type: ignore[arg-type]
    sized = sizer.size(_signal(0.10))  # min_confidence default 0.55
    assert sized.suggested_usd == 0.0


def test_full_confidence_sizes_to_equity_slice(app_config: object) -> None:
    sizer = PortfolioSizer(app_config, _StubMode())  # type: ignore[arg-type]
    sized = sizer.size(_signal(1.0))
    # equity * per_trade_pct = 1000 * 0.02 = 20, multiplier == 1.0 at conf 1.0.
    assert abs(sized.suggested_usd - 20.0) < 1e-6


def test_capped_by_per_trade_usd_cap(app_config: object) -> None:
    cfg = app_config.model_copy(  # type: ignore[attr-defined]
        update={"risk": app_config.risk.model_copy(update={"per_trade_usd_cap": 5.0})}  # type: ignore[attr-defined]
    )
    sizer = PortfolioSizer(cfg, _StubMode())
    sized = sizer.size(_signal(1.0))
    assert sized.suggested_usd <= 5.0


def test_degraded_rpc_applies_size_factor(app_config: object) -> None:
    full = PortfolioSizer(app_config, _StubMode("PAPER")).size(_signal(1.0))  # type: ignore[arg-type]
    degraded = PortfolioSizer(app_config, _StubMode("DEGRADED_RPC")).size(_signal(1.0))  # type: ignore[arg-type]
    # degraded_rpc_size_factor default is 0.5.
    assert abs(degraded.suggested_usd - full.suggested_usd * 0.5) < 1e-6


def test_confidence_ramp_is_monotonic(app_config: object) -> None:
    sizer = PortfolioSizer(app_config, _StubMode())  # type: ignore[arg-type]
    low = sizer.size(_signal(0.6)).suggested_usd
    high = sizer.size(_signal(0.9)).suggested_usd
    assert 0.0 < low < high
