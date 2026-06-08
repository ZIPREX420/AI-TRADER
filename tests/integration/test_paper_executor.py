"""PaperExecutor fill math + PortfolioTracker position bookkeeping."""

from __future__ import annotations

import pytest

from solalpha.domain import OrderIntent
from solalpha.execution.paper_executor import PaperExecutor
from solalpha.observability.portfolio import PortfolioTracker

pytestmark = pytest.mark.integration


def _intent(direction: str = "buy", usd: float = 100.0, mint: str = "M1") -> OrderIntent:
    return OrderIntent(
        signal_id="sg-1",
        mint=mint,
        direction=direction,  # type: ignore[arg-type]
        intended_usd=usd,
        intended_input_amount_raw=1_000_000,
        max_slippage_bps=150,
        trace_id="t-1",
    )


async def test_paper_fill_applies_slippage(clock: object, app_config: object) -> None:
    ex = PaperExecutor(app_config, clock)  # type: ignore[arg-type]
    order, fill = await ex.execute(_intent())
    assert order.status == "confirmed"
    assert fill.order_id == order.order_id
    # Output is the input less total slippage -> strictly smaller.
    assert 0 < fill.output_amount_raw < fill.input_amount_raw
    assert fill.realized_slippage_bps > 0


async def test_portfolio_buy_then_sell_realizes_pnl(
    store: object, clock: object, app_config: object
) -> None:
    ex = PaperExecutor(app_config, clock)  # type: ignore[arg-type]
    pt = PortfolioTracker(store, clock)  # type: ignore[arg-type]
    await pt.load()

    buy_order, buy_fill = await ex.execute(_intent("buy", 100.0, "MX"))
    pos = await pt.apply_fill(buy_fill, buy_order)
    assert pos.state == "open"
    assert pos.cost_basis_usd == 100.0
    assert pt.open_positions_count() == 1

    sell_order, sell_fill = await ex.execute(_intent("sell", 150.0, "MX"))
    # Match the sell quantity to what the position holds.
    sell_fill = sell_fill.model_copy(update={"input_amount_raw": pos.quantity_raw})
    pos2 = await pt.apply_fill(sell_fill, sell_order)
    assert pos2.state == "closed"
    assert abs(pos2.realized_pnl_usd - 50.0) < 1e-6
    assert pt.open_positions_count() == 0

    dp = await pt.daily_pnl()
    assert dp.wins == 1
    assert dp.losses == 0
    assert dp.loss_streak == 0


async def test_loss_increments_streak(store: object, clock: object, app_config: object) -> None:
    ex = PaperExecutor(app_config, clock)  # type: ignore[arg-type]
    pt = PortfolioTracker(store, clock)  # type: ignore[arg-type]
    await pt.load()
    bo, bf = await ex.execute(_intent("buy", 100.0, "ML"))
    pos = await pt.apply_fill(bf, bo)
    so, sf = await ex.execute(_intent("sell", 40.0, "ML"))
    sf = sf.model_copy(update={"input_amount_raw": pos.quantity_raw})
    await pt.apply_fill(sf, so)
    dp = await pt.daily_pnl()
    assert dp.losses == 1
    assert dp.loss_streak == 1
