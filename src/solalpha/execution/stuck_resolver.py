"""Stuck-tx resolver worker.

Every hour, scans the `stuck_signatures` table for `resolved=0` rows older
than the configured budget (default 24h) and re-polls
`getSignatureStatuses` for each. Three outcomes:

  * `confirmed`/`finalized` -> mark resolved; the parent order is updated
    to `confirmed`. (We don't materialize a `Fill` here in Phase 4 -- the
    reconciliation pass that owns SPL balance reads lives in Phase 5.)
  * `err` field set         -> mark resolved with `failed`; the parent
    order moves to `failed`. Inflight counter for the mint is decremented
    via the supplied callback.
  * still pending           -> increment `attempts`, leave for the next tick.

Rows older than 24h with no terminal status are marked `resolved=1,
resolution='abandoned'` so they stop consuming budget; the operator can
re-inspect manually via `solalpha recover --reconcile-stuck`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from solalpha.foundation import metrics
from solalpha.foundation.errors import RpcError
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from solalpha.data.rpc_pool import RpcPool
    from solalpha.foundation.clock import Clock
    from solalpha.foundation.state import SqliteStore

_log = get_logger(__name__)

_ABANDON_AFTER = timedelta(hours=24)


class StuckTxResolver:
    """Periodic worker that re-polls stuck signatures."""

    name = "stuck_tx_resolver"
    modes: tuple[str, ...] = ()

    def __init__(
        self,
        rpc: RpcPool,
        store: SqliteStore,
        clock: Clock,
        *,
        poll_interval_s: float = 3600.0,
        on_resolved: Callable[[str], None] | None = None,
    ) -> None:
        self._rpc = rpc
        self._store = store
        self._clock = clock
        self._poll_interval_s = poll_interval_s
        self._on_resolved = on_resolved or (lambda _mint: None)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:
                _log.warning("stuck_resolver_error", exc=str(e), exc_type=type(e).__name__)
            await self._clock.sleep(self._poll_interval_s)

    async def tick(self) -> int:
        rows = await self._store.fetch_all(
            "SELECT signature, order_id, created_at, attempts FROM stuck_signatures "
            "WHERE resolved = 0 ORDER BY created_at"
        )
        resolved = 0
        now = self._clock.now()
        for row in rows:
            signature = str(row["signature"])
            order_id = str(row["order_id"])
            created = datetime.fromisoformat(str(row["created_at"]))
            try:
                outcome = await self._poll(signature)
            except RpcError as e:
                _log.warning("stuck_poll_rpc_error", sig=signature, exc=str(e))
                continue
            if outcome == "pending":
                if now - created >= _ABANDON_AFTER:
                    await self._mark_resolved(signature, order_id, "abandoned")
                    resolved += 1
                else:
                    await self._store.execute(
                        "UPDATE stuck_signatures SET attempts = attempts + 1, "
                        "last_polled_at = ? WHERE signature = ?",
                        (now.isoformat(), signature),
                    )
                continue
            await self._mark_resolved(signature, order_id, outcome)
            resolved += 1
        if resolved:
            metrics.STUCK_TX.dec(resolved)
        return resolved

    async def _poll(self, signature: str) -> str:
        res = await self._rpc.call(
            "getSignatureStatuses",
            [[signature], {"searchTransactionHistory": True}],
        )
        if not isinstance(res, dict):
            return "pending"
        value = res.get("value")
        if not isinstance(value, list) or not value:
            return "pending"
        entry = value[0]
        if not isinstance(entry, dict):
            return "pending"
        if entry.get("err") is not None and entry.get("err") != "null":
            return "failed"
        status = entry.get("confirmationStatus")
        if status in ("confirmed", "finalized"):
            return "confirmed"
        return "pending"

    async def _mark_resolved(self, signature: str, order_id: str, outcome: str) -> None:
        now = self._clock.now()
        await self._store.execute(
            "UPDATE stuck_signatures SET resolved = 1, resolution = ?, "
            "last_polled_at = ? WHERE signature = ?",
            (outcome, now.isoformat(), signature),
        )
        new_status = "confirmed" if outcome == "confirmed" else "failed"
        await self._store.execute(
            "UPDATE orders SET status = ? WHERE order_id = ?",
            (new_status, order_id),
        )
        # Pull the mint to clear the inflight counter.
        row = await self._store.fetch_one("SELECT mint FROM orders WHERE order_id = ?", (order_id,))
        if row is not None:
            self._on_resolved(str(row["mint"]))
        _log.info("stuck_resolved", sig=signature, order_id=order_id, outcome=outcome)


__all__ = ["StuckTxResolver"]
