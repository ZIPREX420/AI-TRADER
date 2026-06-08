"""Replay / walk-forward metrics.

Pure math: given a sequence of closed trades, compute the headline
metrics used to compare strategies and gate walk-forward releases:

  * `sharpe`         -- annualised Sharpe over per-trade returns.
  * `hit_rate`       -- fraction of trades with realized_pnl > 0.
  * `max_drawdown`   -- worst peak-to-trough on cumulative PnL.
  * `total_pnl_usd`  -- sum of realized PnL over the session.
  * `n_trades`       -- count of closed trades.
  * `exposure_s`     -- total time a position was open (seconds).
  * `turnover_usd`   -- total notional traded across all sides.

The walk-forward harness gates promotion on `sharpe >= min_oos_sharpe`
(configured per `research` section).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Iterable

    from solalpha.domain import Fill

# Assume one trade ~ one day for annualisation purposes; the absolute
# Sharpe magnitude is informational, the *relative* ranking is what
# walk-forward uses to compare configurations.
_ANNUALISATION_FACTOR = math.sqrt(252.0)


class TradeReturn(BaseModel):
    """One closed round-trip used as the input to the metrics calculator."""

    model_config = ConfigDict(frozen=True)

    realized_pnl_usd: float
    entry_usd: float
    exit_usd: float
    open_seconds: float


class SessionMetrics(BaseModel):
    """The numbers a walk-forward run reports per fold."""

    model_config = ConfigDict(frozen=True)

    n_trades: int
    total_pnl_usd: float
    hit_rate: float
    sharpe: float
    max_drawdown: float
    exposure_s: float
    turnover_usd: float


def compute_session_metrics(trades: Iterable[TradeReturn]) -> SessionMetrics:
    """Aggregate per-trade returns into a `SessionMetrics`."""
    trade_list = list(trades)
    if not trade_list:
        return SessionMetrics(
            n_trades=0,
            total_pnl_usd=0.0,
            hit_rate=0.0,
            sharpe=0.0,
            max_drawdown=0.0,
            exposure_s=0.0,
            turnover_usd=0.0,
        )
    pnls = [t.realized_pnl_usd for t in trade_list]
    returns = [t.realized_pnl_usd / t.entry_usd if t.entry_usd > 0 else 0.0 for t in trade_list]
    total = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    hit = wins / len(pnls)
    sharpe = _sharpe(returns)
    drawdown = _max_drawdown(pnls)
    exposure = sum(t.open_seconds for t in trade_list)
    turnover = sum(t.entry_usd + t.exit_usd for t in trade_list)
    return SessionMetrics(
        n_trades=len(pnls),
        total_pnl_usd=total,
        hit_rate=hit,
        sharpe=sharpe,
        max_drawdown=drawdown,
        exposure_s=exposure,
        turnover_usd=turnover,
    )


def trades_from_fills(fills: Iterable[Fill], directions: dict[str, str]) -> list[TradeReturn]:
    """Pair buys with sells per mint to derive closed `TradeReturn`s.

    `directions` maps `fill.order_id` to the parent order's direction so we
    can split fills into entries and exits without a second SQL round-trip.
    Buys open / sells close; FIFO pairing within a mint.
    """
    by_mint: dict[str, list[Fill]] = {}
    for f in fills:
        if not f.usd_value:
            continue
        by_mint.setdefault(_route_mint(f), []).append(f)
    out: list[TradeReturn] = []
    for fills_for_mint in by_mint.values():
        fills_for_mint.sort(key=lambda f: f.block_time)
        open_buys: list[Fill] = []
        for f in fills_for_mint:
            d = directions.get(f.order_id, "buy")
            if d == "buy":
                open_buys.append(f)
                continue
            # sell: pair with the oldest open buy.
            if not open_buys:
                # Naked sell -- realise against zero entry (paper edge case).
                out.append(
                    TradeReturn(
                        realized_pnl_usd=float(f.usd_value or 0.0),
                        entry_usd=0.0,
                        exit_usd=float(f.usd_value or 0.0),
                        open_seconds=0.0,
                    )
                )
                continue
            buy = open_buys.pop(0)
            entry = float(buy.usd_value or 0.0)
            exit_usd = float(f.usd_value or 0.0)
            pnl = exit_usd - entry
            open_s = max(0.0, (f.block_time - buy.block_time).total_seconds())
            out.append(
                TradeReturn(
                    realized_pnl_usd=pnl,
                    entry_usd=entry,
                    exit_usd=exit_usd,
                    open_seconds=open_s,
                )
            )
    return out


def _route_mint(fill: Fill) -> str:
    # The route doesn't carry mint directly; we treat order_id as the
    # bucket key so per-order pairs stay together. The caller may pre-
    # filter by parent-order mint.
    return fill.order_id


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if var <= 0:
        return 0.0
    return (mean / math.sqrt(var)) * _ANNUALISATION_FACTOR


def _max_drawdown(pnls: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    worst = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > worst:
            worst = drawdown
    return worst


__all__ = [
    "SessionMetrics",
    "TradeReturn",
    "compute_session_metrics",
    "trades_from_fills",
]
