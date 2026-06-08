"""Injectable wall-clock so every timestamp call is mockable.

No code in solalpha is allowed to call `datetime.utcnow()`, `time.time()`,
or `datetime.now()` directly. All time access goes through a `Clock` instance
that is wired through config at startup.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return current UTC datetime."""

    def monotonic(self) -> float:
        """Monotonic seconds (for measuring intervals; not wall-time)."""

    async def sleep(self, seconds: float) -> None:
        """Async sleep that respects the clock implementation."""


class SystemClock:
    """Real wall clock. Default in production."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        import anyio

        await anyio.sleep(max(0.0, seconds))


class FakeClock:
    """Deterministic clock for tests and replay.

    `advance(seconds)` moves the clock forward and resolves any pending sleeps
    whose deadline has passed.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now: datetime = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._mono: float = 0.0
        self._sleepers: list[tuple[float, _FakeSleeper]] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        deadline = self._mono + seconds
        sleeper = _FakeSleeper()
        self._sleepers.append((deadline, sleeper))
        self._sleepers.sort(key=lambda x: x[0])
        await sleeper.wait()

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self._now = self._now + timedelta(seconds=seconds)
        ready: list[_FakeSleeper] = []
        remaining: list[tuple[float, _FakeSleeper]] = []
        for deadline, sleeper in self._sleepers:
            if deadline <= self._mono:
                ready.append(sleeper)
            else:
                remaining.append((deadline, sleeper))
        self._sleepers = remaining
        for s in ready:
            s.release()


class _FakeSleeper:
    """A primitive that lets FakeClock release pending sleeps deterministically."""

    def __init__(self) -> None:
        import anyio

        self._event = anyio.Event()

    async def wait(self) -> None:
        await self._event.wait()

    def release(self) -> None:
        self._event.set()


__all__ = ["Clock", "FakeClock", "SystemClock"]
