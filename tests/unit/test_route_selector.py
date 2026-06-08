"""RouteSelector: venue preference, fallback, and hard risk-ceiling re-assertion."""

from __future__ import annotations

from typing import Any

import pytest

from solalpha.execution.base import Quote
from solalpha.execution.route_selector import RouteSelector
from solalpha.foundation.errors import JupiterError, RaydiumError, RouteUnavailable

pytestmark = pytest.mark.unit

WSOL = "So11111111111111111111111111111111111111112"
MINT = "Mint1111111111111111111111111111111111111111"


class _Mode:
    def __init__(self, mode: str = "PAPER") -> None:
        self.mode = mode


class _Venue:
    def __init__(self, *, quote: Quote | None = None, exc: Exception | None = None) -> None:
        self._quote = quote
        self._exc = exc
        self.calls = 0

    async def quote(
        self, *, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int
    ) -> Quote:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        assert self._quote is not None
        return self._quote


def _q(venue: str = "jupiter", impact: float = 0.01, slippage: int = 50, out: int = 1000) -> Quote:
    return Quote(
        venue=venue,
        input_mint=WSOL,
        output_mint=MINT,
        in_amount_raw=100,
        out_amount_raw=out,
        other_amount_threshold=out,
        price_impact_pct=impact,
        slippage_bps=slippage,
        raw_response={},
    )


def _sel(cfg: Any, jup: _Venue, ray: _Venue | None, mode: str = "PAPER") -> RouteSelector:
    return RouteSelector(cfg, _Mode(mode), jup, ray)  # type: ignore[arg-type]


async def _best(sel: RouteSelector) -> Quote:
    return await sel.best_quote(input_mint=WSOL, output_mint=MINT, amount_raw=1000, slippage_bps=50)


async def test_prefers_jupiter(app_config: Any) -> None:
    jup, ray = _Venue(quote=_q("jupiter")), _Venue(quote=_q("raydium"))
    q = await _best(_sel(app_config, jup, ray))
    assert q.venue == "jupiter"
    assert jup.calls == 1 and ray.calls == 0  # raydium never consulted


async def test_falls_back_to_raydium_on_jupiter_error(app_config: Any) -> None:
    jup = _Venue(exc=JupiterError("jup 5xx"))
    ray = _Venue(quote=_q("raydium"))
    q = await _best(_sel(app_config, jup, ray))
    assert q.venue == "raydium"
    assert jup.calls == 1 and ray.calls == 1


async def test_degraded_exec_queries_raydium_first(app_config: Any) -> None:
    jup, ray = _Venue(quote=_q("jupiter")), _Venue(quote=_q("raydium"))
    q = await _best(_sel(app_config, jup, ray, mode="DEGRADED_EXEC"))
    assert q.venue == "raydium"
    assert ray.calls == 1 and jup.calls == 0


async def test_raydium_none_and_jupiter_error_raises(app_config: Any) -> None:
    jup = _Venue(exc=JupiterError("down"))
    with pytest.raises(RouteUnavailable):
        await _best(_sel(app_config, jup, None))


async def test_all_quotes_rejected_by_impact(app_config: Any) -> None:
    # ceiling is 0.05; both quotes exceed it -> rejected (no error -> generic RouteUnavailable)
    jup = _Venue(quote=_q("jupiter", impact=0.06))
    ray = _Venue(quote=_q("raydium", impact=0.06))
    with pytest.raises(RouteUnavailable):
        await _best(_sel(app_config, jup, ray))
    assert jup.calls == 1 and ray.calls == 1  # both tried, both rejected


async def test_rejected_by_slippage_then_fallback(app_config: Any) -> None:
    jup = _Venue(quote=_q("jupiter", slippage=400))  # > hard ceiling 300
    ray = _Venue(quote=_q("raydium", slippage=50))
    q = await _best(_sel(app_config, jup, ray))
    assert q.venue == "raydium"


async def test_both_venues_error_raises_from_last(app_config: Any) -> None:
    jup = _Venue(exc=JupiterError("jup down"))
    ray = _Venue(exc=RaydiumError("ray down"))
    with pytest.raises(RouteUnavailable):
        await _best(_sel(app_config, jup, ray))


async def test_impact_exactly_at_ceiling_passes(app_config: Any) -> None:
    jup = _Venue(quote=_q("jupiter", impact=0.05, slippage=300))  # == ceilings -> not >
    q = await _best(_sel(app_config, jup, None))
    assert q.venue == "jupiter"


async def test_quote_one_unknown_venue_raises(app_config: Any) -> None:
    sel = _sel(app_config, _Venue(quote=_q()), None)
    with pytest.raises(JupiterError):
        await sel._quote_one(
            "weird", input_mint=WSOL, output_mint=MINT, amount_raw=1, slippage_bps=1
        )


async def test_quote_one_raydium_none_raises(app_config: Any) -> None:
    sel = _sel(app_config, _Venue(quote=_q()), None)
    with pytest.raises(RaydiumError):
        await sel._quote_one(
            "raydium", input_mint=WSOL, output_mint=MINT, amount_raw=1, slippage_bps=1
        )
