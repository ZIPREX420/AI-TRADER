"""Smart-wallet subscription manager.

Keeps the top-`max_subscriptions` wallets (by score, persisted in
`smart_wallets.score`) on the websocket subscription list. The signal-plane
scorer mutates the score column; this manager polls the table on a slow
cadence and produces an ordered list the ingestor (and the backfill poller)
read on each reconnect.

Phase 2 ships the data structure + persistence; the smart-wallet scorer
(Phase 3) writes the scores. Until then `current()` returns whatever the
operator has seeded in `smart_wallets` plus an empty list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.state import SqliteStore

_log = get_logger(__name__)


class SmartWalletSubscriptionManager:
    """Owns the materialized list of tracked wallets."""

    name = "smart_wallet_subs"
    modes: tuple[str, ...] = ()

    def __init__(
        self,
        store: SqliteStore,
        clock: Clock,
        *,
        max_subscriptions: int = 200,
        min_score: float = 0.20,
        poll_interval_s: float = 30.0,
    ) -> None:
        self._store = store
        self._clock = clock
        self._max = max_subscriptions
        self._min_score = min_score
        self._poll_interval_s = poll_interval_s
        self._current: list[str] = []

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as e:
                _log.warning(
                    "smart_wallet_refresh_error",
                    exc=str(e),
                    exc_type=type(e).__name__,
                )
            await self._clock.sleep(self._poll_interval_s)

    async def refresh(self) -> list[str]:
        now = self._clock.now().isoformat()
        rows = await self._store.fetch_all(
            """
            SELECT wallet FROM smart_wallets
            WHERE score >= ?
              AND (quarantined_until IS NULL OR quarantined_until < ?)
            ORDER BY score DESC
            LIMIT ?
            """,
            (self._min_score, now, self._max),
        )
        wallets = [str(row["wallet"]) for row in rows]
        if wallets != self._current:
            _log.info(
                "smart_wallet_subscriptions_updated",
                count=len(wallets),
                previous=len(self._current),
            )
            self._current = wallets
        return list(self._current)

    def current(self) -> list[str]:
        """Latest snapshot of tracked wallets. Cheap, in-memory."""
        return list(self._current)


__all__ = ["SmartWalletSubscriptionManager"]
