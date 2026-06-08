"""Route selector -- picks between Jupiter and Raydium for a single intent.

Default policy:
  * In `LIVE` and `PAPER` modes, query Jupiter first; if it errors, fall back
    to Raydium (when enabled).
  * In `DEGRADED_EXEC` mode (Jupiter probe down), query Raydium first.
  * The winning quote must satisfy
        price_impact_pct <= risk.max_price_impact_ceiling_pct
    -- and the hard ceiling is asserted here, not just in config validation.

`RouteSelector.best_quote()` returns the chosen `Quote` or raises
`RouteUnavailable` if every venue failed or every quote violated risk
ceilings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from solalpha.foundation.errors import (
    ExecutionError,
    JupiterError,
    RaydiumError,
    RouteUnavailable,
)
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.execution.base import Quote
    from solalpha.execution.jupiter import JupiterClient
    from solalpha.execution.raydium import RaydiumClient
    from solalpha.foundation.config import AppConfig
    from solalpha.signal.mode_manager import ModeManager

_log = get_logger(__name__)


class RouteSelector:
    """Stateless venue picker. Constructed once at startup."""

    def __init__(
        self,
        cfg: AppConfig,
        mode_manager: ModeManager,
        jupiter: JupiterClient,
        raydium: RaydiumClient | None,
    ) -> None:
        self._cfg = cfg
        self._mode = mode_manager
        self._jupiter = jupiter
        self._raydium = raydium

    async def best_quote(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int,
    ) -> Quote:
        order: list[str] = self._preferred_order()
        last_err: Exception | None = None
        for venue in order:
            try:
                quote = await self._quote_one(
                    venue,
                    input_mint=input_mint,
                    output_mint=output_mint,
                    amount_raw=amount_raw,
                    slippage_bps=slippage_bps,
                )
            except ExecutionError as e:
                _log.warning("route_quote_failed", venue=venue, exc=str(e))
                last_err = e
                continue
            if not self._passes_risk(quote):
                _log.info(
                    "route_quote_rejected_by_risk",
                    venue=venue,
                    price_impact_pct=quote.price_impact_pct,
                )
                continue
            _log.info(
                "route_selected",
                venue=venue,
                price_impact_pct=quote.price_impact_pct,
                out_amount_raw=quote.out_amount_raw,
            )
            return quote
        if last_err is not None:
            raise RouteUnavailable(
                f"no venue produced an acceptable quote: {last_err}"
            ) from last_err
        raise RouteUnavailable("no venue produced an acceptable quote")

    # ---- internals ----

    def _preferred_order(self) -> list[str]:
        if self._mode.mode == "DEGRADED_EXEC" and self._raydium is not None:
            return ["raydium", "jupiter"]
        if self._raydium is None:
            return ["jupiter"]
        return ["jupiter", "raydium"]

    async def _quote_one(
        self,
        venue: str,
        *,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int,
    ) -> Quote:
        if venue == "jupiter":
            return await self._jupiter.quote(
                input_mint=input_mint,
                output_mint=output_mint,
                amount_raw=amount_raw,
                slippage_bps=slippage_bps,
            )
        if venue == "raydium":
            if self._raydium is None:
                raise RaydiumError("raydium client not configured")
            return await self._raydium.quote(
                input_mint=input_mint,
                output_mint=output_mint,
                amount_raw=amount_raw,
                slippage_bps=slippage_bps,
            )
        raise JupiterError(f"unknown venue {venue!r}")

    def _passes_risk(self, quote: Quote) -> bool:
        # Re-assert the hard ceilings here so a misconfigured
        # `max_price_impact_pct` cannot loosen what the config validator allowed.
        risk = self._cfg.risk
        return not (
            quote.price_impact_pct > risk.max_price_impact_ceiling_pct
            or quote.slippage_bps > risk.hard_slippage_ceiling_bps
        )


__all__ = ["RouteSelector"]
