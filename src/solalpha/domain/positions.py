"""Portfolio-plane domain models: positions and daily PnL.

`Position` mirrors the `positions` table; `DailyPnl` mirrors the `daily_pnl`
table. The portfolio tracker (observability plane) owns the logic that builds
these from the `fills` stream -- the models themselves carry no behaviour.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from solalpha.foundation.ids import FillId, PositionId

PositionState = Literal["open", "closed", "unknown"]


class Position(BaseModel):
    """An open or closed position in a single mint (mirrors the `positions` table).

    `state` is `unknown` when a parent order is stuck (tx submitted but not
    confirmed); the risk engine then treats the position as full exposure until
    the stuck-tx resolver reconciles it. See `RUNBOOK.md`.
    """

    model_config = ConfigDict(frozen=True)

    position_id: PositionId
    mint: str
    opened_at: datetime
    closed_at: datetime | None = None
    cost_basis_usd: float = 0.0
    quantity_raw: int = 0
    quantity_ui: float = 0.0
    realized_pnl_usd: float = 0.0
    fills: tuple[FillId, ...] = ()
    state: PositionState = "open"


class DailyPnl(BaseModel):
    """Realized PnL and win/loss bookkeeping for one UTC day.

    `loss_streak` is the count of consecutive losing closes; the risk engine
    halts trading when it reaches `risk.loss_streak_max`.
    """

    model_config = ConfigDict(frozen=True)

    day: str
    pnl_usd: float = 0.0
    wins: int = 0
    losses: int = 0
    loss_streak: int = 0


__all__ = [
    "DailyPnl",
    "Position",
    "PositionState",
]
