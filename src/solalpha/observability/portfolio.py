"""Portfolio tracker: positions, daily PnL, and loss-streak bookkeeping.

`PortfolioTracker` is the authoritative consumer of `Fill`s. It maintains:
  * an open `Position` per mint, with weighted-average cost basis
  * a per-UTC-day `DailyPnl` row tracking realized PnL, wins, losses, streak
  * the loss streak used by the risk engine (`risk.loss_streak_max`)

State is mirrored to SQLite (`positions`, `daily_pnl` tables) and rebuilt from
SQLite on `load()` so a process restart sees the world correctly. The risk
engine reads the open-positions count + daily PnL via the small read API.

`apply_fill(fill, order)` takes the parent `Order` so we can resolve the
fill's `mint` and `direction` -- the `Fill` model alone does not carry those.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from solalpha.domain import DailyPnl, Position
from solalpha.foundation import metrics
from solalpha.foundation.ids import new_position_id
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from solalpha.domain import Fill, Order, PositionState
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.state import SqliteStore

_log = get_logger(__name__)


class PortfolioTracker:
    """Owns the `positions` and `daily_pnl` tables.

    Single-writer: external callers must serialize calls to `apply_fill`.
    """

    def __init__(self, store: SqliteStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock
        # Open positions indexed by mint -- lookups during apply_fill are O(1).
        self._open_by_mint: dict[str, Position] = {}
        self._loss_streak: int = 0

    # ---- load / read ----

    async def load(self) -> None:
        """Rebuild in-memory state from SQLite. Call once after `store.connect()`."""
        rows = await self._store.fetch_all(
            "SELECT * FROM positions WHERE state = 'open' ORDER BY opened_at"
        )
        self._open_by_mint = {str(row["mint"]): self._position_from_row(row) for row in rows}
        today = self._today()
        row = await self._store.fetch_one(
            "SELECT loss_streak FROM daily_pnl WHERE day = ?", (today,)
        )
        self._loss_streak = int(row["loss_streak"]) if row else 0
        metrics.OPEN_POSITIONS.set(len(self._open_by_mint))
        _log.info(
            "portfolio_loaded",
            open_positions=len(self._open_by_mint),
            loss_streak=self._loss_streak,
            day=today,
        )

    def open_positions_count(self) -> int:
        return len(self._open_by_mint)

    def get_open(self, mint: str) -> Position | None:
        return self._open_by_mint.get(mint)

    def loss_streak(self) -> int:
        return self._loss_streak

    async def daily_pnl(self, day: str | None = None) -> DailyPnl:
        d = day or self._today()
        row = await self._store.fetch_one("SELECT * FROM daily_pnl WHERE day = ?", (d,))
        if row is None:
            return DailyPnl(day=d)
        return DailyPnl(
            day=str(row["day"]),
            pnl_usd=float(row["pnl_usd"]),
            wins=int(row["wins"]),
            losses=int(row["losses"]),
            loss_streak=int(row["loss_streak"]),
        )

    # ---- mutation ----

    async def apply_fill(self, fill: Fill, order: Order) -> Position:
        """Update the position for the order's mint and persist."""
        mint = order.mint
        existing = self._open_by_mint.get(mint)
        if order.direction == "buy":
            new_position, realized = self._apply_buy(existing, fill, mint)
        else:
            new_position, realized = self._apply_sell(existing, fill, mint)
        await self._persist_position(new_position)
        if new_position.state == "closed":
            self._open_by_mint.pop(mint, None)
        else:
            self._open_by_mint[mint] = new_position
        if realized != 0.0:
            await self._update_daily_pnl(realized)
        metrics.OPEN_POSITIONS.set(len(self._open_by_mint))
        return new_position

    # ---- private ----

    def _today(self) -> str:
        return self._clock.now().strftime("%Y-%m-%d")

    def _apply_buy(
        self, existing: Position | None, fill: Fill, mint: str
    ) -> tuple[Position, float]:
        added_qty = fill.output_amount_raw
        added_cost = float(fill.usd_value or 0.0)
        now = self._clock.now()
        if existing is None:
            position = Position(
                position_id=new_position_id(int(now.timestamp() * 1000)),
                mint=mint,
                opened_at=now,
                cost_basis_usd=added_cost,
                quantity_raw=added_qty,
                quantity_ui=float(added_qty),
                realized_pnl_usd=0.0,
                fills=(fill.fill_id,),
                state="open",
            )
        else:
            position = existing.model_copy(
                update={
                    "cost_basis_usd": existing.cost_basis_usd + added_cost,
                    "quantity_raw": existing.quantity_raw + added_qty,
                    "quantity_ui": existing.quantity_ui + float(added_qty),
                    "fills": (*existing.fills, fill.fill_id),
                }
            )
        return position, 0.0

    def _apply_sell(
        self, existing: Position | None, fill: Fill, mint: str
    ) -> tuple[Position, float]:
        sold_qty = fill.input_amount_raw
        proceeds = float(fill.usd_value or 0.0)
        now = self._clock.now()
        if existing is None:
            # Selling a mint we have no recorded buy for -- treat as fully realized
            # against zero cost basis (best-effort; reconciliation will fix it).
            realized = proceeds
            position = Position(
                position_id=new_position_id(int(now.timestamp() * 1000)),
                mint=mint,
                opened_at=now,
                closed_at=now,
                cost_basis_usd=0.0,
                quantity_raw=0,
                quantity_ui=0.0,
                realized_pnl_usd=realized,
                fills=(fill.fill_id,),
                state="closed",
            )
            return position, realized
        avg_cost = (
            existing.cost_basis_usd / existing.quantity_raw if existing.quantity_raw > 0 else 0.0
        )
        sold_qty = min(sold_qty, existing.quantity_raw)
        realized = proceeds - (avg_cost * sold_qty)
        new_qty = existing.quantity_raw - sold_qty
        new_cost = existing.cost_basis_usd - (avg_cost * sold_qty)
        state: PositionState = "open" if new_qty > 0 else "closed"
        position = existing.model_copy(
            update={
                "quantity_raw": new_qty,
                "quantity_ui": float(new_qty),
                "cost_basis_usd": max(new_cost, 0.0),
                "realized_pnl_usd": existing.realized_pnl_usd + realized,
                "fills": (*existing.fills, fill.fill_id),
                "state": state,
                "closed_at": now if state == "closed" else existing.closed_at,
            }
        )
        return position, realized

    async def _persist_position(self, position: Position) -> None:
        await self._store.execute(
            """
            INSERT INTO positions (
                position_id, mint, opened_at, closed_at,
                cost_basis_usd, quantity_raw, quantity_ui, realized_pnl_usd,
                fills_json, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(position_id) DO UPDATE SET
                closed_at = excluded.closed_at,
                cost_basis_usd = excluded.cost_basis_usd,
                quantity_raw = excluded.quantity_raw,
                quantity_ui = excluded.quantity_ui,
                realized_pnl_usd = excluded.realized_pnl_usd,
                fills_json = excluded.fills_json,
                state = excluded.state
            """,
            (
                position.position_id,
                position.mint,
                position.opened_at.isoformat(),
                position.closed_at.isoformat() if position.closed_at else None,
                position.cost_basis_usd,
                position.quantity_raw,
                position.quantity_ui,
                position.realized_pnl_usd,
                json.dumps(list(position.fills)),
                position.state,
            ),
        )

    async def _update_daily_pnl(self, realized: float) -> None:
        day = self._today()
        current = await self.daily_pnl(day)
        won = realized > 0
        lost = realized < 0
        new_streak = self._loss_streak + 1 if lost else 0
        self._loss_streak = new_streak
        new = DailyPnl(
            day=day,
            pnl_usd=current.pnl_usd + realized,
            wins=current.wins + (1 if won else 0),
            losses=current.losses + (1 if lost else 0),
            loss_streak=new_streak,
        )
        await self._store.execute(
            """
            INSERT INTO daily_pnl (day, pnl_usd, wins, losses, loss_streak)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                pnl_usd = excluded.pnl_usd,
                wins = excluded.wins,
                losses = excluded.losses,
                loss_streak = excluded.loss_streak
            """,
            (new.day, new.pnl_usd, new.wins, new.losses, new.loss_streak),
        )
        metrics.DAILY_PNL_USD.set(new.pnl_usd)

    @staticmethod
    def _position_from_row(row: Mapping[str, Any]) -> Position:
        closed_raw = row.get("closed_at")
        closed_at = datetime.fromisoformat(str(closed_raw)) if closed_raw else None
        fills_raw = row.get("fills_json")
        fills: tuple[str, ...] = tuple(json.loads(str(fills_raw))) if fills_raw else ()
        state_val = str(row["state"])
        if state_val not in ("open", "closed", "unknown"):
            raise ValueError(f"unexpected position state: {state_val!r}")
        state: PositionState = state_val  # type: ignore[assignment]
        return Position(
            position_id=str(row["position_id"]),
            mint=str(row["mint"]),
            opened_at=datetime.fromisoformat(str(row["opened_at"])),
            closed_at=closed_at,
            cost_basis_usd=float(row["cost_basis_usd"]),
            quantity_raw=int(row["quantity_raw"]),
            quantity_ui=float(row["quantity_ui"]),
            realized_pnl_usd=float(row["realized_pnl_usd"]),
            fills=fills,
            state=state,
        )


__all__ = ["PortfolioTracker"]
