"""Pull parsed swap/transfer txns from Helius enhanced API."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

import httpx

from .. import storage

log = logging.getLogger("collector.helius")

ENHANCED_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"


def _classify_swap(tx: dict, address: str) -> dict | None:
    """Extract a swap row from a parsed Helius tx for `address`. None if not a swap."""
    events = tx.get("events") or {}
    swap = events.get("swap")
    if not swap:
        return None
    ts = float(tx.get("timestamp", 0))
    slot = int(tx.get("slot", 0))
    sig = tx.get("signature", "")
    inputs = swap.get("nativeInput") or {}
    outputs = swap.get("nativeOutput") or {}
    token_inputs = swap.get("tokenInputs") or []
    token_outputs = swap.get("tokenOutputs") or []

    # Heuristic: side=buy if address received a non-SOL token (non-empty token_outputs for it)
    side = None
    mint = None
    sol_amount = 0.0
    token_amount = 0.0
    for tio in token_outputs:
        if tio.get("userAccount") == address:
            side = 0  # buy (received tokens)
            mint = tio.get("mint")
            token_amount = float(tio.get("rawTokenAmount", {}).get("tokenAmount", 0))
            break
    if side is None:
        for tii in token_inputs:
            if tii.get("userAccount") == address:
                side = 1  # sell (sent tokens)
                mint = tii.get("mint")
                token_amount = float(tii.get("rawTokenAmount", {}).get("tokenAmount", 0))
                break
    if side is None or not mint:
        return None
    if inputs.get("account") == address:
        sol_amount = float(inputs.get("amount", 0)) / 1e9
    if outputs.get("account") == address:
        sol_amount = float(outputs.get("amount", 0)) / 1e9
    price_sol = (sol_amount / token_amount) if token_amount > 0 else 0.0
    dex = (tx.get("source") or "unknown").lower()
    return {
        "ts": ts, "slot": slot, "sig": sig, "mint": mint, "wallet": address,
        "side": int(side), "sol_amount": sol_amount, "token_amount": token_amount,
        "price_sol": price_sol, "dex": dex,
    }


def _classify_transfer(tx: dict, tracked: set[str]) -> dict | None:
    """Pick out SOL transfers between tracked wallets."""
    nt = tx.get("nativeTransfers") or []
    for tr in nt:
        f = tr.get("fromUserAccount")
        t = tr.get("toUserAccount")
        if not f or not t:
            continue
        if f in tracked or t in tracked:
            return {
                "ts": float(tx.get("timestamp", 0)),
                "sig": tx.get("signature", ""),
                "src": f, "dst": t,
                "sol_amount": float(tr.get("amount", 0)) / 1e9,
            }
    return None


async def fetch_address_txns(http: httpx.AsyncClient, api_key: str, address: str,
                             before: str | None = None, until: str | None = None,
                             limit: int = 100) -> tuple[list[dict], str | None]:
    """One page. Returns (txns, oldest_signature_for_pagination)."""
    params = {"api-key": api_key, "limit": limit}
    if before:
        params["before"] = before
    if until:
        params["until"] = until
    url = ENHANCED_TX_URL.format(address=address)
    backoff = 1.0
    while True:
        r = await http.get(url, params=params, timeout=30.0)
        if r.status_code == 429:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        if r.status_code != 200:
            log.warning(f"helius {r.status_code}: {r.text[:200]}")
            return [], None
        data = r.json() or []
        if not data:
            return [], None
        oldest = data[-1].get("signature")
        return data, oldest


async def collect_address(
    http: httpx.AsyncClient,
    api_key: str,
    address: str,
    *,
    start_ts: float,
    end_ts: float,
    tracked_wallets: set[str] | None = None,
) -> dict[str, int]:
    """Collect all swap+transfer rows for `address` in [start_ts, end_ts]. Returns counts."""
    swap_rows: list[dict] = []
    transfer_rows: list[dict] = []
    before: str | None = None
    while True:
        page, oldest = await fetch_address_txns(http, api_key, address, before=before, limit=100)
        if not page:
            break
        oldest_ts = float(page[-1].get("timestamp", 0))
        for tx in page:
            ts = float(tx.get("timestamp", 0))
            if ts < start_ts or ts > end_ts:
                continue
            sw = _classify_swap(tx, address)
            if sw:
                swap_rows.append(sw)
            if tracked_wallets:
                tr = _classify_transfer(tx, tracked_wallets)
                if tr:
                    transfer_rows.append(tr)
        if oldest_ts < start_ts:
            break
        before = oldest
    counts: dict[str, int] = {}
    if swap_rows:
        for w in storage.append_rows("swaps", swap_rows, source=f"helius:{address}"):
            counts.setdefault("swaps", 0)
            counts["swaps"] += w.rows
    if transfer_rows:
        for w in storage.append_rows("transfers", transfer_rows, source=f"helius:{address}"):
            counts.setdefault("transfers", 0)
            counts["transfers"] += w.rows
    return counts


async def collect_addresses(api_key: str, addresses: Iterable[str], start_ts: float, end_ts: float,
                            tracked_wallets: set[str] | None = None) -> dict:
    """Collect for many addresses concurrently (semaphore-limited)."""
    sem = asyncio.Semaphore(4)
    out = {"per_address": {}, "total": {}}

    async def _one(addr):
        async with sem:
            async with httpx.AsyncClient() as h:
                c = await collect_address(h, api_key, addr,
                                          start_ts=start_ts, end_ts=end_ts,
                                          tracked_wallets=tracked_wallets)
                out["per_address"][addr] = c
                for k, v in c.items():
                    out["total"][k] = out["total"].get(k, 0) + v

    await asyncio.gather(*[_one(a) for a in addresses])
    return out
