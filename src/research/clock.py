"""Clock abstraction. Live = wallclock; replay = simulated time advancing per event."""
from __future__ import annotations

import time
from dataclasses import dataclass


class Clock:
    def now(self) -> float:
        raise NotImplementedError


class WallClock(Clock):
    def now(self) -> float:
        return time.time()


@dataclass
class ReplayClock(Clock):
    _now: float

    def now(self) -> float:
        return self._now

    def advance_to(self, t: float) -> None:
        if t < self._now:
            return
        self._now = t

    def advance_by(self, dt: float) -> None:
        self._now += max(dt, 0.0)
