"""Pre-trade safety checks: mint/freeze auth, holder concentration, sell route, liquidity."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx
from solders.pubkey import Pubkey

from .constants import (
    BURN_ADDR,
    JUPITER_QUOTE_URL,
    LAMPORTS_PER_SOL,
    SOL_MINT,
)

log = logging.getLogger("rug_filter")


@dataclass
class FilterResult:
    ok: bool
    reason: str
    top10_pct: float = 0.0
    has_sell_route: bool = False
    mint_authority: Optional[str] = None
    freeze_authority: Optional[str] = None
    lp_burned_or_locked: Optional[bool] = None


async def _check_authorities(client, mint: str) -> tuple[bool, str, Optional[str], Optional[str]]:
    try:
        resp = await client.get_account_info_json_parsed(Pubkey.from_string(mint))
    except Exception as e:
        return False, f"acct_info_err:{e!r}", None, None
    val = getattr(resp, "value", None)
    if val is None:
        return False, "no_account", None, None
    try:
        info = val.data.parsed["info"]
    except Exception:
        return False, "not_parsed", None, None
    ma = info.get("mintAuthority")
    fa = info.get("freezeAuthority")
    if ma:
        return False, "mint_authority_active", ma, fa
    if fa:
        return False, "freeze_authority_active", ma, fa
    return True, "ok", ma, fa


async def _check_concentration(client, mint: str) -> tuple[bool, str, float]:
    try:
        resp = await client.get_token_largest_accounts(Pubkey.from_string(mint))
        supply_resp = await client.get_token_supply(Pubkey.from_string(mint))
    except Exception as e:
        return False, f"holders_err:{e!r}", 0.0
    accs = getattr(resp, "value", []) or []
    supply_val = getattr(supply_resp, "value", None)
    if supply_val is None or float(supply_val.amount or 0) == 0:
        return False, "no_supply", 0.0
    supply = float(supply_val.amount)
    top10_amt = sum(float(a.amount) for a in accs[:10])
    pct = top10_amt / supply
    if pct > 0.30:
        # Check if the largest holder is a burn/locked LP — common for valid memecoins
        top1_owner_amt = float(accs[0].amount) if accs else 0
        top1_pct = top1_owner_amt / supply
        # If top1 alone is >25%, almost certainly an LP/vault — only acceptable if it's burn addr
        # We don't have owner from largest_accounts (only token account); approximate via threshold
        if pct - top1_pct > 0.30:  # even excluding top1, still concentrated → bad
            return False, f"holder_concentration:{pct:.2%}", pct
    return True, "ok", pct


async def _check_sell_route(http: httpx.AsyncClient, mint: str, probe_lamports: int = 100_000) -> tuple[bool, str]:
    """Get a Jupiter sell quote for a tiny amount; if no route, mark honeypot."""
    try:
        r = await http.get(
            JUPITER_QUOTE_URL,
            params={
                "inputMint": mint,
                "outputMint": SOL_MINT,
                "amount": probe_lamports,
                "slippageBps": 1500,
                "swapMode": "ExactIn",
                "onlyDirectRoutes": "false",
            },
            timeout=4.0,
        )
        if r.status_code != 200:
            return False, f"sell_quote_http_{r.status_code}"
        data = r.json()
        if not data or "outAmount" not in data or int(data.get("outAmount", 0)) == 0:
            return False, "no_sell_route"
        return True, "ok"
    except Exception as e:
        return False, f"sell_quote_err:{type(e).__name__}"


async def _check_liquidity(http: httpx.AsyncClient, mint: str, min_sol: float = 0.5) -> tuple[bool, str, float]:
    """Use a buy-quote of `min_sol` SOL → token; if route exists with reasonable price impact, OK.

    `min_sol` defaults to 0.5 SOL; price impact <50% on this size = liquidity ≥~$20k for typical pairs.
    """
    try:
        amount = int(min_sol * LAMPORTS_PER_SOL)
        r = await http.get(
            JUPITER_QUOTE_URL,
            params={
                "inputMint": SOL_MINT,
                "outputMint": mint,
                "amount": amount,
                "slippageBps": 5000,
                "swapMode": "ExactIn",
                "onlyDirectRoutes": "false",
            },
            timeout=4.0,
        )
        if r.status_code != 200:
            return False, f"liq_quote_http_{r.status_code}", 0.0
        data = r.json()
        pi = float(data.get("priceImpactPct", 1.0) or 1.0)
        if pi > 0.50:
            return False, f"price_impact:{pi:.2%}", pi
        return True, "ok", pi
    except Exception as e:
        return False, f"liq_err:{type(e).__name__}", 0.0


async def run_checks(client, http: httpx.AsyncClient, mint: str) -> FilterResult:
    """Parallel safety checks. Bails early if any critical check fails."""
    auth_t = asyncio.create_task(_check_authorities(client, mint))
    conc_t = asyncio.create_task(_check_concentration(client, mint))
    sell_t = asyncio.create_task(_check_sell_route(http, mint))
    liq_t = asyncio.create_task(_check_liquidity(http, mint))

    auth_ok, auth_reason, ma, fa = await auth_t
    if not auth_ok:
        for t in (conc_t, sell_t, liq_t):
            t.cancel()
        return FilterResult(False, auth_reason, mint_authority=ma, freeze_authority=fa)

    conc_ok, conc_reason, pct = await conc_t
    if not conc_ok:
        for t in (sell_t, liq_t):
            t.cancel()
        return FilterResult(False, conc_reason, top10_pct=pct, mint_authority=ma, freeze_authority=fa)

    sell_ok, sell_reason = await sell_t
    if not sell_ok:
        liq_t.cancel()
        return FilterResult(False, sell_reason, top10_pct=pct, has_sell_route=False,
                            mint_authority=ma, freeze_authority=fa)

    liq_ok, liq_reason, _pi = await liq_t
    if not liq_ok:
        return FilterResult(False, liq_reason, top10_pct=pct, has_sell_route=True,
                            mint_authority=ma, freeze_authority=fa)

    return FilterResult(True, "ok", top10_pct=pct, has_sell_route=True,
                        mint_authority=ma, freeze_authority=fa)
