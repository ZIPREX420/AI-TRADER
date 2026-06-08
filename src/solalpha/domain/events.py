"""Data-plane domain models: raw on-chain events and normalized swaps.

`RawEvent` is what the websocket ingestor / backfill poller publish to the
`events` topic. `NormalizedSwap` is what the transaction decoder publishes to
the `normalized` topic after dispatching by program id. Both are immutable
value objects -- no behaviour lives here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from solalpha.foundation.ids import EventId

EventSource = Literal["ws", "backfill", "replay"]
SwapVenue = Literal["jupiter", "raydium", "orca", "pumpfun"]
SwapSide = Literal["buy", "sell"]


class RawEvent(BaseModel):
    """An undecoded on-chain event referencing a single program invocation."""

    model_config = ConfigDict(frozen=True)

    event_id: EventId
    signature: str
    slot: int
    block_time: datetime
    program_id: str
    accounts: tuple[str, ...] = ()
    logs: tuple[str, ...] = ()
    data: str = ""
    source: EventSource = "ws"
    received_at: datetime


class NormalizedSwap(BaseModel):
    """A decoded swap, venue-agnostic, ready for the signal plane.

    `side` is expressed relative to `mint`: `buy` means the wallet acquired
    `mint`; `sell` means it disposed of `mint`. Raw amounts are in token base
    units; `price` and `usd_value` are best-effort and may be 0.0 when a USD
    reference is unavailable at decode time.
    """

    model_config = ConfigDict(frozen=True)

    event_id: EventId
    signature: str
    slot: int
    block_time: datetime
    venue: SwapVenue
    wallet: str
    mint: str
    side: SwapSide
    input_mint: str
    output_mint: str
    input_amount_raw: int
    output_amount_raw: int
    price: float = 0.0
    usd_value: float = 0.0
    pool: str | None = None
    features: dict[str, float] = Field(default_factory=dict)
    received_at: datetime


__all__ = [
    "EventSource",
    "NormalizedSwap",
    "RawEvent",
    "SwapSide",
    "SwapVenue",
]
