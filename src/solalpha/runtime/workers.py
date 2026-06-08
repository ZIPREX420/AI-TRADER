"""Worker protocol + supervisor for the runtime task group.

Every long-running coroutine in solalpha is a `Worker`. The supervisor:
  * starts each worker in the runtime's `anyio.TaskGroup`
  * restarts crashed workers with capped exponential backoff
  * enables/disables workers per the current mode (`MODE_TOPIC`)

A worker exposes:
  * `name` -- stable identifier (used in logs and metrics)
  * `modes` -- the modes it should be active in (`()` means "always")
  * `run()` -- the long-running coroutine
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import anyio

from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from solalpha.domain import ModeStr
    from solalpha.foundation.clock import Clock

_log = get_logger(__name__)


class Worker(Protocol):
    """Long-running coroutine with a name and a mode allowlist."""

    name: str
    modes: tuple[ModeStr, ...]

    async def run(self) -> None: ...


class WorkerSupervisor:
    """Runs workers under an `anyio.TaskGroup` with restart + mode gating.

    The supervisor is mode-agnostic; the `Application` tells it which workers
    are allowed in which modes by setting `worker.modes`. The supervisor
    consults `current_mode()` between attempts and sleeps a worker out while
    it is mode-disabled rather than terminating it.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        backoff_base_s: float = 0.5,
        backoff_cap_s: float = 30.0,
    ) -> None:
        self._clock = clock
        self._backoff_base_s = backoff_base_s
        self._backoff_cap_s = backoff_cap_s

    async def supervise(
        self,
        workers: Iterable[Worker],
        current_mode: Callable[[], ModeStr],
    ) -> None:
        """Spawn each worker under a fresh task group; restart on crash."""
        async with anyio.create_task_group() as tg:
            for w in workers:
                tg.start_soon(self._supervise_one, w, current_mode)

    async def _supervise_one(
        self,
        worker: Worker,
        current_mode: Callable[[], ModeStr],
    ) -> None:
        attempt = 0
        while True:
            if worker.modes and current_mode() not in worker.modes:
                # Worker is disabled in this mode -- sleep and re-check.
                await self._clock.sleep(1.0)
                continue
            try:
                _log.info("worker_start", name=worker.name, attempt=attempt)
                await worker.run()
                _log.info("worker_exited_cleanly", name=worker.name)
                return
            except anyio.get_cancelled_exc_class():
                _log.info("worker_cancelled", name=worker.name)
                raise
            except Exception as e:
                delay = min(
                    self._backoff_cap_s,
                    self._backoff_base_s * (2**attempt),
                )
                attempt += 1
                _log.error(
                    "worker_crash",
                    name=worker.name,
                    exc=str(e),
                    exc_type=type(e).__name__,
                    next_retry_s=delay,
                    attempt=attempt,
                )
                await self._clock.sleep(delay)


__all__ = ["Worker", "WorkerSupervisor"]
