"""Bounded ring-buffer dedupe for raw event ids.

`DedupeRing` is a fixed-capacity `seen-set` with insertion-order eviction.
The websocket ingestor and backfill poller both feed events through it
before publishing to `EVENTS_TOPIC`; duplicates increment
`EVENTS_DEDUPED` and are dropped.

Event ids come from `foundation.ids.deterministic_event_id(signature, slot,
ix_index)` so the same on-chain invocation hashes identically whether it
arrives via websocket or backfill -- exactly the contract this ring needs.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING

from solalpha.foundation import metrics

if TYPE_CHECKING:
    from solalpha.foundation.ids import EventId


class DedupeRing:
    """Bounded LRU-style set with O(1) `seen` / `add`."""

    def __init__(self, capacity: int = 100_000) -> None:
        if capacity <= 0:
            raise ValueError("DedupeRing capacity must be positive")
        self._capacity = capacity
        # OrderedDict acts as an insertion-ordered set; keys are EventIds,
        # values are unused (None) -- we only need membership + eviction.
        self._seen: OrderedDict[EventId, None] = OrderedDict()

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        return len(self._seen)

    def seen(self, event_id: EventId) -> bool:
        """Return True (and increment the metric) if `event_id` has been added."""
        if event_id in self._seen:
            metrics.EVENTS_DEDUPED.inc()
            return True
        return False

    def add(self, event_id: EventId) -> bool:
        """Add an id. Returns True if new, False if a duplicate.

        Combining the check and the insert in one call is the common path
        for the ingestor: `if ring.add(eid): publish(...)`.
        """
        if event_id in self._seen:
            metrics.EVENTS_DEDUPED.inc()
            return False
        self._seen[event_id] = None
        if len(self._seen) > self._capacity:
            # Evict the oldest entry (FIFO insertion order).
            self._seen.popitem(last=False)
        return True


__all__ = ["DedupeRing"]
