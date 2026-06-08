"""Execution-plane protocols + value types.

Everything that turns a `Signal` (via an `OrderIntent`) into a `Fill` flows
through an `Executor`. The paper-mode and live-mode implementations share
this surface so the rest of the system never branches on mode.

`Quote` is the venue-agnostic price quote the route selector compares.
`SwapInstructions` is the raw serialized payload returned by Jupiter /
Raydium that the tx builder consumes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from solalpha.domain import Fill, OrderIntent

QuoteVenue = Literal["jupiter", "raydium"]


class Quote(BaseModel):
    """Venue-agnostic quote -- everything the route selector needs to rank."""

    model_config = ConfigDict(frozen=True)

    venue: QuoteVenue
    input_mint: str
    output_mint: str
    in_amount_raw: int
    out_amount_raw: int
    other_amount_threshold: int
    price_impact_pct: float
    slippage_bps: int
    # `raw_response` keeps the original quote payload so the swap call can
    # round-trip it back to Jupiter (which signs the swap based on its own
    # opaque quote id) without re-quoting.
    raw_response: dict[str, object]
    route_plan: tuple[str, ...] = ()


class SwapInstructions(BaseModel):
    """Serialized swap-instructions payload returned by a venue's REST API."""

    model_config = ConfigDict(frozen=True)

    venue: QuoteVenue
    # Each list element is a base64-encoded serialized Solana instruction.
    setup_instructions: tuple[str, ...] = ()
    swap_instruction: str = ""
    cleanup_instructions: tuple[str, ...] = ()
    # `address_lookup_tables` is the list of ALT addresses needed by the
    # versioned transaction (base58 pubkeys).
    address_lookup_tables: tuple[str, ...] = ()
    # `compute_unit_limit` recommended by the venue; the tx builder may
    # override / extend with the configured priority fee.
    compute_unit_limit: int | None = None


class Executor(Protocol):
    """The single API the execution pipeline calls per approved `OrderIntent`."""

    name: str

    async def execute(self, intent: OrderIntent) -> Fill:
        """Run the order through to a confirmed fill, or raise on failure.

        Implementations must always log via `TradeLog` and update inflight
        bookkeeping on the `RiskEngine` (via the runtime wiring) regardless
        of success/failure.
        """


__all__ = ["Executor", "Quote", "QuoteVenue", "SwapInstructions"]
