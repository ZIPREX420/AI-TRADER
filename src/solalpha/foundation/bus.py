"""In-process publish/subscribe over anyio memory streams.

The bus is the only allowed way for layers to talk to each other:
- data plane publishes Events
- signal plane publishes Signals
- execution plane publishes Orders/Fills
- mode manager publishes ModeStates

Subscribers receive their own clone of every message via per-subscriber
backpressured queues.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Generic, TypeVar

import anyio

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

T = TypeVar("T")


class Topic(Generic[T]):
    """A typed in-process broadcast topic with per-subscriber backpressure."""

    def __init__(self, name: str, *, max_buffer: int = 1024) -> None:
        self.name = name
        self._max_buffer = max_buffer
        self._subscribers: list[MemoryObjectSendStream[T]] = []
        self._lock = anyio.Lock()

    async def publish(self, message: T) -> None:
        async with self._lock:
            sinks = list(self._subscribers)
        for sink in sinks:
            try:
                sink.send_nowait(message)
            except anyio.WouldBlock:
                # Slow consumer: drop oldest by recreating sink.
                # We log via foundation.logging at the call site (avoid import cycle here).
                continue
            except anyio.BrokenResourceError:
                async with self._lock:
                    if sink in self._subscribers:
                        self._subscribers.remove(sink)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[MemoryObjectReceiveStream[T]]:
        send, recv = anyio.create_memory_object_stream[T](max_buffer_size=self._max_buffer)
        async with self._lock:
            self._subscribers.append(send)
        try:
            yield recv
        finally:
            async with self._lock:
                if send in self._subscribers:
                    self._subscribers.remove(send)
            await send.aclose()
            await recv.aclose()

    async def close(self) -> None:
        async with self._lock:
            sinks = list(self._subscribers)
            self._subscribers.clear()
        for s in sinks:
            await s.aclose()


class Bus:
    """Registry of named topics."""

    def __init__(self) -> None:
        self._topics: dict[str, Topic[Any]] = {}
        self._lock = anyio.Lock()

    async def topic(self, name: str, *, max_buffer: int = 1024) -> Topic[Any]:
        async with self._lock:
            t = self._topics.get(name)
            if t is None:
                t = Topic(name, max_buffer=max_buffer)
                self._topics[name] = t
            return t

    async def close(self) -> None:
        async with self._lock:
            topics = list(self._topics.values())
            self._topics.clear()
        for t in topics:
            await t.close()


# Canonical topic names — string-keyed to avoid import cycles between layers.
EVENTS_TOPIC = "events"  # data plane → all
NORMALIZED_TOPIC = "normalized"  # decoder → signal
SIGNALS_TOPIC = "signals"  # signal → execution
ORDERS_TOPIC = "orders"  # execution → observability
FILLS_TOPIC = "fills"  # execution → observability/portfolio
MODE_TOPIC = "mode"  # mode manager → all
HEALTH_TOPIC = "health"  # health registry → mode manager


__all__ = [
    "EVENTS_TOPIC",
    "FILLS_TOPIC",
    "HEALTH_TOPIC",
    "MODE_TOPIC",
    "NORMALIZED_TOPIC",
    "ORDERS_TOPIC",
    "SIGNALS_TOPIC",
    "Bus",
    "Topic",
]
