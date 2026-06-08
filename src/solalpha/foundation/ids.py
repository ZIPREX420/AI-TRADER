"""Trace / signal / order id generation.

Uses a uuid7-style scheme: 48 bits unix-millis prefix + 80 bits random,
hex-encoded, so ids sort lexicographically by creation time (useful for
sqlite primary keys and log readability) without requiring a coordinator.
"""

from __future__ import annotations

import secrets
from typing import NewType

TraceId = NewType("TraceId", str)
SignalId = NewType("SignalId", str)
OrderId = NewType("OrderId", str)
FillId = NewType("FillId", str)
PositionId = NewType("PositionId", str)
EventId = NewType("EventId", str)


def _uuid7_hex(now_ms: int) -> str:
    ts_part = f"{now_ms & ((1 << 48) - 1):012x}"
    rand_part = secrets.token_hex(10)
    return f"{ts_part}{rand_part}"


def new_trace_id(now_ms: int) -> TraceId:
    return TraceId("t-" + _uuid7_hex(now_ms))


def new_signal_id(now_ms: int) -> SignalId:
    return SignalId("sg-" + _uuid7_hex(now_ms))


def new_order_id(now_ms: int) -> OrderId:
    return OrderId("od-" + _uuid7_hex(now_ms))


def new_fill_id(now_ms: int) -> FillId:
    return FillId("fl-" + _uuid7_hex(now_ms))


def new_position_id(now_ms: int) -> PositionId:
    return PositionId("ps-" + _uuid7_hex(now_ms))


def deterministic_event_id(signature: str, slot: int, ix_index: int = 0) -> EventId:
    """Stable event id derived from on-chain identifiers; safe for dedupe."""
    return EventId(f"ev-{slot:016x}-{ix_index:04x}-{signature[:16]}")


__all__ = [
    "EventId",
    "FillId",
    "OrderId",
    "PositionId",
    "SignalId",
    "TraceId",
    "deterministic_event_id",
    "new_fill_id",
    "new_order_id",
    "new_position_id",
    "new_signal_id",
    "new_trace_id",
]
