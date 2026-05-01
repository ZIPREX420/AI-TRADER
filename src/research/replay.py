"""Deterministic event-stream replay over Parquet datasets."""
from __future__ import annotations

import bisect
import heapq
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from . import storage
from .clock import ReplayClock

log = logging.getLogger("replay")


@dataclass(order=False)
class ReplayEvent:
    ts: float
    slot: int
    sig: str
    kind: str         # "swap" | "pool" | "transfer"
    payload: dict = field(default_factory=dict)

    def sort_key(self):
        return (self.ts, self.slot, self.sig, self.kind)


def _rows_to_events(rows: list[dict], kind: str) -> list[ReplayEvent]:
    out: list[ReplayEvent] = []
    for r in rows:
        out.append(ReplayEvent(
            ts=float(r.get("ts", 0)),
            slot=int(r.get("slot", 0) or 0),
            sig=str(r.get("sig", "") or ""),
            kind=kind,
            payload=r,
        ))
    return out


def merge_sorted_streams(start_ts: float, end_ts: float,
                         tables: Iterable[str] = ("swaps", "pools", "transfers"),
                         root: Path = storage.ROOT) -> Iterator[ReplayEvent]:
    """k-way merge of pre-sorted parquet streams. Lightweight: loads each day at a time."""
    streams: list[list[ReplayEvent]] = []
    for table in tables:
        rows_iter = storage.read_partition_iter(table, start_ts, end_ts, root=root)
        evs = _rows_to_events(list(rows_iter), kind=table.rstrip("s"))
        evs.sort(key=lambda e: e.sort_key())
        if evs:
            streams.append(evs)
    # heap-merge
    heap: list[tuple[tuple, int, int]] = []
    for i, s in enumerate(streams):
        if s:
            heapq.heappush(heap, (s[0].sort_key(), i, 0))
    while heap:
        key, i, j = heapq.heappop(heap)
        yield streams[i][j]
        nxt = j + 1
        if nxt < len(streams[i]):
            heapq.heappush(heap, (streams[i][nxt].sort_key(), i, nxt))


@dataclass
class PriceProvider:
    """Step interpolation over 1m bars per mint."""
    bars_by_mint: dict[str, list[tuple[float, float]]]   # mint → sorted list[(ts, price_sol)]

    @classmethod
    def from_storage(cls, start_ts: float, end_ts: float, root: Path = storage.ROOT) -> "PriceProvider":
        rows = list(storage.read_partition_iter("prices", start_ts, end_ts, root=root))
        by_mint: dict[str, list[tuple[float, float]]] = {}
        for r in rows:
            by_mint.setdefault(r["mint"], []).append((float(r["ts"]), float(r["sol_per_token"])))
        for k in by_mint:
            by_mint[k].sort()
        return cls(by_mint)

    def at(self, ts: float, mint: str) -> float | None:
        bars = self.bars_by_mint.get(mint)
        if not bars:
            return None
        ts_list = [b[0] for b in bars]
        i = bisect.bisect_right(ts_list, ts) - 1
        if i < 0:
            return bars[0][1]
        return bars[i][1]


@dataclass
class Replay:
    start_ts: float
    end_ts: float
    seed: int = 42
    tables: tuple[str, ...] = ("swaps", "pools", "transfers")
    root: Path = storage.ROOT
    clock: ReplayClock | None = None
    rng: random.Random | None = None
    prices: PriceProvider | None = None

    def __post_init__(self):
        if self.clock is None:
            self.clock = ReplayClock(_now=self.start_ts)
        if self.rng is None:
            self.rng = random.Random(self.seed)
        if self.prices is None:
            self.prices = PriceProvider.from_storage(self.start_ts, self.end_ts, self.root)

    def __iter__(self) -> Iterator[ReplayEvent]:
        for ev in merge_sorted_streams(self.start_ts, self.end_ts, self.tables, self.root):
            if ev.ts > self.end_ts:
                break
            self.clock.advance_to(ev.ts)
            yield ev


def from_memory(events: list[dict], start_ts: float | None = None, end_ts: float | None = None,
                seed: int = 42) -> Replay:
    """Build a Replay from in-memory event dicts (for tests, no parquet files needed)."""
    rep = Replay(
        start_ts=start_ts or min(e["ts"] for e in events),
        end_ts=end_ts or max(e["ts"] for e in events),
        seed=seed,
    )

    def _gen():
        rep.clock.advance_to(rep.start_ts)
        sorted_evs = sorted(events, key=lambda e: (e["ts"], e.get("slot", 0), e.get("sig", "")))
        for r in sorted_evs:
            ev = ReplayEvent(
                ts=float(r["ts"]),
                slot=int(r.get("slot", 0)),
                sig=str(r.get("sig", "")),
                kind=r.get("kind", "swap"),
                payload=r,
            )
            if ev.ts > rep.end_ts:
                break
            rep.clock.advance_to(ev.ts)
            yield ev

    rep.__iter__ = lambda: _gen()  # type: ignore[assignment]
    return rep
