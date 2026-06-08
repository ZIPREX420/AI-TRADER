"""Execution pipeline worker.

Subscribes `SIGNALS_TOPIC`, converts each approved `Signal` to an
`OrderIntent`, picks the paper or live executor based on the runtime
mode + live-eligibility, runs the executor, and republishes:

  * the `Order` row on `ORDERS_TOPIC` (the observability/trade log
    consumer)
  * the `Fill` (plus the parent order, in a tuple) on `FILLS_TOPIC` for
    the `PortfolioTracker`

The pipeline is the bridge between the signal plane and persisted state:
the risk engine has already approved the signal; this layer's job is to
actually make it happen on chain (live) or in the simulator (paper).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from solalpha.domain import OrderIntent
from solalpha.foundation.bus import FILLS_TOPIC, ORDERS_TOPIC, SIGNALS_TOPIC
from solalpha.foundation.ids import new_trace_id
from solalpha.foundation.logging import bind_trace_id, get_logger

if TYPE_CHECKING:
    from solalpha.domain import Signal
    from solalpha.execution.live_executor import LiveExecutor
    from solalpha.execution.paper_executor import PaperExecutor
    from solalpha.foundation.bus import Bus
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.config import AppConfig
    from solalpha.observability.portfolio import PortfolioTracker
    from solalpha.observability.trade_log import TradeLog
    from solalpha.signal.mode_manager import ModeManager

_log = get_logger(__name__)

# A rough conversion: SOL has 9 decimals. The intended USD is converted to
# lamports under an assumed SOL price; for paper mode this is purely
# bookkeeping (PortfolioTracker.apply_fill computes PnL from `usd_value`).
# The live executor will be passed a *more accurate* amount derived from
# real quote data once the price oracle integration lands.
_ASSUMED_SOL_USD = 150.0
_LAMPORTS_PER_SOL = 1_000_000_000


class ExecutionPipeline:
    """SIGNALS -> Order/Fill -> ORDERS/FILLS bridge."""

    name = "execution_pipeline"
    modes: tuple[str, ...] = ()

    def __init__(
        self,
        cfg: AppConfig,
        bus: Bus,
        clock: Clock,
        mode_manager: ModeManager,
        paper_executor: PaperExecutor,
        live_executor: LiveExecutor | None,
        portfolio: PortfolioTracker,
        trade_log: TradeLog,
    ) -> None:
        self._cfg = cfg
        self._bus = bus
        self._clock = clock
        self._mode_manager = mode_manager
        self._paper = paper_executor
        self._live = live_executor
        self._portfolio = portfolio
        self._trade_log = trade_log

    async def run(self) -> None:
        signals_topic = await self._bus.topic(SIGNALS_TOPIC)
        orders_topic = await self._bus.topic(ORDERS_TOPIC)
        fills_topic = await self._bus.topic(FILLS_TOPIC)
        async with signals_topic.subscribe() as recv:
            async for signal in recv:
                await self._handle(signal, orders_topic, fills_topic)

    async def _handle(
        self,
        signal: Signal,
        orders_topic: object,
        fills_topic: object,
    ) -> None:
        with bind_trace_id(signal.trace_id):
            intent = self._intent_from_signal(signal)
            executor = self._pick_executor()
            try:
                order, fill = await executor.execute(intent)
            except Exception as e:
                _log.error(
                    "execution_failed",
                    executor=executor.name,
                    signal_id=signal.signal_id,
                    mint=signal.mint,
                    exc=str(e),
                    exc_type=type(e).__name__,
                )
                return
            await self._trade_log.log_order_event(order, stage="confirmed")
            await self._trade_log.log_fill(fill)
            # Republish for downstream consumers.
            await _publish(orders_topic, order)
            await _publish(fills_topic, (fill, order))
            try:
                await self._portfolio.apply_fill(fill, order)
            except Exception as e:
                _log.warning(
                    "portfolio_apply_failed",
                    order_id=order.order_id,
                    exc=str(e),
                    exc_type=type(e).__name__,
                )

    # ---- helpers ----

    def _pick_executor(self) -> PaperExecutor | LiveExecutor:
        if (
            self._live is not None
            and self._cfg.is_live_eligible()
            and self._mode_manager.mode in ("LIVE", "DEGRADED_RPC", "DEGRADED_EXEC")
        ):
            return self._live
        return self._paper

    def _intent_from_signal(self, signal: Signal) -> OrderIntent:
        amount_raw = self._usd_to_lamports(signal.suggested_usd)
        return OrderIntent(
            signal_id=signal.signal_id,
            mint=signal.mint,
            direction=signal.direction,
            intended_usd=signal.suggested_usd,
            intended_input_amount_raw=amount_raw,
            max_slippage_bps=self._cfg.risk.max_slippage_bps,
            trace_id=signal.trace_id or new_trace_id(int(self._clock.now().timestamp() * 1000)),
        )

    @staticmethod
    def _usd_to_lamports(usd: float) -> int:
        if usd <= 0:
            return 0
        sol = usd / _ASSUMED_SOL_USD
        return int(sol * _LAMPORTS_PER_SOL)


async def _publish(topic: object, payload: object) -> None:
    publish = getattr(topic, "publish", None)
    if publish is None:
        _log.error("topic_invalid_for_publish", type=type(topic).__name__)
        return
    await publish(payload)


__all__ = ["ExecutionPipeline"]
