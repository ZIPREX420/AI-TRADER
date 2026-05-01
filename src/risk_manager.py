"""Position sizing, portfolio caps, daily-loss kill switch."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .constants import LAMPORTS_PER_SOL

log = logging.getLogger("risk")


@dataclass
class Position:
    mint: str
    sol_in: float
    tokens: float
    entry_price_sol: float  # SOL per token
    opened_at: float
    source: str
    high_water_price_sol: float = 0.0
    sold_pct: float = 0.0  # cumulative fraction sold (0..1)


@dataclass
class RiskState:
    capital_sol: float
    capital_usd: float
    sol_price_usd: float
    open_positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl_sol_today: float = 0.0
    day_anchor_ts: float = field(default_factory=time.time)
    halted: bool = False


class RiskManager:
    def __init__(self, *, max_position_pct: float, max_open: int, daily_loss_halt_pct: float, sol_reserve: float):
        self.max_position_pct = max_position_pct
        self.max_open = max_open
        self.daily_loss_halt_pct = daily_loss_halt_pct
        self.sol_reserve = sol_reserve
        self.state: RiskState | None = None

    def init_state(self, capital_sol: float, capital_usd: float, sol_price_usd: float):
        self.state = RiskState(
            capital_sol=capital_sol,
            capital_usd=capital_usd,
            sol_price_usd=sol_price_usd,
        )

    def _roll_day(self):
        if self.state is None:
            return
        if time.time() - self.state.day_anchor_ts > 86_400:
            self.state.realized_pnl_sol_today = 0.0
            self.state.day_anchor_ts = time.time()
            self.state.halted = False

    def allow(self, mint: str, sol_size: float, current_balance_sol: float) -> tuple[bool, str]:
        self._roll_day()
        if self.state is None:
            return False, "no_state"
        if self.state.halted:
            return False, "halted"
        if mint in self.state.open_positions:
            return False, "already_open"
        if len(self.state.open_positions) >= self.max_open:
            return False, "max_open"
        if current_balance_sol - sol_size < self.sol_reserve:
            return False, "below_reserve"
        cap = self.state.capital_sol * self.max_position_pct
        if sol_size > cap * 1.05:
            return False, f"size_over_cap:{sol_size:.4f}>{cap:.4f}"
        loss_pct = self.state.realized_pnl_sol_today / max(self.state.capital_sol, 1e-9)
        if loss_pct <= self.daily_loss_halt_pct:
            self.state.halted = True
            return False, f"daily_loss_halt:{loss_pct:.2%}"
        return True, "ok"

    def open(self, pos: Position):
        if self.state is None:
            return
        self.state.open_positions[pos.mint] = pos

    def close(self, mint: str, sol_out: float, partial_pct: float = 1.0):
        """partial_pct = fraction of *original* tokens being sold in this close action."""
        if self.state is None or mint not in self.state.open_positions:
            return None
        pos = self.state.open_positions[mint]
        cost = pos.sol_in * partial_pct
        pnl = sol_out - cost
        self.state.realized_pnl_sol_today += pnl
        pos.sold_pct = min(1.0, pos.sold_pct + partial_pct)
        if pos.sold_pct >= 0.999:
            del self.state.open_positions[mint]
        return pnl

    def position(self, mint: str) -> Position | None:
        return None if self.state is None else self.state.open_positions.get(mint)

    @property
    def is_halted(self) -> bool:
        return self.state is not None and self.state.halted
