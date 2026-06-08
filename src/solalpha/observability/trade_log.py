"""Per-order / per-fill structured trade log.

`TradeLog` emits one structured log line per order-lifecycle stage and per
fill, appends the same payload to the SQLite `journal` table (so recovery can
replay it), and writes fills to the parquet `trades` table for analytics.

This is the only allowed sink for trade-shaped log lines; other code uses
`get_logger(...)` for generic events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.domain import Fill, Order
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.state import ParquetStore, SqliteStore

_log = get_logger(__name__)


class TradeLog:
    """Structured trade log + journal + parquet sink.

    Constructed once at startup with the runtime stores and clock; injected
    into the execution plane and the portfolio tracker.
    """

    def __init__(
        self,
        store: SqliteStore,
        parquet: ParquetStore,
        clock: Clock,
    ) -> None:
        self._store = store
        self._parquet = parquet
        self._clock = clock

    async def log_order_event(
        self,
        order: Order,
        stage: str,
        **extra: Any,
    ) -> None:
        """Log a single order-lifecycle stage (created/built/submitted/...)."""
        _log.info(
            "order_event",
            stage=stage,
            order_id=order.order_id,
            signal_id=order.signal_id,
            mint=order.mint,
            direction=order.direction,
            status=order.status,
            intended_usd=order.intended_usd,
            attempt=order.last_attempt,
            signature=order.last_signature,
            trace_id=order.trace_id,
            **extra,
        )
        payload: dict[str, Any] = order.model_dump(mode="json")
        payload["stage"] = stage
        payload.update(extra)
        await self._store.journal("order_event", payload, ts=self._clock.now())

    async def log_fill(self, fill: Fill, **extra: Any) -> None:
        """Log a confirmed fill: structured log + journal + parquet trades row."""
        _log.info(
            "fill",
            order_id=fill.order_id,
            fill_id=fill.fill_id,
            signature=fill.signature,
            slot=fill.slot,
            realized_slippage_bps=fill.realized_slippage_bps,
            usd_value=fill.usd_value,
            **extra,
        )
        row: dict[str, Any] = fill.model_dump(mode="json")
        row["received_at"] = self._clock.now().isoformat()
        row.update(extra)
        await self._parquet.append("trades", [row])
        await self._store.journal("fill", row, ts=self._clock.now())


__all__ = ["TradeLog"]
