"""Jupiter v6 quote + swap-tx HTTP client. Async with timeouts + exp backoff."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from .types import Quote

log = logging.getLogger("execution.jupiter")

QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
SWAP_URL = "https://quote-api.jup.ag/v6/swap"
QUOTE_TIMEOUT_S = 5.0
SWAP_TIMEOUT_S = 8.0
MAX_RETRIES = 2


async def _backoff(attempt: int) -> None:
    await asyncio.sleep(0.5 * (1.5 ** attempt))


async def quote(
    http: httpx.AsyncClient,
    in_mint: str,
    out_mint: str,
    amount: int,
    slippage_bps: int,
    *,
    only_direct_routes: bool = False,
    dexes: Optional[str] = None,
) -> Optional[Quote]:
    params = {
        "inputMint": in_mint,
        "outputMint": out_mint,
        "amount": str(amount),
        "slippageBps": str(slippage_bps),
        "swapMode": "ExactIn",
        "onlyDirectRoutes": str(only_direct_routes).lower(),
        "asLegacyTransaction": "false",
        "maxAccounts": "64",
    }
    if dexes:
        params["dexes"] = dexes

    last_err: Optional[str] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = await http.get(QUOTE_URL, params=params, timeout=QUOTE_TIMEOUT_S)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = f"transport:{type(e).__name__}"
            if attempt < MAX_RETRIES:
                await _backoff(attempt)
                continue
            return None
        if r.status_code == 200:
            data = r.json() or {}
            route_plan = data.get("routePlan") or []
            out_amount = int(data.get("outAmount", 0) or 0)
            if not route_plan or out_amount <= 0:
                return None
            try:
                pi = float(data.get("priceImpactPct", 0) or 0)
            except ValueError:
                pi = 0.0
            route_str = ",".join(
                str(rp.get("swapInfo", {}).get("label", "?"))
                for rp in route_plan
                if "swapInfo" in rp
            )
            return Quote(
                in_mint=in_mint,
                out_mint=out_mint,
                in_amount=amount,
                out_amount=out_amount,
                price_impact_pct=pi,
                route=route_str or "jupiter",
                source="jupiter",
                raw=data,
            )
        if r.status_code in (429, 500, 502, 503, 504):
            last_err = f"http_{r.status_code}"
            if attempt < MAX_RETRIES:
                await _backoff(attempt)
                continue
        return None
    log.warning(f"jupiter quote failed: {last_err}")
    return None


async def swap_tx(
    http: httpx.AsyncClient,
    quote_raw: dict,
    user_pubkey: str,
    *,
    prio_lamports: object = "auto",
) -> Optional[str]:
    body = {
        "quoteResponse": quote_raw,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "useSharedAccounts": True,
        "asLegacyTransaction": False,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": prio_lamports,
    }
    last_err: Optional[str] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = await http.post(SWAP_URL, json=body, timeout=SWAP_TIMEOUT_S)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = f"transport:{type(e).__name__}"
            if attempt < MAX_RETRIES:
                await _backoff(attempt)
                continue
            return None
        if r.status_code == 200:
            data = r.json() or {}
            tx_b64 = data.get("swapTransaction")
            return tx_b64 if tx_b64 else None
        if r.status_code in (429, 500, 502, 503, 504):
            last_err = f"http_{r.status_code}"
            if attempt < MAX_RETRIES:
                await _backoff(attempt)
                continue
        return None
    log.warning(f"jupiter swap failed: {last_err}")
    return None
