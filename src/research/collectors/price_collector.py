"""Birdeye OHLCV (1m bars) collector. Free tier: ~30k req/mo."""
from __future__ import annotations

import asyncio
import logging

import httpx

from .. import storage

log = logging.getLogger("collector.price")

BIRDEYE_HISTORY_URL = "https://public-api.birdeye.so/defi/history_price"


async def fetch_bars(http: httpx.AsyncClient, mint: str, start_ts: int, end_ts: int,
                     api_key: str | None = None, resolution: str = "1m") -> list[dict]:
    headers = {"x-chain": "solana"}
    if api_key:
        headers["X-API-KEY"] = api_key
    params = {
        "address": mint,
        "address_type": "token",
        "type": resolution,
        "time_from": int(start_ts),
        "time_to": int(end_ts),
    }
    backoff = 1.0
    while True:
        r = await http.get(BIRDEYE_HISTORY_URL, params=params, headers=headers, timeout=15.0)
        if r.status_code == 429:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        if r.status_code != 200:
            log.warning(f"birdeye {r.status_code}: {r.text[:200]}")
            return []
        items = (r.json() or {}).get("data", {}).get("items", []) or []
        out = []
        for b in items:
            ts = float(b.get("unixTime", 0))
            value = float(b.get("value", 0))   # already in SOL? Birdeye returns USD; user must convert
            out.append({
                "ts": ts, "mint": mint, "sol_per_token": value,
                "source": "birdeye", "bar_seconds": 60,
            })
        return out


async def collect_mints(api_key: str | None, mints: list[str], start_ts: float, end_ts: float) -> dict:
    sem = asyncio.Semaphore(2)
    total = 0

    async def _one(m):
        nonlocal total
        async with sem:
            async with httpx.AsyncClient() as h:
                bars = await fetch_bars(h, m, int(start_ts), int(end_ts), api_key=api_key)
                if bars:
                    for w in storage.append_rows("prices", bars, source=f"birdeye:{m}"):
                        total += w.rows

    await asyncio.gather(*[_one(m) for m in mints])
    return {"prices_rows": total}
