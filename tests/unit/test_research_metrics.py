"""research.metrics: Sharpe / hit-rate / drawdown math."""

from __future__ import annotations

import pytest

from solalpha.research.metrics import TradeReturn, compute_session_metrics

pytestmark = pytest.mark.unit


def test_empty_session_is_zeroed() -> None:
    m = compute_session_metrics([])
    assert m.n_trades == 0
    assert m.total_pnl_usd == 0.0
    assert m.sharpe == 0.0
    assert m.max_drawdown == 0.0


def test_total_pnl_and_hit_rate() -> None:
    m = compute_session_metrics(
        [
            TradeReturn(realized_pnl_usd=50.0, entry_usd=100.0, exit_usd=150.0, open_seconds=60.0),
            TradeReturn(realized_pnl_usd=-20.0, entry_usd=100.0, exit_usd=80.0, open_seconds=30.0),
            TradeReturn(realized_pnl_usd=10.0, entry_usd=50.0, exit_usd=60.0, open_seconds=45.0),
        ]
    )
    assert m.n_trades == 3
    assert abs(m.total_pnl_usd - 40.0) < 1e-9
    assert abs(m.hit_rate - 2 / 3) < 1e-9


def test_max_drawdown() -> None:
    # Cumulative: +100, +50 (dd 50), +150 (peak), +100 (dd 50), -50 (dd 200).
    m = compute_session_metrics(
        [
            TradeReturn(realized_pnl_usd=100.0, entry_usd=100.0, exit_usd=200.0, open_seconds=1.0),
            TradeReturn(realized_pnl_usd=-50.0, entry_usd=100.0, exit_usd=50.0, open_seconds=1.0),
            TradeReturn(realized_pnl_usd=100.0, entry_usd=100.0, exit_usd=200.0, open_seconds=1.0),
            TradeReturn(realized_pnl_usd=-50.0, entry_usd=100.0, exit_usd=50.0, open_seconds=1.0),
            TradeReturn(realized_pnl_usd=-150.0, entry_usd=200.0, exit_usd=50.0, open_seconds=1.0),
        ]
    )
    assert abs(m.max_drawdown - 200.0) < 1e-9


def test_exposure_and_turnover() -> None:
    m = compute_session_metrics(
        [
            TradeReturn(realized_pnl_usd=10.0, entry_usd=100.0, exit_usd=110.0, open_seconds=60.0),
            TradeReturn(realized_pnl_usd=10.0, entry_usd=100.0, exit_usd=110.0, open_seconds=90.0),
        ]
    )
    assert abs(m.exposure_s - 150.0) < 1e-9
    assert abs(m.turnover_usd - 420.0) < 1e-9  # (100+110) * 2


def test_positive_returns_give_positive_sharpe() -> None:
    m = compute_session_metrics(
        [
            TradeReturn(realized_pnl_usd=10.0, entry_usd=100.0, exit_usd=110.0, open_seconds=1.0),
            TradeReturn(realized_pnl_usd=12.0, entry_usd=100.0, exit_usd=112.0, open_seconds=1.0),
            TradeReturn(realized_pnl_usd=8.0, entry_usd=100.0, exit_usd=108.0, open_seconds=1.0),
        ]
    )
    assert m.sharpe > 0.0
