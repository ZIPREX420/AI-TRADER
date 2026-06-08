"""Detector protocol.

Each detector owns its rolling state and produces zero or more
`DetectorSignal`s per polling tick. The combiner is responsible for
aggregating across detectors -- never within a single detector.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from solalpha.domain import DetectorSignal, NormalizedSwap


class Detector(Protocol):
    """Pure-async detector. Implementations are not thread-safe."""

    name: str

    async def observe(self, swap: NormalizedSwap) -> None:
        """Feed one normalized swap into the detector's rolling state."""

    def poll(self, now: datetime) -> list[DetectorSignal]:
        """Emit any detector signals whose criteria are now met."""


__all__ = ["Detector"]
