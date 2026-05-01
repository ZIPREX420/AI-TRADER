"""Pool-event extractor: parses Helius enhanced txns for Raydium init / Pump.fun migrate / LP add-remove."""
from __future__ import annotations

import logging

import httpx

from .. import storage
from ...constants import (
    PUMPFUN, PUMPFUN_AMM, PUMPFUN_MIGRATE_MARKERS,
    RAYDIUM_AMM_V4, RAYDIUM_INIT_LOG_MARKERS,
)
from .helius_collector import fetch_address_txns

log = logging.getLogger("collector.pool")


def _classify_pool_event(tx: dict, program: str) -> dict | None:
    logs = tx.get("instructions") or []
    raw_logs = []
    for ix in logs:
        for inner in (ix.get("innerInstructions") or []):
            raw_logs.append(str(inner))
    msg = " ".join((tx.get("description") or "").splitlines() + raw_logs)
    ts = float(tx.get("timestamp", 0))
    slot = int(tx.get("slot", 0))
    sig = tx.get("signature", "")
    signer = (tx.get("feePayer") or "")
    kind = None
    if program == RAYDIUM_AMM_V4 and any(m in msg for m in RAYDIUM_INIT_LOG_MARKERS):
        kind = "init"
    elif program in (PUMPFUN, PUMPFUN_AMM) and any(m in msg for m in PUMPFUN_MIGRATE_MARKERS):
        kind = "migrate"
    if not kind:
        return None
    # Mint heuristic: pick first non-SOL token in transfers
    mint = ""
    sol_in_pool = 0.0
    for tt in (tx.get("tokenTransfers") or []):
        if tt.get("mint") and tt.get("mint") != "So11111111111111111111111111111111111111112":
            mint = tt["mint"]
            break
    return {
        "ts": ts, "slot": slot, "sig": sig, "mint": mint, "event_kind": kind,
        "source_program": program, "sol_in_pool": sol_in_pool, "lp_holder": "",
        "sol_amount": 0.0, "signer": signer,
    }


async def collect_program(api_key: str, program: str, start_ts: float, end_ts: float) -> dict:
    rows: list[dict] = []
    async with httpx.AsyncClient() as h:
        before: str | None = None
        while True:
            page, oldest = await fetch_address_txns(h, api_key, program, before=before, limit=100)
            if not page:
                break
            oldest_ts = float(page[-1].get("timestamp", 0))
            for tx in page:
                ts = float(tx.get("timestamp", 0))
                if ts < start_ts or ts > end_ts:
                    continue
                ev = _classify_pool_event(tx, program)
                if ev:
                    rows.append(ev)
            if oldest_ts < start_ts:
                break
            before = oldest
    written = 0
    if rows:
        for w in storage.append_rows("pools", rows, source=f"helius:{program}"):
            written += w.rows
    return {"pool_rows": written}
