"""Dual-RPC transaction confirmation.

After a tx is submitted we poll `getSignatureStatuses` against every healthy
RPC at `confirmation_poll_interval_s` intervals up to
`confirmation_timeout_s`. A signature is considered confirmed when *any*
endpoint reports `confirmationStatus in {"confirmed", "finalized"}` and the
`err` field is null. On `err` we raise `ExecutionFailed`; on timeout we
raise `StuckTransaction` -- the parent order is moved to `status="stuck"`
and the `StuckTxResolver` picks it up on its hourly tick.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from solalpha.foundation import metrics
from solalpha.foundation.errors import (
    ExecutionFailed,
    RpcError,
    StuckTransaction,
)
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.data.rpc_pool import RpcPool
    from solalpha.foundation.clock import Clock

_log = get_logger(__name__)


class Confirmer:
    """Polls `getSignatureStatuses` until confirmed, failed, or expired."""

    def __init__(
        self,
        rpc: RpcPool,
        clock: Clock,
        *,
        timeout_s: float = 30.0,
        poll_interval_s: float = 1.0,
    ) -> None:
        self._rpc = rpc
        self._clock = clock
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

    async def confirm(self, signature: str) -> dict[str, object]:
        """Wait for `signature` to confirm; return the status entry."""
        started = self._clock.monotonic()
        deadline = started + self._timeout_s
        while self._clock.monotonic() < deadline:
            try:
                res = await self._rpc.call(
                    "getSignatureStatuses",
                    [[signature], {"searchTransactionHistory": True}],
                )
            except RpcError as e:
                _log.warning("confirm_rpc_error", sig=signature, exc=str(e))
                await self._clock.sleep(self._poll_interval_s)
                continue
            entry = self._extract(res)
            if entry is None:
                await self._clock.sleep(self._poll_interval_s)
                continue
            err = entry.get("err")
            if err is not None and err != "null":
                metrics.CONFIRM_LATENCY.labels(outcome="failed").observe(
                    self._clock.monotonic() - started
                )
                metrics.ORDERS_TOTAL.labels(status="failed").inc()
                raise ExecutionFailed(f"tx {signature} failed on-chain: {err!r}")
            status = entry.get("confirmationStatus")
            if status in ("confirmed", "finalized"):
                metrics.CONFIRM_LATENCY.labels(outcome="ok").observe(
                    self._clock.monotonic() - started
                )
                return entry
            await self._clock.sleep(self._poll_interval_s)
        metrics.CONFIRM_LATENCY.labels(outcome="stuck").observe(self._clock.monotonic() - started)
        metrics.STUCK_TX.inc()
        raise StuckTransaction(
            f"tx {signature} not confirmed within {self._timeout_s:.0f}s",
            signature=signature,
        )

    # ---- internals ----

    @staticmethod
    def _extract(result: object) -> dict[str, object] | None:
        if not isinstance(result, dict):
            return None
        value = result.get("value")
        if not isinstance(value, list) or not value:
            return None
        first = value[0]
        if not isinstance(first, dict):
            return None
        return first


__all__ = ["Confirmer"]
