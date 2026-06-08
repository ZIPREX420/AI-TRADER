"""Domain models -- immutable value objects shared across every plane.

This package is a dependency-free leaf: it imports only from `foundation`
(id NewTypes and the `ModeStr` enum) and carries no behaviour. Each plane
owns the *logic* that produces and consumes these models; the models only
define their shape, which mirrors the SQLite schema in
`foundation/persistence_schema.py`.
"""

from __future__ import annotations

from solalpha.domain.events import (
    EventSource,
    NormalizedSwap,
    RawEvent,
    SwapSide,
    SwapVenue,
)
from solalpha.domain.modes import ModeState, ModeStr, ModeTransition
from solalpha.domain.orders import (
    Fill,
    Order,
    OrderIntent,
    OrderStatus,
    Route,
    RouteVenue,
)
from solalpha.domain.positions import DailyPnl, Position, PositionState
from solalpha.domain.signals import (
    DetectorName,
    DetectorSignal,
    Direction,
    RiskDecision,
    RiskVerdict,
    Signal,
)

__all__ = [
    "DailyPnl",
    "DetectorName",
    "DetectorSignal",
    "Direction",
    "EventSource",
    "Fill",
    "ModeState",
    "ModeStr",
    "ModeTransition",
    "NormalizedSwap",
    "Order",
    "OrderIntent",
    "OrderStatus",
    "Position",
    "PositionState",
    "RawEvent",
    "RiskDecision",
    "RiskVerdict",
    "Route",
    "RouteVenue",
    "Signal",
    "SwapSide",
    "SwapVenue",
]
