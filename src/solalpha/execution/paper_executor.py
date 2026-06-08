"""Paper-mode executor.

Simulates a fill against the configured `paper_executor` parameters:
  * `fee_bps`                  -- venue fee charged on the *output* side
  * `base_slippage_bps`        -- a fixed haircut applied to every fill
  * `impact_slippage_per_pct`  -- additional slippage_bps per 1% notional /
                                  pool liquidity (`min_pool_liquidity_usd`)

The paper executor produces a synthetic `Fill` against the configured cost
basis so the rest of the system -- portfolio tracker, trade log, snapshot
writer -- exercises end-to-end without any RPC. It is the only executor
selected in `PAPER` mode and the default fallback in `DEGRADED_EXEC` when
the live executor's venue is unhealthy and `live_trading` is not enabled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from solalpha.domain import Fill, Order, Route
from solalpha.foundation import metrics
from solalpha.foundation.ids import new_fill_id, new_order_id
from solalpha.foundation.logging import bind_trace_id, get_logger

if TYPE_CHECKING:
    from solalpha.domain import OrderIntent
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.config import AppConfig

_log = get_logger(__name__)


class PaperExecutor:
    """Pure-Python fill simulator; no RPC, no signing, no on-chain effects."""

    name = "paper_executor"

    def __init__(self, cfg: AppConfig, clock: Clock) -> None:
        self._cfg = cfg
        self._clock = clock

    async def execute(self, intent: OrderIntent) -> tuple[Order, Fill]:
        with bind_trace_id(intent.trace_id):
            return self._execute_sync(intent)

    def _execute_sync(self, intent: OrderIntent) -> tuple[Order, Fill]:
        now = self._clock.now()
        now_ms = int(now.timestamp() * 1000)
        # Simulated fee + slippage.
        paper_cfg = self._cfg.paper_executor
        liquidity = max(1.0, self._cfg.risk.min_pool_liquidity_usd)
        impact_pct = min(0.5, intent.intended_usd / liquidity)
        extra_bps = int(paper_cfg.impact_slippage_per_pct * impact_pct * 100.0)
        total_slippage_bps = paper_cfg.base_slippage_bps + extra_bps + paper_cfg.fee_bps
        total_slippage_bps = min(total_slippage_bps, intent.max_slippage_bps)
        # Notional output = intended_input * (1 - slippage_bps/10000), in
        # base units. We don't have token decimals here; for paper-mode
        # bookkeeping we use the raw input as both legs so the portfolio
        # tracker's PnL math is well-defined.
        in_raw = intent.intended_input_amount_raw
        out_raw = int(in_raw * (10_000 - total_slippage_bps) / 10_000) if in_raw else 0
        order = Order(
            order_id=new_order_id(now_ms),
            signal_id=intent.signal_id,
            created_at=now,
            mint=intent.mint,
            direction=intent.direction,
            intended_usd=intent.intended_usd,
            intended_input_amount_raw=intent.intended_input_amount_raw,
            max_slippage_bps=intent.max_slippage_bps,
            status="confirmed",
            last_attempt=1,
            last_signature=f"paper-{now_ms:013x}",
            trace_id=intent.trace_id,
        )
        route = Route(
            venue="jupiter",
            route_plan=("paper",),
            price_impact_pct=impact_pct,
            in_amount_raw=in_raw,
            out_amount_raw=out_raw,
            slippage_bps=total_slippage_bps,
        )
        fill = Fill(
            fill_id=new_fill_id(now_ms),
            order_id=order.order_id,
            signature=order.last_signature or "",
            slot=0,
            block_time=now,
            input_amount_raw=in_raw,
            output_amount_raw=out_raw,
            realized_slippage_bps=total_slippage_bps,
            fee_lamports=0,
            priority_fee_lamports=0,
            route=route,
            usd_value=intent.intended_usd,
        )
        metrics.ORDERS_TOTAL.labels(status="confirmed").inc()
        _log.info(
            "paper_fill",
            order_id=order.order_id,
            fill_id=fill.fill_id,
            mint=intent.mint,
            direction=intent.direction,
            in_raw=in_raw,
            out_raw=out_raw,
            slippage_bps=total_slippage_bps,
            usd_value=fill.usd_value,
        )
        return order, fill


__all__ = ["PaperExecutor"]
