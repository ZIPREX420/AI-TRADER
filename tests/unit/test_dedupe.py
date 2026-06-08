"""DedupeRing: capacity, eviction, return values."""

from __future__ import annotations

import pytest

from solalpha.data.dedupe import DedupeRing
from solalpha.foundation.ids import deterministic_event_id

pytestmark = pytest.mark.unit


def test_first_add_returns_true() -> None:
    ring = DedupeRing(capacity=10)
    eid = deterministic_event_id("s", 1, 0)
    assert ring.add(eid) is True


def test_duplicate_add_returns_false() -> None:
    ring = DedupeRing(capacity=10)
    eid = deterministic_event_id("s", 1, 0)
    ring.add(eid)
    assert ring.add(eid) is False


def test_seen_does_not_evict() -> None:
    ring = DedupeRing(capacity=10)
    eid = deterministic_event_id("s", 1, 0)
    ring.add(eid)
    assert ring.seen(eid) is True
    assert ring.seen(eid) is True  # idempotent


def test_capacity_eviction_fifo() -> None:
    ring = DedupeRing(capacity=3)
    ids = [deterministic_event_id("s", i, 0) for i in range(4)]
    for eid in ids:
        ring.add(eid)
    # The ring holds the newest 3 ids; ids[0] was evicted FIFO.
    # `seen()` is non-mutating, so it doesn't cascade further evictions.
    assert ring.seen(ids[0]) is False, "oldest entry should have been evicted"
    assert ring.seen(ids[1]) is True
    assert ring.seen(ids[2]) is True
    assert ring.seen(ids[3]) is True


def test_capacity_validation() -> None:
    with pytest.raises(ValueError, match="positive"):
        DedupeRing(capacity=0)
