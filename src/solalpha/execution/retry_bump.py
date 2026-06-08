"""Retry-with-bump executor.

Wraps `build -> submit -> confirm` with the configured escalation ladder:

    attempt N: priority_fee_lamports = bump_priority_fee_lamports[N]
               slippage_bps          = base + bump_slippage_bps[N]

A `TransientError` (RPC blip, blockhash expired, route unavailable,
Jupiter 5xx, stuck-tx within budget) advances to attempt N+1. Permanent
errors raise immediately. On exhaustion of attempts we raise the last
caught exception so the live executor records it as a failed order.

`RETRY_BUMPS` metric counts each bump by attempt number.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from solalpha.foundation import metrics
from solalpha.foundation.errors import (
    BlockhashExpired,
    ExecutionFailed,
    JupiterError,
    PermanentError,
    RouteUnavailable,
    StuckTransaction,
    TransientError,
)
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from solalpha.execution.tx_builder import BuiltTx
    from solalpha.foundation.config import ExecutionConfig

_log = get_logger(__name__)


class RetryBumpExecutor:
    """Runs the build/submit/confirm cycle with priority-fee + slippage escalation."""

    def __init__(self, exec_cfg: ExecutionConfig) -> None:
        self._cfg = exec_cfg

    async def run(
        self,
        *,
        base_slippage_bps: int,
        build_fn: Callable[[int, int], Awaitable[BuiltTx]],
        submit_fn: Callable[[BuiltTx], Awaitable[str]],
        confirm_fn: Callable[[str], Awaitable[dict[str, object]]],
    ) -> tuple[BuiltTx, str, dict[str, object]]:
        attempts = self._cfg.max_attempts
        last_exc: Exception | None = None
        for n in range(attempts):
            fee = self._priority_fee(n)
            slip = self._slippage(base_slippage_bps, n)
            try:
                built = await build_fn(fee, slip)
                signature = await submit_fn(built)
                status = await confirm_fn(signature)
                if n > 0:
                    metrics.RETRY_BUMPS.labels(attempt=str(n)).inc()
                return built, signature, status
            except (BlockhashExpired, RouteUnavailable, JupiterError) as e:
                last_exc = e
                _log.warning(
                    "retry_bump_transient",
                    attempt=n,
                    next_fee_lamports=self._priority_fee(min(n + 1, attempts - 1)),
                    exc_type=type(e).__name__,
                    exc=str(e),
                )
                metrics.RETRY_BUMPS.labels(attempt=str(n + 1)).inc()
                continue
            except StuckTransaction as e:
                # Stuck within the per-attempt timeout -- bump and retry.
                last_exc = e
                _log.warning(
                    "retry_bump_stuck",
                    attempt=n,
                    sig=e.signature,
                )
                metrics.RETRY_BUMPS.labels(attempt=str(n + 1)).inc()
                continue
            except (ExecutionFailed, PermanentError):
                raise
            except TransientError as e:
                last_exc = e
                metrics.RETRY_BUMPS.labels(attempt=str(n + 1)).inc()
                continue
        if last_exc is not None:
            raise last_exc
        raise ExecutionFailed("retry exhausted without an outcome")

    def _priority_fee(self, attempt: int) -> int:
        ladder = self._cfg.bump_priority_fee_lamports
        if not ladder:
            return self._cfg.default_priority_fee_lamports
        return int(ladder[min(attempt, len(ladder) - 1)])

    def _slippage(self, base: int, attempt: int) -> int:
        bumps = self._cfg.bump_slippage_bps
        if not bumps:
            return base
        return base + int(bumps[min(attempt, len(bumps) - 1)])


__all__ = ["RetryBumpExecutor"]
