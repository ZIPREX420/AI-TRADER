"""ids: deterministic event ids + lex-sorting time-prefixed ids."""

from __future__ import annotations

import pytest

from solalpha.foundation.ids import (
    deterministic_event_id,
    new_fill_id,
    new_order_id,
    new_signal_id,
    new_trace_id,
)

pytestmark = pytest.mark.unit


def test_deterministic_event_id_stable() -> None:
    a = deterministic_event_id("sig", 100, 0)
    b = deterministic_event_id("sig", 100, 0)
    assert a == b


def test_deterministic_event_id_differs_on_slot() -> None:
    a = deterministic_event_id("sig", 100, 0)
    b = deterministic_event_id("sig", 101, 0)
    assert a != b


def test_deterministic_event_id_differs_on_ix_index() -> None:
    a = deterministic_event_id("sig", 100, 0)
    b = deterministic_event_id("sig", 100, 1)
    assert a != b


def test_new_ids_have_expected_prefixes() -> None:
    assert new_trace_id(1).startswith("t-")
    assert new_signal_id(1).startswith("sg-")
    assert new_order_id(1).startswith("od-")
    assert new_fill_id(1).startswith("fl-")


def test_new_ids_sort_lexicographically_by_time() -> None:
    earlier = new_signal_id(1_000_000)
    later = new_signal_id(2_000_000)
    assert earlier < later


def test_new_ids_unique() -> None:
    ids = {new_signal_id(1_000) for _ in range(100)}
    assert len(ids) == 100  # randomness suffix prevents collision
