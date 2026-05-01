"""Raydium-only fallback. Uses Jupiter aggregator with `dexes=Raydium` filter so the
underlying route is restricted to Raydium pools only. This gives us fallback semantics
(skip Jupiter's broader aggregation) without re-implementing AMM v4 instruction encoding.
"""
from __future__ import annotations

from typing import Optional

import httpx

from . import execution_jupiter
from .types import Quote


async def quote(
    http: httpx.AsyncClient,
    in_mint: str,
    out_mint: str,
    amount: int,
    slippage_bps: int,
) -> Optional[Quote]:
    q = await execution_jupiter.quote(
        http,
        in_mint,
        out_mint,
        amount,
        slippage_bps,
        only_direct_routes=True,
        dexes="Raydium",
    )
    if q is None:
        return None
    return Quote(
        in_mint=q.in_mint,
        out_mint=q.out_mint,
        in_amount=q.in_amount,
        out_amount=q.out_amount,
        price_impact_pct=q.price_impact_pct,
        route=q.route or "raydium",
        source="raydium",
        raw=q.raw,
    )


async def swap_tx(
    http: httpx.AsyncClient,
    quote_raw: dict,
    user_pubkey: str,
    *,
    prio_lamports: object = "auto",
) -> Optional[str]:
    return await execution_jupiter.swap_tx(http, quote_raw, user_pubkey, prio_lamports=prio_lamports)
