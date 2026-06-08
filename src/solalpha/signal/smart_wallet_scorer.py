"""Smart-wallet scorer.

Maintains the `smart_wallets` table:
  * `score`            -- decayed rolling 30-day PnL/winrate composite
  * `rolling_pnl_usd_30d`, `rolling_winrate_30d`
  * `last_active_at`   -- for time-decay
  * `quarantined_until`-- set by risk engine on egregious losses

Scores are *read* by the cluster detector ("only count smart wallets") and
by the `SmartWalletSubscriptionManager` (which wallets to subscribe to).
Scores are *written* here when the execution plane lands fills via
`apply_close()`; until Phase 4 wires that, the scorer only reads + decays
existing rows, which is enough for the cluster detector to function with
operator-seeded entries.

Decay model: `score *= 0.5 ** (days_since_last_active / half_life_days)`,
applied on `refresh()`. Both PnL-positive and winrate-positive wallets are
boosted; PnL-negative wallets decay toward zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.state import SqliteStore

_log = get_logger(__name__)


class SmartWalletScorer:
    """Read-cached scorer over the `smart_wallets` table."""

    def __init__(
        self,
        store: SqliteStore,
        clock: Clock,
        *,
        decay_half_life_days: float = 14.0,
        min_score_smart: float = 0.20,
        refresh_interval_s: float = 30.0,
    ) -> None:
        self._store = store
        self._clock = clock
        self._half_life_days = decay_half_life_days
        self._min_score = min_score_smart
        self._refresh_interval_s = refresh_interval_s
        # In-memory lookup table; refreshed on a slow cadence and after writes.
        self._scores: dict[str, float] = {}

    # ---- worker lifecycle ----

    name = "smart_wallet_scorer"
    modes: tuple[str, ...] = ()

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as e:
                _log.warning(
                    "smart_wallet_scorer_refresh_error",
                    exc=str(e),
                    exc_type=type(e).__name__,
                )
            await self._clock.sleep(self._refresh_interval_s)

    # ---- reads ----

    def is_smart(self, wallet: str) -> bool:
        score = self._scores.get(wallet)
        return score is not None and score >= self._min_score

    def score(self, wallet: str) -> float:
        return self._scores.get(wallet, 0.0)

    def smart_set(self) -> frozenset[str]:
        """All currently-smart wallets, snapshot."""
        return frozenset(w for w, s in self._scores.items() if s >= self._min_score)

    # ---- writes ----

    async def refresh(self) -> None:
        rows = await self._store.fetch_all(
            "SELECT wallet, score, last_active_at FROM smart_wallets"
        )
        now = self._clock.now()
        decayed: dict[str, float] = {}
        for row in rows:
            score = float(row["score"])
            last = row.get("last_active_at")
            if isinstance(last, str) and last:
                last_dt = datetime.fromisoformat(last)
                dt_days = max(0.0, (now - last_dt).total_seconds() / 86400.0)
                score *= 0.5 ** (dt_days / self._half_life_days)
            decayed[str(row["wallet"])] = score
        self._scores = decayed

    async def apply_close(
        self,
        wallet: str,
        *,
        realized_pnl_usd: float,
        is_win: bool,
    ) -> None:
        """Update a wallet's rolling stats after a position closes.

        Wired by the execution plane / portfolio tracker in Phase 4. Until
        then, callable directly to seed scores from historical fills.
        """
        now = self._clock.now()
        # Pull existing row; insert if absent.
        row = await self._store.fetch_one(
            "SELECT rolling_pnl_usd_30d, rolling_winrate_30d FROM smart_wallets WHERE wallet = ?",
            (wallet,),
        )
        if row is None:
            new_pnl = realized_pnl_usd
            new_wr = 1.0 if is_win else 0.0
            await self._store.execute(
                """
                INSERT INTO smart_wallets (
                    wallet, added_at, source, weight, score,
                    rolling_pnl_usd_30d, rolling_winrate_30d, last_active_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wallet,
                    now.isoformat(),
                    "auto",
                    1.0,
                    self._composite(new_pnl, new_wr),
                    new_pnl,
                    new_wr,
                    now.isoformat(),
                ),
            )
        else:
            # Exponential moving update with a soft alpha; the rolling label
            # is approximate but stable across restarts.
            alpha = 0.2
            new_pnl = (1 - alpha) * float(row["rolling_pnl_usd_30d"]) + alpha * realized_pnl_usd
            new_wr = (1 - alpha) * float(row["rolling_winrate_30d"]) + alpha * (
                1.0 if is_win else 0.0
            )
            await self._store.execute(
                """
                UPDATE smart_wallets SET
                    score = ?,
                    rolling_pnl_usd_30d = ?,
                    rolling_winrate_30d = ?,
                    last_active_at = ?
                WHERE wallet = ?
                """,
                (
                    self._composite(new_pnl, new_wr),
                    new_pnl,
                    new_wr,
                    now.isoformat(),
                    wallet,
                ),
            )
        await self.refresh()

    async def quarantine(self, wallet: str, *, duration_s: int, reason: str) -> None:
        until = self._clock.now() + timedelta(seconds=duration_s)
        await self._store.execute(
            "UPDATE smart_wallets SET quarantined_until = ? WHERE wallet = ?",
            (until.isoformat(), wallet),
        )
        _log.warning(
            "smart_wallet_quarantined", wallet=wallet, until=until.isoformat(), reason=reason
        )

    @staticmethod
    def _composite(pnl_usd: float, winrate: float) -> float:
        """Map (pnl, winrate) to a score in roughly [0, 1].

        Winrate dominates because it's already bounded; PnL is squashed
        through a tanh-like curve so a single jackpot doesn't dominate.
        """
        # tanh(x/1000) is roughly linear up to a few hundred dollars and
        # asymptotes at +/-1 beyond a few thousand.
        squashed = _tanh_safe(pnl_usd / 1000.0)
        # Compose: average of winrate and (squashed_pnl + 1) / 2.
        return max(0.0, min(1.0, 0.5 * winrate + 0.25 * (squashed + 1.0)))


def _tanh_safe(x: float) -> float:
    # math.tanh from stdlib -- factored out so unit tests don't need numpy.
    import math

    return math.tanh(x)


__all__ = ["SmartWalletScorer"]
