"""Execution-plane domain models: routes, order intents, orders, fills.

`OrderIntent` is the risk-approved instruction handed to the execution plane;
`Order` is its persisted lifecycle record (mirrors the `orders` table); `Fill`
is a confirmed on-chain settlement (mirrors the `fills` table). `Route` is the
chosen venue path, stored as `route_json` on a fill.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from solalpha.domain.signals import Direction
from solalpha.foundation.ids import FillId, OrderId, SignalId, TraceId

RouteVenue = Literal["jupiter", "raydium"]
OrderStatus = Literal[
    "pending",
    "building",
    "submitted",
    "confirmed",
    "failed",
    "stuck",
]


class Route(BaseModel):
    """A concrete swap route selected for an order."""

    model_config = ConfigDict(frozen=True)

    venue: RouteVenue
    route_plan: tuple[str, ...] = ()
    price_impact_pct: float
    in_amount_raw: int
    out_amount_raw: int
    slippage_bps: int


class OrderIntent(BaseModel):
    """A risk-approved instruction to trade, before any tx is built."""

    model_config = ConfigDict(frozen=True)

    signal_id: SignalId | None
    mint: str
    direction: Direction
    intended_usd: float
    intended_input_amount_raw: int
    max_slippage_bps: int
    trace_id: TraceId


class Order(BaseModel):
    """The persisted lifecycle record for an order (mirrors the `orders` table)."""

    model_config = ConfigDict(frozen=True)

    order_id: OrderId
    signal_id: SignalId | None
    created_at: datetime
    mint: str
    direction: Direction
    intended_usd: float
    intended_input_amount_raw: int
    max_slippage_bps: int
    status: OrderStatus
    last_attempt: int = 0
    last_signature: str | None = None
    trace_id: TraceId


class Fill(BaseModel):
    """A confirmed on-chain settlement of an order (mirrors the `fills` table)."""

    model_config = ConfigDict(frozen=True)

    fill_id: FillId
    order_id: OrderId
    signature: str
    slot: int
    block_time: datetime
    input_amount_raw: int
    output_amount_raw: int
    realized_slippage_bps: int
    fee_lamports: int
    priority_fee_lamports: int
    route: Route
    usd_value: float | None = None


__all__ = [
    "Fill",
    "Order",
    "OrderIntent",
    "OrderStatus",
    "Route",
    "RouteVenue",
]
